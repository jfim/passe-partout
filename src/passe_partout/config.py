from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    max_tabs: int = 10
    idle_tab_close_seconds: int = 300
    idle_chrome_shutdown_seconds: int = 300
    auth_token: str | None = None
    extension_dirs: tuple[str, ...] = ()
    headless: bool = True
    chrome_path: str | None = None
    download_dir: str = "/tmp"
    # When true, all tabs share the default Chrome profile (cookies/storage are not
    # isolated between callers). Required when --load-extension extensions are loaded
    # because Chrome doesn't enable them in incognito-style contexts. Must be opted into
    # explicitly via SHARED_PROFILE=1; not inferred from extension_dirs because the
    # cross-tab cookie sharing it implies is a meaningful posture change.
    shared_profile: bool = False

    @classmethod
    def from_env(cls) -> Config:
        raw_dirs = os.environ.get("UNPACKED_EXTENSION_DIRS", "")
        ext_dirs = tuple(p for p in raw_dirs.split(":") if p)
        for p in ext_dirs:
            if not os.path.isdir(p):
                raise ValueError(f"UNPACKED_EXTENSION_DIRS entry is not a directory: {p}")
        sp_env = os.environ.get("SHARED_PROFILE", "0")
        shared_profile = sp_env.lower() in ("1", "true", "yes")
        if ext_dirs and not shared_profile:
            raise ValueError(
                "UNPACKED_EXTENSION_DIRS is set but SHARED_PROFILE is not enabled. "
                "Chrome does not run --load-extension extensions in incognito-style "
                "contexts; set SHARED_PROFILE=1 to opt into a shared default profile."
            )
        return cls(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            max_tabs=int(os.environ.get("MAX_TABS", "10")),
            idle_tab_close_seconds=int(os.environ.get("IDLE_TAB_CLOSE_SECONDS", "300")),
            idle_chrome_shutdown_seconds=int(os.environ.get("IDLE_CHROME_SHUTDOWN_SECONDS", "300")),
            auth_token=os.environ.get("AUTH_TOKEN") or None,
            extension_dirs=ext_dirs,
            headless=os.environ.get("HEADLESS", "1").lower() not in ("0", "false", "no"),
            chrome_path=os.environ.get("CHROME_PATH") or None,
            download_dir=os.environ.get("DOWNLOAD_DIR", "/tmp"),
            shared_profile=shared_profile,
        )
