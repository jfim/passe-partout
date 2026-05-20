"""Per-tab HTTP response metadata recorder.

Subscribes to CDP Network events to track every request/response Chrome sees.
In NO_COPY mode bodies are not copied (fetched on demand via Network.getResponseBody);
in COPY/COPY_AND_RETAIN modes bodies are pulled eagerly on loadingFinished and
stashed on the record so WARC export and /resources/{id} remain reliable after
Chrome would have evicted them.
"""

from __future__ import annotations

import base64
import time  # noqa: F401  # used by downstream tasks (WARC export)
from dataclasses import dataclass, field

import nodriver as uc

from passe_partout.models import CaptureMode  # noqa: F401  # used by downstream tasks


@dataclass
class RequestRecord:
    """Transient per-request state captured at Network.requestWillBeSent.

    Held in a tab-scoped dict until the matching Network.responseReceived fires,
    at which point the relevant fields are folded into the ResourceRecord. Kept
    around after that too for the WARC writer, which needs the original request
    line and headers.
    """

    request_id: str
    url: str
    method: str
    headers: dict[str, str]
    post_data: bytes | None
    started_at: float


@dataclass
class ResourceRecord:
    """Per-tab metadata for a single Network.responseReceived event.

    In NO_COPY mode `body` is None and the caller fetches it on demand via
    `Network.getResponseBody`; that may fail if Chrome has evicted it. In
    COPY/COPY_AND_RETAIN modes `body` is populated eagerly on loadingFinished
    (decoded from Chrome's base64 wrapping for binary responses) and survives
    until the record itself is pruned.
    """

    request_id: str
    url: str
    status: int
    status_text: str = ""
    mime_type: str = ""
    resource_type: str = ""
    loader_id: str = ""  # main-frame loader at the time of the response
    encoded_size: int = 0  # populated by Network.loadingFinished

    # Always populated when the matching requestWillBeSent was seen.
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
    remote_ip: str = ""
    remote_port: int = 0
    request_post_data: bytes | None = None

    # Populated only in COPY / COPY_AND_RETAIN modes. Already decoded — Chrome's
    # base64 wrapping for binary responses is unwrapped before storage.
    body: bytes | None = None

    # Wall-clock time of Network.responseReceived. Used as WARC-Date.
    captured_at: float = 0.0


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
