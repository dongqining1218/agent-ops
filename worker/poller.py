"""worker 主循环：长轮询领活 → 跑 Agent → 回写结论。

这就是 A 方案里的「接单程序」：机器不用暴露到公网，挂在中台上等活，
一次最长等 50 秒，有活立刻接走，没活再挂上去。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.config import Config, load_config  # noqa: E402
from worker import executor  # noqa: E402


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


def http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    timeout: float = 60.0,
) -> Tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body.decode("utf-8", "replace")
        return exc.code, parsed


def base_url(args: argparse.Namespace, cfg: Config) -> str:
    return args.url or "http://%s:%s" % (cfg.host, cfg.port)


def run_once(args: argparse.Namespace, cfg: Config) -> bool:
    """领一个活并干完。返回 False 表示这次没领到活。"""
    token = args.token or cfg.tokens.get(args.agent)
    url = base_url(args, cfg) + "/lease"
    status, run = http_json(
        "POST", url, {"agent": args.agent, "ttl": args.ttl}, token, timeout=args.ttl + 30
    )
    if status == 204:
        return False
    if status != 200 or not isinstance(run, dict):
        log("领活失败：HTTP %s %s" % (status, run))
        time.sleep(5)
        return False

    log("领到 run #%s：%s / %s" % (run.get("id"), run.get("e_source"), run.get("e_type")))
    try:
        conclusion = executor.execute(run, cfg, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要回写，不能把活卡死
        log("执行异常：%s\n%s" % (exc, traceback.format_exc()))
        conclusion = {
            "run_id": run.get("id"),
            "status": "failed",
            "error": "worker 执行异常：%s" % exc,
            "summary": str(exc)[:300],
        }

    settle_url = base_url(args, cfg) + "/conclude"
    status, result = http_json("POST", settle_url, conclusion, token, timeout=60)
    if status != 200:
        log("回写结论失败：HTTP %s %s" % (status, result))
    else:
        log(
            "已回写 run #%s → %s（重试:%s）"
            % (run.get("id"), result.get("status"), result.get("retried"))
        )
    return True


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Ops worker")
    parser.add_argument("--agent", required=True, help="执行者名字，要跟 rules.json 的 to.agent 对上")
    parser.add_argument("--url", default=None, help="中台地址，默认取 config.json")
    parser.add_argument("--token", default=None, help="agent token，默认取 config.json")
    parser.add_argument("--ttl", type=int, default=50, help="长轮询挂起秒数")
    parser.add_argument("--once", action="store_true", help="只领一次活就退出（调试用）")
    parser.add_argument("--max-runs", type=int, default=0, help="跑够多少次就退出，0 表示不限")
    parser.add_argument("--dry-run", action="store_true", help="不调 codex，直接回写失败（测链路用）")
    parser.add_argument("--config", default=None, help="config.json 路径")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    log(
        "worker 启动：agent=%s 中台=%s 数据目录=%s%s"
        % (args.agent, base_url(args, cfg), cfg.data_dir, "（dry-run）" if args.dry_run else "")
    )

    done = 0
    try:
        while True:
            worked = run_once(args, cfg)
            if worked:
                done += 1
                if args.once:
                    return 0
                if args.max_runs and done >= args.max_runs:
                    return 0
            else:
                if args.once:
                    log("这次没有活")
                    return 0
                time.sleep(1)
    except KeyboardInterrupt:
        log("收到中断，退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
