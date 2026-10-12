import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
import pymupdf
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from book_translator.confidence import ReviewFlag
from book_translator.core import BookDocument
from book_translator.preview import PageView, ThumbnailStrip


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = PageView()
        self.view.resize(640, 600)
        self.view.show()
        data = BytesIO()
        Image.new("RGB", (1000, 1400), "white").save(data, "PNG")
        self.view.set_image(data.getvalue())
        self.app.processEvents()

    def tearDown(self):
        self.view.close()

    def test_wheel_zoom_drag_pan_and_double_click_fit(self):
        initial = self.view.transform().m11()
        point = QPoint(220, 240)
        before = self.view.mapToScene(point)
        wheel = QWheelEvent(
            QPointF(point),
            QPointF(self.view.viewport().mapToGlobal(point)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(self.view.viewport(), wheel)
        after = self.view.mapToScene(point)
        self.assertGreater(self.view.transform().m11(), initial)
        self.assertLess((after - before).manhattanLength(), 8)
        center = self.view.viewport().rect().center()
        before_drag = self.view.mapToScene(center)
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(self.view.viewport(), center + QPoint(55, 30), delay=10)
        QTest.mouseRelease(
            self.view.viewport(), Qt.MouseButton.LeftButton, pos=center + QPoint(55, 30)
        )
        after_drag = self.view.mapToScene(center)
        self.assertGreater((after_drag - before_drag).manhattanLength(), 20)
        QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        self.assertTrue(self.view.fit_mode)
        self.assertAlmostEqual(self.view.transform().m11(), initial, places=5)

    def test_boxes_remain_registered_to_image_under_zoom(self):
        flag = ReviewFlag("source", 0.62, "Source", "Source", "Blur", (100, 200, 300, 700))
        self.view.set_flags([flag])
        overlay = self.view.overlays[0]
        self.assertEqual(overlay.rect().x(), 200)
        self.assertEqual(overlay.rect().y(), 140)
        self.assertEqual(overlay.rect().width(), 500)
        self.assertEqual(overlay.rect().height(), 280)
        rect = overlay.rect()
        self.view.zoom(2)
        self.assertEqual(overlay.rect(), rect)
        self.assertTrue(overlay.pen().isCosmetic())
        self.assertIn("62%", overlay.toolTip())
        self.view.set_flags([])
        self.assertEqual(self.view.overlays, [])

    def test_thumbnail_loading_is_lazy_and_cache_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "many.pdf"
            with pymupdf.open() as pdf:
                for number in range(24):
                    page = pdf.new_page(width=140, height=200)
                    page.insert_text((20, 40), f"Page {number + 1}")
                pdf.save(path)
            book = BookDocument.open([path])
            strip = ThumbnailStrip()
            strip.CACHE_LIMIT = 8
            strip.resize(150, 380)
            strip.show()

            def drain():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    self.app.processEvents()
                    if not strip.timer.isActive():
                        return
                    time.sleep(0.01)
                self.fail("Thumbnail loading did not settle")

            try:
                with patch.object(book, "render_page", wraps=book.render_page) as render:
                    strip.set_book(book)
                    drain()
                    self.assertGreater(render.call_count, 0)
                    self.assertLess(render.call_count, 10)
                    for index in (10, 18, 23):
                        strip.set_current_page(index)
                        drain()
                    self.assertLessEqual(len(strip.cache), 8)
                    self.assertIn(23, strip.cache)
                    self.assertNotIn(0, strip.cache)
            finally:
                strip.timer.stop()
                strip.book = None
                strip.close()
                book.close()
