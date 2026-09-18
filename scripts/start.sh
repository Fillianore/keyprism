#!/usr/bin/env bash
# ============================================================
#  KeyPrism 一键启动 (Linux / macOS)
#  用法:  scripts/start.sh [音频文件]      # 缺省由后端默认加载 assets/demo.m4a
#
#  配置优先级: 环境变量 > ~/.keyprism/config.env > 内置默认值
#    KEYPRISM_HOME            工作区目录 (日志/缓存/上传暂存)
#    KEYPRISM_LOG_DIR         日志目录   (默认 $KEYPRISM_HOME/logs)
#    KEYPRISM_API_HOST/PORT   后端 API 监听地址
#    KEYPRISM_FRONTEND_PORT   前端页面端口
#  config.env 为 KEY=VALUE 纯文本 (# 开头为注释), 只识别 KEYPRISM_ 前缀。
#  在 VS Code 集成终端运行时, 两个端口会自动出现在 Ports 面板。
# ============================================================
set -euo pipefail

PROJ_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ_ROOT"

# ---- 工作区目录: 日志 / 缓存 / 上传暂存 (KEYPRISM_HOME 可重定向) ----
KEYPRISM_HOME="${KEYPRISM_HOME:-$HOME/.keyprism}"
KEYPRISM_HOME="${KEYPRISM_HOME/#\~/$HOME}"
mkdir -p "$KEYPRISM_HOME"

# 用户持久配置: 存在则加载 (已导出的环境变量优先, 不被覆盖)
CONFIG_FILE="$KEYPRISM_HOME/config.env"
if [[ -f "$CONFIG_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^KEYPRISM_[A-Za-z0-9_]+$ ]] || continue
        [[ -n "${!key+x}" ]] || printf -v "$key" '%s' "$value"
    done < <(grep -v '^[[:space:]]*#' "$CONFIG_FILE" | grep -v '^[[:space:]]*$')
fi

LOG_DIR="${KEYPRISM_LOG_DIR:-$KEYPRISM_HOME/logs}"
LOG_DIR="${LOG_DIR/#\~/$HOME}"
mkdir -p "$LOG_DIR"

# ---- 监听地址与端口 (不沿用旧版 8800/5180) ----
API_HOST="${KEYPRISM_API_HOST:-127.0.0.1}"
API_PORT="${KEYPRISM_API_PORT:-9630}"
FRONTEND_PORT="${KEYPRISM_FRONTEND_PORT:-5270}"
export KEYPRISM_HOME
export MPLCONFIGDIR="${MPLCONFIGDIR:-$KEYPRISM_HOME/cache/matplotlib}"
export PYTHONUNBUFFERED=1  # 后端日志实时落盘 (stdout 重定向时不块缓冲)

# ---- 预检 ----
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: 未找到 uv, 请先安装: https://docs.astral.sh/uv/" >&2
    exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
    echo "[初始化] 安装前端依赖..."
    (cd frontend && npm install --no-fund --no-audit)
fi

cleanup() {
    echo ""
    echo "Shutting down services..."
    [[ -n "${PID_API:-}" ]] && kill "$PID_API" 2>/dev/null || true
    [[ -n "${PID_VITE:-}" ]] && kill "$PID_VITE" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "All services stopped."
}
trap cleanup SIGINT SIGTERM

echo "Starting backend API (port $API_PORT)..."
if [[ $# -ge 1 && -n "${1:-}" ]]; then
    uv run python -m keyprism "$1" --serve "$API_PORT" --host "$API_HOST" \
        >"$LOG_DIR/backend.log" 2>&1 &
else
    uv run python -m keyprism --serve "$API_PORT" --host "$API_HOST" \
        >"$LOG_DIR/backend.log" 2>&1 &
fi
PID_API=$!

# 等待 API 就绪 (首次 uv sync + 频谱分析较慢; 超时仅提示, 不阻塞前端)
if command -v curl >/dev/null 2>&1; then
    API_UP=0
    for _ in $(seq 1 120); do
        if curl -sf "http://127.0.0.1:$API_PORT/api/ping" >/dev/null 2>&1; then
            API_UP=1
            break
        fi
        sleep 1
    done
    [[ "$API_UP" == 1 ]] || echo "[警告] API 120s 内未就绪, 详见 $LOG_DIR/backend.log"
else
    sleep 10
fi

echo "Starting Vite frontend (port $FRONTEND_PORT)..."
(cd frontend && npm run dev -- --port "$FRONTEND_PORT" --strictPort) \
    >"$LOG_DIR/vite.log" 2>&1 &
PID_VITE=$!

echo ""
echo "All services running:"
echo "  Backend API : http://$API_HOST:$API_PORT"
echo "  Frontend    : http://localhost:$FRONTEND_PORT"
echo ""
echo "Workspace: $KEYPRISM_HOME"
echo "Logs: $LOG_DIR/{backend,vite}.log"
echo "Press Ctrl+C to stop all services."

wait
