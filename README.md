# passe-partout

HTTP service that fetches and interacts with web pages through a real Chromium browser; it can be used for browser automation as well as archiving of webpages to WARC files.

## Why

Many websites reject plain HTTP clients (eg. curl, requests), and even if not outright blocked, many webpages require an actual browser due to being rendered through client-side JavaScript. Passe-partout drives a real browser via [nodriver](https://github.com/ultrafunkamsterdam/nodriver) and exposes a small REST API any project can call.

## Run

### Docker (recommended)

```bash
docker pull ghcr.io/jfim/passe-partout:0.4
docker run --rm -p 8000:8000 ghcr.io/jfim/passe-partout:0.4
```

The image listens on `0.0.0.0:8000`, runs Chrome for Testing headless under tini as a non-root user, and exposes a `/healthz` healthcheck. Chrome for Testing is bundled instead of Chromium because Google Chrome stable rejects `--load-extension` (breaking `UNPACKED_EXTENSION_DIRS`); CfT mirrors stable behavior closely while keeping the automation switches honored.

The Chrome version is selected at build time via three build args:

- `CHROME_FOR_TESTING_CHANNEL` (default `Stable`) — resolves the latest version of the named channel (`Stable`, `Beta`, `Dev`, or `Canary`) from the [last-known-good catalog](https://googlechromelabs.github.io/chrome-for-testing/) at build time.
- `CHROME_FOR_TESTING_VERSION` (default empty) — when set, pins to an exact version and ignores the channel. Use this for reproducible local builds.
- `CFT_CACHE_BUSTER` (default empty) — escape hatch for Docker's layer cache. Channel-based builds normally reuse the cached CfT layer because the cache key is derived from RUN text + ARG values, not the resolved URL. Pass any value (a date string is conventional) to invalidate just that layer when you want a fresh download.

GitHub Actions sets `CFT_CACHE_BUSTER` to the current ISO calendar week (`%G-%V`), so each weekly run bumps it once and CI tracks upstream Stable without manual intervention. A release tagged in the same week as a recent master build hits the cache and reuses identical image bytes.

```bash
docker build -t passe-partout .                                            # latest Stable, cached after first build
docker build --build-arg CHROME_FOR_TESTING_CHANNEL=Beta -t ... .          # latest Beta
docker build --build-arg CHROME_FOR_TESTING_VERSION=148.0.7778.97 -t ... . # pinned
docker build --build-arg CFT_CACHE_BUSTER=$(date +%F) -t ... .             # force a fresh resolve
```

To load unpacked Chromium extensions, mount each as a subdirectory of `/extensions` and point `UNPACKED_EXTENSION_DIRS` at them (colon-separated):

```bash
docker run --rm -p 8000:8000 \
    -v /path/to/ext1:/extensions/ext1 \
    -v /path/to/ext2:/extensions/ext2 \
    -e UNPACKED_EXTENSION_DIRS=/extensions/ext1:/extensions/ext2 \
    ghcr.io/jfim/passe-partout:0.4
```

To run Chromium under a virtual display instead of headless (better for some extensions and bot-detection bypasses), set `USE_XVFB=1`:

    docker run --rm -p 8000:8000 -e USE_XVFB=1 ghcr.io/jfim/passe-partout:0.4

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
| `IDLE_TAB_CLOSE_SECONDS` | `300` | Timeout after which idle tabs are closed. Can be overridden on a per-tab basis via `ttl_seconds` on creation |
| `IDLE_CHROME_SHUTDOWN_SECONDS` | `300` | Seconds with no open tabs after which Chromium itself is shut down. Set to `0` to keep Chromium always running and start it eagerly. When non-zero, Chromium is started lazily on first request and restarted after shutdown as needed |
| `SWEEPER_INTERVAL_SECONDS` | `30` | How often the idle-tab sweeper runs. Lower values make per-tab `ttl_seconds` more responsive at the cost of more wakeups |
| `AUTH_TOKEN` | unset | When set, all routes except `/healthz` require `Authorization: Bearer <token>` |
| `UNPACKED_EXTENSION_DIRS` | unset | `:`-separated paths to unpacked Chromium extensions to load at launch. Requires `SHARED_PROFILE=1`. Note: Google Chrome stable rejects `--load-extension`; point `CHROME_PATH` at Chromium or Chrome for Testing. |
| `SHARED_PROFILE` | `0` | When `1`, every tab shares the default Chrome profile (cookies/storage are not isolated between callers). Required when `UNPACKED_EXTENSION_DIRS` is set, since Chrome doesn't enable `--load-extension` extensions in incognito-style contexts. The opt-in is explicit because the cross-tab cookie sharing it implies is a meaningful posture change. |
| `HEADLESS` | `1` | Set to `0` to launch Chromium with a visible UI instead of headless (requires a display — typically paired with `USE_XVFB=1` in Docker) |
| `CHROME_PATH` | unset (set to the bundled Chrome for Testing in the Docker image) | Absolute path to a Chrome/Chromium executable. When unset, nodriver auto-detects from default install locations. Note: Google Chrome stable rejects `--load-extension` regardless of feature flags — point this at Chromium or Chrome for Testing if you need extensions. |
| `USE_XVFB` | `0` | Docker image only — set to `1` to start an Xvfb virtual display and run Chromium non-headless inside it. Implies `HEADLESS=0`. |
| `DOWNLOAD_DIR` | `/tmp` | Base directory for browser downloads. Files are stored under `<DOWNLOAD_DIR>/passe-partout/tab-<id>/` (per-tab) or `<DOWNLOAD_DIR>/passe-partout/shared/` when `SHARED_PROFILE=1`, and removed when the tab closes. |

## API

All bodies are JSON. Responses include error bodies of the form `{"error": "<code>", "detail": "<message>"}` on failure.

### One-shot

`POST /fetch` — open a tab, wait for the page to load, return the HTML, then close the tab.

```bash
curl -X POST localhost:8000/fetch -H 'content-type: application/json' \
     -d '{"url":"https://example.com"}'
```

```
{"status":200,"final_url":"https://example.com/","html":"<!DOCTYPE html><html lang=\"en\"><head>...</body></html>","content_type":"text/html"}
```

Body: `url` (required), optional `cookies` (array of `{name, value, domain?, path?, expires?, httpOnly?, secure?, sameSite?}`), optional `ttl_seconds`.
Response: `{status, final_url, html, content_type}`. `status` is the actual HTTP status of the main document response (after following redirects), not a fixed 200. `content_type` is the response MIME type (e.g. `text/html`, `application/pdf`), or `null` if no document response was captured (e.g. `about:blank`).

#### Cookie domain scoping

Cookies follow standard browser scoping rules. The `domain` field controls which hosts the cookie is sent to:

- `domain` omitted → host-only scope on the request URL's host. A cookie sent with a request to `https://example.com/...` is scoped to exactly `example.com` and will **not** apply to `www.example.com` or any subdomain. If the request redirects to a different host, the cookie won't follow.
- `domain: "example.com"` → same as host-only: matches `example.com` only.
- `domain: ".example.com"` (leading dot) → matches `example.com`, `www.example.com`, `api.example.com`, and any other subdomain.

If you expect redirects across subdomains, set `domain` with a leading dot.

### Stateful tabs

For multi-step interaction, create a tab, drive it, then delete it.

| Method & path | Purpose |
| --- | --- |
| `POST /tabs` | Create a tab. Body: `{url, cookies?, ttl_seconds?, capture_mode?}` → `{id, status, final_url, content_type, download?}`. Returns 429 if `MAX_TABS` reached. `capture_mode` is one of `"no_copy"` (default), `"copy"`, `"copy_and_retain"` — see [Resources](#resources). |
| `GET /tabs` | List active tabs. |
| `GET /tabs/{id}` | Tab state: `{url, title, ready_state}`. |
| `DELETE /tabs/{id}` | Close the tab. |
| `GET /tabs/{id}/html` | Current document HTML. |
| `GET /tabs/{id}/cookies` | Cookies visible to the tab. |
| `GET /tabs/{id}/screenshot` | PNG of the viewport. |
| `POST /tabs/{id}/goto` | Navigate. Body: `{url}` → `{status, final_url, content_type, download?}`. |
| `POST /tabs/{id}/click` | Click a selector. Body: `{selector}`. |
| `POST /tabs/{id}/type` | Type into a selector. Body: `{selector, text}`. |
| `POST /tabs/{id}/eval` | Evaluate JS in the page. Body: `{js}` → `{result}`. |
| `POST /tabs/{id}/wait` | Wait for a selector and/or network idle. Body: `{selector?, network_idle?, timeout_ms?}`. |
| `GET /tabs/{id}/resources` | List captured network responses for the tab. Each entry: `{request_id, url, status, mime_type, resource_type, encoded_size}`. |
| `GET /tabs/{id}/resources/{request_id}` | Retrieve the response body bytes with the original `Content-Type`. Returns 404 if the entry was pruned (e.g. on main-frame navigation), 410 if Chrome has already evicted the body. |
| `GET /tabs/{id}/warc` | Download a [WARC](https://iipc.github.io/warc-specifications/) archive of the resources captured for the current page (`application/warc`). See [WARC export](#warc-export). |

### Downloads

Any non-HTML main-frame response (image, PDF, JSON, `application/octet-stream`, etc.) is automatically captured as a download. The origin's `Content-Disposition` header is ignored for the render-vs-download decision — only the response MIME type determines it.

`POST /tabs` and `POST /tabs/{id}/goto` gain an optional `download` field in their response when the navigation triggers a download:

```json
{"status": 200, "final_url": "https://example.com/report.pdf", "content_type": "application/pdf",
 "download": {"id": "d1a2b3", "filename": "report.pdf", "size_bytes": -1}}
```

Download records are tab-scoped and removed when the tab closes.

| Method & path | Purpose |
| --- | --- |
| `GET /tabs/{id}/downloads` | List all downloads on the tab. Each entry is a `DownloadStatus`. |
| `GET /tabs/{id}/downloads/{did}/status` | Single download status: `{id, url, filename, state, bytes_received, size_bytes, started_at, completed_at}`. |
| `GET /tabs/{id}/downloads/{did}` | Retrieve the downloaded bytes. Returns 425 if the download is still in progress, 410 if canceled. |
| `POST /tabs/{id}/downloads/{did}/cancel` | Cancel a download. Returns 409 if already in a terminal state. |
| `DELETE /tabs/{id}/downloads/{did}` | Remove the download record and delete the file from disk. |

Files land under `<DOWNLOAD_DIR>/passe-partout/tab-<id>/` (default `DOWNLOAD_DIR=/tmp`).

#### Example

```bash
# navigate to a binary URL — response includes a download field
RESP=$(curl -s -X POST localhost:8000/tabs -H 'content-type: application/json' \
            -d '{"url":"https://example.com/report.pdf"}')
TAB=$(echo $RESP | jq -r .id)
DID=$(echo $RESP | jq -r .download.id)

# poll until complete
curl localhost:8000/tabs/$TAB/downloads/$DID/status

# retrieve the bytes
curl -o report.pdf localhost:8000/tabs/$TAB/downloads/$DID

# clean up
curl -X DELETE localhost:8000/tabs/$TAB
```

### Resources

Every tab tracks the metadata of every HTTP response Chrome sees (HTML, CSS, JS, images, fonts, XHR, etc.) via the CDP `Network` domain. Bodies are not copied into passe-partout — they live in Chrome's network process and are fetched on demand. Chrome drops bodies tied to a previous main-frame loader on top-level navigation, so capture before navigating away.

```bash
TAB=$(curl -s -X POST localhost:8000/tabs -H 'content-type: application/json' \
           -d '{"url":"https://example.com"}' | jq .id)

# list every response on the page
curl localhost:8000/tabs/$TAB/resources | jq

# pick a request_id from the list and pull its bytes
RID=...
curl -o asset.bin localhost:8000/tabs/$TAB/resources/$RID
```

Per-tab metadata is pruned when the main frame navigates: only entries from the current loader (plus worker-served responses with no loader id) remain.

#### Capture modes

`POST /tabs` accepts an optional `capture_mode` that controls how aggressively the tab retains response bodies. The mode is set at tab creation and applies for the lifetime of the tab.

| Mode | Bodies | Across navigation |
| --- | --- | --- |
| `no_copy` (default) | Lazy via Chrome's network process. May 410 if Chrome has evicted them. | Pruned. |
| `copy` | Buffered eagerly into the record on `loadingFinished`. Survives Chrome-side eviction. | Pruned. |
| `copy_and_retain` | Same as `copy`. | Retained for the tab's lifetime. Memory grows with traffic — no built-in cap. |

Request and response headers are captured in all three modes (they're cheap and needed for WARC export). The only difference between modes is body buffering and cross-navigation retention.

In `copy` and `copy_and_retain` modes, `GET /tabs/{id}/resources/{request_id}` returns the buffered copy when present, so it keeps working after Chrome would normally have evicted the body.

### WARC export

`GET /tabs/{id}/warc` returns a [WARC 1.1](https://iipc.github.io/warc-specifications/) archive of the resources captured on the tab, suitable for replay in tools like [pywb](https://github.com/webrecorder/pywb) or [replayweb.page](https://replayweb.page). The archive contains one `warcinfo` record followed by paired `request` / `response` records for each tracked resource. Scope depends on capture mode: `no_copy` and `copy` archive only the current main-frame loader, while `copy_and_retain` archives every resource ever retained on the tab (prior navigations, iframes, etc.).

```bash
TAB=$(curl -s -X POST localhost:8000/tabs -H 'content-type: application/json' \
           -d '{"url":"https://example.com","capture_mode":"copy"}' | jq .id)

curl -o page.warc localhost:8000/tabs/$TAB/warc
curl -X DELETE localhost:8000/tabs/$TAB
```

To archive a multi-page session — clicking through links, walking through redirect interstitials, capturing iframe traffic — open the tab with `capture_mode: "copy_and_retain"` and call `/warc` before closing; one tab = one session = one WARC. `no_copy` tabs work too — bodies are fetched live from Chrome at export time, and any Chrome has already evicted ship as zero-length responses with `WARC-Truncated: unspecified`. The archive is uncompressed (`application/warc`, not `.warc.gz`); gzip downstream if you need it. `/fetch` does not produce WARC — use the stateful tab flow.

### Browser lifecycle

`GET /browser` → `{running, tab_count, headless, shared_profile, extension_dirs, chrome_path}`. Reports current Chromium state and the static config it was launched under.

`DELETE /browser` → 200 `{ok, stopped}` if Chromium got torn down (or was already down — idempotent), 409 `{error: "tabs_open", detail: ...}` if any tab is still tracked. Use when you need to discard accumulated cookies/storage in shared-profile mode (where there is no per-tab isolation): close all tabs, then `DELETE /browser`. The next `POST /tabs` lazily restarts Chromium with a fresh user-data-dir.

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

## License

GNU AGPL 3.0
