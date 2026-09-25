# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""The `device` marker's own machinery (spec 09 I2/I8).

Every test here runs device-free, deliberately: this file is the CI-side proof that the
marker's gate and the two spawn lists behave, because the hardware tests they guard
(test_device_hardware.py) skip on CI and can prove nothing there. I4 forbids letting a
device-marked test be the only coverage of something provable without a device.
"""

import pytest

import tt_device_mcp.server as srv

from .conftest import _DEVICE_ONLY_SPAWN, _NEVER_SPAWN, _real_device_present, _spawn_is_forbidden


@pytest.mark.parametrize("prog", _NEVER_SPAWN)
def test_the_never_spawn_list_is_refused_even_under_the_device_marker(prog):
    """`allow_device_spawns` lifts _DEVICE_ONLY_SPAWN and nothing else.

    The whole point of two lists: a device test may reset a chip, but nothing in a pytest
    run may reboot or power-cycle the box it is running on, or leak a systemd unit past it.
    """
    assert _spawn_is_forbidden([prog, "--whatever"], allow_device_spawns=True)
    assert _spawn_is_forbidden([prog, "--whatever"], allow_device_spawns=False)


@pytest.mark.parametrize("prog", _DEVICE_ONLY_SPAWN)
def test_the_device_only_list_is_refused_unmarked_and_permitted_marked(prog):
    reason = _spawn_is_forbidden([prog, "-r", "0"], allow_device_spawns=False)
    assert reason and "@pytest.mark.device" in reason
    assert _spawn_is_forbidden([prog, "-r", "0"], allow_device_spawns=True) is None


def test_the_lists_do_not_overlap():
    """An overlap would make the marker's meaning depend on tuple order in the checker."""
    assert set(_NEVER_SPAWN).isdisjoint(_DEVICE_ONLY_SPAWN)


def test_a_forbidden_program_is_caught_anywhere_in_the_argv():
    """Basename-matched at any position: a wrapper prefix must not launder the payload."""
    assert _spawn_is_forbidden(["/usr/bin/env", "-i", "/sbin/reboot"], allow_device_spawns=True)
    assert _spawn_is_forbidden("sudo /usr/local/bin/ipmitool power cycle", allow_device_spawns=True)


def test_mutating_systemctl_stays_refused_under_the_device_marker():
    """On a broker host this would stop the real tt-device-broker out from under the suite."""
    assert _spawn_is_forbidden(["systemctl", "stop", "tt-device-broker"], allow_device_spawns=True)
    assert _spawn_is_forbidden(["systemctl", "stop", "tt-device-broker.service"], allow_device_spawns=True)


@pytest.mark.parametrize("svc", srv.DEVICE_POLLER_SERVICES)
def test_quiescing_a_declared_poller_is_permitted_only_under_the_marker(svc):
    """A real reset must land on a quiesced bus, and quiescing IS `systemctl stop` on these.

    Refusing it would leave a device test able to exercise only an unquiesced reset — the
    configuration production never runs, and the more dangerous one on real silicon.
    """
    for verb in ("stop", "start"):
        assert _spawn_is_forbidden(["systemctl", verb, svc], allow_device_spawns=True) is None
        assert _spawn_is_forbidden(["systemctl", verb, svc], allow_device_spawns=False)


def test_the_poller_exception_does_not_extend_to_other_units():
    """Scoped to the broker's own declared set, so it cannot drift into arbitrary host services."""
    assert _spawn_is_forbidden(["systemctl", "stop", "sshd.service"], allow_device_spawns=True)
    poller = srv.DEVICE_POLLER_SERVICES[0]
    # A poller named alongside a non-poller must not launder the non-poller through.
    assert _spawn_is_forbidden(["systemctl", "stop", poller, "sshd.service"], allow_device_spawns=True)


def test_a_bare_mutating_verb_naming_no_unit_is_still_refused():
    """The exception requires a named poller; an empty unit list must not read as a subset match."""
    assert _spawn_is_forbidden(["systemctl", "stop"], allow_device_spawns=True)


def test_systemd_run_is_permitted_under_the_marker_only_for_what_it_wraps():
    """The reset is issued as a transient scope, so the wrapper must pass under the marker.

    Safe because the check is token-wise over the whole argv: a systemd-run wrapping something
    from _NEVER_SPAWN is refused on that token, marker or not.
    """
    reset = ["systemd-run", "--scope", "--collect", "--unit=ttdev-reset-1-1", "--", "tt-smi", "-r", "0"]
    assert _spawn_is_forbidden(reset, allow_device_spawns=True) is None
    assert _spawn_is_forbidden(reset, allow_device_spawns=False)

    laundered = ["systemd-run", "--scope", "--", "reboot"]
    assert _spawn_is_forbidden(laundered, allow_device_spawns=True)


def test_read_only_systemctl_passes_under_both():
    """Gate paths genuinely query scope liveness; only the mutating verbs are refused."""
    argv = ["systemctl", "is-active", "tt-device-broker"]
    assert _spawn_is_forbidden(argv, allow_device_spawns=False) is None
    assert _spawn_is_forbidden(argv, allow_device_spawns=True) is None


def test_an_empty_argv_is_not_forbidden():
    assert _spawn_is_forbidden([], allow_device_spawns=False) is None


def test_the_device_gate_reads_the_real_dev_dir_not_the_sandboxed_one(monkeypatch, tmp_path):
    """_real_device_present must never consult srv.TT_DEV_DIR.

    isolate_device_state points that attribute at an empty temp dir for every unmarked test,
    so a gate that asked the module would answer "no device" on a box that has four chips —
    every device test would skip everywhere, silently, and look like it passed.
    """
    from . import conftest as ctf

    populated = tmp_path / "dev-tenstorrent"
    populated.mkdir()
    (populated / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path / "empty-and-nonexistent"))
    monkeypatch.setattr(ctf, "_REAL_TT_DEV_DIR", str(populated))
    assert _real_device_present() is True


def test_no_chips_means_no_device(monkeypatch, tmp_path):
    from . import conftest as ctf

    empty = tmp_path / "dev-tenstorrent"
    empty.mkdir()
    monkeypatch.setattr(ctf, "_REAL_TT_DEV_DIR", str(empty))
    assert _real_device_present() is False


def test_metadata_entries_are_not_chips(monkeypatch, tmp_path):
    """A live host's /dev/tenstorrent holds `by-id` alongside the chip nodes.

    Counting any entry would unseal and run every device-marked test on a box whose chips are
    all off the bus — the broker sees none there, so the tests would assert against nothing.
    """
    from . import conftest as ctf

    d = tmp_path / "dev-tenstorrent"
    (d / "by-id").mkdir(parents=True)
    monkeypatch.setattr(ctf, "_REAL_TT_DEV_DIR", str(d))
    assert _real_device_present() is False

    (d / "0").write_text("")
    assert _real_device_present() is True


def test_an_absent_dev_dir_means_no_device(monkeypatch, tmp_path):
    """The device-free case CI itself is in: a missing dir, not an empty one."""
    from . import conftest as ctf

    monkeypatch.setattr(ctf, "_REAL_TT_DEV_DIR", str(tmp_path / "nothing-here"))
    assert _real_device_present() is False


def test_the_device_marker_is_registered(pytestconfig):
    """An unregistered marker is a warning, not an error — and `-m device` would select nothing."""
    markers = pytestconfig.getini("markers")
    assert any(m.startswith("device:") for m in markers)


def test_the_opt_out_flag_exists(pytestconfig):
    assert pytestconfig.getoption("--no-device-tests") in (True, False)


def test_this_test_is_not_device_marked(device_marked):
    """The autouse gate reports False for an unmarked test, so its seals stay installed."""
    assert device_marked is False
