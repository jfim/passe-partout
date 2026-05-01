from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TabRecord:
    id: int
    tab: Any
    created_at: float
    last_used_at: float
    ttl_seconds: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    nav: Any = None


class TabRegistry:
    def __init__(self) -> None:
        self._records: dict[int, TabRecord] = {}
        self._next_id: int = 1
        self._mu = asyncio.Lock()  # guards _records and _next_id

    def register(self, tab: Any, ttl_seconds: int) -> TabRecord:
        now = time.time()
        rec = TabRecord(
            id=self._next_id,
            tab=tab,
            created_at=now,
            last_used_at=now,
            ttl_seconds=ttl_seconds,
        )
        self._records[rec.id] = rec
        self._next_id += 1
        return rec

    def get(self, tab_id: int) -> TabRecord | None:
        return self._records.get(tab_id)

    def remove(self, tab_id: int) -> TabRecord | None:
        return self._records.pop(tab_id, None)

    def touch(self, tab_id: int) -> None:
        rec = self._records.get(tab_id)
        if rec is not None:
            rec.last_used_at = time.time()

    def count(self) -> int:
        return len(self._records)

    def all(self) -> list[TabRecord]:
        return list(self._records.values())

    def idle_ids(self, now: float | None = None) -> list[int]:
        now = time.time() if now is None else now
        return [
            rec.id for rec in self._records.values() if now - rec.last_used_at > rec.ttl_seconds
        ]

    @property
    def mu(self) -> asyncio.Lock:
        return self._mu
