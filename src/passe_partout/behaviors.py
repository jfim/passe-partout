"""Replayable input behaviors (wheel-scroll traces) and a catalog over them.

A Behavior is a parameterless sequence of relative wheel steps
(delta_x, delta_y, dt_ms) that the play endpoint replays via CDP. One built-in
ships (honestly-synthetic, evenly-spaced scroll-down); the rest are loaded from
BEHAVIOR_TRACE_DIR so realistic traces stay operator-private and are never
shipped (avoids a shared cross-client fingerprint).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# (delta_x, delta_y, dt_ms): relative wheel deltas and the pause before the next step.
WheelStep = tuple[float, float, float]

VALID_KINDS = frozenset({"scroll-down", "scroll-up", "jitter", "wheel-scrub"})


@dataclass(frozen=True)
class Behavior:
    name: str
    kind: str  # one of VALID_KINDS
    source: str  # "builtin" | "recorded"
    steps: tuple[WheelStep, ...]


# Honestly-synthetic default: evenly-spaced downward wheel, enough to trip
# IntersectionObserver lazy-load. Makes no claim to be human.
BUILTIN_SCROLL_DOWN = Behavior(
    name="scroll-down",
    kind="scroll-down",
    source="builtin",
    steps=tuple((0.0, 120.0, 16.0) for _ in range(40)),
)


class BehaviorCatalog:
    def __init__(self, behaviors: dict[str, Behavior]) -> None:
        self._behaviors = behaviors

    @classmethod
    def load(cls, trace_dir: str | None) -> BehaviorCatalog:
        items: dict[str, Behavior] = {BUILTIN_SCROLL_DOWN.name: BUILTIN_SCROLL_DOWN}
        if trace_dir:
            for entry in sorted(os.listdir(trace_dir)):
                if not entry.endswith(".json"):
                    continue
                name = entry[:-5]
                with open(os.path.join(trace_dir, entry)) as f:
                    data = json.load(f)
                kind = data.get("kind", "scroll-down")
                if kind not in VALID_KINDS:
                    raise ValueError(f"behavior trace {entry!r} has invalid kind {kind!r}")
                steps = tuple((float(dx), float(dy), float(dt)) for dx, dy, dt in data["steps"])
                items[name] = Behavior(name=name, kind=kind, source="recorded", steps=steps)
        return cls(items)

    def list(self) -> list[Behavior]:
        return list(self._behaviors.values())

    def get(self, name: str) -> Behavior | None:
        return self._behaviors.get(name)
