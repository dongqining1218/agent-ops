#!/usr/bin/env bash
# 看一眼中台状态
set -euo pipefail
BASE="${1:-http://127.0.0.1:8765}"
echo "== health =="; curl -s -m 3 "$BASE/health" | python3 -m json.tool
echo "== inbox =="; curl -s -m 3 "$BASE/api/inbox" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("待决定", len(d));[print(" #%s %s"%(r["id"],r.get("summary") or "")) for r in d]'
echo "== runs (最近 10) =="; curl -s -m 3 "$BASE/api/runs?limit=10" | python3 -c 'import sys,json;[print("#%s %-10s %-9s attempts=%s %s"%(r["id"],r["rule"],r["status"],r["attempts"],(r.get("summary") or r.get("error") or "")[:60])) for r in json.load(sys.stdin)]'
