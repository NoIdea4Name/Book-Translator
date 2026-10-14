"""Literal find/replace across page text, with a reversible replacement step."""

from .i18n import tr

from dataclasses import dataclass
import re

from PySide6.QtCore import Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout,
)

from .widgets import button, label


@dataclass(frozen=True)
class Match:
    page: int
    field: str
    start: int
    end: int


def search_pattern(query, case_sensitive=False, whole_word=False):
    if not query:
        return None
    pattern = re.escape(query)
    if whole_word:
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def find_matches(pages, pattern, scope):
    fields = ("source", "translation") if scope == "both" else (scope,)
    if pattern is None:
        return []
    return [
        Match(index, field, match.start(), match.end())
        for index, page in enumerate(pages)
        for field in fields
        for match in pattern.finditer(getattr(page, field))
    ]


def replace_matches(pages, matches, replacement):
    """Apply original offsets backwards; replacement text is always literal."""
    groups = {}
    for match in matches:
        groups.setdefault((match.page, match.field), []).append(match)
    undo = []
    for (index, field), items in groups.items():
        page = pages[index]
        old = getattr(page, field)
        text = old
        for match in sorted(items, key=lambda item: item.start, reverse=True):
            text = text[:match.start] + replacement + text[match.end:]
        if text == old:
            continue
        assessment = "extraction_assessment" if field == "source" else "translation_assessment"
        undo.append((index, field, old, getattr(page, assessment)))
        setattr(page, field, text)
        # Keep original AI review items until individually dismissed.
        # Keep translated_from unchanged so edited source makes translations stale.
    return undo


class FindReplaceDialog(QDialog):
    pages_changed = Signal(object)

    def __init__(self, parent, book):
        super().__init__(parent)
        self.book = book
        self.matches = []
        self.undo_step = []
        self.setWindowTitle(tr('Find and replace'))
        self.setMinimumSize(600, 460)
        self.resize(820, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(label(tr('Find & Replace'), "title"))
        form = QFormLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText(tr('Text to find across all pages'))
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText(tr('Replace with — leave empty to delete matches'))
        form.addRow(tr('Find'), self.find)
        form.addRow(tr('Replace with'), self.replacement)
        layout.addLayout(form)
        options = QHBoxLayout()
        self.scope = QComboBox()
        for title, value in (("Source and translations", "both"), ("Source text", "source"), ("Translations", "translation")):
            self.scope.addItem(tr(title), value)
        self.case_sensitive = QCheckBox(tr('Match case'))
        self.whole_word = QCheckBox(tr('Whole words'))
        for widget in (self.scope, self.case_sensitive, self.whole_word):
            options.addWidget(widget)
        options.addStretch()
        layout.addLayout(options)
        self.summary = label("", "muted")
        layout.addWidget(self.summary)
        split = QSplitter()
        self.results = QListWidget()
        self.results.setAccessibleName(tr('Search matches across all pages'))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(tr('Matching page text'))
        split.addWidget(self.results)
        split.addWidget(self.preview)
        split.setSizes([290, 470])
        layout.addWidget(split, 1)
        navigation = QHBoxLayout()
        self.previous = button(tr('Previous'), lambda: self.navigate(-1))
        self.next = button(tr('Next'), lambda: self.navigate(1))
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        navigation.addStretch()
        self.undo_button = button(tr('Undo last replace'), self.undo)
        navigation.addWidget(self.undo_button)
        layout.addLayout(navigation)
        actions = QHBoxLayout()
        self.status = label("", "muted")
        actions.addWidget(self.status, 1)
        self.replace_button = button(tr('Replace'), self.replace_one)
        self.replace_all_button = button(tr('Replace all'), self.replace_all, "primary")
        actions.addWidget(self.replace_button)
        actions.addWidget(self.replace_all_button)
        close_button = button(tr('Close'), self.accept)
        close_button.setAutoDefault(False)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.find.textChanged.connect(self.invalidate_search)
        self.scope.currentIndexChanged.connect(self.invalidate_search)
        self.case_sensitive.toggled.connect(self.invalidate_search)
        self.whole_word.toggled.connect(self.invalidate_search)
        self.results.currentRowChanged.connect(self.show_match)
        self.find.returnPressed.connect(self.search)
        # Enter in a search field must not trigger a replacement by default.
        for control in (self.previous, self.next, self.undo_button,
                        self.replace_button, self.replace_all_button):
            control.setAutoDefault(False)
        self.invalidate_search()
        self.find.setFocus()

    def invalidate_search(self, *_):
        """Typing never scans the book; stale offsets cannot be replaced."""
        self.matches = []
        self.results.clear()
        self.preview.clear()
        self.summary.setText(tr("Press Enter to search all pages"))
        self.status.clear()
        for control in (self.previous, self.next, self.replace_button, self.replace_all_button):
            control.setEnabled(False)
        self.undo_button.setEnabled(bool(self.undo_step))

    def search(self, *_):
        pattern = search_pattern(self.find.text(), self.case_sensitive.isChecked(), self.whole_word.isChecked())
        self.matches = find_matches(self.book.pages, pattern, self.scope.currentData())
        self.results.clear()
        for match in self.matches:
            text = getattr(self.book.pages[match.page], match.field)
            excerpt = " ".join(text[max(0, match.start - 25):match.end + 45].split())
            self.results.addItem(tr('Page {p0} · {p1}\n{p2}', p0=match.page + 1, p1=match.field.capitalize(), p2=excerpt))
        self.summary.setText(
            tr('{p0} matches across {p1} pages', p0=len(self.matches), p1=len({m.page for m in self.matches}))
        )
        active = bool(self.matches)
        for control in (self.previous, self.next, self.replace_button, self.replace_all_button):
            control.setEnabled(active)
        self.undo_button.setEnabled(bool(self.undo_step))
        self.preview.clear()
        self.status.clear()
        if active:
            self.results.setCurrentRow(0)

    def show_match(self, row):
        if not 0 <= row < len(self.matches):
            return
        match = self.matches[row]
        text = getattr(self.book.pages[match.page], match.field)
        self.preview.setPlainText(text)
        cursor = self.preview.textCursor()
        # Qt cursor positions count UTF-16 units, while Python offsets count code points.
        cursor.setPosition(len(text[:match.start].encode("utf-16-le")) // 2)
        cursor.setPosition(len(text[:match.end].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
        self.preview.setTextCursor(cursor)
        self.preview.centerCursor()

    def navigate(self, direction):
        if self.matches:
            self.results.setCurrentRow((self.results.currentRow() + direction) % len(self.matches))

    def apply(self, matches):
        undo = replace_matches(self.book.pages, matches, self.replacement.text())
        if not undo:
            self.status.setText(tr('No text changed.'))
            return
        self.undo_step = undo
        self.pages_changed.emit({item[0] for item in undo})
        self.search()
        self.status.setText(tr('Replaced {p0} match(es).', p0=len(matches)))

    def replace_one(self):
        row = self.results.currentRow()
        if not 0 <= row < len(self.matches):
            return
        match = self.matches[row]
        self.apply([match])
        # Continue after the replacement, even if it contains the search term.
        end = match.start + len(self.replacement.text())
        for index, item in enumerate(self.matches):
            if (item.page, item.field) == (match.page, match.field) and item.start >= end:
                self.results.setCurrentRow(index)
                return
            if item.page > match.page or (
                item.page == match.page and match.field == "source" and item.field == "translation"
            ):
                self.results.setCurrentRow(index)
                return

    def replace_all(self):
        if not self.matches:
            return
        answer = QMessageBox.question(
            self, tr('Replace all matches?'),
            tr('Replace {p0} matches across {p1} pages?', p0=len(self.matches), p1=len({m.page for m in self.matches})),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.apply(list(self.matches))

    def undo(self):
        if not self.undo_step:
            return
        changed = set()
        for index, field, text, report in self.undo_step:
            page = self.book.pages[index]
            setattr(page, field, text)
            assessment = "extraction_assessment" if field == "source" else "translation_assessment"
            setattr(page, assessment, report)
            changed.add(index)
        self.undo_step = []
        self.pages_changed.emit(changed)
        self.search()
        self.status.setText(tr('Last replacement undone.'))
