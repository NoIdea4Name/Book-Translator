import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import pymupdf
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from book_translator.core import BookDocument
from book_translator.confidence import Assessment
from book_translator.page_render import render_request, render_file
from book_translator.ui import MainWindow
from book_translator.text_links import move_spans


class RenderTests(unittest.TestCase):
    def test_pdf_render_process_and_password(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'locked.pdf'
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((40,40),'Test page')
                pdf.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='secret')
            data, mime = render_request(path, 0, 'secret', threading.Event())
            self.assertTrue(data.startswith(b'\x89PNG'))
            self.assertEqual(mime, 'image/png')
            with self.assertRaises(ValueError):
                render_file(path, 0, 'bad-password')

    def test_large_repetitive_text_does_not_use_quadratic_diff(self):
        old = 'start ' + 'repeat '*10000 + ' end'
        new = 'start ' + 'different '*9000 + ' end'
        with patch('book_translator.text_links.SequenceMatcher', side_effect=AssertionError('Unbounded diff')):
            spans = move_spans(old, new, [[0,5], [10,16], [len(old)-3,len(old)]])
        self.assertEqual(spans[0], [0,5])
        self.assertIsNone(spans[1])
        self.assertEqual(spans[2], [len(new)-3,len(new)])

    def test_slow_render_keeps_ui_alive_and_cancel_prevents_api_call(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'page.png'; Image.new('RGB',(50,70)).save(path)
            window = MainWindow(); window.set_book(BookDocument.open([path])); window.show()
            window.api_key = 'test-key'
            started, release = threading.Event(), threading.Event()
            def slow_render(*args):
                started.set(); release.wait(3)
                return b'fake', 'image/png'
            with patch('book_translator.page_render.render_request', side_effect=slow_render), patch('book_translator.gemini_service.GeminiService.extract', return_value=Assessment('text', .9, [])) as api:
                operation = window._page_operation('source',0,'','')
                window._start_job('source',operation)
                self.assertTrue(started.wait(1))
                self.assertIsNotNone(window._job)
                self.assertTrue(window.thumbnails.render_paused)
                # Qt remains usable while preparation is blocked in the worker.
                ticks = []
                QTimer.singleShot(0, lambda: ticks.append(True))
                app.processEvents()
                self.assertEqual(ticks, [True])
                self.assertFalse(release.is_set())
                window.cancel_request(); release.set()
                deadline = time.monotonic()+1
                while time.monotonic()<deadline and window._results.empty():
                    app.processEvents(); time.sleep(.005)
                window._poll_results()
                api.assert_not_called()
                self.assertEqual(window.book.pages[0].source,'')
            window.dirty=False; window.close()
