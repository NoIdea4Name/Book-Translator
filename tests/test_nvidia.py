"""NIM contract and integration checks; all provider requests use mock HTTP."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from book_translator.api_errors import APIError
from book_translator.api_rotation import ApiProfile, ApiRouter, create_service
from book_translator.confidence import Assessment, CombinedAssessment
from book_translator.core import BookDocument
from book_translator.gemini_service import GeminiService, GeminiError
from book_translator.nvidia_service import NvidiaService, DEFAULT_NVIDIA_MODEL, normalize_nvidia_model
from book_translator.preferences import GenerationOptions
from book_translator.settings import SettingsDialog
from book_translator.ui import MainWindow

RESULT = {"text": "Hello", "confidence": .9, "regions": []}
REQUEST_ID = "36d0191f-b6a7-48f4-9c0d-14a5376f81bc"


def completion(result=RESULT, finish="stop", **message_fields):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": json.dumps(result), **message_fields}}]}


class FastEvent:
    def __init__(self, cancel_on_wait=False):
        self.cancelled = False
        self.cancel_on_wait = cancel_on_wait
        self.waits = []

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.cancelled = self.cancel_on_wait
        return self.cancelled


class NvidiaServiceTests(unittest.TestCase):
    def client(self, responder):
        return patch.object(NvidiaService, "_client", lambda service: httpx.Client(
            base_url=service.base_url, headers={"Authorization": f"Bearer {service.api_key}"},
            transport=httpx.MockTransport(responder), follow_redirects=False))

    def test_nvidia_waits_longer_without_changing_other_providers(self):
        from book_translator.openai_service import OpenAIService
        with NvidiaService("key", DEFAULT_NVIDIA_MODEL)._client() as client:
            self.assertEqual(client.timeout.read, 600)
            self.assertEqual(client.timeout.connect, 20)
            self.assertFalse(client.follow_redirects)
        with OpenAIService("key", "gpt-model")._client() as client:
            self.assertEqual(client.timeout.read, 120)

    def test_generation_read_timeout_does_not_retry_or_rotate(self):
        requests = []
        def respond(request):
            requests.append(request)
            raise httpx.ReadTimeout("secret-key")
        router = ApiRouter([ApiProfile("nvidia", DEFAULT_NVIDIA_MODEL, "secret-key"), ApiProfile("openai", "gpt-model", "backup")])
        with self.client(respond), self.assertRaisesRegex(APIError, "10 minutes") as raised:
            router.call("extract", (b"image", "image/png"), None, FastEvent())
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("secret-key", str(raised.exception))
        self.assertEqual(len(requests), 1)
        self.assertEqual(router.blocked_until, {})

    def test_connect_timeout_is_distinct_and_retryable(self):
        def respond(request):
            raise httpx.ConnectTimeout("secret-key")
        with self.client(respond), self.assertRaisesRegex(APIError, "establish a connection") as raised:
            NvidiaService("secret-key", DEFAULT_NVIDIA_MODEL).test_connection()
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret-key", str(raised.exception))

    def test_three_actions_send_page_images_and_validated_confidence(self):
        bodies = []
        def respond(request):
            self.assertEqual(str(request.url), "https://integrate.api.nvidia.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            bodies.append(body)
            result = {"extraction": RESULT, "translation": RESULT} if len(bodies) == 3 else RESULT
            return httpx.Response(200, json=completion(result, reasoning_content="Do not show internal reasoning"))
        with self.client(respond):
            service = NvidiaService("test-key", DEFAULT_NVIDIA_MODEL, GenerationOptions(.25, "COMMON", "EXTRACT", "TRANSLATE"))
            self.assertIsInstance(service.extract(b"image", "image/png"), Assessment)
            self.assertIsInstance(service.translate("Hello", "Vietnamese", b"image"), Assessment)
            self.assertIsInstance(service.extract_and_translate(b"image", "image/png", "Vietnamese"), CombinedAssessment)
        for index, body in enumerate(bodies):
            self.assertEqual(body["model"], DEFAULT_NVIDIA_MODEL)
            self.assertEqual(body["reasoning_effort"], "max")
            self.assertEqual(body["temperature"], .25)
            self.assertEqual(body["max_tokens"], 16384)
            self.assertFalse(body["stream"])
            self.assertEqual(body["seed"], 0)
            for unsupported in ("response_format", "input", "store", "max_output_tokens"):
                self.assertNotIn(unsupported, body)
            system, user = body["messages"]
            self.assertIn("COMMON", system["content"])
            self.assertEqual("EXTRACT" in system["content"], index in (0, 2))
            self.assertEqual("TRANSLATE" in system["content"], index in (1, 2))
            self.assertIn('"confidence"', system["content"])
            self.assertEqual(user["content"][1]["image_url"]["url"], "data:image/png;base64,aW1hZ2U=")
        self.assertIn("Hello", bodies[1]["messages"][1]["content"][0]["text"])

    def test_connection_test_uses_sample_low_reasoning_and_no_custom_instructions(self):
        def respond(request):
            body = json.loads(request.content)
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertEqual(body["max_tokens"], 4096)
            self.assertEqual(body["temperature"], 0)
            self.assertNotIn("PRIVATE_CUSTOM_INSTRUCTION", json.dumps(body))
            self.assertIn("image_url", body["messages"][1]["content"][1])
            return httpx.Response(200, json=completion(dict(RESULT, text="CONNECTION_OK")))
        with self.client(respond):
            service = NvidiaService("key", DEFAULT_NVIDIA_MODEL, GenerationOptions(common="PRIVATE_CUSTOM_INSTRUCTION"))
            self.assertIn("verified", service.test_connection())

    def test_sampling_can_be_omitted_and_k3_temperature_is_capped(self):
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=completion())
        with self.client(respond):
            NvidiaService("key", DEFAULT_NVIDIA_MODEL, GenerationOptions(1.8), reasoning_effort="high").extract(b"i", "image/png")
            NvidiaService("key", DEFAULT_NVIDIA_MODEL, send_temperature=False).extract(b"i", "image/png")
            NvidiaService("key", "vendor/other-model", GenerationOptions(.3)).extract(b"i", "image/png")
        self.assertEqual(bodies[0]["temperature"], 1)
        self.assertEqual(bodies[0]["reasoning_effort"], "high")
        self.assertNotIn("temperature", bodies[1])
        self.assertNotIn("reasoning_effort", bodies[2])

    def test_model_ids_and_catalog_preserve_owner_prefix(self):
        self.assertEqual(normalize_nvidia_model(' "moonshotai/kimi-k3" '), DEFAULT_NVIDIA_MODEL)
        for invalid in ("https://build.nvidia.com/moonshotai/kimi-k3", "kimi-k3", "../model", "owner/model/other", None):
            with self.assertRaises(APIError): normalize_nvidia_model(invalid)
        def respond(request):
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(200, json={"data": [{"id": DEFAULT_NVIDIA_MODEL}, {"id": "vendor/embed-v1"}, {"id": None}, {}, None]})
        with self.client(respond):
            self.assertEqual(NvidiaService("key", DEFAULT_NVIDIA_MODEL).list_models(), [DEFAULT_NVIDIA_MODEL])

    def test_refused_truncated_and_invalid_results_never_become_text(self):
        cases = [
            (completion(finish="length"), False),
            (completion(finish="content_filter"), False),
            (completion(refusal="No"), False),
            (completion(finish="tool_calls"), False),
            (completion(content=None, reasoning_content=json.dumps(RESULT)), True),
            (completion(content="not JSON"), True),
            (completion({"text": "partial"}), True),
            ({"choices": []}, True),
        ]
        for response, retryable in cases:
            with self.subTest(response=response), self.assertRaises(APIError) as raised:
                NvidiaService._parse_result(response, False)
            self.assertEqual(raised.exception.retryable, retryable)
        self.assertEqual(NvidiaService._parse_result(completion(content="```json\n" + json.dumps(RESULT) + "\n```"), False).text, "Hello")

    def test_http_errors_rotate_and_redact_key(self):
        for code in (401, 422, 429, 503):
            with self.client(lambda request: httpx.Response(code, json={"detail": "Rejected secret-key"}, headers={"Retry-After": "125"})):
                with self.assertRaises(APIError) as raised:
                    NvidiaService("secret-key", DEFAULT_NVIDIA_MODEL).extract(b"image", "image/png")
            self.assertNotIn("secret-key", str(raised.exception))
            self.assertIn("NVIDIA NIM", str(raised.exception))
            self.assertEqual(raised.exception.rotate_now, code != 503)
            self.assertEqual(raised.exception.retryable, code == 503)
            if code == 429: self.assertEqual(raised.exception.cooldown, 125)

    def test_accepted_request_polls_without_resubmitting(self):
        requests = []
        def respond(request):
            requests.append((request.method, request.url.path))
            if len(requests) == 1:
                return httpx.Response(202, json={"requestId": REQUEST_ID})
            if len(requests) == 2:
                return httpx.Response(202, headers={"Retry-After": "2"})
            return httpx.Response(200, json=completion())
        service = NvidiaService("key", DEFAULT_NVIDIA_MODEL)
        service.cancel_event = FastEvent()
        with self.client(respond): self.assertEqual(service.extract(b"image", "image/png").text, "Hello")
        self.assertEqual(requests, [("POST", "/v1/chat/completions"), ("GET", f"/v1/status/{REQUEST_ID}"), ("GET", f"/v1/status/{REQUEST_ID}")])
        self.assertEqual(service.cancel_event.waits, [1, 2])

    def test_cancel_and_invalid_pending_id_stop_polling(self):
        for response, event, expected in [
            (httpx.Response(202, json={"requestId": "../bad"}), FastEvent(), "valid request ID"),
            (httpx.Response(202, headers={"NVCF-REQID": REQUEST_ID}), FastEvent(True), "cancelled"),
            (httpx.Response(202, json={"requestId": REQUEST_ID}, headers={"Retry-After": "999"}), FastEvent(), "still pending"),
        ]:
            calls = []
            def respond(request):
                calls.append(request)
                return response
            service = NvidiaService("key", DEFAULT_NVIDIA_MODEL)
            service.cancel_event = event
            with self.client(respond), self.assertRaisesRegex(APIError, expected) as raised:
                service.extract(b"image", "image/png")
            self.assertEqual(len(calls), 1)
            self.assertFalse(raised.exception.retryable)

    def test_poll_failure_does_not_retry_accepted_generation_or_rotate(self):
        for failure in (httpx.ReadTimeout("timeout"), httpx.Response(503)):
            requests = []
            def respond(request):
                requests.append(request.method)
                if request.method == "POST": return httpx.Response(202, json={"requestId": REQUEST_ID})
                if isinstance(failure, Exception): raise failure
                return failure
            router = ApiRouter([ApiProfile("nvidia", DEFAULT_NVIDIA_MODEL, "key"), ApiProfile("openai", "gpt-model", "backup")])
            with self.client(respond), self.assertRaisesRegex(APIError, "accepted request"):
                router.call("extract", (b"image", "image/png"), None, FastEvent())
            self.assertEqual(requests, ["POST", "GET"])
            self.assertEqual(router.blocked_until, {})


class NvidiaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        # Destroy closed widgets while Qt is alive, not during interpreter teardown.
        for widget in self.app.topLevelWidgets():
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_settings_keep_nvidia_model_and_reasoning_through_reorder(self):
        dialog = SettingsDialog(None, "google-key", "gemini-model")
        dialog.add_profile()
        dialog.provider.setCurrentIndex(dialog.provider.findData("nvidia"))
        self.assertEqual(dialog.model.currentText(), DEFAULT_NVIDIA_MODEL)
        self.assertFalse(dialog.reasoning.isHidden())
        dialog.key.setText("nvidia-key")
        dialog.reasoning.setCurrentText("low")
        dialog.send_temperature.setChecked(False)
        dialog.move_profile(-1)
        dialog.profile_selector.setCurrentIndex(1)
        self.assertTrue(dialog.reasoning.isHidden())
        dialog.profile_selector.setCurrentIndex(0)
        self.assertEqual(dialog.reasoning.currentText(), "low")
        with patch("book_translator.settings.save_settings", return_value=""):
            dialog.accept()
        profile = dialog.profiles[0]
        self.assertEqual(profile, ApiProfile("nvidia", DEFAULT_NVIDIA_MODEL, "nvidia-key", False, "low"))
        self.assertNotIn("nvidia-key", repr(profile))
        service = create_service(profile)
        self.assertIsInstance(service, NvidiaService)
        self.assertEqual(service.reasoning_effort, "low")

    def test_environment_prefills_nvidia_and_provider_status(self):
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-env-key"}, clear=True):
            window = MainWindow()
        try:
            self.assertEqual(window.api_profiles, [ApiProfile("nvidia", DEFAULT_NVIDIA_MODEL, "test-env-key")])
            self.assertIn("NVIDIA NIM", window.connection_label.text())
        finally:
            window.close()

    def test_combined_batch_rotates_to_nvidia_and_keeps_page_results(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for index in range(2):
                path = Path(folder) / f"{index}.png"
                Image.new("RGB", (50, 80), "white").save(path)
                paths.append(path)
            window = MainWindow()
            window.set_book(BookDocument.open(paths))
            from dataclasses import replace
            window.generation_options = replace(window.generation_options, auto_update_summary=False)
            window.api_profiles = [ApiProfile("gemini", "gemini-model", "first-key"), ApiProfile("nvidia", DEFAULT_NVIDIA_MODEL, "next-key")]
            window.thumbnails.selectAll()
            result = CombinedAssessment(Assessment("Source", .95, []), Assessment("Translation", .9, []))
            try:
                with patch.object(GeminiService, "extract_and_translate", side_effect=GeminiError("quota", rotate_now=True)) as google, patch.object(NvidiaService, "extract_and_translate", return_value=result) as nvidia:
                    window.extract_and_translate()
                    deadline = time.monotonic() + 4
                    while window._busy and time.monotonic() < deadline:
                        self.app.processEvents()
                        window._poll_results()
                        time.sleep(.005)
                    self.assertFalse(window._busy)
                    self.assertEqual(google.call_count, 1)
                    self.assertEqual(nvidia.call_count, 2)
                    self.assertIsInstance(nvidia.call_args.args[0], bytes)
                self.assertTrue(all(page.source == "Source" and page.translation == "Translation" for page in window.book.pages))
                self.assertIn("NVIDIA NIM", window.connection_label.text())
                self.assertEqual(window._router.snapshot(), (1, True))
                self.assertEqual(window._verified_connection, ("next-key", DEFAULT_NVIDIA_MODEL))
            finally:
                window.cancel_request()
                window.dirty = False
                window.close()
