#!/usr/bin/env bash
# 启动 worker（前台）。用法：scripts/worker.sh [--once] [--dry-run] [--max-runs N]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 worker/poller.py --agent "${AGENT:-m4-codex}" "$@"
