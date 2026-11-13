#!/usr/bin/env bash
# 启动定时触发器（前台）。用法：scripts/ticker.sh [--force] [--once]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 triggers/ticker.py "$@"
