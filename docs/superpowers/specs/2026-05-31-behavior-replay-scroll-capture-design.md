# Behavior replay & scroll-to-settle capture — design

**Date:** 2026-05-31
**Status:** Draft for review

## Problem

The current capture path never scrolls. On static pages (most blogs) everything is
already in the DOM, so this is fine. But SPA-style and dynamic news sites (e.g. NYT)
lazy-load content below the fold via `IntersectionObserver`. Without scrolling:

- the serialized DOM (`?rendered=1`) is missing the below-fold content, and
- the **WARC/resource capture is also missing it** — `ResourceRecorder` only sees
  responses Chrome actually fetched, and un-triggered lazy fetches never happen, so
  those bytes are absent from the archive too.

We want the client to be able to drive realistic scrolling that triggers below-fold
lazy-load, then capture a complete page — without baking page-understanding or shared
"human" behavior into passe-partout.

## Design principles

1. **passe-partout stays a thin mechanism; the client owns policy.** This matches the
   existing grain (the client decides what to capture and pulls resource bytes itself).
   Content extraction, "read more" detection, stop conditions, and settling all live in
   the client.
2. **No shared canned behaviors.** Shipping a built-in "realistic" trace would make every
   passe-partout user replay the identical pattern — a cross-client fingerprint, with
   passe-partout as the join key. The app ships only a basic, honestly-synthetic default;
   realistic traces are operator-supplied and private.
3. **The trust boundary forces the input/read split.** Trusted input (`isTrusted:true`,
   rides the compositor + coalescing + IntersectionObserver) can only be produced by CDP
   from the driver side. In-page JS — main or isolated world — has zero CDP access, and
   JS-synthesized events are `isTrusted:false` (detectable, often ignored). So:
   - **input/behavior replay → passe-partout server-side via CDP `Input.*`**
   - **reads (geometry, DOM) → in-page isolated-world `eval`** (tamper-resistant, sees
     live post-scroll layout).
4. **Short calls, not long requests.** One `play` call = one burst. The multi-minute
   orchestration lives in the client making many short calls, which sidesteps proxy/client
   network timeouts entirely.

## Concepts

- **Behavior** — a *parameterless* replayable input trace: a sequence of relative wheel
  deltas + inter-event timings. Examples: `scroll-down`, `scroll-up`, `jitter`,
  `wheel-scrub`. Played as-is (with perturbation). Built-in or loaded from
  `BEHAVIOR_TRACE_DIR`.
- **Targeted action** (future, out of scope here) — a *parametric* operation that takes a
  destination: `move-cursor-to(x,y)`, `click`. Not a behavior, because it can't be a
  pre-recorded clip (the target's position varies per page). It reuses the same CDP-input
  + perturbation machinery and may consume a recorded *motion profile* as its style, but
  its endpoint shape takes coordinates/a selector. Designed separately later.

## API surface

The only genuinely new surface is two endpoints plus one config var. Everything else
composes existing endpoints (`/eval`, `/click`, `/wait`, `/warc`).

### `GET /behaviors`

App-level (the catalog is global, not per-tab). Lists built-ins + traces loaded from
`BEHAVIOR_TRACE_DIR`.

Response: `[{ "name": str, "kind": "scroll-down|scroll-up|jitter|wheel-scrub",
"source": "builtin|recorded" }, ...]`

### `POST /tabs/{id}/behaviors/play`

Replays one perturbed burst of the named behavior against the tab via CDP wheel events.
Pure side-effecting mechanism.

Request: `{ "name": str, "perturb"?: { "enabled"?: bool, "time_warp"?: float,
"delta_scale"?: float, "seed"?: int } }`. Perturbation is applied by default; the
`perturb` object only overrides the defaults (or disables it via `enabled: false` for
deterministic testing).
Response: **`204 No Content`** — returns nothing. All reads (scroll position, viewport,
element rects) go through `eval`; geometry is never smuggled into the mutation response.

Serializes on the per-tab lock like other multi-step routes, and calls
`registry.touch(tab_id)` so a long client-driven scroll session isn't reaped by the tab
sweeper mid-run. (Each call is short, so the lock is held briefly.)

### Reads: existing `POST /tabs/{id}/eval` with `world: "isolated"`

The client gets scroll position, viewport, `scrollHeight`, and any element rects in a
single isolated-world eval, e.g.:

```js
({
  scrollX: scrollX, scrollY: scrollY,
  innerWidth: innerWidth, innerHeight: innerHeight,
  scrollHeight: document.documentElement.scrollHeight,
  rect: document.querySelector(sel)?.getBoundingClientRect(),
})
```

No dedicated `/bounds`, `/viewport`, `/scroll` endpoints — they'd be convenience-creep.

### Config: `BEHAVIOR_TRACE_DIR`

Directory of operator-recorded trace files, loaded at startup and surfaced in
`GET /behaviors`. Mirrors the `UNPACKED_EXTENSION_DIRS` pattern: operator assets stay on
the operator's filesystem, never shipped. No upload endpoint in v1 (deferred).

## Built-in behavior

Exactly one ships: **evenly-spaced wheel `scroll-down`** — a fixed delta at a fixed
interval, enough to trip `IntersectionObserver` lazy-load. Honestly synthetic, makes no
claim to be human. Plus the replay+perturb mechanism. Nothing else is canned.

## Trace format & perturbation

- A trace is a sequence of `(deltaX, deltaY, dt_ms)` wheel steps (capture the `deltaMode`
  too). Replayed via `Input.dispatchMouseEvent{type:"mouseWheel"}` paced by `dt_ms`.
- passe-partout perturbs on every replay — time-warp the gaps, scale the deltas, jitter —
  so an operator replaying a small library doesn't reproduce byte-identical bursts across
  their own sessions. **Variety is the operator's responsibility; perturbation-on-replay
  is the app's.**

## Reference client orchestration (lives in the client, not passe-partout)

1. Pull DOM (`/eval` or `/html`); run readability/trafilatura to find the main-content
   element; decide whether scrolling is even needed (simple blog → skip).
2. Loop, burst-by-burst:
   - `POST /behaviors/play` (e.g. `scroll-down`) — one burst.
   - `eval` (isolated) the main element's `getBoundingClientRect` + viewport.
   - Stop when the main element's bottom has risen to ~10% above the viewport bottom
     (`rect.bottom <= innerHeight * 0.9`) — a natural human stopping point, not the footer.
   - If a "Read more"/expand control is present, `POST /click` it and resume. (It often
     only appears mid-scroll, which the burst-by-burst loop handles naturally — the HTTP
     round-trip lands in the inter-burst pause where a human pauses anyway.)
3. Settle: `POST /wait` with `network_idle` (in-flight lazy fetches finish + record).
4. `GET /warc` (optionally `?rendered=1`; scroll back to top first if the screenshot
   should frame the top).

**Why client-side settle is correct:** with a thin `play` that returns immediately, the
last lazy fetches are still in flight on return, so the client *must* network-idle-wait
before pulling the WARC. Resources keep recording in the background regardless, and
nothing prunes them without a main-frame navigation, so "scroll → settle → WARC" captures
everything triggered during the scroll.

## Why the input must be CDP-side (rationale, for future readers)

- In-page JS (any world) cannot reach CDP; there is no `Input` global.
- `new WheelEvent(...) + dispatchEvent(...)` → `isTrusted:false`: detectable and ignored
  by many scroll-linked loaders.
- `window.scrollTo()` → teleports scroll position, emits no wheel events; neither looks
  like input nor reliably triggers wheel-driven lazy-load.
- Empirically (see `tools/probe_input_coalescing.py`): CDP honors dispatch `timestamp`
  exactly but `samples/event` caps ~3–4, so synthetic wheel input reads as a standard
  125–250Hz mouse, not a 1kHz one. Behaviors are wheel-delta traces because that's the
  fidelity ceiling CDP can faithfully reproduce.

## Out of scope / future work

- **Targeted actions** (`move-cursor-to`, realistic move-then-click): parametric, separate
  endpoint surface; reuse CDP-input + perturbation; `eval` rect → `play`-style move →
  trusted `mousePressed`/`mouseReleased`. The existing selector `/click` stays as the
  teleport version.
- **Trace upload endpoint** (`POST /behaviors`): deferred in favor of `BEHAVIOR_TRACE_DIR`.
- **Recorder tooling** for capturing operator traces (the app may ship a recorder; it does
  not ship recorded traces).
- **Ambient-realism behaviors** (jitter/scrub/dwell for footprint when an antibot script is
  detected) — same `play` mechanism, client decides when to invoke.

## Resolved decisions

- `play` returns `204`; all reads via `eval`. (Consistency: mutation vs. read separation.)
- Geometry is *not* dedicated endpoints — isolated-world `eval`.
- Trace ingestion via `BEHAVIOR_TRACE_DIR`, not an upload endpoint, for v1.
- Ship one honest-basic built-in (`scroll-down`); no canned realistic traces.
- Behaviors are parameterless; targeted move/click is a separate (future) parametric API.
