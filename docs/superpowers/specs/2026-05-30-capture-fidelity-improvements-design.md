# Capture fidelity improvements: isolated worlds, shadow DOM, CSSOM folding

**Date:** 2026-05-30
**Status:** Approved (design); implementation pending

## Goal

Raise the fidelity of passe-partout's page capture along three independent axes:

1. Run passe-partout's own JavaScript in an **isolated world** so a hostile or
   instrumented page can't tamper with the builtins/globals our capture logic
   relies on.
2. Serialize DOM with **`includeShadowDOM=true`** so shadow trees are captured.
3. **Fold the CSSOM into the rendered HTML** so the `renderedContent` snapshot is
   self-contained and faithfully renderable, capturing exactly what
   `DOM.getOuterHTML` loses: adopted/constructed stylesheets (which have no DOM
   node) and `<style>` elements whose rules were mutated via CSSOM (whose
   `textContent` goes stale).

All three were validated against a live Chromium probe before this spec was
written (see "Empirical validation" below).

## Background

The relevant capture paths today:

- `rendered.py::capture_rendered_payload` walks the frame tree and, per frame,
  serializes DOM via `DOM.getOuterHTML` into a HAR-shaped `renderedContent`
  field. It runs an owner-selector JS function via `Runtime.callFunctionOn` and a
  `window.scrollTo(0,0)` via `Runtime.evaluate` — both in the page's **main
  world** today.
- `app.py` reads `document.title` / `document.readyState` via `tab.evaluate`
  (main world) in `GET /tabs/{id}`, and runs user JS via `tab.evaluate` (main
  world) in `POST /tabs/{id}/eval`.
- The `/warc` route folds optional `?rendered=1` and `?domsnapshot=1` conversion
  records that `WARC-Refers-To` the main-document response.

The CSSOM-folding work happens **entirely inside `capture_rendered_payload`** —
it rewrites each frame's `renderedContent` string. The `/warc` route and
`build_warc` are unchanged for it.

## Decisions (locked during brainstorming)

- **`/eval` gets a `world` field**, `"main"` (default) | `"isolated"`. Internal
  capture JS moves to isolated by default; user-facing `/eval` defaults to main
  so callers can still touch page globals.
- **CSS folding & shadow DOM are intrinsic to `?rendered=1`** — no new query
  params. The point of the rendered snapshot is fidelity.
- **Apply CSS edits via byte-preserving span splicing**, not a parse/re-serialize
  round-trip. The Python `html.parser` is used only to *locate* byte ranges; all
  unedited bytes of CDP's serialized string are preserved exactly.
- **External `<link>` stylesheets are NOT inlined** — they are already captured
  as network resource records in the WARC and are replayable. Folding covers only
  CSSOM-only content that `getOuterHTML` loses.
- **Always re-serialize every `<style>` body** from its live `sheet.cssRules`,
  rather than detecting which were mutated. This guarantees CSSOM modifications
  are captured without reintroducing the CSS-domain / styleSheetId↔node bridge.
  Cost is cosmetic: unmodified `<style>` get reserialized (comments stripped,
  whitespace normalized, browser-rejected invalid rules dropped) — all
  render-identical.

## Component 1 — Isolated-world execution (`isolated.py`, new)

A small helper module centralizing isolated-world use. An isolated world shares
the frame's DOM tree but has its own JS global scope and pristine builtins, so
the page cannot tamper with our logic.

```python
async def isolated_context(tab, frame_id) -> ExecutionContextId:
    """Page.createIsolatedWorld(frame_id) -> execution context id."""

async def evaluate_isolated(tab, frame_id, expression, *, return_by_value=True):
    """Runtime.evaluate(expression, context_id=<isolated ctx for frame_id>)."""

async def call_on_node_isolated(tab, frame_id, backend_node_id, fn_decl, *, return_by_value=True):
    """DOM.resolveNode(backend_node_id, execution_context_id=<iso ctx>) then
    Runtime.callFunctionOn(object_id=..., function_declaration=fn_decl)."""
```

**Context lifecycle:** worlds are created **per operation, not cached.** Caching
across navigations risks `Runtime.executionContextDestroyed` staleness;
`createIsolatedWorld` is cheap enough to call on demand. Each helper resolves the
target frame's id, creates a world, runs, and releases any resolved object ids.

**Migrations to isolated world:**

- `rendered.py::_owner_selector` → `call_on_node_isolated` (the owner `<iframe>`
  element's backendNodeId resolved into the parent frame's isolated world).
- `rendered.py::_capture_screenshot_b64`'s `window.scrollTo(0,0)` →
  `evaluate_isolated`.
- `app.py` `document.title` / `document.readyState` reads in `GET /tabs/{id}` →
  `evaluate_isolated` against the main frame.

**`POST /tabs/{id}/eval`:** add `world: Literal["main","isolated"] = "main"` to
`EvalRequest` (models.py). `"main"` preserves today's `tab.evaluate` path;
`"isolated"` routes through `evaluate_isolated` on the main frame and deserializes
the `return_by_value` result into `EvalResponse.result`.

## Component 2 — `includeShadowDOM=true`

Pass `include_shadow_dom=True` to both `DOM.getOuterHTML` calls in `rendered.py`:
`_capture_top_dom` and `_capture_frame_html_by_owner`. CDP serializes each shadow
root (open *and* closed) as a `<template shadowrootmode="open|closed">` inserted
as the **host's first child** (verified empirically). Intrinsic to `?rendered=1`.

## Component 3 — CSSOM folding (`cssom.py`, new), per frame

Two stages, run once per frame inside `capture_rendered_payload` after that
frame's HTML is serialized.

### Stage A — extraction (isolated-world JS, zero live mutation)

A recursive DOM tree walk in the frame's isolated world (NOT `querySelectorAll`,
which neither pierces shadow roots nor yields paths) produces a JSON **plan**: a
list of `{path, action, css}` where:

- `path` is an **element-index path** from `documentElement` — 1-based, counting
  only element children (text/comments ignored, matching how the Python locator
  counts start-tags).
- `action` is `"replace-style-body"` (a `<style>`; replace its inner text with
  serialized `sheet.cssRules`) or `"insert-adopted"` (insert a new `<style>` for
  an adopted/constructed sheet).
- `css` is `Array.from(sheet.cssRules, r => r.cssText).join('\n')`, wrapped in
  try/catch (cross-origin `@import` etc. → skip that sheet).

The walk descends three container edges, each mirrored against CDP's
serialization:

- **`element.children`** — normal element children.
- **`element.shadowRoot`** (open only) — descend under a synthetic leading
  `<template>` step (the host's first serialized child); collect this root's
  `adoptedStyleSheets` as `insert-adopted` at the template path.
- **`template.content`** — a real `<template>`'s children live in `.content`.

Document-level `document.adoptedStyleSheets` → `insert-adopted` at path
`["head"]`.

**Off-by-one fix (found during the probe):** when an element has *both* a shadow
root and light-DOM children, CDP serializes the synthetic `<template>` as child 1,
shifting the light children to indices 2, 3, …. The walk must therefore start the
light-child counter at 1 (so the first light child is index 2) when the element
being walked has a `shadowRoot`. Elements without a shadow root, shadow roots
themselves, and template-content fragments start at 0.

### Stage B — application (Python, byte-preserving splice)

`html.parser.HTMLParser` walks CDP's serialized string tracking the same
element-index path and byte offsets (`getpos()` + line/col→offset mapping). For
each plan entry it resolves the path to a byte span, then:

- `replace-style-body`: replace the bytes between the `<style>`'s open and close
  tags with `css`. `<style>` is a raw-text element, so no HTML-escaping is needed,
  but any literal `</style>` substring inside `css` is neutralized (e.g.
  `<\/style>`) so it can't prematurely close the tag.
- `insert-adopted`: insert `<style>{css}</style>` immediately after the open tag
  of the container at `path` (the `<head>` for document-level; the
  `<template shadowrootmode>` for shadow-scoped).

**Edit ordering:** edits are applied to the original string in **descending byte
offset** so earlier offsets stay valid as later spans are spliced. All bytes
outside edited spans are preserved exactly — CDP's serialization fidelity (closed
shadow roots, attribute quoting, entities) is untouched.

`html.parser` already handles raw-text elements (`<style>`/`<script>`) and
`<template>` nesting; it treats `<template shadowrootmode>` as an ordinary tag,
which is correct for path-walking.

## Limitations (documented behavior)

- **Closed shadow roots:** CDP serializes their structure, but JS (any world)
  cannot reach a closed root's `adoptedStyleSheets`/`cssRules`, so their
  CSSOM-only content cannot be folded in. The structure is still captured.
- **Unmodified `<style>` reserialization:** comments and browser-rejected invalid
  rules are dropped; whitespace is normalized. Render-identical, but not
  byte-identical to the original source.
- **External `<link>` CSS** is not inlined into the snapshot (replayable from the
  WARC network records).

## Empirical validation

A live-Chromium probe (data: URL with a CSSOM-mutated `<style>`, a document-level
adopted sheet, an open shadow root with both a `<style>` and its own adopted
sheet, plus a page that overwrote `Array.from` and redefined `document.title`)
confirmed:

- The isolated world read the real `document.title` (`"orig"`, not the page's
  `"HACKED"` getter) and used a pristine `Array.from` despite the page breaking
  it — wrappers and builtins are isolated.
- All four CSSOM cases were captured, including the `insertRule` mutation and the
  shadow-root adopted sheet — both invisible to raw `getOuterHTML`.
- Plan paths matched the serialized structure exactly, confirming the synthetic
  `<template shadowrootmode>` is the host's first child (`[2,2,1]`) with its
  `<style>` at `[2,2,1,1]`.
- The off-by-one risk for hosts with both a shadow root and light children (the
  probe host had no light children) — fix specified in Stage A.

## Testing

New fixtures under `tests/fixtures/` (served by the existing `fixture_server`),
each asserting the folded `renderedContent` contains the expected rules at the
right nesting depth:

- document-level adopted stylesheet → folded into `<head>`.
- CSSOM-mutated `<style>` (`insertRule`) → folded rule present.
- `<style>` in `<body>` (not just `<head>`) → correct positional splice.
- open shadow root with an adopted sheet + a `<style>` → folded inside the
  `<template shadowrootmode>`.
- host with both a shadow root AND light-DOM children → light-child paths are
  off-by-one-correct (regression guard for the Stage A fix).
- nested shadow DOM → recursion correctness.

Plus:

- `POST /eval` `world` field: `main` sees page globals; `isolated` does not.
- isolated-world tamper test: a page overriding `Array.from` / `document.title`
  still yields a correct capture (mirrors the probe).
- `html.parser` locator unit tests on hand-written serialized strings (raw-text
  `<style>`, `<template>` nesting, descending-offset edit ordering).

Smoke-marked tests are not required; all of the above can use local fixtures and
the session-scoped real-Chromium `browser_pool`.

## Out of scope

- Inlining external `<link>` stylesheets.
- A separate CSSOM WARC record (the data is folded into `renderedContent`).
- Caching isolated-world contexts across navigations.
