import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import unittest

from PySide6.QtWidgets import QApplication
from book_translator.core import BookDocument, export_text
from book_translator.output_preview import OutputPreviewDialog


class OutputPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        path = Path("example.pdf")
        self.book = BookDocument([path], "pdf", [(path, i) for i in range(3)])
        for i, page in enumerate(self.book.pages[:2]):
            page.source = f"Source {i}"
            page.translation = f"Bản dịch 日本語 {i}"
            page.translated_from = page.source
            page.target_language = "Vietnamese"
        self.book.reorder([1, 0, 2])
        self.dialog = OutputPreviewDialog(None, self.book)

    def tearDown(self):
        self.dialog.close()

    def test_modes_match_export_and_copy_exactly_in_page_order(self):
        for index, mode in enumerate(("translation", "source", "both")):
            self.dialog.mode.setCurrentIndex(index)
            expected = export_text(self.book, mode)
            self.assertEqual(self.dialog.text.toPlainText(), expected)
            self.dialog.copy_button.click()
            self.assertEqual(self.app.clipboard().text(), expected)
            self.assertEqual(self.dialog.copy_status.text(), "Copied")
        self.assertLess(expected.index("Source 1"), expected.index("Source 0"))
        self.assertTrue(self.dialog.text.isReadOnly())

    def test_missing_stale_and_explicit_refresh(self):
        self.book.pages[0].source = "Edited"
        self.dialog.refresh_button.click()
        self.assertIn("1 untranslated", self.dialog.summary.text())
        self.assertIn("1 with edited source", self.dialog.summary.text())
        self.assertIn("[Source edited after translation]", self.dialog.text.toPlainText())
        before = self.dialog.text.toPlainText()
        self.book.pages[2].translation = "Just completed"
        self.assertEqual(self.dialog.text.toPlainText(), before)
        self.dialog.refresh_button.click()
        self.assertIn("Just completed", self.dialog.text.toPlainText())
        self.assertNotIn("untranslated", self.dialog.summary.text())
        self.assertEqual(self.book.pages[0].source, "Edited")

    def test_selected_text_can_be_copied_without_mutating_the_book(self):
        cursor = self.dialog.text.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(12, cursor.MoveMode.KeepAnchor)
        self.dialog.text.setTextCursor(cursor)
        self.dialog.text.copy()
        self.assertEqual(self.app.clipboard().text(), self.dialog.text.toPlainText()[:12])
        self.assertEqual(self.book.pages[0].source, "Source 1")
