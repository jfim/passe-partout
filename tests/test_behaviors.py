from __future__ import annotations

import asyncio
import json

import pytest

from passe_partout.behaviors import (
    BUILTIN_SCROLL_DOWN,
    SCROLL_DOWN_FRACTION_RANGE,
    WHEEL_CLICK_PX,
    BehaviorCatalog,
    perturb_steps,
    replay_wheel,
    scroll_down_steps,
)


def test_builtin_scroll_down_present():
    cat = BehaviorCatalog.load(None)
    names = {b.name for b in cat.list()}
    assert "scroll-down" in names
    b = cat.get("scroll-down")
    assert b is not None
    assert b.source == "builtin"
    assert b.kind == "scroll-down"
    assert b.viewport_relative is True
    assert len(b.steps) > 0
    # built-in scrolls downward (positive delta_y)
    assert all(dy > 0 for _, dy, _ in b.steps)


def test_scroll_down_steps_covers_viewport_fraction():
    lo, hi = SCROLL_DOWN_FRACTION_RANGE
    vh = 900.0
    # Across many seeds the total scroll distance should land within the
    # 50-80% band (allowing half a wheel notch of rounding slack either way).
    for seed in range(200):
        steps = scroll_down_steps(vh, seed=seed)
        assert len(steps) >= 1
        assert all(dx == 0.0 and dy == WHEEL_CLICK_PX and dt > 0 for dx, dy, dt in steps)
        total = sum(dy for _, dy, _ in steps)
        assert lo * vh - WHEEL_CLICK_PX / 2 <= total <= hi * vh + WHEEL_CLICK_PX / 2


def test_scroll_down_steps_is_not_a_40_notch_avalanche():
    # Regression: the old builtin was a fixed 40x120px = 4800px burst.
    for seed in range(50):
        assert len(scroll_down_steps(900.0, seed=seed)) < 40


def test_scroll_down_steps_deterministic_with_seed():
    assert scroll_down_steps(1000.0, seed=3) == scroll_down_steps(1000.0, seed=3)


def test_scroll_down_steps_click_count_varies():
    counts = {len(scroll_down_steps(900.0, seed=s)) for s in range(50)}
    assert len(counts) > 1  # random click count, not a constant


def test_scroll_down_steps_scales_with_viewport():
    small = sum(dy for _, dy, _ in scroll_down_steps(400.0, seed=1))
    large = sum(dy for _, dy, _ in scroll_down_steps(1600.0, seed=1))
    assert large > small  # same fraction (same seed) over a bigger viewport scrolls farther


def test_load_recorded_trace(tmp_path):
    (tmp_path / "myscroll.json").write_text(
        json.dumps({"kind": "scroll-up", "steps": [[0, -100, 12], [0, -90, 20]]})
    )
    cat = BehaviorCatalog.load(str(tmp_path))
    b = cat.get("myscroll")
    assert b is not None
    assert b.source == "recorded"
    assert b.kind == "scroll-up"
    assert b.steps[0] == (0.0, -100.0, 12.0)


def test_unknown_behavior_returns_none():
    assert BehaviorCatalog.load(None).get("nope") is None


def test_invalid_kind_rejected(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"kind": "teleport", "steps": []}))
    with pytest.raises(ValueError):
        BehaviorCatalog.load(str(tmp_path))


def test_non_json_files_ignored(tmp_path):
    (tmp_path / "README.txt").write_text("not a trace")
    cat = BehaviorCatalog.load(str(tmp_path))
    assert {b.name for b in cat.list()} == {"scroll-down"}


def test_perturb_disabled_is_passthrough():
    steps = ((0.0, 120.0, 16.0), (0.0, 120.0, 16.0))
    assert perturb_steps(steps, enabled=False) == list(steps)


def test_perturb_is_deterministic_with_seed():
    steps = tuple((0.0, 120.0, 16.0) for _ in range(5))
    a = perturb_steps(steps, seed=7)
    b = perturb_steps(steps, seed=7)
    assert a == b
    assert any(o != s for o, s in zip(a, steps))  # actually perturbed


def test_perturb_stays_within_bounds():
    steps = ((0.0, 100.0, 10.0),)
    dx, dy, dt = perturb_steps(steps, time_warp=0.2, delta_scale=0.1, seed=1)[0]
    assert 90.0 <= dy <= 110.0
    assert 8.0 <= dt <= 12.0
    assert dt >= 0.0


async def test_replay_wheel_scrolls_page(browser_pool):
    # inline styles (no '#') so the data: URL isn't truncated at a fragment
    tall = 'data:text/html,<body style="margin:0"><div style="height:5000px">tall</div></body>'
    tab = await browser_pool.create_context(tall)
    try:
        before = await tab.evaluate("window.scrollY")
        await replay_wheel(tab, list(BUILTIN_SCROLL_DOWN.steps))
        await asyncio.sleep(0.3)  # let the compositor commit the scroll
        after = await tab.evaluate("window.scrollY")
        assert before == 0
        assert after > before
    finally:
        await browser_pool.close_context(tab)
