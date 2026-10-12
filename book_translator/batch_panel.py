"""Nonmodal batch monitor and GUI-thread bookkeeping. Closing it does not stop a run."""
from datetime import datetime
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QPlainTextEdit, QSplitter,
    QApplication, QProgressBar, QWidget,
)
from .batch import BatchItem
from .i18n import tr
from .widgets import button, label

STATUS_NAMES = {'queued': 'Queued', 'running': 'Running', 'completed': 'Completed',
                'failed': 'Failed', 'skipped': 'Skipped', 'cancelled': 'Cancelled', 'removed': 'Removed'}


class BatchWindow(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle(tr('Batch processing'))
        self.setObjectName("batchWindow")
        self.resize(1060, 760)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(label(tr("Batch processing"), "title"))
        self.summary = label('', 'muted', True)
        layout.addWidget(self.summary)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("batchTable")
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setHorizontalHeaderLabels([tr(x) for x in ('Page', 'Status', 'Reported tokens', 'Attempts')])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self.show_details)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_page())
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setStyleSheet('QPlainTextEdit { font-family: Consolas, monospace; font-size: 12px; padding: 8px; }')
        self.details.setAccessibleName(tr('Activity history'))
        history_panel = QWidget()
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(0, 8, 0, 0)
        heading = QHBoxLayout()
        heading.addWidget(label(tr('Activity history'), 'eyebrow'))
        heading.addStretch()
        self.copy_history = button(tr('Copy history'), lambda: QApplication.clipboard().setText(self.details.toPlainText()), 'small')
        heading.addWidget(self.copy_history)
        history_layout.addLayout(heading)
        history_layout.addWidget(self.details)
        split.addWidget(history_panel)
        self.history_run = None
        self.history_count = 0
        split.setChildrenCollapsible(False)
        split.setSizes([240, 340])
        layout.addWidget(split, 1)
        row = QHBoxLayout()
        self.pause = button(tr('Pause'), window.toggle_batch_pause, "primary")
        self.retry = button(tr('Retry failed'), window.retry_failed_batch)
        self.stop = button(tr('Cancel'), window.cancel_request)
        self.visit = button(tr('Open page'), self.open_page)
        for item in (self.pause, self.retry, self.stop, self.visit):
            row.addWidget(item)
        row.addStretch()
        row.addWidget(button(tr('Close'), self.close))
        layout.addLayout(row)
        self.fingerprint = None
        self.row_cache = {}
        self.table_run = None
        self.poll_key = None
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll_refresh)
        self.timer.start()

    def poll_refresh(self):
        if not self.isVisible():
            return
        run = self.window._last_batch
        key = (id(run), len(run.history), run.position, run.paused, run.cancelled,
               self.window._batch is run, bool(self.window._job)) if run else None
        if key != self.poll_key:
            self.poll_key = key
            self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        run = self.window._last_batch
        if run is None:
            self.table.setRowCount(0)
            self.summary.setText(tr('No batch has been run in this workspace.'))
            self.details.clear()
            self.history_run, self.history_count = None, 0
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            for control in (self.pause, self.retry, self.stop, self.visit):
                control.setEnabled(False)
            return
        active = self.window._batch is run
        valid = self.window.book is run.book
        self.pause.setEnabled(active)
        self.pause.setText(tr('Resume') if run.paused else tr('Pause'))
        self.pause.setToolTip(tr('Pause takes effect after the current request finishes.'))
        self.stop.setEnabled(active)
        self.retry.setEnabled(valid and not self.window._busy and any(item.status in ('failed', 'skipped') for item in run.items))
        self.visit.setEnabled(valid and self.table.currentRow() >= 0)
        state = tr('Pausing after current page') if active and run.paused and self.window._job else tr('Paused') if active and run.paused else tr('Running') if active else tr('Cancelled') if run.cancelled else tr('Finished')
        self.progress.setRange(0, max(1, len(run.items)))
        self.progress.setValue(sum(item.status not in ("queued", "running") for item in run.items))
        tokens = sum(item.tokens for item in run.items)
        self.summary.setText(tr('{state} · {summary} · {tokens:,} reported tokens', state=state, summary=run.summary, tokens=tokens))
        self.summary.setToolTip(tr('Only provider-reported usage is counted. Failed requests may use unreported tokens. Closing this window does not stop processing.'))
        self.update_history(run)
        fingerprint = (id(run), tuple((id(item), item.status, item.activity, item.attempts, item.tokens, item.error, len(item.changes), tuple(item.notes)) for item in run.items))
        if fingerprint == self.fingerprint:
            return
        self.fingerprint = fingerprint
        selected = self.table.currentRow()
        self.table.blockSignals(True)
        rebuild = self.table_run is not run or self.table.rowCount() != len(run.items)
        if rebuild:
            self.table_run = run
            self.row_cache.clear()
            self.table.setRowCount(len(run.items))
        for row, item in enumerate(run.items):
            texts = (item.name, item.activity or tr(STATUS_NAMES[item.status]), f'{item.tokens:,}' if item.reports or run.offline else '—', str(item.attempts))
            tooltip = item.error or '\n'.join(item.notes)
            row_key = (texts, item.status, tooltip)
            if self.row_cache.get(row) == row_key:
                continue
            self.row_cache[row] = row_key
            for column, text in enumerate(texts):
                cell = self.table.item(row, column)
                if cell is None:
                    cell = QTableWidgetItem()
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(row, column, cell)
                if cell.text() != text:
                    cell.setText(text)
                if column == 1:
                    colors = {'completed': '#167d65', 'failed': '#b44040', 'running': '#276ca8', 'skipped': '#996a15'}
                    cell.setForeground(QColor(colors.get(item.status, '#697b84')))
                cell.setToolTip(tooltip)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.blockSignals(False)
        if run.items and (rebuild or selected < 0):
            self.table.selectRow(min(max(selected, 0), len(run.items)-1))
        self.show_details()

    def selected_item(self):
        run = self.window._last_batch
        row = self.table.currentRow()
        return run.items[row] if run and 0 <= row < len(run.items) else None

    def show_details(self):
        item = self.selected_item()
        self.visit.setEnabled(item is not None and self.window.book is self.window._last_batch.book)

    def update_history(self, run):
        if self.history_run is not run:
            self.details.clear()
            self.history_run, self.history_count = run, 0
        if self.history_count == len(run.history):
            return
        scroll = self.details.verticalScrollBar()
        follow = scroll.value() >= scroll.maximum() - 2
        position = scroll.value()
        # Append without resetting selection, so history remains easy to copy.
        cursor = self.details.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        prefix = '\n' if self.history_count else ''
        cursor.insertText(prefix + '\n'.join(run.history[self.history_count:]))
        self.history_count = len(run.history)
        scroll.setValue(scroll.maximum() if follow else position)

    def open_page(self):
        run, item = self.window._last_batch, self.selected_item()
        if not run or not item or self.window.book is not run.book:
            return
        index = next((i for i, page in enumerate(run.book.pages) if page is item.page), None)
        if index is not None:
            self.window.go_to(index)


class BatchControls:
    """Methods mixed into MainWindow; no worker thread reads these objects."""
    def show_batch(self):
        if getattr(self, 'batch_window', None) is None:
            self.batch_window = BatchWindow(self)
        self.batch_window.show()
        self.batch_window.raise_()
        self.batch_window.refresh()

    def prepare_batch(self, run):
        run.book = self.book
        run.items = [BatchItem(self.book.pages[i], i, self.book.page_name(i)) for i in run.pages]
        self._last_batch = run
        self.batch_event(tr('Batch started'), run=run)
        self.show_batch()

    def batch_event(self, message, item=None, run=None, update_status=True):
        run = run or self._batch
        if run is None:
            return
        if item is not None and update_status:
            item.activity = message
        prefix = f'{item.name} · ' if item is not None else ''
        run.history.append(f'{datetime.now():%H:%M:%S} · {prefix}{message}')

    def batch_item(self):
        run = self._batch
        if run is None or run.position >= len(run.pages):
            return None
        page = self.book.pages[run.pages[run.position]]
        return next((item for item in run.items if item.page is page), None)

    def start_batch_item(self):
        run, item = self._batch, self.batch_item()
        if item is None:
            return
        item.status, item.error = 'running', ''
        item.attempts += 1
        self.batch_event(tr("Running"), item)
        self.snapshot_batch_item()

    def snapshot_batch_item(self):
        run = self._batch
        index = run.pages[run.position]
        # The preceding page can be changed by a continuity edit in this request.
        run.snapshots = [(self.book.pages[i], self.book.page_name(i), self.book.pages[i].source, self.book.pages[i].translation)
                         for i in range(max(0, index-1), index+1)]
        run.memory_before = self.book.ai_summary

    def finish_batch_item(self, error):
        run, item = self._batch, self.batch_item()
        if item is None:
            return
        item.error = error or ''
        item.status = ('skipped' if error.startswith('Skipped:') else 'failed') if error else 'completed'
        for page, name, source, translation in run.snapshots:
            for field, before, after in [('Source text', source, page.source), ('Translation', translation, page.translation)]:
                if before != after:
                    item.changes.append((name, field, before, after))
        if self.book.ai_summary != run.memory_before:
            item.notes.append(tr('Book summary updated.'))
        for note in item.notes[item.logged_notes:]:
            self.batch_event(note, item, update_status=False)
        item.logged_notes = len(item.notes)
        if item.reports:
            self.batch_event(tr('{input:,} input · {output:,} output · {total:,} total', input=item.input_tokens, output=item.output_tokens, total=item.tokens), item, update_status=False)
        self.batch_event(tr(STATUS_NAMES[item.status]), item)
        if error:
            self.batch_event(error, item, update_status=False)
        run.snapshots = []

    def toggle_batch_pause(self):
        run = self._batch
        if run is None:
            return
        run.paused = not run.paused
        self.batch_event(tr("Pausing after current page") if run.paused and self._job else tr("Paused") if run.paused else tr("Resumed"))
        if not run.paused and self._job is None:
            QTimer.singleShot(0, lambda: self._advance_batch(run))
        self.statusBar().showMessage(tr('Pause takes effect after the current request finishes.') if run.paused and self._job else tr('Paused') if run.paused else tr('Resumed'))

    def retry_failed_batch(self):
        run = self._last_batch
        if self._busy or run is None or self.book is not run.book:
            return
        indices = []
        for item in run.items:
            if item.status not in ('failed', 'skipped'):
                continue
            index = next((i for i, page in enumerate(self.book.pages) if page is item.page), None)
            if index is None:
                item.status = 'removed'
                self.batch_event(tr('Removed'), item, run)
                continue
            item.index = index
            indices.append(index)
        if not indices or (not run.offline and not self._ensure_key()):
            return
        for item in run.items:
            if item.index in indices and item.status in ('failed', 'skipped'):
                item.notes.append(tr('Retry requested. Previous error: {error}', error=item.error))
                item.status = 'queued'
                self.batch_event(tr('Retry queued'), item, run)
        run.pages, run.position = tuple(indices), 0
        run.completed = sum(item.status == 'completed' for item in run.items)
        run.issues, run.unsuccessful_pages = [], []
        run.paused, run.cancelled = False, False
        self._router = None  # Explicit retry allows a fresh attempt with configured APIs.
        self._router_signature = None
        self._batch = run
        self.show_batch()
        self._advance_batch(run)
