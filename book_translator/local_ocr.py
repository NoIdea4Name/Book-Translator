"""Offline Tesseract OCR. Run from a worker, with bounded, cancellable processes.

Tesseract and the requested language data are installed separately. No network
requests, shell commands, or AI credentials are used by this module.
"""
import csv
from io import BytesIO, StringIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image

from .confidence import Assessment, RegionAssessment
from .i18n import tr


def executable_path(configured=''):
    if configured:
        resolved = shutil.which(configured) or (configured if Path(configured).is_file() else None)
    else:
        resolved = shutil.which('tesseract')
        if not resolved and os.name == 'nt':
            candidates = [Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tesseract-OCR/tesseract.exe',
                          Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Tesseract-OCR/tesseract.exe']
            resolved = next((str(path) for path in candidates if path.is_file()), None)
    if not resolved:
        raise RuntimeError(tr('Tesseract was not found. Install Tesseract and its language data, then set its executable in Settings → Local OCR.'))
    return resolved


def parse_tsv(tsv, width, height):
    """Keep reading order and paragraphs, and attach word boxes for review."""
    lines, words, previous_line, previous_paragraph = [], [], None, None
    for row in csv.DictReader(StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        text = row.get('text', '').strip()
        if row.get('level') != '5' or not text:
            continue
        confidence = float(row['conf']) / 100
        if confidence < 0:
            continue
        paragraph = (row['page_num'], row['block_num'], row['par_num'])
        line = paragraph + (row['line_num'],)
        if line != previous_line:
            if lines:
                lines.append('\n\n' if paragraph != previous_paragraph else '\n')
        else:
            lines.append(' ')
        lines.append(text)
        left, top, w, h = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
        box = (top / height * 1000, left / width * 1000,
               (top + h) / height * 1000, (left + w) / width * 1000)
        words.append(RegionAssessment(text, text, min(1., confidence), tr('Tesseract word confidence'),
                                      tuple(max(0., min(1000., value)) for value in box)))
        previous_line, previous_paragraph = line, paragraph
    if not words:
        raise RuntimeError(tr('Local OCR found no text. Check the image and installed OCR languages.'))
    weight = sum(len(word.output_text) for word in words)
    score = sum(word.confidence * len(word.output_text) for word in words) / weight
    # Match the persisted assessment format's limit, prioritizing uncertain words.
    regions = sorted(sorted(enumerate(words), key=lambda item: item[1].confidence)[:2000])
    return Assessment(''.join(lines), score, [word for _, word in regions])


def extract_image(image, executable, languages, cancel, timeout=180):
    if cancel.is_set():
        raise RuntimeError(tr('Request cancelled.'))
    executable = executable_path(executable)
    languages = languages.strip() or 'eng'
    if not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', languages):
        raise RuntimeError(tr('Use OCR language codes such as eng, vie, jpn, or eng+vie.'))
    with Image.open(BytesIO(image)) as source:
        width, height = source.size
    with tempfile.TemporaryDirectory(prefix='book-ocr-') as folder:
        input_path, output_base = Path(folder) / 'page.png', Path(folder) / 'result'
        with Image.open(BytesIO(image)) as source:
            source.convert('RGB').save(input_path)
        # Files avoid pipe-buffer deadlocks for noisy engines or large output.
        with (Path(folder) / 'error.txt').open('w+b') as errors:
            process = subprocess.Popen(
                [executable, str(input_path), str(output_base), '-l', languages, '--psm', '3', 'tsv'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if cancel.wait(.1):
                        raise RuntimeError(tr('Request cancelled.'))
                    if time.monotonic() - started > timeout:
                        raise RuntimeError(tr('Local OCR timed out. Your text was left unchanged.'))
                if cancel.is_set():
                    raise RuntimeError(tr('Request cancelled.'))
                if process.returncode:
                    errors.seek(0)
                    detail = errors.read(4000).decode('utf-8', errors='replace').strip()
                    raise RuntimeError(tr('Local OCR failed. Check that the requested language data is installed.') + '\n' + detail)
                return parse_tsv(output_base.with_suffix('.tsv').read_text(encoding='utf-8'), width, height)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
