import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QMimeData, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox, QMenu

from book_translator.confidence import Assessment
from book_translator.core import BookDocument, save_project, read_project, export_text
from book_translator.ui import MainWindow
from test_core import make_pdf


class MenuChoice(QMenu):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.choice = title

    def exec(self, *args):
        return next(action for action in self.actions() if action.text() == self.choice)


class PageEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf = self.root / "book.pdf"
        make_pdf(self.pdf)
        self.image = self.root / "extra.png"
        Image.new("RGB", (100, 150), "red").save(self.image)
        self.window = MainWindow()
        self.window.resize(1200, 900)
        self.window.set_book(BookDocument.open([self.pdf]))
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.cancel_request()
        self.window.dirty = False
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def add(self, position=None):
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.image)], "")):
            self.window.add_pages(position)

    def test_insert_reorder_delete_save_reopen_keeps_page_identity(self):
        book = self.window.book
        original = book.pages[0]
        original.source = "Edited source"
        original.translation = "Translation"
        original.extraction_assessment = Assessment("Edited source", .8, [])
        self.add(1)
        self.assertEqual(book.kind, "mixed")
        self.assertEqual(book.refs[1], (self.image, 0))
        self.window.move_pages([0], 4)
        self.assertIs(book.pages[3], original)
        self.assertIn("Chapter One", book.extract_pdf_text(3))
        self.assertIn("Chapter Two", book.extract_pdf_text(1))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.delete_page(2)
        project_path = self.root / "edited.btproject"
        save_project(project_path, book, 2, "Vietnamese", "gemini-2.5-flash")
        project = read_project(project_path)
        restored = BookDocument.open(project.paths, sort_images=False)
        try:
            restored.restore_project(project)
            self.assertEqual(restored.refs, book.refs)
            self.assertEqual(restored.pages, book.pages)
            self.assertEqual(restored.pages[2], original)
            self.assertIn("Chapter One", restored.extract_pdf_text(2))
            with Image.open(BytesIO(restored.render_page(0))) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            self.assertLess(export_text(restored, "source").index("=== Page 1"), export_text(restored, "source").index("Edited source"))
        finally:
            restored.close()

    def test_add_button_appends_and_context_menu_inserts_above_below(self):
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.image)], "")):
            self.window.open_button.click()
        self.assertEqual(self.window.book.refs[-1], (self.image, 0))
        strip = self.window.thumbnails
        for title, expected_position in (("Add page above", 1), ("Add page below", 2)):
            item = strip.item(1)
            strip.scrollToItem(item)
            self.app.processEvents()
            def choose(menu, *args):
                return next(action for action in menu.actions() if action.text() == title)
            with patch("book_translator.ui.QMenu", side_effect=lambda parent: MenuChoice(parent, title)), patch.object(
                self.window, "add_pages"
            ) as add:
                self.window.page_context_menu(strip.visualItemRect(item).center())
            add.assert_called_once_with(expected_position)

    def test_context_delete_targets_clicked_page(self):
        strip = self.window.thumbnails
        def choose(menu, *args):
            return next(a for a in menu.actions() if a.text() == "Delete page")
        with patch("book_translator.ui.QMenu", side_effect=lambda parent: MenuChoice(parent, "Delete page")), patch.object(self.window, "delete_page") as delete:
            self.window.page_context_menu(strip.visualItemRect(strip.item(1)).center())
        delete.assert_called_once_with(1)

    def test_multi_page_move_keeps_selection_and_current_page(self):
        states = self.window.book.pages.copy()
        self.window.go_to(1)
        self.window.move_pages([0, 2], 3)
        self.assertEqual([id(p) for p in self.window.book.pages], [id(states[i]) for i in (1, 0, 2)])
        self.assertIs(self.window.book.pages[self.window.index], states[1])
        self.assertEqual(self.window._selected_pages(), [1, 2])

    def test_delete_all_can_save_reopen_and_add_again(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            for _ in range(3):
                self.window.delete_page(0)
        self.assertEqual(self.window.book.pages, [])
        self.assertFalse(self.window.extract_button.isEnabled())
        self.assertTrue(self.window.open_button.isEnabled())
        self.assertTrue(self.window.save_button.isEnabled())
        self.window._update_reviews()
        path = self.root / "empty.btproject"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.assertTrue(self.window.save())
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            self.window.open_project()
        self.assertEqual(self.window.book.pages, [])
        self.add()
        self.assertEqual(len(self.window.book.pages), 1)
        self.assertEqual(self.window.book.refs[0], (self.image, 0))

    def test_cancel_add_and_delete_leave_document_unchanged(self):
        refs = self.window.book.refs.copy()
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([], "")):
            self.window.add_pages(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.window.delete_page(0)
        self.assertEqual(self.window.book.refs, refs)
        self.assertFalse(self.window.dirty)

    def test_duplicate_pdf_insertion_keeps_handles_valid(self):
        incoming = BookDocument.open([self.pdf])
        self.window.book.insert(1, incoming)
        incoming.close()
        self.assertIn("Chapter One", self.window.book.extract_pdf_text(0))
        self.assertIn("Chapter One", self.window.book.extract_pdf_text(1))
        self.assertEqual(len(self.window.book.pages), 6)

    def test_missing_saved_page_and_bad_reference_are_rejected(self):
        path = self.root / "bad.btproject"
        save_project(path, self.window.book, 0, "Vietnamese", "gemini-2.5-flash")
        data = json.loads(path.read_text())
        data["page_refs"][0][1] = 999
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.window.book.restore_project(read_project(path))
        data["page_refs"][0][0] = True
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            read_project(path)

    def test_legacy_project_keeps_text_and_original_order(self):
        self.window.book.pages[1].source = "Saved text"
        path = self.root / "legacy.btproject"
        save_project(path, self.window.book, 1, "Vietnamese", "gemini-2.5-flash")
        data = json.loads(path.read_text())
        data["version"] = 2
        del data["page_refs"]
        path.write_text(json.dumps(data))
        project = read_project(path)
        self.window.book.restore_project(project)
        self.assertEqual(self.window.book.pages[1].source, "Saved text")
        self.assertIn("Chapter Two", self.window.book.extract_pdf_text(1))

    def test_failed_add_leaves_existing_pages_intact(self):
        refs = self.window.book.refs.copy()
        with patch.object(QFileDialog, "getOpenFileNames", return_value=(
            [str(self.image), str(self.root / "missing.png")], ""
        )), patch.object(QMessageBox, "warning") as warning:
            self.window.add_pages(0)
        warning.assert_called_once()
        self.assertEqual(self.window.book.refs, refs)
        self.assertIn("Chapter One", self.window.book.extract_pdf_text(0))

    def test_rename_round_trip_and_headers(self):
        with patch.object(QInputDialog, "getText", return_value=("Chương một", True)):
            self.window.rename_page(0)
        self.assertIn("Chương một", self.window.thumbnails.item(0).text())
        self.assertTrue(self.window.dirty)
        page = self.window.book.pages[0]
        self.window.move_pages([0], 3)
        self.assertIs(self.window.book.pages[-1], page)
        path = self.root / "named.btproject"
        save_project(path, self.window.book, 0, "Vietnamese", "gemini-model")
        self.assertEqual(read_project(path).pages[-1].name, "Chương một")
        self.assertIn("=== Page 3 · Chương một ===", export_text(self.window.book))
        self.assertNotIn("Chương một", export_text(self.window.book, show_page_headers=False))
        with patch.object(QInputDialog, "getText", return_value=("", True)):
            self.window.rename_page(2)
        self.assertEqual(self.window.book.page_name(2), "Page 3")

    def test_double_click_thumbnail_renames_page(self):
        strip = self.window.thumbnails
        item = strip.item(0)
        point = strip.visualItemRect(item).center()
        with patch.object(QInputDialog, "getText", return_value=("Introduction", True)) as prompt:
            QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseDClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=point)
        prompt.assert_called_once()
        self.assertEqual(self.window.book.pages[0].name, "Introduction")

    def test_export_header_option_writes_plain_text(self):
        from book_translator.output_preview import ExportOptionsDialog
        options = ExportOptionsDialog(self.window)
        self.window.book.pages[0].translation = "Translated text"
        path = self.root / "out.txt"
        fake = SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted,
            file_format=options.file_format, page_breaks=options.page_breaks,
            mode=options.mode, show_headers=options.show_headers,
            show_placeholders=options.show_placeholders,
        )
        options.show_placeholders.setChecked(False)
        for enabled in (False, True):
            options.show_headers.setChecked(enabled)
            with patch("book_translator.ui.ExportOptionsDialog", return_value=fake), patch.object(
                QFileDialog, "getSaveFileName", return_value=(str(path), "")
            ):
                self.window.export()
            content = path.read_text()
            self.assertEqual("=== Page 1 ===" in content, enabled)
            self.assertIn("Translated text", content)
            self.assertNotIn("[Not translated]", content)
        options.close()

    def test_open_files_appends_without_losing_unsaved_edits(self):
        page = self.window.book.pages[0]
        page.source = "Keep this edit"
        self.window.dirty = True
        self.window.project_path = self.root / "existing.btproject"
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.image)], "")), patch.object(
            QMessageBox, "question"
        ) as prompt:
            self.window.open_button.click()
        prompt.assert_not_called()
        self.assertEqual(len(self.window.book.pages), 4)
        self.assertIs(self.window.book.pages[0], page)
        self.assertEqual(page.source, "Keep this edit")
        self.assertEqual(self.window.project_path, self.root / "existing.btproject")

    def test_new_workspace_cancel_and_discard(self):
        old_book = self.window.book
        self.window.dirty = True
        self.window.project_path = self.root / "old.btproject"
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.window.new_workspace_action.trigger()
        self.assertIs(self.window.book, old_book)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            self.window.new_workspace_action.trigger()
        self.assertEqual(self.window.book.pages, [])
        self.assertIsNone(self.window.project_path)
        self.assertEqual(self.window.thumbnails.count(), 0)
        self.assertFalse(self.window.dirty)
        self.assertEqual(self.window.source_editor.toPlainText(), "")
        self.assertFalse(hasattr(self.window, "add_page_button"))
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.pdf)], "")):
            self.window.open_files()
        self.assertEqual(len(self.window.book.pages), 3)
        self.assertEqual(self.window.method.currentIndex(), 0)

    def test_new_workspace_respects_failed_save_and_active_batch(self):
        old_book = self.window.book
        self.window.dirty = True
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save), patch.object(
            self.window, "save", return_value=False
        ):
            self.window.new_workspace()
        self.assertIs(self.window.book, old_book)
        from book_translator.batch import BatchRun
        self.window._batch = BatchRun((0, 1), "source", "", False)
        self.window._refresh_controls()
        self.assertFalse(self.window.new_workspace_action.isEnabled())
        self.window.new_workspace()
        self.assertIs(self.window.book, old_book)

    def test_check_flagged_page_keeps_name_and_saves_review(self):
        from book_translator.confidence import review_flags
        from book_translator.preview import CHECKED_ROLE, REVIEW_COUNT_ROLE
        page = self.window.book.pages[0]
        page.name = "Introduction"
        page.source = "[illegible]"
        page.extraction_assessment = Assessment(page.source, .15, [])
        self.window._show_page(render=False)
        item = self.window.thumbnails.item(0)
        self.assertEqual(item.text(), "Introduction")
        self.assertEqual(item.data(REVIEW_COUNT_ROLE), 1)
        self.window.toggle_page_checked(0)
        self.assertTrue(page.is_reviewed)
        self.assertEqual(item.text(), "Introduction")
        self.assertTrue(item.data(CHECKED_ROLE))
        self.assertEqual(item.data(REVIEW_COUNT_ROLE), 0)
        self.assertEqual(review_flags(page, .8), [])
        path = self.root / "reviewed.btproject"
        save_project(path, self.window.book, 0, "Vietnamese", "gemini-model")
        restored = read_project(path).pages[0]
        self.assertTrue(restored.is_reviewed)
        restored.name = "Renamed"
        self.assertTrue(restored.is_reviewed)
        restored.source += " correction"
        self.assertFalse(restored.is_reviewed)
        self.window.toggle_page_checked(0)
        self.assertFalse(page.is_reviewed)
        self.assertEqual(item.data(REVIEW_COUNT_ROLE), 1)

    def test_individual_review_dismissal_survives_edits_save_and_collapse(self):
        from book_translator.confidence import RegionAssessment, review_flags
        from book_translator.ui import PageJob
        page = self.window.book.pages[0]
        page.source = "First second"
        report = Assessment(page.source, .4, [
            RegionAssessment("First", "First", .2, "Blurred", (0, 0, 100, 100)),
            RegionAssessment("second", "second", .3, "Blurred", (200, 0, 300, 100)),
        ])
        page.extraction_assessment = report
        self.window._show_page()
        self.window.source_editor.setPlainText("Corrected second")
        self.assertEqual(self.window.review_list.count(), 2)
        from PySide6.QtWidgets import QPushButton
        row = self.window.review_list.itemWidget(self.window.review_list.item(0))
        next(button for button in row.findChildren(QPushButton) if button.text() == "×").click()
        self.assertEqual(self.window.review_list.count(), 1)
        self.assertEqual(review_flags(page, .8)[0].source_text, "second")
        self.window.review_toggle.click()
        self.assertTrue(self.window.review_body.isHidden())
        self.window._update_reviews()
        self.assertTrue(self.window.review_body.isHidden())
        self.window.review_toggle.click()
        self.assertFalse(self.window.review_body.isHidden())
        path = self.root / "dismissed.btproject"
        save_project(path, self.window.book, 0, "Vietnamese", "gemini-model")
        restored = read_project(path).pages[0]
        self.assertEqual(restored.dismissed_reviews, ["source:0"])
        self.assertEqual(len(review_flags(restored, .8)), 1)
        self.window._dismiss_review("source:1")
        self.assertEqual(self.window.review_list.count(), 0)  # No synthetic whole-page flag.
        self.window._apply_result(PageJob(1, "source", 0, "", "Vietnamese"), report)
        self.assertEqual(page.dismissed_reviews, [])
        self.assertEqual(self.window.review_list.count(), 2)

    def test_manual_flag_menu_persists_and_checked_clears_both_badges(self):
        from book_translator.preview import MANUAL_FLAG_ROLE, REVIEW_COUNT_ROLE
        page = self.window.book.pages[0]
        page.source = "Uncertain"
        page.extraction_assessment = Assessment(page.source, .15, [])
        self.window._show_page(render=False)
        strip = self.window.thumbnails
        position = strip.visualItemRect(strip.item(0)).center()
        with patch("book_translator.ui.QMenu", side_effect=lambda parent: MenuChoice(parent, "Flag page")):
            self.window.page_context_menu(position)
        self.assertTrue(page.manual_flag)
        self.assertTrue(strip.item(0).data(MANUAL_FLAG_ROLE))
        self.assertEqual(strip.item(0).data(REVIEW_COUNT_ROLE), 1)
        path = self.root / "flagged.btproject"
        save_project(path, self.window.book, 0, "Vietnamese", "gemini-model")
        self.assertTrue(read_project(path).pages[0].manual_flag)
        self.window.toggle_page_checked(0)
        self.assertFalse(page.manual_flag)
        self.assertFalse(strip.item(0).data(MANUAL_FLAG_ROLE))
        self.assertEqual(strip.item(0).data(REVIEW_COUNT_ROLE), 0)
        self.assertTrue(page.is_reviewed)
        self.window.toggle_manual_flag(0)
        self.assertTrue(page.is_reviewed)  # Manual flags don't invalidate AI review.
        self.assertTrue(strip.item(0).data(MANUAL_FLAG_ROLE))
        self.window.toggle_page_checked(0)
        self.assertFalse(page.manual_flag)
        self.assertTrue(page.is_reviewed)

    def test_new_gemini_result_clears_review_even_if_identical(self):
        from book_translator.ui import PageJob
        page = self.window.book.pages[0]
        report = Assessment("[illegible]", .15, [])
        page.source = report.text
        page.extraction_assessment = report
        self.window.toggle_page_checked(0)
        self.assertTrue(page.is_reviewed)
        self.window._apply_result(PageJob(1, "source", 0, "", ""), report)
        self.assertFalse(page.is_reviewed)

    def test_pointer_drag_release_outside_cancels(self):
        strip = self.window.thumbnails
        start = strip.visualItemRect(strip.item(0)).center()
        original = self.window.book.pages.copy()
        QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=start)
        point = start + QPoint(0, 60)
        move = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(point),
            QPointF(strip.viewport().mapToGlobal(point)), Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(strip.viewport(), move)
        self.assertTrue(strip._pointer_drag)
        QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(-20, 50))
        self.assertFalse(strip._pointer_drag)
        self.assertIsNone(strip._drop_position)
        self.assertEqual([id(p) for p in original], [id(p) for p in self.window.book.pages])

    def test_page_edits_disabled_during_batch(self):
        from book_translator.batch import BatchRun
        self.window._batch = BatchRun((0, 1), "source", "", False)
        self.window._refresh_controls()
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertFalse(self.window.thumbnails.editing_enabled)
        refs = self.window.book.refs.copy()
        with patch.object(QFileDialog, "getOpenFileNames") as picker, patch.object(QMessageBox, "question") as confirm:
            self.window.add_pages()
            self.window.delete_page(0)
            self.window.move_pages([0], 3)
        picker.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(refs, self.window.book.refs)
        self.window.cancel_request()
        self.assertTrue(self.window.thumbnails.editing_enabled)
