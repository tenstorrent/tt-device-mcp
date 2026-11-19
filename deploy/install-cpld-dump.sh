#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Install the pinned vendor CPLD DIAG script (deploy/cpld-dump.pin) where the broker looks for it.
# The file comes from the operator (copied from a checkout of its source repo); this only checks
# its sha256 and puts it in place, atomically. Undo: rm "$TTDEV_CPLD_DUMP_PATH".
set -euo pipefail

SRC="${1:?usage: install-cpld-dump.sh <BH_s7t_cpld_dump_V1.03.01.sh> [pin]}"
PIN="${2:-$(dirname "$0")/cpld-dump.pin}"
# shellcheck disable=SC1090
. "$PIN"

got=$(sha256sum "$SRC" | cut -d' ' -f1)
if [ "$got" != "$TTDEV_CPLD_DUMP_SHA256" ]; then
    echo "install-cpld-dump: $SRC has sha256 $got, pin wants $TTDEV_CPLD_DUMP_SHA256" >&2
    exit 1
fi
dir=$(dirname "$TTDEV_CPLD_DUMP_PATH")
install -d -m 0755 "$dir"
install -m 0755 "$SRC" "$TTDEV_CPLD_DUMP_PATH.new"
mv -f "$TTDEV_CPLD_DUMP_PATH.new" "$TTDEV_CPLD_DUMP_PATH"
echo "install-cpld-dump: $TTDEV_CPLD_DUMP_VERSION at $TTDEV_CPLD_DUMP_PATH (sha256 $got)"
