"""The leftover fence (spec 01 I16, spec 04 I21).

A reaped job's process stuck in the kernel (state D, SIGKILL pending) can keep its
/dev/tenstorrent fds open long after the runner swept the job. A reset run over it does not
recover the mesh, and a job dispatched beside it fails. The broker names such a holder, emits one
event per episode, and runs no reset and dispatches no job until it is gone.

Every test stages its own /proc under ``job_reap.LEFTOVER_PROC_DIR`` and patches the holder scan;
none reads the real /proc or touches a device.
"""

import os

import pytest
from starlette.testclient import TestClient

import tt_device_mcp.job_reap as job_reap
import tt_device_mcp.server as srv
from tests.conftest import patch_health_event
from tt_device_mcp import device_holders
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import base as recovery_base

LEFTOVER_PID = 6100
SIGKILL_PENDING = f"{1 << 8:016x}"  # bit 9 (SIGKILL) of the pending mask


def _stage(proc_dir, pid, *, job_id="7", state="D", sigpnd=SIGKILL_PENDING, wchan="tt_cdev_release"):
    d = proc_dir / str(pid)
    d.mkdir(parents=True)
    fields = [state] + ["0"] * 18 + ["424242"]  # field 3 (state) .. field 22 (starttime)
    (d / "stat").write_text(f"{pid} (python3) {' '.join(fields)} 0 0\n")
    scope = f"/system.slice/ttdev-job-{job_id}.scope" if job_id else "/user.slice/session-1.scope"
    (d / "cgroup").write_text(f"0::{scope}\n")
    (d / "status").write_text(f"Name:\tpython3\nSigPnd:\t{sigpnd}\nShdPnd:\t{'0' * 16}\n")
    (d / "wchan").write_text(wchan)


@pytest.fixture
def leftover(monkeypatch, tmp_path):
    """A staged reaped-job leftover holding the device, and the events the broker emits.

    Returns a dict: ``scan`` is the live holder scan (assign ``scan["holders"]`` to change it),
    ``events`` the (kind, fields) pairs emitted."""
    proc = tmp_path / "proc"
    proc.mkdir()
    _stage(proc, LEFTOVER_PID)
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(proc))
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv, "current_process", None)
    state = {"holders": [DeviceHolder(pid=LEFTOVER_PID, uid=1234)], "events": [], "proc": proc}
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["0"])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=state["holders"], complete=True))

    def event(kind, **fields):
        state["events"].append((kind, fields))

    patch_health_event(monkeypatch, event)
    monkeypatch.setattr(recovery_base, "health_event", event)
    return state


def _kinds(state, kind):
    return [f for k, f in state["events"] if k == kind]


def test_the_leftover_is_named_and_the_event_fires_once_per_episode(leftover):
    reason = srv._tenant_holder_reason()

    assert "job 7 pid 6100" in reason and "state D" in reason and "wchan tt_cdev_release" in reason
    assert "SIGKILL pending" in reason and "only a host reboot clears it" in reason
    srv._tenant_holder_reason()
    srv._reaped_leftover_reason()
    held = _kinds(leftover, "reaped_leftover_holds_device")
    assert len(held) == 1, "one event per episode, not one per scan"
    (only,) = held[0]["leftovers"]
    assert only == {
        "job_id": "7",
        "pid": LEFTOVER_PID,
        "uid": 1234,
        "state": "D",
        "wchan": "tt_cdev_release",
        "sigkill_pending": True,
    }
    assert held[0]["unkillable"] is True and "host reboot" in held[0]["operator_hint"]


def test_the_fence_lifts_and_the_release_event_fires_once_the_holder_is_gone(leftover):
    assert srv._reaped_leftover_reason()
    leftover["holders"] = []

    assert srv._reaped_leftover_reason() == ""
    assert srv._tenant_holder_reason() == ""
    released = _kinds(leftover, "reaped_leftover_released")
    assert len(released) == 1 and "job 7 pid 6100" in released[0]["leftovers"]
    # A new leftover later is a new episode.
    leftover["holders"] = [DeviceHolder(pid=LEFTOVER_PID, uid=1234)]
    assert srv._reaped_leftover_reason()
    assert len(_kinds(leftover, "reaped_leftover_holds_device")) == 2


def test_dispatch_is_refused_even_when_the_leftover_descends_from_the_broker(leftover, monkeypatch):
    """A process stuck in the kernel is never reaped, so it can still be the broker's child, which
    the ordinary tenant filter skips. The leftover is checked first, whatever its uid or parent."""
    monkeypatch.setattr(device_holders, "_read_proc_ppid", {LEFTOVER_PID: os.getpid()}.get)
    leftover["holders"] = [DeviceHolder(pid=LEFTOVER_PID, uid=0)]

    assert "job 7 pid 6100" in srv._tenant_holder_reason()


def test_a_running_jobs_own_scope_is_not_a_leftover(leftover, monkeypatch):
    job = srv.Job(id="7", owner="alice", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING)
    monkeypatch.setattr(srv, "jobs", {"7": job})

    assert srv._reaped_leftover_reason() == ""
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {"7": "ttdev-job-7.scope"})
    assert srv._reaped_leftover_reason() == ""
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "current_job_id", "7")  # being torn down
    assert srv._reaped_leftover_reason() == ""
    assert not _kinds(leftover, "reaped_leftover_holds_device")


def test_an_ordinary_holder_behaves_as_before(leftover, monkeypatch):
    """Outside any job scope and not a swept survivor: the ordinary tenant rule names it, and a
    broker child is still exempt."""
    _stage(leftover["proc"], 6200, job_id=None, state="S", sigpnd="0" * 16)
    leftover["holders"] = [DeviceHolder(pid=6200, uid=1234)]
    monkeypatch.setattr(device_holders, "_read_proc_ppid", {}.get)

    reason = srv._tenant_holder_reason()
    assert "device held outside the broker" in reason and "pid 6200" in reason and "reaped" not in reason
    monkeypatch.setattr(device_holders, "_read_proc_ppid", {6200: os.getpid()}.get)
    assert srv._tenant_holder_reason() == ""
    assert not _kinds(leftover, "reaped_leftover_holds_device")


def test_a_swept_survivor_outside_a_scope_is_a_leftover_only_with_the_same_starttime(leftover, monkeypatch):
    """Off privsep there is no scope cgroup: the sweep's own record names the job, and a reused pid
    (a different starttime) is not mistaken for it."""
    _stage(leftover["proc"], 6300, job_id=None)
    leftover["holders"] = [DeviceHolder(pid=6300, uid=1234)]
    monkeypatch.setattr(device_holders, "_read_proc_ppid", {6300: os.getpid()}.get)

    monkeypatch.setattr(srv, "reaped_survivors", {6300: ("9", "999")})
    assert srv._reaped_leftover_reason() == ""
    monkeypatch.setattr(srv, "reaped_survivors", {6300: ("9", "424242")})
    assert "job 9 pid 6300" in srv._tenant_holder_reason()


def test_a_survivor_record_is_dropped_once_a_complete_scan_no_longer_sees_it(leftover, monkeypatch):
    monkeypatch.setattr(srv, "reaped_survivors", {6300: ("9", "424242")})
    leftover["holders"] = []
    srv._reaped_leftover_reason()
    assert srv.reaped_survivors == {}


def test_a_scan_error_never_fences_a_free_device(leftover, monkeypatch):
    def boom(*a, **k):
        raise OSError("proc vanished")

    monkeypatch.setattr(srv, "find_reaped_leftovers", boom)
    assert srv._reaped_leftover_reason() == ""


def test_a_leftover_without_pending_sigkill_is_named_but_not_called_unkillable(leftover):
    _stage(leftover["proc"], 6400, job_id="8", state="S", sigpnd="0" * 16)
    leftover["holders"] = [DeviceHolder(pid=6400, uid=1234)]

    reason = srv._reaped_leftover_reason()
    assert "job 8 pid 6400" in reason and "no SIGKILL pending" in reason and "host reboot" not in reason
    assert _kinds(leftover, "reaped_leftover_holds_device")[0]["unkillable"] is False


# --- every reset path is refused --------------------------------------------------------------


def _reset_seams(monkeypatch, tmp_path):
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path / "dev"))
    resets = []

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        resets.append(list(argv))
        return 0, ""

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    return resets, TestClient(srv.build_asgi_app(srv.create_mcp_server()))


@pytest.mark.parametrize("force", [False, True])
def test_the_reset_tool_is_refused_even_with_force(leftover, monkeypatch, tmp_path, force):
    resets, client = _reset_seams(monkeypatch, tmp_path)

    d = client.post("/api/tt_device_reset", json={"force": force}).json()

    assert d["status"] == "refused" and "job 7 pid 6100" in d["reason"]
    assert "host reboot" in d["hint"]
    assert not resets, "a reset ran over a reaped job's leftover"


@pytest.mark.parametrize("force", [False, True])
def test_the_streaming_reset_is_refused_even_with_force(leftover, monkeypatch, tmp_path, force):
    resets, client = _reset_seams(monkeypatch, tmp_path)

    text = client.post("/api/tt_device_reset_stream", json={"force": force}).text

    assert "::status::refused" in text and "leftover REFUSED" in text and "job 7 pid 6100" in text
    assert "host reboot" in text
    assert not resets


@pytest.mark.asyncio
async def test_reset_with_quiesce_is_refused_through_the_fence(leftover, monkeypatch):
    """Every reset passes here (the gate, the ladder, the tools): the fence holds even for a caller
    that skipped its own check."""
    ran = []

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        ran.append(argv)
        return 0, ""

    async def pollers(active, log):
        return []

    async def no_rescan(fn, *a, **k):
        return None

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.asyncio, "to_thread", no_rescan)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)

    rc, out = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None, owner="manual")

    assert rc is None and "reset refused" in out and "job 7 pid 6100" in out
    assert not ran
    (fenced,) = _kinds(leftover, "reset_fenced")
    assert fenced["owner"] == "manual" and fenced["argv"] == ["tt-smi", "-r"]
    leftover["holders"] = []
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc == 0 and ran == [["tt-smi", "-r"]], "the fence lifts once the holder is gone"


def test_the_broker_wires_the_fence_into_the_recovery_mechanism(leftover):
    """The booted broker's mechanism asks the broker's own fence (conftest boots it)."""
    assert srv.recovery_mechanism.reset_fence is not None
    assert "job 7 pid 6100" in srv.recovery_mechanism.reset_fence()


@pytest.mark.asyncio
async def test_the_health_gate_runs_no_probe_and_keeps_the_device_dirty(leftover, monkeypatch):
    observed = []

    async def observe(*a, **k):
        observed.append(a)
        return True, {}

    monkeypatch.setattr(srv.fsm, "observe", observe)
    monkeypatch.setattr(srv.fsm.record, "dirty", False, raising=False)
    marked = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, **k: marked.append((reason, k)))

    await srv.device_health_gate(None, phase="pre-job", run_fabric=True)

    assert not observed, "the gate probed a device a reaped job's leftover holds"
    assert marked and "job 7 pid 6100" in marked[0][0] and marked[0][1]["why"] == "foreign_holder"
