#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Reconcile the expected host state + wire every user's MCP clients. Run once a
# minute by tt-device-reconcile.timer (and once at boot). Idempotent and quiet
# when nothing drifts; logs what it fixes to the journal. New Claude/Cursor
# installs get wired within a minute.
set -u
[ -r /etc/default/tt-device-broker ] && . /etc/default/tt-device-broker
R="${TTDEV_ROOT:-/opt/tt-device-broker}"
SOCK=/run/tt-device-broker/broker.sock
GROUP=ttdev

# 0) Auto-update: pull the tracked branch and, if idle, reinstall + restart.
#    Self-gated (no-op unless TTDEV_AUTOUPDATE=1); applies only in an idle window.
[ -x "$R/autoupdate.sh" ] && "$R/autoupdate.sh" || true

# 1) Broker service: systemd handles normal restarts (Restart=on-failure); only
#    intervene if it's not active (e.g. failed/exhausted), so we don't fight it.
state="$(systemctl is-active tt-device-broker 2>/dev/null || true)"
if [ "$state" != "active" ]; then
    echo "reconcile: broker is '$state' -> resetting + starting" >&2
    systemctl reset-failed tt-device-broker 2>/dev/null || true
    systemctl start tt-device-broker 2>/dev/null || true
fi

# 1b) Stray broker: a SECOND broker the system unit does not manage — a legacy per-user daemon
#     from before `daemon start` learned to defer to the system broker, or a hand-started one. It
#     fragments the device queue and drifts to an old version unseen. We do NOT stop it (it may be
#     a deliberate per-user daemon); we make it LOUD so it cannot hide.
#     Enumerate from LISTENERS, not pgrep: a real broker binds a socket/port, whereas an ephemeral
#     stdio-adapter client and a pgrep self-match do not — starting from `ss` avoids flagging those.
#     A listener running tt_device_mcp.server but outside the unit's cgroup is the stray (the unit's
#     own MainPID and children stay in its cgroup). Dedup on the pid set so a persistent stray logs
#     on change, not every one-minute lap.
strays=""
for pid in $( { ss -lnpxH 2>/dev/null; ss -tlnpH 2>/dev/null; } \
             | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
    grep -qa 'tt_device_mcp\.server' "/proc/$pid/cmdline" 2>/dev/null || continue
    grep -q 'tt-device-broker\.service' "/proc/$pid/cgroup" 2>/dev/null && continue
    strays="$strays $pid"
done
strays="${strays# }"
seen=/run/tt-device-broker/stray_brokers
if [ -n "$strays" ]; then
    if [ "$strays" != "$(cat "$seen" 2>/dev/null || true)" ]; then
        for pid in $strays; do
            cl="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-160)"
            echo "reconcile: STRAY broker pid=$pid not managed by tt-device-broker.service" \
                 "(fragments the device queue; not stopped automatically): $cl" >&2
        done
        printf '%s' "$strays" > "$seen" 2>/dev/null || true
    fi
elif [ -e "$seen" ]; then
    rm -f "$seen" 2>/dev/null || true
fi

# 2) Socket world-connectable (identity is by SO_PEERCRED, not perms).
if [ -S "$SOCK" ] && [ "$(stat -c %a "$SOCK" 2>/dev/null)" != "666" ]; then
    echo "reconcile: fixing socket perms" >&2
    chmod 0666 "$SOCK" 2>/dev/null || true
fi

# 3) System CLI symlink points at the broker venv. Must be on PATH so both the
#    human CLI and the tt-buddy plugin's bare `tt-device-mcp` command resolve.
t="$R/venv/bin/tt-device-mcp"
if [ -x "$t" ] && [ "$(readlink -f /usr/local/bin/tt-device-mcp 2>/dev/null)" != "$(readlink -f "$t" 2>/dev/null)" ]; then
    echo "reconcile: repointing /usr/local/bin/tt-device-mcp -> $t" >&2
    ln -sf "$t" /usr/local/bin/tt-device-mcp
fi

# 4) Lock drift: if lockdown is configured, the device node must stay group $GROUP.
if grep -q 'TT_DEVICE_MCP_DEVICE_GROUP' /etc/systemd/system/tt-device-broker.service 2>/dev/null; then
    node="$(ls /dev/tenstorrent/* 2>/dev/null | head -1 || true)"
    if [ -n "$node" ] && [ "$(stat -c %G "$node" 2>/dev/null)" != "$GROUP" ]; then
        echo "reconcile: device lock drifted (group != $GROUP) -> re-triggering udev" >&2
        udevadm trigger --subsystem-match=tenstorrent 2>/dev/null || true
    fi
fi

# 4b) Age out the per-job logs. They are write-once files the broker reads back by globbing
#     `*.log` for content — the ledger, and the job-id floor — so logrotate must never match
#     them: every stanza it offers renames or truncates the live file, which blanks the whole
#     ledger in one nightly run. Deleting entire files older than 30 days is the requirement,
#     and `find -mtime` does that without touching a current one. The second sweep clears the
#     `.log.1.gz` an earlier rotation rule left behind.
LOGS=/var/log/tt-device-broker
if [ -d "$LOGS" ]; then
    find "$LOGS" -maxdepth 1 -type f -name '*_*.log' -mtime +30 -delete 2>/dev/null || true
    find "$LOGS" -maxdepth 1 -type f -name '*_*.log.*gz' -mtime +30 -delete 2>/dev/null || true
fi

# 5) Per-user MCP client wiring. client-setup self-detects Claude/Cursor and is a
#    no-op when already correct, so this catches a freshly-installed client within
#    a minute. Login shell (-l) so the user's PATH (where `claude` may live) loads.
cs="$R/client-setup.sh"
if [ -x "$cs" ]; then
    getent passwd | awk -F: '$3>=1000 && $3<65000 && $6 ~ "^/home/" {print $1":"$6}' | while IFS=: read -r u home; do
        # Only spend a login shell on users who actually have Claude or Cursor
        # (their config dir exists), so most users cost nothing.
        if [ -e "$home/.claude.json" ] || [ -d "$home/.cursor" ]; then
            sudo -u "$u" bash -lc "$cs" >/dev/null 2>&1 || true
        fi
    done
fi
