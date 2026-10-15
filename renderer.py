"""
renderer.py - optional headless-browser rendering for JavaScript-heavy sites.

The static crawler only sees the raw HTML a server returns. Many modern sites render their
contact/team content client-side with JavaScript, so that HTML is nearly empty. This module
drives a headless Chromium via Playwright to get the DOM *after* scripts run.

Playwright is an optional dependency. If it isn't installed, `Renderer` reports itself
unavailable and the crawler simply falls back to static HTML - nothing breaks.

    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import sys

RENDER_TIMEOUT_MS = 15000


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def looks_thin(html: str | None) -> bool:
    """True if the static HTML carries almost no content - a sign it's JS-rendered."""
    if not html:
        return True
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    has_links = bool(soup.find("a", href=True))
    has_mailto = bool(soup.select_one('a[href^="mailto:"]'))
    # thin = very little visible text and no real navigation, unless a mailto is already present
    return not has_mailto and (len(text) < 500 or not has_links)


class Renderer:
    """Holds one headless browser for the lifetime of a crawl. Lazy and fail-safe:
    if Playwright isn't installed or the browser can't launch, `.available` is False and
    `.html()` returns None, so callers fall back to static fetching."""

    def __init__(self, user_agent: str, timeout_ms: int = RENDER_TIMEOUT_MS):
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self.available = False
        self._start()

    def _start(self) -> None:
        if not playwright_available():
            print("  (renderer: playwright not installed; using static HTML only)", file=sys.stderr)
            return
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self.available = True
        except Exception as exc:  # noqa: BLE001 - any launch failure -> stay unavailable
            print(f"  (renderer: could not launch browser: {exc}; using static HTML only)",
                  file=sys.stderr)
            self.close()

    def html(self, url: str) -> str | None:
        """Rendered DOM after scripts run, or None on any failure (caller falls back)."""
        if not self.available:
            return None
        page = None
        try:
            page = self._browser.new_page(user_agent=self.user_agent)
            page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            return page.content()
        except Exception as exc:  # noqa: BLE001 - render failure is non-fatal
            print(f"  (renderer: {url}: {exc.__class__.__name__}; using static HTML)", file=sys.stderr)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                    pass

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
        self._browser = self._pw = None
        self.available = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
