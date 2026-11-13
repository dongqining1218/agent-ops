#!/usr/bin/env bash
# 启动中台（前台）。默认读 config.json；可用环境变量覆盖：
#   AGENT_OPS_RULES_FILE=rules.mock.json  AGENT_OPS_DATA_DIR=data-demo
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m server
