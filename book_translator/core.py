"""Document handling and project persistence, independent of the interface.

Keep all PyMuPDF operations on the main thread. Only AI requests run in
background threads; those receive immutable text/image snapshots.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageOps
import pymupdf

from .confidence import Assessment
from .config import CONFIDENCE_THRESHOLD, DEFAULT_LANGUAGE, DEFAULT_MODEL

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class PasswordRequired(ValueError):
    """The PDF requires a password, or the supplied password was incorrect."""

    def __init__(self, path):
        self.path = Path(path)
        super().__init__(f"Enter the password for {self.path.name}.")


@dataclass
class PageState:
    """Editable text and the Gemini assessments saved for one book page."""

    source: str = ""
    translation: str = ""
    translated_from: str = ""
    target_language: str = ""
    extraction_assessment: Assessment | None = None
    translation_assessment: Assessment | None = None
    name: str = ""
    reviewed_signature: str = ""
    manual_flag: bool = False
    dismissed_reviews: list[str] = field(default_factory=list)
    review_links: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def __post_init__(self):
        from .page_history import checkpoint
        if not self.history:
            checkpoint(self, "Initial")

    def review_signature(self):
        """A review belongs to this exact text and confidence assessment."""
        # Exclude history before serialization; asdict(self) deep-copies it.
        data = {key: getattr(self, key) for key in
                ('source', 'translation', 'translated_from', 'target_language')}
        for key in ('extraction_assessment', 'translation_assessment'):
            report = getattr(self, key)
            data[key] = asdict(report) if report is not None else None
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @property
    def is_reviewed(self):
        return bool(self.reviewed_signature) and self.reviewed_signature == self.review_signature()


    @property
    def translation_is_stale(self) -> bool:
        return bool(self.translation) and self.source != self.translated_from


def natural_key(path: Path) -> list:
    return [
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path))
    ]


class BookDocument:
    """An open PDF or image collection, plus the editable state for every page.

    Each reference is (file path, page/frame number). Pages from the same PDF
    share one open document; images open only when rendering the requested page.
    """

    def __init__(self, paths: list[Path], kind: str, refs: list[tuple[Path, int]], pdf=None):
        self.paths = paths
        self.kind = kind
        self.refs = refs
        self.pages = [PageState() for _ in refs]
        self._passwords = {}
        self._text_cache = {}
        self.ai_summary = ""
        self.summary_signature = ""
        self._pdfs = {paths[0]: pdf} if pdf is not None else {}

    @property
    def _pdf(self):
        """The first PDF, retained for callers that work with a single PDF."""
        return next(iter(self._pdfs.values()), None)

    @classmethod
    def open(cls, paths, password="", *, sort_images=True):
        paths = list(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))
        if not paths:
            raise ValueError("Choose a PDF or one or more images.")
        if sort_images:
            paths.sort(key=natural_key)
        book = cls(paths, "images", [])
        try:
            for path in paths:
                if not path.is_file():
                    raise FileNotFoundError(f"Cannot find {path.name}.\nExpected location: {path}")
                if path.suffix.lower() == ".pdf":
                    pdf = pymupdf.open(path)
                    book._pdfs[path] = pdf
                    secret = password.get(path, "") if isinstance(password, dict) else password
                    if pdf.needs_pass and not pdf.authenticate(secret):
                        raise PasswordRequired(path)
                    book._passwords[path] = secret
                    if not pdf.page_count:
                        raise ValueError(f"{path.name} has no pages.")
                    book.refs.extend((path, i) for i in range(pdf.page_count))
                else:
                    if path.suffix.lower() not in IMAGE_EXTENSIONS:
                        raise ValueError(f"Unsupported image: {path.name}")
                    with Image.open(path) as image:
                        count = getattr(image, "n_frames", 1) if path.suffix.lower() in {".tif", ".tiff"} else 1
                        book.refs.extend((path, i) for i in range(count))
            book.pages = [PageState() for _ in book.refs]
            book._refresh_sources()
            return book
        except Exception:
            book.close()
            raise

    def replace_image(self, index, path):
        """Keep text/name, but discard confidence coordinates for the old image."""
        path = Path(path).resolve()
        with Image.open(path) as image:
            image.verify()
        self.refs[index] = (path, 0)
        page = self.pages[index]
        page.dismissed_reviews.clear()
        page.review_links.clear()
        page.extraction_assessment = None
        page.translation_assessment = None
        page.reviewed_signature = ""
        self._refresh_sources()

    def _refresh_sources(self):
        self.paths = list(dict.fromkeys(path for path, _ in self.refs))
        for path in list(self._pdfs):
            if path not in self.paths:
                self._pdfs.pop(path).close()
        pdf_count = sum(path.suffix.lower() == ".pdf" for path in self.paths)
        self.kind = "pdf" if self.paths and pdf_count == len(self.paths) else (
            "mixed" if pdf_count else "images"
        )

    def insert(self, index, other):
        """Transfer validated incoming pages and PDF handles without changing originals."""
        if other is self or not 0 <= index <= len(self.pages):
            raise ValueError("Invalid insertion position.")
        self.refs[index:index] = other.refs
        self.pages[index:index] = other.pages
        for path, pdf in other._pdfs.items():
            if path in self._pdfs:
                pdf.close()
            else:
                self._pdfs[path] = pdf
        self._passwords.update(other._passwords)
        other._passwords = {}
        other._pdfs = {}
        other.refs, other.pages = [], []
        self._refresh_sources()

    def delete(self, index):
        if not 0 <= index < len(self.pages):
            raise IndexError("Page number is out of range.")
        del self.refs[index]
        del self.pages[index]
        self._refresh_sources()

    def reorder(self, order):
        if sorted(order) != list(range(len(self.pages))):
            raise ValueError("Page order must include every page exactly once.")
        self.refs = [self.refs[i] for i in order]
        self.pages = [self.pages[i] for i in order]
        self._refresh_sources()

    def restore_project(self, project):
        """Restore saved source-page references, including duplicates and deletions."""
        if project.refs is None:
            if len(self.pages) != len(project.pages) or self.kind != project.kind:
                raise ValueError("The source document no longer matches the saved project.")
        else:
            available = set(self.refs)
            if any(ref not in available for ref in project.refs):
                raise ValueError("A saved source page is missing from the original files.")
            self.refs = list(project.refs)
            self._refresh_sources()
        self.pages = project.pages
        self.ai_summary = project.ai_summary
        self.summary_signature = project.summary_signature

    @property
    def title(self):
        if not self.paths:
            return "Empty book"
        return self.paths[0].name if len(self.paths) == 1 else f"{len(self.pages)} pages"

    def page_name(self, index):
        return self.pages[index].name or f"Page {index + 1}"

    def page_description(self, index):
        path, frame = self.refs[index]
        if path.suffix.lower() == ".pdf":
            return f"{path.name} · Page {frame + 1}"
        return path.name + (
            f" · Frame {frame + 1}" if path.suffix.lower() in {".tif", ".tiff"} else ""
        )

    def extract_pdf_text(self, index):
        path, frame = self.refs[index]
        if path.suffix.lower() != ".pdf":
            raise ValueError("For images, choose Gemini OCR to extract text.")
        key = (path, frame)
        if key not in self._text_cache:
            if len(self._text_cache) >= 128:
                self._text_cache.pop(next(iter(self._text_cache)))
            self._text_cache[key] = self._pdfs[path][frame].get_text("text", sort=True).strip()
        return self._text_cache[key]

    def render_page(self, index, max_edge=2400):
        if not 0 <= index < len(self.pages):
            raise IndexError("Page number is out of range.")
        path, frame = self.refs[index]
        if path.suffix.lower() == ".pdf":
            page = self._pdfs[path][frame]
            scale = min(3.0, max_edge / max(page.rect.width, page.rect.height, 1))
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False
            )
            return pix.tobytes("png")
        with Image.open(path) as original:
            original.seek(frame)
            image = ImageOps.exif_transpose(original)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, "white")
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            buffer = BytesIO()
            flattened.save(buffer, format="PNG")
            return buffer.getvalue()

    def ocr_image(self, index: int) -> tuple[bytes, str]:
        data = self.render_page(index, 3200)
        # Leave room for base64 encoding under Gemini's inline request limit.
        if len(data) <= 12_000_000:
            return data, "image/png"
        with Image.open(BytesIO(data)) as image:
            buffer = BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=90)
            return buffer.getvalue(), "image/jpeg"

    def close(self):
        for pdf in self._pdfs.values():
            pdf.close()
        self._pdfs.clear()


def atomic_write(path: Path, content: str):
    """A failed write must leave the last saved project intact."""
    path = Path(path).resolve()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_project(
    path: Path,
    book: BookDocument,
    index: int,
    target_language: str,
    model: str,
    confidence_threshold: int = CONFIDENCE_THRESHOLD,
):
    path = Path(path).resolve()
    sources = []
    for source in book.paths:
        try:
            sources.append(Path(os.path.relpath(source, path.parent)).as_posix())
        except ValueError:  # Different drive letters on Windows.
            sources.append(str(source))
    from .page_history import checkpoint
    for page in book.pages:
        checkpoint(page)
    payload = {
        "format": "book-translator",
        "version": 3,
        "files": sources,
        "kind": book.kind,
        "page_refs": [[book.paths.index(source), frame] for source, frame in book.refs],
        "current_page": index,
        "target_language": target_language,
        "model": model,
        "pages": [asdict(page) for page in book.pages],
        "confidence_threshold": confidence_threshold,
        "ai_summary": book.ai_summary,
        "summary_signature": book.summary_signature,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


@dataclass
class ProjectData:
    paths: list[Path]
    pages: list[PageState]
    index: int
    target_language: str
    model: str
    kind: str
    confidence_threshold: int = CONFIDENCE_THRESHOLD
    refs: list[tuple[Path, int]] | None = None
    ai_summary: str = ""
    summary_signature: str = ""


def read_project(path: Path) -> ProjectData:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("format") != "book-translator"
        or data.get("version") not in (1, 2, 3)
    ):
        raise ValueError("This is not a supported Book Translator project.")
    files, pages = data.get("files"), data.get("pages")
    if not isinstance(files, list) or not all(isinstance(p, str) and p for p in files):
        raise ValueError("The project has an invalid source file list.")
    if not isinstance(pages, list) or (not pages and data["version"] < 3):
        raise ValueError("The project has no saved pages.")
    if data.get("kind") not in {"pdf", "images", "mixed"}:
        raise ValueError("The project has an invalid document type.")
    states = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("The project has invalid page data.")
        values = {
            key: page.get(key, "")
            for key in ("source", "translation", "translated_from", "target_language", "name", "reviewed_signature")
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("Saved page text must be a string.")
        for key in ("extraction_assessment", "translation_assessment"):
            values[key] = Assessment.from_dict(page[key]) if page.get(key) is not None else None
        if len(values["name"]) > 120 or "\n" in values["name"] or "\r" in values["name"]:
            raise ValueError("Page names must be a single line of up to 120 characters.")
        signature = values["reviewed_signature"]
        if signature and not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("Invalid saved review status.")
        values["manual_flag"] = page.get("manual_flag", False)
        if type(values["manual_flag"]) is not bool:
            raise ValueError("Invalid saved manual flag.")
        dismissed = page.get("dismissed_reviews", [])
        if not isinstance(dismissed, list) or any(not isinstance(key, str) or not re.fullmatch(r"(source|translation):(page|[0-9]+)", key) for key in dismissed):
            raise ValueError("Invalid saved review dismissals.")
        values["dismissed_reviews"] = list(dict.fromkeys(dismissed))
        from .text_links import validate_links
        values["review_links"] = validate_links(page.get("review_links", {}))
        from .page_history import validate_history
        values["history"] = validate_history(page.get("history", []))
        states.append(PageState(**values))
    index = data.get("current_page", 0)
    if type(index) is not int or not 0 <= index < max(1, len(states)):
        raise ValueError("The saved page number is invalid.")
    target = data.get("target_language", DEFAULT_LANGUAGE)
    model = data.get("model", DEFAULT_MODEL)
    if (
        not isinstance(target, str)
        or not target.strip()
        or not isinstance(model, str)
        or not model.strip()
    ):
        raise ValueError("The project has invalid language or model settings.")
    threshold = data.get("confidence_threshold", CONFIDENCE_THRESHOLD)
    if type(threshold) is not int or not 1 <= threshold <= 100:
        raise ValueError("The confidence threshold must be between 1 and 100.")
    paths = [(path.parent / name).resolve() for name in files]
    refs = None
    if data["version"] == 3:
        raw_refs = data.get("page_refs")
        if not isinstance(raw_refs, list) or len(raw_refs) != len(states):
            raise ValueError("The saved page references are invalid.")
        refs = []
        for ref in raw_refs:
            if (
                not isinstance(ref, list) or len(ref) != 2
                or any(type(value) is not int for value in ref)
                or not 0 <= ref[0] < len(paths) or ref[1] < 0
            ):
                raise ValueError("The saved page reference is invalid.")
            refs.append((paths[ref[0]], ref[1]))
    elif not files:
        raise ValueError("The project has no source files.")
    summary = data.get("ai_summary", "")
    summary_signature = data.get("summary_signature", "")
    if not isinstance(summary, str) or len(summary) > 16000:
        raise ValueError("Invalid book summary (maximum 16,000 characters).")
    if not isinstance(summary_signature, str) or (summary_signature and not re.fullmatch(r"[0-9a-f]{64}", summary_signature)):
        raise ValueError("Invalid summary revision.")
    return ProjectData(
        paths,
        states,
        index,
        target,
        model,
        data["kind"],
        threshold,
        refs,
        summary,
        summary_signature,
    )


def export_text(
    book: BookDocument, mode: str = "translation", *, show_page_headers: bool = True,
    show_placeholders: bool = True
) -> str:
    if mode not in {"translation", "source", "both"}:
        raise ValueError("Unknown export format.")
    sections = []
    for number, page in enumerate(book.pages, 1):
        name = f" · {page.name}" if page.name else ""
        heading = f"=== Page {number}{name} ==="
        source = page.source if page.source.strip() else ("[Not extracted]" if show_placeholders else "")
        translation = page.translation if page.translation.strip() else ("[Not translated]" if show_placeholders else "")
        if mode == "both":
            body = f"SOURCE\n{source}\n\nTRANSLATION ({page.target_language or 'unspecified'})\n{translation}"
        elif mode == "source":
            body = source
        else:
            heading += f" ({page.target_language or 'untranslated'})"
            body = translation
        if mode != "source" and page.translation_is_stale:
            heading += " [Source edited after translation]"
        if not show_page_headers and not body.strip():
            continue
        sections.append(f"{heading}\n\n{body}" if show_page_headers else body)
    return "\n\n\n".join(sections) + "\n" if sections else ""
