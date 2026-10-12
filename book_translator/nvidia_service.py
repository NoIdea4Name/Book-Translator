"""NVIDIA hosted NIM adapter, using Chat Completions and inline page images.

Shares task prompts/validation with OpenAIService, but uses NVIDIA's endpoint,
message format and response parser. Only final content is used, never reasoning.
"""
import base64
import json
import re
import threading
import time
import uuid

import httpx

from .api_errors import APIError, retry_after_seconds
from .token_usage import attach_usage
from .memory import memory_contract, parse_memory
from .continuity import continuity_schema, continuity_instruction, parse_continuity
from .confidence import Assessment, CombinedAssessment, RESULT_SCHEMA, COMBINED_RESULT_SCHEMA
from .config import NVIDIA_RESPONSE_TIMEOUT_SECONDS
from .openai_service import OpenAIService
from .prompts import CONFIDENCE_INSTRUCTIONS

DEFAULT_NVIDIA_MODEL = "moonshotai/kimi-k3"


def normalize_nvidia_model(model):
    if not isinstance(model, str):
        raise APIError("NVIDIA model IDs must be text.")
    model = model.strip().strip("\"'")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", model):
        raise APIError("Enter an NVIDIA model ID such as moonshotai/kimi-k3, not a website URL.")
    return model


class NvidiaService(OpenAIService):
    provider_name = "NVIDIA NIM"
    base_url = "https://integrate.api.nvidia.com/v1/"
    normalize_model = staticmethod(normalize_nvidia_model)

    def __init__(self, api_key, model, options=None, send_temperature=True, reasoning_effort="max"):
        super().__init__(api_key, model, options, send_temperature)
        if reasoning_effort not in {"low", "high", "max"}:
            raise APIError("Choose low, high or max reasoning for Kimi K3.")
        self.reasoning_effort = reasoning_effort
        self.cancel_event = threading.Event()

    def _client(self):
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(connect=20, read=NVIDIA_RESPONSE_TIMEOUT_SECONDS, write=120, pool=20),
            follow_redirects=False,
        )

    def _send(self, client, method, route, **kwargs):
        if self.cancel_event.is_set():
            raise APIError("Request cancelled.")
        try:
            response = client.request(method, route, **kwargs)
        except httpx.ConnectTimeout:
            raise APIError("Could not establish a connection to NVIDIA within 20 seconds. Check your internet connection, VPN or proxy.", retryable=True) from None
        except httpx.ReadTimeout:
            if method.upper() == "POST":
                # The server may already be generating. Repeating the POST can
                # duplicate paid work; let the user decide whether to try again.
                raise APIError(
                    f"NVIDIA did not return an answer within {NVIDIA_RESPONSE_TIMEOUT_SECONDS // 60} minutes. "
                    "The request may still be running on NVIDIA. No automatic retry was sent and your text was left unchanged. "
                    "Try again later; for page processing, low reasoning may help."
                ) from None
            raise APIError("NVIDIA model discovery timed out. Try again later.", retryable=True) from None
        deadline = time.monotonic() + NVIDIA_RESPONSE_TIMEOUT_SECONDS
        request_id = None
        while response.status_code == 202:
            if request_id is None:
                request_id = response.headers.get("NVCF-REQID") or response.headers.get("NVCF-REQUEST-ID")
                if not request_id:
                    try:
                        request_id = response.json().get("requestId")
                    except (ValueError, AttributeError):
                        pass
                try:
                    request_id = str(uuid.UUID(request_id))
                except (ValueError, TypeError, AttributeError):
                    raise APIError("NVIDIA accepted the request but returned no valid request ID. No result was saved.") from None
            delay = retry_after_seconds(response.headers["Retry-After"]) if "Retry-After" in response.headers else 1.0
            remaining = deadline - time.monotonic()
            if remaining <= 0 or delay >= remaining:
                # Do not resubmit an accepted request: it may still be running.
                raise APIError("NVIDIA's accepted request is still pending. No partial result was saved; try again later.")
            if self.cancel_event.wait(delay):
                raise APIError("Request cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError("NVIDIA's accepted request is still pending. Try again later.")
            try:
                response = client.get(f"status/{request_id}", timeout=remaining)
            except httpx.RequestError:
                raise APIError("Could not retrieve NVIDIA's accepted request. No result was saved; try again later.") from None
            if response.is_error:
                raise APIError(f"Could not retrieve NVIDIA's accepted request (status {response.status_code}). No result was saved; try again later.")
        return response

    def list_models(self):
        data = self._request("GET", "models")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise APIError("NVIDIA returned an invalid model list.")
        names = []
        for item in data["data"]:
            try:
                name = normalize_nvidia_model(item["id"])
            except (KeyError, TypeError, APIError):
                continue
            if not any(term in name.lower() for term in ("embed", "rerank", "guard", "tts", "asr", "whisper")):
                names.append(name)
        names = sorted(set(names))
        if not names:
            raise APIError("No candidate NVIDIA models were returned. You can enter the model ID manually.")
        return names  # Catalog listing does not prove image or JSON support.

    def _generate(self, text, instruction, image_data=None, mime_type="image/png", *, combined=False, test=False, summary=False, continuity=False, response_schema=None, result_parser=None, attachments=()):
        snapshot = getattr(self.options, "previous_page", None) if continuity else None
        instruction += continuity_instruction(snapshot)
        schema = continuity_schema(combined) if snapshot else (COMBINED_RESULT_SCHEMA if combined else RESULT_SCHEMA)
        schema = response_schema or schema
        schema, instruction = memory_contract(self.options, schema, instruction)
        if not combined and not test and not summary and result_parser is None:
            instruction += "\n\n" + CONFIDENCE_INSTRUCTIONS
        # K3's documented endpoint does not expose response_format. Prompt for the
        # same schema, then validate locally; do not invent unsupported parameters.
        instruction += "\n\nReturn only one JSON object matching this schema, without markdown or commentary:\n" + json.dumps(schema)
        content = [{"type": "text", "text": text}]
        images = ([(image_data, mime_type)] if image_data is not None else []) + list(attachments)
        for image_data, mime_type in images:
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime_type};base64,{base64.b64encode(image_data).decode('ascii')}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": content}],
            "max_tokens": 4096 if test else 16384,
            "stream": False,
            "seed": 0,
        }
        if self.send_temperature:
            temperature = 0.0 if test else self.options.temperature
            body["temperature"] = min(temperature, 1.0) if self.model == DEFAULT_NVIDIA_MODEL else temperature
        if self.model == DEFAULT_NVIDIA_MODEL:
            body["reasoning_effort"] = "low" if test else self.reasoning_effort
        response = self._request("POST", "chat/completions", json=body)
        return self._parse_result(response, combined, snapshot, self.options, result_parser)

    @staticmethod
    def _parse_result(response, combined, snapshot=None, options=None, result_parser=None):
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Missing message")
        except (KeyError, IndexError, TypeError, ValueError):
            raise APIError("NVIDIA returned no valid completion.", retryable=True) from None
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise APIError("NVIDIA declined this request. Your text was left unchanged.")
        if choice.get("finish_reason") != "stop":
            raise APIError("NVIDIA did not finish the answer (output limit or interrupted generation). No partial result was saved. For Kimi K3, try lower reasoning effort.")
        raw = message.get("content")
        if not isinstance(raw, str):
            raise APIError("NVIDIA returned no final JSON answer.", retryable=True)
        raw = raw.strip()
        if raw.startswith("```json\n") and raw.endswith("```"):
            raw = raw[8:-3].strip()
        elif raw.startswith("```\n") and raw.endswith("```"):
            raw = raw[4:-3].strip()
        try:
            parser = result_parser or ((lambda data: parse_continuity(data, combined, snapshot)) if snapshot else (CombinedAssessment if combined else Assessment).from_dict)
            return attach_usage(parse_memory(json.loads(raw), parser, options),
                                response.get("usage"), "nvidia")
        except (ValueError, TypeError):
            raise APIError("NVIDIA returned invalid confidence JSON. Your text was left unchanged.", retryable=True) from None
