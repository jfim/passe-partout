from __future__ import annotations

import httpx
import pytest

from passe_partout.app import build_app
from passe_partout.config import Config


@pytest.mark.asyncio
async def test_get_browser_reports_config(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path), headless=True)
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/browser")
            assert r.status_code == 200
            body = r.json()
            assert body["headless"] is True
            assert body["shared_profile"] is False
            assert body["extension_dirs"] == []
            assert body["tab_count"] == 0
            assert "running" in body  # bool, varies depending on test ordering

            # Once a tab is created the count goes up and the browser is running.
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            r = await c.get("/browser")
            body = r.json()
            assert body["tab_count"] == 1
            assert body["running"] is True
            await c.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_delete_browser_returns_200_when_no_tabs(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # Use a tab to force the browser into a running state, then close it.
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            await c.delete(f"/tabs/{tab_id}")

            r = await c.delete("/browser")
            assert r.status_code == 200
            body = r.json()
            assert body == {"ok": True, "stopped": True}

            # Calling again is harmless: no tabs, browser already down.
            r = await c.delete("/browser")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "stopped": False}


@pytest.mark.asyncio
async def test_delete_browser_returns_409_when_tabs_open(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]

            r = await c.delete("/browser")
            assert r.status_code == 409
            body = r.json()
            assert body["error"] == "tabs_open"
            assert "1 tab" in body["detail"]

            # /browser still reports the browser as running.
            r = await c.get("/browser")
            assert r.json()["running"] is True
            assert r.json()["tab_count"] == 1

            # Cleanup.
            await c.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_delete_browser_then_create_tab_restarts_chromium(
    browser_pool, fixture_server, tmp_path
):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            await c.delete(f"/tabs/{tab_id}")
            r = await c.delete("/browser")
            assert r.status_code == 200

            # Lazy restart should kick in on the next /tabs.
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            await c.delete(f"/tabs/{r.json()['id']}")
