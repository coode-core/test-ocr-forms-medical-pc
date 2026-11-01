"""Network resilience: retry helpers and classification of transient network failures.

Rule of thumb: a request that may already have reached the server is only retried when it
is idempotent. Non-idempotent writes (form submission, tracker append) are retried only when
the failure happened before anything was sent (connect phase) or the server refused it (429).
"""

from __future__ import annotations

import errno
import random
import socket
import ssl
import time
from collections.abc import Callable
from functools import lru_cache
from typing import TypeVar

import gspread

T = TypeVar("T")

GOOGLE_NUM_RETRIES = 4  # googleapiclient: retries SSL errors, timeouts, resets, 429 and 5xx
DEFAULT_ATTEMPTS = 4
BASE_DELAY = 1.0
MAX_DELAY = 16.0
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

_sleep = time.sleep  # patched in tests

_NETWORK_ERRNOS = {
    getattr(errno, name)
    for name in (
        "ECONNREFUSED",
        "ECONNRESET",
        "ECONNABORTED",
        "ETIMEDOUT",
        "ENETUNREACH",
        "ENETDOWN",
        "ENETRESET",
        "EHOSTUNREACH",
        "EHOSTDOWN",
        "EPIPE",
        "EADDRNOTAVAIL",
    )
    if hasattr(errno, name)
}
# Windows socket errors (WSAENETDOWN ... WSATRY_AGAIN).
_NETWORK_WINERRORS = {10050, 10051, 10053, 10054, 10060, 10061, 10064, 10065, 11001, 11002}


def execute(request):
    """Run a googleapiclient request with the shared retry policy."""
    return request.execute(num_retries=GOOGLE_NUM_RETRIES)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with a little jitter; `attempt` starts at 1."""
    delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1))
    return delay + random.uniform(0, delay * 0.25)


def with_retries(
    fn: Callable[[], T],
    *,
    should_retry: Callable[[BaseException], bool],
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], object] | None = None,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not should_retry(exc):
                raise
            (sleep or _sleep)(backoff_delay(attempt))
    raise AssertionError("unreachable")


@lru_cache(maxsize=1)
def _library_network_errors() -> tuple[type[BaseException], ...]:
    classes: list[type[BaseException]] = []
    try:
        import httpx

        classes.append(httpx.TransportError)
    except ImportError:  # pragma: no cover
        pass
    try:
        import requests

        classes += [requests.exceptions.ConnectionError, requests.exceptions.Timeout]
    except ImportError:  # pragma: no cover
        pass
    try:
        import httplib2

        classes.append(httplib2.HttpLib2Error)
    except ImportError:  # pragma: no cover
        pass
    try:
        from google.auth.exceptions import TransportError

        classes.append(TransportError)
    except ImportError:  # pragma: no cover
        pass
    try:
        import openai

        classes.append(openai.APIConnectionError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(classes)


def status_code(exc: BaseException) -> int | None:
    """HTTP status carried by googleapiclient, gspread, httpx or openai errors."""
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "resp", None), "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _is_network_error_single(exc: BaseException) -> bool:
    if isinstance(exc, (ssl.SSLError, TimeoutError, ConnectionError, socket.gaierror, socket.herror)):
        return True
    if isinstance(exc, _library_network_errors()):
        return True
    if isinstance(exc, OSError) and (
        exc.errno in _NETWORK_ERRNOS or getattr(exc, "winerror", None) in _NETWORK_WINERRORS
    ):
        return True
    return status_code(exc) in TRANSIENT_STATUS


def is_network_error(exc: BaseException) -> bool:
    """True for transient network failures, also when wrapped by another exception."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if _is_network_error_single(current):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


# --------------------------------------------------------------------------- gspread


def _never_reached_server(exc: BaseException) -> bool:
    """Failures that guarantee the request was not processed (safe even for appends)."""
    import requests
    from google.auth.exceptions import TransportError as AuthTransportError

    if isinstance(exc, gspread.exceptions.APIError):
        return status_code(exc) == 429
    if isinstance(exc, (requests.exceptions.ConnectTimeout, AuthTransportError)):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError) and not isinstance(exc, requests.exceptions.SSLError):
        from urllib3.exceptions import NewConnectionError

        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        return isinstance(reason, NewConnectionError)
    return False


def _gspread_transient(exc: BaseException) -> bool:
    if isinstance(exc, gspread.exceptions.APIError):
        return status_code(exc) in TRANSIENT_STATUS
    return is_network_error(exc)


class RetryingHTTPClient(gspread.HTTPClient):
    """gspread HTTP client that retries transient failures with backoff.

    Appends (`values:append`) are not idempotent: retrying after the request reached Google
    could duplicate tracker rows, so they are only retried when nothing was sent.
    """

    attempts = 5
    default_timeout = (15, 120)  # (connect, read) seconds; gspread's default is no timeout

    def __init__(self, auth, session=None) -> None:
        super().__init__(auth, session)
        self.timeout = self.default_timeout

    def request(self, method, endpoint, *args, **kwargs):
        should_retry = _never_reached_server if ":append" in endpoint else _gspread_transient
        parent = super().request
        return with_retries(
            lambda: parent(method, endpoint, *args, **kwargs), should_retry=should_retry, attempts=self.attempts
        )
