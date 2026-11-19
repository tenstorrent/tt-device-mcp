#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Template for TT_DEVICE_MCP_MANUAL_RECOVERY_CMD (spec 04 I27): is an operator recovering this
# host right now? The broker stands back from its own off-bus power cycle while this says yes.
#
# Looks in the sudo journal and in auth.log (TT_MANUAL_RECOVERY_AUTHLOG, default /var/log/auth.log,
# skipped only when absent) for a sudo-run reboot, poweroff, shutdown, halt, kexec or an ipmitool
# power action (chassis power cycle/reset/off/on/soft, mc reset, a raw chassis-control or BMC reset)
# within the last TT_MANUAL_RECOVERY_WINDOW_MIN minutes (default 30). Read-only BMC queries
# (sensor, sdr, fru, chassis power status, dcmi power reading) do not count.
#
#   exit 0   an operator recovery was seen (stand back); the matching line is printed
#   exit 1   none seen
#   exit 2   the journal or an existing auth.log could not be read, or the journal hides other
#            users' entries (the broker stands back on this too: unknown is not "none")
#
# Usage: tt-manual-recovery-check.sh [window-minutes]
set -u
window="${1:-${TT_MANUAL_RECOVERY_WINDOW_MIN:-30}}"
case "$window" in '' | *[!0-9]*) echo "bad window: $window" >&2; exit 2 ;; esac
journal="${TT_MANUAL_RECOVERY_JOURNALCTL:-journalctl}"
# No -q: a caller outside the journal's groups gets only its own entries and a warning, which -q
# would hide; that warning is an unreadable journal, not an empty one.
if ! out="$("$journal" _COMM=sudo --since "-${window}min" --no-pager -o short-iso 2>&1)"; then
    echo "journal unreadable: ${out: -200}" >&2
    exit 2
fi
hidden='insufficient permissions|not seeing messages from other users|no journal files'
if printf '%s\n' "$out" | grep -qiE "$hidden"; then
    echo "journal unreadable: ${out: -200}" >&2
    exit 2
fi
recovery() {
    grep -E 'COMMAND=' | grep -E \
        -e '(^|[/ =])(reboot|poweroff|shutdown|halt|kexec)( |$)' \
        -e 'systemctl +(reboot|poweroff|halt|kexec)' \
        -e 'ipmitool .*power +(cycle|reset|off|on|soft)( |$)' \
        -e 'ipmitool .*mc +reset( |$)' \
        -e 'ipmitool .*raw +(0x)?0*6 +(0x)?0*[23]( |$)' \
        -e 'ipmitool .*raw +(0x)?0*0 +(0x)?0*2( |$)'
}
hit="$(printf '%s\n' "$out" | recovery | tail -n 1)"
authlog="${TT_MANUAL_RECOVERY_AUTHLOG:-/var/log/auth.log}"
if [ -z "$hit" ] && [ -e "$authlog" ] && [ ! -r "$authlog" ]; then
    echo "auth.log unreadable: $authlog" >&2
    exit 2
fi
if [ -z "$hit" ] && [ -e "$authlog" ]; then
    # auth.log stamps lines either RFC 3339 (2026-10-13T19:34:12.5+00:00) or syslog style
    # (Oct 13 19:34:12); a line whose stamp does not parse is skipped. grep exits 1 when no line
    # matches; above that it could not read the file, which leaves the answer unknown.
    sudo_lines="$(grep -a 'sudo' "$authlog")"
    if [ "$?" -gt 1 ]; then
        echo "auth.log unreadable: $authlog" >&2
        exit 2
    fi
    cutoff=$(( $(date +%s) - window * 60 ))
    while IFS= read -r line; do
        # Split into words without glob expansion: a '*' in a line must not expand to file names.
        read -ra f <<< "$line"
        case "${f[0]:-}" in [0-9]*) ts="${f[0]}" ;; *) ts="${f[0]:-} ${f[1]:-} ${f[2]:-}" ;; esac
        t="$(date -d "$ts" +%s 2>/dev/null)" || continue
        [ "$t" -ge "$cutoff" ] && hit="$line"
    done < <(printf '%s\n' "$sudo_lines" | recovery | tail -n 50)
fi
if [ -n "$hit" ]; then
    echo "$hit"
    exit 0
fi
exit 1
