#!/usr/bin/env bash
# 一键跑一遍 mock 演示：起中台 → 敲门 → 领活 → 回写 → 打印结果 → 关服务。
set -euo pipefail
cd "$(dirname "$0")/.."
export AGENT_OPS_DATA_DIR="${AGENT_OPS_DATA_DIR:-data-demo}"
export AGENT_OPS_RULES_FILE=rules.mock.json
rm -rf "$AGENT_OPS_DATA_DIR"

python3 -m server >/tmp/agent-ops-demo.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  sleep 0.25
  curl -sf -m 1 http://127.0.0.1:8765/health >/dev/null 2>&1 && break
done

echo "① 定时触发器敲门"
python3 triggers/ticker.py --force
echo "② worker 领活并执行（mock）"
python3 worker/poller.py --agent m4-codex --ttl 5 --once
echo "③ 结果"
curl -s http://127.0.0.1:8765/api/runs | python3 -c 'import sys,json;[print("run #%s  %s  attempts=%s  %s"%(r["id"],r["status"],r["attempts"],r.get("summary",""))) for r in json.load(sys.stdin)]'
echo "仪表盘：http://127.0.0.1:8765 （演示实例已关闭）"
