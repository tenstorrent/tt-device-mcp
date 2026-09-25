#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Per-user, no-sudo install for a container or single-user box. Runs a standalone daemon on a per-user UNIX SOCKET (no root, no
# systemd, no TCP port, NO device lock — you own the box, so bare-metal stays
# available). Survives SSH disconnect — the daemon detaches — but deliberately NOT a reboot:
# everything it installs lives in one boot-cleared base, and re-running this script is the
# supported way back. Keeps nothing under $HOME: on many shared-cluster hosts $HOME is a network
# share mounted by several machines, while everything this installs — venv, socket, pid, logs —
# is machine-specific.
#
# This installs a process shape and its paths, nothing about authority: the daemon health-gates
# like any other, and which recovery rungs it gets is measured at boot from the platform and from
# what the process can execute (spec 04 I17). It does not update itself; re-run this script.
#
# Invoked by the top-level install.sh whenever the host cannot run the full suite — a normal
# user, or root without systemd (a container). It passes the cloned repo path as $1; without one
# this installs from git over HTTPS, which needs no key.
set -u

REPO_SRC="${1:-git+https://github.com/tenstorrent/tt-device-mcp.git@${TTDEV_BRANCH:-main}}"

# Machine-local, never $HOME: $HOME may be a network share mounted by several machines, and a venv of compiled
# wheels is inherently machine- and arch-specific — sharing it would collide across hosts. /tmp
# because nothing here is meant to outlive a boot: this shape has no boot recovery to protect (no
# systemd unit, and the container it usually runs in has no cron), so a base that survived would
# only leave a stale venv for the next install to write over. Runtime state lives under state/ in
# the same base — one tree, one thing to delete.
INSTALL_BASE="${TT_DEVICE_MCP_INSTALL_DIR:-/tmp/tt-device-mcp-$(id -u)}"
BINDIR="$INSTALL_BASE/bin"
VENV="$INSTALL_BASE/venv"

echo "==> per-user install (no sudo) — standalone socket daemon"
# Stop first: the rebuild below replaces the venv a running daemon is executing out of, and
# `daemon start` returns 0 on an already-running one, so re-running this script to upgrade would
# report DONE with the previous build still serving. Try both entry points — a daemon can be up
# while the $BINDIR symlink is missing, and gating on that alone skips the stop it needs.
for _exe in "$VENV/bin/tt-device-mcp" "$BINDIR/tt-device-mcp"; do
    [ -x "$_exe" ] || continue
    "$_exe" daemon stop >/dev/null 2>&1 && break
done

# Its own venv, not `pip install --user`: pip refuses --user inside an active virtualenv, and the
# tt-metalium dev image ships one activated whose python has no pip at all. Installing into the
# caller's env instead would mix these deps into theirs.
mkdir -p "$(dirname "$VENV")"
python3 -m venv --clear "$VENV" || { echo "could not create venv at $VENV" >&2; exit 1; }
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q --upgrade "$REPO_SRC" \
    || { echo "pip install failed" >&2; exit 1; }
mkdir -p "$BINDIR"
ln -sf "$VENV/bin/tt-device-mcp" "$BINDIR/tt-device-mcp"

# PATH wiring is optional and left to the operator — this installer writes nothing under $HOME,
# including rc files. Everything below invokes "$BINDIR/tt-device-mcp" by absolute path, so the
# daemon and the Claude/Cursor wiring both work with $BINDIR off PATH.
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)  echo "    to run 'tt-device-mcp' directly, add to your shell rc:"
        echo "        export PATH=\"$BINDIR:\$PATH\"" ;;
esac

# The daemon IS the product on a single-user box; `daemon start` returns 0 only
# after it confirms /health (or defers to a live system broker), so a non-zero
# means this install can't serve — abort rather than report DONE over it.
if ! "$BINDIR/tt-device-mcp" daemon start; then
    echo "    !! daemon failed to come up — this per-user install is not usable." >&2
    echo "       check the log: ${TT_DEVICE_MCP_STATE_DIR:-$INSTALL_BASE/state}/daemon.log" >&2
    exit 1
fi

# This shape uses no cron. An @reboot entry would re-exec $BINDIR by absolute path inside a
# boot-cleared base — a directory guaranteed to be gone, so it would fail silently every boot
# while claiming reboot persistence, and a promise that cannot be kept is worse than none. Any
# tt-device-mcp entry found in the crontab is therefore stale whatever put it there, and is
# removed rather than left to fail.
if command -v crontab >/dev/null 2>&1; then
    if crontab -l 2>/dev/null | grep -q 'tt-device-mcp \(daemon start\|self-update\)'; then
        crontab -l 2>/dev/null | grep -v 'tt-device-mcp \(daemon start\|self-update\)' | crontab - \
            && echo "    cron: removed a stale tt-device-mcp entry" >&2
    fi
fi

# Wire this user's Claude at tt-device-mcp (spawned with a piped stdin, it runs
# as the stdio adapter and auto-discovers the local socket).
if command -v claude >/dev/null 2>&1; then
    claude mcp remove tt-device-mcp -s user >/dev/null 2>&1
    claude mcp add tt-device-mcp -s user -- "$BINDIR/tt-device-mcp" >/dev/null 2>&1 \
        && echo "    Claude wired (restart Claude to pick it up)"
fi

# Non-zero here is benign (shared host deferred to the system broker, so the
# per-user socket is intentionally absent) — the start gate above already proved
# usability; this only echoes the final state.
"$BINDIR/tt-device-mcp" daemon status
echo "    install: $INSTALL_BASE (venv, $BINDIR/tt-device-mcp)"
echo "    state: ${TT_DEVICE_MCP_STATE_DIR:-$INSTALL_BASE/state} (health journal, job logs, stats, metrics textfile)"
echo "    after a reboot: re-run this installer — $INSTALL_BASE is boot-cleared by design"
echo "DONE — per-user standalone (socket; no lock; bare-metal stays available)."
