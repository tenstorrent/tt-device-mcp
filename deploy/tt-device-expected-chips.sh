#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# How many chips this host SHOULD have, from what the machine IS, never from how many chips are on
# the bus right now. Counting enumerated chips would bake a tray or card that is already missing at
# install time into the bar, and the broker could then never see that drop (spec 08 I28).
#
# Called by apply-host-config.sh only when neither the operator (TTDEV_EXPECTED_CHIPS) nor the
# current unit gives a count. Reads no device: every input is already gathered or is host firmware.
#
# Inputs (environment):
#   TTDEV_BOARD_TYPES       board_type of every chip that answered the installer's tt-smi snapshot,
#                           one per line (empty on auto-update and when no chip answered). The
#                           count of lines is never used, only the types.
#   TTDEV_RESET_MODE        the host's reset mode ("galaxy" was itself set from board_type).
#   TTDEV_DMI_PRODUCT_FILE  the DMI product name (default /sys/class/dmi/id/product_name).
#
# Output: exit 0 and "<count><TAB><source>" when a source decides; exit 3 and the reason on stdout
# when none can tell. Never guesses: a card board (n150/n300/p100/p150/p300) says how many chips one
# card has, not how many cards the box holds, so it decides nothing; neither does any disagreement.
set -uo pipefail

GALAXY_CHIPS=32

norm() {  # <var> <text>
    # Case-folded (ASCII letters only, as tr does), trimmed, without the " L"/" R" suffix tt-smi
    # puts on its per-chip copy, into the variable named $1. Shell builtins only: it runs once per
    # chip, and a tr|sed pipeline per call cost several processes each.
    local n_b="${2,,[ABCDEFGHIJKLMNOPQRSTUVWXYZ]}"
    n_b="${n_b#"${n_b%%[![:space:]]*}"}"
    n_b="${n_b%"${n_b##*[![:space:]]}"}"
    case "$n_b" in *" l" | *" r") n_b="${n_b%??}" ;; esac
    n_b="${n_b%"${n_b##*[![:space:]]}"}"
    printf -v "$1" '%s' "$n_b"
}

# Board-type vote: galaxy boards make the whole system (4 trays x 8 chips, Wormhole or Blackhole).
board_vote="" board_name="" board_why="" b=""
while IFS= read -r raw; do
    norm b "$raw"
    case "$b" in
        "" | n/a) continue ;;
        *galaxy*) v=galaxy ;;
        n150* | n300* | p100* | p150* | p300*) v=card ;;
        *) v=unknown ;;
    esac
    if [ -z "$board_vote" ]; then
        board_vote="$v" board_name="$b"
    elif [ "$board_vote" != "$v" ]; then
        board_vote=conflict board_why="mixed board types ($board_name, $b)"
    fi
    [ "$v" = unknown ] && [ "$board_vote" != conflict ] && board_why="unknown board type '$b'"
done <<< "${TTDEV_BOARD_TYPES:-}"

dmi_file="${TTDEV_DMI_PRODUCT_FILE:-/sys/class/dmi/id/product_name}"
dmi="" dmi_norm=""
[ -r "$dmi_file" ] && dmi="$(head -c 200 "$dmi_file" 2>/dev/null | tr -d '\n')"
dmi_vote=""
norm dmi_norm "$dmi"
case "$dmi_norm" in *galaxy*) dmi_vote=galaxy ;; esac

reset_vote=""
[ "${TTDEV_RESET_MODE:-}" = galaxy ] && reset_vote=galaxy

case "$board_vote" in
    conflict | unknown)
        echo "$board_why"
        exit 3
        ;;
    card)
        if [ -n "$dmi_vote$reset_vote" ]; then
            echo "board type '$board_name' is a card, but the DMI product ('$dmi') or reset mode says galaxy"
        else
            echo "board type '$board_name' is a card board: it gives chips per card, not the number of cards in this box"
        fi
        exit 3
        ;;
    galaxy)
        printf '%s\tboard type %s\n' "$GALAXY_CHIPS" "$board_name"
        exit 0
        ;;
esac
# No chip answered (or auto-update, which reads none): the host firmware and the persisted reset
# mode still know a galaxy, and they cannot be lowered by chips that are off the bus.
if [ "$dmi_vote" = galaxy ]; then
    printf '%s\tDMI product %s\n' "$GALAXY_CHIPS" "$dmi"
    exit 0
fi
if [ "$reset_vote" = galaxy ]; then
    printf '%s\treset mode galaxy\n' "$GALAXY_CHIPS"
    exit 0
fi
echo "no chip board type, no galaxy DMI product ('${dmi:-unreadable}') and no galaxy reset mode"
exit 3
