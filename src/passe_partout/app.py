from __future__ import annotations

import asyncio as _asyncio
import base64
from contextlib import asynccontextmanager

import nodriver as uc
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.models import (
    ClickRequest,
    CreateTabRequest,
    CreateTabResponse,
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
from passe_partout.tab_registry import TabRegistry


def build_app(cfg: Config, browser_pool: BrowserPool | None = None) -> FastAPI:
    state_pool = browser_pool

    async def sweep_once():
        registry = app.state.registry
        pool = app.state.pool
        for tid in registry.idle_ids():
            rec = registry.remove(tid)
            if rec is not None:
                try:
                    await pool.close_context(rec.tab)
                except Exception:
                    pass

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
            await state_pool.start()
        app.state.cfg = cfg
        app.state.pool = state_pool
        app.state.registry = TabRegistry()
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
        return HealthResponse(
            ok=True,
            browser="running" if pool is not None else "down",
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

        async with registry.mu:
            if registry.count() >= cfg_now.max_tabs:
                return JSONResponse(
                    status_code=429,
                    content={"error": "max_tabs", "detail": f"cap of {cfg_now.max_tabs} reached"},
                )

        try:
            if req.cookies:
                # Create context first at about:blank, set cookies, then navigate
                tab = await pool.create_context("about:blank")
                cdp_cookies = _cookies_to_cdp(req.cookies, url=req.url)
                await tab.send(uc.cdp.network.set_cookies(cdp_cookies))
                await tab.get(req.url)
            else:
                tab = await pool.create_context(req.url)
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": "browser_error", "detail": str(e)},
            )

        ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_timeout_seconds
        rec = registry.register(tab=tab, ttl_seconds=ttl)
        return CreateTabResponse(id=rec.id, status=200, final_url=tab.url or req.url)

    @app.delete("/tabs/{tab_id}", status_code=204)
    async def delete_tab(tab_id: int):
        registry = app.state.registry
        pool = app.state.pool
        rec = registry.remove(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        try:
            await pool.close_context(rec.tab)
        except Exception:
            pass
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
            out.append({
                "name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires,
                "httpOnly": c.http_only, "secure": c.secure,
                "sameSite": c.same_site.to_json() if c.same_site else None,
            })
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

    @app.post("/tabs/{tab_id}/goto", response_model=GotoResponse)
    async def goto(tab_id: int, req: GotoRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                await rec.tab.get(req.url)
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return GotoResponse(status=200, final_url=rec.tab.url or req.url)

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
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
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
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
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
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
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
            raise _asyncio.TimeoutError()

        async with rec.lock:
            try:
                tasks = []
                if req.selector:
                    tasks.append(_wait_selector())
                if req.network_idle:
                    tasks.append(_wait_network_idle())
                await _asyncio.wait_for(_asyncio.gather(*tasks), timeout=timeout_s)
            except (_asyncio.TimeoutError, TimeoutError):
                return JSONResponse(status_code=408, content={"error": "timeout", "detail": "wait timed out"})
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
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
        rec = registry.get(tid)
        try:
            async with rec.lock:
                html = await rec.tab.get_content()
            return FetchResponse(status=200, final_url=rec.tab.url or req.url, html=html)
        finally:
            registry.remove(tid)
            try:
                await pool.close_context(rec.tab)
            except Exception:
                pass

    return app
