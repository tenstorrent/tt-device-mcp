# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fabric-check wrapper's dispatch stage, whole mesh (default) and tray by tray.

``TTDEV_FABRIC_STAGGER=tray`` runs the dispatch probe once per PCI host bridge with only that
bridge's chips visible, one after another, so fewer chips start a program at once. It must still
give every chip a program and fail on any one of them, and the link stage before it is the same in
both modes. Runs the wrapper against a stand-in validator, probe and PCI tree: no device is opened.
"""

import subprocess
from pathlib import Path

import pytest

from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.health.monitors.fabric import names_dispatch_cap, names_dispatch_failure

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-fabric-check.sh"


def _pci_tree(tmp_path, per_bridge):
    """A sysfs stand-in: ``per_bridge`` Tenstorrent functions under each host bridge (pci0000:00,
    pci0000:40, ...), linked from a bus directory as /sys/bus/pci/devices is, plus one foreign
    function. Returns the bus directory and every Tenstorrent BDF by bridge."""
    devices = tmp_path / "sys-devices"
    bus = tmp_path / "pci-bus"
    bus.mkdir()
    bdfs = {}
    for b, count in enumerate(per_bridge):
        root = f"pci0000:{b * 0x40:02x}"
        bdfs[root] = []
        for i in range(count):
            bdf = f"0000:{b * 0x40 + i + 1:02x}:00.0"
            fn = devices / root / f"0000:{b * 0x40:02x}:01.{i}" / bdf
            fn.mkdir(parents=True)
            (fn / "vendor").write_text("0x1e52\n")
            (bus / bdf).symlink_to(fn)
            bdfs[root].append(bdf)
    other = devices / "pci0000:00" / "0000:ff:00.0"
    other.mkdir(parents=True)
    (other / "vendor").write_text("0x8086\n")
    (bus / "0000:ff:00.0").symlink_to(other)
    return bus, bdfs


def _run(tmp_path, bus, validator_rc=0, fail_on="", probe_rc=1, validator_out="validator-ran", **extra_env):
    """The wrapper with a validator that exits ``validator_rc`` and a dispatch probe that records the
    TT_VISIBLE_DEVICES of each call and exits ``probe_rc`` when that list contains ``fail_on``."""
    root = tmp_path / "root"
    root.mkdir()
    fake = tmp_path / "run_cluster_validation"
    fake.write_text(f"#!/bin/sh\necho '{validator_out}'\nexit {validator_rc}\n")
    fake.chmod(0o755)
    calls = tmp_path / "dispatch-calls"
    probe = tmp_path / "distributed_program_dispatch"
    probe.write_text(
        "#!/bin/bash\n"
        f'echo "${{TT_VISIBLE_DEVICES-UNSET}}" >>"{calls}"\n'
        'echo "probe opened ${TT_VISIBLE_DEVICES-the whole mesh}"\n'
        f'if [ -n "{fail_on}" ] && [[ "${{TT_VISIBLE_DEVICES-}}" == *"{fail_on}"* ]]; then\n'
        f'  [ {probe_rc} -eq 137 ] && kill -9 $$\n'
        "  echo \"  what():  TT_THROW: chip did not run the program\"\n"
        f"  exit {probe_rc}\n"
        "fi\n"
        "exit 0\n"
    )
    probe.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TTDEV_FABRIC_BIN": str(fake),
        "TTDEV_FABRIC_RUNTIME_ROOT": str(root),
        "TTDEV_FABRIC_CACHE": str(tmp_path / "cache"),
        "TTDEV_DISPATCH_BIN": str(probe),
        "TTDEV_PCI_DEVICES_DIR": str(bus),
        "TTDEV_HUGEPAGES_1G_NR": str(tmp_path / "no-pool"),
        **extra_env,
    }
    r = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    seen = calls.read_text().splitlines() if calls.exists() else []
    return r, seen


@pytest.mark.parametrize("mode", [None, "", "0", "off"])
def test_default_dispatches_once_to_the_whole_mesh_with_the_env_untouched(tmp_path, mode):
    bus, _ = _pci_tree(tmp_path, [8, 8, 8, 8])
    extra = {} if mode is None else {"TTDEV_FABRIC_STAGGER": mode}
    r, seen = _run(tmp_path, bus, **extra)
    assert r.returncode == 0, r.stderr
    assert seen == ["UNSET"]
    assert "dispatch OK: every chip ran a program" in r.stderr
    assert "one at a time" not in r.stderr


def test_tray_mode_gives_every_chip_a_program_one_bridge_at_a_time(tmp_path):
    bus, bdfs = _pci_tree(tmp_path, [8, 8, 8, 8])
    r, seen = _run(tmp_path, bus, TTDEV_FABRIC_STAGGER="tray")
    assert r.returncode == 0, r.stderr
    assert seen == [",".join(bdfs[root]) for root in sorted(bdfs)]
    covered = [bdf for call in seen for bdf in call.split(",")]
    assert sorted(covered) == sorted(b for group in bdfs.values() for b in group)
    assert len(covered) == len(set(covered)) == 32
    assert "0000:ff:00.0" not in covered
    assert "validator-ran" in r.stderr, "the link stage runs before the dispatch in both modes"
    assert "dispatch OK: every chip ran a program (4 trays one at a time)" in r.stderr


def test_tray_mode_fails_on_one_trays_dispatch_and_names_it(tmp_path):
    bus, bdfs = _pci_tree(tmp_path, [8, 8, 8, 8])
    bad = sorted(bdfs)[1]
    r, seen = _run(tmp_path, bus, fail_on=bdfs[bad][3], TTDEV_FABRIC_STAGGER="tray")
    assert r.returncode == 1, r.stderr
    assert len(seen) == 2, "stops at the first tray that fails"
    assert names_dispatch_failure(r.stderr)
    assert f"dispatch FAILED on a green fabric on tray {bad} (rc=1): what():  TT_THROW" in r.stderr


def test_tray_mode_cap_is_cannot_check_and_names_the_tray(tmp_path):
    bus, bdfs = _pci_tree(tmp_path, [8, 8, 8, 8])
    bad = sorted(bdfs)[2]
    r, seen = _run(tmp_path, bus, fail_on=bdfs[bad][0], probe_rc=137, TTDEV_FABRIC_STAGGER="tray")
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    assert len(seen) == 3
    assert names_dispatch_cap(r.stderr)
    assert f"without a verdict on tray {bad}" in r.stderr


@pytest.mark.parametrize("split", [[8, 8, 8, 7], [8], [6, 6]], ids=["chip-missing", "one-bridge", "not-8"])
def test_tray_mode_that_cannot_split_evenly_dispatches_to_the_whole_mesh(tmp_path, split):
    bus, _ = _pci_tree(tmp_path, split)
    r, seen = _run(tmp_path, bus, TTDEV_FABRIC_STAGGER="tray")
    assert r.returncode == 0, r.stderr
    assert seen == ["UNSET"]
    assert "staggered dispatch needs >= 2 PCI host bridges of 8 chips each" in r.stderr


def test_tray_mode_chips_per_bridge_is_configurable(tmp_path):
    bus, bdfs = _pci_tree(tmp_path, [4, 4])
    r, seen = _run(tmp_path, bus, TTDEV_FABRIC_STAGGER="tray", TTDEV_FABRIC_STAGGER_CHIPS="4")
    assert r.returncode == 0, r.stderr
    assert seen == [",".join(bdfs[root]) for root in sorted(bdfs)]


def test_an_unknown_stagger_value_dispatches_to_the_whole_mesh(tmp_path):
    bus, _ = _pci_tree(tmp_path, [8, 8, 8, 8])
    r, seen = _run(tmp_path, bus, TTDEV_FABRIC_STAGGER="chip")
    assert r.returncode == 0, r.stderr
    assert seen == ["UNSET"]
    assert "TTDEV_FABRIC_STAGGER='chip' is not 'tray' or off" in r.stderr


@pytest.mark.parametrize("mode", ["off", "tray"])
def test_links_missing_fail_in_both_modes_without_a_dispatch(tmp_path, mode):
    """The links are measured with the whole mesh open in both modes: links still missing after the
    validator's retrains are the verdict, and no tray-by-tray dispatch turns them into a pass."""
    bus, _ = _pci_tree(tmp_path, [8, 8, 8, 8])
    out = "Encountered unrecoverable state. Please check the system and try again."
    r, seen = _run(tmp_path, bus, validator_rc=134, validator_out=out, TTDEV_FABRIC_STAGGER=mode)
    assert r.returncode not in (0, FABRIC_CHECK_CANNOT_CHECK_RC), r.stderr
    assert "fabric UNHEALTHY" in r.stderr
    assert seen == []
