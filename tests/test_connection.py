import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import time
import unittest
from unittest.mock import patch

from google import genai
import httpx
from PySide6.QtWidgets import QApplication

from book_translator.gemini_service import GeminiError, GeminiService, normalize_model
from book_translator.settings import SettingsDialog


class ConnectionTests(unittest.TestCase):
    def client_mock(self, respond):
        real_client = genai.Client

        def factory(**kwargs):
            self.assertEqual(
                kwargs["http_options"].base_url, "https://generativelanguage.googleapis.com"
            )
            self.assertEqual(kwargs["http_options"].api_version, "v1beta")
            kwargs["http_options"].httpx_client = httpx.Client(
                transport=httpx.MockTransport(respond)
            )
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        return patch("book_translator.gemini_service.genai.Client", side_effect=factory)

    def test_model_list_paginates_and_filters_specialized_models(self):
        seen = []

        def respond(request):
            seen.append(request)
            self.assertEqual(request.url.path, "/v1beta/models")
            if "pageToken" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/gemini-2.5-flash",
                                "supportedGenerationMethods": ["generateContent"],
                            },
                            {
                                "name": "models/gemini-2.5-flash-image",
                                "supportedGenerationMethods": ["generateContent"],
                            },
                            {
                                "name": "models/gemini-embedding",
                                "supportedGenerationMethods": ["embedContent"],
                            },
                        ],
                        "nextPageToken": "second",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.1-flash-lite",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )

        with patch.dict(
            os.environ, {"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_GENAI_USE_ENTERPRISE": "true"}
        ):
            with self.client_mock(respond):
                models = GeminiService("test-key", "gemini-old").list_models()
        self.assertEqual(models, ["gemini-2.5-flash", "gemini-3.1-flash-lite"])
        self.assertEqual(len(seen), 2)

    def test_404_preserves_google_reason_and_redacts_key(self):
        def respond(request):
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "status": "NOT_FOUND",
                        "message": "models/old is not supported for generateContent. key=secret-value",
                    }
                },
            )

        with self.client_mock(respond):
            with self.assertRaises(GeminiError) as raised:
                GeminiService("secret-value", "old").translate("Hello", "Vietnamese")
        message = str(raised.exception)
        self.assertIn("not supported for generateContent", message)
        self.assertIn("Load models", message)
        self.assertIn("Model: old", message)
        self.assertNotIn("secret-value", message)

    def test_connection_checks_image_and_actual_structured_generation(self):
        def respond(request):
            self.assertEqual(request.method, "POST")
            self.assertIn("gemini-2.5-flash:generateContent", request.url.path)
            body = json.loads(request.content)
            inline = body["contents"][0]["parts"][0]["inlineData"]
            self.assertEqual(inline.get("mimeType", inline.get("mime_type")), "image/png")
            self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "text": "CONNECTION_OK",
                                                "confidence": 1,
                                                "regions": [],
                                            }
                                        )
                                    }
                                ]
                            },
                        }
                    ]
                },
            )

        with self.client_mock(respond):
            result = GeminiService("test-key", "models/gemini-2.5-flash").test_connection()
        self.assertIn("Connection verified", result)

    def test_connection_rejects_an_unexpected_test_result(self):
        def respond(request):
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "text": "An unexpected answer",
                                                "confidence": 1,
                                                "regions": [],
                                            }
                                        )
                                    }
                                ]
                            },
                        }
                    ]
                },
            )

        with self.client_mock(respond):
            with self.assertRaisesRegex(GeminiError, "expected result"):
                GeminiService("test-key", "gemini-model").test_connection()

    def test_model_ids_are_normalized_without_guessing_display_names(self):
        self.assertEqual(normalize_model(' "models/gemini-2.5-flash" '), "gemini-2.5-flash")
        for invalid in ("Gemini 2.5 Flash", "https://example.com/model", "", "models/"):
            with self.assertRaises(GeminiError):
                normalize_model(invalid)


class ConnectionDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_check(self, dialog):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            dialog._poll_check()
            if not dialog._checking:
                return
            time.sleep(0.01)
        self.fail("Connection check did not finish")

    def test_discovery_requires_selection_and_test_verification_resets_on_key_edit(self):
        dialog = SettingsDialog(None, "test-key", "gemini-unavailable")
        try:
            with patch.object(GeminiService, "list_models", return_value=["gemini-available"]):
                dialog.load_models()
                self.wait_for_check(dialog)
            self.assertEqual(dialog.model.currentText(), "")
            self.assertIsNone(dialog.tested_connection)
            dialog.model.setCurrentText("gemini-available")
            with patch.object(GeminiService, "test_connection", return_value="Connection verified"):
                dialog.test_connection()
                self.wait_for_check(dialog)
            self.assertEqual(dialog.tested_connection, ("test-key", "gemini-available"))
            dialog.key.setText("another-key")
            self.assertIsNone(dialog.tested_connection)
        finally:
            dialog.reject()

    def test_connection_failure_does_not_show_verified(self):
        dialog = SettingsDialog(None, "test-key", "gemini-model")
        try:
            with patch.object(
                GeminiService, "test_connection", side_effect=GeminiError("Quota exceeded")
            ):
                dialog.test_connection()
                self.wait_for_check(dialog)
            self.assertIsNone(dialog.tested_connection)
            self.assertIn("Quota exceeded", dialog.check_status.toPlainText())
            self.assertTrue(dialog.test_button.isEnabled())
        finally:
            dialog.reject()
