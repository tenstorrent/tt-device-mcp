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
set -euo pipefail

PIN="${1:-/opt/tt-device-broker/fabric-validator.pin}"
[ -r "$PIN" ] || { echo "install-fabric-validator: no pin at $PIN" >&2; exit 1; }
# shellcheck disable=SC1090
. "$PIN"

ROOT="${TTDEV_VALIDATOR_ROOT:-/opt/tt-device-broker/validator}"
SHA="${TTDEV_VALIDATOR_SHA:?pin is missing TTDEV_VALIDATOR_SHA}"
PREFIX="$ROOT/$SHA"
CURRENT="$ROOT/current"
LOCK="$ROOT/.build.lock"

log() { echo "fabric-validator: $*" >&2; }

mkdir -p "$ROOT"
# One build at a time. Two concurrent tt-metal builds in the same tree corrupt it, and the
# reconcile timer can fire again long before a build finishes.
exec 9>"$LOCK"
if ! flock -n 9; then
    log "another build is already running; nothing to do"
    exit 0
fi

# All three binaries, or a prefix is not done. Checking only the validator would leave a host that
# was built before the dispatch probe existed reporting "already built" forever, and the fabric
# check silently skipping its dispatch stage.
built() {
    [ -x "$1/$TTDEV_VALIDATOR_BIN_REL" ] && [ -x "$1/$TTDEV_DISPATCH_BIN_REL" ] \
        && [ -x "$1/$TTDEV_PREJOB_DISPATCH_BIN_REL" ]
}
# Flip atomically, so a health check can never observe a half-built prefix.
flip() { ln -sfn "$1" "$CURRENT.tmp" && mv -Tf "$CURRENT.tmp" "$CURRENT"; }

# Spec 08 I27: each pin has two build slots, $SHA and $SHA.build, and a build only ever runs in the
# slot `current` does not point at. A built tree cannot be renamed into place afterwards (its RPATH
# and runtime root are absolute paths into the tree it was built in), so the slot is its final path.
# A same-pin repair therefore builds beside the live prefix, never in it: the fabric checks keep the
# old tree until the flip.
LIVE="$(readlink "$CURRENT" 2>/dev/null || true)"
case "$LIVE" in "$PREFIX"|"$PREFIX.build") ;; *) LIVE="" ;; esac
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
    log "pin $SHA already built at $slot"
    exit 0
done
if [ "$LIVE" = "$PREFIX" ]; then BUILD="$PREFIX.build"; else BUILD="$PREFIX"; fi

log "building validator from tt-metal $SHA in $BUILD (this takes tens of minutes; the fabric check reports CANNOT CHECK until it lands)"

if [ ! -d "$BUILD/.git" ]; then
    rm -rf "$BUILD"
    mkdir -p "$BUILD"
    git -C "$BUILD" init -q
    git -C "$BUILD" remote add origin "$TTDEV_VALIDATOR_REPO"
fi
git -C "$BUILD" fetch -q --depth 1 origin "$SHA"
git -C "$BUILD" checkout -q --detach FETCH_HEAD
git -C "$BUILD" submodule update -q --init --recursive --depth 1

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
cmake -S "$BUILD" -B "$BUILD/build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DBUILD_PROGRAMMING_EXAMPLES=ON \
    -DCMAKE_BUILD_TYPE=Release >&2

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
log "prepared runtime/hw/toolchain for: $(echo $archs | tr '\n' ' ')"

# Named targets only — a full tt-metal build is far more than we need.
cmake --build "$BUILD/build" \
    --target "$TTDEV_VALIDATOR_TARGET" "$TTDEV_DISPATCH_TARGET" "$TTDEV_PREJOB_DISPATCH_TARGET" \
    -j "$(nproc)" >&2

for rel in "$TTDEV_VALIDATOR_BIN_REL" "$TTDEV_DISPATCH_BIN_REL" "$TTDEV_PREJOB_DISPATCH_BIN_REL"; do
    [ -x "$BUILD/$rel" ] || { log "build finished but $rel is missing"; exit 1; }
done

flip "$BUILD"
log "validator ready: $CURRENT/$TTDEV_VALIDATOR_BIN_REL (tt-metal $SHA)"
