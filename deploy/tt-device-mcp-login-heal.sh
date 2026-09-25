# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Login heal for per-user tt-device-mcp installs that shadow the host CLI.
#
# The broker host exposes one always-current CLI at /usr/local/bin/tt-device-mcp
# (a symlink into the broker venv). Nothing this project installs writes to $HOME,
# so a ~/.local/bin/tt-device-mcp can only come from a user pip-installing the
# package themselves — `pip install --user` puts it exactly there, and rc files
# put ~/.local/bin earlier on PATH. That copy never updates with the host, so it
# goes stale silently and the user talks to a CLI the broker no longer matches.
# Installed to /etc/profile.d/, this removes such a shadowing copy at login so the
# user falls through to the system CLI. No reinstall — the system symlink is the
# single source of truth, so there is nothing to keep current. No-op when there is
# no shadowing copy (fast) or on hosts without the system CLI.

_ttdev_local="$HOME/.local/bin/tt-device-mcp"
if [ -e "$_ttdev_local" ] && [ -x /usr/local/bin/tt-device-mcp ] \
   && [ "$(readlink -f "$_ttdev_local" 2>/dev/null)" != "$(readlink -f /usr/local/bin/tt-device-mcp 2>/dev/null)" ]; then
    echo "tt-device-mcp: removing a stale ~/.local copy so you use the shared broker CLI." >&2
    python3 -m pip uninstall -y tt-device-mcp >/dev/null 2>&1
    rm -f "$_ttdev_local"  # ensure the shadowing launcher is gone even if it wasn't pip-managed
fi
unset _ttdev_local
