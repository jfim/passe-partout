# Capture Fidelity Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise passe-partout capture fidelity by running internal JS in isolated worlds, serializing shadow DOM, and folding CSSOM (adopted stylesheets + CSSOM-mutated `<style>` rules) into the rendered HTML snapshot.

**Architecture:** A new `isolated.py` centralizes `Page.createIsolatedWorld`-based evaluation. `rendered.py` migrates its owner-selector + scroll JS to isolated worlds and passes `include_shadow_dom=True` to `DOM.getOuterHTML`. A new `cssom.py` extracts a CSS "plan" via one isolated-world DOM walk (text + element-index path together, zero live mutation) and applies it to CDP's serialized HTML via byte-preserving span splicing (`html.parser` locates spans; we never re-serialize). Folding runs per frame inside `capture_rendered_payload`; everything rides on the existing `?rendered=1`.

**Tech Stack:** Python 3.12, FastAPI, nodriver (CDP), `html.parser` (stdlib), pytest + pytest-asyncio, warcio (tests).

**Design doc:** `docs/superpowers/specs/2026-05-30-capture-fidelity-improvements-design.md`

---

## File Structure

- **Create** `src/passe_partout/isolated.py` — isolated-world helpers (`evaluate_isolated`, `call_on_node_isolated`, `main_frame_id`).
- **Create** `src/passe_partout/cssom.py` — `EXTRACT_JS`, pure splicer `apply_css_plan`, and `fold_cssom`.
- **Modify** `src/passe_partout/models.py` — add `world` field to `EvalRequest`.
- **Modify** `src/passe_partout/app.py` — `/eval` honors `world`; `get_tab` reads title/readyState in an isolated world.
- **Modify** `src/passe_partout/rendered.py` — `include_shadow_dom=True`; isolated owner-selector + scroll; integrate `fold_cssom`.
- **Create** `tests/fixtures/cssom_page.html`, `tests/fixtures/shadow_page.html`.
- **Create** `tests/test_isolated.py`, `tests/test_cssom.py`.
- **Modify** `tests/test_tab_ops.py` — `/eval` `world` test.
- **Modify** `tests/test_warc_endpoint.py` — shadow-DOM + CSSOM folding e2e tests.

---

## Task 1: Isolated-world helpers (`isolated.py`)

**Files:**
- Create: `src/passe_partout/isolated.py`
- Test: `tests/test_isolated.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_isolated.py
from __future__ import annotations

import pytest

from passe_partout.isolated import call_on_node_isolated, evaluate_isolated, main_frame_id


async def _tab(browser_pool):
    return await browser_pool.create_context(
        "data:text/html,<html><body><div id=x>hi</div></body></html>"
    )


@pytest.mark.asyncio
async def test_evaluate_isolated_reads_dom(browser_pool):
    tab = await _tab(browser_pool)
    try:
        fid = await main_frame_id(tab)
        val = await evaluate_isolated(tab, fid, "document.getElementById('x').textContent")
        assert val == "hi"
    finally:
        await browser_pool.close_context(tab)


@pytest.mark.asyncio
async def test_isolated_world_is_tamper_resistant(browser_pool):
    html = (
        "data:text/html,<html><body><script>"
        "Array.from=function(){throw new Error('x')};"
        "Object.defineProperty(document,'title',{get(){return 'HACKED'}});"
        "</script></body></html>"
    )
    tab = await browser_pool.create_context(html)
    try:
        fid = await main_frame_id(tab)
        title = await evaluate_isolated(tab, fid, "document.title")
        used = await evaluate_isolated(tab, fid, "Array.from([1,2]).length")
        assert title != "HACKED"
        assert used == 2
    finally:
        await browser_pool.close_context(tab)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_isolated.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'passe_partout.isolated'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/passe_partout/isolated.py
"""Run passe-partout's own JavaScript in an isolated world.

An isolated world (Page.createIsolatedWorld) shares the frame's DOM tree but has
its own JS global scope and pristine builtins, so a hostile or instrumented page
cannot tamper with the logic our capture relies on. Worlds are created per
operation (not cached): createIsolatedWorld is cheap, and caching across
navigations risks Runtime.executionContextDestroyed staleness.
"""

from __future__ import annotations

from typing import Any

import nodriver as uc


class IsolatedWorldError(RuntimeError):
    """Raised when evaluation in an isolated world throws."""


async def main_frame_id(tab: uc.Tab) -> uc.cdp.page.FrameId:
    tree = await tab.send(uc.cdp.page.get_frame_tree())
    return tree.frame.id_


async def _create_world(tab: uc.Tab, frame_id: uc.cdp.page.FrameId) -> uc.cdp.runtime.ExecutionContextId:
    return await tab.send(
        uc.cdp.page.create_isolated_world(frame_id=frame_id, world_name="passe_partout")
    )


async def evaluate_isolated(
    tab: uc.Tab,
    frame_id: uc.cdp.page.FrameId,
    expression: str,
    *,
    return_by_value: bool = True,
) -> Any:
    """Evaluate `expression` in a fresh isolated world for `frame_id`."""
    ctx = await _create_world(tab, frame_id)
    result, exc = await tab.send(
        uc.cdp.runtime.evaluate(
            expression=expression,
            context_id=ctx,
            return_by_value=return_by_value,
            await_promise=True,
        )
    )
    if exc is not None:
        raise IsolatedWorldError(str(exc))
    return result.value if return_by_value else result


async def call_on_node_isolated(
    tab: uc.Tab,
    frame_id: uc.cdp.page.FrameId,
    backend_node_id: uc.cdp.dom.BackendNodeId,
    function_declaration: str,
    *,
    return_by_value: bool = True,
) -> Any:
    """Resolve `backend_node_id` into an isolated world for `frame_id`, then
    callFunctionOn with `function_declaration` (whose `this` is the node)."""
    ctx = await _create_world(tab, frame_id)
    resolved = await tab.send(
        uc.cdp.dom.resolve_node(backend_node_id=backend_node_id, execution_context_id=ctx)
    )
    object_id = getattr(resolved, "object_id", None)
    if object_id is None:
        raise IsolatedWorldError("resolve_node returned no object_id")
    try:
        result, exc = await tab.send(
            uc.cdp.runtime.call_function_on(
                function_declaration=function_declaration,
                object_id=object_id,
                return_by_value=return_by_value,
            )
        )
        if exc is not None:
            raise IsolatedWorldError(str(exc))
        return result.value if return_by_value else result
    finally:
        try:
            await tab.send(uc.cdp.runtime.release_object(object_id=object_id))
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_isolated.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/isolated.py tests/test_isolated.py
git commit -m "feat: isolated-world evaluation helpers"
```

---

## Task 2: `/eval` honors a `world` field

**Files:**
- Modify: `src/passe_partout/models.py:146-148`
- Modify: `src/passe_partout/app.py:736-750`
- Test: `tests/test_tab_ops.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tab_ops.py`:

```python
@pytest.mark.asyncio
async def test_eval_world_isolated_ignores_page_globals(client, fixture_server):
    tid = await _open(client, f"{fixture_server}/js.html")
    try:
        # Define a global in the page's main world.
        await client.post(f"/tabs/{tid}/eval", json={"js": "window.__pp = 42; null"})
        main = await client.post(
            f"/tabs/{tid}/eval", json={"js": "window.__pp", "world": "main"}
        )
        iso = await client.post(
            f"/tabs/{tid}/eval", json={"js": "window.__pp ?? 'absent'", "world": "isolated"}
        )
        assert main.json()["result"] == 42
        assert iso.json()["result"] == "absent"
    finally:
        await _close(client, tid)
```

(Reuse the `_open`/`_close` helpers already in `tests/test_tab_ops.py`; if absent, copy the two-line helpers from `tests/test_warc_endpoint.py:10-17`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tab_ops.py::test_eval_world_isolated_ignores_page_globals -v`
Expected: FAIL — `world` rejected as extra field or ignored (isolated returns 42).

- [ ] **Step 3a: Add the field to `EvalRequest`**

In `src/passe_partout/models.py`, add `Literal` to the typing import at the top (`from typing import Annotated, Any, Literal` — match existing import style) and change:

```python
class EvalRequest(BaseModel):
    js: Annotated[str, Field(min_length=1, max_length=JS_MAX)]
    world: Literal["main", "isolated"] = "main"
```

- [ ] **Step 3b: Honor it in the route**

In `src/passe_partout/app.py`, add the import near the other passe_partout imports:

```python
from passe_partout.isolated import evaluate_isolated, main_frame_id
```

Replace the body of `eval_js` (currently `app.py:739-750`) with:

```python
    async def eval_js(tab_id: int, req: EvalRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        async with rec.lock:
            try:
                if req.world == "isolated":
                    fid = await main_frame_id(rec.tab)
                    result = await evaluate_isolated(rec.tab, fid, req.js)
                else:
                    result = await rec.tab.evaluate(req.js)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
        return EvalResponse(result=result)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tab_ops.py::test_eval_world_isolated_ignores_page_globals -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/models.py src/passe_partout/app.py tests/test_tab_ops.py
git commit -m "feat: /eval world field (main|isolated)"
```

---

## Task 3: `get_tab` reads title/readyState in an isolated world

**Files:**
- Modify: `src/passe_partout/app.py:330-341`
- Test: `tests/test_tabs.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tabs.py` (it already builds tabs via `client`; mirror an existing test's `_open`/`_close` usage, or POST/DELETE `/tabs` inline):

```python
@pytest.mark.asyncio
async def test_get_tab_title_resists_page_tampering(client):
    html = (
        "data:text/html,<html><head><title>real</title></head><body><script>"
        "Object.defineProperty(document,'title',{get(){return 'HACKED'}});"
        "</script></body></html>"
    )
    r = await client.post("/tabs", json={"url": html})
    tid = r.json()["id"]
    try:
        got = await client.get(f"/tabs/{tid}")
        assert got.json()["title"] == "real"
    finally:
        await client.delete(f"/tabs/{tid}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tabs.py::test_get_tab_title_resists_page_tampering -v`
Expected: FAIL — title is `"HACKED"` (main-world `tab.evaluate`).

- [ ] **Step 3: Switch to isolated evaluation**

In `src/passe_partout/app.py`, replace the two `rec.tab.evaluate` lines in `get_tab` (`app.py:339-340`) with a single isolated read (import already added in Task 2; add `import json` at the top of `app.py` if not present):

```python
        registry.touch(tab_id)
        fid = await main_frame_id(rec.tab)
        meta = await evaluate_isolated(
            rec.tab, fid, "JSON.stringify([document.title, document.readyState])"
        )
        title, ready = json.loads(meta) if meta else ["", ""]
        return TabState(url=rec.tab.url or "", title=title or "", ready_state=ready or "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tabs.py -v`
Expected: PASS (new test + existing tab tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/app.py tests/test_tabs.py
git commit -m "feat: read tab title/readyState in isolated world"
```

---

## Task 4: shadow DOM + isolated owner-selector/scroll in `rendered.py`

**Files:**
- Modify: `src/passe_partout/rendered.py`
- Create: `tests/fixtures/shadow_page.html`
- Test: `tests/test_warc_endpoint.py`

- [ ] **Step 1: Add the fixture**

```html
<!-- tests/fixtures/shadow_page.html -->
<!DOCTYPE html>
<html>
  <head><title>shadow</title></head>
  <body>
    <h1>Hello</h1>
    <div id="host"></div>
    <script>
      const sr = document.getElementById('host').attachShadow({ mode: 'open' });
      sr.innerHTML = '<span class="inside">SHADOW_CONTENT</span>';
    </script>
  </body>
</html>
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_warc_endpoint.py`:

```python
@pytest.mark.asyncio
async def test_warc_rendered_includes_shadow_dom(client, fixture_server):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/shadow_page.html", mode="copy")
    try:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        pages = json.loads(conv)["log"]["pages"]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        dom_html = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")
        assert "shadowrootmode" in dom_html
        assert "SHADOW_CONTENT" in dom_html
    finally:
        await _close(client, tab_id)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest "tests/test_warc_endpoint.py::test_warc_rendered_includes_shadow_dom" -v`
Expected: FAIL — `shadowrootmode`/`SHADOW_CONTENT` absent (shadow DOM not serialized).

- [ ] **Step 4: Edit `rendered.py`**

4a. Add the import after the existing imports:

```python
from passe_partout.isolated import call_on_node_isolated, evaluate_isolated, main_frame_id
```

4b. `_capture_frame_html_by_owner` — pass shadow DOM (replace the return at `rendered.py:90`):

```python
        return await tab.send(
            uc.cdp.dom.get_outer_html(backend_node_id=content_backend, include_shadow_dom=True)
        )
```

4c. Replace `_capture_top_dom` (`rendered.py:129-137`):

```python
async def _capture_top_dom(tab: uc.Tab) -> str | None:
    try:
        doc = await tab.send(uc.cdp.dom.get_document(-1, True))
    except Exception:
        return None
    try:
        return await tab.send(
            uc.cdp.dom.get_outer_html(backend_node_id=doc.backend_node_id, include_shadow_dom=True)
        )
    except Exception:
        return None
```

4d. Replace `_capture_screenshot_b64` (`rendered.py:140-151`) to scroll via the isolated world:

```python
async def _capture_screenshot_b64(tab: uc.Tab, top_frame_id: uc.cdp.page.FrameId) -> str | None:
    """Scroll to top (best effort) then capture viewport PNG, base64-encoded."""
    try:
        await evaluate_isolated(tab, top_frame_id, "window.scrollTo(0, 0)")
    except Exception:
        pass
    try:
        return await tab.send(uc.cdp.page.capture_screenshot(format_="png"))
    except Exception:
        return None
```

4e. Replace `_owner_selector` (`rendered.py:95-126`) to run in the owner node's frame's isolated world:

```python
async def _owner_selector(
    tab: uc.Tab, frame_id: uc.cdp.page.FrameId, owner_backend_node_id: int
) -> str | None:
    """Generate a CSS selector for an iframe-owning element via an isolated world."""
    try:
        value = await call_on_node_isolated(
            tab,
            frame_id,
            uc.cdp.dom.BackendNodeId(owner_backend_node_id),
            _OWNER_SELECTOR_FN,
            return_by_value=True,
        )
    except Exception:
        return None
    return value if isinstance(value, str) else None
```

4f. In `capture_rendered_payload`, fetch the frame tree first so the top frame id is available, and thread it through. Replace `rendered.py:165-176` (the `started_at`/top_dom/screenshot/tree block) with:

```python
    started_at = _iso_now()
    try:
        tree = await tab.send(uc.cdp.page.get_frame_tree())
    except Exception:
        return None
    top_frame_id = tree.frame.id_
    top_dom = await _capture_top_dom(tab)
    if top_dom is None:
        return None
    screenshot_b64 = await _capture_screenshot_b64(tab, top_frame_id)
    if screenshot_b64 is None:
        return None
```

4g. In `walk`, update the owner-selector call. The owner element lives in the **parent** frame, so pass the parent frame id. Replace the `owner_selector = await _owner_selector(tab, int(backend_id))` line (`rendered.py:202`) with:

```python
            owner_selector = await _owner_selector(
                tab, uc.cdp.page.FrameId(parent_frame_id), int(backend_id)
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_warc_endpoint.py -k "rendered" -v`
Expected: PASS (new shadow test + existing rendered tests, including iframe DOM).

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/rendered.py tests/fixtures/shadow_page.html tests/test_warc_endpoint.py
git commit -m "feat: serialize shadow DOM; run rendered.py capture JS in isolated worlds"
```

---

## Task 5: CSS plan splicer — pure `apply_css_plan` (`cssom.py`)

This task is browser-free and TDD-friendly. It builds the byte-preserving splicer
that applies a plan to a serialized HTML string.

**Files:**
- Create: `src/passe_partout/cssom.py`
- Test: `tests/test_cssom.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cssom.py
from __future__ import annotations

from passe_partout.cssom import apply_css_plan


def test_replace_style_body():
    html = "<html><head></head><body><style>.a{}</style></body></html>"
    # body=[2], style is body's element child #1 -> [2,1]
    plan = [{"action": "replace-style-body", "path": [2, 1], "css": ".a{color:red}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.a{color:red}</style>" in out
    assert ".a{}" not in out


def test_insert_document_adopted_before_body_end():
    html = "<html><head></head><body><h1>x</h1></body></html>"
    plan = [{"action": "insert-document", "css": ".doc{font-size:9px}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.doc{font-size:9px}</style></body>" in out


def test_insert_adopted_into_shadow_template_end():
    html = (
        "<html><head></head><body><div>"
        '<template shadowrootmode="open"><span>s</span></template>'
        "</div></body></html>"
    )
    # body=[2], div=[2,1], synthetic template = div's child #1 -> [2,1,1]
    plan = [{"action": "insert-adopted", "path": [2, 1, 1], "css": ".sh{padding:2px}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.sh{padding:2px}</style></template>" in out


def test_light_child_index_offset_when_host_has_shadow():
    # div has BOTH a shadow template (child #1) AND a light <style> (child #2).
    html = (
        "<html><head></head><body><div>"
        '<template shadowrootmode="open"><style>.in{}</style></template>'
        "<style>.light{}</style>"
        "</div></body></html>"
    )
    # light <style> path = [2,1,2]; shadow <style> path = [2,1,1,1]
    plan = [
        {"action": "replace-style-body", "path": [2, 1, 2], "css": ".light{color:green}"},
        {"action": "replace-style-body", "path": [2, 1, 1, 1], "css": ".in{color:blue}"},
    ]
    out = apply_css_plan(html, plan)
    assert "<style>.light{color:green}</style>" in out
    assert "<style>.in{color:blue}</style>" in out


def test_multiple_inserts_preserve_order():
    html = "<html><head></head><body></body></html>"
    plan = [
        {"action": "insert-document", "css": ".first{}"},
        {"action": "insert-document", "css": ".second{}"},
    ]
    out = apply_css_plan(html, plan)
    assert out.index(".first{}") < out.index(".second{}")


def test_neutralizes_style_close_in_css():
    html = "<html><head></head><body><style>x</style></body></html>"
    plan = [{"action": "replace-style-body", "path": [2, 1], "css": 'a{content:"</style>"}'}]
    out = apply_css_plan(html, plan)
    # The literal </style> inside CSS must not prematurely close the tag.
    assert "</style>" in out
    assert 'content:"<\\/style>"' in out


def test_empty_plan_is_identity():
    html = "<html><head></head><body></body></html>"
    assert apply_css_plan(html, []) == html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cssom.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'passe_partout.cssom'`

- [ ] **Step 3: Write the splicer**

```python
# src/passe_partout/cssom.py
"""Fold CSSOM-only state into CDP's serialized HTML.

`DOM.getOuterHTML` loses CSS that has no faithful DOM representation: adopted /
constructed stylesheets (no DOM node at all) and `<style>` elements whose rules
were mutated via the CSSOM API (whose textContent goes stale). An isolated-world
DOM walk (EXTRACT_JS) produces a plan tying each piece of CSS to an element-index
path; `apply_css_plan` splices it into the serialized string by byte range,
preserving every other byte (no re-serialization). Adopted sheets are inserted
last in their scope to match the CSS cascade (adopted sheets apply after a
scope's own stylesheets).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

import nodriver as uc

from passe_partout.isolated import evaluate_isolated

# HTML void elements never have children or an end tag.
_VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_STYLE_CLOSE_RE = re.compile(r"</(style)", re.IGNORECASE)


def _neutralize_style_close(css: str) -> str:
    """Break any literal `</style` so embedded CSS can't close the host tag.
    Inside a CSS string `<\\/style` resolves back to `</style`, so rendering is
    unchanged; this byte sequence only legitimately occurs inside CSS strings."""
    return _STYLE_CLOSE_RE.sub(r"<\\/\1", css)


def _line_starts(s: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(s):
        if ch == "\n":
            starts.append(i + 1)
    return starts


class _Locator(HTMLParser):
    """Walks serialized HTML tracking each element's element-index path and the
    byte offsets needed to splice: end-of-start-tag, end-tag position, and
    `<style>` inner ranges. Element-index paths are relative to the root element
    (the root itself has the empty path); only element children are counted."""

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=False)
        self._ls = line_starts
        self._stack: list[list[Any]] = []  # [tag, child_count]
        self._path: list[int] = []
        self.starttag_end: dict[tuple[int, ...], int] = {}
        self.element_end: dict[tuple[int, ...], int] = {}
        self.style_inner: dict[tuple[int, ...], tuple[int, int]] = {}
        self.body_end: int | None = None
        self.html_end: int | None = None
        self._style_open: tuple[tuple[int, ...], int] | None = None

    def _off(self) -> int:
        line, col = self.getpos()
        return self._ls[line - 1] + col

    def _enter(self) -> tuple[int, ...]:
        if self._stack:
            self._stack[-1][1] += 1
            self._path.append(self._stack[-1][1])
        return tuple(self._path)

    def handle_starttag(self, tag, attrs):
        path = self._enter()
        end = self._off() + len(self.get_starttag_text() or "")
        self.starttag_end[path] = end
        if tag == "style":
            self._style_open = (path, end)
        if tag in _VOID:
            if self._path:
                self._path.pop()
        else:
            self._stack.append([tag, 0])

    def handle_startendtag(self, tag, attrs):
        path = self._enter()
        self.starttag_end[path] = self._off() + len(self.get_starttag_text() or "")
        if self._path:
            self._path.pop()

    def handle_endtag(self, tag):
        if not self._stack:
            return
        path = tuple(self._path)
        off = self._off()
        self.element_end[path] = off
        if tag == "body":
            self.body_end = off
        elif tag == "html":
            self.html_end = off
        if tag == "style" and self._style_open is not None:
            sp, inner_start = self._style_open
            self.style_inner[sp] = (inner_start, off)
            self._style_open = None
        self._stack.pop()
        if self._path:
            self._path.pop()


def apply_css_plan(html: str, plan: list[dict[str, Any]]) -> str:
    """Apply a CSS plan to serialized HTML via byte-preserving span splicing."""
    if not plan:
        return html
    loc = _Locator(_line_starts(html))
    loc.feed(html)
    loc.close()

    insert_map: dict[int, list[str]] = {}
    replaces: list[tuple[int, int, str]] = []
    for entry in plan:
        action = entry.get("action")
        css = _neutralize_style_close(entry.get("css", ""))
        snippet = f"<style>{css}</style>"
        if action == "insert-document":
            pos = loc.body_end if loc.body_end is not None else loc.html_end
            if pos is not None:
                insert_map.setdefault(pos, []).append(snippet)
        elif action == "insert-adopted":
            pos = loc.element_end.get(tuple(entry.get("path", [])))
            if pos is not None:
                insert_map.setdefault(pos, []).append(snippet)
        elif action == "replace-style-body":
            span = loc.style_inner.get(tuple(entry.get("path", [])))
            if span is not None:
                replaces.append((span[0], span[1], css))

    edits: list[tuple[int, int, str]] = [
        (pos, pos, "".join(parts)) for pos, parts in insert_map.items()
    ]
    edits.extend(replaces)
    edits.sort(key=lambda e: e[0], reverse=True)
    out = html
    for start, end, repl in edits:
        out = out[:start] + repl + out[end:]
    return out


EXTRACT_JS = r"""
(() => {
  const ser = (sheet) => {
    try { return Array.from(sheet.cssRules, (r) => r.cssText).join('\n'); }
    catch (e) { return null; }
  };
  const plan = [];
  for (const s of (document.adoptedStyleSheets || [])) {
    const css = ser(s);
    if (css !== null) plan.push({ action: 'insert-document', css });
  }
  function walk(container, path, shadowOffset) {
    let i = shadowOffset || 0;
    for (const el of container.children) {
      i++;
      const here = path.concat([i]);
      if (el.localName === 'style' && el.sheet) {
        const css = ser(el.sheet);
        if (css !== null) plan.push({ action: 'replace-style-body', path: here, css });
      }
      const sr = el.shadowRoot;  // open roots only; closed -> null
      if (sr) {
        const tplPath = here.concat([1]);
        for (const s of (sr.adoptedStyleSheets || [])) {
          const css = ser(s);
          if (css !== null) plan.push({ action: 'insert-adopted', path: tplPath, css });
        }
        walk(sr, tplPath, 0);
      }
      if (el.localName === 'template' && el.content) walk(el.content, here, 0);
      walk(el, here, sr ? 1 : 0);
    }
  }
  walk(document.documentElement, [], 0);
  return JSON.stringify(plan);
})()
"""


async def fold_cssom(tab: uc.Tab, frame_id: uc.cdp.page.FrameId, html: str) -> str:
    """Best-effort: extract the frame's CSS plan in an isolated world and splice
    it into `html`. Returns `html` unchanged on any failure."""
    try:
        raw = await evaluate_isolated(tab, frame_id, EXTRACT_JS)
        plan = json.loads(raw) if raw else []
    except Exception:
        return html
    try:
        return apply_css_plan(html, plan)
    except Exception:
        return html
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cssom.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/cssom.py tests/test_cssom.py
git commit -m "feat: CSSOM plan splicer and isolated-world extraction"
```

---

## Task 6: Integrate `fold_cssom` into per-frame rendered capture

**Files:**
- Modify: `src/passe_partout/rendered.py` (the `walk` body in `capture_rendered_payload`)
- Create: `tests/fixtures/cssom_page.html`
- Test: `tests/test_warc_endpoint.py`

- [ ] **Step 1: Add the fixture**

```html
<!-- tests/fixtures/cssom_page.html -->
<!DOCTYPE html>
<html>
  <head>
    <title>cssom</title>
    <style id="mut">.base{color:rgb(1,2,3)}</style>
  </head>
  <body>
    <h1 class="base">Hi</h1>
    <div id="host"></div>
    <div id="lighthost">light</div>
    <script>
      document.getElementById('mut').sheet.insertRule('.injected{color:rgb(4,5,6)}', 1);

      const ds = new CSSStyleSheet();
      ds.replaceSync('.docadopt{font-size:11px}');
      document.adoptedStyleSheets = [ds];

      const sr = document.getElementById('host').attachShadow({ mode: 'open' });
      sr.innerHTML = '<style>.shinline{margin:1px}</style><span>shadow</span>';
      const ss = new CSSStyleSheet();
      ss.replaceSync('.shadopt{padding:3px}');
      sr.adoptedStyleSheets = [ss];

      // host with BOTH a shadow root AND a light <style> child (off-by-one guard)
      const lh = document.getElementById('lighthost');
      lh.attachShadow({ mode: 'open' }).innerHTML = '<style>.lr{border:0}</style>';
      const ls = document.createElement('style');
      lh.appendChild(ls);
      ls.sheet.insertRule('.lightinjected{color:rgb(7,8,9)}', 0);
    </script>
  </body>
</html>
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_warc_endpoint.py`:

```python
@pytest.mark.asyncio
async def test_warc_rendered_folds_cssom(client, fixture_server):
    import base64
    import json

    tab_id = await _open(client, f"{fixture_server}/cssom_page.html", mode="copy")
    try:
        await asyncio.sleep(0.6)
        resp = await client.get(f"/tabs/{tab_id}/warc?rendered=1")
        assert resp.status_code == 200, resp.text
        conv = next(
            r.content_stream().read()
            for r in ArchiveIterator(io.BytesIO(resp.content), no_record_parse=True)
            if r.rec_type == "conversion"
        )
        pages = json.loads(conv)["log"]["pages"]
        top = next(p for p in pages if p["_passepartout_parentFrameId"] is None)
        dom = base64.b64decode(top["renderedContent"]["text"]).decode("utf-8")

        # CSSOM-mutated <style> rule folded in
        assert ".injected" in dom
        # document-level adopted sheet inserted before </body>
        assert ".docadopt" in dom
        assert dom.index(".docadopt") < dom.index("</body>")
        # shadow-scoped adopted + inline styles land inside the shadow template
        tpl = dom.index("shadowrootmode")
        tpl_end = dom.index("</template>", tpl)
        assert tpl < dom.index(".shadopt") < tpl_end
        assert tpl < dom.index(".shinline") < tpl_end
        # off-by-one guard: light <style> rule present and NOT inside that template
        assert ".lightinjected" in dom
        assert dom.index(".lightinjected") > dom.index("</template>")
    finally:
        await _close(client, tab_id)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest "tests/test_warc_endpoint.py::test_warc_rendered_folds_cssom" -v`
Expected: FAIL — `.injected` / `.docadopt` / `.shadopt` absent (folding not wired in).

- [ ] **Step 4: Wire `fold_cssom` into the walk**

4a. Add the import in `rendered.py` near the isolated import:

```python
from passe_partout.cssom import fold_cssom
```

4b. In `capture_rendered_payload`'s `walk`, fold each frame's HTML after it is captured. Replace the block that currently reads (`rendered.py:186-201`, the `if parent_frame_id is None:` / `else:` that sets `dom_html`) so that immediately **after** `dom_html` is determined for both branches, it is folded. Concretely, after the `else` branch computes `dom_html` and before building `entry`, insert:

```python
        if dom_html is not None:
            dom_html = await fold_cssom(tab, frame_id, dom_html)
```

`frame_id` here is the string used elsewhere in `walk`; convert it for the CDP call:

```python
            dom_html = await fold_cssom(tab, uc.cdp.page.FrameId(frame_id), dom_html)
```

(Place this single folding call once, covering both the top-frame and sub-frame `dom_html`, right before the `entry: dict[str, Any] = {...}` construction at `rendered.py:204`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest "tests/test_warc_endpoint.py::test_warc_rendered_folds_cssom" tests/test_warc_endpoint.py -k rendered -v`
Expected: PASS (CSSOM folding test + all existing rendered tests).

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/rendered.py tests/fixtures/cssom_page.html tests/test_warc_endpoint.py
git commit -m "feat: fold CSSOM into per-frame rendered HTML"
```

---

## Task 7: Full suite, lint, format, docs

**Files:**
- Modify: `CLAUDE.md` (architecture note)

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest`
Expected: PASS (no `-m smoke` selected by default).

- [ ] **Step 2: Lint and format**

Run: `uv run ruff check --fix . && uv run ruff format .`
Expected: clean.

- [ ] **Step 3: Update CLAUDE.md architecture note**

In `CLAUDE.md`, extend the `ResourceRecorder`/WARC paragraph (the sentence describing `?rendered=1`) to note the new behavior. Add, after the existing `?rendered=1` description:

> `?rendered=1` now also (a) serializes shadow DOM (`DOM.getOuterHTML(includeShadowDOM=true)`) and (b) folds CSSOM-only state into each frame's `renderedContent` via `cssom.py`: adopted/constructed stylesheets and CSSOM-mutated `<style>` rules are spliced into the serialized HTML (byte-preserving; adopted sheets inserted last in their scope to match the cascade). Closed shadow roots' adopted/CSSOM state can't be reached and is omitted. passe-partout's own capture JS (owner-selector, scroll, title/readyState) runs in an isolated world (`isolated.py`); `POST /tabs/{id}/eval` takes a `world` field (`main` default | `isolated`).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note shadow DOM, CSSOM folding, isolated worlds in CLAUDE.md"
```

---

## Self-Review notes (for the implementer)

- **`browser_pool.create_context` / `close_context`:** confirmed present at `browser_pool.py:79` and `:96`. `create_context(url)` returns an `nodriver` `Tab`; `close_context(tab)` tears it down. Task 1's tests use these directly.
- **`get_frame_owner` tuple shape:** unchanged from the existing code (`rendered.py:198-200`); Task 4 keeps that handling.
- **OOPIF / cross-origin sub-frames:** `createIsolatedWorld` on the page target may fail for out-of-process frames; `fold_cssom` and `_owner_selector` are best-effort and degrade to unfolded HTML / no selector, matching existing behavior.
- **Path convention parity:** the JS root call `walk(document.documentElement, [], 0)` and `_Locator` both treat the root element as the empty path and count only element children — verified against the brainstorming probe (`body=[2]`, shadow template at `[2,2,1]`).
