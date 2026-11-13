"""配置加载：一份 config.json，环境变量可覆盖关键项。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = PROJECT_ROOT / "data"
    rules_file: Path = PROJECT_ROOT / "rules.json"
    triggers_file: Path = PROJECT_ROOT / "triggers" / "triggers.json"
    report_schema_file: Path = PROJECT_ROOT / "schemas" / "report.schema.json"
    max_attempts: int = 3
    lease_ttl_seconds: int = 50
    lease_max_ttl_seconds: int = 120
    agent_timeout_seconds: int = 900
    retry_base_seconds: int = 15
    retry_max_seconds: int = 600
    tokens: Dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent-ops.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def load_config(path: Optional[str] = None) -> Config:
    cfg_path = Path(path or os.environ.get("AGENT_OPS_CONFIG", PROJECT_ROOT / "config.json"))
    root = cfg_path.resolve().parent
    raw: Dict[str, object] = {}
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    cfg = Config(root=root)
    cfg.host = str(raw.get("host", cfg.host))
    cfg.port = int(os.environ.get("AGENT_OPS_PORT", raw.get("port", cfg.port)))
    cfg.data_dir = _resolve(
        root, os.environ.get("AGENT_OPS_DATA_DIR", str(raw.get("data_dir", cfg.data_dir)))
    )
    cfg.rules_file = _resolve(
        root, os.environ.get("AGENT_OPS_RULES_FILE", str(raw.get("rules_file", cfg.rules_file)))
    )
    cfg.triggers_file = _resolve(root, str(raw.get("triggers_file", cfg.triggers_file)))
    cfg.report_schema_file = _resolve(
        root, str(raw.get("report_schema_file", cfg.report_schema_file))
    )
    cfg.max_attempts = int(raw.get("max_attempts", cfg.max_attempts))
    cfg.lease_ttl_seconds = int(raw.get("lease_ttl_seconds", cfg.lease_ttl_seconds))
    cfg.lease_max_ttl_seconds = int(raw.get("lease_max_ttl_seconds", cfg.lease_max_ttl_seconds))
    cfg.agent_timeout_seconds = int(raw.get("agent_timeout_seconds", cfg.agent_timeout_seconds))
    cfg.retry_base_seconds = int(raw.get("retry_base_seconds", cfg.retry_base_seconds))
    cfg.retry_max_seconds = int(raw.get("retry_max_seconds", cfg.retry_max_seconds))
    cfg.tokens = dict(raw.get("tokens") or {})  # type: ignore[arg-type]

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    return cfg
