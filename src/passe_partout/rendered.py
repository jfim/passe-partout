"""Capture per-frame rendered DOM and a top-level screenshot for WARC export.

Produces a HAR-shape JSON payload following the IIPC rendered-targets proposal
(http://iipc.github.io/warc-specifications/specifications/warc-rendered-targets/
warc-rendered-targets-1.0/), with `_passepartout_*` extensions to carry
frame-tree metadata the proposal doesn't define.

Each frame in the tab becomes a `page` entry in the JSON, keyed by Chrome's
frameId. For non-top frames we also emit `_passepartout_ownerSelector` — a CSS
selector pointing at the `<iframe>` element in the parent's DOM — so a replayer
can reconstruct nested anonymous (about:blank, srcdoc, JS-injected) iframes
without needing the original network records.

All capture is best-effort per frame. A failure on one frame (renderer crash,
detached frame, etc.) is logged via skipping that frame; failure to capture the
top-level DOM or screenshot returns None, and the caller emits no conversion
record at all (all-or-nothing semantics for the top-level capture).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import nodriver as uc

from passe_partout import __version__
from passe_partout.isolated import call_on_node_isolated, evaluate_isolated

RENDERED_TARGETS_PROFILE = (
    "http://iipc.github.io/warc-specifications/specifications/"
    "warc-rendered-targets/warc-rendered-targets-1.0/"
)

# JS that walks up from `this` to <html> emitting an :nth-of-type-disambiguated
# CSS selector. Generated selectors are absolute from <html>, idempotent, and
# unique by construction even when iframes share tag/id/class.
_OWNER_SELECTOR_FN = """
function() {
  let el = this;
  if (!el || el.nodeType !== 1) return null;
  const parts = [];
  while (el && el.nodeType === 1 && el !== document.documentElement) {
    const tag = el.tagName.toLowerCase();
    const parent = el.parentNode;
    let part = tag;
    if (parent) {
      const siblings = Array.from(parent.children).filter(
        (c) => c.tagName === el.tagName
      );
      if (siblings.length > 1) {
        const idx = siblings.indexOf(el) + 1;
        part = `${tag}:nth-of-type(${idx})`;
      }
    }
    parts.unshift(part);
    el = parent;
  }
  return parts.length ? `html > ${parts.join(' > ')}` : 'html';
}
"""


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _capture_frame_html_by_owner(tab: uc.Tab, owner_backend_node_id: int) -> str | None:
    """Given the backend_node_id of an `<iframe>` element, return its document
    outerHTML, or None if the frame has no document (cross-origin without
    access — shouldn't happen at the CDP layer but defensive). Uses
    DOM.describeNode with pierce=True so we get content_document populated.
    """
    try:
        node = await tab.send(
            uc.cdp.dom.describe_node(
                backend_node_id=uc.cdp.dom.BackendNodeId(owner_backend_node_id),
                pierce=True,
            )
        )
    except Exception:
        return None
    content_doc = getattr(node, "content_document", None)
    if content_doc is None:
        return None
    content_backend = getattr(content_doc, "backend_node_id", None)
    if content_backend is None:
        return None
    try:
        return await tab.send(
            uc.cdp.dom.get_outer_html(backend_node_id=content_backend, include_shadow_dom=True)
        )
    except Exception:
        return None


async def _owner_selector(
    tab: uc.Tab, frame_id: uc.cdp.page.FrameId, owner_backend_node_id: int
) -> str | None:
    """Generate a CSS selector for an iframe-owning element via an isolated world.

    Returns None on any error — caller falls back to omitting the selector and
    a replayer would have to use the parentFrameId + URL heuristics.
    """
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


async def _capture_screenshot_b64(tab: uc.Tab, top_frame_id: uc.cdp.page.FrameId) -> str | None:
    """Scroll to top (best effort) then capture viewport PNG, base64-encoded."""
    try:
        await evaluate_isolated(tab, top_frame_id, "window.scrollTo(0, 0)")
    except Exception:
        # Scrolling failure isn't fatal — we still get a viewport-sized screenshot,
        # just possibly not anchored at the top of the page.
        pass
    try:
        return await tab.send(uc.cdp.page.capture_screenshot(format_="png"))
    except Exception:
        return None


def _b64(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return base64.b64encode(text).decode("ascii")


async def capture_rendered_payload(tab: uc.Tab, page_title: str = "") -> dict[str, Any] | None:
    """Return the HAR-shaped rendered-targets JSON for the tab, or None on
    top-level failure. Per-frame failures (sub-frame DOM unreachable, selector
    generation failure) degrade gracefully — that frame just gets fewer fields.
    """
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

    pages: list[dict[str, Any]] = []

    async def walk(node, parent_frame_id: str | None) -> None:
        frame = node.frame
        frame_id = str(frame.id_)
        loader_id = str(getattr(frame, "loader_id", "") or "")
        url = str(getattr(frame, "url", "") or "")

        if parent_frame_id is None:
            # Top frame — DOM already captured above.
            dom_html = top_dom
            owner_selector = None
        else:
            try:
                owner_backend = await tab.send(
                    uc.cdp.dom.get_frame_owner(frame_id=uc.cdp.page.FrameId(frame_id))
                )
            except Exception:
                # Frame detached between getFrameTree and now, etc.
                return
            # get_frame_owner returns a tuple (backend_node_id, node_id?) in nodriver;
            # the first element is the backendNodeId we want.
            backend_id = owner_backend[0] if isinstance(owner_backend, tuple) else owner_backend
            dom_html = await _capture_frame_html_by_owner(tab, int(backend_id))
            owner_selector = await _owner_selector(
                tab, uc.cdp.page.FrameId(parent_frame_id), int(backend_id)
            )

        entry: dict[str, Any] = {
            "id": f"frame_{frame_id}",
            "title": page_title if parent_frame_id is None else "",
            "startedDateTime": started_at,
            "pageTimings": {},
            "_passepartout_frameId": frame_id,
            "_passepartout_parentFrameId": parent_frame_id,
            "_passepartout_loaderId": loader_id,
            "_passepartout_url": url,
        }
        if owner_selector is not None:
            entry["_passepartout_ownerSelector"] = owner_selector
        if dom_html is not None:
            entry["renderedContent"] = {
                "text": _b64(dom_html),
                "encoding": "base64",
            }
        if parent_frame_id is None:
            entry["renderedElements"] = [
                {
                    "selector": ":root",
                    "format": "PNG",
                    "content": screenshot_b64,
                    "encoding": "base64",
                }
            ]
        pages.append(entry)

        for child in node.child_frames or []:
            await walk(child, frame_id)

    await walk(tree, None)

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "passe-partout", "version": __version__},
            "pages": pages,
        }
    }
