"""Ordered, bounded failover. Runtime credentials and health; secure_settings handles persistence."""
from dataclasses import dataclass, field
import threading
import time

from .api_errors import APIError
from .i18n import tr
from .gemini_service import GeminiService, normalize_model
from .openai_service import OpenAIService, normalize_openai_model, DEFAULT_OPENAI_MODEL
from .nvidia_service import NvidiaService, normalize_nvidia_model, DEFAULT_NVIDIA_MODEL
from .config import DEFAULT_MODEL

PROVIDER_LABELS = {"gemini": "Gemini", "openai": "OpenAI", "nvidia": "NVIDIA NIM"}
DEFAULT_MODELS = {"gemini": DEFAULT_MODEL, "openai": DEFAULT_OPENAI_MODEL, "nvidia": DEFAULT_NVIDIA_MODEL}


@dataclass(frozen=True)
class ApiProfile:
    provider: str
    model: str
    api_key: str = field(repr=False)
    send_temperature: bool = True
    reasoning_effort: str = "max"

    @property
    def label(self):
        return f"{PROVIDER_LABELS.get(self.provider, self.provider)} · {self.model}"

    def normalized(self):
        normalizers = {"gemini": normalize_model, "openai": normalize_openai_model, "nvidia": normalize_nvidia_model}
        if self.provider not in normalizers:
            raise APIError("Choose Gemini, OpenAI or NVIDIA NIM.")
        if self.reasoning_effort not in {"low", "high", "max"}:
            raise APIError("Choose low, high or max reasoning.")
        return ApiProfile(self.provider, normalizers[self.provider](self.model), self.api_key.strip(), self.send_temperature, self.reasoning_effort)


def create_service(profile, options=None):
    if profile.provider == "gemini":
        return GeminiService(profile.api_key, profile.model, options)
    if profile.provider == "nvidia":
        return NvidiaService(profile.api_key, profile.model, options, profile.send_temperature, profile.reasoning_effort)
    return OpenAIService(profile.api_key, profile.model, options, profile.send_temperature)


def redact(text, profiles):
    for key in sorted({p.api_key for p in profiles if p.api_key}, key=len, reverse=True):
        text = text.replace(key, "[redacted]")
    return text


class ApiRouter:
    def __init__(self, profiles, enabled=True, failure_limit=3):
        if not profiles or len(profiles) > 10:
            raise APIError("Configure between 1 and 10 API entries.")
        if not 1 <= failure_limit <= 5:
            raise APIError("Choose a failure limit between 1 and 5.")
        self.profiles = tuple(p.normalized() for p in profiles)
        self.enabled, self.failure_limit = enabled, failure_limit
        self.active = 0
        self.verified = set()
        self.blocked_until = {}  # infinity = disabled until Settings are saved again
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return self.active, self.active in self.verified

    def is_paused(self, index):
        with self.lock:
            return self.blocked_until.get(index, 0) > time.monotonic()

    def call(self, method, args, options, cancel, notify=lambda message: None, factory=create_service):
        """Try each entry at most once, with a bounded retry budget per entry."""
        errors = []
        with self.lock:
            start = self.active if self.enabled else 0
        indices = [(start + offset) % len(self.profiles) for offset in range(len(self.profiles))] if self.enabled else [0]
        for index in indices:
            if cancel.is_set():
                raise APIError("Request cancelled.")
            profile = self.profiles[index]
            with self.lock:
                if self.blocked_until.get(index, 0) > time.monotonic():
                    continue
                self.active = index
            if not profile.api_key:
                continue
            notify(tr("Running · API {api}: {label}", api=index + 1, label=profile.label))
            service = factory(profile, options)
            if hasattr(service, "cancel_event"):
                service.cancel_event = cancel
            for attempt in range(self.failure_limit):
                if cancel.is_set():
                    raise APIError("Request cancelled.")
                try:
                    if attempt:
                        notify(tr("Running · API {api} · attempt {attempt}", api=index + 1, attempt=attempt + 1))
                    result = getattr(service, method)(*args)
                except APIError as exc:
                    if cancel.is_set():
                        raise APIError("Request cancelled.") from None
                    with self.lock:
                        self.verified.discard(index)
                    # Refusals, output limits and invalid input are page failures,
                    # not a reason to spend quota on other credentials.
                    if not exc.rotate_now and not exc.retryable:
                        raise APIError(redact(str(exc), self.profiles)) from None
                    if exc.rotate_now or attempt + 1 == self.failure_limit:
                        errors.append(f"API {index + 1} ({profile.label}): {redact(str(exc), self.profiles)}")
                        with self.lock:
                            self.blocked_until[index] = time.monotonic() + exc.cooldown if exc.cooldown is not None else float("inf")
                        notify(f"API {index + 1} unavailable; checking the next entry…" if self.enabled else f"API {index + 1} unavailable.")
                        break
                    delay = min(2 ** attempt, 8)
                    notify(tr("Retrying API {api} · {attempt}/{limit} in {delay}s…", api=index + 1, attempt=attempt + 2, limit=self.failure_limit, delay=delay))
                    # The next attempt is announced only once the backoff ends.
                    if cancel.wait(delay):
                        raise APIError("Request cancelled.") from None
                except Exception as exc:
                    # Unexpected programming/validation errors must not fan out.
                    raise APIError(redact(str(exc), self.profiles)) from None
                else:
                    if cancel.is_set():
                        raise APIError("Request cancelled.")
                    with self.lock:
                        self.active = index
                        self.verified.add(index)
                        self.blocked_until.pop(index, None)
                    return result
        detail = "\n\n".join(errors)
        raise APIError("No API entry is available. Rate-limited entries wait at least 60 seconds; exhausted or repeatedly failing entries stay paused until you save Settings again."
                       + ("\n\n" + detail if detail else ""))


class RotatingService:
    """A per-request snapshot; cancelled workers cannot start another attempt."""
    def __init__(self, router, options, cancel, notify=lambda message: None):
        self.router, self.options, self.cancel, self.notify = router, options, cancel, notify

    def _call(self, method, *args):
        return self.router.call(method, args, self.options, self.cancel, self.notify)

    def extract(self, *args):
        return self._call("extract", *args)

    def translate(self, *args):
        return self._call("translate", *args)

    def extract_and_translate(self, *args):
        return self._call("extract_and_translate", *args)
