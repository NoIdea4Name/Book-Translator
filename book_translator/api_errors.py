"""Provider-independent failure metadata used by the rotation controller."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math


class APIError(RuntimeError):
    def __init__(self, message, *, retryable=False, rotate_now=False, cooldown=None):
        super().__init__(message)
        self.retryable = retryable
        self.rotate_now = rotate_now
        # None disables this profile until the user saves Settings again.
        self.cooldown = cooldown


def retry_after_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 60.0
    return max(1.0, seconds) if math.isfinite(seconds) else 60.0


def http_failure(status, code="", retry_after=None):
    if status == 429:
        exhausted = code in {"insufficient_quota", "billing_hard_limit_reached"}
        return dict(rotate_now=True, cooldown=None if exhausted else max(60, retry_after_seconds(retry_after)))
    if status in (400, 401, 403, 404, 422):
        return dict(rotate_now=True)
    return dict(retryable=status in (408, 409) or status >= 500)
