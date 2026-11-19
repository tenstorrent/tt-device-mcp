#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Install the PASSIVE crash instruments on any host — including one with no broker.
#
#   tt-crash-recorder.service : captures the firmware's BERT record once per boot
#   tt-device-buslock.service : counts CPU bus locks to disk for the life of the boot
#
# Neither touches the devices. That is the point: a host that has never run our broker,
# instrumented without otherwise changing it, is the control that says whether these fatal
# machine checks are ours or the platform's.
#
# Usage: sudo install-crash-recorder.sh <deploy-dir>
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "install-crash-recorder: must run as root" >&2; exit 1; }
DEPLOY="${1:?usage: install-crash-recorder.sh <deploy-dir>}"

# `install -d -m` sets the mode of the LEAF only, so the parent is named too: on a standalone
# control host nothing else creates it (spec 06 I9). Records an earlier recorder left are brought to
# 0644, the local-reset lock excepted, by the apply's own sweep helper. Like the enables below it is
# best-effort: a sweep that cannot finish is reported and is no reason to leave the instruments
# uninstalled; the `install` lines and the daemon-reload stay fatal under `set -e`.
. "$(dirname "$(readlink -f "$0")")/tt-device-modes.sh"
install -d -m 0755 /var/lib/tt-device-broker
install -d -m 0755 /var/lib/tt-device-broker/health
sweep_to_mode 0644 /var/lib/tt-device-broker/health -maxdepth 1 -type f ! -name local-reset.lock \
    || echo "install-crash-recorder: could not bring every existing health record to 0644 (continuing)" >&2
install -m 0755 "$DEPLOY/tt-crash-recorder.py"      /usr/local/bin/tt-crash-recorder
install -m 0644 "$DEPLOY/tt-crash-recorder.service" /etc/systemd/system/tt-crash-recorder.service
install -m 0644 "$DEPLOY/tt-device-buslock.service" /etc/systemd/system/tt-device-buslock.service

systemctl daemon-reload

# Order matters: the recorder must sum the PREVIOUS boot's bus locks and write its boot
# marker BEFORE the counter starts appending this boot's counts, or the two boots blur.
systemctl enable --now tt-crash-recorder.service >/dev/null 2>&1 || true

if perf stat -a -e ls_locks.bus_lock -- true >/dev/null 2>&1; then
    systemctl enable --now tt-device-buslock.service >/dev/null 2>&1 || true
    echo "install-crash-recorder: bus-lock counter running"
else
    echo "install-crash-recorder: ls_locks.bus_lock PMU event unavailable; counter NOT enabled" >&2
fi

echo "install-crash-recorder: done on $(hostname)"
