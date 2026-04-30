import asyncio
import time

from passe_partout.tab_registry import TabRegistry


def test_register_assigns_monotonic_ids():
    reg = TabRegistry()
    a = reg.register(tab=object(), ttl_seconds=300)
    b = reg.register(tab=object(), ttl_seconds=300)
    assert b.id == a.id + 1


def test_get_returns_record():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    assert reg.get(rec.id).tab == "X"


def test_get_missing_returns_none():
    reg = TabRegistry()
    assert reg.get(999) is None


def test_remove_deletes():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    reg.remove(rec.id)
    assert reg.get(rec.id) is None


def test_touch_updates_last_used_at():
    reg = TabRegistry()
    rec = reg.register(tab="X", ttl_seconds=300)
    original = rec.last_used_at
    time.sleep(0.01)
    reg.touch(rec.id)
    assert reg.get(rec.id).last_used_at > original


def test_idle_returns_expired_ids():
    reg = TabRegistry()
    rec1 = reg.register(tab="A", ttl_seconds=0)  # already expired
    rec2 = reg.register(tab="B", ttl_seconds=300)
    expired = reg.idle_ids(now=time.time() + 1)
    assert rec1.id in expired
    assert rec2.id not in expired


def test_count():
    reg = TabRegistry()
    assert reg.count() == 0
    reg.register(tab="A", ttl_seconds=300)
    reg.register(tab="B", ttl_seconds=300)
    assert reg.count() == 2


async def test_lock_is_per_tab():
    reg = TabRegistry()
    a = reg.register(tab="A", ttl_seconds=300)
    b = reg.register(tab="B", ttl_seconds=300)
    assert a.lock is not b.lock

    async with a.lock:
        # b.lock is independent — should be acquirable without waiting
        await asyncio.wait_for(b.lock.acquire(), timeout=0.1)
        b.lock.release()
