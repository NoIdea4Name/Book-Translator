import json
import unittest
from unittest.mock import patch

import httpx
from google import genai

from book_translator.confidence import CombinedAssessment
from book_translator.gemini_service import GeminiService


class CombinedTests(unittest.TestCase):
    def test_one_request_returns_both_assessments(self):
        requests = []
        real_client = genai.Client
        payload = {
            "extraction": {"text": "Hello", "confidence": .9, "regions": []},
            "translation": {"text": "Xin chào", "confidence": .85, "regions": []},
        }

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": json.dumps(payload)}]},
                "finishReason": "STOP",
            }]})

        def factory(**kwargs):
            kwargs["http_options"].httpx_client = httpx.Client(
                transport=httpx.MockTransport(respond)
            )
            kwargs["http_options"].async_client_args = {"trust_env": False}
            return real_client(**kwargs)

        with patch("book_translator.gemini_service.genai.Client", side_effect=factory):
            result = GeminiService("test", "gemini-2.5-flash").extract_and_translate(
                b"page-image", "image/png", "Vietnamese"
            )
        self.assertEqual(len(requests), 1)
        self.assertEqual(result.extraction.text, "Hello")
        self.assertEqual(result.translation.text, "Xin chào")
        schema = requests[0]["generationConfig"]["responseJsonSchema"]
        self.assertEqual(set(schema["required"]), {"extraction", "translation"})
        self.assertIn("Vietnamese", requests[0]["systemInstruction"]["parts"][0]["text"])

    def test_incomplete_or_invalid_pair_is_rejected(self):
        good = {"text": "Hello", "confidence": .9, "regions": []}
        for bad in (None, {}, dict(good, text=" "), dict(good, confidence=99)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CombinedAssessment.from_dict({"extraction": good, "translation": bad})
