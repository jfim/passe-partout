# WARC DOM Snapshot Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional `?domsnapshot=1&computed_styles=...` parameter to `GET /tabs/{id}/warc` that captures a CDP DOM snapshot and embeds it as a `conversion` record, independent of the existing `?rendered=1` capture.

**Architecture:** A new `domsnapshot.py` module calls `DOMSnapshot.captureSnapshot` and serializes the raw `(documents, strings)` CDP result to a dict. `build_warc` gains parameters to emit a second `conversion` record (alongside any rendered one) that `WARC-Refers-To` the main-document response, tagged with a passe-partout DOM-snapshot profile and an `X-Passe-Partout-Computed-Styles` header. The route hoists the existing main-doc lookup so both captures share it.

**Tech Stack:** Python 3.12, FastAPI, nodriver (CDP), warcio, pytest.

---

### Task 1: DOM snapshot capture module

**Files:**
- Create: `src/passe_partout/domsnapshot.py`
- Test: `tests/test_domsnapshot.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_domsnapshot.py`:

```python
from __future__ import annotations

import pytest

from passe_partout.domsnapshot import DOM_SNAPSHOT_PROFILE, capture_dom_snapshot


class _FakeDoc:
    def __init__(self, data: dict):
        self._data = data

    def to_json(self) -> dict:
        return self._data


class _FakeTab:
    def __init__(self, result=None, raises: bool = False):
        self._result = result
        self._raises = raises

    async def send(self, _cmd):
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def test_profile_is_a_nonempty_string():
    assert isinstance(DOM_SNAPSHOT_PROFILE, str)
    assert "dom-snapshot" in DOM_SNAPSHOT_PROFILE


@pytest.mark.asyncio
async def test_capture_serializes_documents_and_strings():
    tab = _FakeTab(result=([_FakeDoc({"nodes": {"a": 1}})], ["s0", "s1"]))
    out = await capture_dom_snapshot(tab, ["display", "color"])
    assert out == {"documents": [{"nodes": {"a": 1}}], "strings": ["s0", "s1"]}


@pytest.mark.asyncio
async def test_capture_accepts_empty_computed_styles():
    tab = _FakeTab(result=([_FakeDoc({"nodes": {}})], []))
    out = await capture_dom_snapshot(tab, [])
    assert out == {"documents": [{"nodes": {}}], "strings": []}


@pytest.mark.asyncio
async def test_capture_returns_none_on_cdp_error():
    tab = _FakeTab(raises=True)
    assert await capture_dom_snapshot(tab, ["display"]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_domsnapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'passe_partout.domsnapshot'`

- [ ] **Step 3: Write minimal implementation**

Create `src/passe_partout/domsnapshot.py`:

```python
"""Capture a CDP DOM snapshot for WARC export.

Calls DOMSnapshot.captureSnapshot and returns the raw CDP result shape
(`{"documents": [...], "strings": [...]}`) unmodified. The caller embeds it as
a WARC conversion record. Capture is best-effort: any CDP failure returns None
and the caller emits no snapshot record (silent degrade, matching the rendered
capture).
"""

from __future__ import annotations

from typing import Any

import nodriver as uc

# Passe-partout-namespaced profile identifier — there is no IIPC standard for
# DOMSnapshot. It only needs to be a stable, unique identifier; it does not
# need to resolve.
DOM_SNAPSHOT_PROFILE = "urn:passe-partout:warc:dom-snapshot:1.0"


async def capture_dom_snapshot(
    tab: uc.Tab, computed_styles: list[str]
) -> dict[str, Any] | None:
    """Return the raw CDP DOM snapshot as a dict, or None on any CDP failure.

    `computed_styles` is passed verbatim to CDP as the `computedStyles` array;
    an empty list yields a structure-only snapshot (Chrome accepts it).
    """
    try:
        documents, strings = await tab.send(
            uc.cdp.dom_snapshot.capture_snapshot(computed_styles=computed_styles)
        )
    except Exception:
        return None
    return {
        "documents": [d.to_json() for d in documents],
        "strings": strings,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_domsnapshot.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/domsnapshot.py tests/test_domsnapshot.py
git commit -m "feat: DOM snapshot capture module"
```

---

### Task 2: Emit DOM snapshot conversion record in build_warc

**Files:**
- Modify: `src/passe_partout/warc.py` (signature of `build_warc` at lines 56-65; add a block after the rendered conversion block at lines 144-160)
- Test: `tests/test_warc_builder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_warc_builder.py`:

```python
def test_dom_snapshot_payload_emits_conversion_record_linked_to_main_doc():
    import json

    main = _make_record(request_id="main", loader_id="loader-A", url="http://example.com/page")
    tab = _make_tab_record([main])
    payload = {"documents": [{"nodes": {}}], "strings": ["a"]}
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        dom_snapshot_payload=payload,
        dom_snapshot_profile="urn:example:ds:1.0",
        computed_styles=["display", "color"],
        main_doc_request_id="main",
    )
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
    assert conv_headers["WARC-Profile"] == "urn:example:ds:1.0"
    assert conv_headers["Content-Type"] == "application/json"
    assert conv_headers["X-Passe-Partout-Computed-Styles"] == "display,color"
    assert json.loads(conv_body) == payload


def test_dom_snapshot_omits_styles_header_when_empty():
    main = _make_record(request_id="main", loader_id="loader-A", url="http://example.com/page")
    tab = _make_tab_record([main])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        dom_snapshot_payload={"documents": [], "strings": []},
        dom_snapshot_profile="urn:example:ds:1.0",
        computed_styles=[],
        main_doc_request_id="main",
    )
    conv = next(r for r in ArchiveIterator(io.BytesIO(blob)) if r.rec_type == "conversion")
    assert conv.rec_headers.get_header("X-Passe-Partout-Computed-Styles") is None


def test_dom_snapshot_skipped_when_main_doc_not_in_resources():
    main = _make_record(request_id="main", loader_id="loader-A")
    tab = _make_tab_record([main])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        dom_snapshot_payload={"documents": [], "strings": []},
        dom_snapshot_profile="urn:example:ds:1.0",
        computed_styles=["display"],
        main_doc_request_id="nonexistent",
    )
    types = [r.rec_type for r in ArchiveIterator(io.BytesIO(blob))]
    assert "conversion" not in types


def test_rendered_and_dom_snapshot_emit_two_conversion_records():
    main = _make_record(request_id="main", loader_id="loader-A", url="http://example.com/page")
    tab = _make_tab_record([main])
    blob = build_warc(
        tab,
        current_loader_id="loader-A",
        hostname="testhost",
        rendered_payload={"log": {"version": "1.2", "pages": []}},
        rendered_profile="http://example.com/rendered",
        dom_snapshot_payload={"documents": [], "strings": []},
        dom_snapshot_profile="urn:example:ds:1.0",
        computed_styles=["display"],
        main_doc_request_id="main",
    )
    profiles = [
        r.rec_headers.get_header("WARC-Profile")
        for r in ArchiveIterator(io.BytesIO(blob))
        if r.rec_type == "conversion"
    ]
    assert sorted(profiles) == ["http://example.com/rendered", "urn:example:ds:1.0"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_warc_builder.py -k dom_snapshot -v`
Expected: FAIL — `build_warc() got an unexpected keyword argument 'dom_snapshot_payload'`

- [ ] **Step 3: Write minimal implementation**

In `src/passe_partout/warc.py`, extend the `build_warc` signature (currently lines 56-65) by adding three parameters before the closing `)`:

```python
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
```

Then, immediately after the rendered conversion block (after line 160, `writer.write_record(conv_record)`, and before `return writer.get_contents()`), add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_warc_builder.py -v`
Expected: PASS (all tests, including the 4 new ones and the existing rendered tests)

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/warc.py tests/test_warc_builder.py
git commit -m "feat: emit DOM snapshot conversion record in build_warc"
```

---

### Task 3: Wire DOM snapshot into the /warc route

**Files:**
- Modify: `src/passe_partout/app.py` (import near line 45-48; route `get_warc` at lines 450-511)
- Test: `tests/test_warc_endpoint.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_warc_endpoint.py`:

```python
@pytest.mark.asyncio
async def test_warc_endpoint_domsnapshot_emits_conversion_record(client, fixture_server):
    import json

    tab_id = await _open(client, f"{fixture_server}/normal_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(
            f"/tabs/{tab_id}/warc?domsnapshot=1&computed_styles=display,color"
        )
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_warc_endpoint.py -k domsnapshot -v`
Expected: FAIL — `?domsnapshot=1` is ignored, so `test_warc_endpoint_domsnapshot_emits_conversion_record` finds 0 conversion records.

- [ ] **Step 3: Write the implementation**

In `src/passe_partout/app.py`, add the import next to the existing rendered import (line 45):

```python
from passe_partout.domsnapshot import DOM_SNAPSHOT_PROFILE, capture_dom_snapshot
```

Change the route signature (line 451) from:

```python
    async def get_warc(tab_id: int, rendered: bool = False):
```

to:

```python
    async def get_warc(
        tab_id: int,
        rendered: bool = False,
        domsnapshot: bool = False,
        computed_styles: str = "",
    ):
```

Replace the rendered block (current lines 481-501) — which currently reads:

```python
            rendered_payload: dict | None = None
            main_doc_request_id: str | None = None
            if rendered:
                # Identify the current main-frame document response so the
                # conversion record can WARC-Refers-To it. Match by loader +
                # resource_type — there should be exactly one per loader.
                for r in rec.resources.values():
                    if r.loader_id != current_loader:
                        continue
                    rt = (r.resource_type or "").lower()
                    if "document" in rt:
                        main_doc_request_id = r.request_id
                        break
                if main_doc_request_id is not None:
                    try:
                        page_title = getattr(rec.tab, "title", "") or ""
                    except Exception:
                        page_title = ""
                    rendered_payload = await capture_rendered_payload(
                        rec.tab, page_title=page_title
                    )
```

with (hoist the main-doc lookup so both captures share it):

```python
            rendered_payload: dict | None = None
            dom_snapshot_payload: dict | None = None
            main_doc_request_id: str | None = None
            styles_list = [s.strip() for s in computed_styles.split(",") if s.strip()]
            if rendered or domsnapshot:
                # Identify the current main-frame document response so the
                # conversion records can WARC-Refers-To it. Match by loader +
                # resource_type — there should be exactly one per loader.
                for r in rec.resources.values():
                    if r.loader_id != current_loader:
                        continue
                    rt = (r.resource_type or "").lower()
                    if "document" in rt:
                        main_doc_request_id = r.request_id
                        break
            if rendered and main_doc_request_id is not None:
                try:
                    page_title = getattr(rec.tab, "title", "") or ""
                except Exception:
                    page_title = ""
                rendered_payload = await capture_rendered_payload(
                    rec.tab, page_title=page_title
                )
            if domsnapshot and main_doc_request_id is not None:
                dom_snapshot_payload = await capture_dom_snapshot(rec.tab, styles_list)
```

Then extend the `build_warc(...)` call (current lines 502-511) to pass the new arguments:

```python
            blob = build_warc(
                rec,
                current_loader,
                socket.gethostname(),
                body_overrides=body_overrides,
                include_all_loaders=(mode == CaptureMode.COPY_AND_RETAIN),
                rendered_payload=rendered_payload,
                main_doc_request_id=main_doc_request_id,
                rendered_profile=RENDERED_TARGETS_PROFILE,
                dom_snapshot_payload=dom_snapshot_payload,
                dom_snapshot_profile=DOM_SNAPSHOT_PROFILE,
                computed_styles=styles_list,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_warc_endpoint.py -v`
Expected: PASS (all tests, including the 3 new domsnapshot tests and the existing rendered tests)

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/app.py tests/test_warc_endpoint.py
git commit -m "feat: add ?domsnapshot WARC option"
```

---

### Task 4: Documentation and full-suite verification

**Files:**
- Modify: `CLAUDE.md` (the `ResourceRecorder` / WARC architecture area)

- [ ] **Step 1: Update CLAUDE.md**

In `CLAUDE.md`, find the paragraph describing the rendered-targets WARC capture (mentions `?rendered=1` / rendered conversion record). Immediately after it, add a sentence describing the new option:

```markdown
`GET /tabs/{id}/warc?domsnapshot=1` additionally captures a CDP DOM snapshot (`DOMSnapshot.captureSnapshot`) and embeds it as a second `conversion` record that `WARC-Refers-To` the main-document response, tagged with the `urn:passe-partout:warc:dom-snapshot:1.0` profile. The optional `computed_styles` query param (comma-separated CSS property names) is passed verbatim to CDP as the `computedStyles` array and recorded in the `X-Passe-Partout-Computed-Styles` WARC header; an empty list yields a structure-only snapshot. The capture is independent of `?rendered=1` and silently degrades (no record) on CDP failure or a missing main-doc response, matching the rendered path.
```

(If no such rendered paragraph exists in CLAUDE.md, add the sentence to the `ResourceRecorder` bullet instead.)

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest`
Expected: PASS (all non-smoke tests green)

- [ ] **Step 3: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: no errors. If `ruff format --check` reports files, run `uv run ruff format .` and re-stage.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document ?domsnapshot WARC option"
```

---

## Self-Review notes

- **Spec coverage:** API params (Task 3), `domsnapshot.py` capture + profile constant (Task 1), `build_warc` conversion record with refers-to/profile/`X-Passe-Partout-Computed-Styles` (Task 2), silent-degrade + dangling-ref guard (Tasks 1-2), independence from `?rendered=1` (Task 3 hoist + both-records test), unit + route tests (all tasks), docs (Task 4). All spec sections covered.
- **Profile URI:** spec left the exact string to implementation; chosen `urn:passe-partout:warc:dom-snapshot:1.0` (stable, unique, non-resolving URN — no invented domain). Used consistently in `domsnapshot.py`, asserted via substring `dom-snapshot` in route tests, and documented in CLAUDE.md.
- **Type consistency:** `capture_dom_snapshot(tab, computed_styles: list[str]) -> dict | None`; `build_warc(..., dom_snapshot_payload, dom_snapshot_profile, computed_styles)`; route passes `dom_snapshot_payload`, `DOM_SNAPSHOT_PROFILE`, `styles_list`. Names match across tasks.
