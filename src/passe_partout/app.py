from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.models import HealthResponse
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

    # Stub for /tabs so the auth test (test_auth_required_when_token_set) can
    # hit a non-/healthz route and verify auth gating. Fully implemented in
    # Task 7; this is the minimum needed for Task 5's auth test.
    @app.get("/tabs")
    async def list_tabs_stub():
        return []

    return app
