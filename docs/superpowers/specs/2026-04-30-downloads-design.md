# Downloads support

## Goal

Let clients fetch binary resources through passe-partout the same way they fetch HTML pages today: open a URL in a tab, get back a small JSON metadata response, then retrieve the artifact via a separate endpoint. Downloads are tab-scoped — when the tab is closed (explicitly or by TTL), all of its download files are deleted.

## Scope

In:
- A direct binary URL passed to `POST /tabs` (or `POST /tabs/{id}/goto`) where Chromium would normally hand the response to its download manager instead of rendering a document.
- A page-triggered download produced by a `click` (or any in-page action) — the download record appears on the tab and is retrievable through the same endpoints.
- Multiple downloads coexisting on a single tab.

Out:
- Streaming bytes to the client *as they arrive* from the origin. Bytes are served only after the download reaches a terminal state.
- Per-download retention beyond the lifetime of the owning tab.
- A `/fetch`-style one-shot binary download endpoint. Clients use the tab flow.

## CDP plumbing

`nodriver` exposes the relevant CDP surface directly. The handful we use:

| CDP | Purpose |
|---|---|
| `Browser.setDownloadBehavior(behavior="allowAndName", browserContextId, downloadPath, eventsEnabled=True)` | Configured once per tab context at tab creation. `allowAndName` writes each download to disk as its CDP `guid` (no extension), avoiding filename collisions; the suggested filename is reported separately in the event. |
| `Browser.downloadWillBegin` event | Fires when Chromium decides a navigation is a download. Provides `guid`, `url`, `suggestedFilename`. We create a `DownloadRecord` here. |
| `Browser.downloadProgress` event | Fires repeatedly with `guid`, `totalBytes`, `receivedBytes`, `state ∈ {inProgress, completed, canceled}`. We mutate the record fields and bump the owning tab's `last_used_at`. |
| `Browser.cancelDownload(guid, browserContextId)` | Backs the cancel endpoint. |

Note that CDP only reports three states. There is no separate "failed" — a network error mid-stream surfaces as `canceled` with `bytes_received < size_bytes`. We expose CDP's three values verbatim rather than inventing a `failed` state we cannot reliably distinguish.

`totalBytes` arrives as `0` when the origin did not send `Content-Length`. We translate that to `size_bytes: -1` on the wire so `0` unambiguously means "empty file." `size_bytes` may transition from `-1` to a known value partway through; clients treat it as a best current estimate.

## Data model

A new `DownloadRecord` lives on each `TabRecord`:

```python
@dataclass
class DownloadRecord:
    id: str                       # CDP guid
    url: str                      # from downloadWillBegin
    filename: str                 # suggestedFilename, locked at start
    state: Literal["in_progress", "completed", "canceled"]
    bytes_received: int
    size_bytes: int               # -1 if unknown
    started_at: float
    completed_at: float | None
    path: Path                    # server-internal, not serialized
```

`TabRecord` gains `downloads: dict[str, DownloadRecord]` (keyed by guid).

The CDP event handler mutates `DownloadRecord` fields directly without taking the per-tab route lock. State transitions are monotonic (`in_progress` → terminal) and the fields are independent scalars, so torn reads are not a concern. Route handlers read these fields directly when serving status.

## File storage

A new env var **`DOWNLOAD_DIR`** (default `/tmp`) sets the root. Actual layout:

```
<DOWNLOAD_DIR>/passe-partout/tab-<tab_id>/<guid>
```

`BrowserPool.close_context` recursively removes `<DOWNLOAD_DIR>/passe-partout/tab-<tab_id>/` after closing the Chromium context. The directory is created lazily — only tabs that actually produce downloads materialize one.

## API

### Modified responses

`CreateTabResponse` and `GotoResponse` gain an optional `download` field. Present iff the navigation produced a download (in which case `final_url` is the binary URL from `downloadWillBegin` and the page itself is `about:blank`):

```json
{
  "id": 42,
  "status": 200,
  "final_url": "http://domain.tld/binary.zip",
  "content_type": "application/zip",
  "download": {
    "id": "a1b2c3...",
    "filename": "binary.zip",
    "size_bytes": 10485760,
    "url": "/tabs/42/downloads/a1b2c3..."
  }
}
```

`POST /tabs` and `POST /tabs/{id}/goto` return **as soon as `downloadWillBegin` fires** — they do not wait for completion. `size_bytes` may be `-1` at that moment.

`POST /tabs/{id}/click` is unchanged (still 204). Clients discover any download triggered by a click by polling `GET /tabs/{id}/downloads`.

### New endpoints

**`GET /tabs/{id}/downloads`** — list download records for the tab. Response is an array of the `/status` shape below. Empty array if none.

**`GET /tabs/{id}/downloads/{did}/status`**
```json
{
  "id": "a1b2c3...",
  "url": "http://domain.tld/binary.zip",
  "filename": "binary.zip",
  "state": "in_progress",
  "bytes_received": 4194304,
  "size_bytes": 10485760,
  "started_at": 1714502400.123,
  "completed_at": null
}
```

**`GET /tabs/{id}/downloads/{did}`** — the bytes.

| State | Response |
|---|---|
| `completed` | `200 OK`, body is the file, `Content-Type` from the origin, `Content-Disposition: attachment; filename="<filename>"`, `Content-Length` set. |
| `in_progress` | `425 Too Early` with a hint to re-poll. Body is an `ErrorBody`. |
| `canceled` | `410 Gone`. The partial bytes are not exposed through this endpoint — clients can read `bytes_received` from `/status` if they need to know how far it got. |

**`POST /tabs/{id}/downloads/{did}/cancel`** — issues `Browser.cancelDownload`. The record stays around in `state: canceled` with whatever `bytes_received` was reached. `204 No Content` on success. `409 Conflict` if the download is already in a terminal state.

**`DELETE /tabs/{id}/downloads/{did}`** — removes the record and unlinks the file. If the download is still `in_progress`, implicitly cancels first (must stop writing before unlinking). `204 No Content`. After this, `GET /downloads/{did}` and `/status` return `404`.

### Existing routes interacting with downloads

`DELETE /tabs/{id}` and TTL eviction call `BrowserPool.close_context`, which now also removes the per-tab download directory.

## Idle sweep interaction

The tab idle sweeper evicts tabs whose `last_used_at` is older than `IDLE_TAB_CLOSE_SECONDS`. A long-running download with no client polling would otherwise be killed mid-transfer. The CDP `downloadProgress` event handler calls `rec.touch()` on every progress event, so an active download keeps its tab alive without client involvement. Once the download reaches a terminal state, normal TTL behavior resumes.

## Errors

Standard error body shape (`ErrorBody`):

| Condition | Status | `error` code |
|---|---|---|
| Tab not found | 404 | `tab_not_found` |
| Download not found on tab | 404 | `download_not_found` |
| Bytes requested while `in_progress` | 425 | `download_in_progress` |
| Bytes requested while `canceled` | 410 | `download_canceled` |
| Cancel called on terminal state | 409 | `download_terminal` |

## Configuration summary

New env var:

- **`DOWNLOAD_DIR`** (default `/tmp`) — root directory for tab-scoped download storage. The actual files live under `<DOWNLOAD_DIR>/passe-partout/tab-<tab_id>/`.

No changes to existing env vars. `IDLE_TAB_CLOSE_SECONDS` continues to govern tab TTL but is held off by active downloads as described above.

## Testing

- Add a fixture endpoint to `tests/fixtures/` (served by the existing `fixture_server`) that returns a small binary with `Content-Disposition: attachment`. Use that for the happy-path integration tests rather than the public internet.
- Tests:
  - `POST /tabs` to a binary URL returns a response with `download` populated; `final_url` matches.
  - `GET /tabs/{id}/downloads/{did}/status` reports `completed` after the download finishes.
  - `GET /tabs/{id}/downloads/{did}` returns the bytes with correct `Content-Type` and `Content-Disposition`.
  - `425` while in-progress (use a slow fixture or artificial delay to observe the intermediate state).
  - `POST /cancel` mid-flight transitions to `canceled`; subsequent bytes GET → `410`.
  - `DELETE /downloads/{did}` while in-progress cancels and removes the file from disk.
  - Tab close (explicit and TTL) removes the per-tab download directory.
  - Idle sweep does not evict a tab while a download is in progress.
  - Multiple downloads on one tab are tracked and served independently.
- Pool-state tests for download-related lifecycle (e.g. `setDownloadBehavior` is called at context creation) follow the `test_idle_chrome_shutdown.py` pattern with a fake browser.
- Smoke tests are not added — downloads work the same against the public internet as against the fixture server, and we already pay the smoke cost on the page-fetching path.

## Out of scope / future work

- Resumable downloads after a server restart (records are in-memory).
- Authenticated download URLs that don't go through the bearer-token middleware (today downloads inherit the same auth as everything else).
- Per-download size limits or quota enforcement.
- Streaming partial bytes of a canceled download.
