from __future__ import annotations

import uvicorn

from passe_partout.app import build_app
from passe_partout.config import Config


def main() -> None:
    cfg = Config.from_env()
    app = build_app(cfg=cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
