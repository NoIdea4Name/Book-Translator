import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from book_translator.core import BookDocument
from book_translator.confidence import Assessment
from book_translator.find_replace import FindReplaceDialog, find_matches, replace_matches, search_pattern


class FindReplaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.book = BookDocument([], "images", [(Path("image.png"), i) for i in range(2)])
        self.book.pages[0].source = "😀 Cat cat caterpillar"
        self.book.pages[0].translation = "Cat story"
        self.book.pages[0].translated_from = self.book.pages[0].source
        self.book.pages[0].extraction_assessment = Assessment(self.book.pages[0].source, .9, [])
        self.book.pages[1].source = "cat"
        self.dialog = FindReplaceDialog(None, self.book)

    def tearDown(self):
        self.dialog.close()

    def test_case_whole_word_scope_and_empty_query(self):
        pages = self.book.pages
        self.assertEqual(len(find_matches(pages, search_pattern("cat"), "both")), 5)
        self.assertEqual(len(find_matches(pages, search_pattern("cat", whole_word=True), "source")), 3)
        self.assertEqual(len(find_matches(pages, search_pattern("cat", case_sensitive=True), "source")), 3)
        self.assertEqual(find_matches(pages, search_pattern(""), "both"), [])
        self.assertEqual(find_matches(pages, search_pattern(".*"), "both"), [])

    def test_replace_all_literal_and_undo_restores_reports(self):
        old = self.book.pages[0].source
        report = self.book.pages[0].extraction_assessment
        self.dialog.find.setText("cat")
        self.dialog.whole_word.setChecked(True)
        QTest.keyClick(self.dialog.find, Qt.Key.Key_Return)
        self.dialog.replacement.setText(r"cat\1")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.replace_all()
        self.assertEqual(self.book.pages[0].source, "😀 cat\\1 cat\\1 caterpillar")
        self.assertTrue(self.book.pages[0].translation_is_stale)
        self.assertIsNotNone(self.book.pages[0].extraction_assessment)
        self.dialog.undo_button.click()
        self.assertEqual(self.book.pages[0].source, old)
        self.assertIs(self.book.pages[0].extraction_assessment, report)
        self.assertFalse(self.book.pages[0].translation_is_stale)
        self.assertEqual(self.book.pages[0].translation, "Cat story")

    def test_single_match_navigation_and_unicode_highlight(self):
        self.dialog.find.setText("Cat")
        QTest.keyClick(self.dialog.find, Qt.Key.Key_Return)
        self.assertEqual(self.dialog.preview.textCursor().selectedText(), "Cat")
        self.dialog.replacement.setText("Dog")
        self.dialog.replace_one()
        self.assertEqual(self.book.pages[0].source, "😀 Dog cat caterpillar")
        self.assertEqual(self.dialog.preview.textCursor().selectedText(), "cat")
        self.assertEqual(self.book.pages[0].translation, "Cat story")

    def test_cancel_bulk_and_empty_replacement(self):
        self.dialog.find.setText("cat")
        QTest.keyClick(self.dialog.find, Qt.Key.Key_Return)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog.replace_all()
        self.assertEqual(self.book.pages[1].source, "cat")
        matches = find_matches(self.book.pages, search_pattern("cat"), "source")
        replace_matches(self.book.pages, matches, "")
        self.assertEqual(self.book.pages[1].source, "")
        self.assertEqual(self.book.pages[0].translation, "Cat story")

    def test_typing_and_options_do_not_scan_until_enter(self):
        with patch('book_translator.find_replace.find_matches', wraps=find_matches) as scan:
            self.dialog.find.setText('cat')
            self.dialog.whole_word.setChecked(True)
            self.dialog.scope.setCurrentIndex(1)
            scan.assert_not_called()
            QTest.keyClick(self.dialog.find, Qt.Key.Key_Return)
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(len(self.dialog.matches), 3)
            self.dialog.find.setText('dog')
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(self.dialog.matches, [])
            self.assertFalse(self.dialog.replace_all_button.isEnabled())
            self.dialog.replace_one()
            self.assertEqual(self.book.pages[1].source, 'cat')
