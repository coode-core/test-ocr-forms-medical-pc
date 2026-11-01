"""Anonymous HTTP engine: GET the public form and POST to /formResponse."""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from plan_cuidado.adapters.net import TRANSIENT_STATUS, with_retries
from plan_cuidado.ports import SubmitResult

USER_AGENT = "Mozilla/5.0 (plan-cuidado)"
_CONFIRMATION_MARKERS = (
    "freebirdFormviewerViewResponseConfirmationMessage",
    "Se registró tu respuesta",
    "Tu respuesta ha sido registrada",
    "Your response has been recorded",
)


CONNECT_RETRIES = 3  # httpx retries only failed connection attempts: safe for every method
# Raised before any byte of the request is sent, so a POST can be retried without double-submitting.
_CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


def new_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        transport=httpx.HTTPTransport(retries=CONNECT_RETRIES),
    )


def _transient_get_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    return isinstance(exc, httpx.TransportError)


def get_with_retries(client: httpx.Client, url: str, sleep=None, **kwargs) -> httpx.Response:
    """GET with backoff on any transport error or transient status (GET is idempotent)."""

    def attempt() -> httpx.Response:
        response = client.get(url, **kwargs)
        if response.status_code in TRANSIENT_STATUS:
            response.raise_for_status()
        return response

    return with_retries(attempt, should_retry=_transient_get_error, sleep=sleep)


def fetch_form_html(url: str, client: httpx.Client) -> tuple[str, str]:
    response = get_with_retries(client, url)
    response.raise_for_status()
    return response.text, str(response.url)


def form_requires_login(final_url: str, html: str) -> bool:
    return "accounts.google.com" in final_url or ("FB_PUBLIC_LOAD_DATA_" not in html and "ServiceLogin" in html)


def is_confirmation_page(html: str) -> bool:
    return any(marker in html for marker in _CONFIRMATION_MARKERS)


class HttpFormSubmitter:
    def __init__(self, client: httpx.Client):
        self.client = client

    def submit(self, form_url: str, payload: list[tuple[str, str]], prefill_url: str) -> SubmitResult:
        try:
            # urlencode keeps repeated keys (checkbox answers); httpx `data=` would not.
            response = with_retries(
                lambda: self.client.post(
                    form_url,
                    content=urlencode(payload).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ),
                # Never retry once the request may have been sent: it could submit the patient twice.
                should_retry=lambda exc: isinstance(exc, _CONNECT_PHASE_ERRORS),
            )
        except httpx.HTTPError as exc:
            return SubmitResult(False, f"error de red: {exc}")
        if response.status_code != 200:
            return SubmitResult(False, f"HTTP {response.status_code}")
        if not is_confirmation_page(response.text):
            return SubmitResult(False, "el formulario no mostró la página de confirmación")
        return SubmitResult(True, "ok")
