import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google import genai
import httpx

from book_translator.gemini_service import GeminiError, GeminiService, response_text


def assessment_json(text="Xin chào", confidence=0.94, regions=None):
    return json.dumps({"text": text, "confidence": confidence, "regions": regions or []})


class GeminiTests(unittest.TestCase):
    def test_real_sdk_serializes_single_page_text_and_inline_image(self):
        requests = []
        real_client = genai.Client

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": assessment_json()}]},
                            "finishReason": "STOP",
                        }
                    ],
                },
            )

        def factory(**kwargs):
            kwargs["http_options"].httpx_client = httpx.Client(
                transport=httpx.MockTransport(respond)
            )
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            service = GeminiService("test-key", "gemini-2.5-flash")
            self.assertEqual(service.translate("Only this page", "Vietnamese").text, "Xin chào")
            result = service.extract(b"test-image", "image/png")
            self.assertEqual(result.text, "Xin chào")
            self.assertEqual(result.confidence, 0.94)
            service.translate("Edited source", "Vietnamese", b"original-page", "image/png")
        self.assertEqual(requests[0]["contents"][0]["parts"][0]["text"], "Only this page")
        self.assertIn("Vietnamese", requests[0]["systemInstruction"]["parts"][0]["text"])
        inline = requests[1]["contents"][0]["parts"][0]["inlineData"]
        # The API accepts both protobuf snake_case and lowerCamelCase field names.
        self.assertEqual(inline.get("mimeType", inline.get("mime_type")), "image/png")
        self.assertEqual(base64.b64decode(inline["data"]), b"test-image")
        self.assertEqual(requests[0]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn(
            "regions", requests[0]["generationConfig"]["responseJsonSchema"]["properties"]
        )
        self.assertIn(
            "[top,left,bottom,right]", requests[0]["systemInstruction"]["parts"][0]["text"]
        )
        parts = requests[2]["contents"][0]["parts"]
        self.assertEqual(base64.b64decode(parts[0]["inlineData"]["data"]), b"original-page")
        self.assertTrue(parts[1]["text"].endswith("Edited source"))

    def test_invalid_confidence_data_is_rejected_without_saving(self):
        real_client = genai.Client

        def factory(**kwargs):
            def respond(request):
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {"parts": [{"text": assessment_json(confidence=95)}]},
                                "finishReason": "STOP",
                            }
                        ]
                    },
                )

            kwargs["http_options"].httpx_client = httpx.Client(
                transport=httpx.MockTransport(respond)
            )
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            with self.assertRaisesRegex(GeminiError, "invalid confidence data"):
                GeminiService("test-key", "gemini-2.5-flash").translate("Source", "Vietnamese")

    def test_partial_empty_and_blocked_answers_are_not_saved(self):
        for reason in ["MAX_TOKENS", "SAFETY", "RECITATION"]:
            response = SimpleNamespace(
                candidates=[SimpleNamespace(finish_reason=reason)], text="partial"
            )
            with self.assertRaises(GeminiError):
                response_text(response)
        for response in [
            SimpleNamespace(candidates=[], text=""),
            SimpleNamespace(candidates=[SimpleNamespace(finish_reason="STOP")], text=""),
        ]:
            with self.assertRaises(GeminiError):
                response_text(response)

    def test_quota_error_is_actionable_and_no_key_is_disclosed(self):
        real_client = genai.Client

        def factory(**kwargs):
            def respond(request):
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": 429,
                            "message": "Quota exceeded",
                            "status": "RESOURCE_EXHAUSTED",
                        }
                    },
                )

            kwargs["http_options"].httpx_client = httpx.Client(
                transport=httpx.MockTransport(respond)
            )
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            with self.assertRaisesRegex(GeminiError, "quota"):
                GeminiService("secret-test-key", "gemini-2.5-flash").translate("Test", "Vietnamese")

    def test_empty_key_source_and_target_fail_before_network(self):
        with self.assertRaises(GeminiError):
            GeminiService("", "gemini-2.5-flash")
        service = GeminiService("test", "gemini-2.5-flash")
        with self.assertRaises(GeminiError):
            service.translate(" ", "Vietnamese")
        with self.assertRaises(GeminiError):
            service.translate("Hello", " ")


if __name__ == "__main__":
    unittest.main()
