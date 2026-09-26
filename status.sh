#!/usr/bin/env bash
# modules/public/srcradar-mcp-server/status.sh
# 报 PID 文件 + /health + 最近 server.log。
# 不假设 PID 文件里的就是 daemon(读 PID 文件 + curl /health + 顺手看 8764)。
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${MODULE_DIR}/logs"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/srcradar-mcp.pid"
DEFAULT_PORT=8764

echo "==== srcradar-mcp status ===="
echo "Module:    ${MODULE_DIR}"
echo "PID file:  ${PID_FILE}"

if [ -f "${PID_FILE}" ]; then
    PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        echo "PID file:  ${PID_FILE} -> ${PID} (alive)"
    else
        echo "PID file:  ${PID_FILE} -> ${PID:-?} (stale)"
    fi
else
    echo "PID file:  none"
fi

echo "--- /health (127.0.0.1:${DEFAULT_PORT}) ---"
if HEALTH="$(curl -sf -m 2 "http://127.0.0.1:${DEFAULT_PORT}/health" 2>&1)"; then
    echo "${HEALTH}"
    echo
else
    echo "(unreachable)"
fi

echo "--- recent server.log (last 10 lines) ---"
if [ -f "${LOG_DIR}/server.log" ]; then
    tail -10 "${LOG_DIR}/server.log" 2>&1 || true
else
    echo "(no log)"
fi
