<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# CLI

Scope: the developer CLI (`cli.py`) — every `tt-device-mcp` subcommand's contract, the per-user
daemon lifecycle commands, exit codes, output conventions, and what the CLI captures and sends at
submit time. Server-side semantics live elsewhere: env resolution is spec 01, transports/routes
spec 02, authorization spec 05, state paths spec 06, installers spec 08.

## Purpose

The CLI is the human's client to the same broker the MCP tools serve: it submits jobs, watches the
queue, reads logs, kills its own work, and drives the reset stream — all as a REST client over the
unix socket, so a human and an agent are indistinguishable to the broker except by owner tag. Its
second job is the per-user daemon lifecycle: on a reserved box with no system broker, `daemon
start` is the only supported way to bring one up.

## Invariants

- **I1 — Pure REST client for command execution.** Every command that acts on jobs or the device
  (`run`, `run-bg`, `exec`, `status`, `logs`, `kill`, `wait`, `watch`, `reset`) goes through
  `mcp_tool_call` → `utils.api_call` over the resolved unix socket (spec 02 I8) — never a library
  import of server internals. The exceptions are the daemon lifecycle commands, which exist to
  create the server: `daemon start` builds the server argv
  (`python -m tt_device_mcp.server --socket <sock> --no-http --log-dir <state>`) and environment
  and spawns it detached; `daemon start-fg` imports `server.main` and runs it in-process with the
  same argv and environment (`per_user_daemon_env` is the single source for both, so they cannot
  diverge). `smi`, `timezone`, `lock`/`unlock`, and `refresh-banner` are local-only and contact no
  server at all.
- **I2 — Reachability before action.** Every server-touching command probes `GET /health` first
  (`is_daemon_running`: 3 attempts, 10 s timeout each, 0.5 s between) and refuses with guidance
  when no server answers. `status: "degraded"` counts as reachable — a held device leaves the
  broker up and serving. The refusal distinguishes a wedged server (its socket exists: retry /
  ask an admin to restart) from an absent one (start the broker, or `daemon start` on a reserved
  box).
- **I3 — `daemon start` never shadows a broker.** When the system broker socket
  (`constants.DEFAULT_SOCKET`) exists and answers healthy, `daemon start` exits 0 without
  spawning anything — the CLI would prefer the broker socket, so a per-user daemon would never be
  used. When the broker socket exists but does not answer, it exits 1 and points at
  `systemctl restart tt-device-broker` rather than starting a daemon that would be shadowed.
  (Skipped under `--log-dir`, the tests' isolation knob.)
- **I4 — `run`'s exit code is the job's outcome, not the submission's.** `run` (and `wait`) exit 0
  iff the job's final status is `completed`; every other terminal status — `failed`, `timeout`,
  `killed`, `hung` — exits 1 (a doomed reap lands as `hung`, spec 01 I10). The child's own exit code is printed, not propagated.
  `exec` is the exception: it propagates the diagnostic's `exit_code` verbatim.
- **I5 — Bare-binary routing is spec 02 I5.** No argv + piped (non-tty) stdin → the stdio shim; a
  tty → argparse help (exit 1). Any argv is a CLI verb.
- **I6 — Submit-time capture is fixed and client-side.** With no `-e`, the CLI captures exactly
  the `CAPTURED_ENV_VARS` allowlist from its own environment and sends it as `inherited_env`;
  with `-e <file>` it sends the file path and `inherited_env: null`. The workspace sent is
  `-w` or the CLI's cwd — nothing else is resolved client-side; the `<workspace>/tt-metal`
  defaults and the env-file replacement semantics are the server's (spec 01 I11).
- **I7 — `smi` never opens the device with write intent and never proxies.** It rejects any flag
  outside the read-only allowlist (`_SMI_SAFE_FLAGS`) with exit 2, then `execvp`s the real
  `tt-smi` locally — on a locked host via the root-installed NOPASSWD wrapper
  (`sudo -n /usr/local/bin/tt-device-mcp-smi-ro`), which enforces the same allowlist itself
  because any user may exec it. It does not use the broker's `smi_stream` route.
- **I8 — The daemon's state directory must be securable or the daemon does not start.**
  `_daemon_state_dir` chmods the base 0700 every time and exits with the path, its owner, and the
  overrides (`--log-dir` / `TT_DEVICE_MCP_STATE_DIR`) when it cannot — the base sits in shared
  `/tmp` and a pre-created directory is never silently accepted (spec 05/06 own the threat model
  and paths).
- **I9 — The step verbs carry one bit.** `pre-step` and `post-step` exit 0 iff the broker proved
  the device fit (and, for `pre-step`, free); every other outcome — unfit, refused, deadline
  overrun, unreachable broker — is 1. A scheduler reads that bit to drain a node and requeue a
  job, so folding transport failure in with device failure is deliberate: neither is a device
  anyone should dispatch onto. `post-step`'s `--exit-code` carries the finished step's status
  inward (it forces the fabric pass); nothing carries outward but the bit and the printed reason.

  `--exit-code` accepts every shape a scheduler actually reports one in, because only its
  zero/nonzero bit is consumed. Slurm's are not the plain small integers their names suggest:
  `SLURM_JOB_EXIT_CODE` is a wait(2) status, so a real exit of 1 arrives as 256, and the
  `<exit>:<signal>` spelling turns up too — `0:0` for a clean step, `0:9` for one a signal
  killed. A strict `type=int` rejected `0:0`: argparse exited 2, the epilogue never contacted the
  broker, and Slurm drained the node over a job that finished fine. The shipped Epilog normalizes
  both variables to the bit before the call, and the parser accepts the shapes anyway so a
  site's own hook cannot hit the same trap. Anything unparseable becomes 1 here — a step whose
  outcome cannot be read is not evidence the device is clean — while the server's own
  `_parse_post_step_exit_code` deliberately falls to 0, since there the input is a request body
  and forcing on garbage would let any caller buy itself a ~45s traffic pass.
  The deadline that produces a "deadline overrun" bounds only the CLI's own reply, not the
  broker's device work: server-side, `post-step`'s deadline is one absolute budget for the WHOLE
  route — the straggler reclaim and the gate together, not the gate alone, since a two-round
  reclaim (SIGTERM, then SIGKILL, with a real grace sleep between) is not free either. The route
  governs how long it waits before answering `inconclusive` — it does not cancel a reclaim
  mid-signal or a recovery-ladder climb already in progress, and does not detach the device from
  the broker's ownership of it (spec 03 I29). A real climb can take longer than the 600s default
  on its own; when the deadline fires mid-reclaim or mid-climb the CLI reports `inconclusive`
  immediately, while the broker keeps the device reserved against a new job dispatch until
  whichever phase was still running actually finishes. Raising the deadline is a deliberate choice
  left to the maintainer; detaching the device from it is not offered, by design — a step's reply
  is not proof the device is free to hand to anyone. The consequence is unbounded and can outlast
  the deadline indefinitely: a gate task whose `to_thread` read never returns (spec 03's
  wedged-chip read-hang signature) stalls job dispatch until the broker restarts, not until any
  clock expires. That cost is defensible only because it is diagnosable, not silent: `job_runner`
  logs a `job_deferred_for_external_step` health_event and a matching log line the moment it
  defers on the reservation, naming the holder, and `tt_device_queue_status`/`tt-device-mcp
  status` names it too (`external_step_active`, and a RUNNING row reading "external step
  reserved: ...").

  The deadline env vars themselves (`TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC`,
  `TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC`) accept only a positive, finite value —
  `constants.step_deadline_sec` falls back to the default on `inf`, `-inf`, `nan`, or anything
  that does not parse as a float, the same way it already falls back on a non-positive one. A bare
  `val > 0` check would have accepted `float("inf")`, which disables the deadline outright and
  defeats the one guarantee this whole surface exists to keep: a step always returns a verdict.
  `step_client_timeout_sec` (the CLI's own socket timeout, derived from the same var) inherits the
  same floor for free, since it calls `step_deadline_sec` rather than parsing the var itself.

## Interfaces

Global flags: `--version/-V`; `--port/-p` (accepted, ignored — `api_call` is socket-only, spec 02
I8); `--log-dir/-l` (repoints the per-user daemon's state dir and socket; also the tests'
isolation knob).

| Command | Args / flags | REST route(s) (spec 02) | Output | Exit code |
|---|---|---|---|---|
| `daemon start` | — | `GET /health` (probe + readiness poll, ~10 s) | started PID + socket, or refusal | 0 started / already running / broker present; 1 spawn failed, timeout, or unresponsive broker socket |
| `daemon stop` | — | none (pid file + signals) | `Stopped` / `Not running` | always 0 |
| `daemon status` | — | `GET /health` | `Running (socket ...)` / `Not running` | 0 running; 1 not |
| `daemon start-fg` | — | none (in-process `server.main`) | server's own output | server's |
| `run` | `<cmd> [-w dir] [-e file] [-t sec] [-o N]` | `/api/tt_device_job_run_bg`, then poll `/api/tt_device_job_status` | queue banner, streamed log, finish summary (+ last N lines with `-o`) | 0 iff `completed` (I4); 1 otherwise / server down / submit refused |
| `run-bg` | `<cmd> [-w dir] [-e file] [-t sec]` | `/api/tt_device_job_run_bg` | job id, queue position, log path | 0 queued; 1 refused / server down |
| `status` | `[N] [-j ID]` | `/api/tt_device_queue_status` + `/api/tt_device_recent_jobs`; with `-j`: `/api/tt_device_job_status` | overview (RUNNING/QUEUED/RECENT, default 15 recent) or one job's detail | 0; 1 on error / server down |
| `logs` | `[job_id] [-f] [-n N]` | `/api/tt_device_job_status` (+ queue lookup for default job); plain: `/api/tt_device_job_logs` | log tail (default 100 lines) or a local `tail -f` | 0; 1 no job found / error |
| `kill` | `[job_id]` | `/api/tt_device_job_status`, `/api/tt_device_queue_status`, `/api/tt_device_job_kill` | confirmation / interactive picker / result | 0 killed, nothing to kill, or user aborted; 1 refused / invalid choice |
| `wait` | `<job_id>` | `/api/tt_device_job_status` (poll) | streamed log + finish summary | 0 iff `completed`; 1 otherwise |
| `reset` | `[-f/--force]` | `/api/tt_device_reset_stream` (chunked) | progress lines live, waiting dots | 0 on `reset_complete`; 1 on `refused`, `no_devices`, any other/missing sentinel |
| `pre-step` | — (root) | `/api/tt_device_pre_step` | one verdict line; holders named when present | 0 iff device fit **and** free; 1 unfit / refused / inconclusive / server down |
| `post-step` | `[--exit-code N] [--no-reclaim]` (root) | `/api/tt_device_post_step` | reclaim + verdict lines | 0 iff device ends fit; 1 otherwise |
| `exec` | `<cmd> [-t sec] [-f/--force]` | `/api/tt_device_exec` | stdout→stdout, stderr→stderr | the diagnostic's `exit_code`; 1 on refusal / server down |
| `watch` | `[N]` | `/api/tt_device_queue_status` + `/api/tt_device_recent_jobs` (looped) | the `status` overview, redrawn every 2 s | 0 (Ctrl-C); 1 server down at start |
| `smi` | `[tt-smi args]` | none — local exec (I7) | tt-smi owns the terminal | tt-smi's; 2 disallowed flag; 1 launch failure / missing wrapper |
| `timezone` | `[City \| ±N \| pacific] [--list [TEXT]]` | none — local file | current zone + source, listing, or the new label | 0; 1 unknown/ambiguous zone, bad offset |
| `lock` / `unlock` | — (sudo) | none — udev rule, unit edit, self-checks (mechanics: spec 05) | LOCKED/UNLOCKED or self-check verdict | 0 verified; 1 not root / no broker unit / self-check failed or inconclusive |
| `refresh-banner` | — (sudo, hidden) | none | — | 0; 1 not root |
| `help` / bare tty | — | none | argparse help | 1 |

Submissions send no owner: the broker derives it (spec 05 I6). `get_owner()` = `$USER` is used
locally to pick the caller's own job out of a list, and is sent only by `kill`, where the socket's
peercred identity overrides it anyway.

## Behavior

**Reachability probe.** `is_daemon_running` retries `/health` with a generous timeout because a
busy broker can take seconds to answer while a genuinely absent one fails fast; only `ok` and
`degraded` count. On failure, `_server_down_message` checks whether the resolved socket path
exists on disk to pick the wedged-vs-absent wording (I2).

**`run`'s blocking flow.** Probe → capture (`inherited_env` allowlist or `-e` path, I6) → submit
via `tt_device_job_run_bg` → print job id, position, log path → `_wait_and_stream`: every 1 s,
poll `tt_device_job_status` and stream new bytes of the job's log file directly from disk (the log
exists from queue time, spec 01 I3 — the CLI reads the file, not a log route). On a terminal
status: print status, exit code, runtime, the timeout hint when status is `timeout`, the last `-o
N` lines if asked, and exit per I4. `wait` is the same loop attached to an existing job; a job
already finished prints its status and exits 0/1 by the same rule. The CLI does not clamp `-t`;
the server's `MAX_TIMEOUT_SEC` clamp (spec 01 I2) is the ceiling. The CLI accepts an
over-ceiling `-t` without comment, so the job header — not the flag you passed — is what says
what deadline the run actually got.

**`exec`.** Client-side cap: `-t` above 600 s is cut to 600 with a printed notice, mirroring the
server's ceiling so the failure is a clear line rather than a validation error. `force` semantics
(foreign running job only, not the degraded hold) are the server's (spec 04/05).

**`status`.** A digits-only positional is the recent-jobs count (`status 30`); job detail is the
`-j ID` flag — job ids are bare digits, so they cannot share the positional (a numeric id in the
positional is read as a count). A non-digit positional is silently ignored. The overview is one
frame of `watch`: RUNNING and QUEUED from `queue_status`, RECENT from `recent_jobs` (durable
across restarts; includes broker-owned resets and execs, marked by owner icon). ANSI styling only
when stdout is a tty, so piping stays plain text. The TIME column renders server-side naive-UTC
timestamps in the caller's zone (below); recent rows are stamped with start time (execution
order), not queue time — WAIT already carries the queue delay.

**`logs`.** With no job id, the CLI picks the caller's own running-else-queued job (owner `$USER`
or `[agent]$USER`); none → exit 1 with guidance. `-f` fetches the job's log path and runs a local
`tail -f` on it (Ctrl-C exits 0); without `-f` it fetches the last `-n` lines (default 100) via
`tt_device_job_logs`.

**`kill`.** With no job id: list the caller's own jobs, confirm interactively (one job → y/n;
several → a numbered pick); nothing to kill or a user abort exits 0. Authorization is the
server's peercred check (spec 05 I5), so nothing the CLI sends can grant a foreign kill over the
socket. A server refusal prints the error
and exits 1.

**`reset`.** Opens the streaming route with a 300 s connection timeout, prints each progress line
as it arrives, and prints a `.` every 2 s while waiting so a long `tt-smi -r` is visibly alive.
The trailing `::status::<x>` sentinel (spec 02 REST semantics) decides the exit code; no sentinel
is a failure. Gate, quiesce, and audit are the server's (spec 04).

**`pre-step` / `post-step`.** Both probe reachability like every other command, then post to their
route (`pre-step` with no body; `post-step` with `{"exit_code", "reclaim"}` from `--exit-code` and
the inverse of `--no-reclaim`) and hand the response to `_print_step_verdict`: it names any
`holders`/`reclaimed`/`survivors` present, then prints one final line and returns 0 only for
`status == "ok"` (I9) — everything else, including an unreachable broker, is 1. The client
timeout for each route (default 180 s / 660 s) is `constants.step_client_timeout_sec`, computed
from the SAME env var and default the server reads for its own deadline
(`TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC` / `_POST_STEP_DEADLINE_SEC`), plus a fixed 60 s margin. A
client timeout hardcoded independent of the server's would report a transport failure for a step
the broker is still resolving the moment either deadline moved past it — this derivation is what
a single process needs to stay in lockstep with itself.

Across two processes it is not automatic: this reads the variable from the CLI's own process
environment, not the broker's, so the two agree only when whatever sets the variable puts the
same value in both — a deployment property, not something this code enforces (spec 06 catalogs
where each process gets its copy; `deploy/README.md`'s Slurm section covers what happens when
only one side is set).

**`watch`.** Client-side loop: fetch the same overview every 2 s and redraw. On a tty it enters
the alternate screen with the cursor hidden (like `top`), homes, redraws with per-line erase, and
erases the remainder — no scrollback pollution — restoring the screen on Ctrl-C; piped output
writes frames sequentially, plain. The header shows the caller's zone and current time.

**Timezone.** Resolution for the TIME column (`_resolve_tz`, cached): `TT_DEVICE_MCP_TZ` beats the
saved per-user preference file (path: spec 06), which beats `$TZ`, which beats the default
`America/Los_Angeles`. A value is a canonical IANA zone (full key, or a bare city name resolved
against IANA's one-zone-per-region table — aliases like `US/Pacific` and non-zones like
`localtime` are never offered or stored) or a bare integer, which pins a fixed UTC offset
(−12..14). `timezone` with no args shows the zone and which source it came from; `timezone <City>`
stores the resolved full key; `pacific`/`default` deletes the file; `--list` shows a curated
shortlist (bare) or searches the canonical table, redirecting an alias-only hit to canonical zones
on the same clock. A host with no tz database at all falls back to UTC for rendering — `status`
must never traceback over a missing tzdata. The `--list` and set paths still require it, and say
so rather than rendering a wrong local time.

**Per-user daemon lifecycle.** `daemon start` (after the I3 broker check) resolves the state dir
(I8) and socket, spawns the server detached with `per_user_daemon_env` — which sets no health or
recovery policy (spec 04 I17) and redirects the three root-only
paths (`TT_DEVICE_MCP_HEALTH_DIR`, `TT_DEVICE_MCP_JOB_EXIT_DIR`, `TT_DEVICE_MCP_DEVICE_OP_LOCK`)
under the state dir, warning loudly if a redirect target cannot be created — writes the pid file,
and polls `/health` for up to ~10 s before declaring success. `daemon stop` reads the pid file,
verifies via `/proc/<pid>/cmdline` that the pid is still a `tt_device_mcp.server` (a stale or
recycled pid is reported and cleaned, never signalled), SIGTERMs with a 5 s grace then SIGKILLs,
and removes pid file and socket; it always exits 0. `start-fg` runs the identical configuration
in-process for debugging.

## Design decisions

**REST client, not library import.** The daemon may be a different version from the CLI invoking
it (the system broker is installed by root and auto-updated; the CLI may come from a user's own
venv), so the only stable contract is the wire: one route inventory (spec 02), one set of
refusals, and the CLI sees exactly what an MCP agent sees. It also keeps every CLI action subject
to the socket's peercred identity instead of trusting a local import to behave.

**The reachability probe.** Every failure mode after a dead-server submit looks like a broker bug
from the user's side (a hang, a connection traceback mid-flow). Probing `/health` first converts
all of them into one refusal that says what to do — and distinguishing wedged from absent matters
because the remedies are opposite (wait/escalate vs `daemon start`).

**`run` submits via `run_bg` and polls.** A single blocking HTTP call for a 25-minute job would
pin one connection across broker restarts and proxy timeouts; submit-then-poll survives both, and
reading the log file directly from disk gives real-time streaming without a server-side tail.

**Client-side `watch`.** The overview is two cheap read-only routes; looping them client-side
needs no server session, no push channel, and degrades gracefully to sequential frames when piped.
`status` and `watch` share one renderer (`_overview`) so they can never disagree.

**`smi` execs locally.** tt-smi's dashboard is a full-screen TUI that owns the terminal; proxying
its pty through the broker adds a failure mode and leftover terminal modes for zero gain. The
read-only allowlist (enforced twice on a locked host: CLI and root wrapper) is what makes running
it beside a live job safe. This corrects older docs that implied it used a broker stream route.

**Exit codes carry the job, not the transport.** A CI wrapper around `tt-device-mcp run` needs one
bit: did the workload succeed. Folding transport errors, refusals, and job failure into the same
non-zero keeps that bit honest; `exec` propagates the real exit code because a diagnostic's code
(e.g. the deny probe's 13) is itself the answer.

## Test anchors

| Claim | Test(s) |
|---|---|
| I1 (surface exists as specified) | `tests/test_cli.py::test_cli_commands_exist`, `tests/test_cli.py::test_cli_daemon_subcommands` |
| I2 (refusal without a server) | `tests/test_cli.py::test_cli_run_requires_daemon` |
| I2 (degraded counts as reachable) | `tests/test_cli.py::test_is_daemon_running_treats_a_degraded_broker_as_reachable` |
| I2 (wedged vs absent wording) | `tests/test_cli.py::test_server_down_message_distinguishes_wedged_from_absent` |
| I3 | `tests/test_cli.py::test_daemon_start_refuses_on_broker_host` |
| I5 | `tests/test_cli.py::test_main_bare_piped_stdin_runs_stdio_adapter`, `tests/test_cli.py::test_main_bare_tty_shows_help_not_adapter` |
| I7 (wrapper self-enforces the allowlist) | `tests/test_cli.py::test_smi_ro_wrapper_rejects_reset_flag` |
| I7 (lock detection never samples `by-id/`) | `tests/test_cli.py::test_device_nodes_ignores_the_by_id_directory` |
| I7 (wrapper + sudoers install/remove, visudo-gated) | `tests/test_cli.py::test_install_smi_ro_wrapper_creates_wrapper_and_sudoers`, `tests/test_cli.py::test_install_smi_ro_wrapper_skips_sudoers_on_visudo_failure`, `tests/test_cli.py::test_remove_smi_ro_wrapper_is_idempotent` |
| I8 | `tests/test_install_modes.py::test_a_state_dir_that_cannot_be_secured_stops_the_daemon_with_an_explanation` |
| daemon env: gate on by default, sets no rung policy | `tests/test_install_modes.py::test_the_per_user_daemon_health_gates_like_any_other`, `tests/test_install_modes.py::test_the_daemon_env_declares_no_rung_policy` |
| daemon env: root-only paths redirected | `tests/test_install_modes.py::test_per_user_daemon_keeps_its_state_somewhere_writable`, `tests/test_install_modes.py::test_the_per_user_daemon_redirects_the_device_op_lock` |
| daemon status/stop exit codes | `tests/test_cli.py::test_cli_daemon_status_not_running`, `tests/test_cli.py::test_cli_daemon_stop_not_running` |
| `help` == `--help` | `tests/test_cli.py::test_help_subcommand_matches_full_help` |
| timezone: offset zones + rendering | `tests/test_cli.py::test_timezone_offset_and_label` |
| timezone: UTC fallback without tzdata | `tests/test_cli.py::test_timezone_falls_back_to_utc_without_a_tz_database` |
| timezone: DST-aware default, city labels | `tests/test_cli.py::test_timezone_default_is_pacific_dst_aware`, `tests/test_cli.py::test_timezone_zone_name_label_is_the_city` |
| timezone: canonical-zone resolution | `tests/test_cli.py::test_zone_from_name_accepts_city_and_rejects_non_zones`, `tests/test_cli.py::test_canonical_city_names_are_unique`, `tests/test_cli.py::test_common_zone_shortlist_is_real_and_small` |
| lock self-checks (contract only; mechanics spec 05) | `tests/test_cli.py::test_cmd_lock_refuses_when_deny_probe_opens_the_node`, `tests/test_cli.py::test_cmd_lock_locks_when_deny_probe_is_refused` |
| banner reconciles with lock state | `tests/test_cli.py::test_refresh_banner_tracks_lock_state` |
| I9 (binary exit, both directions) | `tests/test_cli.py::test_pre_step_exits_zero_only_when_the_device_is_fit_and_free`, `tests/test_cli.py::test_a_step_refusal_exits_non_zero`, `tests/test_cli.py::test_a_step_with_no_broker_exits_non_zero` |
| I9 (client timeout tracks the server deadline env var) | `tests/test_cli.py::test_a_step_client_timeout_tracks_the_server_deadline_env_var` |
| I9 (the server-side deadline bounds the reply only; the gate keeps running and the device stays reserved) | `tests/test_slurm_steps.py::test_a_steps_deadline_leaves_the_gate_task_running_and_the_reservation_held` |
| I9 (post-step's deadline bounds reclaim plus gate together, not the gate alone) | `tests/test_slurm_steps.py::test_the_post_step_deadline_bounds_reclaim_too_not_just_the_gate` |
| I9 (every shape Slurm reports an exit code in normalizes to the bit; a clean `0:0` never drains) | `tests/test_slurm_steps.py::test_the_epilog_normalizes_every_shape_slurm_reports_an_exit_code_in` |
| I9 (a nonzero SLURM_JOB_EXIT_CODE still reaches post-step when derived is zero) | `tests/test_slurm_steps.py::test_the_epilog_falls_back_to_slurm_job_exit_code_when_derived_ec_is_zero` |
| I9 (a deferred dispatch is diagnosable: health_event + log line, holder named in queue/status) | `tests/test_slurm_steps.py::test_a_deferred_dispatch_is_visible_in_the_health_log_and_the_queue_status` |
| I9 (a non-finite deadline env var falls back to the default, in both the deadline and the derived client timeout) | `tests/test_cli.py::test_a_non_finite_step_deadline_falls_back_to_the_default`, `tests/test_cli.py::test_a_non_finite_step_deadline_env_var_does_not_disable_the_cli_socket_timeout` |
| `post-step` carries the step's exit code and the reclaim switch | `tests/test_cli.py::test_post_step_sends_the_exit_code_and_reclaim_flag` |
| step verbs exist under the names a site scripts | `tests/test_cli.py::test_cli_step_commands_exist` |

Unanchored (verified against `cli.py` directly, no test exercises them): I4's exit-code mapping
(`_wait_and_stream` returns 0 iff `completed`), I6's allowlist capture and `-e` mutual exclusion,
`exec`'s 600 s client cap, `status`'s `N`-positional / `-j` split, `logs -f` local tail, `kill`'s
interactive selection, `reset`'s sentinel→exit mapping, `watch`'s alternate-screen redraw, and the
dead `--port` flag.
