#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Wire the CURRENT user's Claude Code + Cursor MCP at tt-device-mcp, idempotently.
# Claude spawns the bare binary with a piped stdin; it runs as the stdio MCP
# adapter and auto-discovers the broker socket. Only touches a client that's
# actually installed, and only rewrites when the entry is missing/wrong — so it's
# a fast no-op once correct. Run at login and from the per-minute reconcile.
[ -r /etc/default/tt-device-broker ] && . /etc/default/tt-device-broker

# Absolute path so the entry is independent of Claude's spawn PATH. Falls through the system
# broker venv, then this user's own per-user install (deploy/install-user.sh, machine-local under
# TT_DEVICE_MCP_INSTALL_DIR / /tmp — never $HOME, so it's off PATH unless the operator added it).
CMD="$(command -v tt-device-mcp 2>/dev/null)"
[ -n "$CMD" ] || CMD="${TTDEV_ROOT:-/opt/tt-device-broker}/venv/bin/tt-device-mcp"
[ -x "$CMD" ] || CMD="${TT_DEVICE_MCP_INSTALL_DIR:-/tmp/tt-device-mcp-$(id -u)}/bin/tt-device-mcp"
[ -x "$CMD" ] || exit 0

# --- Claude Code (only if the `claude` CLI is on this user's PATH) ---
if command -v claude >/dev/null 2>&1; then
    ok=$(python3 - "$CMD" <<'PY' 2>/dev/null
import json, os, sys
cmd = sys.argv[1]
try:
    e = json.load(open(os.path.expanduser("~/.claude.json"))).get("mcpServers", {}).get("tt-device-mcp", {})
    print("ok" if e.get("type") == "stdio" and e.get("command") == cmd else "no")
except Exception:
    print("no")
PY
)
    if [ "$ok" != "ok" ]; then
        claude mcp remove tt-device-mcp -s user >/dev/null 2>&1
        claude mcp add tt-device-mcp -s user -- "$CMD" >/dev/null 2>&1
    fi
fi

# --- Cursor (only if it's installed, i.e. ~/.cursor exists) ---
if [ -d "$HOME/.cursor" ]; then
    python3 - "$CMD" <<'PY' 2>/dev/null
import json, pathlib, sys
cmd = sys.argv[1]
p = pathlib.Path.home() / ".cursor" / "mcp.json"
try:
    d = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
except Exception:
    d = {}
want = {"command": cmd}
if d.get("mcpServers", {}).get("tt-device-mcp") != want:
    d.setdefault("mcpServers", {})["tt-device-mcp"] = want
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))
PY
fi
