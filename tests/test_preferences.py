import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from google import genai
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from book_translator.gemini_service import GeminiService
from book_translator.preferences import GenerationOptions, load_options, save_options
from book_translator.settings import SettingsDialog


class PreferenceTests(unittest.TestCase):
    def test_persistence_and_invalid_temperature(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "settings.ini")
            store = QSettings(path, QSettings.Format.IniFormat)
            options = GenerationOptions(.35, "Names unchanged", "Keep lines", "Tiếng Việt")
            save_options(options, store)
            self.assertEqual(load_options(QSettings(path, QSettings.Format.IniFormat)), options)
            self.assertNotIn("api_key", Path(path).read_text())
            for value in (-1, 3, float("nan"), True, "zero"):
                with self.assertRaises(ValueError):
                    GenerationOptions(value)
            store.setValue("generation_options", "invalid")
            self.assertEqual(load_options(store), GenerationOptions())

    def test_real_sdk_sends_correct_preferences_and_temperature(self):
        requests = []
        real_client = genai.Client
        assessment = {"text": "Hello", "confidence": .9, "regions": []}

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            schema = body["generationConfig"]["responseJsonSchema"]
            result = {"extraction": assessment, "translation": assessment} if "extraction" in schema["properties"] else assessment
            if "Connection test" in json.dumps(body["contents"]):
                result = dict(assessment, text="CONNECTION_OK")
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": json.dumps(result)}]}, "finishReason": "STOP"
            }]})

        def factory(**kwargs):
            kwargs["http_options"].httpx_client = httpx.Client(transport=httpx.MockTransport(respond))
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        options = GenerationOptions(.35, "COMMON_MARKER", "EXTRACTION_MARKER", "TRANSLATION_MARKER")
        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            service = GeminiService("key", "gemini-model", options)
            service.extract(b"image", "image/png")
            service.translate("Hello", "Vietnamese")
            service.extract_and_translate(b"image", "image/png", "Vietnamese")
            service.test_connection()
            GeminiService("key", "gemini-model").translate("Hello", "Vietnamese")
        for index in (0, 1, 2):
            self.assertEqual(requests[index]["generationConfig"]["temperature"], .35)
            instruction = json.dumps(requests[index]["systemInstruction"])
            self.assertIn("COMMON_MARKER", instruction)
            self.assertEqual("EXTRACTION_MARKER" in instruction, index in (0, 2))
            self.assertEqual("TRANSLATION_MARKER" in instruction, index in (1, 2))
        self.assertNotIn("COMMON_MARKER", json.dumps(requests[3]))
        self.assertEqual(requests[3]["generationConfig"]["temperature"], 0)
        self.assertEqual(requests[4]["generationConfig"]["temperature"], 0)
        self.assertEqual(len(requests), 5)

    def test_dialog_save_and_cancel(self):
        app = QApplication.instance() or QApplication([])
        original = GenerationOptions()
        dialog = SettingsDialog(None, "key", "gemini-model", options=original)
        dialog.temperature.setValue(.4)
        dialog.common_instructions.setPlainText("All tasks")
        dialog.extraction_instructions.setPlainText("Keep line breaks")
        dialog.translation_instructions.setPlainText("Use plain language")
        with patch("book_translator.settings.save_settings", return_value="") as save:
            dialog.accept()
        self.assertEqual(save.call_args.args[0], GenerationOptions(.4, "All tasks", "Keep line breaks", "Use plain language"))
        self.assertEqual(dialog.options.temperature, .4)
        cancelled = SettingsDialog(None, "key", "gemini-model", options=original)
        cancelled.temperature.setValue(1)
        with patch("book_translator.settings.save_settings", return_value="") as save:
            cancelled.reject()
        save.assert_not_called()
        self.assertEqual(cancelled.options, original)
