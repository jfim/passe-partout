"""Capture a CDP DOM snapshot for WARC export.

Calls DOMSnapshot.captureSnapshot and returns the raw CDP result shape
(`{"documents": [...], "strings": [...]}`) unmodified. The caller embeds it as
a WARC conversion record. Capture is best-effort: any CDP failure returns None
and the caller emits no snapshot record (silent degrade, matching the rendered
capture).
"""

from __future__ import annotations

from typing import Any

import nodriver as uc

# Passe-partout-namespaced profile identifier — there is no IIPC standard for
# DOMSnapshot. It only needs to be a stable, unique identifier; it does not
# need to resolve.
DOM_SNAPSHOT_PROFILE = "urn:passe-partout:warc:dom-snapshot:1.0"


async def capture_dom_snapshot(
    tab: uc.Tab,
    computed_styles: list[str],
    *,
    include_dom_rects: bool = False,
    include_paint_order: bool = False,
) -> dict[str, Any] | None:
    """Return the raw CDP DOM snapshot as a dict, or None on any CDP failure.

    `computed_styles` is passed verbatim to CDP as the `computedStyles` array;
    an empty list yields a structure-only snapshot (Chrome accepts it).

    `include_dom_rects` adds per-node `offsetRects`/`scrollRects`/`clientRects`
    to the layout tree; `include_paint_order` adds `paintOrders`. The absolute
    bounding box (`bounds`) is always present regardless of these flags.
    """
    try:
        documents, strings = await tab.send(
            uc.cdp.dom_snapshot.capture_snapshot(
                computed_styles=computed_styles,
                include_dom_rects=include_dom_rects,
                include_paint_order=include_paint_order,
            )
        )
    except Exception:
        return None
    return {
        "documents": [d.to_json() for d in documents],
        "strings": strings,
    }
