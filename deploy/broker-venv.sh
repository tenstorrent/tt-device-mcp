# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Broker venv swap, sourced by install-tt-device-broker.sh (spec 08 I26).
#
# The installer runs while an installed broker may still be serving, and that broker resolves its
# tt-smi (every reset rung) and its lazy imports through $R/venv for as long as it runs. So the
# venv is never rebuilt in place. $R/venv is a symlink to a versioned venv, $R/venv.<id>:
#
#   venv_build  <root> <repo>   build and verify a new venv at its final path; print that path
#   venv_flip   <root> <new>    point $R/venv at it with one rename (two on a legacy real-dir venv)
#   venv_prune  <root>          remove every venv.* the link does not point at
#
# The new venv is built at its final path because a venv is not relocatable: its scripts' shebangs
# and pyvenv.cfg name the directory it was created in. The installer calls venv_prune only once the
# restarted broker answers /health, so a failed install leaves the old venv for a rollback.

venv_verify() {
    local v="$1"
    # The broker's own entry point (the unit runs `python -m tt_device_mcp.server`) and the tt-smi its
    # reset rungs import and spawn. Help only: nothing here opens the device.
    timeout 300 "$v/bin/python" -c 'import tt_device_mcp.server' >/dev/null \
        || { echo "install: new venv $v cannot import tt_device_mcp.server" >&2; return 1; }
    timeout 120 "$v/bin/python" -c 'import tt_smi' >/dev/null \
        || { echo "install: new venv $v cannot import tt_smi" >&2; return 1; }
    timeout 120 "$v/bin/tt-smi" --help >/dev/null \
        || { echo "install: new venv $v: tt-smi --help failed" >&2; return 1; }
}

venv_build() {
    local r="$1" repo="$2" id new
    id="$(git -C "$repo" rev-parse --short=12 HEAD 2>/dev/null || echo local)"
    # mktemp: a name no earlier run used, so a re-install of the same commit never builds over the
    # venv the live broker runs from.
    new="$(mktemp -d "$r/venv.$id.XXXXXX")" || return 1
    # Each step checked here: errexit does not reach into the caller's $(...).
    python3 -m venv "$new" >&2 \
        && "$new/bin/pip" install -q --upgrade pip >&2 \
        && "$new/bin/pip" install -q "$repo" >&2 \
        && venv_verify "$new" \
        || { echo "install: building the new venv failed; the live venv is untouched" >&2; rm -rf "$new"; return 1; }
    echo "$new"
}

venv_flip() {
    local r="$1" new="$2"
    ln -sfn "$(basename "$new")" "$r/venv.link.tmp"
    if [ -d "$r/venv" ] && [ ! -L "$r/venv" ]; then
        # Legacy layout: $R/venv is the real venv. A rename cannot replace a directory with a
        # symlink, so it moves aside and the link moves in, two renames apart.
        rm -rf "$r/venv.legacy"
        mv -T "$r/venv" "$r/venv.legacy"
    fi
    mv -Tf "$r/venv.link.tmp" "$r/venv"
}

venv_prune() {
    local r="$1" live d
    live="$(readlink "$r/venv" 2>/dev/null)" || return 0
    live="$(basename "$live")"
    for d in "$r"/venv.*; do
        [ -d "$d" ] && [ ! -L "$d" ] || continue
        [ "$(basename "$d")" = "$live" ] && continue
        rm -rf "$d"
    done
}
