"""FastAPI app factory and REST route definitions.

Wires together BrowserPool, TabRegistry, DownloadCoordinator, and ResourceRecorder,
and runs the idle-tab sweeper as a background task.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import socket
from contextlib import asynccontextmanager

import nodriver as uc
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from passe_partout.behaviors import BehaviorCatalog, perturb_steps, replay_wheel
from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.domsnapshot import DOM_SNAPSHOT_PROFILE, capture_dom_snapshot
from passe_partout.downloads import DownloadCoordinator
from passe_partout.isolated import evaluate_isolated, main_frame_id
from passe_partout.models import (
    BehaviorInfo,
    BrowserInfo,
    BrowserShutdownResponse,
    CaptureMode,
    ClickRequest,
    CreateTabRequest,
    CreateTabResponse,
    DownloadInfo,
    DownloadStatus,
    EvalRequest,
    EvalResponse,
    FetchRequest,
    FetchResponse,
    GotoRequest,
    GotoResponse,
    HealthResponse,
    PerturbParams,
    PlayBehaviorRequest,
    ResourceSummary,
    TabState,
    TabSummary,
    TypeRequest,
    WaitRequest,
)
from passe_partout.nav_capture import NavCapture
from passe_partout.rendered import RENDERED_TARGETS_PROFILE, capture_rendered_payload
from passe_partout.resources import ResourceRecorder
from passe_partout.tab_registry import TabRegistry
from passe_partout.warc import build_warc


async def _wait_for_first_download(rec, baseline: set[str] | None = None):
    """Briefly poll for a download that appeared after ``baseline`` (~0.5s).

    Returns the first new ``DownloadRecord`` found, or ``None`` if none arrived.
    Used by both ``create_tab`` and ``goto`` to attach an optional ``download``
    field to the response without forcing the caller to poll.
    """
    base: set[str] = baseline if baseline is not None else set()
    for _ in range(20):
        diff = set(rec.downloads.keys()) - base
        if diff:
            return rec.downloads[next(iter(diff))]
        await asyncio.sleep(0.025)
    return None


async def _sweep_once(app: FastAPI) -> None:
    registry = app.state.registry
    pool = app.state.pool
    coord = app.state.coord
    recorder = app.state.recorder
    for tid in registry.idle_ids():
        rec = registry.remove(tid)
        if rec is not None:
            try:
                await pool.close_context(rec.tab)
            finally:
                await coord.detach_tab(tid)
                recorder.detach_tab(tid)


async def _sweeper_loop(app: FastAPI) -> None:
    interval = app.state.cfg.sweeper_interval_seconds
    while True:
        try:
            await _sweep_once(app)
        except Exception:
            pass
        await asyncio.sleep(interval)


def build_app(cfg: Config, browser_pool: BrowserPool | None = None) -> FastAPI:
    state_pool = browser_pool

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal state_pool
        owns_pool = state_pool is None
        if owns_pool:
            state_pool = BrowserPool(cfg)
            if cfg.idle_chrome_shutdown_seconds == 0:
                await state_pool.start()
        app.state.cfg = cfg
        app.state.pool = state_pool
        app.state.registry = TabRegistry()
        app.state.coord = DownloadCoordinator(
            root_dir=cfg.download_dir, shared_profile=cfg.shared_profile
        )
        app.state.coord.set_registry(app.state.registry)
        app.state.recorder = ResourceRecorder()
        app.state.recorder.set_registry(app.state.registry)
        app.state.behaviors = BehaviorCatalog.load(cfg.behavior_trace_dir)
        app.state.sweep_once = lambda: _sweep_once(app)

        sweeper_task = asyncio.create_task(_sweeper_loop(app))
        try:
            yield
        finally:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except asyncio.CancelledError:
                pass
            if owns_pool and state_pool is not None:
                await state_pool.stop()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        token = cfg.auth_token
        if token and request.url.path != "/healthz":
            header = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(header, expected):
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "detail": "invalid or missing token"},
                )
        return await call_next(request)

    @app.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
    async def healthz():
        return HealthResponse(ok=True)

    @app.get("/browser", response_model=BrowserInfo, summary="Report Chromium status and config")
    async def get_browser():
        pool = app.state.pool
        registry = app.state.registry
        cfg_now = app.state.cfg
        return BrowserInfo(
            running=pool is not None and pool.is_running,
            tab_count=registry.count(),
            headless=cfg_now.headless,
            shared_profile=cfg_now.shared_profile,
            extension_dirs=list(cfg_now.extension_dirs),
            chrome_path=cfg_now.chrome_path,
        )

    @app.delete(
        "/browser",
        response_model=BrowserShutdownResponse,
        summary="Force-stop Chromium when no tabs are open",
    )
    async def delete_browser():
        # Force-close Chromium iff no tabs are open. Useful in shared-profile mode
        # where there's no per-tab isolation, so a fresh browser/profile is the only
        # way to discard accumulated cookies and state. Returns 200 if Chromium got
        # stopped (or was already down), 409 if any tab is still tracked. The
        # active-count check and the stop happen under the same lock in
        # BrowserPool.stop_if_idle, so a racing POST /tabs either lands first (and we
        # refuse) or runs after teardown and lazily restarts.
        pool = app.state.pool
        registry = app.state.registry
        async with registry.mu:
            tabs_open = registry.count()
        if tabs_open > 0:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "tabs_open",
                    "detail": f"{tabs_open} tab(s) still open; close them first",
                },
            )
        stopped, active = await pool.stop_if_idle()
        if active > 0:
            # Lost the race: a tab creation slipped in after our registry check but
            # before stop_if_idle acquired the pool lock.
            return JSONResponse(
                status_code=409,
                content={
                    "error": "tabs_open",
                    "detail": f"{active} pending context(s); retry",
                },
            )
        return BrowserShutdownResponse(ok=True, stopped=stopped)

    @app.get("/tabs", response_model=list[TabSummary], summary="List active tabs")
    async def list_tabs():
        registry = app.state.registry
        return [
            TabSummary(
                id=rec.id,
                url=getattr(rec.tab, "url", "") or "",
                created_at=rec.created_at,
                last_used_at=rec.last_used_at,
            )
            for rec in registry.all()
        ]

    @app.get(
        "/behaviors",
        response_model=list[BehaviorInfo],
        summary="List available scroll/input behaviors",
    )
    async def list_behaviors():
        return [
            BehaviorInfo(name=b.name, kind=b.kind, source=b.source)
            for b in app.state.behaviors.list()
        ]

    def _download_to_status(dl) -> DownloadStatus:
        return DownloadStatus(
            id=dl.id,
            url=dl.url,
            filename=dl.filename,
            state=dl.state,
            bytes_received=dl.bytes_received,
            size_bytes=dl.size_bytes if dl.size_bytes >= 0 else None,
            started_at=dl.started_at,
            completed_at=dl.completed_at,
        )

    def _cookies_to_cdp(cookies, url: str | None = None):
        out = []
        for c in cookies or []:
            out.append(
                uc.cdp.network.CookieParam(
                    name=c.name,
                    value=c.value,
                    url=url if not c.domain else None,
                    domain=c.domain or None,
                    path=c.path or None,
                    expires=c.expires,
                    http_only=c.http_only,
                    secure=c.secure,
                )
            )
        return out

    @app.post(
        "/tabs",
        response_model=CreateTabResponse,
        summary="Open a new tab and navigate to the given URL",
    )
    async def create_tab(req: CreateTabRequest):
        cfg_now = app.state.cfg
        registry = app.state.registry
        pool = app.state.pool
        coord = app.state.coord

        async with registry.mu:
            if registry.count() >= cfg_now.max_tabs:
                return JSONResponse(
                    status_code=429,
                    content={"error": "max_tabs", "detail": f"cap of {cfg_now.max_tabs} reached"},
                )

        tab = None
        rec = None
        try:
            tab = await pool.create_context("about:blank")
            ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_tab_close_seconds
            rec = registry.register(tab=tab, ttl_seconds=ttl, capture_mode=req.capture_mode)
            await coord.attach_tab(rec.id, tab)
            await app.state.recorder.attach_tab(rec.id, tab, req.capture_mode)
            nav = NavCapture(tab)
            await nav.attach()
            rec.nav = nav
            if req.cookies:
                cdp_cookies = _cookies_to_cdp(req.cookies, url=req.url)
                await tab.send(uc.cdp.network.set_cookies(cdp_cookies))
            nav.reset()
            await tab.get(req.url)
            await nav.wait()
            if nav.status is None and req.url.lower().startswith(("http://", "https://")):
                raise RuntimeError(f"no response captured for {req.url}")
        except Exception as e:
            if rec is not None:
                registry.remove(rec.id)
                await coord.detach_tab(rec.id)
                app.state.recorder.detach_tab(rec.id)
            if tab is not None:
                try:
                    await pool.close_context(tab)
                except Exception:
                    pass
            return JSONResponse(
                status_code=502,
                content={"error": "browser_error", "detail": str(e)},
            )

        final_url = tab.url or req.url
        download_info = None
        dl_first = await _wait_for_first_download(rec)
        if dl_first is not None:
            download_info = DownloadInfo(
                id=dl_first.id, filename=dl_first.filename, size_bytes=dl_first.size_bytes
            )
            final_url = dl_first.url  # spec requires the origin URL, not about:blank

        return CreateTabResponse(
            id=rec.id,
            status=nav.status if nav.status is not None else 200,
            final_url=final_url,
            content_type=nav.mime_type,
            download=download_info,
        )

    @app.delete("/tabs/{tab_id}", status_code=204, summary="Close a tab")
    async def delete_tab(tab_id: int):
        registry = app.state.registry
        pool = app.state.pool
        coord = app.state.coord
        rec = registry.remove(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        try:
            await pool.close_context(rec.tab)
        finally:
            await coord.detach_tab(tab_id)
            app.state.recorder.detach_tab(tab_id)
        return Response(status_code=204)

    @app.get(
        "/tabs/{tab_id}", response_model=TabState, summary="Get a tab's URL, title, ready state"
    )
    async def get_tab(tab_id: int):
        registry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        registry.touch(tab_id)
        try:
            fid = await main_frame_id(rec.tab)
            meta = await evaluate_isolated(
                rec.tab, fid, "JSON.stringify([document.title, document.readyState])"
            )
        except Exception as e:
            return JSONResponse(
                status_code=502, content={"error": "browser_error", "detail": str(e)}
            )
        title, ready = json.loads(meta) if meta else ["", ""]
        return TabState(url=rec.tab.url or "", title=title or "", ready_state=ready or "")

    async def _require_tab(tab_id: int):
        registry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return None
        registry.touch(tab_id)
        return rec

    @app.get("/tabs/{tab_id}/html", summary="Current document HTML")
    async def get_html(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            html = await rec.tab.get_content()
        return HTMLResponse(content=html)

    @app.get("/tabs/{tab_id}/cookies", summary="Cookies visible to the tab")
    async def get_cookies(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            raw = await rec.tab.send(uc.cdp.network.get_cookies())
        out = []
        for c in raw:
            out.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "expires": c.expires,
                    "httpOnly": c.http_only,
                    "secure": c.secure,
                    "sameSite": c.same_site.to_json() if c.same_site else None,
                }
            )
        return out

    @app.get("/tabs/{tab_id}/screenshot", summary="PNG screenshot of the viewport")
    async def get_screenshot(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            b64 = await rec.tab.send(uc.cdp.page.capture_screenshot(format_="png"))
        return Response(content=base64.b64decode(b64), media_type="image/png")

    @app.get(
        "/tabs/{tab_id}/resources",
        response_model=list[ResourceSummary],
        summary="List captured network responses",
    )
    async def list_resources(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        return [
            ResourceSummary(
                request_id=r.request_id,
                url=r.url,
                status=r.status,
                mime_type=r.mime_type,
                resource_type=r.resource_type,
                encoded_size=r.encoded_size,
            )
            for r in rec.resources.values()
        ]

    @app.get("/tabs/{tab_id}/resources/{request_id}", summary="Fetch a captured response body")
    async def get_resource_body(tab_id: int, request_id: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        meta = rec.resources.get(request_id)
        if meta is None:
            return JSONResponse(
                status_code=404,
                content={"error": "resource_not_found", "detail": request_id},
            )
        async with rec.lock:
            try:
                body, _ = await app.state.recorder.get_body_for(meta, rec.tab)
            except Exception as e:
                # Body may have been evicted by Chrome (navigation, buffer cap, opaque
                # cross-origin response, etc.). Surface as 410 Gone.
                return JSONResponse(
                    status_code=410,
                    content={"error": "body_unavailable", "detail": str(e)},
                )
        return Response(content=body, media_type=meta.mime_type or "application/octet-stream")

    @app.get("/tabs/{tab_id}/warc", summary="WARC archive of the current page's resources")
    async def get_warc(
        tab_id: int,
        rendered: bool = False,
        domsnapshot: bool = False,
        computed_styles: str = "",
        dom_rects: bool = False,
        paint_order: bool = False,
    ):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        recorder = app.state.recorder
        current_loader = recorder.current_loader(tab_id)
        async with rec.lock:
            # Only live-fetch bodies for records that don't already have one
            # buffered. In NO_COPY mode we keep results in a local dict so
            # `r.body` stays None and the /resources/{id} endpoint continues
            # to do live fetches (preserving NO_COPY semantics). In COPY /
            # COPY_AND_RETAIN modes the records already carry bodies — no
            # live fetch is needed in the route at all.
            mode = recorder.mode_for(tab_id)
            body_overrides: dict[str, bytes] | None = None
            if mode == CaptureMode.NO_COPY:
                body_overrides = {}
                for r in list(rec.resources.values()):
                    if r.body is not None:
                        continue
                    if r.loader_id and r.loader_id != current_loader:
                        continue
                    try:
                        body, _ = await recorder.get_body(rec.tab, r.request_id)
                        body_overrides[r.request_id] = body
                    except Exception:
                        pass
            rendered_payload: dict | None = None
            dom_snapshot_payload: dict | None = None
            main_doc_request_id: str | None = None
            styles_list = [s.strip() for s in computed_styles.split(",") if s.strip()]
            if rendered or domsnapshot:
                # Identify the current main-frame document response so the
                # conversion records can WARC-Refers-To it. Match by loader +
                # resource_type — there should be exactly one per loader.
                for r in rec.resources.values():
                    if r.loader_id != current_loader:
                        continue
                    rt = (r.resource_type or "").lower()
                    if "document" in rt:
                        main_doc_request_id = r.request_id
                        break
            if rendered and main_doc_request_id is not None:
                try:
                    page_title = getattr(rec.tab, "title", "") or ""
                except Exception:
                    page_title = ""
                rendered_payload = await capture_rendered_payload(rec.tab, page_title=page_title)
            if domsnapshot and main_doc_request_id is not None:
                dom_snapshot_payload = await capture_dom_snapshot(
                    rec.tab,
                    styles_list,
                    include_dom_rects=dom_rects,
                    include_paint_order=paint_order,
                )
            blob = build_warc(
                rec,
                current_loader,
                socket.gethostname(),
                body_overrides=body_overrides,
                include_all_loaders=(mode == CaptureMode.COPY_AND_RETAIN),
                rendered_payload=rendered_payload,
                main_doc_request_id=main_doc_request_id,
                rendered_profile=RENDERED_TARGETS_PROFILE,
                dom_snapshot_payload=dom_snapshot_payload,
                dom_snapshot_profile=DOM_SNAPSHOT_PROFILE,
                computed_styles=styles_list,
            )
        filename = f"tab-{tab_id}-{current_loader or 'noloader'}.warc"
        return Response(
            content=blob,
            media_type="application/warc",
            headers={"content-disposition": f'attachment; filename="{filename}"'},
        )

    @app.get(
        "/tabs/{tab_id}/downloads",
        response_model=list[DownloadStatus],
        summary="List downloads on the tab",
    )
    async def list_downloads(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        return [_download_to_status(dl) for dl in rec.downloads.values()]

    @app.get(
        "/tabs/{tab_id}/downloads/{did}/status",
        response_model=DownloadStatus,
        summary="Single download status",
    )
    async def download_status(tab_id: int, did: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        dl = rec.downloads.get(did)
        if dl is None:
            return JSONResponse(
                status_code=404,
                content={"error": "download_not_found", "detail": f"no download {did}"},
            )
        return _download_to_status(dl)

    @app.get("/tabs/{tab_id}/downloads/{did}", summary="Retrieve the downloaded bytes")
    async def download_bytes(tab_id: int, did: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        dl = rec.downloads.get(did)
        if dl is None:
            return JSONResponse(
                status_code=404,
                content={"error": "download_not_found", "detail": f"no download {did}"},
            )
        if dl.state == "in_progress":
            return JSONResponse(
                status_code=425,
                content={"error": "download_in_progress", "detail": "still downloading"},
                headers={"Retry-After": "1"},
            )
        if dl.state == "canceled":
            return JSONResponse(
                status_code=410,
                content={"error": "download_canceled", "detail": "download was canceled"},
            )
        return FileResponse(
            path=str(dl.path),
            filename=dl.filename,
            media_type=dl.content_type or "application/octet-stream",
        )

    @app.post(
        "/tabs/{tab_id}/downloads/{did}/cancel",
        status_code=204,
        summary="Cancel an in-progress download",
    )
    async def cancel_download(tab_id: int, did: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        dl = rec.downloads.get(did)
        if dl is None:
            return JSONResponse(
                status_code=404,
                content={"error": "download_not_found", "detail": f"no download {did}"},
            )
        if dl.state != "in_progress":
            return JSONResponse(
                status_code=409,
                content={"error": "download_terminal", "detail": f"state is {dl.state}"},
            )
        coord = app.state.coord
        async with rec.lock:
            await coord.cancel(rec.tab, did)
        return Response(status_code=204)

    @app.delete(
        "/tabs/{tab_id}/downloads/{did}",
        status_code=204,
        summary="Delete a download record and its file",
    )
    async def delete_download(tab_id: int, did: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        coord = app.state.coord
        async with rec.lock:
            dl = rec.downloads.pop(did, None)
            if dl is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "download_not_found", "detail": f"no download {did}"},
                )
            if dl.state == "in_progress":
                try:
                    await coord.cancel(rec.tab, did)
                except Exception:
                    pass

        def _unlink_if_exists() -> None:
            try:
                if dl.path.exists():
                    dl.path.unlink()
            except OSError:
                pass

        await asyncio.to_thread(_unlink_if_exists)
        return Response(status_code=204)

    @app.post("/tabs/{tab_id}/goto", response_model=GotoResponse, summary="Navigate the tab")
    async def goto(tab_id: int, req: GotoRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        pre_existing = set(rec.downloads.keys())
        async with rec.lock:
            try:
                if rec.nav is not None:
                    rec.nav.reset()
                await rec.tab.get(req.url)
                if rec.nav is not None:
                    await rec.nav.wait()
                if (
                    rec.nav is not None
                    and rec.nav.status is None
                    and req.url.lower().startswith(("http://", "https://"))
                ):
                    raise RuntimeError(f"no response captured for {req.url}")
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        status = rec.nav.status if rec.nav and rec.nav.status is not None else 200
        ctype = rec.nav.mime_type if rec.nav else None

        new_dl = await _wait_for_first_download(rec, baseline=pre_existing)
        final_url = new_dl.url if new_dl is not None else (rec.tab.url or req.url)
        download_info = (
            DownloadInfo(id=new_dl.id, filename=new_dl.filename, size_bytes=new_dl.size_bytes)
            if new_dl is not None
            else None
        )
        return GotoResponse(
            status=status,
            final_url=final_url,
            content_type=ctype,
            download=download_info,
        )

    @app.post("/tabs/{tab_id}/click", status_code=204, summary="Click an element by selector")
    async def click(tab_id: int, req: ClickRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                el = await rec.tab.select(req.selector)
                await el.click()
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return Response(status_code=204)

    @app.post("/tabs/{tab_id}/type", status_code=204, summary="Type into an element by selector")
    async def type_(tab_id: int, req: TypeRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                el = await rec.tab.select(req.selector)
                await el.send_keys(req.text)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return Response(status_code=204)

    @app.post(
        "/tabs/{tab_id}/eval", response_model=EvalResponse, summary="Evaluate JavaScript in the tab"
    )
    async def eval_js(tab_id: int, req: EvalRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                if req.world == "isolated":
                    fid = await main_frame_id(rec.tab)
                    result = await evaluate_isolated(rec.tab, fid, req.js)
                else:
                    result = await rec.tab.evaluate(req.js)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return EvalResponse(result=result)

    @app.post(
        "/tabs/{tab_id}/behaviors/play",
        status_code=204,
        summary="Replay a behavior (e.g. scroll burst) against the tab",
    )
    async def play_behavior(tab_id: int, req: PlayBehaviorRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        behavior = app.state.behaviors.get(req.name)
        if behavior is None:
            return JSONResponse(
                status_code=404,
                content={"error": "behavior_not_found", "detail": req.name},
            )
        p = req.perturb or PerturbParams()
        steps = perturb_steps(
            behavior.steps,
            enabled=p.enabled,
            time_warp=p.time_warp if p.time_warp is not None else 0.15,
            delta_scale=p.delta_scale if p.delta_scale is not None else 0.10,
            seed=p.seed,
        )
        async with rec.lock:
            try:
                await replay_wheel(rec.tab, steps)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
            # Bump TTL: the idle sweeper closes tabs purely on last_used_at without
            # taking rec.lock, so a long client-driven scroll session must keep the
            # tab alive across bursts.
            app.state.registry.touch(tab_id)
        return Response(status_code=204)

    @app.post(
        "/tabs/{tab_id}/wait",
        status_code=204,
        summary="Wait for a selector and/or network idle",
    )
    async def wait(tab_id: int, req: WaitRequest):
        if not req.selector and not req.network_idle:
            return JSONResponse(
                status_code=400,
                content={"error": "bad_request", "detail": "provide selector and/or network_idle"},
            )
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})

        timeout_s = (req.timeout_ms or 5000) / 1000.0

        async def _wait_selector():
            await rec.tab.wait_for(selector=req.selector, timeout=timeout_s)

        async def _wait_network_idle():
            inflight = 0
            last_zero_at = asyncio.get_event_loop().time()

            def _on_request(_e):
                nonlocal inflight
                inflight += 1

            def _on_done(_e):
                nonlocal inflight, last_zero_at
                inflight = max(0, inflight - 1)
                if inflight == 0:
                    last_zero_at = asyncio.get_event_loop().time()

            rec.tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
            rec.tab.add_handler(uc.cdp.network.LoadingFinished, _on_done)
            rec.tab.add_handler(uc.cdp.network.LoadingFailed, _on_done)
            await rec.tab.send(uc.cdp.network.enable())

            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                now = asyncio.get_event_loop().time()
                if inflight == 0 and (now - last_zero_at) >= 0.5:
                    return
                await asyncio.sleep(0.05)
            raise TimeoutError()

        async with rec.lock:
            try:
                tasks = []
                if req.selector:
                    tasks.append(_wait_selector())
                if req.network_idle:
                    tasks.append(_wait_network_idle())
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout_s)
            except TimeoutError:
                return JSONResponse(
                    status_code=408, content={"error": "timeout", "detail": "wait timed out"}
                )
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return Response(status_code=204)

    @app.post(
        "/fetch",
        response_model=FetchResponse,
        summary="One-shot: open tab, fetch HTML, close tab",
    )
    async def fetch(req: FetchRequest):
        create_req = CreateTabRequest(url=req.url, cookies=req.cookies, ttl_seconds=req.ttl_seconds)
        created = await create_tab(create_req)
        # If create_tab returned a JSONResponse (error like 429 or 502), surface it directly
        if isinstance(created, JSONResponse):
            return created
        tid = created.id
        registry = app.state.registry
        pool = app.state.pool
        coord = app.state.coord
        rec = registry.get(tid)
        try:
            async with rec.lock:
                html = await rec.tab.get_content()
            return FetchResponse(
                status=created.status,
                final_url=rec.tab.url or req.url,
                html=html,
                content_type=created.content_type,
            )
        finally:
            registry.remove(tid)
            try:
                await pool.close_context(rec.tab)
            except Exception:
                pass
            await coord.detach_tab(tid)
            app.state.recorder.detach_tab(tid)

    return app
