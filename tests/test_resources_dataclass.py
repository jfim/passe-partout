from __future__ import annotations

from passe_partout.resources import RequestRecord, ResourceRecord


def test_resource_record_defaults():
    r = ResourceRecord(request_id="abc", url="http://x/", status=200)
    assert r.status_text == ""
    assert r.mime_type == ""
    assert r.method == "GET"
    assert r.request_headers == {}
    assert r.response_headers == {}
    assert r.protocol == ""
    assert r.remote_ip == ""
    assert r.remote_port == 0
    assert r.request_post_data is None
    assert r.body is None
    assert r.captured_at == 0.0


def test_request_record_minimal():
    rr = RequestRecord(
        request_id="abc",
        url="http://x/",
        method="POST",
        headers={"x-test": "1"},
        post_data=b"hello",
        started_at=1.5,
    )
    assert rr.request_id == "abc"
    assert rr.headers["x-test"] == "1"
    assert rr.post_data == b"hello"
