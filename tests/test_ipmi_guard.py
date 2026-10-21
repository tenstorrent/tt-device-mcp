# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""A broker mesh reset never sends the BMC's legacy retimer reset (spec 04 I91).

tt-smi's Galaxy tray reset retries a failed op_mode 0x3 BMC reset with op_mode 0x0, which also
asserts the PCIe retimer reset and can drop every device behind it. Every mesh reset the broker
launches runs under an ipmitool guard that refuses 0x0, after a read-only BMC preflight. Device-free:
these tests assert on argv, the guard's verdicts and the ladder's state.
"""

import ast
import pathlib
import subprocess
import sys

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tests.test_post_reset_gate import F77, FOK, _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import ipmi_guard

ROOT = pathlib.Path(__file__).resolve().parents[1]
MESH = ["tt-smi", "-glx_reset"]
UBB = ["0x30", "0x8b"]
_REAL_RUN_MC_INFO = ipmi_guard._run_mc_info  # before the suite's per-test stub


def _raw(*tail):
    return ["raw", *UBB, *tail]


# --- the guard's verdict ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        _raw("0xF", "0xFF", "0x0", "0xF"),  # tt-smi's fallback, as it sends it
        _raw("0xf", "0xff", "0x00", "0xf"),
        _raw("0xf", "0xff", "0", "0xf"),
        _raw("0xf", "0xff", "00", "0xf"),
        _raw("0xf", "0xff", "0X0", "0xf"),
        _raw("0xf", "0xff", "0x000", "0xf"),
        ["RAW", "0X30", "0X8B", "0xf", "0xff", "0x0", "0xf"],
        ["raw", "48", "139", "15", "255", "0", "15"],  # decimal
        ["raw", "060", "0213", "017", "0377", "0", "017"],  # octal, as strtol base 0 reads it
        ["-I", "lanplus", "-H", "bmc", "-U", "u", "-P", "p", *_raw("0xf", "0xff", "0x0", "0xf")],
        _raw("0xf", "0xff"),  # no op_mode byte: may be 0x0
        _raw("0xf", "0xff", "zz", "0xf"),  # unreadable op_mode byte
        ["exec", "/tmp/cmds"],
        ["shell"],
    ],
)
def test_the_guard_refuses_the_legacy_retimer_reset(args):
    assert ipmi_guard.refusal(args)


@pytest.mark.parametrize(
    "args",
    [
        _raw("0xF", "0xFF", "0x3", "0xF"),  # the op_mode 0x3 tray reset tt-smi tries first
        _raw("0xf", "0xff", "0x03", "0xf"),
        ["raw", "0x30", "0x8a", "0x0", "0x0", "0x0"],  # another OEM command
        ["raw", "0x06", "0x01"],
        ["mc", "info"],
        ["sel", "list"],
        ["sdr", "elist"],
        ["chassis", "status"],
        ["fru", "print"],
    ],
)
def test_the_guard_passes_op_mode_3_and_read_only_calls(args):
    assert ipmi_guard.refusal(args) is None


def test_a_refused_call_never_execs_the_real_binary(monkeypatch, capsys):
    execs = []
    monkeypatch.setattr(ipmi_guard.os, "execv", lambda *a: execs.append(a))
    rc = ipmi_guard.main(_raw("0xf", "0xff", "0x0", "0xf"))
    assert rc == ipmi_guard.GUARD_REFUSED_RC != 0 and not execs
    assert ipmi_guard.REFUSED_MARKER in capsys.readouterr().err


def test_an_allowed_call_execs_the_real_binary_with_its_args(monkeypatch):
    execs = []

    def execv(path, argv):
        execs.append((path, argv))
        raise OSError("stub")

    monkeypatch.setattr(ipmi_guard.os, "execv", execv)
    ipmi_guard.main(_raw("0xf", "0xff", "0x3", "0xf"))
    assert execs == [
        (f"{ipmi_guard.GUARD_DIR}/{ipmi_guard.REAL_NAME}", ["ipmitool", *_raw("0xf", "0xff", "0x3", "0xf")])
    ]


def test_the_mounted_copy_refuses_as_ipmitool(tmp_path):
    """The file mounted over ipmitool is guard_source(): run it the way sudo would (under another
    name, so the suite's spawn tripwire lets it run; the guard does not read its argv[0])."""
    copy = tmp_path / "guard-copy"
    copy.write_text(ipmi_guard.guard_source())
    copy.chmod(0o755)
    res = subprocess.run([str(copy), *_raw("0xF", "0xFF", "0x0", "0xF")], capture_output=True, text=True, timeout=30)
    assert res.returncode == ipmi_guard.GUARD_REFUSED_RC
    assert ipmi_guard.REFUSED_MARKER in res.stderr
    assert ipmi_guard.GUARD_TAG in copy.read_text().splitlines()[1]


def test_nothing_switches_the_guard_off():
    """No environment variable, flag or config is read (user msgs on broker overrides): the module
    never touches os.environ except for PATH, which only widens what it guards."""
    src = (ROOT / "src/tt_device_mcp/health/recovery/ipmi_guard.py").read_text()
    tree = ast.parse(src)
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv") and isinstance(n.value, ast.Name)
    ]
    assert len(reads) == 1 and '"PATH"' in ast.get_source_segment(src, _parent_call(tree, reads[0]))


def _parent_call(tree, node):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and any(c is node for c in ast.walk(n.func)):
            return n
    raise AssertionError("no call")


# --- arming ------------------------------------------------------------------------------------------


def test_arm_mounts_the_guard_over_every_target_read_only(tmp_path):
    a, b = tmp_path / "usr-bin-ipmitool", tmp_path / "usr-sbin-ipmitool"
    mounts = []

    def mount(*args):
        mounts.append(args)
        if args[0] == "--bind" and args[1].endswith("/" + ipmi_guard.GUARD_NAME):
            pathlib.Path(args[2]).write_text(pathlib.Path(args[1]).read_text())

    guard_dir = tmp_path / "guard"
    assert ipmi_guard.arm(str(guard_dir), [str(a), str(b)], mount) == [str(a), str(b)]
    assert ("--bind", str(a), str(guard_dir / ipmi_guard.REAL_NAME)) in mounts  # the real one, first
    for t in (a, b):
        assert ("-o", "remount,bind,ro", str(t)) in mounts
        assert ipmi_guard.GUARD_TAG in t.read_text()


def test_arm_fails_closed_when_a_target_is_not_the_guard_after_mounting(tmp_path):
    t = tmp_path / "ipmitool"
    t.write_text("real binary")
    with pytest.raises(ipmi_guard.GuardNotArmed):
        ipmi_guard.arm(str(tmp_path / "g"), [str(t)], lambda *a: None)


def test_arm_fails_closed_when_a_mount_fails(tmp_path):
    def mount(*args):
        raise ipmi_guard.GuardNotArmed("mount: permission denied")

    with pytest.raises(ipmi_guard.GuardNotArmed):
        ipmi_guard.arm(str(tmp_path / "g"), [str(tmp_path / "ipmitool")], mount)


def test_the_wrapper_runs_no_reset_when_it_cannot_arm(monkeypatch, capsys):
    execs = []
    monkeypatch.setattr(ipmi_guard, "arm", lambda: (_ for _ in ()).throw(ipmi_guard.GuardNotArmed("x")))
    monkeypatch.setattr(ipmi_guard.os, "execvp", lambda *a: execs.append(a))
    assert ipmi_guard.main(["--wrap", "--", *MESH]) == 1 and not execs
    assert ipmi_guard.NOT_ARMED_MARKER in capsys.readouterr().err


def test_the_wrapper_refuses_to_mount_in_the_host_namespace(monkeypatch):
    monkeypatch.setattr(ipmi_guard.os, "readlink", lambda p: "mnt:[4026531841]")
    with pytest.raises(ipmi_guard.GuardNotArmed, match="private mount namespace"):
        ipmi_guard.arm()


# --- which argv runs under the guard -----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, mesh",
    [
        (MESH, True),
        (["/usr/local/bin/tt-smi", "-glx_reset"], True),
        (["tt-smi", "--galaxy_6u_trays_reset"], True),
        (["tt-smi", "-r", "0,1"], False),
        (["tt-smi", "-r"], False),
        (["true"], False),
        # TT_DEVICE_MCP_RESET_ARGS is a full argv: the flag counts wherever it sits (review of I91).
        (["sudo", "tt-smi", "-glx_reset"], True),
        (["sudo", "-n", "/usr/local/bin/tt-smi", "-glx_reset_auto"], True),
        (["env", "X=1", "tt-smi", "-glx_reset"], True),
        (["python3", "-m", "tt_smi", "-glx_reset"], True),
        (["/opt/site/galaxy-reset.sh", "-glx_reset"], True),
        (["bash", "-c", "'tt-smi", "-glx_reset'"], True),
        (["sudo", "tt-smi", "--GALAXY_6U_TRAYS_RESET"], True),
        (["/opt/site/galaxy-reset.sh"], False),
    ],
)
def test_is_mesh_reset(argv, mesh):
    assert ipmi_guard.is_mesh_reset(argv) is mesh


@pytest.mark.parametrize(
    "argv, classified",
    [
        (MESH, True),
        (["sudo", "tt-smi", "-glx_reset"], True),
        (["/opt/site/galaxy-reset.sh", "-glx_reset"], True),
        (["tt-smi", "-r", "0"], True),
        (["sudo", "/usr/local/bin/tt-smi", "-r"], True),
        (["python3", "-m", "tt_smi", "-r", "0"], True),
        (["python3", "/opt/tt_smi.py", "-r"], True),
        (["/opt/site/galaxy-reset.sh"], False),
        (["sudo", "/opt/site/reset"], False),
        (["/opt/tt-smi-wrapper.sh"], False),
        (["python3", "/opt/reset.py"], False),
    ],
)
def test_override_unclassified(argv, classified):
    why = ipmi_guard.override_unclassified(argv)
    assert (why == "") is classified
    if not classified:
        assert "TT_DEVICE_MCP_RESET_ARGS" in why and "cannot tell" in why


def test_the_guarded_argv_is_a_private_mount_namespace_running_the_wrapper():
    g = ipmi_guard.guarded_argv(MESH)
    assert g[:5] == ["unshare", "--mount", "--propagation", "private", "--"]
    assert g[5] == sys.executable and g[6] == "-I" and g[7].endswith("ipmi_guard.py")
    assert g[8:10] == ["--wrap", "--"] and g[10:] == MESH


def _fake_exec(seen, raise_after=False):
    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))
        if raise_after:
            raise OSError("stub: not launched")

        class P:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        return P()

    return fake_exec


@pytest.mark.asyncio
@pytest.mark.parametrize("argv, guarded", [(MESH, True), (["tt-smi", "-r", "0"], False)])
async def test_the_scope_backend_launches_a_mesh_reset_under_the_guard(monkeypatch, argv, guarded):
    seen = []
    monkeypatch.setattr(srv.recovery_mechanism, "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _fake_exec(seen))
    await srv.recovery_mechanism.run_scoped(argv, lambda m: None)
    payload = seen[0][seen[0].index("--") + 1 :]
    assert payload == (ipmi_guard.guarded_argv(argv) if guarded else argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("argv, guarded", [(MESH, True), (["tt-smi", "-r", "0"], False)])
async def test_the_local_backend_launches_a_mesh_reset_under_the_guard(monkeypatch, tmp_path, argv, guarded):
    seen = []
    monkeypatch.setattr(srv.recovery_mechanism, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "_local_reset_dir", lambda: tmp_path)
    monkeypatch.setattr(srv.recovery_mechanism, "_local_reset_lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(recovery_base.asyncio, "create_subprocess_exec", _fake_exec(seen, raise_after=True))
    await srv.recovery_mechanism.run_scoped(argv, lambda m: None)
    assert seen and seen[0] == (ipmi_guard.guarded_argv(argv) if guarded else argv)


def _calls_named(name):
    out = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name:
                out.append(f"{path.relative_to(ROOT / 'src/tt_device_mcp')}")
    return out


def test_every_reset_path_goes_through_the_guarded_backends():
    """Gate and ladder (recovery/__init__), the last-chance sweep (galaxy), the operator reset and
    the startup reset (server) all launch through reset_with_quiesce -> run_scoped, whose two
    backends wrap a mesh reset (tests above). Nothing else builds a mesh-reset argv."""
    assert sorted(_calls_named("reset_with_quiesce")) == [
        "health/recovery/__init__.py",
        "health/recovery/galaxy.py",
        "server.py",
        "server.py",
    ]
    assert sorted(_calls_named("run_scoped")) == ["health/recovery/base.py", "health/recovery/base.py"]
    flagged = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.List) and any(
                isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.startswith("-glx_reset")
                for e in n.elts
            ):
                flagged.append(path.name)
    assert flagged == ["galaxy.py"], "a mesh-reset argv is built outside galaxy.reset_argv"


# --- the BMC preflight -------------------------------------------------------------------------------

MC_INFO = "Device ID : 32\nFirmware Revision : {rev}\nAux Firmware Rev Info :\n    {aux}\n    0x00\n"


@pytest.mark.parametrize(
    "rev, aux, too_old",
    [
        ("0.05", "0x16", False),
        ("0.05", "0x22", False),
        ("0.06", "0x00", False),
        ("1.00", "0x00", False),
        ("0.05", "0x15", True),
        ("0.04", "0x30", True),
    ],
)
def test_firmware_older_than_0_05_22_is_refused(rev, aux, too_old):
    assert bool(ipmi_guard.firmware_too_old(MC_INFO.format(rev=rev, aux=aux))) is too_old


def _mc(rc=0, out=MC_INFO.format(rev="0.05", aux="0x23")):
    return lambda timeout: subprocess.CompletedProcess(["ipmitool", "mc", "info"], rc, out, "")


@pytest.mark.parametrize(
    "run, refused",
    [
        (_mc(), False),
        (_mc(rc=1, out="Error: Unable to establish IPMI v2 / RMCP+ session"), True),
        (_mc(out=MC_INFO.format(rev="0.05", aux="0x10")), True),
        (lambda timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("ipmitool", timeout)), True),
        (lambda timeout: (_ for _ in ()).throw(FileNotFoundError("ipmitool")), True),
    ],
    ids=["ok", "bmc-error", "old-firmware", "timeout", "no-ipmitool"],
)
def test_the_preflight(run, refused):
    assert bool(ipmi_guard.bmc_preflight(run)) is refused


def test_the_preflight_is_read_only_and_bounded(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, MC_INFO.format(rev="0.05", aux="0x23"), "")

    monkeypatch.setattr(ipmi_guard, "_run_mc_info", _REAL_RUN_MC_INFO)  # not the suite's stub
    monkeypatch.setattr(ipmi_guard.subprocess, "run", run)
    assert ipmi_guard.bmc_preflight() == ""
    assert seen["argv"] == ["ipmitool", "mc", "info"] and seen["timeout"] == ipmi_guard.BMC_PREFLIGHT_TIMEOUT_SEC
    assert ipmi_guard.BMC_PREFLIGHT_TIMEOUT_SEC <= 10


@pytest.fixture
def ladder(monkeypatch, tmp_path, clear_job_state):
    """A 32-chip Galaxy gate whose mesh reset runs the real reset_with_quiesce over a stubbed launch."""
    from tests.test_arc_not_ready import _fake_sysfs

    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", "tt-smi -glx_reset")
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(recovery_pkg, "ARC_NOT_READY_RETRY_SETTLE_SEC", 0)
    _fake_sysfs(monkeypatch, tmp_path / "sysfs")
    g = srv.galaxy_recovery
    m = g.mechanism
    monkeypatch.setattr(m, "scope_active", lambda: None)
    monkeypatch.setattr(m, "bmc_guard_state", None)
    monkeypatch.setattr(m, "_bmc_guard_paged", False)
    monkeypatch.setattr(m, "last_reset_failed", False)

    async def no_pollers(on, log):
        return []

    monkeypatch.setattr(m, "_set_device_pollers", no_pollers)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    pages = []
    monkeypatch.setattr(recovery_base.alert, "send_alert", lambda event: pages.append(event) or 0)
    launches = []
    fsm_dirty(srv, "gate/post-job: reset", why="job_killed")
    return {"g": g, "m": m, "events": events, "pages": pages, "launches": launches}


def _launch(monkeypatch, rig, rc, text):
    async def run_scoped(argv, log, owner="", on_output=None):
        rig["launches"].append(list(argv))
        return rc, text

    monkeypatch.setattr(rig["m"], "run_scoped", run_scoped)


@pytest.mark.asyncio
async def test_a_preflight_refusal_is_a_rung_that_did_not_fire_and_keeps_the_hold(ladder, monkeypatch):
    rig = ladder
    _launch(monkeypatch, rig, 0, "ok")

    async def refuse():
        return "the BMC did not answer `ipmitool mc info` within 10s"

    monkeypatch.setattr(rig["m"], "bmc_preflight", refuse)
    verifies = []
    _verify_returning(monkeypatch, FOK, verifies)
    out = await rig["g"]._reset_and_verify_device([str(i) for i in range(32)], lambda m: None)
    assert out is False, "the ladder climbs to its next rung"
    assert rig["launches"] == [], "no mesh reset ran"
    assert not rig["m"].last_reset_failed, "a reset that never fired arms no failed-reset cooldown"
    kinds = [k for k, _ in rig["events"]]
    assert "mesh_reset_bmc_preflight_refused" in kinds
    assert srv.fsm.state is not srv.ServerState.HEALTHY, "the hold stays"
    health = srv._health_payload()["mesh_reset_bmc_guard"]
    assert health["kind"] == "preflight_refused" and "did not answer" in health["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, kind",
    [
        ("Falling back to legacy retimer reset\n" + ipmi_guard.REFUSED_MARKER + ": ...\n", "legacy_reset_refused"),
        ("Falling back to legacy retimer reset (op_mode 0x0)\n", "legacy_reset_sent"),
    ],
)
async def test_the_fallback_never_reads_as_a_passing_reset_and_pages_once(ladder, monkeypatch, text, kind):
    rig = ladder
    _launch(monkeypatch, rig, 0, text)
    _verify_returning(monkeypatch, FOK)
    for _ in range(2):
        rc, _ = await rig["m"].reset_with_quiesce(MESH, lambda m: None)
        assert rc == 1, "tt-smi's exit 0 after the fallback is not a recovered mesh"
    attempted = [f for k, f in rig["events"] if k == "bmc_legacy_reset_attempted"]
    assert len(attempted) == 2 and all(f["host_at_risk"] for f in attempted)
    assert len(rig["pages"]) == 1, "paged once per episode"
    assert srv._health_payload()["mesh_reset_bmc_guard"]["kind"] == kind


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [ipmi_guard.NOT_ARMED_MARKER + ": x; mesh reset NOT run\n", "unshare: unshare failed: Operation not permitted\n"],
)
async def test_a_guard_that_cannot_arm_is_a_reset_that_did_not_fire(ladder, monkeypatch, text):
    rig = ladder
    _launch(monkeypatch, rig, 1, text)
    rc, _ = await rig["m"].reset_with_quiesce(MESH, lambda m: None)
    assert rc is None
    assert srv._health_payload()["mesh_reset_bmc_guard"]["kind"] == "guard_not_armed"


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence, released", [(F77, False), (FOK, True)], ids=["fabric-77", "fabric-ok"])
async def test_a_guarded_reset_still_owes_the_full_check_before_release(ladder, monkeypatch, evidence, released):
    """Goals 8/11: a clean guarded mesh reset releases only once the full check, fabric included,
    has passed after it; the guard state clears on that clean reset."""
    rig = ladder
    rig["m"].bmc_guard_state = {"kind": "preflight_refused", "reason": "x", "at": "t"}

    async def run_scoped(argv, log, owner="", on_output=None):
        FABRIC_DEBT.note_reset(" ".join(argv))
        rig["launches"].append(list(argv))
        return 0, "ok"

    monkeypatch.setattr(rig["m"], "run_scoped", run_scoped)
    verifies = []
    _verify_returning(monkeypatch, evidence, verifies)
    out = await rig["g"]._reset_and_verify_device([str(i) for i in range(32)], lambda m: None)
    assert rig["launches"] == [MESH] and verifies and all(verifies), "the full check ran, fabric included"
    if out:
        srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    _assert_settled(released)
    assert srv._health_payload()["mesh_reset_bmc_guard"] is None


OVERRIDE_MESH_FORMS = [
    "sudo tt-smi -glx_reset",
    "env TT_SMI_LOG=1 tt-smi -glx_reset",
    "python3 -m tt_smi -glx_reset",
    "/opt/site/galaxy-reset.sh -glx_reset",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("override", OVERRIDE_MESH_FORMS)
async def test_an_override_mesh_reset_in_any_form_gets_the_preflight_and_the_guard(ladder, monkeypatch, override):
    """Review of I91: a TT_DEVICE_MCP_RESET_ARGS mesh reset not starting with tt-smi ran unguarded."""
    rig = ladder
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", override)
    argv = rig["g"].reset_argv([str(i) for i in range(32)])
    assert argv == override.split()
    preflights = []

    async def preflight():
        preflights.append(True)
        return ""

    monkeypatch.setattr(rig["m"], "bmc_preflight", preflight)
    seen = []
    monkeypatch.setattr(rig["m"], "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(recovery_base.asyncio, "create_subprocess_exec", _fake_exec(seen))
    await rig["m"].reset_with_quiesce(argv, lambda m: None)
    assert preflights == [True], "the BMC preflight ran"
    payload = seen[0][seen[0].index("--") + 1 :]
    assert payload == ipmi_guard.guarded_argv(argv), "the reset ran under the guard"


@pytest.mark.asyncio
async def test_a_plain_tt_smi_override_runs_unguarded(ladder, monkeypatch):
    """`sudo tt-smi -r` never sends the BMC UBB reset: no preflight, no guard, nothing refused."""
    rig = ladder
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", "sudo tt-smi -r 0")

    async def preflight():
        raise AssertionError("no preflight for a non-mesh reset")

    monkeypatch.setattr(rig["m"], "bmc_preflight", preflight)
    seen = []
    monkeypatch.setattr(rig["m"], "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(recovery_base.asyncio, "create_subprocess_exec", _fake_exec(seen))
    await rig["m"].reset_with_quiesce(["sudo", "tt-smi", "-r", "0"], lambda m: None)
    assert seen[0][seen[0].index("--") + 1 :] == ["sudo", "tt-smi", "-r", "0"]
    assert "reset_override_unclassified" not in [k for k, _ in rig["events"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["/opt/site/galaxy-reset.sh", "sudo /opt/site/reset", "python3 /opt/reset.py"])
async def test_an_unclassifiable_override_is_refused_and_the_ladder_climbs_with_the_hold_kept(
    ladder, monkeypatch, override
):
    """Fail closed: an override the guard cannot classify never runs. The rung did not fire (rc None,
    no cooldown), the ladder climbs, and the hold lifts only after a full check (fabric included)
    passes after a reset that did run."""
    rig = ladder
    _launch(monkeypatch, rig, 0, "ok")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", override)
    preflights = []

    async def preflight():
        preflights.append(True)
        return ""

    monkeypatch.setattr(rig["m"], "bmc_preflight", preflight)
    verifies = []
    _verify_returning(monkeypatch, FOK, verifies)
    indices = [str(i) for i in range(32)]
    rc, text = await rig["m"].reset_with_quiesce(override.split(), lambda m: None)
    assert rc is None and "reset NOT run" in text
    out = await rig["g"]._reset_and_verify_device(indices, lambda m: None)
    assert out is False, "the ladder climbs to its next rung"
    assert rig["launches"] == [] and preflights == [], "nothing was launched"
    assert not rig["m"].last_reset_failed, "a reset that never fired arms no failed-reset cooldown"
    refused = [f for k, f in rig["events"] if k == "reset_override_unclassified"]
    assert refused and refused[0]["argv"] == override.split()
    assert srv.fsm.state is not srv.ServerState.HEALTHY, "the hold stays"
    health = srv._health_payload()["mesh_reset_bmc_guard"]
    assert health["kind"] == "override_unclassified" and "cannot tell" in health["reason"]
    # With a classifiable command the same hold lifts only once the full check has passed after it.
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    out = await rig["g"]._reset_and_verify_device(indices, lambda m: None)
    assert rig["launches"] == [MESH] and preflights == [True] and verifies and all(verifies)
    assert out is True
    srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    _assert_settled(True)
