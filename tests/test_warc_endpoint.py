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
async def test_warc_endpoint_includes_redirect_hops(client, fixture_server):
    """A 302 → final-page chain should appear as two response records in the WARC."""
    tab_id = await _open(client, f"{fixture_server}/redirect-to-static", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        records = [r for r in ArchiveIterator(io.BytesIO(resp.content)) if r.rec_type == "response"]
        by_uri = {r.rec_headers.get_header("WARC-Target-URI"): r for r in records}

        redirect_uri = f"{fixture_server}/redirect-to-static"
        final_uri = f"{fixture_server}/static.html"
        assert redirect_uri in by_uri, f"missing redirect record; saw {list(by_uri)}"
        assert final_uri in by_uri, f"missing final record; saw {list(by_uri)}"

        # The redirect hop is headers-only (Chrome doesn't expose the body for
        # auto-followed redirects), so it ships with WARC-Truncated: unspecified.
        assert by_uri[redirect_uri].rec_headers.get_header("WARC-Truncated") == "unspecified"
        assert by_uri[final_uri].rec_headers.get_header("WARC-Truncated") is None
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_includes_multi_hop_redirect_chain(client, fixture_server):
    """A two-hop redirect chain should appear as three response records."""
    tab_id = await _open(client, f"{fixture_server}/redirect-chain", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200
        records = [r for r in ArchiveIterator(io.BytesIO(resp.content)) if r.rec_type == "response"]
        uris = [r.rec_headers.get_header("WARC-Target-URI") for r in records]
        assert f"{fixture_server}/redirect-chain" in uris
        assert f"{fixture_server}/redirect-to-static" in uris
        assert f"{fixture_server}/static.html" in uris
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
async def test_warc_endpoint_retains_previous_navigation_in_copy_and_retain(client, fixture_server):
    """COPY_AND_RETAIN: WARC must include resources from prior navigations on the tab."""
    tab_id = await _open(client, f"{fixture_server}/warc_page.html", mode="copy_and_retain")
    try:
        await asyncio.sleep(0.8)
        # Navigate again — would create a new loader_id and prune under COPY/NO_COPY.
        nav = await client.post(
            f"/tabs/{tab_id}/goto", json={"url": f"{fixture_server}/normal_page.html"}
        )
        assert nav.status_code == 200, nav.text
        await asyncio.sleep(0.5)

        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        urls = {
            r.rec_headers.get_header("WARC-Target-URI")
            for r in ArchiveIterator(io.BytesIO(resp.content))
            if r.rec_type == "response"
        }
        # Both navigations and the first page's subresources should be present.
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/sample.png") for u in urls)
        assert any(u.endswith("/normal_page.html") for u in urls)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_rendered_emits_conversion_record_with_dom_and_screenshot(
    client, fixture_server
):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text

        conversions: list[tuple[dict, bytes]] = []
        responses: list[tuple[dict, bytes]] = []
        for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True):
            headers = dict(r.rec_headers.headers)
            body = r.content_stream().read()
            if r.rec_type == "conversion":
                conversions.append((headers, body))
            elif r.rec_type == "response":
                responses.append((headers, body))

        assert len(conversions) == 1, "expected one rendered-targets conversion record"
        conv_headers, conv_body = conversions[0]
        assert conv_headers["Content-Type"] == "application/json"
        assert "warc-rendered-targets" in conv_headers["WARC-Profile"]
        main_doc_uri = f"{fixture_server}/normal_page.html"
        main_resp = next(h for h, _ in responses if h.get("WARC-Target-URI") == main_doc_uri)
        assert conv_headers["WARC-Refers-To"] == main_resp["WARC-Record-ID"]

        payload = json.loads(conv_body)
        pages = payload["log"]["pages"]
        assert len(pages) >= 1
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        dom_html = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")
        assert "<h1>Hello</h1>" in dom_html
        png_bytes = base64.b64decode(top["renderedElements"][0]["content"])
        assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), png_bytes[:8]
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_rendered_captures_iframe_dom(client, fixture_server):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/iframe_with_image.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            (dict(r.rec_headers.headers), r.content_stream().read())
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        payload = json.loads(conv[1])
        pages = payload["log"]["pages"]
        # One top + at least one iframe.
        assert len(pages) >= 2, [p["_passepartout_url"] for p in pages]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        children = [
            p for p in pages if p["_passepartout_parentFrameId"] == top["_passepartout_frameId"]
        ]
        assert children, "expected at least one child frame entry"
        child = children[0]
        assert "iframe" in child["_passepartout_ownerSelector"].lower()
        # Top has the screenshot; iframe entries do not.
        assert "renderedElements" in top
        assert "renderedElements" not in child
        # Top DOM contains an <iframe> tag.
        top_dom = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")
        assert "<iframe" in top_dom
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_rendered_screenshot_opt_out(client, fixture_server):
    """`?rendered=1&screenshot=0` → rendered DOM record, but no screenshot."""
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1&screenshot=0")
        assert resp.status_code == 200, resp.text
        conv = next(
            (dict(r.rec_headers.headers), r.content_stream().read())
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        payload = json.loads(conv[1])
        pages = payload["log"]["pages"]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        # DOM is still captured...
        dom_html = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")
        assert "<h1>Hello</h1>" in dom_html
        # ...but no screenshot was taken.
        assert "renderedElements" not in top
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_rendered_off_by_default(client, fixture_server):
    """No `?rendered=1` → no conversion record."""
    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        types = [r.rec_type for r in ArchiveIterator(io.BytesIO(resp.content))]
        assert "conversion" not in types
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_domsnapshot_emits_conversion_record(client, fixture_server):
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?domsnapshot=1&computed_styles=display,color")
        assert resp.status_code == 200, resp.text

        conversions: list[tuple[dict, bytes]] = []
        responses: list[tuple[dict, bytes]] = []
        for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True):
            headers = dict(r.rec_headers.headers)
            body = r.content_stream().read()
            if r.rec_type == "conversion":
                conversions.append((headers, body))
            elif r.rec_type == "response":
                responses.append((headers, body))

        assert len(conversions) == 1, "expected one DOM snapshot conversion record"
        conv_headers, conv_body = conversions[0]
        assert conv_headers["Content-Type"] == "application/json"
        assert "dom-snapshot" in conv_headers["WARC-Profile"]
        assert conv_headers["X-Passe-Partout-Computed-Styles"] == "display,color"
        main_doc_uri = f"{fixture_server}/normal_page.html"
        main_resp = next(h for h, _ in responses if h.get("WARC-Target-URI") == main_doc_uri)
        assert conv_headers["WARC-Refers-To"] == main_resp["WARC-Record-ID"]

        payload = json.loads(conv_body)
        assert "documents" in payload and "strings" in payload
        assert isinstance(payload["documents"], list)
        assert isinstance(payload["strings"], list)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_domsnapshot_off_by_default(client, fixture_server):
    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        types = [r.rec_type for r in ArchiveIterator(io.BytesIO(resp.content))]
        assert "conversion" not in types
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_domsnapshot_includes_dom_rects_and_paint_order(client, fixture_server):
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?domsnapshot=1&dom_rects=1&paint_order=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        layout = json.loads(conv)["documents"][0]["layout"]
        # dom_rects adds offset/scroll/client rects; paint_order adds paintOrders.
        assert "offsetRects" in layout
        assert "scrollRects" in layout
        assert "clientRects" in layout
        assert "paintOrders" in layout
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_domsnapshot_omits_rects_and_paint_order_by_default(
    client, fixture_server
):
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?domsnapshot=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        layout = json.loads(conv)["documents"][0]["layout"]
        # Absolute bounds are always present; the optional rect sets and paint
        # order are not, since the flags default off.
        assert "bounds" in layout
        assert "offsetRects" not in layout
        assert "paintOrders" not in layout
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_endpoint_rendered_and_domsnapshot_both_emit_records(client, fixture_server):
    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1&domsnapshot=1")
        assert resp.status_code == 200, resp.text
        profiles = [
            r.rec_headers.get_header("WARC-Profile")
            for r in ArchiveIterator(io.BytesIO(resp.content))
            if r.rec_type == "conversion"
        ]
        assert len(profiles) == 2
        assert any("warc-rendered-targets" in p for p in profiles)
        assert any("dom-snapshot" in p for p in profiles)
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


@pytest.mark.asyncio
async def test_warc_rendered_folds_cssom(client, fixture_server):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/cssom_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        pages = json.loads(conv)["log"]["pages"]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        dom = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")

        # CSSOM-mutated <style> rule folded in
        assert ".injected" in dom
        # document-level adopted sheet inserted before </body>
        assert ".docadopt" in dom
        assert dom.index(".docadopt") < dom.index("</body>")
        # shadow-scoped adopted + inline styles land inside the shadow template
        tpl = dom.index("shadowrootmode")
        tpl_end = dom.index("</template>", tpl)
        assert tpl < dom.index(".shadopt") < tpl_end
        assert tpl < dom.index(".shinline") < tpl_end
        # off-by-one guard: light <style> rule present and NOT inside that template
        assert ".lightinjected" in dom
        assert dom.index(".lightinjected") > dom.index("</template>")
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_rendered_includes_shadow_dom(client, fixture_server):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/shadow_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        pages = json.loads(conv)["log"]["pages"]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        dom_html = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")
        assert "shadowrootmode" in dom_html
        assert "SHADOW_CONTENT" in dom_html
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_fold_cssom_max_attempts_zero_forces_fallback(client, fixture_server):
    """max_attempts=0 deterministically skips splicing and dumps the sheets,
    without needing a flaky DOM-churn fixture."""
    from passe_partout.cssom import fold_cssom
    from passe_partout.isolated import main_frame_id
    from passe_partout.rendered import _capture_top_dom

    tab_id = await _open(client, f"{fixture_server}/cssom_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)
        rec = client._transport.app.state.registry.get(tab_id)
        fid = await main_frame_id(rec.tab)

        async def serialize():
            return await _capture_top_dom(rec.tab)

        html, fallback = await fold_cssom(rec.tab, fid, serialize, max_attempts=0)

        assert html is not None and "<html" in html.lower()
        # Skipping the splice forces the CSSOM into the fallback dump, scope-tagged.
        assert fallback, "max_attempts=0 must force a fallback dump"
        for s in fallback:
            assert set(s) == {"scope", "shadowHostSelector", "order", "css"}
            assert isinstance(s["order"], int)
        all_css = "\n".join(s["css"] for s in fallback)
        assert ".docadopt" in all_css  # document-level adopted sheet captured
        shadow = [s for s in fallback if s["scope"] == "shadow"]
        assert shadow and all(s["shadowHostSelector"] for s in shadow)
        assert any(".shadopt" in s["css"] or ".shinline" in s["css"] for s in shadow)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_rendered_cssom_fallback_record_emitted_when_forced(client, fixture_server):
    import json

    tab_id = await _open(client, f"{fixture_server}/cssom_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1&cssom_max_attempts=0")
        assert resp.status_code == 200, resp.text

        conv: dict[str, tuple[dict, bytes]] = {}
        responses: list[tuple[dict, bytes]] = []
        for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True):
            headers = dict(r.rec_headers.headers)
            body = r.content_stream().read()
            if r.rec_type == "conversion":
                conv[headers.get("WARC-Profile", "")] = (headers, body)
            elif r.rec_type == "response":
                responses.append((headers, body))

        # The rendered-targets record is still emitted alongside the fallback.
        assert any("warc-rendered-targets" in p for p in conv)
        fb_profile = next(p for p in conv if "cssom-fallback" in p)
        fb_headers, fb_body = conv[fb_profile]
        assert fb_headers["Content-Type"] == "application/json"
        main_doc_uri = f"{fixture_server}/cssom_page.html"
        main_resp = next(h for h, _ in responses if h.get("WARC-Target-URI") == main_doc_uri)
        assert fb_headers["WARC-Refers-To"] == main_resp["WARC-Record-ID"]

        payload = json.loads(fb_body)
        assert payload["version"] == "1.0"
        frames = payload["frames"]
        assert frames and "frameId" in frames[0]
        sheets = frames[0]["sheets"]
        assert sheets
        assert {"scope", "shadowHostSelector", "order", "css"} == set(sheets[0])
        assert any(s["scope"] == "document" for s in sheets)
    finally:
        await _close(client, tab_id)


@pytest.mark.asyncio
async def test_warc_rendered_no_cssom_fallback_on_happy_path(client, fixture_server):
    """Default attempts on a static page fold inline and emit NO fallback record."""
    tab_id = await _open(client, f"{fixture_server}/cssom_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        profiles = [
            r.rec_headers.get_header("WARC-Profile")
            for r in ArchiveIterator(io.BytesIO(resp.content))
            if r.rec_type == "conversion"
        ]
        assert any("warc-rendered-targets" in (p or "") for p in profiles)
        assert not any("cssom-fallback" in (p or "") for p in profiles)
    finally:
        await _close(client, tab_id)
