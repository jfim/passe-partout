from __future__ import annotations

import nodriver as uc

from passe_partout.config import Config


class BrowserPool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._browser: uc.Browser | None = None

    async def start(self) -> None:
        browser_args: list[str] = ["--no-sandbox"]
        if self.cfg.extension_dirs:
            browser_args.append(
                "--load-extension=" + ",".join(self.cfg.extension_dirs)
            )
        self._browser = await uc.start(browser_args=browser_args, headless=True)

    async def stop(self) -> None:
        if self._browser is not None:
            self._browser.stop()
            self._browser = None

    async def create_context(self, url: str) -> uc.Tab:
        if self._browser is None:
            raise RuntimeError("BrowserPool not started")
        return await self._browser.create_context(url=url, new_window=True)

    async def close_context(self, tab: uc.Tab) -> None:
        await tab.close()
