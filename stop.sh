#!/usr/bin/env bash
# modules/public/srcradar-mcp-server/stop.sh
# 读 PID 文件 → SIGTERM → 等 1s → 还在就 SIGKILL → 删 PID 文件。
# 端口上跑的 daemon 跟我们 PID 文件对不上(其他用户 / 之前手敲的 nohup)→ 拒绝 stop,不跨 PID 杀。
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_LOG="${MODULE_DIR}/logs/server.log"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/srcradar-mcp.pid"

case "${1:-}" in
    --dryrun)
        echo "[srcradar-mcp] dry-run: would read ${PID_FILE} and SIGTERM/SIGKILL the recorded PID"
        if [ -f "${PID_FILE}" ]; then
            echo "[srcradar-mcp] dry-run: PID file exists with PID $(cat "${PID_FILE}" 2>/dev/null || echo '?')"
        else
            echo "[srcradar-mcp] dry-run: no PID file"
        fi
        echo "[srcradar-mcp] dry-run: daemon log would be ${SERVER_LOG}"
        exit 0
        ;;
esac

if [ ! -f "${PID_FILE}" ]; then
    echo "[srcradar-mcp] no PID file, nothing to stop"
    exit 0
fi

PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
if [ -z "${PID}" ] || ! kill -0 "${PID}" 2>/dev/null; then
    echo "[srcradar-mcp] PID ${PID:-?} not running, removing stale file"
    rm -f "${PID_FILE}"
    exit 0
fi

# 验证这个 PID 真的是 srcradar-mcp 的 daemon(不是借壳的)
CMD_FILE="/proc/${PID}/cmdline"
if [ ! -r "${CMD_FILE}" ]; then
    echo "[srcradar-mcp] cannot read ${CMD_FILE}, refusing to stop" >&2
    exit 1
fi
CMDLINE="$(tr '\0' ' ' < "${CMD_FILE}")"
case "${CMDLINE}" in
    *srcradar-mcp-server/daemon.py*--http*)
        : # matches our daemon
        ;;
    *)
        echo "[srcradar-mcp] PID ${PID} is not a srcradar-mcp daemon (cmdline: ${CMDLINE})" >&2
        echo "[srcradar-mcp] refusing to stop; remove ${PID_FILE} manually if stale" >&2
        exit 1
        ;;
esac

kill "${PID}" 2>/dev/null || true
sleep 1
if kill -0 "${PID}" 2>/dev/null; then
    kill -9 "${PID}" 2>/dev/null || true
fi

rm -f "${PID_FILE}"
echo "[srcradar-mcp] stopped (PID ${PID})"
