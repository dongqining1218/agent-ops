"""codex 适配器：把一个派工变成一个真的 codex 进程，并把结论收成结构化 Report。

关键取舍：
- 用 `codex exec --output-schema` 让 Agent 直接吐结构化结论，中台不用解析散文。
- 用 `-s <sandbox>` 限制它能干什么：P0 的巡检任务一律 read-only。
- 用 `-o last_message.txt` 取最后一条消息，避免从 stdout 里抠。
- 超时、非零退出、schema 不合，统统当失败回写（走重试/上报），不假装成功。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.config import Config
from server.memory import render_memory

MAX_SUMMARY = 800


def _read_skill(cfg: Config, skill: Optional[str]) -> str:
    if not skill:
        return ""
    path = cfg.root / "skills" / skill / "SKILL.md"
    if not path.exists():
        return "（skill 文件缺失：%s）" % path
    return path.read_text(encoding="utf-8")


def build_prompt(run: Dict[str, Any], cfg: Config) -> str:
    skill_text = _read_skill(cfg, run.get("skill"))
    lines: List[str] = [
        "你是「Agent 自动化中台」叫起来的执行者。你在无人值守模式下工作：没有人会回答你的提问，",
        "也不能指望有人在场。请自己判断、自己完成，最后按约定格式交结论。",
        "",
        "## 任务",
        str(run.get("ask") or "完成这件事。"),
        "",
        "## 事件",
        "来源：%s" % run.get("e_source", ""),
        "类型：%s" % run.get("e_type", ""),
        "摘要：%s" % (run.get("e_summary") or ""),
        "回链：%s" % (run.get("e_link") or "（无）"),
        "",
        "## 工作目录",
        str(run.get("cwd", "")),
        "",
    ]
    payload = run.get("e_payload")
    if payload:
        lines += [
            "## 事件内容",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "",
        ]
    if skill_text:
        lines += [
            "## 作业指导书（skill: %s）" % run.get("skill"),
            skill_text,
            "",
        ]
    memory_text = render_memory(cfg.memory_dir)
    if memory_text:
        lines += [
            "## 决策记忆（人已经定过的事，不要重复问）",
            memory_text,
            "",
        ]
    lines += [
        "## 输出要求",
        "- 最后一条消息必须是符合给定 JSON schema 的 JSON 对象。",
        "- 不要包 ``` 代码块，不要在 JSON 前后写任何解释。",
        "- 只写结论，不写过程；没有值得汇报的事就 status=\"nothing\"。",
        "- 沙箱是只读的，不要试图修改文件、提交、联网或安装东西。",
    ]
    return "\n".join(lines)


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _tail(text: str, n: int = 1200) -> str:
    text = (text or "").strip()
    return text[-n:]


def execute(run: Dict[str, Any], cfg: Config, dry_run: bool = False) -> Dict[str, Any]:
    """执行一次派工，返回可直接 POST /conclude 的 body。"""
    run_id = run["id"]
    artifact_dir = cfg.runs_dir / ("run-%s" % run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(run, cfg)
    prompt_file = _write(artifact_dir / "prompt.md", prompt)
    artifacts: List[str] = [prompt_file]

    if dry_run:
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "dry-run：未调用 codex",
            "summary": "dry-run",
            "artifacts": artifacts,
        }

    codex = shutil.which("codex")
    if not codex:
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "找不到 codex CLI（PATH 里没有）",
            "summary": "找不到 codex CLI",
            "artifacts": artifacts,
        }

    last_message = artifact_dir / "last_message.txt"
    cmd: List[str] = [
        codex,
        "exec",
        "-C",
        str(run.get("cwd") or cfg.root),
        "-s",
        str(run.get("sandbox") or "read-only"),
        "--skip-git-repo-check",
        "--ephemeral",
        "-o",
        str(last_message),
        "--output-schema",
        str(cfg.report_schema_file),
        "--color",
        "never",
    ]
    if run.get("model"):
        cmd += ["-m", str(run["model"])]
    cmd.append(prompt)

    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(run.get("cwd") or cfg.root),
            capture_output=True,
            text=True,
            timeout=cfg.agent_timeout_seconds,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "超时：%d 秒内没跑完" % cfg.agent_timeout_seconds,
            "summary": "执行超时",
            "artifacts": artifacts,
        }

    artifacts.append(_write(artifact_dir / "stdout.log", proc.stdout or ""))
    artifacts.append(_write(artifact_dir / "stderr.log", proc.stderr or ""))

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        hint = ""
        if "sign in again" in stderr or "refresh token" in stderr:
            hint = "（codex 登录失效：请先在终端运行 `codex login`）"
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "codex 退出码 %d%s：%s" % (proc.returncode, hint, _tail(stderr)),
            "summary": _tail(proc.stderr, 300),
            "artifacts": artifacts,
            "session_ref": "codex:run-%s" % run_id,
        }

    raw = last_message.read_text(encoding="utf-8") if last_message.exists() else ""
    if not raw.strip():
        raw = proc.stdout or ""

    report, parse_error = _parse_report(raw)
    if parse_error:
        artifacts.append(_write(artifact_dir / "report_raw.txt", raw))
        return {
            "run_id": run_id,
            "status": "completed",
            "summary": _tail(raw, MAX_SUMMARY),
            "report": {"parse_error": parse_error, "raw": _tail(raw, 4000)},
            "artifacts": artifacts,
            "session_ref": "codex:run-%s" % run_id,
        }

    report_file = _write(
        artifact_dir / "report.json", json.dumps(report, ensure_ascii=False, indent=2)
    )
    artifacts.append(report_file)

    summary = str(report.get("summary") or "").strip() or str(report.get("title") or "")
    needs_human = bool(report.get("needs_human")) or report.get("status") == "blocked"
    status = "need_decision" if needs_human else "completed"
    return {
        "run_id": run_id,
        "status": status,
        "summary": _tail(summary, MAX_SUMMARY),
        "report": report,
        "artifacts": artifacts,
        "session_ref": "codex:run-%s" % run_id,
    }


def _parse_report(raw: str) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        value = json.loads(text)
    except ValueError as exc:
        return None, "JSON 解析失败：%s" % exc
    if not isinstance(value, dict):
        return None, "结论不是 JSON 对象"
    if "status" not in value:
        return None, "结论缺少 status 字段"
    return value, None
