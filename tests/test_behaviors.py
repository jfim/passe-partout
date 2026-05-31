from __future__ import annotations

import json

import pytest

from passe_partout.behaviors import BehaviorCatalog


def test_builtin_scroll_down_present():
    cat = BehaviorCatalog.load(None)
    names = {b.name for b in cat.list()}
    assert "scroll-down" in names
    b = cat.get("scroll-down")
    assert b is not None
    assert b.source == "builtin"
    assert b.kind == "scroll-down"
    assert len(b.steps) > 0
    # built-in scrolls downward (positive delta_y)
    assert all(dy > 0 for _, dy, _ in b.steps)


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
