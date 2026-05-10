from __future__ import annotations

import base64
import logging
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright

logger = logging.getLogger(__name__)

# Subresource elements to inline (tag, attribute).
_INLINE_TARGETS = [
    ("img", "src"),
    ("embed", "src"),
    ("audio", "src"),
    ("video", "src"),
    ("source", "src"),
    ("script", "src"),
    ("link", "href"),  # only when rel=stylesheet — checked at runtime
]


@dataclass
class PageHandle:
    page: Page
    response: Response | None = None
    error: str | None = None

    async def html(self) -> str:
        return await self.page.content()

    async def title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def save_thumbnail(self, dest: Path) -> None:
        await self.page.screenshot(path=str(dest), full_page=False, type="png")

    async def save_inline_html(self, dest: Path) -> None:
        """Save the page as a self-contained HTML file with subresources
        inlined as data URIs. Renders in iframes from any origin (unlike
        MHTML, which Chromium refuses to render over HTTP).
        """
        html = await self.page.content()
        page_url = self.page.url
        soup = BeautifulSoup(html, "html.parser")
        request = self.page.context.request

        for tag_name, attr in _INLINE_TARGETS:
            for el in soup.find_all(tag_name):
                if tag_name == "link":
                    rel = el.get("rel") or []
                    if "stylesheet" not in rel:
                        continue
                ref = el.get(attr)
                if not ref or ref.startswith(("data:", "javascript:", "#")):
                    continue
                try:
                    absolute = urljoin(page_url, ref)
                    response = await request.get(absolute, timeout=10000)
                    body = await response.body()
                except Exception as e:
                    logger.debug("inline fetch failed for %s: %s", ref, e)
                    continue
                mime = (
                    response.headers.get("content-type", "").split(";")[0].strip()
                    or mimetypes.guess_type(absolute)[0]
                    or "application/octet-stream"
                )
                if tag_name == "link" and mime.startswith("text"):
                    style = soup.new_tag("style")
                    style.string = body.decode("utf-8", errors="replace")
                    el.replace_with(style)
                else:
                    encoded = base64.b64encode(body).decode("ascii")
                    el[attr] = f"data:{mime};base64,{encoded}"

        # Strip <base> so relative URLs in the inlined doc don't try to fetch
        # from the original origin when viewed locally.
        for base_el in soup.find_all("base"):
            base_el.decompose()

        # Record the original URL so the viewer can show it.
        meta = soup.new_tag("meta")
        meta["name"] = "site-cartographer-source"
        meta["content"] = page_url
        if soup.head is not None:
            soup.head.insert(0, meta)

        dest.write_text(str(soup), encoding="utf-8")


class BrowserSession:
    """Async-context manager around a single Playwright Chromium context."""

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (320, 240),
        user_agent: str = "site-cartographer/0.1",
    ) -> None:
        self.headless = headless
        self.viewport_w, self.viewport_h = viewport
        self.user_agent = user_agent
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport_w, "height": self.viewport_h},
            user_agent=self.user_agent,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    @asynccontextmanager
    async def open_page(self, url: str, *, timeout_ms: int) -> AsyncIterator[PageHandle]:
        assert self._context is not None, "BrowserSession not entered"
        page = await self._context.new_page()
        handle = PageHandle(page=page)
        try:
            try:
                handle.response = await page.goto(
                    url, timeout=timeout_ms, wait_until="domcontentloaded"
                )
            except Exception as e:
                handle.error = str(e)
            if handle.error is None:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass  # networkidle is best-effort
            yield handle
        finally:
            await page.close()
