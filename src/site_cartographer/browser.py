from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright


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

    async def save_mhtml(self, dest: Path) -> None:
        cdp = await self.page.context.new_cdp_session(self.page)
        snap = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        dest.write_text(snap["data"], encoding="utf-8")


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
