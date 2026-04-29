# passe-partout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python HTTP service that fronts a real Chromium browser via nodriver, exposing a tab-based REST API with optional unpacked-extension loading, isolated per-tab browser contexts, idle TTL cleanup, and a `/fetch` convenience endpoint.

**Architecture:** Single FastAPI process owns one nodriver `Browser` (one Chromium tree). Each `POST /tabs` allocates an isolated `BrowserContext` (incognito-style); per-tab ops serialize on an `asyncio.Lock`; an idle sweeper closes contexts that exceed their TTL. All config via env vars; default bind is localhost.

**Tech Stack:** Python 3.12+, uv, FastAPI, uvicorn, nodriver, Pydantic v2, pytest, pytest-asyncio, httpx, aiohttp (test-only fixture server), pytest-aiohttp.

---

## Reference: spec

The approved design is at `docs/superpowers/specs/2026-04-29-passe-partout-design.md`. Read it first.

## Reference: testing approach

Tests run against a **real** Chromium via nodriver — there is no mock layer. To keep them fast:

- Browser is launched once per test session (`session`-scoped fixture in `conftest.py`).
- A small `aiohttp` server serves static fixture HTML on a random port, also session-scoped.
- Each test creates and disposes its own tab(s); cross-test isolation comes from per-tab contexts.
- `pytest-asyncio` is configured with `asyncio_mode = "auto"` so tests can be plain `async def`.
- A single network-touching `test_smoke.py` is marked `@pytest.mark.smoke` and skipped by default; run with `pytest -m smoke`.

---

## File Structure

**Create:**
- `src/passe_partout/__init__.py` — package marker
- `src/passe_partout/config.py` — env-var dataclass
- `src/passe_partout/browser_pool.py` — `BrowserPool` class (start/stop/create_context/close_context)
- `src/passe_partout/tab_registry.py` — `TabRegistry` class (id allocation, locks, last_used_at)
- `src/passe_partout/models.py` — Pydantic request/response schemas
- `src/passe_partout/app.py` — FastAPI app + routes + lifespan + auth middleware
- `src/passe_partout/__main__.py` — `python -m passe_partout` entrypoint that runs uvicorn
- `tests/conftest.py` — session-scoped browser, fixture HTTP server, FastAPI client
- `tests/fixtures/static.html`, `tests/fixtures/js.html`, `tests/fixtures/delayed.html` — fixture pages
- `tests/test_config.py`, `tests/test_tab_registry.py`, `tests/test_health.py`, `tests/test_tabs.py`, `tests/test_tab_ops.py`, `tests/test_fetch.py`, `tests/test_idle_sweeper.py`, `tests/test_smoke.py`

**Modify:**
- `pyproject.toml` — add deps and pytest config
- `README.md` — usage docs

**Delete:**
- `hello.py` — uv default scaffold, unused

---

## Task 1: Project bootstrap

**Files:**
- Modify: `pyproject.toml`
- Delete: `hello.py`
- Create: `src/passe_partout/__init__.py`

- [ ] **Step 1: Update `pyproject.toml` with deps and layout**

```toml
[project]
name = "passe-partout"
version = "0.1.0"
description = "HTTP service that fetches web pages through a real Chromium browser to bypass bot walls and paywalls."
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "nodriver>=0.38",
    "pydantic>=2.7",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
    "aiohttp>=3.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/passe_partout"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "smoke: network-touching end-to-end test (run with -m smoke)",
]
addopts = "-m 'not smoke'"
testpaths = ["tests"]
```

- [ ] **Step 2: Remove the uv-default `hello.py` and create the package dir**

```bash
rm /home/jfim/projects/passe-partout/hello.py
mkdir -p /home/jfim/projects/passe-partout/src/passe_partout
touch /home/jfim/projects/passe-partout/src/passe_partout/__init__.py
mkdir -p /home/jfim/projects/passe-partout/tests
```

- [ ] **Step 3: Sync deps**

```bash
cd /home/jfim/projects/passe-partout && uv sync
```

Expected: `uv` creates `.venv/`, installs all deps, writes `uv.lock`.

- [ ] **Step 4: Verify pytest discovers an empty test suite**

Create `tests/test_smoketest.py` with one trivial passing test, then delete it after verifying:

```bash
cd /home/jfim/projects/passe-partout && echo 'def test_imports(): import passe_partout' > tests/test_smoketest.py
uv run pytest -v
```

Expected: PASS, 1 test. Then `rm tests/test_smoketest.py`.

- [ ] **Step 5: Commit**

```bash
cd /home/jfim/projects/passe-partout
git add -A
git commit -m "Bootstrap project: deps, src layout, pytest config"
```

---

## Task 2: Config module

**Files:**
- Create: `src/passe_partout/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
import pytest
from passe_partout.config import Config


def test_defaults(monkeypatch):
    for k in ("HOST", "PORT", "MAX_TABS", "IDLE_TIMEOUT_SECONDS",
              "AUTH_TOKEN", "UNPACKED_EXTENSION_DIRS"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.max_tabs == 10
    assert cfg.idle_timeout_seconds == 300
    assert cfg.auth_token is None
    assert cfg.extension_dirs == []


def test_overrides(monkeypatch, tmp_path):
    ext_a = tmp_path / "ext_a"
    ext_b = tmp_path / "ext_b"
    ext_a.mkdir(); ext_b.mkdir()
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("MAX_TABS", "3")
    monkeypatch.setenv("IDLE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("UNPACKED_EXTENSION_DIRS", f"{ext_a}:{ext_b}")
    cfg = Config.from_env()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9001
    assert cfg.max_tabs == 3
    assert cfg.idle_timeout_seconds == 60
    assert cfg.auth_token == "secret"
    assert cfg.extension_dirs == [str(ext_a), str(ext_b)]


def test_extension_dir_must_exist(monkeypatch):
    monkeypatch.setenv("UNPACKED_EXTENSION_DIRS", "/nonexistent/path")
    with pytest.raises(ValueError, match="not a directory"):
        Config.from_env()
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_config.py -v
```

Expected: ImportError / module not found.

- [ ] **Step 3: Implement `config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    max_tabs: int = 10
    idle_timeout_seconds: int = 300
    auth_token: str | None = None
    extension_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        raw_dirs = os.environ.get("UNPACKED_EXTENSION_DIRS", "")
        ext_dirs = [p for p in raw_dirs.split(":") if p]
        for p in ext_dirs:
            if not os.path.isdir(p):
                raise ValueError(f"UNPACKED_EXTENSION_DIRS entry is not a directory: {p}")
        return cls(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            max_tabs=int(os.environ.get("MAX_TABS", "10")),
            idle_timeout_seconds=int(os.environ.get("IDLE_TIMEOUT_SECONDS", "300")),
            auth_token=os.environ.get("AUTH_TOKEN") or None,
            extension_dirs=ext_dirs,
        )
```

- [ ] **Step 4: Run to confirm pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_config.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add Config dataclass with env-var parsing"
```

---

## Task 3: Browser pool

**Files:**
- Create: `src/passe_partout/browser_pool.py`
- Test: `tests/conftest.py`, `tests/fixtures/static.html`, `tests/fixtures/js.html`, `tests/fixtures/delayed.html`, `tests/test_browser_pool.py`

This task introduces the session-scoped browser fixture other tests will rely on.

- [ ] **Step 1: Create fixture HTML files**

`tests/fixtures/static.html`:
```html
<!doctype html><html><head><title>static</title></head>
<body><h1 id="hello">hello passe-partout</h1></body></html>
```

`tests/fixtures/js.html`:
```html
<!doctype html><html><head><title>before</title></head>
<body><script>document.title = "after"; document.body.dataset.ready = "1";</script></body></html>
```

`tests/fixtures/delayed.html`:
```html
<!doctype html><html><head><title>delayed</title></head>
<body><div id="root"></div>
<script>setTimeout(() => {
  const e = document.createElement("p");
  e.id = "later"; e.textContent = "appeared";
  document.getElementById("root").appendChild(e);
}, 500);</script></body></html>
```

- [ ] **Step 2: Write `tests/conftest.py` — fixtures shared across the suite**

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def fixture_server():
    async def handler(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = FIXTURES / f"{name}.html"
        if not path.exists():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{name}.html", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    await runner.cleanup()


@pytest_asyncio.fixture(scope="session")
async def browser_pool():
    cfg = Config()  # all defaults
    pool = BrowserPool(cfg)
    await pool.start()
    try:
        yield pool
    finally:
        await pool.stop()
```

- [ ] **Step 3: Write the failing `BrowserPool` tests**

`tests/test_browser_pool.py`:

```python
import pytest


async def test_pool_can_create_and_close_context(browser_pool, fixture_server):
    tab = await browser_pool.create_context(f"{fixture_server}/static.html")
    try:
        assert tab is not None
        html = await tab.get_content()
        assert "hello passe-partout" in html
    finally:
        await browser_pool.close_context(tab)


async def test_pool_contexts_are_isolated(browser_pool, fixture_server):
    tab_a = await browser_pool.create_context(f"{fixture_server}/static.html")
    tab_b = await browser_pool.create_context(f"{fixture_server}/static.html")
    try:
        await tab_a.evaluate("document.cookie = 'k=A; path=/'")
        cookies_b = await tab_b.evaluate("document.cookie")
        assert "k=A" not in (cookies_b or "")
    finally:
        await browser_pool.close_context(tab_a)
        await browser_pool.close_context(tab_b)
```

- [ ] **Step 4: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_browser_pool.py -v
```

Expected: ImportError on `BrowserPool`.

- [ ] **Step 5: Implement `browser_pool.py`**

```python
from __future__ import annotations

import nodriver as uc

from passe_partout.config import Config


class BrowserPool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._browser: uc.Browser | None = None

    async def start(self) -> None:
        browser_args: list[str] = []
        if self.cfg.extension_dirs:
            browser_args.append(
                "--load-extension=" + ",".join(self.cfg.extension_dirs)
            )
        self._browser = await uc.start(browser_args=browser_args, headless=True)

    async def stop(self) -> None:
        if self._browser is not None:
            self._browser.stop()
            self._browser = None

    async def create_context(self, url: str) -> uc.Tab:
        if self._browser is None:
            raise RuntimeError("BrowserPool not started")
        return await self._browser.create_context(url=url, new_window=False)

    async def close_context(self, tab: uc.Tab) -> None:
        await tab.close()
```

- [ ] **Step 6: Run to confirm pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_browser_pool.py -v
```

Expected: 2 tests pass. (First run downloads Chromium if not cached — may take ~60s.)

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Add BrowserPool wrapping nodriver, with isolation tests"
```

---

## Task 4: Tab registry

**Files:**
- Create: `src/passe_partout/tab_registry.py`
- Test: `tests/test_tab_registry.py`

This is a pure data structure — no browser needed for its tests. It tracks `(tab_object, last_used_at, ttl_seconds, lock)` keyed by monotonic int id.

- [ ] **Step 1: Write the failing tests**

`tests/test_tab_registry.py`:

```python
import asyncio
import time

import pytest
from passe_partout.tab_registry import TabRegistry, TabRecord


def test_register_assigns_monotonic_ids():
    reg = TabRegistry()
    a = reg.register(tab=object(), ttl_seconds=300)
    b = reg.register(tab=object(), ttl_seconds=300)
    assert b.id == a.id + 1


def test_get_returns_record():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    assert reg.get(rec.id).tab == "X"


def test_get_missing_returns_none():
    reg = TabRegistry()
    assert reg.get(999) is None


def test_remove_deletes():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    reg.remove(rec.id)
    assert reg.get(rec.id) is None


def test_touch_updates_last_used_at():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    original = rec.last_used_at
    time.sleep(0.01)
    reg.touch(rec.id)
    assert reg.get(rec.id).last_used_at > original


def test_idle_returns_expired_ids():
    reg = TabRegistry()
    rec1 = reg.register(tab="A", ttl_seconds=0)   # already expired
    rec2 = reg.register(tab="B", ttl_seconds=300)
    expired = reg.idle_ids(now=time.time() + 1)
    assert rec1.id in expired
    assert rec2.id not in expired


def test_count():
    reg = TabRegistry()
    assert reg.count() == 0
    reg.register(tab="A", ttl_seconds=300)
    reg.register(tab="B", ttl_seconds=300)
    assert reg.count() == 2


async def test_lock_is_per_tab():
    reg = TabRegistry()
    a = reg.register(tab="A", ttl_seconds=300)
    b = reg.register(tab="B", ttl_seconds=300)
    assert a.lock is not b.lock

    async with a.lock:
        # b.lock is independent — should be acquirable without waiting
        async with asyncio.wait_for(b.lock.acquire(), timeout=0.1):
            b.lock.release()
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_registry.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `tab_registry.py`**

```python
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TabRecord:
    id: int
    tab: Any
    created_at: float
    last_used_at: float
    ttl_seconds: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TabRegistry:
    def __init__(self) -> None:
        self._records: dict[int, TabRecord] = {}
        self._next_id: int = 1
        self._mu = asyncio.Lock()  # guards _records and _next_id

    def register(self, tab: Any, ttl_seconds: int) -> TabRecord:
        now = time.time()
        rec = TabRecord(
            id=self._next_id,
            tab=tab,
            created_at=now,
            last_used_at=now,
            ttl_seconds=ttl_seconds,
        )
        self._records[rec.id] = rec
        self._next_id += 1
        return rec

    def get(self, tab_id: int) -> TabRecord | None:
        return self._records.get(tab_id)

    def remove(self, tab_id: int) -> TabRecord | None:
        return self._records.pop(tab_id, None)

    def touch(self, tab_id: int) -> None:
        rec = self._records.get(tab_id)
        if rec is not None:
            rec.last_used_at = time.time()

    def count(self) -> int:
        return len(self._records)

    def all(self) -> list[TabRecord]:
        return list(self._records.values())

    def idle_ids(self, now: float | None = None) -> list[int]:
        now = time.time() if now is None else now
        return [
            rec.id
            for rec in self._records.values()
            if now - rec.last_used_at > rec.ttl_seconds
        ]

    @property
    def mu(self) -> asyncio.Lock:
        return self._mu
```

- [ ] **Step 4: Run to confirm pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_registry.py -v
```

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add TabRegistry tracking ids, locks, last-used timestamps"
```

---

## Task 5: FastAPI skeleton with lifespan, /healthz, and auth

**Files:**
- Create: `src/passe_partout/models.py`, `src/passe_partout/app.py`, `src/passe_partout/__main__.py`
- Test: `tests/test_health.py`
- Modify: `tests/conftest.py` (add an `httpx.AsyncClient` fixture pointing at the FastAPI app)

- [ ] **Step 1: Write `models.py` with the schemas this task needs**

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Cookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = None
    expires: float | None = None
    http_only: bool | None = Field(default=None, alias="httpOnly")
    secure: bool | None = None
    same_site: str | None = Field(default=None, alias="sameSite")

    model_config = {"populate_by_name": True}


class CreateTabRequest(BaseModel):
    url: str
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None


class TabSummary(BaseModel):
    id: int
    url: str
    created_at: float
    last_used_at: float


class TabState(BaseModel):
    url: str
    title: str
    ready_state: str


class CreateTabResponse(BaseModel):
    id: int
    status: int
    final_url: str


class FetchRequest(BaseModel):
    url: str
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None


class FetchResponse(BaseModel):
    status: int
    final_url: str
    html: str


class GotoRequest(BaseModel):
    url: str


class GotoResponse(BaseModel):
    status: int
    final_url: str


class ClickRequest(BaseModel):
    selector: str


class TypeRequest(BaseModel):
    selector: str
    text: str


class EvalRequest(BaseModel):
    js: str


class EvalResponse(BaseModel):
    result: Any


class WaitRequest(BaseModel):
    selector: str | None = None
    network_idle: bool | None = None
    timeout_ms: int | None = None  # default 5000 if unset


class HealthResponse(BaseModel):
    ok: bool
    browser: str
    tabs: int


class ErrorBody(BaseModel):
    error: str
    detail: str
```

- [ ] **Step 2: Write the failing health test**

`tests/test_health.py`:

```python
import pytest


async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["browser"] == "running"
    assert body["tabs"] == 0


async def test_auth_required_when_token_set(client_with_auth):
    client, token = client_with_auth
    r = await client.get("/tabs")
    assert r.status_code == 401

    r = await client.get("/tabs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_healthz_does_not_require_auth(client_with_auth):
    client, _ = client_with_auth
    r = await client.get("/healthz")
    assert r.status_code == 200
```

- [ ] **Step 3: Add `client` and `client_with_auth` fixtures to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
import httpx
import pytest_asyncio
from passe_partout.app import build_app
from passe_partout.config import Config


@pytest_asyncio.fixture
async def client(browser_pool):
    cfg = Config()
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest_asyncio.fixture
async def client_with_auth(browser_pool):
    cfg = Config(auth_token="secret123")
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c, "secret123"
```

(Note: we pass the already-running session-scoped `browser_pool` into the app so each test doesn't relaunch Chromium.)

- [ ] **Step 4: Implement `app.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config
from passe_partout.models import HealthResponse
from passe_partout.tab_registry import TabRegistry


def build_app(cfg: Config, browser_pool: BrowserPool | None = None) -> FastAPI:
    state_pool = browser_pool

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal state_pool
        owns_pool = state_pool is None
        if owns_pool:
            state_pool = BrowserPool(cfg)
            await state_pool.start()
        app.state.cfg = cfg
        app.state.pool = state_pool
        app.state.registry = TabRegistry()
        try:
            yield
        finally:
            if owns_pool and state_pool is not None:
                await state_pool.stop()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        token = cfg.auth_token
        if token and request.url.path != "/healthz":
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "detail": "invalid or missing token"},
                )
        return await call_next(request)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz():
        pool = app.state.pool
        registry = app.state.registry
        return HealthResponse(
            ok=True,
            browser="running" if pool is not None else "down",
            tabs=registry.count(),
        )

    return app
```

- [ ] **Step 5: Implement `__main__.py`**

```python
from __future__ import annotations

import uvicorn

from passe_partout.app import build_app
from passe_partout.config import Config


def main() -> None:
    cfg = Config.from_env()
    app = build_app(cfg=cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run tests**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_health.py -v
```

Expected: 3 tests pass.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Add FastAPI app skeleton with lifespan, /healthz, bearer auth"
```

---

## Task 6: POST /tabs and DELETE /tabs/{id} (with cookies, TTL, MAX_TABS cap)

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tabs.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_tabs.py`:

```python
import pytest


async def test_create_returns_id(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["id"], int)
    assert body["status"] == 200
    assert body["final_url"].endswith("/static.html")
    # cleanup
    await client.delete(f"/tabs/{body['id']}")


async def test_delete_removes_tab(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tab_id = r.json()["id"]
    r = await client.delete(f"/tabs/{tab_id}")
    assert r.status_code == 204
    r = await client.get(f"/tabs/{tab_id}")
    assert r.status_code == 404


async def test_create_with_cookies(client, fixture_server):
    r = await client.post(
        "/tabs",
        json={
            "url": f"{fixture_server}/static.html",
            "cookies": [{"name": "k", "value": "V", "domain": "127.0.0.1", "path": "/"}],
        },
    )
    assert r.status_code == 200
    tab_id = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tab_id}/eval", json={"js": "document.cookie"})
        assert "k=V" in (r.json()["result"] or "")
    finally:
        await client.delete(f"/tabs/{tab_id}")


async def test_max_tabs_cap_returns_429(client, fixture_server, monkeypatch):
    # Set cap to 1 by replacing the in-app config
    from passe_partout.config import Config
    client._transport.app.state.cfg = Config(max_tabs=1)

    r1 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    assert r1.status_code == 200
    try:
        r2 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
        assert r2.status_code == 429
    finally:
        await client.delete(f"/tabs/{r1.json()['id']}")
        client._transport.app.state.cfg = Config()


async def test_delete_unknown_returns_404(client):
    r = await client.delete("/tabs/9999")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tabs.py -v
```

Expected: 404 / NotFound errors on routes that don't exist yet.

- [ ] **Step 3: Implement the routes in `app.py`**

Add to `app.py` (inside `build_app`, after `healthz`):

```python
    from passe_partout.models import (
        CreateTabRequest, CreateTabResponse, TabSummary, TabState,
    )
    import nodriver as uc

    def _cookies_to_cdp(cookies):
        out = []
        for c in cookies or []:
            d = {"name": c.name, "value": c.value}
            if c.domain: d["domain"] = c.domain
            if c.path: d["path"] = c.path
            if c.expires is not None: d["expires"] = c.expires
            if c.http_only is not None: d["httpOnly"] = c.http_only
            if c.secure is not None: d["secure"] = c.secure
            if c.same_site is not None: d["sameSite"] = c.same_site
            out.append(d)
        return out

    @app.post("/tabs", response_model=CreateTabResponse)
    async def create_tab(req: CreateTabRequest):
        cfg_now = app.state.cfg
        registry: TabRegistry = app.state.registry
        pool: BrowserPool = app.state.pool

        async with registry.mu:
            if registry.count() >= cfg_now.max_tabs:
                return JSONResponse(
                    status_code=429,
                    content={"error": "max_tabs", "detail": f"cap of {cfg_now.max_tabs} reached"},
                )

        try:
            if req.cookies:
                # Create context first at about:blank, set cookies, then navigate
                tab = await pool.create_context("about:blank")
                cdp_cookies = _cookies_to_cdp(req.cookies)
                await tab.send(uc.cdp.network.set_cookies(cdp_cookies))
                await tab.get(req.url)
            else:
                tab = await pool.create_context(req.url)
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": "browser_error", "detail": str(e)},
            )

        ttl = req.ttl_seconds if req.ttl_seconds is not None else cfg_now.idle_timeout_seconds
        rec = registry.register(tab=tab, ttl_seconds=ttl)
        return CreateTabResponse(id=rec.id, status=200, final_url=tab.url or req.url)

    @app.delete("/tabs/{tab_id}", status_code=204)
    async def delete_tab(tab_id: int):
        registry: TabRegistry = app.state.registry
        pool: BrowserPool = app.state.pool
        rec = registry.remove(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        try:
            await pool.close_context(rec.tab)
        except Exception:
            pass
        return Response(status_code=204)

    @app.get("/tabs/{tab_id}", response_model=TabState)
    async def get_tab(tab_id: int):
        registry: TabRegistry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        registry.touch(tab_id)
        title = await rec.tab.evaluate("document.title")
        ready = await rec.tab.evaluate("document.readyState")
        return TabState(url=rec.tab.url or "", title=title or "", ready_state=ready or "")
```

(`get_tab` is implemented now since the delete-then-404 test needs it; the dedicated GET tests come in Task 7.)

- [ ] **Step 4: Run tests**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tabs.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add POST /tabs, DELETE /tabs/{id}, GET /tabs/{id} with cookies + cap"
```

---

## Task 7: GET /tabs (list)

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tabs.py` (extend)

- [ ] **Step 1: Append the failing test to `tests/test_tabs.py`**

```python
async def test_list_tabs(client, fixture_server):
    r1 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    r2 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    try:
        r = await client.get("/tabs")
        assert r.status_code == 200
        ids = {t["id"] for t in r.json()}
        assert r1.json()["id"] in ids
        assert r2.json()["id"] in ids
        for t in r.json():
            assert {"id", "url", "created_at", "last_used_at"}.issubset(t.keys())
    finally:
        await client.delete(f"/tabs/{r1.json()['id']}")
        await client.delete(f"/tabs/{r2.json()['id']}")
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tabs.py::test_list_tabs -v
```

Expected: 405 or 404 on GET /tabs.

- [ ] **Step 3: Add the route in `app.py`**

```python
    @app.get("/tabs", response_model=list[TabSummary])
    async def list_tabs():
        registry: TabRegistry = app.state.registry
        return [
            TabSummary(
                id=rec.id,
                url=getattr(rec.tab, "url", "") or "",
                created_at=rec.created_at,
                last_used_at=rec.last_used_at,
            )
            for rec in registry.all()
        ]
```

- [ ] **Step 4: Run, expect pass; commit**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tabs.py -v
git add -A && git commit -m "Add GET /tabs"
```

---

## Task 8: Read endpoints — html, cookies, screenshot

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tab_ops.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_tab_ops.py`:

```python
import pytest


async def test_get_html(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/html")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "hello passe-partout" in r.text
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_get_html_404_unknown(client):
    r = await client.get("/tabs/9999/html")
    assert r.status_code == 404


async def test_get_cookies(client, fixture_server):
    r = await client.post(
        "/tabs",
        json={
            "url": f"{fixture_server}/static.html",
            "cookies": [{"name": "k", "value": "V", "domain": "127.0.0.1", "path": "/"}],
        },
    )
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/cookies")
        assert r.status_code == 200
        names = {c["name"] for c in r.json()}
        assert "k" in names
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_get_screenshot(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/screenshot")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await client.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_ops.py -v
```

Expected: 404 on missing routes.

- [ ] **Step 3: Add routes in `app.py`**

```python
    import base64
    from fastapi.responses import HTMLResponse

    async def _require_tab(tab_id: int):
        registry: TabRegistry = app.state.registry
        rec = registry.get(tab_id)
        if rec is None:
            return None
        registry.touch(tab_id)
        return rec

    @app.get("/tabs/{tab_id}/html")
    async def get_html(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            html = await rec.tab.get_content()
        return HTMLResponse(content=html)

    @app.get("/tabs/{tab_id}/cookies")
    async def get_cookies(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            raw = await rec.tab.send(uc.cdp.network.get_cookies())
        out = []
        for c in raw:
            out.append({
                "name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires,
                "httpOnly": c.http_only, "secure": c.secure,
                "sameSite": c.same_site.to_json() if c.same_site else None,
            })
        return out

    @app.get("/tabs/{tab_id}/screenshot")
    async def get_screenshot(tab_id: int):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={"error": "tab_not_found", "detail": f"no tab with id {tab_id}"},
            )
        async with rec.lock:
            b64 = await rec.tab.send(uc.cdp.page.capture_screenshot(format_="png"))
        return Response(content=base64.b64decode(b64), media_type="image/png")
```

- [ ] **Step 4: Run; expect pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_ops.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add GET /tabs/{id}/{html,cookies,screenshot}"
```

---

## Task 9: Interaction endpoints — goto, click, type, eval, wait

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tab_ops.py` (extend)

- [ ] **Step 1: Append the failing tests**

```python
async def test_goto(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/js.html"})
        assert r.status_code == 200
        assert r.json()["final_url"].endswith("/js.html")
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_eval_and_click(client, fixture_server):
    # Build a page where a click changes a value we can read with eval
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(
            f"/tabs/{tid}/eval",
            json={"js": "document.body.insertAdjacentHTML('beforeend', '<button id=b>x</button>'); document.getElementById('b').addEventListener('click', () => { document.body.dataset.clicked = 'yes'; });"},
        )
        assert r.status_code == 200

        r = await client.post(f"/tabs/{tid}/click", json={"selector": "#b"})
        assert r.status_code == 204

        r = await client.post(f"/tabs/{tid}/eval", json={"js": "document.body.dataset.clicked"})
        assert r.json()["result"] == "yes"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_type(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        await client.post(
            f"/tabs/{tid}/eval",
            json={"js": "document.body.insertAdjacentHTML('beforeend', '<input id=i>')"},
        )
        r = await client.post(f"/tabs/{tid}/type", json={"selector": "#i", "text": "hello"})
        assert r.status_code == 204
        r = await client.post(f"/tabs/{tid}/eval", json={"js": "document.getElementById('i').value"})
        assert r.json()["result"] == "hello"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_for_selector(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={"selector": "#later", "timeout_ms": 3000})
        assert r.status_code == 204
        r = await client.post(f"/tabs/{tid}/eval", json={"js": "document.getElementById('later').textContent"})
        assert r.json()["result"] == "appeared"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_network_idle(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={"network_idle": True, "timeout_ms": 3000})
        assert r.status_code == 204
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_both_selector_and_network_idle(client, fixture_server):
    # AND semantics: both must be satisfied. delayed.html settles network ~immediately
    # and adds #later after 500ms, so this should pass once #later exists.
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(
            f"/tabs/{tid}/wait",
            json={"selector": "#later", "network_idle": True, "timeout_ms": 3000},
        )
        assert r.status_code == 204
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_requires_one_condition(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={})
        assert r.status_code == 400
    finally:
        await client.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run; expect failure**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_ops.py -v
```

- [ ] **Step 3: Implement routes in `app.py`**

```python
    import asyncio as _asyncio
    from passe_partout.models import (
        GotoRequest, GotoResponse, ClickRequest, TypeRequest,
        EvalRequest, EvalResponse, WaitRequest,
    )

    @app.post("/tabs/{tab_id}/goto", response_model=GotoResponse)
    async def goto(tab_id: int, req: GotoRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                await rec.tab.get(req.url)
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return GotoResponse(status=200, final_url=rec.tab.url or req.url)

    @app.post("/tabs/{tab_id}/click", status_code=204)
    async def click(tab_id: int, req: ClickRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                el = await rec.tab.select(req.selector)
                await el.click()
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return Response(status_code=204)

    @app.post("/tabs/{tab_id}/type", status_code=204)
    async def type_(tab_id: int, req: TypeRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                el = await rec.tab.select(req.selector)
                await el.send_keys(req.text)
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return Response(status_code=204)

    @app.post("/tabs/{tab_id}/eval", response_model=EvalResponse)
    async def eval_js(tab_id: int, req: EvalRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                result = await rec.tab.evaluate(req.js)
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return EvalResponse(result=result)

    @app.post("/tabs/{tab_id}/wait", status_code=204)
    async def wait(tab_id: int, req: WaitRequest):
        if not req.selector and not req.network_idle:
            return JSONResponse(
                status_code=400,
                content={"error": "bad_request", "detail": "provide selector and/or network_idle"},
            )
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})

        timeout_s = (req.timeout_ms or 5000) / 1000.0
        async with rec.lock:
            try:
                async def _wait_selector():
                    await rec.tab.wait_for(selector=req.selector, timeout=timeout_s)

                async def _wait_network_idle():
                    # Track in-flight requests via CDP Network domain. Idle = 0
                    # in-flight requests sustained for 500ms.
                    inflight = 0
                    idle_event = _asyncio.Event()
                    last_zero_at = _asyncio.get_event_loop().time()

                    def _on_request(_e):
                        nonlocal inflight
                        inflight += 1
                        idle_event.clear()

                    def _on_done(_e):
                        nonlocal inflight, last_zero_at
                        inflight = max(0, inflight - 1)
                        if inflight == 0:
                            last_zero_at = _asyncio.get_event_loop().time()

                    rec.tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)
                    rec.tab.add_handler(uc.cdp.network.LoadingFinished, _on_done)
                    rec.tab.add_handler(uc.cdp.network.LoadingFailed, _on_done)
                    await rec.tab.send(uc.cdp.network.enable())
                    try:
                        deadline = _asyncio.get_event_loop().time() + timeout_s
                        while _asyncio.get_event_loop().time() < deadline:
                            now = _asyncio.get_event_loop().time()
                            if inflight == 0 and (now - last_zero_at) >= 0.5:
                                return
                            await _asyncio.sleep(0.05)
                        raise _asyncio.TimeoutError()
                    finally:
                        # Best-effort handler cleanup; nodriver's add_handler API
                        # may not expose remove. Disabling Network is sufficient
                        # for the next wait since handlers are scoped to events.
                        pass

                tasks = []
                if req.selector:
                    tasks.append(_wait_selector())
                if req.network_idle:
                    tasks.append(_wait_network_idle())
                # AND semantics: all conditions must be satisfied.
                await _asyncio.wait_for(_asyncio.gather(*tasks), timeout=timeout_s)
            except (_asyncio.TimeoutError, TimeoutError):
                return JSONResponse(status_code=408, content={"error": "timeout", "detail": "wait timed out"})
            except Exception as e:
                return JSONResponse(status_code=502, content={"error": "browser_error", "detail": str(e)})
        return Response(status_code=204)
```

- [ ] **Step 4: Run; expect pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_tab_ops.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add interaction endpoints: goto, click, type, eval, wait"
```

---

## Task 10: POST /fetch sugar

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_fetch.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_fetch.py`:

```python
import pytest


async def test_fetch_returns_rendered_html(client, fixture_server):
    r = await client.post("/fetch", json={"url": f"{fixture_server}/js.html"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert body["final_url"].endswith("/js.html")
    # JS sets data-ready=1 — proves we got rendered, not raw, HTML
    assert 'data-ready="1"' in body["html"]


async def test_fetch_disposes_tab(client, fixture_server):
    r1 = await client.get("/tabs")
    before = len(r1.json())
    r = await client.post("/fetch", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200
    r2 = await client.get("/tabs")
    after = len(r2.json())
    assert after == before, "fetch should dispose its tab"
```

- [ ] **Step 2: Run; expect failure**

- [ ] **Step 3: Implement `/fetch` in `app.py`**

```python
    from passe_partout.models import FetchRequest, FetchResponse

    @app.post("/fetch", response_model=FetchResponse)
    async def fetch(req: FetchRequest):
        create_req = CreateTabRequest(url=req.url, cookies=req.cookies, ttl_seconds=req.ttl_seconds)
        created = await create_tab(create_req)
        # If create_tab returned a JSONResponse (error), surface it directly
        if isinstance(created, JSONResponse):
            return created
        tid = created.id
        registry: TabRegistry = app.state.registry
        pool: BrowserPool = app.state.pool
        rec = registry.get(tid)
        try:
            async with rec.lock:
                html = await rec.tab.get_content()
            return FetchResponse(status=200, final_url=rec.tab.url or req.url, html=html)
        finally:
            registry.remove(tid)
            try:
                await pool.close_context(rec.tab)
            except Exception:
                pass
```

- [ ] **Step 4: Run; expect pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_fetch.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add POST /fetch convenience endpoint"
```

---

## Task 11: Idle sweeper

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_idle_sweeper.py`

- [ ] **Step 1: Write the failing test**

`tests/test_idle_sweeper.py`:

```python
import asyncio
import pytest


async def test_idle_tab_is_swept(client, fixture_server):
    # ttl_seconds=1 so it expires fast; sweeper wakes every 30s by default,
    # so we'll trigger it manually via the kept-around helper.
    r = await client.post(
        "/tabs",
        json={"url": f"{fixture_server}/static.html", "ttl_seconds": 1},
    )
    tid = r.json()["id"]
    await asyncio.sleep(1.2)

    # Force a sweep tick rather than waiting 30s
    sweep = client._transport.app.state.sweep_once
    await sweep()

    r = await client.get(f"/tabs/{tid}")
    assert r.status_code == 404
```

- [ ] **Step 2: Run; expect failure (route returns 200 still, no sweeper)**

- [ ] **Step 3: Add sweeper to `app.py`**

Modify the `lifespan` and add a sweeper function.

```python
    import asyncio

    async def sweep_once():
        registry: TabRegistry = app.state.registry
        pool: BrowserPool = app.state.pool
        for tid in registry.idle_ids():
            rec = registry.remove(tid)
            if rec is not None:
                try:
                    await pool.close_context(rec.tab)
                except Exception:
                    pass

    async def sweeper_loop():
        while True:
            try:
                await sweep_once()
            except Exception:
                pass
            await asyncio.sleep(30)
```

Update `lifespan` body to start/stop the sweeper:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal state_pool
        owns_pool = state_pool is None
        if owns_pool:
            state_pool = BrowserPool(cfg)
            await state_pool.start()
        app.state.cfg = cfg
        app.state.pool = state_pool
        app.state.registry = TabRegistry()
        app.state.sweep_once = sweep_once
        sweeper_task = asyncio.create_task(sweeper_loop())
        try:
            yield
        finally:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except asyncio.CancelledError:
                pass
            if owns_pool and state_pool is not None:
                await state_pool.stop()
```

- [ ] **Step 4: Run; expect pass**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest tests/test_idle_sweeper.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add idle sweeper closing tabs past their TTL"
```

---

## Task 12: Smoke test + README

**Files:**
- Create: `tests/test_smoke.py`
- Modify: `README.md`

- [ ] **Step 1: Write the smoke test**

`tests/test_smoke.py`:

```python
import os
import pytest
import httpx

from passe_partout.app import build_app
from passe_partout.config import Config


SMOKE_URL = "https://files.jean-francois.im/passe-partout-test.html"


@pytest.mark.smoke
async def test_smoke_against_personal_site(browser_pool):
    cfg = Config()
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            r = await c.post("/fetch", json={"url": SMOKE_URL})
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == 200
            assert "<html" in body["html"].lower()
```

(Marker assertion is intentionally minimal — the only contract is "we get rendered HTML back". Tighten later by putting a known marker on the page.)

- [ ] **Step 2: Run the smoke test explicitly**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest -m smoke -v
```

Expected: PASS (assuming the fixture URL is reachable and serves HTML).

- [ ] **Step 3: Verify default test run still skips it**

```bash
cd /home/jfim/projects/passe-partout && uv run pytest -v
```

Expected: smoke test deselected.

- [ ] **Step 4: Write `README.md`**

```markdown
# passe-partout

HTTP service that fetches and interacts with web pages through a real Chromium browser.

## Why

Some sites (Cloudflare, paywalls, JS-only rendering) reject plain HTTP clients. passe-partout fronts them with a real browser via [nodriver](https://github.com/ultrafunkamsterdam/nodriver) and exposes a small REST API any project can call.

## Run

```bash
uv sync
uv run python -m passe_partout
```

## Configuration (env vars)

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | |
| `PORT` | `8000` | |
| `MAX_TABS` | `10` | |
| `IDLE_TIMEOUT_SECONDS` | `300` | per-tab override via `ttl_seconds` on creation |
| `AUTH_TOKEN` | unset | when set, all routes except `/healthz` require `Authorization: Bearer <token>` |
| `UNPACKED_EXTENSION_DIRS` | unset | `:`-separated paths to unpacked Chromium extensions to load at launch |

## API

See `docs/superpowers/specs/2026-04-29-passe-partout-design.md` for the full surface.

Quickstart:

```bash
# one-shot
curl -X POST localhost:8000/fetch -H 'content-type: application/json' \
     -d '{"url":"https://example.com"}'

# stateful tab
curl -X POST localhost:8000/tabs -H 'content-type: application/json' \
     -d '{"url":"https://example.com"}'   # → {"id": 1, ...}
curl localhost:8000/tabs/1/html
curl -X DELETE localhost:8000/tabs/1
```
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add smoke test and README"
```

---

## Done

After Task 12, the full test suite (`uv run pytest`) should pass and `uv run python -m passe_partout` should serve the API. The smoke test (`uv run pytest -m smoke`) verifies end-to-end behavior against your personal site.
