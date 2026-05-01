from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DownloadState = Literal["in_progress", "completed", "canceled"]


@dataclass
class DownloadRecord:
    id: str  # CDP guid
    url: str
    filename: str
    path: Path
    started_at: float
    state: DownloadState = "in_progress"
    bytes_received: int = 0
    size_bytes: int = -1
    completed_at: float | None = None
