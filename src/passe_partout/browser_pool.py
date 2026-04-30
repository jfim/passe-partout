from __future__ import annotations

import asyncio

import nodriver as uc

from passe_partout.config import Config


class BrowserPool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._browser: uc.Browser | None = None
        self._active = 0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_started_locked()

    async def _ensure_started_locked(self) -> None:
        if self._browser is not None:
            return
        browser_args: list[str] = ["--no-sandbox"]
        if self.cfg.extension_dirs:
            browser_args.append(
                "--load-extension=" + ",".join(self.cfg.extension_dirs)
            )
        self._browser = await uc.start(browser_args=browser_args, headless=self.cfg.headless)

    async def stop(self) -> None:
        async with self._lock:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            await self._stop_browser_locked()

    async def _stop_browser_locked(self) -> None:
        if self._browser is not None:
            self._browser.stop()
            self._browser = None

    async def create_context(self, url: str) -> uc.Tab:
        async with self._lock:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            await self._ensure_started_locked()
            self._active += 1
            browser = self._browser
        assert browser is not None
        return await browser.create_context(url=url, new_window=True)

    async def close_context(self, tab: uc.Tab) -> None:
        try:
            await tab.close()
        finally:
            async with self._lock:
                self._active = max(0, self._active - 1)
                if self._active == 0 and self.cfg.idle_chrome_shutdown_seconds > 0:
                    if self._idle_task is not None:
                        self._idle_task.cancel()
                    self._idle_task = asyncio.create_task(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(self.cfg.idle_chrome_shutdown_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._active == 0:
                await self._stop_browser_locked()
            self._idle_task = None
