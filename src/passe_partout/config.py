from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    max_tabs: int = 10
    idle_timeout_seconds: int = 300
    auth_token: str | None = None
    extension_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        raw_dirs = os.environ.get("UNPACKED_EXTENSION_DIRS", "")
        ext_dirs = [p for p in raw_dirs.split(":") if p]
        for p in ext_dirs:
            if not os.path.isdir(p):
                raise ValueError(f"UNPACKED_EXTENSION_DIRS entry is not a directory: {p}")
        return cls(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            max_tabs=int(os.environ.get("MAX_TABS", "10")),
            idle_timeout_seconds=int(os.environ.get("IDLE_TIMEOUT_SECONDS", "300")),
            auth_token=os.environ.get("AUTH_TOKEN") or None,
            extension_dirs=ext_dirs,
        )
