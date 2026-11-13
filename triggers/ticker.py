"""定时触发器：到点往中台发一条事件。

触发器本身不做任何业务判断，只负责「什么时候敲门」。这样加一个定时任务
= 在 triggers.json 加一条，跟加 webhook 来源一样，不改中台代码。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.config import Config, load_config  # noqa: E402

CHECK_INTERVAL_SECONDS = 5


def log(msg: str) -> None:
    print("[ticker %s] %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


def post_event(base: str, body: Dict[str, Any]) -> None:
    req = urllib.request.Request(
        base + "/events",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read() or b"{}")
            log(
                "已发事件 %s → %s%s"
                % (
                    body.get("idempotency_key"),
                    payload.get("status"),
                    "（重复，已忽略）" if payload.get("duplicate") else "",
                )
            )
    except urllib.error.HTTPError as exc:
        log("发事件失败：HTTP %s %s" % (exc.code, exc.read()[:300]))
    except urllib.error.URLError as exc:
        log("发事件失败（中台没起来？）：%s" % exc)


def due_key(trigger: Dict[str, Any], now: datetime) -> Optional[str]:
    """到点返回这次的去重键，没到点返回 None。"""
    if trigger.get("every_seconds"):
        interval = int(trigger["every_seconds"])
        if interval <= 0:
            return None
        return str(int(now.timestamp()) // interval)
    at = trigger.get("daily_at")
    if at:
        hh, mm = (int(x) for x in str(at).split(":"))
        if now.hour * 60 + now.minute >= hh * 60 + mm:
            return now.strftime("%Y-%m-%d")
        return None
    return None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Ops 定时触发器")
    parser.add_argument("--url", default=None, help="中台地址，默认取 config.json")
    parser.add_argument("--once", action="store_true", help="只检查一轮就退出")
    parser.add_argument("--force", action="store_true", help="无视时间，立刻把所有触发器发一遍")
    parser.add_argument("--config", default=None, help="config.json 路径")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    base = args.url or "http://%s:%s" % (cfg.host, cfg.port)
    state_file = cfg.data_dir / "ticker_state.json"
    state: Dict[str, str] = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except ValueError:
            state = {}

    triggers: List[Dict[str, Any]] = json.loads(
        Path(cfg.triggers_file).read_text(encoding="utf-8")
    ).get("triggers", [])
    log("启动：%d 个触发器 → %s" % (len(triggers), base))

    if args.force:
        # 调试用：无视时间与状态，把每个触发器立刻发一遍，然后退出。
        for trigger in triggers:
            name = str(trigger.get("name", ""))
            body = {
                "source": trigger.get("source", "ticker"),
                "type": trigger.get("type", ""),
                "subject": trigger.get("subject", ""),
                "summary": trigger.get("summary", name),
                "link": trigger.get("link"),
                "payload": {"trigger": name, "slot": "force"},
                "idempotency_key": "%s:%s:force-%d"
                % (trigger.get("source", "ticker"), name, int(time.time())),
            }
            post_event(base, body)
        return 0

    while True:
        now = datetime.now()
        for trigger in triggers:
            name = str(trigger.get("name", ""))
            key = due_key(trigger, now)
            if key is None or state.get(name) == key:
                continue
            body = {
                "source": trigger.get("source", "ticker"),
                "type": trigger.get("type", ""),
                "subject": trigger.get("subject", ""),
                "summary": trigger.get("summary", name),
                "link": trigger.get("link"),
                "payload": {"trigger": name, "slot": key},
                "idempotency_key": "%s:%s:%s" % (trigger.get("source", "ticker"), name, key),
            }
            post_event(base, body)
            state[name] = key
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.once:
            return 0
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
