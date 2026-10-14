"""Main window actions and page state.

Start at MainWindow.__init__. layout.py builds its widgets; the methods here
handle their actions. AI requests run in workers; AI-input rendering uses
an isolated process. Live document handles and widget updates stay on Qt.
"""

from __future__ import annotations

from .i18n import tr

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import queue
import threading
import time

from PIL import Image, ImageOps

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, QSize
from PySide6.QtGui import QAction, QColor, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QListWidgetItem,
    QLineEdit,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QMenu,
    QMessageBox,
    QProgressDialog,
)

from .chat_panel import open_chat
from .book_context import options_for_page, book_signature
from .continuity import continuity_options, apply_previous_edit
from .summary_panel import SummaryDialog
from .docx_export import export_docx, save_docx
from .batch import BatchRun
from .batch_panel import BatchControls
from .config import DEFAULT_LANGUAGE, DEFAULT_MODEL
from .core import (
    BookDocument,
    PasswordRequired,
    atomic_write,
    export_text,
    read_project,
    save_project,
)
from .gemini_service import GeminiService
from .api_rotation import ApiProfile, ApiRouter, RotatingService, redact, PROVIDER_LABELS
from .openai_service import DEFAULT_OPENAI_MODEL
from .nvidia_service import DEFAULT_NVIDIA_MODEL
from .confidence import Assessment, CombinedAssessment, review_flags
from .text_links import track_editor_edit
from .layout import build_interface
from .preview import COLORS
from .image_export import default_image_name, save_page_image, save_page_copy
from .image_editor import ImageEditorDialog
from .phone_import import PhoneImportDialog, save_edited_image
from .find_replace import FindReplaceDialog
from .output_preview import ExportOptionsDialog, OutputPreviewDialog
from .preferences import load_options
from .settings import SettingsDialog
from .secure_settings import load_settings, SettingsError
from .widgets import app_icon, load_stylesheet


@dataclass(frozen=True)
class PageJob:
    """Snapshot of the page a request belongs to, even if the user navigates away."""

    token: int
    kind: str
    page_index: int
    source: str
    target_language: str
    local: bool = False


class MainWindow(BatchControls, QMainWindow):
    def __init__(self):
        super().__init__()
        self.book: BookDocument | None = None
        self.index = 0
        self.project_path = None
        self.dirty = False
        self._loading = False
        self._previous_edit_undo = None
        self._last_batch = None
        self.batch_window = None
        self._review_cursor = None
        self._review_edits = {"source": [], "translation": []}
        self.api_key = (os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")).strip()
        self.model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
        self._model_chosen = bool(os.getenv("GEMINI_MODEL", "").strip())
        self._verified_connection = None
        self.generation_options = load_options()
        self.api_profiles = []
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
        if openai_key or nvidia_key:
            if self.api_key:
                self.api_profiles.append(ApiProfile("gemini", self.model, self.api_key))
            if openai_key:
                self.api_profiles.append(ApiProfile("openai", os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL, openai_key))
            if nvidia_key:
                self.api_profiles.append(ApiProfile("nvidia", os.getenv("NVIDIA_MODEL", "").strip() or DEFAULT_NVIDIA_MODEL, nvidia_key))
            self.api_key, self.model = self.api_profiles[0].api_key, self.api_profiles[0].model
            self._model_chosen = True
        self.rotation_enabled = True
        self.failure_limit = 3
        self._settings_warning = ""
        try:
            saved = load_settings()
            if saved is not None:
                self.api_profiles = saved.profiles
                self.api_key, self.model = saved.profiles[0].api_key, saved.profiles[0].model
                self.generation_options = saved.options
                self.rotation_enabled = saved.rotation_enabled
                self.failure_limit = saved.failure_limit
                self._model_chosen = True
                self._settings_warning = saved.warning
        except SettingsError as exc:
            self._settings_warning = str(exc)
        self._router = None
        self._router_signature = None
        self._request_cancel = threading.Event()
        self._api_events = queue.Queue()
        self._job: PageJob | None = None
        self._batch: BatchRun | None = None
        self._job_id = 0
        self._results = queue.Queue()
        self.setWindowTitle(tr('Book Translator'))
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1000, 620)
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        width, height = 1440, 900
        if area is not None:
            width = max(1000, min(width, area.width() - 24))
            height = max(620, min(height, area.height() - 24))
        self.resize(width, height)
        self.setStyleSheet(load_stylesheet())
        build_interface(self)
        self._build_menus()
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._poll_results)
        self._poll.start()
        self._refresh_controls()
        if self._settings_warning:
            self.statusBar().showMessage(self._settings_warning)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu(tr('File'))
        edit_menu = self.menuBar().addMenu(tr('Edit'))
        page_menu = self.menuBar().addMenu(tr('Page'))
        self.file_actions = []

        def action(menu, text, shortcut, callback):
            item = QAction(text, self)
            if shortcut:
                item.setShortcut(QKeySequence(shortcut))
            item.triggered.connect(callback)
            menu.addAction(item)
            return item

        self.undo_continuity_action = action(edit_menu, tr('Undo previous-page fix'), "", self.undo_previous_fix)
        self.batch_action = action(edit_menu, tr("Batch processing…"), "Ctrl+Shift+B", self.show_batch)
        review_menu = self.menuBar().addMenu(tr("Review"))
        self.next_passage_action = action(review_menu, tr("Next flagged passage"), "F8", self.next_review_passage)
        self.next_flagged_page_action = action(review_menu, tr("Next flagged page"), "Shift+F8", self.next_review_page)
        self.check_next_action = action(review_menu, tr("Mark checked and next page"), "Ctrl+Shift+K", self.check_and_next_review)
        self.dismiss_passage_action = action(review_menu, tr("Dismiss current passage"), "Ctrl+Shift+D", self.dismiss_current_review)
        self.summary_action = action(edit_menu, tr('Book summary…'), "Ctrl+Shift+M", self.show_summary)
        self.chat_action = action(edit_menu, tr('Ask AI…'), "Ctrl+J", lambda: open_chat(self))
        self.find_page_action = action(edit_menu, tr("Find in current page"), "Ctrl+F", lambda: self.page_find.open())
        self.find_replace_action = action(
            edit_menu, tr('Find and replace…'), "Ctrl+H", self.find_replace
        )
        self.new_workspace_action = action(
            file_menu, tr('New workspace'), "Ctrl+N", self.new_workspace
        )
        self.file_actions.append(self.new_workspace_action)
        self.file_actions.append(
            action(file_menu, tr('Open files…'), "Ctrl+O", self.open_files)
        )
        self.file_actions.append(
            action(file_menu, tr('Open project…'), "Ctrl+Shift+O", self.open_project)
        )
        self.file_actions.append(
            action(file_menu, tr('Receive from phone…'), "Ctrl+Shift+I", self.receive_from_phone)
        )
        file_menu.addSeparator()
        self.save_action = action(file_menu, tr('Save project'), "Ctrl+S", self.save)
        self.save_as_action = action(
            file_menu, tr('Save project as…'), "Ctrl+Shift+S", lambda: self.save(force_dialog=True)
        )
        self.output_action = action(
            file_menu, tr('Preview output…'), "Ctrl+Shift+P", self.preview_output
        )
        self.export_action = action(file_menu, tr('Export…'), "Ctrl+E", self.export)
        file_menu.addSeparator()
        action(file_menu, tr('Quit'), "Ctrl+Q", self.close)
        self.prev_action = action(
            page_menu, tr('Previous page'), "Alt+Left", lambda: self.go_to(self.index - 1)
        )
        self.next_action = action(
            page_menu, tr('Next page'), "Alt+Right", lambda: self.go_to(self.index + 1)
        )
        self.jump_action = action(page_menu, tr('Go to page…'), "Ctrl+G", self.jump_to_page)
        page_menu.addSeparator()
        self.edit_image_action = action(page_menu, tr('Edit image…'), "", lambda: self.edit_page_image(self.index))
        page_menu.addSeparator()
        self.extract_action = action(page_menu, tr('Extract text'), "Ctrl+Shift+E", self.extract)
        self.translate_action = action(page_menu, tr('Translate text'), "Ctrl+Return", self.translate)
        self.combined_action = action(
            page_menu, tr('Extract && Translate'), "Ctrl+Shift+Return", self.extract_and_translate
        )

    def _mark_dirty(self, history_reason="Updated", merge_history=False, pages=None):
        from .page_history import checkpoint
        if self.book and self.book.pages:
            changed = pages if pages is not None else (self.book.pages[self.index],)
            for page in changed:
                checkpoint(page, history_reason, merge_history)
        self.dirty = True
        self._update_title()

    def _update_title(self):
        name = (
            self.project_path.name if self.project_path else (self.book.title if self.book else "")
        )
        self.setWindowTitle(
            tr('{p0}{p1}Book Translator', p0='* ' if self.dirty else '', p1=name + ' — ' if name else '')
        )

    def _refresh_controls(self):
        loaded = self.book is not None and bool(self.book.pages)
        busy = self._busy
        previous = loaded and self.index > 0
        next_page = loaded and self.index < len(self.book.pages) - 1
        for widget in (self.previous_button, self.prev_action):
            widget.setEnabled(previous)
        for widget in (self.next_button, self.next_action):
            widget.setEnabled(next_page)
        for widget in (
            self.page_button,
            self.jump_action,
            self.save_button,
            self.save_action,
            self.save_as_action,
            self.export_action,
            self.output_action,
            self.copy_button,
            self.zoom_in,
            self.zoom_out,
            self.fit_button,
            self.show_flags,
            self.threshold,
        ):
            widget.setEnabled(loaded)
        for widget in (self.open_button, self.settings_button, *self.file_actions):
            widget.setEnabled(not busy)
        self.chat_action.setEnabled(loaded and not busy)
        self.chat_button.setEnabled(loaded and not busy)
        self.summary_action.setEnabled(loaded and not busy)
        for action in (self.next_passage_action, self.next_flagged_page_action):
            action.setEnabled(loaded)
        self.check_next_action.setEnabled(loaded and not busy)
        self.dismiss_passage_action.setEnabled(loaded and not busy)
        self.summary_button.setEnabled(loaded and not busy)
        self.undo_continuity_action.setEnabled(bool(self._previous_edit_undo) and not busy)
        self.region_button.setEnabled(loaded)
        self.find_replace_action.setEnabled(loaded and not busy)
        self.find_page_action.setEnabled(loaded)
        self.history_button.setEnabled(loaded and not busy)
        self.edit_image_action.setEnabled(loaded and not busy)
        self.language.setEnabled(not busy)
        self.method.setEnabled(loaded and not busy)
        pdf_available = loaded and self.book.kind in {"pdf", "mixed"}
        self.method.model().item(0).setEnabled(pdf_available)
        can_extract = loaded and not busy
        can_translate = loaded and not busy
        for widget in (self.extract_button, self.extract_action, self.combined_button, self.combined_action):
            widget.setEnabled(can_extract)
        for widget in (self.translate_button, self.translate_action):
            widget.setEnabled(can_translate)
        locked = (
            self._batch and self.index in self._batch.remaining
        ) or (self._job and self._job.page_index == self.index)
        for editor in (self.source_editor, self.translation_editor):
            editor.setEnabled(loaded)
            editor.setReadOnly(bool(locked))
        self.page_find.update_actions()
        self.select_all_button.setEnabled(loaded)
        self.thumbnails.set_editing_enabled(not busy)
        self.thumbnails.render_paused = busy
        if busy:
            self.thumbnails.timer.stop()
        else:
            self.thumbnails.schedule()
        for widget in (self.save_button, self.save_action, self.save_as_action):
            widget.setEnabled(self.book is not None)
        count = len(self._selected_pages())
        self.selection_label.setText(tr("1 page selected") if count == 1 else tr("{count} pages selected", count=count))
        self.progress.setRange(0, len(self._batch.pages) if self._batch else 0)
        if self._batch:
            self.progress.setValue(self._batch.position)
        self.progress.setVisible(busy)
        self.cancel_button.setVisible(busy)
        profiles = self._api_profiles()
        active, verified = self._router.snapshot() if self._router else (0, self._verified_connection == (self.api_key, self.model))
        active = min(active, len(profiles) - 1)
        profile = profiles[active]
        provider = PROVIDER_LABELS[profile.provider]
        state = "connected" if verified else ("untested" if profile.api_key else "no key")
        if self._router and self._router.is_paused(active):
            state = "paused"
        prefix = f"API {active + 1} · " if len(profiles) > 1 else ""
        self.connection_label.setText(tr('{p0}{p1} · {p2}', p0=prefix, p1=provider, p2=tr(state)))
        self.connection_label.setToolTip(profile.model)

    def _record_review_edit(self, kind, position, removed, added):
        if not self._loading:
            self._review_edits[kind].append((position, removed, added))

    def _track_review_edit(self, kind, text):
        changes = self._review_edits[kind]
        self._review_edits[kind] = []
        page = self.book.pages[self.index]
        track_editor_edit(page, kind, getattr(page, kind), text, changes)

    def _source_changed(self):
        if self._loading or not self.book or not self.book.pages:
            return
        text = self.source_editor.toPlainText()
        self._track_review_edit("source", text)
        self.book.pages[self.index].source = text
        self._mark_dirty("Edited", merge_history=True)
        self._update_page_details()
        self._refresh_controls()

    def _translation_changed(self):
        if self._loading or not self.book or not self.book.pages:
            return
        page = self.book.pages[self.index]
        text = self.translation_editor.toPlainText()
        if not page.translation and text:
            page.translated_from = page.source
            page.target_language = self.language.currentText().strip()
        self._track_review_edit("translation", text)
        page.translation = text
        self._mark_dirty("Edited", merge_history=True)
        self._update_page_details()

    def _language_changed(self):
        if self._loading:
            return
        if self.book:
            self._mark_dirty()
        self._update_page_details()

    def _update_page_details(self):
        if not hasattr(self, "word_count"):
            return
        editor = self.source_editor if self.tabs.currentIndex() == 0 else self.translation_editor
        text = editor.toPlainText()
        self.word_count.setText(tr('{p0:,} words · {p1:,} characters', p0=len(text.split()), p1=len(text)))
        if not self.book or not self.book.pages:
            return
        page = self.book.pages[self.index]
        if self._job and self._job.page_index == self.index:
            note = "Processing…"
        elif self._batch and self.index in self._batch.remaining:
            note = "Queued…"
        elif page.translation_is_stale:
            note = "Source text changed · translate again"
        elif page.translation and page.target_language != self.language.currentText().strip():
            note = f"Translation language: {page.target_language or 'unspecified'}"
        else:
            note = ""
        self.page_note.setText(note)
        self.page_note.setVisible(bool(note))
        self._update_reviews()

    def _threshold_changed(self):
        if self._loading:
            return
        if self.book:
            self._mark_dirty()
            for index, page in enumerate(self.book.pages):
                self.thumbnails.set_review_count(
                    index, len(review_flags(page, self.threshold.value() / 100))
                )
        self._update_reviews()

    def _update_reviews(self, *_):
        if not self.book or not self.book.pages or not hasattr(self, "threshold"):
            return
        page = self.book.pages[self.index]
        flags = review_flags(page, self.threshold.value() / 100)
        summary = []
        for name, report, current, valid in (
            ("Extract", page.extraction_assessment, page.source, True),
            (
                "Translation",
                page.translation_assessment,
                page.translation,
                not page.translation_is_stale,
            ),
        ):
            if report is not None and (report.text != current or not valid):
                summary.append(tr("{name}: score outdated", name=tr(name)))
            elif report is not None:
                summary.append(f"{tr(name)}: {report.confidence:.0%}")
        self.confidence_label.setText(" · ".join(summary))
        self.confidence_label.setVisible(bool(summary))
        self.review_list.clear()
        for flag in flags:
            passage = (
                "Whole page"
                if flag.page_level
                else " ".join((flag.source_text or flag.output_text).split())
            )
            passage = passage[:66] + ("…" if len(passage) > 66 else "")
            location = " · unlocated" if not flag.box else ""
            original = " · original result" if flag.outdated else ""
            item = QListWidgetItem(f"{flag.name} {flag.confidence:.0%}{original}{location} · {passage}")
            item.setForeground(QColor(COLORS[flag.kind]))
            item.setToolTip(flag.tooltip)
            item.setData(Qt.ItemDataRole.UserRole, flag)
            self.review_list.addItem(item)
            row = QWidget()
            row.setAutoFillBackground(True)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 0, 6, 0)
            jump = QPushButton(item.text())
            jump.setFlat(True)
            jump.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            jump.setStyleSheet(
                f"QPushButton {{ text-align: left; color: {COLORS[flag.kind]}; padding: 3px; border: none; background: transparent; }}"
                "QPushButton:hover { background: #e5f2ec; border-radius: 4px; }"
            )
            jump.setToolTip(flag.tooltip)
            jump.clicked.connect(lambda _=False, entry=item: self._focus_review(entry))
            remove = QPushButton("×")
            remove.setFixedSize(26, 26)
            remove.setStyleSheet(
                "QPushButton { padding: 0; font-size: 17px; border: none; background: transparent; border-radius: 4px; }"
                "QPushButton:hover { background: #fde7e7; color: #b42323; }"
                "QPushButton:pressed { background: #f7cccc; }"
            )
            remove.setToolTip(tr('Dismiss this review item'))
            remove.setAccessibleName(tr('Dismiss review item'))
            remove.setEnabled(not self._busy)
            remove.clicked.connect(lambda _=False, key=flag.key: self._dismiss_review(key))
            row_layout.addWidget(jump, 1)
            row_layout.addWidget(remove)
            item.setSizeHint(QSize(0, 34))
            self.review_list.setItemWidget(item, row)
        self.review_panel.setVisible(bool(summary or flags))
        self.review_toggle.setToolTip(("Collapse" if self.review_toggle.isChecked() else "Expand") + f" review passages ({len(flags)})")
        self.review_list.setVisible(bool(flags))
        self.review_list.setFixedHeight(min(142, len(flags) * 34 + 6))
        self.preview.set_flags(flags if self.show_flags.isChecked() else [])
        self.thumbnails.set_review_count(self.index, len(flags))

    def _toggle_review_panel(self, expanded):
        self.review_body.setVisible(expanded)
        self.review_toggle.setArrowType(Qt.ArrowType.UpArrow if expanded else Qt.ArrowType.DownArrow)
        self.review_toggle.setToolTip((tr('Collapse review passages') if expanded else tr('Expand review passages')))

    def _dismiss_review(self, key):
        if self._busy or not self.book or not self.book.pages:
            return
        page = self.book.pages[self.index]
        if key not in page.dismissed_reviews:
            page.dismissed_reviews.append(key)
            self._mark_dirty()
            self._update_reviews()

    def next_review_passage(self):
        if not self.book or not self.book.pages:
            return
        entries = [(index, flag) for index, page in enumerate(self.book.pages)
                   for flag in review_flags(page, self.threshold.value() / 100)]
        if not entries:
            self.statusBar().showMessage(tr("No confidence passages need review."), 6000)
            return
        position = None
        if self._review_cursor:
            page, key = self._review_cursor
            position = next((i for i, (index, flag) in enumerate(entries)
                             if self.book.pages[index] is page and index == self.index and flag.key == key), None)
        if position is None:
            position = next((i for i, (index, _) in enumerate(entries) if index >= self.index), 0)
        else:
            position = (position + 1) % len(entries)
        index, flag = entries[position]
        self.go_to(index)
        self._focus_review_flag(flag)

    def next_review_page(self):
        if not self.book or not self.book.pages:
            return
        for distance in range(1, len(self.book.pages) + 1):
            index = (self.index + distance) % len(self.book.pages)
            page = self.book.pages[index]
            flags = review_flags(page, self.threshold.value() / 100)
            if page.manual_flag or flags:
                self.go_to(index)
                if flags:
                    self._focus_review_flag(flags[0])
                else:
                    self.statusBar().showMessage(tr("This page was flagged manually."), 6000)
                return
        self.statusBar().showMessage(tr("No flagged pages remain."), 6000)

    def check_and_next_review(self):
        if self._busy or not self.book or not self.book.pages:
            return
        page = self.book.pages[self.index]
        if page.manual_flag or review_flags(page, self.threshold.value() / 100):
            self.toggle_page_checked(self.index)
        self._review_cursor = None
        self.next_review_page()

    def dismiss_current_review(self):
        if self._busy or not self._review_cursor or not self.book:
            return
        page, key = self._review_cursor
        if self.book.pages[self.index] is page:
            self._dismiss_review(key)
            self.next_review_passage()

    def _focus_review(self, item):
        flag = item.data(Qt.ItemDataRole.UserRole)
        self._focus_review_flag(flag)

    def _focus_review_flag(self, flag):
        if not flag:
            return
        self._review_cursor = (self.book.pages[self.index], flag.key)
        self.review_toggle.setChecked(True)
        self.tabs.setCurrentIndex(0 if flag.kind == "source" else 1)
        editor = self.source_editor if flag.kind == "source" else self.translation_editor
        cursor = editor.textCursor()
        cursor.clearSelection()
        if flag.text_span:
            start, end = flag.text_span
            text = editor.toPlainText()
            cursor.setPosition(len(text[:start].encode("utf-16-le")) // 2)
            cursor.setPosition(len(text[:end].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        if flag.text_span:
            editor.centerCursor()
        if flag.box:
            self.preview.focus_box(flag.box)
        self.statusBar().showMessage(
            tr('{p0} {p1:.0%}: {p2}', p0=flag.name, p1=flag.confidence, p2=flag.reason or 'Review this passage.'), 12000
        )

    def _show_page(self, render=True):
        if not self.book.pages:
            self._loading = True
            self.source_editor.clear()
            self.translation_editor.clear()
            self._loading = False
            self.page_button.setText(tr('Page —'))
            self.total_label.setText("")
            self.preview_stack.setCurrentIndex(0)
            self.empty_message.setText(tr('Open files to add pages'))
            self.preview_caption.hide()
            self.review_list.clear()
            self.review_list.hide()
            self.confidence_label.hide()
            self.review_panel.hide()
            self.page_note.hide()
            self.word_count.setText(tr('0 words · 0 characters'))
            self._refresh_controls()
            return
        page = self.book.pages[self.index]
        self._loading = True
        try:
            self.source_editor.setPlainText(page.source)
            self.translation_editor.setPlainText(page.translation)
        finally:
            self._loading = False
        self.page_button.setText(tr('Page {p0}', p0=self.index + 1))
        self.total_label.setText(tr('of {p0}', p0=len(self.book.pages)))
        self.thumbnails.set_current_page(self.index)
        description = self.book.page_description(self.index)
        self.preview_caption.setText(
            (description if len(description) <= 68 else description[:65] + "…")
        )
        self.preview_caption.setToolTip(description)
        self.preview_caption.show()
        if render:
            try:
                self.preview.set_image(self.book.render_page(self.index))
                self.preview_stack.setCurrentIndex(1)
            except Exception as exc:
                self.empty_message.setText(tr('Preview unavailable: {p0}', p0=exc))
                self.preview_stack.setCurrentIndex(0)
        self._update_page_details()
        self._refresh_controls()

    def set_book(self, book, project_path=None, project=None):
        """Install a fully validated document only after opening has succeeded."""
        if self.book:
            self.thumbnails.timer.stop()
            self.book.close()
        self.book = book
        self._last_batch = None
        self._review_cursor = None
        if self.batch_window:
            self.batch_window.close()
        self._previous_edit_undo = None
        self.project_path = Path(project_path) if project_path else None
        self.index = project.index if project else 0
        if project:
            self.book.pages = project.pages
            self.book.ai_summary = project.ai_summary
            self.book.summary_signature = project.summary_signature
            self._loading = True
            self.language.setCurrentText(project.target_language)
            if not self._model_chosen and project.model.startswith("gemini-"):
                self.model = project.model
            self.threshold.setValue(project.confidence_threshold)
            self._loading = False
        from .page_history import checkpoint
        for page in book.pages:
            checkpoint(page, "Initial")
        self.page_find.hide()
        self.thumbnails.set_book(book)
        self.thumbnail_title.setToolTip(tr('{p0} pages', p0=len(book.pages)))
        for index, page in enumerate(book.pages):
            self.thumbnails.set_review_count(
                index, len(review_flags(page, self.threshold.value() / 100))
            )
        self.method.setCurrentIndex(0 if book.kind == "pdf" else 1)
        title = book.title
        self.document_name.setText((title if len(title) <= 40 else title[:37] + "…"))
        self.document_name.setToolTip(title)
        self.document_name.show()
        self.tabs.setCurrentIndex(0)
        self.dirty = False
        self._update_title()
        self._show_page()
        self.statusBar().showMessage(tr('Opened {p0} page(s).', p0=len(book.pages)))

    def _maybe_save(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self,
            tr('Save your work?'),
            tr('Save your page text and translations before continuing?'),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        return self.save() if answer == QMessageBox.StandardButton.Save else True

    def _open_with_password(self, paths, sort_images=True):
        passwords = {}
        while True:
            try:
                return BookDocument.open(paths, passwords, sort_images=sort_images)
            except PasswordRequired as exc:
                password, accepted = QInputDialog.getText(
                    self,
                    tr('Password protected PDF'),
                    tr('Enter the password for {p0}:', p0=exc.path.name),
                    QLineEdit.EchoMode.Password,
                )
                if not accepted:
                    return None
                passwords[exc.path] = password

    def add_pages(self, position=None):
        if self._busy:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr('Open files to add pages'), "",
            "Books and images (*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if not paths:
            return
        incoming = None
        try:
            incoming = self._open_with_password(paths)
            if incoming is None:
                return
            self._insert_document(incoming, position)
            incoming = None
        except Exception as exc:
            QMessageBox.warning(self, tr('Could not add pages'), str(exc))
        finally:
            if incoming is not None:
                incoming.close()

    def _insert_document(self, incoming, position=None):
        if self.book is None:
            self.set_book(incoming)
            self._mark_dirty()
            return
        position = len(self.book.pages) if position is None else position
        count = len(incoming.pages)
        was_empty = not self.book.pages
        self.book.insert(position, incoming)
        incoming.close()
        if was_empty:
            self.method.setCurrentIndex(0 if self.book.kind == "pdf" else 1)
        self.index = position
        self._refresh_page_order(range(position, position + count))
        self.statusBar().showMessage(tr('Added {p0} page(s).', p0=count), 6000)

    def import_phone_images(self, paths):
        if self._busy:
            raise ValueError("Wait for the current request to finish.")
        incoming = BookDocument.open(paths, sort_images=False)
        self._insert_document(incoming)

    def receive_from_phone(self):
        if self._busy:
            return
        dialog = None
        try:
            dialog = PhoneImportDialog(self.import_phone_images, self)
            dialog.exec()
        except Exception as exc:
            QMessageBox.warning(self, tr('Could not receive photos'), str(exc))
        finally:
            if dialog is not None:
                dialog.stop()

    def edit_page_image(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        try:
            path, frame = self.book.refs[index]
            if path.suffix.lower() == ".pdf":
                image = Image.open(BytesIO(self.book.render_page(index, max_edge=3200))).convert("RGB")
            else:
                with Image.open(path) as source:
                    source.seek(frame)
                    image = ImageOps.exif_transpose(source).convert("RGB")
            dialog = ImageEditorDialog(image, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            edited = save_edited_image(dialog.result_image)
            self.book.replace_image(index, edited)
            self.index = index
            self._refresh_page_order([index])
            self.statusBar().showMessage(tr('Image updated. Existing text kept; extract again to update it. Old confidence flags cleared.'), 10000)
        except Exception as exc:
            QMessageBox.warning(self, tr('Could not edit image'), str(exc))

    def delete_page(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        answer = QMessageBox.question(
            self, tr('Delete page?'),
            tr('Remove page {p0} and its text from this project? The original file will stay unchanged.', p0=index + 1),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        current = self.book.pages[self.index]
        self.book.delete(index)
        if any(page is current for page in self.book.pages):
            self.index = next(i for i, page in enumerate(self.book.pages) if page is current)
        else:
            self.index = min(index, max(0, len(self.book.pages) - 1))
        self._refresh_page_order([self.index] if self.book.pages else [])
        self.statusBar().showMessage(tr('Page removed from project.'), 6000)

    def move_pages(self, rows, destination):
        if self._busy or not self.book:
            return
        rows = sorted(set(rows))
        count = len(self.book.pages)
        if not rows or any(not 0 <= row < count for row in rows) or not 0 <= destination <= count:
            return
        remaining = [i for i in range(count) if i not in rows]
        insertion = destination - sum(row < destination for row in rows)
        order = remaining[:insertion] + rows + remaining[insertion:]
        if order == list(range(count)):
            return
        current = self.index
        self.book.reorder(order)
        self.index = order.index(current)
        self._refresh_page_order([order.index(row) for row in rows])
        self.statusBar().showMessage(tr('Page order updated.'), 6000)

    def _refresh_page_order(self, selected):
        """Rebuild the thumbnail cache after every structural edit."""
        self.thumbnails.set_book(self.book)
        self.thumbnails.blockSignals(True)
        for index in selected:
            self.thumbnails.item(index).setSelected(True)
        self.thumbnails.setCurrentRow(self.index, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.thumbnails.blockSignals(False)
        self.thumbnail_title.setToolTip(tr('{p0} pages', p0=len(self.book.pages)))
        for index, page in enumerate(self.book.pages):
            self.thumbnails.set_review_count(
                index, len(review_flags(page, self.threshold.value() / 100))
            )
        if self.book.kind == "images":
            self.method.setCurrentIndex(1)
        self.document_name.setText(self.book.title)
        self.document_name.setToolTip(self.book.title)
        self._mark_dirty()
        self._show_page()

    def rename_page(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        page = self.book.pages[index]
        name, accepted = QInputDialog.getText(
            self, tr('Rename page'), tr('Page name (blank restores the default):'),
            QLineEdit.EchoMode.Normal, page.name,
        )
        if not accepted:
            return
        name = " ".join(name.split())
        if len(name) > 120:
            QMessageBox.warning(self, tr('Name too long'), tr('Use up to 120 characters.'))
            return
        if name == page.name:
            return
        page.name = name
        self.thumbnails.set_review_count(
            index, len(review_flags(page, self.threshold.value() / 100))
        )
        self._mark_dirty()
        self.statusBar().showMessage(tr('Page renamed.'), 4000)

    def toggle_page_checked(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        page = self.book.pages[index]
        if page.is_reviewed and not page.manual_flag:
            page.reviewed_signature = ""
        elif page.manual_flag or review_flags(page, self.threshold.value() / 100):
            page.manual_flag = False
            page.reviewed_signature = page.review_signature()
        else:
            return
        self.thumbnails.set_review_count(
            index, len(review_flags(page, self.threshold.value() / 100))
        )
        self._mark_dirty()
        if index == self.index:
            self._update_page_details()

    def toggle_manual_flag(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        page = self.book.pages[index]
        page.manual_flag = not page.manual_flag
        self.thumbnails.set_review_count(index, len(review_flags(page, self.threshold.value() / 100)))
        self._mark_dirty()

    def save_thumbnail_image(self, index):
        if self._busy or not self.book or not 0 <= index < len(self.book.pages):
            return
        dialog = QFileDialog(self, "Save image as")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter("PNG image (*.png)")
        dialog.setDefaultSuffix("png")
        dialog.selectFile(default_image_name(self.book.page_name(index)))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        destination = Path(dialog.selectedFiles()[0])
        if destination.suffix.lower() != ".png":
            QMessageBox.warning(self, tr('Choose a PNG filename'), tr('Save this image with a .png extension.'))
            return
        try:
            save_page_image(self.book, index, destination)
            self.statusBar().showMessage(tr('Saved image to {p0}', p0=destination), 6000)
        except Exception as exc:
            QMessageBox.warning(self, tr('Could not save image'), str(exc))

    def save_selected_images(self, indices):
        if self._busy or not self.book or not indices:
            return
        indices = sorted(set(indices))
        folder = QFileDialog.getExistingDirectory(self, tr('Save selected images'))
        if not folder:
            return
        try:
            used_names = {path.name.casefold() for path in Path(folder).iterdir()}
        except OSError as exc:
            QMessageBox.warning(self, tr('Could not open folder'), str(exc))
            return
        progress = QProgressDialog("Saving images…", "Cancel", 0, len(indices), self)
        progress.setWindowTitle(tr('Save selected images'))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.show()
        saved, failures = 0, []
        try:
            for position, index in enumerate(indices):
                progress.setValue(position)
                progress.setLabelText(f"Saving {position + 1} of {len(indices)}")
                QApplication.processEvents()
                if progress.wasCanceled():
                    break
                try:
                    save_page_copy(self.book, index, folder, used_names)
                    saved += 1
                except Exception as exc:
                    failures.append(f"{self.book.page_name(index)}: {exc}")
            cancelled = progress.wasCanceled()
        finally:
            progress.close()
            progress.deleteLater()
        message = f"Saved {saved} of {len(indices)} images to {folder}."
        if cancelled:
            message += " Cancelled; completed images were kept."
        self.statusBar().showMessage(message, 10000)
        if failures:
            details = "\n".join(failures[:20])
            if len(failures) > 20:
                details += f"\n… and {len(failures) - 20} more."
            QMessageBox.warning(self, tr('Some images could not be saved'), message + "\n\n" + details)

    def page_context_menu(self, position):
        item = self.thumbnails.itemAt(position)
        if item is None:
            return
        index = self.thumbnails.row(item)
        menu = QMenu(self)
        page = self.book.pages[index]
        checked = menu.addAction((tr('Mark as unchecked') if page.is_reviewed and not page.manual_flag else tr('Mark as checked')))
        checked.setEnabled(not self._busy and (
            page.is_reviewed or page.manual_flag or bool(review_flags(page, self.threshold.value() / 100))
        ))
        flag = menu.addAction((tr('Remove flag') if page.manual_flag else tr('Flag page')))
        flag.setEnabled(not self._busy)
        rename = menu.addAction(tr('Rename page'))
        edit_image = menu.addAction(tr('Edit image…'))
        selected = self._selected_pages() if item.isSelected() else [index]
        save_image = menu.addAction(
            (tr('Save selected images… ({p0})', p0=len(selected)) if len(selected) > 1 else tr('Save image as…'))
        )
        menu.addSeparator()
        above = menu.addAction(tr('Add page above'))
        below = menu.addAction(tr('Add page below'))
        menu.addSeparator()
        delete = menu.addAction(tr('Delete page'))
        for action in (rename, edit_image, save_image, above, below, delete):
            action.setEnabled(not self._busy)
        chosen = menu.exec(self.thumbnails.viewport().mapToGlobal(position))
        if chosen == checked:
            self.toggle_page_checked(index)
        elif chosen == flag:
            self.toggle_manual_flag(index)
        elif chosen == rename:
            self.rename_page(index)
        elif chosen == edit_image:
            self.edit_page_image(index)
        elif chosen == save_image:
            if len(selected) > 1:
                self.save_selected_images(selected)
            else:
                self.save_thumbnail_image(index)
        elif chosen == above:
            self.add_pages(index)
        elif chosen == below:
            self.add_pages(index + 1)
        elif chosen == delete:
            self.delete_page(index)

    def new_workspace(self):
        if self._busy or not self._maybe_save():
            return
        self.set_book(BookDocument([], "images", []))
        self.statusBar().showMessage(tr('New workspace. Open files to add pages.'), 6000)

    def open_files(self):
        self.add_pages()

    def load_paths(self, paths):
        if self._busy or not self._maybe_save():
            return False
        try:
            book = self._open_with_password(paths)
            if book is None:
                return False
            self.set_book(book)
            return True
        except Exception as exc:
            QMessageBox.critical(self, tr('Could not open document'), str(exc))
            return False

    def open_project(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open saved project", "", "Book Translator project (*.btproject);;JSON (*.json)"
        )
        if not path or not self._maybe_save():
            return
        book = None
        try:
            project = read_project(Path(path))
            book = (
                self._open_with_password(project.paths, sort_images=False)
                if project.paths else BookDocument([], "images", [])
            )
            if book is None:
                return
            book.restore_project(project)
            self.set_book(book, Path(path), project)
        except Exception as exc:
            if book is not None and book is not self.book:
                book.close()
            QMessageBox.critical(self, tr('Could not open project'), str(exc))

    def save(self, checked=False, *, force_dialog=False):
        if self.book is None:
            return False
        path = self.project_path
        if path is None or force_dialog:
            suggested = str(path) if path else (
                str(self.book.paths[0].with_suffix(".btproject")) if self.book.paths else "book.btproject"
            )
            chosen, _ = QFileDialog.getSaveFileName(
                self, tr('Save project'), suggested, "Book Translator project (*.btproject)"
            )
            if not chosen:
                return False
            path = Path(chosen)
            if path.suffix.lower() != ".btproject":
                path = path.with_name(path.name + ".btproject")
        try:
            save_project(
                path,
                self.book,
                self.index,
                self.language.currentText().strip() or DEFAULT_LANGUAGE,
                self.model,
                self.threshold.value(),
            )
        except Exception as exc:
            QMessageBox.critical(self, tr('Could not save project'), str(exc))
            return False
        self.project_path = path
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage(tr('Saved {p0}.', p0=path.name), 6000)
        return True

    def find_replace(self):
        if self._busy or not self.book or not self.book.pages:
            return
        dialog = FindReplaceDialog(self, self.book)
        dialog.pages_changed.connect(self._replaced_pages)
        dialog.exec()

    def _replaced_pages(self, indices):
        self._mark_dirty(pages=[self.book.pages[index] for index in indices])
        for index in indices:
            self.thumbnails.set_review_count(
                index, len(review_flags(self.book.pages[index], self.threshold.value() / 100))
            )
        self._show_page(render=False)

    def preview_output(self):
        if not self.book or not self.book.pages:
            return
        OutputPreviewDialog(self, self.book).exec()

    def export(self):
        if not self.book or not self.book.pages:
            return
        options = ExportOptionsDialog(self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        extension = options.file_format.currentData()
        path, _ = QFileDialog.getSaveFileName(
            self, tr('Export text'), f"translation.{extension}",
            "Word document (*.docx)" if extension == "docx" else "Text file (*.txt)"
        )
        if not path:
            return
        try:
            if not Path(path).suffix:
                path += "." + extension
            if Path(path).suffix.lower() != "." + extension:
                raise ValueError(tr("Use the .{extension} filename extension for this format.", extension=extension))
            export_options = dict(show_page_headers=options.show_headers.isChecked(),
                                  show_placeholders=options.show_placeholders.isChecked())
            if extension == "docx":
                save_docx(path, export_docx(self.book, options.mode.currentData(),
                                          page_breaks=options.page_breaks.isChecked(), **export_options))
            else:
                atomic_write(Path(path), export_text(self.book, options.mode.currentData(), **export_options))
            self.statusBar().showMessage(tr('Exported {p0}.', p0=Path(path).name), 6000)
        except Exception as exc:
            QMessageBox.critical(self, tr('Could not export'), str(exc))

    def settings(self):
        if self._busy:
            return
        dialog = SettingsDialog(
            self, self.api_key, self.model, self._verified_connection, self.generation_options,
            profiles=self._api_profiles(), rotation_enabled=self.rotation_enabled, failure_limit=self.failure_limit,
        )
        if self._settings_warning:
            dialog._set_status(self._settings_warning)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._settings_warning = ""
            self.generation_options = dialog.options
            previous_model = self.model
            self.api_profiles = dialog.profiles
            self.api_key, self.model = self.api_profiles[0].api_key, self.api_profiles[0].model
            self.rotation_enabled = dialog.rotation.isChecked()
            self.failure_limit = dialog.failure_limit.value()
            self._router = None
            self._router_signature = None
            self._chat_router_signature = None
            if getattr(self, "chat_panel", None) is not None:
                self.chat_panel.reject()
            self._model_chosen = True
            self._verified_connection = dialog.tested_connection if dialog._editing == 0 else None
            if self.book and self.model != previous_model:
                self._mark_dirty()
            self._refresh_controls()

    def _api_profiles(self):
        # Legacy attributes keep command-line Gemini setup and older projects working.
        return self.api_profiles or [ApiProfile("gemini", self.model, self.api_key)]

    def show_summary(self):
        if self.book and self.book.pages and not self._busy:
            SummaryDialog(self).exec()

    def _service(self, page_index=None):
        profiles = tuple(self._api_profiles())
        signature = (profiles, self.rotation_enabled, self.failure_limit)
        if self._router is None or signature != self._router_signature:
            self._router = ApiRouter(profiles, self.rotation_enabled, self.failure_limit)
            self._router_signature = signature
        self._request_cancel = threading.Event()
        events, token = self._api_events, self._job_id + 1
        index = self.index if page_index is None else page_index
        options = options_for_page(self.book, index, self.generation_options) if self.book else self.generation_options
        if self.book:
            options = continuity_options(self.book, index, options, self.language.currentText())
        return RotatingService(self._router, options, self._request_cancel,
                               lambda message: events.put((token, message)))

    def _ensure_key(self):
        if not any(profile.api_key for profile in self._api_profiles()):
            self.settings()
        return any(profile.api_key for profile in self._api_profiles())

    def go_to(self, index):
        if self.book and 0 <= index < len(self.book.pages) and index != self.index:
            self.index = index
            self._show_page()

    def jump_to_page(self):
        if not self.book or not self.book.pages:
            return
        number, accepted = QInputDialog.getInt(
            self, "Go to page", "Page number:", self.index + 1, 1, len(self.book.pages)
        )
        if accepted:
            self.go_to(number - 1)

    def copy_text(self):
        editor = self.source_editor if self.tabs.currentIndex() == 0 else self.translation_editor
        QApplication.clipboard().setText(editor.toPlainText())
        self.statusBar().showMessage(tr('Page text copied.'), 3000)

    @property
    def _busy(self):
        return self._job is not None or self._batch is not None

    def _selected_pages(self):
        if not self.book or not self.book.pages:
            return []
        selected = sorted(self.thumbnails.row(item) for item in self.thumbnails.selectedItems())
        return selected or [self.index]

    def _selection_changed(self):
        if hasattr(self, "extract_action"):
            self._refresh_controls()

    def _run_selected(self, kind):
        """Start a batch when several pages are selected; otherwise use the single-page path."""
        indices = self._selected_pages()
        if len(indices) < 2:
            if indices and indices[0] != self.index:
                self.go_to(indices[0])
            return False
        target = self.language.currentText().strip()
        if kind != "source" and not target:
            QMessageBox.information(self, tr('Choose a language'), tr('Enter the language to translate into.'))
            return True
        offline = kind == "source" and self.method.currentIndex() in (0, 2)
        if not offline and not self._ensure_key():
            return True
        replace_count = 0
        for index in indices:
            page = self.book.pages[index]
            texts = {
                "source": page.source,
                "translation": page.translation,
                "both": page.source + page.translation,
            }
            replace_count += bool(texts[kind].strip())
        if replace_count:
            answer = QMessageBox.question(
                self, tr('Replace selected page text?'),
                tr('Existing text on {p0} selected page(s) will be replaced. Continue?', p0=replace_count),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return True
        self._batch = BatchRun(tuple(indices), kind, target, offline)
        self._batch.local_ocr = kind == "source" and self.method.currentIndex() == 2
        self.prepare_batch(self._batch)
        self._advance_batch(self._batch)
        return True

    def _advance_batch(self, batch):
        # Identity protects against callbacks left over after cancellation/new batches.
        if self._batch is not batch or self._job is not None:
            return
        if batch.position == len(batch.pages):
            self.batch_event(tr("Batch finished"))
            self._batch = None
            if batch.unsuccessful_pages:
                self.thumbnails.clearSelection()
                for index in batch.unsuccessful_pages:
                    self.thumbnails.item(index).setSelected(True)
                self.go_to(batch.unsuccessful_pages[0])
            self._refresh_controls()
            self._update_page_details()
            self.statusBar().showMessage(tr('Batch finished: {p0}.', p0=batch.summary), 15000)
            if self.batch_window:
                self.batch_window.refresh()
            return
        if batch.paused:
            self.batch_event(tr("Paused"))
            return
        self.start_batch_item()
        index = batch.pages[batch.position]
        page = self.book.pages[index]
        self._refresh_controls()
        self._update_page_details()
        self.statusBar().showMessage(
            tr('Processing {p0} of {p1} · Page {p2}…', p0=batch.position + 1, p1=len(batch.pages), p2=index + 1)
        )
        try:
            if batch.local_ocr:
                operation = self._local_ocr_operation(index)
                self._start_job('source', operation, page_index=index, local=True)
                return
            if batch.offline:
                text = self.book.extract_pdf_text(index)
                if not text:
                    self._finish_batch_page("Skipped: no PDF text. Use AI OCR for this page.")
                    return
                self.batch_event(tr("Adding text to source"), self.batch_item())
                page.source = text
                page.extraction_assessment = None
                self._mark_dirty(pages=(page,))
                self.thumbnails.set_review_count(
                    index, len(review_flags(page, self.threshold.value() / 100))
                )
                if index == self.index:
                    self._show_page(render=False)
                    self.tabs.setCurrentIndex(0)
                self._finish_batch_page()
                return
            source = page.source
            operation = self._page_operation(batch.kind, index, source, batch.target)
            self._start_job(batch.kind, operation, source, batch.target, page_index=index)
            self.statusBar().showMessage(
                tr('Processing {p0} of {p1} · Page {p2}…', p0=batch.position + 1, p1=len(batch.pages), p2=index + 1)
            )
        except Exception as exc:
            message = redact(str(exc), self._api_profiles())
            self._finish_batch_page(message or "Could not process page.")

    def _finish_batch_page(self, error=None):
        self.finish_batch_item(error)
        batch = self._batch
        index = batch.pages[batch.position]
        if error:
            batch.issues.append(f"Page {index + 1}: {error}")
            batch.unsuccessful_pages.append(index)
        else:
            batch.completed += 1
        batch.position += 1
        self._refresh_controls()
        self._update_page_details()
        # Yield between pages so navigation, painting and cancellation remain responsive.
        QTimer.singleShot(0, lambda: self._advance_batch(batch))

    def _confirm_replace(self, kind):
        page = self.book.pages[self.index]
        value = page.source if kind == "source" else page.translation
        if kind == "both":
            value = page.source + page.translation
            kind = "source and translation"
        if not value.strip():
            return True
        answer = QMessageBox.question(
            self,
            tr('Replace {p0}?', p0=kind),
            tr("This will replace the current page's {p0} text. Continue?", p0=kind),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def extract(self):
        if not self.book or not self.book.pages or self._busy:
            return
        if self._run_selected("source"):
            return
        if self.method.currentIndex() == 2:
            if self._confirm_replace('source'):
                self._start_job('source', self._local_ocr_operation(self.index), local=True)
            return
        if self.method.currentIndex() == 0:
            try:
                text = self.book.extract_pdf_text(self.index)
            except Exception as exc:
                QMessageBox.warning(self, tr('Extraction failed'), str(exc))
                return
            if not text:
                QMessageBox.information(
                    self,
                    tr('No PDF text found'),
                    tr('This page may be scanned. Select AI OCR, then click Extract text.'),
                )
                return
            if not self._confirm_replace("source"):
                return
            self.book.pages[self.index].source = text
            self.book.pages[self.index].extraction_assessment = None
            self._show_page(render=False)
            self.tabs.setCurrentIndex(0)
            self._mark_dirty()
            self.statusBar().showMessage(tr('Extracted page {p0} locally.', p0=self.index + 1), 6000)
            return
        if not self._ensure_key() or not self._confirm_replace("source"):
            return
        try:
            operation = self._page_operation("source", self.index, "", "")
            self._start_job("source", operation)
        except Exception as exc:
            QMessageBox.warning(self, tr('Extraction failed'), str(exc))

    def translate(self):
        if not self.book or not self.book.pages or self._busy:
            return
        if self._run_selected("translation"):
            return
        source = self.book.pages[self.index].source
        target = self.language.currentText().strip()
        if not target:
            QMessageBox.information(
                self, tr('Choose a language'), tr('Enter the language to translate into.')
            )
            return
        if not self._ensure_key() or not self._confirm_replace("translation"):
            return
        try:
            operation = self._page_operation("translation", self.index, source, target)
            self._start_job(
                "translation",
                operation,
                source,
                target,
            )
        except Exception as exc:
            QMessageBox.warning(self, tr('Translation failed'), str(exc))

    def extract_and_translate(self):
        if not self.book or not self.book.pages or self._busy:
            return
        if self._run_selected("both"):
            return
        target = self.language.currentText().strip()
        if not target:
            QMessageBox.information(
                self, tr('Choose a language'), tr('Enter the language to translate into.')
            )
            return
        if not self._ensure_key() or not self._confirm_replace("both"):
            return
        try:
            operation = self._page_operation("both", self.index, "", target)
            self._start_job(
                "both",
                operation,
                target=target,
            )
        except Exception as exc:
            QMessageBox.warning(self, tr('Extraction and translation failed'), str(exc))

    def _local_ocr_operation(self, index):
        from .local_ocr import extract_image
        from .page_render import render_request
        path, frame = self.book.refs[index]
        password = self.book._passwords.get(path, '')
        options = self.generation_options
        self._request_cancel = threading.Event()
        cancel, events, token = self._request_cancel, self._api_events, self._job_id + 1
        def operation():
            events.put((token, tr('Preparing page image…')))
            image, _ = render_request(path, frame, password, cancel)
            events.put((token, tr('Running local OCR…')))
            return extract_image(image, options.ocr_executable, options.ocr_languages, cancel)
        return operation

    def _page_operation(self, kind, index, source, target):
        from .page_render import render_request
        path, frame = self.book.refs[index]
        password = self.book._passwords.get(path, '')
        context_started = time.perf_counter()
        service = self._service(index)
        context_seconds = time.perf_counter() - context_started
        cancel, events, token = self._request_cancel, self._api_events, self._job_id + 1
        def operation():
            events.put((token, tr("Context prepared in {seconds:.2f}s", seconds=context_seconds)))
            started = time.perf_counter()
            events.put((token, tr('Preparing page image…')))
            image, mime = render_request(path, frame, password, cancel)
            events.put((token, tr('Page image prepared in {seconds:.2f}s', seconds=time.perf_counter()-started)))
            if cancel.is_set():
                raise RuntimeError('Request cancelled.')
            started = time.perf_counter()
            events.put((token, tr('Waiting for AI response…')))
            try:
                if kind == 'source':
                    return service.extract(image, mime)
                if kind == 'translation':
                    return service.translate(source, target, image, mime)
                return service.extract_and_translate(image, mime, target)
            finally:
                events.put((token, tr('AI request finished in {seconds:.2f}s', seconds=time.perf_counter()-started)))
        return operation

    def _start_job(self, kind, operation, source="", target="", page_index=None, local=False):
        self._job_id += 1
        token = self._job_id
        page_index = self.index if page_index is None else page_index
        self._job = PageJob(token, kind, page_index, source, target, local)
        results, profiles = self._results, tuple(self._api_profiles())

        def work():
            # This function never touches a widget or a PyMuPDF document.
            try:
                result = operation()
                expected_type = CombinedAssessment if kind == "both" else Assessment
                if not isinstance(result, expected_type):
                    raise ValueError(
                        "The API did not return a valid confidence assessment. Your text was left unchanged."
                    )
                memory = getattr(result, "memory_update", None)
                results.put((token, result, None, memory))
            except Exception as exc:
                message = redact(str(exc), profiles)
                results.put((token, None, message or "The request could not be completed."))

        threading.Thread(target=work, name=f"ai-page-{page_index + 1}", daemon=True).start()
        verb = {"source": "Extracting", "translation": "Translating", "both": "Extracting and translating"}[kind]
        self.statusBar().showMessage(tr('{p0} page {p1}…', p0=tr(verb), p1=page_index + 1))
        self._refresh_controls()
        self._update_page_details()

    def record_token_usage(self, result):
        usage = getattr(result, "token_usage", None)
        if usage is None:
            return
        self.token_total += usage.total
        self.token_requests += 1
        self.token_label.setText(tr('Tokens: {p0:,}', p0=self.token_total))
        self.token_label.setToolTip(
            tr('Session total: {p0:,} tokens across {p1} reported page/chat responses.\nLast response: {p2:,} input · {p3:,} output · {p4:,} total.\nIncludes summary/context and image tokens when reported. Output includes reasoning when reported.\nUnreported usage, failed requests, connection tests and manual summary requests are not included.', p0=self.token_total, p1=self.token_requests, p2=usage.input, p3=usage.output, p4=usage.total)
        )

    def _drain_api_events(self):
        while not self._api_events.empty():
            token, message = self._api_events.get_nowait()
            if self._job and token == self._job.token:
                self.statusBar().showMessage(message)
                self.batch_event(message, self.batch_item())
                self._refresh_controls()

    def _poll_results(self):
        self._drain_api_events()
        while True:
            try:
                token, result, error, *extra = self._results.get_nowait()
                memory = extra[0] if extra else None
            except queue.Empty:
                return
            self.record_token_usage(result)
            if not self._job or token != self._job.token:
                continue  # A cancelled request must not replace newer work.
            # The worker enqueues activity before its result; drain any events
            # that arrived during this poll before recording completion.
            self._drain_api_events()
            job = self._job
            self._job = None
            if not job.local:
                self._verified_connection = None
            if not error and not job.local:
                if self._router:
                    active, _ = self._router.snapshot()
                    profile = self._router.profiles[active]
                    self._verified_connection = (profile.api_key, profile.model)
                else:
                    self._verified_connection = (self.api_key, self.model)
            if not error and not job.local:
                self._model_chosen = True
            self._refresh_controls()
            if self._batch:
                item = self.batch_item()
                usage = getattr(result, "token_usage", None)
                if item and usage:
                    item.tokens += usage.total
                    item.input_tokens += usage.input
                    item.output_tokens += usage.output
                    item.reports += 1
                if not error:
                    applied = time.perf_counter()
                    self._apply_result(job, result, memory)
                    self.batch_event(tr("Applied result in {seconds:.2f}s", seconds=time.perf_counter()-applied), item)
                self._finish_batch_page(error)
                continue
            if error:
                self._update_page_details()
                self.statusBar().showMessage(tr('Request failed. Your text was left unchanged.'), 7000)
                QMessageBox.warning(self, tr('Local OCR failed') if job.local else tr('API request failed'), error)
                continue
            self._apply_result(job, result, memory)

    def undo_previous_fix(self):
        if not self._previous_edit_undo or self._busy:
            return
        page, before, after, source = self._previous_edit_undo
        index = next((i for i, item in enumerate(self.book.pages) if item is page), None) if self.book else None
        if index is None or page.translation != after or page.source != source:
            self.statusBar().showMessage(tr('Cannot undo: that page has changed since the continuity fix.'), 7000)
            return
        page.translation = before
        self._previous_edit_undo = None
        self._mark_dirty(pages=(page,))
        self.thumbnails.set_review_count(index, len(review_flags(page, self.threshold.value() / 100)))
        if self.index == index:
            self._show_page(render=False)
        self._refresh_controls()
        self.statusBar().showMessage(tr('Undid the continuity fix on page {p0}.', p0=index + 1), 7000)

    def _apply_continuity(self, report):
        try:
            change = apply_previous_edit(self.book, report)
        except ValueError as exc:
            return str(exc)
        if change is None:
            return ""
        page, before, after, reason = change
        from .page_history import checkpoint
        checkpoint(page)
        snapshot = report.previous_page
        self.batch_event(tr("Changed ending of page {page}", page=snapshot.index + 1), self.batch_item())
        self._previous_edit_undo = (page, before, after, page.source)
        self.thumbnails.set_review_count(snapshot.index, len(review_flags(page, self.threshold.value() / 100)))
        if self.index == snapshot.index:
            self._show_page(render=False)
        self._refresh_controls()
        self.undo_continuity_action.setToolTip(reason)
        return f"Page {snapshot.index + 1} ending adjusted: {reason}"

    def _apply_result(self, job: PageJob, result: Assessment | CombinedAssessment, memory=None):
        """Save to the original page and refresh it only when it is visible."""
        if self._batch:
            self.snapshot_batch_item()  # Capture only changes made by applying this result.
        page = self.book.pages[job.page_index]
        page.reviewed_signature = ""
        replaced = ("source", "translation") if job.kind == "both" else (job.kind,)
        page.dismissed_reviews = [key for key in page.dismissed_reviews if key.split(":", 1)[0] not in replaced]
        for kind in replaced:
            page.review_links.pop(kind, None)
        if job.kind == "both":
            self.batch_event(tr("Adding text to source"), self.batch_item())
            page.source = result.extraction.text
            page.extraction_assessment = result.extraction
            self.batch_event(tr("Adding text to translation"), self.batch_item())
            page.translation = result.translation.text
            page.translation_assessment = result.translation
            page.translated_from = result.extraction.text
            page.target_language = job.target_language
        elif job.kind == "source":
            self.batch_event(tr("Adding text to source"), self.batch_item())
            page.source = result.text
            page.extraction_assessment = result
        else:
            self.batch_event(tr("Adding text to translation"), self.batch_item())
            page.translation = result.text
            page.translation_assessment = result
            page.translated_from = job.source
            page.target_language = job.target_language
        continuity_note = self._apply_continuity(result.translation if job.kind == "both" else result)
        if continuity_note and self._batch:
            self._batch.continuity_notes.append(continuity_note)
            if self.batch_item():
                self.batch_item().notes.append(continuity_note)
        memory_note = ""
        if memory is not None:
            previous, updated, error = memory
            if error:
                memory_note = f"Page {job.page_index + 1}: summary update failed; previous summary kept. {error}"
            elif self.book.ai_summary != previous:
                memory_note = "Summary changed during this run; kept your newer summary."
            else:
                self.batch_event(tr("Updating book summary"), self.batch_item())
                self.book.ai_summary = updated
                self.book.summary_signature = book_signature(self.book)
            if memory_note and self._batch:
                self._batch.summary_notes.append(memory_note)
                if self.batch_item():
                    self.batch_item().notes.append(memory_note)
        self._mark_dirty(pages=(page,))
        self.thumbnails.set_review_count(
            job.page_index, len(review_flags(page, self.threshold.value() / 100))
        )
        if self.index == job.page_index:
            self._show_page(render=False)
            self.tabs.setCurrentIndex(0 if job.kind == "source" else 1)
        action = {"source": "extracted", "translation": "translated", "both": "extracted and translated"}
        self.statusBar().showMessage(
            tr("Page {page} {action}. ", page=job.page_index + 1, action=tr(action[job.kind])) + continuity_note + (" " + memory_note if memory_note else ""),
            6000,
        )

    def cancel_request(self):
        if not self._busy:
            return
        self._request_cancel.set()
        summary = self._batch.summary if self._batch else ""
        if self._batch:
            self._batch.cancelled = True
            for item in self._batch.items:
                if item.status in ("queued", "running"):
                    item.status = "cancelled"
                    self.batch_event(tr("Cancelled"), item)
        self._batch = None
        self._job = None
        self._refresh_controls()
        self._update_page_details()
        self.statusBar().showMessage(
            tr('Cancelled. {p0} Completed results kept. A request already sent to the provider may still use quota.', p0=summary), 9000
        )

    def closeEvent(self, event):
        if not self._maybe_save():
            event.ignore()
            return
        self._request_cancel.set()
        if getattr(self, "chat_panel", None) is not None:
            self.chat_panel.reject()
        self._batch = None
        self._job = None
        self.thumbnails.timer.stop()
        self.thumbnails.book = None
        if self.book:
            self.book.close()
        # Workers are daemon threads, hold only snapshots, and use a queue;
        # closing cannot destroy a live QThread or access a deleted Qt object.
        event.accept()
