#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Read-only tt-smi. Installed as /usr/local/bin/tt-device-mcp-smi-ro and granted
# NOPASSWD sudo so a non-ttdev user can run tt-smi against a LOCKED device for
# telemetry — without bare-metal access and without being able to reset it. The
# read-only allowlist is what makes that sudo grant safe; tt-smi's interactive
# TUI has no reset action, so the live dashboard is safe too. `tt-device-mcp smi`
# invokes this via `sudo -n`.
set -u

[ -r /etc/default/tt-device-broker ] && . /etc/default/tt-device-broker
TTSMI="${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin/tt-smi"
[ -x "$TTSMI" ] || TTSMI="$(command -v tt-smi || true)"
[ -n "$TTSMI" ] && [ -x "$TTSMI" ] || { echo "tt-smi-ro: tt-smi not found" >&2; exit 1; }

# Allowlist of read-only flags; bare (no args) = the interactive dashboard.
# Any other flag (reset/config/blinky) is refused.
SAFE=" -ls --list -s --snapshot --snapshot_no_tty -v --version -l --local -f --filename -h --help "
for a in "$@"; do
    case "$a" in
        -*) case "$SAFE" in *" $a "*) ;; *) echo "tt-smi-ro: '$a' is not allowed (read-only)" >&2; exit 2;; esac ;;
    esac
done

exec "$TTSMI" "$@"
