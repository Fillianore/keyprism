#!/usr/bin/env bash
# ============================================================
#  KeyPrism 一键启动 (Linux / macOS)
#  用法:  ./start.sh [音频文件] [后端端口] [前端端口]
#  示例:  ./start.sh demo.m4a
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

AUDIO="${1:-demo.m4a}"
BPORT="${2:-8800}"
FPORT="${3:-5180}"

if [[ ! -f "$AUDIO" ]]; then
  echo "[错误] 音频文件不存在: $AUDIO"
  echo "用法: ./start.sh <音频文件> [后端端口] [前端端口]"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[错误] 未找到 uv, 请先安装: https://docs.astral.sh/uv/"
  exit 1
fi

if [[ ! -d frontend/node_modules ]]; then
  echo "[初始化] 安装前端依赖..."
  (cd frontend && npm install --no-fund --no-audit)
fi

echo "[启动] 后端 API  http://localhost:$BPORT  ($AUDIO)"
uv run python backend.py "$AUDIO" --serve "$BPORT" &
BACK_PID=$!

echo "[启动] 前端页面  http://localhost:$FPORT"
(cd frontend && npm run dev -- --port "$FPORT" --strictPort) &
FRONT_PID=$!

echo "浏览器打开: http://localhost:$FPORT"
trap 'kill $BACK_PID $FRONT_PID 2>/dev/null' EXIT
wait
