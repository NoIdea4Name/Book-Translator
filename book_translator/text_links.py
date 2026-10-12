"""Resolve review passages once, then track their ranges without first-match jumps.

Offsets use Python characters. Qt conversion is done only when selecting text.
Uncertain duplicate matches stay unlinked; edited ranges follow known edits.
"""
from collections import defaultdict
from dataclasses import asdict
from difflib import SequenceMatcher
import hashlib
import json
import re


def occurrences(text, quote):
    words = quote.split()
    if not words:
        return []
    pattern = r"\s+".join(re.escape(word) for word in words)
    return [list(match.span()) for match in re.finditer(pattern, text)]


def initial_spans(report):
    groups = defaultdict(list)
    spans = [None] * len(report.regions)
    for index, region in enumerate(report.regions):
        groups[' '.join(region.output_text.split())].append(index)
    for quote, indices in groups.items():
        matches = occurrences(report.text, quote)
        if len(matches) == 1:
            for index in indices:
                spans[index] = matches[0]
        elif matches and len(matches) == len(indices):
            # Only allocate repeated quotes when every occurrence has a distinct
            # location. Include high-confidence and dismissed regions as well.
            boxes = [report.regions[i].box for i in indices]
            if all(boxes) and len(set(boxes)) == len(boxes):
                ordered = sorted(indices, key=lambda i: (report.regions[i].box[0], report.regions[i].box[1]))
                for index, span in zip(ordered, matches):
                    spans[index] = span
    # Unique surrounding passages can disambiguate a repeated quote even when
    # only some occurrences were flagged. Use established ranges as boundaries.
    for index, region in enumerate(report.regions):
        if spans[index] is not None or not region.box:
            continue
        low, high = 0, len(report.text)
        for other, span in zip(report.regions, spans):
            if span is None or not other.box:
                continue
            if other.box[2] <= region.box[0]:
                low = max(low, span[1])
            elif other.box[0] >= region.box[2]:
                high = min(high, span[0])
        matches = [span for span in occurrences(report.text, region.output_text) if low <= span[0] and span[1] <= high]
        if len(matches) == 1:
            spans[index] = matches[0]
    return spans


def apply_edit(spans, position, removed, added):
    """Move ranges through a known edit; changes within a range stay linked."""
    result = []
    edit_end = position + removed
    for span in spans:
        if span is None:
            result.append(None)
            continue
        start, end = span
        if removed == 0:
            if position < start or (position == start and start != end):
                start += added
                end += added
            elif start <= position <= end:
                end += added
        elif edit_end <= start:
            start += added - removed
            end += added - removed
        elif position < end:
            start = min(start, position)
            end = end + added - removed if end > edit_end else position + added
        result.append([start, max(start, end)])
    return result


def move_spans(old, new, spans):
    if old == new or not any(spans):
        return spans
    # Fallback for edits made outside the editor, including find/replace.
    # Keep ambiguous duplicate deletions unlinked rather than guessing which copy.
    ambiguous = set()
    for index, span in enumerate(spans):
        if span and span[0] != span[1]:
            quote = old[span[0]:span[1]]
            before, after = occurrences(old, quote), occurrences(new, quote)
            if len(before) > 1 and len(after) < len(before):
                ambiguous.add(index)
    moved = [None if i in ambiguous else span for i, span in enumerate(spans)]
    if len(old) * len(new) > 4_000_000:
        # Large repetitive OCR output can make exact character diff quadratic.
        # Preserve provably unchanged edges; do not guess links in the middle.
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(old), len(new)) - prefix and old[-suffix-1] == new[-suffix-1]:
            suffix += 1
        boundary = len(old) - suffix
        delta = len(new) - len(old)
        return [None if span is None else span if span[1] <= prefix else
                [span[0]+delta, span[1]+delta] if span[0] >= boundary else None
                for span in moved]
    changes = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, start, end, new_start, new_end in reversed(changes):
        if tag != 'equal':
            moved = apply_edit(moved, start, end - start, new_end - new_start)
    return moved


def track_editor_edit(page, kind, old, new, changes):
    report = page.extraction_assessment if kind == 'source' else page.translation_assessment
    if report is None:
        return
    spans = linked_spans(page, kind, report, old)
    if len(changes) == 1:
        position, removed, added = changes[0]
        # QTextDocument counts UTF-16 code units; the stored ranges use Python indices.
        old_bytes, new_bytes = old.encode('utf-16-le'), new.encode('utf-16-le')
        try:
            start = len(old_bytes[:position * 2].decode('utf-16-le'))
            end = len(old_bytes[:(position + removed) * 2].decode('utf-16-le'))
            insert = new_bytes[position * 2:(position + added) * 2].decode('utf-16-le')
            if old[:start] + insert + old[end:] == new:
                spans = apply_edit(spans, start, end - start, len(insert))
            else:
                spans = move_spans(old, new, spans)
        except UnicodeDecodeError:
            spans = move_spans(old, new, spans)
    else:
        spans = move_spans(old, new, spans)
    page.review_links[kind].update(text=new, spans=spans)


def linked_spans(page, kind, report, current):
    signature = hashlib.sha256(json.dumps(asdict(report), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    entry = page.review_links.get(kind)
    if not entry or entry['signature'] != signature or len(entry['spans']) != len(report.regions):
        entry = dict(signature=signature, text=report.text, spans=initial_spans(report))
    elif any(span is None for span in entry['spans']):
        # Older versions discarded links as soon as their passage was edited.
        # Recover those that the original assessment can still locate safely.
        recovered = move_spans(report.text, entry['text'], initial_spans(report))
        entry['spans'] = [old if old is not None else restored
                          for old, restored in zip(entry['spans'], recovered)]
    spans = move_spans(entry['text'], current, entry['spans'])
    page.review_links[kind] = dict(signature=signature, text=current, spans=spans)
    return spans


def validate_links(value):
    if not isinstance(value, dict) or any(key not in ('source', 'translation') for key in value):
        raise ValueError('Invalid review text links.')
    for entry in value.values():
        if not isinstance(entry, dict) or set(entry) != {'signature', 'text', 'spans'}:
            raise ValueError('Invalid review text links.')
        if not isinstance(entry['signature'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['signature']) or not isinstance(entry['text'], str) or not isinstance(entry['spans'], list) or len(entry['spans']) > 2000:
            raise ValueError('Invalid review text links.')
        for span in entry['spans']:
            if span is not None and (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span) or not 0 <= span[0] <= span[1] <= len(entry['text'])):
                raise ValueError('Invalid review text range.')
    return value
