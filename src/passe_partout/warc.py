"""Build WARC archives from a TabRecord's captured resources.

Pure function over a TabRecord + the current main-frame loader id. No CDP
interaction here — bodies are taken from ResourceRecord.body (populated in
COPY/COPY_AND_RETAIN modes); when missing, the response record is emitted
with empty payload and ``WARC-Truncated: unspecified``.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from passe_partout.resources import ResourceRecord
from passe_partout.tab_registry import TabRecord

_WARC_SPEC_URL = "http://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers_to_list(h: dict[str, str]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in h.items()]


def _select(
    resources: dict[str, ResourceRecord], current_loader_id: str
) -> list[ResourceRecord]:
    out: list[ResourceRecord] = []
    for r in resources.values():
        # Empty loader_id = worker-served response; keep across loaders.
        if r.loader_id and r.loader_id != current_loader_id:
            continue
        out.append(r)
    return out


def build_warc(rec: TabRecord, current_loader_id: str, hostname: str) -> bytes:
    writer = BufferWARCWriter(gzip=False)

    info_record = writer.create_warcinfo_record(
        filename=f"tab-{rec.id}.warc",
        info={
            "software": "passe-partout",
            "format": "WARC/1.1",
            "conformsTo": _WARC_SPEC_URL,
            "hostname": hostname,
            "isPartOf": f"tab-{rec.id}",
        },
    )
    writer.write_record(info_record)

    for r in _select(rec.resources, current_loader_id):
        warc_date = _iso(r.captured_at) if r.captured_at else _iso(rec.created_at)

        # Request record
        req_line = f"{r.method} {r.url} HTTP/1.1"
        req_headers = StatusAndHeaders(
            req_line,
            _headers_to_list(r.request_headers),
            protocol="HTTP/1.1",
            is_http_request=True,
        )
        req_post = r.request_post_data or b""
        req_record = writer.create_warc_record(
            uri=r.url,
            record_type="request",
            payload=io.BytesIO(req_post),
            length=len(req_post),
            http_headers=req_headers,
            warc_headers_dict={"WARC-Date": warc_date},
        )
        writer.write_record(req_record)

        # Response record
        status_line = f"{r.status} {r.status_text}".strip() or str(r.status)
        resp_headers = StatusAndHeaders(
            status_line,
            _headers_to_list(r.response_headers),
            protocol="HTTP/1.1",
        )
        warc_headers: dict[str, str] = {
            "WARC-Date": warc_date,
            "WARC-Concurrent-To": req_record.rec_headers.get_header("WARC-Record-ID"),
        }
        if r.body is None:
            warc_headers["WARC-Truncated"] = "unspecified"
            payload_bytes = b""
        else:
            payload_bytes = r.body
        resp_record = writer.create_warc_record(
            uri=r.url,
            record_type="response",
            payload=io.BytesIO(payload_bytes),
            length=len(payload_bytes),
            http_headers=resp_headers,
            warc_headers_dict=warc_headers,
        )
        writer.write_record(resp_record)

    return writer.get_contents()
