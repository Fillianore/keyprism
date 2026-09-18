#!/usr/bin/env bash
# ============================================================
#  KeyPrism one-command launch (Linux / macOS)
#  Usage:  scripts/start.sh [audio file]   # the backend loads assets/demo.m4a by default
#
#  Config precedence: env vars > ~/.keyprism/config.env > built-in defaults
#    KEYPRISM_HOME            workspace directory (logs/cache/upload staging)
#    KEYPRISM_LOG_DIR         log directory (default $KEYPRISM_HOME/logs)
#    KEYPRISM_API_HOST/PORT   backend API listen address
#    KEYPRISM_FRONTEND_PORT   frontend page port
#  config.env is plain KEY=VALUE text (lines starting with # are comments);
#  only keys prefixed with KEYPRISM_ are recognized.
#  In the VS Code integrated terminal, both ports appear in the Ports panel.
# ============================================================
set -euo pipefail

PROJ_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ_ROOT"

# ---- Workspace: logs / cache / upload staging (relocatable via KEYPRISM_HOME) ----
KEYPRISM_HOME="${KEYPRISM_HOME:-$HOME/.keyprism}"
KEYPRISM_HOME="${KEYPRISM_HOME/#\~/$HOME}"
mkdir -p "$KEYPRISM_HOME"

# User persistent config: loaded if present (already-exported env vars win)
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

# ---- Listen address and ports (old 8800/5180 intentionally not reused) ----
API_HOST="${KEYPRISM_API_HOST:-127.0.0.1}"
API_PORT="${KEYPRISM_API_PORT:-9630}"
FRONTEND_PORT="${KEYPRISM_FRONTEND_PORT:-5270}"
export KEYPRISM_HOME
export MPLCONFIGDIR="${MPLCONFIGDIR:-$KEYPRISM_HOME/cache/matplotlib}"
export PYTHONUNBUFFERED=1  # backend logs hit disk in real time (no block buffering when stdout is redirected)

# ---- Preflight ----
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

# Wait for the API to be ready (first uv sync + spectrum analysis are slow;
# timeout only warns, never blocks the frontend)
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
