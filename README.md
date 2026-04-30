# passe-partout

HTTP service that fetches and interacts with web pages through a real Chromium browser.

## Why

Some sites (Cloudflare, paywalls, JS-only rendering) reject plain HTTP clients. passe-partout fronts them with a real browser via [nodriver](https://github.com/ultrafunkamsterdam/nodriver) and exposes a small REST API any project can call.

## Run

### Docker (recommended)

```bash
docker pull ghcr.io/jfim/passe-partout:0.1
docker run --rm -p 8000:8000 ghcr.io/jfim/passe-partout:0.1
```

The image listens on `0.0.0.0:8000`, runs Chromium headless under tini as a non-root user, and exposes a `/healthz` healthcheck.

To load unpacked Chromium extensions, mount each as a subdirectory of `/extensions` and point `UNPACKED_EXTENSION_DIRS` at them (colon-separated):

```bash
docker run --rm -p 8000:8000 \
    -v /path/to/ext1:/extensions/ext1 \
    -v /path/to/ext2:/extensions/ext2 \
    -e UNPACKED_EXTENSION_DIRS=/extensions/ext1:/extensions/ext2 \
    ghcr.io/jfim/passe-partout:0.1
```

To run Chromium under a virtual display instead of headless (better for some extensions and bot-detection bypasses), set `USE_XVFB=1`:

    docker run --rm -p 8000:8000 -e USE_XVFB=1 ghcr.io/jfim/passe-partout:0.1

### From source

```bash
uv sync
uv run python -m passe_partout
```

## Configuration (env vars)

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` | `127.0.0.1` (`0.0.0.0` in the Docker image) | Interface to listen on, `127.0.0.1` for loopback only, `0.0.0.0` to listen on all addresses |
| `PORT` | `8000` | Port on which to listen for the REST API |
| `MAX_TABS` | `10` | Maximum number of open tabs, after which opening additional tabs will return HTTP 429 |
| `IDLE_TIMEOUT_SECONDS` | `300` | Timeout after which tabs are closed. Can be overridden on a per-tab basis via `ttl_seconds` on creation |
| `AUTH_TOKEN` | unset | When set, all routes except `/healthz` require `Authorization: Bearer <token>` |
| `UNPACKED_EXTENSION_DIRS` | unset | `:`-separated paths to unpacked Chromium extensions to load at launch |
| `USE_XVFB` | `0` | Docker image only — set to `1` to run Chromium under `xvfb-run` instead of headless |

## API

All bodies are JSON. Responses include error bodies of the form `{"error": "<code>", "detail": "<message>"}` on failure.

### One-shot

`POST /fetch` — open a tab, wait for the page to load, return the HTML, then close the tab.

```bash
curl -X POST localhost:8000/fetch -H 'content-type: application/json' \
     -d '{"url":"https://example.com"}'
```

```
{"status":200,"final_url":"https://example.com/","html":"<!DOCTYPE html><html lang=\"en\"><head>...</body></html>"}
```

Body: `url` (required), optional `cookies` (array of `{name, value, domain?, path?, expires?, httpOnly?, secure?, sameSite?}`), optional `ttl_seconds`.
Response: `{status, final_url, html}`.

### Stateful tabs

For multi-step interaction, create a tab, drive it, then delete it.

| Method & path | Purpose |
| --- | --- |
| `POST /tabs` | Create a tab. Body: `{url, cookies?, ttl_seconds?}` → `{id, status, final_url}`. Returns 429 if `MAX_TABS` reached. |
| `GET /tabs` | List active tabs. |
| `GET /tabs/{id}` | Tab state: `{url, title, ready_state}`. |
| `DELETE /tabs/{id}` | Close the tab. |
| `GET /tabs/{id}/html` | Current document HTML. |
| `GET /tabs/{id}/cookies` | Cookies visible to the tab. |
| `GET /tabs/{id}/screenshot` | PNG of the viewport. |
| `POST /tabs/{id}/goto` | Navigate. Body: `{url}` → `{status, final_url}`. |
| `POST /tabs/{id}/click` | Click a selector. Body: `{selector}`. |
| `POST /tabs/{id}/type` | Type into a selector. Body: `{selector, text}`. |
| `POST /tabs/{id}/eval` | Evaluate JS in the page. Body: `{js}` → `{result}`. |
| `POST /tabs/{id}/wait` | Wait for a selector and/or network idle. Body: `{selector?, network_idle?, timeout_ms?}`. |

### Health

`GET /healthz` → `{ok, browser, tabs}`. Used by the Docker `HEALTHCHECK`; not subject to `AUTH_TOKEN`.

### Example

```bash
# create a tab
TAB=$(curl -s -X POST localhost:8000/tabs -H 'content-type: application/json' \
           -d '{"url":"https://example.com"}' | jq .id)

# wait for network idle, then grab HTML
curl -X POST localhost:8000/tabs/$TAB/wait -H 'content-type: application/json' \
     -d '{"network_idle":true,"timeout_ms":5000}'
curl localhost:8000/tabs/$TAB/html

# clean up
curl -X DELETE localhost:8000/tabs/$TAB
```
