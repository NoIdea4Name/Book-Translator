import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from io import BytesIO

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from PySide6.QtWidgets import QApplication, QMessageBox

from book_translator.core import BookDocument, ProjectData
from book_translator.confidence import Assessment, RegionAssessment, CombinedAssessment
from book_translator.preview import REVIEW_COUNT_ROLE
from book_translator.ui import MainWindow
from test_core import make_pdf


class InterfaceTests(unittest.TestCase):
    def test_review_navigation_skips_dismissed_checked_and_visits_manual_flags(self):
        page = self.window.book.pages[0]
        page.source = "First. Second."
        page.extraction_assessment = Assessment(page.source, .9, [
            RegionAssessment("First.", "First.", .4, "Blur", (10, 10, 30, 100)),
            RegionAssessment("Second.", "Second.", .5, "Blur", (50, 10, 80, 100)),
        ])
        self.window.book.pages[1].manual_flag = True
        self.window._show_page(render=False)
        self.window.next_review_passage()
        self.assertEqual(self.window.source_editor.textCursor().selectedText(), "First.")
        self.window.next_review_passage()
        self.assertEqual(self.window.source_editor.textCursor().selectedText(), "Second.")
        self.window.dismiss_current_review()
        self.assertIn("source:1", page.dismissed_reviews)
        self.assertEqual(self.window.source_editor.textCursor().selectedText(), "First.")
        self.window.check_and_next_review()
        self.assertTrue(page.is_reviewed)
        self.assertEqual(self.window.index, 1)
        self.window.check_and_next_review()
        self.assertFalse(self.window.book.pages[1].manual_flag)
        self.assertIn("No flagged pages", self.window.statusBar().currentMessage())

    def test_translate_empty_source_uses_image_and_keeps_source_empty(self):
        self.window.api_key = "test-key"
        self.window._refresh_controls()
        self.assertTrue(self.window.translate_button.isEnabled())
        self.assertTrue(self.window.translate_action.isEnabled())
        with patch("book_translator.ui.GeminiService.translate", return_value=Assessment("Direct translation", .9, [])) as translate, patch("book_translator.ui.GeminiService.extract") as extract:
            self.window.translate_button.click()
            self.wait_for(lambda: self.window._job is None)
        translate.assert_called_once()
        extract.assert_not_called()
        self.assertEqual(translate.call_args.args[0], "")
        self.assertIsInstance(translate.call_args.args[2], bytes)
        page = self.window.book.pages[0]
        self.assertEqual(page.source, "")
        self.assertIsNone(page.extraction_assessment)
        self.assertEqual(page.translation, "Direct translation")
        self.assertEqual(page.translated_from, "")
        self.assertFalse(page.translation_is_stale)
        self.window.source_editor.setPlainText("New source")
        self.assertTrue(page.translation_is_stale)

    def test_combined_action_saves_both_to_original_page(self):
        self.window.api_key = "test-key"
        gate = threading.Event()
        result = CombinedAssessment(Assessment("Hello", .9, []), Assessment("Xin chào", .85, []))

        def complete(*args):
            gate.wait(2)
            return result

        with patch("book_translator.ui.GeminiService.extract_and_translate", side_effect=complete) as call:
            self.window.combined_button.click()
            self.assertFalse(self.window.combined_button.isEnabled())
            self.window.go_to(1)
            gate.set()
            self.wait_for(lambda: self.window._job is None)
        call.assert_called_once()
        page = self.window.book.pages[0]
        self.assertEqual(page.source, "Hello")
        self.assertEqual(page.translation, "Xin chào")
        self.assertEqual(page.translated_from, "Hello")
        self.assertFalse(page.translation_is_stale)
        self.assertEqual(self.window.book.pages[1].source, "")
        self.assertEqual(self.window.source_editor.toPlainText(), "")

    def test_combined_failure_preserves_both_texts(self):
        self.window.api_key = "test-key"
        page = self.window.book.pages[0]
        page.source, page.translation = "Original", "Existing translation"
        with patch("book_translator.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as confirm, patch(
            "book_translator.ui.QMessageBox.warning"
        ), patch("book_translator.ui.GeminiService.extract_and_translate", side_effect=ValueError("Invalid pair")):
            self.window.combined_button.click()
            self.wait_for(lambda: self.window._job is None)
        confirm.assert_called_once()
        self.assertEqual((page.source, page.translation), ("Original", "Existing translation"))

    def test_project_cannot_replace_model_chosen_this_session(self):
        self.window.model = "gemini-current"
        self.window._model_chosen = True
        book = BookDocument.open([self.path])
        project = ProjectData([self.path], book.pages, 0, "Vietnamese", "gemini-old", "pdf")
        self.window.set_book(book, project=project)
        self.assertEqual(self.window.model, "gemini-current")

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "book.pdf"
        make_pdf(self.path)
        self.window = MainWindow()
        from dataclasses import replace
        self.window.generation_options = replace(self.window.generation_options, auto_update_summary=False)
        self.window.show()
        self.window.set_book(BookDocument.open([self.path]))
        self.app.processEvents()

    def tearDown(self):
        self.window.cancel_request()
        self.window.dirty = False
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            self.window._poll_results()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for the background task")

    def test_extract_edit_navigate_and_translate_original_source(self):
        self.window.extract_button.click()
        text = self.window.source_editor.toPlainText()
        self.assertIn("Chapter One", text)
        self.window.source_editor.insertPlainText("Edited. ")
        edited = self.window.source_editor.toPlainText()
        self.window.next_button.click()
        self.assertEqual(self.window.source_editor.toPlainText(), "")
        self.window.previous_button.click()
        self.assertEqual(self.window.source_editor.toPlainText(), edited)
        self.window.api_key = "test-key"
        with patch(
            "book_translator.ui.GeminiService.translate",
            return_value=Assessment("Bản dịch", 0.95, []),
        ) as translate:
            self.window.translate_button.click()
            self.wait_for(lambda: self.window._job is None)
            translate.assert_called_once()
            args = translate.call_args.args
            self.assertEqual(args[:2], (edited, "Vietnamese"))
            self.assertEqual(args[3], "image/png")
            with Image.open(BytesIO(args[2])) as image:
                self.assertGreater(image.width, 100)
        self.assertEqual(self.window.translation_editor.toPlainText(), "Bản dịch")
        self.assertEqual(self.window.book.pages[0].source, edited)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.window.source_editor.setPlainText("Changed after translating")
        self.assertIn("Source text changed", self.window.page_note.text())

    def test_navigation_during_request_routes_result_to_origin_page(self):
        self.window.source_editor.setPlainText("First page")
        gate = threading.Event()

        def operation():
            gate.wait(2)
            return Assessment("Trang đầu", 0.95, [])

        self.window._start_job("translation", operation, "First page", "Vietnamese")
        self.assertTrue(self.window.source_editor.isReadOnly())
        self.window.go_to(1)
        self.assertFalse(self.window.source_editor.isReadOnly())
        self.window.source_editor.setPlainText("Second page")
        gate.set()
        self.wait_for(lambda: self.window._job is None)
        self.assertEqual(self.window.book.pages[0].translation, "Trang đầu")
        self.assertEqual(self.window.book.pages[1].translation, "")
        self.assertEqual(self.window.source_editor.toPlainText(), "Second page")

    def test_cancelled_result_does_not_overwrite_new_work(self):
        self.window.source_editor.setPlainText("Original")
        gate = threading.Event()
        done = threading.Event()

        def operation():
            gate.wait(2)
            done.set()
            return Assessment("Discard this", 0.95, [])

        self.window._start_job("source", operation)
        self.window.cancel_request()
        self.window.source_editor.setPlainText("Keep this edit")
        self.window._poll.stop()
        gate.set()
        completed = self.window._results.get(timeout=3)
        self.window._results.put(completed)
        self.window._poll_results()
        self.assertEqual(self.window.book.pages[0].source, "Keep this edit")

    def test_empty_pdf_extraction_preserves_previous_source(self):
        self.window.go_to(2)
        self.window.source_editor.setPlainText("Typed manually")
        with patch.object(QMessageBox, "information") as message:
            self.window.extract()
        self.assertTrue(message.called)
        self.assertEqual(self.window.source_editor.toPlainText(), "Typed manually")

    def test_failed_request_preserves_existing_translation(self):
        page = self.window.book.pages[0]
        page.source = "Original"
        page.translation = "Keep translation"
        self.window._show_page(render=False)

        def operation():
            raise RuntimeError("Connection failed")

        with patch.object(QMessageBox, "warning") as warning:
            self.window._start_job("translation", operation, page.source, "Vietnamese")
            self.wait_for(lambda: self.window._job is None)
            self.assertTrue(warning.called)
        self.assertEqual(page.translation, "Keep translation")
        self.assertFalse(self.window.source_editor.isReadOnly())

    def test_thumbnail_click_navigates_and_preserves_text(self):
        self.window.source_editor.setPlainText("Keep this on page one")
        self.assertEqual(self.window.thumbnails.count(), 3)
        thumb = self.window.thumbnails.item(1)
        self.window.thumbnails.scrollToItem(thumb)
        self.app.processEvents()
        point = self.window.thumbnails.visualItemRect(thumb).center()
        QTest.mouseClick(self.window.thumbnails.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.window.index, 1)
        self.assertEqual(self.window.page_button.text(), "Page 2")
        self.assertEqual(self.window.book.pages[0].source, "Keep this on page one")
        self.window.previous_button.click()
        self.assertEqual(self.window.thumbnails.currentRow(), 0)

    def test_confidence_flags_threshold_and_edited_text(self):
        page = self.window.book.pages[0]
        page.source = "Original text"
        page.translation = "Bản dịch"
        page.translated_from = page.source
        page.extraction_assessment = Assessment(
            page.source,
            0.9,
            [
                RegionAssessment(
                    "Original text", "Original text", 0.61, "Blurry type", (100, 100, 220, 500)
                ),
            ],
        )
        page.translation_assessment = Assessment(
            page.translation,
            0.91,
            [
                RegionAssessment(
                    "Original text", "Bản dịch", 0.72, "Ambiguous meaning", (300, 100, 420, 500)
                ),
            ],
        )
        self.window._show_page()
        self.assertEqual(len(self.window.preview.overlays), 2)
        self.assertEqual(self.window.thumbnails.item(0).data(REVIEW_COUNT_ROLE), 2)
        self.assertIn("Needs review", self.window.thumbnails.item(0).toolTip())
        self.assertEqual(self.window.review_list.count(), 2)
        self.window.threshold.setValue(70)
        self.assertEqual(len(self.window.preview.overlays), 1)
        self.window.show_flags.setChecked(False)
        self.assertEqual(len(self.window.preview.overlays), 0)
        self.assertEqual(self.window.thumbnails.item(0).data(REVIEW_COUNT_ROLE), 1)
        self.window.show_flags.setChecked(True)
        self.assertEqual(len(self.window.preview.overlays), 1)
        self.window.source_editor.setPlainText("Edited source")
        self.assertEqual(len(self.window.preview.overlays), 1)
        self.assertIn("outdated", self.window.confidence_label.text())
        self.assertEqual(self.window.thumbnails.item(0).data(REVIEW_COUNT_ROLE), 1)

    def test_unlocated_region_is_listed_without_false_rectangle(self):
        page = self.window.book.pages[0]
        page.source = "Pasted text not on the page"
        page.extraction_assessment = Assessment(
            page.source,
            0.9,
            [
                RegionAssessment(page.source, page.source, 0.5, "Cannot locate passage", ()),
            ],
        )
        self.window._show_page()
        self.assertEqual(len(self.window.preview.overlays), 0)
        self.assertEqual(self.window.review_list.count(), 1)
        self.assertIn("unlocated", self.window.review_list.item(0).text())

    def test_passage_links_follow_typing_replacement_deletion_and_undo(self):
        from PySide6.QtGui import QTextCursor
        for kind in ("source", "translation"):
            page = self.window.book.pages[0]
            page.source = "Repeat Repeat"
            page.translation = "Repeat Repeat"
            page.translated_from = page.source
            report = Assessment("Repeat Repeat", .9, [
                RegionAssessment("Repeat", "Repeat", .4, "Blur", (20, 20, 40, 200)),
                RegionAssessment("Repeat", "Repeat", .4, "Blur", (200, 20, 240, 200)),
            ])
            page.extraction_assessment = report if kind == "source" else None
            page.translation_assessment = report if kind == "translation" else None
            page.review_links.clear()
            self.window._show_page()
            editor = self.window.source_editor if kind == "source" else self.window.translation_editor
            self.window._focus_review(self.window.review_list.item(0))
            cursor = editor.textCursor()
            cursor.insertText("Corrected")
            self.window._focus_review(self.window.review_list.item(0))
            self.assertEqual(editor.textCursor().selectedText(), "Corrected")
            self.window._focus_review(self.window.review_list.item(1))
            self.assertEqual(editor.textCursor().selectedText(), "Repeat")
            self.assertEqual(editor.textCursor().selectionStart(), 10)
            self.window._focus_review(self.window.review_list.item(0))
            cursor = editor.textCursor()
            cursor.removeSelectedText()
            self.window._focus_review(self.window.review_list.item(0))
            self.assertFalse(editor.textCursor().hasSelection())
            self.window._focus_review(self.window.review_list.item(1))
            self.assertEqual(editor.textCursor().selectedText(), "Repeat")
            editor.undo()
            self.window._focus_review(self.window.review_list.item(0))
            self.assertEqual(editor.textCursor().selectedText(), "Corrected")
            cursor = editor.textCursor()
            cursor.setPosition(cursor.selectionStart() + 3)
            editor.setTextCursor(cursor)
            QTest.keyClicks(editor, "XYZ")
            self.window._focus_review(self.window.review_list.item(0))
            self.assertEqual(editor.textCursor().selectedText(), "CorXYZrected")

    def test_duplicate_review_highlights_second_occurrence_after_unicode_edit(self):
        page = self.window.book.pages[0]
        page.source = "😀 Repeat then Repeat"
        page.extraction_assessment = Assessment(page.source, .9, [
            RegionAssessment("Repeat", "Repeat", .4, "Blur", (20, 20, 40, 200)),
            RegionAssessment("Repeat", "Repeat", .4, "Blur", (200, 20, 240, 200)),
        ])
        self.window._show_page()
        self.window._focus_review(self.window.review_list.item(1))
        cursor = self.window.source_editor.textCursor()
        self.assertEqual(cursor.selectedText(), "Repeat")
        self.assertEqual(cursor.selectionStart(), 15)  # Qt counts emoji as two UTF-16 units.
        self.window.preview.fit_page()
        self.app.processEvents()
        overlay = self.window.preview.overlays[1]
        position = self.window.preview.mapFromScene(overlay.rect().center())
        QTest.mouseClick(self.window.preview.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.assertEqual(self.window.source_editor.textCursor().selectionStart(), 15)
        self.window.source_editor.setPlainText("Intro " + page.source)
        self.window._focus_review(self.window.review_list.item(1))
        self.assertEqual(self.window.source_editor.textCursor().selectionStart(), 21)
        self.window.source_editor.setPlainText("Repeat")
        self.window._focus_review(self.window.review_list.item(1))
        self.assertFalse(self.window.source_editor.textCursor().hasSelection())

    def test_review_click_focuses_box_and_preserves_the_text(self):
        page = self.window.book.pages[0]
        page.source = "Original text"
        page.extraction_assessment = Assessment(
            page.source,
            0.9,
            [
                RegionAssessment(
                    page.source, page.source, 0.5, "Blurry letters", (100, 100, 160, 300)
                ),
            ],
        )
        self.window._show_page()
        initial = self.window.preview.transform().m11()
        self.window._focus_review(self.window.review_list.item(0))
        self.assertGreater(self.window.preview.transform().m11(), initial)
        self.assertEqual(self.window.source_editor.toPlainText(), "Original text")


if __name__ == "__main__":
    unittest.main()
