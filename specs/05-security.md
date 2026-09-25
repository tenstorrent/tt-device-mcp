<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Security

Scope: caller identity and authorization — SO_PEERCRED identity on the unix socket
(`peercred.py`, `socket_transport.py`), privilege separation / run-as-submitter (`privsep.py`),
the owner format and ownership checks (kill/cancel), the job-exit-file trust model, the
`tt_device_exec` gate, and the bare-metal device lock (`tt-device-mcp lock`/`unlock`). Transport
wiring and tool schemas are spec 02; the reset gate's holder scan and force semantics are spec 04;
state-file paths and their layout are spec 06; the CLI surface is spec 07.

## Purpose

The broker arbitrates one device among mutually untrusting tenants, so it must know *who* is
asking before it acts: who a job runs as, who may kill it, who may push a diagnostic onto the
device, and who may reset it over someone else's run. The subsystem rests on one asymmetry: the
kernel's SO_PEERCRED on a unix socket is unforgeable, while any owner string in a request body is
a claim. Everything privileged keys off the former; the latter is used for attribution and for
the legacy single-user HTTP transport only. Privilege separation extends the same identity into
execution: a job runs *as* its submitter, in a systemd scope, so job-side privilege is the
submitter's own — and the bare-metal lock closes the flank, denying the device node to anything
that did not come through the broker at all.

## Invariants

- **I1 — Socket callers are identified by the kernel, never by self-report.** Every request over
  the unix socket carries the connecting process's real uid from SO_PEERCRED
  (`read_peer_credentials`), stamped into the ASGI scope at connection time and published
  per-request via the `current_peer_uid` contextvar. When a peer uid is present, `authz_owner`
  resolves the caller from it and ignores the request's `owner` field for authorization.
- **I2 — HTTP carries no identity.** A TCP/HTTP request has no peer credentials;
  `current_peer_uid` stays `None` and `authz_owner` returns the reported owner (or `"unknown"`)
  flagged unauthenticated. No privsep decision may treat that self-report as an identity: under
  active privsep, job submission and `tt_device_exec` over HTTP are refused outright
  (`privsep_identity_error`) and pointed at the socket.
- **I3 — Under privsep a job runs as its submitter, never as root by fallback.** With privsep
  active, every job and every exec runs as its real, non-root submitter via
  `systemd-run --scope --uid=<peer_uid>`. An identity that cannot be honored — no peer uid, or a
  uid with no passwd entry — is refused (`privsep_refusal`) *before* dispatch; the direct-exec
  fallthrough (which would run the work as the broker, root) is never taken for it. A caller who
  genuinely is root is not refused: it already runs as itself.
- **I4 — Privsep activation is conjunctive and explicit.** `should_privsep` requires all of: the
  `TT_DEVICE_MCP_PRIVSEP` opt-in, the daemon running as root (euid 0), and `systemd-run` on PATH.
  Any missing condition logs a warning and disables privsep entirely — jobs then run directly as
  the daemon (the intended legacy mode, where the daemon is not root). There is no partial mode.
- **I5 — Only the owner (or an equivalent identity) kills or cancels a job.** `_kill_job` resolves
  the caller through `authz_owner` and refuses unless `owner_matches(job.owner, caller)`: an exact
  match, or the job tagged `[agent]<caller>` — the agent-submitted job a user may manage under
  their bare name. Every kill/cancel is audited with both the requester and the job's owner
  (log line + `job_killed` health event), so "the broker killed it" is never the whole record.
- **I6 — Owner strings are `<user>` or `[agent]<user>`, and the broker derives both.** Over the
  socket the peer uid names the submitter and the surface the request arrived on decides the tag:
  the MCP endpoint gets `[agent]<user>`, every other route the bare `<user>`. The MCP path is
  matched positively, so a route that is neither reads as the plainer of the two. Nothing the
  caller sends is read — the tools take no `owner` field.

  The tag reads one way only. `[agent]` is proof the submission came through the MCP surface; a
  bare `<user>` is not proof a human typed it, because an agent may shell out to the CLI. Treat it
  as attribution, never as an authorization input — authz is the peer uid, which is the same
  either way. Over HTTP there is no peer identity, so a missing owner honestly stays
  `"unknown"` — untagged, since a label naming no one must not read as an agent's, nor match
  another identity-less caller. A uid with no passwd entry renders as `uid:<n>` rather than
  failing.
- **I7 — The broker's own observation of a job's exit outranks the job's record.** The exit file
  (`<job_id>.exit`, written by the job's own EXIT trap) is consulted only for a job this broker
  did not watch exit — one re-adopted across a broker restart. A broker that reaped the process
  itself uses `proc.returncode` and deletes the file unread. A re-adopted job that left no exit
  status is reported FAILED with an explanation, never COMPLETED; malformed content reads as no
  status. A per-user (redirected) exit directory is never widened world-writable.
- **I8 — `tt_device_exec` is gated, and force is scoped to exactly one gate.** Exec refuses:
  (a) under privsep, an identity-less or unhonorable caller (I2/I3); (b) while a job owned by
  someone else is running — unless `force=true`, which admits the diagnostic *alongside* the
  foreign run and puts the override on the record (`exec_forced` event naming caller, job, owner,
  command); (c) whenever the device is degraded for tenants — the same live-probe verdict the job
  runner gates on, including faults that set no in-memory flag (a chip off the bus). `force`
  covers only (b), never (c) or a broker device-op in flight.
- **I9 — The bare-metal lock is root-only and self-verifying.** `tt-device-mcp lock`/`unlock`
  refuse without euid 0 and without an installed broker unit (the lock applies only to a broker
  host). `lock` moves `/dev/tenstorrent` to `0660 root:ttdev` via a udev rule and then proves both
  directions before claiming success: an admitted (gid=ttdev) open must succeed — else it reverts
  rather than brick the host — and a non-ttdev open must be denied — else it reverts rather than
  print a false LOCKED. An inconclusive deny probe is reported as UNVERIFIED, not claimed.
  `unlock` restores world-rw. Both are idempotent.
- **I10 — Read-only telemetry on a locked host stays read-only.** The `smi` path grants no write
  or reset capability: the client CLI rejects non-allowlisted flags before invoking anything, and
  the root-owned NOPASSWD wrapper installed by `lock` re-enforces the same read-only allowlist
  itself (any user may exec it, so it cannot trust its caller). The streaming server-side smi
  applies the same allowlist (`smi_args_ok`) before running as the broker. A sudoers drop-in that
  fails `visudo -c` is not installed.
- **I11 — An anonymous caller owns no holder.** Where an action is gated on device holders
  (the reset gate, spec 04), the caller's identity is the socket peer uid. Over HTTP on a privsep
  host there is no identity to scope the gate to, so every tenant holder counts as foreign and the
  gate fails closed; off privsep, HTTP keeps the legacy single-tenant skip. `MIN_TENANT_UID`
  (1000) divides tenants from infrastructure: holders below it (root, telemetry daemons) never
  count as foreign.

## Interfaces

Consumed by other subsystems and by tests:

- **`peercred.py`**: `PeerCredentials(pid, uid, gid)` (frozen; `.username` property),
  `username_for_uid(uid) -> str` (falls back to `uid:<n>`),
  `read_peer_credentials(sock) -> PeerCredentials | None` — `None`, never an exception, when the
  platform or socket cannot supply SO_PEERCRED, so a connection degrades to anonymous rather
  than crash.
- **`socket_transport.py`** (identity attach): a uvicorn `H11Protocol` subclass reads SO_PEERCRED
  at `connection_made` and stamps `("peercred", uid)` into the connection's client tuple, which
  uvicorn copies verbatim into `scope["client"]`; `peer_uid_from_scope(scope)` reads it back
  (TCP `("host", port)` tuples never match the marker); `PeerCredMiddleware` publishes it as the
  `current_peer_uid` contextvar for the request's task, so MCP tool handlers and `/api/*` routes
  see the same value; `current_peer_uid` defaults to `None` (HTTP).
- **`privsep.py`** (consumed by the job runner and `_exec_impl`):
  `privsep_enabled(env)` — the opt-in flag alone; `should_privsep(env)` — flag ∧ root ∧
  systemd-run; `systemd_run_prefix(uid, gid, device_group, unit)` — pure argv builder
  (`systemd-run --scope --quiet --collect --uid=<uid> --gid=<gid> [--unit=<name>]`);
  `privsep_prefix_for(peer_uid, env, unit) -> list | None` — the prefix, or `None` meaning "run
  directly" (privsep off, no peer, caller is root, or no passwd entry);
  `privsep_refusal(peer_uid, env) -> str | None` — the refusal reason when `None` from
  `privsep_prefix_for` must NOT be read as "run it anyway". Env knobs: `TT_DEVICE_MCP_PRIVSEP`
  (opt-in), `TT_DEVICE_MCP_DEVICE_GROUP` (lockdown gid; `DEFAULT_DEVICE_GROUP = "ttdev"`).
- **`server.py` authz helpers**: `authz_owner(reported) -> (owner, authenticated)`;
  `owner_matches(job_owner, caller_owner) -> bool`; `privsep_identity_error() -> dict | None`
  (the transport-level guard); `Job.peer_uid` — the submitter's SO_PEERCRED uid captured at queue
  time, `None` over HTTP, carried in the durable queued-job spec so a restart preserves the
  privsep target.
- **`device_holders.MIN_TENANT_UID`** (= 1000): the tenant/infrastructure boundary, used by the
  reset gate (spec 04) and the health gate's holder checks (spec 03).
- **Lock surface** (`cli.py`): `cmd_lock` / `cmd_unlock` (sudo; `tt-device-mcp lock|unlock`);
  artifacts managed by them: the udev rule (`99-tenstorrent-ttdev.rules`), the
  `TT_DEVICE_MCP_DEVICE_GROUP=ttdev` line in the broker unit, the read-only smi wrapper +
  its sudoers drop-in, the login banner. `_device_locked()` — group+mode sniff of a real device
  node (never `by-id/`) — selects the CLI `smi` path.

## Behavior

**Identity resolution.** Unix socket: SO_PEERCRED is read once per connection and rides the ASGI
scope into a per-request contextvar; the caller *is* that uid, and their display name is its
passwd entry. HTTP/TCP: no credentials exist; the reported `owner` field is accepted as an
unauthenticated claim (attribution, queue display, legacy single-user authz). A socket whose
platform cannot supply SO_PEERCRED degrades to the HTTP behavior rather than refusing the
connection.

**Owner derivation at submission.** `_queue_job` derives the label itself (`submitting_owner`):
the peer uid names the user, the request surface decides the tag. No route reads an owner from a
request body, so a caller cannot name anyone — themselves included. Off the socket there is no
identity and the label is `unknown`. `job.peer_uid` is captured separately from the label;
privsep targets the uid, never the label.

**Kill/cancel authorization.** `_kill_job(job_id, owner)` (shared by the REST route and the MCP
tool): resolve `caller_owner` via `authz_owner` (peer uid wins; reported owner only over HTTP);
refuse with "Permission denied: job belongs to \<owner\>" unless `owner_matches`. A RUNNING job is
terminated scope-aware (`_terminate_job`, spec 01 I5 — a privsep job's scope is signalled, not the
wrapper's process group); a QUEUED job is cancelled in place. The requester and the job's owner
are both logged and journalled.

**Privsep job launch.** Before a job flips to RUNNING, the runner evaluates
`privsep_refusal(job.peer_uid)`: any reason terminalizes the job as FAILED with the reason in its
error and log — refusal must never wedge the runner, and the job is never dispatched as root.
Otherwise `privsep_prefix_for(job.peer_uid, unit=job_scope_unit(job_id))` yields either a
`systemd-run` prefix (job runs as the submitter, in a named transient scope that survives — and is
re-adoptable after — a broker restart) or `None` (legacy direct exec: privsep off, or the caller is
root). Cooperative mode keeps the submitter's primary gid; lockdown mode
(`TT_DEVICE_MCP_DEVICE_GROUP` set, node locked to `0660 root:ttdev`) runs the job with the device
group as its *primary* gid so plain DAC admits it while bare shells are denied.

**Exec gating** (`_exec_impl`, shared by the MCP tool and REST route), in order:
1. `privsep_identity_error()` — identity-less exec under privsep is refused toward the socket.
2. Foreign-run check: a RUNNING job not owned by the caller refuses without `force`; with
   `force`, the exec proceeds alongside the run and the override is audited first.
3. Degraded check: `_device_degraded_for_tenant()` — the live-probe verdict, not just in-memory
   flags — refuses with the hold reason. Exec is a short synchronous call, so it refuses and says
   why rather than queueing behind a possibly minutes-long device op.
4. `privsep_refusal(current_peer_uid.get())` — the passwd-less-uid case the step-1 guard cannot
   see; fail closed rather than run as the broker.
5. Launch: under privsep, in its own deterministically named scope (so a timeout reaps the scope,
   not merely the `systemd-run` wrapper); otherwise in its own session (killpg on timeout). Every
   exec lands in the action log under the resolved caller.

**Straggler reclaim.** `reclaim_foreign_holders` signals device holders at or above
`MIN_TENANT_UID` (SIGTERM, grace, SIGKILL). It is reachable only from `post-step`, whose route
refuses a non-root peer (`SO_PEERCRED`), because only a caller that owns the allocation's
lifecycle can assert that a remaining holder is a straggler rather than a tenant. It signals only
pids present in its own first scan — a pid that appears only in a later rescan is a holder that
opened the device after the reclaim began, not a straggler of the allocation that just ended, and
is reported as a survivor rather than escalated onto. Two exclusions are structural: it never
signals uids below `MIN_TENANT_UID` (infrastructure that survives a board reset), and it never
signals itself or its own process group. It has no way to recognize "a broker-owned job" as such
— under privsep that job runs as its submitter's uid and looks like any other tenant holder — so
protecting a live broker job from the reclaim is the in-flight guard's job (`_broker_work_in_flight`,
re-checked after the reclaim returns), not a property of this function. A job the broker itself
means to end still terminates through `_kill_job`.

**Job-step routes are root-only.** `/api/tt_device_pre_step` and `/api/tt_device_post_step`
refuse any peer whose `SO_PEERCRED` uid is not 0, including an unauthenticated caller. Both can
reset the device and `post-step` can SIGKILL another user's processes; the authority to declare
an allocation over is not a tenant's.

**Lock mechanics.** `lock`: create the `ttdev` system group; write the device-group line into the
broker unit and restart it *first* (privsep jobs must already carry gid=ttdev before the node
locks, or admitted work is denied in the gap); write the udev rule and re-trigger the subsystem;
then self-check on a real device node — positive (systemd-run as `nobody:ttdev` must open the
node; failure reverts via `unlock`) and negative (same uid without the group must get EACCES,
distinguished by a dedicated exit code from a probe that never ran; an open reverts, an
inconclusive probe is reported unverified); finally install the read-only smi wrapper (root-owned
`0700`, validated sudoers `0400 root:root`) and the login banner. `unlock` removes the rule,
re-triggers, strips the unit line, removes banner and wrapper, restarts the broker.

**Read-only smi.** On a locked host `tt-device-mcp smi` execs the wrapper via `sudo -n`; on a
cooperative host it execs `tt-smi` directly. Both the CLI and the wrapper independently enforce
the read-only flag allowlist, so neither trusts the other. The server-side streaming smi applies
the same allowlist and runs as the broker (read-only, parallel to jobs, telemetry never blocks
the queue).

## Design decisions

**Why SO_PEERCRED.** Anything a client sends — an `owner` field, a header — is forgeable by any
process that can reach the endpoint, and localhost HTTP is reachable by every local user. The
kernel's SO_PEERCRED is populated from the connecting process's task credentials and cannot be
faked without already having those credentials. It also costs nothing to deploy: no tokens to
issue, rotate, or leak on a box whose users already have Unix identities.

**Why refuse, not fall back, on an unhonorable identity.** When privsep is active the broker runs
as root, so the only fallthrough from "cannot build a per-user scope" is "exec directly as root" —
a silent privilege *escalation* handed to exactly the callers whose identity is least established
(HTTP, or a uid the host cannot resolve). `privsep_prefix_for` returning `None` is therefore
deliberately ambiguous ("run directly" is correct for root callers and for privsep-off), and
`privsep_refusal` exists as the separate, explicit predicate for "this None means refuse". Callers
consult it before dispatch; a missing consultation would fail open, which is why both the runner
and exec check it independently of the transport guard.

**Why the lock exists.** Serialization by queue only binds tenants who use the queue. On a shared
host, one bare-metal `tt-smi` or stray pytest opens the device under a running job and wedges the
mesh (spec 03/04 exist because of exactly this). The lock moves enforcement from convention to the
kernel: DAC on the device node denies any process not admitted through a broker scope. Group-based
DAC via the job's primary gid was chosen over systemd `DevicePolicy`/`SupplementaryGroups`
properties because older `systemd-run` rejects transient `-p` properties on a scope, and plain DAC
suffices. The self-checks exist because a lock that silently fails open is worse than no lock: it
converts "unprotected" into "believed protected".

**Why `MIN_TENANT_UID`.** System uids (< 1000) belong to infrastructure that legitimately holds
the device continuously — telemetry exporters, the broker's own tooling. Treating them as tenants
would make every holder-gated action permanently refusable on any instrumented host. Treating
uids ≥ 1000 as tenants is the conservative default: a human's process is never reset over. Spec 04
(I6, I7) defines how the reset gate and the automatic ladder consume the boundary.

**Why the exit file defers to the broker's own reaping.** A record written by the job is the only
exit evidence that survives a broker restart (the scope reports its exit only to whoever spawned
it), but it is also job-side state; preferring the broker's own `waitpid` result whenever the
broker witnessed the exit keeps the authoritative path kernel-sourced and confines reliance on the
job-side record to the one case with no alternative.

## Test anchors

| Claim | Test(s) |
|---|---|
| I1 (credential parse) | `tests/test_peercred.py::TestReadPeerCredentials::test_parses_pid_uid_gid`, `tests/test_peercred.py::TestReadPeerCredentials::test_distinct_uid` |
| I1 (graceful None) | `tests/test_peercred.py::TestReadPeerCredentials::test_returns_none_on_oserror`, `tests/test_peercred.py::TestReadPeerCredentials::test_returns_none_when_attribute_missing`, `tests/test_peercred.py::TestReadPeerCredentials::test_returns_none_on_short_blob` |
| I1 (scope stamp → contextvar) | `tests/test_socket_transport.py::TestPeerUidScope::test_peer_uid_from_unix_scope`, `tests/test_socket_transport.py::TestPeerUidScope::test_middleware_publishes_and_clears_contextvar` |
| I1 (peer uid overrides self-report) | `tests/test_authz.py::TestAuthzOwner::test_socket_overrides_reported_owner` |
| I2 (HTTP anonymous) | `tests/test_socket_transport.py::TestPeerUidScope::test_tcp_scope_has_no_peer_uid`, `tests/test_socket_transport.py::TestPeerUidScope::test_missing_client_is_none`, `tests/test_authz.py::TestAuthzOwner::test_http_uses_reported_owner`, `tests/test_authz.py::TestAuthzOwner::test_http_unknown_when_no_owner` |
| I2 (privsep refuses identity-less) | `tests/test_privsep_guard.py::test_guard_refuses_identity_less_under_privsep`, `tests/test_privsep_guard.py::test_guard_allows_when_peer_uid_present`, `tests/test_privsep_guard.py::test_no_guard_when_privsep_disabled` |
| I3 (refuse, never root fallback) | `tests/test_privsep.py::test_privsep_refusal_refuses_identity_less`, `tests/test_privsep.py::test_privsep_refusal_refuses_uid_without_passwd`, `tests/test_privsep.py::test_privsep_refusal_allows_root`, `tests/test_privsep.py::test_privsep_refusal_allows_resolvable_uid`, `tests/test_privsep.py::test_privsep_refusal_none_when_inactive` |
| I3 (prefix construction) | `tests/test_privsep.py::test_privsep_prefix_for_builds_when_enabled`, `tests/test_privsep.py::test_privsep_prefix_for_skips_root_and_unknown_uid`, `tests/test_privsep.py::test_privsep_prefix_for_returns_none_when_disabled` |
| I4 | `tests/test_privsep.py::test_privsep_enabled_flag`, `tests/test_privsep.py::test_should_privsep_requires_root_and_systemd` |
| I5 (owner matching) | `tests/test_authz.py::TestOwnerMatches::test_exact_match`, `tests/test_authz.py::TestOwnerMatches::test_mismatch`, `tests/test_authz.py::TestOwnerMatches::test_agent_prefixed_job_matches_bare_user`, `tests/test_authz.py::TestOwnerMatches::test_agent_prefix_does_not_match_other_user` |
| I5 (kill audit) | `tests/test_recent_jobs.py::test_an_interrupted_job_names_the_user_who_killed_it` |
| I6 (owner and tag derived, never reported) | `tests/test_device_safety.py::test_the_owner_comes_from_the_peer_uid_and_the_surface`, `tests/test_reset.py::test_a_reset_row_is_owned_by_the_caller_not_by_what_they_posted` |
| I6 (the surface is derived from the request path, not reported) | `tests/test_socket_transport.py::TestPeerUidScope::test_the_middleware_derives_the_surface_from_the_request_path` |
| I6 (end to end: the surface that carried the submit is what tags it) | `tests/test_socket_transport.py::test_the_surface_that_carried_the_submit_is_what_tags_the_owner` |
| I6 (no tool accepts an owner field) | `tests/test_authz.py::test_no_tool_takes_an_owner_field` |
| I6 (no peer identity means no tag) | `tests/test_authz.py::test_without_a_peer_identity_there_is_no_agent_tag` |
| I6 (uid rendering) | `tests/test_peercred.py::TestUsernameForUid::test_root_resolves`, `tests/test_peercred.py::TestUsernameForUid::test_unknown_uid_falls_back`, `tests/test_peercred.py::TestUsernameForUid::test_credentials_username_property` |
| I7 (re-adopted exit recovery) | `tests/test_readopt.py::test_readopted_job_recovers_its_real_exit_code`, `tests/test_readopt.py::test_a_signalled_job_records_a_real_exit_status` |
| I7 (no status ≠ completed) | `tests/test_readopt.py::test_readopted_job_with_no_exit_status_is_not_called_completed` |
| I7 (redirected dir not widened) | `tests/test_readopt.py::test_a_redirected_job_exit_dir_is_not_made_world_writable`, `tests/test_readopt.py::test_the_job_exit_dir_is_redirectable` |
| I8 (degraded / live-probe gate) | `tests/test_device_safety.py::test_exec_refuses_the_device_the_broker_is_working_on`, `tests/test_device_safety.py::test_exec_refuses_a_chip_off_the_bus` |
| I8 (force alongside foreign, audited) | `tests/test_device_safety.py::test_exec_force_runs_a_diagnostic_alongside_a_foreign_job` |
| I8 (scoped exec reaping) | `tests/test_device_safety.py::test_exec_timeout_scope_routes_a_privsep_diagnostic`, `tests/test_device_safety.py::test_exec_timeout_kills_the_command_it_gave_up_on` |
| I9 (self-checks decide) | `tests/test_cli.py::test_cmd_lock_locks_when_deny_probe_is_refused`, `tests/test_cli.py::test_cmd_lock_refuses_when_deny_probe_opens_the_node` |
| I9 (banner tracks state) | `tests/test_cli.py::test_refresh_banner_tracks_lock_state` |
| I10 (wrapper + sudoers) | `tests/test_cli.py::test_install_smi_ro_wrapper_creates_wrapper_and_sudoers`, `tests/test_cli.py::test_install_smi_ro_wrapper_skips_sudoers_on_visudo_failure`, `tests/test_cli.py::test_smi_ro_wrapper_rejects_reset_flag`, `tests/test_cli.py::test_remove_smi_ro_wrapper_is_idempotent` |
| I10 (streaming smi allowlist) | `tests/test_reset.py::test_smi_args_ok_allows_readonly_rejects_reset` |
| I11 (anonymous fails closed) | `tests/test_reset.py::test_privsep_http_reset_refuses_over_a_foreign_holder`, `tests/test_reset.py::test_privsep_http_reset_allows_a_provably_idle_device`, `tests/test_reset.py::test_non_privsep_http_reset_keeps_the_legacy_skip`, `tests/test_reset.py::test_privsep_streaming_reset_refuses_over_a_foreign_holder` |
| I11 (tenant boundary) | `tests/test_reset_gate.py::TestForeignHolders::test_ignores_system_holders`, `tests/test_reset_gate.py::TestForeignHolders::test_foreign_holders_filters_caller` |
| B-Privsep launch (gid modes) | `tests/test_privsep.py::test_systemd_run_prefix_cooperative_keeps_user_gid`, `tests/test_privsep.py::test_systemd_run_prefix_lockdown_uses_device_gid`, `tests/test_privsep.py::test_cooperative_mode_keeps_user_gid`, `tests/test_privsep.py::test_lockdown_uses_device_gid_via_env` |
| B-Kill (scope-aware terminate) | `tests/test_reset.py::test_a_live_privsep_kill_signals_the_scope_not_the_pgroup`, `tests/test_reset.py::test_terminate_job_falls_back_to_killpg_without_a_scope` |
