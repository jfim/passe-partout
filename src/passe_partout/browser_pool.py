"""Single shared Chromium process with lazy start and idle shutdown.

One BrowserPool instance backs the whole app; each tab is a per-call
``create_context`` invocation that yields an isolated incognito-style window
(or a default-profile tab in shared mode).
"""

from __future__ import annotations

import asyncio

import nodriver as uc

from passe_partout.config import Config

# With --load-extension, extensions open their own tabs on first launch and the
# browser's compositor/input pipeline needs a few seconds to warm up. Until it
# does, the first trusted CDP wheel event sent to a fresh tab can wedge: Chrome
# never acks it, MouseWheelEventQueue stalls, and Input.dispatchMouseEvent hangs
# forever. Empirically a ~5s settle after a cold start clears it (0/5 wedges vs
# 5/5 without). One-time cost per cold start; only when extensions are loaded.
_EXTENSION_COLD_START_SETTLE_SECONDS = 5.0


class BrowserPool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._browser: uc.Browser | None = None
        self._active = 0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._browser is not None

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_started_locked()

    async def _ensure_started_locked(self) -> None:
        if self._browser is not None:
            return
        browser_args: list[str] = ["--no-sandbox"]
        if self.cfg.extension_dirs:
            browser_args.append("--load-extension=" + ",".join(self.cfg.extension_dirs))
            browser_args.append("--disable-features=DisableLoadExtensionCommandLineSwitch")
            browser_args.append("--enable-extension-developer-mode")
        start_kwargs: dict = {"browser_args": browser_args, "headless": self.cfg.headless}
        if self.cfg.chrome_path:
            start_kwargs["browser_executable_path"] = self.cfg.chrome_path
        self._browser = await uc.start(**start_kwargs)
        if self.cfg.extension_dirs:
            await asyncio.sleep(_EXTENSION_COLD_START_SETTLE_SECONDS)

    async def stop(self) -> None:
        async with self._lock:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            await self._stop_browser_locked()

    async def stop_if_idle(self) -> tuple[bool, int]:
        """Stop Chromium iff no contexts are active. Returns ``(stopped, active_count)``.

        Both the active-count check and the stop happen under ``self._lock`` so a
        racing ``create_context`` either bumps ``_active`` first (and we refuse to
        stop) or runs after we've torn down (and lazily restarts the browser).
        ``stopped`` is True when we actually shut Chromium down; False either when
        ``_active > 0`` (caller should retry later) or when the browser was already
        down (callers can treat that as success). The two cases are distinguishable
        via ``active_count``.
        """
        async with self._lock:
            if self._active > 0:
                return False, self._active
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            was_running = self._browser is not None
            await self._stop_browser_locked()
            return was_running, 0

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
        # In shared-profile mode, return a regular tab in the default profile so
        # --load-extension extensions are active (Chrome disables them in incognito,
        # and the "Allow in incognito" toggle isn't persisted because nodriver mints
        # a fresh user-data-dir each launch). Tabs share cookies/storage in this mode.
        if self.cfg.shared_profile:
            return await browser.get(url=url, new_tab=True)
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
