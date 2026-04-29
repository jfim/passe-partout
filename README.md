# passe-partout

HTTP service that fetches and interacts with web pages through a real Chromium browser.

## Why

Some sites (Cloudflare, paywalls, JS-only rendering) reject plain HTTP clients. passe-partout fronts them with a real browser via [nodriver](https://github.com/ultrafunkamsterdam/nodriver) and exposes a small REST API any project can call.

## Run

    uv sync
    uv run python -m passe_partout

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

    # one-shot
    curl -X POST localhost:8000/fetch -H 'content-type: application/json' \
         -d '{"url":"https://example.com"}'

    # stateful tab
    curl -X POST localhost:8000/tabs -H 'content-type: application/json' \
         -d '{"url":"https://example.com"}'   # → {"id": 1, ...}
    curl localhost:8000/tabs/1/html
    curl -X DELETE localhost:8000/tabs/1
