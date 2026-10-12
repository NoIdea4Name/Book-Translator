"""Modeless, snapshot-based multi-provider chat. Workers never touch widgets or the book."""

from .i18n import tr

import html
import queue
import threading

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QPlainTextEdit, QTextBrowser,
    QVBoxLayout,
)

from .api_rotation import ApiRouter, redact
from .book_context import options_for_page, book_signature
from .widgets import button, label


class ChatPrompt(QPlainTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if not event.isAutoRepeat():
                self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatPanel(QDialog):
    def __init__(self, window, router):
        super().__init__(window, Qt.WindowType.Tool)
        self.setWindowTitle(tr('Ask AI'))
        self.setObjectName("chatPanel")
        self.resize(420, 510)
        self.setMinimumSize(360, 410)
        self.window = window
        self.router = router
        self.book = window.book
        self.page = self.book.pages[window.index]
        self.kind = "source" if window.tabs.currentIndex() == 0 else "translation"
        self.editor = window.source_editor if self.kind == "source" else window.translation_editor
        self.original = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        self.start, self.end = cursor.selectionStart(), cursor.selectionEnd()
        self.selected = cursor.selectedText().replace("\u2029", "\n")
        # Qt offsets are UTF-16 units; slicing Python text would mishandle emoji.
        context_cursor = QTextCursor(self.editor.document())
        context_cursor.setPosition(max(0, self.start - 1000))
        context_cursor.setPosition(min(self.editor.document().characterCount() - 1, self.end + 1000), QTextCursor.MoveMode.KeepAnchor)
        self.context = {
            "page": self.book.page_name(window.index), "field": self.kind,
            "target_language": window.language.currentText(),
            "selected_text": self.selected,
            "surrounding_text": context_cursor.selectedText().replace("\u2029", "\n"),
        }
        self.region = window.preview.region_image()
        self.history = []
        self.reply = None
        self.pending = False
        self.cancel = threading.Event()
        self.results = queue.Queue()
        self.token = 0
        self.applied = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        self.profiles = tuple(router.profiles)
        self.model_selector = QComboBox()
        self.model_selector.setAccessibleName(tr('Chat model'))
        for index, profile in enumerate(self.profiles):
            self.model_selector.addItem(f'{index + 1} · {profile.label}', index)
        self.model_selector.setCurrentIndex(router.active)
        self.model_selector.currentIndexChanged.connect(self.select_model)
        layout.addWidget(self.model_selector)
        context_card = QFrame()
        context_card.setObjectName("chatContext")
        context_layout = QVBoxLayout(context_card)
        context_layout.setContentsMargins(10, 8, 10, 8)
        context_layout.setSpacing(3)
        context_layout.addWidget(label(tr('{p0} · {p1}', p0=self.context['page'], p1=self.kind.title()), "eyebrow"))
        self.selection_label = label((self.selected[:140] + ("…" if len(self.selected) > 140 else "") if self.selected else tr('No text selected')), "muted", True)
        self.selection_label.setMaximumHeight(36)
        self.selection_label.setToolTip(self.selected[:2000])
        context_layout.addWidget(self.selection_label)
        layout.addWidget(context_card)
        self.transcript = QTextBrowser()
        self.transcript.setObjectName("chatTranscript")
        self.transcript.setOpenLinks(False)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setPlaceholderText(tr('Ask about this passage…'))
        self.transcript.setAccessibleName(tr('Chat conversation'))
        layout.addWidget(self.transcript, 1)
        self.proposal_card = QFrame()
        self.proposal_card.setObjectName("chatProposal")
        proposal_layout = QVBoxLayout(self.proposal_card)
        proposal_layout.setContentsMargins(10, 8, 10, 8)
        proposal_layout.setSpacing(5)
        proposal_header = QHBoxLayout()
        proposal_header.addWidget(label(tr('Suggested edit'), "eyebrow"))
        proposal_header.addStretch()
        proposal_actions = QHBoxLayout()
        self.apply_mode = QComboBox()
        self.apply_mode.addItems([tr('Add to page'), tr('Replace selection')])
        self.apply_mode.setAccessibleName(tr('How to apply suggested text'))
        self.apply_mode.setToolTip(tr('Add at the end of this page, or replace the original selection'))
        self.apply_mode.setMaximumWidth(140)
        self.apply_mode.currentIndexChanged.connect(self.update_apply_button)
        proposal_actions.addWidget(self.apply_mode)
        proposal_actions.addStretch()
        self.apply_button = button(tr('Apply'), self.apply_replacement, "primary")
        self.apply_button.setAccessibleName(tr('Apply suggested text'))
        self.apply_button.hide()
        proposal_actions.addWidget(self.apply_button)
        self.dismiss_button = button("×", self.dismiss_proposal, "iconButton", tr('Dismiss suggested edit'))
        self.dismiss_button.setFixedSize(24, 24)
        self.dismiss_button.setAccessibleName(tr('Dismiss suggested edit'))
        proposal_header.addWidget(self.dismiss_button)
        proposal_layout.addLayout(proposal_header)
        self.proposal = QPlainTextEdit()
        self.proposal.setObjectName("chatReplacement")
        self.proposal.setReadOnly(True)
        self.proposal.setFixedHeight(72)
        self.proposal.setAccessibleName(tr('Suggested replacement'))
        self.proposal.hide()
        proposal_layout.addWidget(self.proposal)
        proposal_layout.addLayout(proposal_actions)
        self.proposal_card.hide()
        layout.addWidget(self.proposal_card)
        composer = QFrame()
        composer.setObjectName("chatComposer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(8, 6, 8, 8)
        composer_layout.setSpacing(6)
        self.prompt = ChatPrompt()
        self.prompt.send_requested.connect(self.send)
        self.prompt.setObjectName("chatPrompt")
        self.prompt.setPlaceholderText(tr('Ask AI…'))
        self.prompt.setFixedHeight(60)
        composer_layout.addWidget(self.prompt)
        attachments = QHBoxLayout()
        attachments.setSpacing(6)
        self.attach_page = QCheckBox(tr('Page'))
        self.attach_page.setToolTip(tr('Attach the full page image'))
        self.attach_page.setAccessibleName(tr('Attach page image'))
        self.attach_region = QCheckBox(tr('Region'))
        self.attach_region.setToolTip(tr('Attach the selected image region'))
        self.attach_region.setAccessibleName(tr('Attach selected region'))
        self.attach_region.setEnabled(self.region is not None)
        self.attach_region.setChecked(self.region is not None)
        attachments.addWidget(self.attach_page)
        attachments.addWidget(self.attach_region)
        self.region_button = button(tr('Select region'), self.pick_region, "small")
        attachments.addWidget(self.region_button)
        attachments.addStretch()
        self.status = label("", "muted", True)
        self.status.setMaximumHeight(42)
        self.status.hide()
        self.send_button = button(tr('Send'), self.send, "primary", tr('Enter to send · Shift+Enter for a new line'))
        self.stop_button = button(tr('Stop'), self.stop, "small")
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        attachments.addWidget(self.stop_button)
        attachments.addWidget(self.send_button)
        composer_layout.addLayout(attachments)
        composer_layout.addWidget(self.status)
        layout.addWidget(composer)
        self.send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.send_shortcut.activated.connect(self.send)
        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self.poll_results)
        self.poll.start()
        self.region_connection = window.preview.region_changed.connect(self.region_selected)
        self.picking = False

    def select_model(self, index):
        if self.pending or index < 0:
            return
        # Selected API is first, including when automatic rotation is disabled.
        ordered = self.profiles[index:] + self.profiles[:index]
        self.router = ApiRouter(ordered, self.window.rotation_enabled, self.window.failure_limit)

    def show_panel(self):
        """Center on the screen containing the app, independently of the mouse."""
        self.show()
        screen = self.window.screen() or QApplication.primaryScreen()
        frame = self.frameGeometry()
        area = screen.availableGeometry() if screen else self.window.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())
        self.raise_()
        self.activateWindow()
        self.prompt.setFocus()

    def set_status(self, text):
        self.status.setText(text)
        self.status.setToolTip(text)
        self.status.setVisible(bool(text))

    def append_message(self, role, text):
        # Treat model output as text, never executable HTML or active links.
        color = "#147d68" if role == "You" else "#52626b"
        escaped = html.escape(text).replace("\n", "<br>")
        self.transcript.append(
            f'<p style="margin-top:10px; margin-bottom:4px; color:{color}; font-size:11px;"><b>{html.escape(tr(role))}</b></p>'
            f'<p style="margin-top:0; margin-bottom:12px; color:#243238;">{escaped}</p>'
        )

    def hide_proposal(self):
        self.proposal.hide()
        self.apply_button.hide()
        self.proposal_card.hide()

    def dismiss_proposal(self):
        self.reply = None
        self.hide_proposal()
        self.history.append({"role": "user", "text": "I dismissed the suggested edit without applying it."})

    def update_apply_button(self):
        replacement = self.reply.replacement if self.reply else None
        replacing = self.apply_mode.currentIndex() == 1
        self.apply_button.setText((tr('Replace') if replacing else tr('Add')))
        self.apply_button.setEnabled(
            replacement is not None and not self.applied
            and (bool(self.selected) if replacing else bool(replacement))
        )

    def current_page_matches(self):
        w = self.window
        return w.book is self.book and bool(w.book.pages) and w.book.pages[w.index] is self.page

    def pick_region(self):
        if not self.current_page_matches():
            self.set_status(tr('Return to this page to select a region.'))
            return
        self.picking = True
        self.hide()
        self.window.preview.select_region(True)
        self.window.preview.setFocus()

    def region_selected(self):
        if not self.picking and not self.current_page_matches():
            return
        # Ignore the initial clear emitted on mouse press.
        if self.window.preview.region_mode:
            return
        was_picking = self.picking
        self.picking = False
        if self.current_page_matches():
            self.region = self.window.preview.region_image()
        else:
            self.region = None
        self.attach_region.setEnabled(self.region is not None and not self.pending)
        self.attach_region.setChecked(self.region is not None)
        if was_picking:
            self.show()
            self.raise_()
            self.activateWindow()

    def set_pending(self, pending):
        self.pending = pending
        for widget in (self.send_button, self.prompt, self.attach_page, self.region_button, self.model_selector):
            widget.setEnabled(not pending)
        self.attach_region.setEnabled(not pending and self.region is not None)
        self.stop_button.setEnabled(pending)
        self.stop_button.setVisible(pending)
        self.send_button.setVisible(not pending)

    def send(self):
        prompt = self.prompt.toPlainText().strip()
        if self.pending or not prompt:
            return
        attachments, names = [], []
        if self.attach_page.isChecked():
            if self.window.book is not self.book:
                self.set_status(tr('This workspace has changed. Open a new chat.'))
                return
            index = next((i for i, page in enumerate(self.book.pages) if page is self.page), None)
            if index is None:
                self.set_status(tr('This page was deleted. Open a new chat.'))
                return
            try:
                attachments.append(self.book.ocr_image(index))
            except Exception as exc:
                self.set_status(str(exc))
                return
            names.append("full page")
        if self.attach_region.isChecked() and self.region is not None:
            attachments.append((self.region, "image/png"))
            names.append("selected region")
        context = dict(self.context, image_attachments=names)
        history = self.history[-12:]
        index = next((i for i, page in enumerate(self.book.pages) if page is self.page), None)
        if self.window.book is not self.book or index is None:
            self.set_status(tr('This page or workspace has changed. Open a new chat.'))
            return
        options = options_for_page(self.book, index, self.window.generation_options)
        self.cancel = threading.Event()
        self.token += 1
        token, cancel, results, router = self.token, self.cancel, self.results, self.router
        self.reply = None
        self.applied = False
        self.hide_proposal()
        self.append_message("You", prompt + ("\nAttached: " + ", ".join(names) if names else ""))
        self.prompt.clear()
        self.set_status(tr('Asking AI…'))
        self.set_pending(True)

        def work():
            try:
                reply = router.call("chat", (context, history, prompt, attachments), options, cancel)
                results.put((token, prompt, reply, None))
            except Exception as exc:
                results.put((token, prompt, None, redact(str(exc), router.profiles)))

        threading.Thread(target=work, daemon=True).start()

    def poll_results(self):
        while not self.results.empty():
            token, prompt, reply, error = self.results.get_nowait()
            self.window.record_token_usage(reply)
            if token != self.token or self.cancel.is_set():
                continue
            self.set_pending(False)
            if error:
                self.set_status(error)
                self.prompt.setPlainText(prompt)
                continue
            memory_note = ""
            memory = getattr(reply, "memory_update", None)
            if memory is not None and self.window.book is self.book:
                previous, updated, error = memory
                if error:
                    memory_note = "Summary unchanged: " + error
                elif self.book.ai_summary != previous:
                    memory_note = "Kept a newer summary updated by another run."
                else:
                    self.book.ai_summary = updated
                    self.book.summary_signature = book_signature(self.book)
                    self.window._mark_dirty()
            self.reply = reply
            self.history.extend([{"role": "user", "text": prompt}, {
                "role": "assistant", "answer": reply.answer, "replacement": reply.replacement,
            }])
            self.history = self.history[-12:]
            if reply.answer:
                self.append_message("AI", reply.answer)
            if reply.replacement is not None:
                self.proposal.setPlainText(reply.replacement or "(Delete selected text)")
                self.proposal.show()
                self.apply_mode.setCurrentIndex(0)
                self.update_apply_button()
                self.apply_button.show()
                self.proposal_card.show()
            self.set_status(memory_note)

    def apply_replacement(self):
        if self.reply is None or self.reply.replacement is None or self.applied:
            return
        if not self.current_page_matches() or self.window._busy or getattr(self.page, self.kind) != self.original or self.editor.toPlainText() != self.original:
            self.set_status(tr('The page or text changed. Re-select the text and open a new chat.'))
            return
        replacing = self.apply_mode.currentIndex() == 1
        if replacing and not self.selected:
            self.set_status(tr('Select text before using Replace selection.'))
            return
        if not replacing and not self.reply.replacement:
            self.set_status(tr('There is no text to add. Choose Replace selection to delete.'))
            return
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        if replacing:
            cursor.setPosition(self.start)
            cursor.setPosition(self.end, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            separator = "" if not self.original or self.original.endswith("\n\n") else ("\n" if self.original.endswith("\n") else "\n\n")
            cursor.insertText(separator)
            self.start = cursor.position()
        cursor.insertText(self.reply.replacement)
        cursor.endEditBlock()
        self.end = cursor.position()
        cursor.setPosition(self.start, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.window.tabs.setCurrentIndex(0 if self.kind == "source" else 1)
        self.original = self.editor.toPlainText()
        self.selected = self.reply.replacement
        self.context["selected_text"] = self.selected
        context_cursor = QTextCursor(self.editor.document())
        context_cursor.setPosition(max(0, self.start - 1000))
        context_cursor.setPosition(min(self.editor.document().characterCount() - 1, self.end + 1000), QTextCursor.MoveMode.KeepAnchor)
        self.context["surrounding_text"] = context_cursor.selectedText().replace("\u2029", "\n")
        self.selection_label.setText(self.selected[:180] + ("…" if len(self.selected) > 180 else ""))
        self.selection_label.setToolTip(self.selected[:2000])
        self.history.append({"role": "user", "text": "I applied the suggested text using " + ("replacement" if replacing else "add to page") + ". The context now contains the applied text."})
        self.applied = True
        self.hide_proposal()
        self.append_message("Applied", self.selected or "Selected text deleted.")
        self.set_status(tr('Applied · Ctrl+Z to undo in editor'))

    def stop(self):
        self.cancel.set()
        self.token += 1
        self.set_pending(False)
        self.set_status(tr('Stopped. A sent request may still use quota.'))

    def done(self, result):
        self.cancel.set()
        self.token += 1
        self.poll.stop()
        if self.picking:
            self.picking = False
            self.window.preview.select_region(False)
        # done() may run twice: closing chat, then replacing it on reopening.
        if self.region_connection is not None:
            QObject.disconnect(self.region_connection)
            self.region_connection = None
        super().done(result)


def open_chat(window):
    if not window.book or not window.book.pages:
        return
    profiles = tuple(profile for profile in window._api_profiles() if profile.api_key)
    if not profiles:
        window.statusBar().showMessage(tr('Add an API entry in Settings to use chat.'), 8000)
        window.settings()
        return
    existing = getattr(window, "chat_panel", None)
    if existing is not None:
        existing.reject()
        existing.deleteLater()
    signature = (profiles, window.rotation_enabled, window.failure_limit)
    if signature != getattr(window, "_chat_router_signature", None):
        window._chat_router = ApiRouter(*signature)
        window._chat_router_signature = signature
    window.chat_panel = ChatPanel(window, window._chat_router)
    window.chat_panel.show_panel()
