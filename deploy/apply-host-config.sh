#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Idempotently apply the broker's host-level config: the systemd unit (incl. the
# watchdog), the reconcile units, the read-only smi sudoers + wrapper, and the
# login/client profile.d hooks. Called by the installer on first install AND by
# auto-update on every version bump — so a change to the unit/sudoers/etc. reaches
# every host through the normal pipeline, with no manual installer re-run.
#
# Reads per-host values from /etc/default/tt-device-broker (written by the
# installer); never rebuilds the venv or rewrites that config. Preserves the
# device-lock state (the DEVICE_GROUP env the `lock` command adds to the unit).
#
# Usage: apply-host-config.sh <src-dir>   # dir containing deploy/
set -euo pipefail
# Dry-run seam (tests/debugging): `--print-unit` renders the systemd unit to stdout from the
# given config and exits, needing no root and writing no host path. Every install step is
# downstream of the unit render, so the seam stops the instant the unit is built.
RENDER_ONLY=""
[ "${1:-}" = --print-unit ] && { RENDER_ONLY=1; shift; }
[ -n "$RENDER_ONLY" ] || [ "$(id -u)" = 0 ] || { echo "apply-host-config: must run as root" >&2; exit 1; }
SRC="${1:?usage: apply-host-config.sh [--print-unit] <repo-root>}"
DEPLOY="$SRC/deploy"
[ -d "$DEPLOY" ] || { echo "apply-host-config: no deploy dir at $DEPLOY" >&2; exit 1; }

# The config source and unit path are overridable ONLY so the render seam can point them at a
# sandbox; the defaults are the real host paths, so the installed path resolves identically.
ETC_DEFAULT="${TTDEV_ETC_DEFAULT:-/etc/default/tt-device-broker}"
[ -r "$ETC_DEFAULT" ] && . "$ETC_DEFAULT"
VENV="${TTDEV_VENV:?apply-host-config: TTDEV_VENV not set in /etc/default/tt-device-broker}"
ROOT="${TTDEV_ROOT:-/opt/tt-device-broker}"
SOCK=/run/tt-device-broker/broker.sock
UNIT="${TTDEV_UNIT_PATH:-/etc/systemd/system/tt-device-broker.service}"

# Per-host knobs come from /etc/default (written by the new installer). For a host
# that predates those keys, fall back to the values already baked into the current
# unit so a migrating auto-update never drops RESET_MODE / the fabric check.
RESET_MODE="${TTDEV_RESET_MODE:-}"
# Guard the unit read: on a FRESH install $UNIT doesn't exist yet (it's written
# below), so an unguarded `sed "$UNIT"` exits non-zero, pipefail propagates it, and
# being the command after the final `&&` it trips `set -e` and aborts the installer.
[ -z "$RESET_MODE" ] && [ -r "$UNIT" ] && RESET_MODE="$(sed -n 's/^Environment=TT_DEVICE_MCP_RESET_MODE=//p' "$UNIT" 2>/dev/null | head -1)"
FABRIC="${TTDEV_FABRIC_CHECK_CMD:-}"
[ -z "$FABRIC" ] && [ -r "$UNIT" ] && FABRIC="$(sed -n 's/^Environment=TT_DEVICE_MCP_FABRIC_CHECK_CMD=//p' "$UNIT" 2>/dev/null | head -1)"
DESC_PRESET="${TTDEV_FABRIC_DESCRIPTOR:-}"
[ -z "$DESC_PRESET" ] && [ -r "$UNIT" ] && DESC_PRESET="$(sed -n 's/^Environment=TTDEV_FABRIC_DESCRIPTOR=//p' "$UNIT" 2>/dev/null | head -1)"
# The fabric check is not optional on ANY multi-chip machine: chip liveness (sysfs/tt-smi) cannot see
# an ethernet core that never retrained, and that is what the next tenant meets as "waiting for active
# ethernet core". Nor is its absence free — with no fabric evidence the health gate cannot clear a
# device after an abnormal exit and falls back to a full reset (server.py, `fabric_ok is None`), so a
# host without this check resets the whole board every time a job dies badly.
# It was galaxy-only because it defaulted to the galaxy cabling descriptor and aborted on anything
# else; the descriptor is opt-in now and the traffic pass needs none, so arm it everywhere. Hosts that
# cannot run it (validator not built) report CANNOT CHECK and the gate leaves the device alone.
[ -z "$FABRIC" ] && FABRIC="$ROOT/fabric-check.sh"
# Only a galaxy gets a golden descriptor: it is the one machine we ship a matching one for.
DESCRIPTOR="$DESC_PRESET"
[ -z "$DESCRIPTOR" ] && [ "$RESET_MODE" = "galaxy" ] && \
    DESCRIPTOR="$ROOT/validator/current/tools/tests/scaleout/cabling_descriptors/bh_galaxy_xy_torus.textproto"
# The passive eth-heartbeat pre-read is a built-in, in-process probe now (see
# health.monitors.eth) — no separate staged wrapper, and it runs by default, mirroring the
# fabric traffic check's own built-in path. It stays behaviour-neutral until validated: the
# probe self-blocks to a skip (its own TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN gate, unrelated to
# this unit's env) until an operator who has validated the device-attach on a reserved box
# sets that flag directly. TT_DEVICE_MCP_ETH_HEARTBEAT_CMD remains purely an OPTIONAL operator
# override (see verify_eth_heartbeat) — an explicit value wins, and any value an operator
# already set on the unit survives an auto-update.
HEARTBEAT="${TTDEV_ETH_HEARTBEAT_CMD:-}"
[ -z "$HEARTBEAT" ] && [ -r "$UNIT" ] && HEARTBEAT="$(sed -n 's/^Environment=TT_DEVICE_MCP_ETH_HEARTBEAT_CMD=//p' "$UNIT" 2>/dev/null | head -1)"
# Migration: a host that applied host-config between the wrapper's install (0c12709, on
# origin/main since Aug 7) and its retirement here may have this auto-wired to the now-deleted
# $ROOT/eth-heartbeat-check.sh, persisted into /etc/default and/or already rendered into the
# unit — and auto-update never re-runs the full installer, only this script, so that value would
# otherwise survive forever. It must not: arming TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN=1 would run a
# frozen, unmanaged copy of retired code, and if anyone later deletes that stray file by hand,
# `bash -c <missing path>` exits 127, which the override split (verify_eth_heartbeat) reads as a
# plain non-zero -> False -> HOLD on an otherwise healthy mesh. Treat it as unset on this run
# (so the unit renders without it) and scrub it from /etc/default too, so it is not re-sourced
# on the NEXT apply either — a one-time self-heal, not a recurring check. Matched against the
# exact staged path (not a bare basename glob), so an operator's own override script that merely
# happens to share the filename at some OTHER location is never mistaken for the retired one.
if [ "$HEARTBEAT" = "$ROOT/eth-heartbeat-check.sh" ]; then
    echo "apply-host-config: clearing TTDEV_ETH_HEARTBEAT_CMD (pointed at the retired eth-heartbeat-check.sh wrapper; the read is now built-in and arms itself per host at broker startup)" >&2
    HEARTBEAT=""
    [ -n "$RENDER_ONLY" ] || sed -i '/^TTDEV_ETH_HEARTBEAT_CMD=/d' /etc/default/tt-device-broker 2>/dev/null || true
fi
# Remove the retired artifact itself unconditionally — staging it stopped in
# install-tt-device-broker.sh, but auto-update never re-runs the installer, only this script,
# so a host that had it staged before retirement would otherwise keep the file forever.
[ -n "$RENDER_ONLY" ] || rm -f "$ROOT/eth-heartbeat-check.sh"
# The reset-path activation knobs. server.py reads TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC (a below-floor
# off-bus drop HOLDS instead of resetting) and TT_DEVICE_MCP_SELFHEAL_RELIFT (a self-healed hold
# lifts from the idle sampler). Both are OFF when unset, so this wiring renders no line until a host
# opts in — it only makes activation a one-line /etc/default edit, mirroring the heartbeat seam
# above. Without it those keys are inert: the broker's env is exactly the Environment= lines here,
# and nothing else sources /etc/default into the process.
RESET_FLOOR="${TTDEV_RESET_MIN_DEAD_FRAC:-}"
[ -z "$RESET_FLOOR" ] && [ -r "$UNIT" ] && RESET_FLOOR="$(sed -n 's/^Environment=TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC=//p' "$UNIT" 2>/dev/null | head -1)"
RELIFT="${TTDEV_SELFHEAL_RELIFT:-}"
[ -z "$RELIFT" ] && [ -r "$UNIT" ] && RELIFT="$(sed -n 's/^Environment=TT_DEVICE_MCP_SELFHEAL_RELIFT=//p' "$UNIT" 2>/dev/null | head -1)"
# The mesh this host SHOULD have. server.py's expected_chip_count() otherwise derives the bar from a
# high-water mark of chips seen, which bakes a degraded survivor count when the broker first runs while
# a chip is off the bus — then a reset that recovers the mesh reads as an over-count drop and a good box
# never re-verifies. An explicit per-host count (32 galaxy / 8 f07) makes the bar authoritative. Unset
# renders no line, so the hwm fallback is unchanged until a host opts in.
EXPECTED_CHIPS="${TTDEV_EXPECTED_CHIPS:-}"
[ -z "$EXPECTED_CHIPS" ] && [ -r "$UNIT" ] && EXPECTED_CHIPS="$(sed -n 's/^Environment=TT_DEVICE_MCP_EXPECTED_CHIPS=//p' "$UNIT" 2>/dev/null | head -1)"
# The cold rungs — the TOP of the recovery cascade. server.py reads TT_DEVICE_MCP_AUTO_REBOOT (warm
# reboot) and TT_DEVICE_MCP_AUTO_POWER_CYCLE (BMC power cycle), each default OFF, each armed with "1".
# Without this wiring neither key can be set (nothing else sources /etc/default into the broker process),
# so the cascade can only ever reach the rung below them and an all-off-bus mesh — the one state a warm
# reboot cannot re-enumerate — holds forever with no reachable recovery. Unset renders no line, so arming
# a destructive rung stays a deliberate one-line /etc/default edit that no host takes by accident; the
# power-cycle preflight still requires ipmitool wherever AUTO_POWER_CYCLE is on. Unit-read fallback
# mirrors the others so an auto-update never drops a value an operator already set.
AUTO_REBOOT="${TTDEV_AUTO_REBOOT:-}"
[ -z "$AUTO_REBOOT" ] && [ -r "$UNIT" ] && AUTO_REBOOT="$(sed -n 's/^Environment=TT_DEVICE_MCP_AUTO_REBOOT=//p' "$UNIT" 2>/dev/null | head -1)"
AUTO_POWER_CYCLE="${TTDEV_AUTO_POWER_CYCLE:-}"
[ -z "$AUTO_POWER_CYCLE" ] && [ -r "$UNIT" ] && AUTO_POWER_CYCLE="$(sed -n 's/^Environment=TT_DEVICE_MCP_AUTO_POWER_CYCLE=//p' "$UNIT" 2>/dev/null | head -1)"
# The per-tray BMC reset rung is armed by DEFAULT in server.py (TT_DEVICE_MCP_AUTO_UBB_RESET, only "0"
# disables it), so unlike the cold rungs above this passthrough's job is the reverse: a force-OFF path.
# An operator whose BMC bit order the broker has not confirmed disarms the rung — back to naming the
# command and holding — with TTDEV_AUTO_UBB_RESET=0, without hand-editing the unit. Unset renders no line,
# leaving the rung armed at its code default; the unit-read fallback mirrors the others so an auto-update
# never drops a disarm an operator already set.
AUTO_UBB_RESET="${TTDEV_AUTO_UBB_RESET:-}"
[ -z "$AUTO_UBB_RESET" ] && [ -r "$UNIT" ] && AUTO_UBB_RESET="$(sed -n 's/^Environment=TT_DEVICE_MCP_AUTO_UBB_RESET=//p' "$UNIT" 2>/dev/null | head -1)"
# The pre-job dispatch probe opt-in. server.py reads TT_DEVICE_MCP_PREJOB_DISPATCH ("1" arms it),
# default OFF: the gate proves the mesh runs a kernel before admitting a tenant, but a probe slower
# than its timeout on a given host reads as a false wedge — and that class of false verdict has cost
# this fleet reboots — so a host arms it only after the probe is timed there. Without this wiring the
# key is unreachable (nothing else sources /etc/default into the broker), and a hand-edited unit line
# is wiped by the next auto-update re-render; the unit-read fallback mirrors the others so an update
# never drops an opt-in an operator already made.
PREJOB_DISPATCH="${TTDEV_PREJOB_DISPATCH:-}"
[ -z "$PREJOB_DISPATCH" ] && [ -r "$UNIT" ] && PREJOB_DISPATCH="$(sed -n 's/^Environment=TT_DEVICE_MCP_PREJOB_DISPATCH=//p' "$UNIT" 2>/dev/null | head -1)"
# The python the eth reader resolves ttexalens from. Its own docs call this the per-box mechanism,
# but nothing rendered it, so the reader could never find one and the rung self-tested to OFF on every
# host — documented and unreachable is the same bug as unwired.
ETH_PYTHON="${TTDEV_ETH_CHECK_PYTHON:-}"
[ -z "$ETH_PYTHON" ] && [ -r "$UNIT" ] && ETH_PYTHON="$(sed -n 's/^Environment=TTDEV_ETH_CHECK_PYTHON=//p' "$UNIT" 2>/dev/null | head -1)"
reset_env=""; [ -n "$RESET_MODE" ] && reset_env="Environment=TT_DEVICE_MCP_RESET_MODE=$RESET_MODE"
fabric_env=""; [ -n "$FABRIC" ] && fabric_env="Environment=TT_DEVICE_MCP_FABRIC_CHECK_CMD=$FABRIC"
# fabric-check.sh reads this from its own environment, so it has to reach the broker's unit.
desc_env=""; [ -n "$DESCRIPTOR" ] && desc_env="Environment=TTDEV_FABRIC_DESCRIPTOR=$DESCRIPTOR"
hb_env=""; [ -n "$HEARTBEAT" ] && hb_env="Environment=TT_DEVICE_MCP_ETH_HEARTBEAT_CMD=$HEARTBEAT"
floor_env=""; [ -n "$RESET_FLOOR" ] && floor_env="Environment=TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC=$RESET_FLOOR"
relift_env=""; [ -n "$RELIFT" ] && relift_env="Environment=TT_DEVICE_MCP_SELFHEAL_RELIFT=$RELIFT"
expected_env=""; [ -n "$EXPECTED_CHIPS" ] && expected_env="Environment=TT_DEVICE_MCP_EXPECTED_CHIPS=$EXPECTED_CHIPS"
autoreboot_env=""; [ -n "$AUTO_REBOOT" ] && autoreboot_env="Environment=TT_DEVICE_MCP_AUTO_REBOOT=$AUTO_REBOOT"
autopc_env=""; [ -n "$AUTO_POWER_CYCLE" ] && autopc_env="Environment=TT_DEVICE_MCP_AUTO_POWER_CYCLE=$AUTO_POWER_CYCLE"
autoubb_env=""; [ -n "$AUTO_UBB_RESET" ] && autoubb_env="Environment=TT_DEVICE_MCP_AUTO_UBB_RESET=$AUTO_UBB_RESET"
prejobdispatch_env=""; [ -n "$PREJOB_DISPATCH" ] && prejobdispatch_env="Environment=TT_DEVICE_MCP_PREJOB_DISPATCH=$PREJOB_DISPATCH"
ethpython_env=""; [ -n "$ETH_PYTHON" ] && ethpython_env="Environment=TTDEV_ETH_CHECK_PYTHON=$ETH_PYTHON"

# Backfill keys the running version expects into /etc/default — a host installed
# before a key existed won't have it (auto-update never rewrites this file), so the
# resolved value (recovered from the unit above) is persisted here. Existing keys
# are left untouched.
# Backfill a key that is missing OR present-but-empty. An empty key is not a choice a
# host made; it is a key that predates its value. Leaving it empty makes /etc/default
# disagree with the unit the broker actually runs from, which is how the fabric check
# read as "disabled" on a host where it was in fact enabled.
ensure_default() {
    [ -n "$2" ] || return 0
    if grep -qE "^$1=.+" /etc/default/tt-device-broker 2>/dev/null; then
        return 0
    fi
    sed -i "/^$1=$/d" /etc/default/tt-device-broker 2>/dev/null || true
    echo "$1=$2" >> /etc/default/tt-device-broker
}
# Skipped under the render seam: it writes /etc/default, and a dry run must touch no host path.
if [ -z "$RENDER_ONLY" ]; then
    ensure_default TTDEV_RESET_MODE "$RESET_MODE"
    ensure_default TTDEV_FABRIC_CHECK_CMD "$FABRIC"
    ensure_default TTDEV_FABRIC_DESCRIPTOR "$DESCRIPTOR"
    ensure_default TTDEV_ETH_HEARTBEAT_CMD "$HEARTBEAT"
    ensure_default TTDEV_RESET_MIN_DEAD_FRAC "$RESET_FLOOR"
    ensure_default TTDEV_SELFHEAL_RELIFT "$RELIFT"
    ensure_default TTDEV_EXPECTED_CHIPS "$EXPECTED_CHIPS"
    ensure_default TTDEV_AUTO_REBOOT "$AUTO_REBOOT"
    ensure_default TTDEV_AUTO_POWER_CYCLE "$AUTO_POWER_CYCLE"
    ensure_default TTDEV_AUTO_UBB_RESET "$AUTO_UBB_RESET"
    ensure_default TTDEV_PREJOB_DISPATCH "$PREJOB_DISPATCH"
    ensure_default TTDEV_ETH_CHECK_PYTHON "$ETH_PYTHON"
fi
# Preserve the lock: `tt-device-mcp lock` adds DEVICE_GROUP to the unit; keep it
# across a re-render so an update doesn't silently unlock a shared host.
group_env=""; grep -q 'TT_DEVICE_MCP_DEVICE_GROUP' "$UNIT" 2>/dev/null \
    && group_env="Environment=TT_DEVICE_MCP_DEVICE_GROUP=ttdev"

[ -n "$RENDER_ONLY" ] || mkdir -p /var/log/tt-device-broker
# Render to stdout under the seam; write the real unit otherwise.
unit_dest="$UNIT"; [ -n "$RENDER_ONLY" ] && unit_dest=/dev/stdout
cat > "$unit_dest" <<UNIT
[Unit]
Description=Tenstorrent device MCP broker (multi-tenant arbiter)
After=network.target
# The startup fabric pass needs the 1G hugepage pool; start after its mount where the host has one
# (a Wants= on a unit the host lacks is a no-op). The broker still waits for the page count itself.
Wants=dev-hugepages\x2d1G.mount
After=dev-hugepages\x2d1G.mount
[Service]
User=root
# Type=notify + WatchdogSec: the broker pings the watchdog from its event loop;
# if the loop wedges, the pings stop and systemd restarts it. Jobs live in their
# own scopes and re-adopt on startup, so the restart is non-destructive.
Type=notify
NotifyAccess=main
WatchdogSec=300
RuntimeDirectory=tt-device-broker
RuntimeDirectoryMode=0755
# TT_DEVICE_MCP_HEALTH_DIR / TT_DEVICE_MCP_TEXTFILE_DIR are deliberately absent: with
# User=root above, the code's own default already resolves to /var/lib/tt-device-broker/health
# and /var/lib/prometheus/node-exporter (see evidence._default_health_dir, metrics._textfile_dir)
# — naming them here would just be a second copy of the same two paths to keep in sync.
Environment=TT_DEVICE_MCP_PRIVSEP=1
Environment=PATH=$VENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$reset_env
$fabric_env
$desc_env
$hb_env
$floor_env
$relift_env
$expected_env
$autoreboot_env
$autopc_env
$autoubb_env
$prejobdispatch_env
$ethpython_env
$group_env
ExecStart=$VENV/bin/python -m tt_device_mcp.server --socket $SOCK --no-http --log-dir /var/log/tt-device-broker
ExecStartPost=/bin/sh -c 'for i in \$(seq 1 50); do [ -S $SOCK ] && { chmod 0666 $SOCK; exit 0; }; sleep 0.1; done'
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
[ -n "$RENDER_ONLY" ] && exit 0

# Read-only smi grant (so `tt-device-mcp smi` works on a locked host).
install -m 0755 "$DEPLOY/tt-smi-ro.sh" /usr/local/bin/tt-device-mcp-smi-ro
install -m 0440 "$DEPLOY/tt-device-mcp-smi.sudoers" /etc/sudoers.d/tt-device-mcp-smi
# This sudoers file ships with the broker; if it fails visudo -c it is a build defect, not a
# host condition to tolerate. Remove it first — a syntactically invalid file in /etc/sudoers.d
# makes sudo refuse to run for the WHOLE host — then abort. Continuing would mark a build that
# silently dropped the smi grant as applied; aborting keeps auto-update on last-known-good until
# the shipped file is fixed.
visudo -cf /etc/sudoers.d/tt-device-mcp-smi >/dev/null 2>&1 \
    || { echo "apply-host-config: shipped smi sudoers fails visudo -c (build defect) — removed, aborting" >&2; rm -f /etc/sudoers.d/tt-device-mcp-smi; exit 1; }

# Fabric validator: the pinned build, so every host validates with identical code.
# The check script and the pin are refreshed here (they used to be written once by the
# installer and never updated, so fixes to them never reached a running host).
install -m 0755 "$DEPLOY/tt-device-fabric-check.sh"     "$ROOT/fabric-check.sh"
install -m 0755 "$DEPLOY/install-fabric-validator.sh"   "$ROOT/install-fabric-validator.sh"
install -m 0644 "$DEPLOY/fabric-validator.pin"          "$ROOT/fabric-validator.pin"
install -m 0644 "$DEPLOY/tt-device-fabric-validator.service" /etc/systemd/system/tt-device-fabric-validator.service
install -m 0644 "$DEPLOY/tt-device-fabric-validator.timer"   /etc/systemd/system/tt-device-fabric-validator.timer

# Slurm hooks: staged here too, not just by install-tt-device-broker.sh — auto-update never
# re-runs the installer, only this script (this file's own header comment), so an autoupdating
# host would otherwise never receive deploy/slurm/*.sh at all, and deploy/README.md's
# Prolog=/opt/tt-device-broker/slurm/prolog.sh would point at a path that never exists on it.
mkdir -p "$ROOT/slurm"
install -m 0755 "$DEPLOY/slurm/prolog.sh" "$ROOT/slurm/prolog.sh"
install -m 0755 "$DEPLOY/slurm/epilog.sh" "$ROOT/slurm/epilog.sh"

# Bus-lock counter: the discriminator between AMD erratum 1431 and a core stalled on a hung
# accelerator. Both crash the box identically; only 1431 needs a bus lock. See the unit.
mkdir -p /var/lib/tt-device-broker/health
install -m 0644 "$DEPLOY/tt-device-buslock.service" /etc/systemd/system/tt-device-buslock.service

# Rotation for the broker + per-job logs. This directory had no rule at all and had grown
# to 3.4GB; a full disk is a worse outage than anything the logs would have explained.
install -m 0644 "$DEPLOY/tt-device-broker.logrotate" /etc/logrotate.d/tt-device-broker

# Journal retention. The systemd default (4G cap) held ~6 days, too short to reconstruct a
# device drop from the kernel's AER/PCIe log; the drop-in lifts it to ~30 days.
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$DEPLOY/tt-device-broker-journald.conf" /etc/systemd/journald.conf.d/30-tt-device-retention.conf
# journald only re-reads its limits on restart; guarded because a retention change must never
# abort a host-config apply that the broker's own recovery depends on.
systemctl restart systemd-journald 2>/dev/null || true

# Reconcile timer/service + login self-heal + per-user client wiring.
install -m 0644 "$DEPLOY/tt-device-reconcile.service" /etc/systemd/system/tt-device-reconcile.service
install -m 0644 "$DEPLOY/tt-device-reconcile.timer"   /etc/systemd/system/tt-device-reconcile.timer
install -m 0644 "$DEPLOY/tt-device-mcp-login-heal.sh" /etc/profile.d/tt-device-mcp-heal.sh
cat > /etc/profile.d/tt-device-mcp-client.sh <<CLIENT
[ -x $ROOT/client-setup.sh ] && bash $ROOT/client-setup.sh >/dev/null 2>&1 || true
CLIENT
chmod 0644 /etc/profile.d/tt-device-mcp-client.sh

systemctl daemon-reload

# The timer retries a failed build; this only makes the FIRST attempt immediate rather
# than waiting out the first interval. --no-block because the build takes tens of
# minutes: an update must never wait on a compile, and until it lands the fabric check
# reports CANNOT CHECK rather than guessing.
systemctl enable --now tt-device-fabric-validator.timer >/dev/null 2>&1 \
    || { echo "apply-host-config: could not enable tt-device-fabric-validator.timer — host loses its authoritative fabric validator" >&2; exit 1; }
# `enable` can report success while the unit stays masked/unarmed; the host then silently
# runs with no fabric validation, so confirm the timer is actually armed before continuing.
systemctl is-active --quiet tt-device-fabric-validator.timer \
    || { echo "apply-host-config: tt-device-fabric-validator.timer enabled but not active — host loses its authoritative fabric validator" >&2; exit 1; }
# Only where perf can actually count it; a missing PMU event must not leave a failed unit.
if perf stat -a -e ls_locks.bus_lock -- true >/dev/null 2>&1; then
    systemctl enable --now tt-device-buslock.service >/dev/null 2>&1 || true
else
    echo "apply-host-config: ls_locks.bus_lock PMU event unavailable; bus-lock counter not enabled" >&2
fi
pinned_sha="$(sed -n 's/^TTDEV_VALIDATOR_SHA=//p' "$ROOT/fabric-validator.pin" 2>/dev/null | head -1)"
if [ -n "$pinned_sha" ] && [ ! -x "/opt/tt-device-broker/validator/$pinned_sha/build/tools/scaleout/run_cluster_validation" ]; then
    echo "apply-host-config: fabric validator for $pinned_sha not built; starting build out of band" >&2
    systemctl reset-failed tt-device-fabric-validator.service 2>/dev/null || true
    systemctl start --no-block tt-device-fabric-validator.service 2>/dev/null || true
fi

# Reconcile the login banner text with the lock state (write when locked, remove
# when not) so a banner change lands on update without re-running `lock`.
"$VENV/bin/tt-device-mcp" refresh-banner 2>/dev/null || true
