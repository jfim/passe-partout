from __future__ import annotations

import asyncio
import io

import pytest
from warcio.archiveiterator import ArchiveIterator


async def _open(client, url: str, mode: str = "copy") -> int:
    r = await client.post("/tabs", json={"url": url, "capture_mode": mode})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _close(client, tab_id: int) -> None:
    await client.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_warc_endpoint_returns_archive_with_all_resources(client, fixture_server):
    tab_id = await _open(client, f"{fixture_server}/warc_page.html", mode="copy")
    try:
        await asyncio.sleep(0.8)  # let the in-page fetch complete
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/warc")
        assert "attachment" in resp.headers["content-disposition"]

        records = list(ArchiveIterator(io.BytesIO(resp.content)))
        types = [r.rec_type for r in records]
        assert types[0] == "warcinfo"
        n_responses = sum(1 for t in types if t == "response")
        n_requests = sum(1 for t in types if t == "request")
        assert n_responses == n_requests
        assert n_responses >= 3

        urls = {
            r.rec_headers.get_header("WARC-Target-URI") for r in records if r.rec_type == "response"
        }
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/sample.png") for u in urls)
        assert any(u.endswith("/data.json") for u in urls)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_404_on_unknown_tab(client):
    resp = await client.get("/tabs/999999/warc")
    assert resp.status_code == 404
    assert resp.json()["error"] == "tab_not_found"


@pytest.mark.asyncio
async def test_warc_endpoint_works_for_no_copy_mode(client, fixture_server):
    """NO_COPY mode: bodies fetched live from Chrome at export time."""
    tab_id = await _open(client, f"{fixture_server}/warc_page.html", mode="no_copy")
    try:
        await asyncio.sleep(0.8)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        records = list(ArchiveIterator(io.BytesIO(resp.content)))
        assert any(r.rec_type == "response" for r in records)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_does_not_buffer_bodies_in_no_copy_mode(client, fixture_server):
    """Regression: GET /warc must not populate r.body for NO_COPY tabs."""
    tab_id = await _open(client, f"{fixture_server}/warc_page.html", mode="no_copy")
    try:
        await asyncio.sleep(0.8)
        # Sanity: before the WARC export, no bodies are buffered.
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        assert all(r.body is None for r in rec.resources.values())

        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200

        # After the WARC export, NO_COPY records must STILL have no buffered body.
        assert all(r.body is None for r in rec.resources.values()), (
            "GET /warc leaked bodies onto NO_COPY records"
        )
    finally:
        await _close(client, tab_id)
