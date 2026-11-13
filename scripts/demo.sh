#!/usr/bin/env bash
# 一键演示完整闭环：定时敲门 → 派单执行 → 遇事上报 → 我拍板 → 继续执行。
# 用 mock 运行时，不花 token。默认隔离在 data-demo/，不污染真实数据。
set -euo pipefail
cd "$(dirname "$0")/.."

export AGENT_OPS_DATA_DIR="${AGENT_OPS_DATA_DIR:-data-demo}"
export AGENT_OPS_MEMORY_DIR="$AGENT_OPS_DATA_DIR/memory"
export AGENT_OPS_RULES_FILE=rules.mock.json
BASE="http://127.0.0.1:8765"
rm -rf "$AGENT_OPS_DATA_DIR"

python3 -m server >/tmp/agent-ops-demo.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  sleep 0.25
  curl -sf -m 1 "$BASE/health" >/dev/null 2>&1 && break
done

json() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "① 定时触发器敲门（repo.daily_check）"
python3 triggers/ticker.py --force >/dev/null
python3 worker/poller.py --agent m4-codex --ttl 5 --once 2>/dev/null
echo "   第一条结论：$(curl -s "$BASE/api/runs?limit=1" | json 'd[0]["status"] + "：" + d[0]["summary"][:50]')"

echo
echo "② 来了一件需要人定的事（事件里带 question）"
RUN_ID=$(curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' -d '{
  "source": "ticker", "type": "repo.daily_check", "subject": "repo:appeal-insight",
  "summary": "发现 3 个 30 天未合并的分支", "payload": {"question": "要不要删掉这 3 个陈旧分支？"}
}' | json 'd["run_id"]')
python3 worker/poller.py --agent m4-codex --ttl 5 --once 2>/dev/null
echo "   收件箱：$(curl -s "$BASE/api/inbox" | json 'str(len(d)) + " 条待决定"')"

echo
echo "③ 我去批奏折（同意）"
curl -s -X POST "$BASE/api/runs/$RUN_ID/decide" -H 'Content-Type: application/json' \
  -d '{"choice": "approve", "note": "确认是过期分支"}' \
  | json '"   决定=" + d["decision"]["choice"] + "，生成后续事件 #" + str(d["followup"]["event_id"]) + "（" + d["followup"]["status"] + "）"'
echo "   记忆写入：$AGENT_OPS_MEMORY_DIR/定了的事.md"

echo
echo "④ Agent 带着决定继续干"
python3 worker/poller.py --agent m4-codex --ttl 5 --once 2>/dev/null

echo
echo "⑤ 最终状态"
curl -s "$BASE/api/runs" | python3 -c '
import sys, json
rows = json.load(sys.stdin)
for r in reversed(rows):
    print("   run #%s  %-9s %s" % (r["id"], r["status"], (r.get("summary") or "")[:60]))
'
echo
echo "   面板：$BASE   待决定：$BASE/ui/inbox"
echo "   （演示实例已关闭；真实数据在 data/，演示数据在 data-demo/）"
