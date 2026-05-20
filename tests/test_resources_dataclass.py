from __future__ import annotations

import pytest

from passe_partout.resources import RequestRecord, ResourceRecord, ResourceRecorder


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


class _FakeTab:
    def __init__(self):
        self.sent = []

    async def send(self, cmd):
        self.sent.append(cmd)
        raise AssertionError("Network.getResponseBody must NOT be called when body is buffered")


@pytest.mark.asyncio
async def test_get_body_prefers_buffered_copy():
    recorder = ResourceRecorder()
    rec_body = b"hello world"
    record = ResourceRecord(
        request_id="abc",
        url="http://x/",
        status=200,
        body=rec_body,
    )
    body, was_b64 = await recorder.get_body_for(record, _FakeTab())
    assert body == rec_body
    assert was_b64 is False
