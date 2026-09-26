#!/usr/bin/env bash
# modules/public/srcradar-mcp-server/install.sh
# 真 installer:检端口占用,若无冲突则 setsid nohup 拉 daemon,写 PID 文件,轮询 /health。
# 端口 8764 被占时 → 早退 "already running",不真起(不抢他人的 daemon)。
# 不创建 systemd / cron / supervisor;生命周期 = 手动 + 空闲自杀。
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${MODULE_DIR}/logs"
DAEMON="${MODULE_DIR}/daemon.py"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/srcradar-mcp.pid"
SERVER_LOG="${LOG_DIR}/server.log"
DEFAULT_PORT=8764

mkdir -p "${LOG_DIR}"
touch "${LOG_DIR}/.gitkeep"

# 把 install.sh 自己、start/stop/status/uninstall.sh、daemon.py 加可执行
chmod +x "${MODULE_DIR}/install.sh" \
         "${MODULE_DIR}/start.sh" \
         "${MODULE_DIR}/stop.sh" \
         "${MODULE_DIR}/status.sh" \
         "${MODULE_DIR}/uninstall.sh" \
         "${DAEMON}" 2>/dev/null || true

# py_compile 自检
python3 -m py_compile "${DAEMON}"

# 端口探测:8764 被活 mcp daemon 占用 → 已跑(其他实例)
is_loopback_listen() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E "127\.0\.0\.1:${port}\$|:\[::1\]:${port}\$" >/dev/null
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -E "127\.0\.0\.1:${port}\$|:\[::1\]:${port}\$" >/dev/null
    else
        # 最后退路:尝试 GET /health
        curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1
    fi
}

is_our_pid_alive() {
    if [ -f "${PID_FILE}" ]; then
        local pid
        pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

case "${1:-}" in
    --check)
        # 静态检查通过即返回 0
        exit 0
        ;;
    --uninstall)
        # 卸载走专用 uninstall.sh
        exec bash "${MODULE_DIR}/uninstall.sh"
        ;;
    --yes|"")
        # 1) 已有 PID 文件 + 进程在跑 → 早退
        if is_our_pid_alive; then
            local_pid="$(cat "${PID_FILE}" 2>/dev/null || echo "?")"
            echo "[srcradar-mcp] already running (PID file ${PID_FILE} → ${local_pid})" >&2
            echo "[srcradar-mcp] run status.sh or stop.sh first" >&2
            exit 0
        fi

        # 2) 8764 端口被占(其他 mcp daemon 在跑) → 早退,不抢
        if is_loopback_listen "${DEFAULT_PORT}"; then
            echo "[srcradar-mcp] port ${DEFAULT_PORT} already bound on loopback (likely another mcp daemon)" >&2
            echo "[srcradar-mcp] nothing to do; status.sh reports its /health" >&2
            exit 0
        fi

        # 3) 拉起
        rm -f "${PID_FILE}"
        # setsid + nohup:SSH 断开不 SIGHUP 杀;tty 独立
        setsid nohup python3 "${DAEMON}" --http >> "${SERVER_LOG}" 2>&1 < /dev/null &
        DAEMON_PID=$!
        echo "${DAEMON_PID}" > "${PID_FILE}"

        # 4) 轮询 /health(最多 10s,20 * 0.5s)
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

        cat <<EOF

[install] OK. srcradar-mcp daemon installed under:
   ${MODULE_DIR}

[install] PID file: ${PID_FILE} (PID ${DAEMON_PID})
[install] Log:      ${SERVER_LOG}
[install] Loopback only: 127.0.0.1:${DEFAULT_PORT}

[install] To check status:   bash ${MODULE_DIR}/status.sh
[install] To stop:          bash ${MODULE_DIR}/stop.sh
[install] To uninstall:     bash ${MODULE_DIR}/uninstall.sh

[install] NOTE: v1 ops 在 daemon 启起来后再决定是否开 SSH tunnel。
[install]       没有创建 cron / systemd;空闲超时由 daemon 内部处理(若启用)。
EOF
        exit 0
        ;;
    -h|--help)
        cat <<EOF
modules/public/srcradar-mcp-server/install.sh — user-managed daemon installer

Usage:
  install.sh --yes        install (or report "already running") and exit 0
  install.sh --check      static check (always 0 if script is valid)
  install.sh --uninstall  forward to uninstall.sh
  install.sh --help       this help
EOF
        exit 0
        ;;
    *)
        echo "unknown arg: $1" >&2
        exit 1
        ;;
esac
