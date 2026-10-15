<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Transports

Scope: how requests reach the broker — the unix-socket MCP transport, the stdio shim, the REST
surface, the MCP tool inventory, and the HTTP/TCP listener (target: removed).

## Purpose

Every client of the broker — an MCP client (Claude via the stdio shim), the developer CLI, a
health poller — is a local process on the same host. The transport layer's job is to carry all of
them to one shared core (spec 01's queue helpers, spec 03's gate) over one ASGI app, and to attach
a real, unforgeable caller identity on the way in. The unix socket does both; it is the normative
transport. The HTTP/TCP listener predates it, attaches no identity, and is to be removed
(see `Target: remove HTTP/TCP`).

## Invariants

- **I1 — One app, all surfaces.** The MCP endpoint (`/mcp`), the REST surface (`/api/*`), and
  `/health` are one Starlette ASGI app (`server.build_asgi_app`). A transport serves all three or
  none; there is no route reachable on one transport but not another.
- **I2 — Transport-independent semantics.** For every operation exposed both as an MCP tool and a
  REST route, both call the same shared helper (`_queue_job`, `_get_job_status`, `_get_job_logs`,
  `_kill_job`, `_get_queue_status`, `_exec_impl`, `_reset_device`). Limits and refusals enforced
  in the helper (e.g. spec 01's `MAX_TIMEOUT_SEC` clamp) hold identically on every ingress path.
- **I3 — Transport identity exists on the socket and only on the socket.** A request arriving over
  the unix socket carries the peer's real uid (SO_PEERCRED), stamped per request into
  `socket_transport.current_peer_uid`; a TCP/HTTP request carries none (`None`, unauthenticated).
  The same middleware stamps which surface the request arrived on — `current_via_mcp`, false for
  `/api/*` and true otherwise — which is what lets spec 05 tell a CLI call from an agent's without
  asking the caller. Peercred mechanics and owner authz are spec 05; the transport-level fact is:
  socket = authenticated identity, HTTP = no identity.
- **I4 — The stdio shim is a socket-only MCP proxy.** The shim (`stdio_shim.py`) serves stdio MCP
  to its client and forwards `tools/list` and `tools/call` to the broker over its unix socket —
  never TCP. Upstream resolution order: explicit/`TT_DEVICE_MCP_SOCKET`, else the host broker
  socket, else this user's standalone daemon, lazy-started if absent.
- **I5 — One binary, unambiguous routing.** The bare `tt-device-mcp` binary with a piped (non-tty)
  stdin is an MCP client attaching: `cli.main` routes to the stdio shim. On a tty it is a human:
  the CLI (help) answers, never the shim.
- **I6 — Tool naming and annotations.** Every MCP tool name carries the `tt_device_` prefix. Every
  tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`
  annotations (all `openWorldHint: false`), and takes a Pydantic input model with `Field()`
  descriptions (exception: `tt_device_recent_jobs` takes a bare `limit: int`).
- **I7 — The synthetic Host is served.** The app must answer requests whose `Host` is the shim's
  synthetic `tt-device-broker` (any UDS client sends a synthetic Host). SDK host validation /
  DNS-rebinding protection must never 421 the socket path.
- **I8 — The CLI's REST client is socket-only.** `utils.api_call` resolves a unix socket
  (explicit/env, broker, user daemon — `constants.resolve_socket`) and speaks HTTP over it; its
  `port`/`host` parameters are ignored. No reachable server means a refusal, not a TCP attempt.
- **I9 — Streaming endpoints are not a side door.** `/api/tt_device_reset_stream` runs the same
  reset gate, busy check, poller quiesce, systemd scope, and audit row as the `tt_device_reset` tool; only the
  delivery (progress lines as they happen) differs.
- **I10 — Socket-only serving is self-consistent.** `--no-http` requires a socket
  (`--socket` or `TT_DEVICE_MCP_SOCKET`); with HTTP disabled and no socket configured the broker
  refuses to start rather than serve nothing.
- **I11 — Two routes deliberately have no tool.** `/api/tt_device_pre_step` and
  `/api/tt_device_post_step` are registered as REST routes with no MCP tool counterpart: they are
  a scheduler's lifecycle hooks, refuse any non-root peer, and an agent's device work belongs in
  the queue. A route without a tool is intentional here, not an omission.

## Interfaces

### MCP tool inventory

All tools registered in `server.create_mcp_server()`; blocking tools (`job_run`, `job_wait`) take
an MCP `Context` and stream logs via progress updates. Job semantics are spec 01.

| Tool | Purpose | readOnly | destructive | idempotent | Input model |
|---|---|---|---|---|---|
| `tt_device_job_run` | Queue and wait (blocking, streams logs) | no | no | no | `JobRunInput` |
| `tt_device_job_run_bg` | Queue only (non-blocking) | no | no | no | `JobSubmitInput` |
| `tt_device_job_status` | Poll status/results | yes | no | yes | `JobIdInput` |
| `tt_device_job_wait` | Block until done, stream logs | yes | no | yes | `JobWaitInput` |
| `tt_device_job_logs` | Get log content | yes | no | yes | `JobLogsInput` |
| `tt_device_job_kill` | Kill running / cancel queued (owner-gated, spec 05) | no | yes | no | `JobKillInput` |
| `tt_device_queue_status` | Running/queued jobs, device busy | yes | no | yes | — (no args) |
| `tt_device_recent_jobs` | Recent job history, all users | yes | no | yes | bare `limit` |
| `tt_device_exec` | Direct diagnostic outside the queue (gated) | no | yes | no | `DeviceExecInput` |
| `tt_device_reset` | Reset TT devices (reset gate, busy check, spec 04) | no | yes | yes | `DeviceResetInput` |

### REST route inventory

All under the same app (I1); the CLI reaches them through `mcp_tool_call` → `api_call` over the
socket (I8). All `/api/*` routes are POST with a JSON body.

| Route | Method | Shared helper | CLI consumers |
|---|---|---|---|
| `/health` | GET | `_health_payload` | `daemon status`, reachability probe before every command; installers' post-start check |
| `/api/tt_device_job_run_bg` | POST | `_queue_job` | `run`, `run-bg` |
| `/api/tt_device_job_status` | POST | `_get_job_status` | `status <id>`, `wait`, `logs`, `kill` (lookup) |
| `/api/tt_device_job_logs` | POST | `_get_job_logs` | `logs` |
| `/api/tt_device_job_kill` | POST | `_kill_job` | `kill` |
| `/api/tt_device_queue_status` | POST | `_get_queue_status` | `status`, `watch`, `wait` |
| `/api/tt_device_recent_jobs` | POST | `_recent_jobs` | `status`, `watch` (overview) |
| `/api/tt_device_exec` | POST | `_exec_impl` | `exec` |
| `/api/tt_device_reset` | POST | `_reset_device` | — (CLI uses the stream route) |
| `/api/tt_device_reset_stream` | POST | `_reset_stream` (StreamingResponse) | `reset` |
| `/api/tt_device_smi_stream` | POST | `_smi_stream` (StreamingResponse) | — (no in-repo consumer; `smi` execs tt-smi locally) |
| `/api/tt_device_pre_step` | POST | `_pre_step_impl` | REST only — no MCP tool |
| `/api/tt_device_post_step` | POST | `_post_step_impl` | REST only — no MCP tool |

`/health` payload fields (`status`, `fsm_state`, `fsm_why`, `held*`, `device_degraded`, queue
counts, version) are specified in spec 03 `Interfaces`.

### Sockets and discovery

- Host broker socket: `constants.DEFAULT_SOCKET` = `/run/tt-device-broker/broker.sock`
  (systemd unit chmods it 0666 once it appears).
- Per-user daemon socket: `constants.user_socket_path()` =
  `<state dir>/daemon.sock`, state dir `/tmp/tt-device-mcp-<uid>` (override
  `TT_DEVICE_MCP_STATE_DIR`).
- Client-side resolution (`constants.resolve_socket`): explicit arg / `TT_DEVICE_MCP_SOCKET`
  wins unconditionally; else the broker socket if it exists; else the user daemon socket if it
  exists; else none.
- Server-side (`socket_transport.resolve_socket_path`): `--socket` flag, else
  `TT_DEVICE_MCP_SOCKET`, else the socket transport is off.

## Behavior

### Socket serving

`serve_unix_socket(app, path)` removes a stale socket left by a crashed run, then binds uvicorn to
the UDS with a protocol subclass that reads SO_PEERCRED at connection time and stamps
`("peercred", uid)` into the ASGI scope's `client`; `PeerCredMiddleware` publishes that uid into
the `current_peer_uid` contextvar for exactly the request's duration (spec 05 consumes it). A
socket-transport start failure is logged, never fatal — but under `--no-http` there is then
nothing to serve and the broker exits (I10). Under socket-only serving the process blocks on the
socket server's serve task.

### Shim lifecycle

- **Spawn**: an MCP client executes the bare `tt-device-mcp` binary with piped stdin; `cli.main`
  detects argv-empty + non-tty stdin and hands off to `stdio_shim.serve_stdio` (I5). Logging goes
  to stderr only; stdout is the MCP stream and must not be polluted.
- **Upstream**: per I4 resolution. When neither broker nor user-daemon socket exists, the shim
  launches `tt-device-mcp daemon start` detached and waits up to ~15 s for the socket. The
  connection is httpx-over-UDS with the synthetic base URL `http://tt-device-broker/mcp` (I7).
  Timeouts: 30 s connect/write/pool, 300 s SSE read (a blocking tool call holds the channel for a
  job's runtime; sse-starlette's 15 s keepalives fill the gap between reads). The read timeout is
  a gap budget, never a cap on a call: the broker's SSE keepalives fill the gap, so a tool that
  sends nothing for longer (a blocking job, a mesh `tt_device_reset`, about 26 minutes worst case
  once it holds the device, spec 04 I13) still returns its result. A stream cut for another reason (a broker restart) is
  retried like a failed connect, which re-sends the call.
- **Reconnect**: the stdio session (the client's view) lives for the whole session; each
  `tools/list` / `tools/call` opens a fresh upstream session, retrying up to 12 times at 1 s
  backoff — sized to cover a systemd restart — so a broker bounce blips one call's connect, never
  the client's session. After the budget: a raised error on that call, session intact. A
  `tools/call` is retried only while it provably never reached a tool: the connect and `initialize`,
  a refused connect, or a restarted broker's 404 for the old session id (`Session not found`). Past
  that the broker may already be running it (a job submit, a reset), so a stream cut (a broker
  restart) comes back as an `is_error` result saying the call was not re-sent, never as a second
  send. Any other JSON-RPC error on a `tools/call`, including one the SDK makes from an HTTP error
  status, is passed on unretried. `tools/list` is read-only and is retried whole on any failure.
- **Transparency**: upstream results are returned unaltered (preserving `is_error`, structured
  content, pagination cursors); `_meta` is forwarded on calls so progress tokens reach the broker.

### REST semantics

- `/api/*` routes parse a JSON body and return JSON. Client-input failures come back as
  `{"error": ..., "hint": ...}` bodies, not 5xx tracebacks — a bad env file on submit is a
  refusal shaped like every other refusal.
- Streaming routes return chunked bodies: `reset_stream` emits human-readable progress lines
  (`text/plain`) with a trailing `::status::<reset_complete|reset_unhealthy|reset_unverified|reset_failed|refused|no_devices>`
  sentinel the CLI parses for its exit code; `smi_stream` streams raw pty bytes
  (`application/octet-stream`), read-only-allowlisted, deliberately parallel to running jobs.
- `reset_stream` keepalives are opt-in. A body with `"keepalive": true` gets a `::keepalive::`
  line after every quiet `RESET_STREAM_KEEPALIVE_SEC` (15 s), so a reset that is silent for
  minutes (quiesce, an overrun `tt-smi`, the post-reset check) still puts bytes on the wire for
  a client reading with a per-read timeout. The broker waits on the same pending step again; a
  quiet interval never cancels it. A client that does not ask gets no keepalives (an older CLI
  would print the sentinel). A client that leaves mid-reset closes the stream, not the reset:
  the reset runs to the end and the post-reset check is skipped, with or without keepalives.
- Client-side error shape (`utils.api_call`): HTTP ≥ 400 becomes `{"error": "HTTP <status>: ..."}`;
  a connect failure becomes `{"error": "Connection failed: ..."}`; no reachable socket becomes an
  `{"error": ...}` naming both remedies (broker vs `daemon start`).
- Reachability: every CLI command probes `/health` first and refuses with guidance when no server
  answers; `daemon start` refuses when a healthy broker socket is present (a per-user daemon would
  be shadowed, never used).

## Target: remove HTTP/TCP

**What exists today.** `run_transports(mcp, port, socket_path, serve_http=True)` serves the same
ASGI app over TCP (`uvicorn`, `0.0.0.0:<port>`, default `constants.DEFAULT_PORT` = 8333) unless
`--no-http` is passed. Every deployed entry point passes it — verified against the actual
launchers:

- system broker: the systemd unit rendered by `deploy/apply-host-config.sh` (run by
  `deploy/install-tt-device-broker.sh`) — `ExecStart=... --socket $SOCK --no-http ...`;
- per-user daemon: `cli.py` `daemon start` and `daemon start-fg` both build
  `--socket <sock> --no-http` argv.

So no install path serves TCP. What still can: a bare `python -m tt_device_mcp.server` (HTTP on
8333 is the default when `--no-http` is absent), and one residual client path — the CLI's
`_open_reset_stream` falls back to a TCP `HTTPConnection(host, port)` when no socket resolves
(dead in practice: `is_daemon_running` has already refused by then, since `api_call` is
socket-only). HTTP requests carry no peer identity (I3): owner is self-reported, and the reset
gate over HTTP fails closed under privsep (an anonymous caller owns no holder) but keeps the
legacy ungated skip off privsep. `streamable_http_app()` is passed a non-loopback `host` so the
SDK does not auto-enable DNS-rebinding protection.

**It is to be removed.** The TCP listener, the `--port`/`--no-http` flags, `DEFAULT_PORT`, the
`serve_http` parameter, and the CLI's TCP fallback go; the unix socket becomes the only transport
and mandatory (today's `--no-http requires --socket` refusal, unconditionally).

Tracking: the first spec-driven implementation PR after this spec suite lands (per
CONTRIBUTING's workflow) — spec diff first, then the removal, in its own PR.

**What the removal must preserve.**

- The stdio shim's synthetic `tt-device-broker` Host over the socket (I7). The
  `streamable_http_app(host=...)` workaround exists for the socket path, not for TCP — host
  validation must stay off (or equivalently permissive) after TCP goes, or the only remaining
  transport 421s.
- The full REST surface as reachable over the socket — every route in the inventory above, since
  the CLI and tt-run are REST-over-UDS clients.
- `/health` over the socket (installers, `daemon status`, and pollers curl it via
  `--unix-socket`).
- The identity model simplifies, it must not weaken: with no identity-less transport left, every
  request has a peer uid; the HTTP-legacy branches (self-reported owner, the non-privsep reset
  gate skip) become unreachable and can be retired with the listener (spec 05).

## Design decisions

- **Socket-only rationale.** Exec-locality: every legitimate client — the shim, the CLI, tt-run,
  pollers — is a process on the same host, so a TCP listener buys reach nobody needs. And
  localhost HTTP is an authz hole, not a convenience: it carries no peer identity, so a kill is
  authorized against whatever owner the caller typed, while the socket authenticates every request
  for free via SO_PEERCRED. Removing TCP removes the last surface where a caller's own word is
  read at all.
- **Streamable HTTP framing, UDS carriage.** The MCP endpoint uses the SDK's modern
  `streamable_http_app()` (not deprecated SSE transport), stateful, at `/mcp` — those are
  arguments to that call, not to `MCPServer()`. The protocol keeps its HTTP framing; only the
  byte carrier is the unix socket. Listen addresses belong to uvicorn in `run_transports`.
- **Host validation stays off.** A loopback `host` makes the SDK auto-enable DNS-rebinding
  protection, which 421s any unrecognised `Host` — including the shim's synthetic
  `tt-device-broker`, i.e. the one path every tenant uses. Hence the non-loopback `host`
  argument. This survives TCP removal (see Target).
- **Port 8333 (historical).** Chosen to dodge common listeners — 8000 (vLLM), 8086 (Tracy),
  8100 (vLLM lmcache), 8888 (Jupyter). With TCP removed the rationale is history; today
  `DEFAULT_PORT`'s own comment records that the socket-based broker never binds it.
- **One app, two carriers.** Serving the identical ASGI app over every transport (I1/I2) is what
  keeps REST and MCP from drifting: a limit or refusal added to a shared helper is enforced
  everywhere at once, and the test suite can drive the real wire path in-process.
- **Per-call upstream sessions in the shim.** Holding one upstream session would tie the client's
  stdio session to the broker's uptime; a fresh session per call plus a retry budget makes a
  broker restart invisible except as one call's ~seconds of connect latency. The budget stops at
  the send because the shim cannot tell whether a cut call ran: re-sending a submit or a reset
  could run it twice, while the caller can check `tt_device_queue_status` and decide.

## Test anchors

| Claim | Test(s) |
|---|---|
| I1 / I7 | `tests/test_server.py::test_the_app_serves_the_shims_synthetic_socket_host`, `tests/test_socket_transport.py::test_socket_jsonrpc_round_trip` |
| I2 | `tests/test_device_safety.py::test_rest_submit_clamps_timeout_to_the_hard_ceiling`, `tests/test_reset.py::test_privsep_streaming_reset_refuses_over_a_foreign_holder` |
| I3 (uid stamped on socket, none on TCP) | `tests/test_socket_transport.py::TestPeerUidScope::test_peer_uid_from_unix_scope`, `tests/test_socket_transport.py::TestPeerUidScope::test_tcp_scope_has_no_peer_uid`, `tests/test_socket_transport.py::TestPeerUidScope::test_missing_client_is_none`, `tests/test_socket_transport.py::TestPeerUidScope::test_middleware_publishes_and_clears_contextvar`, `tests/test_socket_transport.py::TestPeerUidScope::test_the_middleware_derives_the_surface_from_the_request_path` |
| I3 (identity is authoritative vs self-report — spec 05 owns mechanics) | `tests/test_authz.py::TestAuthzOwner::test_socket_overrides_reported_owner`, `tests/test_authz.py::TestAuthzOwner::test_http_uses_reported_owner`, `tests/test_authz.py::TestAuthzOwner::test_http_unknown_when_no_owner` |
| I4 (resolution order, lazy daemon, no TCP) | `tests/test_stdio_shim.py::test_resolve_prefers_explicit_socket`, `tests/test_stdio_shim.py::test_resolve_auto_discovers_broker_socket`, `tests/test_stdio_shim.py::test_resolve_lazy_starts_user_daemon_when_none` |
| Shim retries the connect, never a sent `tools/call` | `tests/test_stdio_shim.py::test_a_broker_restart_mid_call_does_not_re_send_the_call`, `tests/test_stdio_shim.py::test_a_call_made_while_the_broker_is_down_is_sent_once_it_is_back`, `tests/test_stdio_shim.py::test_an_error_reply_from_the_broker_is_passed_on_not_retried`, `tests/test_stdio_shim.py::test_a_call_that_never_reached_a_tool_is_retried`, `tests/test_stdio_shim.py::test_a_wrapped_failure_is_retried_only_if_every_part_says_never_delivered` |
| Shim read timeout is a gap budget, not a call cap | `tests/test_stdio_shim.py::test_a_tool_call_longer_than_the_read_timeout_completes_on_keepalives` |
| I5 | `tests/test_cli.py::test_main_bare_piped_stdin_runs_stdio_adapter`, `tests/test_cli.py::test_main_bare_tty_shows_help_not_adapter` |
| I6 (tool names present over the wire) | `tests/test_socket_transport.py::test_socket_jsonrpc_round_trip` |
| I8 (server-side socket resolution) | `tests/test_socket_transport.py::TestResolveSocketPath::test_cli_value_wins`, `tests/test_socket_transport.py::TestResolveSocketPath::test_env_fallback`, `tests/test_socket_transport.py::TestResolveSocketPath::test_disabled_when_unset` |
| I8 (CLI refuses without a reachable server) | `tests/test_cli.py::test_cli_run_requires_daemon` |
| I9 | `tests/test_reset.py::test_streaming_reset_quiesces_pollers_and_flags_in_flight`, `tests/test_reset.py::test_reset_stream_quiesce_scope_routes_a_live_privsep_job`, `tests/test_reset.py::test_the_sampler_recognises_a_streaming_resets_scope` |
| MCP layer injects Context / progress streaming works end-to-end | `tests/test_server.py::test_a_blocking_job_run_reports_progress_through_the_mcp_layer` |
| `reset_stream` keepalives: opt-in, sent on a quiet reset, progress and status kept | `tests/test_reset.py::test_a_silent_reset_stream_sends_keepalives_when_asked` |
| `reset_stream`: a client leaving mid-reset leaves the reset running and the stream closed, under both ASGI disconnect paths | `tests/test_reset.py::test_a_client_that_leaves_a_silent_reset_does_not_stop_it` |
| REST errors are refusals, not 500s | `tests/test_device_safety.py::test_rest_submit_bad_env_file_is_a_refusal_not_a_500` |
| /health payload (spec 03) reachable and hold-aware | `tests/test_device_safety.py::test_health_payload_reports_a_held_device_as_degraded`, `tests/test_device_safety.py::test_health_payload_reads_ok_on_a_fit_device` |
| exec gating identical via shared helper | `tests/test_device_safety.py::test_exec_refuses_the_device_the_broker_is_working_on`, `tests/test_device_safety.py::test_exec_force_runs_a_diagnostic_alongside_a_foreign_job` |
| Target (a): HTTP reset-gate degradation as it exists today | `tests/test_reset.py::test_privsep_http_reset_refuses_over_a_foreign_holder`, `tests/test_reset.py::test_privsep_http_reset_allows_a_provably_idle_device`, `tests/test_reset.py::test_non_privsep_http_reset_keeps_the_legacy_skip` |
| `daemon start` refuses under a live broker | `tests/test_cli.py::test_daemon_start_refuses_on_broker_host` |
| I11 (root-only, no tool) | `tests/test_slurm_steps.py::test_both_step_routes_refuse_a_non_root_peer` |
| pre-step: read-only, busy refusal, verdict | `tests/test_slurm_steps.py::test_pre_step_never_recovers`, `tests/test_slurm_steps.py::test_pre_step_refuses_while_a_broker_job_is_in_flight`, `tests/test_slurm_steps.py::test_the_in_flight_guard_catches_a_readopted_jobs_scope_until_it_ends`, `tests/test_slurm_steps.py::test_pre_step_reports_ok_on_a_healthy_free_device` |
| post-step: reclaim-then-gate, fabric on failure, survivor refusal | `tests/test_slurm_steps.py::test_post_step_reclaims_then_runs_the_gate`, `tests/test_slurm_steps.py::test_post_step_no_reclaim_skips_the_kill`, `tests/test_slurm_steps.py::test_post_step_forces_the_fabric_pass_on_a_failed_step`, `tests/test_slurm_steps.py::test_post_step_reports_a_surviving_straggler_as_refused` |
| deadlines return a verdict, never hang | `tests/test_slurm_steps.py::test_a_step_that_exceeds_its_deadline_is_inconclusive` |
| phase labels stay a closed set | `tests/test_slurm_steps.py::test_the_step_phase_labels_are_the_closed_set` |
