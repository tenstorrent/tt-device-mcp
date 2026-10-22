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
#   TTDEV_MIN_APPLY_INTERVAL_SEC=1800  # optional: min seconds between applies (default 1800; 0 = off)
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
# that job boundary. The wait under the hold is bounded by the broker's hard job limit, counted
# from the job's start: a broker job past it still gets no restart, so the lap lets go of dispatch
# and the next lap retries until the job ends (a capped job by the broker's limit kill; a
# no-timeout-except-on-contention job only by itself or on contention, so with nothing queued the
# update waits with no bound). A drain blocks this lap (and the
# reconcile after it) for at most that long.
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

# Apply rate limit (spec 08 I21), ON by default (1800 s). Every apply but a docs/tests/CI-only one
# (I29, below) restarts the broker, and every start runs the full startup check, fabric traffic
# pass included (spec 03 I4) -- a burst of pushes is a burst of restarts and fabric passes, each
# holding the queue. A new sha waits until
# TTDEV_MIN_APPLY_INTERVAL_SEC (0 = off) has passed since the last apply, so the burst becomes one
# restart onto its newest sha.
# Checked before the device-op bar and the dispatch hold: a lap that will not apply holds nothing.
# Only the timing of an apply moves; the startup check after it is never skipped or shortened.
# The stamp is written just before the restart, so a failed apply retries on the next lap.
# The limit is for a healthy broker only: a broker that is not running, restarts itself, holds the
# device or fails its health check may be broken by the last apply, and the next sha may be its
# fix, so it applies at once (broker_broken below). A broker still in its startup check is booting,
# not broken, and stays limited, for a bounded time. Escape by hand: rm "$ROOT/last-apply" (the
# config file sourced above wins over an env override on the command line).
LAST_APPLY="$ROOT/last-apply"
LAST_APPLY_NRESTARTS="$ROOT/last-apply-nrestarts"
MIN_APPLY="${TTDEV_MIN_APPLY_INTERVAL_SEC:-1800}"
# Every start opens RECOVERING(startup_unverified) and holds the device until the startup check
# passes: the hugepage wait (STARTUP_HUGEPAGES_WAIT_SEC, 120 s, spec 03 I33) and then one full gate
# pass with its fabric check (436 s at most, spec 04 reset duration bound). That is a broker
# booting, not a broken one, so it stays limited for STARTUP_BOUND from the last apply: 120 + 436
# s plus 44 s for the restart itself. Past the bound it counts as broken. A failed startup check
# names its fault in fsm_why (and a dirty episode is held as one), so it is broken at once.
STARTUP_BOUND=600
# Why the broker is not a healthy one to rate-limit, or nothing; $1 is the seconds since the last
# apply. Not active (failed, activating, inactive); systemd restarted it since the last apply
# (NRestarts grew past the count saved right after that apply's restart: a crash loop); /health
# silent; or an FSM that is recovering/down or holds the device, unless that is the startup check
# above, within STARTUP_BOUND, held for nothing but that check.
broker_broken() {
    local st n base h age="${1:-0}"
    st="$(systemctl is-active tt-device-broker 2>/dev/null)"
    if [ "$st" != active ]; then echo "is ${st:-unknown}, not active"; return; fi
    n="$(systemctl show tt-device-broker -p NRestarts --value 2>/dev/null)"
    base=""; { read -r base < "$LAST_APPLY_NRESTARTS"; } 2>/dev/null || true
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    case "$base" in ''|*[!0-9]*) base=0 ;; esac
    if [ $(( 10#$n )) -gt $(( 10#$base )) ]; then
        echo "restarted itself since the last apply (NRestarts $(( 10#$base )) -> $(( 10#$n )))"; return
    fi
    h="$(curl -s --max-time 5 --unix-socket /run/tt-device-broker/broker.sock http://localhost/health 2>/dev/null)"
    if [ -z "$h" ]; then echo "does not answer /health"; return; fi
    if printf '%s' "$h" | grep -Eq '"fsm_state": *"recovering"' \
            && printf '%s' "$h" | grep -Eq '"fsm_why": *"startup_unverified"'; then
        if [ "$age" -gt "$STARTUP_BOUND" ]; then
            echo "is still in its startup check ${age}s after the last apply (bound ${STARTUP_BOUND}s)"; return
        fi
        if ! printf '%s' "$h" | grep -Eq '"held": *true' \
                || printf '%s' "$h" | grep -Eq '"held_reason": *"device unverified: '; then
            return   # booting: the startup check is running
        fi
    fi
    if printf '%s' "$h" | grep -Eq '"held": *true'; then echo "holds the device"; return; fi
    if printf '%s' "$h" | grep -Eq '"fsm_state": *"(recovering|down)"'; then
        echo "fails its health check (fsm $(printf '%s' "$h" | grep -Eo '"fsm_state": *"[a-z]*"' | grep -Eo '[a-z]*"$' | tr -d '"'))"
    fi
}
if [ "$MIN_APPLY" -gt 0 ] 2>/dev/null; then
    last=""; { read -r last < "$LAST_APPLY"; } 2>/dev/null || true
    now="$(date +%s)"
    # A stamp that is not a number, or lies in the future (the clock stepped back), bars nothing.
    # Read base 10, so a hand-written leading zero is not taken for octal.
    case "$last" in ''|*[!0-9]*) last="" ;; *) last=$(( 10#$last )) ;; esac
    if [ -n "$last" ] && [ "$last" -le "$now" ] && [ $(( now - last )) -lt "$MIN_APPLY" ]; then
        broken="$(broker_broken "$(( now - last ))")"
        if [ -z "$broken" ]; then
            echo "autoupdate: rate-limited: last apply $(( now - last ))s ago (min ${MIN_APPLY}s); ${remote_sha:0:9} applies in $(( last + MIN_APPLY - now ))s" >&2
            exit 0
        fi
        echo "autoupdate: rate limit skipped: the broker $broken; ${remote_sha:0:9} applies now (last apply $(( now - last ))s ago)" >&2
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
# The wait under the hold is bounded by the broker's hard job limit, MAX_TIMEOUT_SEC (1500) plus its
# kill grace GRACEFUL_KILL_GRACE_SEC (60), counted from the start of the youngest running job scope
# (a job with no readable start counts as just started). A broker job still running past it (a child,
# a job scope, or a device holder inside one: a re-adopted job) never gets a restart: the lap releases
# dispatch and the next lap retries, so the update applies at that job's end. A capped job ends by
# the broker's limit kill, at most JOB_CAP after its start. A no-timeout-except-on-contention job
# (spec 01 I20) is not killed for time: it runs until it ends, or until a queued job contends for it
# (preempt grace plus kill grace); until then the update waits, with no bound while nothing is queued. Only a device holder outside every broker job, which no broker
# kill ends, stops holding the update at the bound, so a box busy around the clock still updates.
JOB_CAP=1560
UPTIME=/proc/uptime
count_busy() {
    local mp p uu q
    mp="$(systemctl show tt-device-broker -p MainPID --value 2>/dev/null)"
    # A stopped broker reads MainPID 0, and `pgrep -P 0` lists init and kthreadd: count no kids.
    kids=0
    [ "${mp:-0}" -gt 0 ] 2>/dev/null && kids="$(pgrep -P "$mp" 2>/dev/null | wc -l)"
    holders=0; jobholders=0
    for p in /proc/[0-9]*; do
        if ls -l "$p/fd" 2>/dev/null | grep -q tenstorrent; then
            uu=$(stat -c %u "$p" 2>/dev/null)
            if [ "${uu:-0}" -ge 1000 ]; then
                holders=$((holders+1))
                # A process cannot leave its job's scope cgroup: a holder in one is a broker job's,
                # re-adopted ones included, even when the scope list below cannot be read.
                grep -q 'ttdev-job-' "$p/cgroup" 2>/dev/null && jobholders=$((jobholders+1))
            fi
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
# Returns 0 there, 0 when the broker is down (a down broker protects no job by waiting, and the
# restart is what starts it again), and 0 at JOB_CAP when only a device holder outside every broker
# job is left; 1 at JOB_CAP when a broker job is still running or a device op is in flight. $1 names
# the wait in the log.
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
            if [ "$kids" -eq 0 ] && [ "${scopes:-0}" -eq 0 ] && [ "$jobholders" -eq 0 ] && [ "$holders" -gt 0 ]; then
                echo "autoupdate: $1: a device holder outside any broker job is past the job cap (${JOB_CAP}s) after $(( w_now - w_start ))s (children=$kids holders=$holders scopes=${scopes:-0}); it no longer holds the update, applying" >&2
                return 0
            fi
            echo "autoupdate: $1: the running job is past the job cap (${JOB_CAP}s) after $(( w_now - w_start ))s (children=$kids holders=$holders job_holders=$jobholders scopes=${scopes:-0}); a running broker job always holds the update: dispatch released, the next lap retries" >&2
            return 1
        fi
        sleep 5
        # Refreshed, so the broker's overdue bound on a hold counts from the last poll: a drain
        # that hangs lets go of dispatch on its own, one that polls keeps it.
        hold_write
    done
}
# For the autoupdate_apply event below: whether this lap ran into a busy broker, and how long it drained.
idle=true; drained_s=0
count_busy
if [ "$busy" -ne 0 ]; then
    idle=false; d_t0="$(date +%s)"
    if ! wait_job_boundary drain; then
        rm -f "$HOLD" 2>/dev/null || true
        exit 0
    fi
    drained_s=$(( $(date +%s) - d_t0 ))
    # The apply's own stretch, under the broker's overdue bound, starts now.
    hold_write
fi

echo "autoupdate: applying ${remote_sha:0:9}" >&2

# No-restart apply (spec 08 I29). A diff from the installed sha that touches only files no running
# broker, job subprocess, staged script or host config ever reads moves the tree and installed.sha
# and restarts nothing: every restart reruns the full startup check, fabric pass included, and a
# restart that changes no loaded byte buys nothing for that exposure. An explicit allowlist, and
# fail-safe: any other path, a path git has to quote (no pattern matches it), a diff that cannot
# be computed, a clone not at the installed sha (an earlier apply that stopped after
# its reset may have left the venv or a staged script newer than the running broker), or a broker
# that is not healthy (I21's reasons; the restart may be its fix) restarts as before. Nothing here
# moves the rate limit or the gates above: this runs after them, and leaves last-apply alone.
NO_RESTART_PATHS='docs/** tests/** .github/** *.md (outside src/)'
# Prints why this apply needs the restart, or nothing when it does not.
restart_needed() {
    local p head paths la age
    case "$installed_sha" in
        ''|*[!0-9a-f]*) echo "installed sha '${installed_sha}' is not a commit"; return ;;
    esac
    head="$(git -C "$clone" rev-parse --verify -q HEAD 2>/dev/null)"
    [ "$head" = "$installed_sha" ] || { echo "the clone is at ${head:0:9}, not the installed ${installed_sha:0:9}"; return; }
    paths="$(git -C "$clone" diff --name-only --no-renames "$installed_sha" "$remote_sha" 2>/dev/null)" \
        || { echo "cannot diff ${installed_sha:0:9}..${remote_sha:0:9}"; return; }
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        case "$p" in
            src/*) echo "changes $p"; return ;;   # package data: never by extension
            docs/*|tests/*|.github/*|*.md) ;;
            *) echo "changes $p"; return ;;
        esac
    done <<< "$paths"
    la=""; { read -r la < "$LAST_APPLY"; } 2>/dev/null || true
    case "$la" in ''|*[!0-9]*) age=0 ;; *) age=$(( $(date +%s) - 10#$la )) ;; esac
    [ "$age" -ge 0 ] || age=0
    p="$(broker_broken "$age")"
    [ -z "$p" ] || { echo "the broker $p"; return; }
}
# Decided before the reset below: restart_needed reads where the clone was left.
why_restart="$(restart_needed)"
changed_paths=""
if [ -z "$why_restart" ]; then
    # For the event: restart_needed ran in a subshell, so its list is listed again here.
    changed_paths="$(git -C "$clone" diff --name-only --no-renames "$installed_sha" "$remote_sha" 2>/dev/null)" \
        || why_restart="cannot diff ${installed_sha:0:9}..${remote_sha:0:9}"
fi

# Reset to the sha this lap decided on, NOT to origin/$BR: the drain above can wait for
# minutes, and a branch that moved in the meantime would otherwise install something this lap
# never looked at and never announced — and installed.sha would then name the wrong commit.
git -C "$clone" reset --hard -q "$remote_sha" \
    || { echo "autoupdate: reset failed" >&2; exit 1; }

if [ -z "$why_restart" ]; then
    # Nothing to install or stage: the venv, the staged scripts and the host config already match
    # the new tree, which is now the one on disk.
    printf '%s\n' "$remote_sha" > "$STATE"
    echo "autoupdate: ${installed_sha:0:9} -> ${remote_sha:0:9} changes only ${NO_RESTART_PATHS}; tree and installed.sha moved, broker not restarted" >&2
    EVENTS="${TT_DEVICE_MCP_HEALTH_DIR:-/var/lib/tt-device-broker/health}/health_events.jsonl"
    now="$(date +%s.%N)"
    paths_json="$(printf '%s\n' "$changed_paths" | sed -e '/^$/d' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/.*/"&"/' | paste -sd, -)"
    printf '{"ts": %s, "iso": "%s", "kind": "autoupdate_applied_no_restart", "from_sha": "%s", "to_sha": "%s", "idle": %s, "drained_s": %s, "changed_paths": [%s]}\n' \
        "$now" "$(date -u -d "@${now%.*}" +%Y-%m-%dT%H:%M:%SZ)" "$installed_sha" "$remote_sha" "$idle" "$drained_s" "$paths_json" \
        >> "$EVENTS" 2>/dev/null \
        || echo "autoupdate: warning: could not append autoupdate_applied_no_restart to $EVENTS" >&2
    exit 0
fi
echo "autoupdate: restart needed: $why_restart" >&2

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
            # Check and warm it once before the broker restart below, so the restarted broker's
            # startup self-test finds an importable ttexalens (a running broker picks it up on its
            # paced retry, spec 03 I28). Fail-open, as above, but never silent.
            if ! timeout 60 "$ROOT/eth-venv/bin/python" -c 'import ttexalens' >/dev/null 2>&1; then
                echo "autoupdate: WARNING eth-venv installed but 'import ttexalens' FAILS; eth-heartbeat rung will report OFF" >&2
            fi
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
# Nothing slow runs between the last busy look and the restart, so this goes first.
chmod -R a+rX "$VENV" 2>/dev/null || true
# The old defer marker is obsolete (the drain replaced it); removed once, fine if absent.
rm -f "$ROOT/pending.defer"
# Last look before the restart (spec 08 I24). The install ran under the dispatch hold, but a job
# can still be running here: a tenant that opened the device on its own, or a dispatch after an
# install slow enough for the broker to drop the hold as overdue. It is waited out the same way;
# a broker job or device op still running at the cap restarts nothing and leaves installed.sha on
# the old sha, so the next lap retries.
hold_write
count_busy
if [ $(( kids + holders + ${scopes:-0} )) -ne 0 ] || op_in_flight; then
    idle=false; d_t0="$(date +%s)"
    if ! wait_job_boundary "restart point"; then
        echo "autoupdate: not restarting, not marking current" >&2
        exit 0
    fi
    drained_s=$(( drained_s + $(date +%s) - d_t0 ))
fi
echo "$remote_sha" > "$STATE"

date +%s > "$LAST_APPLY" 2>/dev/null || true   # I21: the rate limit counts from this restart
systemctl restart tt-device-broker
restart_rc=$?
# I21: the crash-loop baseline, read after the restart (an explicit restart may zero NRestarts).
systemctl show tt-device-broker -p NRestarts --value > "$LAST_APPLY_NRESTARTS" 2>/dev/null || true
if [ "$restart_rc" -eq 0 ]; then
    echo "autoupdate: broker restarted at ${remote_sha:0:9} (via $src_desc)" >&2
else
    echo "autoupdate: broker restart at ${remote_sha:0:9} failed (rc=$restart_rc, via $src_desc)" >&2
fi

# One line in the broker's event journal (same fields as health_event(): ts, iso, kind), so a restart
# the update caused is traceable without the systemd journal. restart_rc is the restart's exit code:
# a failed restart is recorded as such, not as a clean apply. Same journal dir as the broker
# (TT_DEVICE_MCP_HEALTH_DIR, else root's default). A single O_APPEND write of one short line, by
# root like the broker; a failed write only warns, the apply is already done.
EVENTS="${TT_DEVICE_MCP_HEALTH_DIR:-/var/lib/tt-device-broker/health}/health_events.jsonl"
now="$(date +%s.%N)"
printf '{"ts": %s, "iso": "%s", "kind": "autoupdate_apply", "from_sha": "%s", "to_sha": "%s", "idle": %s, "drained_s": %s, "restart_rc": %s}\n' \
    "$now" "$(date -u -d "@${now%.*}" +%Y-%m-%dT%H:%M:%SZ)" "$installed_sha" "$remote_sha" "$idle" "$drained_s" "$restart_rc" \
    >> "$EVENTS" 2>/dev/null \
    || echo "autoupdate: warning: could not append autoupdate_apply to $EVENTS" >&2
