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
- **`DownloadCoordinator` (`downloads.py`)** — lives beside `BrowserPool`/`TabRegistry`, owned by `app.state`. Manages per-tab download directories at `<DOWNLOAD_DIR>/passe-partout/tab-<id>/` and cleans them up on tab close. Hooks `Browser.setDownloadBehavior` per browser context, and listens to `Browser.downloadWillBegin`/`Browser.downloadProgress` events to track download lifecycle. Also intercepts non-HTML main-frame responses via `Fetch.requestPaused`, rewriting `Content-Disposition` to `attachment` so the browser treats them as downloads rather than renders. Bumps `last_used_at` on download progress events to prevent the tab sweeper from evicting tabs with active downloads.
- **`ResourceRecorder` (`resources.py`)** — owned by `app.state`, attached to every tab in `create_tab`. Calls `Network.enable` so Chrome retains response bodies in its network process, then subscribes to `Network.responseReceived` (metadata) and `Network.loadingFinished` (final byte count) to populate `TabRecord.resources` (`request_id → ResourceRecord`). Bodies are pulled on demand via `Network.getResponseBody` from the route handler — passe-partout never copies the bytes itself. Each `ResourceRecord` carries the main-frame `loader_id` from its responseReceived; on `Page.frameNavigated` for the top-level frame, `_on_frame_navigated` prunes entries from older loaders so the dict stays bounded over a long-lived tab. Worker-served responses (empty `loader_id`) are kept across navigations. These captured resources are exported via `GET /tabs/{id}/warc` (`warc.py`'s `build_warc`). Two optional, independent query params each add a `conversion` record that `WARC-Refers-To` the main-document response: `?rendered=1` embeds the per-frame rendered DOM + screenshot (`rendered.py`); it also serializes shadow DOM (`DOM.getOuterHTML(includeShadowDOM=true)`) and folds CSSOM-only state into each frame's `renderedContent` via `cssom.py`: adopted/constructed stylesheets and CSSOM-mutated `<style>` rules are spliced into the serialized HTML by byte-preserving span splicing (no re-serialization), with adopted sheets inserted last in their scope to match the cascade. Closed shadow roots' adopted/CSSOM state can't be reached by JS and is omitted (their structure is still captured). passe-partout's own capture JS (owner-selector, scroll, and the `GET /tabs/{id}` title/readyState reads) runs in an isolated world via `isolated.py` so a hostile page can't tamper with it; `POST /tabs/{id}/eval` accepts a `world` field (`main` default | `isolated`). `?domsnapshot=1` embeds a CDP `DOMSnapshot.captureSnapshot` result (`domsnapshot.py`, profile `urn:passe-partout:warc:dom-snapshot:1.0`). The `domsnapshot` capture takes an optional `computed_styles` param (comma-separated CSS property names) passed verbatim to CDP as the `computedStyles` array and recorded in the `X-Passe-Partout-Computed-Styles` WARC header; an empty list yields a structure-only snapshot. `?dom_rects=1` and `?paint_order=1` (both default off) add the layout tree's optional `offsetRects`/`scrollRects`/`clientRects` and `paintOrders` respectively (the absolute `bounds` box is always present regardless). Both captures silently degrade (no record) on CDP failure or a missing main-doc response.
- **`BehaviorCatalog` (`behaviors.py`)** — owns the catalog of replayable wheel-scroll behaviors: one honestly-synthetic built-in (`scroll-down`, evenly-spaced) plus traces loaded at startup from `BEHAVIOR_TRACE_DIR` (operator-private; never shipped, to avoid a shared cross-client fingerprint). `GET /behaviors` lists them; `POST /tabs/{id}/behaviors/play` replays one burst against a tab via trusted CDP `Input.dispatchMouseEvent{mouseWheel}`, perturbing the trace (time-warp/delta-scale, seedable) on each replay. The endpoint is a thin mechanism: all policy — content extraction, stop condition, settle (network-idle), WARC — stays in the client, which reads geometry via `POST /tabs/{id}/eval` in the isolated world. Input must be CDP-side because in-page JS can't reach CDP and JS-synthesized events are `isTrusted:false`.

Per-tab TTL defaults to `IDLE_TAB_CLOSE_SECONDS` and can be overridden per request via `ttl_seconds`. Two distinct timers exist and should not be conflated: `IDLE_TAB_CLOSE_SECONDS` (per-tab inactivity) vs `IDLE_CHROME_SHUTDOWN_SECONDS` (whole-browser shutdown when zero tabs remain). `DOWNLOAD_DIR` (default `/tmp`) sets the base directory for downloaded files. `BEHAVIOR_TRACE_DIR` (unset by default) points at a directory of operator-recorded behavior trace JSON files (`{"kind": ..., "steps": [[dx, dy, dt_ms], ...]}`), loaded at startup and surfaced via `GET /behaviors`.

`SHARED_PROFILE=1` opts every tab into the default Chrome profile (cookies/storage not isolated between callers) instead of each getting its own incognito-style context. Defaults to `0`. Required when `UNPACKED_EXTENSION_DIRS` is non-empty — Chrome doesn't run `--load-extension` extensions in incognito contexts, so `Config.from_env` aborts with a `ValueError` if extensions are configured but `SHARED_PROFILE` isn't on. The opt-in is explicit (not auto-inferred from `UNPACKED_EXTENSION_DIRS`) because the cross-tab cookie sharing it implies is a meaningful security/privacy posture change.

In shared mode the download coordinator stops using per-tab subdirectories and points every tab at `<DOWNLOAD_DIR>/passe-partout/shared/`. Concurrent downloads still don't collide because `Browser.setDownloadBehavior` is set to `allowAndName`, which makes Chrome write each file as `<dir>/<guid>` (CDP guids are globally unique). `cleanup_tab_dir` walks `_tab_lookup` and unlinks only that tab's guids instead of `rmtree`-ing the shared directory.

Auth is a single bearer token (`AUTH_TOKEN`) enforced by middleware; `/healthz` is exempt so Docker's healthcheck works without it.

`DELETE /browser` force-stops Chromium iff `registry.count() == 0`, returning 409 otherwise. The check-and-stop is race-safe via `BrowserPool.stop_if_idle`, which inspects `_active` and stops under a single `_lock` acquisition (a racing `create_context` either bumps `_active` first or runs after teardown and lazily restarts). `GET /browser` is a static config snapshot (`BrowserInfo`) — `running`, `tab_count`, `headless`, `shared_profile`, `extension_dirs`, `chrome_path`.

## Testing notes

- `tests/conftest.py` provides a session-scoped `browser_pool` fixture that launches a real Chromium — most tests touch it. `client` and `client_with_auth` build the FastAPI app over an in-memory ASGI transport (httpx), reusing the shared pool.
- Tests marked `@pytest.mark.smoke` hit the public internet and are deselected by default (see `pyproject.toml`'s `addopts`). Run them explicitly with `-m smoke`.
- `tests/fixtures/` has small static HTML pages served by a local aiohttp test server (`fixture_server` fixture) — prefer these over real URLs for new tests.
- `test_idle_chrome_shutdown.py` monkeypatches `nodriver.start` with a fake browser to exercise pool lifecycle without launching real Chromium; mirror that pattern for any pool-state tests.

## Conventions

- Python 3.12+, `from __future__ import annotations` at the top of every module.
- All public types live in `models.py` as Pydantic models; route handlers return them directly so FastAPI handles serialization. Error responses are bare `JSONResponse` of `{"error": "<code>", "detail": "<msg>"}`.
- Ruff config is in `pyproject.toml` (`E,W,F,I,B,UP`, line-length 100, `E501` ignored because the formatter handles it). Tests are exempt from `B` rules.
