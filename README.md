# srcradar MCP server module

让 srcradar 的 15 个工具(13 个只读 + 2 个 stage 原语)通过 MCP 协议的
streamable-http transport 被远程调用,而不是每次都 SSH 上 v1 开新会话。

stage_file / stage_dir 是 2026-09-26 新增的 in-process staging 原语,
让 client 把本地文件 / 目录内容落到 daemon 侧的 `~/.cache/srcradar-mcp/
staged/` 拿到绝对路径,再传给吃路径的 srcradar 子命令(如 add_business
`-s <seed.tsv>` 或 `-i <input_dir>`)。

## 约束

- **只绑 `127.0.0.1:8764`**。daemon.py 默认 host/port,操作员若手动改成
  `0.0.0.0` daemon 不会拦 —— 但本项目 README / 脚本不宣传 / 不引导这种用法。
  跨机器调用一律走 SSH 隧道 (见下文)。
- **loopback 即信任**。daemon 不做 OAuth / token / session id。
  Origin 头校验只放行 `{null, 127.0.0.1, ::1, localhost}`,其他来源 403。
- **不做 OAuth / token 校验 / subscriptions/listen / MRTR /
  Stream resume**。spec 删了的全部不实现。
- **空逻辑 / 不留指纹**:daemon log 不打客户名 / token / cookie / 真实路径;
  上游 access log 一样按 http 模块的"超长 args hash 化"做,不暴露
  operational fingerprint。
- **不创建 cron / systemd / supervisor**。生命周期 = 手动 `--start` +
  (可选) 空闲自杀。daemon 当前不动;若未来要加 idle-exit,在 daemon 内部
  加,不在这里管。
- **不引入 v1 上未装的依赖**(只用 stdlib)。

## Transport

MCP spec revision 2026-07-28,streamable-http,单 endpoint:

| Method | Path            | 说明                                                     |
| ------ | --------------- | -------------------------------------------------------- |
| POST   | `/mcp`          | JSON-RPC 2.0 over streamable-http (本模块主入口)         |
| GET    | `/health`       | 健康检查,独立路径 (loopback HTTP 返回简单 JSON)          |

`POST /rpc` (上版本遗留) **不在**:本项目不保留 2025-03-26 兼容。

### `/mcp` 必校验的头

- `Accept`:含 `application/json` 或 `text/event-stream` 任一即放行
- `MCP-Protocol-Version`:必须是 `2026-07-28`,缺失或别的版本 → `-32022`
- `Mcp-Method`:必须与 body `method` 字段一致;不一致 → `-32020 HeaderMismatch`
- `Mcp-Name`:对 `tools/call` (本项目 `tools.invoke`) 严格校验
- `Origin`:仅放行 `{null, 127.0.0.1, ::1, localhost}`,其他 403

### 响应形式

两种都支持,由 client 的 `Accept` 决定:

- `application/json`:单帧 JSON-RPC 响应 (默认)
- `text/event-stream`:SSE 帧,单帧 + close

## 安装 / 启动 / 停止 / 卸载

在 v1 上:

```bash
bash ./modules/public/srcradar-mcp-server/install.sh --yes
bash ./modules/public/srcradar-mcp-server/start.sh
bash ./modules/public/srcradar-mcp-server/status.sh
bash ./modules/public/srcradar-mcp-server/stop.sh
bash ./modules/public/srcradar-mcp-server/uninstall.sh
```

`install.sh --yes` 的行为:

1. PID 文件有活进程 → 早退 "already running"
2. 端口 8764 被占(其他 mcp daemon) → 早退,不抢
3. 否则 `setsid nohup python3 daemon.py --http` + 写 PID + 轮询 `/health`

`stop.sh --dryrun` 留个开关,真 SIGTERM 前先看一眼。

## SSH tunnel play-book

SSH tunnel 由**用户**手动开,不让 client 触发;以下示例用 v1 上的
`<srcradar install>` 作为 daemon 端。

```bash
# 本地一条 LocalForward,把 v1:8764 -> 本机 127.0.0.1:8764
ssh -fN -o ExitOnForwardFailure=yes \
    -L 8764:127.0.0.1:8764 v1

# 验证
curl -s http://127.0.0.1:8764/health
# 期望: {"status":"ok","daemon":"srcradar-mcp"}
```

tunnel 之前要确保 daemon 在 v1 上`/health` 已经 200;daemon 没起来,tunnel
连过去一样 404 / 连不上。`status.sh` 一行报当前 PID 状态 + `/health` + 最近日志。

## stage_file / stage_dir(2026-09-26 新增)

client-local 文件传到 srcradar 的子命令(`add_business -s seed.tsv` /
`add_business -i input_dir/`)需要一个 daemon 侧的真实路径。两个 staging
工具负责把 client 内容落盘到 `~/.cache/srcradar-mcp/staged/`(由
`STAGED_ROOT` 常量决定,优先 `$XDG_CACHE_HOME`):

### stage_file

把一段字节写成一个文件。返回 `{staged_path, upload_id, size_bytes, ...}`,
把 `staged_path` 透传给后续的 `-s` flag。

三种输入模式(三选一):

| 字段              | 形态                       | 何时用                              |
| ----------------- | -------------------------- | ----------------------------------- |
| `stdin`           | inline string              | LLM 现场生成的小内容(< 几 KB)        |
| `base64`          | base64 编码的字节流         | client 读文件 → base64 → 发送(最常用)|
| `file_path` + `base64` | 客户端路径 + base64 字节 | 同 base64 模式,但额外声明 client 路径(daemon 仅记录不访问)|

`file_path` 单独不收 — 必须配 `base64`(防 client 想让 daemon 直接读
client fs)。`filename` 走严格白名单(`[A-Za-z0-9._-]+`,最长 255,无 `..`
无路径分隔符)。payload 上限 100 MiB(STAGE_MAX_BYTES)。

调用样例(client 在 v1 / 桌面 app / 测试 harness 都一样):

```python
# client 端 Python
import base64, json, urllib.request

payload = open("/local/path/seed.tsv", "rb").read()
body = {
    "jsonrpc": "2.0", "id": 1, "method": "tools.invoke",
    "params": {
        "name": "stage_file",
        "args": {
            "filename": "seed.tsv",
            "base64": base64.b64encode(payload).decode(),
            "file_path": "/local/path/seed.tsv",  # 审计字段
        },
    },
}
resp = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8764/mcp", data=json.dumps(body).encode(),
    headers={"Content-Type":"application/json",
             "Accept":"application/json",
             "MCP-Protocol-Version":"2026-07-28",
             "Mcp-Method":"tools.invoke",
             "Mcp-Name":"stage_file"},
)).read()
upload_id = json.loads(resp)["result"]["structuredContent"]["upload_id"]
staged = json.loads(resp)["result"]["structuredContent"]["staged_path"]
```

随后调下游工具时把 `staged` 当普通路径塞进 args,顺手加 `_staging_ref:
<upload_id>` 让 daemon 在调用结束后清掉临时文件:

```python
body = {
    "jsonrpc": "2.0", "id": 2, "method": "tools.invoke",
    "params": {
        "name": "manage.add_business",
        "args": {"-n": "Acme", "-s": staged},
        "_staging_ref": upload_id,  # daemon 在 finally 清理
    },
}
```

### stage_dir

把多文件目录树落盘成一个目录。返回 `{staged_dir, upload_id, ...}`,把
`staged_dir` 透传给 `-i <input_dir>`。两种输入模式:

- `base64`:client 跑 `tar -czf - dir/ | base64 -w0`,daemon 解 tar.gz
  到 `~/.cache/.../staged/<id>/`。所有 entry 走 path-traversal 校验。
- `entries`:`[{filename, content_b64}, ...]` 的平铺列表(target.txt,
  exclude.txt 这种)。每条 entry 的 `filename` 跟 stage_file 走同样的
  白名单校验;不支持子目录(嵌套需求走 tar.gz)。

### 临时文件清理

三层兜底:

1. **立即**:下游工具调用时传 `_staging_ref: <upload_id>`,daemon 在
   `finally` 块删 `<id>` 匹配的文件 / 目录。
2. **daemon 启动**:每次 daemon `__init__` 调 `_sweep_staged()`,删
   `> STAGE_TTL_SECONDS`(默认 24h)的孤儿。
3. **手工**:`Daemon._sweep_staged()` 是 `@staticmethod`,operator 可
   直接在 Python 里调。**清理策略不耦合到 health 模块**(health 只
   observe,不写盘);见 README §约束。

`upload_id` 是 `uuid4().hex[:16]`(16 位 hex),`filename` 走白名单;daemon
对 `_staging_ref` 做 `^[0-9a-f]{16}$` 二次校验,杜绝 path glob。

### 与 e1-confirmed 的关系

stage_file / stage_dir 在 whitelist 里标 `auth: e1-confirmed`(虽然
daemon 不再校验这个字段,见 README §约束);目的是让 `tools_schema.py`
把它们的 `annotations.readOnlyHint` 设成 `false`,这样 Hermes 端
`trust: untrusted` 会触发原生审批弹窗。**stage 操作会落盘并占磁盘,跟
`manage.*` / `daily.*` 一样属于写操作**,理应弹窗。

## 用户责任划分

- **srcradar 提供**:daemon.py + whitelist.json(15 个工具:13 个 srcradar
  子命令转发 + 2 个 stage 原语)+ 5 个 shell 包装脚本 + 本 README。
- **v1 ops 责任**:登 v1,跑 `install.sh --yes` / `start.sh` / `stop.sh`;
  把 daemon log 收尾;决定要不要 SSH tunnel、要不要把端口收进 iptables / firewalld。
- **不在 srcradar 范围**:OAuth、token 校验、session id、TLS 终止(走 SSH
  隧道自带 channel security)、systemd unit、`subscriptions/listen` 长连接
  / sampling / elicitation / Stream resume。

## 日志

- `logs/server.log` —— nohup 启动 stdout/stderr(daemon 当前不写文件 log,
  这里承接 `print` / `sys.stderr`)
- `logs/access.log` —— 保留给未来的 access log 钩子(目前 daemon 不写)
- `logs/error.log` —— 同上,保留位

## PID 文件

`/tmp/srcradar-mcp.pid`(默认;若 `$XDG_RUNTIME_DIR` 存在则优先)。
