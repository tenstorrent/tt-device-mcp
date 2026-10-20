# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A broker reboot counts as tried for its whole episode (spec 04 I10, I65).

The chooser offered the warm reboot again once the last reboot record was 3600 s old, while the power
cycle's cooldown runs from the reboot's own boot. After two cycles that healed nothing the cooldown is
3600 s or more, so at its end the reboot was chosen again: a present-mesh wedge a reboot does not heal
looped on hourly reboots and the power cycle never fired. A reboot stamped after the last verified
release now counts until the next one; with no verified release on record, for a bounded time. No
test reaches a real reset, reboot or power cycle."""

import json
import types

import pytest

from tests.conftest import patch_health_event
from tests.test_device_safety import _ledger_dir
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy

T0 = 1_800_000_000.0
BOOT_SEC = 180  # reboot or cycle to the next boot


@pytest.fixture
def box(monkeypatch, tmp_path):
    """The real governor and ledger on a settable epoch clock, boot id and boot time; both host rungs
    opted in."""
    _ledger_dir(monkeypatch, tmp_path)
    clk = [T0]
    boot = {"id": "boot-A", "btime": T0 - 86400}
    fake_time = types.SimpleNamespace(**{n: getattr(recovery_base.time, n) for n in dir(recovery_base.time)})
    fake_time.time = lambda: clk[0]
    monkeypatch.setattr(recovery_base, "time", fake_time)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: boot["id"])
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(boot["btime"])))
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", 1800)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(srv.recovery_mechanism, "_power_cycle_backstop_paged", 0.0)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    return types.SimpleNamespace(m=srv.recovery_mechanism, clk=clk, boot=boot, dir=tmp_path)


def _choose(b, at):
    b.clk[0] = at
    return galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs())


def _fire(b, action, at, next_boot):
    """The rung the chooser picked, through the real governor and ledger, then the boot it brings."""
    b.clk[0] = at
    allowed, why = b.m.auto_recovery_allowed(action, tenant_active=False, now_epoch=at)
    assert allowed, why
    assert b.m.record_auto_recovery(action, "present mesh wedged", now_epoch=at)
    b.boot.update(id=next_boot, btime=at + BOOT_SEC)


def test_after_two_unhealed_cycles_a_failed_reboot_climbs_to_the_cycle_at_the_cooldown_end(box):
    """Two cycles heal nothing (the cooldown is now 3600 s), the chooser then tries the reboot, which
    heals nothing either. The power cycle fires at the end of the cooldown counted from the reboot's
    boot; base chose a second reboot there (the reboot record was 3780 s old) and looped hourly."""
    # The box was last verified healthy before the wedge: that is where the episode starts.
    (box.dir / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(json.dumps({"boot_id": "boot-A", "at_epoch": T0 - 600}))
    _fire(box, "power-cycle", T0, "boot-B")
    _fire(box, "power-cycle", T0 + BOOT_SEC + 1800, "boot-C")
    assert box.m.power_cycle_interval(now_epoch=T0 + 4000) == 3600
    reboot_at = T0 + 2 * BOOT_SEC + 1800 + 3600  # the cycles bar de-escalating for their interval
    assert _choose(box, reboot_at) == "reboot", "no reboot tried yet: the gentler rung first"
    _fire(box, "reboot", reboot_at, "boot-D")

    back = reboot_at + BOOT_SEC + 240  # the reboot's boot, still wedged
    assert _choose(box, back) == "power-cycle"
    allowed, why = box.m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=back)
    assert allowed is False and "3600s cooldown" in why
    until = box.m.power_cycle_cooldown(now_epoch=back)["until"]
    assert until == reboot_at + BOOT_SEC + 3600
    assert until - reboot_at > recovery_base.AUTO_RECOVERY_MIN_INTERVAL_SEC, "past the old 3600 s window"

    assert _choose(box, until) == "power-cycle", "a second reboot at the cooldown end is the loop"
    assert box.m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=until) == (True, "")
    # And it stays the cycle for the rest of the episode, however long the backoff grows.
    _fire(box, "power-cycle", until, "boot-E")
    assert _choose(box, until + 6 * 3600) == "power-cycle"


def test_a_reboot_before_the_last_verified_release_does_not_count(box):
    """A reboot a verified release followed healed the box: a later wedge is a new episode and gets
    the reboot first again. A reboot after that release counts, on whichever boot it was stamped."""
    box.m.record_auto_recovery("reboot", "wedged", now_epoch=T0)
    box.boot.update(id="boot-B", btime=T0 + BOOT_SEC)
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + 600) is True
    box.m.record_verified_release("gate/startup", now_epoch=T0 + 600)
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + 900) is False
    assert _choose(box, T0 + 900) == "reboot"
    # A new wedge on boot-B: its reboot is stamped after the release, so the next boot climbs.
    box.m.record_auto_recovery("reboot", "wedged again", now_epoch=T0 + 9000)
    box.boot.update(id="boot-C", btime=T0 + 9000 + BOOT_SEC)
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + 9000 + 5 * 3600) is True


@pytest.mark.parametrize("verified", ["missing", "garbled", "future"])
def test_with_no_usable_verified_release_a_reboot_counts_for_a_bounded_time(box, verified):
    """No verified release on record (none yet, unreadable, or stamped later than now): the episode has
    no known start, so a reboot counts for REBOOT_EPISODE_UNVERIFIED_MAX_SEC, past any cooldown, and no
    longer: an old reboot never sends every later wedge straight to the power cycle."""
    path = box.dir / recovery_base.AUTO_RECOVERY_VERIFIED
    if verified == "garbled":
        path.write_text("{not json")
    elif verified == "future":
        path.write_text(json.dumps({"boot_id": "boot-B", "at_epoch": T0 + 10**7}))
    box.m.record_auto_recovery("reboot", "wedged", now_epoch=T0)
    box.boot.update(id="boot-B", btime=T0 + BOOT_SEC)
    ceiling = recovery_base.REBOOT_EPISODE_UNVERIFIED_MAX_SEC
    assert ceiling > recovery_base.AUTO_RECOVERY_MIN_INTERVAL_SEC + recovery_base.POWER_CYCLE_BACKOFF_CAP_SEC - 1
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + 3600 + BOOT_SEC) is True
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + ceiling - 1) is True
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + ceiling) is False


def test_this_boot_and_unreadable_reboot_records_still_do_not_count(box):
    """Unchanged guards: a reboot recorded under this boot has not taken the box down; a row with no
    readable or a future stamp is skipped."""
    (box.dir / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in (
                {"action": "reboot", "boot_id": "boot-A", "at_epoch": T0 - 60},
                {"action": "reboot", "boot_id": "boot-0", "at_epoch": "garbage"},
                {"action": "reboot", "boot_id": "boot-0", "at_epoch": T0 + 3600},
            )
        )
    )
    assert box.m.reboot_tried_this_episode(now_epoch=T0) is False
    assert _choose(box, T0) == "reboot"


async def _fire_cycle_that_cannot_launch(b, monkeypatch, at, exc):
    """The real escalation body with a fire that raises: the cycle's ledger row is taken back."""
    b.clk[0] = at
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    def fire():
        raise exc

    await b.m._fire_recovery_escalation(
        "power-cycle", row_owner="[broker]power-cycle-request", label="auto BMC power cycle",
        detail="reset+reboot could not recover", fire=fire, log=lambda _m: None, reason="wedge",
    )  # fmt: skip
    assert not any(r.get("action") == "power-cycle" for r in b.m.read_auto_recovery_ledger() or [])


async def test_a_power_cycle_that_cannot_launch_gets_one_fallback_reboot_per_episode(box, monkeypatch):
    """The cycle is opted in but cannot launch (its command is missing or exits non-zero), so its row
    is taken back. The reboot comes back once in the episode, under its own interval, so the hold
    keeps a host rung that can run; after that second reboot the episode offers the cycle only."""
    (box.dir / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(json.dumps({"boot_id": "boot-A", "at_epoch": T0 - 600}))
    _fire(box, "reboot", T0, "boot-B")
    back = T0 + BOOT_SEC + 240
    assert _choose(box, back) == "power-cycle"
    await _fire_cycle_that_cannot_launch(box, monkeypatch, back, FileNotFoundError("ipmitool"))

    assert _choose(box, back) == "reboot", "no runnable host rung without the fallback"
    allowed, why = box.m.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=back)
    assert allowed is False, "the fallback reboot keeps its own interval"
    fallback_at = T0 + recovery_base.AUTO_RECOVERY_MIN_INTERVAL_SEC
    assert _choose(box, fallback_at) == "reboot"
    _fire(box, "reboot", fallback_at, "boot-C")
    box.m.power_cycle_unlaunched_at = None  # the broker starts fresh on the new boot

    again = fallback_at + BOOT_SEC + 240
    assert _choose(box, again) == "power-cycle"
    await _fire_cycle_that_cannot_launch(box, monkeypatch, again, RuntimeError("bmc unreachable"))
    assert _choose(box, again + 2 * 3600) == "power-cycle", "one fallback reboot per episode, not hourly"
    # A verified release ends the episode: the next wedge gets the reboot first again.
    box.m.record_verified_release("gate/startup", now_epoch=again + 3 * 3600)
    assert _choose(box, again + 4 * 3600) == "reboot"


async def test_a_cycle_launch_that_timed_out_gives_no_fallback_reboot(box, monkeypatch):
    """A timed-out launch may still cycle the host, so it is not a cycle that cannot launch: the
    chooser keeps the cycle and no reboot is offered under it."""
    _fire(box, "reboot", T0, "boot-B")
    back = T0 + BOOT_SEC + 240
    await _fire_cycle_that_cannot_launch(box, monkeypatch, back, recovery_base.HostActionTimedOut("no answer"))
    assert box.m.power_cycle_unlaunched_at is None
    assert _choose(box, T0 + 2 * 3600) == "power-cycle"


def test_a_release_verified_on_this_boot_ends_the_episode_whatever_the_stamps(box):
    """A release verified on the current boot came after every earlier-boot reboot. A clock that
    stepped back across the boot (release stamped before the reboot) must not keep that reboot
    counting for the rest of the boot."""
    box.m.record_auto_recovery("reboot", "wedged", now_epoch=T0)
    box.boot.update(id="boot-B", btime=T0 + BOOT_SEC)
    box.m.record_verified_release("gate/startup", now_epoch=T0 - 900)
    assert box.m.reboot_tried_this_episode(now_epoch=T0 + 600) is False
    assert _choose(box, T0 + 600) == "reboot"
