from __future__ import annotations

from passe_partout.models import (
    CreateTabResponse,
    DownloadInfo,
    DownloadStatus,
    GotoResponse,
)


def test_create_tab_response_download_optional():
    r = CreateTabResponse(id=1, status=200, final_url="http://x/", content_type="text/html")
    assert r.download is None


def test_create_tab_response_with_download():
    r = CreateTabResponse(
        id=1,
        status=200,
        final_url="http://x/file.zip",
        content_type="application/zip",
        download=DownloadInfo(id="abc", filename="file.zip", size_bytes=1024),
    )
    assert r.download.id == "abc"
    assert r.download.size_bytes == 1024


def test_download_status_unknown_size_is_minus_one():
    s = DownloadStatus(
        id="abc",
        url="http://x/",
        filename="x.zip",
        state="in_progress",
        bytes_received=0,
        size_bytes=-1,
        started_at=1.0,
        completed_at=None,
    )
    assert s.size_bytes == -1
    assert s.completed_at is None


def test_goto_response_download_optional():
    r = GotoResponse(status=200, final_url="http://x/", content_type="text/html")
    assert r.download is None
