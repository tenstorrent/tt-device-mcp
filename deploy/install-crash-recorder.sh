#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Install the PASSIVE crash instruments on any host — including one with no broker.
#
#   tt-crash-recorder.service : captures the firmware's BERT record once per boot
#   tt-device-buslock.service : counts CPU bus locks to disk for the life of the boot
#
# Neither touches the accelerators. That is the point: a host that has never run our broker,
# instrumented without otherwise changing it, is the control that says whether these fatal
# machine checks are ours or the platform's.
#
# Usage: sudo install-crash-recorder.sh <deploy-dir>
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "install-crash-recorder: must run as root" >&2; exit 1; }
DEPLOY="${1:?usage: install-crash-recorder.sh <deploy-dir>}"

install -d -m 0755 /var/lib/tt-device-broker/health
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
