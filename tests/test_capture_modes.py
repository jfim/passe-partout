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


@pytest.mark.asyncio
async def test_create_tab_propagates_capture_mode_to_registry(client, fixture_server):
    resp = await client.post(
        "/tabs",
        json={
            "url": f"{fixture_server}/static.html",
            "capture_mode": "copy_and_retain",
        },
    )
    assert resp.status_code == 200, resp.text
    tab_id = resp.json()["id"]
    try:
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        assert rec.capture_mode is CaptureMode.COPY_AND_RETAIN
        assert app.state.recorder.mode_for(tab_id) is CaptureMode.COPY_AND_RETAIN
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_no_copy_does_not_buffer_bodies(client, fixture_server):
    tab_id = await _open_tab(client, f"{fixture_server}/warc_page.html", CaptureMode.NO_COPY)
    try:
        await asyncio.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        assert all(r.body is None for r in rec.resources.values())
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_buffers_bodies(client, fixture_server):
    tab_id = await _open_tab(client, f"{fixture_server}/warc_page.html", CaptureMode.COPY)
    try:
        await asyncio.sleep(0.8)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        json_records = [r for r in rec.resources.values() if r.url.endswith("/data.json")]
        assert json_records, "no /data.json record found"
        assert json_records[0].body == b'{"hello":"world"}'
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_prunes_on_navigation(client, fixture_server):
    tab_id = await _open_tab(client, f"{fixture_server}/warc_page.html", CaptureMode.COPY)
    try:
        await asyncio.sleep(0.5)
        nav = await client.post(
            f"/tabs/{tab_id}/goto", json={"url": f"{fixture_server}/static.html"}
        )
        assert nav.status_code == 200, nav.text
        await asyncio.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        urls = {r.url for r in rec.resources.values()}
        assert not any(u.endswith("/warc_page.html") for u in urls)
        assert not any(u.endswith("/sample.png") for u in urls)
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_and_retain_keeps_resources_across_navigation(client, fixture_server):
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.COPY_AND_RETAIN
    )
    try:
        await asyncio.sleep(0.5)
        nav = await client.post(
            f"/tabs/{tab_id}/goto", json={"url": f"{fixture_server}/static.html"}
        )
        assert nav.status_code == 200, nav.text
        await asyncio.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        urls = {r.url for r in rec.resources.values()}
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/static.html") for u in urls)
    finally:
        await _close_tab(client, tab_id)
