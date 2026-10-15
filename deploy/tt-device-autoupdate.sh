#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Keep the broker current by tracking a branch of this project's own public
# repository. Invoked once a minute from the reconcile timer. Config in
# /etc/default/tt-device-broker:
#
#   TTDEV_AUTOUPDATE=1                 # master switch
#   TTDEV_BRANCH=<branch>              # branch to track (main in prod)
#   TTDEV_VENV=/path/to/broker/venv    # venv we pip-install into
#   TTDEV_MIN_APPLY_INTERVAL_SEC=3600  # optional: min seconds between applies (0/unset = off)
#
# THE SOURCE IS NOT CONFIGURABLE (spec 08 I16). It is the constant below, and the only
# lever an operator has is which branch of it to track. That is what keeps this path
# credential-free by construction rather than by convention: a source that can be set can
# be set to a private repository, and a private source needs a credential — a key for root
# to hold, or somebody else's to borrow. There is none here, and no way to configure the
# need for one back into existence.
#
# So root fetches the public repo itself, into a root-owned clone: code that is about to be
# installed as root has no business sitting anywhere else on the way in.
#
# Idle-gated: an update never interrupts an in-flight device op, and re-adoption covers a
# running job across the restart. From its busy check through the restart the updater holds
# dispatch (spec 08 I18): the broker starts no queued job while the hold names a live pid, so
# a job queued behind one that just ended waits, persisted, for the new broker instead of being
# dispatched into the restart. A box that is never idle is drained instead (spec 08 I20): past
# TTDEV_DRAIN_AFTER_SEC the lap keeps the hold, lets the running job finish and applies at that job
# boundary, and lets go after TTDEV_DRAIN_TIMEOUT_SEC if the job runs on. A drain blocks this lap
# (and the reconcile after it) for at most that long.
set -u

[ -r /etc/default/tt-device-broker ] && . /etc/default/tt-device-broker
[ "${TTDEV_AUTOUPDATE:-0}" = "1" ] || exit 0

# The one and only update source. Deliberately a literal and not read from config —
# see the header. Changing it is a code change, reviewed and shipped like any other.
readonly REPO="https://github.com/tenstorrent/tt-device-mcp.git"

BR="${TTDEV_BRANCH:-}"; VENV="${TTDEV_VENV:-}"; ROOT="${TTDEV_ROOT:-}"
[ -n "$BR" ] && [ -n "$VENV" ] && [ -n "$ROOT" ] || exit 0

# The clone lives under the broker's own root, 0700 and root-owned, never in a user's
# home: what lands here is installed as root on the next lap, so anywhere an
# unprivileged user could write to it would be a way to choose what root installs.
CLONE="$ROOT/src"
STATE="$ROOT/installed.sha"
# Pinned to the version validated on hardware (40 active-eth cores, 0.5s read).
ETH_EXALENS_VERSION="0.3.20"

# Serialize: two pip installs racing into the same venv leave a dist-info with no
# RECORD, which pip can then never uninstall — auto-update stays wedged forever.
# The timer fires every 60s and an admin may run this by hand, so overlap is real.
exec 9>"$ROOT/.autoupdate.lock"
flock -n 9 || { echo "autoupdate: another run holds the lock; skipping" >&2; exit 0; }

# Fetch as root, straight from the public repo. Every failure below is a no-op lap that says
# why: a host that cannot reach the network, or a branch nobody has pushed, is a thing to
# report and retry next minute, not a reason to install anything.
#
# Credentials are refused explicitly, not merely unavailable. The source is public, so a fetch
# that wants a username is a fetch aimed somewhere it should not be — a URL that has changed
# under us, or a host carrying a credential helper someone configured for other work. Under
# systemd there is no terminal for git to prompt on, so it would fail anyway, but only by
# accident of having no tty; these two say so on purpose, and keep a stored credential from
# quietly making the fetch succeed.
clone="$CLONE"; remote_sha=""; src_desc="$REPO ($BR)"
if [ ! -d "$CLONE/.git" ]; then
    # A leftover that is not a repository (an interrupted first clone) would fail every lap
    # forever, because git will not clone into a non-empty directory.
    rm -rf "$CLONE" 2>/dev/null || true
    # umask in the subshell so the tree is 0700 from creation, never briefly world-readable.
    ( umask 077 && GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true git clone -q -b "$BR" "$REPO" "$CLONE" ) \
        || { echo "autoupdate: cannot clone $REPO ($BR)" >&2; rm -rf "$CLONE" 2>/dev/null; exit 0; }
fi
GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true git -C "$CLONE" fetch -q origin "$BR" \
    || { echo "autoupdate: cannot reach $REPO" >&2; exit 0; }
# --verify -q, because a bare `rev-parse <missing ref>` echoes its own argument to stdout
# and exits non-zero, which a plain non-empty test would take for a sha.
remote_sha="$(git -C "$CLONE" rev-parse --verify -q "refs/remotes/origin/$BR" || true)"
[ -n "$remote_sha" ] \
    || { echo "autoupdate: $REPO has no branch $BR" >&2; exit 0; }

installed_sha="$(cat "$STATE" 2>/dev/null || true)"
[ "$installed_sha" = "$remote_sha" ] && exit 0   # already current

echo "autoupdate: $BR ${installed_sha:0:9} -> ${remote_sha:0:9} via $src_desc" >&2

# Apply rate limit (spec 08 I21), OFF by default. Every apply restarts the broker, and every start
# runs the full startup check, fabric traffic pass included (spec 03 I4) -- a burst of pushes is a
# burst of fabric passes on a galaxy. With TTDEV_MIN_APPLY_INTERVAL_SEC set, a new sha waits until
# that long has passed since the last apply, so the burst becomes one restart onto its newest sha.
# Checked before the device-op bar and the dispatch hold: a lap that will not apply holds nothing.
# Only the timing of an apply moves; the startup check after it is never skipped or shortened.
# The stamp is written just before the restart, so a failed apply retries on the next lap.
LAST_APPLY="$ROOT/last-apply"
MIN_APPLY="${TTDEV_MIN_APPLY_INTERVAL_SEC:-0}"
if [ "$MIN_APPLY" -gt 0 ] 2>/dev/null; then
    last=""; read -r last < "$LAST_APPLY" 2>/dev/null || true
    now="$(date +%s)"
    # A stamp that is not a number, or lies in the future (the clock stepped back), bars nothing.
    case "$last" in ''|*[!0-9]*) last="" ;; esac
    if [ -n "$last" ] && [ "$last" -le "$now" ] && [ $(( now - last )) -lt "$MIN_APPLY" ]; then
        echo "autoupdate: rate-limited: last apply $(( now - last ))s ago (min ${MIN_APPLY}s); ${remote_sha:0:9} applies in $(( last + MIN_APPLY - now ))s" >&2
        exit 0
    fi
fi

# An in-flight device operation is an ABSOLUTE bar — not the bounded defer below.
# Restarting the broker mid-reset lets KillMode=control-group SIGTERM the running
# tt-smi, leaving 32 ASICs half-reset, which is the state that costs a power-cycle.
# Unlike a long job, a device op is bounded (DEVICE_RESET_TIMEOUT_SEC), so waiting
# it out can never starve the updater.
INHIBIT=/run/tt-device-broker/device-op.lock
if [ -f "$INHIBIT" ]; then
    op_pid=""; op_name=""
    read -r op_pid op_name < "$INHIBIT" 2>/dev/null || true
    if [ -n "${op_pid:-}" ] && kill -0 "$op_pid" 2>/dev/null; then
        echo "autoupdate: device op in flight (${op_name:-unknown}, pid $op_pid); deferring" >&2
        exit 0
    fi
    rm -f "$INHIBIT" 2>/dev/null || true   # stale: the broker that held it is gone
fi
# A reset outlives the broker by design (it runs in its own scope), so the lock file
# above can be absent while a reset is still in flight. Ask systemd, not the broker.
if systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-reset-*' 2>/dev/null \
        | grep -q 'ttdev-reset-'; then
    echo "autoupdate: a device reset scope is still running; deferring" >&2
    exit 0
fi

# Dispatch hold (spec 08 I18, spec 01 I18). Taken before anything is counted, then a short
# settle so a dispatch already past the broker's check shows up as a child. A deferring lap
# drops it at once; an applying lap keeps it until `systemctl restart` returns (the trap drops
# it on every exit, and systemd removes it with the RuntimeDirectory anyway). Written whole under
# a temporary name and renamed into place: the broker removes a hold it cannot parse, so it must
# never see this one empty or half-written.
HOLD=/run/tt-device-broker/dispatch-hold
hold_write() {
    [ -d "${HOLD%/*}" ] || return 0
    { echo "$$ $(date +%s)" > "$HOLD.$$" && mv -f "$HOLD.$$" "$HOLD"; } 2>/dev/null || true
}
if [ -d "${HOLD%/*}" ]; then
    trap 'rm -f "$HOLD" "$HOLD.$$" 2>/dev/null || true' EXIT
    hold_write
    sleep "${TTDEV_HOLD_SETTLE_SEC:-2}"
fi

# Idle gate, OFF by default. A job runs in its own scope and is re-adopted across a restart
# (tests/test_readopt.py), so a busy box is not a reason to hold an update back: waiting buys
# no safety, only a host left stale on the fix it is waiting for. A host that does want an idle
# window sets TTDEV_MAX_DEFER_SEC to the seconds it will wait. The in-flight device-op bar above
# is separate and unconditional — re-adoption covers a job, not a tt-smi killed mid-reset.
MAX_DEFER="${TTDEV_MAX_DEFER_SEC:-0}"
# Drain (spec 08 I20). A box whose queue is never empty (jobs back to back, a second apart) has no
# idle tick, so an update waiting for one waits forever. Once a pending update has waited
# DRAIN_AFTER, the lap keeps the dispatch hold instead of dropping it: the running job finishes,
# nothing new starts, and the update applies at that job boundary while the queued jobs wait,
# persisted, for the new broker. A job that outlives DRAIN_TIMEOUT is left alone: the hold is
# dropped, the queue runs again, and the next drain starts DRAIN_AFTER later.
DRAIN_AFTER="${TTDEV_DRAIN_AFTER_SEC:-300}"
DRAIN_TIMEOUT="${TTDEV_DRAIN_TIMEOUT_SEC:-300}"
PENDING="$ROOT/pending.defer"
count_busy() {
    local mp p uu q
    mp="$(systemctl show tt-device-broker -p MainPID --value 2>/dev/null)"
    kids="$(pgrep -P "${mp:-0}" 2>/dev/null | wc -l)"
    holders=0
    for p in /proc/[0-9]*; do
        if ls -l "$p/fd" 2>/dev/null | grep -q tenstorrent; then
            uu=$(stat -c %u "$p" 2>/dev/null); [ "${uu:-0}" -ge 1000 ] && holders=$((holders+1))
        fi
    done
    # A re-adopted job is no child of this broker, and a queued job is about to be one: a job
    # scope still running and a persisted queued spec both count as busy.
    scopes="$(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-job-*' 2>/dev/null \
        | grep -c 'ttdev-job-')"
    queued=0
    for q in "${TTDEV_LOG_DIR:-/var/log/tt-device-broker}"/queued/*.json; do
        [ -e "$q" ] && queued=$((queued+1))
    done
    busy=$(( kids + holders + ${scopes:-0} + queued ))
}
# The device-op bar above, asked again while a drain waits: the broker can start a device op (a
# gate pass, a reset) on its own while dispatch is held, and the apply must not restart over it.
op_in_flight() {
    local op_pid="" op_name=""
    if [ -f "$INHIBIT" ]; then
        read -r op_pid op_name < "$INHIBIT" 2>/dev/null || true
        [ -n "${op_pid:-}" ] && kill -0 "$op_pid" 2>/dev/null && return 0
    fi
    systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-reset-*' 2>/dev/null \
        | grep -q 'ttdev-reset-'
}
count_busy
if [ "$busy" -ne 0 ] && [ "$MAX_DEFER" -gt 0 ]; then
    now="$(date +%s)"
    p_sha=""; p_since=""; p_drain=""
    [ -f "$PENDING" ] && read -r p_sha p_since p_drain < "$PENDING" 2>/dev/null
    # Clock from the FIRST pending update, not the latest commit — so a burst of
    # commits can't keep resetting the timer and starving a perpetually-busy host.
    [ -n "${p_since:-}" ] || p_since="$now"
    # The first drain is due DRAIN_AFTER into the wait; a drain that timed out set the next one.
    [ -n "${p_drain:-}" ] || p_drain=$(( p_since + DRAIN_AFTER ))
    echo "$remote_sha $p_since $p_drain" > "$PENDING"
    waited=$(( now - p_since ))
    if [ "$waited" -lt "$MAX_DEFER" ] && [ "$now" -lt "$p_drain" ]; then
        echo "autoupdate: deferring (busy: children=$kids holders=$holders scopes=${scopes:-0} queued=$queued; waited ${waited}s/${MAX_DEFER}s, drain in $(( p_drain - now ))s)" >&2
        rm -f "$HOLD" 2>/dev/null || true
        exit 0
    fi
    if [ "$waited" -lt "$MAX_DEFER" ]; then
        echo "autoupdate: busy for ${waited}s -> draining: dispatch held, the running job finishes, then the update applies (bound ${DRAIN_TIMEOUT}s)" >&2
        d_start="$now"
        while :; do
            count_busy
            running=$(( kids + holders + ${scopes:-0} ))
            if [ "$running" -eq 0 ] && ! op_in_flight; then
                break
            fi
            d_now="$(date +%s)"
            if [ $(( d_now - d_start )) -ge "$DRAIN_TIMEOUT" ]; then
                echo "$remote_sha $p_since $(( d_now + DRAIN_AFTER ))" > "$PENDING"
                echo "autoupdate: drain timed out after $(( d_now - d_start ))s (children=$kids holders=$holders scopes=${scopes:-0}); the running job is left alone, dispatch released, next drain in ${DRAIN_AFTER}s" >&2
                rm -f "$HOLD" 2>/dev/null || true
                exit 0
            fi
            sleep 5
            # Refreshed, so the broker's overdue bound on a hold counts from the last poll: a drain
            # that hangs lets go of dispatch on its own, one that polls keeps it.
            hold_write
        done
        echo "autoupdate: drained after $(( $(date +%s) - d_start ))s (queued=$queued wait for the new broker)" >&2
        # The apply's own stretch, under the broker's overdue bound, starts now.
        hold_write
    else
        echo "autoupdate: busy for ${waited}s (>= ${MAX_DEFER}s) -> applying anyway; re-adoption preserves the running job" >&2
    fi
elif [ "$busy" -ne 0 ]; then
    echo "autoupdate: busy (children=$kids holders=$holders scopes=${scopes:-0} queued=$queued); idle gate off -> applying, the running job is re-adopted" >&2
fi
rm -f "$PENDING" 2>/dev/null || true

echo "autoupdate: applying ${remote_sha:0:9}" >&2
# Reset to the sha this lap decided on, NOT to origin/$BR: the idle gate above can defer for
# minutes, and a branch that moved in the meantime would otherwise install something this lap
# never looked at and never announced — and installed.sha would then name the wrong commit.
git -C "$clone" reset --hard -q "$remote_sha" \
    || { echo "autoupdate: reset failed" >&2; exit 1; }

if ! "$VENV/bin/pip" install -q "$clone"; then
    # Recover from a venv left half-installed by an earlier crash/race: pip refuses
    # to uninstall a dist-info it can't inventory ("no RECORD file"), which would
    # wedge every future update. Drop the broken metadata and install over the top.
    echo "autoupdate: pip failed — clearing broken package metadata and retrying" >&2
    rm -rf "$VENV"/lib/python*/site-packages/tt_device_mcp-*.dist-info \
           "$VENV"/lib/python*/site-packages/~t_device_mcp-*.dist-info
    "$VENV/bin/pip" install -q --ignore-installed "$clone" \
        || { echo "autoupdate: pip install failed" >&2; exit 1; }
fi
# Staging the deploy scripts is REQUIRED: a swallowed copy would leave the host running
# the prior version's script while installed.sha below marks it current — a silent
# degrade that no later reconcile would retry. Fail closed instead: surface it and leave
# the host on the last-known-good sha so the next reconcile tries again.
if [ -d "$clone/deploy" ]; then
    install -m 0755 "$clone/deploy/tt-device-reconcile.sh"    "$ROOT/reconcile.sh" \
        || { echo "autoupdate: staging reconcile.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-client-setup.sh" "$ROOT/client-setup.sh" \
        || { echo "autoupdate: staging client-setup.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-autoupdate.sh"   "$ROOT/autoupdate.sh" \
        || { echo "autoupdate: staging autoupdate.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-fabric-check.sh" "$ROOT/fabric-check.sh" \
        || { echo "autoupdate: staging fabric-check.sh failed (required); not marking current" >&2; exit 1; }
    # The eth-heartbeat probe. Its resolution/classification lives in-package
    # (health.monitors.eth) and runs this script via the broker-owned eth-venv; a probe the
    # deploy does not carry is a rung that silently rots, so it stages fail-closed like the
    # rest. The retired wrapper at $ROOT/eth-heartbeat-check.sh is cleaned up by
    # apply-host-config.sh below.
    install -m 0755 "$clone/deploy/tt-device-eth-heartbeat-probe.py" "$ROOT/eth-heartbeat-probe.py" \
        || { echo "autoupdate: staging eth-heartbeat-probe.py failed (required); not marking current" >&2; exit 1; }
    # The AICLK ceiling helper (health.aiclk_ceiling; off unless TTDEV_AICLK_CEILING_MHZ is set).
    # Staged on every update all the same, so a host that opts in later never runs a stale copy.
    install -m 0755 "$clone/deploy/tt-device-aiclk-ceiling.py" "$ROOT/aiclk-ceiling.py" \
        || { echo "autoupdate: staging aiclk-ceiling.py failed (required); not marking current" >&2; exit 1; }
    # The reader's interpreter, broker-owned. Pinning a developer's tt-metal python_env makes a
    # health rung die the moment that user rebuilds or moves their tree; tt-exalens ships manylinux
    # wheels on PyPI, so the box owns its own copy at a pinned version. Idempotent, and fail-OPEN:
    # a box that cannot build it reports the rung OFF at startup, which is loud enough on its own.
    if ! "$ROOT/eth-venv/bin/python" -c 'import ttexalens' >/dev/null 2>&1; then
        python3 -m venv "$ROOT/eth-venv" >/dev/null 2>&1
        if "$ROOT/eth-venv/bin/pip" install -q "tt-exalens==$ETH_EXALENS_VERSION" >/dev/null 2>&1; then
            chmod -R a+rX "$ROOT/eth-venv" 2>/dev/null || true
            echo "autoupdate: provisioned eth-venv (tt-exalens $ETH_EXALENS_VERSION)" >&2
        else
            echo "autoupdate: eth-venv provisioning failed; eth-heartbeat rung will report OFF" >&2
        fi
    fi
else
    echo "autoupdate: pulled tree has no deploy/ dir; cannot stage required scripts — not marking current" >&2
    exit 1
fi
# Reconcile host config (systemd unit + watchdog, sudoers, smi wrapper, reconcile
# units, profile.d) from the pulled tree. This is what makes a deploy-surface
# change propagate on auto-update, not just the python package — so no host ever
# needs a manual installer re-run for a unit/sudoers change.
#
# REQUIRED and fail-closed: a failed apply leaves the new python package running
# against stale units/sudoers — a split-version state no later reconcile would retry
# once installed.sha marks the sha current. On failure (or an unrunnable script)
# leave installed.sha on the last-known-good so the next reconcile retries, and do
# not restart onto a half-applied host config.
if [ ! -x "$clone/deploy/apply-host-config.sh" ]; then
    echo "autoupdate: apply-host-config.sh missing or not executable (required); not marking current" >&2
    exit 1
fi
bash "$clone/deploy/apply-host-config.sh" "$clone" \
    || { echo "autoupdate: host-config apply failed (required); not marking current" >&2; exit 1; }
echo "$remote_sha" > "$STATE"
chmod -R a+rX "$VENV" 2>/dev/null || true

date +%s > "$LAST_APPLY" 2>/dev/null || true   # I21: the rate limit counts from this restart
systemctl restart tt-device-broker
echo "autoupdate: broker restarted at ${remote_sha:0:9} (via $src_desc)" >&2
