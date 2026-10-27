# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared shell-script assertions for the test_deploy_* fail-closed suites."""

from pathlib import Path


def _statements(script: Path) -> list[str]:
    """Logical shell statements: backslash-continued source lines joined into one string,
    so an `... || { ...; exit 1; }` spread over two lines reads as a single unit."""
    out: list[str] = []
    buf = ""
    for line in script.read_text().splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _one(script: Path, *needles: str) -> str:
    """The single logical statement containing every needle."""
    hits = [s for s in _statements(script) if all(n in s for n in needles)]
    assert len(hits) == 1, f"expected exactly one statement matching {needles} in {script.name}, got {hits}"
    return hits[0]


# Shell stand-ins for the lap simulations, which run the autoupdate script's own lines hundreds of
# times per test. Each saves a process per call and changes nothing a lap can see:
# - mv: the script writes a small file whole as `echo ... > "$f.$$" && mv -f "$f.$$" "$f"` on
#   every lap or poll. This writes the same bytes to "$f" with builtins; "$f.$$" stays behind,
#   which the script never reads (its exit trap removes the hold's). Any other mv, or a target
#   that is a link or not a plain file, is the real one. The tests that pin the rename itself
#   (no half-written hold, no temporary left) define their own mv over this one.
# - rm: `rm -f` of paths none of which exist does nothing, as rm would; otherwise the real one.
LAP_STAND_INS = r"""mv() {
    if [ "$#" -eq 3 ] && [ "$1" = -f ] && [ "$2" = "$3.$$" ] && [ -f "$2" ] && [ ! -L "$2" ] \
        && { [ ! -e "$3" ] && [ ! -L "$3" ] || { [ -f "$3" ] && [ ! -L "$3" ]; }; }; then
        local _mv_b=""
        IFS= read -r -d '' _mv_b < "$2"
        printf '%s' "$_mv_b" > "$3"
    else
        command mv "$@"
    fi
}
rm() {
    if [ "${1-}" = -f ]; then
        local _rm_p
        for _rm_p in "${@:2}"; do
            if [ -e "$_rm_p" ] || [ -L "$_rm_p" ]; then
                command rm "$@"
                return
            fi
        done
        return 0
    fi
    command rm "$@"
}
"""
