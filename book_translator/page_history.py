"""Bounded, project-persisted text revisions. No API calls or image copies."""
from datetime import datetime
import time
from functools import lru_cache

FIELDS = ('source', 'translation', 'translated_from', 'target_language')
LIMIT = 100


@lru_cache(maxsize=1)
def history_options():
    from PySide6.QtCore import QSettings
    settings = QSettings('BookTranslator', 'BookTranslator')
    try:
        return (settings.value('history/enabled', True, type=bool),
                max(10, min(LIMIT, int(settings.value('history/limit', LIMIT)))),
                max(0, min(10, float(settings.value('history/group_seconds', 1.2)))))
    except (ValueError, TypeError):
        return True, LIMIT, 1.2


def edit_history_settings(parent):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QDialog, QFormLayout, QCheckBox, QSpinBox, QDoubleSpinBox, QDialogButtonBox
    from .i18n import tr
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr('History settings'))
    layout = QFormLayout(dialog)
    enabled, limit, seconds = history_options()
    record = QCheckBox(tr('Record page text history')); record.setChecked(enabled)
    maximum = QSpinBox(); maximum.setRange(10, LIMIT); maximum.setValue(limit)
    grouping = QDoubleSpinBox(); grouping.setRange(0, 10); grouping.setSingleStep(0.2); grouping.setValue(seconds)
    layout.addRow(record)
    layout.addRow(tr('Revisions per page'), maximum)
    layout.addRow(tr('Group typing within (seconds)'), grouping)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        settings = QSettings('BookTranslator', 'BookTranslator')
        settings.setValue('history/enabled', record.isChecked())
        settings.setValue('history/limit', maximum.value())
        settings.setValue('history/group_seconds', grouping.value())
        settings.sync()
        history_options.cache_clear()


def validate_history(value):
    if not isinstance(value, list) or len(value) > LIMIT:
        raise ValueError('Invalid page history.')
    keys = (*FIELDS, 'time', 'reason')
    if any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in keys) for row in value):
        raise ValueError('Invalid page history entry.')
    return [{key: row[key] for key in keys} for row in value]


def checkpoint(page, reason='Updated', merge=False, force=False):
    enabled, limit, seconds = history_options()
    if not enabled and page.history and not force:
        return
    text = {key: getattr(page, key) for key in FIELDS}
    if page.history and all(page.history[-1][key] == text[key] for key in FIELDS):
        return
    now = time.monotonic()
    entry = dict(text, time=datetime.now().isoformat(timespec='seconds'), reason=reason)
    # Merge consecutive typing, keeping the revision from before typing began.
    previous = getattr(page, '_history_edit_time', 0)
    if merge and len(page.history) > 1 and page.history[-1]['reason'] == 'Edited' and now - previous < seconds:
        page.history[-1] = entry
    else:
        page.history.append(entry)
        del page.history[:-limit]
    page._history_edit_time = now if merge else 0


def restore(page, entry):
    checkpoint(page, force=True)
    for key in FIELDS:
        setattr(page, key, entry[key])
    page.extraction_assessment = page.translation_assessment = None
    page.reviewed_signature = ''
    page.dismissed_reviews.clear()
    page.review_links.clear()
    checkpoint(page, 'Restored', force=True)


def show_history(window):
    if window.book and window.book.pages:
        HistoryDialog(window).exec()


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QTabWidget, QApplication, QSplitter, QWidget, QListWidget, QListWidgetItem,
    QAbstractItemView)
from .widgets import button, label
from .i18n import tr
from .preview import ThumbnailStrip


class HistoryDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle(tr('Page history'))
        self.setObjectName('historyDialog')
        self.resize(1100, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        header = QHBoxLayout()
        header.addWidget(label(tr('Page history'), 'title'))
        header.addStretch()
        header.addWidget(button(tr('History settings'), lambda: edit_history_settings(self), 'small'))
        layout.addLayout(header)
        split = QSplitter()
        self.pages = ThumbnailStrip()
        self.pages.set_editing_enabled(False)
        self.pages.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pages.setToolTip(tr('Choose a page to view its revisions'))
        self.pages.setMinimumWidth(150)
        self.pages.setMaximumWidth(210)
        self.pages.set_book(window.book)
        split.addWidget(self.pages)
        revisions_panel = QWidget()
        revisions_layout = QVBoxLayout(revisions_panel)
        revisions_layout.setContentsMargins(8, 0, 8, 0)
        revisions_layout.addWidget(label(tr('Revisions'), 'eyebrow'))
        self.versions = QListWidget()
        self.versions.setObjectName('historyRevisions')
        revisions_layout.addWidget(self.versions)
        split.addWidget(revisions_panel)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.caption = label('', 'muted')
        content_layout.addWidget(self.caption)
        self.tabs = QTabWidget()
        self.editors = [QPlainTextEdit(), QPlainTextEdit()]
        for editor, title in zip(self.editors, ('Source text', 'Translation')):
            editor.setReadOnly(True)
            self.tabs.addTab(editor, tr(title))
        content_layout.addWidget(self.tabs)
        split.addWidget(content)
        split.setChildrenCollapsible(False)
        split.setSizes([165, 245, 650])
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        actions.addWidget(button(tr('Copy'), lambda: QApplication.clipboard().setText(self.editors[self.tabs.currentIndex()].toPlainText())))
        self.apply = button(tr('Restore revision'), self.apply_revision, 'primary')
        actions.addWidget(self.apply)
        actions.addStretch()
        actions.addWidget(button(tr('Close'), self.accept))
        layout.addLayout(actions)
        self.pages.currentRowChanged.connect(self.populate)
        self.versions.currentRowChanged.connect(self.display)
        self.pages.setCurrentRow(window.index)

    def entry(self):
        item = self.versions.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def populate(self):
        index = self.pages.currentRow()
        if index < 0:
            return
        page = self.window.book.pages[index]
        self.caption.setText(self.window.book.page_name(index))
        self.versions.blockSignals(True)
        self.versions.clear()
        for number, entry in reversed(list(enumerate(page.history, 1))):
            title = tr('Revision {number}', number=number) + ' · ' + tr(entry['reason'])
            item = QListWidgetItem(title + '\n' + entry['time'].replace('T', ' '))
            item.setData(Qt.ItemDataRole.UserRole, dict(entry))
            self.versions.addItem(item)
        self.versions.blockSignals(False)
        self.versions.setCurrentRow(0)
        self.display()

    def display(self):
        entry = self.entry()
        for editor, field in zip(self.editors, FIELDS):
            editor.setPlainText(entry[field] if entry else '')
        page = self.window.book.pages[self.pages.currentRow()]
        changed = bool(entry) and any(entry[key] != getattr(page, key) for key in FIELDS)
        self.apply.setEnabled(changed and not self.window._busy)

    def apply_revision(self):
        if self.window._busy or not self.entry():
            return
        index = self.pages.currentRow()
        restore(self.window.book.pages[index], self.entry())
        self.window._mark_dirty()
        self.window.go_to(index)
        self.window._show_page(render=False)
        self.populate()
        self.window.statusBar().showMessage(tr('Page revision restored.'), 5000)

    def done(self, result):
        self.pages.timer.stop()
        super().done(result)
