"""Exercise installed translators, not just direct tr() calls.

A missing translation must never erase Qt plugin diagnostics or standard labels.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import pymupdf
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox
from book_translator import i18n
from book_translator.core import BookDocument
from book_translator.ui import MainWindow
from book_translator.settings import SettingsDialog
from book_translator.preview import PageView


class InstalledLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def tearDown(self):
        with patch.object(i18n, 'saved_language', return_value='en'):
            i18n.install_language(self.app)

    def install(self, code):
        with patch.object(i18n, 'saved_language', return_value=code):
            i18n.install_language(self.app)

    def test_english_has_no_custom_translator_and_switch_removes_old_one(self):
        self.install('vi')
        self.assertIsNotNone(self.app.ui_translator)
        self.install('en')
        self.assertIsNone(self.app.ui_translator)
        self.assertEqual(QCoreApplication.translate('QPlatformTheme', 'Save'), 'Save')

    def test_unknown_diagnostics_never_become_empty(self):
        self.install('vi')
        self.assertEqual(QCoreApplication.translate('QTgaFile', 'Image type not supported'), 'Image type not supported')
        self.assertEqual(QCoreApplication.translate('UnrelatedContext', 'Save'), 'Save')
        self.assertEqual(QCoreApplication.translate('QPlatformTheme', 'Unknown button'), 'Unknown button')
        self.assertEqual(QCoreApplication.translate('QPlatformTheme', 'Save'), 'Lưu')

    def test_actual_settings_and_save_work_buttons_have_labels(self):
        for code, expected in [('en', ('Save', 'Cancel', 'Discard')), ('vi', ('Lưu', 'Hủy', 'Bỏ thay đổi'))]:
            with self.subTest(language=code):
                self.install(code)
                settings = SettingsDialog(None, '', 'gemini-test')
                try:
                    self.assertEqual(settings.buttons.button(QDialogButtonBox.StandardButton.Save).text().replace('&',''), expected[0])
                    self.assertEqual(settings.buttons.button(QDialogButtonBox.StandardButton.Cancel).text().replace('&',''), expected[1])
                    message = QMessageBox()
                    message.setWindowTitle(i18n.tr('Save your work?'))
                    message.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Discard)
                    for standard, title in zip((QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard), expected):
                        self.assertEqual(message.button(standard).text().replace('&',''), title)
                    message.close()
                finally:
                    settings.reject()

    def test_load_images_and_thumbnails_with_language_installed(self):
        limit = QImageReader.allocationLimit()
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for fmt in ('PNG', 'JPEG', 'BMP', 'TIFF', 'WEBP'):
                path = Path(folder) / ('sample.' + fmt.lower())
                Image.new('RGB', (100, 150), 'lightblue').save(path, fmt)
                paths.append(path)
            pdf_path = Path(folder) / 'sample.pdf'
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((72, 72), 'PDF preview test')
                pdf.save(pdf_path)
            paths.append(pdf_path)
            for code in ('en', 'vi'):
                with self.subTest(language=code):
                    self.install(code)
                    window = MainWindow()
                    window.set_book(BookDocument.open(paths))
                    try:
                        for index in range(len(paths)):
                            window.go_to(index)
                            self.assertIsNotNone(window.preview.item)
                            self.assertFalse(window.preview.item.pixmap().isNull())
                            # Decode each rendered thumbnail too (same Qt plugin path).
                            preview = PageView()
                            preview.set_image(window.book.render_page(index, 220))
                            self.assertFalse(preview.item.pixmap().isNull())
                            preview.close()
                    finally:
                        window.dirty = False
                        window.close()
            self.assertEqual(QImageReader.allocationLimit(), limit)
