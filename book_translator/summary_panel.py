"""Editable book memory and a read-only view of the current request context."""

from .i18n import tr

import queue
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QPlainTextEdit,
    QTabWidget, QVBoxLayout,
)

from .api_rotation import ApiRouter, redact
from .book_context import (
    SUMMARY_LIMIT, book_signature, reference_context, summary_material, update_summary,
)
from .widgets import button, label


class SummaryDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.book = window, window.book
        self.setWindowTitle(tr('Book summary & context'))
        self.resize(720, 590)
        self.signature = self.book.summary_signature
        self.cancel = threading.Event()
        self.results = queue.Queue()
        self.token = 0
        self.pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        tabs = QTabWidget()
        self.editor = QPlainTextEdit(self.book.ai_summary)
        self.editor.setPlaceholderText(tr('Names, terminology, translation style, relationships and story context…'))
        self.editor.setAccessibleName(tr('Editable book summary'))
        tabs.addTab(self.editor, tr('Book summary'))
        self.context_preview = QPlainTextEdit()
        self.context_preview.setReadOnly(True)
        self.context_preview.setAccessibleName(tr('Current page reference context'))
        self.context_preview.setPlainText(reference_context(self.book, window.index, window.generation_options)
                                          or "No reference context is currently included.")
        tabs.addTab(self.context_preview, tr('Current context'))
        layout.addWidget(tabs, 1)
        self.status = label("", "muted", True)
        layout.addWidget(self.status)
        stale = self.signature and self.signature != book_signature(self.book)
        self.status.setText((tr('Pages changed since this summary was saved. Review or update it.') if stale else tr('Saved with this project. Inclusion is controlled in Settings → Context.')))
        row = QHBoxLayout()
        self.generate_button = button(tr('Update from current page'), self.generate, "primary")
        self.stop_button = button(tr('Stop'), self.stop)
        self.stop_button.hide()
        row.addWidget(self.generate_button)
        row.addWidget(self.stop_button)
        row.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll_results)
        self.timer.start()

    def set_pending(self, pending):
        self.pending = pending
        self.editor.setReadOnly(pending)
        self.generate_button.setEnabled(not pending)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not pending)
        self.stop_button.setVisible(pending)

    def generate(self):
        if self.pending:
            return
        profiles = tuple(self.window._api_profiles())
        if not any(profile.api_key for profile in profiles):
            self.status.setText(tr('Configure an API key in Settings before generating. You can still write a summary here.'))
            return
        previous = self.editor.toPlainText()
        if len(previous) > SUMMARY_LIMIT:
            self.status.setText(tr('Keep the summary within 16,000 characters.'))
            return
        try:
            chunks = summary_material(self.book, self.window.index)
            page_number = self.window.index + 1
            if not chunks:
                self.status.setText(tr('This page has no text. Extract it first, or write notes manually.'))
                return
            router = ApiRouter(profiles, self.window.rotation_enabled, self.window.failure_limit)
        except Exception as exc:
            self.status.setText(redact(str(exc), profiles))
            return
        signature = book_signature(self.book)
        options, target = self.window.generation_options, self.window.language.currentText()
        self.cancel = threading.Event()
        self.token += 1
        token, cancel, results = self.token, self.cancel, self.results
        self.set_pending(True)
        self.status.setText(tr('Updating memory from page {p0}…', p0=page_number))

        def work():
            try:
                summary = update_summary(router, chunks, previous, target, options, cancel,
                                         lambda message: results.put((token, "progress", message)))
                results.put((token, "done", (summary, signature, page_number)))
            except Exception as exc:
                results.put((token, "error", redact(str(exc), profiles)))

        threading.Thread(target=work, daemon=True).start()

    def poll_results(self):
        while not self.results.empty():
            token, kind, payload = self.results.get_nowait()
            if token != self.token or self.cancel.is_set():
                continue
            if kind == "progress":
                self.status.setText(payload)
                continue
            self.set_pending(False)
            if kind == "error":
                self.status.setText(payload)
                continue
            text, self.signature, page_number = payload
            self.editor.setPlainText(text)
            self.status.setText(tr('Memory updated from page {p0}. Review, then Save to use it.', p0=page_number))

    def stop(self):
        self.cancel.set()
        self.token += 1
        self.set_pending(False)
        self.status.setText(tr('Stopped. Your draft is unchanged. A sent request may still use quota.'))

    def accept(self):
        if self.pending:
            return
        text = self.editor.toPlainText().strip()
        if len(text) > SUMMARY_LIMIT:
            self.status.setText(tr('Keep the summary within 16,000 characters.'))
            return
        if self.window.book is not self.book:
            self.status.setText(tr('The workspace changed. Close this window and try again.'))
            return
        signature = book_signature(self.book) if text else ""
        if text != self.book.ai_summary or signature != self.book.summary_signature:
            self.book.ai_summary = text
            self.book.summary_signature = signature
            self.window._mark_dirty()
        super().accept()

    def done(self, result):
        self.cancel.set()
        self.token += 1
        self.timer.stop()
        super().done(result)
