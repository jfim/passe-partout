"""Build WARC archives from a TabRecord's captured resources.

Pure function over a TabRecord + the current main-frame loader id. No CDP
interaction here — bodies are taken from ResourceRecord.body (populated in
COPY/COPY_AND_RETAIN modes); when missing, the response record is emitted
with empty payload and ``WARC-Truncated: unspecified``.

By default only resources matching the current main-frame loader_id are
emitted (plus worker-served responses with empty loader_id). Callers in
COPY_AND_RETAIN mode pass ``include_all_loaders=True`` to dump every
retained resource on the tab, which spans prior navigations, iframes, and
other non-current loaders.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from passe_partout import __version__
from passe_partout.resources import ResourceRecord
from passe_partout.tab_registry import TabRecord

_WARC_SPEC_URL = "http://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers_to_list(h: dict[str, str]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in h.items()]


# Headers that describe wire-transfer encoding of bytes we no longer hold.
_DECODED_BODY_HEADERS = ("content-encoding", "transfer-encoding")


def _response_headers_to_list(headers: dict[str, str], stored_length: int) -> list[tuple[str, str]]:
    """Reconcile verbatim wire response headers with the decoded body we stored.

    CDP hands back bodies Chrome already decoded, but the response headers are the
    raw wire headers — so content-encoding/transfer-encoding and the compressed
    Content-Length describe bytes we no longer hold. Rename the encoding headers to
    inert ``x-orig-*`` breadcrumbs (preserve provenance without telling a replayer
    to re-decode plain bytes) and restate Content-Length as the stored length. This
    matches the ArchiveWeb.page/har2warc convention for browser-captured WARCs.
    """
    out: list[tuple[str, str]] = []
    had_content_length = False
    for k, v in headers.items():
        lk = k.lower()
        if lk in _DECODED_BODY_HEADERS:
            out.append((f"x-orig-{lk}", v))
        elif lk == "content-length":
            out.append((k, str(stored_length)))
            had_content_length = True
        else:
            out.append((k, v))
    if not had_content_length:
        out.append(("Content-Length", str(stored_length)))
    return out


def _select(
    resources: dict[str, ResourceRecord],
    current_loader_id: str,
    include_all_loaders: bool = False,
) -> list[ResourceRecord]:
    if include_all_loaders:
        return list(resources.values())
    out: list[ResourceRecord] = []
    for r in resources.values():
        # Empty loader_id = worker-served response; keep across loaders.
        if r.loader_id and r.loader_id != current_loader_id:
            continue
        out.append(r)
    return out


def build_warc(
    rec: TabRecord,
    current_loader_id: str,
    hostname: str,
    body_overrides: dict[str, bytes] | None = None,
    include_all_loaders: bool = False,
    rendered_payload: dict[str, Any] | None = None,
    main_doc_request_id: str | None = None,
    rendered_profile: str | None = None,
    dom_snapshot_payload: dict[str, Any] | None = None,
    dom_snapshot_profile: str | None = None,
    computed_styles: list[str] | None = None,
) -> bytes:
    writer = BufferWARCWriter(gzip=False)

    info_record = writer.create_warcinfo_record(
        filename=f"tab-{rec.id}.warc",
        info={
            "software": f"passe-partout/{__version__}",
            "format": "WARC/1.1",
            "conformsTo": _WARC_SPEC_URL,
            "hostname": hostname,
            "isPartOf": f"tab-{rec.id}",
            "X-Passe-Partout-Body": "decoded",
        },
    )
    writer.write_record(info_record)

    main_doc_record_id: str | None = None
    main_doc_uri: str | None = None
    main_doc_date: str | None = None
    for r in _select(rec.resources, current_loader_id, include_all_loaders=include_all_loaders):
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

        # Response record. Resolve the stored body first so the HTTP headers can be
        # made consistent with the decoded bytes we actually hold (see
        # _response_headers_to_list).
        warc_headers: dict[str, str] = {
            "WARC-Date": warc_date,
            "WARC-Concurrent-To": req_record.rec_headers.get_header("WARC-Record-ID"),
        }
        body = None
        if body_overrides is not None and r.request_id in body_overrides:
            body = body_overrides[r.request_id]
        elif r.body is not None:
            body = r.body
        if body is None:
            warc_headers["WARC-Truncated"] = "unspecified"
            payload_bytes = b""
        else:
            payload_bytes = body
        status_line = f"{r.status} {r.status_text}".strip() or str(r.status)
        resp_headers = StatusAndHeaders(
            status_line,
            _response_headers_to_list(r.response_headers, len(payload_bytes)),
            protocol="HTTP/1.1",
        )
        resp_record = writer.create_warc_record(
            uri=r.url,
            record_type="response",
            payload=io.BytesIO(payload_bytes),
            length=len(payload_bytes),
            http_headers=resp_headers,
            warc_headers_dict=warc_headers,
        )
        writer.write_record(resp_record)
        if main_doc_request_id is not None and r.request_id == main_doc_request_id:
            main_doc_record_id = resp_record.rec_headers.get_header("WARC-Record-ID")
            main_doc_uri = r.url
            main_doc_date = warc_date

    # Conversion record carrying the rendered-targets HAR-shaped JSON. Only emitted
    # when we have both a payload and a matching main-doc response to refer to —
    # otherwise the WARC-Refers-To linkage would dangle and the record would be
    # orphaned from the network capture.
    if rendered_payload is not None and main_doc_record_id is not None:
        body = json.dumps(rendered_payload).encode("utf-8")
        warc_headers = {
            "WARC-Date": main_doc_date or _iso(rec.created_at),
            "WARC-Refers-To": main_doc_record_id,
        }
        if rendered_profile:
            warc_headers["WARC-Profile"] = rendered_profile
        conv_record = writer.create_warc_record(
            uri=main_doc_uri or "",
            record_type="conversion",
            payload=io.BytesIO(body),
            length=len(body),
            warc_content_type="application/json",
            warc_headers_dict=warc_headers,
        )
        writer.write_record(conv_record)

    # DOM snapshot conversion record. Same dangling-reference guard as the
    # rendered record: only emitted when there is a main-doc response to refer to.
    if dom_snapshot_payload is not None and main_doc_record_id is not None:
        body = json.dumps(dom_snapshot_payload).encode("utf-8")
        warc_headers = {
            "WARC-Date": main_doc_date or _iso(rec.created_at),
            "WARC-Refers-To": main_doc_record_id,
        }
        if dom_snapshot_profile:
            warc_headers["WARC-Profile"] = dom_snapshot_profile
        if computed_styles:
            warc_headers["X-Passe-Partout-Computed-Styles"] = ",".join(computed_styles)
        conv_record = writer.create_warc_record(
            uri=main_doc_uri or "",
            record_type="conversion",
            payload=io.BytesIO(body),
            length=len(body),
            warc_content_type="application/json",
            warc_headers_dict=warc_headers,
        )
        writer.write_record(conv_record)

    return writer.get_contents()
