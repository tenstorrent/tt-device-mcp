# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker installer warns when the host needs a 1G hugepage pool and nothing sets one up.

A function behind no IOMMU or an identity one needs one 1G page. A fresh install on such a host with
no pool and no tenstorrent-hugepages.service comes up with a startup and fabric check that can only
report cannot-check, and the device stays held. The preflight names the missing pool and the page
count, by the same rule the broker's startup wait and the fabric-check wrapper apply. Sysfs stand-ins
only: no device is opened.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.deploy_helpers import _statements
from tests.test_fabric_check_reason import _pci_with_pool, _run_wrapper
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.health.monitors import hostpci, pci

# Fork-heavy deploy-script laps: one xdist worker runs them all (conftest: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_INSTALLER = Path(__file__).resolve().parent.parent / "deploy" / "install-tt-device-broker.sh"


def _host(tmp_path, monkeypatch, iommu_types, pages, *, setup_unit=False):
    """Point hostpci at a stand-in bus and pool (``pages`` "" = unreadable) and a unit dir that holds
    tenstorrent-hugepages.service only when ``setup_unit``."""
    env = _pci_with_pool(tmp_path, iommu_types, pages)
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", Path(env["TTDEV_PCI_DEVICES_DIR"]))
    monkeypatch.setattr(hostpci, "HUGEPAGES_1G_NR", Path(env["TTDEV_HUGEPAGES_1G_NR"]))
    units = tmp_path / "units"
    units.mkdir()
    if setup_unit:
        (units / hostpci.HUGEPAGES_SETUP_UNIT).write_text("[Unit]\n")
    monkeypatch.setattr(hostpci, "SYSTEMD_UNIT_DIRS", (tmp_path / "absent-units", units))
    return env


# --- the preflight's verdict ------------------------------------------------------------------


def test_a_translating_iommu_needs_no_page_and_gets_no_warning(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, ["DMA-FQ"] * 32, 0)
    assert hostpci.hugepages_install_warning() is None
    assert hostpci.hugepages_shortfall() is None, "nothing to wait for either"


def test_an_identity_iommu_with_no_pool_warns_with_the_page_count(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, ["identity"] * 8, 0)
    warn = hostpci.hugepages_install_warning()
    assert warn is not None
    assert "1G hugepage pool holds 0" in warn
    assert "(8 pages)" in warn and "at least 8 1G pages" in warn
    assert hostpci.HUGEPAGES_SETUP_UNIT in warn


def test_a_function_with_no_iommu_group_needs_a_page(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, [None, None, "DMA-FQ"], 0)
    warn = hostpci.hugepages_install_warning()
    assert warn is not None and "(2 pages)" in warn


def test_a_short_pool_with_nothing_to_fill_it_warns(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, ["identity"] * 8, 4)
    assert "pool holds 4" in hostpci.hugepages_install_warning()


def test_a_kernel_with_no_1g_pool_warns(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, ["identity"], "")
    assert "is unreadable" in hostpci.hugepages_install_warning()


def test_a_full_pool_gets_no_warning(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, ["identity"] * 8, 32)
    assert hostpci.hugepages_install_warning() is None


def test_an_installed_hugepages_service_gets_no_warning(tmp_path, monkeypatch):
    """Boot fills the pool late; the broker waits for the count itself (spec 03 I33)."""
    _host(tmp_path, monkeypatch, ["identity"] * 8, 0, setup_unit=True)
    assert hostpci.hugepages_install_warning() is None


def test_the_vendor_unit_dirs_are_searched():
    dirs = {str(d) for d in hostpci.SYSTEMD_UNIT_DIRS}
    assert {"/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"} <= dirs


# --- one rule: the preflight, the broker's wait and the fabric-check wrapper agree ---------------

_RULE_CASES = [
    (["identity"] * 4, 0),
    (["identity"] * 4, 3),
    (["identity"] * 4, 4),
    ([None, None], 1),
    (["DMA-FQ", "DMA"], 0),
    (["DMA-FQ", "identity", None], 1),
    (["DMA-FQ", "identity", None], 2),
    ([], 0),
]


@pytest.mark.parametrize("iommu_types,pages", _RULE_CASES)
def test_the_preflight_the_broker_and_the_wrapper_count_the_same_pages(tmp_path, monkeypatch, iommu_types, pages):
    env = _host(tmp_path, monkeypatch, iommu_types, pages)
    need = sum(1 for t in iommu_types if t is None or t == "identity")
    assert hostpci._functions_needing_hugepages() == need
    short = hostpci.hugepages_shortfall()
    assert short == ((pages, need) if pages < need else None)
    assert (hostpci.hugepages_install_warning() is not None) == (short is not None)
    r = _run_wrapper(tmp_path, "ran\n", 0, **env)
    if short is None:
        assert r.returncode == 0, r.stderr
    else:
        assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
        assert f"hugepage pool not ready: {pages}/{need} 1G pages allocated" in r.stderr


# --- the installer runs it, before the broker starts, and never fails or waits on it --------------


def _preflight_block() -> str:
    lines = _INSTALLER.read_text().splitlines()
    start = lines.index('echo "==> preflight: 1G hugepage pool"')
    end = lines.index("", start)
    return "\n".join(lines[start:end])


def _run_block(tmp_path, python_body: str):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\n{python_body}\n")
    py.chmod(0o700)
    script = f"set -euo pipefail\nNEW_VENV={venv}\n{_preflight_block()}\necho reached-end\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=dict(os.environ))


def test_the_installer_prints_the_warning_and_carries_on(tmp_path):
    r = _run_block(tmp_path, "echo 'pool missing: 8 pages'")
    assert r.returncode == 0, r.stderr
    assert "warn: pool missing: 8 pages" in r.stderr
    assert "reached-end" in r.stdout


def test_the_installer_is_silent_when_no_page_is_needed(tmp_path):
    r = _run_block(tmp_path, "echo ''")
    assert r.returncode == 0, r.stderr
    assert "warn:" not in r.stderr and "reached-end" in r.stdout


def test_a_preflight_that_cannot_run_is_loud_but_not_fatal(tmp_path):
    r = _run_block(tmp_path, "exit 1")
    assert r.returncode == 0, r.stderr
    assert "warn: the 1G hugepage preflight could not run" in r.stderr
    assert "reached-end" in r.stdout


def test_the_installer_calls_the_brokers_own_rule_from_the_new_venv(tmp_path):
    block = _preflight_block()
    assert '"$NEW_VENV/bin/python"' in block
    assert "hugepages_install_warning" in block
    for waits in ("sleep", "systemctl", "timeout", "while", "until"):
        assert waits not in block, f"the preflight must not wait ({waits})"
    # The snippet the installer hands python imports and runs on this interpreter too.
    code = block.split("-c '", 1)[1].split("')\"", 1)[0]
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_the_preflight_runs_before_the_broker_starts():
    stmts = [s for s in _statements(_INSTALLER) if not s.lstrip().startswith("#")]

    def at(needle: str) -> int:
        return next(i for i, s in enumerate(stmts) if needle in s)

    assert at("==> preflight: 1G hugepage pool") < at("apply-host-config.sh") < at("systemctl restart tt-device-broker")
    assert "tenstorrent-hugepages" not in "\n".join(stmts).replace("hugepages_install_warning", "")
