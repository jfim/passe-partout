# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency management. Always run tools through `uv run` so the locked environment is used.

```bash
uv sync                              # install/refresh deps from uv.lock
uv run python -m passe_partout       # start the API on $HOST:$PORT (defaults 127.0.0.1:8000)

uv run pytest                        # full suite minus smoke (default addopts: -m 'not smoke')
uv run pytest -m smoke               # network-touching end-to-end tests
uv run pytest tests/test_app.py::test_name   # single test
uv run pytest -k pattern             # by name pattern

uv run ruff check .                  # lint
uv run ruff check --fix .            # lint + auto-fix
uv run ruff format .                 # format
uv run ruff format --check .         # CI-style format verification
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, and `pytest` on push/PR. Pre-commit hooks (`.pre-commit-config.yaml`) run ruff lint+format only — pytest is intentionally left to CI to keep commits fast.

## Architecture

The service exposes a FastAPI app that drives a single shared Chromium instance through `nodriver`. Three collaborating components, all wired together in `app.build_app`:

- **`BrowserPool` (`browser_pool.py`)** — owns the Chromium process. Lazy-start by default (first `create_context` call launches Chromium); when `IDLE_CHROME_SHUTDOWN_SECONDS > 0`, an idle-shutdown task stops Chromium after the last context closes and waits out the timeout. Setting that var to `0` reverts to always-on with eager startup at app lifespan start. All start/stop transitions and the active-context counter are guarded by a single `asyncio.Lock`; the idle task re-checks `_active == 0` after sleeping to avoid racing an arriving request. Each tab uses an isolated incognito-style "context" (`new_window=True`) so cookies and storage don't leak between callers.
- **`TabRegistry` (`tab_registry.py`)** — in-memory `id → TabRecord` map for stateful `/tabs/{id}/...` routes. Each record carries a per-tab `asyncio.Lock` so concurrent requests against the same tab serialize at the route handler (every multi-step route does `async with rec.lock:`). `last_used_at` is bumped via `touch()` on each access; `idle_ids()` reports tabs past their TTL.
- **`app.py`** — defines all routes plus a background sweeper task started in the lifespan that calls `sweep_once()` every 30s to evict expired tabs through `BrowserPool.close_context`. `POST /fetch` is implemented as a thin wrapper that calls the same `create_tab` handler then deletes the tab in a `finally`.

Per-tab TTL defaults to `IDLE_TAB_CLOSE_SECONDS` and can be overridden per request via `ttl_seconds`. Two distinct timers exist and should not be conflated: `IDLE_TAB_CLOSE_SECONDS` (per-tab inactivity) vs `IDLE_CHROME_SHUTDOWN_SECONDS` (whole-browser shutdown when zero tabs remain).

Auth is a single bearer token (`AUTH_TOKEN`) enforced by middleware; `/healthz` is exempt so Docker's healthcheck works without it.

## Testing notes

- `tests/conftest.py` provides a session-scoped `browser_pool` fixture that launches a real Chromium — most tests touch it. `client` and `client_with_auth` build the FastAPI app over an in-memory ASGI transport (httpx), reusing the shared pool.
- Tests marked `@pytest.mark.smoke` hit the public internet and are deselected by default (see `pyproject.toml`'s `addopts`). Run them explicitly with `-m smoke`.
- `tests/fixtures/` has small static HTML pages served by a local aiohttp test server (`fixture_server` fixture) — prefer these over real URLs for new tests.
- `test_idle_chrome_shutdown.py` monkeypatches `nodriver.start` with a fake browser to exercise pool lifecycle without launching real Chromium; mirror that pattern for any pool-state tests.

## Conventions

- Python 3.12+, `from __future__ import annotations` at the top of every module.
- All public types live in `models.py` as Pydantic models; route handlers return them directly so FastAPI handles serialization. Error responses are bare `JSONResponse` of `{"error": "<code>", "detail": "<msg>"}`.
- Ruff config is in `pyproject.toml` (`E,W,F,I,B,UP`, line-length 100, `E501` ignored because the formatter handles it). Tests are exempt from `B` rules.
