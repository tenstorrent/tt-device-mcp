# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""What this process can actually execute, measured rather than declared (spec 04 I17).

Every probe here answers "would this rung run if the ladder reached for it", never "does this
allocation belong to me". A rung that reports armed and then raises on EPERM at the top of the
ladder is the failure these exist to prevent: the refusal is the same either way, but discovering
it at boot means it is printed once, in the rung inventory, instead of during a wedge.
"""

import os

import pytest

from tt_device_mcp import privileges


@pytest.fixture(autouse=True)
def unprivileged(monkeypatch):
    """Measure live, against a host staged to its negatives.

    conftest latches a fully privileged record for the rest of the suite; these tests cover the
    probes themselves, so they clear it and drive the real syscalls."""
    monkeypatch.setattr(privileges, "_LATCHED", None)
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(privileges.shutil, "which", lambda _name: None)
    monkeypatch.setattr(privileges, "SYSTEMD_DIR", "/nonexistent")
    monkeypatch.setattr(privileges, "IPMI_DEVICE_NODES", ())


def test_root_is_euid_zero_not_the_login_user(monkeypatch):
    """euid, not uid: the broker's authority is what it is running as now."""
    assert privileges.is_root() is False
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    assert privileges.is_root() is True


def test_systemd_is_the_runtime_directory_not_a_binary_on_path(monkeypatch, tmp_path):
    """What sd_booted(3) reads. A restricted PATH still runs systemd, so testing for `systemctl`
    would drop the scoped-reset backend on a host that has it."""
    assert privileges.has_systemd() is False
    monkeypatch.setattr(privileges, "SYSTEMD_DIR", str(tmp_path))
    assert privileges.has_systemd() is True


def test_setpci_needs_the_binary_and_root(monkeypatch):
    """A Secondary Bus Reset writes the parent BRIDGE's config space, which is root-only. Without
    root the rung's first setpci read fails and the ladder retries a permission error it can never
    resolve, so having the binary is not having the rung."""
    monkeypatch.setattr(privileges.shutil, "which", lambda name: "/usr/bin/setpci" if name == "setpci" else None)
    assert privileges.can_setpci() is False, "the binary alone is not enough"

    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    assert privileges.can_setpci() is True

    monkeypatch.setattr(privileges.shutil, "which", lambda _name: None)
    assert privileges.can_setpci() is False, "root alone is not enough either"


def test_ipmi_needs_the_binary_and_a_node_it_can_open(monkeypatch, tmp_path):
    """`ipmitool raw` with no `-I lanplus` talks to the local BMC through /dev/ipmi*. A host with
    the binary and no node cannot cold-cycle, and I14 says armed means fireable."""
    monkeypatch.setattr(privileges.shutil, "which", lambda name: "/usr/bin/ipmitool" if name == "ipmitool" else None)
    assert privileges.can_ipmi() is False, "no IPMI node means the fire would raise"

    node = tmp_path / "ipmi0"
    node.touch()
    monkeypatch.setattr(privileges, "IPMI_DEVICE_NODES", (str(node),))
    assert privileges.can_ipmi() is True

    monkeypatch.setattr(privileges.shutil, "which", lambda _name: None)
    assert privileges.can_ipmi() is False, "a node without ipmitool is not the rung either"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions, so an unopenable node cannot be staged")
def test_an_unopenable_ipmi_node_is_not_a_reachable_bmc(monkeypatch, tmp_path):
    """The node existing is not the same as this process being allowed to use it — the ordinary
    case for a non-root daemon on a host whose /dev/ipmi0 is root-only."""
    monkeypatch.setattr(privileges.shutil, "which", lambda name: "/usr/bin/ipmitool" if name == "ipmitool" else None)
    node = tmp_path / "ipmi0"
    node.touch()
    node.chmod(0o000)
    monkeypatch.setattr(privileges, "IPMI_DEVICE_NODES", (str(node),))

    assert privileges.can_ipmi() is False


def test_the_snapshot_names_every_probe_for_the_boot_line(monkeypatch, tmp_path):
    """The boot line has to state all four, because a rung reported OFF is only readable if the
    privilege it lacked is named alongside it."""
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    monkeypatch.setattr(privileges, "SYSTEMD_DIR", str(tmp_path))

    snap = privileges.snapshot()

    assert snap == {
        "euid": 0,
        "root": True,
        "systemd": True,
        "setpci_bin": False,
        "ipmitool": False,
        "ipmi_node": False,
    }


def test_the_boot_line_reports_a_present_binary_as_present(monkeypatch):
    """setpci installed on a non-root daemon must not read as absent: folding `and root` into the
    bit sends the reader to install a package they already have. The derived capability is False;
    the fact it was derived from is not."""
    monkeypatch.setattr(privileges.shutil, "which", lambda name: "/usr/bin/setpci" if name == "setpci" else None)

    assert privileges.snapshot()["setpci_bin"] is True
    assert privileges.can_setpci() is False


def test_the_latch_holds_the_host_it_measured(monkeypatch, tmp_path):
    """A rung reached hours into a wedge must be the rung the boot line promised. Latched, a host
    that changes underneath the broker cannot make the two disagree."""
    monkeypatch.setattr(privileges, "SYSTEMD_DIR", str(tmp_path))
    privileges.latch()

    monkeypatch.setattr(privileges, "SYSTEMD_DIR", "/nonexistent")

    assert privileges.has_systemd() is True, "the latched record stands"
    assert privileges.snapshot()["systemd"] is True

    monkeypatch.setattr(privileges, "_LATCHED", None)
    assert privileges.has_systemd() is False, "unlatched, the probes measure live again"
