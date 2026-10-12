import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QFileDialog, QDialog, QMessageBox

from book_translator.confidence import Assessment
from book_translator.core import BookDocument, save_project, read_project
from book_translator.image_editor import ImageEditorDialog, full_corners, transform_image
from book_translator.phone_import import PhoneImportDialog, save_image_copies
from book_translator.phone_server import PhoneReceiver
from book_translator.ui import MainWindow


def photo_bytes(color="white"):
    data = BytesIO()
    Image.new("RGB", (160, 240), color).save(data, format="PNG")
    return data.getvalue()


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.server = PhoneReceiver("127.0.0.1")
        self.url = self.server.url("127.0.0.1")

    def tearDown(self):
        self.server.close()

    def post(self, route, body, headers=None):
        request = Request(self.url + route, body, headers=headers or {}, method="POST")
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def test_real_http_upload_order_commit_retry_and_release(self):
        with urlopen(self.url, timeout=4) as response:
            self.assertIn(b'capture="environment"', response.read())
        first = self.post("upload", photo_bytes("red"))["id"]
        second = self.post("upload", photo_bytes("blue"))["id"]
        body = json.dumps({"batch": "one", "ids": [second, first]}).encode()
        self.assertEqual(self.post("commit", body), {"received": 2})
        batch = self.server.batches.get_nowait()
        self.assertEqual([p.id for p in batch], [second, first])
        with Image.open(batch[0].path) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
        self.server.release(batch)
        self.assertFalse(batch[0].path.exists())
        self.assertEqual(self.post("commit", body), {"received": 2})
        self.assertTrue(self.server.batches.empty())

    def test_token_origin_invalid_image_and_unknown_commit(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url.replace(self.server.token, "wrong"), timeout=4)
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:
            self.post("upload", photo_bytes(), {"Origin": "https://other.example"})
        self.assertEqual(error.exception.code, 403)
        for route, body in [("upload", b"not a photo"), ("commit", b'{"batch":"bad","ids":["missing"]}'), ("commit", b"[]")]:
            with self.assertRaises(HTTPError) as error:
                self.post(route, body)
            self.assertEqual(error.exception.code, 400)
        self.assertTrue(self.server.batches.empty())

    def test_lost_upload_response_retry_does_not_duplicate_photo(self):
        headers = {"X-Photo-Id": "photo123"}
        first = self.post("upload", photo_bytes(), headers)
        with patch("book_translator.phone_server.MAX_IMAGES", 1):
            second = self.post("upload", photo_bytes(), headers)
        self.assertEqual(first, second)
        self.assertEqual(len(self.server._images), 1)

    def test_limits_and_duplicate_ids(self):
        with patch("book_translator.phone_server.MAX_UPLOAD", 4):
            with self.assertRaises(HTTPError) as error:
                self.post("upload", b"12345")
            self.assertEqual(error.exception.code, 413)
        identifier = self.post("upload", photo_bytes())["id"]
        with self.assertRaises(HTTPError):
            self.post("commit", json.dumps({"batch":"duplicate", "ids":[identifier, identifier]}).encode())
        with patch("book_translator.phone_server.MAX_IMAGES", 1):
            with self.assertRaises(HTTPError):
                self.post("upload", photo_bytes())


class ImageTransformTests(unittest.TestCase):
    def test_crop_and_identity_perspective_keep_pixels(self):
        image = Image.new("RGB", (100, 80), "white")
        image.paste("red", (20, 10, 60, 60))
        result = transform_image(image, [(20, 10), (60, 10), (60, 60), (20, 60)], "Crop")
        self.assertEqual(result.size, (40, 50))
        self.assertEqual(result.getpixel((15, 15)), (255, 0, 0))
        identity = transform_image(image, full_corners(image), "Perspective")
        self.assertEqual(identity.tobytes(), image.tobytes())

    def test_perspective_and_invalid_corners(self):
        image = Image.new("RGB", (120, 160), "white")
        result = transform_image(image, [(20, 10), (110, 25), (100, 145), (10, 130)], "Perspective")
        self.assertEqual(result.size, (91, 120))
        self.assertEqual(result.getpixel((40, 50)), (255, 255, 255))
        for points in [[(0,0), (100,100), (100,0), (0,100)], [(0,0)]*4, [(-1,0),(80,0),(80,100),(0,100)]]:
            with self.assertRaises(ValueError):
                transform_image(image, points, "Perspective")


class PhoneUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "original.png"
        self.source.write_bytes(photo_bytes("red"))
        self.storage_patch = patch("book_translator.phone_import.image_storage", return_value=self.root / "managed")
        self.storage_patch.start()
        self.window = MainWindow()
        self.window.set_book(BookDocument.open([self.source]))
        self.receiver = PhoneReceiver("127.0.0.1")
        self.dialog = PhoneImportDialog(self.window.import_phone_images, self.window, self.receiver)

    def tearDown(self):
        self.dialog.stop()
        self.dialog.deleteLater()
        self.window.dirty = False
        self.window.close()
        self.app.processEvents()
        self.storage_patch.stop()
        self.temp.cleanup()

    def receive(self, colors=("blue", "green")):
        ids = [self.receiver._store(photo_bytes(color))["id"] for color in colors]
        self.receiver._commit({"batch": str(self.receiver._receipts), "ids": ids})
        self.dialog.receive_batches()

    def test_add_persists_after_receiver_close_and_project_reopen(self):
        page = self.window.book.pages[0]
        page.source, page.name = "Keep this text", "Original name"
        self.receive()
        self.dialog.process_images("add")
        self.assertEqual(len(self.window.book.pages), 3)
        self.assertIs(self.window.book.pages[0], page)
        self.assertEqual(page.name, "Original name")
        self.assertFalse(self.dialog.pending)
        self.dialog.stop()
        project_path = self.root / "book.btproject"
        save_project(project_path, self.window.book, 0, "English", "model")
        project = read_project(project_path)
        restored = BookDocument.open(project.paths, sort_images=False)
        try:
            restored.restore_project(project)
            with Image.open(BytesIO(restored.render_page(1))) as image:
                self.assertEqual(image.getpixel((0,0)), (0,0,255))
            self.assertEqual(restored.pages[0].source, "Keep this text")
        finally:
            restored.close()

    def test_save_only_and_save_add_respect_workspace_choice(self):
        self.receive(("blue",))
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root / "saved")):
            self.dialog.process_images("save")
        self.assertEqual(len(self.window.book.pages), 1)
        saved = list((self.root / "saved").glob("*/Page-001.png"))
        self.assertEqual(len(saved), 1)
        self.receive(("green",))
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root / "saved")):
            self.dialog.process_images("both")
        self.assertEqual(len(self.window.book.pages), 2)
        self.assertIn("saved", str(self.window.book.refs[1][0]))
        self.assertEqual(len(list((self.root / "saved").glob("*/Page-001.png"))), 2)

    def test_cancel_folder_and_failed_import_keep_pending(self):
        self.receive(("blue",))
        with patch.object(QFileDialog, "getExistingDirectory", return_value=""):
            self.dialog.process_images("both")
        self.assertEqual(len(self.dialog.pending), 1)
        self.dialog.import_images = lambda paths: (_ for _ in ()).throw(ValueError("Cannot import"))
        with patch.object(QMessageBox, "warning"):
            self.dialog.process_images("add")
        self.assertEqual(len(self.dialog.pending), 1)
        self.assertFalse(list((self.root / "managed").glob("Pages-*")))

    def test_edit_existing_keeps_original_name_text_and_clears_old_flags(self):
        page = self.window.book.pages[0]
        page.name, page.source, page.translation = "Named page", "Text", "Translation"
        page.extraction_assessment = Assessment("Text", .2, [])
        page.reviewed_signature = page.review_signature()
        original_bytes = self.source.read_bytes()
        def accept(editor):
            editor.result_image = Image.new("RGB", (80, 120), "blue")
            return QDialog.DialogCode.Accepted
        with patch.object(ImageEditorDialog, "exec", accept):
            self.window.edit_page_image(0)
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertEqual((page.name, page.source, page.translation), ("Named page", "Text", "Translation"))
        self.assertIsNone(page.extraction_assessment)
        self.assertFalse(page.is_reviewed)
        self.assertNotEqual(self.window.book.refs[0][0], self.source)

    def test_editor_crop_rotation_reset_and_cancel(self):
        editor = ImageEditorDialog(Image.new("RGB", (100, 140), "red"))
        editor.canvas.move_corner(0, QPointF(20, 30))
        editor.accept_image()
        self.assertEqual(editor.result_image.size, (80,110))
        editor.rotate(90)
        self.assertEqual(editor.current.size, (110,80))
        editor.reset()
        self.assertEqual(editor.current.size, (100,140))
        editor.reject()

    def test_partial_copy_failure_is_cleaned(self):
        with self.assertRaises(FileNotFoundError):
            save_image_copies([self.source, self.root / "missing.png"], self.root / "copies")
        self.assertEqual(list((self.root / "copies").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
