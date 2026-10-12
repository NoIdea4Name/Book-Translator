import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from PySide6.QtWidgets import QApplication
from book_translator.core import BookDocument, PageState, save_project, read_project
from book_translator.page_history import checkpoint, restore, validate_history
from book_translator.ui import MainWindow


class HistoryFindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_history_grouping_restore_and_confidence_signature(self):
        page = PageState(source='original')
        signature = page.review_signature()
        page.history.append(dict(page.history[0], reason='Restored'))
        self.assertEqual(signature, page.review_signature())
        page.source = 'edit'
        checkpoint(page, 'Edited', True)
        length = len(page.history)
        page.source = 'edited again'
        checkpoint(page, 'Edited', True)
        self.assertEqual(len(page.history), length)
        restore(page, page.history[0])
        self.assertEqual(page.source, 'original')
        self.assertEqual(page.history[-2]['source'], 'edited again')
        self.assertEqual(page.history[-1]['reason'], 'Restored')
        with self.assertRaises(ValueError):
            validate_history([{'source': 4}])

    def test_history_round_trip_and_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'page.png'
            Image.new('RGB', (30,40)).save(path)
            book = BookDocument.open([path])
            page = book.pages[0]
            for i in range(105):
                page.translation = str(i)
                checkpoint(page)
            self.assertEqual(len(page.history), 100)
            project = Path(temp)/'book.json'
            save_project(project, book, 0, 'Vietnamese', 'gemini-test')
            loaded = read_project(project)
            self.assertEqual(loaded.pages[0].history, page.history)
            book.close()

    def test_history_preferences_disable_grouping_limit_and_restore(self):
        from unittest.mock import patch
        with patch('book_translator.page_history.history_options', return_value=(True, 10, 0)):
            page = PageState(source='first')
            for i in range(20):
                page.source = str(i)
                checkpoint(page, 'Edited', True)
            self.assertEqual(len(page.history), 10)
        entry = dict(page.history[0])
        with patch('book_translator.page_history.history_options', return_value=(False, 100, 0)):
            page.source = 'unrecorded'
            checkpoint(page)
            self.assertNotEqual(page.history[-1]['source'], 'unrecorded')
            restore(page, entry)
            self.assertEqual(page.history[-2]['source'], 'unrecorded')
            self.assertEqual(page.source, entry['source'])

    def test_thumbnail_pointer_drag_release_reorders_and_escape_cancels(self):
        from PySide6.QtCore import Qt, QPointF, QEvent
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtTest import QTest
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp)/f'{i}.png' for i in range(3)]
            for p in paths: Image.new('RGB', (30,40)).save(p)
            window = MainWindow(); window.set_book(BookDocument.open(paths)); window.show()
            self.app.processEvents()
            strip = window.thumbnails
            original = list(window.book.pages)
            def move(point):
                event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(strip.viewport().mapToGlobal(point)), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(strip.viewport(), event)
            start = strip.visualItemRect(strip.item(0)).center()
            end = strip.visualItemRect(strip.item(2)).bottomLeft()
            end.setX(start.x())
            QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=start)
            move(end)
            self.assertTrue(strip._pointer_drag)
            self.assertIs(window.book.pages[0], original[0])
            self.assertEqual(strip._drop_position, 3)
            QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertIs(window.book.pages[2], original[0])
            self.assertIs(window.book.pages[0], original[1])
            self.assertFalse(strip._pointer_drag)
            self.app.processEvents()
            # Reverse direction, then cancel with Escape: no reorder should occur.
            order = list(window.book.pages)
            start = strip.visualItemRect(strip.item(2)).center()
            end = strip.visualItemRect(strip.item(0)).topLeft(); end.setX(start.x())
            QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=start)
            move(end)
            QTest.keyClick(strip, Qt.Key.Key_Escape)
            QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual([id(p) for p in window.book.pages], [id(p) for p in order])
            # Now complete the upward move.
            QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=start)
            move(end)
            QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertIs(window.book.pages[0], original[0])
            self.app.processEvents()
            # Move an existing multi-selection together with an ordinary drag.
            strip.clearSelection()
            strip.item(0).setSelected(True)
            strip.item(1).setSelected(True)
            start = strip.visualItemRect(strip.item(0)).center()
            end = strip.visualItemRect(strip.item(2)).bottomLeft(); end.setX(start.x())
            QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=start)
            move(end)
            self.assertEqual(strip._drag_rows, [0, 1])
            QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual([id(p) for p in window.book.pages], [id(original[i]) for i in (2, 0, 1)])
            self.assertEqual(sorted(strip.row(item) for item in strip.selectedItems()), [1, 2])
            window.dirty=False; window.close()

    def test_updates_do_not_scan_unrelated_history_or_rebuild_batch_cells(self):
        from unittest.mock import patch
        from book_translator.batch import BatchRun
        from book_translator.ui import PageJob
        from book_translator.confidence import Assessment
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp)/f'{i}.png' for i in range(3)]
            for p in paths: Image.new('RGB', (30,40)).save(p)
            window = MainWindow(); window.set_book(BookDocument.open(paths)); window.show()
            with patch('book_translator.page_history.checkpoint') as capture:
                window._mark_dirty()
                self.assertEqual(capture.call_count, 1)
                self.assertIs(capture.call_args.args[0], window.book.pages[0])
            window._apply_result(PageJob(1, 'source', 2, '', ''), Assessment('Result', .9, []))
            self.assertEqual(window.book.pages[2].history[-1]['source'], 'Result')
            run = BatchRun((0,1,2), 'source', '', False)
            window.prepare_batch(run)
            monitor = window.batch_window
            before = monitor.table.item(0, 0)
            changed = monitor.table.item(1, 1)
            run.items[1].status = 'running'
            monitor.refresh()
            self.assertIs(monitor.table.item(0,0), before)
            self.assertIs(monitor.table.item(1,1), changed)
            self.assertEqual(changed.text(), 'Running')
            monitor.poll_refresh()
            with patch.object(monitor, 'refresh') as refresh:
                monitor.poll_refresh()
                refresh.assert_not_called()
            window.dirty=False; window.close()

    def test_review_signature_never_copies_history(self):
        class NoCopy:
            def __deepcopy__(self, memo):
                raise AssertionError('History must not be copied for a confidence check')
        page = PageState(source='source')
        signature = page.review_signature()
        page.history = [NoCopy()]
        self.assertEqual(page.review_signature(), signature)

    def test_current_page_replace_scope_unicode_undo_and_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp)/f'{i}.png' for i in range(2)]
            for p in paths: Image.new('RGB', (30,40)).save(p)
            window = MainWindow(); window.set_book(BookDocument.open(paths)); window.show()
            window.source_editor.setPlainText('😀 cat Cat cat')
            window.translation_editor.setPlainText('cat')
            window.book.pages[1].source = 'cat'
            window.tabs.setCurrentIndex(0)
            search = window.page_find; search.open()
            search.query.setText('cat'); search.case.setChecked(True)
            search.replacement.setText('dog'); search.replace_all()
            self.assertEqual(window.source_editor.toPlainText(), '😀 dog Cat dog')
            self.assertEqual(window.book.pages[0].source, '😀 dog Cat dog')
            self.assertEqual(window.translation_editor.toPlainText(), 'cat')
            self.assertEqual(window.book.pages[1].source, 'cat')
            window.source_editor.undo()
            self.assertEqual(window.source_editor.toPlainText(), '😀 cat Cat cat')
            search.case.setChecked(False); search.move(1); search.move(1)
            search.replacement.setText(''); search.replace_one()
            self.assertEqual(window.source_editor.toPlainText(), '😀 cat  cat')
            window.source_editor.setReadOnly(True)
            search.replace_all()
            self.assertEqual(window.source_editor.toPlainText(), '😀 cat  cat')
            window.dirty=False; window.close()

    def test_find_unicode_wrap_tab_and_page_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp)/f'{i}.png' for i in range(2)]
            for p in paths: Image.new('RGB', (30,40)).save(p)
            window = MainWindow()
            window.set_book(BookDocument.open(paths))
            window.show()
            window.source_editor.setPlainText('😀 repeat Repeat')
            window.translation_editor.setPlainText('repeat')
            window.tabs.setCurrentIndex(0)
            search = window.page_find
            search.open()
            search.query.setText('repeat')
            self.assertEqual(len(search.matches), 2)
            search.move(1)
            self.assertEqual(window.source_editor.textCursor().selectionStart(), 3)
            search.move(1)
            self.assertEqual(window.source_editor.textCursor().selectionStart(), 10)
            search.move(1)
            self.assertEqual(window.source_editor.textCursor().selectionStart(), 3)
            search.case.setChecked(True)
            self.assertEqual(len(search.matches), 1)
            window.tabs.setCurrentIndex(1)
            self.assertEqual(len(search.matches), 1)
            window.go_to(1)
            self.assertEqual(len(search.matches), 0)
            window.dirty = False
            window.close()
