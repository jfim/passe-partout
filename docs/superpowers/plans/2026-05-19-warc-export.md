# WARC Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /tabs/{tab_id}/warc` that returns a WARC archive of the current page's network traffic, plus a per-tab `capture_mode` controlling whether request/response bodies are eagerly buffered and retained across navigation.

**Architecture:** Extend `ResourceRecord` to carry full request/response headers and an optional buffered body. The existing `ResourceRecorder` gains a `requestWillBeSent` handler, expanded `responseReceived` handling, and (in `COPY`/`COPY_AND_RETAIN` modes) eager body capture on `loadingFinished`. A new `warc.py` module builds WARC bytes from a `TabRecord` using `warcio`, exposed via a new route in `app.py`.

**Tech Stack:** Python 3.12, FastAPI, nodriver (CDP), `warcio` (new dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-05-19-warc-design.md`

## File Structure

- **Modify** `pyproject.toml` — add `warcio` to runtime deps.
- **Modify** `src/passe_partout/models.py` — add `CaptureMode` enum; add `capture_mode` to `CreateTabRequest`.
- **Modify** `src/passe_partout/resources.py` — expand `ResourceRecord`; add `RequestRecord` (transient pre-response state); add `requestWillBeSent` handler; expand `responseReceived` handler; eager body capture; mode-aware pruning; buffered-body preference in `get_body()`.
- **Modify** `src/passe_partout/tab_registry.py` — add `capture_mode` to `TabRecord`; thread through `register()`.
- **Modify** `src/passe_partout/app.py` — pass `req.capture_mode` through `create_tab`; add `/tabs/{tab_id}/warc` route; import the new WARC builder.
- **Create** `src/passe_partout/warc.py` — `build_warc(rec, current_loader_id, hostname) -> bytes` using `warcio`.
- **Create** `tests/fixtures/warc_page.html` — small page with an `<img>` and an inline `fetch('/data.json')` to give the recorder multiple subresources.
- **Create** `tests/test_capture_modes.py` — per-mode behavior of `ResourceRecorder`.
- **Create** `tests/test_warc.py` — end-to-end WARC export.

---

### Task 1: Add `warcio` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add warcio to runtime dependencies**

In `pyproject.toml`, in the `[project].dependencies` list, add `"warcio>=1.7"`. After the change the block should read:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "nodriver>=0.38",
    "pydantic>=2.7",
    "warcio>=1.7",
]
```

- [ ] **Step 2: Resolve the lockfile**

Run: `uv sync`
Expected: exit 0; `uv.lock` is updated to include warcio and its transitive deps (`six`).

- [ ] **Step 3: Smoke-import warcio**

Run: `uv run python -c "from warcio.warcwriter import WARCWriter; from warcio.statusandheaders import StatusAndHeaders; print('ok')"`
Expected: prints `ok` and exits 0.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add warcio for WARC export"
```

---

### Task 2: `CaptureMode` enum and `CreateTabRequest` field

**Files:**
- Modify: `src/passe_partout/models.py`
- Test: `tests/test_models.py` (create if missing)

- [ ] **Step 1: Write the failing tests**

Create or extend `tests/test_models.py`:

```python
from __future__ import annotations

import pytest

from passe_partout.models import CaptureMode, CreateTabRequest


def test_capture_mode_values():
    assert CaptureMode.NO_COPY.value == "no_copy"
    assert CaptureMode.COPY.value == "copy"
    assert CaptureMode.COPY_AND_RETAIN.value == "copy_and_retain"


def test_create_tab_request_defaults_to_no_copy():
    req = CreateTabRequest(url="http://example.com/")
    assert req.capture_mode is CaptureMode.NO_COPY


def test_create_tab_request_accepts_each_mode():
    for value, expected in [
        ("no_copy", CaptureMode.NO_COPY),
        ("copy", CaptureMode.COPY),
        ("copy_and_retain", CaptureMode.COPY_AND_RETAIN),
    ]:
        req = CreateTabRequest(url="http://example.com/", capture_mode=value)
        assert req.capture_mode is expected


def test_create_tab_request_rejects_unknown_mode():
    with pytest.raises(ValueError):
        CreateTabRequest(url="http://example.com/", capture_mode="bogus")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: import-error or ImportError for `CaptureMode`.

- [ ] **Step 3: Implement the enum and field**

In `src/passe_partout/models.py`, add at the top (after the existing imports):

```python
from enum import Enum
```

Then add the enum near the other constants (above `Cookie`):

```python
class CaptureMode(str, Enum):
    """Per-tab network capture behavior.

    NO_COPY (default): no body buffering; bodies fetched lazily from Chrome
    and pruned on main-frame navigation. Today's behavior.

    COPY: eagerly buffer request/response headers + bodies on loadingFinished.
    Still pruned on main-frame navigation.

    COPY_AND_RETAIN: like COPY, but resources are retained across navigations
    for the lifetime of the tab. Caller accepts unbounded growth.
    """

    NO_COPY = "no_copy"
    COPY = "copy"
    COPY_AND_RETAIN = "copy_and_retain"
```

Update `CreateTabRequest`:

```python
class CreateTabRequest(BaseModel):
    url: URLStr
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None
    capture_mode: CaptureMode = CaptureMode.NO_COPY
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/models.py tests/test_models.py
git commit -m "models: add CaptureMode enum and CreateTabRequest.capture_mode"
```

---

### Task 3: Expand `ResourceRecord` and add `RequestRecord`

This task extends the dataclass shape without changing behavior. Existing callers continue to work because every new field has a default.

**Files:**
- Modify: `src/passe_partout/resources.py`
- Test: `tests/test_resources_dataclass.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_resources_dataclass.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_resources_dataclass.py -v`
Expected: ImportError for `RequestRecord` or AttributeError on new fields.

- [ ] **Step 3: Update `resources.py` dataclasses**

In `src/passe_partout/resources.py`, replace the existing imports and `ResourceRecord` definition with:

```python
"""Per-tab HTTP response metadata recorder.

Subscribes to CDP Network events to track every request/response Chrome sees.
In NO_COPY mode bodies are not copied (fetched on demand via Network.getResponseBody);
in COPY/COPY_AND_RETAIN modes bodies are pulled eagerly on loadingFinished and
stashed on the record so WARC export and /resources/{id} remain reliable after
Chrome would have evicted them.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import nodriver as uc

from passe_partout.models import CaptureMode


@dataclass
class RequestRecord:
    """Transient per-request state captured at Network.requestWillBeSent.

    Held in a tab-scoped dict until the matching Network.responseReceived fires,
    at which point the relevant fields are folded into the ResourceRecord. Kept
    around after that too for the WARC writer, which needs the original request
    line and headers.
    """

    request_id: str
    url: str
    method: str
    headers: dict[str, str]
    post_data: bytes | None
    started_at: float


@dataclass
class ResourceRecord:
    """Per-tab metadata for a single Network.responseReceived event.

    In NO_COPY mode `body` is None and the caller fetches it on demand via
    `Network.getResponseBody`; that may fail if Chrome has evicted it. In
    COPY/COPY_AND_RETAIN modes `body` is populated eagerly on loadingFinished
    (decoded from Chrome's base64 wrapping for binary responses) and survives
    until the record itself is pruned.
    """

    request_id: str
    url: str
    status: int
    status_text: str = ""
    mime_type: str = ""
    resource_type: str = ""
    loader_id: str = ""  # main-frame loader at the time of the response
    encoded_size: int = 0  # populated by Network.loadingFinished

    # Always populated when the matching requestWillBeSent was seen.
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
    remote_ip: str = ""
    remote_port: int = 0
    request_post_data: bytes | None = None

    # Populated only in COPY / COPY_AND_RETAIN modes. Already decoded — Chrome's
    # base64 wrapping for binary responses is unwrapped before storage.
    body: bytes | None = None

    # Wall-clock time of Network.responseReceived. Used as WARC-Date.
    captured_at: float = 0.0
```

(The `ResourceRecorder` class below this stays untouched for now.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_resources_dataclass.py tests/test_models.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full suite to confirm no regression from the new fields**

Run: `uv run pytest`
Expected: same pass/fail counts as before (new fields are additive with defaults).

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/resources.py tests/test_resources_dataclass.py
git commit -m "resources: extend ResourceRecord, add RequestRecord"
```

---

### Task 4: Plumb `capture_mode` through `TabRegistry`

**Files:**
- Modify: `src/passe_partout/tab_registry.py`
- Test: `tests/test_tab_registry.py` (create if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tab_registry.py`:

```python
from __future__ import annotations

from passe_partout.models import CaptureMode
from passe_partout.tab_registry import TabRegistry


class _DummyTab:
    url = "about:blank"


def test_register_defaults_capture_mode_to_no_copy():
    reg = TabRegistry()
    rec = reg.register(tab=_DummyTab(), ttl_seconds=60)
    assert rec.capture_mode is CaptureMode.NO_COPY


def test_register_accepts_explicit_capture_mode():
    reg = TabRegistry()
    rec = reg.register(
        tab=_DummyTab(), ttl_seconds=60, capture_mode=CaptureMode.COPY_AND_RETAIN
    )
    assert rec.capture_mode is CaptureMode.COPY_AND_RETAIN
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_tab_registry.py -v`
Expected: AttributeError on `rec.capture_mode` or TypeError on the kwarg.

- [ ] **Step 3: Update `TabRecord` and `register()`**

In `src/passe_partout/tab_registry.py`, add the import and field, and accept the kwarg in `register()`:

```python
from passe_partout.downloads import DownloadRecord
from passe_partout.models import CaptureMode
from passe_partout.resources import ResourceRecord


@dataclass
class TabRecord:
    id: int
    tab: Any
    created_at: float
    last_used_at: float
    ttl_seconds: int
    capture_mode: CaptureMode = CaptureMode.NO_COPY
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    nav: Any = None
    downloads: dict[str, DownloadRecord] = field(default_factory=dict)
    resources: dict[str, ResourceRecord] = field(default_factory=dict)
    main_frame_id: str | None = None
```

Update `register()`:

```python
    def register(
        self,
        tab: Any,
        ttl_seconds: int,
        capture_mode: CaptureMode = CaptureMode.NO_COPY,
    ) -> TabRecord:
        now = time.time()
        rec = TabRecord(
            id=self._next_id,
            tab=tab,
            created_at=now,
            last_used_at=now,
            ttl_seconds=ttl_seconds,
            capture_mode=capture_mode,
        )
        self._records[rec.id] = rec
        self._next_id += 1
        return rec
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_tab_registry.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: no regressions (existing `register(tab=..., ttl_seconds=...)` calls still work because `capture_mode` defaults).

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/tab_registry.py tests/test_tab_registry.py
git commit -m "tab_registry: thread capture_mode through TabRecord/register"
```

---

### Task 5: Recorder accepts mode; `get_body` prefers buffered body

This task expands the recorder API and changes `get_body()` lookup, but does NOT yet add the new CDP event handlers or eager capture. Those land in Task 6 and Task 7.

**Files:**
- Modify: `src/passe_partout/resources.py`
- Test: `tests/test_resources_dataclass.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resources_dataclass.py`:

```python
import pytest

from passe_partout.resources import ResourceRecorder


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_resources_dataclass.py::test_get_body_prefers_buffered_copy -v`
Expected: AttributeError on `recorder.get_body_for`.

- [ ] **Step 3: Implement `get_body_for` and accept mode in `attach_tab`**

In `src/passe_partout/resources.py`, replace `ResourceRecorder.__init__`, `attach_tab` signature, and `get_body`, leaving the existing handlers in place for now. The class becomes:

```python
class ResourceRecorder:
    """Tracks Chrome Network responses per tab and fetches bodies on demand.

    See module docstring for the capture-mode contract.
    """

    def __init__(self) -> None:
        self._registry = None  # injected by app.py
        # tab_id -> current main-frame loader_id (str). Empty until first frameNavigated.
        self._current_loader: dict[int, str] = {}
        # tab_id -> CaptureMode chosen at attach_tab time.
        self._mode: dict[int, CaptureMode] = {}

    def set_registry(self, registry) -> None:
        self._registry = registry

    def mode_for(self, tab_id: int) -> CaptureMode:
        return self._mode.get(tab_id, CaptureMode.NO_COPY)

    def current_loader(self, tab_id: int) -> str:
        return self._current_loader.get(tab_id, "")

    async def attach_tab(
        self,
        tab_id: int,
        tab: uc.Tab,
        capture_mode: CaptureMode = CaptureMode.NO_COPY,
    ) -> None:
        self._mode[tab_id] = capture_mode
        await tab.send(uc.cdp.network.enable())

        # ... existing _on_response / _on_finished / _on_frame_navigated handlers
        # remain unchanged in this task; they are updated in Tasks 6 and 7.
        def _on_response(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            rec.resources[str(evt.request_id)] = ResourceRecord(
                request_id=str(evt.request_id),
                url=evt.response.url,
                status=int(evt.response.status),
                mime_type=evt.response.mime_type or "",
                resource_type=str(evt.type_),
                loader_id=str(evt.loader_id) if evt.loader_id is not None else "",
            )

        def _on_finished(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            r = rec.resources.get(str(evt.request_id))
            if r is not None:
                r.encoded_size = int(evt.encoded_data_length)

        def _on_frame_navigated(evt) -> None:
            if evt.frame.parent_id is not None:
                return
            new_loader = str(evt.frame.loader_id) if evt.frame.loader_id is not None else ""
            self._current_loader[tab_id] = new_loader
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            stale = [
                rid for rid, r in rec.resources.items() if r.loader_id and r.loader_id != new_loader
            ]
            for rid in stale:
                del rec.resources[rid]

        tab.add_handler(uc.cdp.network.ResponseReceived, _on_response)
        tab.add_handler(uc.cdp.network.LoadingFinished, _on_finished)
        tab.add_handler(uc.cdp.page.FrameNavigated, _on_frame_navigated)

    async def get_body(self, tab: uc.Tab, request_id: str) -> tuple[bytes, bool]:
        """Returns (body_bytes, was_base64).

        Always goes to Chrome — for buffered-body preference use `get_body_for`.
        """
        body, base64_encoded = await tab.send(
            uc.cdp.network.get_response_body(request_id=uc.cdp.network.RequestId(request_id))
        )
        if base64_encoded:
            return base64.b64decode(body), True
        return body.encode("utf-8"), False

    async def get_body_for(
        self, record: ResourceRecord, tab: uc.Tab
    ) -> tuple[bytes, bool]:
        """Returns (body_bytes, was_base64), preferring the buffered copy."""
        if record.body is not None:
            return record.body, False
        return await self.get_body(tab, record.request_id)

    def detach_tab(self, tab_id: int) -> None:
        self._current_loader.pop(tab_id, None)
        self._mode.pop(tab_id, None)
```

- [ ] **Step 4: Update `/tabs/{id}/resources/{request_id}` to use `get_body_for`**

In `src/passe_partout/app.py`, find `get_resource_body` (~line 420) and change the body-fetch call:

```python
        async with rec.lock:
            try:
                body, _ = await app.state.recorder.get_body_for(meta, rec.tab)
            except Exception as e:
```

- [ ] **Step 5: Add the asyncio marker if missing**

Confirm `pyproject.toml`'s `[tool.pytest.ini_options]` has `asyncio_mode = "auto"` (or similar). If not, add `@pytest_asyncio.fixture` / `@pytest.mark.asyncio` to the new test as needed. Check with:

Run: `grep -n asyncio_mode pyproject.toml`

Expected: prints a line containing `asyncio_mode = "auto"`. If it doesn't, add the marker explicitly to the test (already done in Step 1).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_resources_dataclass.py tests/test_tab_registry.py tests/test_models.py -v`
Expected: all pass.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: no regressions; existing `attach_tab(tab_id, tab)` callers still work (mode defaults).

- [ ] **Step 8: Commit**

```bash
git add src/passe_partout/resources.py src/passe_partout/app.py tests/test_resources_dataclass.py
git commit -m "resources: ResourceRecorder accepts capture_mode; get_body_for prefers buffer"
```

---

### Task 6: Capture request and richer response metadata

Add `Network.requestWillBeSent` handling and expand `Network.responseReceived` to populate headers, status text, protocol, remote IP, captured_at, and the request method/post body. This task does NOT yet do eager body capture or change pruning.

**Files:**
- Modify: `src/passe_partout/resources.py`
- Create: `tests/fixtures/warc_page.html`
- Create: `tests/test_capture_modes.py`
- Modify: `tests/conftest.py` (route `/warc_page.html` is already served by the existing wildcard `/{name}.html`; only `fetch('/data.json')` is needed — `/data.json` already exists)

- [ ] **Step 1: Create the fixture page**

Create `tests/fixtures/warc_page.html`:

```html
<!doctype html>
<html>
  <head><title>warc page</title></head>
  <body>
    <h1>warc page</h1>
    <img id="img" src="/sample.png" width="1" height="1">
    <pre id="out">loading</pre>
    <script>
      fetch('/data.json')
        .then(r => r.text())
        .then(t => { document.getElementById('out').textContent = t; });
    </script>
  </body>
</html>
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_capture_modes.py`:

```python
from __future__ import annotations

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
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.NO_COPY
    )
    try:
        # Give the in-page fetch() a moment to complete.
        import asyncio as _a
        await _a.sleep(0.5)

        # Reach into the registry via the app to inspect ResourceRecord internals.
        # We don't expose response_headers via the public REST API, so testing
        # via the in-memory transport gives us direct access through app state.
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        assert rec is not None
        # The main document, the PNG, and the JSON should all be present.
        urls = {r.url for r in rec.resources.values()}
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/sample.png") for u in urls)
        assert any(u.endswith("/data.json") for u in urls)

        for r in rec.resources.values():
            # Every record should have a method and at least one response header.
            assert r.method in ("GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS")
            assert r.response_headers, f"no response headers captured for {r.url}"
            assert r.captured_at > 0.0
    finally:
        await _close_tab(client, tab_id)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_capture_modes.py::test_request_and_response_headers_are_captured -v`
Expected: AssertionError — `response_headers` is empty (existing recorder doesn't populate it).

- [ ] **Step 4: Add the new event handlers**

In `src/passe_partout/resources.py`, inside `ResourceRecorder.attach_tab`, replace the existing `_on_response` and add a `_on_request` handler. The relevant block now reads:

```python
        # tab_id -> pending RequestRecord by request_id, keyed pre-response.
        pending: dict[str, RequestRecord] = {}

        def _on_request(evt) -> None:
            req = evt.request
            method = str(getattr(req, "method", "GET") or "GET")
            headers = dict(getattr(req, "headers", {}) or {})
            post_data = None
            raw_post = getattr(req, "post_data", None)
            if raw_post is not None:
                if isinstance(raw_post, bytes):
                    post_data = raw_post
                else:
                    post_data = str(raw_post).encode("utf-8")
            pending[str(evt.request_id)] = RequestRecord(
                request_id=str(evt.request_id),
                url=str(getattr(req, "url", "") or ""),
                method=method,
                headers=headers,
                post_data=post_data,
                started_at=time.time(),
            )

        def _on_response(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            resp = evt.response
            req_meta = pending.pop(str(evt.request_id), None)
            record = ResourceRecord(
                request_id=str(evt.request_id),
                url=resp.url,
                status=int(resp.status),
                status_text=str(getattr(resp, "status_text", "") or ""),
                mime_type=resp.mime_type or "",
                resource_type=str(evt.type_),
                loader_id=str(evt.loader_id) if evt.loader_id is not None else "",
                response_headers=dict(getattr(resp, "headers", {}) or {}),
                protocol=str(getattr(resp, "protocol", "") or ""),
                remote_ip=str(getattr(resp, "remote_ip_address", "") or ""),
                remote_port=int(getattr(resp, "remote_port", 0) or 0),
                captured_at=time.time(),
            )
            if req_meta is not None:
                record.method = req_meta.method
                record.request_headers = req_meta.headers
                record.request_post_data = req_meta.post_data
            rec.resources[str(evt.request_id)] = record
```

Then below the existing `tab.add_handler(...)` calls, add:

```python
        tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_capture_modes.py::test_request_and_response_headers_are_captured -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: no regressions; existing `/resources` and `/resources/{id}` tests still pass (the new fields are additive).

- [ ] **Step 7: Commit**

```bash
git add src/passe_partout/resources.py tests/fixtures/warc_page.html tests/test_capture_modes.py
git commit -m "resources: capture request method/headers/post body and richer response metadata"
```

---

### Task 7: Eager body capture and mode-aware pruning

**Files:**
- Modify: `src/passe_partout/resources.py`
- Test: `tests/test_capture_modes.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture_modes.py`:

```python
@pytest.mark.asyncio
async def test_no_copy_does_not_buffer_bodies(client, fixture_server):
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.NO_COPY
    )
    try:
        import asyncio as _a
        await _a.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        # No body should be buffered in NO_COPY mode.
        assert all(r.body is None for r in rec.resources.values())
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_buffers_bodies(client, fixture_server):
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.COPY
    )
    try:
        import asyncio as _a
        await _a.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        # At minimum the JSON body should be buffered (it's small + text).
        json_records = [
            r for r in rec.resources.values() if r.url.endswith("/data.json")
        ]
        assert json_records, "no /data.json record found"
        assert json_records[0].body == b'{"hello":"world"}'
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_prunes_on_navigation(client, fixture_server):
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.COPY
    )
    try:
        import asyncio as _a
        await _a.sleep(0.5)
        # Navigate to a second page.
        nav = await client.post(
            f"/tabs/{tab_id}/goto", json={"url": f"{fixture_server}/static.html"}
        )
        assert nav.status_code == 200, nav.text
        await _a.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        urls = {r.url for r in rec.resources.values()}
        # Old page's resources should be pruned.
        assert not any(u.endswith("/warc_page.html") for u in urls)
        assert not any(u.endswith("/sample.png") for u in urls)
    finally:
        await _close_tab(client, tab_id)


@pytest.mark.asyncio
async def test_copy_and_retain_keeps_resources_across_navigation(
    client, fixture_server
):
    tab_id = await _open_tab(
        client, f"{fixture_server}/warc_page.html", CaptureMode.COPY_AND_RETAIN
    )
    try:
        import asyncio as _a
        await _a.sleep(0.5)
        nav = await client.post(
            f"/tabs/{tab_id}/goto", json={"url": f"{fixture_server}/static.html"}
        )
        assert nav.status_code == 200, nav.text
        await _a.sleep(0.5)
        app = client._transport.app
        rec = app.state.registry.get(tab_id)
        urls = {r.url for r in rec.resources.values()}
        # Both pages' resources should be present.
        assert any(u.endswith("/warc_page.html") for u in urls)
        assert any(u.endswith("/static.html") for u in urls)
    finally:
        await _close_tab(client, tab_id)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_capture_modes.py -v`
Expected: `test_no_copy_does_not_buffer_bodies` passes (no buffering yet); `test_copy_buffers_bodies` fails (body is None); `test_copy_and_retain_keeps_resources_across_navigation` fails (pruning still runs).

- [ ] **Step 3: Add eager body capture and mode-aware pruning**

In `src/passe_partout/resources.py`, update the `_on_finished` and `_on_frame_navigated` handlers inside `attach_tab`. Replace them with:

```python
        async def _capture_body(tab_id_inner: int, request_id_str: str) -> None:
            rec_inner = self._registry.get(tab_id_inner) if self._registry else None
            if rec_inner is None:
                return
            record = rec_inner.resources.get(request_id_str)
            if record is None or record.body is not None:
                return
            try:
                body, _ = await self.get_body(tab, request_id_str)
            except Exception:
                # Opaque cross-origin, too large, already evicted, etc. Leave body=None;
                # WARC writer will emit headers with WARC-Truncated: unspecified.
                return
            # Re-check; the record may have been pruned while we awaited.
            record = rec_inner.resources.get(request_id_str)
            if record is not None:
                record.body = body

        def _on_finished(evt) -> None:
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            r = rec.resources.get(str(evt.request_id))
            if r is not None:
                r.encoded_size = int(evt.encoded_data_length)
            if self._mode.get(tab_id, CaptureMode.NO_COPY) in (
                CaptureMode.COPY,
                CaptureMode.COPY_AND_RETAIN,
            ):
                # Schedule the body fetch on the running loop; we can't await
                # inside a CDP handler callback because nodriver invokes them
                # synchronously from its event pump.
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(_capture_body(tab_id, str(evt.request_id)))

        def _on_frame_navigated(evt) -> None:
            if evt.frame.parent_id is not None:
                return
            new_loader = str(evt.frame.loader_id) if evt.frame.loader_id is not None else ""
            self._current_loader[tab_id] = new_loader
            if self._mode.get(tab_id, CaptureMode.NO_COPY) == CaptureMode.COPY_AND_RETAIN:
                return  # retention mode: keep all loaders' entries forever
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            stale = [
                rid for rid, r in rec.resources.items() if r.loader_id and r.loader_id != new_loader
            ]
            for rid in stale:
                del rec.resources[rid]
```

- [ ] **Step 4: Run the capture-mode tests**

Run: `uv run pytest tests/test_capture_modes.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/resources.py tests/test_capture_modes.py
git commit -m "resources: eager body capture in COPY modes; skip prune in COPY_AND_RETAIN"
```

---

### Task 8: Thread `capture_mode` through `create_tab`

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_capture_modes.py` (extend) — already verifies end-to-end via the REST API, so adding one assertion that the registry stores the mode is enough.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_capture_modes.py`:

```python
@pytest.mark.asyncio
async def test_create_tab_propagates_capture_mode_to_registry(
    client, fixture_server
):
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_capture_modes.py::test_create_tab_propagates_capture_mode_to_registry -v`
Expected: `rec.capture_mode is CaptureMode.NO_COPY` (default), assertion fails.

- [ ] **Step 3: Pass `capture_mode` through `create_tab`**

In `src/passe_partout/app.py`, locate the `create_tab` handler. Find the lines:

```python
            ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_tab_close_seconds
            rec = registry.register(tab=tab, ttl_seconds=ttl)
            await coord.attach_tab(rec.id, tab)
            await app.state.recorder.attach_tab(rec.id, tab)
```

Replace with:

```python
            ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_tab_close_seconds
            rec = registry.register(
                tab=tab, ttl_seconds=ttl, capture_mode=req.capture_mode
            )
            await coord.attach_tab(rec.id, tab)
            await app.state.recorder.attach_tab(rec.id, tab, req.capture_mode)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_capture_modes.py::test_create_tab_propagates_capture_mode_to_registry -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/app.py tests/test_capture_modes.py
git commit -m "app: propagate capture_mode from CreateTabRequest into recorder/registry"
```

---

### Task 9: WARC builder module

A pure function that takes a `TabRecord` + current loader id + hostname and returns WARC bytes. Unit-testable without launching Chromium.

**Files:**
- Create: `src/passe_partout/warc.py`
- Create: `tests/test_warc_builder.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_warc_builder.py`:

```python
from __future__ import annotations

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
    import asyncio
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
    records = list(ArchiveIterator(io.BytesIO(blob)))
    types = [rec.rec_type for rec in records]
    assert types == ["warcinfo", "request", "response"]
    response = records[2]
    assert response.rec_headers.get_header("WARC-Target-URI") == "http://example.com/page"
    body = response.content_stream().read()
    # Status line + headers + body.
    assert b"200" in body
    assert b"<html>hi</html>" in body


def test_resources_from_other_loaders_are_skipped():
    keep = _make_record(request_id="keep", loader_id="loader-A")
    drop = _make_record(request_id="drop", loader_id="loader-B", url="http://example.com/old")
    worker = _make_record(request_id="worker", loader_id="", url="http://example.com/sw.js")
    tab = _make_tab_record([keep, drop, worker])
    blob = build_warc(tab, current_loader_id="loader-A", hostname="testhost")
    records = list(ArchiveIterator(io.BytesIO(blob)))
    response_urls = [
        r.rec_headers.get_header("WARC-Target-URI")
        for r in records
        if r.rec_type == "response"
    ]
    assert "http://example.com/page" in response_urls  # keep
    assert "http://example.com/sw.js" in response_urls  # worker (empty loader_id)
    assert "http://example.com/old" not in response_urls  # drop


def test_missing_body_emits_truncated_response():
    r = _make_record(body=None)
    tab = _make_tab_record([r])
    blob = build_warc(tab, current_loader_id="loader-A", hostname="testhost")
    records = list(ArchiveIterator(io.BytesIO(blob)))
    response = [r for r in records if r.rec_type == "response"][0]
    assert response.rec_headers.get_header("WARC-Truncated") == "unspecified"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_warc_builder.py -v`
Expected: ImportError for `passe_partout.warc`.

- [ ] **Step 3: Implement `warc.py`**

Create `src/passe_partout/warc.py`:

```python
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


def build_warc(
    rec: TabRecord, current_loader_id: str, hostname: str
) -> bytes:
    buf = io.BytesIO()
    writer = BufferWARCWriter(gzip=False)

    info_payload = (
        f"software: passe-partout\r\n"
        f"format: WARC/1.1\r\n"
        f"conformsTo: {_WARC_SPEC_URL}\r\n"
        f"hostname: {hostname}\r\n"
        f"isPartOf: tab-{rec.id}\r\n"
    ).encode("utf-8")
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
        req_headers = StatusAndHeaders(
            f"{r.method} {r.url} HTTP/1.1",
            _headers_to_list(r.request_headers),
            protocol="HTTP/1.1",
            is_http_request=True,
        )
        req_body = io.BytesIO(r.request_post_data or b"")
        req_record = writer.create_warc_record(
            uri=r.url,
            record_type="request",
            payload=req_body,
            length=len(r.request_post_data or b""),
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
            payload = io.BytesIO(b"")
            length = 0
        else:
            payload = io.BytesIO(r.body)
            length = len(r.body)
        resp_record = writer.create_warc_record(
            uri=r.url,
            record_type="response",
            payload=payload,
            length=length,
            http_headers=resp_headers,
            warc_headers_dict=warc_headers,
        )
        writer.write_record(resp_record)

    buf.write(writer.get_contents())
    return buf.getvalue()
```

- [ ] **Step 4: Run the builder tests**

Run: `uv run pytest tests/test_warc_builder.py -v`
Expected: 4 passed.

If a test fails because `BufferWARCWriter.create_warcinfo_record` has a different signature in your warcio version, drop the `filename=` kwarg and pass only `info={...}`. Re-run.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/warc.py tests/test_warc_builder.py
git commit -m "warc: pure builder that turns a TabRecord into a WARC byte blob"
```

---

### Task 10: `/tabs/{tab_id}/warc` endpoint

**Files:**
- Modify: `src/passe_partout/app.py`
- Create: `tests/test_warc_endpoint.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_warc_endpoint.py`:

```python
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
async def test_warc_endpoint_returns_archive_with_all_resources(
    client, fixture_server
):
    tab_id = await _open(client, f"{fixture_server}/warc_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)  # let the in-page fetch complete
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/warc")
        assert "attachment" in resp.headers["content-disposition"]

        records = list(ArchiveIterator(io.BytesIO(resp.content)))
        types = [r.rec_type for r in records]
        # warcinfo + (request, response) per resource. At minimum we expect the
        # HTML doc + sample.png + data.json = 3 pairs.
        assert types[0] == "warcinfo"
        n_responses = sum(1 for t in types if t == "response")
        n_requests = sum(1 for t in types if t == "request")
        assert n_responses == n_requests
        assert n_responses >= 3

        urls = {
            r.rec_headers.get_header("WARC-Target-URI")
            for r in records
            if r.rec_type == "response"
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
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc")
        assert resp.status_code == 200, resp.text
        records = list(ArchiveIterator(io.BytesIO(resp.content)))
        # At least warcinfo + one request/response pair for the HTML doc.
        assert any(r.rec_type == "response" for r in records)
    finally:
        await _close(client, tab_id)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_warc_endpoint.py -v`
Expected: 404 on `/tabs/{id}/warc` for all three tests (route not yet defined).

- [ ] **Step 3: Implement the route**

In `src/passe_partout/app.py`:

Add to the imports (top of file):

```python
import socket

from passe_partout.warc import build_warc
```

Add the route, placing it next to the other `/tabs/{tab_id}/resources/...` routes (after `get_resource_body`, around line 444):

```python
    @app.get("/tabs/{tab_id}/warc", summary="WARC archive of the current page's resources")
    async def get_warc(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        recorder = app.state.recorder
        current_loader = recorder.current_loader(tab_id)
        async with rec.lock:
            # In NO_COPY mode (or for any resource whose body isn't buffered)
            # fetch live bodies from Chrome so the WARC has payloads where it can.
            for r in list(rec.resources.values()):
                if r.body is not None:
                    continue
                if r.loader_id and r.loader_id != current_loader:
                    continue
                try:
                    body, _ = await recorder.get_body(rec.tab, r.request_id)
                    r.body = body
                except Exception:
                    pass  # leaves body=None → WARC-Truncated
            blob = build_warc(rec, current_loader, socket.gethostname())
        filename = f"tab-{tab_id}-{current_loader or 'noloader'}.warc"
        return Response(
            content=blob,
            media_type="application/warc",
            headers={"content-disposition": f'attachment; filename="{filename}"'},
        )
```

- [ ] **Step 4: Run the endpoint tests**

Run: `uv run pytest tests/test_warc_endpoint.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: all green.

- [ ] **Step 6: Lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: both clean. If format check fails, run `uv run ruff format .` and stage the result.

- [ ] **Step 7: Commit**

```bash
git add src/passe_partout/app.py tests/test_warc_endpoint.py
git commit -m "app: GET /tabs/{id}/warc returns WARC archive of current page's resources"
```

---

## Self-Review Notes

Coverage check against spec:
- `CaptureMode` enum and propagation → Tasks 2, 4, 5, 8.
- Always-on header capture → Task 6.
- Eager body capture in COPY/COPY_AND_RETAIN → Task 7.
- Skip prune in COPY_AND_RETAIN → Task 7.
- `get_body()` prefers buffered → Task 5 (`get_body_for`).
- `/resources/{id}` reliability improvement under buffered modes → Task 5 step 4.
- `GET /tabs/{id}/warc` returning WARC for current loader → Tasks 9–10.
- WARC writer uses `warcio`, emits `warcinfo` + request/response pairs, handles missing bodies with `WARC-Truncated` → Task 9.
- Empty-tab case (warcinfo only) → Task 9.
- 404 case → Task 10.
- POST body capture (best-effort via `request.post_data`) → Task 6.
- `/fetch` unchanged → no task touches it.

Naming/type cross-check: `CaptureMode`, `ResourceRecord` (with new fields), `RequestRecord`, `build_warc(rec, current_loader_id, hostname)`, `ResourceRecorder.attach_tab(tab_id, tab, capture_mode)`, `ResourceRecorder.get_body_for(record, tab)`, `ResourceRecorder.current_loader(tab_id)`, `ResourceRecorder.mode_for(tab_id)` — all referenced consistently between tasks.
