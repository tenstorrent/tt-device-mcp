#!/bin/bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Slurm Prolog: refuse the node unless the broker can prove the device fit and free.
#
# Slurm runs Prolog/Epilog with no search path, deliberately, "for security reasons" (Slurm's
# own docs) — these hooks run as root, so PATH is never the primary source of truth here either:
# the pinned absolute install is tried FIRST, exactly like deploy/tt-smi-ro.sh (another
# root/sudo-privileged script resolving the same way). Preferring PATH first would re-introduce
# exactly the trust Slurm removed — a directory ahead of the real install on a root process's
# PATH, however it got there, would run as root in tt-device-mcp's place. `command -v` is only a
# fallback, for a non-system install with no /etc/default/tt-device-broker at all. The config is
# sourced unconditionally (not only inside the fallback) so a site's TTDEV_VENV is honored for
# both resolution and the failure message below, never silently skipped.
#
# Read-only by design. A non-zero exit drains this node and requeues the job, so the work
# re-routes to a healthy node instead of waiting for a repair on the critical path of every
# other node in the allocation. Repair is the Epilog's job.
#
# Requires SchedulerParameters=nohold_on_prolog_fail, or the requeued job is HELD and needs an
# operator to release it.
#
# slurmd builds Prolog/Epilog a fresh environment rather than propagating its own service
# environment, and /etc/default/tt-device-broker uses plain KEY=value with no `export` — so a
# site that raises the deadline only in that file previously reached neither this script nor the
# CLI child it execs. Exporting it here, once, after sourcing, is what makes that file the one
# place a site needs to set it; harmless if it was never set (bash exports nothing for a name
# with no value, and `set -u` does not treat naming a var in `export` as reading it).
set -uo pipefail

ETC_DEFAULT="${TTDEV_ETC_DEFAULT:-/etc/default/tt-device-broker}"
[ -r "$ETC_DEFAULT" ] && . "$ETC_DEFAULT"
export TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC
TTDEV_MCP="${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin/tt-device-mcp"
[ -x "$TTDEV_MCP" ] || TTDEV_MCP="$(command -v tt-device-mcp || true)"
[ -n "$TTDEV_MCP" ] && [ -x "$TTDEV_MCP" ] || {
    echo "prolog.sh: tt-device-mcp not found on PATH or at ${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin — refusing the node" >&2
    exit 1
}

exec "$TTDEV_MCP" pre-step
