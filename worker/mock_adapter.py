"""mock 执行器：不调模型，只证明「派工 → 执行 → 回写」这条链路是通的。

用途：联调、演示、以及在真实 CLI 不可用时做兜底（比如 codex 登录失效）。
它不做任何真实调查——这是刻意的，假的结论比没有结论更危险。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from server.config import Config


def execute(run: Dict[str, Any], cfg: Config, dry_run: bool = False) -> Dict[str, Any]:
    run_id = run["id"]
    artifact_dir = cfg.runs_dir / ("run-%s" % run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "status": "done",
        "title": "模拟执行：收到派工",
        "summary": (
            "mock 运行时收到了这条派工并走完了全链路；把 rules.json 的 to.runtime 改成 "
            "codex，同一条链路就会真的调用本地 Agent。"
        ),
        "findings": [
            {
                "what": "派工已送达 worker 并成功回写",
                "why_it_matters": "证明中台→worker→结论回写→落库这条链路是通的",
                "where": "run #%s / skill=%s" % (run_id, run.get("skill") or "（无）"),
            }
        ],
        "needs_human": False,
        "human_question": "",
    }

    artifacts: List[str] = []
    prompt_file = artifact_dir / "prompt.md"
    prompt_file.write_text(
        "「任务」\n%s\n\n「事件」\n%s / %s\n\n（mock 运行时，未调用模型）"
        % (run.get("ask", ""), run.get("e_source", ""), run.get("e_type", "")),
        encoding="utf-8",
    )
    artifacts.append(str(prompt_file))

    report_file = artifact_dir / "report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(str(report_file))

    return {
        "run_id": run_id,
        "status": "completed",
        "summary": report["summary"],
        "report": report,
        "artifacts": artifacts,
        "session_ref": "mock:run-%s" % run_id,
    }
