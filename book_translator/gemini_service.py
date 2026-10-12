"""The only module that sends document content to Google."""

from __future__ import annotations

import json
import re
from io import BytesIO

from PIL import Image

from google import genai
from google.genai import errors, types
import httpx

from .token_usage import attach_usage
from .memory import memory_contract, parse_memory
from .continuity import continuity_schema, continuity_instruction, parse_continuity
from .confidence import (
    Assessment, CombinedAssessment, RESULT_SCHEMA, COMBINED_RESULT_SCHEMA, check_text_anchors,
)
from .config import API_BASE_URL, API_VERSION, REQUEST_TIMEOUT_MS, MAX_OUTPUT_TOKENS
from .prompts import CONFIDENCE_INSTRUCTIONS, EXTRACTION_PROMPT, translation_instructions, combined_instructions


from .preferences import GenerationOptions
from .api_errors import APIError, http_failure


class GeminiError(APIError):
    pass


def normalize_model(model: str) -> str:
    name = model.strip().strip("\"'").removeprefix("models/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise GeminiError(
            "Use a model ID such as gemini-2.5-flash, not a display name or URL. Open Settings → Load models to choose an available ID."
        )
    return name


def response_text(response) -> str:
    candidates = response.candidates or []
    if not candidates:
        raise GeminiError(
            "Gemini returned no result. The request may have been blocked. Your text was left unchanged."
        )
    reason = candidates[0].finish_reason
    reason = getattr(reason, "value", reason)
    if reason and reason != "STOP":
        if reason == "MAX_TOKENS":
            raise GeminiError(
                "Gemini reached its output limit. No partial result was saved. Try a shorter section of text."
            )
        raise GeminiError(
            f"Gemini did not complete the request ({reason}). Your text was left unchanged."
        )
    text = (response.text or "").strip()
    if not text:
        raise GeminiError("Gemini returned an empty answer. Your text was left unchanged.")
    return text


class GeminiService:
    def __init__(self, api_key: str, model: str, options: GenerationOptions | None = None):
        if not api_key.strip():
            raise GeminiError("Add your Gemini API key in Settings first.")
        if not model.strip():
            raise GeminiError("Enter a Gemini model name in Settings.")
        self.options = options or GenerationOptions()
        self.api_key = api_key.strip()
        self.model = normalize_model(model)

    def _client(self):
        return genai.Client(
            api_key=self.api_key,
            vertexai=False,
            http_options=types.HttpOptions(
                base_url=API_BASE_URL,
                api_version=API_VERSION,
                timeout=REQUEST_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def _api_error(self, exc):
        hints = {
            400: "Google rejected the request. Check the details below for an invalid key or unsupported request option.",
            401: "Google did not accept the API key.",
            403: "Access was denied. Check the key restrictions, project permissions, and service availability.",
            404: "The requested model/resource is unavailable for this API request. Open Settings → Load models, select a model, then Test connection.",
            429: "The Gemini quota or rate limit was reached. Check AI Studio quota/billing before trying again.",
        }
        detail = str(getattr(exc, "message", "") or exc).replace(self.api_key, "[redacted]")
        detail = re.sub(r"(?i)(key=)[^\s&]+", r"\1[redacted]", detail)[:1200]
        return (
            f"{hints.get(exc.code, 'Google could not complete the request. Try again later.')}\n\n"
            f"Model: {self.model}\nAPI: Gemini Developer API ({API_VERSION})\n"
            f"Google API status: {exc.code}\nDetails: {detail}"
        )

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
                names = set()
                for model in client.models.list():
                    name = (model.name or "").removeprefix("models/")
                    if (
                        name.startswith("gemini-")
                        and "generateContent" in (model.supported_actions or [])
                        and not any(
                            term in name
                            for term in ("embedding", "image", "tts", "audio", "live", "robotics")
                        )
                    ):
                        names.add(name)
                if not names:
                    raise GeminiError(
                        "The API returned no candidate Gemini text-generation models. Check the key/project in AI Studio."
                    )
                # Listing is discovery, not proof of quota, image, or JSON support.
                return sorted(names)
        except errors.APIError as exc:
            raise GeminiError(self._api_error(exc), **http_failure(exc.code, retry_after=getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After"))) from None
        except httpx.TimeoutException:
            raise GeminiError(
                "Loading models timed out. Check your connection and try again."
            ) from None
        except httpx.RequestError:
            raise GeminiError(
                "Could not reach the Gemini API. Check your internet connection or proxy."
            ) from None

    def test_connection(self) -> str:
        image = BytesIO()
        Image.new("RGB", (64, 64), "white").save(image, "PNG")
        result = self._generate(
            [
                types.Part.from_bytes(data=image.getvalue(), mime_type="image/png"),
                'Connection test. Return {"text":"CONNECTION_OK","confidence":1,"regions":[]}.',
            ],
            "This is a connection test with a blank sample image, not a book page. Follow the requested test JSON exactly.",
            0.0,
            max_tokens=2048,
            review_confidence=False,
        )
        if result.text.strip() != "CONNECTION_OK":
            raise GeminiError(
                "The API responded, but the image/JSON test did not return the expected result. Try another model."
            )
        return (
            f"Connection verified: {self.model}. Image input and structured JSON output succeeded."
        )

    def _generate(
        self,
        contents,
        instruction: str,
        temperature: float,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        review_confidence: bool = True,
        combined: bool = False,
        response_schema=None,
        result_parser=None,
        continuity=False,
    ) -> Assessment | CombinedAssessment:
        snapshot = getattr(self.options, "previous_page", None) if continuity else None
        instruction += continuity_instruction(snapshot)
        schema = response_schema or (continuity_schema(combined) if snapshot else (COMBINED_RESULT_SCHEMA if combined else RESULT_SCHEMA))
        schema, instruction = memory_contract(self.options, schema, instruction)
        try:
            with self._client() as client:
                response = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=instruction
                        + ("\n\n" + CONFIDENCE_INSTRUCTIONS if review_confidence else ""),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        response_mime_type="application/json",
                        response_json_schema=schema,
                    ),
                )
                raw = response_text(response)
                try:
                    parser = result_parser or (
                        (lambda data: parse_continuity(data, combined, snapshot)) if snapshot
                        else (CombinedAssessment if combined else Assessment).from_dict)
                    return attach_usage(parse_memory(json.loads(raw), parser, self.options),
                                        getattr(response, "usage_metadata", None), "gemini")
                except (ValueError, TypeError) as exc:
                    raise GeminiError(
                        f"Gemini returned invalid {'chat' if result_parser else 'confidence'} data: {exc}\nYour text was left unchanged. Try the request again.", retryable=True
                    ) from None
        except errors.APIError as exc:
            raise GeminiError(self._api_error(exc), **http_failure(exc.code, retry_after=getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After"))) from None
        except httpx.TimeoutException:
            raise GeminiError(
                "The request timed out. Your text was left unchanged; try again.", retryable=True
            ) from None
        except httpx.RequestError:
            raise GeminiError(
                "Could not connect to Gemini. Check your internet connection and try again.", retryable=True
            ) from None

    def extract(self, image_data: bytes, mime_type: str) -> Assessment:
        instruction = EXTRACTION_PROMPT + self.options.instructions("extraction")
        result = self._generate(
            [
                types.Part.from_bytes(data=image_data, mime_type=mime_type),
                "Transcribe all text on this single page.",
            ],
            instruction,
            self.options.temperature,
        )
        if not result.text.strip():
            raise GeminiError(
                "No readable text was found on this page. Your source text was left unchanged."
            )
        check_text_anchors(result, result.text)
        return result

    def translate(
        self,
        source: str,
        target_language: str,
        image_data: bytes | None = None,
        mime_type: str = "image/png",
    ) -> Assessment:
        has_source = bool(source.strip())
        if not has_source and image_data is None:
            raise GeminiError("Provide source text or a page image to translate.")
        target_language = target_language.strip()
        if not target_language or len(target_language) > 80:
            raise GeminiError("Enter a target language (up to 80 characters).")
        instruction = translation_instructions(target_language, has_image=image_data is not None, has_source=has_source)
        if image_data is None:
            contents = source
        else:
            contents = [
                types.Part.from_bytes(data=image_data, mime_type=mime_type),
                ("Authoritative source text to translate:\n" + source if has_source
                 else "Translate directly from this page image. Return the translation only, not a transcription."),
            ]
        instruction += self.options.instructions("translation")
        result = self._generate(contents, instruction, self.options.temperature, continuity=True)
        if not result.text.strip():
            raise GeminiError("Gemini returned an empty translation. Your text was left unchanged.")
        check_text_anchors(result, source if has_source else None)
        return result


    def extract_and_translate(
        self, image_data: bytes, mime_type: str, target_language: str
    ) -> CombinedAssessment:
        """One image, one Gemini request, two independently assessed results."""
        target_language = target_language.strip()
        if not target_language or len(target_language) > 80:
            raise GeminiError("Enter a target language (up to 80 characters).")
        result = self._generate(
            [
                types.Part.from_bytes(data=image_data, mime_type=mime_type),
                "Extract and translate this single page.",
            ],
            combined_instructions(target_language) + self.options.instructions("both"),
            self.options.temperature,
            review_confidence=False,
            combined=True,
            continuity=True,
        )
        check_text_anchors(result.extraction, result.extraction.text)
        check_text_anchors(result.translation, result.extraction.text)
        return result

    def chat(self, context, history, prompt, attachments):
        """Selected-text assistant; images are explicit, per-message attachments."""
        from .chat import CHAT_SCHEMA, CHAT_INSTRUCTIONS, ChatReply

        contents = [types.Part.from_bytes(data=data, mime_type=mime)
                    for data, mime in attachments]
        contents.append(json.dumps({
            "context": context, "conversation": history, "request": prompt,
            "attached_images": len(attachments),
        }, ensure_ascii=False))
        instruction = CHAT_INSTRUCTIONS
        if self.options.common.strip():
            instruction += "\nUser style preferences: " + self.options.common.strip()
        return self._generate(
            contents, instruction, self.options.temperature,
            review_confidence=False, response_schema=CHAT_SCHEMA,
            result_parser=ChatReply.from_dict,
        )

    def summarize(self, chunk, previous, target):
        from .book_context import SUMMARY_INSTRUCTIONS, summary_prompt, validate_summary
        result = self._generate(
            summary_prompt(chunk, previous, target, self.options),
            SUMMARY_INSTRUCTIONS, self.options.temperature, review_confidence=False,
        )
        return validate_summary(result)
