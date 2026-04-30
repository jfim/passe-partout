from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest_asyncio
from aiohttp import web

from passe_partout.app import build_app
from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fixture_server():
    async def handler(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = FIXTURES / f"{name}.html"
        if not path.exists():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{name}.html", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    await runner.cleanup()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser_pool():
    cfg = Config(chrome_path=os.environ.get("CHROME_PATH") or None)
    pool = BrowserPool(cfg)
    await pool.start()
    try:
        yield pool
    finally:
        await pool.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def client(browser_pool):
    cfg = Config()
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture(loop_scope="session")
async def client_with_auth(browser_pool):
    cfg = Config(auth_token="secret123")
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, "secret123"
