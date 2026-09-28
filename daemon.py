#!/usr/bin/env python3
"""v1/daemon.py - JSON-RPC 2.0 daemon for srcradar-mcp.

Long-running process that:
  - opens a single read-only sqlite3 connection (uri=True, mode=ro)
  - prepares and caches sqlite3 statements for db.* tools at startup; the
    cache is reused per request (no subprocess fork, no per-request
    statement recompile)
  - enforces the whitelist loaded from whitelist.json (path passed via --whitelist)
  - serves JSON-RPC 2.0 over an HTTP listener (only transport)
  - only resolves whitelist-listed tools
  - 15 tools: 6 db.* reads + 1 dispatcher.list + 1 ymicp.icp_mapp_query
    + 5 e1-confirmed subprocess tools (manage.* / daily.* /
    dispatcher.run_confirmed) + 2 staging primitives
    (stage_file / stage_dir)

Supported methods (transport-agnostic; daemon.handle_request is the single
source of truth for every payload):
  {"jsonrpc":"2.0","id":N,"method":"tools.list"}              -> {tools:[{name,path,auth}]}
  {"jsonrpc":"2.0","id":N,"method":"tools.invoke","params":{"name":..., "args":{...}}}
    -> for db.* tools: dispatch to the prepared-statement registry.
    -> for dispatcher.* tools: forward to ./srcradar <args> via subprocess.
    Auth gate (e1 / e1-confirmed) enforced before any work happens.

CLI flags:
  --http [--port N]     serve JSON-RPC over HTTP on 127.0.0.1:N (default: 8764)
                        (only transport)
  --self-test           run the JSON-RPC sanity check and exit
  --test TOOL [args]    one-shot CLI: load whitelist, dispatch TOOL with the
                        remaining argv as its args, print JSON response, exit.

Whitelist authority: tool resolution and dispatch derive strictly from
whitelist.json. db.* tools are served by the in-process registry
(DB_QUERIES); dispatcher.* are forwarded to srcradar via subprocess.

HTTP transport notes (streamable-http, MCP spec revision 2026-07-28):
  - Binds 127.0.0.1 only (loopback). Pair with `ssh -L 8765:v1:8765 <alias>`.
  - Single JSON-RPC endpoint POST /mcp; GET /health is preserved on its own
    path. GET/DELETE /mcp and any other path -> 405 / 404. The legacy
    POST /rpc route is not retained.
  - Envelope gates (spec MUST) run before dispatch:
      * Accept must offer application/json and/or text/event-stream.
      * MCP-Protocol-Version must equal "2026-07-28".
      * Mcp-Method must equal body method (else -32020 HeaderMismatch).
      * Mcp-Name (only on tools.invoke) must equal params.name.
      * Origin, when present, must be a loopback sentinel (127.0.0.1 /
        ::1 / localhost / null). Missing Origin is allowed.
  - Response framing follows the request Accept preference: JSON or
    text/event-stream (single frame + Connection: close). Envelope
    errors are always JSON. Notifications (no `id`) get 202 Accepted.
  - daemon.handle_request is the dispatch source of truth -- the HTTP
    layer only validates the envelope and frames the response.
  - ThreadingHTTPServer handles one request per thread (db.* are read-only
    sqlite3 queries on a single connection, safe for concurrent reads).
"""

import argparse
import base64
import binascii
import io
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import uuid

import datetime as dt
from typing import ClassVar, Optional
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools_schema import list_tools_payload

HARD_CAP = 10000

# ---------------------------------------------------------------------------
# Stage file/dir materialisation (2026-09-26)
# ---------------------------------------------------------------------------
# Path on the daemon host where stage_file / stage_dir write client-supplied
# bytes. We use $XDG_CACHE_HOME/srcradar-mcp/staged if set, else
# ~/.cache/srcradar-mcp/staged. The directory is created lazily and swept
# on daemon startup; per-upload cleanup happens in _invoke when the caller
# passes _staging_ref=<upload_id>.
STAGED_ROOT = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "srcradar-mcp",
    "staged",
)
# 100 MiB hard cap on a single staged payload. Anything bigger almost
# certainly is operator error (TSV seeds for srcradar are kB-MB scale).
STAGE_MAX_BYTES = 100 * 1024 * 1024
# How long an unreferenced staged upload survives before the next daemon
# startup sweep removes it. 24h matches the rest of the system's hygiene
# cadence (health cron daily 03:15).
STAGE_TTL_SECONDS = 24 * 3600


def _gen_upload_id() -> str:
    """Random 16-hex-char upload id (matches the filename stem)."""
    return uuid.uuid4().hex[:16]


def _validate_filename(name: str) -> tuple[bool, str]:
    """Reject anything outside [A-Za-z0-9._-] or longer than 255 chars.

    Returns (ok, normalised_or_error). On failure the caller should
    surface the second element as the -32602 message.

    Why so strict: the filename becomes part of an absolute path under
    STAGED_ROOT. Allowing slashes, parent-traversal, or NUL bytes would
    re-open the path-traversal class we closed by isolating the
    whitelist + loopback gate.
    """
    if not isinstance(name, str) or not name:
        return False, "filename must be a non-empty string"
    if len(name) > 255:
        return False, "filename too long (max 255 chars)"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return False, (
            "filename must match [A-Za-z0-9._-]+ (no path separators, "
            "no .., no NUL, no unicode)"
        )
    return True, name
# Default to the module-local sandbox sqlite. The previous default
# pointed at ~/tools/recon/db/recon.sqlite3, which is the production
# database the operator runs daily reconnaissance against; that made
# the daemon ship production data on any leak (notably the
# daemon.ping echo, but also the SELECT statements we run when
# `db.read_*` is invoked without a sandbox). Two of those risks are
# fixed by the change below:
#   - The default db is now under modules/main/db/, where the
#     srcradar scripts already keep a per-module scratch sqlite. It
#     is in .gitignore so it never reaches the remote; ops who want
#     to query production have to opt in explicitly via
#     $RECON_DB env var, or config/db.conf's recon_db_path= field.
#   - The "default" path is computed from the daemon's own location
#     so it is absolute, deterministic, and does not depend on the
#     operator's HOME (no more `os.path.expanduser`).
_DEFAULT_DB_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "main", "db",
)
DEFAULT_DB = os.environ.get(
    "RECON_DB_PATH",
    os.path.join(_DEFAULT_DB_DIR, "recon.sqlite3"),
)
DEFAULT_LIMIT = 100


def _open_ro(db_path: str) -> sqlite3.Connection:
    uri = db_path if (db_path.startswith("file:") and "mode=ro" in db_path) else (
        db_path if db_path.startswith("file:") else f"file:{db_path}?mode=ro"
    )
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON;")
    return conn


# ----------------------------------------------------------------------
# db.* prepared-statement registry.
#
# Each entry is a callable (conn, params, args) -> JSON-ready result. The
# conn's prepare-cache + the lock in Daemon give us reusable statements across
# calls without per-request subprocess overhead. All parameters are bound via
# ? placeholders; no SQL strings are built from caller input.
# ----------------------------------------------------------------------

DB_COLS_BIZ = ("id", "business_name", "change_type")
DB_COLS_SUBDOMAINS = (
    "id", "subdomain", "port", "url", "status_code", "content_length",
    "title", "technologies", "first_seen", "last_seen", "fetched_at",
    "is_active", "change_type",
)
DB_COLS_OPEN_PORTS = (
    "id", "ip", "port", "first_seen", "last_seen", "fetched_at",
    "is_active", "raw_value", "hosts", "change_type",
)
DB_COLS_COMPANIES = (
    "id", "unit_name", "nature_name", "main_licence",
    "created_at", "updated_at", "change_type",
)
DB_COLS_SINGLE_SUBDOMAIN = DB_COLS_SUBDOMAINS + ("raw_json",)


def _row_to_dicts(rows, cols):
    out = []
    for r in rows:
        out.append({c: r[i] for i, c in enumerate(cols)})
    return out


def _lookup_business_id(conn, business_name):
    row = conn.execute(
        "SELECT id, business_name FROM businesses WHERE business_name = ?",
        (business_name,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _clamp_limit(args, default=DEFAULT_LIMIT, cap=HARD_CAP):
    raw = args.get("limit", default)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    if n < 1:
        n = default
    if n > cap:
        sys.stderr.write(f"WARNING: --limit {n} exceeds hard cap {cap}; truncating to {cap}\n")
        n = cap
    return n


def _q_read_business_summary(conn, args):
    name = args.get("business")
    if name is None:
        # --all mode: list businesses (not the per-business summary).
        limit = _clamp_limit(args)
        rows = conn.execute(
            "SELECT id, business_name, change_type FROM businesses ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return {"count": len(rows), "truncated": False, "businesses": _row_to_dicts(rows, DB_COLS_BIZ)}

    row = conn.execute(
        "SELECT id, business_name, change_type FROM businesses WHERE business_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return {"found": False, "business_name": name}
    biz_id = row[0]
    biz = dict(zip(DB_COLS_BIZ, row))

    # 8 counts reused from read_business_summary.py
    counts = {}
    counts["companies"] = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE business_id = ?", (biz_id,)
    ).fetchone()[0]
    counts["mapp_records"] = conn.execute(
        "SELECT COUNT(*) FROM mapp_records WHERE company_id IN "
        "(SELECT id FROM companies WHERE business_id = ?)",
        (biz_id,),
    ).fetchone()[0]
    counts["web_subdomains_total"] = conn.execute(
        "SELECT COUNT(*) FROM web_subdomains WHERE business_id = ?", (biz_id,)
    ).fetchone()[0]
    counts["web_subdomains_active"] = conn.execute(
        "SELECT COUNT(*) FROM web_subdomains WHERE business_id = ? AND is_active = 1",
        (biz_id,),
    ).fetchone()[0]
    counts["tcp_assets_total"] = conn.execute(
        "SELECT COUNT(*) FROM tcp_assets WHERE business_id = ?", (biz_id,)
    ).fetchone()[0]
    counts["tcp_assets_active"] = conn.execute(
        "SELECT COUNT(*) FROM tcp_assets WHERE business_id = ? AND is_active = 1",
        (biz_id,),
    ).fetchone()[0]
    counts["scopes"] = conn.execute(
        "SELECT COUNT(*) FROM scopes WHERE business_id = ?", (biz_id,)
    ).fetchone()[0]
    counts["web_hashes"] = conn.execute(
        "SELECT COUNT(*) FROM web_hashes WHERE business_id = ?", (biz_id,)
    ).fetchone()[0]

    cfg = conn.execute(
        "SELECT enabled, web, tcp, icp FROM recon_business_config WHERE business_id = ?",
        (biz_id,),
    ).fetchone()
    if cfg is None:
        config = {"enabled": None, "web": None, "tcp": None, "icp": None}
    else:
        config = {"enabled": cfg[0], "web": cfg[1], "tcp": cfg[2], "icp": cfg[3]}

    return {
        "found": True,
        "business": biz,
        "counts": counts,
        "config": config,
    }


def _q_read_subdomains(conn, args):
    name = args.get("business")
    if not name:
        return {"error": {"code": -32602, "message": "missing required --business"}}
    since = args.get("since")
    limit = _clamp_limit(args)
    biz_id, biz_name = _lookup_business_id(conn, name)
    if biz_id is None:
        return {"found": False, "business_name": name, "rows": []}
    params = [biz_id]
    sql = (
        "SELECT id, subdomain, port, url, status_code, content_length, title, "
        "technologies, first_seen, last_seen, fetched_at, is_active, change_type "
        "FROM web_subdomains WHERE business_id = ?"
    )
    if since:
        sql += " AND last_seen >= ?"
        params.append(since)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {
        "found": True,
        "business": {"id": biz_id, "business_name": biz_name},
        "row_count": len(rows),
        "limit": limit,
        "since": since,
        "rows": _row_to_dicts(rows, DB_COLS_SUBDOMAINS),
    }


def _q_read_open_ports(conn, args):
    name = args.get("business")
    if not name:
        return {"error": {"code": -32602, "message": "missing required --business"}}
    limit = _clamp_limit(args)
    biz_id, biz_name = _lookup_business_id(conn, name)
    if biz_id is None:
        return {"found": False, "business_name": name, "rows": []}
    rows = conn.execute(
        "SELECT id, ip, port, first_seen, last_seen, fetched_at, is_active, "
        "raw_value, hosts, change_type "
        "FROM tcp_assets WHERE business_id = ? ORDER BY last_seen DESC LIMIT ?",
        (biz_id, limit),
    ).fetchall()
    return {
        "found": True,
        "business": {"id": biz_id, "business_name": biz_name},
        "row_count": len(rows),
        "limit": limit,
        "rows": _row_to_dicts(rows, DB_COLS_OPEN_PORTS),
    }


def _q_read_companies(conn, args):
    name = args.get("business")
    if not name:
        return {"error": {"code": -32602, "message": "missing required --business"}}
    limit = _clamp_limit(args)
    biz_id, biz_name = _lookup_business_id(conn, name)
    if biz_id is None:
        return {"found": False, "business_name": name, "rows": []}
    rows = conn.execute(
        "SELECT id, unit_name, nature_name, main_licence, created_at, updated_at, "
        "change_type FROM companies WHERE business_id = ? ORDER BY id LIMIT ?",
        (biz_id, limit),
    ).fetchall()
    return {
        "found": True,
        "business": {"id": biz_id, "business_name": biz_name},
        "row_count": len(rows),
        "limit": limit,
        "rows": _row_to_dicts(rows, DB_COLS_COMPANIES),
    }


def _q_read_diff(conn, args):
    name = args.get("business")
    if not name:
        return {"error": {"code": -32602, "message": "missing required --business"}}
    limit = _clamp_limit(args)
    biz_id, biz_name = _lookup_business_id(conn, name)
    if biz_id is None:
        return {"found": False, "business_name": name, "rows": []}

    rows = conn.execute(
        "SELECT subdomain, port, change_type, last_seen, is_active "
        "FROM web_subdomains WHERE business_id = ? AND change_type != 0 "
        "ORDER BY last_seen DESC LIMIT ?",
        (biz_id, limit),
    ).fetchall()

    return {
        "found": True,
        "business": {"id": biz_id, "business_name": biz_name},
        "row_count": len(rows),
        "limit": limit,
        "rows": [
            {
                "subdomain": r[0], "port": r[1], "change_type": r[2],
                "last_seen": r[3], "is_active": r[4],
            }
            for r in rows
        ],
    }


def _q_read_single_subdomain(conn, args):
    name = args.get("business")
    subdomain = args.get("subdomain")
    if not (name and subdomain):
        return {"error": {"code": -32602, "message": "missing required --business/--subdomain"}}
    biz_id, biz_name = _lookup_business_id(conn, name)
    if biz_id is None:
        return {"found": False, "business_name": name, "rows": []}
    rows = conn.execute(
        "SELECT id, subdomain, port, url, status_code, content_length, title, "
        "technologies, first_seen, last_seen, fetched_at, is_active, change_type, raw_json "
        "FROM web_subdomains WHERE business_id = ? AND subdomain = ? ORDER BY last_seen DESC",
        (biz_id, subdomain),
    ).fetchall()
    return {
        "found": True,
        "business": {"id": biz_id, "business_name": biz_name},
        "subdomain": subdomain,
        "row_count": len(rows),
        "rows": _row_to_dicts(rows, DB_COLS_SINGLE_SUBDOMAIN),
    }


# Registry of db.* tool handlers. Each entry is invoked under self.lock.
DB_QUERIES: dict[str, callable] = {
    "db.read_business_summary":  _q_read_business_summary,
    "db.read_subdomains":        _q_read_subdomains,
    "db.read_open_ports":        _q_read_open_ports,
    "db.read_companies":         _q_read_companies,
    "db.read_diff":              _q_read_diff,
    "db.read_single_subdomain":  _q_read_single_subdomain,
}


# Resolve the top-level srcradar binary relative to this file. Lives at
# <repo>/modules/public/srcradar-mcp-server/daemon.py; the binary sits at <repo>/srcradar.
def _default_srcradar_bin() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "..", "srcradar"))


class Daemon:
    def __init__(self, db_path: str, whitelist_path: str, scripts_root: str, srcradar_bin: str | None = None):
        self.db_path = db_path
        self.whitelist_path = whitelist_path
        self.scripts_root = scripts_root
        self.srcradar_bin = srcradar_bin or _default_srcradar_bin()
        self.conn = _open_ro(db_path)
        # Compile the per-tool SQL once: sqlite3 caches prepared statements
        # inside the connection; we additionally validate each handler with
        # a "SELECT 0 WHERE 0" warm-up so the first real call is hot.
        self.stmt_cache: dict[str, object] = {}
        self.lock = threading.Lock()
        self.tools = self._load_whitelist(whitelist_path)
        self._warm_db_queries()
        # Hygiene sweep on startup: drop staged uploads older than TTL.
        # Failures are non-fatal -- a broken tmpfs shouldn't prevent
        # the daemon from serving.
        try:
            removed = self._sweep_staged()
            if removed:
                sys.stderr.write(f"[srcradar-mcp] sweep removed {removed} stale staged entries\n")
        except OSError as exc:
            sys.stderr.write(f"[srcradar-mcp] sweep failed: {exc}\n")

    # ------------------------------------------------------------------
    # Whitelist is the single source of truth for tool resolution.
    # ------------------------------------------------------------------
    @staticmethod
    def _load_whitelist(path: str) -> list:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "tools" not in data or not isinstance(data["tools"], list):
            raise ValueError(f"whitelist at {path} must be {{'tools':[...]}}")
        seen = set()
        for t in data["tools"]:
            name = t.get("name")
            auth = t.get("auth")
            path_v = t.get("path")
            if name in seen:
                raise ValueError(f"duplicate tool name in whitelist: {name}")
            seen.add(name)
            if auth not in ("none", "e1", "e1-confirmed"):
                raise ValueError(f"invalid auth for {name}: {auth}")
            # stage_file / stage_dir are in-process tools; their "path"
            # is a sentinel name (e.g. "_stage_file"), not a script path.
            # Accept any non-empty string for those -- _dispatch routes them
            # before any path resolution runs.
            if name in ("stage_file", "stage_dir"):
                if not isinstance(path_v, str) or not path_v.strip():
                    raise ValueError(f"invalid path for {name}: {path_v!r}")
                continue
            if not isinstance(path_v, str) or not path_v.strip():
                raise ValueError(f"invalid path for {name}: {path_v!r}")
        return data["tools"]

    def _warm_db_queries(self) -> None:
        """Prime the per-connection statement cache by touching each db.* tool.

        We invoke each handler with a benign args dict (--business=__warmup__)
        that misses the business lookup. The result is discarded; the
        statement cache inside sqlite3 is now primed for the real traffic.
        """
        warmup_args = {"business": "__warmup__"}
        with self.lock:
            for name, handler in DB_QUERIES.items():
                # Each handler does its own validation; on -business missing
                # some return errors, which we ignore - we only care that the
                # SQL strings got compiled and cached.
                try:
                    handler(self.conn, warmup_args)
                except sqlite3.Error as exc:
                    sys.stderr.write(f"WARMUP: {name} raised: {exc}\n")
                self.stmt_cache[name] = handler

    def tool_record(self, name: str) -> dict | None:
        for t in self.tools:
            if t["name"] == name:
                return t
        return None

    def allowed(self, name: str) -> bool:
        return self.tool_record(name) is not None

    def auth_for(self, name: str) -> str | None:
        rec = self.tool_record(name)
        return rec["auth"] if rec else None

    def tool_path(self, name: str) -> str | None:
        rec = self.tool_record(name)
        return rec.get("path") if rec else None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def dispatch_db(self, name: str, args: dict) -> dict:
        handler = DB_QUERIES.get(name)
        if handler is None:
            return {"error": {"code": -32011, "message": f"no db handler for {name}"}}
        with self.lock:
            try:
                return handler(self.conn, args)
            except sqlite3.Error as exc:
                return {"error": {"code": -32017, "message": f"db error: {exc}"}}

    def dispatch_subprocess(self, name: str, args: dict, timeout: float = 30.0) -> dict:
        argv, err = self._dispatch_argv(name, args)
        if err:
            return {"error": {"code": -32013, "message": err}}
        # Optional stdin forwarding. The caller passes either a literal
        # ``stdin_file`` (absolute path; we forward as-is, daemon never
        # reads the bytes) or ``stdin`` (string content). In both cases
        # we materialise to a temp file and set STDIN_FILE_PATH so the
        # dispatched script can read it deterministically. The temp file
        # is cleaned up in a finally so a SIGKILL on the subprocess
        # doesn't leak it indefinitely.
        stdin_path, stdin_cleanup, stdin_err = self._resolve_stdin(args)
        if stdin_err:
            return {"error": {"code": -32018, "message": stdin_err}}
        # When stdin was materialised, the dispatched script may either:
        #   - read $STDIN_FILE_PATH itself (newly-written scripts), or
        #   - read sys.stdin the old way (legacy scripts that came in
        #     through shell redirect "< companies.txt" before the MCP
        #     path existed).
        # To support both at once we set the env var AND pipe the file
        # contents to proc.stdin. This way sys.stdin.read() in the
        # child sees the same payload as opening $STDIN_FILE_PATH.
        stdin_bytes = None
        run_env = None
        if stdin_path is not None:
            run_env = {**os.environ, "STDIN_FILE_PATH": stdin_path}
            if stdin_cleanup:  # we wrote it, so we own the bytes
                # subprocess.run(..., text=True, input=...) treats input
                # as text and tries to .encode() it; passing bytes would
                # raise AttributeError. Read as text so the chain stays
                # consistent.
                with open(stdin_path, "r", encoding="utf-8") as _fh:
                    stdin_bytes = _fh.read()
        try:
            if stdin_bytes is None:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=timeout,
                    check=False, env=run_env,
                )
            else:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=timeout,
                    check=False, env=run_env, input=stdin_bytes,
                )
        except subprocess.TimeoutExpired:
            if stdin_cleanup:
                stdin_cleanup()
            return {"error": {"code": -32014, "message": "subprocess timeout"}}
        try:
            if proc.returncode != 0:
                return {
                    "error": {
                        "code": -32015,
                        "message": f"subprocess exit {proc.returncode}: {proc.stderr.strip()[:500]}",
                    }
                }
            out = proc.stdout.strip()
            if not out:
                return {"status": "ok", "stdout": "", "rc": proc.returncode}
            try:
                parsed = json.loads(out.splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                return {
                    "status": "ok",
                    "stdout": out,
                    "rc": proc.returncode,
                    "format": "text",
                }
            return parsed
        finally:
            if stdin_cleanup:
                stdin_cleanup()

    @staticmethod
    def _resolve_stdin(args: dict) -> tuple[str | None, Optional[Callable[[], None]], Optional[str]]:
        """Materialise caller-supplied stdin into a temp file path.

        Accepts two shapes from the JSON-RPC args dict:

          - ``stdin_file``: absolute path on the daemon's filesystem
            (typically v1); forwarded verbatim. We never read it, the
            dispatched script opens it directly.
          - ``stdin``: string content; written to a NamedTemporaryFile
            so the dispatched script sees a stable path.

        Returns ``(path, cleanup_callable, error_message)``. ``path``
        is ``None`` when the caller did not supply either shape; in
        that case ``cleanup_callable`` is also ``None`` and
        ``error_message`` is ``None``.

        Why a temp file and not ``proc.communicate(input=...)``:
          - ARG_MAX caps argv but not stdin, so this is the right
            channel for variable-size payloads.
          - The script on the other end reads ``$STDIN_FILE_PATH``
            instead of ``/dev/stdin``; this avoids subtle behaviour
            differences between a tty, a pipe, and a redirected file
            when the script is spawned via subprocess.run.
        """
        if not isinstance(args, dict):
            return None, None, None
        explicit_path = args.get("stdin_file")
        if explicit_path:
            if not isinstance(explicit_path, str) or not os.path.isabs(explicit_path):
                return None, None, "stdin_file must be an absolute path"
            if not os.path.isfile(explicit_path) or not os.access(explicit_path, os.R_OK):
                return None, None, f"stdin_file not readable: {explicit_path}"
            return explicit_path, None, None  # caller owns the file; no cleanup
        content = args.get("stdin")
        if content is None:
            return None, None, None
        if not isinstance(content, str):
            return None, None, "stdin must be a string"
        # Materialise to a temp file the script can read. We use
        # delete=False so we control cleanup precisely (named tempfile
        # keeps the path stable across the subprocess.run boundary).
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="srcradar-mcp-stdin-",
            suffix=".txt", delete=False,
        )
        try:
            tmp.write(content)
            tmp.flush()
        finally:
            tmp.close()
        path = tmp.name

        def _cleanup(path=path):
            try:
                os.unlink(path)
            except OSError:
                pass

        return path, _cleanup, None

    @staticmethod
    def _split_path(path_v: str) -> tuple[str, str]:
        path_v = path_v.strip()
        if " " in path_v:
            head, _, rest = path_v.partition(" ")
            return head, rest.strip()
        return path_v, ""

    def _resolve_binary(self, head: str) -> str:
        if head.startswith("./"):
            return self.srcradar_bin
        return os.path.join(self.scripts_root, head)

    def _dispatch_argv(self, name: str, args: dict) -> tuple[list | None, str | None]:
        rec = self.tool_record(name)
        if rec is None:
            return None, f"tool not in whitelist: {name}"
        # db.* tools are served in-process via DB_QUERIES and never reach
        # this method. Only dispatcher.* / srcradar-bound tools use the
        # subprocess dispatcher.
        head, rest = self._split_path(rec["path"])
        binary = self._resolve_binary(head)
        base_argv = [sys.executable, binary] if not head.startswith("./") else [binary]

        # dispatcher.* / future srcradar-bound tools
        tokens = shlex.split(rest) if rest else []
        free_args: list[str] = []
        if isinstance(args, dict) and "args" in args:
            v = args["args"]
            if isinstance(v, list):
                free_args = [str(x) for x in v]
            elif isinstance(v, str):
                free_args = shlex.split(v)
        if "<args>" in tokens:
            idx = tokens.index("<args>")
            tokens = tokens[:idx] + free_args + tokens[idx + 1:]
        else:
            tokens = tokens + free_args
        return base_argv + tokens, None

    # ------------------------------------------------------------------
    # stage_file / stage_dir (in-process, no subprocess)
    # ------------------------------------------------------------------
    def _stage_root_dir(self) -> str:
        os.makedirs(STAGED_ROOT, exist_ok=True)
        return STAGED_ROOT

    def _stage_file(self, args: dict) -> dict:
        """Materialise client-supplied bytes to STAGED_ROOT/<id>.<ext>.

        Input modes (one of):
          - stdin:       inline string content (LLM-generated)
          - base64:      base64-encoded bytes from the client
          - file_path + base64:
                          client declares which local path it read
                          (recorded for audit; daemon never reads
                          the client filesystem)

        Returns:
          {kind: "file", staged_path, upload_id, size_bytes,
           created_at, ttl_seconds, source, source_path?}

        On error returns the standard {error:{code,message}} envelope
        so callers can rely on the existing JSON-RPC error path.
        """
        if not isinstance(args, dict):
            return {"error": {"code": -32602, "message": "params.args must be object"}}
        ok, fn_or_err = _validate_filename(args.get("filename"))
        if not ok:
            return {"error": {"code": -32602, "message": fn_or_err}}
        filename = fn_or_err
        stdin_v = args.get("stdin")
        base64_v = args.get("base64")
        file_path_v = args.get("file_path")
        present = [n for n, v in (("stdin", stdin_v), ("base64", base64_v),
                                   ("file_path+base64", file_path_v and base64_v)) if v]
        if "file_path" in args and not base64_v:
            return {"error": {"code": -32602,
                "message": "file_path MUST be paired with base64 (file_path is audit-only)"}}
        if not present:
            return {"error": {"code": -32602,
                "message": "one of stdin / base64 / file_path+base64 required"}}
        if len(present) > 1 and not (file_path_v and base64_v):
            return {"error": {"code": -32602,
                "message": "stdin and base64 are mutually exclusive"}}
        # Decode bytes.
        try:
            if stdin_v is not None:
                raw = stdin_v.encode("utf-8")
                source = "stdin"
            else:
                raw = base64.b64decode(base64_v, validate=True)
                source = "file_path+base64" if file_path_v else "base64"
        except (binascii.Error, ValueError, TypeError) as exc:
            return {"error": {"code": -32602, "message": f"base64 decode failed: {exc}"}}
        if len(raw) > STAGE_MAX_BYTES:
            return {"error": {"code": -32602,
                "message": f"payload too large: {len(raw)} > {STAGE_MAX_BYTES}"}}
        upload_id = _gen_upload_id()
        staged_dir = self._stage_root_dir()
        staged_path = os.path.join(staged_dir, upload_id + "." + filename)
        # Defence-in-depth: even though _validate_filename rejects ".." and
        # path separators, an attacker that bypassed the JSON-schema gate
        # could still try. Resolve and assert the path stays under root.
        real_root = os.path.realpath(staged_dir)
        real_path = os.path.realpath(staged_path)
        if os.path.commonpath([real_root, real_path]) != real_root:
            return {"error": {"code": -32602, "message": "staged_path escapes STAGED_ROOT"}}
        try:
            with open(staged_path, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            return {"error": {"code": -32018, "message": f"write failed: {exc}"}}
        out = {
            "kind": "file",
            "staged_path": staged_path,
            "upload_id": upload_id,
            "size_bytes": len(raw),
            "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "ttl_seconds": STAGE_TTL_SECONDS,
            "source": source,
        }
        if file_path_v:
            out["source_path"] = file_path_v
        return out

    def _stage_dir(self, args: dict) -> dict:
        """Materialise a directory tree under STAGED_ROOT/<id>/.

        Two modes:
          - base64:    tar.gz encoded as base64
          - entries:   [{filename, content_b64}, ...] (flat only)

        Returns:
          {kind: "dir", staged_dir, upload_id, file_count, total_bytes,
           created_at, ttl_seconds, source}
        """
        if not isinstance(args, dict):
            return {"error": {"code": -32602, "message": "params.args must be object"}}
        base64_v = args.get("base64")
        entries = args.get("entries")
        if (base64_v is None) == (not entries):
            return {"error": {"code": -32602,
                "message": "exactly one of base64 (tar.gz) or entries is required"}}
        upload_id = _gen_upload_id()
        staged_root = self._stage_root_dir()
        staged_dir = os.path.join(staged_root, upload_id)
        real_root = os.path.realpath(staged_root)
        real_dir = os.path.realpath(staged_dir)
        if os.path.commonpath([real_root, real_dir]) != real_root:
            return {"error": {"code": -32602, "message": "staged_dir escapes STAGED_ROOT"}}
        try:
            os.makedirs(staged_dir, exist_ok=False)  # fail if already exists
        except FileExistsError:
            return {"error": {"code": -32018, "message": "upload_id collision (retry)"}}
        except OSError as exc:
            return {"error": {"code": -32018, "message": f"mkdir failed: {exc}"}}
        file_count = 0
        total_bytes = 0
        try:
            if base64_v is not None:
                try:
                    raw = base64.b64decode(base64_v, validate=True)
                except (binascii.Error, ValueError, TypeError) as exc:
                    return {"error": {"code": -32602, "message": f"base64 decode failed: {exc}"}}
                if len(raw) > STAGE_MAX_BYTES:
                    return {"error": {"code": -32602,
                        "message": f"archive too large: {len(raw)} > {STAGE_MAX_BYTES}"}}
                try:
                    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                        # Defence: reject absolute paths and parent traversal
                        for member in tar.getmembers():
                            member_path = os.path.realpath(os.path.join(staged_dir, member.name))
                            if (os.path.commonpath([real_root, member_path]) != real_root
                                    or member.name.startswith(("/", "\\"))
                                    or ".." in member.name.split("/")):
                                return {"error": {"code": -32602,
                                    "message": f"unsafe entry in archive: {member.name!r}"}}
                        tar.extractall(path=staged_dir)
                        file_count = len(tar.getnames())
                except tarfile.TarError as exc:
                    return {"error": {"code": -32602, "message": f"tar.gz parse failed: {exc}"}}
                source = "base64-archive"
            else:
                if not isinstance(entries, list) or not entries:
                    return {"error": {"code": -32602,
                        "message": "entries must be a non-empty list"}}
                for entry in entries:
                    if not isinstance(entry, dict):
                        return {"error": {"code": -32602,
                            "message": "each entry must be an object"}}
                    ok, fn_or_err = _validate_filename(entry.get("filename"))
                    if not ok:
                        return {"error": {"code": -32602, "message": fn_or_err}}
                    content_b64 = entry.get("content_b64")
                    if not isinstance(content_b64, str):
                        return {"error": {"code": -32602,
                            "message": f"entry {fn_or_err!r}: content_b64 must be string"}}
                    try:
                        raw_e = base64.b64decode(content_b64, validate=True)
                    except (binascii.Error, ValueError, TypeError) as exc:
                        return {"error": {"code": -32602,
                            "message": f"entry {fn_or_err!r}: base64 decode failed: {exc}"}}
                    if len(raw_e) > STAGE_MAX_BYTES:
                        return {"error": {"code": -32602,
                            "message": f"entry {fn_or_err!r}: too large"}},
                    dest = os.path.join(staged_dir, fn_or_err)
                    real_dest = os.path.realpath(dest)
                    if os.path.commonpath([real_root, real_dest]) != real_root:
                        return {"error": {"code": -32602,
                            "message": f"entry {fn_or_err!r}: escapes STAGED_ROOT"}}
                    with open(dest, "wb") as fh:
                        fh.write(raw_e)
                    file_count += 1
                    total_bytes += len(raw_e)
                source = "entries"
        except Exception:
            # Best-effort cleanup of half-written staged dir
            try:
                shutil.rmtree(staged_dir, ignore_errors=True)
            except OSError:
                pass
            raise
        return {
            "kind": "dir",
            "staged_dir": staged_dir,
            "upload_id": upload_id,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "ttl_seconds": STAGE_TTL_SECONDS,
            "source": source,
        }

    @staticmethod
    def _sweep_staged(max_age_seconds: int = STAGE_TTL_SECONDS) -> int:
        """Remove staged uploads older than the TTL. Idempotent.

        Returns the number of entries removed. Runs on daemon startup and
        can be invoked manually via the operator-facing health module if
        needed (see modules/public/health/lib/health_check.py for the
        observe-only check variant).
        """
        if not os.path.isdir(STAGED_ROOT):
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in os.listdir(STAGED_ROOT):
            full = os.path.join(STAGED_ROOT, entry)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
                removed += 1
            except OSError as exc:
                sys.stderr.write(f"SWEEP: failed to remove {full}: {exc}\n")
        return removed

    @staticmethod
    def _cleanup_staging_ref(staged_root: str, upload_id: str) -> bool:
        """Delete the staged file or dir matching <upload_id>.* / <upload_id>/.

        Returns True if anything was removed. Safe to call with an
        unknown id (no-op).
        """
        if not upload_id or not isinstance(upload_id, str):
            return False
        if not re.fullmatch(r"[0-9a-f]{16}", upload_id):
            return False  # reject non-hex-16 ids; never glob user input
        removed = False
        dir_path = os.path.join(staged_root, upload_id)
        if os.path.isdir(dir_path) and not os.path.islink(dir_path):
            try:
                shutil.rmtree(dir_path)
                removed = True
            except OSError as exc:
                sys.stderr.write(f"STAGE_CLEAN: rmtree failed {dir_path}: {exc}\n")
        # Also sweep any file with this upload_id as stem (stage_file).
        for entry in os.listdir(staged_root) if os.path.isdir(staged_root) else []:
            full = os.path.join(staged_root, entry)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            stem, dot, ext = entry.partition(".")
            if stem == upload_id:
                try:
                    os.unlink(full)
                    removed = True
                except OSError as exc:
                    sys.stderr.write(f"STAGE_CLEAN: unlink failed {full}: {exc}\n")
        return removed

    def _dispatch(self, name: str, args: dict) -> dict:
        """Pick the right dispatcher (db.* / stage.* in-process vs subprocess)."""
        if name == "stage_file":
            return self._stage_file(args)
        if name == "stage_dir":
            return self._stage_dir(args)
        if name.startswith("db."):
            # CLI test mode passes flat --key val pairs (already translated
            # into dict by _parse_cli_test_args). JSON-RPC path already has
            # args as a dict.
            if name in DB_QUERIES:
                return self.dispatch_db(name, args or {})
            return {"error": {"code": -32011, "message": f"no db handler for {name}"}}
        # dispatcher.* / non-db tools -> subprocess
        return self.dispatch_subprocess(name, args)

    def cache_stmt(self, key: str, sql: str) -> object:
        # Legacy API retained for compatibility; the daemon now uses
        # DB_QUERIES directly. Each entry is a callable bound to the conn.
        with self.lock:
            stmt = self.stmt_cache.get(key)
            if stmt is None:
                stmt = self.conn.execute("SELECT 0 WHERE 0;")
                self.stmt_cache[key] = stmt
            return stmt

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------
    def _err(self, code: int, message: str, request_id=None) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _ok(self, request_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle_request(self, raw: str) -> dict | None:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._err(-32700, f"parse error: {exc}", None)
        if not isinstance(req, dict):
            return self._err(-32600, "invalid request object", None)
        request_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not method:
            return self._err(-32600, "missing method", request_id)
        # Spec aliases: bridges "tools/list" (slash, MCP 2026-07-28)
        # and "tools/call" (slash) to the legacy dot namespace used
        # internally. The slash form names the arguments field "arguments";
        # the dot form uses "args". Translate so callers may use either.
        if method == "tools/list":
            method = "tools.list"
        elif method == "tools/call":
            method = "tools.invoke"
            if isinstance(params, dict) and "arguments" in params and "args" not in params:
                params = {**params, "args": params["arguments"]}
        try:
            if method == "tools.list":
                # MCP 2026-07-28 ListToolsResult: each tool must carry
                # title / description / inputSchema / annotations. The
                # whitelist row is the source of truth for auth gating;
                # tools_schema.py owns the human-readable metadata so
                # daemon.py stays focused on dispatch.
                auth_for = {t["name"]: t["auth"] for t in self.tools}
                return self._ok(request_id, list_tools_payload(auth_for))
            if method == "tools.invoke":
                # Wrap the internal dispatch result into an MCP 2026-07-28
                # CallToolResult envelope. Without this, hermes (and any
                # spec-compliant client) rejects the response with
                # "ValidationError: 3 validation errors for union[CallToolResult,
                # InputRequiredResult]" because content/resultType are missing.
                return self._ok(
                    request_id,
                    self._wrap_call_result(self._invoke(params, request_id)),
                )
            if method == "initialize":
                return self._ok(request_id, self._initialize(params))
            if method == "notifications/initialized":
                # Per spec, a no-op acknowledgement: client is telling us
                # the handshake is complete. The HTTP layer turns the
                # notification into 202 Accepted regardless of Accept.
                return self._ok(request_id, {})
            if method == "daemon.ping":
                # db_path was previously echoed here, which leaked the
                # absolute path of whichever sqlite the daemon was
                # bound to. With the new default (module-local
                # sandbox), the path is no longer sensitive, but we
                # still drop it from the wire response so future
                # production opt-ins via RECON_DB_PATH don't
                # accidentally re-leak.
                return self._ok(request_id, {"pong": True})
            return self._err(-32601, f"method not found: {method}", request_id)
        except Exception as exc:  # noqa: BLE001 - top-level error boundary for JSON-RPC
            return self._err(-32000, f"server error: {exc}\n{traceback.format_exc()}", request_id)

    def _initialize(self, params: dict) -> dict:
        """MCP 2026-07-28 standard `initialize` response.

        The HTTP envelope gate has already enforced MCP-Protocol-Version
        == "2026-07-28" for non-server/discover traffic, so reaching this
        method implies compatibility. We echo the client's protocolVersion
        back so the client can confirm we negotiated correctly.

        Capabilities mirror server/discover: tools.listChanged is False
        because the whitelist is read at startup; resources and prompts
        are not implemented (empty dicts, not None).
        """
        if not isinstance(params, dict):
            params = {}
        client_version = params.get("protocolVersion", "2026-07-28")
        client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        return {
            "protocolVersion": client_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
                "prompts": {},
            },
            "serverInfo": {
                "name": "srcradar-mcp",
                "version": "0.1.0",
            },
            "instructions": (
                "Read-only MCP tools over a srcradar SQLite database. "
                "13 whitelisted tools; no write tools are advertised."
            ),
            # Echo client info for debugging. Loopback-only auth model:
            # we do not persist anything from this handshake.
            "_meta": {
                "clientEcho": {
                    "name": client_info.get("name", ""),
                    "version": client_info.get("version", ""),
                    "negotiatedVersion": client_version,
                },
            },
        }

    @staticmethod
    def _wrap_call_result(internal):
        """Wrap an internal dispatch result into an MCP 2026-07-28 CallToolResult.

        The dispatch layer (`dispatch_db`, `dispatch_subprocess`) and the
        e1/e1-confirmed gate in `_invoke` return plain dicts that pre-date
        the MCP 2026-07-28 spec; they keep the existing keys (`status`,
        `stdout`, `rc`, `found`, `error`, etc.) so `--self-test` and any
        future tooling that inspects the daemon directly still works.

        On the wire we need a CallToolResult:
          { resultType: "complete",
            content: [{type: "text", text: ...}],
            structuredContent?: <dict>,
            isError?: bool }

        Strategy:
          - Tool-level error (`internal.error`) -> isError=True, content
            describes the error message.
          - Anything else (success dict, plain JSON, text subprocess
            output) -> content carries a JSON dump of the full internal
            dict so callers always get a non-empty payload, and
            structuredContent mirrors the same dict for machines that
            prefer the JSON object directly.
        """
        # 1. Error envelope: dispatched from dispatch_db / dispatch_subprocess
        #    via {"error": {"code": -320xx, "message": "..."}}.
        if isinstance(internal, dict) and "error" in internal:
            err = internal["error"] if isinstance(internal["error"], dict) else {}
            message = err.get("message") or "tool error"
            return {
                "resultType": "complete",
                "isError": True,
                "content": [{"type": "text", "text": message}],
                "structuredContent": internal,
            }

        # 2. Successful dispatch. Normalise into a single content block.
        #    Anything that json.dumps can serialise is fine; we don't try
        #    to be clever about typing since hermes renders the text block.
        try:
            text_payload = json.dumps(internal, ensure_ascii=False)
        except (TypeError, ValueError):
            text_payload = str(internal)

        # structuredContent must itself be a JSON object (spec). If the
        # internal payload is a dict, mirror it verbatim; otherwise drop
        # structuredContent rather than send an invalid primitive.
        structured = internal if isinstance(internal, dict) else None

        result = {
            "resultType": "complete",
            "content": [{"type": "text", "text": text_payload}],
        }
        if structured is not None:
            result["structuredContent"] = structured
        return result

    def _invoke(self, params: dict, request_id):
        if not isinstance(params, dict):
            return {"error": {"code": -32602, "message": "params must be object"}}
        name = params.get("name")
        args = params.get("args") or {}
        if not name:
            return {"error": {"code": -32602, "message": "missing tool name"}}
        if not self.allowed(name):
            return {"error": {"code": -32010, "message": f"tool not in whitelist: {name}"}}
        # NOTE: e1 / e1-confirmed gates removed 2026-09-26. Approval is
        # now handled by the MCP client (Hermes trust: untrusted +
        # readOnlyHint annotations in tools_schema.py). The daemon no
        # longer raises InputRequiredResult -- it dispatches whatever
        # passes the whitelist + loopback gate. The auth field in
        # whitelist.json is retained as documentation only.
        #
        # stage_file / stage_dir do their own per-call cleanup; for any
        # other tool, an optional `_staging_ref` field at the top level
        # of params tells the daemon to clean up a previously staged
        # upload once this tool returns.
        staging_ref = params.get("_staging_ref")
        try:
            return self._dispatch(name, args)
        finally:
            if isinstance(staging_ref, str) and staging_ref:
                try:
                    self._cleanup_staging_ref(STAGED_ROOT, staging_ref)
                except OSError as exc:
                    sys.stderr.write(f"[srcradar-mcp] staging_ref cleanup failed: {exc}\n")

    @staticmethod
    def _user_confirmed(params: dict) -> bool:
        """Read the multiple confirmation shapes an MCP client may carry.

        Returns True if the operator has explicitly confirmed this call in
        either the legacy (`confirmed=true`) or MRTR
        (`inputResponses.confirm.content.confirmed=true`) shape. Anything
        else (missing field, wrong type, decline/cancel) is False.
        """
        if not isinstance(params, dict):
            return False
        if params.get("confirmed") is True:
            return True
        responses = params.get("inputResponses")
        if not isinstance(responses, dict):
            return False
        confirm = responses.get("confirm")
        if not isinstance(confirm, dict):
            return False
        if confirm.get("action") == "accept":
            content = confirm.get("content")
            if isinstance(content, dict) and content.get("confirmed") is True:
                return True
            # action=accept alone is treated as confirmation; client must
            # not send accept without content for form-mode but be lenient.
            if content is None:
                return True
        if isinstance(confirm.get("content"), dict) and            confirm["content"].get("confirmed") is True:
            return True
        return False

    # ------------------------------------------------------------------
    # Self-test
    # ------------------------------------------------------------------
    def self_test(self) -> dict:
        results = {"steps": []}
        results["steps"].append({"step": "whitelist_load", "tool_count": len(self.tools)})
        results["steps"].append({"step": "db_query_registry", "registered": list(DB_QUERIES.keys())})
        resp = self.handle_request(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools.list"}))
        assert resp and "result" in resp and "tools" in resp["result"], resp
        results["steps"].append({"step": "tools.list", "tool_count": len(resp["result"]["tools"])})
        bad = self.handle_request(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools.invoke",
            "params": {"name": "no.such.tool", "args": {}}
        }))
        # After MCP 2026-07-28 wrapping, error payloads live under
        # result.structuredContent.error (CallToolResult body), not at
        # the JSON-RPC top level. Fall through every plausible shape so
        # self_test stays a faithful regression check.
        bad_result = (bad or {}).get("result", {}) if isinstance(bad, dict) else {}
        bad_sc = bad_result.get("structuredContent", {}) if isinstance(bad_result, dict) else {}
        bad_err = (
            (bad or {}).get("error")
            or bad_result.get("error")
            or bad_sc.get("error")
            or {}
        )
        assert bad_err.get("code") == -32010, bad
        results["steps"].append({"step": "reject_unknown_tool", "ok": True})
        # In-process dispatch check (e1 gate removed 2026-09-26): an
        # unknown-business read must NOT yield pending_confirmation.
        resp = self.handle_request(json.dumps({
            "jsonrpc": "2.0", "id": 5, "method": "tools.invoke",
            "params": {"name": "db.read_business_summary", "args": {"business": "NotExistCo"}}
        }))
        sc = resp["result"].get("structuredContent", {}) if isinstance(resp, dict) else {}
        assert sc.get("status") != "pending_confirmation", resp
        results["steps"].append({
            "step": "db.read_business_summary:notfound",
            "ok": isinstance(resp, dict) and sc.get("status") != "pending_confirmation",
            "response_keys": list((resp.get("result") or {}).keys()) if resp else None,
        })
        # Real db.read_subdomains round-trip on a known business
        biz = self.conn.execute("SELECT business_name FROM businesses LIMIT 1").fetchone()
        if biz:
            resp = self.handle_request(json.dumps({
                "jsonrpc": "2.0", "id": 6, "method": "tools.invoke",
                "params": {"name": "db.read_subdomains", "args": {"business": biz[0], "limit": 3}}
            }))
            res = resp.get("result") if resp else None
            results["steps"].append({
                "step": f"db.read_subdomains:{biz[0]}",
                "found": res.get("found") if res else None,
                "row_count": res.get("row_count") if res else None,
            })
        return results

    # ------------------------------------------------------------------
    # One-shot CLI test (--test TOOL [args])
    # ------------------------------------------------------------------
    def cli_test(self, tool: str, args: dict) -> dict:
        if tool == "tools.list":
            return {"tools": self.tools}
        if not self.allowed(tool):
            return {"error": {"code": -32010, "message": f"tool not in whitelist: {tool}"}}
        return self._dispatch(tool, args)

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001, S110 - defensive cleanup
            pass


def _build_handler(daemon: Daemon):
    """Return a BaseHTTPRequestHandler subclass bound to `daemon`.

    Streamable-HTTP transport (MCP spec revision 2026-07-28). Single JSON-RPC
    endpoint is POST /mcp; GET /health lives on its own path and is the only
    other route. Any other path or method -> 404 / 405 respectively. We do
    NOT keep a legacy POST /rpc route (2025-03-26 clients are out of scope).

    Envelope validation runs before the body is dispatched to
    daemon.handle_request. Order matters because each gate short-circuits
    the next:

      1. method routing      -> 405 if /mcp gets GET/DELETE/etc.
      2. Origin              -> 403 if present and not loopback.
      3. Accept              -> 406 if neither application/json nor
                                text/event-stream is offered.
      4. Content-Length/body -> 400/413 on missing or oversized body.
      5. body parse          -> -32700 parse error from handle_request.
      6. MCP-Protocol-Version-> -32022 UnsupportedProtocolVersionError.
      7. Mcp-Method / Mcp-Name header/body mismatch -> -32020
                                HeaderMismatch.

    Responses are framed per the request's Accept preference. If both
    JSON and SSE are offered we honour text/event-stream (the spec lets
    servers pick; a single-frame SSE is a valid response shape for a
    synchronous request). Notification frames (no `id`) get a bare 202
    Accepted regardless of Accept -- they have nothing to ship back.

    ThreadingHTTPServer dispatches each request on its own thread. db.*
    tools hit a single shared read-only sqlite3 connection -- concurrent
    reads are safe by sqlite3's own contract.
    """

    SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28",)
    LOOPBACK_ORIGINS = {"", "127.0.0.1", "::1", "localhost", "null"}

    class Handler(BaseHTTPRequestHandler):
        # server/discover result (MCP spec 2026-07-28). Returned verbatim
        # to any client that probes the protocol version before the
        # version gate fires. Kept as a class attribute so every request
        # handler shares one immutable dict; we cannot use a closure
        # variable of _build_handler because attribute lookups do not
        # cross the class boundary.
        _SERVER_DISCOVER_RESULT: ClassVar[dict] = {
            "resultType": "complete",
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
                "prompts": {},
            },
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "srcradar-mcp",
                    "version": "0.1.0",
                },
            },
            "instructions": (
                "MCP tools for srcradar: 6 read-only db.* queries plus "
                "2 staging primitives (stage_file / stage_dir) that "
                "materialise client-supplied bytes into a daemon-managed "
                "temp dir. 15 tools total. Write tools carry "
                "readOnlyHint=false so Hermes trust: untrusted will "
                "surface the native approval gate for them."
            ),
            "ttlMs": 3600000,
            "cacheScope": "public",
        }

        # Silence the default per-request stderr access log; the daemon's
        # startup banner + SshTunnel's debug output is already informative.
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API
            return

        # ------------------------------------------------------------------
        # Low-level writers
        # ------------------------------------------------------------------
        def _write_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_sse(self, status: int, payload: dict) -> None:
            # Single SSE frame + Connection: close. We don't stream (no
            # long-running tools yet); the framing is what the spec demands
            # so a future streamed response can drop in here.
            frame = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            body = frame.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _write_health(self) -> None:
            self._write_json(200, {"status": "ok", "daemon": "srcradar-mcp"})

        # ------------------------------------------------------------------
        # Envelope helpers (run before body dispatch)
        # ------------------------------------------------------------------
        def _is_loopback_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                # No Origin header at all -- loopback clients (curl, the
                # bundled SSH-tunnel workflow) commonly omit it. Spec only
                # gates when an Origin IS present.
                return True
            # Tolerate "Origin: null" (file:// or sandboxed iframes); the
            # spec treats it as a sentinel for "no origin".
            return origin.strip() in LOOPBACK_ORIGINS

        def _accept_prefers_sse(self) -> bool:
            accept = (self.headers.get("Accept") or "").lower()
            has_json = "application/json" in accept
            has_sse = "text/event-stream" in accept
            if not has_json and not has_sse:
                return False  # caller will turn this into 406
            # If both are offered, honour SSE (spec leaves the choice to the
            # server; both frames must carry the same payload anyway).
            return has_sse

        def _envelope_error(
            self,
            request_id,
            code: int,
            message: str,
            data: dict | None = None,
        ) -> dict:
            err: dict = {"code": code, "message": message}
            if data is not None:
                err["data"] = data
            return {"jsonrpc": "2.0", "id": request_id, "error": err}

        def _send_envelope(self, payload: dict, request_id, prefer_sse: bool) -> None:
            # Envelope errors are always JSON -- the client only framed the
            # request, not the failure mode.
            if "error" in payload:
                self._write_json(200, payload)
                return
            if prefer_sse:
                self._write_sse(200, payload)
                return
            self._write_json(200, payload)

        # ------------------------------------------------------------------
        # Routing
        # ------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            route = self.path.split("?", 1)[0]
            if route == "/health":
                self._write_health()
                return
            if route == "/mcp":
                # 2026-07-28 spec does not define GET /mcp (no SSE GET
                # listener); reject explicitly rather than 404 so clients
                # can tell the endpoint exists.
                self._write_json(405, {"error": "method not allowed"})
                return
            self._write_json(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib API
            route = self.path.split("?", 1)[0]
            if route == "/mcp":
                self._write_json(405, {"error": "method not allowed"})
                return
            self._write_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            route = self.path.split("?", 1)[0]
            if route != "/mcp":
                self._write_json(404, {"error": "not found"})
                return

            # 1. Origin gate (spec MUST). Empty Origin = loopback client,
            #    always allowed. Otherwise must be a loopback sentinel.
            if not self._is_loopback_origin():
                self._write_json(
                    403,
                    self._envelope_error(
                        None,
                        -32000,
                        "origin not allowed",
                    ),
                )
                return

            # 2. Accept gate. Must offer JSON and/or SSE; otherwise 406.
            accept = (self.headers.get("Accept") or "").lower()
            has_json = "application/json" in accept
            has_sse = "text/event-stream" in accept
            if not has_json and not has_sse:
                self._write_json(
                    406,
                    self._envelope_error(
                        None,
                        -32000,
                        "Accept must include application/json or text/event-stream",
                    ),
                )
                return
            prefer_sse = has_sse  # tie-break: SSE wins if both offered

            # 3. Body limits -- identical to the pre-streamable handler.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._write_json(
                    400,
                    self._envelope_error(
                        None,
                        -32700,
                        "invalid Content-Length",
                    ),
                )
                return
            if length <= 0 or length > 10 * 1024 * 1024:
                self._write_json(
                    413,
                    self._envelope_error(
                        None,
                        -32000,
                        "body too large or empty",
                    ),
                )
                return
            try:
                raw = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self._write_json(
                    400,
                    self._envelope_error(
                        None,
                        -32700,
                        "body must be utf-8",
                    ),
                )
                return

            # 4. Parse body. We need the request id for header/body match
            #    error responses, so we parse up front. handle_request
            #    would re-parse; that's harmless (same JSON, same answer).
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._write_json(
                    400,
                    self._envelope_error(
                        None,
                        -32700,
                        f"parse error: {exc}",
                    ),
                )
                return
            if not isinstance(req, dict):
                self._write_json(
                    400,
                    self._envelope_error(
                        None,
                        -32600,
                        "invalid request object",
                    ),
                )
                return
            request_id = req.get("id")
            body_method = req.get("method")
            if not isinstance(body_method, str) or not body_method:
                # Let handle_request emit -32600 with the parsed id; we
                # can't make a meaningful envelope check without a method.
                resp = daemon.handle_request(raw)
                if resp is None:
                    self._write_empty(202)
                    return
                self._send_envelope(resp, request_id, prefer_sse)
                return

            # 4.5. server/discover short-circuit. This is a meta-level
            #      method that exists to negotiate the protocol version,
            #      so it must be reachable BEFORE the version gate.
            #      We also bypass the Mcp-Method / Mcp-Name header checks
            #      because spec defines server/discover with no params
            #      and no Mcp-Name requirement. Notifications (no id) get
            #      202 Accepted; everything else returns the cached
            #      SERVER_DISCOVER result verbatim.
            if body_method == "server/discover":
                if request_id is None:
                    self._write_empty(202)
                    return
                self._send_envelope(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": self._SERVER_DISCOVER_RESULT,
                    },
                    request_id,
                    prefer_sse,
                )
                return

            # 5. Protocol version gate. MUST equal one of the versions we
            #    advertise; anything else (or missing) is -32022.
            proto_header = self.headers.get("MCP-Protocol-Version")
            if proto_header != SUPPORTED_PROTOCOL_VERSIONS[0]:
                self._write_json(
                    400,
                    self._envelope_error(
                        request_id,
                        -32022,
                        "UnsupportedProtocolVersionError",
                        {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
                    ),
                )
                return

            # 6. Mcp-Method header must equal body method. Spec names this
            #    -32020 HeaderMismatch.
            method_header = self.headers.get("Mcp-Method")
            if method_header != body_method:
                self._write_json(
                    400,
                    self._envelope_error(
                        request_id,
                        -32020,
                        "HeaderMismatch: Mcp-Method does not match body method",
                    ),
                )
                return

            # 7. Mcp-Name gate -- only enforced for tools.invoke. The spec
            #    wording is "tools/call"; we accept the MCP-shaped name
            #    "tools.invoke" because that's our JSON-RPC method id.
            if body_method == "tools.invoke":
                name_header = self.headers.get("Mcp-Name")
                params = req.get("params") or {}
                body_name = params.get("name") if isinstance(params, dict) else None
                if name_header != body_name:
                    self._write_json(
                        400,
                        self._envelope_error(
                            request_id,
                            -32020,
                            "HeaderMismatch: Mcp-Name does not match params.name",
                        ),
                    )
                    return

            # 8. Notification? No `id` means the client isn't waiting for
            #    a reply -- honour 202 Accepted regardless of Accept.
            if "id" not in req:
                daemon.handle_request(raw)  # side effects only
                self._write_empty(202)
                return

            # 9. Dispatch. handle_request returns a fully-formed JSON-RPC
            #    envelope; we frame it as JSON or SSE.
            resp = daemon.handle_request(raw)
            if resp is None:
                # Defensive: handle_request should not return None for an
                # id-bearing request, but if a future refactor regresses
                # we still owe the client an empty OK.
                self._write_empty(202)
                return
            self._send_envelope(resp, request_id, prefer_sse)

    return Handler





def _serve_http(daemon: Daemon, host: str, port: int) -> None:
    """Bind loopback HTTP and serve forever. Returns only on signal/error.

    We log a single startup banner so an operator (or test harness) can
    confirm the daemon bound the expected port before issuing requests.
    """
    handler_cls = _build_handler(daemon)
    server = ThreadingHTTPServer((host, port), handler_cls)
    sys.stderr.write(
        f"[srcradar-mcp-daemon] listening on {host}:{port} pid={os.getpid()}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[srcradar-mcp-daemon] KeyboardInterrupt, shutting down\n")
        sys.stderr.flush()
    finally:
        server.server_close()


def _parse_cli_test_args(extra: list[str]) -> dict:
    """Turn ['--business', 'X', '--limit', '5'] into {'business':'X','limit':'5'}.

    Special flag --args <json> is parsed as JSON into args.args (used by
    dispatcher.* to forward freeform arguments).
    """
    parsed: dict = {}
    i = 0
    while i < len(extra):
        tok = extra[i]
        if not tok.startswith("--"):
            i += 1
            continue
        if tok == "--args":
            if i + 1 >= len(extra):
                raise ValueError("--args requires a JSON value")
            try:
                parsed["args"] = json.loads(extra[i + 1])
            except json.JSONDecodeError as exc:
                raise ValueError(f"--args value not valid JSON: {exc}")
            i += 2
            continue
        if "=" in tok:
            k, _, v = tok.partition("=")
            parsed[k[2:].replace("-", "_")] = v
            i += 1
            continue
        key = tok[2:].replace("-", "_")
        if i + 1 < len(extra) and not extra[i + 1].startswith("--"):
            parsed[key] = extra[i + 1]
            i += 2
            continue
        parsed[key] = True
        i += 1
    return parsed


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8764


def main(argv=None):
    p = argparse.ArgumentParser(description="srcradar-mcp JSON-RPC daemon (HTTP)")
    p.add_argument("--db", default=DEFAULT_DB, help="path to recon.sqlite3 (read-only)")
    p.add_argument("--whitelist", default=os.path.join(os.path.dirname(__file__), "whitelist.json"))
    p.add_argument("--scripts-root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "main"))
    p.add_argument("--srcradar-bin", default=_default_srcradar_bin())
    p.add_argument("--self-test", action="store_true", help="run JSON-RPC sanity check and exit")
    p.add_argument("--http", dest="http", action="store_true", default=True,
                   help="serve JSON-RPC over HTTP on 127.0.0.1 (default)")
    p.add_argument("--host", default=DEFAULT_HTTP_HOST,
                   help=f"HTTP bind host (default {DEFAULT_HTTP_HOST}, loopback only)")
    p.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP bind port (default {DEFAULT_HTTP_PORT})")
    p.add_argument("--test", default=None, metavar="TOOL",
                   help="CLI self-test: load whitelist, dispatch TOOL with the "
                        "remaining argv as its args, print JSON response, exit. "
                        "Example: --test tools.list ; "
                        "--test db.read_business_summary --business X ; "
                        "--test dispatcher.run --args '[\"--list\"]'")
    args, extra = p.parse_known_args(argv)

    whitelist_path = os.path.abspath(args.whitelist)
    if not os.path.isfile(whitelist_path):
        sys.stderr.write(f"daemon: whitelist not found: {whitelist_path}\n")
        return 2

    try:
        daemon = Daemon(args.db, whitelist_path, args.scripts_root, srcradar_bin=args.srcradar_bin)
    except (sqlite3.Error, ValueError) as exc:
        sys.stderr.write(f"daemon: init failed: {exc}\n")
        return 3

    try:
        if args.self_test:
            result = daemon.self_test()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.test is not None:
            try:
                tool_args = _parse_cli_test_args(extra)
            except ValueError as exc:
                sys.stderr.write(f"daemon: --test arg parse failed: {exc}\n")
                return 4
            result = daemon.cli_test(args.test, tool_args)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        _serve_http(daemon, args.host, args.port)
        return 0
    finally:
        daemon.close()


if __name__ == "__main__":
    sys.exit(main())