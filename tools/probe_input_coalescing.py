#!/usr/bin/env python
"""Probe how Chrome handles CDP-injected mouse moves: whether it coalesces them
like real hardware, and whether it honors the per-event ``timestamp`` we set.

Launches its own Chromium via nodriver, loads input-cadence.html, injects an
independent ``pointermove`` listener that records, per delivered event, the
``timeStamp`` of every ``getCoalescedEvents()`` sub-sample. Then dispatches
synthetic ``Input.dispatchMouseEvent`` moves under several regimes and reads back
what the page actually saw.

Run headful (default) on a machine with a real display for a meaningful result --
compositor coalescing depends on real vsync, so a headless run mostly verifies the
plumbing.

    uv run python tools/probe_input_coalescing.py
    uv run python tools/probe_input_coalescing.py --headless
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import nodriver
from nodriver import cdp

HTML = Path(__file__).resolve().parent / "input-cadence.html"

# Per delivered pointermove, record [eventTimeStamp, arrivalNow, clientX, [sub timeStamps...]].
# arrivalNow (performance.now() inside the handler) is the REAL wall-clock arrival,
# independent of whatever timestamp Chrome stamps on the event itself.
LISTENER_JS = """
(() => {
  window.__probe = [];
  if (!window.__probeHooked) {
    window.__probeHooked = true;
    window.addEventListener('pointermove', (e) => {
      const subs = e.getCoalescedEvents
        ? e.getCoalescedEvents().map((ev) => ev.timeStamp)
        : [e.timeStamp];
      window.__probe.push([e.timeStamp, performance.now(), e.clientX, subs]);
    }, { passive: true });
  }
  return typeof PointerEvent.prototype.getCoalescedEvents === 'function';
})()
"""


def move(x: float, y: float, ts: float | None = None):
    return cdp.input_.dispatch_mouse_event(
        type_="mouseMoved",
        x=x,
        y=y,
        timestamp=cdp.input_.TimeSinceEpoch(ts) if ts is not None else None,
    )


async def read_probe(tab) -> list:
    raw = await tab.evaluate("JSON.stringify(window.__probe)", return_by_value=True)
    return json.loads(raw) if isinstance(raw, str) else raw


async def reset(tab) -> None:
    await tab.evaluate("window.__probe = []; 'ok'", return_by_value=True)


def sub_count(probe) -> int:
    return sum(len(entry[-1]) for entry in probe)


async def run_paced(tab, *, n: int, spacing_ms: float, x0=300, y0=300):
    await reset(tab)
    if spacing_ms == 0:
        moves = [move(x0 + (i % 200), y0) for i in range(n)]
        await asyncio.gather(*[tab.send(m, _is_update=True) for m in moves])
    else:
        for i in range(n):
            await tab.send(move(x0 + (i % 200), y0), _is_update=True)
            await asyncio.sleep(spacing_ms / 1000)
    await asyncio.sleep(0.25)
    return await read_probe(tab)


def summarize_paced(label: str, sent: int, probe) -> None:
    delivered = len(probe)
    total = sub_count(probe)
    per_event = total / delivered if delivered else 0.0
    coalescing = "YES" if per_event > 1.5 else "no"
    print(f"\n[{label}]  sent={sent}")
    print(f"  delivered={delivered}  sub-samples={total}  samples/event={per_event:.2f}"
          f"  coalescing? {coalescing}")


async def run_flood(tab, *, n: int, span_s: float, x0=100, y0=300):
    """Hand Chrome all n events at once, timestamped evenly across the next span_s
    seconds. Tests playback vs. dump, and timestamp-honoring vs. arrival-time."""
    await reset(tab)
    base = time.time()
    step = span_s / n
    moves = [move(x0 + (i % 500), y0, ts=base + i * step) for i in range(n)]
    t_send = time.perf_counter()
    await asyncio.gather(*[tab.send(m, _is_update=True) for m in moves])
    send_wall = time.perf_counter() - t_send
    # Wait well past span_s: if Chrome plays events back on their timestamps,
    # delivery would stretch across the full second.
    await asyncio.sleep(span_s + 1.0)
    probe = await read_probe(tab)
    return probe, send_wall


def summarize_flood(label: str, sent: int, span_s: float, probe, send_wall: float) -> None:
    delivered = len(probe)
    total = sub_count(probe)

    event_ts = [e[0] for e in probe]
    arrivals = [e[1] for e in probe]
    event_span = (max(event_ts) - min(event_ts)) if len(event_ts) > 1 else 0.0
    arrival_span = (max(arrivals) - min(arrivals)) if len(arrivals) > 1 else 0.0

    subs = sorted(s for e in probe for s in e[-1])
    sub_span = (subs[-1] - subs[0]) if len(subs) > 1 else 0.0
    gaps = [b - a for a, b in zip(subs, subs[1:], strict=False)]
    med_gap = statistics.median(gaps) if gaps else float("nan")

    print(f"\n[{label}]  sent={sent} over {span_s:.0f}s of timestamps"
          f"  (websocket write took {send_wall * 1000:.0f} ms)")
    print(f"  delivered pointermoves      : {delivered}")
    print(f"  sub-samples received        : {total}  (of {sent} sent)")
    print(f"  REAL arrival wall span      : {arrival_span:.1f} ms   <- actual delivery duration")
    print(f"  event-timeStamp span        : {event_span:.1f} ms")
    print(f"  sub-sample timeStamp span   : {sub_span:.1f} ms")
    print(f"  median sub-sample gap       : {med_gap:.3f} ms")
    print("  interpretation:")
    if arrival_span > span_s * 1000 * 0.5:
        print(f"    -> Chrome PLAYED BACK over real wall time (~{arrival_span:.0f} ms) — scheduled delivery")
    else:
        print(f"    -> Chrome delivered FAST (~{arrival_span:.0f} ms wall) — no real-time playback")
    if sub_span > span_s * 1000 * 0.5:
        print("    -> event/sub timeStamps HONOR our dispatch timestamps (spread ~1ms apart)")
    elif total:
        print("    -> timeStamps COLLAPSED toward arrival time (our timestamps ignored)")
    if total < sent * 0.8:
        print(f"    -> {sent - total} events were DROPPED/CLAMPED (future-dated rejected?)")


async def run_frame_paced(tab, *, n_per_frame: int, frames: int, frame_ms=16, x0=200, y0=300):
    """The candidate 'realistic' pattern: each real frame, fire n_per_frame events
    concurrently with 1ms-spaced timestamps, then sleep one frame. Tests whether a
    frame's worth of injected events collapses into ~1 delivered pointermove (so the
    page sees ~n_per_frame coalesced sub-samples, like a real high-Hz mouse)."""
    await reset(tab)
    base = time.time()
    k = 0
    for _ in range(frames):
        batch = []
        for _ in range(n_per_frame):
            batch.append(move(x0 + (k % 300), y0, ts=base + k * 0.001))
            k += 1
        await asyncio.gather(*[tab.send(m, _is_update=True) for m in batch])
        await asyncio.sleep(frame_ms / 1000)
    await asyncio.sleep(0.3)
    return await read_probe(tab)


def summarize_frame_paced(n_per_frame: int, frames: int, probe) -> None:
    sent = n_per_frame * frames
    delivered = len(probe)
    total = sub_count(probe)
    per_event = total / delivered if delivered else 0.0
    arrivals = [e[1] for e in probe]
    arr_span = (max(arrivals) - min(arrivals)) if len(arrivals) > 1 else 0.0
    verdict = ("collapses to ~1/frame (GOOD: looks like a high-Hz mouse)"
               if per_event >= n_per_frame * 0.6
               else "shatters into many delivered events (CDP can't reproduce the grouping)")
    print(f"\n[frame-paced  n/frame={n_per_frame}  frames={frames}]")
    print(f"  sent={sent}  received={total}  delivered={delivered}")
    print(f"  samples/event achieved : {per_event:.2f}   (ideal {n_per_frame})")
    print(f"  real arrival wall span : {arr_span:.0f} ms   (ideal ~{frames * 16} ms)")
    print(f"  -> {verdict}")


async def main(headless: bool) -> None:
    browser = await nodriver.start(headless=headless)
    try:
        tab = await browser.get(HTML.as_uri())
        await asyncio.sleep(0.5)
        has_api = await tab.evaluate(LISTENER_JS, return_by_value=True)
        print(f"getCoalescedEvents available: {has_api}   headless={headless}")

        # Context: tight concurrent burst vs. await-paced (from earlier runs).
        summarize_paced("burst (spacing=0)", 50, await run_paced(tab, n=50, spacing_ms=0))
        summarize_paced("paced (spacing=1ms)", 50, await run_paced(tab, n=50, spacing_ms=1))

        # The flood: 1000 events at once, timestamped 1ms apart across 1 second.
        probe, send_wall = await run_flood(tab, n=1000, span_s=1.0)
        summarize_flood("flood (1000 @ 1ms timestamps)", 1000, 1.0, probe, send_wall)

        # Decisive test: can a frame's worth of injected events collapse into one
        # delivered pointermove (i.e. reproduce a real high-Hz mouse's samples/event)?
        for n_per_frame in (8, 16, 32):
            probe = await run_frame_paced(tab, n_per_frame=n_per_frame, frames=30)
            summarize_frame_paced(n_per_frame, 30, probe)
    finally:
        browser.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--headless", action="store_true", help="run headless (default headful)")
    args = p.parse_args()
    asyncio.run(main(headless=args.headless))
