# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The server FSM: BOOT/HEALTHY/RECOVERING/DOWN, and the durable record behind it."""

import json
from datetime import datetime

from tt_device_mcp.fsm import FsmRecord, ServerFsm, ServerState
from tt_device_mcp.health import OUTCOME_RECOVERED, OUTCOME_TERMINAL
from tt_device_mcp.health.core import HealthState


def _fsm(tmp_path):
    return ServerFsm(tmp_path / "fsm.json")


def test_boot_with_no_open_episode_starts_closed(tmp_path):
    """boot_merge never opens the door itself — absent an episode still open from before this
    restart, it always lands on a fresh RECOVERING(startup_unverified): only the startup gate's
    own forced probe pass, run right after, may prove the mesh fit. There is no "the file was
    clean, so skip the verify" shortcut."""
    f = _fsm(tmp_path)
    assert f.boot_merge(open_episode=None, attributed_boot=None) is ServerState.RECOVERING
    assert f.record.why == "startup_unverified"


def test_boot_open_episode_is_adopted(tmp_path):
    f = _fsm(tmp_path)
    ep = FsmRecord(ServerState.RECOVERING, why="eth_frozen", since="2026-08-17T00:00:00Z")
    assert f.boot_merge(open_episode=ep, attributed_boot=None) is ServerState.RECOVERING
    assert f.record.why == "eth_frozen"


def test_terminal_episode_survives_boot_merge(tmp_path):
    f = _fsm(tmp_path)
    ep = FsmRecord(ServerState.DOWN, why="off_bus", since="2026-08-17T00:00:00Z")
    assert f.boot_merge(open_episode=ep, attributed_boot=None) is ServerState.DOWN


def test_episode_latch_fires_once(tmp_path):
    f = _fsm(tmp_path)
    f.on_fault("job_killed")
    assert not f.latch("idle_escalation")  # unset for a fresh episode
    f.set_latch("idle_escalation", True)
    assert f.latch("idle_escalation")  # stays set for the rest of this episode
    f.on_outcome("recovered")
    f.on_fault("job_killed")
    assert not f.latch("idle_escalation")  # new episode, latch re-arms


def test_survives_restart(tmp_path):
    f = _fsm(tmp_path)
    f.on_fault("job_killed")
    g = _fsm(tmp_path)
    assert g.record.why == "job_killed"


def test_dirty_survives_restart(tmp_path):
    """dirty is the axis a restart most needs to remember: it is what tells the next gate pass
    whether this episode still owes a reset attempt, and that question spans the very crash the
    restart is recovering from."""
    f = _fsm(tmp_path)
    f.on_fault("job_killed")  # dirty=True by default
    g = _fsm(tmp_path)
    assert g.record.dirty is True

    f.on_fault("eth_frozen", dirty=False)  # a hold, not owed a reset
    h = _fsm(tmp_path)
    assert h.record.dirty is False


def test_a_wrong_shape_file_degrades_to_boot_not_a_crash(tmp_path):
    """Valid JSON, wrong shape (a bare list, here) must not raise out of the constructor —
    ServerFsm is built at module scope in server.py, so an exception here takes the whole broker
    down at import time, recoverable only by hand-deleting the file."""
    path = tmp_path / "fsm.json"
    path.write_text("[1, 2, 3]")
    f = ServerFsm(path)
    assert f.state is ServerState.BOOT


def test_malformed_field_types_degrade_to_boot_not_a_crash(tmp_path):
    """A dict-shaped file with the wrong type in a nested field (latches not a mapping) must not
    raise either — this is the shape a hand-edit or a future version's schema change would take,
    not just a totally different JSON shape."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(json.dumps({"state": "recovering", "why": "job_killed", "latches": True}))
    f = ServerFsm(path)
    assert f.state is ServerState.BOOT


def test_an_unknown_why_on_disk_clamps_on_load(tmp_path):
    """why is validated on write; a stale value from a different vocabulary (an older/newer
    version, or a hand-edited file) must still clamp on load, or nothing downstream — the idle
    relift and the escalation ladder both switch on exact membership in FAULTS — ever matches it
    and the episode sits inert forever."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(json.dumps({"state": "recovering", "why": "not_a_real_fault", "since": "2026-08-17T00:00:00Z"}))
    f = ServerFsm(path)
    assert f.record.why == "gate_error"


def test_boot_sentinel_is_not_an_open_episode(tmp_path):
    """A record still at BOOT (never reconciled — a first boot with no prior fsm.json, or one
    freshly constructed) must not be treated as an open episode to adopt: boot_merge closes on a
    fresh episode instead, exactly as if open_episode had been None."""
    f = _fsm(tmp_path)
    boot_record = f.record
    assert boot_record.state is ServerState.BOOT
    assert f.boot_merge(open_episode=boot_record) is ServerState.RECOVERING
    assert f.record.why == "startup_unverified"


def test_same_boot_restart_keeps_job_and_dirty(tmp_path):
    """A broker restart or crash within one boot — the same kernel boot_id both times — is the
    case this file mainly exists for: restore everything exactly as persisted, job and dirty
    included."""
    path = tmp_path / "fsm.json"
    f = ServerFsm(path, current_boot_id="boot-aaa")
    f.on_fault("job_killed", job={"id": "042"})

    g = ServerFsm(path, current_boot_id="boot-aaa")
    assert g.record.job == {"id": "042"}
    assert g.record.dirty is True
    assert g.record.why == "job_killed"


def test_cross_boot_load_voids_job_but_keeps_device_facts(tmp_path):
    """A different boot_id on disk than this boot's own means the box went down and came back
    between the write and this read — the job that was running cannot have survived that (job
    status already does not survive a restart; it lives under /run, tmpfs), so its context is
    void. state/why/since and the latches survive untouched — that is what lets the recovery
    ladder resume above whatever rung wrote this instead of re-entering at the bottom."""
    path = tmp_path / "fsm.json"
    f = ServerFsm(path, current_boot_id="boot-aaa")
    f.on_fault("off_bus", job={"id": "042"}, dirty=False)
    f.set_latch("escalated", True)

    g = ServerFsm(path, current_boot_id="boot-bbb")
    assert g.record.job == {}
    assert g.record.state is ServerState.RECOVERING
    assert g.record.why == "off_bus"
    assert g.record.dirty is False
    assert (
        g.latch("escalated") is True
    ), "the escalation ladder must see its own already-fired rung across the reboot it caused"


def test_a_dirty_mark_survives_a_cross_boot_load(tmp_path):
    """dirty is not cleared on a cross-boot load even though job is: this FSM does not track
    whether a dirty mark came from a job's own exit (moot across a reboot) or a probe/gate
    finding (very much not moot), so it fails closed and keeps it — the permissive read would be
    to assume it was job-only and clear it, which could skip a reset attempt an actually-dirty
    mesh is still owed."""
    path = tmp_path / "fsm.json"
    f = ServerFsm(path, current_boot_id="boot-aaa")
    f.on_fault("heartbeat")  # dirty=True by default

    g = ServerFsm(path, current_boot_id="boot-bbb")
    assert g.record.dirty is True
    assert g.record.job == {}


def test_a_file_with_no_boot_id_is_treated_as_cross_boot(tmp_path):
    """The upgrade path: a file written before boot-scoping existed carries no boot_id field at
    all, which must read the same as a mismatched one — unknown is never 'the same boot'."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "off_bus",
                "since": "2026-08-17T00:00:00Z",
                "job": {"id": "042"},
                "dirty": True,
            }
        )
    )

    f = ServerFsm(path, current_boot_id="boot-aaa")
    assert f.record.job == {}
    assert f.record.state is ServerState.RECOVERING
    assert f.record.why == "off_bus"


def test_a_non_dict_job_on_disk_degrades_to_empty_not_a_crash(tmp_path):
    """job is dict-shaped everywhere it is read (record's own dict() copy, the health payload,
    incident capture) — a hand-edited or truncated file that leaves a string/list/bool in its
    place must land on {} on load, not survive to raise out of the first caller that touches it."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "job_killed",
                "since": "2026-08-17T00:00:00Z",
                "job": "not-a-dict",
            }
        )
    )
    f = ServerFsm(path)
    assert f.record.job == {}
    assert dict(f.record.job) == {}  # the exact call record() makes on every read


def test_a_non_str_since_on_disk_degrades_to_a_string(tmp_path):
    """since feeds strptime (via _episode_elapsed_sec) and datetime.fromisoformat (the /health
    held_age_sec calc) — both raise TypeError, not ValueError, on a non-str argument, so either
    call site's narrow except would let a numeric/dict `since` take it down. Coercing to str on
    load means the worst a bad value does downstream is fail its own parse and report 0/None."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "job_killed",
                "since": 12345,
            }
        )
    )
    f = ServerFsm(path)
    assert isinstance(f.record.since, str)


def test_a_non_str_detail_on_disk_degrades_to_a_string(tmp_path):
    """detail is concatenated into log/event strings (f"{detail}" call sites) without its own
    type check — a non-str value must not ride through _load only to raise the first time
    something formats it."""
    import json

    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "job_killed",
                "since": "2026-08-17T00:00:00Z",
                "detail": {"nested": "object"},
            }
        )
    )
    f = ServerFsm(path)
    assert isinstance(f.record.detail, str)


def test_waiting_outcome_never_transitions(tmp_path):
    """The one invariant a future edit to on_outcome could break silently: WAITING must never move
    the state, from RECOVERING or from DOWN — a per-target host (no ladder, so escalate() always
    reports WAITING) must never be wedged into DOWN by an outcome that never fired a rung."""
    f = _fsm(tmp_path)
    f.on_fault("off_bus")
    f.on_outcome("waiting")
    assert f.state is ServerState.RECOVERING

    f.on_outcome("terminal")
    assert f.state is ServerState.DOWN
    f.on_outcome("waiting")
    assert f.state is ServerState.DOWN


def test_boot_merge_drops_a_stale_boot_attribution_carried_on_a_long_episode(tmp_path):
    """A boot-attribution clause names how THIS boot came up. Carried unconditionally on a long
    RECOVERING episode's detail, an 18h-old reboot reads as this boot's cause (the stale-attribution
    ledger bug). boot_merge must drop it on carry, keeping the rest of the detail. Fails on base,
    where open_episode.detail rides forward verbatim."""
    open_ep = FsmRecord(
        state=ServerState.RECOVERING,
        why="eth_frozen",
        since="2026-08-25T10:20:00Z",
        detail="gate/startup: eth/fabric fault boot attributed to a broker-fired reboot recorded at 2026-08-25T10:20:17",
    )
    f = ServerFsm(tmp_path / "fsm.json")
    f.boot_merge(open_episode=open_ep, attributed_boot=None)  # this boot has no adjacent escalation
    assert "boot attributed" not in f.record.detail, "a stale carried attribution must be dropped"
    assert "eth/fabric fault" in f.record.detail, "the rest of the detail must be kept"


def test_boot_merge_reattributes_only_the_current_boot(tmp_path):
    """When this boot IS a broker-fired escalation, the current (guarded) attribution replaces any
    stale carried one — not appended alongside it. Handles a hyphenated action (power-cycle)."""
    open_ep = FsmRecord(
        state=ServerState.RECOVERING,
        why="eth_frozen",
        detail="fault boot attributed to a broker-fired reboot recorded at OLD",
    )
    f = ServerFsm(tmp_path / "fsm.json")
    f.boot_merge(open_episode=open_ep, attributed_boot={"action": "power-cycle", "at": "NEW"})
    d = f.record.detail
    assert d.count("boot attributed") == 1, "no stale-plus-new duplicate"
    assert "recorded at NEW" in d and "recorded at OLD" not in d


def test_boot_merge_attributes_the_escalation_only_on_the_boots_first_broker_start(tmp_path):
    """The ledger names the escalation that brought THIS boot up, and stays adjacent to it for the
    whole boot. A broker restarted later in that boot (an autoupdate, a crash) was not caused by
    it: stamping it there labels every later hold with a weeks-old power cycle. Fails on base,
    where every start of the boot re-attributes."""
    path = tmp_path / "fsm.json"
    escalation = {"action": "power-cycle", "at": "2026-09-16T05:21:11"}
    first = ServerFsm(path, current_boot_id="boot-A")
    first.boot_merge(open_episode=None, attributed_boot=escalation)
    assert "boot attributed to a broker-fired power-cycle" in first.record.detail

    again = ServerFsm(path, current_boot_id="boot-A")  # restart within the same boot
    again.boot_merge(open_episode=None, attributed_boot=escalation)
    assert "boot attributed" not in (again.record.detail or "")

    carried = ServerFsm(path, current_boot_id="boot-A")  # a carried episode loses the stale clause too
    carried.boot_merge(
        open_episode=FsmRecord(state=ServerState.RECOVERING, why="eth_frozen", detail=first.record.detail),
        attributed_boot=escalation,
    )
    assert "boot attributed" not in (carried.record.detail or "")

    next_boot = ServerFsm(path, current_boot_id="boot-B")  # a new boot it brought up attributes again
    next_boot.boot_merge(open_episode=None, attributed_boot={"action": "reboot", "at": "2026-10-13T20:30:00"})
    assert "boot attributed to a broker-fired reboot" in next_boot.record.detail


def test_on_next_close_fires_once_on_whichever_path_reaches_healthy(tmp_path):
    """A caller pairing an opening event with its close (the startup hold's release row) registers
    once and must hear about the close exactly once — not on a refreshed fault mid-episode, not
    again on a later episode — whether a healthy read or a recovered rung closes it."""
    f = _fsm(tmp_path)
    f.boot_merge(open_episode=None, attributed_boot=None)
    closed = []
    f.on_next_close(closed.append)

    f.on_fault("foreign_holder", detail="pid 42 holds it", dirty=False)  # same episode, new why
    assert closed == []
    f.on_outcome(OUTCOME_RECOVERED)
    assert [(r.state, r.why) for r in closed] == [(ServerState.RECOVERING, "foreign_holder")]

    f.on_fault("job_killed")
    f.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))
    assert len(closed) == 1, "a one-shot close hook fired again on a later episode"


def test_a_failing_close_hook_does_not_break_the_transition(tmp_path):
    f = _fsm(tmp_path)
    f.on_fault("job_killed")

    def boom(_record):
        raise RuntimeError("journal unwritable")

    f.on_next_close(boom)
    f.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))
    assert f.state is ServerState.HEALTHY


def _startup_hold(path, boot_id="boot-aaa"):
    f = ServerFsm(path, current_boot_id=boot_id)
    f.boot_merge(open_episode=None)  # RECOVERING(startup_unverified)
    return f


def test_readopted_jobs_persist_and_survive_a_same_boot_restart(tmp_path):
    """Spec 03 I76: fsm.json carries the re-adopted jobs the startup hold waits on, so a client that
    reads the file can tell that hold from a fault; it survives a broker restart within the boot."""
    path = tmp_path / "fsm.json"
    f = _startup_hold(path)
    assert f.readopted_jobs == [] and json.loads(path.read_text())["readopted_jobs"] == []
    f.set_readopted_jobs(["428", "430"])
    assert json.loads(path.read_text())["readopted_jobs"] == ["428", "430"]

    g = ServerFsm(path, current_boot_id="boot-aaa")
    assert g.readopted_jobs == ["428", "430"]
    assert ServerFsm(path, current_boot_id="boot-bbb").readopted_jobs == [], "no job survives a reboot"


def test_readopted_jobs_clear_on_release_and_on_down(tmp_path):
    """Spec 03 I76: a release (the startup gate's healthy verdict) or DOWN ends what the field reports."""
    path = tmp_path / "fsm.json"
    f = _startup_hold(path)
    f.set_readopted_jobs(["428"])
    f.on_readings(HealthState(phase="startup", at=datetime.now(), expected=0))
    assert f.state is ServerState.HEALTHY
    assert f.readopted_jobs == [] and json.loads(path.read_text())["readopted_jobs"] == []
    f.set_readopted_jobs(["428"])
    assert f.readopted_jobs == [], "only a held device (boot or recovering) reports re-adopted jobs"

    g = _startup_hold(tmp_path / "down.json")
    g.set_readopted_jobs(["428"])
    g.on_outcome(OUTCOME_TERMINAL)
    assert g.state is ServerState.DOWN
    assert json.loads((tmp_path / "down.json").read_text())["readopted_jobs"] == []


def test_readopted_jobs_clear_when_the_job_ends(tmp_path):
    path = tmp_path / "fsm.json"
    f = _startup_hold(path)
    f.set_readopted_jobs(["428"])
    f.set_readopted_jobs([])
    assert json.loads(path.read_text())["readopted_jobs"] == []
    assert f.state is ServerState.RECOVERING and f.record.why == "startup_unverified", "the hold stands"


def test_an_fsm_file_without_readopted_jobs_still_loads(tmp_path):
    """Spec 03 I76: a file written before the field existed loads whole, with readopted_jobs []; a
    malformed value degrades to [] too."""
    path = tmp_path / "fsm.json"
    old = {
        "state": "recovering",
        "why": "startup_unverified",
        "since": "2026-10-17T05:08:50",
        "detail": "",
        "job": {},
        "dirty": False,
        "latches": {"x": True},
        "boot_id": "boot-aaa",
    }
    path.write_text(json.dumps(old))
    f = ServerFsm(path, current_boot_id="boot-aaa")
    assert f.state is ServerState.RECOVERING and f.record.why == "startup_unverified" and f.latch("x")
    assert f.readopted_jobs == []
    path.write_text(json.dumps({**old, "readopted_jobs": "428"}))
    assert ServerFsm(path, current_boot_id="boot-aaa").readopted_jobs == []
