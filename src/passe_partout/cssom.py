"""Fold CSSOM-only state into CDP's serialized HTML.

`DOM.getOuterHTML` loses CSS that has no faithful DOM representation: adopted /
constructed stylesheets (no DOM node at all) and `<style>` elements whose rules
were mutated via the CSSOM API (whose textContent goes stale). An isolated-world
DOM walk (`EXTRACT_JS`) produces a plan tying each piece of CSS to a `<style>`
element (keyed by its parent's element-index path + its ordinal among its
parent's `<style>` children) or to a container for adopted sheets; `apply_css_plan`
splices it into the serialized string by byte range, preserving every other byte
(no re-serialization).

The walk and the serialized HTML are two non-atomic reads of a live DOM, so they
can disagree if the page mutates between them. To avoid ever splicing into the
wrong element, both sides emit a structural **signature** (the ordered keys of
every `<style>` element); `apply_css_plan` splices only when the signatures match
and refuses (returns None) otherwise. `fold_cssom` retries on mismatch and, if it
still can't get a consistent pair, returns the extracted sheets as a fallback the
caller can dump into the WARC for viewer-side recovery.
"""

from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any

import nodriver as uc

from passe_partout.isolated import evaluate_isolated

# HTML void elements never have children or an end tag.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_STYLE_CLOSE_RE = re.compile(r"</(style)", re.IGNORECASE)

# Retry policy for the extract/serialize consistency gate (Layer 3).
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (0.2, 0.4)  # sleeps between attempts; len == _MAX_ATTEMPTS - 1


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


def _norm_key(key: Any) -> tuple[tuple[int, ...], int]:
    """Normalize a JSON style key `[[parent_path], ordinal]` to a hashable tuple."""
    parent, ordinal = key
    return (tuple(parent), ordinal)


def _norm_path(path: Any) -> tuple[int, ...]:
    return tuple(path)


class _Locator(HTMLParser):
    """Walks serialized HTML tracking, for each element, its element-index path
    and the byte offsets needed to splice. `<style>` elements are additionally
    keyed by `(parent_path, style_ordinal)` — the ordinal among their parent's
    `<style>` children — which is immune to churn of non-`<style>` siblings.
    Element-index paths are relative to the root element (the root itself has the
    empty path); only element children are counted."""

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=False)
        self._ls = line_starts
        # stack entries: [tag, element_child_count, style_child_count]
        self._stack: list[list[Any]] = []
        self._path: list[int] = []
        self.element_end: dict[tuple[int, ...], int] = {}
        self.style_span: dict[tuple[tuple[int, ...], int], tuple[int, int]] = {}
        self.signature: list[tuple[tuple[int, ...], int]] = []
        self.body_end: int | None = None
        self.html_end: int | None = None
        self._style_open: tuple[tuple[tuple[int, ...], int], int] | None = None

    def _off(self) -> int:
        line, col = self.getpos()
        return self._ls[line - 1] + col

    def _enter(self) -> tuple[int, ...]:
        if self._stack:
            self._stack[-1][1] += 1
            self._path.append(self._stack[-1][1])
        return tuple(self._path)

    def _style_key(self) -> tuple[tuple[int, ...], int]:
        parent_path = tuple(self._path[:-1])
        if self._stack:
            self._stack[-1][2] += 1
            ordinal = self._stack[-1][2]
        else:
            ordinal = 1
        return (parent_path, ordinal)

    def handle_starttag(self, tag, attrs):
        self._enter()
        if tag == "style":
            key = self._style_key()
            self.signature.append(key)
            inner_start = self._off() + len(self.get_starttag_text() or "")
            self._style_open = (key, inner_start)
        if tag in _VOID:
            if self._path:
                self._path.pop()
        else:
            self._stack.append([tag, 0, 0])

    def handle_startendtag(self, tag, attrs):
        self._enter()
        if tag == "style":
            key = self._style_key()
            self.signature.append(key)
            pos = self._off() + len(self.get_starttag_text() or "")
            self.style_span[key] = (pos, pos)
        if self._path:
            self._path.pop()

    def handle_endtag(self, tag):
        # CDP output is well-formed, so end tags always match the open element;
        # this guard is purely defensive against unexpected/malformed input.
        if not self._stack or self._stack[-1][0] != tag:
            return
        path = tuple(self._path)
        off = self._off()
        self.element_end[path] = off
        if tag == "body":
            self.body_end = off
        elif tag == "html":
            self.html_end = off
        if tag == "style" and self._style_open is not None:
            key, inner_start = self._style_open
            self.style_span[key] = (inner_start, off)
            self._style_open = None
        self._stack.pop()
        if self._path:
            self._path.pop()


def apply_css_plan(
    html: str,
    plan: list[dict[str, Any]],
    signature: list[Any] | None = None,
) -> str | None:
    """Apply a CSS plan to serialized HTML via byte-preserving span splicing.

    When `signature` (the ordered `<style>` keys from the extraction walk) is
    provided, splicing happens **only if** the walk's signature matches the one
    re-derived from `html`; on mismatch this returns `None` (the DOM moved between
    the two reads — never guess). When `signature` is `None`, the gate is skipped
    (used by unit tests exercising splice mechanics directly).

    Returns the spliced HTML, or `None` if the plan cannot be applied consistently.
    """
    if not plan and not signature:
        return html
    loc = _Locator(_line_starts(html))
    loc.feed(html)
    loc.close()

    if signature is not None:
        want = [_norm_key(k) for k in signature]
        if loc.signature != want:
            return None

    insert_map: dict[int, list[str]] = {}
    replaces: list[tuple[int, int, str]] = []
    for entry in plan:
        action = entry.get("action")
        css = _neutralize_style_close(entry.get("css", ""))
        snippet = f"<style>{css}</style>"
        if action == "insert-document":
            pos = loc.body_end if loc.body_end is not None else loc.html_end
            if pos is None:
                return None
            insert_map.setdefault(pos, []).append(snippet)
        elif action == "insert-adopted":
            pos = loc.element_end.get(_norm_path(entry.get("path", [])))
            if pos is None:
                return None
            insert_map.setdefault(pos, []).append(snippet)
        elif action == "replace-style-body":
            span = loc.style_span.get(_norm_key(entry.get("key")))
            if span is None:
                return None
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


# Isolated-world walk. Emits {plan, signature}:
#   plan       — splice/insert actions, each carrying scope/order/host metadata so
#                a failed splice can still be dumped for viewer-side recovery.
#   signature  — ordered keys of EVERY <style> element (sheet-bearing or not), used
#                as the consistency gate against the serialized HTML.
EXTRACT_JS = r"""
(() => {
  const ser = (sheet) => {
    try { return Array.from(sheet.cssRules, (r) => r.cssText).join('\n'); }
    catch (e) { return null; }
  };
  const selectorOf = (el) => {
    if (!el || el.nodeType !== 1) return null;
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      const tag = el.tagName.toLowerCase();
      const parent = el.parentNode;
      let part = tag;
      if (parent) {
        const sib = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
        if (sib.length > 1) part = `${tag}:nth-of-type(${sib.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.length ? `html > ${parts.join(' > ')}` : 'html';
  };
  const plan = [];
  const signature = [];
  function walk(container, path, shadowOffset, scope, hostSel, ord) {
    let i = shadowOffset || 0;
    let styleOrd = 0;
    for (const el of container.children) {
      i++;
      const here = path.concat([i]);
      if (el.localName === 'style') {
        styleOrd++;
        const key = [path, styleOrd];
        signature.push(key);
        if (el.sheet) {
          const css = ser(el.sheet);
          if (css !== null) {
            plan.push({
              action: 'replace-style-body', key, css,
              scope, shadowHostSelector: hostSel, order: ord.n++,
            });
          }
        }
      }
      const sr = el.shadowRoot;  // open roots only; closed -> null
      if (sr) {
        const tplPath = here.concat([1]);
        const hs = selectorOf(el);
        const sOrd = { n: 0 };
        // Walk the shadow tree first (its <style>s), then the root's adopted
        // sheets, so adopted sheets sort after author styles within the scope.
        walk(sr, tplPath, 0, 'shadow', hs, sOrd);
        for (const s of (sr.adoptedStyleSheets || [])) {
          const css = ser(s);
          if (css !== null) {
            plan.push({
              action: 'insert-adopted', path: tplPath, css,
              scope: 'shadow', shadowHostSelector: hs, order: sOrd.n++,
            });
          }
        }
      }
      if (el.localName === 'template' && el.content) walk(el.content, here, 0, scope, hostSel, ord);
      walk(el, here, sr ? 1 : 0, scope, hostSel, ord);
    }
  }
  const docOrd = { n: 0 };
  walk(document.documentElement, [], 0, 'document', null, docOrd);
  for (const s of (document.adoptedStyleSheets || [])) {
    const css = ser(s);
    if (css !== null) {
      plan.push({
        action: 'insert-document', css,
        scope: 'document', shadowHostSelector: null, order: docOrd.n++,
      });
    }
  }
  return JSON.stringify({ plan, signature });
})()
"""


def _fallback_sheets(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a plan into the scope-tagged sheet dump used when the splice can't
    be applied. Only scope/order/css matter for viewer-side end-of-scope reinjection
    (the exact owner `<style>` is irrelevant for class-scoped CSS-in-JS)."""
    sheets = []
    for entry in plan:
        sheets.append(
            {
                "scope": entry.get("scope", "document"),
                "shadowHostSelector": entry.get("shadowHostSelector"),
                "order": entry.get("order", 0),
                "css": entry.get("css", ""),
            }
        )
    return sheets


async def fold_cssom(
    tab: uc.Tab,
    frame_id: uc.cdp.page.FrameId,
    serialize: Any,
    max_attempts: int | None = None,
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Capture the frame's serialized HTML and fold CSSOM-only state into it.

    `serialize` is an awaitable callable returning the frame's current
    `getOuterHTML` (re-invoked each attempt so the HTML and the extraction walk
    are an adjacent, consistent pair). `max_attempts` caps the splice attempts
    (default `_MAX_ATTEMPTS`); **`0` skips splicing and forces the fallback path**
    after one serialize+extract — a deterministic way to exercise the dump without
    racing live DOM mutations. Returns `(html, fallback)`:

    - `(spliced_html, None)` on success (or when there is nothing to fold).
    - `(html, sheets)` when, after retries (or with `max_attempts=0`), the walk and
      the serialized HTML stay inconsistent: `html` is the last serialization (left
      un-spliced, never misrouted) and `sheets` is the scope-tagged dump.
    - `(None, None)` if the frame's HTML cannot be serialized at all.
    """
    if max_attempts is None:
        max_attempts = _MAX_ATTEMPTS
    force_fallback = max_attempts <= 0
    attempts = 1 if force_fallback else max_attempts

    last_html: str | None = None
    last_plan: list[dict[str, Any]] = []
    for attempt in range(attempts):
        try:
            html = await serialize()
        except Exception:
            html = None
        if html is None:
            return (last_html, None) if last_html is not None else (None, None)
        last_html = html
        try:
            raw = await evaluate_isolated(tab, frame_id, EXTRACT_JS)
            data = json.loads(raw) if raw else {"plan": [], "signature": []}
        except Exception:
            # Extraction failed entirely — emit the raw HTML, nothing to fold.
            return html, None
        plan = data.get("plan", [])
        signature = data.get("signature", [])
        last_plan = plan
        if force_fallback:
            break
        try:
            spliced = apply_css_plan(html, plan, signature)
        except Exception:
            spliced = None
        if spliced is not None:
            return spliced, None
        if attempt + 1 < attempts:
            await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])

    # Retries exhausted (or forced) and still inconsistent: keep the un-spliced
    # HTML and dump whatever sheets we extracted so the viewer can recover.
    fallback = _fallback_sheets(last_plan) if last_plan else None
    return last_html, fallback
