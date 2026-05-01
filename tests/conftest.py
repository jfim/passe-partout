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
    async def html_handler(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = FIXTURES / f"{name}.html"
        if not path.exists():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="text/html")

    async def binary_handler(_request: web.Request) -> web.Response:
        return web.Response(
            body=b"PK\x03\x04 fake zip body",
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="binary.zip"',
            },
        )

    async def png_handler(_request: web.Request) -> web.Response:
        # 1x1 transparent PNG.
        body = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
        )
        return web.Response(
            body=body,
            headers={"Content-Type": "image/png", "Content-Disposition": "inline"},
        )

    async def json_handler(_request: web.Request) -> web.Response:
        return web.Response(
            body=b'{"hello":"world"}',
            headers={"Content-Type": "application/json", "Content-Disposition": "inline"},
        )

    async def slow_binary_handler(_request: web.Request) -> web.StreamResponse:
        # Sends 8 chunks of 1KB with delays so tests can observe in_progress state.
        import asyncio as _asyncio

        resp = web.StreamResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="slow.bin"',
                "Content-Length": str(8 * 1024),
            }
        )
        await resp.prepare(_request)
        for _ in range(8):
            await resp.write(b"\x00" * 1024)
            await _asyncio.sleep(0.2)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/{name}.html", html_handler)
    app.router.add_get("/binary.zip", binary_handler)
    app.router.add_get("/sample.png", png_handler)
    app.router.add_get("/data.json", json_handler)
    app.router.add_get("/slow.bin", slow_binary_handler)
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
