from __future__ import annotations

from pathlib import Path

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
