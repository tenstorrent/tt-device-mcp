# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The systemd unit `apply-host-config.sh` renders, exercised through its `--print-unit`
seam so no root and no host paths are involved.

The load-bearing assertion is the eth-heartbeat wiring: an operator arms the passive
eth-core heartbeat pre-read per host by setting TTDEV_ETH_HEARTBEAT_CMD in /etc/default,
and that value has to reach the broker's unit as TT_DEVICE_MCP_ETH_HEARTBEAT_CMD (the env
var server.py keys on) or the detector can never be turned on without a code change. Its
default is UNSET — no line — so wiring it changes nothing until a host opts in; the inert
case is pinned alongside so the wiring cannot start rendering a line by accident.
"""

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "apply-host-config.sh"
_REPO = _SCRIPT.parent.parent


def _render(tmp_path, defaults: dict, *, eth_cmd: str | None = None) -> str:
    """Render the unit from a sandboxed config, touching no host path. The unit-read
    fallback is pointed at a nonexistent file so the render is fully determined by
    `defaults` (+ eth_cmd), not by whatever unit this host happens to have installed."""
    conf = tmp_path / "default"
    conf.write_text("".join(f"{k}={v}\n" for k, v in defaults.items()))
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TTDEV_ETC_DEFAULT": str(conf),
        "TTDEV_UNIT_PATH": str(tmp_path / "absent.service"),
    }
    if eth_cmd is not None:
        env["TTDEV_ETH_HEARTBEAT_CMD"] = eth_cmd
    r = subprocess.run(
        ["bash", str(_SCRIPT), "--print-unit", str(_REPO)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"render failed: {r.returncode}\n{r.stderr}"
    return r.stdout


_GALAXY = {
    "TTDEV_VENV": "/opt/tt-device-broker/venv",
    "TTDEV_ROOT": "/opt/tt-device-broker",
    "TTDEV_RESET_MODE": "galaxy",
    "TTDEV_FABRIC_CHECK_CMD": "/opt/tt-device-broker/fabric-check.sh",
}


def test_eth_heartbeat_unset_renders_no_line(tmp_path):
    # The default: nothing set AND no staged reader under ROOT. The broker sees no
    # TT_DEVICE_MCP_ETH_HEARTBEAT_CMD, so verify_eth_heartbeat stays inert and the health gate is
    # unchanged. ROOT is sandboxed here because the docstring's "no host paths" promise is what makes
    # this case meaningful: against a real install the staged-reader auto-wire below fires instead,
    # which is the intended behaviour rather than an unset host.
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_ROOT=str(tmp_path)))
    assert "TT_DEVICE_MCP_ETH_HEARTBEAT_CMD" not in unit


def test_eth_heartbeat_set_reaches_the_unit(tmp_path):
    # A host that opted in: the /etc/default value must reach the unit under the name the
    # server reads, in the [Service] section (before ExecStart) so it is in the broker's env.
    # Deliberately NOT the retired eth-heartbeat-check.sh's own path (see the migration test
    # below) — this proves an arbitrary operator override survives, a distinct property.
    cmd = "/opt/tt-device-broker/my-custom-eth-check.sh"
    unit = _render(tmp_path, _GALAXY, eth_cmd=cmd)
    line = f"Environment=TT_DEVICE_MCP_ETH_HEARTBEAT_CMD={cmd}"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_eth_heartbeat_pointed_at_the_retired_wrapper_is_scrubbed(tmp_path):
    # Migration (see apply-host-config.sh, Task 11 review Important 4): a host that had this
    # auto-wired to the now-deleted eth-heartbeat-check.sh before it was retired must not keep
    # rendering it forever — arming TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN=1 there would run a
    # frozen, unmanaged copy of retired code. The render seam cannot touch the real
    # /etc/default (RENDER_ONLY), but the unit itself must still come out clean, exactly as if
    # the value had never been set.
    cmd = "/opt/tt-device-broker/eth-heartbeat-check.sh"  # $ROOT/eth-heartbeat-check.sh, exactly
    unit = _render(tmp_path, _GALAXY, eth_cmd=cmd)
    assert "TT_DEVICE_MCP_ETH_HEARTBEAT_CMD" not in unit


def test_seam_preserves_the_existing_render(tmp_path):
    # Characterization: the render seam must not disturb the other Environment= lines the
    # unit has always carried, or a dry-run test would be validating a different unit than
    # the one the installer writes.
    unit = _render(tmp_path, _GALAXY)
    assert "Environment=TT_DEVICE_MCP_RESET_MODE=galaxy" in unit
    assert "Environment=TT_DEVICE_MCP_FABRIC_CHECK_CMD=/opt/tt-device-broker/fabric-check.sh" in unit
    assert "Environment=TTDEV_FABRIC_DESCRIPTOR=" in unit
    assert "Type=notify" in unit and "WatchdogSec=300" in unit


def test_reset_floor_knobs_unset_render_no_line(tmp_path):
    # Default: neither reset-path knob set. server.py sees no floor and relift OFF, so the
    # recovery path is byte-identical to a host that never had this wiring. Pinned so the
    # seam cannot start rendering a live floor by accident.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC" not in unit
    assert "TT_DEVICE_MCP_SELFHEAL_RELIFT" not in unit


def test_reset_floor_knobs_set_reach_the_unit(tmp_path):
    # A host that opts into the floor: the /etc/default values must reach the unit under the
    # names server.py reads, before ExecStart so they are in the broker's env. Without the
    # wiring these keys are inert (nothing sources /etc/default into the broker process).
    opted = dict(_GALAXY, TTDEV_RESET_MIN_DEAD_FRAC="0.5", TTDEV_SELFHEAL_RELIFT="1")
    unit = _render(tmp_path, opted)
    floor = "Environment=TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC=0.5"
    relift = "Environment=TT_DEVICE_MCP_SELFHEAL_RELIFT=1"
    assert floor in unit
    assert relift in unit
    assert unit.index(floor) < unit.index("ExecStart=")
    assert unit.index(relift) < unit.index("ExecStart=")


def test_expected_chips_unset_renders_no_line(tmp_path):
    # Default: no per-host count. expected_chip_count() falls back to the high-water mark, exactly
    # as before this wiring. Pinned so the seam cannot start pinning a chip count by accident.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_EXPECTED_CHIPS" not in unit


def test_expected_chips_set_reaches_the_unit(tmp_path):
    # A host that pins its mesh: the /etc/default count must reach the unit under the name
    # expected_chip_count() reads, before ExecStart so it is in the broker's env — otherwise the
    # bar is derived from a high-water mark that a degraded broker-start can bake too low.
    opted = dict(_GALAXY, TTDEV_EXPECTED_CHIPS="32")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_EXPECTED_CHIPS=32"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_cold_rung_knobs_unset_render_no_line(tmp_path):
    # Default: neither cold rung armed. server.py's _auto_reboot_allowed / _auto_power_cycle_allowed
    # both read "0", so the cascade tops out at the rung below them exactly as before this wiring — no
    # host gains an automatic destructive rung. Pinned so the seam cannot start arming one by accident.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_AUTO_REBOOT" not in unit
    assert "TT_DEVICE_MCP_AUTO_POWER_CYCLE" not in unit


def test_cold_rung_knobs_set_reach_the_unit(tmp_path):
    # A host that arms the top of the cascade: the /etc/default opt-ins must reach the unit under the
    # names server.py reads, before ExecStart so they are in the broker's env — otherwise the cold
    # rung is unreachable in the deployed config and an all-off-bus mesh holds with no recovery path.
    opted = dict(_GALAXY, TTDEV_AUTO_REBOOT="1", TTDEV_AUTO_POWER_CYCLE="1")
    unit = _render(tmp_path, opted)
    reboot = "Environment=TT_DEVICE_MCP_AUTO_REBOOT=1"
    power = "Environment=TT_DEVICE_MCP_AUTO_POWER_CYCLE=1"
    assert reboot in unit
    assert power in unit
    assert unit.index(reboot) < unit.index("ExecStart=")
    assert unit.index(power) < unit.index("ExecStart=")


def test_ubb_reset_force_off_unset_renders_no_line(tmp_path):
    # The per-tray BMC reset rung is armed by default in server.py, so unset must render no line — the
    # rung stays at its code default (armed). Pinned so the seam cannot start rendering a disarm by
    # accident, which would silently drop the default-on rung fleet-wide.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_AUTO_UBB_RESET" not in unit


def test_ubb_reset_force_off_reaches_the_unit(tmp_path):
    # A host whose BMC bit order the broker has not confirmed disarms the default-on tray rung: the
    # /etc/default TTDEV_AUTO_UBB_RESET=0 must reach the unit under the name server.py reads, before
    # ExecStart so it is in the broker's env — otherwise the only way to force the rung off is to
    # hand-edit the unit, and the standard /etc/default interface cannot disarm a destructive rung.
    opted = dict(_GALAXY, TTDEV_AUTO_UBB_RESET="0")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_AUTO_UBB_RESET=0"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_prejob_dispatch_unset_renders_no_line(tmp_path):
    # Default: the pre-job dispatch probe is opted out. server.py's _prejob_dispatch_enabled() reads
    # "0", so the gate never runs the probe and admits exactly as before this wiring. Pinned so the
    # seam cannot start arming an untimed probe by accident — an untimed probe that runs slow reads as
    # a false wedge and holds the box, the reboot-class failure the opt-in default exists to prevent.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_PREJOB_DISPATCH" not in unit


def test_prejob_dispatch_set_reaches_the_unit(tmp_path):
    # A host that has timed the probe and opts in: the /etc/default value must reach the unit under the
    # name _prejob_dispatch_enabled() reads, before ExecStart so it is in the broker's env — otherwise
    # the only way to arm it is to hand-edit the unit, which the next auto-update re-render silently
    # wipes, and clause G1 ("observed running in a real job log") can never durably hold.
    opted = dict(_GALAXY, TTDEV_PREJOB_DISPATCH="1")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_PREJOB_DISPATCH=1"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_poller_services_unset_renders_no_line(tmp_path):
    # Default: no line, so server.py's built-in poller list rules.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_POLLER_SERVICES" not in unit


def test_poller_services_set_reaches_the_unit(tmp_path):
    # A host with an extra device poller opts in via /etc/default; the list must reach the unit
    # verbatim, before ExecStart, or a reset races that poller.
    pollers = "tt-telemetry.service,tt-metrics-exporter.service,tt-fmax-cap.service"
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_POLLER_SERVICES=pollers))
    line = f"Environment=TT_DEVICE_MCP_POLLER_SERVICES={pollers}"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_eth_check_python_reaches_the_unit(tmp_path):
    # The reader resolves ttexalens from this and calls it the per-box mechanism, but nothing rendered
    # it — so no host could satisfy it and the rung self-tested to OFF everywhere. Documented and
    # unreachable is the same bug as unwired.
    pinned = dict(_GALAXY, TTDEV_ETH_CHECK_PYTHON="/opt/tt/python_env/bin/python")
    unit = _render(tmp_path, pinned)
    line = "Environment=TTDEV_ETH_CHECK_PYTHON=/opt/tt/python_env/bin/python"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_eth_check_python_unset_renders_no_line(tmp_path):
    unit = _render(tmp_path, _GALAXY)
    assert "TTDEV_ETH_CHECK_PYTHON" not in unit


def test_aiclk_ceiling_unset_renders_no_line(tmp_path):
    # Default: no ceiling. ceiling_mhz() reads None, so no helper runs and no time is added to a pass.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_AICLK_CEILING" not in unit


def test_aiclk_ceiling_set_reaches_the_unit(tmp_path):
    # A host that caps its clock: the /etc/default value must reach the unit under the name
    # ceiling_mhz() reads, before ExecStart, or the broker never re-applies it after a reset.
    opted = dict(_GALAXY, TTDEV_AICLK_CEILING_MHZ="900")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_AICLK_CEILING_MHZ=900"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")
    assert "TT_DEVICE_MCP_AICLK_CEILING_CMD" not in unit


def test_the_unit_never_orders_after_the_vendor_hugepages_service(tmp_path):
    # tenstorrent-hugepages.service is After=multi-user.target and the broker is ordered before it
    # (WantedBy=), so an After= on it is a boot ordering cycle: systemd deletes the broker's (or
    # ltx-host's) start job to break it. The broker waits for the pool in-process instead.
    unit = _render(tmp_path, _GALAXY)
    deps = [ln for ln in unit.splitlines() if ln.split("=", 1)[0] in ("After", "Wants", "Requires", "BindsTo")]
    assert not [ln for ln in deps if "tenstorrent-hugepages" in ln]
    assert "Wants=dev-hugepages\\x2d1G.mount" in deps
