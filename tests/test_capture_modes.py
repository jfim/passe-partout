from __future__ import annotations

import asyncio

import pytest

from passe_partout.models import CaptureMode


async def _open_tab(client, url: str, mode: CaptureMode) -> int:
    resp = await client.post(
        "/tabs",
        json={"url": url, "capture_mode": mode.value},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _close_tab(client, tab_id: int) -> None:
    await client.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_request_and_response_headers_are_captured(client, fixture_server):
    tab_id = await _open_tab(client, f"{fixture_server}/warc_page.html", CaptureMode.NO_COPY)
    try:
        # Give the in-page fetch() a moment to complete.
        await asyncio.sleep(0.5)

        # Reach into the registry via the app to inspect ResourceRecord internals.
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        assert rec is not None
        urls = {r.url for r in rec.resources.values()}
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/sample.png") for u in urls)
        assert any(u.endswith("/data.json") for u in urls)

        for r in rec.resources.values():
            assert r.method in ("GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS")
            assert r.response_headers, f"no response headers captured for {r.url}"
            assert r.captured_at > 0.0
    finally:
        await _close_tab(client, tab_id)
