"""Validated model estimates and page-relative review regions.

Coordinates always refer to the rendered, orientation-corrected original page,
in [top, left, bottom, right] order on a 0..1000 scale.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from .text_links import linked_spans


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Complete extraction or translation, with paragraph breaks.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Self-estimated confidence in the complete result.",
        },
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_text": {
                        "type": "string",
                        "description": "Exact source passage; do not paraphrase. Empty for direct image-only translation without a source transcript.",
                    },
                    "output_text": {
                        "type": "string",
                        "description": "Exact corresponding passage within text.",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {
                        "type": "string",
                        "description": "Brief, specific explanation for uncertainty, or an empty string.",
                    },
                    "box": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0, "maximum": 1000},
                        "maxItems": 4,
                        "description": "[top,left,bottom,right] on the original page, 0..1000. Use [] when unlocatable.",
                    },
                },
                "required": ["source_text", "output_text", "confidence", "reason", "box"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["text", "confidence", "regions"],
    "additionalProperties": False,
}


COMBINED_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"extraction": RESULT_SCHEMA, "translation": RESULT_SCHEMA},
    "required": ["extraction", "translation"],
    "additionalProperties": False,
}


def valid_score(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Confidence must be a finite number between 0 and 1.")
    return float(value)


def valid_box(value) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) not in (0, 4):
        raise ValueError("A region needs four coordinates or an empty box.")
    if not value:
        return ()
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in value
    ):
        raise ValueError("Region coordinates must be between 0 and 1000.")
    top, left, bottom, right = value
    if top >= bottom or left >= right:
        raise ValueError("Region bounds must have positive width and height.")
    return tuple(float(v) for v in value)


@dataclass
class RegionAssessment:
    source_text: str
    output_text: str
    confidence: float
    reason: str
    box: tuple[float, ...]

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid confidence region.")
        strings = {key: data.get(key) for key in ("source_text", "output_text", "reason")}
        if not all(isinstance(value, str) for value in strings.values()):
            raise ValueError("Region text and explanations must be strings.")
        return cls(
            **strings,
            confidence=valid_score(data.get("confidence")),
            box=valid_box(data.get("box")),
        )


@dataclass
class Assessment:
    text: str
    confidence: float
    regions: list[RegionAssessment]

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise ValueError("The result must contain its complete text.")
        regions = data.get("regions")
        if not isinstance(regions, list) or len(regions) > 2000:
            raise ValueError("Invalid confidence regions.")
        return cls(
            data["text"],
            valid_score(data.get("confidence")),
            [RegionAssessment.from_dict(region) for region in regions],
        )


@dataclass
class CombinedAssessment:
    extraction: Assessment
    translation: Assessment

    @classmethod
    def from_dict(cls, data):
        """Validate both results before either can replace page text."""
        if not isinstance(data, dict):
            raise ValueError("Expected extraction and translation assessments.")
        extraction = Assessment.from_dict(data.get("extraction"))
        translation = Assessment.from_dict(data.get("translation"))
        if not extraction.text.strip() or not translation.text.strip():
            raise ValueError("Both extraction and translation must contain text.")
        return cls(extraction, translation)


@dataclass
class ReviewFlag:
    kind: str
    confidence: float
    source_text: str
    output_text: str
    reason: str
    box: tuple[float, ...]
    page_level: bool = False
    key: str = ""
    outdated: bool = False
    text_span: list[int] | None = None

    @property
    def name(self):
        return "Extraction" if self.kind == "source" else "Translation"

    @property
    def tooltip(self):
        location = (
            "Whole-page estimate"
            if self.page_level
            else (
                "Approximate page location"
                if self.box
                else "Location not found on the original page"
            )
        )
        return (
            f"{self.name} · {self.confidence:.0%} estimated confidence\n{location}\n\n"
            f"Source: {self.source_text or '(whole page)'}\n"
            f"Result: {self.output_text or '(whole page)'}\n\n"
            f"{self.reason or 'Gemini marked this passage as uncertain.'}\n\n"
            "AI self-estimate, not measured accuracy."
            + ("\nText has been edited. This flag describes the original AI result." if self.outdated else "")
        )


def review_flags(page, threshold: float) -> list[ReviewFlag]:
    """Keep original-image review items until individually dismissed or replaced."""
    if page.is_reviewed:
        return []
    flags = []
    candidates = [
        ("source", page.extraction_assessment, page.source, True),
        (
            "translation",
            page.translation_assessment,
            page.translation,
            not page.translation_is_stale,
        ),
    ]
    for kind, report, current_text, valid in candidates:
        if not report:
            continue
        spans = linked_spans(page, kind, report, current_text)
        selected = [region for region in report.regions if region.confidence < threshold]
        for index, region in enumerate(report.regions):
            if region.confidence >= threshold:
                continue
            flags.append(
                ReviewFlag(
                    kind,
                    region.confidence,
                    region.source_text,
                    region.output_text,
                    region.reason,
                    region.box,
                    key=f"{kind}:{index}",
                    text_span=spans[index],
                    outdated=not valid or report.text != current_text,
                )
            )
        if report.confidence < threshold and not selected:
            flags.append(
                ReviewFlag(
                    kind,
                    report.confidence,
                    "",
                    "",
                    "The whole-page score is below the threshold; Gemini did not identify a specific low-confidence passage.",
                    (0, 0, 1000, 1000),
                    page_level=True,
                    key=f"{kind}:page",
                    outdated=not valid or report.text != current_text,
                )
            )
    return [flag for flag in flags if flag.key not in page.dismissed_reviews]


def check_text_anchors(report: Assessment, source: str | None):
    """Do not locate a quote that isn't present in the actual input/output."""
    # Image-only translations have no source transcript to validate against.
    normalized_source = " ".join(source.split()) if source is not None else None
    normalized_output = " ".join(report.text.split())
    for region in report.regions:
        source_quote = " ".join(region.source_text.split())
        output_quote = " ".join(region.output_text.split())
        if (
            (normalized_source is not None and (not source_quote or source_quote not in normalized_source))
            or not output_quote
            or output_quote not in normalized_output
        ):
            region.box = ()
            region.reason = (
                region.reason
                + " The returned quote did not match the supplied/result text; its location was not used."
            ).strip()
