#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# One entry point for any box, and it picks the mode by asking whether this host can run the full
# suite: root on a systemd host gets the shared multi-tenant broker (unit, socket, privsep;
# cooperative by default — lock with TTDEV_LOCK=1). Anything else gets the per-user standalone
# daemon. The choice is about process shape and where state lives, not about authority: which
# recovery rungs a daemon gets is measured at boot from the platform and from what the process can
# actually execute (spec 04 I17). Both are socket-only (no TCP).
#
#   sudo ./install.sh          # shared host: system broker
#        ./install.sh          # per-user daemon
#   --broker / --user          # force one explicitly
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

# What sd_booted(3) reads. Overridable so the tests can drive both branches on any host.
SYSTEMD_DIR="${SYSTEMD_DIR:-/run/systemd/system}"

MODE=""
case "${1:-}" in
    --broker)      MODE=broker ;;
    --user)        MODE=user ;;
    # The broker install needs root AND systemd (its unit, its privsep scopes); root in a container
    # has neither, so uid alone is not the question.
    "")       { [ "$(id -u)" = 0 ] && [ -d "$SYSTEMD_DIR" ]; } && MODE=broker || MODE=user ;;
    *) echo "usage: [sudo] install.sh [--broker|--user]" >&2; exit 2 ;;
esac

if [ "$MODE" = broker ]; then
    [ "$(id -u)" = 0 ] || { echo "broker install needs root — re-run with sudo" >&2; exit 1; }
    [ -d "$SYSTEMD_DIR" ] || { echo "broker install needs systemd; use --user here" >&2; exit 1; }
    exec "$HERE/deploy/install-tt-device-broker.sh"
else
    [ "$(id -u)" = 0 ] && [ -d "$SYSTEMD_DIR" ] \
        && echo "note: running per-user install as root; for a shared host use --broker" >&2
    exec "$HERE/deploy/install-user.sh" "$HERE"
fi
