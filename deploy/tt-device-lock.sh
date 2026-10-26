#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The host side of the bare-metal device lock (spec 05 I9). Installed as $ROOT/device-lock.sh.
#
#   device-lock.sh apply <deploy-dir>   (apply-host-config, only under TTDEV_LOCK=1)
#       Create the group, empty it of people (uid >= 1000: each member opens the node without the
#       broker), install the udev rule and reload udev's rules. It does NOT trigger: the broker
#       re-applies the rule to the nodes itself, between jobs, once it runs with the device group.
#   device-lock.sh check                (reconcile, wherever the unit carries the device group)
#       Every node must be 0660 group ttdev. Once all are, mark the lock in effect for this boot.
#       Drift after that: re-trigger udev and re-stat; drift that persists is logged, recorded for
#       /health and journalled as one `device_lock_drift` event per episode. Members are named.
#       It never unlocks: undoing the lock is `tt-device-mcp unlock`, an operator's call.
#
# Opens no device node: it stats them, reads the group database and asks udev to re-apply rules.
# Paths are overridable only so tests can point them at a sandbox.
set -u
GROUP=ttdev
MIN_UID=1000
DEV_DIR="${TTDEV_DEV_DIR:-/dev/tenstorrent}"
RUN="${TTDEV_RUN_DIR:-/run/tt-device-broker}"
RULES_DIR="${TTDEV_UDEV_RULES_DIR:-/etc/udev/rules.d}"
EVENTS="${TT_DEVICE_MCP_HEALTH_DIR:-/var/lib/tt-device-broker/health}/health_events.jsonl"
APPLIED="$RUN/device_lock_applied"
DRIFT="$RUN/device_lock_drift"
PENDING="$RUN/device_lock_pending"
MEMBERS="$RUN/device_lock_members"

log() { echo "device-lock: $*" >&2; }

# Members with uid >= 1000, one per line. A name with no passwd entry cannot log in: skipped.
human_members() {
    local list u uid
    list="$(getent group "$GROUP" 2>/dev/null | cut -d: -f4)"
    for u in ${list//,/ }; do
        uid="$(getent passwd "$u" 2>/dev/null | cut -d: -f3)"
        [ -n "$uid" ] && [ "$uid" -ge "$MIN_UID" ] 2>/dev/null && echo "$u"
    done
    return 0
}

nodes() {
    local n
    for n in "$DEV_DIR"/*; do
        case "${n##*/}" in ''|*[!0-9]*) continue ;; esac
        [ -e "$n" ] && echo "$n"
    done
    return 0
}

# Nodes not 0660 group ttdev, one per line. A node gone since the glob is a chip leaving the bus,
# not drift; any other stat failure is drift.
drifted() {
    local n st
    while read -r n; do
        [ -n "$n" ] || continue
        st="$(stat -c '%a %G' "$n" 2>/dev/null)" || { [ -e "$n" ] && echo "$n"; continue; }
        [ "$st" = "660 $GROUP" ] || echo "$n"
    done < <(nodes)
    return 0
}

json_list() { sed -e '/^$/d' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/.*/"&"/' | paste -sd, -; }

report_members() {
    local m
    m="$(human_members)"
    if [ -z "$m" ]; then rm -f "$MEMBERS" 2>/dev/null; return 0; fi
    # Name them once per change, not on every pass of the timer.
    [ "$(cat "$MEMBERS" 2>/dev/null)" = "$m" ] && return 0
    while read -r u; do log "lock not effective for $u (member of $GROUP)"; done <<< "$m"
    printf '%s\n' "$m" > "$MEMBERS" 2>/dev/null || true
}

cmd_apply() {
    local deploy="${1:?usage: device-lock.sh apply <deploy-dir>}" u
    getent group "$GROUP" >/dev/null 2>&1 || groupadd --system "$GROUP" \
        || { log "cannot create group $GROUP"; return 1; }
    while read -r u; do
        [ -n "$u" ] || continue
        if gpasswd -d "$u" "$GROUP" >/dev/null 2>&1; then
            log "removed $u from $GROUP (a member opens the device without the broker)"
        else
            log "lock not effective for $u: could not remove from $GROUP"
        fi
    done < <(human_members)
    install -m 0644 "$deploy/99-tenstorrent-ttdev.rules" "$RULES_DIR/99-tenstorrent-ttdev.rules" \
        || { log "cannot install the udev rule"; return 1; }
    udevadm control --reload-rules 2>/dev/null || log "udevadm control --reload-rules failed"
    return 0
}

cmd_check() {
    local bad now nl
    mkdir -p "$RUN" 2>/dev/null || true
    report_members
    bad="$(drifted)"
    if [ -z "$bad" ]; then
        [ -n "$(nodes)" ] && { date +%s > "$APPLIED"; rm -f "$DRIFT" "$PENDING"; }
        return 0
    fi
    if [ ! -e "$APPLIED" ]; then
        # Not yet in effect this boot: the broker applies the rule between jobs. A trigger here
        # could cut off a job that started before the broker ran with the device group.
        [ -e "$PENDING" ] || { log "lock configured, not yet in effect on: ${bad//$'\n'/ }"; : > "$PENDING"; }
        return 0
    fi
    log "lock drifted on: ${bad//$'\n'/ }; re-triggering udev"
    udevadm trigger --subsystem-match=tenstorrent 2>/dev/null || true
    udevadm settle --timeout=20 2>/dev/null || true
    bad="$(drifted)"
    [ -z "$bad" ] && { rm -f "$DRIFT"; return 0; }
    log "lock drift persists on: ${bad//$'\n'/ } (lock not in effect there; never unlocked automatically)"
    [ -e "$DRIFT" ] && return 0
    now="$(date +%s.%N)"
    nl="$(printf '%s\n' "$bad" | json_list)"
    printf '{"ts": %s, "iso": "%s", "source": "reconcile", "nodes": [%s]}\n' \
        "$now" "$(date -u -d "@${now%.*}" +%Y-%m-%dT%H:%M:%SZ)" "$nl" > "$DRIFT" 2>/dev/null || true
    printf '{"ts": %s, "iso": "%s", "kind": "device_lock_drift", "source": "reconcile", "nodes": [%s]}\n' \
        "$now" "$(date -u -d "@${now%.*}" +%Y-%m-%dT%H:%M:%SZ)" "$nl" >> "$EVENTS" 2>/dev/null \
        || log "warning: could not append device_lock_drift to $EVENTS"
    return 0
}

case "${1:-}" in
    apply) shift; cmd_apply "$@" ;;
    check) cmd_check ;;
    *) echo "usage: device-lock.sh apply <deploy-dir> | check" >&2; exit 2 ;;
esac
