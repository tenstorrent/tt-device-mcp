# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Client retries a refused connect / missing socket (broker restart), device-free."""

import os
import socket
import threading

import pytest

from tt_device_mcp import utils


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv(utils.CONNECT_DELAY_ENV, "0")
    monkeypatch.delenv(utils.CONNECT_RETRIES_ENV, raising=False)


def _serve_once(path, ready_after=None):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(65536)
        body = b'{"ok": true}'
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()


def test_retry_then_success(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "s.sock")
    monkeypatch.setattr(utils, "resolve_socket", lambda *a: path)
    real_sleep = utils.time.sleep
    calls = []

    def fake_sleep(d):
        calls.append(d)
        if len(calls) == 2:
            _serve_once(path)  # broker comes back after two failed connects
        real_sleep(0)

    monkeypatch.setattr(utils.time, "sleep", fake_sleep)
    assert utils.api_call(0, "/x") == {"ok": True}
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert err.count("broker restart may be in progress") == 1


def test_retries_exhausted_same_error(monkeypatch, tmp_path):
    path = str(tmp_path / "missing.sock")
    monkeypatch.setattr(utils, "resolve_socket", lambda *a: path)
    monkeypatch.setenv(utils.CONNECT_RETRIES_ENV, "3")
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    res = utils.api_call(0, "/x")
    assert len(sleeps) == 3
    assert res["error"].startswith("Connection failed:")


def test_missing_socket_path_retried_then_no_server(monkeypatch):
    monkeypatch.setattr(utils, "resolve_socket", lambda *a: None)
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    res = utils.api_call(0, "/x")
    assert len(sleeps) == utils.DEFAULT_CONNECT_RETRIES
    assert "no tt-device-mcp server reachable" in res["error"]


def test_no_retry_on_other_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    for exc in (PermissionError("denied"), ConnectionResetError("reset"), socket.timeout("t")):
        n = []

        def attempt(exc=exc, n=n):
            n.append(1)
            raise exc

        with pytest.raises(type(exc)):
            utils.retry_unreachable(attempt)
        assert len(n) == 1
    assert sleeps == []


def test_no_resend_after_connect(monkeypatch, tmp_path):
    """A failure after the connect (request may have been accepted) is not retried."""
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(5)
    accepted = []

    def run():
        conn, _ = srv.accept()
        accepted.append(1)
        conn.recv(65536)
        conn.close()  # accepted the request, then died without answering

    threading.Thread(target=run, daemon=True).start()
    monkeypatch.setattr(utils, "resolve_socket", lambda *a: path)
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    res = utils.api_call(0, "/submit", data={"a": 1})
    srv.close()
    assert "error" in res
    assert sleeps == [] and len(accepted) == 1
    assert os.path.exists(path)


def _refused_socket(tmp_path):
    """A socket file with no listener: connect() raises ConnectionRefusedError."""
    path = str(tmp_path / "dead.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()
    return path


def _count_attempts(monkeypatch):
    calls = []
    real = utils.retry_unreachable

    def spy(attempt, **kw):
        def counted():
            calls.append(1)
            return attempt()

        return real(counted, **kw)

    monkeypatch.setattr(utils, "retry_unreachable", spy)
    return calls


@pytest.mark.parametrize("kind", ["refused", "missing"])
def test_health_probe_fails_fast_without_retry(monkeypatch, tmp_path, capsys, kind):
    """The root health probe must see a dead broker at once: one connect, no wait."""
    from tt_device_mcp import health_probe

    path = _refused_socket(tmp_path) if kind == "refused" else str(tmp_path / "none.sock")
    monkeypatch.setattr(health_probe, "DEFAULT_SOCKET", path)
    monkeypatch.delenv(utils.CONNECT_DELAY_ENV)  # real 1 s default: a retry would show
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    attempts = _count_attempts(monkeypatch)
    res = health_probe.Probe(root=tmp_path).health()
    assert res["error"].startswith("Connection failed:")
    assert attempts == [1] and sleeps == []
    assert "broker restart may be in progress" not in capsys.readouterr().err


def test_normal_client_still_retries_refused_connect(monkeypatch, tmp_path):
    """Contrast: the same refused socket is retried for a normal client call."""
    path = _refused_socket(tmp_path)
    monkeypatch.setattr(utils, "resolve_socket", lambda *a: path)
    sleeps = []
    monkeypatch.setattr(utils.time, "sleep", sleeps.append)
    attempts = _count_attempts(monkeypatch)
    res = utils.api_call(0, "/health", method="GET")
    assert res["error"].startswith("Connection failed:")
    assert len(attempts) == utils.DEFAULT_CONNECT_RETRIES + 1
    assert len(sleeps) == utils.DEFAULT_CONNECT_RETRIES
