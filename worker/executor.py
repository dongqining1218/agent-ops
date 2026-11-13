"""执行器分发：按规则的 to.runtime 选择真的跑谁。

- codex：本机 codex CLI（真实执行）
- mock：不调模型，只验证「中台 → worker → 回写」这条链路（联调/演示/兜底）

加一个新执行器 = 新增一个模块 + 在这里加一行，不动中台。
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from server.config import Config
from worker import codex_adapter, mock_adapter

ExecuteFn = Callable[[Dict[str, Any], Config, bool], Dict[str, Any]]


def available() -> Dict[str, str]:
    return {"codex": "本机 codex CLI", "mock": "离线模拟（验证链路）"}


def execute(run: Dict[str, Any], cfg: Config, dry_run: bool = False) -> Dict[str, Any]:
    runtime = str(run.get("runtime") or "codex")
    if runtime == "mock":
        return mock_adapter.execute(run, cfg, dry_run=dry_run)
    if runtime == "codex":
        return codex_adapter.execute(run, cfg, dry_run=dry_run)
    return {
        "run_id": run.get("id"),
        "status": "failed",
        "error": "未知的 runtime：%s（可选：%s）" % (runtime, ", ".join(available())),
        "summary": "未知的 runtime",
    }
