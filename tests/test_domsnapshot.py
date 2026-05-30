from __future__ import annotations

import pytest

from passe_partout.domsnapshot import DOM_SNAPSHOT_PROFILE, capture_dom_snapshot


class _FakeDoc:
    def __init__(self, data: dict):
        self._data = data

    def to_json(self) -> dict:
        return self._data


class _FakeTab:
    def __init__(self, result=None, raises: bool = False):
        self._result = result
        self._raises = raises

    async def send(self, _cmd):
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def test_profile_is_a_nonempty_string():
    assert isinstance(DOM_SNAPSHOT_PROFILE, str)
    assert "dom-snapshot" in DOM_SNAPSHOT_PROFILE


@pytest.mark.asyncio
async def test_capture_serializes_documents_and_strings():
    tab = _FakeTab(result=([_FakeDoc({"nodes": {"a": 1}})], ["s0", "s1"]))
    out = await capture_dom_snapshot(tab, ["display", "color"])
    assert out == {"documents": [{"nodes": {"a": 1}}], "strings": ["s0", "s1"]}


@pytest.mark.asyncio
async def test_capture_accepts_empty_computed_styles():
    tab = _FakeTab(result=([_FakeDoc({"nodes": {}})], []))
    out = await capture_dom_snapshot(tab, [])
    assert out == {"documents": [{"nodes": {}}], "strings": []}


@pytest.mark.asyncio
async def test_capture_returns_none_on_cdp_error():
    tab = _FakeTab(raises=True)
    assert await capture_dom_snapshot(tab, ["display"]) is None


@pytest.mark.asyncio
async def test_capture_passes_dom_rects_and_paint_order_flags(monkeypatch):
    import nodriver as uc

    captured: dict = {}

    def fake_capture_snapshot(**kwargs):
        captured.update(kwargs)
        return "SENTINEL_CMD"

    monkeypatch.setattr(uc.cdp.dom_snapshot, "capture_snapshot", fake_capture_snapshot)
    tab = _FakeTab(result=([_FakeDoc({"nodes": {}})], []))
    await capture_dom_snapshot(tab, ["display"], include_dom_rects=True, include_paint_order=True)
    assert captured["computed_styles"] == ["display"]
    assert captured["include_dom_rects"] is True
    assert captured["include_paint_order"] is True


@pytest.mark.asyncio
async def test_capture_flags_default_off(monkeypatch):
    import nodriver as uc

    captured: dict = {}

    def fake_capture_snapshot(**kwargs):
        captured.update(kwargs)
        return "SENTINEL_CMD"

    monkeypatch.setattr(uc.cdp.dom_snapshot, "capture_snapshot", fake_capture_snapshot)
    tab = _FakeTab(result=([], []))
    await capture_dom_snapshot(tab, [])
    assert captured["include_dom_rects"] is False
    assert captured["include_paint_order"] is False
