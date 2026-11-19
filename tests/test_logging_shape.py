# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 06 I10 — one logging shape: `setup_logging` writes every line once to `<log dir>/server.log`
and once to stderr through one formatter, follows a rename of the file, and routes an uncaught
exception through the same logger before the process dies."""

import inspect
import io
import logging
import logging.handlers
import os
import re
import stat
import sys

import pytest
import uvicorn

import tt_device_mcp.server as srv
from tt_device_mcp import log_setup, socket_transport


def _only(handlers, kind):
    # By kind, not exact class: the file handler is a WatchedFileHandler subclass that states its
    # own mode (spec 06 I9), and a FileHandler is itself a StreamHandler, so the stderr lookup must
    # not count it.
    found = [
        h
        for h in handlers
        if isinstance(h, kind) and not (kind is logging.StreamHandler and isinstance(h, logging.FileHandler))
    ]
    assert len(found) == 1, f"expected exactly one {kind.__name__}, got {[type(h).__name__ for h in handlers]}"
    return found[0]


def test_setup_logging_installs_one_file_and_one_stderr_handler_sharing_a_formatter(
    monkeypatch, tmp_path, pristine_loggers
):
    """Per logger: exactly one WatchedFileHandler on server.log and one StreamHandler on stderr,
    one formatter between them; the named logger does not propagate, so a line lands once per
    destination and the server.log line is byte-identical to the stderr line."""
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    log_file = tmp_path / "server.log"

    logger = srv.setup_logging(log_file)

    for lg in (logger, logging.getLogger()):
        assert len(lg.handlers) == 2, [type(h).__name__ for h in lg.handlers]
        fh = _only(lg.handlers, logging.handlers.WatchedFileHandler)
        sh = _only(lg.handlers, logging.StreamHandler)
        assert fh.baseFilename == str(log_file)
        assert sh.stream is stderr, "the stderr copy is the journal's / daemon.log's copy"
        assert fh.formatter is sh.formatter, "one formatter, so the two copies are the same bytes"
        assert isinstance(fh.formatter, log_setup._DualTZFormatter)
    assert logger.propagate is False

    logger.info("one line, two destinations")
    logging.getLogger("tt_device_mcp.some.module").warning("a root-routed line")

    file_lines = log_file.read_text().splitlines()
    stderr_lines = stderr.getvalue().splitlines()
    assert len(file_lines) == 2, file_lines
    assert file_lines == stderr_lines, "written once to each destination, byte-identical"
    assert file_lines[0].endswith("| INFO  | one line, two destinations")
    assert file_lines[1].endswith("| WARNING | a root-routed line"), "root-routed lines take the same shape"


def test_a_renamed_server_log_is_followed_by_a_fresh_file_at_the_path(
    monkeypatch, tmp_path, pristine_loggers, strict_umask
):
    """logrotate renames; the next line lands in a new server.log, not in the rotated file — and
    that fresh file is 0644 by the handler's own chmod (spec 06 I9): under umask 077 a plain reopen
    would leave it 0600, readable by root alone, until logrotate's `create` happened to win."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    log_file = tmp_path / "server.log"
    logger = srv.setup_logging(log_file)
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o644, "the first open states the mode too"

    logger.info("before the rotation")
    rotated = tmp_path / "server.log.1"
    os.rename(log_file, rotated)
    logger.info("after the rotation")

    assert log_file.exists(), "the handler must reopen the path after a rename"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o644, "the reopened file's mode is the handler's, not the umask's"
    assert "after the rotation" in log_file.read_text()
    assert "before the rotation" not in log_file.read_text()
    assert "before the rotation" in rotated.read_text()
    assert "after the rotation" not in rotated.read_text(), "a line landed in the rotated file"


def test_an_uncaught_exception_reaches_server_log_through_the_excepthook(monkeypatch, tmp_path, pristine_loggers):
    """A crash traceback is in server.log, not only wherever stderr went."""
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    log_file = tmp_path / "server.log"
    srv.setup_logging(log_file)

    assert sys.excepthook is log_setup._log_uncaught_exception, "setup_logging installs the hook"
    try:
        raise ZeroDivisionError("the broker fell over")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())

    body = log_file.read_text()
    assert "ZeroDivisionError: the broker fell over" in body
    assert "Traceback (most recent call last)" in body
    assert "| CRITICAL | uncaught exception" in body
    assert body == stderr.getvalue(), "the same bytes went to stderr"


def test_a_ctrl_c_is_logged_as_a_stop_and_nothing_else_is(monkeypatch, tmp_path, pristine_loggers):
    """Spec 06 I10: uvicorn re-raises SIGINT after its clean shutdown, so Ctrl-C on `daemon start-fg`
    ends as a KeyboardInterrupt at the hook. That is a stop: INFO, traceback kept, no CRITICAL.
    The exemption is that one type — a subclass of Exception still logs as a crash."""
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    log_file = tmp_path / "server.log"
    srv.setup_logging(log_file)

    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    body = log_file.read_text()
    assert re.search(r"\| INFO +\| stopped by SIGINT \(Ctrl-C\)", body), body
    assert "Traceback (most recent call last)" in body and "KeyboardInterrupt" in body
    assert "CRITICAL" not in body

    class Interrupted(Exception):
        pass

    try:
        raise Interrupted("not a keyboard interrupt")
    except Interrupted:
        sys.excepthook(*sys.exc_info())
    assert "| CRITICAL | uncaught exception" in log_file.read_text()


@pytest.mark.asyncio
async def test_uvicorn_logs_through_the_brokers_handlers_with_access_logging_off(
    monkeypatch, tmp_path, pristine_loggers
):
    """Spec 06 I10: uvicorn's default log config hands its loggers their own stderr/stdout handlers
    and turns propagation off, so its startup and error lines would reach the journal and never
    server.log. The socket transport builds it with log_config=None (propagate to the root pair) and
    access_log=False (no per-request line; the broker logs each call itself). uvicorn.Server is
    stubbed so nothing listens; the Config it was handed is what is asserted. The HTTP path
    (spec 02 Target: remove) builds its Config from the same UVICORN_LOGGING constant: pinned on
    its source and on the module binding the name, since nothing drives run_transports in the
    suite."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    log_file = tmp_path / "server.log"
    srv.setup_logging(log_file)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        lg = logging.getLogger(name)
        monkeypatch.setattr(lg, "handlers", [])
        monkeypatch.setattr(lg, "propagate", True)
        monkeypatch.setattr(lg, "level", lg.level)  # Config.configure_logging sets INFO on them

    built = {}

    class _Server:
        def __init__(self, config):
            built["config"] = config

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", _Server)
    await socket_transport.serve_unix_socket(object(), str(tmp_path / "s.sock"))

    config = built["config"]
    assert config.log_config is None, "uvicorn must not install its own handlers"
    assert config.access_log is False, "no per-request access line"
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        assert lg.handlers == [] and lg.propagate is True, (name, lg.handlers, lg.propagate)
    logging.getLogger("uvicorn.error").info("Uvicorn running on a unix socket")
    assert "Uvicorn running on a unix socket" in log_file.read_text(), "uvicorn's lines reach server.log"
    assert socket_transport.UVICORN_LOGGING == {"log_config": None, "access_log": False}
    http_path = inspect.getsource(srv.run_transports)
    assert "uvicorn.Config(" in http_path and "**UVICORN_LOGGING" in http_path, "the HTTP Config shares the constant"
    # The source check alone passes over a name the module never binds, which is a NameError on
    # the first HTTP start; the module must hold the very constant the socket transport uses.
    assert srv.UVICORN_LOGGING is socket_transport.UVICORN_LOGGING, "server.py must import UVICORN_LOGGING"


# --- the health probe: the same shape, its own file ----------------------------------------------


def test_the_health_probe_logs_through_the_broker_shape_to_its_own_file(monkeypatch, tmp_path, pristine_loggers):
    """Spec 06 I10: the probe's unit runs every 2 min, and its lines (rescans, alerts, poller
    restarts) are read by users without journal access. They go to `<log dir>/health-probe.log` at
    0644 under a strict umask, byte-identical on stderr, through the broker's own setup_logging."""
    from tt_device_mcp import health_probe

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("TTDEV_LOG_DIR", str(tmp_path / "logs"))
    previous = os.umask(0o077)
    try:
        health_probe._setup_log()
        health_probe.log("2/4 chips present, rescanning the PCI bus")
    finally:
        os.umask(previous)

    log_file = tmp_path / "logs" / "health-probe.log"
    body = log_file.read_text()
    assert re.search(r"\| INFO +\| 2/4 chips present, rescanning the PCI bus$", body, re.M), body
    assert body == stderr.getvalue(), "the same bytes went to stderr, the unit's journal copy"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o755


def test_a_health_probe_that_cannot_write_its_log_keeps_stderr_and_carries_on(monkeypatch, tmp_path, pristine_loggers):
    """Spec 06 I10: the probe can do its job without its log file, so an unwritable log dir is one
    warning and the stderr copy, as log.sh does for the deploy scripts, never a crashed probe."""
    from tt_device_mcp import health_probe

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("TTDEV_LOG_DIR", str(blocker))

    health_probe._setup_log()
    health_probe.log("ok 4/4 chips")

    out = stderr.getvalue()
    assert "| WARNING | cannot write" in out and "logs to stderr only" in out, out
    assert out.rstrip().endswith("| INFO  | ok 4/4 chips"), out


def test_the_health_probe_entry_point_sets_up_its_log_before_it_runs(monkeypatch, tmp_path, pristine_loggers):
    """Spec 06 I10: the unit runs `main()`, so `main()` is what must set the shape up. Without it
    `log()` is an INFO call on a logger with no handlers, which Python's last-resort handler
    drops: the probe's rescans and poller restarts would reach neither the file nor the journal."""
    from tt_device_mcp import health_probe

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("TTDEV_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(health_probe.Probe, "run", lambda self: health_probe.log("probe ran") or 0)

    assert health_probe.main() == 0
    body = (tmp_path / "logs" / "health-probe.log").read_text()
    assert re.search(r"\| INFO +\| probe ran$", body, re.M), body
    assert body == stderr.getvalue(), "the same line on stderr, the unit's journal copy"
