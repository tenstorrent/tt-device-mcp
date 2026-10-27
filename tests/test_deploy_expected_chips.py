# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I28: a fresh install knows the box's expected chip count from what the machine IS.

Without it the broker's bar is a high-water mark of chips seen, so a tray already off the bus at
first start reads as a smaller box and is never detected. The count comes from the board type of
any chip that answers the installer's snapshot, the DMI product, or the galaxy reset mode, never
from how many chips answer. The operator's TTDEV_EXPECTED_CHIPS and a value the unit already has
win. No source that can tell leaves the key unset and says so loudly. Everything here runs on
stubs: no device, no root, no host path (the DMI file is sandboxed in every case).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.evidence import health_dir
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.monitors.heartbeat import heartbeat_verdict

# Fork-heavy deploy-script laps: one xdist worker runs them all (pyproject: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_REPO = Path(__file__).resolve().parent.parent
_DERIVE = _REPO / "deploy" / "tt-device-expected-chips.sh"
_APPLY = _REPO / "deploy" / "apply-host-config.sh"
_INSTALLER = _REPO / "deploy" / "install-tt-device-broker.sh"
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _derive(tmp_path, *, boards=None, reset_mode=None, dmi=None):
    dmi_file = tmp_path / "product_name"
    if dmi is not None:
        dmi_file.write_text(dmi + "\n")
    env = {"PATH": _PATH, "TTDEV_DMI_PRODUCT_FILE": str(dmi_file)}
    if boards is not None:
        env["TTDEV_BOARD_TYPES"] = "\n".join(boards)
    if reset_mode is not None:
        env["TTDEV_RESET_MODE"] = reset_mode
    r = subprocess.run(["bash", str(_DERIVE)], env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip()


def _render(tmp_path, defaults: dict, *, env_extra=None, unit_text=None, dmi=None):
    conf = tmp_path / "default"
    conf.write_text("".join(f"{k}={v}\n" for k, v in defaults.items()))
    unit = tmp_path / "current.service"
    if unit_text is not None:
        unit.write_text(unit_text)
    dmi_file = tmp_path / "product_name"
    if dmi is not None:
        dmi_file.write_text(dmi + "\n")
    env = {
        "PATH": _PATH,
        "TTDEV_ETC_DEFAULT": str(conf),
        "TTDEV_UNIT_PATH": str(unit),
        "TTDEV_DMI_PRODUCT_FILE": str(dmi_file),
    }
    env.update(env_extra or {})
    r = subprocess.run(
        ["bash", str(_APPLY), "--print-unit", str(_REPO)], env=env, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"render failed: {r.returncode}\n{r.stderr}"
    return r.stdout, r.stderr


_CARD_HOST = {"TTDEV_VENV": "/opt/tt-device-broker/venv", "TTDEV_ROOT": "/opt/tt-device-broker"}
_LINE = "Environment=TT_DEVICE_MCP_EXPECTED_CHIPS="


# ------------------------------------------------------------------ the mapping


@pytest.mark.parametrize(
    "boards",
    [
        ["tt-galaxy-bh"] * 32,
        ["tt-galaxy-wh L", "tt-galaxy-wh R"],
        [" TT-GALAXY-BH "],
        ["N/A", "", "tt-galaxy-bh"],  # an unidentified chip does not contradict an identified one
    ],
)
def test_a_galaxy_board_type_maps_to_32(tmp_path, boards):
    rc, out = _derive(tmp_path, boards=boards)
    assert rc == 0, out
    count, source = out.split("\t")
    assert count == "32" and source.startswith("board type tt-galaxy-")


@pytest.mark.parametrize("present", [1, 8, 16, 24, 31, 32])
def test_the_count_never_follows_how_many_chips_answered(tmp_path, present):
    # The whole point: one tray (or three) already off the bus must not lower the bar.
    assert _derive(tmp_path, boards=["tt-galaxy-bh"] * present) == (0, "32\tboard type tt-galaxy-bh")


def test_the_board_vote_starts_no_process_per_chip(tmp_path):
    """Every chip's board type is normalised by shell builtins: 32 chips start no tr or sed, only
    the one DMI read does (head | tr)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for tool in ("tr", "sed", "head"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho {tool} >> {calls}\nexec /usr/bin/{tool} "$@"\n')
        (bin_dir / tool).chmod(0o755)
    dmi_file = tmp_path / "product_name"
    dmi_file.write_text("Tenstorrent Galaxy\n")
    env = {
        "PATH": f"{bin_dir}:{_PATH}",
        "TTDEV_DMI_PRODUCT_FILE": str(dmi_file),
        "TTDEV_BOARD_TYPES": "\n".join(["tt-galaxy-bh L", " TT-GALAXY-BH R "] * 16),
    }
    r = subprocess.run(["bash", str(_DERIVE)], env=env, capture_output=True, text=True, timeout=30)
    assert (r.returncode, r.stdout.strip()) == (0, "32\tboard type tt-galaxy-bh"), r.stderr
    assert sorted(calls.read_text().split()) == ["head", "tr"]


def test_the_dmi_product_decides_when_no_chip_answers(tmp_path):
    # Every chip off the bus at install: tt-smi gives nothing, the host firmware still knows.
    rc, out = _derive(tmp_path, boards=[], dmi="Galaxy Blackhole 6U Server")
    assert (rc, out) == (0, "32\tDMI product Galaxy Blackhole 6U Server")


def test_the_galaxy_reset_mode_decides_on_an_update(tmp_path):
    # Auto-update reads no device: the persisted reset mode (itself set from board_type) still tells.
    assert _derive(tmp_path, reset_mode="galaxy") == (0, "32\treset mode galaxy")


@pytest.mark.parametrize(
    "boards",
    [["n150"], ["n300 L", "n300 R"], ["p100a"], ["p150a"] * 8, ["p300c", "p300c"]],
)
def test_a_card_board_cannot_tell(tmp_path, boards):
    # A card's type says chips per card, not how many cards the box holds. Mapping n300 to 2 would
    # bypass the high-water mark on an 8-chip box of four n300 cards and hide a card drop there.
    rc, out = _derive(tmp_path, boards=boards)
    assert rc == 3 and "card board" in out


@pytest.mark.parametrize(
    "kw",
    [
        {"boards": ["tt-galaxy-bh", "n300 L"]},  # mixed types
        {"boards": ["some-new-board"]},  # unknown type
        {"boards": ["some-new-board"], "dmi": "Galaxy Blackhole 6U Server"},  # unknown type wins over DMI
        {"boards": ["p150a"], "reset_mode": "galaxy"},  # card board vs galaxy reset mode
        {"boards": ["p150a"], "dmi": "Galaxy Blackhole 6U Server"},  # card board vs galaxy DMI
        {},  # nothing at all
        {"dmi": "AS -4125GS-TNRT"},  # a generic server chassis says nothing
    ],
)
def test_no_source_that_can_tell_never_guesses(tmp_path, kw):
    rc, out = _derive(tmp_path, **kw)
    assert rc == 3 and out and not out[0].isdigit(), out


# ------------------------------------------------------------------ precedence in apply-host-config


def test_the_operator_value_wins_over_every_derived_source(tmp_path):
    unit, err = _render(
        tmp_path,
        dict(_CARD_HOST, TTDEV_RESET_MODE="galaxy"),
        env_extra={"TTDEV_EXPECTED_CHIPS": "24", "TTDEV_BOARD_TYPES": "tt-galaxy-bh"},
        unit_text=f"{_LINE}16\n",
        dmi="Galaxy Blackhole 6U Server",
    )
    assert f"{_LINE}24" in unit and f"{_LINE}32" not in unit and f"{_LINE}16" not in unit
    assert "expected chips 24 (source: TTDEV_EXPECTED_CHIPS" in err


def test_an_etc_default_value_wins_like_the_operators(tmp_path):
    unit, _ = _render(tmp_path, dict(_CARD_HOST, TTDEV_EXPECTED_CHIPS="8"), dmi="Galaxy Blackhole 6U Server")
    assert f"{_LINE}8\n" in unit


def test_the_existing_unit_value_wins_over_derivation(tmp_path):
    unit, err = _render(
        tmp_path,
        dict(_CARD_HOST, TTDEV_RESET_MODE="galaxy"),
        env_extra={"TTDEV_BOARD_TYPES": "tt-galaxy-bh"},
        unit_text=f"{_LINE}16\n",
    )
    assert f"{_LINE}16" in unit and f"{_LINE}32" not in unit
    assert "expected chips 16 (source: existing unit)" in err


def test_cannot_tell_leaves_it_unset_and_says_so_loudly(tmp_path):
    unit, err = _render(tmp_path, _CARD_HOST, env_extra={"TTDEV_BOARD_TYPES": "p150a\np150a"})
    assert "TT_DEVICE_MCP_EXPECTED_CHIPS" not in unit, "never guess: /health keeps the count unknown"
    assert "WARNING: expected chip count UNKNOWN" in err and "card board" in err
    assert "TTDEV_EXPECTED_CHIPS=<n>" in err, "the warning says how to set it"


def test_the_derived_value_is_persisted_like_any_resolved_knob(tmp_path):
    # ensure_default backfills the resolved value, so the next update reads it back from /etc/default
    # and never re-derives (or loses it on a box whose chips are all off the bus by then).
    lines = [line.strip() for line in _APPLY.read_text().splitlines()]
    derive = next(i for i, line in enumerate(lines) if "tt-device-expected-chips.sh" in line and "derived=" in line)
    persist = lines.index('ensure_default TTDEV_EXPECTED_CHIPS "$EXPECTED_CHIPS"')
    assert derive < persist


# ------------------------------------------------------------------ the fresh install (msg: "from the get go")


def _detect_block() -> str:
    """The installer's machine-detection step, verbatim: from its banner to the fabric-check step."""
    text = _INSTALLER.read_text()
    start = text.index('echo "==> detect machine type')
    return text[start : text.index("# Post-job fabric traffic check", start)]


def _fresh_install(tmp_path, smi_json, *, dmi=None, extra_env=None):
    """Run the installer's detection step against a stub tt-smi, then render the unit the way the
    installer's apply-host-config call does (fresh: no unit yet, /etc/default as the installer writes it)."""
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    payload = tmp_path / "smi.json"
    payload.write_text(smi_json)
    (venv / "tt-smi").write_text(f'#!/bin/bash\n[ "$1" = -s ] && cat "{payload}"\n')
    (venv / "tt-smi").chmod(0o755)
    os.symlink("/usr/bin/python3", venv / "python")
    body = (
        f'set -euo pipefail\nNEW_VENV="{venv.parent}"\n{_detect_block()}\n'
        + 'printf "%s" "$RESET_MODE" > "$OUT.mode"\nprintf "%s" "$TTDEV_BOARD_TYPES" > "$OUT"\n'
    )
    env = {"PATH": _PATH, "OUT": str(tmp_path / "boards")}
    env.update(extra_env or {})
    r = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    reset_mode = (tmp_path / "boards.mode").read_text()
    defaults = dict(_CARD_HOST, TTDEV_RESET_MODE=reset_mode)
    render_env = {"TTDEV_BOARD_TYPES": (tmp_path / "boards").read_text()}
    render_env.update(extra_env or {})
    return _render(tmp_path, defaults, env_extra=render_env, dmi=dmi)


def _smi(n, board="tt-galaxy-bh"):
    return json.dumps({"device_info": [{"board_info": {"board_type": board}} for _ in range(n)]})


def test_a_fresh_galaxy_install_writes_32_with_no_operator_input(tmp_path):
    unit, err = _fresh_install(tmp_path, _smi(32))
    assert f"{_LINE}32\n" in unit
    assert "Environment=TT_DEVICE_MCP_RESET_MODE=galaxy" in unit
    assert "expected chips 32 (source: board type tt-galaxy-bh)" in err


def test_a_fresh_galaxy_install_with_a_tray_already_missing_still_gets_32(tmp_path):
    unit, _ = _fresh_install(tmp_path, _smi(24))  # tray 2 off the bus at install time
    assert f"{_LINE}32\n" in unit


def test_a_fresh_galaxy_install_with_no_chip_answering_gets_32_from_dmi(tmp_path):
    unit, err = _fresh_install(tmp_path, "", dmi="Galaxy Blackhole 6U Server")
    assert f"{_LINE}32\n" in unit
    assert "source: DMI product" in err


def test_a_fresh_card_install_leaves_it_unset_and_warns(tmp_path):
    unit, err = _fresh_install(tmp_path, _smi(8, "p150a"), dmi="AS -4125GS-TNRT")
    assert "TT_DEVICE_MCP_EXPECTED_CHIPS" not in unit
    assert "expected chip count UNKNOWN" in err


def test_a_fresh_install_with_an_operator_count_keeps_it(tmp_path):
    unit, _ = _fresh_install(tmp_path, _smi(24), extra_env={"TTDEV_EXPECTED_CHIPS": "32"})
    assert f"{_LINE}32\n" in unit


# ------------------------------------------------------------------ goal: short is a failed check


def test_a_declared_32_is_the_bar_even_over_a_degraded_high_water_mark(monkeypatch):
    # The case the derived count exists for: the broker's first start saw 24 chips and wrote that as
    # its high-water mark. The declared count is the bar; the survivors are not.
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    (health_dir() / srv.health_monitor.CHIP_BASELINE_FILE).write_text(json.dumps({"chips": 24}))
    assert srv.health_monitor.expected(24) == 32


def test_fewer_chips_than_the_declared_count_fails_the_check(monkeypatch):
    for i in range(24):  # one tray of a 32-chip galaxy off the bus
        d = pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}"
        d.mkdir(exist_ok=True)
        (d / "tt_heartbeat").write_text("100\n")
    verdict, detail, _ = heartbeat_verdict(expected_count=32)
    assert verdict is Verdict.UNHEALTHY, "fewer chips than expected is a failed check, never a pass"
    assert "expected 32" in detail
