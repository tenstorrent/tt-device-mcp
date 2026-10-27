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
# A new sha also waits for its branch to go quiet (600 s) or for the oldest unapplied sha to
# have waited 3600 s, whichever comes first (spec 08 I21, burst coalescing). Code defaults.
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
# and the next lap retries until the job ends (a capped job by the broker's limit kill). A
# no-timeout-except-on-contention job is preempted by the hold itself once past its time limit:
# the broker counts a live hold as a waiting job (spec 01 I20), so the lap keeps the hold through
# the job's limit, the broker's preempt grace and kill grace, and applies when it ends. A drain
# blocks this lap (and the reconcile after it) for at most the job cap.
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

# The clock: whole seconds since the epoch, into the variable named $1, by the shell itself (a
# drain reads it on every 5 s poll). Every time a lap decides on comes from here, so it is the one
# seam a test replaces; defined after the config is sourced, so nothing on a box can.
au_now() { printf -v "$1" '%(%s)T' -1; }
# Epoch seconds $2 as local time, the way the decision log writes it, into the variable named $1.
au_time() { printf -v "$1" '%(%Y-%m-%dT%H:%M:%S%z)T' "$2"; }

# Read as $(cat) would, without a process: the whole file, less its trailing newlines.
installed_sha=""; { IFS= read -r -d '' installed_sha < "$STATE"; } 2>/dev/null
while [ "${installed_sha%$'\n'}" != "$installed_sha" ]; do installed_sha="${installed_sha%$'\n'}"; done
# Already current. A coalescing record (I21, below) belongs to an unapplied sha: with none left, it goes.
if [ "$installed_sha" = "$remote_sha" ]; then rm -f "$ROOT/pending-head" 2>/dev/null; exit 0; fi

echo "autoupdate: $BR ${installed_sha:0:9} -> ${remote_sha:0:9} via $src_desc" >&2

# Decision log (spec 08 I22). Every lap that has an update to make records what it decided
# (deferred, applied, failed), when, and from which sha to which, in the broker's log dir and
# the journal under a stable tag. The stderr lines above and below land in the reconcile unit's
# journal, which may not survive a reboot; this file does, so an operator can match a broker
# restart (and the startup pass after it) to the update that caused it. Kept to about 1 MiB.
AU_LOG="${TTDEV_LOG_DIR:-/var/log/tt-device-broker}/autoupdate.log"
au_logged=0
au_log() {  # <decision> <reason>
    local line t size
    au_now t; au_time line "$t"
    line="$line decision=$1 branch=$BR old=${installed_sha:-none} new=$remote_sha reason=$2"
    au_logged=1
    logger -t tt-device-autoupdate -- "$line" 2>/dev/null || true
    {
        [ -d "${AU_LOG%/*}" ] || mkdir -p "${AU_LOG%/*}"
        size=$(stat -c %s "$AU_LOG" 2>/dev/null) || size=0
        if [ "$size" -ge 1048576 ]; then
            mv -f "$AU_LOG" "$AU_LOG.1"
        fi
        echo "$line" >> "$AU_LOG"
    } 2>/dev/null || true
}

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
    local st n base h age="${1:-0}" re rest
    broken=""
    st="$(systemctl is-active tt-device-broker 2>/dev/null)"
    if [ "$st" != active ]; then broken="is ${st:-unknown}, not active"; return; fi
    n="$(systemctl show tt-device-broker -p NRestarts --value 2>/dev/null)"
    base=""; { read -r base < "$LAST_APPLY_NRESTARTS"; } 2>/dev/null || true
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    case "$base" in ''|*[!0-9]*) base=0 ;; esac
    if [ $(( 10#$n )) -gt $(( 10#$base )) ]; then
        broken="restarted itself since the last apply (NRestarts $(( 10#$base )) -> $(( 10#$n )))"; return
    fi
    h="$(curl -s --max-time 5 --unix-socket /run/tt-device-broker/broker.sock http://localhost/health 2>/dev/null)"
    if [ -z "$h" ]; then broken="does not answer /health"; return; fi
    # The shell's own ERE matching, where a `printf | grep -E` per question cost two processes. None
    # of these patterns can match across a newline, so the whole answer reads as grep reads its lines.
    local recovering='"fsm_state": *"recovering"' unverified='"fsm_why": *"startup_unverified"'
    local held='"held": *true' booting='"held_reason": *"device unverified: ' bad='"fsm_state": *"(recovering|down)"'
    if [[ $h =~ $recovering ]] && [[ $h =~ $unverified ]]; then
        if [ "$age" -gt "$STARTUP_BOUND" ]; then
            broken="is still in its startup check ${age}s after the last apply (bound ${STARTUP_BOUND}s)"; return
        fi
        if ! [[ $h =~ $held ]] || [[ $h =~ $booting ]]; then
            return   # booting: the startup check is running
        fi
    fi
    if [[ $h =~ $held ]]; then broken="holds the device"; return; fi
    if [[ $h =~ $bad ]]; then
        # Every state named, one per line, as `grep -Eo` lists them.
        re='"fsm_state": *"([a-z]*)"'; rest="$h"; st=""
        while [[ $rest =~ $re ]]; do
            st="${st:+$st$'\n'}${BASH_REMATCH[1]}"
            rest="${rest#*"${BASH_REMATCH[0]}"}"
        done
        broken="fails its health check (fsm $st)"
    fi
}
# Burst coalescing (spec 08 I21). The interval above paces restarts but not bursts: a branch that
# moves every few minutes for hours finds a new sha each time the interval ends, so every box
# restarts (and runs its full startup check, fabric included) once per interval for the whole
# burst. So a new sha also waits for the branch to go quiet: it applies once the head has stayed
# the same for APPLY_QUIET, or once the oldest sha not yet applied has waited APPLY_BOUND,
# whichever comes first. A lone push waits APPLY_QUIET; a burst becomes one restart per
# APPLY_BOUND at most, then one onto its last sha APPLY_QUIET after it ends. The interval, the
# gates below and the startup check after the restart are unchanged; a broken broker (I21's
# reasons) applies at once. Every lap records the head it saw before any gate can exit, so a sha
# seen while the interval or another gate defers still counts its time. The record is
# "<first_seen> <sha> <sha_seen>" in $ROOT/pending-head, removed by an apply and when the branch
# is back at the installed sha; a missing, bad or future time counts from now.
# Defaults: 600 s quiet sits above the gap between merges in a landing burst (a few minutes), so
# a burst is one apply, and adds 10 min to a lone fix; 3600 s bound is twice the interval, so a
# continuous burst restarts at most once an hour, and no sha waits longer than that plus a lap and
# the drain (the interval counts from the last apply, before the first sha was seen, so it ends
# within the bound). TTDEV_APPLY_QUIET_SEC=0 turns coalescing off.
PENDING_HEAD="$ROOT/pending-head"
APPLY_QUIET="${TTDEV_APPLY_QUIET_SEC:-600}"
APPLY_BOUND="${TTDEV_APPLY_BOUND_SEC:-3600}"
if [ "$APPLY_QUIET" -gt 0 ] 2>/dev/null; then
    [ "$APPLY_BOUND" -gt 0 ] 2>/dev/null || APPLY_BOUND=3600
    au_now now
    p_first=""; p_sha=""; p_seen=""
    { read -r p_first p_sha p_seen < "$PENDING_HEAD"; } 2>/dev/null || true
    case "$p_first" in ''|*[!0-9]*) p_first=$now ;; *) p_first=$(( 10#$p_first )) ;; esac
    case "$p_seen" in ''|*[!0-9]*) p_seen=$now ;; *) p_seen=$(( 10#$p_seen )) ;; esac
    [ "$p_first" -le "$now" ] || p_first=$now
    [ "$p_seen" -le "$now" ] || p_seen=$now
    [ "$p_sha" = "$remote_sha" ] || p_seen=$now
    { echo "$p_first $remote_sha $p_seen" > "$PENDING_HEAD.$$" && mv -f "$PENDING_HEAD.$$" "$PENDING_HEAD"; } 2>/dev/null || true
    quiet_left=$(( p_seen + APPLY_QUIET - now ))
    bound_left=$(( p_first + APPLY_BOUND - now ))
    if [ "$quiet_left" -gt 0 ] && [ "$bound_left" -gt 0 ]; then
        la=""; { read -r la < "$LAST_APPLY"; } 2>/dev/null || true
        case "$la" in ''|*[!0-9]*) age=0 ;; *) age=$(( now - 10#$la )) ;; esac
        [ "$age" -ge 0 ] || age=0
        broker_broken "$age"
        if [ -z "$broken" ]; then
            left=$quiet_left; [ "$bound_left" -lt "$left" ] && left=$bound_left
            echo "autoupdate: coalescing: head ${remote_sha:0:9} seen $(( now - p_seen ))s ago (quiet ${APPLY_QUIET}s), oldest unapplied $(( now - p_first ))s ago (bound ${APPLY_BOUND}s); applies in ${left}s if the branch stays put" >&2
            au_time apply_by $(( now + left ))
            au_log deferred "burst coalescing: head unchanged $(( now - p_seen ))s (quiet ${APPLY_QUIET}s), oldest unapplied $(( now - p_first ))s (bound ${APPLY_BOUND}s); apply_by=$apply_by"
            exit 0
        fi
        echo "autoupdate: coalescing skipped: the broker $broken; ${remote_sha:0:9} goes on to the rate limit" >&2
    fi
fi

if [ "$MIN_APPLY" -gt 0 ] 2>/dev/null; then
    last=""; { read -r last < "$LAST_APPLY"; } 2>/dev/null || true
    au_now now
    # A stamp that is not a number, or lies in the future (the clock stepped back), bars nothing.
    # Read base 10, so a hand-written leading zero is not taken for octal.
    case "$last" in ''|*[!0-9]*) last="" ;; *) last=$(( 10#$last )) ;; esac
    if [ -n "$last" ] && [ "$last" -le "$now" ] && [ $(( now - last )) -lt "$MIN_APPLY" ]; then
        broker_broken "$(( now - last ))"
        if [ -z "$broken" ]; then
            echo "autoupdate: rate-limited: last apply $(( now - last ))s ago (min ${MIN_APPLY}s); ${remote_sha:0:9} applies in $(( last + MIN_APPLY - now ))s" >&2
            au_time apply_by $(( last + MIN_APPLY ))
            au_log deferred "apply rate limit: last apply $(( now - last ))s ago (min ${MIN_APPLY}s); apply_by=$apply_by"
            exit 0
        fi
        echo "autoupdate: rate limit skipped: the broker $broken; ${remote_sha:0:9} applies now (last apply $(( now - last ))s ago)" >&2
    fi
fi

# An in-flight device operation is an ABSOLUTE bar — not the bounded defer below.
# Restarting the broker mid-reset lets KillMode=control-group SIGTERM the running
# tt-smi, leaving 32 ASICs half-reset, which is the state that costs a power-cycle.
# A device op is not bounded by the broker: a reset scope that outlives its timeout is
# left running. So the waits below hold dispatch for an op at most OP_CAP, then let go
# of it and leave the update to the next lap; never restart over one (wait_job_boundary).
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
if reset_units="$(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-reset-*' 2>/dev/null)"
        [[ $reset_units == *ttdev-reset-* ]]; then
    echo "autoupdate: a device reset scope is still running; deferring" >&2
    au_log deferred "device reset scope running; apply_by=when the reset ends (bounded)"
    exit 0
fi

# Recovery deferral (spec 08 I23). The device-op bar above covers a rung while it runs, not the
# hold around it: between rungs (a settle, a backoff, the wait for the hold deadline) no op is in
# flight, and a restart there throws the episode back to the startup check, a forced fabric pass
# on a mesh the broker already knows is bad, and can turn a heal in progress into a longer one.
# So while the broker's /health reports a hold, or its FSM is recovering or down, the update
# waits, bounded: it applies once the hold has lasted RECOVERY_DEFER (above every fault class's
# own time-to-recover budget, so a recovery still over it is over budget and the update may be
# its fix; a hold waiting out the power-cycle cooldown can be longer, and its durable anchors
# keep the cycle's deadline across the restart), or once this pending update has waited
# RECOVERY_DEFER in all, so a hold that keeps coming back cannot starve a box of a fix. Never
# more than 30 min; 0 turns it off. A broker that does not answer /health is not deferred for:
# an update may be what it needs. Every recovery deferral comes before the install: once pip has
# run, the lap restarts. The deferral delays only the restart; no rung, reset, power cycle or
# escalation waits on it.
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
# A running no-timeout-except-on-contention job, from the broker's /health: prints "<running jobs>
# <its job id> <seconds before its time limit ends, 0 past it>", nothing if no answer or none runs.
broker_preempt() {
    curl -fsS --max-time 5 --unix-socket "$SOCK" http://localhost/health 2>/dev/null \
        | "$VENV/bin/python" -I -c 'import json, sys
h = json.load(sys.stdin)
left = h.get("preempt_limit_left_sec")
if h.get("preempt_job_id") and isinstance(left, int) and isinstance(h.get("running"), int):
    print(h["running"], h["preempt_job_id"], max(0, left))' 2>/dev/null
}
# Whether /health still names job $1 as the running preemptable job: prints 1 (it does), 0 (it
# answers without it: the job has ended), nothing if no answer.
broker_names_preempt_job() {
    curl -fsS --max-time 5 --unix-socket "$SOCK" http://localhost/health 2>/dev/null \
        | "$VENV/bin/python" -I -c 'import json, sys
h = json.load(sys.stdin)
print(int(h.get("preempt_job_id") == sys.argv[1]))' "$1" 2>/dev/null
}
# Succeeds (and names why in $recovery_why) while the update should wait for the recovery.
recovery_gate() {
    local held="" state="" age="" now since="" sha="" waited apply_by
    recovery_why=""
    read -r held state age <<EOF
$(broker_hold)
EOF
    case "${held:-0}:${state:-}" in
        1:*|*:recovering|*:down) ;;
        *) return 1 ;;
    esac
    [ "$RECOVERY_DEFER" -gt 0 ] 2>/dev/null || return 1
    au_now now
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
    au_time apply_by "$deadline"
    recovery_why="fsm=$state held=$held hold_age=${age:--1}s waited=${waited}s bound=${RECOVERY_DEFER}s apply_by=$apply_by"
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
    local t; au_now t
    { echo "$$ $t" > "$HOLD.$$" && mv -f "$HOLD.$$" "$HOLD"; } 2>/dev/null || true
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
# (spec 01 I20) is not killed for time: it ends by itself, or on contention past its time limit, and
# the hold is contention (the broker counts a live hold as one waiting job, never dispatched). So
# when the broker reports the only running job as one, the bound is the time left before its limit
# plus PREEMPT_TAIL (the broker's preempt grace, its kill grace and a margin for the job's exit),
# from this wait's start; never shorter than the job cap's own bound. Once that job has ended (its
# scope gone, or /health no longer names it), the broker's post-job gate runs before the job
# boundary (a kill reset, the full check with fabric): what is left is held as that gate for at most
# GATE_CAP from the job's end, so a lap whose hold preempted the job applies under the same hold.
# A limit too far off for limit + PREEMPT_TAIL to fit under JOB_CAP is never held through: the lap
# releases dispatch at once, before the hold can preempt anything, and a later lap nearer the limit
# applies. Only a device holder outside every broker job, which no broker kill ends, stops holding
# the update at the bound, so a box busy around the clock still updates.
JOB_CAP=1560
# PREEMPT_GRACE_SEC (60) + PREEMPT_KILL_GRACE_SEC (10) + 30 s for the job's exit to show.
PREEMPT_TAIL=100
# A device op's own bound, DEVICE_RESET_TIMEOUT_SEC (600) plus the same kill grace: how long an op
# with no job running may keep the hold (see wait_job_boundary). It bounds the wait, never the
# bar: past it the lap releases dispatch and restarts nothing (I7); the next lap retries.
OP_CAP=660
# The post-job gate after a preemption: a kill reset (OP_CAP) and the full check with fabric,
# FABRIC_CHECK_TIMEOUT_SEC (180) + FULL_CHECK_PER_CHIP_SEC (3) on 32 chips. Past it the lap
# releases dispatch and restarts nothing (I7); the next lap retries.
GATE_CAP=936
UPTIME=/proc/uptime
count_busy() {
    local mp p uu q out cg l
    mp="$(systemctl show tt-device-broker -p MainPID --value 2>/dev/null)"
    # A stopped broker reads MainPID 0, and `pgrep -P 0` lists init and kthreadd: count no kids.
    kids=0
    if [ "${mp:-0}" -gt 0 ] 2>/dev/null; then
        # One pid a line, counted as `wc -l` counts them, without the pipe.
        out="$(pgrep -P "$mp" 2>/dev/null)"; l="${out//[!$'\n']/}"
        [ -z "$out" ] || kids=$(( ${#l} + 1 ))
    fi
    holders=0; jobholders=0
    for p in /proc/[0-9]*; do
        if [[ $(ls -l "$p/fd" 2>/dev/null) == *tenstorrent* ]]; then
            uu=$(stat -c %u "$p" 2>/dev/null)
            if [ "${uu:-0}" -ge 1000 ]; then
                holders=$((holders+1))
                # A process cannot leave its job's scope cgroup: a holder in one is a broker job's,
                # re-adopted ones included, even when the scope list below cannot be read.
                cg=""; { IFS= read -r -d '' cg < "$p/cgroup"; } 2>/dev/null
                [[ $cg == *ttdev-job-* ]] && jobholders=$((jobholders+1))
            fi
        fi
    done
    # A re-adopted job is no child of this broker, and a queued job is about to be one: a job
    # scope still running and a persisted queued spec both count as busy.
    out="$(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-job-*' 2>/dev/null)"
    scopes=0   # the lines naming a job scope, as `grep -c` counts them
    while IFS= read -r l; do [[ $l == *ttdev-job-* ]] && scopes=$((scopes+1)); done <<< "$out"
    queued=0
    for q in "${TTDEV_LOG_DIR:-/var/log/tt-device-broker}"/queued/*.json; do
        [ -e "$q" ] && queued=$((queued+1))
    done
    busy=$(( kids + holders + ${scopes:-0} + queued ))
}
# Seconds the youngest running job scope has run (j_age), from systemd's own record of its start:
# a re-adopted job keeps its scope, so its age survives broker restarts. 0 when none can be read.
youngest_job_age() {
    local up="" u t a out l re='ttdev-job-[^ ]*\.scope' units=""
    j_age=""
    { IFS= read -r up < "$UPTIME"; } 2>/dev/null; up="${up%% *}"; up="${up%.*}"
    # Every unit name in the list, as `grep -o` picks them out of each line.
    out="$(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-job-*' 2>/dev/null)"
    while IFS= read -r l; do
        while [[ $l =~ $re ]]; do units="$units ${BASH_REMATCH[0]}"; l="${l#*"${BASH_REMATCH[0]}"}"; done
    done <<< "$out"
    for u in $units; do
        t="$(systemctl show "$u" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)"
        if [ -n "$up" ] && [ "${t:-0}" -gt 0 ] 2>/dev/null; then a=$(( up - t / 1000000 )); else a=0; fi
        { [ -z "$j_age" ] || [ "$a" -lt "$j_age" ]; } && j_age="$a"
    done
    [ -n "$j_age" ] || j_age=0
}
# The device-op bar above, asked again while a drain waits: the broker can start a device op (a
# gate pass, a reset) on its own while dispatch is held, and the apply must not restart over it.
# op_lock_live: the broker itself runs the op (the device-op lock names a live pid), inside its own
# cgroup, so a restart SIGTERMs it. reset_scope_listed: a ttdev-reset-* scope, its own systemd unit,
# which a broker restart does not stop; a new broker's startup waits it out (await_foreign_scope)
# before anything touches the device.
op_lock_live() {
    local op_pid="" op_name=""
    [ -f "$INHIBIT" ] || return 1
    read -r op_pid op_name < "$INHIBIT" 2>/dev/null || true
    [ -n "${op_pid:-}" ] && kill -0 "$op_pid" 2>/dev/null
}
reset_scope_listed() {
    local out
    out="$(systemctl list-units --type=scope --state=running --no-legend --no-pager 'ttdev-reset-*' 2>/dev/null)"
    [[ $out == *ttdev-reset-* ]]
}
op_in_flight() { op_lock_live || reset_scope_listed; }
job_scope_listed() {
    systemctl list-units --type=scope --state=running --no-legend --no-pager "ttdev-job-$1.scope" 2>/dev/null \
        | grep -q "ttdev-job-$1\.scope"
}
# Whether the preemptable job the drain holds for has ended: its scope, listed when the wait began,
# is gone, or /health answers without naming it. No answer and no scope to read: not known to.
preempt_job_ended() {
    if [ "$p_scope" = 1 ] && ! job_scope_listed "$p_job"; then return 0; fi
    [ "$(broker_names_preempt_job "$p_job")" = 0 ]
}
# Wait, under the dispatch hold, for the job boundary: nothing running and no device op in flight.
# Returns 0 there, 0 when the broker is down and runs no op of its own (a down broker protects no
# job by waiting, and the restart is what starts it again; a reset scope outlives it in its own unit
# and the new broker's startup waits that out before the queue opens), and 0 at JOB_CAP when only a
# device holder outside every broker job is left; 1 at JOB_CAP when a broker job is still running or
# a device op is in flight. A device op with no job running holds the wait for at most OP_CAP from
# when it is first seen alone, then 1 too: the broker does not bound every op (a reset that outlives
# its timeout is left running in its scope), and an update never restarts over one (I7). The only
# running job preemptable: 1 at once when its limit is too far off to hold through (no kill on a lap
# that cannot apply), and once it has ended the broker's post-job gate holds the wait for at most
# GATE_CAP, then 1. $1 names the wait in the log; a return of 1 names why in $drain_why.
wait_job_boundary() {
    local w_start w_now w_bound w_why op_since="" p_running="" p_job="" p_left="" p_scope=0 p_end=""
    drain_why=""
    youngest_job_age
    w_bound=$(( JOB_CAP - j_age )); [ "$w_bound" -gt 0 ] || w_bound=0
    w_why="job cap ${JOB_CAP}s, youngest job ${j_age}s in"
    # Spec 01 I20, spec 08 I20: the only running job is preemptable, so the hold preempts it once
    # past its limit; keep the hold that long.
    read -r p_running p_job p_left <<EOF
$(broker_preempt)
EOF
    if [ "${p_running:-0}" = 1 ] && [ -n "${p_job:-}" ] && [ "${p_left:--1}" -ge 0 ] 2>/dev/null; then
        if [ $(( p_left + PREEMPT_TAIL )) -gt "$JOB_CAP" ]; then
            # Held through, the hold would preempt the job at its limit and then let go at the cap:
            # a kill on a lap that cannot apply. Let go now; nothing is contention before the limit.
            echo "autoupdate: $1: the only running job $p_job is preemptable and ${p_left}s before its limit: limit + ${PREEMPT_TAIL}s is past the job cap (${JOB_CAP}s), so it is not held through; dispatch released, a later lap nearer the limit applies" >&2
            drain_why="preemptable job ${p_left}s before its limit, past the job cap (${JOB_CAP}s)"
            return 1
        fi
        p_left=$(( p_left + PREEMPT_TAIL ))
        [ "$p_left" -le "$w_bound" ] || w_bound=$p_left
        job_scope_listed "$p_job" && p_scope=1
        w_why="$w_why; preemptable job $p_job: limit + ${PREEMPT_TAIL}s, then its post-job gate for at most ${GATE_CAP}s"
    else
        p_job=""
    fi
    au_now w_start
    echo "autoupdate: $1: busy (children=$kids holders=$holders scopes=${scopes:-0} queued=$queued) -> draining: dispatch held, the running job finishes, then the update applies (bound ${w_bound}s: $w_why)" >&2
    while :; do
        count_busy
        running=$(( kids + holders + ${scopes:-0} ))
        if [ "$running" -eq 0 ] && ! op_in_flight; then
            au_now w_now
            echo "autoupdate: $1: drained after $(( w_now - w_start ))s (queued=$queued wait for the new broker)" >&2
            return 0
        fi
        au_now w_now
        if ! op_lock_live; then
            case "$(systemctl is-active tt-device-broker 2>/dev/null)" in
                active|activating|deactivating|reloading) ;;
                *)  echo "autoupdate: $1: the broker is down after $(( w_now - w_start ))s; applying (the restart starts it, and its startup waits out any reset scope before the queue opens)" >&2
                    return 0 ;;
            esac
        fi
        if [ -n "$p_job" ] && [ -z "$p_end" ] && preempt_job_ended; then
            p_end="$w_now"
            echo "autoupdate: $1: the preemptable job $p_job ended after $(( w_now - w_start ))s; the broker's post-job gate is waited out under the hold for at most ${GATE_CAP}s" >&2
        fi
        if [ -n "$p_end" ] && [ "${scopes:-0}" -eq 0 ] && [ "$jobholders" -eq 0 ] && { [ "$kids" -gt 0 ] || op_in_flight; }; then
            # The broker's own post-job gate: its probes are its children, and its reset holds the op lock.
            if [ $(( w_now - p_end )) -ge "$GATE_CAP" ]; then
                echo "autoupdate: $1: the broker's post-job gate is still running ${GATE_CAP}s after the preempted job ended; never restarted over (I7): dispatch released, the next lap retries" >&2
                drain_why="post-job gate still running ${GATE_CAP}s after the preemption"
                return 1
            fi
        elif op_in_flight && [ "$running" -eq 0 ]; then
            if [ -z "$op_since" ]; then
                op_since="$w_now"
                echo "autoupdate: $1: a device op is in flight after $(( w_now - w_start ))s with no job running; it is waited out under the hold for at most ${OP_CAP}s" >&2
            fi
            if [ $(( w_now - op_since )) -ge "$OP_CAP" ]; then
                echo "autoupdate: $1: a device op is still in flight ${OP_CAP}s on; never restarted over (I7): dispatch released, the next lap retries" >&2
                return 1
            fi
        else
            op_since=""
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
    idle=false; au_now d_t0
    if ! wait_job_boundary drain; then
        rm -f "$HOLD" 2>/dev/null || true
        au_log deferred "${drain_why:-broker job or device op still running at the job cap (${JOB_CAP}s)}; dispatch released, the next lap retries"
        exit 0
    fi
    au_now d_t1; drained_s=$(( d_t1 - d_t0 ))
    # The apply's own stretch, under the broker's overdue bound, starts now.
    hold_write
fi

# The recovery deferral once more, and for the last time (spec 08 I23): the drain above can wait
# out a whole job, and the broker starts holds on its own meanwhile. Nothing is installed yet. From
# the install on, pip and apply-host-config change the broker's venv, units and timer, and a lap
# that stopped after them would leave the old broker running on the new code (it imports some
# modules lazily, at a fault's onset) until a later lap. So a lap defers here or not at all, and
# one that defers drops the dispatch hold and leaves the tree and installed.sha alone.
if recovery_gate; then
    rm -f "$HOLD" 2>/dev/null || true
    echo "autoupdate: broker recovery began before the install ($recovery_why); deferring" >&2
    au_log deferred "broker recovery in progress before the install ($recovery_why)"
    exit 0
fi
[ -n "$recovery_why" ] && echo "autoupdate: broker recovery past the deferral bound ($recovery_why); applying" >&2

echo "autoupdate: applying ${remote_sha:0:9}" >&2
trap au_exit EXIT

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
    local p head paths la age now
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
    au_now now
    case "$la" in ''|*[!0-9]*) age=0 ;; *) age=$(( now - 10#$la )) ;; esac
    [ "$age" -ge 0 ] || age=0
    broker_broken "$age"
    [ -z "$broken" ] || { echo "the broker $broken"; return; }
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
    # I23: the deferral clock belongs to an update not yet applied; a later one starts its own.
    rm -f "$RECOVERY_PENDING" 2>/dev/null || true
    rm -f "$ROOT/pending-head" 2>/dev/null || true   # I21 coalescing: so does the bound
    echo "autoupdate: ${installed_sha:0:9} -> ${remote_sha:0:9} changes only ${NO_RESTART_PATHS}; tree and installed.sha moved, broker not restarted" >&2
    EVENTS="${TT_DEVICE_MCP_HEALTH_DIR:-/var/lib/tt-device-broker/health}/health_events.jsonl"
    now="$(date +%s.%N)"
    paths_json="$(printf '%s\n' "$changed_paths" | sed -e '/^$/d' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/.*/"&"/' | paste -sd, -)"
    printf '{"ts": %s, "iso": "%s", "kind": "autoupdate_applied_no_restart", "from_sha": "%s", "to_sha": "%s", "idle": %s, "drained_s": %s, "changed_paths": [%s]}\n' \
        "$now" "$(date -u -d "@${now%.*}" +%Y-%m-%dT%H:%M:%SZ)" "$installed_sha" "$remote_sha" "$idle" "$drained_s" "$paths_json" \
        >> "$EVENTS" 2>/dev/null \
        || echo "autoupdate: warning: could not append autoupdate_applied_no_restart to $EVENTS" >&2
    au_log applied "no restart: changes only ${NO_RESTART_PATHS}"
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
# a broker job still running at the job cap, or a device op still in flight at its bound (the job
# cap, or OP_CAP with no job running), restarts nothing and leaves installed.sha on the old sha, so
# the next lap retries.
hold_write
count_busy
if [ $(( kids + holders + ${scopes:-0} )) -ne 0 ] || op_in_flight; then
    idle=false; au_now d_t0
    if ! wait_job_boundary "restart point"; then
        echo "autoupdate: not restarting, not marking current" >&2
        au_log deferred "${drain_why:-broker job or device op still running at restart time (job cap ${JOB_CAP}s)}"
        exit 0
    fi
    au_now d_t1; drained_s=$(( drained_s + d_t1 - d_t0 ))
fi
# No recovery deferral from here on (spec 08 I23): the install has already changed the broker's
# code, and an old broker left running on it is worse than a restart. A hold that began during the
# install is named and the restart goes ahead; the new broker's startup check (I20, fabric
# included) holds the device again until it passes.
recovery_gate || :
[ -n "$recovery_why" ] \
    && echo "autoupdate: broker recovery began during the install ($recovery_why); restarting, the install has changed the broker's code" >&2
echo "$remote_sha" > "$STATE"
rm -f "$ROOT/pending-head" 2>/dev/null || true   # I21 coalescing: the next sha starts its own bound

date +%s > "$LAST_APPLY" 2>/dev/null || true   # I21: the rate limit counts from this restart
systemctl restart tt-device-broker
restart_rc=$?
rm -f "$RECOVERY_PENDING" 2>/dev/null || true
# I21: the crash-loop baseline, read after the restart (an explicit restart may zero NRestarts).
systemctl show tt-device-broker -p NRestarts --value > "$LAST_APPLY_NRESTARTS" 2>/dev/null || true
if [ "$restart_rc" -eq 0 ]; then
    echo "autoupdate: broker restarted at ${remote_sha:0:9} (via $src_desc)" >&2
    au_log applied "broker restarted${recovery_why:+ (into a recovery: $recovery_why)}"
else
    echo "autoupdate: broker restart at ${remote_sha:0:9} failed (rc=$restart_rc, via $src_desc); reconcile starts it" >&2
    au_log failed "broker restart failed (exit $restart_rc)"
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
