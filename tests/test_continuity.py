import json
import os
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from book_translator.continuity import continuity_options, parse_continuity, apply_previous_edit
from book_translator.core import BookDocument
from book_translator.preferences import GenerationOptions
from book_translator.gemini_service import GeminiService
from book_translator.openai_service import OpenAIService
from book_translator.nvidia_service import NvidiaService
from book_translator.ui import MainWindow, PageJob


class ContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        paths = [Path(self.temp.name) / f"{n}.png" for n in range(2)]
        for path in paths:
            Image.new("RGB", (80, 100), "white").save(path)
        self.book = BookDocument.open(paths)
        page = self.book.pages[0]
        page.source = page.translated_from = "She opened the door and"
        page.translation = "She opened the door and\n\nTo be continued..."
        page.target_language = "English"
        self.book.pages[1].source = "walked into the garden."
        self.options = continuity_options(self.book, 1, GenerationOptions(), "English")
        self.data = {"text": "walked into the garden.", "confidence": .95, "regions": [],
                     "previous_page_edit": {"old_text": "\n\nTo be continued...", "new_text": "",
                                            "reason": "Remove invented page-break filler."}}

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_suffix_only_and_transient_metadata(self):
        result = parse_continuity(self.data, False, self.options.previous_page)
        self.assertNotIn("previous_page", asdict(result))
        self.assertNotIn("previous_edit", asdict(result))
        apply_previous_edit(self.book, result)
        self.assertEqual(self.book.pages[0].translation, "She opened the door and")
        self.assertEqual(self.book.pages[0].source, "She opened the door and")
        for old in ("She opened", "", "X" * 1600):
            data = dict(self.data, previous_page_edit=dict(self.data["previous_page_edit"], old_text=old))
            with self.assertRaises(ValueError):
                parse_continuity(data, False, self.options.previous_page)

    def test_stale_user_edit_is_protected(self):
        result = parse_continuity(self.data, False, self.options.previous_page)
        self.book.pages[0].translation = "My manual correction"
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_previous_edit(self.book, result)
        self.assertEqual(self.book.pages[0].translation, "My manual correction")

    def test_enablement_and_language_guards(self):
        for options in (GenerationOptions(fix_previous_page=False), GenerationOptions(context_enabled=False),
                        GenerationOptions(pages_before=0)):
            self.assertIs(continuity_options(self.book, 1, options, "English"), options)
        base = GenerationOptions()
        self.assertIs(continuity_options(self.book, 0, base, "English"), base)
        self.assertIs(continuity_options(self.book, 1, base, "Vietnamese"), base)
        self.book.pages[0].source = "Edited source"
        self.assertIs(continuity_options(self.book, 1, base, "English"), base)

    def test_combined_schema_result_and_undo(self):
        combined = {"extraction": {"text": self.data["text"], "confidence": 1, "regions": []},
                    "translation": self.data}
        result = parse_continuity(combined, True, self.options.previous_page)
        window = MainWindow()
        window.set_book(self.book)
        original = self.book.pages[0].translation
        try:
            job = PageJob(1, "both", 1, "", "English")
            window._apply_result(job, result)
            self.assertEqual(self.book.pages[0].translation, "She opened the door and")
            self.assertEqual(self.book.pages[1].translation, self.data["text"])
            self.assertTrue(window.undo_continuity_action.isEnabled())
            window.undo_previous_fix()
            self.assertEqual(self.book.pages[0].translation, original)
            self.assertEqual(self.book.pages[1].translation, self.data["text"])
        finally:
            window.dirty = False
            window.close()

    def test_gemini_request_contains_patch_schema(self):
        service = GeminiService("key", "gemini-test", self.options)
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason="STOP")], text=json.dumps(self.data))
        manager = MagicMock()
        manager.__enter__.return_value = client
        with patch.object(service, "_client", return_value=manager):
            report = service.translate("walked into the garden.", "English")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIn("previous_page_edit", config.response_json_schema["properties"])
        self.assertIn("To be continued", config.system_instruction)
        self.assertEqual(report.previous_edit["new_text"], "")
        # One request includes both the current translation and preceding suffix edit.
        client.models.generate_content.assert_called_once()

    def test_openai_and_nvidia_patch_contract(self):
        for cls, model in ((OpenAIService, "gpt-test"), (NvidiaService, "test/model")):
            service = cls("key", model, self.options)
            if cls is OpenAIService:
                response = {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(self.data)}]}]}
            else:
                response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.data)}}]}
            with patch.object(service, "_request", return_value=response) as request:
                report = service.translate("walked into the garden.", "English")
            self.assertEqual(report.previous_edit["old_text"], "\n\nTo be continued...")
            self.assertIn("previous_page_edit", json.dumps(request.call_args.kwargs["json"]))
            request.assert_called_once()
