"""Device readers stop, verified, before every fabric check and reset path, and come back after (spec 04 I112)."""

import asyncio
import contextlib
import time

import pytest

from tests.conftest import stub_device_pollers
from tests.test_device_safety import _short_poller_bound, _systemctl_scripted, _systemctl_with_units
from tt_device_mcp import health
from tt_device_mcp import server as srv

EXPORTER = "prometheus-node-exporter.service"


def _lines():
    got: list = []
    return got, got.append


# --- the stop is verified -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_readers_name_the_node_exporter():
    """Its hwmon collector reads every chip's telemetry through the driver on each scrape."""
    assert EXPORTER in srv.DEFAULT_POLLER_SERVICES.split(",")


@pytest.mark.asyncio
async def test_a_reader_that_stops_is_verified_without_a_warning(monkeypatch):
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", (EXPORTER,))
    monkeypatch.setattr(srv, "POLLER_STOP_VERIFY_SEC", 5.0)
    calls: list = []
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _systemctl_with_units({EXPORTER}, calls))
    lines, log = _lines()

    assert await srv._set_device_pollers(False, log) == [EXPORTER]

    assert calls == [("is-active", EXPORTER), ("stop", EXPORTER), ("is-active", EXPORTER)]
    assert not [ln for ln in lines if "WARNING" in ln], lines
    assert health.read_health_events(kinds={"poller_stop_unverified", "pollers_none_active"}) == []


@pytest.mark.asyncio
async def test_a_reader_still_running_after_its_stop_is_named_with_its_pid_and_the_path_goes_on(monkeypatch):
    """`systemctl stop` returned 0 yet the unit still reads active: warned by name and pid, bounded."""
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", (EXPORTER,))
    monkeypatch.setattr(srv, "POLLER_STOP_VERIFY_SEC", 0.3)
    calls: list = []
    scripted = _systemctl_scripted(calls)

    class _Pid:
        returncode = 0

        async def communicate(self):
            return b"4242\n", b""

    async def _exec(*argv, **kw):
        if argv[1] == "show":
            calls.append(argv[1:])
            return _Pid()
        return await scripted(*argv, **kw)

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _exec)
    lines, log = _lines()

    t0 = time.monotonic()
    assert await srv._set_device_pollers(False, log) == [EXPORTER]
    assert time.monotonic() - t0 < 2.0, "the verify is bounded"

    assert ("show", "-p", "MainPID", "--value", EXPORTER) in calls
    assert sum(1 for c in calls if c[0] == "is-active") >= 2, "re-read until the bound"
    warn = [ln for ln in lines if ln.startswith("WARNING")]
    assert warn and EXPORTER in warn[0] and "pid 4242" in warn[0], lines
    events = health.read_health_events(kinds={"poller_stop_unverified"})
    assert [(e["service"], e["pid"]) for e in events] == [(EXPORTER, "4242")]
    # Still owed its start: the restore record keeps it.
    assert EXPORTER in srv._pollers_restore_record()


@pytest.mark.asyncio
async def test_a_reader_whose_stop_times_out_is_named_and_owed(monkeypatch):
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", (EXPORTER,))
    _short_poller_bound(monkeypatch)
    calls: list = []
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _systemctl_scripted(calls, stop=(0, 0.5)))
    lines, log = _lines()

    assert await srv._set_device_pollers(False, log) == []

    assert srv._pollers_owed is not None
    assert [ln for ln in lines if ln.startswith("WARNING") and EXPORTER in ln and "pid" in ln], lines
    assert [e["service"] for e in health.read_health_events(kinds={"poller_stop_unverified"})] == [EXPORTER]


# --- the fabric check ------------------------------------------------------------------------------


def _recording_quiesce(order: list):
    @contextlib.asynccontextmanager
    async def quiesce(what, log):
        order.append(("stop", what))
        try:
            yield ["x.service"]
        finally:
            order.append(("restore", what))

    return quiesce


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["pass", "fail", "timeout", "skip", "raise", "cancel"]
)
async def test_the_fabric_check_runs_with_readers_stopped_and_restores_them_on_every_exit(monkeypatch, outcome):
    order: list = []
    monkeypatch.setattr(srv._recovery_deps, "quiesce_readers", _recording_quiesce(order))
    mon = srv.health_monitor

    async def body(timeout_sec):
        order.append(("check", None))
        if outcome == "raise":
            raise RuntimeError("validator crashed")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return {
            "pass": (True, "ok"),
            "fail": (False, "link down"),
            "timeout": (False, "fabric check timed out after 1s"),
            "skip": (None, "not configured"),
        }[outcome]

    monkeypatch.setattr(mon, "_verify_fabric_health_body", body)
    if outcome in ("raise", "cancel"):
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await mon.verify_fabric_health(1)
    else:
        await mon.verify_fabric_health(1)

    assert order == [("stop", "the fabric check"), ("check", None), ("restore", "the fabric check")]


@pytest.mark.asyncio
async def test_the_fabric_check_goes_through_the_broker_quiesce(monkeypatch):
    """The wiring: the server's deps hold the check inside the same quiesce as the startup check."""
    monkeypatch.setattr(srv._recovery_deps, "quiesce_readers", lambda what, log: srv._pollers_quiesced_for(what, log))
    calls = stub_device_pollers(monkeypatch, stopped=[EXPORTER])
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    seen: list = []

    async def body(timeout_sec):
        seen.append(list(calls))
        return True, "ok"

    monkeypatch.setattr(srv.health_monitor, "_verify_fabric_health_body", body)
    await srv.health_monitor.verify_fabric_health(1)

    assert seen == [[False]], "stopped before the check"
    assert calls == [False, True], "started again after it"


@pytest.mark.asyncio
async def test_a_fabric_check_inside_the_startup_hold_does_not_stop_them_again(monkeypatch):
    calls = stub_device_pollers(monkeypatch, stopped=[EXPORTER])
    monkeypatch.setattr(srv, "_pollers_held_for", "the startup fabric check")

    async with srv._pollers_quiesced_for("the fabric check", lambda m: None) as stopped:
        assert stopped == []
    assert calls == []
    assert srv._pollers_held_for == "the startup fabric check"


@pytest.mark.asyncio
async def test_an_unstoppable_reader_never_blocks_the_fabric_check(monkeypatch):
    """A stop that raises is logged; the check still runs."""
    monkeypatch.setattr(srv._recovery_deps, "quiesce_readers", lambda what, log: srv._pollers_quiesced_for(what, log))

    async def broken(active, log):
        raise OSError("systemd unreachable")

    monkeypatch.setattr(srv, "_set_device_pollers", broken)
    ran: list = []

    async def body(timeout_sec):
        ran.append(True)
        return True, "ok"

    monkeypatch.setattr(srv.health_monitor, "_verify_fabric_health_body", body)
    assert (await srv.health_monitor.verify_fabric_health(1))[0] is True
    assert ran == [True]


# --- the bridge reset (also the last-chance sweep) ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["recovered", "not recovered", "raise", "cancel"])
async def test_the_bridge_reset_runs_with_readers_stopped_and_restores_or_owes_them(monkeypatch, outcome):
    rec = srv.galaxy_recovery
    calls = stub_device_pollers(monkeypatch, stopped=[EXPORTER])
    monkeypatch.setattr(rec.deps, "isolated_chips", lambda: {"3"})
    owed: list = []
    monkeypatch.setattr(rec.mechanism, "owe_pollers", lambda after: owed.append(after))
    seen: list = []

    async def body(log, exclude, not_reset):
        seen.append(list(calls))
        if outcome == "raise":
            raise RuntimeError("setpci failed")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return outcome == "recovered"

    monkeypatch.setattr(rec, "_recover_isolated_chips_body", body)
    if outcome in ("raise", "cancel"):
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await rec._recover_isolated_chips(lambda m: None)
    else:
        assert await rec._recover_isolated_chips(lambda m: None) is (outcome == "recovered")

    assert seen == [[False]], "stopped before the bridge write"
    if outcome == "recovered":
        assert calls == [False, True] and owed == []
    else:
        assert calls == [False] and owed == ["bridge reset"], "a step that did not complete owes them"


@pytest.mark.asyncio
async def test_the_bridge_reset_with_nothing_to_reset_touches_no_reader(monkeypatch):
    rec = srv.galaxy_recovery
    calls = stub_device_pollers(monkeypatch, stopped=[EXPORTER])
    monkeypatch.setattr(rec.deps, "isolated_chips", lambda: set())
    assert await rec._recover_isolated_chips(lambda m: None) is True
    assert calls == []


@pytest.mark.asyncio
async def test_an_unstoppable_reader_never_blocks_the_bridge_reset(monkeypatch):
    rec = srv.galaxy_recovery
    monkeypatch.setattr(rec.deps, "isolated_chips", lambda: {"3"})

    async def broken(active, log):
        raise OSError("systemd unreachable")

    monkeypatch.setattr(srv, "_set_device_pollers", broken)
    ran: list = []

    async def body(log, exclude, not_reset):
        ran.append(True)
        return True

    monkeypatch.setattr(rec, "_recover_isolated_chips_body", body)
    lines, log = _lines()
    assert await rec._recover_isolated_chips(log) is True
    assert ran == [True]
    assert [ln for ln in lines if "could not stop the device pollers" in ln], lines


# --- the power-cycle request -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["launched", "not launched", "timed out"])
async def test_a_power_cycle_request_stops_readers_before_it_fires(monkeypatch, tmp_path, case):
    from tests.test_power_cycle_host_rung import _ledger_dir
    from tests.conftest import patch_health_event
    from tt_device_mcp.health.recovery import base as recovery_base

    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    rm = srv.recovery_mechanism
    monkeypatch.setattr(rm, "host_action_requested", None)
    calls = stub_device_pollers(monkeypatch, stopped=[EXPORTER])
    owed: list = []
    monkeypatch.setattr(rm, "owe_pollers", lambda after: owed.append(after))
    restored: list = []

    async def restore(quiesced, log, *, ok, after):
        restored.append((list(quiesced), ok, after))

    monkeypatch.setattr(rm, "restore_pollers", restore)
    seen: list = []

    def fire():
        seen.append(list(calls))
        if case == "not launched":
            raise FileNotFoundError("ipmitool")
        if case == "timed out":
            raise recovery_base.HostActionTimedOut("no answer")

    await rm._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]power-cycle-request",
        label="auto BMC power cycle",
        detail="the ladder could not recover",
        fire=fire,
        log=lambda m: None,
        reason="wedge",
    )

    assert seen == [[False]], "stopped before the cycle fires"
    if case == "not launched":
        assert restored == [([EXPORTER], True, "power-cycle that did not launch")] and owed == []
    else:
        assert restored == [] and len(owed) == 1, "a cycle that may still act leaves them owed"


@pytest.mark.asyncio
async def test_an_unstoppable_reader_never_blocks_the_power_cycle_request(monkeypatch, tmp_path):
    from tests.test_power_cycle_host_rung import _ledger_dir
    from tests.conftest import patch_health_event

    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    rm = srv.recovery_mechanism
    monkeypatch.setattr(rm, "host_action_requested", None)

    async def broken(active, log):
        raise OSError("systemd unreachable")

    monkeypatch.setattr(srv, "_set_device_pollers", broken)
    fired: list = []
    got = await rm._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]power-cycle-request",
        label="auto BMC power cycle",
        detail="the ladder could not recover",
        fire=lambda: fired.append(True),
        log=lambda m: None,
        reason="wedge",
    )
    assert got is True and fired == [True]


# --- the tt-smi resets (operator reset, ladder rungs, glx_reset) keep their quiesce --------------


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "fail", "timeout"])
async def test_a_tt_smi_reset_stops_readers_and_restores_or_owes_them(monkeypatch, outcome):
    rm = srv.recovery_mechanism
    order: list = []

    async def pollers(active, log):
        order.append("start" if active else "stop")
        return [EXPORTER]

    async def reset(argv, log, owner="[broker]health-gate", on_output=None):
        order.append("reset")
        if outcome == "timeout":
            raise asyncio.TimeoutError()
        return (0 if outcome == "ok" else 1), ""

    restored: list = []
    real_restore = rm.restore_pollers

    async def restore(quiesced, log, *, ok, after):
        restored.append(ok)
        await real_restore(quiesced, log, ok=ok, after=after)

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(rm, "run_scoped", reset)
    monkeypatch.setattr(rm, "restore_pollers", restore)
    with contextlib.suppress(asyncio.TimeoutError):
        await rm.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)

    assert order[:2] == ["stop", "reset"]
    assert restored, "every exit goes through the restore"
