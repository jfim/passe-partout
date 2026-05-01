from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from passe_partout.downloads import DownloadRecord


def test_download_record_defaults():
    rec = DownloadRecord(
        id="abc",
        url="http://x/file.zip",
        filename="file.zip",
        path=Path("/tmp/file"),
        started_at=1.0,
    )
    assert rec.state == "in_progress"
    assert rec.bytes_received == 0
    assert rec.size_bytes == -1
    assert rec.completed_at is None


@pytest.mark.asyncio
async def test_coordinator_creates_and_removes_per_tab_dir(tmp_path):
    from passe_partout.downloads import DownloadCoordinator

    coord = DownloadCoordinator(root_dir=str(tmp_path))
    tab_dir = coord.tab_dir(tab_id=42)
    assert not tab_dir.exists()
    coord.ensure_tab_dir(tab_id=42)
    assert tab_dir.exists()
    coord.cleanup_tab_dir(tab_id=42)
    assert not tab_dir.exists()


@pytest.mark.asyncio
async def test_coordinator_cleanup_is_idempotent(tmp_path):
    from passe_partout.downloads import DownloadCoordinator

    coord = DownloadCoordinator(root_dir=str(tmp_path))
    coord.cleanup_tab_dir(tab_id=99)  # never created
    coord.ensure_tab_dir(tab_id=99)
    coord.cleanup_tab_dir(tab_id=99)
    coord.cleanup_tab_dir(tab_id=99)  # second call is fine


@pytest.mark.asyncio
async def test_tab_creation_creates_download_dir(browser_pool, fixture_server, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            tab_id = r.json()["id"]
            tab_dir = tmp_path / "passe-partout" / f"tab-{tab_id}"
            assert tab_dir.exists()
            await c.delete(f"/tabs/{tab_id}")
            assert not tab_dir.exists()


@pytest.mark.asyncio
async def test_origin_attachment_creates_download_record(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            assert r.status_code == 200
            body = r.json()
            assert body["download"] is not None
            assert body["download"]["filename"] == "binary.zip"
            tab_id = body["id"]
            # Wait for completion event.
            dl = None
            for _ in range(40):
                rec = app.state.registry.get(tab_id)
                dl = next(iter(rec.downloads.values()), None)
                if dl is not None and dl.state == "completed":
                    break
                await asyncio.sleep(0.05)
            assert dl is not None
            assert dl.state == "completed"
            assert dl.bytes_received > 0
            await c.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_image_navigation_becomes_download(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/sample.png"})
            assert r.status_code == 200
            body = r.json()
            assert body["download"] is not None
            assert body["download"]["filename"]  # non-empty (Chromium derives from URL)
            await c.delete(f"/tabs/{body['id']}")


@pytest.mark.asyncio
async def test_html_navigation_does_not_become_download(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            assert r.json()["download"] is None
            await c.delete(f"/tabs/{r.json()['id']}")


@pytest.mark.asyncio
async def test_subresources_do_not_become_downloads(fixture_server, browser_pool, tmp_path):
    """An HTML page with an embedded <img> must not produce a download record."""
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            await asyncio.sleep(0.5)  # let the embedded image finish loading
            rec = app.state.registry.get(tab_id)
            assert rec.downloads == {}
            await c.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_list_downloads_returns_records(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            for _ in range(40):
                lst = await c.get(f"/tabs/{tid}/downloads")
                if lst.json() and lst.json()[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            data = lst.json()
            assert len(data) == 1
            assert data[0]["filename"] == "binary.zip"
            assert data[0]["state"] == "completed"
            assert data[0]["bytes_received"] > 0
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_list_downloads_404_for_unknown_tab(client):
    r = await client.get("/tabs/99999/downloads")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_iframe_document_does_not_become_download(fixture_server, browser_pool, tmp_path):
    """A page whose iframe loads a non-HTML resource must not produce a download."""
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/iframe_with_image.html"})
            tab_id = r.json()["id"]
            await asyncio.sleep(0.5)
            rec = app.state.registry.get(tab_id)
            assert rec.downloads == {}
            await c.delete(f"/tabs/{tab_id}")
