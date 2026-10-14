"""State for a sequential batch. The window schedules one page at a time."""

from .i18n import tr
from dataclasses import dataclass, field


@dataclass
class BatchRun:
    pages: tuple[int, ...]
    kind: str
    target: str
    offline: bool
    position: int = 0
    completed: int = 0
    issues: list[str] = field(default_factory=list)
    unsuccessful_pages: list[int] = field(default_factory=list)
    continuity_notes: list[str] = field(default_factory=list)
    summary_notes: list[str] = field(default_factory=list)
    paused: bool = False
    cancelled: bool = False
    book: object = None
    items: list = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    memory_before: str = ""
    local_ocr: bool = False

    @property
    def remaining(self):
        return self.pages[self.position:]

    @property
    def summary(self):
        return (
            tr("{done} completed · {failed} skipped or failed · {selected} selected",
               done=self.completed, failed=len(self.issues), selected=len(self.items) if self.items else len(self.pages))
            + (tr(" · {count} summary warning(s)", count=len(self.summary_notes)) if self.summary_notes else "")
            + (tr(" · {count} continuity note(s)", count=len(self.continuity_notes)) if self.continuity_notes else "")
        )


@dataclass
class BatchItem:
    page: object
    index: int
    name: str
    status: str = 'queued'
    activity: str = ''
    attempts: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reports: int = 0
    changes: list = field(default_factory=list)  # (page name, field, before, after)
    notes: list[str] = field(default_factory=list)
    error: str = ''
    logged_notes: int = 0
