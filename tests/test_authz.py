# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for owner authz: peer-uid authority vs legacy HTTP self-report."""

import json

import pwd

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.server import authz_owner, owner_matches
from tt_device_mcp.socket_transport import current_peer_uid


class TestAuthzOwner:
    def test_http_uses_reported_owner(self):
        # No peer uid set (HTTP/TCP) -> legacy behavior, reported owner trusted.
        assert current_peer_uid.get() is None
        owner, authed = authz_owner("jdoe")
        assert owner == "jdoe"
        assert authed is False

    def test_http_unknown_when_no_owner(self):
        owner, authed = authz_owner(None)
        assert owner == "unknown"
        assert authed is False

    def test_socket_overrides_reported_owner(self):
        my_uid = __import__("os").getuid()
        my_name = pwd.getpwuid(my_uid).pw_name
        token = current_peer_uid.set(my_uid)
        try:
            # Caller claims to be "eve" but peer uid is authoritative.
            owner, authed = authz_owner("eve")
            assert owner == my_name
            assert authed is True
        finally:
            current_peer_uid.reset(token)


class TestOwnerMatches:
    def test_exact_match(self):
        assert owner_matches("bjones", "bjones") is True

    def test_mismatch(self):
        assert owner_matches("bjones", "jdoe") is False

    def test_agent_prefixed_job_matches_bare_user(self):
        # An agent-submitted job, with the caller authenticated as the bare user, caller authenticated as bare user.
        assert owner_matches("[agent]bjones", "bjones") is True

    def test_agent_prefix_does_not_match_other_user(self):
        assert owner_matches("[agent]bjones", "jdoe") is False


def test_no_tool_takes_an_owner_field():
    """The owner is derived from the peer uid and the request surface (spec 05 I6). A tool that
    accepted one would let a caller label their own job, and pydantic ignores unknown fields — so
    a re-added field would fail silently rather than break a test."""
    import tt_device_mcp.server as srv

    models = [srv.JobSubmitInput, srv.JobKillInput, srv.DeviceExecInput, srv.DeviceResetInput]
    offenders = [m.__name__ for m in models if "owner" in m.model_fields]

    assert offenders == [], f"these tool inputs still accept a caller-supplied owner: {offenders}"


def test_without_a_peer_identity_there_is_no_agent_tag():
    """Off the socket there is nobody to attribute to. `[agent]unknown` would name no one, and
    `owner_matches` would read it as every other identity-less caller's own job (spec 05 I6)."""
    import tt_device_mcp.server as srv
    from tt_device_mcp.socket_transport import current_peer_uid, current_via_mcp

    uid_tok, mcp_tok = current_peer_uid.set(None), current_via_mcp.set(True)
    try:
        assert srv.submitting_owner() == "unknown"
    finally:
        current_via_mcp.reset(mcp_tok)
        current_peer_uid.reset(uid_tok)


class TestJobLogsAuthz:
    """`_get_job_logs` must gate the same way `_kill_job` does: unlike queue-status metadata
    (owner/command, already shared for coordination — see `_get_queue_status`/`_recent_jobs`),
    log content carries cleartext resolved env vars and full stdout/stderr, so only the job's
    owner may read it."""

    def _client(self):
        from starlette.testclient import TestClient

        return TestClient(srv.build_asgi_app(srv.create_mcp_server()))

    def test_the_owner_can_read_their_own_logs(self, tmp_path, monkeypatch):
        log_file = tmp_path / "job.log"
        log_file.write_text("ENVIRONMENT VARIABLES:\n  SECRET=xyz\n")
        job = srv.Job(
            id="900-2", owner="bjones", workspace="/w", command="pytest", queued_at="",
            status=srv.JobStatus.COMPLETED, log_file=str(log_file),
        )
        monkeypatch.setattr(srv, "jobs", {"900-2": job})

        d = self._client().post(
            "/api/tt_device_job_logs", json={"job_id": "900-2", "owner": "bjones"}
        ).json()

        assert "error" not in d
        assert "SECRET=xyz" in d["content"]

    def test_a_different_caller_is_refused_the_content(self, tmp_path, monkeypatch):
        log_file = tmp_path / "job.log"
        log_file.write_text("ENVIRONMENT VARIABLES:\n  SECRET=xyz\n")
        job = srv.Job(
            id="900-3", owner="bjones", workspace="/w", command="pytest", queued_at="",
            status=srv.JobStatus.COMPLETED, log_file=str(log_file),
        )
        monkeypatch.setattr(srv, "jobs", {"900-3": job})

        d = self._client().post(
            "/api/tt_device_job_logs", json={"job_id": "900-3", "owner": "jdoe"}
        ).json()

        assert d.get("error", "").startswith("Permission denied")
        assert "content" not in d
        assert "SECRET" not in str(d)

    def test_job_status_omits_output_for_a_different_caller(self, monkeypatch):
        """The MCP tool layer (`job_status`) attaches stdout/stderr for finished jobs; that
        attachment must be owner-gated even though the REST route's bare metadata
        (owner/command/status) stays visible to any caller, matching
        `_get_queue_status`/`_recent_jobs`'s existing shared visibility."""
        job = srv.Job(
            id="900-4", owner="bjones", workspace="/w", command="pytest", queued_at="",
            status=srv.JobStatus.COMPLETED,
        )
        job.output = "secret stdout"
        job.error = "secret stderr"
        monkeypatch.setattr(srv, "jobs", {"900-4": job})

        mcp = srv.create_mcp_server()

        async def _status_as(owner):
            monkeypatch.setattr(srv, "authz_owner", lambda reported=None: (owner, True))
            out = await mcp.call_tool("tt_device_job_status", {"params": {"job_id": "900-4"}})
            return json.loads(out.content[0].text)

        import asyncio

        mine = asyncio.run(_status_as("bjones"))
        assert mine["output"] == "secret stdout" and mine["error"] == "secret stderr"

        theirs = asyncio.run(_status_as("jdoe"))
        assert "output" not in theirs and "error" not in theirs
        assert theirs["owner"] == "bjones"  # metadata itself stays visible
