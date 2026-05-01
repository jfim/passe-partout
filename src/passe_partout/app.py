from __future__ import annotations

import asyncio as _asyncio
import base64
from contextlib import asynccontextmanager

import nodriver as uc
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.downloads import DownloadCoordinator
from passe_partout.models import (
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
    TabState,
    TabSummary,
    TypeRequest,
    WaitRequest,
)
from passe_partout.nav_capture import NavCapture
from passe_partout.tab_registry import TabRegistry


def build_app(cfg: Config, browser_pool: BrowserPool | None = None) -> FastAPI:
    state_pool = browser_pool

    async def sweep_once():
        registry = app.state.registry
        pool = app.state.pool
        coord = app.state.coord
        for tid in registry.idle_ids():
            rec = registry.remove(tid)
            if rec is not None:
                try:
                    await pool.close_context(rec.tab)
                finally:
                    await coord.detach_tab(tid)

    async def sweeper_loop():
        import asyncio as _aio

        while True:
            try:
                await sweep_once()
            except Exception:
                pass
            await _aio.sleep(30)

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
        app.state.coord = DownloadCoordinator(root_dir=cfg.download_dir)
        app.state.coord.set_registry(app.state.registry)
        app.state.sweep_once = sweep_once

        import asyncio as _aio

        sweeper_task = _aio.create_task(sweeper_loop())
        try:
            yield
        finally:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except _aio.CancelledError:
                pass
            if owns_pool and state_pool is not None:
                await state_pool.stop()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        token = cfg.auth_token
        if token and request.url.path != "/healthz":
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "detail": "invalid or missing token"},
                )
        return await call_next(request)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz():
        pool = app.state.pool
        registry = app.state.registry
        running = pool is not None and pool._browser is not None
        return HealthResponse(
            ok=True,
            browser="running" if running else "down",
            tabs=registry.count(),
        )

    @app.get("/tabs", response_model=list[TabSummary])
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

    def _download_to_status(dl) -> DownloadStatus:
        return DownloadStatus(
            id=dl.id,
            url=dl.url,
            filename=dl.filename,
            state=dl.state,
            bytes_received=dl.bytes_received,
            size_bytes=dl.size_bytes,
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

    @app.post("/tabs", response_model=CreateTabResponse)
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
            rec = registry.register(tab=tab, ttl_seconds=ttl)
            await coord.attach_tab(rec.id, tab)
            nav = NavCapture(tab)
            await nav.attach()
            rec.nav = nav
            if req.cookies:
                cdp_cookies = _cookies_to_cdp(req.cookies, url=req.url)
                await tab.send(uc.cdp.network.set_cookies(cdp_cookies))
            nav.reset()
            await tab.get(req.url)
            await nav.wait()
        except Exception as e:
            if rec is not None:
                registry.remove(rec.id)
                await coord.detach_tab(rec.id)
            if tab is not None:
                try:
                    await pool.close_context(tab)
                except Exception:
                    pass
            return JSONResponse(
                status_code=502,
                content={"error": "browser_error", "detail": str(e)},
            )

        # Briefly poll for a download record triggered by the navigation.
        final_url = tab.url or req.url
        download_info = None
        for _ in range(20):  # up to ~0.5s
            if rec.downloads:
                dl_first = next(iter(rec.downloads.values()))
                download_info = DownloadInfo(
                    id=dl_first.id, filename=dl_first.filename, size_bytes=dl_first.size_bytes
                )
                final_url = dl_first.url  # spec requires the origin URL, not about:blank
                break
            await _asyncio.sleep(0.025)

        return CreateTabResponse(
            id=rec.id,
            status=nav.status if nav.status is not None else 200,
            final_url=final_url,
            content_type=nav.mime_type,
            download=download_info,
        )

    @app.delete("/tabs/{tab_id}", status_code=204)
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
        return Response(status_code=204)

    @app.get("/tabs/{tab_id}", response_model=TabState)
    async def get_tab(tab_id: int):
        registry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        registry.touch(tab_id)
        title = await rec.tab.evaluate("document.title")
        ready = await rec.tab.evaluate("document.readyState")
        return TabState(url=rec.tab.url or "", title=title or "", ready_state=ready or "")

    async def _require_tab(tab_id: int):
        registry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return None
        registry.touch(tab_id)
        return rec

    @app.get("/tabs/{tab_id}/html")
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

    @app.get("/tabs/{tab_id}/cookies")
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

    @app.get("/tabs/{tab_id}/screenshot")
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

    @app.get("/tabs/{tab_id}/downloads", response_model=list[DownloadStatus])
    async def list_downloads(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        return [_download_to_status(dl) for dl in rec.downloads.values()]

    @app.get("/tabs/{tab_id}/downloads/{did}/status", response_model=DownloadStatus)
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

    @app.get("/tabs/{tab_id}/downloads/{did}")
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

    @app.post("/tabs/{tab_id}/downloads/{did}/cancel", status_code=204)
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

    @app.delete("/tabs/{tab_id}/downloads/{did}", status_code=204)
    async def delete_download(tab_id: int, did: str):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        dl = rec.downloads.pop(did, None)
        if dl is None:
            return JSONResponse(
                status_code=404,
                content={"error": "download_not_found", "detail": f"no download {did}"},
            )
        coord = app.state.coord
        if dl.state == "in_progress":
            try:
                async with rec.lock:
                    await coord.cancel(rec.tab, did)
            except Exception:
                pass
        try:
            if dl.path.exists():
                dl.path.unlink()
        except OSError:
            pass
        return Response(status_code=204)

    @app.post("/tabs/{tab_id}/goto", response_model=GotoResponse)
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
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        status = rec.nav.status if rec.nav and rec.nav.status is not None else 200
        ctype = rec.nav.mime_type if rec.nav else None

        new_dl = None
        for _ in range(20):
            diff = set(rec.downloads.keys()) - pre_existing
            if diff:
                new_dl = rec.downloads[next(iter(diff))]
                break
            await _asyncio.sleep(0.025)

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

    @app.post("/tabs/{tab_id}/click", status_code=204)
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

    @app.post("/tabs/{tab_id}/type", status_code=204)
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

    @app.post("/tabs/{tab_id}/eval", response_model=EvalResponse)
    async def eval_js(tab_id: int, req: EvalRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                result = await rec.tab.evaluate(req.js)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return EvalResponse(result=result)

    @app.post("/tabs/{tab_id}/wait", status_code=204)
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
            last_zero_at = _asyncio.get_event_loop().time()

            def _on_request(_e):
                nonlocal inflight
                inflight += 1

            def _on_done(_e):
                nonlocal inflight, last_zero_at
                inflight = max(0, inflight - 1)
                if inflight == 0:
                    last_zero_at = _asyncio.get_event_loop().time()

            rec.tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
            rec.tab.add_handler(uc.cdp.network.LoadingFinished, _on_done)
            rec.tab.add_handler(uc.cdp.network.LoadingFailed, _on_done)
            await rec.tab.send(uc.cdp.network.enable())

            deadline = _asyncio.get_event_loop().time() + timeout_s
            while _asyncio.get_event_loop().time() < deadline:
                now = _asyncio.get_event_loop().time()
                if inflight == 0 and (now - last_zero_at) >= 0.5:
                    return
                await _asyncio.sleep(0.05)
            raise TimeoutError()

        async with rec.lock:
            try:
                tasks = []
                if req.selector:
                    tasks.append(_wait_selector())
                if req.network_idle:
                    tasks.append(_wait_network_idle())
                await _asyncio.wait_for(_asyncio.gather(*tasks), timeout=timeout_s)
            except TimeoutError:
                return JSONResponse(
                    status_code=408, content={"error": "timeout", "detail": "wait timed out"}
                )
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return Response(status_code=204)

    @app.post("/fetch", response_model=FetchResponse)
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
                deadline = _asyncio.get_event_loop().time() + 10.0
                while _asyncio.get_event_loop().time() < deadline:
                    ready = await rec.tab.evaluate("document.readyState")
                    if ready == "complete":
                        break
                    await _asyncio.sleep(0.05)
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

    return app
