# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A foreign device holder's identity in the journal (logging only): read from /proc, secrets
redacted, truncated, and never raising when the process is gone or a file is missing."""

from __future__ import annotations

import os

import pytest

from tt_device_mcp import holder_identity as hi

BTIME = 1_700_000_000


def _stage(root, pid, *, ppid=1, uid=1001, comm="python3", argv=(b"python3", b"run.py"), start=500, cgroup=True):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {start} 0 0\n")
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "comm").write_text(f"{comm}\n")
    (d / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    if cgroup:
        (d / "cgroup").write_text("0::/user.slice/user-1001.slice/user@1001.service/app.slice/demo.service\n")
    return d


@pytest.fixture
def proc(tmp_path, monkeypatch):
    (tmp_path / "stat").write_text(f"cpu 1 2 3\nbtime {BTIME}\n")
    monkeypatch.setattr(hi, "PROC_DIR", str(tmp_path))
    monkeypatch.setattr(hi, "_btime_cache", [])
    monkeypatch.setattr(hi, "_clk_tck_cache", [100.0])
    monkeypatch.setattr(hi, "username_for_uid", lambda uid: f"user{uid}")
    return tmp_path


def test_a_live_holder_is_named_by_parent_user_unit_start_and_cmdline(proc):
    _stage(proc, 1, comm="systemd", argv=(b"/sbin/init",))
    _stage(proc, 4242, ppid=1, start=500)

    row = hi.holder_identity(4242)

    assert row["status"] == "live"
    assert row["pid"] == 4242 and row["ppid"] == 1 and row["parent_comm"] == "systemd"
    assert row["uid"] == 1001 and row["user"] == "user1001"
    assert row["comm"] == "python3"
    assert row["cmdline"] == "python3 run.py"
    assert row["unit"] == "demo.service"
    assert row["cgroup"].endswith("/app.slice/demo.service")
    from datetime import datetime

    assert datetime.fromisoformat(row["started"]).timestamp() == BTIME + 5
    line = hi.describe_identity(row)
    assert "pid 4242" in line and "unit=demo.service" in line and "cmdline='python3 run.py'" in line


def test_the_real_current_process_reads_without_error():
    """Against the real /proc: this test's own process is live and fully readable."""
    row = hi.holder_identity(os.getpid())
    assert row["status"] == "live" and "error" not in row
    assert row["ppid"] == os.getppid() and row["uid"] == os.getuid()
    assert row["cmdline"] and row["started"]


def test_an_exited_holder_is_recorded_gone_and_never_raises(proc):
    assert hi.holder_identity(99999) == {"pid": 99999, "status": "gone"}
    assert hi.describe_identity({"pid": 99999, "status": "gone"}) == "pid 99999 gone before it could be read"


def test_a_holder_that_exits_mid_read_keeps_what_was_read_and_reads_gone(proc):
    d = _stage(proc, 4243)
    (d / "cmdline").unlink()  # the process went between its stat and its cmdline
    row = hi.holder_identity(4243)
    assert row["status"] == "gone"
    assert row["ppid"] == 1 and "cmdline" not in row


def test_a_reused_pid_mid_read_is_not_mixed_into_one_identity(proc, monkeypatch):
    _stage(proc, 4244, start=500)
    real = hi._read
    stats = iter([None, "4244 (other) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 900 0 0\n"])

    def read(path, binary=False):
        if path.endswith("4244/stat"):
            nxt = next(stats)
            if nxt is not None:
                return nxt
        return real(path, binary)

    monkeypatch.setattr(hi, "_read", read)
    assert hi.holder_identity(4244) == {"pid": 4244, "status": "gone"}


def test_a_missing_cgroup_leaves_the_unit_out_and_keeps_the_rest(proc):
    _stage(proc, 4245, cgroup=False)
    row = hi.holder_identity(4245)
    assert row["status"] == "live"
    assert "cgroup" not in row and "unit" not in row
    assert row["cmdline"] == "python3 run.py" and row["ppid"] == 1


def test_a_gone_parent_reads_gone_not_an_error(proc):
    _stage(proc, 4246, ppid=777)
    row = hi.holder_identity(4246)
    assert row["status"] == "live" and row["parent_comm"] == "gone"


def test_an_unexpected_read_error_is_recorded_not_raised(proc):
    d = _stage(proc, 4247)
    (d / "stat").write_text("garbage")
    row = hi.holder_identity(4247)
    assert row["pid"] == 4247 and row["error"]


@pytest.mark.parametrize(
    "argv, leaked, kept",
    [
        (["tool", "--token=abc123"], "abc123", "--token=***"),
        (["tool", "--api-key", "abc123"], "abc123", "--api-key ***"),
        (["tool", "-p", "x", "--password", "hunter2"], "hunter2", "--password ***"),
        (["HF_TOKEN=hf_abc123", "python"], "hf_abc123", "HF_TOKEN=***"),
        (["curl", "https://bob:s3cret@example.com/x"], "s3cret", "https://***@example.com/x"),
        (["curl", "https://bob@example.com/x"], "bob", "https://***@example.com/x"),
        (["app", "https://h/x?access_token=abc123&a=1"], "abc123", "access_token=***&a=1"),
        (["app", '{"client_secret": "abc123"}'], "abc123", '"client_secret": "***"'),
        (["app", "--db-passwd=abc123"], "abc123", "--db-passwd=***"),
        (["app", "SECRET_KEY:abc123"], "abc123", "SECRET_KEY:***"),
    ],
)
def test_secrets_are_redacted(argv, leaked, kept):
    line = hi.format_cmdline(b"\0".join(a.encode() for a in argv) + b"\0")
    assert leaked not in line
    assert kept in line


def test_harmless_arguments_survive_redaction():
    line = hi.format_cmdline(b"python3\0-m\0pytest\0tests/test_x.py\0-k\0mesh\0")
    assert line == "python3 -m pytest tests/test_x.py -k mesh"


def test_a_long_cmdline_is_cut_after_redaction():
    argv = [b"python3"] + [b"x" * 300] + [b"--token=abc123"]
    line = hi.format_cmdline(b"\0".join(argv))
    assert len(line) == hi.CMDLINE_MAX_CHARS and line.endswith("...")
    # A secret at the cut is redacted before the cut, so no prefix of it survives.
    long_secret = hi.format_cmdline(b"a" * 250 + b"\0--token=abcdefghijklmnop")
    assert "abc" not in long_secret.split()[-1]


def test_control_characters_never_reach_the_log_line():
    assert hi.format_cmdline(b"evil\nINJECTED\0x\x1b[31m\0") == "evil?INJECTED x?[31m"


def test_identities_are_deduplicated_in_order(proc):
    _stage(proc, 11)
    rows = hi.holder_identities([11, 12, 11])
    assert [r["pid"] for r in rows] == [11, 12]
    assert rows[1]["status"] == "gone"
