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
async def test_download_status_endpoint(fixture_server, browser_pool, tmp_path):
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
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert s.status_code == 200
            assert s.json()["id"] == did
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_status_404(client):
    r = await client.post("/tabs", json={"url": "about:blank"})
    tid = r.json()["id"]
    bad = await client.get(f"/tabs/{tid}/downloads/nope/status")
    assert bad.status_code == 404
    await client.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_bytes_completed(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 200
            assert b.headers["content-disposition"].startswith("attachment")
            assert "binary.zip" in b.headers["content-disposition"]
            assert b.headers["content-type"].startswith("application/zip")
            assert b.content == b"PK\x03\x04 fake zip body"
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_bytes_in_progress_returns_425(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 425
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_cancel_in_progress_download(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            cx = await c.post(f"/tabs/{tid}/downloads/{did}/cancel")
            assert cx.status_code == 204
            for _ in range(20):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "canceled":
                    break
                await asyncio.sleep(0.05)
            assert s.json()["state"] == "canceled"
            # Bytes endpoint now returns 410.
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 410
            # Cancel on terminal state returns 409.
            cx2 = await c.post(f"/tabs/{tid}/downloads/{did}/cancel")
            assert cx2.status_code == 409
            await c.delete(f"/tabs/{tid}")


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


@pytest.mark.asyncio
async def test_delete_completed_download_unlinks_file(fixture_server, browser_pool, tmp_path):
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
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            file_path = tmp_path / "passe-partout" / f"tab-{tid}" / did
            assert file_path.exists()
            d = await c.delete(f"/tabs/{tid}/downloads/{did}")
            assert d.status_code == 204
            assert not file_path.exists()
            s2 = await c.get(f"/tabs/{tid}/downloads/{did}/status")
            assert s2.status_code == 404
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_goto_to_binary_returns_download(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tid = r.json()["id"]
            r2 = await c.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/binary.zip"})
            assert r2.status_code == 200
            body = r2.json()
            assert body["download"] is not None
            assert body["download"]["filename"] == "binary.zip"
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_delete_in_progress_cancels_then_unlinks(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            d = await c.delete(f"/tabs/{tid}/downloads/{did}")
            assert d.status_code == 204
            file_path = tmp_path / "passe-partout" / f"tab-{tid}" / did
            assert not file_path.exists()
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_click_triggers_download(fixture_server, browser_pool, tmp_path):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/click_to_download.html"})
            tid = r.json()["id"]
            assert r.json()["download"] is None
            cl = await c.post(f"/tabs/{tid}/click", json={"selector": "#dl"})
            assert cl.status_code == 204
            lst = []
            for _ in range(40):
                lst = (await c.get(f"/tabs/{tid}/downloads")).json()
                if lst and lst[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert lst and lst[0]["filename"] == "binary.zip"
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_multiple_downloads_per_tab(fixture_server, browser_pool, tmp_path):
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
            await c.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/data.json"})
            await asyncio.sleep(0.5)
            lst = (await c.get(f"/tabs/{tid}/downloads")).json()
            assert len(lst) == 2
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_response_final_url_is_origin(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            url = f"{fixture_server}/binary.zip"
            r = await c.post("/tabs", json={"url": url})
            assert r.status_code == 200
            body = r.json()
            assert body["download"] is not None
            assert body["final_url"] == url, f"expected {url}, got {body['final_url']}"
            await c.delete(f"/tabs/{body['id']}")


@pytest.mark.asyncio
async def test_idle_sweep_does_not_evict_during_active_download(
    fixture_server, browser_pool, tmp_path
):
    import httpx

    from passe_partout.app import build_app
    from passe_partout.config import Config

    # ttl=1s would otherwise evict quickly. The slow fixture streams ~1.6s,
    # so without the touch on progress events it would be evicted.
    cfg = Config(download_dir=str(tmp_path), idle_tab_close_seconds=1)
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            # Manually run the sweeper a few times during the download.
            for _ in range(8):
                await app.state.sweep_once()
                await asyncio.sleep(0.2)
            assert app.state.registry.get(tid) is not None
            # Eventually the download completes.
            for _ in range(40):
                lst = (await c.get(f"/tabs/{tid}/downloads")).json()
                if lst and lst[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
            assert lst[0]["state"] == "completed"
            await c.delete(f"/tabs/{tid}")
