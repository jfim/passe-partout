# Optional screenshot in WARC rendered capture

## Problem

`GET /tabs/{id}/warc?rendered=1` always captures a viewport screenshot as part of
the rendered-targets conversion record. The screenshot path calls
`window.scrollTo(0, 0)` before grabbing the PNG (`rendered.py` `_capture_screenshot_b64`),
which scrolls the live page back to the top. Callers who want the rendered per-frame
DOM (CSSOM fold, shadow DOM, owner selectors) but not the screenshot have no way to
avoid that scroll side effect — the screenshot is mandatory and, in fact, a screenshot
failure currently aborts the entire rendered record (all-or-nothing).

A plain `?warc` (no `rendered`) already produces a WARC with no screenshot and no
scroll. The gap is specifically: rendered DOM capture *without* the screenshot.

## Goal

Let callers opt out of the screenshot (and its scroll-to-top) while still getting the
rendered DOM capture. Backward compatible: existing `?rendered=1` behavior is unchanged.

## API

Add a query param to `GET /tabs/{id}/warc`:

- `screenshot: bool = True`

Behavior matrix:

| Request | Result |
| --- | --- |
| `?rendered=1` (no `screenshot`) | Unchanged: per-frame DOM + top-frame screenshot; scrolls to top. |
| `?rendered=1&screenshot=0` | Per-frame DOM only; **no scroll-to-top**; top frame has no `renderedElements`. |
| `?rendered=0` (default) | No rendered record at all; `screenshot` is a no-op. |

`screenshot` is only meaningful alongside `rendered=1` because the screenshot lives
inside the rendered capture. With `rendered=0` it is silently ignored (no error).

## Implementation

### `rendered.py` — `capture_rendered_payload`

- New parameter `include_screenshot: bool = True`.
- When `False`:
  - Skip `_capture_screenshot_b64` entirely, so `window.scrollTo(0, 0)` never runs.
  - Omit the `renderedElements` array from the top-frame entry.
- The all-or-nothing guard that returns `None, None` on a missing screenshot becomes
  conditional on `include_screenshot`. When screenshots are off there is no screenshot
  to fail on; the mandatory part of the top-level capture is the DOM only (a missing
  top-level DOM still aborts the whole record, as today).

### `app.py` — `get_warc`

- Add `screenshot: bool = True` to the handler signature.
- Thread it into the existing `capture_rendered_payload(...)` call as
  `include_screenshot=screenshot`.

## Testing

Add a test to `tests/test_warc_endpoint.py`:

- `GET /tabs/{id}/warc?rendered=1&screenshot=0` returns a rendered conversion record
  whose top-frame page entry has `renderedContent` but **no** `renderedElements`.

Existing rendered tests (which assert `renderedElements` is present for `?rendered=1`)
remain unchanged and continue to pass, confirming backward compatibility.

## Docs

Update the `ResourceRecorder` paragraph in `CLAUDE.md` to note that `?screenshot=0`
opts out of the screenshot (and its scroll-to-top) while keeping the rendered DOM.
