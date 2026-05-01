from __future__ import annotations

import shutil
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


class DownloadCoordinator:
    """Owns per-tab download directories and (later) CDP plumbing.

    Each tab gets a directory under <root_dir>/passe-partout/tab-<tab_id>/
    where Chromium writes downloaded files (named by their CDP guid).
    """

    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir) / "passe-partout"

    def tab_dir(self, tab_id: int) -> Path:
        return self.root / f"tab-{tab_id}"

    def ensure_tab_dir(self, tab_id: int) -> Path:
        d = self.tab_dir(tab_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup_tab_dir(self, tab_id: int) -> None:
        d = self.tab_dir(tab_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
