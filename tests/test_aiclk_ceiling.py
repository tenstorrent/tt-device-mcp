# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The opt-in per-host AICLK ceiling (health/aiclk_ceiling.py, spec 03 I33 / 04 I20): off without
config, re-applied after every reset rung and at start before any traffic pass, proven at the job
door, and a failure that holds the box through the bounded ladder rather than wedging the queue."""

import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.health import aiclk_ceiling as ac
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitor import HealthMonitor

_HELPER = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-aiclk-ceiling.py"


@pytest.fixture(autouse=True)
def _fresh_ceiling(monkeypatch):
    """Each test starts with the process-wide state of a fresh broker and no config."""
    for k in (ac.ENV_MHZ, ac.ENV_CMD, ac.ENV_TIMEOUT, ac.ENV_PYTHON):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(CEILING, "owed", True)
    monkeypatch.setattr(CEILING, "disarmed", "")
    monkeypatch.setattr(CEILING, "last", {})
    monkeypatch.setattr(CEILING, "proc", None)
    events = []
    monkeypatch.setattr(ac, "health_event", lambda kind, **f: events.append((kind, f)))
    return events


def _arm(monkeypatch, cmd: str, mhz: str = "900"):
    monkeypatch.setenv(ac.ENV_MHZ, mhz)
    monkeypatch.setenv(ac.ENV_CMD, cmd)


_OK_CMD = 'echo \'{"summary": true, "mhz": 900, "chips": 32, "ok": 32, "sent": 3}\''


def _fake_apply(calls, results):
    """A stand-in for CEILING.apply that records where it ran and replays canned verdicts."""
    results = list(results)

    async def apply(where, *, log=None, terminate=None):
        calls.append(where)
        ok = results.pop(0) if results else True
        if ok:
            CEILING.owed = False
        if log:
            log(f"aiclk-ceiling: {'OK' if ok else 'UNHEALTHY'} — ceiling 900 MHz applied to 32/32")
        return ok, "ceiling 900 MHz applied to 32/32" if ok else "NOT verified", {}

    return apply


# --- config ---------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", " "])
def test_unset_or_invalid_ceiling_is_off(monkeypatch, raw):
    monkeypatch.setenv(ac.ENV_MHZ, raw)
    assert ac.ceiling_mhz() is None
    assert CEILING.armed() is False


def test_builtin_argv_runs_the_helper_with_a_per_chip_timeout(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    argv = ac.build_argv(900)
    assert argv[1].endswith("aiclk-ceiling.py")
    assert argv[2:] == ["--mhz", "900", "--chip-timeout", str(ac.CHIP_TIMEOUT_SEC)]


# --- apply() --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_unconfigured_runs_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned a helper with no ceiling configured")

    monkeypatch.setattr(ac, "run_probe", boom)
    assert await CEILING.apply("pre-job") == (None, "not armed", {})


@pytest.mark.asyncio
async def test_apply_ok_clears_owed_and_logs_the_count(monkeypatch, _fresh_ceiling):
    _arm(monkeypatch, _OK_CMD)
    lines = []
    ok, detail, ev = await CEILING.apply("post-job", log=lines.append)
    assert ok is True and CEILING.owed is False
    assert "ceiling 900 MHz applied to 32/32" in detail
    assert ev["chips_ok"] == 32 and ev["sent"] == 3 and "ok" not in ev
    assert lines and lines[0].startswith("aiclk-ceiling: OK")
    assert [k for k, _ in _fresh_ceiling] == ["aiclk_ceiling_applied"]


@pytest.mark.asyncio
async def test_apply_failure_keeps_it_owed(monkeypatch, _fresh_ceiling):
    _arm(monkeypatch, 'echo \'{"summary": true, "chips": 32, "ok": 31, "over": [7]}\'; exit 1')
    CEILING.owed = False
    ok, detail, ev = await CEILING.apply("post-job")
    assert ok is False and CEILING.owed is True
    assert "NOT verified" in detail and "31/32" in detail and ev["over"] == [7]
    assert _fresh_ceiling[-1][0] == "aiclk_ceiling_unverified"


@pytest.mark.asyncio
async def test_apply_timeout_kills_the_helper_and_fails(monkeypatch):
    _arm(monkeypatch, "sleep 30")
    monkeypatch.setenv(ac.ENV_TIMEOUT, "1")
    procs = []
    real_track = CEILING._track
    monkeypatch.setattr(CEILING, "_track", lambda p: (procs.append(p), real_track(p)))
    t0 = time.monotonic()
    ok, detail, _ = await CEILING.apply("pre-job")
    assert time.monotonic() - t0 < 10, "a hung helper must be bounded by the timeout"
    assert ok is False and "timed out" in detail
    await asyncio.sleep(0.2)
    stat = Path(f"/proc/{procs[0].pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z", "the helper was left running"
    assert CEILING.proc is None, "a finished helper must leave the kill path"


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", [77, 127])
async def test_apply_cannot_run_here_disarms_once(monkeypatch, _fresh_ceiling, rc):
    _arm(monkeypatch, f"echo 'not a Blackhole chip'; exit {rc}")
    ok, detail, _ = await CEILING.apply("startup")
    assert ok is None and "DISARMED" in detail
    assert CEILING.armed() is False
    assert [k for k, _ in _fresh_ceiling] == ["aiclk_ceiling_disarmed"]
    # Disarmed: never runs again in this process.
    assert (await CEILING.apply("pre-job"))[0] is None


def test_mark_owed_journals_only_the_transition(monkeypatch, _fresh_ceiling):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    CEILING.owed = False
    CEILING.mark_owed("job end")
    CEILING.mark_owed("job end")
    assert CEILING.owed is True
    assert [k for k, _ in _fresh_ceiling] == ["aiclk_ceiling_owed"]


def test_mark_owed_unconfigured_is_silent(_fresh_ceiling):
    CEILING.owed = False
    CEILING.mark_owed("reset")
    assert _fresh_ceiling == []


# --- probe pass (HealthMonitor.update) --------------------------------------------------------


def _monitor(monkeypatch, health_deps, order, pci_ok=True):
    m = HealthMonitor(health_deps)

    async def pci(expected):
        order.append("pci")
        return pci_ok, "ok" if pci_ok else "chip missing"

    async def eth():
        order.append("eth")
        return None, "not configured"

    async def fabric(*a, **k):
        order.append("fabric")
        return True, "links healthy"

    monkeypatch.setattr(m, "_verify_device", pci)
    monkeypatch.setattr(m, "verify_eth_heartbeat", eth)
    monkeypatch.setattr(m, "verify_fabric_health", fabric)
    return m


@pytest.mark.asyncio
async def test_update_unconfigured_has_no_ceiling_step(monkeypatch, health_deps):
    order = []
    m = _monitor(monkeypatch, health_deps, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))
    st = await m.update("post-job", run_fabric=True)
    assert order == ["pci", "eth", "fabric"]
    assert st.of("aiclk_ceiling") is None


@pytest.mark.asyncio
async def test_update_applies_after_the_snapshot_and_before_any_traffic(monkeypatch, health_deps):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order, lines = [], []
    m = _monitor(monkeypatch, health_deps, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [True]))
    st = await m.update("startup", run_fabric=True, log=lines.append)
    assert order == ["pci", "startup", "eth", "fabric"]
    assert st.of("aiclk_ceiling").verdict is Verdict.HEALTHY
    assert any("ceiling 900 MHz applied to 32/32" in line for line in lines)
    assert st.healthy


@pytest.mark.asyncio
async def test_update_failed_ceiling_is_unhealthy_and_runs_no_traffic(monkeypatch, health_deps):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    m = _monitor(monkeypatch, health_deps, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [False]))
    st = await m.update("post-reset", run_fabric=True, force_fabric=True)
    assert order == ["pci", "post-reset"], "the traffic pass ran above an unverified ceiling"
    assert st.of("aiclk_ceiling").verdict is Verdict.UNHEALTHY
    assert st.healthy is False, "the gate must hold (and the bounded ladder run) on an unverified ceiling"


@pytest.mark.asyncio
async def test_update_skips_the_ceiling_when_the_snapshot_failed(monkeypatch, health_deps):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    m = _monitor(monkeypatch, health_deps, order, pci_ok=False)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))
    await m.update("post-job", run_fabric=True)
    assert order == ["pci"]


# --- reset path (RecoveryMechanism.reset_with_quiesce) ----------------------------------------


def _reset_seams(monkeypatch, order, rc=0):
    async def pollers(active, log):
        order.append(f"pollers:{active}")
        return ["telem"] if not active else []

    async def run(argv, log, owner="[broker]health-gate"):
        order.append("reset")
        return rc, "out"

    async def to_thread(fn, *a, **k):
        order.append("rescan")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_reset_reapplies_after_the_rescan_before_the_pollers_return(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    _reset_seams(monkeypatch, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [True]))
    CEILING.owed = False
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert order == ["pollers:False", "reset", "rescan", "post-reset", "pollers:True"]
    assert CEILING.owed is False


@pytest.mark.asyncio
async def test_reset_failed_rung_leaves_the_ceiling_owed(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    _reset_seams(monkeypatch, order, rc=1)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))
    CEILING.owed = False
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert "post-reset" not in order, "applied to chips a failed reset may have left off the bus"
    assert CEILING.owed is True, "a reset rung clears the cap, completed or not"


@pytest.mark.asyncio
async def test_reset_apply_error_never_breaks_the_rung(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    _reset_seams(monkeypatch, order)

    async def raises(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(CEILING, "apply", raises)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc == 0 and order[-1] == "pollers:True"


@pytest.mark.asyncio
async def test_reset_unconfigured_runs_no_ceiling(monkeypatch):
    order = []
    _reset_seams(monkeypatch, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert order == ["pollers:False", "reset", "rescan", "pollers:True"]


# --- broker start, job door, job end ----------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_applies_before_the_startup_fabric_gate(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [True]))

    async def gate(*a, phase, **k):
        order.append(f"gate:{phase}")

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    await srv._verify_fabric_on_start()
    assert order == ["startup", "gate:startup"]


@pytest.mark.asyncio
async def test_startup_unconfigured_goes_straight_to_the_gate(monkeypatch):
    order = []
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))

    async def gate(*a, phase, **k):
        order.append(f"gate:{phase}")

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    await srv._verify_fabric_on_start()
    assert order == ["gate:startup"]


def _door_seams(monkeypatch, order):
    async def dispatch_ok(job_log_file=None):
        order.append("dispatch")
        return True

    dirty = []
    monkeypatch.setattr(srv, "_dispatch_probe_ok", dispatch_ok)
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, *a, **k: dirty.append(reason))
    return dirty


@pytest.mark.asyncio
async def test_door_reapplies_an_owed_ceiling_before_admitting(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    dirty = _door_seams(monkeypatch, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [True]))
    await srv._ensure_device_clean_for_next_job(None)
    assert order == ["pre-job", "dispatch"] and dirty == []


@pytest.mark.asyncio
async def test_door_verified_ceiling_costs_nothing(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    CEILING.owed = False
    order = []
    _door_seams(monkeypatch, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, []))
    await srv._ensure_device_clean_for_next_job(None)
    assert order == ["dispatch"]


@pytest.mark.asyncio
async def test_door_unverified_ceiling_flags_dirty_and_returns(monkeypatch):
    """Two failed applies: flag the device so the pre-job gate's bounded reset + verify owns it.
    The door itself returns promptly — it never loops on the ceiling or blocks the queue."""
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    dirty = _door_seams(monkeypatch, order)
    monkeypatch.setattr(CEILING, "apply", _fake_apply(order, [False, False]))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    await srv._ensure_device_clean_for_next_job(None)
    assert order == ["pre-job", "pre-job retry", "dispatch"]
    assert len(dirty) == 1 and "aiclk ceiling unverified" in dirty[0]


@pytest.mark.asyncio
async def test_door_apply_error_flags_dirty_never_raises(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    order = []
    dirty = _door_seams(monkeypatch, order)

    async def raises(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(CEILING, "apply", raises)
    await srv._ensure_device_clean_for_next_job(None)
    assert order == ["dispatch"] and len(dirty) == 1


@pytest.mark.asyncio
async def test_job_end_marks_the_ceiling_owed(monkeypatch):
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    CEILING.owed = False
    seen = []

    async def gate(*a, phase, **k):
        seen.append((phase, CEILING.owed))

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    await srv._verify_device_after_job(None)
    assert seen == [("post-job", True)]


# --- the helper (deploy/tt-device-aiclk-ceiling.py), against a fake tt-umd ---------------------


def _load_helper():
    spec = importlib.util.spec_from_file_location("aiclk_ceiling_helper", _HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_umd(arb_max: dict, arch="bh", hang=(), broken_send=()):
    """A tt_umd stand-in: chip -> AICLK_ARB_MAX; a sent cap lowers it unless the chip is in
    broken_send. Records every message sent."""
    sent = []
    umd = types.SimpleNamespace(
        ARCH=types.SimpleNamespace(BLACKHOLE="bh", WORMHOLE_B0="wh"),
        TelemetryTag=types.SimpleNamespace(AICLK_ARB_MAX="arb", __members__={"AICLK_ARB_MAX": 1}),
    )

    class Tel:
        def __init__(self, chip):
            self.chip = chip

        def is_entry_available(self, tag):
            return True

        def read_entry(self, tag):
            return 0xABCD0000 | arb_max[self.chip]

    class Dev:
        def __init__(self, chip):
            self.chip = chip

        def init_tt_device(self):
            if self.chip in hang:
                time.sleep(5)

        def get_arch(self):
            return arch

        def get_arc_telemetry_reader(self):
            return Tel(self.chip)

        def arc_msg(self, code, wait, args, timeout_ms):
            sent.append((self.chip, code, list(args)))
            if self.chip not in broken_send:
                arb_max[self.chip] = args[0]
            return (0, 0, 0)

    umd.TTDevice = types.SimpleNamespace(create=Dev)
    return umd, sent


def _run_helper(umd, chips, mhz=900, send=True, chip_timeout=2.0):
    import io

    h = _load_helper()
    out = io.StringIO()
    rc = h.run(umd, chips, mhz, send, chip_timeout, out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return rc, lines[:-1], lines[-1]


def test_helper_sends_only_to_chips_above_the_ceiling():
    umd, sent = _fake_umd({0: 1350, 1: 900, 2: 800})
    rc, recs, summary = _run_helper(umd, [0, 1, 2])
    assert rc == 0
    assert sent == [(0, 0x23, [900, 0])], "a chip already at or below the ceiling must not be sent anything"
    assert [r["after"] for r in recs] == [900, 900, 800]
    assert summary["ok"] == 3 and summary["sent"] == 1 and summary["over"] == []


def test_helper_check_mode_never_sends():
    umd, sent = _fake_umd({0: 1350})
    rc, _, summary = _run_helper(umd, [0], send=False)
    assert rc == 1 and sent == [] and summary["over"] == [0]


def test_helper_chip_that_keeps_its_clock_fails():
    umd, _ = _fake_umd({0: 1350, 1: 1350}, broken_send={1})
    rc, _, summary = _run_helper(umd, [0, 1])
    assert rc == 1 and summary["ok"] == 1 and summary["over"] == [1]


def test_helper_hung_chip_stops_at_the_per_chip_timeout():
    umd, _ = _fake_umd({0: 900, 1: 900, 2: 900}, hang={1})
    t0 = time.monotonic()
    rc, recs, summary = _run_helper(umd, [0, 1, 2], chip_timeout=0.2)
    assert time.monotonic() - t0 < 2
    assert rc == 1 and [r["chip"] for r in recs] == [0, 1] and summary["unreadable"] == [1]


def test_helper_non_blackhole_host_is_unsupported():
    umd, sent = _fake_umd({0: 1000, 1: 1000}, arch="wh")
    rc, _, _ = _run_helper(umd, [0, 1])
    assert rc == 77 and sent == []


def test_helper_no_chips_is_a_failure_not_a_pass():
    umd, _ = _fake_umd({})
    rc, _, summary = _run_helper(umd, [])
    assert rc == 1 and summary["chips"] == 0


def test_helper_without_tt_umd_is_unsupported(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "tt_umd", None)  # import raises ImportError
    assert _load_helper().main(["--mhz", "900"]) == 77
