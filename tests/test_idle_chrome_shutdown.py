import asyncio

import pytest

from passe_partout.browser_pool import BrowserPool
from passe_partout.config import Config


class _FakeTab:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.stopped = False
        self.tabs: list[_FakeTab] = []

    async def create_context(self, url: str, new_window: bool = False) -> _FakeTab:
        t = _FakeTab()
        self.tabs.append(t)
        return t

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def patched_uc_start(monkeypatch):
    started: list[_FakeBrowser] = []

    async def _fake_start(browser_args=None, headless=True):
        b = _FakeBrowser()
        started.append(b)
        return b

    import passe_partout.browser_pool as bp

    monkeypatch.setattr(bp.uc, "start", _fake_start)
    return started


async def test_lazy_start_does_not_launch_until_first_context(patched_uc_start):
    cfg = Config(idle_chrome_shutdown_seconds=300)
    pool = BrowserPool(cfg)
    assert pool._browser is None
    assert patched_uc_start == []


async def test_idle_shutdown_after_last_context_closes(patched_uc_start):
    cfg = Config(idle_chrome_shutdown_seconds=1)
    pool = BrowserPool(cfg)

    tab = await pool.create_context("about:blank")
    assert pool._browser is not None
    browser = pool._browser

    await pool.close_context(tab)
    # Idle task should be scheduled
    assert pool._idle_task is not None

    await asyncio.sleep(1.3)
    assert browser.stopped is True
    assert pool._browser is None


async def test_new_request_cancels_idle_shutdown(patched_uc_start):
    cfg = Config(idle_chrome_shutdown_seconds=2)
    pool = BrowserPool(cfg)

    tab = await pool.create_context("about:blank")
    browser = pool._browser
    await pool.close_context(tab)
    assert pool._idle_task is not None

    await asyncio.sleep(0.3)
    # Arriving request before timeout should cancel shutdown
    tab2 = await pool.create_context("about:blank")
    assert pool._idle_task is None
    assert pool._browser is browser
    assert browser.stopped is False

    await pool.close_context(tab2)
    await pool.stop()


async def test_shutdown_disabled_when_zero(patched_uc_start):
    cfg = Config(idle_chrome_shutdown_seconds=0)
    pool = BrowserPool(cfg)

    tab = await pool.create_context("about:blank")
    await pool.close_context(tab)

    assert pool._idle_task is None
    assert pool._browser is not None
    await pool.stop()
    assert pool._browser is None


async def test_lazy_restart_after_shutdown(patched_uc_start):
    cfg = Config(idle_chrome_shutdown_seconds=1)
    pool = BrowserPool(cfg)

    tab = await pool.create_context("about:blank")
    first_browser = pool._browser
    await pool.close_context(tab)

    await asyncio.sleep(1.3)
    assert pool._browser is None

    tab2 = await pool.create_context("about:blank")
    assert pool._browser is not None
    assert pool._browser is not first_browser
    await pool.close_context(tab2)
    await pool.stop()
