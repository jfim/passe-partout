from __future__ import annotations

import httpx
import pytest

from passe_partout.app import build_app
from passe_partout.config import Config
from passe_partout.resources import ResourceRecord, ResourceRecorder
from passe_partout.tab_registry import TabRegistry


def test_resource_record_defaults():
    r = ResourceRecord(
        request_id="r1",
        url="https://x/y.css",
        status=200,
        mime_type="text/css",
        resource_type="ResourceType.STYLESHEET",
        loader_id="L1",
    )
    assert r.encoded_size == 0


def test_recorder_detach_clears_loader_state():
    recorder = ResourceRecorder()
    recorder._current_loader[7] = "L7"
    recorder.detach_tab(7)
    assert 7 not in recorder._current_loader
    # Idempotent
    recorder.detach_tab(7)


@pytest.mark.asyncio
async def test_list_resources_includes_document_and_subresources(
    browser_pool, fixture_server, tmp_path
):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            tab_id = r.json()["id"]

            r = await c.get(f"/tabs/{tab_id}/resources")
            assert r.status_code == 200
            items = r.json()
            urls = {it["url"] for it in items}
            assert any(u.endswith("/normal_page.html") for u in urls), urls
            assert any(u.endswith("/sample.png") for u in urls), urls

            doc = next(it for it in items if it["url"].endswith("/normal_page.html"))
            assert doc["status"] == 200
            assert doc["mime_type"].startswith("text/html")
            assert doc["resource_type"] == "ResourceType.DOCUMENT"
            assert doc["encoded_size"] > 0  # Network.loadingFinished should have populated this


@pytest.mark.asyncio
async def test_get_resource_body_returns_document_html(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            items = (await c.get(f"/tabs/{tab_id}/resources")).json()
            doc = next(it for it in items if it["url"].endswith("/normal_page.html"))

            body = await c.get(f"/tabs/{tab_id}/resources/{doc['request_id']}")
            assert body.status_code == 200
            assert body.headers["content-type"].startswith("text/html")
            assert b"<h1>Hello</h1>" in body.content


@pytest.mark.asyncio
async def test_get_resource_body_returns_binary_image(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            items = (await c.get(f"/tabs/{tab_id}/resources")).json()
            png = next((it for it in items if it["url"].endswith("/sample.png")), None)
            assert png is not None, items

            body = await c.get(f"/tabs/{tab_id}/resources/{png['request_id']}")
            assert body.status_code == 200
            # PNG magic bytes — confirms base64 round-trip happened correctly.
            assert body.content.startswith(b"\x89PNG\r\n\x1a\n"), body.content[:8]


@pytest.mark.asyncio
async def test_navigation_evicts_previous_loader_metadata(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            items_before = (await c.get(f"/tabs/{tab_id}/resources")).json()
            old_doc = next(it for it in items_before if it["url"].endswith("/normal_page.html"))

            # Navigate to a different fixture page; previous loader's metadata should
            # be pruned from rec.resources, and Chrome should drop the body so a body
            # request returns 410.
            r = await c.post(
                f"/tabs/{tab_id}/goto",
                json={"url": f"{fixture_server}/static.html"},
            )
            assert r.status_code == 200

            items_after = (await c.get(f"/tabs/{tab_id}/resources")).json()
            urls_after = {it["url"] for it in items_after}
            assert not any(u.endswith("/normal_page.html") for u in urls_after), urls_after
            assert any(u.endswith("/static.html") for u in urls_after), urls_after

            # Loader-id pruning removed the old request from rec.resources, so the
            # lookup short-circuits to 404 resource_not_found before we even ask Chrome
            # for the body. (The 410 body_unavailable path is for the rarer race where
            # metadata is still present but Chrome has already evicted the body, e.g.
            # under buffer-cap pressure.)
            body = await c.get(f"/tabs/{tab_id}/resources/{old_doc['request_id']}")
            assert body.status_code == 404
            assert body.json()["error"] == "resource_not_found"


@pytest.mark.asyncio
async def test_resources_404_when_tab_missing(browser_pool, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/tabs/99/resources")
            assert r.status_code == 404
            r = await c.get("/tabs/99/resources/abc")
            assert r.status_code == 404


@pytest.mark.asyncio
async def test_resource_body_404_when_request_id_unknown(browser_pool, fixture_server, tmp_path):
    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            r = await c.get(f"/tabs/{tab_id}/resources/never-seen-this-id")
            assert r.status_code == 404
            assert r.json()["error"] == "resource_not_found"


def test_tab_registry_record_carries_resources_dict():
    reg = TabRegistry()
    rec = reg.register(tab=object(), ttl_seconds=10)
    assert rec.resources == {}
    rec.resources["r1"] = ResourceRecord(
        request_id="r1",
        url="x",
        status=200,
        mime_type="text/plain",
        resource_type="ResourceType.OTHER",
        loader_id="L1",
    )
    assert reg.get(rec.id).resources["r1"].url == "x"
