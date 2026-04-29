# passe-partout — design

**Status:** approved (2026-04-29)

## Purpose

A small HTTP service that fronts a real Chromium browser, exposing a tab-based REST API so other projects (in any language) can fetch and interact with web pages that block plain HTTP clients (datacenter IPs, missing browser fingerprints, paywalls, JS-rendered content).

Sibling to `passeur` — `passeur` ferries; `passe-partout` is the skeleton key that gets through closed doors.

## Stack

- Python ≥ 3.12, `uv` for dependency management
- FastAPI (async, matches nodriver's asyncio model)
- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) — undetected Chromium driver via direct CDP
- Chromium (system or auto-fetched by nodriver)
- Optional unpacked Chrome extensions loaded at launch via `--load-extension=`

## Project layout

```
passe-partout/
├── pyproject.toml
├── README.md
├── src/passe_partout/
│   ├── __init__.py
│   ├── app.py            # FastAPI app, route definitions
│   ├── browser_pool.py   # singleton Browser, idle sweeper, MAX_TABS cap
│   ├── tab_registry.py   # int id ↔ (Tab, BrowserContext, last_used_at, lock)
│   ├── config.py         # env-var parsing
│   └── models.py         # Pydantic request/response schemas
└── tests/
    ├── conftest.py
    ├── fixtures/         # static HTML pages served by aiohttp test server
    ├── test_tabs.py
    ├── test_fetch.py
    └── test_smoke.py     # hits https://files.jean-francois.im/passe-partout-test.html
```

## Configuration (env vars)

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Set to `0.0.0.0` for LAN exposure |
| `PORT` | `8000` | |
| `MAX_TABS` | `10` | Hard cap; `POST /tabs` returns 429 above this |
| `IDLE_TIMEOUT_SECONDS` | `300` | Default per-tab idle TTL; per-tab override on creation |
| `AUTH_TOKEN` | unset | When set, all routes except `/healthz` require `Authorization: Bearer <token>` |
| `UNPACKED_EXTENSION_DIRS` | unset | `:`-separated list of unpacked extension directory paths |

## Lifecycle

**Startup**
1. Parse env vars.
2. If `UNPACKED_EXTENSION_DIRS` is set, validate each path is a directory; build a comma-joined `--load-extension=` argument.
3. Launch one nodriver `Browser` with the extension flag (if any) and any other necessary stealth flags nodriver sets by default.
4. Start the idle-sweeper async task (runs every 30s).
5. FastAPI starts accepting requests.

**Per request (`POST /tabs`)**
1. If `len(tabs) >= MAX_TABS`, return 429.
2. `browser.create_context(url, new_window=False)` — isolated cookie/storage context.
3. If `cookies` provided, set them via CDP after context creation.
4. Assign a monotonic int id, register `(tab, context, last_used_at=now, lock, ttl_seconds)`.
5. Return `{id, status, final_url}`.

**Idle sweeper**
- Every 30s: for each tab where `now - last_used_at > ttl_seconds`, close the context and remove from registry.
- Touching a tab (any non-DELETE op against it) updates `last_used_at`.

**Shutdown**
- Cancel sweeper, close all contexts, stop the browser.

## API

```
POST   /fetch                  {url, cookies?, ttl_seconds?}
                               → {status, final_url, html}
                               # convenience: create context, get HTML, dispose

POST   /tabs                   {url, cookies?, ttl_seconds?}
                               → {id, status, final_url}
GET    /tabs                   → [{id, url, created_at, last_used_at}]
GET    /tabs/{id}              → {url, title, ready_state}
DELETE /tabs/{id}              → 204

GET    /tabs/{id}/html         → text/html
GET    /tabs/{id}/screenshot   → image/png
GET    /tabs/{id}/cookies      → [{name, value, domain, path, expires, http_only, secure, same_site}]

POST   /tabs/{id}/goto         {url}                → {status, final_url}
POST   /tabs/{id}/click        {selector}           → 204
POST   /tabs/{id}/type         {selector, text}     → 204
POST   /tabs/{id}/eval         {js}                 → {result}             # result is JSON-serialized return value
POST   /tabs/{id}/wait         {selector?, ms?}     → 204                  # one of selector or ms required

GET    /healthz                → {ok: true, browser: "running" | "down", tabs: N}
```

**Cookie shape (input and output):** standard CDP cookie objects — `{name, value, domain, path?, expires?, http_only?, secure?, same_site?}`. Pydantic schema in `models.py`.

## Concurrency

- Single global `Browser` (one Chromium process tree).
- Each tab is one `BrowserContext` — fully isolated cookie jar/storage.
- FastAPI handlers are async. Per-tab operations serialize on an `asyncio.Lock` keyed by tab id, preventing `click` + `eval` + `html` from racing on the same tab.
- The tab registry itself is protected by a separate lock for create/delete/list.

## Errors

| Status | When |
| --- | --- |
| 400 | Malformed request (Pydantic validation) |
| 401 | `AUTH_TOKEN` configured and missing or wrong |
| 404 | Unknown tab id |
| 408 | `wait` or `goto` timed out |
| 429 | `MAX_TABS` cap hit on `POST /tabs` |
| 502 | nodriver/CDP raised an error during the operation |

Error body: `{error: "<short_code>", detail: "<human message>"}`.

## Testing

- `httpx.AsyncClient` against the FastAPI app via `lifespan` context.
- `aiohttp` test server in `conftest.py` serves static fixture pages (one HTML doc, one with JS that flips `document.title`, one with a delayed-render element for `wait` tests).
- `test_tabs.py` — full lifecycle: create, list, html, click, eval, delete; isolation between two tabs (cookie set in tab A not visible in tab B); 429 on cap; 404 on unknown id; idle-sweeper closes a tab past its TTL.
- `test_fetch.py` — `POST /fetch` returns rendered HTML and disposes the underlying context.
- `test_smoke.py` — single network test against `https://files.jean-francois.im/passe-partout-test.html`, asserts 200 and a known marker substring in the HTML. Marked `@pytest.mark.smoke`, run via `pytest -m smoke` (default pytest invocation skips it).

## Out of scope (v1)

- Shared cookie/session abstraction across tabs (`/sessions`). Add later if needed; per-tab `cookies` on creation covers the main reuse case.
- Per-tab proxy configuration (nodriver supports it via `create_context(proxy_server=…)`; trivial to add as a tab-creation field later).
- Network interception / request blocking.
- Multi-browser pools (multiple Chromium processes).
- Persistent contexts (resume across server restarts).
- Authentication beyond a single shared bearer token.
