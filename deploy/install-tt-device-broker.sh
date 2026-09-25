#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# One-command installer for the tt-device broker. Run once per host from a
# checkout of this repo, as a sudoer:
#
#     sudo deploy/install-tt-device-broker.sh            # locks the device (hard exclusion)
#     sudo TTDEV_NO_LOCK=1 deploy/install-tt-device-broker.sh   # cooperative (no udev lock)
#
# Idempotent. Builds a self-contained venv, installs the broker as a systemd
# service (socket-only, privsep), puts `tt-device-mcp` on every user's PATH, wires
# the login self-heal, and (unless TTDEV_NO_LOCK) applies the udev lock — but only
# after a self-check proves an admitted job can still open the device; if it
# can't, the lock is reverted and the host stays cooperative rather than bricked.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "error: run with sudo" >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
R=/opt/tt-device-broker
GROUP=ttdev
SOCK=/run/tt-device-broker/broker.sock

echo "==> build venv + install broker from $REPO"
mkdir -p "$R"; rm -rf "$R/venv"
python3 -m venv "$R/venv"
"$R/venv/bin/pip" install -q --upgrade pip
"$R/venv/bin/pip" install -q "$REPO"
rm -rf "$R/wheel"; "$R/venv/bin/pip" wheel -q --no-deps -w "$R/wheel" "$REPO"
chmod -R a+rX "$R"

echo "==> CLI on PATH"
# One binary: a CLI for humans and, when spawned by an MCP client with a piped
# stdin, the stdio adapter. The plugin's mcpServers entry is the bare
# "tt-device-mcp"; this symlink is what makes that resolve.
ln -sf "$R/venv/bin/tt-device-mcp" /usr/local/bin/tt-device-mcp

# Device locking is OPT-IN — shared hosts only. Default is cooperative: the device
# stays world-rw and bare-metal works, which is correct for reserved/single-user
# boxes (most of the fleet). Lock a shared host with TTDEV_LOCK=1; it's applied
# after the broker is up via the idempotent `tt-device-mcp lock` (self-checks and
# reverts if an admitted job can't open the node).
LOCK="${TTDEV_LOCK:-0}"
[ "${TTDEV_NO_LOCK:-}" = "1" ] && LOCK=0   # back-compat: forces cooperative

echo "==> detect machine type (reset mode + fabric check)"
# Reset command is machine-type-specific: a Galaxy needs -glx_reset (all ASICs);
# loudbox / n150 / n300 use -r on targets. Detect from tt-smi's authoritative
# board_type (the device is healthy at install time). Persisted to /etc/default so
# the unit renders identically from the installer and from auto-update.
RESET_MODE="${TT_DEVICE_MCP_RESET_MODE:-}"
if [ -z "$RESET_MODE" ] && timeout 30 "$R/venv/bin/tt-smi" -s 2>/dev/null | "$R/venv/bin/python" -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(2)
bt = [x.get('board_info', {}).get('board_type', '') or '' for x in d.get('device_info', [])]
sys.exit(0 if any('galaxy' in b.lower() for b in bt) else 1)" 2>/dev/null; then
    RESET_MODE=galaxy
fi
[ "$RESET_MODE" = galaxy ] && echo "    machine: Galaxy (board_type) -> reset uses -glx_reset"

# Post-job fabric traffic check (run_cluster_validation) — EVERY machine. Chip liveness cannot see an
# ethernet core that never retrained, and with no fabric evidence the health gate falls back to a full
# board reset after every abnormal exit. The traffic pass needs no cabling descriptor (it drives the
# links discovery found), so it is not galaxy-specific; only the golden-topology comparison is, and
# that is what the descriptor below opts into. A host that cannot run the check reports CANNOT CHECK
# and the gate leaves the device alone.
# Required validator: a failed stage is fatal under `set -e`, not swallowed — a broker
# with no fabric-check falls back to a full board reset after every abnormal exit.
install -m 0755 "$REPO/deploy/tt-device-fabric-check.sh" "$R/fabric-check.sh"
FABRIC_CHECK_CMD="$R/fabric-check.sh"
FABRIC_DESCRIPTOR=""

# Passive eth-heartbeat pre-read (runs before the traffic pass; frozen core -> hold, not reset).
# The resolution/classification logic that used to be a staged wrapper script now lives
# in-package (health.monitors.eth) and runs built-in, same as the fabric check — only the
# ttexalens probe itself still needs staging, since it imports a native package tt-metal
# builds, never the broker's own venv. Deliberately NOT wired to a verdict yet: the probe
# self-blocks (TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN) until a root ttexalens env is provisioned
# and the read's device-attach is validated on a reserved box.
# Optional pre-read (doctrine): staging is best-effort, so a failure is not fatal — but
# it is loud, never a silent swallow, so an operator arming the heartbeat later can see
# the artifact is missing.
install -m 0755 "$REPO/deploy/tt-device-eth-heartbeat-probe.py" "$R/eth-heartbeat-probe.py" \
    || echo "warn: eth-heartbeat probe not staged (optional pre-read unavailable): $R/eth-heartbeat-probe.py" >&2
# Remove the retired wrapper a host installed before it was retired may still have on disk
# (apply-host-config.sh, run on every auto-update, also scrubs this and any /etc/default value
# still pointing at it — this covers the manual re-install path too).
rm -f "$R/eth-heartbeat-check.sh"
[ "$RESET_MODE" = galaxy ] && \
    FABRIC_DESCRIPTOR="$R/validator/current/tools/tests/scaleout/cabling_descriptors/bh_galaxy_xy_torus.textproto"

# Slurm hooks: staged so deploy/README.md's Prolog=/Epilog= paths actually exist on the host.
# Required (like fabric-check.sh above): a site pointing slurm.conf at a path the installer
# never created gets a Prolog that exits 127 on every node, indistinguishable from every node
# being unfit.
mkdir -p "$R/slurm"
install -m 0755 "$REPO/deploy/slurm/prolog.sh" "$R/slurm/prolog.sh"
install -m 0755 "$REPO/deploy/slurm/epilog.sh" "$R/slurm/epilog.sh"

echo "==> write config + per-user client wiring"
# Auto-update tracks main; a host on a dev branch overrides TTDEV_BRANCH here. The source
# itself is not configurable — it is a constant in autoupdate.sh and only the branch is
# settable (spec 08 I16) — so a host can never be pointed at a fork, and therefore never at
# a private one, which is the only thing that would need a credential. Root fetches the
# public repo itself; there is no deploy identity to configure.
TTDEV_BRANCH="${TTDEV_BRANCH:-main}"
# On by default. An operator who does not want a self-updating host says so outright:
#
#     sudo TTDEV_AUTOUPDATE=0 ./install.sh
#
# The assignment goes AFTER sudo. sudo resets the environment (env_reset), so a variable
# exported in the caller's shell is dropped before this script runs and the default wins —
# the host would self-update while the operator believed they had declined.
AUTOUPDATE="${TTDEV_AUTOUPDATE:-1}"
# TTDEV_BRANCH goes through printf %q, not quote characters: this file is sourced, and a branch
# containing an apostrophe would close the literal and have the rest of the value parsed as
# shell. It is only ever sourced by bash (nothing reads it as a systemd EnvironmentFile), so
# bash's own escaping is what it must round-trip through.
cat > /etc/default/tt-device-broker <<CONF
TTDEV_ROOT=$R
TTDEV_VENV=$R/venv
TTDEV_RESET_MODE=$RESET_MODE
TTDEV_FABRIC_CHECK_CMD=$FABRIC_CHECK_CMD
TTDEV_FABRIC_DESCRIPTOR=$FABRIC_DESCRIPTOR
TTDEV_AUTOUPDATE=$AUTOUPDATE
TTDEV_BRANCH=$(printf %q "$TTDEV_BRANCH")
CONF
install -m 0755 "$REPO/deploy/tt-device-client-setup.sh" "$R/client-setup.sh"
install -m 0755 "$REPO/deploy/tt-device-reconcile.sh" "$R/reconcile.sh"
install -m 0755 "$REPO/deploy/tt-device-autoupdate.sh" "$R/autoupdate.sh"

echo "==> apply host config (unit + watchdog + sudoers + reconcile units + hooks)"
# Single source of truth, shared with auto-update — so a change to the unit,
# sudoers, or reconcile units reaches every host on the next version bump with no
# manual installer re-run.
bash "$REPO/deploy/apply-host-config.sh" "$REPO"

# `enable --now` does not restart an already-active unit, so an upgrade needs the explicit restart
# to leave the rebuilt venv running.
systemctl enable tt-device-broker
systemctl restart tt-device-broker
systemctl enable --now tt-device-reconcile.timer >/dev/null 2>&1 \
    || { echo "install: could not enable tt-device-reconcile.timer — agent wiring and config reconcile will not run" >&2; exit 1; }
# A unit that 'enabled' cleanly can still be masked/unarmed, leaving users unwired and
# config drift unreconciled; confirm it is armed before declaring the install done.
systemctl is-active --quiet tt-device-reconcile.timer \
    || { echo "install: tt-device-reconcile.timer enabled but not active — agent wiring and config reconcile will not run" >&2; exit 1; }
# Wire the invoking user now (don't make them wait for login/timer).
[ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ] \
    && sudo -u "$SUDO_USER" bash -lc "$R/client-setup.sh" >/dev/null 2>&1 || true

echo "==> wait for broker /health"
# `enable --now` returns once the unit launched, not once the socket serves — the broker
# may still be binding, or may have started and then crashed. On a locked host a dead broker
# is a total device outage, so a broker that never answers /health is a failed install, not a
# warning: poll until it answers, then hard-fail with where to look.
BROKER_UP=0
for _ in $(seq 1 30); do
    if curl -sf --max-time 5 --unix-socket "$SOCK" http://localhost/health >/dev/null 2>&1; then
        BROKER_UP=1; break
    fi
    sleep 1
done
[ "$BROKER_UP" = 1 ] \
    || { echo "install: broker did not answer /health after enable — check: journalctl -u tt-device-broker -e" >&2; exit 1; }
echo "==> health:"; curl -s --max-time 5 --unix-socket "$SOCK" http://localhost/health

MODE="COOPERATIVE (bare-metal allowed; route runs through tt-device-mcp)"
if [ "$LOCK" = "1" ]; then
    echo "==> locking device (shared host)"
    if "$R/venv/bin/tt-device-mcp" lock; then
        MODE="LOCKED (bare-metal denied; all device work via the broker)"
    else
        echo "    !! lock failed — host stays cooperative. Re-run: sudo tt-device-mcp lock" >&2
    fi
fi
echo
echo "DONE — mode: $MODE"
echo "  Shell (any user, no setup): tt-device-mcp run \"pytest ...\""
echo "  Claude/Cursor: wired automatically — at login, and within ~1 min for already-logged-in"
echo "                 users (reconcile timer). Restart an open Claude/Cursor to pick it up."
echo "  Lock policy: cooperative by default; lock a SHARED host with: sudo tt-device-mcp lock"
