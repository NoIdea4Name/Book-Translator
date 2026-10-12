import json
import os
from io import BytesIO
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from google import genai
from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from book_translator.chat import ChatReply
from book_translator.chat_panel import open_chat
from book_translator.core import BookDocument
from book_translator.gemini_service import GeminiService
from book_translator.ui import MainWindow


class ChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "page.png"
        Image.new("RGB", (800, 1000), "white").save(path)
        self.window = MainWindow()
        self.window.api_profiles = []
        self.window.api_key = "test-key"
        self.window.show()
        self.window.set_book(BookDocument.open([path]))
        self.window.source_editor.setPlainText("😀 repeated text repeated text")
        cursor = self.window.source_editor.textCursor()
        cursor.setPosition(17)
        cursor.setPosition(30, QTextCursor.MoveMode.KeepAnchor)
        self.window.source_editor.setTextCursor(cursor)
        self.app.processEvents()
        open_chat(self.window)
        self.panel = self.window.chat_panel

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_close_then_reopen_does_not_disconnect_twice(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.panel.reject()
            open_chat(self.window)
            self.panel = self.window.chat_panel
            self.window.preview.region_changed.emit()
            self.panel.reject()
            self.panel.reject()
        self.assertFalse([w for w in caught if 'disconnect' in str(w.message)])

    def test_selector_uses_selected_provider_when_rotation_disabled(self):
        from book_translator.api_rotation import ApiProfile
        self.window.api_profiles = [ApiProfile('gemini', 'gemini-test', 'key-one'), ApiProfile('openai', 'gpt-test', 'key-two'), ApiProfile('nvidia', 'vendor/model', 'key-three')]
        self.window.rotation_enabled = False
        open_chat(self.window)
        self.panel = self.window.chat_panel
        self.assertEqual(self.panel.model_selector.count(), 3)
        self.panel.model_selector.setCurrentIndex(2)
        self.assertEqual(self.panel.router.profiles[0].provider, 'nvidia')
        self.panel.set_pending(True)
        self.assertFalse(self.panel.model_selector.isEnabled())
        self.panel.set_pending(False)

    def complete(self, reply):
        with patch.object(GeminiService, "chat", return_value=reply) as call:
            self.panel.prompt.setPlainText("Explain and improve this")
            self.panel.send()
            deadline = time.monotonic() + 3
            while self.panel.pending and time.monotonic() < deadline:
                self.app.processEvents()
                self.panel.poll_results()
                time.sleep(.01)
            self.assertFalse(self.panel.pending)
            return call.call_args.args

    def test_answer_and_replacement_target_exact_duplicate_and_undo(self):
        self.assertEqual(self.panel.selected, "repeated text")
        args = self.complete(ChatReply("Explanation", "improved"))
        self.assertEqual(args[0]["selected_text"], "repeated text")
        self.assertEqual(args[3], [])
        self.panel.apply_mode.setCurrentIndex(1)
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), "😀 repeated text improved")
        self.window.source_editor.undo()
        self.assertEqual(self.window.source_editor.toPlainText(), "😀 repeated text repeated text")

    def test_default_add_preserves_page_and_undo(self):
        original = self.window.source_editor.toPlainText()
        self.complete(ChatReply("", "New paragraph"))
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), original + "\n\nNew paragraph")
        self.assertTrue(self.panel.proposal_card.isHidden())
        self.window.source_editor.undo()
        self.assertEqual(self.window.source_editor.toPlainText(), original)

    def test_add_without_selection_and_dismiss(self):
        cursor = self.window.source_editor.textCursor()
        cursor.clearSelection()
        self.window.source_editor.setTextCursor(cursor)
        open_chat(self.window)
        self.panel = self.window.chat_panel
        self.complete(ChatReply("", "Added text"))
        self.assertTrue(self.panel.apply_button.isEnabled())
        original = self.window.source_editor.toPlainText()
        self.panel.dismiss_button.click()
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), original)
        self.assertTrue(self.panel.proposal_card.isHidden())
        self.complete(ChatReply("", "Added text"))
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), original + "\n\nAdded text")

    def test_stale_text_rejected(self):
        self.complete(ChatReply("", "changed"))
        self.window.source_editor.insertPlainText("manual edit")
        original = self.window.source_editor.toPlainText()
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), original)
        self.assertIn("changed", self.panel.status.text())

    def test_followup_can_refine_applied_replacement(self):
        self.complete(ChatReply("", "first edit"))
        self.panel.apply_mode.setCurrentIndex(1)
        self.panel.apply_replacement()
        args = self.complete(ChatReply("", "second edit"))
        self.assertEqual(args[0]["selected_text"], "first edit")
        self.panel.apply_mode.setCurrentIndex(1)
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), "😀 repeated text second edit")

    def test_changed_workspace_cannot_receive_replacement(self):
        self.complete(ChatReply("", "wrong page"))
        path = Path(self.temp.name) / "other.png"
        Image.new("RGB", (100, 100)).save(path)
        self.window.set_book(BookDocument.open([path]))
        self.panel.apply_replacement()
        self.assertEqual(self.window.source_editor.toPlainText(), "")

    def test_answer_only_and_followup(self):
        self.complete(ChatReply("A useful answer", None))
        self.assertTrue(self.panel.apply_button.isHidden())
        args = self.complete(ChatReply("Follow-up answer", None))
        self.assertEqual(len(args[1]), 2)

    def test_stop_ignores_late_result(self):
        self.panel.token = 5
        self.panel.set_pending(True)
        self.panel.stop()
        self.panel.results.put((5, "request", ChatReply("late", "wrong"), None))
        self.panel.poll_results()
        self.assertIsNone(self.panel.reply)
        self.assertEqual(self.window.source_editor.toPlainText(), self.panel.original)

    def test_region_crop_zoom_and_attachment_options(self):
        self.panel.pick_region()
        view = self.window.preview
        view.zoom(1.4)
        start = view.mapFromScene(QPointF(220, 300))
        end = view.mapFromScene(QPointF(420, 500))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(view.region_mode)
        self.assertTrue(self.panel.attach_region.isChecked())
        with Image.open(BytesIO(self.panel.region)) as crop:
            self.assertAlmostEqual(crop.width, 200, delta=4)
            self.assertAlmostEqual(crop.height, 200, delta=4)
            self.assertEqual(crop.getpixel((20, 20)), (255, 255, 255))
        self.panel.attach_page.setChecked(True)
        args = self.complete(ChatReply("Image answer", None))
        self.assertEqual(len(args[3]), 2)
        self.assertEqual(args[0]["image_attachments"], ["full page", "selected region"])
        open_chat(self.window)
        self.panel = self.window.chat_panel
        self.assertTrue(self.panel.attach_region.isChecked())

    def test_cancel_region_returns_to_panel(self):
        self.panel.pick_region()
        QTest.keyClick(self.window.preview, Qt.Key.Key_Escape)
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel.attach_region.isEnabled())


class ChatServiceTests(unittest.TestCase):
    def test_reply_validation(self):
        for value in [{}, {"answer": "", "replacement": None}, {"answer": "ok", "replacement": False}]:
            with self.assertRaises(ValueError):
                ChatReply.from_dict(value)
        self.assertEqual(ChatReply.from_dict({"answer": "", "replacement": ""}).replacement, "")

    def test_real_sdk_chat_schema_image_and_temperature(self):
        requests = []
        real_client = genai.Client

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": json.dumps({"answer": "Answer", "replacement": None})}]},
                "finishReason": "STOP",
            }]})

        def factory(**kwargs):
            kwargs["http_options"].httpx_client = httpx.Client(transport=httpx.MockTransport(respond))
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            reply = GeminiService("test-key", "gemini-2.5-flash").chat(
                {"selected_text": "Hello"}, [], "Explain", [(b"image", "image/png")])
        self.assertEqual(reply.answer, "Answer")
        config = requests[0]["generationConfig"]
        self.assertEqual(config["temperature"], 0)
        self.assertIn("replacement", config["responseJsonSchema"]["properties"])
        parts = requests[0]["contents"][0]["parts"]
        self.assertIn("inlineData", parts[0])
        self.assertEqual(json.loads(parts[1]["text"])["request"], "Explain")
