from __future__ import annotations

import pytest

from passe_partout.models import (
    CaptureMode,
    CreateTabRequest,
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


def test_download_status_unknown_size_is_none():
    s = DownloadStatus(
        id="abc",
        url="http://x/",
        filename="x.zip",
        state="in_progress",
        bytes_received=0,
        started_at=1.0,
        completed_at=None,
    )
    assert s.size_bytes is None
    assert s.completed_at is None


def test_goto_response_download_optional():
    r = GotoResponse(status=200, final_url="http://x/", content_type="text/html")
    assert r.download is None


def test_capture_mode_values():
    assert CaptureMode.NO_COPY.value == "no_copy"
    assert CaptureMode.COPY.value == "copy"
    assert CaptureMode.COPY_AND_RETAIN.value == "copy_and_retain"


def test_create_tab_request_defaults_to_no_copy():
    req = CreateTabRequest(url="http://example.com/")
    assert req.capture_mode is CaptureMode.NO_COPY


def test_create_tab_request_accepts_each_mode():
    for value, expected in [
        ("no_copy", CaptureMode.NO_COPY),
        ("copy", CaptureMode.COPY),
        ("copy_and_retain", CaptureMode.COPY_AND_RETAIN),
    ]:
        req = CreateTabRequest(url="http://example.com/", capture_mode=value)
        assert req.capture_mode is expected


def test_create_tab_request_rejects_unknown_mode():
    with pytest.raises(ValueError):
        CreateTabRequest(url="http://example.com/", capture_mode="bogus")
