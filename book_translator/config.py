"""Defaults you can edit without changing the application logic.

These apply to new projects. Saved projects keep their own language, model,
and confidence threshold. API keys are entered in Settings, not stored here.
"""

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LANGUAGE = "Vietnamese"
CONFIDENCE_THRESHOLD = 80  # Flag passages below this percentage.

LANGUAGES = [
    "Vietnamese",
    "English",
    "Chinese (Simplified)",
    "Chinese (Traditional)",
    "Japanese",
    "Korean",
    "French",
    "Spanish",
    "German",
    "Arabic",
    "Portuguese",
]

# Gemini Developer API connection and response limits.
API_BASE_URL = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"
REQUEST_TIMEOUT_MS = 120_000
# NVIDIA reasoning models may queue and think before returning any response.
NVIDIA_RESPONSE_TIMEOUT_SECONDS = 600
MAX_OUTPUT_TOKENS = 32_768
