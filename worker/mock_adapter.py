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

    payload = run.get("e_payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    question = str(payload.get("question") or "").strip()
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else None

    if question and not decision:
        # 演示「批奏折」：事件里带 question 时，mock 就停下来等人决定。
        report = {
            "status": "blocked",
            "title": "需要你拍板",
            "summary": question,
            "findings": [
                {
                    "what": "这条派工需要人的决定才能继续",
                    "why_it_matters": "属于「只有你能定」的那一类，不应该由 Agent 自己拍板",
                    "where": "run #%s" % run_id,
                }
            ],
            "needs_human": True,
            "human_question": question,
            "options": payload.get("options") or ["approve", "reject"],
        }
        conclude_status = "need_decision"
    elif decision:
        report = {
            "status": "done",
            "title": "已按你的决定继续",
            "summary": "收到决定：%s；按它继续执行（mock）。" % decision.get("choice", ""),
            "findings": [
                {
                    "what": "已带着人的决定继续干活",
                    "why_it_matters": "证明「决策 → 新事件 → 继续执行」这一环是通的",
                    "where": "run #%s / prior_run=%s" % (run_id, payload.get("prior_run")),
                }
            ],
            "needs_human": False,
            "human_question": "",
        }
        conclude_status = "completed"
    else:
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
        conclude_status = "completed"

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
        "status": conclude_status,
        "summary": report["summary"],
        "report": report,
        "artifacts": artifacts,
        "session_ref": "mock:run-%s" % run_id,
    }
