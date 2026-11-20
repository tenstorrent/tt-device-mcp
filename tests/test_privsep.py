# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for privilege-separation command construction (no root/systemd needed)."""

from tt_device_mcp import privsep


def test_systemd_run_prefix_cooperative_keeps_user_gid():
    # No device_group (cooperative): run with the user's own gid, no -p props.
    argv = privsep.systemd_run_prefix(uid=1000, gid=1000)
    assert argv[0] == "systemd-run"
    assert "--scope" in argv and "--collect" in argv
    assert "--uid=1000" in argv and "--gid=1000" in argv
    assert "-p" not in argv


def test_systemd_run_prefix_lockdown_uses_device_gid_and_keeps_groups():
    # Lockdown: the device group is the primary gid (DAC opens the 0660 node) and the submitter's
    # supplementary groups survive. A scope rejects -p SupplementaryGroups and systemd-run --uid
    # clears the groups, so setpriv does the drop.
    argv = privsep.systemd_run_prefix(uid=1000, gid=1000, device_group="ttdev", unit="u.scope", groups=[27, 1000, 1500])
    assert argv[:5] == ["systemd-run", "--scope", "--quiet", "--collect", "--unit=u.scope"]
    assert argv[5:] == ["--", "setpriv", "--reuid=1000", "--regid=ttdev", "--groups=27,1000,1500"]
    assert "-p" not in argv and "--uid=1000" not in argv


def test_lockdown_prefix_for_passes_the_submitters_groups(monkeypatch):
    monkeypatch.setattr(privsep, "should_privsep", lambda env=None: True)
    monkeypatch.setattr(privsep, "_gid_for_uid", lambda uid: 1000)
    monkeypatch.setattr(privsep, "_groups_for_uid", lambda uid: [27, 1000])
    argv = privsep.privsep_prefix_for(1000, env={"TT_DEVICE_MCP_DEVICE_GROUP": "ttdev"})
    assert argv[-1] == "--groups=27,1000"


def test_groups_for_uid_includes_primary_and_supplementary(monkeypatch):
    monkeypatch.setattr(privsep.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "u", "pw_gid": 1000})())
    monkeypatch.setattr(privsep.os, "getgrouplist", lambda name, gid: [1000, 27, 27, 44])
    assert privsep._groups_for_uid(1000) == [27, 44, 1000]


def test_privsep_enabled_flag(monkeypatch):
    assert privsep.privsep_enabled({}) is False
    assert privsep.privsep_enabled({"TT_DEVICE_MCP_PRIVSEP": "1"}) is True
    assert privsep.privsep_enabled({"TT_DEVICE_MCP_PRIVSEP": "true"}) is True
    assert privsep.privsep_enabled({"TT_DEVICE_MCP_PRIVSEP": "off"}) is False


def test_should_privsep_requires_root_and_systemd(monkeypatch):
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    monkeypatch.setattr(privsep.shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 0)
    assert privsep.should_privsep(env) is True
    # not root -> disabled
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 1000)
    assert privsep.should_privsep(env) is False
    # root but systemd-run missing -> disabled
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 0)
    monkeypatch.setattr(privsep.shutil, "which", lambda _: None)
    assert privsep.should_privsep(env) is False


def _enable(monkeypatch):
    monkeypatch.setattr(privsep.shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 0)
    monkeypatch.setattr(privsep, "_gid_for_uid", lambda uid: 1000)


def test_privsep_prefix_for_returns_none_when_disabled():
    assert privsep.privsep_prefix_for(1000, env={}) is None


def test_privsep_prefix_for_skips_root_and_unknown_uid(monkeypatch):
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    assert privsep.privsep_prefix_for(None, env=env) is None  # HTTP, no peer identity
    assert privsep.privsep_prefix_for(0, env=env) is None  # already root
    monkeypatch.setattr(privsep, "_gid_for_uid", lambda uid: None)  # no passwd entry
    assert privsep.privsep_prefix_for(1234, env=env) is None


def test_privsep_prefix_for_builds_when_enabled(monkeypatch):
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    prefix = privsep.privsep_prefix_for(1234, env=env)
    assert prefix is not None
    assert "--uid=1234" in prefix and "systemd-run" == prefix[0]


def test_cooperative_mode_keeps_user_gid(monkeypatch):
    # privsep on, no device group -> run with the user's own gid (1000)
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    prefix = privsep.privsep_prefix_for(1234, env=env)
    assert "--uid=1234" in prefix and "--gid=1000" in prefix


def test_lockdown_uses_device_gid_via_env(monkeypatch):
    env = {"TT_DEVICE_MCP_PRIVSEP": "1", "TT_DEVICE_MCP_DEVICE_GROUP": "ttdev"}
    _enable(monkeypatch)
    prefix = privsep.privsep_prefix_for(1234, env=env)
    assert "--reuid=1234" in prefix and "--regid=ttdev" in prefix


def test_privsep_refusal_none_when_inactive():
    # privsep off (also: daemon not root) — direct exec is the intended legacy path, not a refusal.
    assert privsep.privsep_refusal(None, env={}) is None
    assert privsep.privsep_refusal(1234, env={}) is None


def test_privsep_refusal_refuses_identity_less(monkeypatch):
    # Active privsep + no peer identity: refuse rather than let the runner exec as the broker (root).
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    reason = privsep.privsep_refusal(None, env=env)
    assert reason is not None and "root" in reason


def test_privsep_refusal_allows_root(monkeypatch):
    # The caller genuinely IS root — running directly runs it as itself, not an escalation.
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    assert privsep.privsep_refusal(0, env=env) is None


def test_privsep_refusal_refuses_uid_without_passwd(monkeypatch):
    # The R6 fail-open: an authenticated uid with no passwd entry cannot be scoped, so
    # privsep_prefix_for returns None and the job would fall through to exec as root. Refuse.
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    monkeypatch.setattr(privsep, "_gid_for_uid", lambda uid: None)
    reason = privsep.privsep_refusal(1234, env=env)
    assert reason is not None and "passwd" in reason


def test_privsep_refusal_allows_resolvable_uid(monkeypatch):
    # A real, non-root, resolvable submitter: no refusal — the job runs under a systemd scope.
    env = {"TT_DEVICE_MCP_PRIVSEP": "1"}
    _enable(monkeypatch)
    assert privsep.privsep_refusal(1234, env=env) is None
