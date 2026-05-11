"""Per-tab HTTP response metadata recorder.

Subscribes to CDP Network events to track every response Chrome sees; bodies are
not copied and are fetched on demand via Network.getResponseBody.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import nodriver as uc


@dataclass
class ResourceRecord:
    """Per-tab metadata for a single Network.responseReceived event.

    Bodies are NOT stored in this record; they live in Chrome's network process
    (enabled via Network.enable) and are pulled on demand via getResponseBody.
    Chrome evicts these bodies when the document navigates or when its internal
    buffer caps are hit, so callers should be ready for retrieval to fail.
    """

    request_id: str
    url: str
    status: int
    mime_type: str
    resource_type: str
    loader_id: str  # main-frame loader at the time of the response
    encoded_size: int = 0  # populated by Network.loadingFinished


class ResourceRecorder:
    """Tracks Chrome Network responses per tab and fetches bodies on demand.

    Subscribes to Network.responseReceived (metadata) and Network.loadingFinished
    (final byte count) on each attached tab. On main-frame navigation we record the
    new loader_id and prune metadata tied to older loaders, mirroring Chrome's own
    body eviction so the dict stays bounded over a long-lived tab.
    """

    def __init__(self) -> None:
        self._registry = None  # injected by app.py
        # tab_id -> current main-frame loader_id (str). Empty until first frameNavigated.
        self._current_loader: dict[int, str] = {}

    def set_registry(self, registry) -> None:
        self._registry = registry

    async def attach_tab(self, tab_id: int, tab: uc.Tab) -> None:
        await tab.send(uc.cdp.network.enable())

        def _on_response(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            rec.resources[str(evt.request_id)] = ResourceRecord(
                request_id=str(evt.request_id),
                url=evt.response.url,
                status=int(evt.response.status),
                mime_type=evt.response.mime_type or "",
                resource_type=str(evt.type_),
                loader_id=str(evt.loader_id) if evt.loader_id is not None else "",
            )

        def _on_finished(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            r = rec.resources.get(str(evt.request_id))
            if r is not None:
                r.encoded_size = int(evt.encoded_data_length)

        def _on_frame_navigated(evt) -> None:
            # Only react to top-level navigations. Subframe navigations don't evict
            # main-frame bodies, so leaving their entries alone is correct.
            if evt.frame.parent_id is not None:
                return
            new_loader = str(evt.frame.loader_id) if evt.frame.loader_id is not None else ""
            self._current_loader[tab_id] = new_loader
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            # Prune entries from older loaders; keep the new loader's entries (including
            # the document responseReceived that fired just before this event) and any
            # worker-served responses (which carry an empty loader_id).
            stale = [
                rid for rid, r in rec.resources.items() if r.loader_id and r.loader_id != new_loader
            ]
            for rid in stale:
                del rec.resources[rid]

        tab.add_handler(uc.cdp.network.ResponseReceived, _on_response)
        tab.add_handler(uc.cdp.network.LoadingFinished, _on_finished)
        tab.add_handler(uc.cdp.page.FrameNavigated, _on_frame_navigated)

    async def get_body(self, tab: uc.Tab, request_id: str) -> tuple[bytes, bool]:
        """Returns (body_bytes, was_base64).

        Raises whatever CDP raises if Chrome no longer has the body (navigation,
        buffer eviction, opaque cross-origin response, etc.).
        """
        body, base64_encoded = await tab.send(
            uc.cdp.network.get_response_body(request_id=uc.cdp.network.RequestId(request_id))
        )
        if base64_encoded:
            return base64.b64decode(body), True
        return body.encode("utf-8"), False

    def detach_tab(self, tab_id: int) -> None:
        self._current_loader.pop(tab_id, None)
