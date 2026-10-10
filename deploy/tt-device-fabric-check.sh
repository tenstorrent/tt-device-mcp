#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Fabric traffic health check for the tt-device-broker health gate.
#
# The broker resolves and runs the pinned validator directly when
# TT_DEVICE_MCP_FABRIC_CHECK_CMD is unset (see health.monitors.fabric.build_command), so
# this script is no longer required on a stock host. It stays as a template for an operator
# who wants their own wrapper — a different validator invocation, extra checks bolted on, a
# non-default layout — set TT_DEVICE_MCP_FABRIC_CHECK_CMD to it (or a copy) and the broker
# runs it unconditionally in place of the built-in path, judged on its exit code alone:
#
#   exit 0   => fabric verified healthy.
#   exit 77  => the check could not run at all (binary/descriptor absent, or the
#               descriptor does not describe this host). The broker records this and
#               SKIPS — it does not reset, because nothing about the device was learned.
#   exit *   => fabric UNHEALTHY -> the broker resets the device.
#
# The distinction between 0 and 77 is the whole contract. This script used to
# `exit 0` whenever it failed to build or could not find its inputs, which reports
# a mesh it never looked at as healthy — a check that cannot fail is not a check.
# "I did not run" and "I ran and the fabric is fine" must never share an exit code.
#
# The same line divides 77 from UNHEALTHY, and it is drawn at a single question: could
# a board reset possibly fix this? A dead link, yes. A descriptor that describes other
# hardware, never — so routing that to the reset path resets after every job, forever,
# and converges on nothing. "I cannot check this host" is not "this host is broken".
#
# It pushes one round of packets across every inter-chip ethernet link via
# tt-metal's run_cluster_validation — the only check that proves the fabric moves
# data, where tt-smi only proves chips enumerate. ~20-45s.
#
# The validator comes from the pinned build published under /opt by
# install-fabric-validator.sh (deploy/fabric-validator.pin), so every host validates
# with identical code. This script never builds anything: a build takes tens of
# minutes, and a health check that can block for tens of minutes is not a health
# check. If the pinned build has not landed yet, we report CANNOT CHECK and the
# broker leaves the device alone.
#
# Config (env):
#   TTDEV_VALIDATOR_ROOT      pinned-build root (default /opt/tt-device-broker/validator)
#   TTDEV_FABRIC_BIN          override the validator binary outright
#   TTDEV_FABRIC_DESCRIPTOR   cabling descriptor textproto — OPTIONAL, and only set it to one that
#                             describes THIS machine. Unset (the default) = traffic-only, which is
#                             what makes the check work on every machine config.
#   TTDEV_FABRIC_ITERS        traffic iterations (default 1)
set -u

EXIT_CANNOT_CHECK=77

# Once $OUT exists, every log line also goes to $LAST: a small file, overwritten each run, that keeps
# the last run's verdict and output tail where an operator can read it after the journal rotated.
LAST=""
log() {
    echo "fabric-check: $*" >&2
    [ -n "$LAST" ] && echo "fabric-check: $*" >>"$LAST"
    return 0
}
cannot_check() { log "$* -> CANNOT CHECK (exit $EXIT_CANNOT_CHECK; broker will not reset on this)"; exit "$EXIT_CANNOT_CHECK"; }

VROOT="${TTDEV_VALIDATOR_ROOT:-/opt/tt-device-broker/validator}"
CURRENT="$VROOT/current"
ITERS="${TTDEV_FABRIC_ITERS:-1}"

# Dedicated firmware/kernel cache so the (root-run) check never depends on $HOME
# and never pollutes a user's cache.
export TT_METAL_CACHE="${TTDEV_FABRIC_CACHE:-/var/cache/tt-device-broker/fabric-tt-metal-cache}"
mkdir -p "$TT_METAL_CACHE" 2>/dev/null || true

BIN="${TTDEV_FABRIC_BIN:-$CURRENT/build/tools/scaleout/run_cluster_validation}"
[ -x "$BIN" ] || cannot_check "pinned validator not built yet ('$BIN'); see tt-device-fabric-validator.service"

# run_cluster_validation resolves its kernels from the runtime root and has an RPATH
# into its own build tree, so it must run against the checkout it was built from —
# not against whatever tt-metal happens to be on the host.
RUNTIME_ROOT="${TTDEV_FABRIC_RUNTIME_ROOT:-$CURRENT}"
[ -d "$RUNTIME_ROOT" ] || cannot_check "validator runtime root '$RUNTIME_ROOT' missing"
export TT_METAL_RUNTIME_ROOT="$RUNTIME_ROOT"
export TT_METAL_HOME="$RUNTIME_ROOT"

# The cabling descriptor is OPTIONAL, and that is what makes this check portable.
#
# It buys one thing: comparing the discovered topology against a golden one, which catches a link
# that is missing entirely (traffic can only test links discovery FOUND). It is not what proves the
# fabric moves data. The validator derives validate_connectivity purely from whether a descriptor was
# passed (run_cluster_validation.cpp:232), skips the whole FSD path when it was not (:298), and runs
# generate_link_metrics on the DISCOVERED topology either way (:426) — so --send-traffic --hard-fail
# still pushes packets over every real link and still fails on a bad one.
#
# Defaulting it to the galaxy descriptor made this check galaxy-only for no reason: on any other
# machine the FSD-vs-GSD comparison aborts on hardware it does not describe (a loudbox is a P150_LB,
# motherboard H13DSG-O-CPU; the galaxy descriptor declares S7T-MB), taking the traffic pass down with
# it. So the descriptor is now opt-in per host, and every machine gets the traffic check.
DESC="${TTDEV_FABRIC_DESCRIPTOR:-}"
desc_argv=()
if [ -n "$DESC" ]; then
    [ -f "$DESC" ] || cannot_check "cabling descriptor '$DESC' missing"
    desc_argv=(--cabling-descriptor-path "$DESC")
    log "descriptor: $DESC (adds golden-topology validation)"
else
    log "no descriptor configured: traffic-only check over every discovered inter-chip link"
fi

OUT="${TTDEV_FABRIC_OUTPUT:-$TT_METAL_CACHE/cluster_validation_logs}"
mkdir -p "$OUT" 2>/dev/null || true
# Bounded tails only (see tail_to_last): a chatty validator must not grow this file.
if : >"$OUT/last-run.log" 2>/dev/null; then
    LAST="$OUT/last-run.log"
    echo "fabric-check: run started $(date -Is)" >>"$LAST"
fi
tail_to_last() { [ -n "$LAST" ] && tail -n 200 "$1" | cut -c1-400 >>"$LAST"; return 0; }

log "running run_cluster_validation --send-traffic --num-iterations $ITERS"
cd "$RUNTIME_ROOT" || cannot_check "cannot cd to '$RUNTIME_ROOT'"
RUNLOG="$(mktemp)" || cannot_check "cannot create a temp file for the validator output"
trap 'rm -f "$RUNLOG"' EXIT
"$BIN" "${desc_argv[@]}" --output-path "$OUT" --hard-fail --send-traffic --num-iterations "$ITERS" \
    >"$RUNLOG" 2>&1
rc=$?
cat "$RUNLOG" >&2
tail_to_last "$RUNLOG"


# UNHEALTHY is a VERDICT, and only the validator can return one. It reaches that verdict in exactly
# one place — generate_link_metrics comes back with an unhealthy link and --hard-fail turns it into
#   TT_THROW("Encountered unhealthy ethernet connections, listed above")   (run_cluster_validation.cpp:440)
# — and that is the only outcome a board reset can act on.
#
# Every OTHER non-zero exit means the validator never finished measuring: the descriptor did not
# describe this host, a directory was not writable, the binary was killed, it aborted somewhere in
# setup. Treating those as UNHEALTHY resets the board over something no reset can fix, which is how a
# health gate turns into a reset loop. Both real failures were of exactly this shape: the galaxy
# descriptor aborting in FSD-vs-GSD on hardware it does not describe, and a permission error while
# creating the watcher's kernel file. Neither one had touched a link.
#
# So: measured-bad is UNHEALTHY; did-not-measure is CANNOT CHECK. Anything the script cannot place in
# the first bucket belongs in the second.
if [ "$rc" -eq 0 ]; then
    log "fabric healthy"
elif grep -q "unhealthy ethernet connections" "$RUNLOG"; then
    log "fabric UNHEALTHY (rc=$rc): the validator measured bad links -> the broker will reset"
    [ "$rc" -eq "$EXIT_CANNOT_CHECK" ] && rc=1   # never collide with the cannot-check sentinel
elif grep -qE "waiting for active ethernet core|Try resetting the board" "$RUNLOG"; then
    # A wedged eth core stalls the validator's OWN device init before it can measure a link —
    # so it never reaches the "unhealthy ethernet connections" verdict and would otherwise fall
    # to CANNOT CHECK. But this one IS resettable (the firmware says so), and skipping it leaves
    # the wedge riding on: the check that would trigger recovery is the one the wedge blocks.
    log "fabric UNHEALTHY (rc=$rc): a wedged ethernet core stalled the validator's device init before any link was measured -> resettable, the broker will reset"
    [ "$rc" -eq "$EXIT_CANNOT_CHECK" ] && rc=1
elif grep -qE "Workload execution timed out after [0-9]+ seconds" "$RUNLOG"; then
    # The validator's own per-iteration watchdog: traffic went out and no link ever reported
    # back, so it never reaches the "unhealthy ethernet connections" verdict and would fall to
    # CANNOT CHECK. But a stalled traffic pass IS a measurement — the validator calls it "the
    # cluster is in an unhealthy state" — and it is the dominant reason this host learns nothing
    # about its fabric. Skipping it leaves the wedge riding on, exactly like the eth-core case.
    log "fabric UNHEALTHY (rc=$rc): traffic stalled until the validator's watchdog fired; no link reported back -> resettable"
    [ "$rc" -eq "$EXIT_CANNOT_CHECK" ] && rc=1
else
    # The validator's FIRST error line, most specific first (same order as fabric.first_reason):
    # "terminate called after throwing ..." sits on the line ABOVE "  what():  <cause>", so a single
    # first-match grep kept the exception type and dropped the cause — a missing hugepage pool read
    # as a bare UmdException.
    reason=""
    for pat in 'what\(\):.*' 'filesystem error:.*' \
        '(\|\s*(critical|fatal|error)\s*\||\[(critical|error)\]|TT_THROW|TT_FATAL).*' 'terminate called.*'; do
        reason="$(grep -m1 -oiE "$pat" "$RUNLOG" | head -1 | cut -c1-300)"
        [ -n "$reason" ] && break
    done
    [ -n "$reason" ] || reason="last output: $(grep -vE '^\s*$' "$RUNLOG" | sed -n '$p' | cut -c1-200)"
    # The reason goes on the FINAL line: an operator's wrapper is summarized by its last line, and
    # a cause printed above it was dropped from every log that quoted the check.
    log "no link was tested, so this says nothing about the fabric -- see $OUT/last-run.log"
    cannot_check "validator did not complete a measurement (rc=$rc): $reason"
fi
[ "$rc" -eq 0 ] || exit "$rc"

# ---- dispatch ----------------------------------------------------------------------------------
# A green fabric is not a usable mesh. The validator drives inter-chip ETHERNET traffic and never
# enqueues a program, so it returns 0 on a mesh whose DISPATCH path is dead — measured: exit 0
# seventy seconds before a cold worker hung in warmup, and again with all 8 chips throwing at
# program.cpp:260 while the ARC heartbeat read healthy. A reset can leave the bus fine and dispatch
# wedged, which is how a queue spends hours failing every job at the operation timeout.
#
# Folded in here rather than made its own check: this runs only where the broker already pays for a
# fabric pass, and a second entry point would mean a second device open for one more verdict.
# Runs LAST and only on a green fabric — the gentler, cheaper measurement first, and a fabric that
# already failed has its answer.
#
# The probe is the pinned build's own mesh example, not a python snippet: it opens the whole mesh in
# ONE process and dispatches to every chip in 14s, where opening eight chips one at a time from
# python costs ~14s EACH. That difference is the whole design. The first version of this check was
# the python loop, and on a healthy mesh it took ~120s, tripped its own cap, and was read as a wedge
# — it reset the box repeatedly for being slow. A probe that cannot finish comfortably inside its
# cap cannot be trusted to say anything.
DBIN="${TTDEV_DISPATCH_BIN:-$CURRENT/build/programming_examples/distributed/distributed_program_dispatch}"
[ -x "$DBIN" ] || {
    log "pinned dispatch probe not built ('$DBIN'); fabric verdict stands on its own"
    exit 0
}
DTMO="${TTDEV_DISPATCH_TIMEOUT:-90}"
DOUT="$(mktemp)" || cannot_check "cannot create a temp file for the dispatch probe"
trap 'rm -f "$RUNLOG" "$DOUT"' EXIT

log "fabric healthy; dispatching a program to every chip via the mesh (typ 14s, cap ${DTMO}s)"
# The operation timeout is what turns a hang into a verdict. Without it a wedged core sits in
# metal's own 180s-per-device wait, outlasts the cap, and all we learn is that we timed out; with
# it metal throws by name and the probe exits non-zero having actually measured something.
TT_METAL_OPERATION_TIMEOUT_SECONDS="${TTDEV_DISPATCH_OP_TIMEOUT:-25}" \
    timeout --signal=KILL "$DTMO" "$DBIN" >"$DOUT" 2>&1
drc=$?

case "$drc" in
    0)   log "dispatch OK: every chip ran a program"; exit 0 ;;
    # Killed at the cap. We did NOT measure a wedge — we failed to measure at all, and this is the
    # exact verdict that reset this host in a loop when the probe itself was the slow thing. A real
    # dispatch hang comes back through the operation timeout as a non-zero exit above, so nothing
    # is lost by refusing to guess here.
    137) tail_to_last "$DOUT"
         log "dispatch probe last output: $(grep -vE '^\s*$' "$DOUT" | sed -n '$p' | cut -c1-160)"
         cannot_check "dispatch probe hit its ${DTMO}s cap without a verdict (typical is 14s)" ;;
    *)   reason="$(grep -m1 -oE 'Timed out.*|TT_THROW.*|what\(\):.*|terminate called.*' "$DOUT")"
         grep -vE '^\s*$' "$DOUT" | sed -n '1,40p' | sed 's/^/fabric-check|  /' >&2
         tail_to_last "$DOUT"
         log "the bus and the links are fine and the mesh still cannot run a program -> UNHEALTHY, the broker will reset"
         log "dispatch FAILED on a green fabric (rc=$drc)${reason:+: $reason}"
         exit 1 ;;
esac
