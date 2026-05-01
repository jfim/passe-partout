# Downloads Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tab-scoped binary download support so non-HTML navigations and click-triggered downloads return retrievable artifacts via REST endpoints.

**Architecture:** A new `DownloadCoordinator` (in `src/passe_partout/downloads.py`) sits beside `BrowserPool` and `TabRegistry`. Per tab, it sets `Browser.setDownloadBehavior` to direct files to a per-tab temp dir, enables `Fetch` interception on main-frame documents to force non-HTML responses into the download path (by injecting `Content-Disposition: attachment`), and listens for `Browser.downloadWillBegin` / `Browser.downloadProgress` to populate `DownloadRecord` instances on the owning `TabRecord`. Five new REST endpoints expose the records.

**Tech Stack:** Python 3.12+, FastAPI, `nodriver` (CDP wrapper), pytest, ruff. Existing patterns to mirror: `NavCapture` (CDP event subscription via `tab.add_handler` + `tab.send(...enable())`), `TabRegistry` (registry + per-record asyncio.Lock).

**Spec:** See `docs/superpowers/specs/2026-04-30-downloads-design.md` for the full design.

---

## Conventions used by every task

- **TDD strictly.** Each task: failing test → implementation → passing test → commit.
- Run tests with `uv run pytest -x tests/<file>::<name>`. The `-x` stops on first failure.
- Run lint after edits: `uv run ruff check . && uv run ruff format .`.
- Every module begins with `from __future__ import annotations`.
- Commit messages follow the existing repo style: `feat:`, `test:`, `refactor:`, `docs:` (lowercase prefix, imperative). Add the standard `Co-Authored-By` trailer.
- Tests of full request flows use the existing `client` fixture in `tests/conftest.py`. Real Chromium is used (no mocking) unless the test is purely about pool/coordinator state, in which case follow the `test_idle_chrome_shutdown.py` fake-browser pattern.

---

## Task 1: Add `DOWNLOAD_DIR` to Config

**Files:**
- Modify: `src/passe_partout/config.py`
- Test: `tests/test_config.py` (add a new test function)

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_config.py`:

```python
def test_download_dir_default_is_tmp(monkeypatch):
    monkeypatch.delenv("DOWNLOAD_DIR", raising=False)
    cfg = Config.from_env()
    assert cfg.download_dir == "/tmp"


def test_download_dir_from_env(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_DIR", "/var/passe-partout-dl")
    cfg = Config.from_env()
    assert cfg.download_dir == "/var/passe-partout-dl"
```

- [ ] **Step 2: Run tests to verify they fail.**

```
uv run pytest tests/test_config.py::test_download_dir_default_is_tmp -v
```
Expected: `FAILED` with `AttributeError: 'Config' object has no attribute 'download_dir'`.

- [ ] **Step 3: Add the field and env loading.**

In `src/passe_partout/config.py`, add to the `Config` dataclass and `from_env`:

```python
@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    max_tabs: int = 10
    idle_tab_close_seconds: int = 300
    idle_chrome_shutdown_seconds: int = 300
    auth_token: str | None = None
    extension_dirs: list[str] = field(default_factory=list)
    headless: bool = True
    chrome_path: str | None = None
    download_dir: str = "/tmp"

    @classmethod
    def from_env(cls) -> Config:
        # ...existing body...
        return cls(
            # ...existing kwargs...
            download_dir=os.environ.get("DOWNLOAD_DIR", "/tmp"),
        )
```

- [ ] **Step 4: Run tests to verify they pass.**

```
uv run pytest tests/test_config.py -v
```
Expected: all green.

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/config.py tests/test_config.py
git commit -m "feat: add DOWNLOAD_DIR config"
```

---

## Task 2: New `DownloadInfo` and `DownloadStatus` models; extend `CreateTabResponse` and `GotoResponse`

**Files:**
- Modify: `src/passe_partout/models.py`
- Test: `tests/test_models.py` (new file)

- [ ] **Step 1: Write the failing test.**

Create `tests/test_models.py`:

```python
from __future__ import annotations

from passe_partout.models import (
    CreateTabResponse,
    DownloadInfo,
    DownloadStatus,
    GotoResponse,
)


def test_create_tab_response_download_optional():
    r = CreateTabResponse(id=1, status=200, final_url="http://x/", content_type="text/html")
    assert r.download is None


def test_create_tab_response_with_download():
    r = CreateTabResponse(
        id=1,
        status=200,
        final_url="http://x/file.zip",
        content_type="application/zip",
        download=DownloadInfo(id="abc", filename="file.zip", size_bytes=1024),
    )
    assert r.download.id == "abc"
    assert r.download.size_bytes == 1024


def test_download_status_unknown_size_is_minus_one():
    s = DownloadStatus(
        id="abc",
        url="http://x/",
        filename="x.zip",
        state="in_progress",
        bytes_received=0,
        size_bytes=-1,
        started_at=1.0,
        completed_at=None,
    )
    assert s.size_bytes == -1
    assert s.completed_at is None


def test_goto_response_download_optional():
    r = GotoResponse(status=200, final_url="http://x/", content_type="text/html")
    assert r.download is None
```

- [ ] **Step 2: Run tests to verify they fail.**

```
uv run pytest tests/test_models.py -v
```
Expected: `ImportError: cannot import name 'DownloadInfo'`.

- [ ] **Step 3: Add the models.**

Append to `src/passe_partout/models.py`:

```python
class DownloadInfo(BaseModel):
    id: str
    filename: str
    size_bytes: int  # -1 when unknown


class DownloadStatus(BaseModel):
    id: str
    url: str
    filename: str
    state: str  # "in_progress" | "completed" | "canceled"
    bytes_received: int
    size_bytes: int  # -1 when unknown
    started_at: float
    completed_at: float | None
```

In the same file, add an optional `download` field to `CreateTabResponse` and `GotoResponse`:

```python
class CreateTabResponse(BaseModel):
    id: int
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None


class GotoResponse(BaseModel):
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None
```

(`DownloadInfo` and `DownloadStatus` must be defined before they're referenced — put them above `CreateTabResponse`.)

- [ ] **Step 4: Run tests to verify they pass.**

```
uv run pytest tests/test_models.py -v
```
Expected: all green.

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/models.py tests/test_models.py
git commit -m "feat: add DownloadInfo/DownloadStatus models and tab response download field"
```

---

## Task 3: Add `DownloadRecord` and extend `TabRecord`

**Files:**
- Create: `src/passe_partout/downloads.py`
- Modify: `src/passe_partout/tab_registry.py`
- Test: `tests/test_tab_registry.py` (add new test) and `tests/test_downloads.py` (new file)

- [ ] **Step 1: Write failing tests.**

Create `tests/test_downloads.py`:

```python
from __future__ import annotations

from pathlib import Path

from passe_partout.downloads import DownloadRecord


def test_download_record_defaults():
    rec = DownloadRecord(
        id="abc",
        url="http://x/file.zip",
        filename="file.zip",
        path=Path("/tmp/file"),
        started_at=1.0,
    )
    assert rec.state == "in_progress"
    assert rec.bytes_received == 0
    assert rec.size_bytes == -1
    assert rec.completed_at is None
```

Append to `tests/test_tab_registry.py`:

```python
def test_tab_record_has_downloads_dict():
    from passe_partout.tab_registry import TabRecord

    rec = TabRecord(id=1, tab=None, created_at=0.0, last_used_at=0.0, ttl_seconds=10)
    assert rec.downloads == {}
    assert rec.main_frame_id is None
```

- [ ] **Step 2: Run tests to verify they fail.**

```
uv run pytest tests/test_downloads.py tests/test_tab_registry.py -v
```
Expected: `ImportError` for `DownloadRecord`; `AttributeError` for `downloads`.

- [ ] **Step 3: Implement.**

Create `src/passe_partout/downloads.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DownloadState = Literal["in_progress", "completed", "canceled"]


@dataclass
class DownloadRecord:
    id: str  # CDP guid
    url: str
    filename: str
    path: Path
    started_at: float
    state: DownloadState = "in_progress"
    bytes_received: int = 0
    size_bytes: int = -1
    completed_at: float | None = None
```

Modify `src/passe_partout/tab_registry.py` — add fields to `TabRecord`:

```python
from passe_partout.downloads import DownloadRecord


@dataclass
class TabRecord:
    id: int
    tab: Any
    created_at: float
    last_used_at: float
    ttl_seconds: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    nav: Any = None
    downloads: dict[str, DownloadRecord] = field(default_factory=dict)
    main_frame_id: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass.**

```
uv run pytest tests/test_downloads.py tests/test_tab_registry.py -v
```

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/downloads.py src/passe_partout/tab_registry.py tests/test_downloads.py tests/test_tab_registry.py
git commit -m "feat: add DownloadRecord and extend TabRecord with downloads dict"
```

---

## Task 4: Extend the test fixture server with binary, image, JSON, and iframe fixtures

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/fixtures/iframe_with_image.html`, `tests/fixtures/normal_page.html`

These fixtures are reused by every later download test, so building them now means later tasks just exercise endpoints.

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_fetch.py` (or create `tests/test_fixture_server.py` if you prefer; the existing `tests/test_fetch.py` already exercises the fixture server):

```python
async def test_fixture_server_serves_zip(fixture_server):
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.get(f"{fixture_server}/binary.zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert r.headers.get("content-disposition", "").startswith("attachment")
        assert r.content == b"PK\x03\x04 fake zip body"


async def test_fixture_server_serves_png_inline(fixture_server):
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.get(f"{fixture_server}/sample.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        # Origin sends inline (or no CD); spec says we still treat as download.
        assert "attachment" not in r.headers.get("content-disposition", "")
```

(Mark these `@pytest.mark.asyncio` if your existing tests do; check `tests/test_fetch.py` for the convention used.)

- [ ] **Step 2: Run tests to verify they fail.**

Expected: 404 from fixture_server.

- [ ] **Step 3: Extend the fixture server.**

Replace the `fixture_server` fixture in `tests/conftest.py`:

```python
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fixture_server():
    async def html_handler(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = FIXTURES / f"{name}.html"
        if not path.exists():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="text/html")

    async def binary_handler(_request: web.Request) -> web.Response:
        return web.Response(
            body=b"PK\x03\x04 fake zip body",
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="binary.zip"',
            },
        )

    async def png_handler(_request: web.Request) -> web.Response:
        # 1x1 transparent PNG.
        body = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
        )
        return web.Response(
            body=body,
            headers={"Content-Type": "image/png", "Content-Disposition": "inline"},
        )

    async def json_handler(_request: web.Request) -> web.Response:
        return web.Response(
            body=b'{"hello":"world"}',
            headers={"Content-Type": "application/json", "Content-Disposition": "inline"},
        )

    async def slow_binary_handler(_request: web.Request) -> web.StreamResponse:
        # Sends 8 chunks of 1KB with delays so tests can observe in_progress state.
        import asyncio as _asyncio

        resp = web.StreamResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="slow.bin"',
                "Content-Length": str(8 * 1024),
            }
        )
        await resp.prepare(_request)
        for _ in range(8):
            await resp.write(b"\x00" * 1024)
            await _asyncio.sleep(0.2)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/{name}.html", html_handler)
    app.router.add_get("/binary.zip", binary_handler)
    app.router.add_get("/sample.png", png_handler)
    app.router.add_get("/data.json", json_handler)
    app.router.add_get("/slow.bin", slow_binary_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    await runner.cleanup()
```

Create `tests/fixtures/iframe_with_image.html`:

```html
<!doctype html>
<html><body>
<p>Iframe test page.</p>
<iframe src="/sample.png" width="50" height="50"></iframe>
</body></html>
```

Create `tests/fixtures/normal_page.html`:

```html
<!doctype html>
<html><body><h1>Hello</h1><img src="/sample.png"/></body></html>
```

- [ ] **Step 4: Run tests to verify they pass.**

```
uv run pytest tests/test_fetch.py -v
```

- [ ] **Step 5: Commit.**

```
git add tests/conftest.py tests/fixtures/iframe_with_image.html tests/fixtures/normal_page.html tests/test_fetch.py
git commit -m "test: extend fixture server with binary, image, JSON, and iframe routes"
```

---

## Task 5: Implement `DownloadCoordinator` (lifecycle: per-tab attach/detach)

This is the central new component. It does not yet handle Fetch interception or events — that comes in Tasks 6–8. This task gets the scaffolding and per-tab directory management in place.

**Files:**
- Modify: `src/passe_partout/downloads.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write failing tests.**

Append to `tests/test_downloads.py`:

```python
import asyncio
import os
import shutil

import pytest


@pytest.mark.asyncio
async def test_coordinator_creates_and_removes_per_tab_dir(tmp_path):
    from passe_partout.downloads import DownloadCoordinator

    coord = DownloadCoordinator(root_dir=str(tmp_path))
    tab_dir = coord.tab_dir(tab_id=42)
    assert not tab_dir.exists()
    coord.ensure_tab_dir(tab_id=42)
    assert tab_dir.exists()
    coord.cleanup_tab_dir(tab_id=42)
    assert not tab_dir.exists()


@pytest.mark.asyncio
async def test_coordinator_cleanup_is_idempotent(tmp_path):
    from passe_partout.downloads import DownloadCoordinator

    coord = DownloadCoordinator(root_dir=str(tmp_path))
    coord.cleanup_tab_dir(tab_id=99)  # never created
    coord.ensure_tab_dir(tab_id=99)
    coord.cleanup_tab_dir(tab_id=99)
    coord.cleanup_tab_dir(tab_id=99)  # second call is fine
```

- [ ] **Step 2: Run tests to verify they fail.**

Expected: `ImportError: cannot import name 'DownloadCoordinator'`.

- [ ] **Step 3: Implement the coordinator skeleton.**

Append to `src/passe_partout/downloads.py`:

```python
import shutil
from pathlib import Path


class DownloadCoordinator:
    """Owns per-tab download directories and (later) CDP plumbing.

    Each tab gets a directory under <root_dir>/passe-partout/tab-<tab_id>/
    where Chromium writes downloaded files (named by their CDP guid).
    """

    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir) / "passe-partout"

    def tab_dir(self, tab_id: int) -> Path:
        return self.root / f"tab-{tab_id}"

    def ensure_tab_dir(self, tab_id: int) -> Path:
        d = self.tab_dir(tab_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup_tab_dir(self, tab_id: int) -> None:
        d = self.tab_dir(tab_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
```

- [ ] **Step 4: Run tests to verify they pass.**

```
uv run pytest tests/test_downloads.py -v
```

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/downloads.py tests/test_downloads.py
git commit -m "feat: add DownloadCoordinator with per-tab directory management"
```

---

## Task 6: Set `Browser.setDownloadBehavior` per tab and capture main frame id

This task hooks the coordinator into `POST /tabs` so each tab gets:
1. Its download directory created.
2. `Browser.setDownloadBehavior(behavior="allowAndName", downloadPath=<tab_dir>, eventsEnabled=True)` issued (scoped to the tab's browser context).
3. Its main frame id captured for the iframe filter (used in Task 7).

**Files:**
- Modify: `src/passe_partout/downloads.py`
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_downloads.py` (this is an integration test — uses real Chromium):

```python
@pytest.mark.asyncio
async def test_tab_creation_creates_download_dir(client, fixture_server, tmp_path, monkeypatch):
    # client uses Config() which has download_dir="/tmp" by default; we want to
    # verify the per-tab dir gets created. Easiest: re-create the app with a
    # temporary download_dir.
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    # Reuse the session pool from the `client` fixture by reading it off the existing app.
    pool = client._transport.app.state.pool
    app = build_app(cfg=cfg, browser_pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            tab_id = r.json()["id"]
            tab_dir = tmp_path / "passe-partout" / f"tab-{tab_id}"
            assert tab_dir.exists()
            await c.delete(f"/tabs/{tab_id}")
            assert not tab_dir.exists()
```

- [ ] **Step 2: Run the test to verify it fails.**

```
uv run pytest tests/test_downloads.py::test_tab_creation_creates_download_dir -v
```
Expected: directory does not exist.

- [ ] **Step 3: Add `attach_tab` / `detach_tab` to the coordinator.**

In `src/passe_partout/downloads.py`, add:

```python
import nodriver as uc


class DownloadCoordinator:
    # ... existing ...

    async def attach_tab(self, tab_id: int, tab: uc.Tab) -> None:
        """Configure Chromium to route downloads for this tab to its dir.

        Captures the main frame id for later iframe filtering. Must be called
        before any navigation so behavior is in place when the first response
        arrives.
        """
        download_path = str(self.ensure_tab_dir(tab_id).resolve())
        # Browser-level setDownloadBehavior takes a browserContextId. With
        # nodriver's `new_window=True`, the tab is its own context. We send the
        # CDP command via the tab; nodriver routes it to the correct context.
        await tab.send(
            uc.cdp.browser.set_download_behavior(
                behavior="allowAndName",
                download_path=download_path,
                events_enabled=True,
            )
        )

    async def detach_tab(self, tab_id: int) -> None:
        self.cleanup_tab_dir(tab_id)
```

- [ ] **Step 4: Wire it into `app.py`.**

In `src/passe_partout/app.py`:

a) Import the coordinator at the top:

```python
from passe_partout.downloads import DownloadCoordinator
```

b) In the `lifespan` function, create the coordinator alongside the registry:

```python
app.state.coord = DownloadCoordinator(root_dir=cfg.download_dir)
```

c) In `create_tab`, **register the tab in the registry BEFORE navigation** (so we have a tab id to give the coordinator), then call `coord.attach_tab`. Replace the body of `create_tab` with:

```python
@app.post("/tabs", response_model=CreateTabResponse)
async def create_tab(req: CreateTabRequest):
    cfg_now = app.state.cfg
    registry = app.state.registry
    pool = app.state.pool
    coord = app.state.coord

    async with registry.mu:
        if registry.count() >= cfg_now.max_tabs:
            return JSONResponse(
                status_code=429,
                content={"error": "max_tabs", "detail": f"cap of {cfg_now.max_tabs} reached"},
            )

    tab = None
    rec = None
    try:
        tab = await pool.create_context("about:blank")
        ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_tab_close_seconds
        rec = registry.register(tab=tab, ttl_seconds=ttl)
        await coord.attach_tab(rec.id, tab)
        nav = NavCapture(tab)
        await nav.attach()
        rec.nav = nav
        if req.cookies:
            cdp_cookies = _cookies_to_cdp(req.cookies, url=req.url)
            await tab.send(uc.cdp.network.set_cookies(cdp_cookies))
        nav.reset()
        await tab.get(req.url)
        await nav.wait()
    except Exception as e:
        if rec is not None:
            registry.remove(rec.id)
            await coord.detach_tab(rec.id)
        if tab is not None:
            try:
                await pool.close_context(tab)
            except Exception:
                pass
        return JSONResponse(
            status_code=502,
            content={"error": "browser_error", "detail": str(e)},
        )

    return CreateTabResponse(
        id=rec.id,
        status=nav.status if nav.status is not None else 200,
        final_url=tab.url or req.url,
        content_type=nav.mime_type,
    )
```

d) In `delete_tab`, call `coord.detach_tab` after closing:

```python
@app.delete("/tabs/{tab_id}", status_code=204)
async def delete_tab(tab_id: int):
    registry = app.state.registry
    pool = app.state.pool
    coord = app.state.coord
    rec = registry.remove(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    try:
        await pool.close_context(rec.tab)
    finally:
        await coord.detach_tab(tab_id)
    return Response(status_code=204)
```

e) In `sweep_once`, call `coord.detach_tab` after eviction:

```python
async def sweep_once():
    registry = app.state.registry
    pool = app.state.pool
    coord = app.state.coord
    for tid in registry.idle_ids():
        rec = registry.remove(tid)
        if rec is not None:
            try:
                await pool.close_context(rec.tab)
            finally:
                await coord.detach_tab(tid)
```

- [ ] **Step 5: Run the test.**

```
uv run pytest tests/test_downloads.py::test_tab_creation_creates_download_dir -v
```
Expected: PASS.

- [ ] **Step 6: Run the full suite to make sure nothing regressed.**

```
uv run pytest -x
```

- [ ] **Step 7: Commit.**

```
git add src/passe_partout/downloads.py src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: per-tab download directory and Browser.setDownloadBehavior"
```

---

## Task 7: Capture downloads via `Browser.downloadWillBegin` and `Browser.downloadProgress`

This task makes `DownloadRecord` instances appear on the `TabRecord` in response to actual Chromium download events. We use `Browser`-level events because that is what the spec calls for; nodriver lets us subscribe via `tab.add_handler` (events route to handlers attached to any tab in the corresponding context — see `NavCapture` for the pattern with `Network` events).

**Files:**
- Modify: `src/passe_partout/downloads.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write the failing test (real Chromium, real download).**

Append to `tests/test_downloads.py`:

```python
@pytest.mark.asyncio
async def test_origin_attachment_creates_download_record(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            assert r.status_code == 200
            body = r.json()
            assert body["download"] is not None
            assert body["download"]["filename"] == "binary.zip"
            tab_id = body["id"]
            # Wait a moment for completion event.
            for _ in range(40):
                rec = app.state.registry.get(tab_id)
                dl = next(iter(rec.downloads.values()), None)
                if dl is not None and dl.state == "completed":
                    break
                await asyncio.sleep(0.05)
            assert dl is not None
            assert dl.state == "completed"
            assert dl.bytes_received > 0
            await c.delete(f"/tabs/{tab_id}")
```

- [ ] **Step 2: Run test to verify it fails.**

Expected: `body["download"]` is `None` because we are not yet creating download records.

- [ ] **Step 3: Subscribe to download events in `attach_tab`.**

Update `attach_tab` in `src/passe_partout/downloads.py` to attach handlers and own the lookup table:

```python
import time

class DownloadCoordinator:
    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir) / "passe-partout"
        # Map CDP guid -> (tab_id, registry, on_progress callback target).
        self._tab_lookup: dict[str, int] = {}
        # Map tab_id -> registry handle for finding the TabRecord. Set in attach_tab.
        self._registry = None  # injected by app.py via set_registry()

    def set_registry(self, registry) -> None:
        self._registry = registry

    async def attach_tab(self, tab_id: int, tab: uc.Tab) -> None:
        download_path = str(self.ensure_tab_dir(tab_id).resolve())

        def _on_will_begin(evt) -> None:
            # evt has: guid, url, suggested_filename, frame_id
            rec = self._registry.get(tab_id) if self._registry else None
            if rec is None:
                return
            from passe_partout.downloads import DownloadRecord  # avoid circular at import time
            dl = DownloadRecord(
                id=evt.guid,
                url=evt.url,
                filename=evt.suggested_filename,
                path=self.tab_dir(tab_id) / evt.guid,
                started_at=time.time(),
            )
            rec.downloads[evt.guid] = dl
            self._tab_lookup[evt.guid] = tab_id

        def _on_progress(evt) -> None:
            tid = self._tab_lookup.get(evt.guid)
            if tid is None or self._registry is None:
                return
            rec = self._registry.get(tid)
            if rec is None:
                return
            dl = rec.downloads.get(evt.guid)
            if dl is None:
                return
            dl.bytes_received = int(evt.received_bytes)
            total = int(evt.total_bytes)
            dl.size_bytes = -1 if total == 0 else total
            state = str(evt.state)  # "inProgress" | "completed" | "canceled"
            if state == "inProgress":
                dl.state = "in_progress"
            elif state == "completed":
                dl.state = "completed"
                dl.completed_at = time.time()
            elif state == "canceled":
                dl.state = "canceled"
                dl.completed_at = time.time()
            # Active download keeps tab alive.
            rec.last_used_at = time.time()

        tab.add_handler(uc.cdp.browser.DownloadWillBegin, _on_will_begin)
        tab.add_handler(uc.cdp.browser.DownloadProgress, _on_progress)
        await tab.send(
            uc.cdp.browser.set_download_behavior(
                behavior="allowAndName",
                download_path=download_path,
                events_enabled=True,
            )
        )
```

- [ ] **Step 4: Wire `set_registry` in `app.py` lifespan.**

Inside `lifespan`, after creating the registry and coordinator:

```python
app.state.registry = TabRegistry()
app.state.coord = DownloadCoordinator(root_dir=cfg.download_dir)
app.state.coord.set_registry(app.state.registry)
```

- [ ] **Step 5: Add the `download` field to the `CreateTabResponse` returned by `create_tab`.**

In `create_tab`, after `await nav.wait()`, check whether a download record was created on the tab. The download event may or may not have fired by the time `nav.wait()` returns; for the "origin sent attachment" case it usually has, but for safety we wait briefly:

```python
# After nav.wait():
download_info = None
for _ in range(20):  # up to ~0.5s
    if rec.downloads:
        dl = next(iter(rec.downloads.values()))
        download_info = DownloadInfo(
            id=dl.id, filename=dl.filename, size_bytes=dl.size_bytes
        )
        break
    await _asyncio.sleep(0.025)

return CreateTabResponse(
    id=rec.id,
    status=nav.status if nav.status is not None else 200,
    final_url=tab.url or req.url,
    content_type=nav.mime_type,
    download=download_info,
)
```

Add the `DownloadInfo` import at the top of `app.py`:

```python
from passe_partout.models import (
    ...
    DownloadInfo,
    ...
)
```

- [ ] **Step 6: Run the test.**

```
uv run pytest tests/test_downloads.py::test_origin_attachment_creates_download_record -v
```
Expected: PASS. If `dl.state` is still `in_progress` at the end, increase the wait loop iterations.

- [ ] **Step 7: Run the full suite.**

```
uv run pytest -x
```

- [ ] **Step 8: Commit.**

```
git add src/passe_partout/downloads.py src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: capture downloads via Browser.downloadWillBegin/downloadProgress"
```

---

## Task 8: `Fetch.enable` interception to force non-HTML into the download path

This is the spec's headline behavior. Without this, navigating to `/sample.png` produces Chromium's synthetic image-viewer page, not a download. After this task, any non-HTML main-frame response becomes a download.

**Files:**
- Modify: `src/passe_partout/downloads.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write the failing test.**

```python
@pytest.mark.asyncio
async def test_image_navigation_becomes_download(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/sample.png"})
            assert r.status_code == 200
            body = r.json()
            assert body["download"] is not None
            assert body["download"]["filename"]  # non-empty (Chromium derives from URL)
            await c.delete(f"/tabs/{body['id']}")


@pytest.mark.asyncio
async def test_html_navigation_does_not_become_download(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            assert r.status_code == 200
            assert r.json()["download"] is None
            await c.delete(f"/tabs/{r.json()['id']}")
```

- [ ] **Step 2: Run tests to verify failure.**

Expected: image case returns `download: null` because Chromium renders it inline.

- [ ] **Step 3: Implement Fetch interception in `attach_tab`.**

Add to `attach_tab` in `src/passe_partout/downloads.py`, after the download handlers and before `set_download_behavior`:

```python
async def _on_request_paused(evt) -> None:
    # Only main-frame Document responses get rewritten. Iframes and
    # subresources pass through untouched.
    rec = self._registry.get(tab_id) if self._registry else None
    main_frame_id = rec.main_frame_id if rec else None
    if main_frame_id is not None and evt.frame_id != main_frame_id:
        await tab.send(uc.cdp.fetch.continue_response(request_id=evt.request_id))
        return

    headers = list(evt.response_headers or [])
    ctype = ""
    for h in headers:
        if h.name.lower() == "content-type":
            ctype = (h.value or "").lower()
            break
    is_html = ctype.startswith("text/html") or ctype.startswith("application/xhtml+xml")
    if is_html:
        await tab.send(uc.cdp.fetch.continue_response(request_id=evt.request_id))
        return

    # Non-HTML: inject Content-Disposition: attachment.
    headers = [h for h in headers if h.name.lower() != "content-disposition"]
    headers.append(uc.cdp.fetch.HeaderEntry(name="Content-Disposition", value="attachment"))
    await tab.send(
        uc.cdp.fetch.continue_response(request_id=evt.request_id, response_headers=headers)
    )

tab.add_handler(uc.cdp.fetch.RequestPaused, _on_request_paused)
await tab.send(
    uc.cdp.fetch.enable(
        patterns=[
            uc.cdp.fetch.RequestPattern(
                url_pattern="*",
                resource_type=uc.cdp.network.ResourceType.DOCUMENT,
                request_stage=uc.cdp.fetch.RequestStage.RESPONSE,
            )
        ]
    )
)
```

Also capture the main frame id as soon as the page loads. In `attach_tab`, before returning, fetch and store it:

```python
# After Fetch.enable:
tree = await tab.send(uc.cdp.page.get_frame_tree())
rec = self._registry.get(tab_id) if self._registry else None
if rec is not None:
    rec.main_frame_id = tree.frame.id

def _on_frame_navigated(evt) -> None:
    if evt.frame.parent_id is None:
        rec2 = self._registry.get(tab_id) if self._registry else None
        if rec2 is not None:
            rec2.main_frame_id = evt.frame.id

tab.add_handler(uc.cdp.page.FrameNavigated, _on_frame_navigated)
await tab.send(uc.cdp.page.enable())
```

> **Note:** the exact attribute names (`evt.frame_id`, `evt.response_headers`, `tree.frame.id`, `evt.frame.parent_id`) come from nodriver's CDP code-gen. If a name doesn't match, run `python -c "import nodriver; help(nodriver.cdp.fetch.RequestPaused)"` to inspect the dataclass and adjust.

- [ ] **Step 4: Run the new tests.**

```
uv run pytest tests/test_downloads.py::test_image_navigation_becomes_download tests/test_downloads.py::test_html_navigation_does_not_become_download -v
```

- [ ] **Step 5: Run the full suite.**

```
uv run pytest -x
```

- [ ] **Step 6: Commit.**

```
git add src/passe_partout/downloads.py tests/test_downloads.py
git commit -m "feat: Fetch interception forces non-HTML responses into download path"
```

---

## Task 9: Subresource and iframe non-interception tests

Belt-and-suspenders coverage for the spec's main-frame-only rule.

**Files:**
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write the tests.**

```python
@pytest.mark.asyncio
async def test_subresources_do_not_become_downloads(fixture_server, browser_pool, tmp_path):
    """An HTML page with an embedded <img> must not produce a download record."""
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tab_id = r.json()["id"]
            await asyncio.sleep(0.5)  # let the embedded image finish loading
            rec = app.state.registry.get(tab_id)
            assert rec.downloads == {}
            await c.delete(f"/tabs/{tab_id}")


@pytest.mark.asyncio
async def test_iframe_document_does_not_become_download(fixture_server, browser_pool, tmp_path):
    """A page whose iframe loads a non-HTML resource must not produce a download."""
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/tabs", json={"url": f"{fixture_server}/iframe_with_image.html"}
            )
            tab_id = r.json()["id"]
            await asyncio.sleep(0.5)
            rec = app.state.registry.get(tab_id)
            assert rec.downloads == {}
            await c.delete(f"/tabs/{tab_id}")
```

- [ ] **Step 2: Run the tests.**

Expected: PASS (the work in Task 8 already implements both filters).

- [ ] **Step 3: Commit.**

```
git add tests/test_downloads.py
git commit -m "test: subresources and iframes do not produce downloads"
```

---

## Task 10: `GET /tabs/{id}/downloads` route

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Write the failing test.**

```python
@pytest.mark.asyncio
async def test_list_downloads_returns_records(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            for _ in range(40):
                lst = await c.get(f"/tabs/{tid}/downloads")
                if lst.json() and lst.json()[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            data = lst.json()
            assert len(data) == 1
            assert data[0]["filename"] == "binary.zip"
            assert data[0]["state"] == "completed"
            assert data[0]["bytes_received"] > 0
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_list_downloads_404_for_unknown_tab(client):
    r = await client.get("/tabs/99999/downloads")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Add the route in `app.py`.**

A small helper turns a `DownloadRecord` into the wire shape:

```python
from passe_partout.models import DownloadStatus

def _download_to_status(dl) -> DownloadStatus:
    return DownloadStatus(
        id=dl.id,
        url=dl.url,
        filename=dl.filename,
        state=dl.state,
        bytes_received=dl.bytes_received,
        size_bytes=dl.size_bytes,
        started_at=dl.started_at,
        completed_at=dl.completed_at,
    )

@app.get("/tabs/{tab_id}/downloads", response_model=list[DownloadStatus])
async def list_downloads(tab_id: int):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    return [_download_to_status(dl) for dl in rec.downloads.values()]
```

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: add GET /tabs/{id}/downloads"
```

---

## Task 11: `GET /tabs/{id}/downloads/{did}/status` route

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_download_status_endpoint(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert s.status_code == 200
            assert s.json()["id"] == did
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_status_404(client):
    # Open any tab first to get a valid tab_id, then ask for a bogus did.
    r = await client.post("/tabs", json={"url": "about:blank"})
    tid = r.json()["id"]
    bad = await client.get(f"/tabs/{tid}/downloads/nope/status")
    assert bad.status_code == 404
    await client.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Add the route.**

```python
@app.get("/tabs/{tab_id}/downloads/{did}/status", response_model=DownloadStatus)
async def download_status(tab_id: int, did: str):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    dl = rec.downloads.get(did)
    if dl is None:
        return JSONResponse(
            status_code=404,
            content={"error": "download_not_found", "detail": f"no download {did}"},
        )
    return _download_to_status(dl)
```

- [ ] **Step 4: Run tests, then commit.**

```
git add src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: add GET /tabs/{id}/downloads/{did}/status"
```

---

## Task 12: `GET /tabs/{id}/downloads/{did}` (bytes) with 425 / 410 logic

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_download_bytes_completed(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 200
            assert b.headers["content-disposition"].startswith("attachment")
            assert "binary.zip" in b.headers["content-disposition"]
            assert b.content == b"PK\x03\x04 fake zip body"
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_download_bytes_in_progress_returns_425(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            # The slow fixture sleeps; we should be able to hit it during in_progress.
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 425
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Add the route.**

```python
from fastapi.responses import FileResponse

@app.get("/tabs/{tab_id}/downloads/{did}")
async def download_bytes(tab_id: int, did: str):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    dl = rec.downloads.get(did)
    if dl is None:
        return JSONResponse(
            status_code=404,
            content={"error": "download_not_found", "detail": f"no download {did}"},
        )
    if dl.state == "in_progress":
        return JSONResponse(
            status_code=425,
            content={"error": "download_in_progress", "detail": "still downloading"},
            headers={"Retry-After": "1"},
        )
    if dl.state == "canceled":
        return JSONResponse(
            status_code=410,
            content={"error": "download_canceled", "detail": "download was canceled"},
        )
    return FileResponse(
        path=str(dl.path),
        filename=dl.filename,
        media_type="application/octet-stream",  # we don't have the origin Content-Type stored; see note
    )
```

> **Implementation note:** the spec says we should preserve the origin's `Content-Type`. Today we don't capture it on the `DownloadRecord`. To do so cleanly, capture `Content-Type` in the `Fetch.requestPaused` handler (we already inspect headers there) and stash it on the record. Add a `content_type: str | None = None` field to `DownloadRecord`, set it from `headers` in the rewrite handler, and use it as `media_type` in the `FileResponse` (falling back to `application/octet-stream` if missing). Make this part of this task — write a separate small test asserting `b.headers["content-type"] == "application/zip"`.

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/app.py src/passe_partout/downloads.py tests/test_downloads.py
git commit -m "feat: serve download bytes with 425 in-progress and 410 canceled"
```

---

## Task 13: `POST /tabs/{id}/downloads/{did}/cancel` route

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_cancel_in_progress_download(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            cx = await c.post(f"/tabs/{tid}/downloads/{did}/cancel")
            assert cx.status_code == 204
            for _ in range(20):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "canceled":
                    break
                await asyncio.sleep(0.05)
            assert s.json()["state"] == "canceled"
            # Bytes endpoint now returns 410.
            b = await c.get(f"/tabs/{tid}/downloads/{did}")
            assert b.status_code == 410
            # Cancel on terminal state returns 409.
            cx2 = await c.post(f"/tabs/{tid}/downloads/{did}/cancel")
            assert cx2.status_code == 409
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Add the route plus a coordinator helper.**

In `src/passe_partout/downloads.py`:

```python
class DownloadCoordinator:
    # ...
    async def cancel(self, tab: uc.Tab, guid: str) -> None:
        await tab.send(uc.cdp.browser.cancel_download(guid=guid))
```

In `src/passe_partout/app.py`:

```python
@app.post("/tabs/{tab_id}/downloads/{did}/cancel", status_code=204)
async def cancel_download(tab_id: int, did: str):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    dl = rec.downloads.get(did)
    if dl is None:
        return JSONResponse(
            status_code=404,
            content={"error": "download_not_found", "detail": f"no download {did}"},
        )
    if dl.state != "in_progress":
        return JSONResponse(
            status_code=409,
            content={"error": "download_terminal", "detail": f"state is {dl.state}"},
        )
    coord = app.state.coord
    async with rec.lock:
        await coord.cancel(rec.tab, did)
    return Response(status_code=204)
```

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/app.py src/passe_partout/downloads.py tests/test_downloads.py
git commit -m "feat: POST /tabs/{id}/downloads/{did}/cancel"
```

---

## Task 14: `DELETE /tabs/{id}/downloads/{did}` route

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_delete_completed_download_unlinks_file(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads/{did}/status")
                if s.json()["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            file_path = tmp_path / "passe-partout" / f"tab-{tid}" / did
            assert file_path.exists()
            d = await c.delete(f"/tabs/{tid}/downloads/{did}")
            assert d.status_code == 204
            assert not file_path.exists()
            # Subsequent GET 404s.
            s2 = await c.get(f"/tabs/{tid}/downloads/{did}/status")
            assert s2.status_code == 404
            await c.delete(f"/tabs/{tid}")


@pytest.mark.asyncio
async def test_delete_in_progress_cancels_then_unlinks(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            did = r.json()["download"]["id"]
            d = await c.delete(f"/tabs/{tid}/downloads/{did}")
            assert d.status_code == 204
            file_path = tmp_path / "passe-partout" / f"tab-{tid}" / did
            assert not file_path.exists()
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Add the route.**

```python
@app.delete("/tabs/{tab_id}/downloads/{did}", status_code=204)
async def delete_download(tab_id: int, did: str):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(
            status_code=404,
            content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
        )
    dl = rec.downloads.pop(did, None)
    if dl is None:
        return JSONResponse(
            status_code=404,
            content={"error": "download_not_found", "detail": f"no download {did}"},
        )
    coord = app.state.coord
    if dl.state == "in_progress":
        try:
            async with rec.lock:
                await coord.cancel(rec.tab, did)
        except Exception:
            pass
    try:
        if dl.path.exists():
            dl.path.unlink()
    except OSError:
        pass
    return Response(status_code=204)
```

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: DELETE /tabs/{id}/downloads/{did}"
```

---

## Task 15: Goto download support

`POST /tabs/{id}/goto` should mirror `POST /tabs` and surface a `download` field when navigation produces one.

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_goto_to_binary_returns_download(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/normal_page.html"})
            tid = r.json()["id"]
            r2 = await c.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/binary.zip"})
            assert r2.status_code == 200
            body = r2.json()
            assert body["download"] is not None
            assert body["download"]["filename"] == "binary.zip"
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to fail.**

- [ ] **Step 3: Update `goto` to surface downloads.**

In `app.py`:

```python
@app.post("/tabs/{tab_id}/goto", response_model=GotoResponse)
async def goto(tab_id: int, req: GotoRequest):
    rec = await _require_tab(tab_id)
    if rec is None:
        return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
    pre_existing = set(rec.downloads.keys())
    async with rec.lock:
        try:
            if rec.nav is not None:
                rec.nav.reset()
            await rec.tab.get(req.url)
            if rec.nav is not None:
                await rec.nav.wait()
        except Exception as e:
            return JSONResponse(
                status_code=502, content={"error": "browser_error", "detail": str(e)}
            )
    status = rec.nav.status if rec.nav and rec.nav.status is not None else 200
    ctype = rec.nav.mime_type if rec.nav else None

    # Check for a new download.
    new_dl = None
    for _ in range(20):
        diff = set(rec.downloads.keys()) - pre_existing
        if diff:
            new_dl = rec.downloads[next(iter(diff))]
            break
        await _asyncio.sleep(0.025)

    download_info = (
        DownloadInfo(id=new_dl.id, filename=new_dl.filename, size_bytes=new_dl.size_bytes)
        if new_dl is not None
        else None
    )
    return GotoResponse(
        status=status, final_url=rec.tab.url or req.url, content_type=ctype, download=download_info
    )
```

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit.**

```
git add src/passe_partout/app.py tests/test_downloads.py
git commit -m "feat: surface downloads on POST /tabs/{id}/goto"
```

---

## Task 16: Click-triggered download regression test

Already works via `Browser.downloadWillBegin`, but lock it in.

**Files:**
- Create: `tests/fixtures/click_to_download.html`
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Add the fixture.**

`tests/fixtures/click_to_download.html`:

```html
<!doctype html>
<html><body>
<a id="dl" href="/binary.zip" download>Download</a>
</body></html>
```

- [ ] **Step 2: Failing test.**

```python
@pytest.mark.asyncio
async def test_click_triggers_download(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/tabs", json={"url": f"{fixture_server}/click_to_download.html"}
            )
            tid = r.json()["id"]
            assert r.json()["download"] is None
            cl = await c.post(f"/tabs/{tid}/click", json={"selector": "#dl"})
            assert cl.status_code == 204
            for _ in range(40):
                lst = (await c.get(f"/tabs/{tid}/downloads")).json()
                if lst and lst[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert lst and lst[0]["filename"] == "binary.zip"
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 3: Run.** Expected: PASS without code changes.

- [ ] **Step 4: Commit.**

```
git add tests/fixtures/click_to_download.html tests/test_downloads.py
git commit -m "test: click-triggered download produces a download record"
```

---

## Task 17: Idle sweeper does not evict tab during in-progress download

The progress handler in Task 7 already calls `rec.last_used_at = time.time()` on every progress event. This task locks that behavior in with a test.

**Files:**
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Failing test.**

```python
@pytest.mark.asyncio
async def test_idle_sweep_does_not_evict_during_active_download(
    fixture_server, browser_pool, tmp_path
):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    # ttl=1 second so sweep would otherwise evict quickly. The slow fixture
    # streams for ~1.6s, so without the touch it would be evicted.
    cfg = Config(download_dir=str(tmp_path), idle_tab_close_seconds=1)
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/slow.bin"})
            tid = r.json()["id"]
            # Manually run the sweeper a few times during the download.
            for _ in range(8):
                await app.state.sweep_once()
                await asyncio.sleep(0.2)
            assert app.state.registry.get(tid) is not None
            # Eventually it should complete.
            for _ in range(40):
                s = await c.get(f"/tabs/{tid}/downloads")
                if s.json() and s.json()[0]["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
            assert s.json()[0]["state"] == "completed"
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to verify behavior.** This should already pass thanks to Task 7's progress handler.

- [ ] **Step 3: Commit.**

```
git add tests/test_downloads.py
git commit -m "test: in-progress download keeps tab from being idle-swept"
```

---

## Task 18: Multiple-downloads-per-tab regression test

**Files:**
- Test: `tests/test_downloads.py`

- [ ] **Step 1: Test.**

```python
@pytest.mark.asyncio
async def test_multiple_downloads_per_tab(fixture_server, browser_pool, tmp_path):
    from passe_partout.app import build_app
    from passe_partout.config import Config
    import httpx

    cfg = Config(download_dir=str(tmp_path))
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/tabs", json={"url": f"{fixture_server}/binary.zip"})
            tid = r.json()["id"]
            await c.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/data.json"})
            await asyncio.sleep(0.5)
            lst = (await c.get(f"/tabs/{tid}/downloads")).json()
            assert len(lst) == 2
            await c.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run.**

- [ ] **Step 3: Commit.**

```
git add tests/test_downloads.py
git commit -m "test: multiple downloads coexist on a single tab"
```

---

## Task 19: Update README and CLAUDE.md

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a downloads section to `README.md` describing:**
  - The five new endpoints with one-line descriptions and example responses.
  - The `DOWNLOAD_DIR` env var.
  - The "non-HTML response → download" rule (one paragraph).

  Reference the existing `Tabs` documentation for tone and formatting; do not invent a new style.

- [ ] **Step 2: Add a brief paragraph to `CLAUDE.md`'s Architecture section** noting that `DownloadCoordinator` sits beside `BrowserPool`/`TabRegistry`, owns Fetch interception and download-event handling per tab, and that the per-tab download dir lives at `<DOWNLOAD_DIR>/passe-partout/tab-<id>/`.

- [ ] **Step 3: Commit.**

```
git add README.md CLAUDE.md
git commit -m "docs: document download endpoints and DOWNLOAD_DIR"
```

---

## Task 20: Final verification pass

- [ ] **Step 1: Lint and format.**

```
uv run ruff check --fix . && uv run ruff format .
```

- [ ] **Step 2: Full test suite (non-smoke).**

```
uv run pytest
```

Expected: all green. If any test is flaky on timing, increase the polling loops, not the test's strictness.

- [ ] **Step 3: Smoke run (manual sanity check).**

Start the server in one terminal:

```
uv run python -m passe_partout
```

In another, exercise the flow against a real public binary URL of your choice (any non-HTML resource):

```
curl -sX POST http://127.0.0.1:8000/tabs -H 'Content-Type: application/json' \
  -d '{"url":"https://www.example.com/some-binary"}'
# → response includes "download": {...}; pull the bytes:
curl -sO http://127.0.0.1:8000/tabs/<id>/downloads/<did>
curl -X DELETE http://127.0.0.1:8000/tabs/<id>
```

- [ ] **Step 4: Final commit if anything was tweaked.**

---

## Self-review checklist (run after writing all tasks)

This is a checklist for the plan author to run, not part of execution.

**Spec coverage:**

- ✅ Goal & user-facing API (Tasks 2, 7, 10–15)
- ✅ Tab-scoped storage with cleanup on close (Tasks 5–6)
- ✅ `DOWNLOAD_DIR` env var (Task 1)
- ✅ Non-HTML → download (Fetch interception) (Task 8)
- ✅ Subresource non-interception (Tasks 8, 9)
- ✅ Iframe non-interception (Tasks 8, 9)
- ✅ Click-triggered downloads (Task 16)
- ✅ Multiple downloads per tab (Task 18)
- ✅ State machine (in_progress → completed/canceled) (Task 7)
- ✅ `size_bytes: -1` when unknown (Tasks 2, 7)
- ✅ Bytes endpoint: 200 / 425 / 410 (Task 12)
- ✅ Cancel endpoint with 409 on terminal (Task 13)
- ✅ Delete endpoint, including in-progress (Task 14)
- ✅ Idle sweeper held off by progress (Task 17)
- ✅ `Content-Disposition: attachment` always on serving (Task 12)
- ✅ Origin `Content-Type` preserved (Task 12 implementation note)

**Type consistency:** `DownloadRecord` fields stay constant from Task 3 onward; `DownloadInfo`/`DownloadStatus` field sets match the spec; route paths match the spec exactly (`/tabs/{id}/downloads`, `/tabs/{id}/downloads/{did}/status`, `/tabs/{id}/downloads/{did}`, `/tabs/{id}/downloads/{did}/cancel`).

**Placeholder scan:** No "TBD" or open-ended steps. Two "implementation notes" exist: Task 8 mentions `python -c "import nodriver; help(...)"` to verify CDP attribute names if codegen differs (this is genuine guidance, not a placeholder); Task 12 has a concrete instruction to add `content_type` to `DownloadRecord` and use it in the response.
