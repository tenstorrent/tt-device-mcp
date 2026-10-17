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
# Idle-gated: an update never interrupts an in-flight device op, and never restarts the broker
# under a running job. From its busy check through the restart the updater holds dispatch (spec 08
# I18): the broker starts no queued job while the hold names a live pid, so a job queued behind one
# that just ended waits, persisted, for the new broker instead of being dispatched into the restart.
# A busy lap drains (spec 08 I20): it keeps the hold, lets the running job finish and applies at
# that job boundary. The wait is bounded by the broker's hard job limit, counted from the job's
# start: a job past it no longer holds the update. A drain blocks this lap (and the reconcile after
# it) for at most that long.
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

# Decision log (spec 08 I22). Every lap that has an update to make records what it decided
# (deferred, applied, failed), when, and from which sha to which, in the broker's log dir and
# the journal under a stable tag. The stderr lines above and below land in the reconcile unit's
# journal, which may not survive a reboot; this file does, so an operator can match a broker
# restart (and the startup pass after it) to the update that caused it. Kept to about 1 MiB.
AU_LOG="${TTDEV_LOG_DIR:-/var/log/tt-device-broker}/autoupdate.log"
au_logged=0
au_log() {  # <decision> <reason>
    local line
    line="$(date '+%Y-%m-%dT%H:%M:%S%z') decision=$1 branch=$BR old=${installed_sha:-none} new=$remote_sha reason=$2"
    au_logged=1
    logger -t tt-device-autoupdate -- "$line" 2>/dev/null || true
    {
        mkdir -p "${AU_LOG%/*}"
        if [ "$(stat -c %s "$AU_LOG" 2>/dev/null || echo 0)" -ge 1048576 ]; then
            mv -f "$AU_LOG" "$AU_LOG.1"
        fi
        echo "$line" >> "$AU_LOG"
    } 2>/dev/null || true
}

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
        au_log deferred "device op in flight (${op_name:-unknown}); apply_by=when the op ends (bounded)"
        exit 0
    fi
    rm -f "$INHIBIT" 2>/dev/null || true   # stale: the broker that held it is gone
fi
# A reset outlives the broker by design (it runs in its own scope), so the lock file
# above can be absent while a reset is still in flight. Ask systemd, not the broker.
if systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-reset-*' 2>/dev/null \
        | grep -q 'ttdev-reset-'; then
    echo "autoupdate: a device reset scope is still running; deferring" >&2
    au_log deferred "device reset scope running; apply_by=when the reset ends (bounded)"
    exit 0
fi

# Recovery deferral (spec 08 I23). The device-op bar above covers a rung while it runs, not the
# hold around it: between rungs (a settle, a backoff, the wait for the hold deadline) no op is in
# flight, and a restart there throws the episode back to the startup check, a forced fabric pass
# on a mesh the broker already knows is bad, and can turn a heal in progress into a longer one.
# So while the broker's /health reports a hold, or its FSM is recovering or down, the update
# waits, bounded: it applies once the hold has lasted RECOVERY_DEFER (every fault class's
# time-to-recover budget is shorter, so a recovery still over it is over budget and the update
# may be its fix), or once this pending update has waited RECOVERY_DEFER in all, so a hold that
# keeps coming back cannot starve a box of a fix. Never more than 30 min; 0 turns it off. A
# broker that does not answer /health is not deferred for: an update may be what it needs.
# The deferral delays only the restart; no rung, reset, power cycle or escalation waits on it.
RECOVERY_DEFER="${TTDEV_RECOVERY_DEFER_SEC:-1200}"
[ "$RECOVERY_DEFER" -le 1800 ] 2>/dev/null || RECOVERY_DEFER=1800
RECOVERY_PENDING="$ROOT/recovery.defer"
SOCK=/run/tt-device-broker/broker.sock
broker_hold() {  # prints "<held 0|1> <fsm_state> <held_age_sec, -1 unknown>", nothing if no answer
    curl -fsS --max-time 5 --unix-socket "$SOCK" http://localhost/health 2>/dev/null \
        | "$VENV/bin/python" -I -c 'import json, sys
h = json.load(sys.stdin)
age = h.get("held_age_sec")
print(int(bool(h.get("held"))), h.get("fsm_state") or "unknown", age if isinstance(age, int) else -1)' 2>/dev/null
}
# Succeeds (and names why in $recovery_why) while the update should wait for the recovery.
recovery_gate() {
    local held="" state="" age="" now since="" sha="" waited
    recovery_why=""
    read -r held state age <<EOF
$(broker_hold)
EOF
    case "${held:-0}:${state:-}" in
        1:*|*:recovering|*:down) ;;
        *) return 1 ;;
    esac
    [ "$RECOVERY_DEFER" -gt 0 ] 2>/dev/null || return 1
    now="$(date +%s)"
    [ -f "$RECOVERY_PENDING" ] && read -r sha since < "$RECOVERY_PENDING" 2>/dev/null
    # Clocked from the FIRST deferral of a pending update: a newer commit does not restart it.
    [ -n "${since:-}" ] || since="$now"
    echo "$remote_sha $since" > "$RECOVERY_PENDING"
    waited=$(( now - since ))
    # The deadline this deferral runs to: the bound from the first deferral, or from the hold's
    # start if that comes sooner.
    deadline=$(( since + RECOVERY_DEFER ))
    [ "${age:--1}" -ge 0 ] 2>/dev/null && [ $(( now - age + RECOVERY_DEFER )) -lt "$deadline" ] \
        && deadline=$(( now - age + RECOVERY_DEFER ))
    recovery_why="fsm=$state held=$held hold_age=${age:--1}s waited=${waited}s bound=${RECOVERY_DEFER}s apply_by=$(date -d "@$deadline" '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo "$deadline")"
    [ "${age:--1}" -lt "$RECOVERY_DEFER" ] 2>/dev/null || return 1
    [ "$waited" -lt "$RECOVERY_DEFER" ] || return 1
    return 0
}
if recovery_gate; then
    echo "autoupdate: broker recovery in progress ($recovery_why); deferring" >&2
    au_log deferred "broker recovery in progress ($recovery_why)"
    exit 0
fi
[ -n "$recovery_why" ] && echo "autoupdate: broker recovery past the deferral bound ($recovery_why); applying" >&2
# Drops the dispatch hold on every exit of an applying lap, and records an apply that stopped.
au_exit() {
    local rc=$?
    rm -f "$HOLD" "$HOLD.$$" 2>/dev/null || true
    [ "$rc" -eq 0 ] || [ "$au_logged" -eq 1 ] \
        || au_log failed "apply stopped before the restart (exit $rc); installed.sha unchanged, the next lap retries"
}

# Dispatch hold (spec 08 I18, spec 01 I18). Taken before anything is counted, then a short
# settle so a dispatch already past the broker's check shows up as a child. A lap that restarts
# nothing drops it at once; an applying lap keeps it until `systemctl restart` returns (the trap drops
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

# Idle gate (spec 08 I24), always on, with no setting. A running job is re-adopted across a restart
# (tests/test_readopt.py), but the restart still costs it: the socket drops for seconds under a
# polling client, a client that reads the drop as a failure kills the job (and owes a kill reset),
# and the new broker holds the queue for its startup checks until the re-adopted job ends. So an
# update never restarts the broker under a running job: a busy lap drains (spec 08 I20) at once,
# holding dispatch so nothing new starts, and applies at that job's end, polling every 5 s. The
# in-flight device-op bar above is separate, and absolute.
#
# The wait is bounded by the broker's hard job limit, MAX_TIMEOUT_SEC (1500) plus its kill grace
# GRACEFUL_KILL_GRACE_SEC (60), counted from the start of the youngest running job scope (a job
# with no readable start counts as just started). A job past it is overdue for the broker's own kill
# and no longer holds the update, so a box busy around the clock still updates within one job.
JOB_CAP=1560
UPTIME=/proc/uptime
count_busy() {
    local mp p uu q
    mp="$(systemctl show tt-device-broker -p MainPID --value 2>/dev/null)"
    # A stopped broker reads MainPID 0, and `pgrep -P 0` lists init and kthreadd: count no kids.
    kids=0
    [ "${mp:-0}" -gt 0 ] 2>/dev/null && kids="$(pgrep -P "$mp" 2>/dev/null | wc -l)"
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
# Seconds the youngest running job scope has run (j_age), from systemd's own record of its start:
# a re-adopted job keeps its scope, so its age survives broker restarts. 0 when none can be read.
youngest_job_age() {
    local up u t a
    j_age=""
    up="$(cut -d' ' -f1 "$UPTIME" 2>/dev/null)"; up="${up%.*}"
    for u in $(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-job-*' 2>/dev/null \
            | grep -o 'ttdev-job-[^ ]*\.scope'); do
        t="$(systemctl show "$u" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)"
        if [ -n "$up" ] && [ "${t:-0}" -gt 0 ] 2>/dev/null; then a=$(( up - t / 1000000 )); else a=0; fi
        { [ -z "$j_age" ] || [ "$a" -lt "$j_age" ]; } && j_age="$a"
    done
    [ -n "$j_age" ] || j_age=0
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
# Wait, under the dispatch hold, for the job boundary: nothing running and no device op in flight.
# Returns 0 there, and 0 once the youngest running job is past JOB_CAP or the broker is down (a down
# broker protects no job by waiting, and the restart is what starts it again); 1 when a device op is
# still in flight at the cap. $1 names the wait in the log.
wait_job_boundary() {
    local w_start w_now w_bound
    youngest_job_age
    w_bound=$(( JOB_CAP - j_age )); [ "$w_bound" -gt 0 ] || w_bound=0
    w_start="$(date +%s)"
    echo "autoupdate: $1: busy (children=$kids holders=$holders scopes=${scopes:-0} queued=$queued) -> draining: dispatch held, the running job finishes, then the update applies (bound ${w_bound}s: job cap ${JOB_CAP}s, youngest job ${j_age}s in)" >&2
    while :; do
        count_busy
        running=$(( kids + holders + ${scopes:-0} ))
        if [ "$running" -eq 0 ] && ! op_in_flight; then
            echo "autoupdate: $1: drained after $(( $(date +%s) - w_start ))s (queued=$queued wait for the new broker)" >&2
            return 0
        fi
        w_now="$(date +%s)"
        if ! op_in_flight; then
            case "$(systemctl is-active tt-device-broker 2>/dev/null)" in
                active|activating|deactivating|reloading) ;;
                *)  echo "autoupdate: $1: the broker is down after $(( w_now - w_start ))s; applying (the restart starts it)" >&2
                    return 0 ;;
            esac
        fi
        if [ $(( w_now - w_start )) -ge "$w_bound" ]; then
            if op_in_flight; then
                echo "autoupdate: $1: a device op is still in flight at the job cap after $(( w_now - w_start ))s; dispatch released, the next lap retries" >&2
                return 1
            fi
            echo "autoupdate: $1: the running job is past the job cap (${JOB_CAP}s) after $(( w_now - w_start ))s (children=$kids holders=$holders scopes=${scopes:-0}); it no longer holds the update, applying" >&2
            return 0
        fi
        sleep 5
        # Refreshed, so the broker's overdue bound on a hold counts from the last poll: a drain
        # that hangs lets go of dispatch on its own, one that polls keeps it.
        hold_write
    done
}
count_busy
if [ "$busy" -ne 0 ]; then
    if ! wait_job_boundary drain; then
        rm -f "$HOLD" 2>/dev/null || true
        au_log deferred "device op still in flight at the job cap (${JOB_CAP}s); dispatch released, the next lap retries"
        exit 0
    fi
    # The apply's own stretch, under the broker's overdue bound, starts now.
    hold_write
fi

echo "autoupdate: applying ${remote_sha:0:9}" >&2
trap au_exit EXIT
# Reset to the sha this lap decided on, NOT to origin/$BR: the drain above can wait for
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
# Last look before the restart (spec 08 I24). The install ran under the dispatch hold, but a job
# can still be running here: a tenant that opened the device on its own, or a dispatch after an
# install slow enough for the broker to drop the hold as overdue. It is waited out the same way;
# a device op still in flight at the cap restarts nothing and leaves installed.sha on the old sha,
# so the next lap retries.
hold_write
count_busy
if [ $(( kids + holders + ${scopes:-0} )) -ne 0 ] || op_in_flight; then
    if ! wait_job_boundary "restart point"; then
        echo "autoupdate: not restarting, not marking current" >&2
        au_log deferred "device op still in flight at restart time (job cap ${JOB_CAP}s)"
        exit 0
    fi
fi
# Then the recovery deferral again (spec 08 I23): the install and any wait above take a minute or
# more, and the broker starts holds on its own meanwhile. A hold that began within the deferral
# bound ends the lap without the restart and before installed.sha moves, so the next lap goes
# through every gate again.
if recovery_gate; then
    echo "autoupdate: broker recovery began during the install ($recovery_why); deferring the restart" >&2
    au_log deferred "broker recovery in progress at restart time ($recovery_why)"
    exit 0
fi
echo "$remote_sha" > "$STATE"
chmod -R a+rX "$VENV" 2>/dev/null || true

rc=0
systemctl restart tt-device-broker || rc=$?
rm -f "$RECOVERY_PENDING" 2>/dev/null || true
if [ "$rc" -eq 0 ]; then
    echo "autoupdate: broker restarted at ${remote_sha:0:9} (via $src_desc)" >&2
    au_log applied "broker restarted${recovery_why:+ (recovery past the deferral bound: $recovery_why)}"
else
    echo "autoupdate: broker restart at ${remote_sha:0:9} failed (exit $rc); reconcile starts it" >&2
    au_log failed "broker restart failed (exit $rc)"
fi
