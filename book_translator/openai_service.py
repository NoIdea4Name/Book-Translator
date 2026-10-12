"""OpenAI Responses API adapter with the same assessments as Gemini."""
import base64
from io import BytesIO
import json
import re

import httpx
from PIL import Image

from .api_errors import APIError, http_failure
from .token_usage import attach_usage
from .memory import memory_contract, parse_memory
from .continuity import continuity_schema, continuity_instruction, parse_continuity
from .confidence import Assessment, CombinedAssessment, RESULT_SCHEMA, COMBINED_RESULT_SCHEMA, check_text_anchors
from .config import MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT_MS
from .preferences import GenerationOptions
from .prompts import CONFIDENCE_INSTRUCTIONS, EXTRACTION_PROMPT, translation_instructions, combined_instructions

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


def normalize_openai_model(model):
    model = model.strip().strip("\"'")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", model):
        raise APIError("Enter an OpenAI model ID, for example gpt-4.1-mini. Use Load models to check your account.")
    return model


class OpenAIService:
    provider_name = "OpenAI"
    base_url = "https://api.openai.com/v1/"
    normalize_model = staticmethod(normalize_openai_model)

    def __init__(self, api_key, model, options=None, send_temperature=True):
        self.api_key = api_key.strip()
        if not self.api_key:
            raise APIError(f"Add your {self.provider_name} API key in Settings first.")
        self.model = self.normalize_model(model)
        self.options = options or GenerationOptions()
        self.send_temperature = send_temperature

    def _client(self):
        return httpx.Client(base_url=self.base_url,
                            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                            timeout=REQUEST_TIMEOUT_MS / 1000, follow_redirects=False)

    def _send(self, client, method, route, **kwargs):
        return client.request(method, route, **kwargs)

    def _request(self, method, route, **kwargs):
        try:
            with self._client() as client:
                response = self._send(client, method, route, **kwargs)
            if response.is_error:
                try:
                    payload = response.json()
                    error = payload.get("error") or {"message": payload.get("detail", "Request failed")}
                    message, code = str(error.get("message", "Request failed")), str(error.get("code", ""))
                except (ValueError, AttributeError):
                    message, code = "The server could not complete the request.", ""
                message = message.replace(self.api_key, "[redacted]")[:1200]
                hint = "\nIf this model rejects temperature, turn off Send temperature for this entry." if "temperature" in message.lower() else ""
                raise APIError(f"{self.provider_name} API status: {response.status_code}\nModel: {self.model}\n{message}{hint}",
                               **http_failure(response.status_code, code, response.headers.get("Retry-After")))
            if response.is_redirect:
                raise APIError(f"{self.provider_name} returned an unexpected redirect. Check your network/proxy.")
            try:
                return response.json()
            except ValueError:
                raise APIError(f"{self.provider_name} returned invalid JSON.", retryable=True) from None
        except httpx.TimeoutException:
            raise APIError(f"{self.provider_name} request timed out. Your text was left unchanged.", retryable=True) from None
        except httpx.RequestError:
            raise APIError(f"Could not connect to {self.provider_name}. Check your internet connection or proxy.", retryable=True) from None

    def list_models(self):
        data = self._request("GET", "models")
        names = sorted({item["id"] for item in data.get("data", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                        and item["id"].startswith(("gpt-", "chatgpt-", "o1", "o3", "o4"))
                        and not any(term in item["id"] for term in ("audio", "realtime", "transcribe", "tts", "image", "instruct", "search"))})
        if not names:
            raise APIError("No candidate OpenAI text models were listed for this key.")
        return names  # Discovery alone cannot confirm image/JSON support; run the test.

    def _generate(self, text, instruction, image_data=None, mime_type="image/png", *, combined=False, test=False, summary=False, continuity=False, response_schema=None, result_parser=None, attachments=()):
        snapshot = getattr(self.options, "previous_page", None) if continuity else None
        instruction += continuity_instruction(snapshot)
        schema = continuity_schema(combined) if snapshot else (COMBINED_RESULT_SCHEMA if combined else RESULT_SCHEMA)
        schema = response_schema or schema
        schema, instruction = memory_contract(self.options, schema, instruction)
        content = [{"type": "input_text", "text": text}]
        images = ([(image_data, mime_type)] if image_data is not None else []) + list(attachments)
        for image_data, mime_type in images:
            content.append({"type": "input_image", "detail": "high",
                            "image_url": f"data:{mime_type};base64,{base64.b64encode(image_data).decode('ascii')}"})
        body = {
            "model": self.model, "store": False,
            "instructions": instruction + ("\n\n" + CONFIDENCE_INSTRUCTIONS if not combined and not test and not summary and result_parser is None else ""),
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 2048 if test else min(MAX_OUTPUT_TOKENS, 16_384),
            "text": {"format": {"type": "json_schema", "name": "page_assessment", "strict": True,
                                 "schema": schema}},
        }
        if self.send_temperature:
            body["temperature"] = 0.0 if test else self.options.temperature
        response = self._request("POST", "responses", json=body)
        if not isinstance(response, dict):
            raise APIError("OpenAI returned an invalid response.", retryable=True)
        if response.get("status") == "failed":
            error = response.get("error") or {}
            code = str(error.get("code", ""))
            message = str(error.get("message", "OpenAI could not complete the request.")).replace(self.api_key, "[redacted]")[:1200]
            policy = http_failure(429, code) if code in {"rate_limit_exceeded", "insufficient_quota"} else {"retryable": code == "server_error"}
            raise APIError(message, **policy)
        if response.get("status") != "completed":
            reason = (response.get("incomplete_details") or {}).get("reason", response.get("status", "unknown"))
            raise APIError(f"OpenAI did not complete the request ({reason}). No partial result was saved.")
        pieces = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise APIError("OpenAI declined this request. Your text was left unchanged.")
                if part.get("type") == "output_text":
                    pieces.append(part.get("text", ""))
        try:
            parser = result_parser or ((lambda data: parse_continuity(data, combined, snapshot)) if snapshot else (CombinedAssessment if combined else Assessment).from_dict)
            return attach_usage(parse_memory(json.loads("".join(pieces)), parser, self.options),
                                response.get("usage"), "openai")
        except (ValueError, TypeError):
            raise APIError("OpenAI returned invalid confidence data. Your text was left unchanged.", retryable=True) from None

    def chat(self, context, history, prompt, attachments):
        from .chat import CHAT_SCHEMA, CHAT_INSTRUCTIONS, ChatReply
        text = json.dumps({'context': context, 'conversation': history,
                           'request': prompt, 'attached_images': len(attachments)}, ensure_ascii=False)
        instruction = CHAT_INSTRUCTIONS
        if self.options.common.strip():
            instruction += '\nUser style preferences: ' + self.options.common.strip()
        return self._generate(text, instruction, response_schema=CHAT_SCHEMA,
                              result_parser=ChatReply.from_dict, attachments=attachments)

    def test_connection(self):
        image = BytesIO()
        Image.new("RGB", (64, 64), "white").save(image, "PNG")
        result = self._generate('Return {"text":"CONNECTION_OK","confidence":1,"regions":[]}.',
                                "This is a connection test with a blank image. Follow the test JSON exactly.",
                                image.getvalue(), test=True)
        if result.text.strip() != "CONNECTION_OK":
            raise APIError(f"{self.provider_name} responded, but the image/JSON test did not return the expected result.")
        return f"Connection verified: {self.model}. Image input and structured JSON output succeeded."

    def extract(self, image_data, mime_type):
        result = self._generate("Transcribe all text on this single page.",
                                EXTRACTION_PROMPT + self.options.instructions("extraction"), image_data, mime_type)
        if not result.text.strip():
            raise APIError("No readable text was found. Your source text was left unchanged.")
        check_text_anchors(result, result.text)
        return result

    def translate(self, source, target_language, image_data=None, mime_type="image/png"):
        has_source = bool(source.strip())
        if not has_source and image_data is None:
            raise APIError("Provide source text or a page image to translate.")
        target = self._target(target_language)
        result = self._generate(("Authoritative source text to translate:\n" + source if has_source
                 else "Translate directly from this page image. Return the translation only, not a transcription."),
                                translation_instructions(target, has_image=image_data is not None, has_source=has_source) + self.options.instructions("translation"),
                                image_data, mime_type, continuity=True)
        if not result.text.strip():
            raise APIError(f"{self.provider_name} returned an empty translation. Your text was left unchanged.")
        check_text_anchors(result, source if has_source else None)
        return result

    def extract_and_translate(self, image_data, mime_type, target_language):
        result = self._generate("Extract and translate this single page.",
                                combined_instructions(self._target(target_language)) + self.options.instructions("both"),
                                image_data, mime_type, combined=True, continuity=True)
        check_text_anchors(result.extraction, result.extraction.text)
        check_text_anchors(result.translation, result.extraction.text)
        return result

    @staticmethod
    def _target(value):
        value = value.strip()
        if not value or len(value) > 80:
            raise APIError("Enter a target language (up to 80 characters).")
        return value

    def summarize(self, chunk, previous, target):
        from .book_context import SUMMARY_INSTRUCTIONS, summary_prompt, validate_summary
        result = self._generate(
            summary_prompt(chunk, previous, target, self.options),
            SUMMARY_INSTRUCTIONS, summary=True,
        )
        return validate_summary(result)
