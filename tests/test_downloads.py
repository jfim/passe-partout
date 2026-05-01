from __future__ import annotations

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
