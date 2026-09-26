#!/usr/bin/env bash
# modules/public/srcradar-mcp-server/start.sh
# 操作员手动启(跟 install.sh --yes 等价,但不再次 install / chmod / py_compile)。
# 已跑则早退;端口被占 → 早退(不抢他人的 8764)。
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="${MODULE_DIR}/daemon.py"
LOG_DIR="${MODULE_DIR}/logs"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/srcradar-mcp.pid"
SERVER_LOG="${LOG_DIR}/server.log"
DEFAULT_PORT=8764

mkdir -p "${LOG_DIR}"

# 已有 PID 文件 + 进程在跑 → 早退
if [ -f "${PID_FILE}" ]; then
    OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "[srcradar-mcp] already running PID ${OLD_PID}" >&2
        exit 0
    fi
    echo "[srcradar-mcp] stale PID ${OLD_PID:-?}, removing" >&2
    rm -f "${PID_FILE}"
fi

# 端口被占 → 早退
if curl -sf "http://127.0.0.1:${DEFAULT_PORT}/health" >/dev/null 2>&1; then
    echo "[srcradar-mcp] port ${DEFAULT_PORT} already bound on loopback (likely another mcp daemon)" >&2
    echo "[srcradar-mcp] nothing to do" >&2
    exit 0
fi

# 拉起
rm -f "${PID_FILE}"
setsid nohup python3 "${DAEMON}" --http >> "${SERVER_LOG}" 2>&1 < /dev/null &
DAEMON_PID=$!
echo "${DAEMON_PID}" > "${PID_FILE}"

# 轮询 /health(最多 10s)
READY=0
for _ in $(seq 1 20); do
    sleep 0.5
    if curl -sf "http://127.0.0.1:${DEFAULT_PORT}/health" >/dev/null 2>&1; then
        READY=1
        break
    fi
    if ! kill -0 "${DAEMON_PID}" 2>/dev/null; then
        echo "[srcradar-mcp] daemon died during startup, see ${SERVER_LOG}" >&2
        rm -f "${PID_FILE}"
        exit 1
    fi
done

if [ "${READY}" != "1" ]; then
    echo "[srcradar-mcp] /health not ready in 10s, see ${SERVER_LOG}" >&2
    rm -f "${PID_FILE}"
    kill "${DAEMON_PID}" 2>/dev/null || true
    exit 1
fi

echo "[srcradar-mcp] started PID ${DAEMON_PID}, log ${SERVER_LOG}"
echo "[srcradar-mcp] PID file ${PID_FILE}"
