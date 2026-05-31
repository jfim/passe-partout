# CSSOM folding robustness: snapshot consistency, retry, and fallback dump

**Date:** 2026-05-31
**Status:** Approved (design); implementation pending
**Supersedes parts of:** `2026-05-30-capture-fidelity-improvements-design.md`
(Component 3 — CSSOM folding). That design assumed the extraction walk and the
serialized HTML describe the *same* DOM. They do not on live, mutating pages, and
the failure is silent and corrupting. This spec hardens that path.

## Problem

`fold_cssom` builds a CSS plan keyed by **absolute element-index path** from a
live isolated-world DOM walk (`EXTRACT_JS`), then splices it into a *separately
captured* `DOM.getOuterHTML` string located by a Python `HTMLParser`. The two are
**non-atomic reads of a live DOM**:

1. In `rendered.py`, `_capture_top_dom` (getOuterHTML) runs, then a
   viewport-**scrolling** screenshot runs, then `fold_cssom` runs `EXTRACT_JS` —
   so for the top frame the two reads are separated by a scroll and other async
   work.
2. Pages reorder/churn `<head>` between the reads (emotion/"lights" CSS-in-JS, ad
   styles, prefetch `<link>`s). Any churn before a target shifts its absolute
   index, so `replace-style-body` splices into the **wrong adjacent `<style>`**.

**Confirmed empirically (NYTimes article, live instance, 2026-05-31):** a fresh
capture spliced a ~452 KB emotion sheet into `<style id="ext10">` instead of
`<style data-lights="css">`, leaving the masthead-logo sizing class (`css-93zicp`)
undefined → the inline NYT `<svg>` logo renders at full column width. Reproduced
with zero observed DOM mutation in the sampling window, so this is a **structural**
flaw in the matching, not merely a race. The earlier "cross-origin `@import`
poisons the sheet" theory was disproved by live probe (the lights sheets read
cleanly; only Google Identity's cross-origin sheet throws).

A misrouted splice is **worse than no splice** — it injects wrong rules. The fix
must never misroute: it must either splice correctly, or not splice and preserve
the data for recovery.

## Design constraints (locked)

- **No live DOM mutation during extraction.** The whole reason for the
  byte-preserving splice is to avoid anything the main world can react to
  (`MutationObserver`s fire across worlds since the DOM is shared; even a sentinel
  attribute is observable). We keep the read-only extract + offline splice.
- **Never misroute.** Splice only when we can prove the plan and the serialized
  HTML correspond. Otherwise retry; if still inconsistent, fall back to a lossless
  dump. Degrade safe, never corrupt.

## Approach — four layers, each failing into the next

### Layer 1 — Shrink the window (`rendered.py` ordering)

For the **top frame**, capture `getOuterHTML` and run the `EXTRACT_JS` walk
back-to-back, with no intervening work. Move the screenshot (and its
`window.scrollTo(0,0)`) to **after** both reads, or before both — never between.
Child frames already capture HTML (`_capture_frame_html_by_owner`) and fold
adjacently; only the top frame needs reordering. This minimizes the churn window;
it does not by itself guarantee consistency (Layers 2–3 do).

### Layer 2 — Churn-resistant keys (`cssom.py`)

Replace the absolute element-index path as the *match key* with a key that is
immune to non-`<style>` sibling churn:

```
style_key = (parent_path, style_index_within_parent)
```

- `parent_path` — the element-index path to the `<style>`'s parent (same 1-based,
  element-only counting as today).
- `style_index_within_parent` — 1-based index of this `<style>` **among its
  parent's `<style>` element children only**.

Rationale: the dominant churn is non-style head nodes (`<meta>`/`<link>`/
`<script>`/prefetch) being added/removed/reordered. Those shift absolute indices
but not the style-among-styles index under a stable parent (`<head>` is `html`'s
child — maximally stable). So for the common case the keys are unchanged between
snapshots and the splice just works, with no retry.

`parent_path` can still drift if a *non-style ancestor* of a deeply nested
`<style>` churns. That does not cause misrouting — it causes a key-set mismatch
(Layer 3), i.e. a retry. So Layer 2 reduces retries; Layer 3 guarantees safety.

`insert-adopted` actions are keyed by their **container**: `("document",)` for
`document.adoptedStyleSheets` (injected at end of `<head>`), or the open shadow
host's `style_key`-style path for shadow-root adopted sheets (injected at end of
the `<template shadowrootmode>`). Adopted sheets append at end-of-container, so
their position among styles is irrelevant to correctness.

### Layer 3 — Consistency gate + retry (`cssom.py` + `rendered.py`)

The Python locator already re-derives structure from the stored HTML, so no extra
CDP read is needed. Have **both** sides emit a **structural signature**:

```
signature(dom) = ordered list, in document order, of every <style> element's
                 style_key  ++  the set of insert-adopted container keys
```

- `EXTRACT_JS` returns `signature_walk` alongside the plan.
- The `HTMLParser` locator computes `signature_locator` from the stored HTML.

**Gate:** splice **iff** `signature_walk == signature_locator`. On equality, every
plan entry's key resolves to exactly one locator byte span — apply the
byte-preserving splice exactly as today (descending-offset edits, `</style>`
neutralized).

**On mismatch:** the DOM moved in a structurally relevant way. **Retry** the
whole unit for that frame — re-serialize its HTML *and* re-run `EXTRACT_JS`,
back-to-back — after a short settle. Cap retries; small backoff:

- `max_attempts = 3` (parameterized; threaded through `capture_rendered_payload`
  and exposed as the `/warc?...&cssom_max_attempts=N` query knob).
- **`max_attempts = 0` skips splicing entirely and goes straight to the fallback**
  after one serialize+extract — a deterministic way to exercise the dump path in
  tests (and to force a dump in the field if a page is known-pathological)
  without a flaky DOM-churn fixture.
- settle backoff before re-read: `200ms`, then `400ms` (cheap fixed backoff; not
  network-idle — we only need the head to stop churning briefly).

This should resolve essentially always (the inconsistency window is sub-frame).

### Layer 4 — Lossless fallback dump (`cssom.py` → `rendered.py` → `warc.py`)

If all attempts still mismatch for a frame, **do not splice that frame** (leave
its `renderedContent` as the raw `getOuterHTML`, which already contains correct
`<link>` CSS and any server-rendered `<style>` text). Instead, carry that frame's
extracted CSS out as a **fallback dump**, emitted as a new WARC conversion record.

Key insight that makes the dump fully recoverable: **for rendering, only scope and
order matter, not the exact owner `<style>`.** CSS-in-JS rules are class-scoped
(`.css-93zicp { … }` only affects elements with that class), so a viewer that
injects each dumped sheet at **end-of-scope** reproduces the styling. The only
residual loss is cascade ties (equal-specificity rules across sheets) and explicit
`@layer` ordering — documented, rare for class-scoped CSS-in-JS.

## WARC record contract (the viewer contract)

New conversion record, emitted **only when at least one frame fell back** (absence
of the record ⇒ all frames folded inline; nothing for the viewer to do).

- `WARC-Type: conversion`
- `WARC-Profile: urn:passe-partout:warc:cssom-fallback:1.0`
- `Content-Type: application/json`
- `WARC-Refers-To: <record-id of the main-document response>` (same dangling-ref
  guard as the rendered/dom-snapshot records — only emitted when a main-doc
  response exists)
- `WARC-Target-URI: <page URL>`

Body JSON:

```jsonc
{
  "version": "1.0",
  "frames": [
    {
      "frameId": "<Chrome frameId, matches rendered-targets _passepartout_frameId>",
      "sheets": [
        {
          "scope": "document",          // or "shadow"
          "shadowHostSelector": null,    // CSS selector to the shadow host when scope=="shadow"
          "order": 0,                    // document/insertion order within the scope (cascade order)
          "css": "<serialized sheet.cssRules text>"
        }
        // … in ascending `order`
      ]
    }
    // … only frames that fell back
  ]
}
```

**Viewer-side application (warc-viewer; specified here, implemented later):**
per frame, group `sheets` by scope in ascending `order`:

- `scope: "document"` → append one `<style>` per sheet at the **end of that
  frame's `<head>`** (matches adopted-sheet cascade position).
- `scope: "shadow"` → resolve `shadowHostSelector` within the frame; append each
  `<style>` at the **end of the host's declarative shadow template**. If the host
  can't be resolved, skip that sheet (and the viewer may log it) — document-scoped
  recovery is unaffected.

`shadowHostSelector` reuses the existing absolute-from-`<html>`
`:nth-of-type`-disambiguated selector generator already used for
`_passepartout_ownerSelector` (`rendered.py::_OWNER_SELECTOR_FN`).

## Plumbing

- `cssom.py`:
  - `EXTRACT_JS` additionally returns `signature` and, per sheet, `scope` /
    `shadowHostSelector` / `order` (so a fallback can be built without re-walking).
  - `fold_cssom(tab, frame_id, html)` → returns `(html, fallback)` where
    `fallback` is `None` on success (spliced, or genuinely nothing to fold) or a
    `{frameId, sheets:[…]}` dict when the gate failed after retries. Retry loop
    lives here (it owns the re-serialize + re-walk).
  - `apply_css_plan` unchanged except keys are `(parent_path, style_index)`; the
    locator tracks per-parent `<style>` ordinal in addition to byte spans.
- `rendered.py`:
  - Reorder top-frame capture so HTML serialize + fold are adjacent; screenshot
    moved out of the middle.
  - `capture_rendered_payload` collects per-frame `fallback`s; returns them
    alongside the HAR payload (e.g. `(payload, cssom_fallback)` where
    `cssom_fallback` is `{version, frames:[…]}` or `None`).
- `warc.py` / `/warc` route:
  - `build_warc` gains `cssom_fallback_payload` + `cssom_fallback_profile` params,
    mirroring the dom-snapshot record block (same `WARC-Refers-To` guard).

## Testing (capture side)

Local fixtures via `fixture_server`, real-Chromium `browser_pool`:

- **Layer 2 key correctness:** fixture whose `<head>` has non-style siblings
  interleaved with `<style>`s; assert keys are `(parent, style-ordinal)` and a
  splice lands correctly.
- **Gate never misroutes:** craft walk/locator signatures that differ; assert
  `apply_css_plan` refuses to splice (returns `None`) rather than guessing.
  (Unit, no browser — `test_cssom.py`.)
- **Layer 4 dump path (deterministic, no churn fixture):** call
  `fold_cssom(..., max_attempts=0)` against the `cssom_page` fixture and assert it
  returns `(html, sheets)` with document- and shadow-scoped entries carrying
  `order` and a non-null `shadowHostSelector`. Forcing fallback via the retry knob
  is far more reliable than racing a timer-mutated `<head>`.
- **Layer 4 record shape (end-to-end):** `GET /warc?rendered=1&cssom_max_attempts=0`
  emits the `cssom-fallback` conversion record alongside the rendered-targets one,
  with correct `WARC-Profile`/`WARC-Refers-To` and a JSON body of
  `{version, frames:[{frameId, sheets:[{scope,shadowHostSelector,order,css}]}]}`.
- **Happy path unchanged:** `cssom_page` with default attempts folds inline and
  emits **no** fallback record (regression guard against always-dumping).
- Existing `test_cssom.py` splice/locator unit tests updated for the new key, plus
  a style-ordinal test proving non-`<style>` sibling churn doesn't shift keys.

## Out of scope (this change)

- warc-viewer-side consumption of the fallback record (separate change; contract
  defined above).
- Inlining external `<link>` CSS (unchanged: replayable from network records).
- Preserving `@layer` order / equal-specificity cascade ties across dumped sheets.
