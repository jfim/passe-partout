from __future__ import annotations

import asyncio

import nodriver as uc


class NavCapture:
    def __init__(self, tab: uc.Tab) -> None:
        self.tab = tab
        self.status: int | None = None
        self.mime_type: str | None = None
        self.url: str | None = None
        self._ready = asyncio.Event()
        self._attached = False

    async def attach(self) -> None:
        if self._attached:
            return
        self.tab.add_handler(uc.cdp.network.ResponseReceived, self._on_response)
        await self.tab.send(uc.cdp.network.enable())
        self._attached = True

    def reset(self) -> None:
        self.status = None
        self.mime_type = None
        self.url = None
        self._ready.clear()

    def _on_response(self, evt) -> None:
        # Only the main document response — subresources (scripts, images, xhr) are ignored.
        # CDP collapses redirects: only the final response gets a ResponseReceived event;
        # intermediate 3xx responses ride along as redirect_response on RequestWillBeSent.
        if evt.type_ != uc.cdp.network.ResourceType.DOCUMENT:
            return
        r = evt.response
        self.status = r.status
        self.mime_type = r.mime_type
        self.url = r.url
        self._ready.set()

    async def wait(self, timeout: float = 2.0) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            pass
