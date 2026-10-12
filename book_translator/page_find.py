"""Compact current-page search, using Qt cursors for correct Unicode offsets."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QTextDocument, QShortcut, QKeySequence
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QCheckBox
from .widgets import button, label
from .i18n import tr


class PageFind(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setObjectName("pageFind")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(3)
        layout = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(tr('Find in current page…'))
        self.query.setAccessibleName(tr('Find in current page'))
        self.case = QCheckBox(tr('Match case'))
        self.count = label('', 'muted')
        outer.addWidget(self.query)
        replace_row = QHBoxLayout()
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText(tr('Replace with…'))
        self.replacement.setAccessibleName(tr('Replace with'))
        self.replace_button = button(tr('Replace'), self.replace_one, 'small')
        self.replace_all_button = button(tr('Replace all'), self.replace_all, 'small')
        replace_row.addWidget(self.replacement, 1)
        replace_row.addWidget(self.replace_button)
        replace_row.addWidget(self.replace_all_button)
        outer.addLayout(replace_row)
        outer.addLayout(layout)
        layout.addWidget(self.case)
        layout.addStretch()
        layout.addWidget(self.count)
        layout.addWidget(button('↑', lambda: self.move(-1), 'iconButton', tr('Previous match')))
        layout.addWidget(button('↓', lambda: self.move(1), 'iconButton', tr('Next match')))
        layout.addWidget(button('×', self.hide, 'iconButton', tr('Close')))
        close_shortcut = QShortcut(QKeySequence("Escape"), self)
        close_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        close_shortcut.activated.connect(self.hide)
        self.matches = []
        self.position = -1
        self.query.textChanged.connect(self.refresh)
        self.case.toggled.connect(self.refresh)
        self.query.returnPressed.connect(lambda: self.move(1))
        window.tabs.currentChanged.connect(self.refresh)
        window.source_editor.textChanged.connect(self.refresh)
        window.translation_editor.textChanged.connect(self.refresh)
        self.hide()

    def editor(self):
        return self.window.source_editor if self.window.tabs.currentIndex() == 0 else self.window.translation_editor

    def open(self):
        self.show()
        selection = self.editor().textCursor().selectedText()
        if selection and '\u2029' not in selection:
            self.query.setText(selection)
        self.refresh()
        self.query.setFocus()
        self.query.selectAll()

    def refresh(self, *args):
        if not self.isVisible():
            return
        self.matches = []
        query = self.query.text()
        if query:
            document = self.editor().document()
            cursor = QTextCursor(document)
            flags = QTextDocument.FindFlag.FindCaseSensitively if self.case.isChecked() else QTextDocument.FindFlag(0)
            while True:
                cursor = document.find(query, cursor, flags)
                if cursor.isNull():
                    break
                self.matches.append(QTextCursor(cursor))
        self.position = -1
        self.update_actions()
        self.count.setText(tr('{count} matches', count=len(self.matches)))

    def move(self, direction):
        if not self.matches:
            return
        self.position = (self.position + direction) % len(self.matches) if self.position >= 0 else (0 if direction > 0 else len(self.matches)-1)
        self.editor().setTextCursor(self.matches[self.position])
        self.editor().ensureCursorVisible()
        self.count.setText(f'{self.position+1} / {len(self.matches)}')

    def update_actions(self):
        allowed = bool(self.matches) and not self.editor().isReadOnly()
        self.replace_button.setEnabled(allowed)
        self.replace_all_button.setEnabled(allowed)

    def replace_one(self):
        if self.editor().isReadOnly() or not self.matches:
            return
        index = self.position if self.position >= 0 else 0
        cursor = QTextCursor(self.matches[index])
        cursor.insertText(self.replacement.text())
        after = cursor.position()
        self.editor().setTextCursor(cursor)
        self.refresh()
        if self.matches:
            next_index = next((i for i, match in enumerate(self.matches) if match.selectionStart() >= after), 0)
            self.position = next_index - 1
            self.move(1)

    def replace_all(self):
        if self.editor().isReadOnly() or not self.matches:
            return
        # Work backwards so replacements cannot shift remaining match offsets.
        matches = [QTextCursor(match) for match in self.matches]
        replacement = self.replacement.text()
        transaction = QTextCursor(self.editor().document())
        transaction.beginEditBlock()
        try:
            for cursor in reversed(matches):
                cursor.insertText(replacement)
        finally:
            transaction.endEditBlock()
        self.refresh()
