from __future__ import annotations

from contextlib import asynccontextmanager

import nodriver as uc
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.models import (
    CreateTabRequest,
    CreateTabResponse,
    HealthResponse,
    TabState,
    TabSummary,
)
from passe_partout.tab_registry import TabRegistry


def build_app(cfg: Config, browser_pool: BrowserPool | None = None) -> FastAPI:
    state_pool = browser_pool

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
        try:
            yield
        finally:
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

    return app
