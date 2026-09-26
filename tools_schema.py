#!/usr/bin/env python3
"""MCP ListToolsResult schema registry for srcradar-mcp.

Centralises title / description / inputSchema / annotations for every tool
advertised over MCP. The schema here is what `tools/list` returns to clients;
the SQL / dispatch logic in daemon.py stays the source of truth for behaviour
and is not duplicated here.

inputSchema follows JSON Schema Draft 2020-12 ("type":"object"). All fields
not listed in `properties` are rejected by hermes-side validation, so we
keep `additionalProperties: False` everywhere -- clients that pass extra
keys will get a clean 422 back instead of being silently ignored.

Annotations follow MCP spec 2026-07-28. `readOnlyHint` is set per-tool:
stage_file / stage_dir are writes (readOnlyHint=false); db.* and the
non-confirmed dispatcher / ymicp tools are reads (readOnlyHint=true).
e1-confirmed tools (manage.* / daily.* / dispatcher.run_confirmed) are
also readOnlyHint=false so Hermes `trust: untrusted` always pops the
native approval gate for them.
"""
from __future__ import annotations


def _ro_schema(props: dict, required: list[str]) -> dict:
    """Wrap a properties dict into a JSON Schema object with closed shape."""
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# db.* -- in-process sqlite3 read handlers. Argument names match what
# `args.get(...)` keys look up inside each handler in daemon.py.
# ---------------------------------------------------------------------------

DB_TOOLS: dict[str, dict] = {
    "db.read_business_summary": {
        "title": "Read business summary",
        "description": (
            "Return aggregate counts for one business (subdomains, ports, "
            "companies, scopes, web_hashes, mapp_records) plus its "
            "recon_business_config. Omit `business` to list all businesses "
            "(summary mode)."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {
                    "type": "string",
                    "description": "Business name. Omit to list all businesses.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "description": "Row cap (default 100, hard max 10000).",
                },
            },
            required=[],
        ),
    },
    "db.read_subdomains": {
        "title": "Read web subdomains",
        "description": (
            "List subdomains for one business. Filters by status_code, port, "
            "and is_active. Pagination via limit + offset."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                "offset": {"type": "integer", "minimum": 0},
                "since": {
                    "type": "string",
                    "description": "ISO date or timestamp; rows with last_seen >= since.",
                },
                "status_code": {"type": "integer", "minimum": 0, "maximum": 999},
                "port": {"type": "integer", "minimum": 0, "maximum": 65535},
                "is_active": {"type": "integer", "enum": [0, 1]},
            },
            required=["business"],
        ),
    },
    "db.read_open_ports": {
        "title": "Read open ports",
        "description": (
            "List open ports (tcp_assets) for one business. Filters by port "
            "and is_active."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                "offset": {"type": "integer", "minimum": 0},
                "port": {"type": "integer", "minimum": 0, "maximum": 65535},
                "is_active": {"type": "integer", "enum": [0, 1]},
            },
            required=["business"],
        ),
    },
    "db.read_companies": {
        "title": "Read companies",
        "description": (
            "List companies (seed rows + discovered subsidiaries) for one "
            "business. Pagination via limit + offset."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                "offset": {"type": "integer", "minimum": 0},
            },
            required=["business"],
        ),
    },
    "db.read_diff": {
        "title": "Read diff",
        "description": (
            "Return the diff rows (last_seen / first_seen / change_type) "
            "for one business, optionally since a timestamp."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            required=["business"],
        ),
    },
    "db.read_single_subdomain": {
        "title": "Read single subdomain",
        "description": (
            "Return one subdomain row (plus raw_json if present) by exact "
            "subdomain string."
        ),
        "inputSchema": _ro_schema(
            {
                "business": {"type": "string"},
                "subdomain": {"type": "string"},
            },
            required=["business", "subdomain"],
        ),
    },
}


# ---------------------------------------------------------------------------
# dispatcher.* / manage.* / daily.* / ymicp.* -- forwarded to the
# srcradar CLI. Args are passed through as argv; we do not deeply model
# each CLI flag (the CLI is the source of truth and has its own argparse),
# but we DO model the high-level shape so hermes can render tooltips and
# reject obvious shape errors before the call hits the subprocess.
# ---------------------------------------------------------------------------

# `args` here is the catch-all freeform argv list that the daemon forwards
# to the srcradar subprocess. We intentionally keep its schema permissive
# (`additionalProperties: True`) because the underlying CLI accepts many
# flags per invocation; over-restricting it would force every dispatcher to
# duplicate srcradar's argparse, which would rot on the next srcradar
# refactor.
_FREEFORM_SCHEMA = _ro_schema(
    {
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Argv passed verbatim to the srcradar subprocess. See the "
                "matching CLI script for accepted flags."
            ),
        },
    },
    required=[],
)

_SUBPROCESS_TOOLS: dict[str, dict] = {
    "dispatcher.list": {
        "title": "List srcradar scripts",
        "description": (
            "List every executable script srcradar can dispatch across "
            "modules/main, modules/public, modules/private. Read-only."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "dispatcher.run_confirmed": {
        "title": "Run arbitrary srcradar command",
        "description": (
            "Forward `args` as argv to ./srcradar. Requires e1-confirmed "
            "auth (set `confirmed: true` in the params). Use sparingly; "
            "prefer the named module tools when available."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "manage.add_business": {
        "title": "Bootstrap a new business",
        "description": (
            "Forward to ./srcradar manage add_business. Creates the "
            "businesses row + recon_business_config, optionally seeds "
            "companies from a TSV and imports scopes. Requires "
            "e1-confirmed."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "manage.set_config": {
        "title": "View or update business config",
        "description": (
            "Forward to ./srcradar manage set_config. Read-only if no "
            "change flag given; otherwise updates enabled/web/tcp/icp for "
            "one business. Requires e1-confirmed."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "daily.run_one_business": {
        "title": "Run selected stages for one business",
        "description": (
            "Forward to ./srcradar daily run_one_business. Stages are "
            "pdtm / enscan / icp / daily-url; the CLI runs them in its own "
            "internal order regardless of the caller's -type ordering. "
            "Requires e1-confirmed."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "daily.run_dashboard_watchdog": {
        "title": "Restart dashboard if not running",
        "description": (
            "Forward to ./srcradar daily dashboard_watchdog. Idempotent "
            "pgrep-based restart. Requires e1-confirmed."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
    "ymicp.icp_mapp_query": {
        "title": "Query ymicp ICP / mapp records",
        "description": (
            "Forward to ./srcradar ymicp icp_mapp_query. Reads company "
            "names from stdin, scrapes ymicp /query/mapp, writes mapp "
            "records. Read-only on the ymicp side; writes to recon.sqlite3."
        ),
        "inputSchema": _FREEFORM_SCHEMA,
    },
}


# ---------------------------------------------------------------------------
# stage_file / stage_dir -- materialise client-supplied bytes to a
# daemon-managed temp dir. Returns an absolute path the caller can pass
# to downstream tools (add_business -s, add_business -i, etc.). These are
# writes (readOnlyHint=false) but do not require e1-confirmed: the daemon
# already binds 127.0.0.1 + whitelist, so the only consumer is the
# operator at the other end of the loopback MCP client.
# ---------------------------------------------------------------------------

_STAGE_FILE_SCHEMA = _ro_schema(
        {
            "filename": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": r"^[A-Za-z0-9._-]+$",
                "description": (
                    "Target filename on the daemon host (16-hex prefix + your ext is auto-applied). "
                    "No path separators, no .., no NUL. Recommended: pass just the basename like seed.tsv."
                ),
            },
            "stdin": {
                "type": "string",
                "description": "Inline text content. Use for tiny payloads the LLM generates itself.",
            },
            "base64": {
                "type": "string",
                "description": (
                    "Base64-encoded bytes. Use this (combined with optional file_path) "
                    "when uploading a client-local file: client reads the file, "
                    "base64-encodes, and sends the string here. Daemon never reads client fs."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional. Client-local absolute path the bytes came from (Windows/macOS/Linux path). "
                    "DAEMON DOES NOT ACCESS THIS PATH -- it is recorded for audit/log only. "
                    "MUST be paired with `base64` (file_path alone is rejected)."
                ),
            },
        },
        required=["filename"],
    )

_STAGE_DIR_SCHEMA = _ro_schema(
        {
            "base64": {
                "type": "string",
                "description": (
                    "Base64-encoded tar.gz of the directory tree. Client runs "
                    "`tar -czf - dir/ | base64 -w0` and sends the string."
                ),
            },
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "pattern": r"^[A-Za-z0-9._-]+$",
                        },
                        "content_b64": {"type": "string"},
                    },
                    "required": ["filename", "content_b64"],
                    "additionalProperties": False,
                },
                "description": (
                    "Alternative to tar.gz: explicit file list. Each entry "
                    "is one file (no nesting; subdirectories not allowed at "
                    "this layer -- use tar.gz for nested trees)."
                ),
            },
        },
        required=[],
    )

_STAGE_TOOLS: dict[str, dict] = {
    "stage_file": {
        "title": "Stage a single file",
        "description": (
            "Materialise client-supplied bytes into a daemon-managed temp "
            "file and return its absolute path. Use this when a downstream "
            "tool (add_business -s, etc.) needs a path you do not have. "
            "Supports three input modes -- pick one: \n"
            "  - `stdin`: inline text (LLM-generated content)\n"
            "  - `base64`: client reads local file, base64-encodes, sends bytes\n"
            "  - `file_path` + `base64`: same as base64 mode, plus the client\n"
            "    declares which local path it read (recorded for audit only).\n"
            "\n"
            "The returned `staged_path` is on the daemon host (v1), NOT on the\n"
            "client. Pass it verbatim to downstream tools via their flag args.\n"
            "Use the `_staging_ref: <upload_id>` field on the follow-up call\n"
            "to ask the daemon to delete the staged file as soon as that\n"
            "tool finishes."
        ),
        "inputSchema": _STAGE_FILE_SCHEMA,
    },
    "stage_dir": {
        "title": "Stage a directory",
        "description": (
                "Materialise a directory tree into a daemon-managed temp dir and\n"
                "return its absolute path. Use this when a downstream tool needs\n"
                "-i <dir> (e.g. add_business -i). Supports two input modes:\n"
                "  - `base64`: client runs `tar -czf - dir/ | base64 -w0` and sends\n"
                "    the string; daemon extracts to a fresh upload_id dir.\n"
                "  - `entries`: explicit list of {filename, content_b64} for flat\n"
                "    dirs (target.txt, exclude.txt, ...). No subdir nesting.\n"
                "\n"
                "The returned `staged_dir` is on the daemon host. Pass it verbatim\n"
                "to downstream tools via their flag args."
            ),
        "inputSchema": _STAGE_DIR_SCHEMA,
    },
}
# Merged view -- one place to look up any tool.
ALL_TOOLS: dict[str, dict] = {**DB_TOOLS, **_SUBPROCESS_TOOLS, **_STAGE_TOOLS}


def list_tools_payload(auth_for: dict[str, str]) -> dict:
    """Build the MCP ListToolsResult envelope.

    `auth_for` maps tool name -> auth tier; we mirror that into
    `annotations.readOnlyHint` so hermes can mark which tools need
    user approval under trust=untrusted without consulting our auth
    table separately.
    """
    out = []
    for name, meta in ALL_TOOLS.items():
        auth = auth_for.get(name, "none")
        out.append({
            "name": name,
            "title": meta["title"],
            "description": meta["description"],
            "inputSchema": meta["inputSchema"],
            "annotations": {
                "readOnlyHint": auth == "none",
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        })
    return {
        # MCP 2026-07-28 ListToolsResult top-level required fields.
        # resultType is "complete" because we ship the full whitelist in
        # one shot (no pagination). ttlMs gives clients a cache hint;
        # 1h matches the server/discover ttl we already advertise.
        # cacheScope is "public" because the whitelist has no per-user
        # gating -- every loopback client gets the same 13 tools.
        "resultType": "complete",
        "tools": out,
        "nextCursor": None,
        "ttlMs": 3600000,
        "cacheScope": "public",
    }
