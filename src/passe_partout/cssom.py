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
