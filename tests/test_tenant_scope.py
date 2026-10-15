# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Holders below MIN_TENANT_UID that are still tenants: broker-scoped job stragglers and the
operator's TT_DEVICE_MCP_TENANT_UIDS.

A job run under a service account (uid < 1000) was invisible to every holder check. Once it was
marked killed or finished, the queue no longer reported it running, yet its processes held the
device for seconds more while their scope wound down, and a reset or forced verify could fire under
them."""

import os
import pwd

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp import device_holders as dh
from tt_device_mcp.device_holders import DeviceHolder, HolderScan

SERVICE_UID = 996  # a system account: below MIN_TENANT_UID


def _stage_cgroup(root, pid, text):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(text)


@pytest.fixture
def scope_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(dh, "SCOPE_PROC_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _fresh_tenant_uid_cache():
    dh._clear_tenant_uid_cache()
    yield
    dh._clear_tenant_uid_cache()


# ------------------------------------------------------------------ scope detection


@pytest.mark.parametrize(
    "cgroup",
    [
        "0::/system.slice/ttdev-job-859.scope\n",  # cgroup v2
        "0::/system.slice/ttdev-exec-4100-3.scope\n",
        "12:pids:/system.slice/ttdev-job-12.scope\n1:name=systemd:/system.slice/ttdev-job-12.scope\n",  # v1
    ],
)
def test_a_process_in_a_broker_scope_is_scoped(scope_proc, cgroup):
    _stage_cgroup(scope_proc, 4242, cgroup)
    assert dh.in_broker_scope(4242) is True


@pytest.mark.parametrize(
    "cgroup",
    [
        "0::/user.slice/user-1000.slice/session-3.scope\n",
        "0::/system.slice/some-daemon.service\n",
        "0::/system.slice/ttdev-job-859.service\n",  # the prefix alone is not a scope
        "0::/system.slice/not-ttdev-job-1.scope\n",
    ],
)
def test_a_process_outside_a_broker_scope_is_not_scoped(scope_proc, cgroup):
    _stage_cgroup(scope_proc, 4242, cgroup)
    assert dh.in_broker_scope(4242) is False


def test_an_unreadable_cgroup_is_not_scoped(scope_proc):
    assert dh.in_broker_scope(4242) is False


def test_the_fd_walk_marks_a_scoped_holder(tmp_path, monkeypatch, scope_proc):
    fake_dev = tmp_path / "dev"
    fake_dev.mkdir()
    (fake_dev / "0").write_text("")
    monkeypatch.setattr(dh, "DEVICE_DIR", str(fake_dev))
    _stage_cgroup(scope_proc, os.getpid(), "0::/system.slice/ttdev-job-7.scope\n")

    fd = os.open(fake_dev / "0", os.O_RDONLY)
    try:
        scan = dh.enumerate_device_holders()
    finally:
        os.close(fd)
    (me,) = [h for h in scan.holders if h.pid == os.getpid()]
    assert me.scoped is True


def test_the_driver_record_marks_a_scoped_holder(tmp_path, monkeypatch, scope_proc):
    driver = tmp_path / "driver"
    (driver / "0").mkdir(parents=True)
    (driver / "0" / "pids").write_text("4242\n4343\n")
    monkeypatch.setattr(dh, "DRIVER_PROC_DIR", str(driver))
    monkeypatch.setattr(dh, "_proc_uid", lambda pid: SERVICE_UID)
    monkeypatch.setattr(dh, "_process_holds_device", lambda pid: True)
    _stage_cgroup(scope_proc, 4242, "0::/system.slice/ttdev-job-859.scope\n")
    _stage_cgroup(scope_proc, 4343, "0::/system.slice/some-daemon.service\n")

    scan = dh.enumerate_device_holders()
    assert {h.pid: h.scoped for h in scan.holders} == {4242: True, 4343: False}
    assert [h.pid for h in scan.foreign_holders(None)] == [4242]


def test_scoped_is_not_part_of_holder_identity():
    assert DeviceHolder(pid=1, uid=SERVICE_UID, scoped=True) == DeviceHolder(pid=1, uid=SERVICE_UID)


# ------------------------------------------------------------------ the tenant rule


def test_the_uid_floor_still_decides_unscoped_holders():
    assert dh.is_tenant(DeviceHolder(pid=1, uid=1000)) is True
    assert dh.is_tenant(DeviceHolder(pid=1, uid=SERVICE_UID)) is False
    assert dh.is_tenant(DeviceHolder(pid=1, uid=0)) is False


def test_a_scoped_holder_is_a_tenant_whatever_its_uid():
    assert dh.is_tenant(DeviceHolder(pid=1, uid=SERVICE_UID, scoped=True)) is True
    assert dh.is_tenant(DeviceHolder(pid=1, uid=0, scoped=True)) is True


def test_tenant_uids_env_names_extra_tenants_by_uid_or_name(monkeypatch):
    name, uid = "svc-runner", 113
    monkeypatch.setattr(dh.pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", uid, uid, "", "/", "/bin/sh")))
    monkeypatch.setenv(dh.TENANT_UIDS_ENV, f" {SERVICE_UID} , {name},")
    assert dh.configured_tenant_uids() == frozenset({SERVICE_UID, uid})
    assert dh.is_tenant(DeviceHolder(pid=1, uid=SERVICE_UID)) is True
    assert dh.is_tenant(DeviceHolder(pid=1, uid=uid)) is True
    assert dh.is_tenant(DeviceHolder(pid=1, uid=114)) is False


@pytest.mark.parametrize("entry", ["0", pwd.getpwuid(0).pw_name])
def test_tenant_uids_env_never_makes_root_a_tenant(monkeypatch, caplog, entry):
    """Root's daemons hold the device permanently: as a tenant, every gate, reset and reclaim
    would stay blocked for good. Root is ignored, with a warning, by uid or by name."""
    monkeypatch.setenv(dh.TENANT_UIDS_ENV, f"{entry},{SERVICE_UID}")
    with caplog.at_level("WARNING", logger="tt-device-mcp"):
        assert dh.configured_tenant_uids() == frozenset({SERVICE_UID})
    assert dh.is_tenant(DeviceHolder(pid=1, uid=0)) is False
    assert "root" in caplog.text


def test_an_unresolved_tenant_name_is_retried_not_cached(monkeypatch, caplog):
    """A lookup that fails at the first scan (the directory service is down) must not leave the
    account unprotected until a restart: the next scan asks again, and only a fully resolved
    value is cached. The warning is logged once, not on every scan."""
    directory = {}
    lookups = []

    def getpwnam(name):
        lookups.append(name)
        if name not in directory:
            raise KeyError(name)
        return pwd.struct_passwd((name, "x", directory[name], directory[name], "", "/", "/bin/sh"))

    monkeypatch.setattr(dh.pwd, "getpwnam", getpwnam)
    monkeypatch.setenv(dh.TENANT_UIDS_ENV, "svc-runner")
    with caplog.at_level("WARNING", logger="tt-device-mcp"):
        assert dh.configured_tenant_uids() == frozenset()
        assert dh.configured_tenant_uids() == frozenset()
    assert caplog.text.count("svc-runner") == 1, caplog.text

    directory["svc-runner"] = 113  # the directory service is back
    assert dh.is_tenant(DeviceHolder(pid=1, uid=113)) is True
    n = len(lookups)
    assert dh.configured_tenant_uids() == frozenset({113})
    assert len(lookups) == n, "a fully resolved value is cached"


def test_tenant_uids_env_unset_adds_nothing(monkeypatch):
    monkeypatch.delenv(dh.TENANT_UIDS_ENV, raising=False)
    assert dh.configured_tenant_uids() == frozenset()


# ------------------------------------------------------------------ every consumer uses it


def test_the_reset_gate_refuses_over_a_scoped_service_account_holder():
    scan = HolderScan(holders=[DeviceHolder(pid=4242, uid=SERVICE_UID, scoped=True)], complete=True)
    d = dh.evaluate_reset_gate(None, scan)
    assert d.allowed is False and [h.pid for h in d.foreign_holders] == [4242]
    # The caller's own scoped holder is still its own.
    assert dh.evaluate_reset_gate(SERVICE_UID, scan).allowed is True


def test_the_reset_gate_refuses_over_a_configured_tenant_uid(monkeypatch):
    monkeypatch.setenv(dh.TENANT_UIDS_ENV, str(SERVICE_UID))
    scan = HolderScan(holders=[DeviceHolder(pid=4242, uid=SERVICE_UID)], complete=True)
    assert dh.evaluate_reset_gate(None, scan).allowed is False


def test_reclaim_signals_a_scoped_service_account_holder():
    scan = HolderScan(
        holders=[DeviceHolder(pid=8, uid=113), DeviceHolder(pid=4242, uid=SERVICE_UID, scoped=True)],
        complete=True,
    )
    signals = []
    res = dh.reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan if not signals else HolderScan(holders=[], complete=True),
        read_starttime=lambda pid: "fixed",
        getpgid=lambda pid: -1 if pid == os.getpid() else pid,
    )
    assert [p for p, _ in signals] == [4242]
    assert res.survivors == []


def test_the_ladder_counts_a_scoped_straggler_of_a_killed_job_as_a_tenant(monkeypatch):
    """The window this closes: the job is no longer RUNNING, its scope still holds the device."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.deps, "job_running", lambda: False)
    straggler = HolderScan(holders=[DeviceHolder(pid=4242, uid=SERVICE_UID, scoped=True)], complete=True)
    assert g._tenant_active(straggler) is True
    assert g._tenant_active(straggler, force=True) is True, "force never waives a known tenant"
    infra = HolderScan(holders=[DeviceHolder(pid=4242, uid=SERVICE_UID)], complete=True)
    assert g._tenant_active(infra) is False


def test_dispatch_waits_for_a_scoped_service_account_holder(monkeypatch):
    holder = DeviceHolder(pid=4242, uid=SERVICE_UID, scoped=True)
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["0"])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))
    monkeypatch.setattr(dh, "_read_proc_ppid", {}.get)  # not the broker's child
    assert "pid 4242" in srv._tenant_holder_reason()

    unscoped = DeviceHolder(pid=4242, uid=SERVICE_UID)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[unscoped], complete=True))
    assert srv._tenant_holder_reason() == ""


def test_a_slurm_step_is_not_free_beside_a_scoped_service_account_holder(monkeypatch):
    holder = DeviceHolder(pid=4242, uid=SERVICE_UID, scoped=True)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))
    monkeypatch.setattr(srv, "_device_degraded_for_tenant", lambda: "")
    verdict = srv._slurm_step_verdict(require_free=True)
    assert [h["pid"] for h in verdict.get("holders", [])] == [4242], verdict
