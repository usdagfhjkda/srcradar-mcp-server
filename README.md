# srcradar-mcp-server —— srcradar 的 MCP 适配层

> 把 srcradar 主仓的 15 个工具(转发 srcradar 子命令 + 2 个 stage 原语)用 MCP 协议的 streamable-http transport 暴露出来,本地 agent 直接通过 HTTP 调,不再每次 SSH 上 vps 开新会话。

<p align="left">
  <a href="https://github.com/usdagfhjkda/srcradar/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://github.com/usdagfhjkda/srcradar-mcp-server"><img src="https://img.shields.io/badge/repo-srcradar--mcp--server-181717.svg" alt="Repo"></a>
  <img src="https://img.shields.io/badge/python-%3E=3.11-3776AB.svg" alt="Python">
  <img src="https://img.shields.io/badge/MCP-transport--streamable--http-9cf.svg" alt="MCP transport">
  <img src="https://img.shields.io/badge/information--collection-only-important.svg" alt="Info Collection Only">
  <img src="https://img.shields.io/badge/parent-srcradar-success.svg" alt="parent: srcradar">
</p>

<br>

**面向 agent 的持久化业务级攻击面知识库**。把 srcradar 的"业务名 → 法律实体图谱 → 主动测绘资产(小程序/公众号 + Web 范围 → 子域名/端口/指纹) → 每日增量 diff"通过 MCP 协议暴露出来,让任何 agent 无需重新扫描即可直接拿到已知资产清单与最新变动 —— 跳过信息收集,从已识别的目标开始。

**srcradar-mcp-server** 是 srcradar 主仓的 **MCP 适配层**,不替代主仓、不持有业务数据。daemon 把 MCP `tools.invoke` 消息按 `whitelist.json` 转发到本机 `./srcradar <subcmd>`,或执行两个 stage 原语把 client 字节落到 daemon 侧的 `~/.cache/srcradar-mcp/staged/`,把"绝对路径"返还给吃路径的 srcradar 子命令(例如 `add_business -s seed.tsv` / `add_business -i input_dir/`)。

---

## 使用前提与合规

本模块作为 srcradar 的 MCP 适配层,完整使用前提与免责声明以主仓为准:

- 完整条款:[`srcradar/LICENSE`](https://github.com/usdagfhjkda/srcradar/blob/main/LICENSE)(Apache-2.0)
- 附加使用限制与免责声明:[`srcradar/TERMS_ADDENDUM.md`](https://github.com/usdagfhjkda/srcradar/blob/main/TERMS_ADDENDUM.md)
- 上游致谢:[`srcradar/NOTICE`](https://github.com/usdagfhjkda/srcradar/blob/main/NOTICE)

本模块**仅供已获合法书面授权的场景**(SRC 协议 / 渗透测试授权 / 自有资产白名单)使用;**不做漏洞利用或 PoC 触发**,不参与、不背书、不知情任何具体使用场景。

---

## 是什么

三条契约,违反任一条都不在本模块范围内:

- **协议契约**:`streamable-http` 单 endpoint `POST /mcp` 接 MCP `tools.invoke`,JSON-RPC 2.0 over HTTP(SSE 仅作 Accept 协商,默认 `application/json`);`GET /health` 给 SSH 隧道后的健康探针
- **端口契约**:daemon **只绑 `127.0.0.1:8764`**,loopback 即信任;远程调用一律走 SSH 隧道,daemon 不做 OAuth / token / session id
- **数据契约**:15 个工具(`auth: none` 转发 srcradar 子命令 + 2 个 `auth: e1-confirmed` stage 原语)按 `whitelist.json` 转发到同机 `./srcradar <subcmd>` 或执行 stage 原语落盘;**daemon 不持有 srcradar SQLite**,只读 `whitelist.json` + `tools_schema.py` + 自家 `logs/`

**主从关系**:srcradar 主仓是数据与业务源,本模块只是它的"远程面板"。

---

## 快速开始


### 服务端

本仓库仅在以下场景使用 `./install.sh` / `./start.sh` 等独立脚本:

- 从独立仓(非主仓子树)克隆 / 部署
- operator 手动验证 daemon 行为而不走主仓 dispatcher

### 客户端

本仓是 daemon,client 端约定见 [`srcradar-mcp-skill` §快速开始](https://github.com/usdagfhjkda/srcradar-mcp-skill#%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B)。
主仓已经集成 srcradar-mcp-server(默认不安装,需要在主仓 `./install.sh`
交互式 checklist 里手动勾选 `public/mcp-server`)。安装、启动、停止、状态
查询、日志查看等所有运维动作,均跟随主仓 dispatcher,详见
[`srcradar/README.md` §快速开始](https://github.com/usdagfhjkda/srcradar#%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B)。

## 架构

```
┌──────────────────┐         ┌────────────────────────────────────────────┐
│  MCP client      │         │  daemon host (loopback 127.0.0.1)          │
│  (agent / IDE /  │         │                                            │
│   harness / LLM) │         │  ┌──────────────────────────┐              │
│                  │  POST   │  │  daemon.py (streamable-   │              │
│                  │ ──────► │  │   http :8764, stdlib)     │              │
│                  │ /mcp    │  │                          │              │
│                  │         │  │  GET  /health  ── 200 OK │              │
│                  │ ◄────── │  │  POST /mcp     ──┐       │              │
│  (或本地通过      │  SSE /  │  └─────────────────┼───────┘              │
│   SSH 隧道转发)   │  JSON   │                    │ whitelist.json       │
└──────────────────┘         │                    ▼                      │
                             │  ┌──────────────────────────────────────┐   │
                             │  │ _stage_file / _stage_dir (e1-confirmed)│  │
                             │  │   ↳ 落 ~/.cache/srcradar-mcp/staged/  │   │
                             │  └──────────────────────────────────────┘   │
                             │                    │                      │
                             │                    ▼                      │
                             │  ┌──────────────────────────────────────┐   │
                             │  │ db/*.py  (auth: none, 只读)          │   │
                             │  │  ↳ 直查 srcradar 主仓 SQLite          │   │
                             │  └──────────────────────────────────────┘   │
                             │                    │                      │
                             │                    ▼                      │
                             │  ┌──────────────────────────────────────┐   │
                             │  │ ./srcradar <subcmd>  (主仓 dispatcher) │   │
                             │  │   ↳ manage.* / daily.* / ymicp.*     │   │
                             │  │   ↳ dispatcher.list / run_confirmed   │   │
                             │  └──────────────────────────────────────┘   │
                             │                    │                      │
                             │                    ▼                      │
                             │  ┌──────────────────────────────────────┐   │
                             │  │ srcradar 主仓 SQLite (只读 / 写)    │   │
                             │  │   db/recon.sqlite3 (WAL)             │   │
                             │  └──────────────────────────────────────┘   │
                             └────────────────────────────────────────────┘
```

### 关键不变量

- **daemon 不持有 srcradar SQLite**:所有数据契约都通过 `./srcradar <subcmd>` 子进程 + `db/*.py` 只读脚本间接完成;daemon 自己的工作目录只放 `whitelist.json` / `tools_schema.py` / `daemon.py` / `logs/`
- **whitelist.json 是唯一调度表**:工具名 → handler(脚本 / srcradar 子命令 / 原语)的映射;加新工具只改 `whitelist.json` + 对应脚本,不动 daemon 主循环
- **`auth: e1-confirmed` ≠ daemon 校验**:`tools_schema.py` 看到这个标记就把 `annotations.readOnlyHint` 设 `false`,让 Hermes `trust:untrusted` 触发原生审批弹窗;daemon 本身只校验 schema + whitelist 命中
- **stage 落盘即写操作**:`stage_file` / `stage_dir` 也标 `e1-confirmed`,因为它们写磁盘 + 占磁盘,跟 `manage.*` / `daily.*` 同档
- **不在 daemon 里做凭据 / 长连接 / 流恢复**:OAuth、token、session id、subscriptions/listen、MRTR、Stream resume 一律不实现(spec 删了的全部不实现)

---

## 工具清单

15 个工具,按 `auth` 分类。**实际数量以本仓库 `whitelist.json` 为准**。

| 工具 | 类型 | handler 形态 | auth | 一句话 |
|---|---|---|---|---|
| `stage_file` | 写(原语) | `_stage_file` | e1-confirmed | 把 client 字节落 `~/.cache/srcradar-mcp/staged/<id>.<ext>`,返回 `staged_path` + `upload_id` |
| `stage_dir` | 写(原语) | `_stage_dir` | e1-confirmed | tar.gz (base64) 或 entries-list 落 `~/.cache/srcradar-mcp/staged/<id>/`,返回 `staged_dir` + `upload_id` |
| `db.read_business_summary` | 读 | `db/read_business_summary.py` | none | 业务摘要(`businesses` + 关联 companies 计数) |
| `db.read_subdomains` | 读 | `db/read_subdomains.py` | none | `web_subdomains` 按业务过滤 |
| `db.read_open_ports` | 读 | `db/read_open_ports.py` | none | `tcp_assets` 按业务过滤 |
| `db.read_companies` | 读 | `db/read_companies.py` | none | `companies` 按业务过滤 |
| `db.read_diff` | 读 | `db/read_diff.py` | none | `daily/reports/<run-id>/` 增量报告 |
| `db.read_single_subdomain` | 读 | `db/read_single_subdomain.py` | none | 单子域按 `(business, subdomain)` 查询 |
| `dispatcher.list` | 读 | `./srcradar --list` | none | 列出主仓全部 (module, script) 对 |
| `dispatcher.run_confirmed` | 写 | `./srcradar -- <args>` | e1-confirmed | 透传任意主仓子命令(白名单内) |
| `manage.add_business` | 写 | `./srcradar manage add_business` | e1-confirmed | 新建 SRC 业务,灌入 scope |
| `manage.set_config` | 写 | `./srcradar manage set_config` | e1-confirmed | 业务级开关(`enabled` / `web` / `tcp` / `icp`) |
| `daily.run_one_business` | 写 | `./srcradar daily run_one_business` | e1-confirmed | 跑单业务全流程(位掩码 exit code) |
| `ymicp.icp_mapp_query` | 读 | `./srcradar ymicp icp_mapp_query` | none | 小程序 / 公众号备案反查 |
| `daily.run_dashboard_watchdog` | 写 | `./srcradar daily dashboard_watchdog` | e1-confirmed | dashboard 健康自检 + 自动重启(可选用) |

读 / 写比 = 8 读 + 7 写(其中 2 个 stage 原语 + 5 个 `./srcradar` 写子命令)。

---

## 共享数据模型

daemon **不直接写** srcradar 主仓 SQLite;所有数据访问都走 `db/*.py` 只读脚本(查询)或 `./srcradar <subcmd>` 子进程(写入)。下表是主仓 SQLite 表与本模块工具的映射,口径精简自 srcradar 主仓 README §共享数据模型:

| 主仓 SQLite 表 | 用途 | 本模块读端 | 本模块写端 |
|---|---|---|---|
| `businesses` | SRC 业务字典(`id`, `business_name`) | `db.read_business_summary` | `manage.add_business`(经 `./srcradar`) |
| `recon_business_config` | 业务级阶段开关(`enabled` / `web` / `tcp` / `icp`) | `db.read_business_summary` | `manage.set_config`(经 `./srcradar`) |
| `companies` | 法律实体(`business_id`, `unit_name`, `nature_name`) | `db.read_companies` | 主仓 `db_align` / `ymicp` 写,本模块**不写** |
| `mapp_records` | ICP / 小程序 / App / 公众号备案 | — | 主仓 `db_align` / `ymicp` 写,本模块**不写**;`ymicp.icp_mapp_query` 触发读取 |
| `scopes` | 可测 / 非可测资产白名单 | `db.read_business_summary` | `manage.add_business -i <dir>`(经 `./srcradar`) |
| `web_subdomains` / `web_hashes` | Web 资产 + 指纹库 | `db.read_subdomains` / `db.read_single_subdomain` | 主仓 `pdtm` 写,本模块**不写** |
| `tcp_assets` | TCP 端口资产 | `db.read_open_ports` | 主仓 `pdtm` 写,本模块**不写** |
| `permutation_state` | alterx 派生状态缓存 | — | 主仓 `pdtm` 维护,本模块**不写** |
| `service_type_map` | `service_type` 整型 → 人类可读名 | `db.read_companies`(间接) | 主仓 `db_align` 维护,本模块**不写** |
| `daily/reports/<run-id>/` | 增量 diff 报告 | `db.read_diff` | 主仓 `daily` 写,本模块**不写** |

**唯一经本模块触达的写入路径**:`./srcradar manage.*` + `./srcradar daily.run_one_business` + `dispatcher.run_confirmed`(透传白名单内的子命令)。其余写入由主仓 cron / 主动测绘流水线完成,本模块只是只读面板 + 受限转发。

staging 区域(本模块自有,**不入 srcradar 主仓 SQLite**):`~/.cache/srcradar-mcp/staged/<id>.<ext>` 与 `~/.cache/srcradar-mcp/staged/<id>/`,TTL 到期或下游调用 `_staging_ref` 触发即清理(见 §stage_file / stage_dir)。

---

## stage_file / stage_dir

client-local 文件传到 srcradar 的子命令(`add_business -s seed.tsv` / `add_business -i input_dir/`)需要一个 daemon 侧的真实路径。两个 staging 工具负责把 client 内容落盘到 `~/.cache/srcradar-mcp/staged/`(由 `STAGED_ROOT` 常量决定,优先 `$XDG_CACHE_HOME`),返回 `staged_path` / `staged_dir`,client 把它透传给后续的 `-s` / `-i` flag。

### stage_file

把一段字节写成一个文件。返回 `{staged_path, upload_id, size_bytes, ...}`,把 `staged_path` 透传给后续的 `-s` flag。

三种输入模式(三选一):

| 字段 | 形态 | 何时用 |
|---|---|---|
| `stdin` | inline string | LLM 现场生成的小内容(< 几 KB) |
| `base64` | base64 编码的字节流 | client 读文件 → base64 → 发送(最常用) |
| `file_path` + `base64` | 客户端路径 + base64 字节 | 同 `base64` 模式,但额外声明 client 路径(daemon 仅记录不访问) |

`file_path` 单独不收 — 必须配 `base64`(防 client 想让 daemon 直接读 client fs)。`filename` 走严格白名单(`[A-Za-z0-9._-]+`,最长 255,无 `..` 无路径分隔符)。payload 设上限,见 §约束。

调用样例:

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
             "Mcp-Name":"stage_file"})).read()
upload_id = json.loads(resp)["result"]["structuredContent"]["upload_id"]
staged = json.loads(resp)["result"]["structuredContent"]["staged_path"]
```

随后调下游工具时把 `staged` 当普通路径塞进 args,顺手加 `_staging_ref: <upload_id>` 让 daemon 在调用结束后清掉临时文件:

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

把多文件目录树落盘成一个目录。返回 `{staged_dir, upload_id, ...}`,把 `staged_dir` 透传给 `-i <input_dir>`。两种输入模式:

- `base64`:client 跑 `tar -czf - dir/ | base64 -w0`,daemon 解 tar.gz 到 `~/.cache/.../staged/<id>/`。所有 entry 走 path-traversal 校验
- `entries`:`[{filename, content_b64}, ...]` 的平铺列表(`target.txt` / `exclude.txt` 这种)。每条 entry 的 `filename` 跟 `stage_file` 走同样的白名单校验;**不支持子目录**(嵌套需求走 tar.gz)

### 临时文件清理

三层兜底:

1. **立即**:下游工具调用时传 `_staging_ref: <upload_id>`,daemon 在 `finally` 块删 `<id>` 匹配的文件 / 目录
2. **daemon 启动**:每次 daemon `__init__` 调 `_sweep_staged()`,删超过 TTL(默认 24h)的孤儿
3. **手工**:`Daemon._sweep_staged()` 是 `@staticmethod`,operator 可直接在 Python 里调

`upload_id` 是 `uuid4().hex[:16]`(16 位 hex),`filename` 走白名单;daemon 对 `_staging_ref` 做 `^[0-9a-f]{16}$` 二次校验,杜绝 path glob。

---

## 约束

- **只绑 `127.0.0.1:8764`**。daemon.py 默认 host/port,操作员若手动改成 `0.0.0.0` daemon 不会拦 — 但本项目 README / 脚本不宣传 / 不引导这种用法。跨机器调用一律走 SSH 隧道
- **loopback 即信任**。daemon 不做 OAuth / token / session id。Origin 头校验只放行 `{null, 127.0.0.1, ::1, localhost}`,其他来源 403
- **不做 OAuth / token 校验 / subscriptions/listen / MRTR / Stream resume**。MCP spec 删了的全部不实现
- **空逻辑 / 不留指纹**:daemon log 不打客户名 / token / cookie / 真实路径;上游 access log 一样按 http 模块的"超长 args hash 化"做,不暴露 operational fingerprint
- **不创建 cron / systemd / supervisor**。生命周期 = 手动 `--start` + (可选)空闲自杀;daemon 当前不动;若未来要加 idle-exit,在 daemon 内部加,不在这里管
- **不引入 daemon 机上未装的依赖**(只用 stdlib)

---

## 上游致谢与 License

本模块自身只依赖 Python stdlib(`http.server` / `json` / `socketserver` / `subprocess` / `uuid` / `hashlib`),不引入 daemon 机上未装的第三方包。能力由下列上游支撑:

- **MCP 协议** — [modelcontextprotocol/specification](https://github.com/modelcontextprotocol/specification),MCP 本模块按其 `streamable-http` transport 实现
- **srcradar 主仓** — [`usdagfhjkda/srcradar`](https://github.com/usdagfhjkda/srcradar),本模块是它的 MCP 适配层,所有业务数据由主仓持有
- **srcradar 主仓上游** — 见 [`srcradar/NOTICE`](https://github.com/usdagfhjkda/srcradar/blob/main/NOTICE),ENScan_GO / pdtm / dnsx / httpx / naabu / subfinder / alterx / cdnmatch 等

本模块与主仓同 License(Apache-2.0),条款以 [`srcradar/LICENSE`](https://github.com/usdagfhjkda/srcradar/blob/main/LICENSE) 为准;附加使用限制与免责声明见 [`srcradar/TERMS_ADDENDUM.md`](https://github.com/usdagfhjkda/srcradar/blob/main/TERMS_ADDENDUM.md);上游致谢见 [`srcradar/NOTICE`](https://github.com/usdagfhjkda/srcradar/blob/main/NOTICE)。

---

## 项目总结

### 数据现状

本模块本身**不持有数据**,全部通过 srcradar 主仓 SQLite(经 `db/*.py` 只读脚本或 `./srcradar` 子进程)间接触达。当前主仓 SQLite(`db/recon.sqlite3`)由 `pdtm` / `daily` / `db_align` / `ymicp` 四个模块写入,详细数量与设计亮点见 srcradar 主仓 README §项目总结。本模块在数据流中的位置 = **只读面板 + 受限转发器**。

### 设计亮点

1. **whitelist.json 单一调度表** — 加新工具只改 `whitelist.json` + 对应脚本,daemon 主循环不动;`auth: e1-confirmed` 是声明式 hint,daemon 不强制校验,把审批语义让给客户端(Hermes `trust:untrusted`)
2. **stdin / base64 / file_path+base64 三模 stage** — 覆盖 LLM 现场生成 / 普通 client / 审计可追溯三种 client 形态;`filename` 白名单 + 路径分隔符拒绝 + path-traversal 校验共同阻止 path glob
3. **三层 staging 清理兜底** — 立即(`_staging_ref`)、启动时(`_sweep_staged()`)、手工(`@staticmethod`);daemon 跑久不会脏
4. **Origin 头三件套** — `Accept` / `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` / `Origin` 五项校验集中在 POST `/mcp` 入口,返回标准 JSON-RPC 错误码(`-32022` / `-32020` / `403` 等),不暴露 daemon 内部
5. **SSH 隧道替代 TLS 终止** — 不在 daemon 里塞证书 / TLS,跨机器调用安全由 SSH channel 兜;约束更少、攻击面更小

### 已知能力边界(故意不做)

- **OAuth / token / session** — loopback 即信任,远程一律 SSH 隧道
- **`subscriptions/listen` / `MRTR` / Stream resume** — MCP spec 删了的全部不实现
- **TLS 终止** — 由 SSH 隧道自带 channel security 兜
- **systemd / cron / supervisor** — 生命周期 = 手动 `--start` / `stop.sh` / `status.sh`
- **idle-exit** — 当前未实现,若未来加,在 daemon 内部加,README 与 install.sh 同步更新

---

## License

srcradar-mcp-server 以 Apache License 2.0 分发,完整条款见 [`srcradar/LICENSE`](https://github.com/usdagfhjkda/srcradar/blob/main/LICENSE)。
上游项目与各自 License 详见 [`srcradar/NOTICE`](https://github.com/usdagfhjkda/srcradar/blob/main/NOTICE)。
工具使用前提与免责声明见 [`srcradar/TERMS_ADDENDUM.md`](https://github.com/usdagfhjkda/srcradar/blob/main/TERMS_ADDENDUM.md)。