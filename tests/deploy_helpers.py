# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared shell-script assertions for the test_deploy_* fail-closed suites."""

import subprocess
from pathlib import Path

_APPLY_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "apply-host-config.sh"
_REPO = _APPLY_SCRIPT.parent.parent


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
# - au_cap: the script's capture seam, `au_cap v cmd args` for `v="$(cmd args)"`. The stand-ins for
#   systemd, pgrep and curl are shell functions, so each $(...) of one is a subshell, several on every
#   poll of a drain. This runs the command in the shell itself, its stdout through a file beside
#   $ROOT, and keeps what $(...) keeps: the output less its trailing newlines, and the exit status.
#   tests/test_deploy_autoupdate_forks.py holds it to the script's own au_cap.
LAP_STAND_INS = r"""au_cap() {
    local _cap_f="${ROOT%/*}/au-cap.${#FUNCNAME[@]}" _cap_o="" _cap_rc=0
    "${@:2}" > "$_cap_f" || _cap_rc=$?
    IFS= read -r -d '' _cap_o < "$_cap_f"
    while [ "${_cap_o%$'\n'}" != "$_cap_o" ]; do _cap_o="${_cap_o%$'\n'}"; done
    printf -v "$1" '%s' "$_cap_o"
    return "$_cap_rc"
}
mv() {
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


def _clock(text):
    """The real log.sh as `autoupdate`, then the script's seams (au_now, au_cap, au_time, au_root_uid)
    and its decision records (au_log) as it defines them, ahead of the source: the lap reads the real
    clock. Its lines land in TTDEV_LOG_DIR and on stderr; a lap that leaves it unset stops here
    rather than append to the host's /var/log."""
    i = text.index("au_now() {")
    helper = (
        ': "${TTDEV_LOG_DIR:?a lap sets TTDEV_LOG_DIR}"\n'
        f'. "{_REPO / "deploy" / "tt-device-log.sh"}"\nTTDEV_LOG_NAME=autoupdate\n'
    )
    return helper + text[i : text.index("\n}\n", text.index("au_log() {", i)) + 3]


def _render(tmp_path, defaults: dict, *, eth_cmd: str | None = None) -> str:
    """Render the unit from a sandboxed config, touching no host path. The unit-read
    fallback is pointed at a nonexistent file so the render is fully determined by
    `defaults` (+ eth_cmd), not by whatever unit this host happens to have installed."""
    conf = tmp_path / "default"
    conf.write_text("".join(f"{k}={v}\n" for k, v in defaults.items()))
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TTDEV_ETC_DEFAULT": str(conf),
        "TTDEV_UNIT_PATH": str(tmp_path / "absent.service"),
        # No host DMI product either: on a Galaxy host it would derive an expected chip count.
        "TTDEV_DMI_PRODUCT_FILE": str(tmp_path / "absent.product_name"),
        # Where the apply would log if its render seam stopped forcing stderr-only: a temp dir, never
        # /var/log on a root host. The seam itself is pinned by its own test.
        "TTDEV_LOG_DIR": str(tmp_path / "render-logs"),
    }
    if eth_cmd is not None:
        env["TTDEV_ETH_HEARTBEAT_CMD"] = eth_cmd
    r = subprocess.run(
        ["bash", str(_APPLY_SCRIPT), "--print-unit", str(_REPO)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"render failed: {r.returncode}\n{r.stderr}"
    return r.stdout


_GALAXY = {
    "TTDEV_VENV": "/opt/tt-device-broker/venv",
    "TTDEV_ROOT": "/opt/tt-device-broker",
    "TTDEV_RESET_MODE": "galaxy",
    "TTDEV_FABRIC_CHECK_CMD": "/opt/tt-device-broker/fabric-check.sh",
}
