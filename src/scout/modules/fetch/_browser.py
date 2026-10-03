from __future__ import annotations

import contextlib
import threading
from typing import Any


class Browser:
    """Lazily started headless Chromium, one per thread.

    Playwright's sync API binds to the thread that started it, so each worker thread gets its
    own browser on first render. Most threads never render, so most never start one.
    """

    def __init__(self, user_agent: str, timeout_ms: int, wait_ms: int) -> None:
        self._user_agent = user_agent
        self._timeout_ms = timeout_ms
        self._wait_ms = wait_ms
        self._local = threading.local()

    def render(self, url: str) -> tuple[str, str]:
        """Return (final_url, html) after the page's scripts have had a moment to run."""
        browser = self._browser()
        context = browser.new_context(user_agent=self._user_agent)
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            # A page that never goes network-idle is still worth reading.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=self._wait_ms)
            return page.url, page.content()
        finally:
            context.close()

    def close(self) -> None:
        """Close the calling thread's browser. Other threads' browsers close with their thread."""
        pw, browser = getattr(self._local, "pw", None), getattr(self._local, "browser", None)
        if browser is not None:
            browser.close()
            pw.stop()
            self._local.browser = None
            self._local.pw = None

    def _browser(self) -> Any:
        browser = getattr(self._local, "browser", None)
        if browser is None:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            self._local.pw, self._local.browser = pw, browser
        return browser
