# WARC export

## Goal

Let clients download a WARC file of everything a tab fetched while loading the current page. WARC ([ISO 28500](https://iipc.github.io/warc-specifications/)) is the standard archive format used by Internet Archive, Browsertrix, and friends; producing it lets passe-partout slot into existing crawl/replay tooling without bespoke serialization on the client side.

A secondary goal is to give callers control over *how much* of the network traffic the tab retains. Today resources are tracked via Chrome's network process and bodies are fetched on demand; they can be evicted by navigation or buffer caps. For archival use that's unreliable, so this change introduces a per-tab capture mode that opts into eager body buffering and (optionally) cross-navigation retention.

## Scope

In:
- A new `GET /tabs/{tab_id}/warc` endpoint that streams a WARC file containing the resources captured for the tab's *current* main-frame loader. One `warcinfo` record followed by paired `request` + `response` records in arrival order.
- A new `capture_mode` field on `CreateTabRequest` controlling network-body buffering. Three values: `NO_COPY` (default, today's behavior), `COPY` (eagerly buffer headers + bodies, prune on navigation), `COPY_AND_RETAIN` (eagerly buffer, never prune). The mode is set at tab creation and applies for the lifetime of the tab — `/goto`, click handlers, and other in-tab operations don't override it.
- Always-on capture of full request/response headers (cheap; needed to produce valid WARC records regardless of mode).

Out:
- `POST /fetch` does not change. It stays a thin tab-create-then-delete wrapper that returns HTML. Callers who want WARC use the stateful tab flow.
- Streaming WARC bytes as they arrive from origins. The WARC is assembled at request time from buffered/Chrome-held state.
- Buffer eviction policies for `COPY_AND_RETAIN`. Callers who pick that mode accept that the tab grows monotonically until they close it. Memory pressure is left to the OS; a future change can add a byte/count cap if real usage demands it.
- gzip-compressed (`.warc.gz`) output. We emit uncompressed WARC; clients can gzip downstream if they care.
- WARC-revisit records, deduplication, or any of the multi-archive optimizations real crawlers use. Each export is a self-contained snapshot of one tab's one loader.
- Multi-loader / session-scope WARC. The endpoint is scoped to the current main-frame loader, matching the existing `ResourceRecorder` pruning behavior.

## Capture modes

`CaptureMode` is a new enum in `models.py`:

| Mode | Bodies | Retention across main-frame navigation |
|---|---|---|
| `NO_COPY` (default) | Lazy via `Network.getResponseBody` at access time. May be unavailable (opaque CORS, eviction). | Pruned by existing `_on_frame_navigated` handler. |
| `COPY` | Eagerly pulled at `Network.loadingFinished` and stashed on the `ResourceRecord`. | Pruned by existing handler. |
| `COPY_AND_RETAIN` | Same as `COPY`. | **Not pruned.** Resources accumulate for the tab's lifetime. |

Headers (request + response) are captured for every resource in all three modes — they're small and the WARC export needs them.

The mode is set when the tab is created and is immutable for the tab's lifetime. Changing capture behavior partway through a tab's life would create ambiguous semantics ("are old entries retroactively buffered?") for no real-world gain; if a caller needs different behavior, they create a new tab.

## CDP plumbing

We already subscribe to `Network.responseReceived`, `Network.loadingFinished`, and `Page.frameNavigated`. WARC export adds:

- **`Network.requestWillBeSent`** — captures `request.method`, `request.headers`, `request.url`, `request.hasPostData`, `request.postData` (when small enough to ship inline), `request.postDataEntries` (when present), and the `Request-Id`. Stored on a new `RequestRecord` that gets paired with the `ResourceRecord` on `responseReceived`. We do *not* attempt to recover POST bodies that Chrome dropped due to size — those records ship without a request payload.
- **Expanded `Network.responseReceived` handling** — in addition to the fields we keep today (`status`, `mime_type`, `resource_type`, `loader_id`), record `status_text`, `response.headers`, `response.protocol` (e.g. `"h2"`, `"http/1.1"`), `response.remote_ip_address`, `response.remote_port`, and `response.timing` if present. Headers are stored as the original CDP `dict[str, str]` (Chrome already normalizes them for us).

Eager body capture for `COPY`/`COPY_AND_RETAIN` happens inside the existing `_on_finished` handler: when the mode is one of those two, immediately call the recorder's existing `get_body()` helper (which already decodes Chrome's base64-encoded binary responses to raw `bytes`) and store the result as `body: bytes | None` on the record. The call is wrapped — failures (opaque cross-origin, body too large, already evicted) leave `body=None` and the record still ships, with the WARC writer emitting headers and a zero-length payload (marked with `WARC-Truncated: unspecified`).

`ResourceRecorder.get_body()` is updated to prefer the buffered `record.body` when present, falling back to live `Network.getResponseBody` only when it's `None`. This means the existing `GET /tabs/{id}/resources/{request_id}` endpoint becomes more reliable in `COPY` / `COPY_AND_RETAIN` modes: requests that would 410 today (Chrome evicted the body) succeed when we have a buffered copy. `NO_COPY` behavior is unchanged. No endpoint signature or response-shape changes.

`_on_frame_navigated`'s prune step gets a mode check: skip pruning when the tab's mode is `COPY_AND_RETAIN`. The current-loader bookkeeping (`self._current_loader[tab_id] = new_loader`) stays in place either way so the WARC endpoint can scope to the current loader.

## Data model

`ResourceRecord` (in `resources.py`) grows:

```python
@dataclass
class ResourceRecord:
    request_id: str
    url: str
    status: int
    status_text: str = ""
    mime_type: str = ""
    resource_type: str = ""
    loader_id: str = ""
    encoded_size: int = 0

    # Always populated (cheap, needed for WARC):
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
    remote_ip: str = ""
    remote_port: int = 0
    request_post_data: bytes | None = None

    # Populated only in COPY / COPY_AND_RETAIN. Already decoded — Chrome's
    # base64 wrapping for binary responses is unwrapped before storage.
    body: bytes | None = None
```

`TabRecord` gains `capture_mode: CaptureMode`. `CreateTabRequest` gains `capture_mode: CaptureMode = CaptureMode.NO_COPY`.

The mode is propagated from request → `BrowserPool.create_tab` → `TabRecord` → `ResourceRecorder.attach_tab(tab_id, tab, mode)`. The recorder stashes `tab_id → mode` in a dict alongside `_current_loader` so its event handlers can branch on mode without reaching back into the registry.

## WARC endpoint

`GET /tabs/{tab_id}/warc`

Response: `application/warc`, `Content-Disposition: attachment; filename="tab-<id>-<loader_id>.warc"`.

Body assembly (in a new `warc.py` module):

1. One `warcinfo` record at the top. Fields: `software: passe-partout/<version>`, `format: WARC/1.1`, `conformsTo: <ISO 28500 URL>`, `hostname` (from `socket.gethostname()`), `isPartOf: tab-<id>`.
2. For each `ResourceRecord` in `rec.resources.values()` whose `loader_id == current_loader` (or whose `loader_id == ""`, i.e. worker-served), in insertion order:
   - One `request` record carrying the HTTP request line + headers + post body (if any). `WARC-Target-URI` is the resource URL; `WARC-Date` is the recorder's capture time (we'll need to add a `captured_at: datetime` field to `ResourceRecord`).
   - One `response` record carrying the HTTP status line + headers + body. Body sourced from `record.body` if present; otherwise we try `Network.getResponseBody` live; if that fails we emit headers + empty payload + `WARC-Truncated: unspecified`. The two records share a `WARC-Concurrent-To` cross-reference.

We use [`warcio`](https://github.com/webrecorder/warcio) for record formatting. It handles the WARC framing, content-length math, and concurrent-to linking. We hand it a `BytesIO` and stream the result back as the response body. For an MVP this is fine; if WARCs get large enough that buffering hurts, we can switch to a streaming `StreamingResponse` later.

`warcio` is added as a runtime dependency in `pyproject.toml`.

Edge cases:

- **Tab has no resources** (created but never navigated): return a WARC containing only the `warcinfo` record. 200, not 404.
- **Tab not found**: 404 with the existing `{"error": "tab_not_found", ...}` shape, matching every other tab route.
- **Current loader is empty** (tab opened to `about:blank` and nothing else): same as "no resources" — empty WARC, 200.
- **Live `Network.getResponseBody` fails for a resource in `NO_COPY` mode**: skip the body, mark `WARC-Truncated: unspecified`. Do not 5xx the whole export — partial WARCs are still useful and the standard supports them explicitly.

The route holds `rec.lock` for the entire body-assembly pass, matching how `/screenshot` and `/resources/{id}` already serialize per-tab work.

## Testing

New tests under `tests/`:

- `test_capture_modes.py`:
  - `NO_COPY` (default) — load a fixture page with an image subresource, navigate away, assert `rec.resources` is pruned and `/resources/{id}` for an old resource returns 410.
  - `COPY` — same flow, assert that *during* the original page bodies come back from the buffered copy (verified by killing Chrome's copy via navigation and re-fetching from `rec.body`), and that pruning still happens on navigation.
  - `COPY_AND_RETAIN` — same flow, assert resources from the first page are still present after navigating to a second page.
- `test_warc.py`:
  - Load a fixture page with HTML + an image + an XHR-fetched JSON blob. Hit `/warc`. Pipe response through `warcio.ArchiveIterator` and assert: one `warcinfo`, three `request`/`response` pairs, each response body matches the served bytes, status lines and headers round-trip.
  - Empty-tab case: open a tab without navigating, hit `/warc`, assert only `warcinfo` is present.
  - 404 case: hit `/warc` on a nonexistent tab id, assert error shape.
  - `NO_COPY` body-eviction case: navigate, then hit `/warc` scoped to the new loader, assert old-loader entries are not in the WARC. (The pruning means they're already gone from `rec.resources`, so this is mostly a regression guard.)

All tests use the existing `fixture_server` fixture rather than the public internet. No new smoke tests.

## Risks / open questions

- **`warcio` API stability.** It's the de facto standard but is maintained at a modest cadence. If it ever blocks us we can hand-roll WARC framing — the format is straightforward — but for v1 the library save is worth it.
- **`COPY` memory cost is unbounded within a single page load.** A page that triggers thousands of XHRs will hold every response body in Python memory until navigation. This is the user-accepted tradeoff of asking for `COPY`. If it becomes a real problem we add a per-tab byte cap, but punting for v1.
- **POST bodies for large requests.** Chrome truncates `requestWillBeSent.request.postData` beyond a size threshold and only sets `hasPostData=True`. We don't currently have a way to recover those bytes via CDP (`Network.getRequestPostData` exists but is best-effort and not always available). We ship the WARC `request` record without a payload in that case — same `WARC-Truncated: unspecified` treatment as missing response bodies.
