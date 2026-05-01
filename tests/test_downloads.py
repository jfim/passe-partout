from __future__ import annotations

from pathlib import Path

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
