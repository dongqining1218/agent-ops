"""`python -m server` 启动入口。"""
from __future__ import annotations

import uvicorn

from .config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run("server.app:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
