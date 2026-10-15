# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the /proc-based device-holder enumeration helper."""

import os

import pytest

import tt_device_mcp.device_holders as dh


class TestEnumerateDeviceHolders:
    def test_detects_self_holding_a_device_node(self, tmp_path, monkeypatch):
        """An open fd whose target is under DEVICE_DIR is reported as a holder.

        We point DEVICE_DIR at a temp dir and open a file inside it so the scan
        finds *this* process without touching the real Tenstorrent device.
        """
        fake_dev = tmp_path / "fakedev"
        fake_dev.mkdir()
        node = fake_dev / "0"
        node.write_text("")

        monkeypatch.setattr(dh, "DEVICE_DIR", str(fake_dev))

        fd = os.open(node, os.O_RDONLY)
        try:
            result = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        my_uid = os.getuid()
        assert any(
            h.pid == os.getpid() and h.uid == my_uid for h in result.holders
        ), f"current process not found among holders: {result.holders}"

    def test_no_holders_when_nothing_open(self, tmp_path, monkeypatch):
        empty_dev = tmp_path / "empty"
        empty_dev.mkdir()
        monkeypatch.setattr(dh, "DEVICE_DIR", str(empty_dev))

        result = dh.enumerate_device_holders()
        assert result.holders == []

    def test_deleted_node_fd_is_not_a_holder(self, tmp_path, monkeypatch):
        """A stale fd to a since-recreated node reads as '... (deleted)' and must
        NOT count as a holder — else a hung process jams the reset gate forever."""
        fake_dev = tmp_path / "fakedev"
        fake_dev.mkdir()
        node = fake_dev / "0"
        node.write_text("")
        monkeypatch.setattr(dh, "DEVICE_DIR", str(fake_dev))

        fd = os.open(node, os.O_RDONLY)
        os.unlink(node)  # /proc/self/fd/N now points at ".../0 (deleted)"
        try:
            result = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert not any(h.pid == os.getpid() for h in result.holders)

    def test_permission_denied_marks_scan_incomplete(self, monkeypatch):
        """A PermissionError reading another uid's fds degrades gracefully."""
        monkeypatch.setattr(dh, "_iter_pids", lambda: [1, 2, 3])

        def fake_holds(pid):
            if pid == 2:
                raise PermissionError("cannot read other uid's fds")
            return False

        monkeypatch.setattr(dh, "_process_holds_device", fake_holds)

        result = dh.enumerate_device_holders()
        assert result.complete is False
        assert result.holders == []

    def test_vanished_process_is_skipped(self, monkeypatch):
        """A process that exits mid-scan (FileNotFoundError) is ignored, scan stays complete."""
        monkeypatch.setattr(dh, "_iter_pids", lambda: [1, 2])

        def fake_holds(pid):
            if pid == 2:
                raise FileNotFoundError("proc gone")
            return False

        monkeypatch.setattr(dh, "_process_holds_device", fake_holds)

        result = dh.enumerate_device_holders()
        assert result.complete is True
        assert result.holders == []


def test_reclaim_signals_only_tenant_holders():
    """Infrastructure below MIN_TENANT_UID (root, tt_telemetry_server) survives a board reset and
    is not a competing tenant; signalling it would take out the host's telemetry."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    scan = HolderScan(
        holders=[DeviceHolder(pid=7, uid=0), DeviceHolder(pid=8, uid=113), DeviceHolder(pid=4242, uid=1001)],
        complete=True,
    )
    signals = []
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan if not signals else HolderScan(holders=[], complete=True),
        read_starttime=lambda pid: "fixed",  # same process throughout; identity never changes
    )
    assert [p for p, _ in signals] == [4242], f"reclaim signalled non-tenant pids: {signals}"
    assert [h.pid for h in res.signalled] == [4242]
    assert res.survivors == []


def test_reclaim_escalates_term_to_kill():
    """A straggler that ignores SIGTERM still has to let go of the device before the ladder can
    run — the epilogue is the authority that the allocation is over."""
    import signal

    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    stubborn = HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True)
    signals = []
    scans = [stubborn, stubborn, HolderScan(holders=[], complete=True)]
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scans.pop(0) if scans else HolderScan(holders=[], complete=True),
        read_starttime=lambda pid: "fixed",  # same process throughout; identity never changes
    )
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)], f"wrong escalation: {signals}"
    assert res.survivors == []


def test_reclaim_reports_a_survivor_rather_than_claiming_success():
    """A holder that survives SIGKILL is unkillable (kernel-blocked in a device ioctl). The gate's
    fail-closed tenant rule must then stand — reporting success here would be a reset over a
    tenant by the back door."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    stuck = HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True)
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: None,
        sleep=lambda _: None,
        rescan=lambda: stuck,
        read_starttime=lambda pid: "fixed",  # same process throughout; identity never changes
    )
    assert [h.pid for h in res.survivors] == [4242], "an unkillable holder was not reported as a survivor"


def test_reclaim_tolerates_a_process_that_already_exited():
    """The pid is read from a scan and signalled later; a race is normal, not an error. But
    nothing was actually signalled here -- the kill call never landed -- so the pid must not
    appear in the audit's `signalled` list, which asserts a broker action that really happened."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    scans = [
        HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True),
        HolderScan(holders=[], complete=True),
    ]

    calls = []

    def gone(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError(pid)

    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=gone,
        sleep=lambda _: None,
        rescan=lambda: scans.pop(0),
        read_starttime=lambda pid: "fixed",  # deterministic identity: without this, whether
        # `gone` is reached at all depends on whether pid 4242 happens to exist as a real
        # process on the host running the suite (an unreadable starttime skips the kill call
        # entirely, which would give the same signalled==[]/survivors==[] result for a reason
        # unrelated to ProcessLookupError handling -- the `calls` assertion below is what tells
        # the two apart).
    )
    assert calls, "the kill call was never actually attempted; this test proved nothing"
    assert res.survivors == [], "a process that already exited must not be reported as a survivor"
    assert res.signalled == [], "a kill that raised ProcessLookupError must not be reported as signalled"


def test_reclaim_does_not_report_a_permission_denied_pid_as_signalled():
    """Recording a holder as signalled before the kill call meant a PermissionError -- we never
    actually touched that process -- still produced an audit row claiming the broker signalled
    it. The audit must reflect what happened, not what was merely attempted."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    scan = HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True)

    def denied(pid, sig):
        raise PermissionError(pid)

    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=denied,
        sleep=lambda _: None,
        rescan=lambda: scan,
        read_starttime=lambda pid: "fixed",
    )
    assert res.signalled == [], f"a permission-denied pid was reported as signalled: {res.signalled}"
    assert [h.pid for h in res.survivors] == [4242], "the never-signalled holder must still show up as a survivor"


def test_reclaim_carries_the_rescan_completeness():
    """A blind re-scan cannot prove the device is free; the caller has to fail closed on it."""
    from tt_device_mcp.device_holders import HolderScan, reclaim_foreign_holders

    res = reclaim_foreign_holders(
        grace_sec=0, kill=lambda p, s: None, sleep=lambda _: None, rescan=lambda: HolderScan(holders=[], complete=False)
    )
    assert res.scan_complete is False, "reclaim reported a blind re-scan as complete"


def test_reclaim_never_retargets_onto_a_new_holder_that_appears_after_it_began():
    """The reclaim's authority is over the allocation that just ended, not over the device in
    general. If the original target dies to SIGTERM but a DIFFERENT tenant opens the device
    during the grace window, that newcomer is not a straggler of the ended allocation — it is a
    bystander with no relation to it, and must never be escalated onto (no SIGKILL, no SIGTERM)."""
    import signal

    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    original = DeviceHolder(pid=100, uid=1001)
    newcomer = DeviceHolder(pid=200, uid=1002)
    # Round 0 (initial scan): only the original target. Round 1 (after SIGTERM+grace): the
    # original is gone, but the newcomer has shown up in its place. It persists from then on.
    scans = [
        HolderScan(holders=[original], complete=True),
        HolderScan(holders=[newcomer], complete=True),
        HolderScan(holders=[newcomer], complete=True),
        HolderScan(holders=[newcomer], complete=True),
    ]
    signals = []
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scans.pop(0) if scans else HolderScan(holders=[newcomer], complete=True),
        read_starttime=lambda pid: "fixed",  # deterministic identity, so round 1 actually
        # signals the original -- otherwise "the newcomer was never signalled" would hold
        # trivially whether or not the original ever was either, and depend on whether pid 100
        # happens to exist as a real process on this host. Safe for pid 200 too: a newcomer's
        # eligibility is decided by pid membership in target_starttimes (captured only for the
        # original), never by what read_starttime returns for it.
    )
    assert signals == [(100, signal.SIGTERM)], f"round 1 did not cleanly signal only the original: {signals}"
    assert 200 not in [pid for pid, _ in signals], f"the newcomer was signalled: {signals}"
    assert [h.pid for h in res.survivors] == [200], f"the newcomer did not come back as a survivor: {res.survivors}"


def test_reclaim_never_signals_itself_or_its_own_process_group():
    """A defensive belt, not the mechanism that keeps the reclaim off a broker job (that is the
    in-flight guard in server.py): even a holder scan that somehow reported this very process, or
    another process in its own process group, as a device holder must never be escalated onto."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    self_pid = 555
    packmate_pid = 556  # shares self_pid's process group but is a distinct pid
    stranger_pid = 4242  # a genuine foreign tenant, no relation to this process's group
    scan = HolderScan(
        holders=[
            DeviceHolder(pid=self_pid, uid=1001),
            DeviceHolder(pid=packmate_pid, uid=1001),
            DeviceHolder(pid=stranger_pid, uid=1001),
        ],
        complete=True,
    )
    pgids = {self_pid: 555, packmate_pid: 555, stranger_pid: 4242}
    signals = []
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan if not signals else HolderScan(holders=[], complete=True),
        getpid=lambda: self_pid,
        getpgid=lambda pid: pgids[pid],
        read_starttime=lambda pid: "fixed",  # same process throughout; identity never changes
    )
    assert {pid for pid, _ in signals} == {stranger_pid}, f"a self/packmate pid was signalled: {signals}"
    assert [h.pid for h in res.signalled] == [stranger_pid]


def test_reclaim_reports_a_pid_that_died_to_an_earlier_round_as_signalled():
    """`targets` narrows every round on purpose (a survivor of round N is the only thing
    eligible for round N+1's escalation), but the reported `signalled` set must not narrow
    with it: a pid that died to SIGTERM and is gone by the SIGKILL round was still signalled by
    this call and must show up in the result, or a run that SIGKILLed a tenant can report an
    empty reclaim."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    term_only = DeviceHolder(pid=100, uid=1001)  # dies to SIGTERM, gone before the SIGKILL round
    escalated = DeviceHolder(pid=200, uid=1001)  # survives SIGTERM, dies to SIGKILL
    newcomer = DeviceHolder(pid=300, uid=1002)  # opens the device mid-reclaim; never a target
    scans = [
        HolderScan(holders=[term_only, escalated], complete=True),  # initial scan
        HolderScan(holders=[escalated, newcomer], complete=True),  # after SIGTERM: term_only gone
        HolderScan(holders=[newcomer], complete=True),  # after SIGKILL: escalated gone too
        HolderScan(holders=[newcomer], complete=True),  # final rescan
    ]
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: None,
        sleep=lambda _: None,
        rescan=lambda: scans.pop(0),
        read_starttime=lambda pid: "fixed",  # same processes throughout; identity never changes
    )
    assert {h.pid for h in res.signalled} == {100, 200}, f"a pid signalled earlier went unreported: {res.signalled}"
    assert [h.pid for h in res.survivors] == [300], f"the newcomer was not the only survivor: {res.survivors}"


def test_reclaim_never_escalates_onto_a_pid_reused_by_an_unrelated_process():
    """The grace window between rounds is long enough for the original holder to exit and its pid
    to be reused by an unrelated new process before the rescan runs. Tracking eligibility by pid
    alone would then SIGKILL that newcomer under the belief it is the round-1 straggler -- exactly
    the retargeting the newcomer protection exists to prevent. starttime tells them apart: a
    reused pid never carries the same starttime as the process it replaced."""
    import signal

    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    original = DeviceHolder(pid=100, uid=1001)
    reused = DeviceHolder(pid=100, uid=1002)  # same pid, unrelated process, after reuse
    scans = [
        HolderScan(holders=[original], complete=True),
        HolderScan(holders=[reused], complete=True),
        HolderScan(holders=[reused], complete=True),
        HolderScan(holders=[reused], complete=True),
    ]
    # The pid is reused during the grace window BETWEEN rounds, not before round 1's own signal —
    # keyed off the grace sleep (the only thing that actually represents elapsed time here), not
    # off a raw call count. A raw count would also trip on the pre-signal revalidation immediately
    # before round 1's own kill (no time has passed yet at that point), which is exactly the
    # identity check this test must NOT defeat: round 1 has to actually signal the original
    # process for "never escalates onto the reused pid" to mean anything.
    reused_now = {"flag": False}

    def fake_sleep(_):
        reused_now["flag"] = True

    def read_starttime(pid):
        return "2000" if reused_now["flag"] else "1000"

    signals = []
    res = reclaim_foreign_holders(
        grace_sec=1,  # truthy, so `sleep` (and thus the reuse) actually happens between rounds
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=fake_sleep,
        rescan=lambda: scans.pop(0) if scans else HolderScan(holders=[reused], complete=True),
        read_starttime=read_starttime,
    )
    assert signals == [(100, signal.SIGTERM)], f"the reused pid was escalated onto: {signals}"
    assert [h.pid for h in res.survivors] == [100], f"the reused pid was not reported as a survivor: {res.survivors}"


def test_reclaim_never_signals_a_target_whose_starttime_was_unreadable_at_selection():
    """'An unreadable starttime means never signal this identity' is supposed to be absolute, not
    merely a bar to being RE-matched after round 1. Before the per-signal revalidation, the very
    first SIGTERM signalled every initial target unconditionally, including one whose starttime
    read had already come back None at selection -- only round 2's between-round narrowing would
    have excluded it, one signal too late."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    unreadable = DeviceHolder(pid=100, uid=1001)
    scan = HolderScan(holders=[unreadable], complete=True)
    signals = []
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan,
        read_starttime=lambda pid: None,
    )
    assert signals == [], f"a target with an unreadable starttime was signalled: {signals}"
    assert [h.pid for h in res.survivors] == [100], "an unsignalled target must still show up as a survivor"


def test_reclaim_revalidates_identity_immediately_before_the_first_signal_too():
    """The between-round narrowing alone left the FIRST round unguarded: a pid whose identity
    changed between the selection scan and the moment of the very first kill call -- not merely
    between rounds -- must still never be signalled. The check has to run immediately before
    EVERY signal, not just when rebuilding `targets` for the next round. This would fail on a
    version that only revalidates identity between rounds: round 1 would signal pid 100 before
    ever comparing its post-selection starttime against the captured one."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders

    holder = DeviceHolder(pid=100, uid=1001)
    scan = HolderScan(holders=[holder], complete=True)
    calls = {"n": 0}

    def read_starttime(pid):
        calls["n"] += 1
        # Selection (call 1) reads one value; the pre-signal revalidation (call 2, still round 1,
        # before any grace sleep) reads a different one -- the pid was reused in between.
        return "1000" if calls["n"] == 1 else "2000"

    signals = []
    reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan,
        read_starttime=read_starttime,
    )
    assert signals == [], f"round 1 signalled a target whose identity had already changed: {signals}"
    assert calls["n"] >= 2, "the test never actually exercised a pre-signal revalidation call"


def test_reclaim_with_nothing_to_reclaim_does_not_rescan_again():
    """When there is nothing to reclaim, the single scan's own completeness is reported — a
    second rescan here would be pure waste and a real window where a holder could appear unseen
    between the two calls."""
    from tt_device_mcp.device_holders import HolderScan, reclaim_foreign_holders

    scans = [HolderScan(holders=[], complete=False), HolderScan(holders=[], complete=True)]

    res = reclaim_foreign_holders(
        grace_sec=0, kill=lambda p, s: None, sleep=lambda _: None, rescan=lambda: scans.pop(0)
    )
    assert res.scan_complete is False, "reclaim used a second rescan instead of the first scan's own completeness"


class TestDriverHolderRecord:
    """tt-kmd's own holder record (`/proc/driver/tenstorrent/<n>/pids`), spec 04 I6.

    The fd walk needs CAP_DAC_READ_SEARCH to read another uid's fd table, so an unprivileged
    scan is blind to exactly the tenants the reset gate protects and fails closed for lack of
    privilege rather than for anything about the device. This route is world-readable and
    complete by construction.
    """

    @staticmethod
    def _driver_dir(tmp_path, monkeypatch, per_device):
        """Stage a fake driver proc dir. `per_device` maps index -> pids file contents."""
        root = tmp_path / "ttdriver"
        for index, contents in per_device.items():
            d = root / str(index)
            d.mkdir(parents=True)
            (d / "pids").write_text(contents)
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dh, "DRIVER_PROC_DIR", str(root))
        return root

    @staticmethod
    def _hold_a_fake_node(tmp_path, monkeypatch):
        """Make this process a genuine holder of a staged device node. Returns the fd."""
        fake_dev = tmp_path / "fakedev"
        fake_dev.mkdir()
        node = fake_dev / "0"
        node.write_text("")
        monkeypatch.setattr(dh, "DEVICE_DIR", str(fake_dev))
        return os.open(node, os.O_RDONLY)

    def test_a_free_device_scans_complete_without_privilege(self, tmp_path, monkeypatch):
        """The win: an empty record is a COMPLETE scan proving nothing holds the device.

        The walk cannot produce this for an unprivileged caller — it can only report that it was
        unable to look — so the reset gate refused on a free device (04 I6 fails closed on a blind
        scan, correctly, which is why the scan must stop being blind).
        """
        self._driver_dir(tmp_path, monkeypatch, {0: "", 1: "", 2: ""})

        scan = dh.enumerate_device_holders()

        assert scan.source == "driver"
        assert scan.holders == []
        assert scan.complete is True
        assert dh.evaluate_reset_gate(os.getuid(), scan).allowed is True

    def test_a_driver_named_holder_is_attributed_to_its_uid(self, tmp_path, monkeypatch):
        self._driver_dir(tmp_path, monkeypatch, {0: f"{os.getpid()}\n"})
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert scan.source == "driver"
        assert [(h.pid, h.uid) for h in scan.holders] == [(os.getpid(), os.getuid())]
        assert scan.complete is True

    def test_a_process_holding_two_devices_is_listed_once(self, tmp_path, monkeypatch):
        """The driver lists a pid under every device it holds; the scan must not double-count."""
        pid = os.getpid()
        self._driver_dir(tmp_path, monkeypatch, {0: f"{pid}\n", 1: f"{pid}\n"})
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert [h.pid for h in scan.holders] == [pid]

    def test_junk_lines_are_ignored(self, tmp_path, monkeypatch):
        self._driver_dir(tmp_path, monkeypatch, {0: f"not-a-pid\n{os.getpid()}\n\n"})
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert [h.pid for h in scan.holders] == [os.getpid()]

    def test_a_pid_we_cannot_see_is_unattributed_not_absent(self, tmp_path, monkeypatch):
        """A holder the driver sees and we cannot must fail the scan closed, never read as free."""
        absent = 2**22 - 1  # above every plausible pid_max
        self._driver_dir(tmp_path, monkeypatch, {0: f"{absent}\n"})

        scan = dh.enumerate_device_holders()

        assert scan.holders == []
        assert scan.complete is False, "an unattributable holder read as an empty, complete scan"
        assert dh.evaluate_reset_gate(os.getuid(), scan).allowed is False

    def test_a_pid_that_holds_no_device_here_is_not_attributed(self, tmp_path, monkeypatch):
        """The pid-namespace guard.

        The driver's pids are always the host's. Read through a container's own /proc the same
        number is an unrelated process, and charging its uid with holding the device would let the
        gate mistake a stranger for the caller's own holder. This process is named by the record
        but holds nothing under DEVICE_DIR, which is exactly that shape.
        """
        fake_dev = tmp_path / "fakedev"
        fake_dev.mkdir()
        monkeypatch.setattr(dh, "DEVICE_DIR", str(fake_dev))
        self._driver_dir(tmp_path, monkeypatch, {0: f"{os.getpid()}\n"})

        scan = dh.enumerate_device_holders()

        assert scan.holders == [], "a pid holding no device node was attributed as a holder"
        assert scan.complete is False

    def test_an_unreadable_device_file_marks_the_scan_incomplete(self, tmp_path, monkeypatch):
        """Readable devices still answer, but the driver knows of holders we could not enumerate."""
        if os.geteuid() == 0:
            pytest.skip("root reads a 0000 file regardless, so an unreadable record cannot be staged")
        root = self._driver_dir(tmp_path, monkeypatch, {0: "", 1: ""})
        (root / "1" / "pids").chmod(0o000)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            (root / "1" / "pids").chmod(0o644)

        assert scan.source == "driver"
        assert scan.complete is False

    def test_an_absent_driver_dir_falls_back_to_the_walk(self, tmp_path, monkeypatch):
        """A host whose tt-kmd predates this interface must not read as 'no holders'."""
        monkeypatch.setattr(dh, "DRIVER_PROC_DIR", str(tmp_path / "nothing-here"))
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert scan.source == "proc"
        assert any(h.pid == os.getpid() for h in scan.holders)

    def test_a_driver_dir_with_no_devices_falls_back_to_the_walk(self, tmp_path, monkeypatch):
        empty = tmp_path / "ttdriver-empty"
        empty.mkdir()
        monkeypatch.setattr(dh, "DRIVER_PROC_DIR", str(empty))
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)

        assert scan.source == "proc"

    def test_wholly_unreadable_records_fall_back_rather_than_report_blind(self, tmp_path, monkeypatch):
        """Nothing distinguishes "not published" from "could not read" when every file fails.

        Declining is better than handing the gate a blind driver scan: the walk can still answer,
        and its blind spots are the ones 04 I6/I7 were written against.
        """
        if os.geteuid() == 0:
            pytest.skip("root reads a 0000 file regardless, so an unreadable record cannot be staged")
        root = self._driver_dir(tmp_path, monkeypatch, {0: "", 1: ""})
        for index in ("0", "1"):
            (root / index / "pids").chmod(0o000)
        fd = self._hold_a_fake_node(tmp_path, monkeypatch)
        try:
            scan = dh.enumerate_device_holders()
        finally:
            os.close(fd)
            for index in ("0", "1"):
                (root / index / "pids").chmod(0o644)

        assert scan.source == "proc"
        assert any(h.pid == os.getpid() for h in scan.holders)


def test_read_proc_ppid_reads_this_process_parent():
    """The real /proc parse (no device involved): our own ppid, and None for a pid that is gone."""
    assert dh._read_proc_ppid(os.getpid()) == os.getppid()
    assert dh._read_proc_ppid(2**22 + 1) is None  # above pid_max
