#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The mode sweep of spec 06 I9 (spec 08, Migration sweep). SOURCED, never run, by
# apply-host-config.sh and install-crash-recorder.sh, which both run from the repo's deploy/
# directory beside this file:
#
#     sweep_to_mode <mode> <root> [find options and tests...]
#
# brings every path under <root> that matches the tests and is not already at <mode> to <mode>,
# and returns non-zero only when a chmod fails on a path that still exists.
#
# It runs against a live broker, which deletes short-lived files under these trees. An entry that
# vanishes inside find's own traversal is not an error (-ignore_readdir_race, GNU find; the deploy
# target is GNU/Linux, spec 09), and each path is chmod'ed on its own so a path gone by its chmod
# is skipped instead of failing the caller. `! -perm` makes a tree already at mode a no-op, and -P
# never follows a symlink (stats/current points at a file asserted at its own path).

sweep_to_mode() {
    local mode="$1" root="$2"
    shift 2
    find -P "$root" -ignore_readdir_race "$@" ! -perm "$mode" -exec sh -c '
        mode="$1"
        shift
        for p; do
            [ -e "$p" ] || continue
            chmod "$mode" "$p" || [ ! -e "$p" ] || exit 1
        done' _ "$mode" {} +
}
