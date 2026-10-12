import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from book_translator.confidence import Assessment, CombinedAssessment
from book_translator.core import BookDocument
from book_translator.ui import MainWindow
from test_core import make_pdf


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        paths = []
        for index, color in enumerate(("red", "green", "blue")):
            path = Path(self.temp.name) / f"{index}.png"
            Image.new("RGB", (120, 180), color).save(path)
            paths.append(path)
        self.window = MainWindow()
        from dataclasses import replace
        self.window.generation_options = replace(self.window.generation_options, auto_update_summary=False)
        self.window.api_key = "test-key"
        self.window.show()
        self.window.set_book(BookDocument.open(paths))
        self.app.processEvents()
        self.dialog = patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        self.dialog.start()

    def tearDown(self):
        self.window.cancel_request()
        self.window.dirty = False
        self.window.close()
        self.app.processEvents()
        self.dialog.stop()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents()
            self.window._poll_results()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Batch did not reach expected state")

    def select(self, *indices):
        self.window.thumbnails.clearSelection()
        for index in indices:
            self.window.thumbnails.item(index).setSelected(True)

    def click(self, index, modifier=Qt.KeyboardModifier.NoModifier):
        strip = self.window.thumbnails
        item = strip.item(index)
        strip.scrollToItem(item)
        self.app.processEvents()
        QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, modifier,
                         strip.visualItemRect(item).center())

    def test_activity_history_is_ordered_copyable_and_independent_of_selection(self):
        from book_translator.batch import BatchRun
        run = BatchRun((0, 1), 'translate', 'Vietnamese', False)
        self.window.prepare_batch(run)
        self.window._batch = run
        self.window.batch_event('Running', run.items[0])
        self.window.batch_event('Retrying API', run.items[0])
        self.window.batch_event('Adding text to translation', run.items[0])
        self.window.batch_event('Completed', run.items[0])
        monitor = self.window.batch_window
        monitor.refresh()
        history = monitor.details.toPlainText()
        self.assertLess(history.index('Running'), history.index('Retrying API'))
        self.assertLess(history.index('Retrying API'), history.index('Adding text'))
        monitor.table.selectRow(1)
        self.assertEqual(monitor.details.toPlainText(), history)
        monitor.copy_history.click()
        self.assertEqual(self.app.clipboard().text(), history)
        self.assertEqual(monitor.table.item(0, 1).text(), 'Completed')
        self.assertFalse(hasattr(monitor, 'before_text'))

    def test_pause_resume_tokens_changes_and_retry_only_failures(self):
        from book_translator.token_usage import TokenUsage
        for i, page in enumerate(self.window.book.pages):
            page.source = f"Source {i}"
        self.select(0, 1, 2)
        release = threading.Event()
        calls = []
        failed_once = set()
        def translate(source, *args):
            calls.append(source)
            if source == "Source 0":
                release.wait(3)
            if source == "Source 1" and source not in failed_once:
                failed_once.add(source)
                raise ValueError("Temporary test failure")
            result = Assessment("Translated " + source, .9, [])
            result.token_usage = TokenUsage(10, 20, 30)
            return result
        with patch("book_translator.ui.GeminiService.translate", side_effect=translate):
            self.window.translate()
            batch = self.window._batch
            self.window.toggle_batch_pause()
            release.set()
            self.wait_for(lambda: self.window._job is None)
            self.assertIs(self.window._batch, batch)
            self.assertEqual(calls, ["Source 0"])
            self.assertEqual([item.status for item in batch.items], ["completed", "queued", "queued"])
            self.window.batch_window.close()  # Closing is independent of run control.
            self.window.toggle_batch_pause()
            self.wait_for(lambda: not self.window._busy)
            self.assertEqual([item.status for item in batch.items], ["completed", "failed", "completed"])
            self.assertEqual(sum(item.tokens for item in batch.items), 60)
            self.assertEqual(batch.items[0].changes[0][1:], ("Translation", "", "Translated Source 0"))
            self.window.retry_failed_batch()
            self.wait_for(lambda: not self.window._busy)
            self.assertEqual(calls, ["Source 0", "Source 1", "Source 2", "Source 1"])
            self.assertEqual([item.attempts for item in batch.items], [1, 2, 1])
            self.assertEqual([item.status for item in batch.items], ["completed"] * 3)
            self.assertEqual(sum(item.tokens for item in batch.items), 90)
            self.assertEqual(batch.issues, [])

    def test_changes_include_continuity_edit_on_unselected_previous_page(self):
        from book_translator.continuity import ContinuityAssessment, PreviousPage
        previous = self.window.book.pages[0]
        previous.source = "Earlier source"
        previous.translation = "Earlier translation. To be continued..."
        previous.translated_from = previous.source
        previous.target_language = "English"
        self.window.language.setCurrentText("English")
        snapshot = PreviousPage(0, previous.source, previous.translation, previous.source, "English", previous.source)
        for index in (1, 2):
            self.window.book.pages[index].source = f"Source {index}"
        self.select(1, 2)
        def translated(source, *args):
            report = Assessment("Translation " + source, .9, [])
            if source == "Source 1":
                return ContinuityAssessment(report, snapshot, {"old_text": " To be continued...", "new_text": "", "reason": "Remove invented filler"})
            return report
        with patch("book_translator.ui.GeminiService.translate", side_effect=translated):
            self.window.translate()
            batch = self.window._batch
            self.wait_for(lambda: not self.window._busy)
        self.assertEqual(previous.translation, "Earlier translation.")
        changes = batch.items[0].changes
        self.assertTrue(any(before == snapshot.translation and after == previous.translation for _, _, before, after in changes))
        self.assertEqual(len(changes), 2)

    def test_cancel_marks_remaining_rows_and_retry_rejects_new_workspace(self):
        self.select(0, 1, 2)
        gate = threading.Event()
        def complete(*args):
            gate.wait(2)
            return Assessment("Ignored", .9, [])
        with patch("book_translator.ui.GeminiService.translate", side_effect=complete):
            self.window.translate()
            batch = self.window._batch
            self.window.cancel_request()
            gate.set()
        self.assertTrue(batch.cancelled)
        self.assertEqual([item.status for item in batch.items], ["cancelled"] * 3)
        self.assertTrue(all(not page.translation for page in self.window.book.pages))
        old_book = self.window.book
        self.window.set_book(BookDocument([], "images", []))
        self.assertIsNone(self.window._last_batch)
        self.window.retry_failed_batch()
        self.assertIsNone(self.window._batch)

    def test_ctrl_shift_and_select_all_selection_survive_refresh(self):
        self.click(0)
        self.click(2, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window._selected_pages(), [0, 2])
        self.window._show_page(render=False)
        self.assertEqual(self.window._selected_pages(), [0, 2])
        self.click(0)
        self.click(2, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.window._selected_pages(), [0, 1, 2])
        self.click(1)
        self.assertEqual(self.window._selected_pages(), [1])
        self.window.select_all_button.click()
        self.assertEqual(self.window._selected_pages(), [0, 1, 2])

    def test_alt_click_previews_without_changing_single_or_multiple_selection(self):
        for selected in ((0,), (0, 1)):
            self.select(*selected)
            self.click(2, Qt.KeyboardModifier.AltModifier)
            self.assertEqual(self.window.index, 2)
            self.assertEqual(self.window._selected_pages(), list(selected))
            self.window._show_page(render=False)
            self.assertEqual(self.window._selected_pages(), list(selected))
            self.click(0)
        self.click(2, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window._selected_pages(), [0, 2])

    def test_each_action_processes_only_selected_images_in_order(self):
        for method, kind in (("extract", "source"), ("translate", "translation"),
                             ("extract_and_translate", "both")):
            with self.subTest(method=method):
                for index, page in enumerate(self.window.book.pages):
                    page.source = f"Source {index}" if kind == "translation" else ""
                    page.translation = ""
                self.window._show_page(render=False)
                self.select(0, 2)
                results = [Assessment("Result 0", .9, []), Assessment("Result 2", .8, [])]
                if kind == "both":
                    results = [CombinedAssessment(r, Assessment("T " + r.text, .9, [])) for r in results]
                with patch(f"book_translator.ui.GeminiService.{method}", side_effect=results) as call:
                    getattr(self.window, method)()
                    self.wait_for(lambda: not self.window._busy)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(self.window._selected_pages(), [0, 2])
                pages = self.window.book.pages
                if kind == "translation":
                    self.assertEqual([c.args[0] for c in call.call_args_list], ["Source 0", "Source 2"])
                    self.assertEqual(pages[0].translated_from, "Source 0")
                else:
                    self.assertEqual(pages[0].source, "Result 0")
                    self.assertEqual(pages[2].source, "Result 2")
                    self.assertEqual(pages[1].source, "")
                if kind == "both":
                    self.assertEqual(pages[2].translation, "T Result 2")
                    self.assertFalse(pages[2].translation_is_stale)
                self.assertEqual(pages[1].translation, "")
                self.assertIn("2 completed", self.window.statusBar().currentMessage())

    def test_failure_keeps_existing_text_and_continues(self):
        self.window.book.pages[1].source = "Keep me"
        self.select(0, 1, 2)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm, patch(
            "book_translator.ui.GeminiService.extract",
            side_effect=[Assessment("First", .9, []), ValueError("Bad response"), Assessment("Third", .9, [])]
        ) as call:
            self.window.extract()
            batch = self.window._batch
            self.wait_for(lambda: not self.window._busy)
        self.assertEqual(call.call_count, 3)
        confirm.assert_called_once()
        self.assertEqual([p.source for p in self.window.book.pages], ["First", "Keep me", "Third"])
        self.assertIn("Page 2: Bad response", batch.issues)
        self.assertEqual(self.window._selected_pages(), [1])
        self.assertEqual(self.window.index, 1)

    def test_cancel_discards_active_result_and_stops_pending_pages(self):
        entered = threading.Event()
        gate = threading.Event()
        calls = []

        def extract(*args):
            calls.append(args)
            if len(calls) == 2:
                entered.set()
                gate.wait(3)
            return Assessment(f"Result {len(calls)}", .9, [])

        self.select(0, 1, 2)
        with patch("book_translator.ui.GeminiService.extract", side_effect=extract):
            self.window.extract()
            self.wait_for(entered.is_set)
            self.window.go_to(2)
            self.assertTrue(self.window.source_editor.isReadOnly())
            self.window.cancel_request()
            self.assertFalse(self.window.source_editor.isReadOnly())
            gate.set()
            completed = self.window._results.get(timeout=3)
            self.window._results.put(completed)
            self.window._poll_results()
            self.app.processEvents()
        self.assertEqual(len(calls), 2)
        self.assertEqual([p.source for p in self.window.book.pages], ["Result 1", "", ""])

    def test_translate_includes_empty_source_and_uses_frozen_selection(self):
        self.window.book.pages[0].source = "First"
        self.window.book.pages[2].source = "Third"
        self.select(0, 1, 2)
        gate = threading.Event()

        def translate(source, *args):
            gate.wait(2)
            return Assessment("T " + source, .9, [])

        with patch("book_translator.ui.GeminiService.translate", side_effect=translate) as call:
            self.window.translate()
            batch = self.window._batch
            self.select(1)
            self.window.go_to(1)
            gate.set()
            self.wait_for(lambda: not self.window._busy)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(self.window.book.pages[2].translation, "T Third")
        self.assertEqual(batch.issues, [])
        self.assertEqual(self.window.book.pages[1].source, "")
        self.assertEqual(self.window.book.pages[1].translation, "T ")
        self.assertEqual(call.call_args_list[1].args[0], "")
        self.assertIsInstance(call.call_args_list[1].args[2], bytes)
        self.assertEqual(self.window._selected_pages(), [1])

    def test_offline_batch_skips_scans_without_api_calls(self):
        path = Path(self.temp.name) / "book.pdf"
        make_pdf(path)
        self.window.set_book(BookDocument.open([path]))
        self.window.select_all_button.click()
        with patch("book_translator.ui.GeminiService") as service:
            self.window.extract()
            batch = self.window._batch
            self.wait_for(lambda: not self.window._busy)
        service.assert_not_called()
        self.assertEqual(batch.completed, 2)
        self.assertIn("Page 3: Skipped", batch.issues[0])
        self.assertIn("Chapter Two", self.window.book.pages[1].source)

    def test_declining_overwrite_starts_no_requests(self):
        self.window.book.pages[2].translation = "Keep"
        self.select(0, 2)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm, patch(
            "book_translator.ui.GeminiService.extract_and_translate"
        ) as call:
            self.window.extract_and_translate()
        confirm.assert_called_once()
        call.assert_not_called()
        self.assertFalse(self.window._busy)
        self.assertEqual(self.window.book.pages[2].translation, "Keep")
