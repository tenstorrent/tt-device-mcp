# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The bare-metal device lock (spec 05 I9): a lock that holds on every node, is checked without
opening a chip, names the people it does not stop, comes in only where a host opts in, and touches
no recovery path. No hardware: udev, the group database and stat are stubbed or modelled."""

import ast
import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tt_device_mcp import cli, device_lock
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState

_REPO = Path(__file__).resolve().parent.parent
_RULE = _REPO / "deploy" / "99-tenstorrent-ttdev.rules"
_LOCK_SH = _REPO / "deploy" / "tt-device-lock.sh"
_APPLY = _REPO / "deploy" / "apply-host-config.sh"
_SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_REAL_RUN = subprocess.run


def _rule_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# --- item 1: the rule wins over any later rule -------------------------------------------------


def test_the_rule_assigns_group_and_mode_final():
    for text in (_RULE.read_text(), cli._UDEV_RULE):
        lines = _rule_lines(text)
        assert len(lines) == 2
        for ln in lines:
            assert 'GROUP:="ttdev"' in ln and 'MODE:="0660"' in ln, ln
            assert not re.search(r'\b(GROUP|MODE)="', ln), ln


def _udev_resolve(rules_dir: Path, kernel: str = "tenstorrent!0") -> dict:
    """A model of udev's rule evaluation for one tenstorrent node: files in lexical order of their
    names, each matching line's GROUP/MODE applied in order; ``=`` sets unless a ``:=`` already
    made the key final. Stands in for udevadm on a box with no device."""
    out = {"GROUP": "root", "MODE": "0600"}
    final: set = set()
    for path in sorted(rules_dir.iterdir(), key=lambda p: p.name):
        for ln in _rule_lines(path.read_text()):
            matches = re.findall(r'(SUBSYSTEM|KERNEL)=="([^"]*)"', ln)
            if not matches or not all(
                (v == "tenstorrent") if k == "SUBSYSTEM" else fnmatch.fnmatch(kernel, v) for k, v in matches
            ):
                continue
            for key, op, val in re.findall(r'\b(GROUP|MODE)(:?=)"([^"]*)"', ln):
                if key in final:
                    continue
                out[key] = val
                if op == ":=":
                    final.add(key)
    return out


def test_a_later_kmd_rule_cannot_reopen_the_node(tmp_path):
    rules = tmp_path / "rules.d"
    rules.mkdir()
    (rules / "99-tenstorrent-ttdev.rules").write_text(_RULE.read_text())
    # The kmd's own rule: sorts after 99-* ('u' > '9') and opens the node to the world.
    (rules / "udev-50-tenstorrent.rules").write_text('SUBSYSTEM=="tenstorrent", MODE="0666"\n')
    assert _udev_resolve(rules) == {"GROUP": "ttdev", "MODE": "0660"}
    # The model is not vacuous: a plain '=' rule loses to the later 0666.
    (rules / "99-tenstorrent-ttdev.rules").write_text(_RULE.read_text().replace(":=", "="))
    assert _udev_resolve(rules)["MODE"] == "0666"


# --- item 2: locked means every node ---------------------------------------------------------


def _nodes(tmp_path, modes: dict) -> list:
    out = []
    for name, mode in modes.items():
        p = tmp_path / name
        p.write_text("")
        os.chmod(p, mode)
        out.append(str(p))
    return out


def test_device_locked_checks_every_node(tmp_path, monkeypatch):
    # The lock group is this test's own gid, so a 0660 node reads as locked without root.
    monkeypatch.setattr(device_lock.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid(), gr_mem=[]))
    nodes = _nodes(tmp_path, {"0": 0o660, "1": 0o660, "2": 0o660, "3": 0o666})
    monkeypatch.setattr(cli, "device_nodes", lambda: nodes)
    assert cli._device_locked() is False  # node 0 is locked; node 3 is world-rw
    os.chmod(nodes[3], 0o660)
    assert cli._device_locked() is True
    assert device_lock.node_drift(nodes) == []


# The shell helper, run in a sandbox: stat, udevadm, getent, gpasswd and groupadd are stubs on PATH.
_STUBS = {
    # `stat -c '%a %G' <node>`: the node's "<mode> <group>" from the fake-modes dir.
    "stat": 'f="$FAKE_MODES/$(basename "${@: -1}")"; [ -e "$f" ] && cat "$f" || exit 1\n',
    "udevadm": (
        'echo "udevadm $*" >> "$CALLS"\n'
        'if [ "$1" = trigger ] && [ "${HEAL:-0}" = 1 ]; then for f in "$FAKE_MODES"/*; do echo "660 ttdev" > "$f"; done; fi\n'
    ),
    "getent": (
        'case "$1" in\n'
        '  group) echo "ttdev:x:990:${FAKE_MEMBERS:-}" ;;\n'
        '  passwd) grep "^$2:" "$FAKE_PASSWD" || exit 2 ;;\n'
        "esac\n"
    ),
    "gpasswd": 'echo "gpasswd $*" >> "$CALLS"\n',
    "groupadd": 'echo "groupadd $*" >> "$CALLS"\n',
}


class _Sandbox:
    def __init__(self, tmp_path: Path, modes: dict, members: str = "", applied: bool = False, heal: bool = False):
        self.dev = tmp_path / "dev"
        self.dev.mkdir()
        (self.dev / "by-id").mkdir()
        self.fake = tmp_path / "modes"
        self.fake.mkdir()
        for name, st in modes.items():
            (self.dev / name).write_text("")
            (self.fake / name).write_text(st + "\n")
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        if applied:
            (self.run_dir / "device_lock_applied").write_text("1\n")
        self.health = tmp_path / "health"
        self.health.mkdir()
        self.rules = tmp_path / "rules.d"
        self.rules.mkdir()
        self.calls = tmp_path / "calls"
        self.calls.write_text("")
        passwd = tmp_path / "passwd"
        passwd.write_text("alice:x:1001:1001::/home/alice:/bin/bash\nsvc:x:998:998::/:/usr/sbin/nologin\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in _STUBS.items():
            (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body)
            os.chmod(bin_dir / name, 0o755)
        self.env = {
            "PATH": f"{bin_dir}:{_SYS_PATH}",
            "TTDEV_DEV_DIR": str(self.dev),
            "TTDEV_RUN_DIR": str(self.run_dir),
            "TTDEV_UDEV_RULES_DIR": str(self.rules),
            "TT_DEVICE_MCP_HEALTH_DIR": str(self.health),
            "FAKE_MODES": str(self.fake),
            "FAKE_PASSWD": str(passwd),
            "FAKE_MEMBERS": members,
            "CALLS": str(self.calls),
            "HEAL": "1" if heal else "0",
        }

    def run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(_LOCK_SH), *args], env=self.env, capture_output=True, text=True, timeout=30)

    def calls_list(self) -> list:
        return [ln for ln in self.calls.read_text().splitlines() if ln]

    def events(self) -> list:
        f = self.health / "health_events.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []


def test_check_retriggers_and_records_persistent_drift(tmp_path):
    sb = _Sandbox(tmp_path, {"0": "660 ttdev", "1": "666 root"}, applied=True)
    r = sb.run("check")
    assert r.returncode == 0, r.stderr
    assert "udevadm trigger --subsystem-match=tenstorrent" in sb.calls_list()
    assert "drift persists" in r.stderr and "/1" in r.stderr
    ev = sb.events()
    assert [e["kind"] for e in ev] == ["device_lock_drift"]
    assert ev[0]["nodes"] == [str(sb.dev / "1")] and ev[0]["source"] == "reconcile"
    rec = json.loads((sb.run_dir / "device_lock_drift").read_text())
    assert rec["nodes"] == [str(sb.dev / "1")]
    # The next pass of the timer re-triggers again but journals the same episode only once.
    sb.run("check")
    assert len(sb.events()) == 1
    assert sum(c.startswith("udevadm trigger") for c in sb.calls_list()) == 2
    # It never unlocks: no rule removal, no group or mode change of its own.
    assert not any(c.startswith(("gpasswd", "groupadd")) for c in sb.calls_list())


def test_check_heals_drift_without_an_event(tmp_path):
    sb = _Sandbox(tmp_path, {"0": "660 ttdev", "1": "666 root"}, applied=True, heal=True)
    r = sb.run("check")
    assert r.returncode == 0, r.stderr
    assert "udevadm trigger --subsystem-match=tenstorrent" in sb.calls_list()
    assert sb.events() == []
    assert not (sb.run_dir / "device_lock_drift").exists()


def test_check_never_triggers_before_the_lock_is_in_effect(tmp_path):
    sb = _Sandbox(tmp_path, {"0": "666 root", "1": "666 root"})
    r = sb.run("check")
    assert r.returncode == 0, r.stderr
    assert sb.calls_list() == [] and sb.events() == []
    assert "not yet in effect" in r.stderr
    assert "not yet in effect" not in sb.run("check").stderr  # logged once, not every pass
    for f in sb.fake.iterdir():
        f.write_text("660 ttdev\n")
    sb.run("check")
    assert (sb.run_dir / "device_lock_applied").exists()


def test_check_reports_members(tmp_path):
    sb = _Sandbox(tmp_path, {"0": "660 ttdev"}, members="alice,svc,ghost")
    r = sb.run("check")
    assert "lock not effective for alice" in r.stderr
    assert "svc" not in r.stderr and "ghost" not in r.stderr  # service account; no passwd entry
    assert "alice" not in sb.run("check").stderr  # named once per change
    assert not any(c.startswith("gpasswd") for c in sb.calls_list())  # check reports, never edits


def test_apply_empties_the_group_and_installs_the_rule(tmp_path):
    sb = _Sandbox(tmp_path, {"0": "666 root"}, members="alice,svc")
    r = sb.run("apply", str(_REPO / "deploy"))
    assert r.returncode == 0, r.stderr
    calls = sb.calls_list()
    assert "gpasswd -d alice ttdev" in calls
    assert not any("svc" in c for c in calls)
    assert "udevadm control --reload-rules" in calls
    assert not any(c.startswith("udevadm trigger") for c in calls)  # the broker flips, between jobs
    assert (sb.rules / "99-tenstorrent-ttdev.rules").read_text() == _RULE.read_text()


# --- item 3: self-checks open no chip -----------------------------------------------------------


def _stub_cmd_lock(monkeypatch, tmp_path, nodes, deny_rc):
    monkeypatch.setattr(cli, "_require_root_broker", lambda: True)
    monkeypatch.setattr(cli, "_set_service_device_group", lambda enable: True)
    monkeypatch.setattr(cli, "_install_smi_ro_wrapper", lambda: None)
    monkeypatch.setattr(cli, "_remove_smi_ro_wrapper", lambda: None)
    monkeypatch.setattr(cli, "device_nodes", lambda: nodes)
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cli, "_UDEV_RULE_PATH", str(tmp_path / "99-ttdev.rules"))
    monkeypatch.setattr(cli, "_LOCK_BANNER", str(tmp_path / "banner.sh"))
    monkeypatch.setattr(device_lock, "APPLIED_MARKER", str(tmp_path / "device_lock_applied"))
    monkeypatch.setattr(device_lock, "DRIFT_RECORD", str(tmp_path / "device_lock_drift"))
    monkeypatch.setattr(device_lock, "human_members", lambda group="ttdev": [])
    issued = []

    def fake_run(cmd, *a, **k):
        issued.append(cmd)
        if cmd[:1] == ["systemd-run"]:
            return SimpleNamespace(returncode=0 if f"--gid={cli._LOCK_GROUP}" in cmd else deny_rc)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return issued


def test_lock_probes_use_access_on_every_node_and_never_open(tmp_path, monkeypatch):
    nodes = [f"/dev/tenstorrent/{i}" for i in range(3)]
    issued = _stub_cmd_lock(monkeypatch, tmp_path, nodes, deny_rc=13)
    assert cli.cmd_lock(object()) == 0
    probes = [c for c in issued if c[:1] == ["systemd-run"]]
    assert len(probes) == 2
    assert {("--gid=ttdev" in c) for c in probes} == {True, False}
    for c in probes:
        assert "--uid=nobody" in c
        payload = c[-1]
        assert "os.access" in payload and "open(" not in payload
        assert all(n in payload for n in nodes)
    assert Path(device_lock.APPLIED_MARKER).exists()

    # The payload's verdicts, run here as this user against files it can and cannot reach.
    ok = _nodes(tmp_path, {"a": 0o660, "b": 0o660})
    shut = _nodes(tmp_path, {"c": 0o000})

    def rc(paths, quantifier):
        # The real run: cli.subprocess.run (the same module's) is stubbed above.
        return _REAL_RUN([sys.executable, "-c", cli._ACCESS_PROBE % (paths, quantifier)]).returncode

    assert rc(ok, "all") == 0 and rc(ok, "any") == 0
    assert rc(ok + [str(tmp_path / "gone")], "all") == 1  # no verdict, not a denial
    if os.geteuid() != 0:  # root passes every access() check
        assert rc(ok + shut, "all") == 13
        assert rc(shut, "any") == 13
        assert rc(ok + shut, "any") == 0  # one reachable node is a leak


def test_lock_reports_unverified_when_the_probe_has_no_verdict(tmp_path, monkeypatch, capsys):
    _stub_cmd_lock(monkeypatch, tmp_path, ["/dev/tenstorrent/0"], deny_rc=1)
    assert cli.cmd_lock(object()) != 0
    out = capsys.readouterr().out
    assert "UNVERIFIED" in out and "bare-metal denied." not in out


# --- item 4: the group must be empty ------------------------------------------------------------


def test_human_members_skips_system_accounts(monkeypatch):
    monkeypatch.setattr(
        device_lock.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=990, gr_mem=["alice", "svc", "ghost"])
    )
    uids = {"alice": 1001, "svc": 998}

    def getpwnam(name):
        if name not in uids:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=uids[name])

    monkeypatch.setattr(device_lock.pwd, "getpwnam", getpwnam)
    assert device_lock.human_members() == ["alice"]
    assert device_lock.not_effective_lines(["alice"]) == ["lock not effective for alice"]


def test_lock_names_members_it_is_not_effective_for(tmp_path, monkeypatch, capsys):
    _stub_cmd_lock(monkeypatch, tmp_path, ["/dev/tenstorrent/0"], deny_rc=13)
    monkeypatch.setattr(device_lock, "human_members", lambda group="ttdev": ["alice"])
    assert cli.cmd_lock(object()) == 0
    assert "lock not effective for alice" in capsys.readouterr().out


# --- item 5: opt-in via TTDEV_LOCK --------------------------------------------------------------


def _render(tmp_path, defaults: dict, unit_text: str | None = None) -> str:
    conf = tmp_path / "default"
    conf.write_text("".join(f"{k}={v}\n" for k, v in defaults.items()))
    unit = tmp_path / "existing.service"
    if unit_text is not None:
        unit.write_text(unit_text)
    env = {
        "PATH": _SYS_PATH,
        "TTDEV_ETC_DEFAULT": str(conf),
        "TTDEV_UNIT_PATH": str(unit),
        "TTDEV_DMI_PRODUCT_FILE": str(tmp_path / "absent.product_name"),
    }
    r = subprocess.run(
        ["bash", str(_APPLY), "--print-unit", str(_REPO)], env=env, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _base(tmp_path) -> dict:
    return {"TTDEV_VENV": str(tmp_path / "venv"), "TTDEV_ROOT": str(tmp_path / "root")}


_GROUP_LINE = "Environment=TT_DEVICE_MCP_DEVICE_GROUP=ttdev"
_LOCK_LINE = "Environment=TT_DEVICE_MCP_DEVICE_LOCK=1"


def test_lock_opt_in_renders_both_unit_lines(tmp_path):
    lines = _render(tmp_path, {**_base(tmp_path), "TTDEV_LOCK": "1"}).splitlines()
    assert lines.count(_GROUP_LINE) == 1 and lines.count(_LOCK_LINE) == 1


def test_lock_off_renders_no_lock_lines(tmp_path):
    unset = _render(tmp_path, _base(tmp_path))
    zero = _render(tmp_path, {**_base(tmp_path), "TTDEV_LOCK": "0"})
    for unit in (unset, zero):
        assert _GROUP_LINE not in unit and "TT_DEVICE_MCP_DEVICE_LOCK" not in unit
    assert unset == zero  # 0 is exactly today's host


def test_lock_off_preserves_an_existing_group_line(tmp_path):
    # A host an operator locked by hand (`tt-device-mcp lock`) keeps that lock across an update.
    unit = _render(tmp_path, _base(tmp_path), unit_text=f"[Service]\n{_GROUP_LINE}\n")
    assert _GROUP_LINE in unit.splitlines() and "TT_DEVICE_MCP_DEVICE_LOCK" not in unit


def test_apply_runs_only_under_the_opt_in():
    text = _APPLY.read_text()
    assert re.search(r'^LOCK="\$\{TTDEV_LOCK:-0\}"$', text, re.M)
    render_exit = text.index('[ -n "$RENDER_ONLY" ] && exit 0')
    calls = [m.start() for m in re.finditer(r"device-lock\.sh\" apply", text)]
    assert len(calls) == 1 and calls[0] > render_exit  # never under the dry-run render
    block = re.search(r'if \[ "\$LOCK" = 1 \]; then\n(.*?)\nfi\n', text[render_exit:], re.S)
    assert block and 'device-lock.sh" apply' in block.group(1)
    # Nothing else in the script calls the helper's apply, restarts the broker, or triggers udev.
    assert "systemctl restart tt-device-broker" not in text
    assert "udevadm trigger" not in text


# --- item 5: the broker's flip, between jobs ----------------------------------------------------


@pytest.fixture
def flip_env(tmp_path, monkeypatch):
    """The broker side with a healthy, idle device, its markers in tmp and its udev call stubbed:
    ``state['heal']`` decides whether the trigger locks the nodes."""
    state = {"drift": ["/dev/tenstorrent/1"], "heal": True, "flips": 0, "events": []}
    monkeypatch.setattr(device_lock, "APPLIED_MARKER", str(tmp_path / "device_lock_applied"))
    monkeypatch.setattr(device_lock, "DRIFT_RECORD", str(tmp_path / "device_lock_drift"))
    monkeypatch.setattr(device_lock, "device_nodes", lambda: ["/dev/tenstorrent/0", "/dev/tenstorrent/1"])
    monkeypatch.setattr(device_lock, "node_drift", lambda nodes: list(state["drift"]))

    def fake_flip(run=None):
        state["flips"] += 1
        if state["heal"]:
            state["drift"] = []
        return list(state["drift"])

    monkeypatch.setattr(device_lock, "flip", fake_flip)
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: state["events"].append((kind, f)))
    monkeypatch.setattr(srv, "fsm", SimpleNamespace(state=ServerState.HEALTHY))
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    return state


def _step():
    return asyncio.run(srv._device_lock_step())


def test_flip_waits_for_a_running_job(flip_env, monkeypatch):
    monkeypatch.setattr(srv, "jobs", {"j1": SimpleNamespace(status=srv.JobStatus.RUNNING)})
    assert _step() == "waiting" and flip_env["flips"] == 0
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "current_job_id", "j2")
    assert _step() == "waiting" and flip_env["flips"] == 0
    # A recovery in flight is left alone too.
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv, "fsm", SimpleNamespace(state=ServerState.RECOVERING))
    assert _step() == "waiting" and flip_env["flips"] == 0


def test_flip_waits_for_a_readopted_scope(flip_env, monkeypatch):
    monkeypatch.setattr(srv, "readopted_scopes", {"j0": "ttdev-job-j0.scope"})
    assert _step() == "waiting" and flip_env["flips"] == 0


def test_flip_marks_the_lock_in_effect(flip_env):
    assert _step() == "locked" and flip_env["flips"] == 1
    assert Path(device_lock.APPLIED_MARKER).exists()
    assert [k for k, _ in flip_env["events"]] == ["device_lock_applied"]
    # Already locked: no trigger at all.
    assert _step() == "locked" and flip_env["flips"] == 1


def test_flip_records_drift_once_and_never_unlocks(flip_env, monkeypatch):
    flip_env["heal"] = False
    unlocked = []
    monkeypatch.setattr(cli, "cmd_unlock", lambda *a: unlocked.append(a))
    assert _step() == "drift"
    assert _step() == "drift"
    assert [k for k, _ in flip_env["events"]] == ["device_lock_drift"]
    assert flip_env["events"][0][1]["nodes"] == ["/dev/tenstorrent/1"]
    assert device_lock.read_drift_record()["source"] == "broker"
    assert not Path(device_lock.APPLIED_MARKER).exists() and unlocked == []
    # The loop stops on drift rather than re-triggering without bound.
    monkeypatch.setattr(srv, "_DEVICE_LOCK_POLL_SEC", 0)
    asyncio.run(asyncio.wait_for(srv._device_lock_loop(), timeout=5))
    assert flip_env["flips"] == 3


def test_health_shows_drift_and_members(tmp_path, monkeypatch):
    monkeypatch.setattr(device_lock, "DRIFT_RECORD", str(tmp_path / "device_lock_drift"))
    monkeypatch.setattr(device_lock, "device_nodes", lambda: ["/dev/tenstorrent/0", "/dev/tenstorrent/1"])
    monkeypatch.setattr(device_lock, "node_drift", lambda nodes: ["/dev/tenstorrent/1"])
    monkeypatch.setattr(device_lock, "human_members", lambda group="ttdev": ["alice"])
    assert device_lock.health_view({}) is None  # never configured: /health unchanged
    device_lock.record_drift(["/dev/tenstorrent/1"], source="reconcile")
    env = {"TT_DEVICE_MCP_DEVICE_GROUP": "ttdev", "TT_DEVICE_MCP_DEVICE_LOCK": "1"}
    view = device_lock.health_view(env)
    assert view["opted_in"] is True and view["locked"] is False and view["nodes"] == 2
    assert view["drift"] == ["/dev/tenstorrent/1"]
    assert view["not_effective"] == ["lock not effective for alice"]
    assert view["drift_event"]["source"] == "reconcile"
    # And the broker's /health body carries it.
    monkeypatch.setattr(device_lock, "health_view", lambda env=None: view)
    assert srv._health_payload()["device_lock"] == view
    monkeypatch.setattr(device_lock, "health_view", lambda env=None: None)
    assert "device_lock" not in srv._health_payload()


# --- item 6: nothing in recovery changes ---------------------------------------------------------

# The only places a command is wrapped in the privsep scope: the tenant's own job and exec, and the
# job's output-dir probe. Recovery (ladder, glx_reset, power cycle, startup check, full health check
# with fabric) runs as the broker, outside any job scope, and the lock never changes that.
_PRIVSEP_WRAPS = {"_job_output_dir_problems", "_job_runner_loop", "_exec_impl"}


def _innermost_users(tree, name: str) -> set:
    found = set()

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                if isinstance(child, ast.Name) and child.id == name:
                    found.add(owner)
                visit(child, owner)

    visit(tree, None)
    return found


def test_privsep_wraps_no_recovery_path():
    src = _REPO / "src" / "tt_device_mcp"
    tree = ast.parse((src / "server.py").read_text())
    assert _innermost_users(tree, "privsep_prefix_for") == _PRIVSEP_WRAPS
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    recovery = {
        f
        for f in defined
        if re.search(r"ladder|power_cycle|glx_reset|startup_verify|full_check|_reset_device$|health_gate", f)
    }
    assert {"_reset_device", "_full_check_gate", "device_health_gate"} <= recovery
    assert not recovery & _PRIVSEP_WRAPS
    # No recovery or health module, nor the lock itself, reaches for the privsep wrapper.
    for path in [*sorted((src / "health").rglob("*.py")), src / "device_lock.py"]:
        text = path.read_text()
        assert "privsep_prefix_for" not in text and "from tt_device_mcp.privsep" not in text, path
    # The lock's broker side never resets, power-cycles or holds: it only asks udev to re-apply.
    lock_fns = {"_device_lock_blocker", "_device_lock_step", "_device_lock_loop"}
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        if fn.name in lock_fns:
            called = {
                c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
                for c in ast.walk(fn)
                if isinstance(c, ast.Call)
            }
            assert not {c for c in called if re.search(r"reset|power_cycle|ladder|hold|privsep", c)}, (fn.name, called)
