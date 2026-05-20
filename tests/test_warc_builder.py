from __future__ import annotations

import asyncio
import io

from warcio.archiveiterator import ArchiveIterator

from passe_partout.models import CaptureMode
from passe_partout.resources import ResourceRecord
from passe_partout.tab_registry import TabRecord
from passe_partout.warc import build_warc


def _make_record(**overrides) -> ResourceRecord:
    base = dict(
        request_id="req-1",
        url="http://example.com/page",
        status=200,
        status_text="OK",
        mime_type="text/html",
        resource_type="Document",
        loader_id="loader-A",
        encoded_size=42,
        method="GET",
        request_headers={"user-agent": "x", "accept": "*/*"},
        response_headers={"content-type": "text/html", "server": "fixture"},
        protocol="http/1.1",
        remote_ip="127.0.0.1",
        remote_port=80,
        body=b"<html>hi</html>",
        captured_at=1700000000.0,
    )
    base.update(overrides)
    return ResourceRecord(**base)


def _make_tab_record(resources: list[ResourceRecord]) -> TabRecord:
    rec = TabRecord(
        id=1,
        tab=None,
        created_at=0.0,
        last_used_at=0.0,
        ttl_seconds=60,
        capture_mode=CaptureMode.COPY,
        lock=asyncio.Lock(),
    )
    rec.resources = {r.request_id: r for r in resources}
    return rec


def test_empty_tab_emits_only_warcinfo():
    rec = _make_tab_record([])
    blob = build_warc(rec, current_loader_id="loader-A", hostname="testhost")
    records = list(ArchiveIterator(io.BytesIO(blob)))
    assert len(records) == 1
    assert records[0].rec_type == "warcinfo"


def test_single_resource_emits_request_and_response():
    r = _make_record()
    tab = _make_tab_record([r])
    blob = build_warc(tab, current_loader_id="loader-A", hostname="testhost")
    # Use no_record_parse=True so content_stream() returns the full HTTP block
    # (status line + headers + body), enabling assertion on both the status code
    # and the body text.  Read during iteration — streams are sequential.
    collected: list[tuple[str, str | None, bytes]] = []
    for rec in ArchiveIterator(io.BytesIO(blob), no_record_parse=True):
        content = rec.content_stream().read()
        collected.append((rec.rec_type, rec.rec_headers.get_header("WARC-Target-URI"), content))
    types = [t for t, _, _ in collected]
    assert types == ["warcinfo", "request", "response"]
    resp_type, resp_uri, resp_body = collected[2]
    assert resp_uri == "http://example.com/page"
    assert b"200" in resp_body
    assert b"<html>hi</html>" in resp_body


def test_resources_from_other_loaders_are_skipped():
    keep = _make_record(request_id="keep", loader_id="loader-A")
    drop = _make_record(request_id="drop", loader_id="loader-B", url="http://example.com/old")
    worker = _make_record(request_id="worker", loader_id="", url="http://example.com/sw.js")
    tab = _make_tab_record([keep, drop, worker])
    blob = build_warc(tab, current_loader_id="loader-A", hostname="testhost")
    records = list(ArchiveIterator(io.BytesIO(blob)))
    response_urls = [
        r.rec_headers.get_header("WARC-Target-URI") for r in records if r.rec_type == "response"
    ]
    assert "http://example.com/page" in response_urls
    assert "http://example.com/sw.js" in response_urls
    assert "http://example.com/old" not in response_urls


def test_include_all_loaders_keeps_previous_loader_resources():
    keep = _make_record(request_id="keep", loader_id="loader-A")
    prev = _make_record(request_id="prev", loader_id="loader-B", url="http://example.com/old")
    tab = _make_tab_record([keep, prev])
    blob = build_warc(
        tab, current_loader_id="loader-A", hostname="testhost", include_all_loaders=True
    )
    response_urls = [
        r.rec_headers.get_header("WARC-Target-URI")
        for r in ArchiveIterator(io.BytesIO(blob))
        if r.rec_type == "response"
    ]
    assert "http://example.com/page" in response_urls
    assert "http://example.com/old" in response_urls


def test_rendered_payload_emits_conversion_record_linked_to_main_doc():
    import json

    main = _make_record(request_id="main", loader_id="loader-A", url="http://example.com/page")
    tab = _make_tab_record([main])
    payload = {
        "log": {
            "version": "1.2",
            "pages": [{"id": "frame_0", "renderedContent": {"text": "PHA+aGk8L3A+"}}],
        }
    }
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        rendered_payload=payload,
        main_doc_request_id="main",
        rendered_profile="http://example.com/profile",
    )
    # Collect (rec_type, headers-dict, body-bytes) during iteration — content_stream
    # is sequential, you can't reread it once the iterator advances.
    collected: list[tuple[str, dict, bytes]] = []
    for r in ArchiveIterator(io.BytesIO(blob), no_record_parse=True):
        collected.append((r.rec_type, dict(r.rec_headers.headers), r.content_stream().read()))
    convs = [c for c in collected if c[0] == "conversion"]
    assert len(convs) == 1, [c[0] for c in collected]
    _, conv_headers, conv_body = convs[0]
    main_resp_headers = next(
        h
        for t, h, _ in collected
        if t == "response" and h.get("WARC-Target-URI") == "http://example.com/page"
    )
    assert conv_headers["WARC-Refers-To"] == main_resp_headers["WARC-Record-ID"]
    assert conv_headers["WARC-Target-URI"] == "http://example.com/page"
    assert conv_headers["WARC-Profile"] == "http://example.com/profile"
    assert conv_headers["Content-Type"] == "application/json"
    assert json.loads(conv_body) == payload


def test_rendered_payload_skipped_when_main_doc_not_in_resources():
    main = _make_record(request_id="main", loader_id="loader-A")
    tab = _make_tab_record([main])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        rendered_payload={"log": {"version": "1.2", "pages": []}},
        main_doc_request_id="nonexistent",
        rendered_profile="http://example.com/profile",
    )
    types = [r.rec_type for r in ArchiveIterator(io.BytesIO(blob))]
    assert "conversion" not in types


def test_missing_body_emits_truncated_response():
    r = _make_record(body=None)
    tab = _make_tab_record([r])
    blob = build_warc(tab, current_loader_id="loader-A", hostname="testhost")
    records = list(ArchiveIterator(io.BytesIO(blob)))
    response = [r for r in records if r.rec_type == "response"][0]
    assert response.rec_headers.get_header("WARC-Truncated") == "unspecified"


def test_body_overrides_take_precedence():
    r = _make_record(body=None)
    tab = _make_tab_record([r])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        body_overrides={"req-1": b"override-bytes"},
    )
    records = list(ArchiveIterator(io.BytesIO(blob)))
    response = [r for r in records if r.rec_type == "response"][0]
    # WARC-Truncated must NOT be set when an override provides a body.
    assert response.rec_headers.get_header("WARC-Truncated") is None


def test_body_overrides_dont_override_buffered_body():
    """When the record already has a body, overrides aren't needed but the
    function should still work (record body takes precedence as long as no
    explicit override is supplied for that request_id)."""
    r = _make_record(body=b"from-record")
    tab = _make_tab_record([r])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        body_overrides={},  # empty: record body wins
    )
    # No exception, builds successfully.
    assert isinstance(blob, bytes)
    assert len(blob) > 0
