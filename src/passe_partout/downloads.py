from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import nodriver as uc

DownloadState = Literal["in_progress", "completed", "canceled"]


@dataclass
class DownloadRecord:
    id: str  # CDP guid
    url: str
    filename: str
    path: Path
    started_at: float
    state: DownloadState = "in_progress"
    bytes_received: int = 0
    size_bytes: int = -1
    completed_at: float | None = None
    content_type: str | None = None  # captured from origin via Fetch.requestPaused


class DownloadCoordinator:
    """Owns per-tab download directories and CDP download event plumbing.

    Each tab gets a directory under <root_dir>/passe-partout/tab-<tab_id>/
    where Chromium writes downloaded files (named by their CDP guid).
    """

    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir) / "passe-partout"
        # guid -> tab_id, populated in downloadWillBegin handler so progress events can be routed.
        self._tab_lookup: dict[str, int] = {}
        self._registry = None  # injected by app.py via set_registry()
        self._pending_content_type: dict[int, str] = {}  # tab_id -> last seen origin Content-Type

    def set_registry(self, registry) -> None:
        self._registry = registry

    def tab_dir(self, tab_id: int) -> Path:
        return self.root / f"tab-{tab_id}"

    def ensure_tab_dir(self, tab_id: int) -> Path:
        d = self.tab_dir(tab_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup_tab_dir(self, tab_id: int) -> None:
        d = self.tab_dir(tab_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    async def attach_tab(self, tab_id: int, tab: uc.Tab) -> None:
        """Configure Chromium to route downloads for this tab to its dir.

        Must be called before any navigation so behavior is in place when
        the first response arrives. Also subscribes to Browser.downloadWillBegin
        and Browser.downloadProgress to populate TabRecord.downloads.
        """
        download_path = str(self.ensure_tab_dir(tab_id).resolve())

        def _on_will_begin(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            dl = DownloadRecord(
                id=evt.guid,
                url=evt.url,
                filename=evt.suggested_filename,
                path=self.tab_dir(tab_id) / evt.guid,
                started_at=time.time(),
                content_type=self._pending_content_type.pop(tab_id, None),
            )
            rec.downloads[evt.guid] = dl
            self._tab_lookup[evt.guid] = tab_id

        def _on_progress(evt) -> None:
            tid = self._tab_lookup.get(evt.guid)
            if tid is None or self._registry is None:
                return
            rec = self._registry.get(tid)
            if rec is None:
                return
            dl = rec.downloads.get(evt.guid)
            if dl is None:
                return
            dl.bytes_received = int(evt.received_bytes)
            total = int(evt.total_bytes)
            dl.size_bytes = -1 if total == 0 else total
            state = str(evt.state)  # "inProgress" | "completed" | "canceled"
            if state == "inProgress":
                dl.state = "in_progress"
            elif state == "completed":
                dl.state = "completed"
                dl.completed_at = time.time()
            elif state == "canceled":
                dl.state = "canceled"
                dl.completed_at = time.time()
            rec.last_used_at = time.time()

        tab.add_handler(uc.cdp.browser.DownloadWillBegin, _on_will_begin)
        tab.add_handler(uc.cdp.browser.DownloadProgress, _on_progress)

        async def _on_request_paused(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            main_frame_id = rec.main_frame_id if rec else None
            if main_frame_id is not None and evt.frame_id != main_frame_id:
                await tab.send(uc.cdp.fetch.continue_response(request_id=evt.request_id))
                return

            headers = list(evt.response_headers or [])
            ctype_raw = ""
            for h in headers:
                if h.name.lower() == "content-type":
                    ctype_raw = h.value or ""
                    break
            ctype_lower = ctype_raw.lower()
            is_html = ctype_lower.startswith("text/html") or ctype_lower.startswith(
                "application/xhtml+xml"
            )
            if is_html:
                await tab.send(uc.cdp.fetch.continue_response(request_id=evt.request_id))
                return

            # Non-HTML: note the content-type for serving later, then inject CD: attachment.
            self._pending_content_type[tab_id] = ctype_raw  # raw, not lowercased
            # When modifying headers, response_code and response_phrase must also be provided.
            status_code = evt.response_status_code or 200
            status_text = evt.response_status_text or "OK"
            headers = [h for h in headers if h.name.lower() != "content-disposition"]
            headers.append(uc.cdp.fetch.HeaderEntry(name="Content-Disposition", value="attachment"))
            await tab.send(
                uc.cdp.fetch.continue_response(
                    request_id=evt.request_id,
                    response_code=status_code,
                    response_phrase=status_text,
                    response_headers=headers,
                )
            )

        tab.add_handler(uc.cdp.fetch.RequestPaused, _on_request_paused)
        await tab.send(
            uc.cdp.fetch.enable(
                patterns=[
                    uc.cdp.fetch.RequestPattern(
                        url_pattern="*",
                        resource_type=uc.cdp.network.ResourceType.DOCUMENT,
                        request_stage=uc.cdp.fetch.RequestStage.RESPONSE,
                    )
                ]
            )
        )

        # Capture main frame id and subscribe to frameNavigated so we can update it on navigation.
        tree = await tab.send(uc.cdp.page.get_frame_tree())
        rec = self._registry.get(tab_id) if self._registry else None
        if rec is not None:
            rec.main_frame_id = tree.frame.id_

        def _on_frame_navigated(evt) -> None:
            if evt.frame.parent_id is None:
                rec2 = self._registry.get(tab_id) if self._registry else None
                if rec2 is not None:
                    rec2.main_frame_id = evt.frame.id_

        tab.add_handler(uc.cdp.page.FrameNavigated, _on_frame_navigated)
        await tab.send(uc.cdp.page.enable())

        # Pass browser_context_id so behavior applies to incognito/isolated contexts.
        browser_context_id = tab.target.browser_context_id if tab.target else None
        await tab.send(
            uc.cdp.browser.set_download_behavior(
                behavior="allowAndName",
                browser_context_id=browser_context_id,
                download_path=download_path,
                events_enabled=True,
            )
        )

    async def cancel(self, tab: uc.Tab, guid: str) -> None:
        browser_context_id = tab.target.browser_context_id if tab.target else None
        await tab.send(
            uc.cdp.browser.cancel_download(guid=guid, browser_context_id=browser_context_id)
        )

    async def detach_tab(self, tab_id: int) -> None:
        self.cleanup_tab_dir(tab_id)
        stale_guids = [guid for guid, tid in self._tab_lookup.items() if tid == tab_id]
        for guid in stale_guids:
            del self._tab_lookup[guid]
        self._pending_content_type.pop(tab_id, None)
