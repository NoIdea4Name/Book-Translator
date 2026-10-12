"""A narrow, guarded edit to the preceding page's translation ending."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import json

from .confidence import Assessment, CombinedAssessment, RESULT_SCHEMA, COMBINED_RESULT_SCHEMA
from .memory import MemoryOptions
from .book_context import page_text

TAIL_LIMIT = 1500


@dataclass(frozen=True)
class PreviousPage:
    index: int
    source: str
    translation: str
    translated_from: str
    target_language: str
    source_tail: str


@dataclass(frozen=True)
class ContinuityOptions(MemoryOptions):
    # Request-only snapshot: never assigned to the window's saved options.
    previous_page: PreviousPage | None = None


def continuity_options(book, index, options, target):
    if not (options.fix_previous_page and options.context_enabled and options.pages_before > 0 and index > 0):
        return options
    page = book.pages[index - 1]
    source, _ = page_text(book, index - 1)
    if (not source.strip() or not page.translation.strip() or page.translation_is_stale
            or page.target_language.strip().casefold() != target.strip().casefold()):
        return options
    snapshot = PreviousPage(index - 1, page.source, page.translation, page.translated_from,
                            page.target_language, source[-2000:])
    return ContinuityOptions(**asdict(options), previous_page=snapshot)


def continuity_instruction(snapshot):
    if snapshot is None:
        return ""
    data = {"page": snapshot.index + 1, "source_ending": snapshot.source_tail,
            "translation_ending": snapshot.translation[-TAIL_LIMIT:]}
    return (
        "\n\nPREVIOUS-PAGE CONTINUITY EDIT:\n" + json.dumps(data, ensure_ascii=False)
        + "\nReturn previous_page_edit inside the translation assessment: null if no fix is needed, "
        "otherwise {old_text, new_text, reason}. old_text must quote an exact nonempty suffix "
        "of translation_ending. Fix only that ending to flow naturally into the current page's "
        "translation for continuous narration. Remove an invented 'to be continued' or similar "
        "page-break filler only when it is absent from the source. Preserve genuine source text. "
        "Do not move, repeat, summarize or omit book content across pages. Do not invent the "
        "rest of an unfinished sentence. Keep each page's actual content on its own page; "
        "adjust only necessary boundary phrasing or punctuation. Never rewrite earlier paragraphs. "
        "If uncertain or the boundary is a genuine chapter/section ending, return null. "
        "This explicitly permits a small previous translation edit; the current result still "
        "contains ONLY the current page. Provide a brief reason for any edit."
    )


def continuity_schema(combined=False):
    schema = deepcopy(COMBINED_RESULT_SCHEMA if combined else RESULT_SCHEMA)
    translation = schema["properties"]["translation"] if combined else schema
    translation["properties"]["previous_page_edit"] = {
        "anyOf": [
            {"type": "null"},
            {"type": "object", "properties": {
                "old_text": {"type": "string"}, "new_text": {"type": "string"},
                "reason": {"type": "string"},
            }, "required": ["old_text", "new_text", "reason"], "additionalProperties": False},
        ],
    }
    translation["required"].append("previous_page_edit")
    return schema


class ContinuityAssessment(Assessment):
    """Normal assessment plus transient edit metadata; asdict stores only base fields."""
    def __init__(self, result, snapshot, edit):
        super().__init__(result.text, result.confidence, result.regions)
        self.previous_page = snapshot
        self.previous_edit = edit


def parse_continuity(data, combined, snapshot):
    result = CombinedAssessment.from_dict(data) if combined else Assessment.from_dict(data)
    translation_data = data["translation"] if combined else data
    edit = translation_data.get("previous_page_edit")
    if edit is None:
        return result
    if (not isinstance(edit, dict) or set(edit) != {"old_text", "new_text", "reason"}
            or not all(isinstance(value, str) for value in edit.values())):
        raise ValueError("Invalid previous-page edit.")
    old, new = edit["old_text"], edit["new_text"]
    if (not old or len(old) > TAIL_LIMIT or len(new) > 3000 or len(edit["reason"]) > 1000
            or not snapshot.translation[-TAIL_LIMIT:].endswith(old)):
        raise ValueError("Previous-page edit must match the exact translation ending.")
    if not (snapshot.translation[:-len(old)] + new).strip():
        raise ValueError("A continuity fix cannot erase the previous page.")
    report = result.translation if combined else result
    report = ContinuityAssessment(report, snapshot, edit)
    if combined:
        result.translation = report
        return result
    return report


def apply_previous_edit(book, report):
    """Return (page, before, after, reason), or refuse a stale request snapshot."""
    snapshot = getattr(report, "previous_page", None)
    edit = getattr(report, "previous_edit", None)
    if snapshot is None or edit is None:
        return None
    if not 0 <= snapshot.index < len(book.pages):
        raise ValueError("Previous-page continuity fix skipped: page no longer exists.")
    page = book.pages[snapshot.index]
    if (page.source, page.translation, page.translated_from, page.target_language) != (
            snapshot.source, snapshot.translation, snapshot.translated_from, snapshot.target_language):
        raise ValueError("Previous-page continuity fix skipped: its text changed during the request.")
    before = page.translation
    after = before[:-len(edit["old_text"])] + edit["new_text"]
    if before == after:
        return None
    page.translation = after
    return page, before, after, edit["reason"]
