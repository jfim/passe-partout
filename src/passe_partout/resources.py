"""Per-tab HTTP response metadata recorder.

Subscribes to CDP Network events to track every request/response Chrome sees.
In NO_COPY mode bodies are not copied (fetched on demand via Network.getResponseBody);
in COPY/COPY_AND_RETAIN modes bodies are pulled eagerly on loadingFinished and
stashed on the record so WARC export and /resources/{id} remain reliable after
Chrome would have evicted them.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field

import nodriver as uc

from passe_partout.models import CaptureMode


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

    See module docstring for the capture-mode contract.
    """

    def __init__(self) -> None:
        self._registry = None  # injected by app.py
        # tab_id -> current main-frame loader_id (str). Empty until first frameNavigated.
        self._current_loader: dict[int, str] = {}
        # tab_id -> CaptureMode chosen at attach_tab time.
        self._mode: dict[int, CaptureMode] = {}

    def set_registry(self, registry) -> None:
        self._registry = registry

    def mode_for(self, tab_id: int) -> CaptureMode:
        return self._mode.get(tab_id, CaptureMode.NO_COPY)

    def current_loader(self, tab_id: int) -> str:
        return self._current_loader.get(tab_id, "")

    async def attach_tab(
        self,
        tab_id: int,
        tab: uc.Tab,
        capture_mode: CaptureMode = CaptureMode.NO_COPY,
    ) -> None:
        self._mode[tab_id] = capture_mode
        await tab.send(uc.cdp.network.enable())

        # request_id -> pending RequestRecord captured at requestWillBeSent; popped
        # when the matching responseReceived fires and folded into the ResourceRecord.
        pending: dict[str, RequestRecord] = {}

        def _on_request(evt) -> None:
            req = evt.request
            method = str(getattr(req, "method", "GET") or "GET")
            headers = dict(getattr(req, "headers", {}) or {})
            post_data: bytes | None = None
            raw_post = getattr(req, "post_data", None)
            if raw_post is not None:
                if isinstance(raw_post, bytes):
                    post_data = raw_post
                else:
                    post_data = str(raw_post).encode("utf-8")
            pending[str(evt.request_id)] = RequestRecord(
                request_id=str(evt.request_id),
                url=str(getattr(req, "url", "") or ""),
                method=method,
                headers=headers,
                post_data=post_data,
                started_at=time.time(),
            )

        def _on_response(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            resp = evt.response
            req_meta = pending.pop(str(evt.request_id), None)
            record = ResourceRecord(
                request_id=str(evt.request_id),
                url=resp.url,
                status=int(resp.status),
                status_text=str(getattr(resp, "status_text", "") or ""),
                mime_type=resp.mime_type or "",
                resource_type=str(evt.type_),
                loader_id=str(evt.loader_id) if evt.loader_id is not None else "",
                response_headers=dict(getattr(resp, "headers", {}) or {}),
                protocol=str(getattr(resp, "protocol", "") or ""),
                remote_ip=str(getattr(resp, "remote_ip_address", "") or ""),
                remote_port=int(getattr(resp, "remote_port", 0) or 0),
                captured_at=time.time(),
            )
            if req_meta is not None:
                record.method = req_meta.method
                record.request_headers = req_meta.headers
                record.request_post_data = req_meta.post_data
            rec.resources[str(evt.request_id)] = record

        async def _capture_body(request_id_str: str) -> None:
            rec_inner = self._registry.get(tab_id) if self._registry else None
            if rec_inner is None:
                return
            record = rec_inner.resources.get(request_id_str)
            if record is None or record.body is not None:
                return
            try:
                body, _ = await self.get_body(tab, request_id_str)
            except Exception:
                # Opaque cross-origin, too large, already evicted, etc. Leave body=None;
                # WARC writer will emit headers with WARC-Truncated: unspecified.
                return
            # Re-check; the record may have been pruned while we awaited.
            record = rec_inner.resources.get(request_id_str)
            if record is not None:
                record.body = body

        def _on_finished(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            r = rec.resources.get(str(evt.request_id))
            if r is not None:
                r.encoded_size = int(evt.encoded_data_length)
            if self._mode.get(tab_id, CaptureMode.NO_COPY) in (
                CaptureMode.COPY,
                CaptureMode.COPY_AND_RETAIN,
            ):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(_capture_body(str(evt.request_id)))

        def _on_frame_navigated(evt) -> None:
            if evt.frame.parent_id is not None:
                return
            new_loader = str(evt.frame.loader_id) if evt.frame.loader_id is not None else ""
            self._current_loader[tab_id] = new_loader
            if self._mode.get(tab_id, CaptureMode.NO_COPY) == CaptureMode.COPY_AND_RETAIN:
                return  # retention mode: keep all loaders' entries forever
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            stale = [
                rid for rid, r in rec.resources.items() if r.loader_id and r.loader_id != new_loader
            ]
            for rid in stale:
                del rec.resources[rid]

        tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
        tab.add_handler(uc.cdp.network.ResponseReceived, _on_response)
        tab.add_handler(uc.cdp.network.LoadingFinished, _on_finished)
        tab.add_handler(uc.cdp.page.FrameNavigated, _on_frame_navigated)

    async def get_body(self, tab: uc.Tab, request_id: str) -> tuple[bytes, bool]:
        """Returns (body_bytes, was_base64).

        Always goes to Chrome — for buffered-body preference use ``get_body_for``.
        """
        body, base64_encoded = await tab.send(
            uc.cdp.network.get_response_body(request_id=uc.cdp.network.RequestId(request_id))
        )
        if base64_encoded:
            return base64.b64decode(body), True
        return body.encode("utf-8"), False

    async def get_body_for(self, record: ResourceRecord, tab: uc.Tab) -> tuple[bytes, bool]:
        """Returns (body_bytes, was_base64), preferring the buffered copy on the record."""
        if record.body is not None:
            return record.body, False
        return await self.get_body(tab, record.request_id)

    def detach_tab(self, tab_id: int) -> None:
        self._current_loader.pop(tab_id, None)
        self._mode.pop(tab_id, None)
