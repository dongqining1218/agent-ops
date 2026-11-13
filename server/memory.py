"""决策记忆库：`定了的事` / `定了不做的事`。

只有一个作用：同类问题不问第二遍。
A 方案原话：「我们的仓库里，有一份文件就叫『卡兹克定的事』，只记我亲口做的决定，带着原话和日期。」
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

APPROVE_WORDS = {"approve", "yes", "ok", "同意", "批准", "准", "可以"}
REJECT_WORDS = {"reject", "no", "拒绝", "不做", "不同意", "否"}


def memory_files(memory_dir: Path) -> Tuple[Path, Path]:
    return memory_dir / "定了的事.md", memory_dir / "定了不做的事.md"


def _is_rejected(choice: str) -> bool:
    return choice.strip().lower() in REJECT_WORDS


def append_decision(
    memory_dir: Path,
    *,
    question: str,
    choice: str,
    note: str = "",
    run_id: int = 0,
    source: str = "",
) -> str:
    """把一次人的决定追加进记忆库，返回写入的文件路径。"""
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    done, rejected = memory_files(memory_dir)
    rejected_case = _is_rejected(choice)
    target = rejected if rejected_case else done

    if not target.exists():
        header = (
            "# 定了不做的事（Agent 遇到同类问题直接回绝，不要再问）\n\n"
            if rejected_case
            else "# 定了的事（Agent 遇到同类问题直接照做，不要再问）\n\n"
        )
        target.write_text(header, encoding="utf-8")

    today = datetime.now().strftime("%Y-%m-%d")
    line = "- %s · run#%s · %s · 问：%s → 定：%s" % (
        today,
        run_id,
        source or "-",
        " ".join(question.split()) or "（未记录问题）",
        choice,
    )
    if note:
        line += "（备注：%s）" % " ".join(note.split())
    line += "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return str(target)


def read_memory(memory_dir: Path, limit: int = 40) -> Dict[str, List[str]]:
    """读回最近的记忆条目（只取条目行，跳过标题）。"""
    memory_dir = Path(memory_dir)
    result: Dict[str, List[str]] = {"decisions": [], "rejected": []}
    for key, path in (
        ("decisions", memory_files(memory_dir)[0]),
        ("rejected", memory_files(memory_dir)[1]),
    ):
        if not path.exists():
            continue
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        items = [ln for ln in lines if ln.startswith("- ")]
        result[key] = items[-limit:]
    return result


def render_memory(memory_dir: Path, limit: int = 40) -> str:
    """渲染成可以塞进 Agent 提示词的一段文本；没有记忆返回空串。"""
    memory = read_memory(memory_dir, limit=limit)
    blocks: List[str] = []
    if memory["decisions"]:
        blocks.append("### 我已经定过的事（遇到同类问题直接照做，不要再问）")
        blocks.extend(memory["decisions"])
    if memory["rejected"]:
        blocks.append("### 我已经拒绝过的事（遇到同类问题直接回绝，不要再问）")
        blocks.extend(memory["rejected"])
    return "\n".join(blocks)
