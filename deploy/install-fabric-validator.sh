#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Build the fabric validator from the pinned tt-metal commit (deploy/fabric-validator.pin)
# and publish it under /opt, so every host validates its fabric with identical code.
#
# Idempotent and SHA-keyed: each pin gets its own prefix (and a second slot beside it for a
# repair under a live `current`), and `current` flips to it only once the build succeeds. Re-running for an already-built pin is a no-op, so the reconcile
# timer can call this freely.
#
# Runs OUT OF BAND (a systemd oneshot), never inside the health gate or the auto-updater:
# a tt-metal build takes tens of minutes, and a health check that might block for tens of
# minutes is not a health check. Until a build lands, the fabric check reports CANNOT CHECK
# and the broker simply does not use it — it never guesses.
#
# The whole checkout+build tree is kept, not just the binary: run_cluster_validation has an
# RPATH into the build tree (libtt_metal, libtt-umd, libtt_stl, libtracy) and resolves its
# kernels from the runtime root, so a lone binary is useless.
#
# Progress and verdicts go through log.sh as `fabric-validator` (spec 06 I10): the helper is
# staged beside this script under $TTDEV_ROOT, so it is found by adjacency — this script sources
# only its pin and has no TTDEV_ROOT of its own. The build tool's own output is NOT relayed into
# fabric-validator.log: each cmake step is tee'd to build.log in the slot being built ($BUILD, and to
# stderr, so the journal copy is unchanged), and the stage lines below name that path.
set -euo pipefail
# Without the helper (a checkout run, or an upgrade that staged this script first) `log` degrades
# to stderr only, the contract log.sh itself keeps for an unwritable log dir.
. "$(dirname "$(readlink -f "$0")")/log.sh" 2>/dev/null \
    || log() { printf '%s | %s | %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "${TTDEV_LOG_NAME:-unnamed}" "$*" >&2 || true; }
TTDEV_LOG_NAME=fabric-validator

# `<pin> --key` prints the pin's prefix name (see PREFIX below) and builds nothing.
PIN="${1:-/opt/tt-device-broker/fabric-validator.pin}"
[ -r "$PIN" ] || { log "no pin at $PIN"; exit 1; }
# shellcheck disable=SC1090
. "$PIN"

ROOT="${TTDEV_VALIDATOR_ROOT:-/opt/tt-device-broker/validator}"
SHA="${TTDEV_VALIDATOR_SHA:?pin is missing TTDEV_VALIDATOR_SHA}"
# The pin's patches, from the pin's own directory, applied in order to the checkout before the build.
# They are part of the build's identity: the prefix is keyed on the SHA plus a hash of the patches, so
# a changed patch builds a new prefix and flips to it exactly like a new pin does.
PATCHES=()
# Absolute, because `git -C "$BUILD" apply` resolves a relative name from the checkout.
PIN_DIR="$(cd -- "$(dirname -- "$PIN")" && pwd)"
for p in ${TTDEV_VALIDATOR_PATCHES:-}; do
    p="$PIN_DIR/$p"
    [ -r "$p" ] || { log "pinned patch $p missing"; exit 1; }
    PATCHES+=("$p")
done
KEY="$SHA"
[ "${#PATCHES[@]}" -eq 0 ] || KEY="$SHA-$(cat -- "${PATCHES[@]}" | sha256sum | cut -c1-12)"
if [ "${2:-}" = --key ]; then echo "$KEY"; exit 0; fi
PREFIX="$ROOT/$KEY"
CURRENT="$ROOT/current"
LOCK="$ROOT/.build.lock"

mkdir -p "$ROOT"
# One build at a time. Two concurrent tt-metal builds in the same tree corrupt it, and the
# reconcile timer can fire again long before a build finishes. The lock is PRIVATE (0600) on
# purpose, for the broker's local-reset lock's reason (spec 06 I9): flock takes LOCK_EX on a
# read-only descriptor, so a 0644 lock in a 0755 directory would let any local user hold it and
# stall every build behind "another build is already running". Created at mode and re-asserted
# every run, which also brings a lock an earlier run left at the umask's mode to 0600 (spec 08).
[ -e "$LOCK" ] || install -m 0600 /dev/null "$LOCK"
chmod 0600 "$LOCK"
exec 9>"$LOCK"
if ! flock -n 9; then
    log "another build is already running; nothing to do"
    exit 0
fi

# Modes first, before the early exit below: the unit's UMask=0022 covers only what THIS run
# creates, and a prefix an earlier run made under a strict umask is 0700 with its transcript
# unreachable inside — and nothing below runs for a pin that is already built (spec 08).
# Every pin's prefix, not only the current one: an older transcript is still the record of how
# that build went, and nothing else ever revisits it.
chmod 0755 "$ROOT"
for prefix in "$ROOT"/*/; do
    [ -d "$prefix" ] || continue
    chmod 0755 "$prefix"
    if [ -f "$prefix/build.log" ]; then chmod 0644 "$prefix/build.log"; fi
done

# All three binaries, or a prefix is not done. Checking only the validator would leave a host that
# was built before the dispatch probe existed reporting "already built" forever, and the fabric
# check silently skipping its dispatch stage.
built() {
    [ -x "$1/$TTDEV_VALIDATOR_BIN_REL" ] && [ -x "$1/$TTDEV_DISPATCH_BIN_REL" ] \
        && [ -x "$1/$TTDEV_PREJOB_DISPATCH_BIN_REL" ]
}
# Flip atomically, so a health check can never observe a half-built prefix.
flip() { ln -sfn "$1" "$CURRENT.tmp" && mv -Tf "$CURRENT.tmp" "$CURRENT"; }

# Spec 08 I27: each pin has two build slots, $KEY and $KEY.build, and a build only ever runs in the
# slot `current` does not point at. A built tree cannot be renamed into place afterwards (its RPATH
# and runtime root are absolute paths into the tree it was built in), so the slot is its final path.
# A same-pin repair therefore builds beside the live prefix, never in it: the fabric checks keep the
# old tree until the flip.
# Slots are told apart by resolved path, not by the link's text: a root spelled differently from
# the run that wrote `current` (a trailing slash, a symlinked parent) must still find the live slot,
# or the build below would land in it. LIVE keeps this run's spelling for the comparisons below.
LIVE=""
CUR_REAL=""
if [ -e "$CURRENT" ]; then
    CUR_REAL="$(readlink -f -- "$CURRENT")" || CUR_REAL=""
    [ -n "$CUR_REAL" ] || { log "cannot resolve live $CURRENT; refusing to build"; exit 1; }
    for slot in "$PREFIX" "$PREFIX.build"; do
        if [ "$CUR_REAL" = "$(readlink -m -- "$slot")" ]; then LIVE="$slot"; fi
    done
fi
for slot in ${LIVE:+"$LIVE"} "$PREFIX" "$PREFIX.build"; do
    built "$slot" || continue
    if [ "$slot" != "$LIVE" ]; then
        # Already built for this pin. Make sure `current` points at it (a previous run could
        # have died between the build and the symlink flip) and stop.
        flip "$slot"
    elif [ $(( $(date +%s) - $(stat -c %Y "$CURRENT") )) -ge 3600 ]; then
        # Live for an hour (the link's own mtime is its last flip), far longer than any fabric
        # check, so no check still runs from the other slot: a stale repair source, removed.
        for other in "$PREFIX" "$PREFIX.build"; do
            [ "$other" = "$slot" ] || rm -rf "$other"
        done
    fi
    log "pin $KEY already built at $slot"
    exit 0
done
if [ "$LIVE" = "$PREFIX" ]; then BUILD="$PREFIX.build"; else BUILD="$PREFIX"; fi
# The live tree must never be the build slot or inside it, whatever `current` points at.
BUILD_REAL="$(readlink -m -- "$BUILD")"
case "$CUR_REAL" in
    "$BUILD_REAL" | "$BUILD_REAL"/*)
        log "live $CURRENT resolves into build slot $BUILD; refusing to build over it"
        exit 1
        ;;
esac

log "building validator from tt-metal $SHA in $BUILD; cmake transcript in $BUILD/build.log (this takes tens of minutes; the fabric check reports CANNOT CHECK until it lands)"

if [ ! -d "$BUILD/.git" ]; then
    rm -rf "$BUILD"
    mkdir -p "$BUILD"
    git -C "$BUILD" init -q
    git -C "$BUILD" remote add origin "$TTDEV_VALIDATOR_REPO"
fi
# One transcript per attempt, at a stated mode (spec 08): the previous attempt's is kept as
# build.log.prev, the record of the failure this retry follows, and anything older is dropped, so
# the hourly retry of a pin that keeps failing cannot fill the disk.
[ ! -e "$BUILD/build.log" ] || mv -f "$BUILD/build.log" "$BUILD/build.log.prev"
install -m 0644 /dev/null "$BUILD/build.log"
git -C "$BUILD" fetch -q --depth 1 origin "$SHA"
# -f: a retry finds the previous attempt's patches applied, and they must apply to a clean tree.
git -C "$BUILD" checkout -q -f --detach FETCH_HEAD
git -C "$BUILD" submodule update -q --init --recursive --depth 1
for p in ${PATCHES[@]+"${PATCHES[@]}"}; do
    git -C "$BUILD" apply "$p" || { log "pinned patch $p does not apply to tt-metal $SHA; not building"; exit 1; }
    log "applied $p"
done

TOOLCHAIN="$BUILD/${TTDEV_VALIDATOR_TOOLCHAIN:?pin is missing TTDEV_VALIDATOR_TOOLCHAIN}"
[ -f "$TOOLCHAIN" ] || { log "toolchain '$TOOLCHAIN' not in the checkout"; exit 1; }

# Fail on a missing compiler here, with its name, rather than letting cmake fall back
# to the distro default (GCC-11 on these hosts) and die 30 lines into a configure log.
for c in $(sed -n 's/^set(CMAKE_C\(XX\)\?_COMPILER \([^ ]*\).*/\2/p' "$TOOLCHAIN"); do
    command -v "$c" >/dev/null || { log "toolchain needs '$c', which is not on this host"; exit 1; }
done

# Configure from scratch, and that means the dependency cache too, not just the build
# dir. A configure that is interrupted mid-download (a timeout, a reboot, an OOM kill)
# leaves a CPM package half-populated; the next run then finds it "cached", adds the
# truncated source, and fails with a target that does not exist — reproducibly, forever,
# because nothing ever re-downloads it. Likewise a failed configure leaves a CMakeCache
# pinning the compiler it chose. Both caches are worthless without a build to show for
# them, so a retry starts clean.
rm -rf "$BUILD/build" "$BUILD/.cpmcache"
# Ninja, not the cmake default. tt-metal builds its HALs as object libraries, and the
# Unix Makefiles generator does not emit rules for their objects when you ask for one
# target rather than `all` — the build dies on "No rule to make target wh_hal.cpp.o".
# Ninja is what tt-metal's own build uses, and it gets this right.
command -v ninja >/dev/null || { log "ninja is required to build tt-metal and is not installed"; exit 1; }
# BUILD_PROGRAMMING_EXAMPLES is off by default and the dispatch probe is one of them: without it
# the target is not configured at all and the build fails with "no rule to make target".
# Both cmake steps: the transcript lands in $BUILD/build.log and every line still reaches stderr
# (hence the journal). `set -o pipefail` above keeps a failing cmake the pipeline's failure, so
# `set -e` still aborts the build on it; tee's own exit never masks it.
cmake -S "$BUILD" -B "$BUILD/build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DBUILD_PROGRAMMING_EXAMPLES=ON \
    -DCMAKE_BUILD_TYPE=Release 2>&1 | tee -a "$BUILD/build.log" >&2

# tt-metal's hw_toolchain writes its linker scripts into the SOURCE tree, but for the
# wormhole/erisc scripts it creates the output directory with a path relative to the
# build tree — so the directory never appears where the compiler writes, and a clean
# checkout dies with "unable to open output file .../erisc-b0-kernel.ld". A tree that
# has been built before already has the directory and never sees this, which is why it
# reproduces only here. Create what the build is about to write into.
archs="$(sed -n '/^set(ARCHS/,/^)/p' "$BUILD/tt_metal/hw/CMakeLists.txt" | sed -n 's/^[[:space:]]\{1,\}\([a-z0-9_]\{1,\}\)[[:space:]]*$/\1/p')"
[ -n "$archs" ] || { log "could not read ARCHS from tt_metal/hw/CMakeLists.txt"; exit 1; }
for arch in $archs; do
    mkdir -p "$BUILD/runtime/hw/toolchain/$arch"
done
log "prepared runtime/hw/toolchain for: ${archs//$'\n'/ }"

# Named targets only — a full tt-metal build is far more than we need.
cmake --build "$BUILD/build" \
    --target "$TTDEV_VALIDATOR_TARGET" "$TTDEV_DISPATCH_TARGET" "$TTDEV_PREJOB_DISPATCH_TARGET" \
    -j "$(nproc)" 2>&1 | tee -a "$BUILD/build.log" >&2

for rel in "$TTDEV_VALIDATOR_BIN_REL" "$TTDEV_DISPATCH_BIN_REL" "$TTDEV_PREJOB_DISPATCH_BIN_REL"; do
    [ -x "$BUILD/$rel" ] || { log "build finished but $rel is missing (transcript: $BUILD/build.log)"; exit 1; }
done

flip "$BUILD"
log "validator ready: $CURRENT/$TTDEV_VALIDATOR_BIN_REL (tt-metal $KEY)"
