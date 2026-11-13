"""规则文件：rules.json 的加载、校验与匹配。

加一种新事件 = 加一条规则，不改代码（这是 A 方案最值得保留的一条）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

TELL_VALUES = {"needed", "silent"}
SANDBOX_VALUES = {"read-only", "workspace-write", "danger-full-access"}
RUNTIME_VALUES = {"codex", "mock"}


@dataclass(frozen=True)
class Rule:
    name: str
    sources: List[str]
    types: List[str]
    agent: str
    cwd: str
    ask: str
    skill: Optional[str] = None
    group: str = ""
    parallel: int = 1
    tell: str = "needed"
    sandbox: str = "read-only"
    runtime: str = "codex"
    model: Optional[str] = None

    def matches(self, source: str, event_type: str) -> bool:
        if self.sources and source not in self.sources:
            return False
        if self.types and event_type not in self.types:
            return False
        return True


class RuleError(ValueError):
    pass


def _require(raw: dict, key: str, rule_name: str) -> object:
    if key not in raw:
        raise RuleError("规则 %r 缺少字段 %r" % (rule_name, key))
    return raw[key]


def parse_rules(raw: dict) -> List[Rule]:
    rules: List[Rule] = []
    seen = set()
    for item in raw.get("rules", []):
        name = str(_require(item, "name", "?"))
        if name in seen:
            raise RuleError("规则重名：%s" % name)
        seen.add(name)

        when = item.get("when") or {}
        to = item.get("to") or {}
        skill = to.get("skill")
        sandbox = str(to.get("sandbox", "read-only"))
        runtime = str(to.get("runtime", "codex"))
        tell = str(item.get("tell", "needed"))
        parallel = int(item.get("parallel", 1))

        if sandbox not in SANDBOX_VALUES:
            raise RuleError("规则 %r 的 sandbox 非法：%s" % (name, sandbox))
        if runtime not in RUNTIME_VALUES:
            raise RuleError("规则 %r 的 runtime 非法：%s" % (name, runtime))
        if tell not in TELL_VALUES:
            raise RuleError("规则 %r 的 tell 非法：%s" % (name, tell))
        if parallel < 1:
            raise RuleError("规则 %r 的 parallel 必须 >= 1" % name)
        for key in ("agent", "cwd"):
            if not to.get(key):
                raise RuleError("规则 %r 的 to 缺少 %r" % (name, key))

        rules.append(
            Rule(
                name=name,
                sources=[str(x) for x in when.get("source", [])],
                types=[str(x) for x in when.get("types", [])],
                agent=str(to["agent"]),
                cwd=str(to["cwd"]),
                ask=str(item.get("ask", "")),
                skill=str(skill) if skill else None,
                group=str(item.get("group", name)),
                parallel=parallel,
                tell=tell,
                sandbox=sandbox,
                runtime=runtime,
                model=str(to["model"]) if to.get("model") else None,
            )
        )
    return rules


def load_rules(path: Path) -> List[Rule]:
    if not path.exists():
        raise RuleError("规则文件不存在：%s" % path)
    return parse_rules(json.loads(path.read_text(encoding="utf-8")))


def match_rule(rules: List[Rule], source: str, event_type: str) -> Optional[Rule]:
    """按文件顺序取第一条匹配的规则；没有就返回 None（事件只记录，不派单）。"""
    for rule in rules:
        if rule.matches(source, event_type):
            return rule
    return None
