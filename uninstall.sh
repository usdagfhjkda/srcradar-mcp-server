#!/usr/bin/env bash
# modules/public/srcradar-mcp-server/uninstall.sh
# 停服务 + 删 PID + 删 log 文件(保留 daemon.py + whitelist.json + 脚本本身)。
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${MODULE_DIR}/logs"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/srcradar-mcp.pid"

# 1) 停服务
if [ -x "${MODULE_DIR}/stop.sh" ]; then
    bash "${MODULE_DIR}/stop.sh" || true
fi

# 2) 删 log 文件(保留 .gitkeep)
rm -f "${LOG_DIR}/server.log"
# access.log / error.log 当前 daemon 不写,删了也无害
rm -f "${LOG_DIR}/access.log" "${LOG_DIR}/error.log" 2>/dev/null || true

# 3) 删 PID
rm -f "${PID_FILE}"

echo "[uninstall] done. To remove the module entirely: rm -rf ${MODULE_DIR}"
echo "[uninstall] daemon.py + whitelist.json + scripts were left in place."
