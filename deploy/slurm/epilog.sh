#!/bin/bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Slurm Epilog: reclaim the allocation's stragglers, recover the device if needed, confirm.
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
# This is where recovery belongs: nobody is waiting on it. A non-zero exit drains the node,
# which is the right signal — the ladder ran and could not prove the mesh fit.
#
# The step's exit code forces the fabric traffic pass. SLURM_JOB_EXIT_CODE is only the wrapping
# batch script's own status and can read 0 even when a step genuinely failed (multiple srun
# steps, `|| true`, a trap); SLURM_JOB_DERIVED_EC is the highest exit code across every step in
# the job, so it is authoritative whenever it is non-zero. A fabric wedge is invisible to the
# enum snapshot but fails any job running across it, so a failed step is when the pass earns its
# cost. Neither variable is set outside a real Slurm Epilog; the `:-0` fallback below is a
# deliberate fail-open for a manual invocation (an operator testing the hook by hand), where a
# script that was never a Slurm step should not pay for a fabric pass.
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
export TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC
TTDEV_MCP="${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin/tt-device-mcp"
[ -x "$TTDEV_MCP" ] || TTDEV_MCP="$(command -v tt-device-mcp || true)"
[ -n "$TTDEV_MCP" ] && [ -x "$TTDEV_MCP" ] || {
    echo "epilog.sh: tt-device-mcp not found on PATH or at ${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin — draining the node" >&2
    exit 1
}

# Slurm spells these more than one way, and neither is the plain small integer the name
# suggests: SLURM_JOB_EXIT_CODE is a wait(2) status, so an exit of 1 arrives as 256, and the
# <exit>:<signal> spelling turns up too — `0:0` for a clean step, `0:9` for one a signal killed.
# post-step consumes only the zero/nonzero bit (it decides whether to force the fabric pass), so
# collapse to that bit here. Handing the raw value through was a drain-the-node-on-success bug:
# argparse's int() rejects `0:0`, the epilogue exits non-zero without ever reaching the broker,
# and Slurm drains the node over a job that finished fine.
EC=0
for part in $(printf '%s %s' "${SLURM_JOB_EXIT_CODE:-0}" "${SLURM_JOB_DERIVED_EC:-0}" | tr ':' ' '); do
    case "$part" in
    '' | 0) ;;
    *) EC=1 ;;
    esac
done

exec "$TTDEV_MCP" post-step --exit-code "$EC"
