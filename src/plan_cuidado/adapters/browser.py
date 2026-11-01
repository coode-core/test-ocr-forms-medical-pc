"""Browser engine (Playwright, persistent profile): submits with the user's Google session."""

from __future__ import annotations

from pathlib import Path

from plan_cuidado.adapters.forms_http import is_confirmation_page
from plan_cuidado.ports import SubmitResult

_SUBMIT_BUTTON = r"^(Enviar|Submit)$"


class PlaywrightSubmitter:
    def __init__(self, profile_dir: Path, headless: bool = False, timeout_ms: int = 30000):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw = None
        self._context = None

    def _page(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._pw = sync_playwright().start()
            self._context = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
        return self._context.pages[0] if self._context.pages else self._context.new_page()

    def submit(self, form_url: str, payload: list[tuple[str, str]], prefill_url: str) -> SubmitResult:
        import re

        page = self._page()
        page.goto(prefill_url, wait_until="domcontentloaded")
        page.get_by_role("button", name=re.compile(_SUBMIT_BUTTON, re.I)).click(timeout=self.timeout_ms)
        try:
            page.wait_for_url(re.compile(r"formResponse"), timeout=self.timeout_ms)
        except Exception:
            pass
        if is_confirmation_page(page.content()):
            return SubmitResult(True, "ok")
        return SubmitResult(False, "el navegador no llegó a la página de confirmación")

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._pw is not None:
            self._pw.stop()
        self._context = self._pw = None


def interactive_login(profile_dir: Path, url: str = "https://accounts.google.com/") -> None:
    """Open the persistent profile so the user can sign in to Google once."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        input("Inicie sesión en la ventana del navegador y luego presione Enter aquí... ")
        context.close()
