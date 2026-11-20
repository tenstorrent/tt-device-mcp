#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The deploy scripts' logging helper (spec 06 I10). SOURCED, never run: the scripts staged under
# $TTDEV_ROOT (reconcile.sh, autoupdate.sh, install-fabric-validator.sh, device-lock.sh) find it
# beside them as log.sh; apply-host-config.sh, run from the repo's deploy/, sources it here under
# its repo name.
#
#     . "$(dirname "$(readlink -f "$0")")/log.sh"
#     TTDEV_LOG_NAME=reconcile
#     log "broker is 'failed' -> resetting + starting"
#
# `log` never ends its caller: the validator runs under `set -e`, so every write is guarded and
# `log` returns 0 even when the log dir is unwritable (the stderr copy still goes out).
# TTDEV_LOG_STDERR_ONLY=1 writes no file, for apply-host-config's `--print-unit` render, which
# writes no host path. An empty TTDEV_LOG_DIR means unset, as for every other env var here; it is
# resolved per call, so a value /etc/default sets after this file is sourced still applies.

_TTDEV_LOG_WARNED=""

log() {
    local name stamp line dir file
    # Keeps a `log` call safe under `set -u` before the caller has set its name.
    name="${TTDEV_LOG_NAME:-unnamed}"
    # The shell's own clock (bash >= 4.2), no process per line; `date` where the builtin is missing.
    TZ=UTC0 printf -v stamp '%(%Y-%m-%d %H:%M:%S)T UTC' -1 2>/dev/null \
        || stamp="$(date -u '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || true)"
    line="$stamp | $name | $*"
    if [ "${TTDEV_LOG_STDERR_ONLY:-0}" = 1 ]; then
        printf '%s\n' "$line" >&2 || true
        return 0
    fi
    dir="${TTDEV_LOG_DIR:-/var/log/tt-device-broker}"
    file="$dir/$name.log"
    # umask 022 in a subshell: the dir and the file are created at 0755/0644 (spec 06 I9) whatever
    # the caller's umask, without changing it, including a file `>>` recreates after logrotate's
    # rename. No `install`: autoupdate's lap tests stand it in as a stage recorder.
    # 2>/dev/null BEFORE >>: bash applies redirections left to right, so the other order would
    # still print bash's own "Not a directory" to stderr on every call, not the one warning below.
    if ! (umask 022; { [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null; } \
        && printf '%s\n' "$line" 2>/dev/null >> "$file"); then
        if [ -z "$_TTDEV_LOG_WARNED" ]; then
            _TTDEV_LOG_WARNED=1
            printf '%s\n' "$stamp | $name | WARNING: cannot write $file; this process logs to stderr only" >&2 || true
        fi
    fi
    printf '%s\n' "$line" >&2 || true
    return 0
}
