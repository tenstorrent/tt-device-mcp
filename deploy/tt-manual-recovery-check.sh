#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Template for TT_DEVICE_MCP_MANUAL_RECOVERY_CMD (spec 04 I27): is an operator recovering this
# host right now? The broker stands back from its own off-bus power cycle while this says yes.
#
# Looks in the journal for a sudo-run reboot, poweroff, shutdown, halt, kexec or a power/reset
# ipmitool command within the last TT_MANUAL_RECOVERY_WINDOW_MIN minutes (default 30). Read-only
# BMC queries (sensor, sdr, fru) do not count.
#
#   exit 0   an operator recovery was seen (stand back); the matching line is printed
#   exit 1   none seen
#   exit 2   the journal could not be read (the broker stands back on this too)
#
# Usage: tt-manual-recovery-check.sh [window-minutes]
set -u
window="${1:-${TT_MANUAL_RECOVERY_WINDOW_MIN:-30}}"
case "$window" in '' | *[!0-9]*) echo "bad window: $window" >&2; exit 2 ;; esac
journal="${TT_MANUAL_RECOVERY_JOURNALCTL:-journalctl}"
if ! out="$("$journal" _COMM=sudo --since "-${window}min" --no-pager -q -o short-iso 2>&1)"; then
    echo "journal unreadable: ${out: -200}" >&2
    exit 2
fi
hit="$(printf '%s\n' "$out" | grep -E 'COMMAND=' | grep -E \
    -e '(^|[/ =])(reboot|poweroff|shutdown|halt|kexec)( |$)' \
    -e 'systemctl +(reboot|poweroff|halt|kexec)' \
    -e 'ipmitool .*(power|reset)' | tail -n 1)"
if [ -n "$hit" ]; then
    echo "$hit"
    exit 0
fi
exit 1
