<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# State paths & configuration

Scope: every durable path in both deployment shapes (system broker and per-user daemon), the
install-base vs runtime-state split, path-resolution and directory-permission rules, and the
consolidated environment-variable index. What each variable *does* is its owning spec's contract;
this spec owns where state lives and how a path is resolved.

## Purpose

The broker has two deployment shapes with opposite privileges: the system broker (root, systemd,
`deploy/apply-host-config.sh`) owns root-only system paths, and the per-user daemon
(`deploy/install-user.sh`, no sudo, container or single-user box) cannot write any of them. Every durable
path must therefore resolve per deployment, or a per-user daemon loses durability *silently* — the
historical failure this subsystem exists to prevent: journal writes never raise by design, so a
non-root daemon that kept a `/var/lib` default kept serving while its FSM record, health journal,
and job exit statuses never hit disk.

## Invariants

- **I1 — Every durable path has two defaults.** Root resolves to the system path
  (`/var/lib/tt-device-broker/health`, `/var/lib/prometheus/node-exporter`,
  `/run/tt-device-broker/...`, `/var/log/tt-device-broker`); any other euid resolves under the same
  machine-local per-user base, `constants.user_state_dir()`. A hardcoded root-only default with no
  non-root fallback is forbidden (`evidence._default_health_dir`, `metrics._textfile_dir`,
  `cli.per_user_daemon_env` for the jobexit/device-op paths).
- **I2 — Each path's own env var wins over both defaults**, at either euid. Override precedence is
  always: the path's dedicated `TT_DEVICE_MCP_*` var > the euid-selected default. The per-user base
  itself is overridable (`TT_DEVICE_MCP_STATE_DIR`), and a dedicated var already present in the
  environment is never clobbered by the daemon entry points (`per_user_daemon_env` uses
  `setdefault` / skips preset vars).
- **I3 — The per-user default is durable in fact, not just a plausible string.** A real `ServerFsm`
  pointed at the resolved non-root default must round-trip an episode through disk and reload it in
  a second instance. "Reported over `/health` but no `fsm.json` anywhere" is the regression class.
- **I4 — The per-user shape is one base, and it is boot-cleared by design.** Everything —
  venv, CLI symlink, and under `state/` the socket, pid, logs, stats, health journal and
  jobexit records — lives in `/tmp/tt-device-mcp-<uid>`. One tree to find, one to delete. Nothing
  in it may outlive a boot, and nothing needs to: a socket must not survive, a job cannot, the
  reboot ledger belongs to the system broker, and there is no boot recovery to protect — the
  per-user daemon is not a systemd unit, and the container it usually runs in has no cron to fire
  an `@reboot` entry. Re-running the installer is the supported way back after a reboot, not a
  fallback. The base is never under `$HOME`.
- **I5 — Env-derived path accessors are late-bound: resolved per call, never cached at import.**
  `constants.user_state_dir()`, `server.job_exit_dir()`, `server.device_op_inhibit()`, and
  `metrics._textfile_dir()` all re-read their env var on every call, so the process that spawns a
  daemon (and a test) can redirect them. Documented exception today: `health.evidence.HEALTH_DIR`
  binds once at import (a deliberate test-monkeypatch seam; `health_dir()` just reads it back), and
  `server.py` binds `fsm = ServerFsm(health_dir() / "fsm.json")` at server import — so
  `TT_DEVICE_MCP_HEALTH_DIR` MUST be in the process environment before `tt_device_mcp` is first
  imported. Both daemon entry points guarantee that (`daemon start` via the spawn env, `start-fg`
  by exporting before importing the server). New accessors MUST be late-bound; the import-time
  binding is a defect being tracked, not a pattern to copy.
- **I6 — The per-user state base is secured or the daemon refuses to start.** `_daemon_state_dir`
  creates the base and `chmod 0700`s it *every* start; a base it cannot own/secure (e.g. another
  tenant pre-created the predictable `/tmp` path) exits with the owner named and the overrides
  (`--log-dir` / `TT_DEVICE_MCP_STATE_DIR`) offered — it never silently accepts a foreign
  directory.
- **I7 — An unwritable path degrades per its subsystem's contract, never silently by
  misconfiguration.** Journal appends and textfile writes never raise (they must not take the
  broker down); but the setup paths are loud: `per_user_daemon_env` warns when it cannot create a
  redirect target, and preflight *fails* on an unwritable health dir when health checks are on
  (warns when off).

## Path matrix

`<state>` = `constants.user_state_dir()` = `/tmp/tt-device-mcp-<uid>` unless
`TT_DEVICE_MCP_STATE_DIR` (or `--log-dir`, which repoints the whole base) says otherwise.

| State | System broker (root) | Per-user daemon | Override |
|---|---|---|---|
| Install base | `/opt/tt-device-broker` (venv, wheel, staged scripts) | `/tmp/tt-device-mcp-<uid>` (venv, `bin/` symlink, `state/`) | per-user only: `TT_DEVICE_MCP_INSTALL_DIR` |
| Runtime state base | n/a (system paths below) | `<install>/state` | `TT_DEVICE_MCP_STATE_DIR` (independent of the install base), `--log-dir` |
| Socket | `/run/tt-device-broker/broker.sock` (`RuntimeDirectory=`, chmod 0666) | `<state>/daemon.sock` | `--socket` / `TT_DEVICE_MCP_SOCKET` |
| FSM state file | `/var/lib/tt-device-broker/health/fsm.json` | `<state>/health/fsm.json` | follows the health dir |
| Health journal (events, telemetry trace, buslock, chip baseline, incidents/) | `/var/lib/tt-device-broker/health/` | `<state>/health/` | `TT_DEVICE_MCP_HEALTH_DIR` |
| Server log + job logs | `/var/log/tt-device-broker/` (the unit passes `--log-dir`; the bare server defaults to CWD) | `<state>/` | `--log-dir` |
| Stats | `<log-dir>/stats/` → `/var/log/tt-device-broker/stats/` | `<state>/stats/` | follows `--log-dir` |
| Metrics textfile | `/var/lib/prometheus/node-exporter/tt_device_mcp.prom` | `<state>/metrics/tt_device_mcp.prom` | `TT_DEVICE_MCP_TEXTFILE_DIR` |
| Job exit records | `/run/tt-device-broker/jobexit/` (sticky 1777) | `<state>/jobexit/` (exported by the CLI, 0700 base) | `TT_DEVICE_MCP_JOB_EXIT_DIR` |
| Device-op inhibit lock | `/run/tt-device-broker/device-op.lock` | `<state>/device-op.lock` (exported by the CLI) | `TT_DEVICE_MCP_DEVICE_OP_LOCK` |
| Daemon pid / daemon stdout | n/a (systemd / journald) | `<state>/daemon.pid`, `<state>/daemon.log` | follows the state base |
| Host config | `/etc/default/tt-device-broker` (written by installer, sourced by apply-host-config, autoupdate, and the Slurm hooks) | n/a | `TTDEV_ETC_DEFAULT` (apply-host-config's render/test seam; also a real runtime override for the Slurm hooks, which source it unconditionally on every invocation) |
| Per-user client prefs (CLI-side, any shape) | `~/.config/tt-device-mcp/timezone` (`XDG_CONFIG_HOME` honored) | same | `TT_DEVICE_MCP_TZ` beats the saved timezone |

Corrections/additions relative to the CLAUDE.md tables this migrates: the socket, pid file, job
exit dir, and device-op lock rows were missing there; the server's *own* `--log-dir` default is the
current directory (the `/var/log` value comes from the systemd unit, not from `server.py`); the
metrics row's file name is `tt_device_mcp.prom` inside the directory the override names.

## Behavior

**`user_state_dir()` resolution.** `TT_DEVICE_MCP_STATE_DIR` (stripped; empty means unset) wins;
otherwise `/tmp/tt-device-mcp-<uid>`. Per-uid because `/tmp` is shared on bare metal; two users'
daemons must not collide. Read fresh per call (I5).

**Per-user base creation.** `_daemon_state_dir` runs on every daemon start (`start`, `start-fg`,
`stop`'s pid lookup): `mkdir -p` then `chmod 0700` unconditionally, so a pre-created base is either
secured or refused with the owning user named (I6). Subdirectories (`health/`, `jobexit/`,
`stats/`, `metrics/`) are created by their writers or by `per_user_daemon_env`.

**Daemon environment assembly** (`cli.per_user_daemon_env`, shared by `start` and `start-fg` so
they cannot diverge): `setdefault` `TT_DEVICE_MCP_DEVICE_OP_LOCK=<state>/device-op.lock`, and export
`TT_DEVICE_MCP_HEALTH_DIR=<state>/health` and `TT_DEVICE_MCP_JOB_EXIT_DIR=<state>/jobexit` unless
already set (preset values are left alone, I2). A redirect target it cannot create is warned about
and left unset — fail-soft, never silent (I7).

**Job exit dir permissions.** `ensure_job_exit_dir` sets sticky world-writable (1777) *only* on the
shared system default `/run/tt-device-broker/jobexit`, where privsep jobs run as different users
and each must write its own `.exit` file without removing another's. A redirected dir lives inside
one user's 0700 state base and is never widened — widening it would publish job records to every
tenant.

**Unwritable paths, per subsystem** (I7):

- *Health journal / incidents / buslock* (`evidence._append_durable`): never raises; each append
  `mkdir -p`s, writes, fsyncs, and swallows `OSError`. Loss is best-effort by design — which is
  exactly why the *defaults* must land somewhere writable (I1) and why preflight probes the dir:
  fails the boot when health checks are on, warns when off.
- *FSM persist*: degrades and logs once (spec 03 I3 owns the persist contract; the path here is
  `health_dir()/fsm.json`).
- *Metrics textfile* (`write_textfile`): renders to a pid-suffixed `.tmp` in the target dir and
  `os.replace`s over the real name (node_exporter ignores `.tmp`); creates the directory; never
  raises; logs the failure once per process and recovers silently when the dir becomes writable.
- *Job logs / stats*: `main()` creates them at start; a failure there is a startup failure, not a
  runtime degrade.

**Stats persistence cadence.** `stats_persistence_loop` saves the session stats JSON every
`STATS_UPDATE_SEC` (30 s), repoints the `current` symlink at the latest session file, and publishes
the Prometheus textfile on the same cadence plus once on clean shutdown.

**Metrics textfile placement.** Root writes into node_exporter's real collector directory; a
per-user daemon writes `<state>/metrics/` — writable, but *not scraped*: nothing reads a per-user
path unless the operator points node_exporter's own `--collector.textfile.directory` at it.

## Environment variable reference

The index of every `TT_DEVICE_MCP_*` variable in `src/` plus the two deploy-defined ones —
65 total. One line each; behavioral detail lives in the owning spec
(01 jobs, 02 tools/transports, 03 health, 04 recovery, 05 identity/privsep, 06 this spec,
07 CLI, 08 install/deploy). "1"/"0" defaults are the effective on/off state when unset.

| Variable | Default | Effect | Spec |
|---|---|---|---|
| `TT_DEVICE_MCP_SOCKET` | unset | Socket override; resolution: explicit/env > broker socket > user daemon socket | 02 |
| `TT_DEVICE_MCP_STATE_DIR` | `/tmp/tt-device-mcp-<uid>` | Per-user runtime state base | 06 |
| `TT_DEVICE_MCP_HEALTH_DIR` | euid-split (matrix) | Health journal dir (and via it the FSM file); must be set pre-import (I5) | 06 |
| `TT_DEVICE_MCP_TEXTFILE_DIR` | euid-split (matrix) | Prometheus textfile directory | 06 |
| `TT_DEVICE_MCP_JOB_EXIT_DIR` | `/run/tt-device-broker/jobexit` | Where jobs record their own exit status | 06 |
| `TT_DEVICE_MCP_DEVICE_OP_LOCK` | `/run/tt-device-broker/device-op.lock` | Restart-inhibit file for in-flight device ops | 06 |
| `TT_DEVICE_MCP_INSTALL_DIR` | `/tmp/tt-device-mcp-<uid>` | Per-user base: venv, CLI symlink, and `state/` (deploy-defined, install-user.sh) | 06/08 |
| `TT_DEVICE_MCP_PRIVSEP` | unset (off) | Run each job as its submitter via systemd-run | 05 |
| `TT_DEVICE_MCP_DEVICE_GROUP` | unset (off) | Group-membership admission gate for jobs (device lock) | 05 |
| `TT_DEVICE_MCP_TZ` | unset | CLI TIME-column zone; beats the saved preference | 07 |
| `TT_DEVICE_MCP_JOB_COOLDOWN_SEC` | 0 | Minimum idle gap between jobs | 01 |
| `TT_DEVICE_MCP_JOB_BURST_MAX` | 0 (off) | Per-owner burst ceiling | 01 |
| `TT_DEVICE_MCP_JOB_BURST_WINDOW_SEC` | 60 | Window for the burst ceiling | 01 |
| `TT_DEVICE_MCP_HUNG_SILENCE_SEC` | 300 | Output silence before a job is called hung | 01 |
| `TT_DEVICE_MCP_HUNG_NOTICE_SEC` | 60 | Cadence of hung-job notices | 01 |
| `TT_DEVICE_MCP_DOOMED_GRACE_SEC` | 45 | Grace before a doomed job is reaped | 01 |
| `TT_DEVICE_MCP_HEALTH_CHECK` | 1 | Master switch for health checks. On in both shapes; preflight forces 0 for a non-root daemon with no tt-smi | 03 |
| `TT_DEVICE_MCP_FABRIC_CHECK_CMD` | unset (built-in validator) | Operator override for the fabric traffic check | 03 |
| `TT_DEVICE_MCP_FABRIC_CHECK_INTERVAL_SEC` | 1200 | Staleness window before the gate re-runs the fabric pass | 03 |
| `TT_DEVICE_MCP_ETH_HEARTBEAT_CMD` | unset (built-in probe) | Operator override for the passive eth-heartbeat read | 03 |
| `TT_DEVICE_MCP_EXPECTED_CHIPS` | unset (baseline/hwm-derived) | Authoritative chip count for this host | 03 |
| `TT_DEVICE_MCP_AICLK_CEILING_MHZ` | unset (off) | Per-host AICLK ceiling the broker re-applies and proves before any load; a positive integer arms it | 03 |
| `TT_DEVICE_MCP_AICLK_CEILING_CMD` | unset (built-in helper) | Operator override for the ceiling apply, judged on exit code alone | 03 |
| `TT_DEVICE_MCP_AICLK_CEILING_TIMEOUT_SEC` | 8 | Bound on one ceiling apply (min 1) | 03 |
| `TT_DEVICE_MCP_AICLK_CEILING_PYTHON` | broker's interpreter | Interpreter for the built-in ceiling helper (needs tt-umd) | 03 |
| `TT_DEVICE_MCP_SYSFS_DIR` | `/sys/class/tenstorrent` | Sysfs class dir (test seam) | 03 |
| `TT_DEVICE_MCP_PCI_DIR` | `/sys/bus/pci/devices` | PCI devices dir (test seam) | 03 |
| `TT_DEVICE_MCP_SAMPLE_INTERVAL_SEC` | 10 | Telemetry sampler cadence | 03 |
| `TT_DEVICE_MCP_SAMPLE_RING` | 120 | Sampler ring size | 03 |
| `TT_DEVICE_MCP_SAMPLER_STALL_SEC` | 120 | Sampler-stall watchdog threshold | 03 |
| `TT_DEVICE_MCP_BOOT_PROBE_TIMEOUT_SEC` | 20 | Boot platform-probe timeout | 03 |
| `TT_DEVICE_MCP_PREJOB_DISPATCH` | 0 | Opt-in pre-job single-kernel dispatch proof | 03 |
| `TT_DEVICE_MCP_DISPATCH_BIN` | validator's `metal_example_add_2_integers_in_compute` | Pre-job dispatch probe binary | 03 |
| `TT_DEVICE_MCP_DISPATCH_TIMEOUT_SEC` | 90 | Pre-job dispatch probe timeout | 03 |
| `TT_DEVICE_MCP_SELFHEAL_RELIFT` | 1 | Idle relift of a self-healed hold | 03 |
| `TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC` | 120 | Idle relift cadence | 03 |
| `TT_DEVICE_MCP_FABRIC_RELIFT` | 0 | Opt-in relift of fabric-unverified holds | 03 |
| `TT_DEVICE_MCP_TENANT_HOLD` | 1 | Hold tenant jobs at the door while degraded (vs fail-fast) | 03 |
| `TT_DEVICE_MCP_TENANT_HOLD_POLL_SEC` | 60 | Held-job re-check cadence | 03 |
| `TT_DEVICE_MCP_HOLD_DEADLINE_SEC` | 2× stuck ceiling (2400) | Hold age flagged STUCK to the durable timeline | 03 |
| `TT_DEVICE_MCP_STUCK_HOLD_SEC` | 1200 | General hold ceiling before forced escalation | 03 |
| `TT_DEVICE_MCP_STUCK_HOLD_RESET` | 1 | Kill switch: idle escalation may reset a stuck hold | 03 |
| `TT_DEVICE_MCP_OFFBUS_HOLD_SEC` | 120 | Off-bus hold ceiling (an order below the general one) | 03 |
| `TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE` | 1 | Kill switch: off-bus holds escalate early | 03 |
| `TT_DEVICE_MCP_PRESENT_MESH_RESET_GRACE_SEC` | 300 | Present-mesh eth/fabric hold grace before reset | 03 |
| `TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE` | 1 | Kill switch: generic holds join the idle escalation | 03 |
| `TT_DEVICE_MCP_FORCE_ESCALATE` | 1 | Kill switch: past-ceiling forced escalation | 03 |
| `TT_DEVICE_MCP_ETH_FREEZE_HOLD` | 1 | Frozen-eth verdict holds instead of resetting | 03 |
| `TT_DEVICE_MCP_HOLD_REARM_SEC` | 1800 | Re-arm window for hold-escalation alerts | 03 |
| `TT_DEVICE_MCP_RESET_MODE` | unset (derived from boards) | Declared platform: `galaxy`/`per-target`/`loudbox` | 04 |
| `TT_DEVICE_MCP_RESET_ARGS` | unset | Full reset-command override (argv) | 04 |
| `TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC` | unset → 0.5 floor | Off-bus fraction below which the gate holds instead of resetting; 0 disables the floor | 04 |
| `TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC` | 0.5 | Dead fraction gating the host-reboot rung | 04 |
| `TT_DEVICE_MCP_AUTO_REBOOT` | 1 | Arm the warm host-reboot rung | 04 |
| `TT_DEVICE_MCP_AUTO_POWER_CYCLE` | 1 | Arm the BMC power-cycle rung | 04 |
| `TT_DEVICE_MCP_AUTO_UBB_RESET` | 1 | Arm the per-tray UBB reset rung | 04 |
| `TT_DEVICE_MCP_UBB_RESET_SETTLE_SEC` | 28 | Settle time after a UBB tray reset | 04 |
| `TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET` | 0 | Opt-in bridge reset for a gone chip | 04 |
| `TT_DEVICE_MCP_POST_RESET_FABRIC_RETRIES` | 1 | Fabric re-check retries after a reset | 04 |
| `TT_DEVICE_MCP_POST_RESET_FABRIC_SLEEP_SEC` | 60 | Sleep between post-reset fabric retries | 04 |
| `TT_DEVICE_MCP_POST_REBOOT_VERIFY` | 1 | Verify the mesh actually came back after a reboot | 04 |
| `TT_DEVICE_MCP_AUTO_RECOVERY_INTERVAL_SEC` | 3600 | Durable rate limit between auto reboot/power-cycle rungs | 04 |
| `TT_DEVICE_MCP_BOOT_ATTRIBUTION_WINDOW_SEC` | 900 | Window to attribute a boot to our own reboot rung | 04 |
| `TT_DEVICE_MCP_POLLER_SERVICES` | `tt-telemetry.service,tt-metrics-exporter.service` | Pollers quiesced around a reset | 04 |
| `TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC` | `120` | Wall-clock cap on the reply to an external read-only health pass, **and** the base the CLI derives its pre-step client timeout from (+60 s margin) | 03/07 |
| `TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC` | `600` | Wall-clock cap on the reply to the whole post-step route — the straggler reclaim and the recovering health pass together, not the pass alone — **and** the base the CLI derives its post-step client timeout from (+60 s margin) | 03/07 |

These two are read by two different processes that do not share an environment: the broker reads
its copy from the unit's own `Environment=` lines. The CLI's copy comes from whatever environment
`deploy/slurm/prolog.sh`/`epilog.sh` hand it — NOT from slurmd's own service environment, which
those hooks never inherit (Slurm builds Prolog/Epilog a fresh one deliberately). Each hook
`export`s its own deadline var immediately after `.`-sourcing `/etc/default/tt-device-broker`, so
that ONE file is what a site edits for the CLI/hooks side; the broker side is still a separate
systemd drop-in. Setting the variable on only one side changes only that side's number — see
`deploy/README.md`'s Slurm section for what a one-sided raise actually does.

`TT_DEVICE_MCP_DEVICE_ALLOW` appears only in `deploy/README.md` (udev-lock documentation); nothing
in `src/` or the shipped rules reads it — a doc/code gap, not a variable.

### Deploy-owned variables (`TTDEV_*`)

Read by the deploy pipeline (installer, autoupdate/reconcile, `apply-host-config.sh` unit render,
`fabric-check.sh`, the eth probe) — spec 08 owns the pipeline. The `/etc/default` config keys are
rendered into `Environment=TT_DEVICE_MCP_*` unit lines; the probe-runtime knobs marked (src) are
also read directly by `health/monitors/` and `server.py`, so their behavior is spec 03's.
`TTDEV_ETC_DEFAULT`/`TTDEV_VENV` are also read outside the pipeline proper, at Prolog/Epilog
runtime, by `deploy/slurm/prolog.sh` and `deploy/slurm/epilog.sh` (spec 08's Slurm-hooks
Behavior).

| Variable | Default | Effect |
|---|---|---|
| `TTDEV_ETC_DEFAULT` | `/etc/default/tt-device-broker` | Config file path — `apply-host-config.sh`'s render/test seam, but also read at runtime, unconditionally, by `deploy/slurm/prolog.sh`/`epilog.sh` to resolve `tt-device-mcp`, fill `TTDEV_VENV` into their failure message, and export the hook's own step-deadline var from whatever the sourced file set |
| `TTDEV_VENV` | required in config | Broker venv path the unit execs from |
| `TTDEV_ROOT` | `/opt/tt-device-broker` | Broker install root (also read by src fabric/dispatch resolvers via `TTDEV_VALIDATOR_ROOT` default) |
| `TTDEV_UNIT_PATH` | `/etc/systemd/system/tt-device-broker.service` | Unit path (render seam) |
| `TTDEV_BRANCH` | `main` | Branch autoupdate tracks — the **only** settable part of the update source (08 I16); the repository itself is a constant in `autoupdate.sh`, not configuration |
| `TTDEV_AUTOUPDATE` | `1` | Autoupdate master switch. Read at install to seed the config file, and by every lap; `sudo TTDEV_AUTOUPDATE=0 ./install.sh` turns a host off outright (after `sudo`, which resets the environment) |
| `TTDEV_MAX_DEFER_SEC` | 0 (off — apply immediately; jobs re-adopt) | Opt-in busy/idle gate: seconds autoupdate waits for an idle window. The in-flight device-op bar is separate and unconditional (spec 08) |
| `TTDEV_LOCK` | 0 | Apply the udev device lock at install (shared host) |
| `TTDEV_NO_LOCK` | unset | Back-compat: force cooperative (no lock) |
| `TTDEV_RESET_MODE`, `TTDEV_FABRIC_CHECK_CMD`, `TTDEV_ETH_HEARTBEAT_CMD`, `TTDEV_RESET_MIN_DEAD_FRAC`, `TTDEV_SELFHEAL_RELIFT`, `TTDEV_EXPECTED_CHIPS`, `TTDEV_AUTO_REBOOT`, `TTDEV_AUTO_POWER_CYCLE`, `TTDEV_AUTO_UBB_RESET`, `TTDEV_PREJOB_DISPATCH`, `TTDEV_AICLK_CEILING_MHZ`, `TTDEV_AICLK_CEILING_CMD` | unset | Per-host config keys; each renders the same-named `TT_DEVICE_MCP_*` unit env line (values already in the unit survive an update) |
| `TTDEV_FABRIC_DESCRIPTOR` | galaxy: shipped descriptor; else unset | Cabling descriptor for the fabric check (src, fabric.py) |
| `TTDEV_FABRIC_BIN` | validator `run_cluster_validation` | Fabric validator binary override (src) |
| `TTDEV_FABRIC_RUNTIME_ROOT` | validator `current/` | `TT_METAL_HOME` for the fabric check (src) |
| `TTDEV_FABRIC_CACHE` | `/var/cache/tt-device-broker/fabric-tt-metal-cache` | `TT_METAL_CACHE` for the fabric check (src) |
| `TTDEV_FABRIC_OUTPUT` | `<cache>/cluster_validation_logs` | Validator output dir (src) |
| `TTDEV_FABRIC_ITERS` | 1 | Traffic iterations (src) |
| `TTDEV_VALIDATOR_ROOT` | `/opt/tt-device-broker/validator` | Pinned-validator install root (src + installer) |
| `TTDEV_VALIDATOR_REPO`, `TTDEV_VALIDATOR_SHA`, `TTDEV_VALIDATOR_TOOLCHAIN`, `TTDEV_VALIDATOR_TARGET`, `TTDEV_VALIDATOR_BIN_REL`, `TTDEV_VALIDATOR_DESCRIPTOR_REL` | `deploy/fabric-validator.pin` | Validator build pin (repo, SHA, toolchain, target, artifact paths) |
| `TTDEV_DISPATCH_TARGET`, `TTDEV_DISPATCH_BIN_REL`, `TTDEV_PREJOB_DISPATCH_TARGET`, `TTDEV_PREJOB_DISPATCH_BIN_REL` | pin file | Dispatch-probe build targets/artifacts |
| `TTDEV_DISPATCH_BIN` | validator `distributed_program_dispatch` | Fabric-check dispatch stage binary |
| `TTDEV_DISPATCH_TIMEOUT` | 90 | Fabric-check dispatch stage timeout |
| `TTDEV_DISPATCH_OP_TIMEOUT` | 25 | `TT_METAL_OPERATION_TIMEOUT_SECONDS` for the dispatch stage |
| `TTDEV_DISPATCH_RUNTIME_ROOT`, `TTDEV_DISPATCH_CACHE` | validator `current/`, broker cache | Runtime root/cache for the pre-job dispatch probe (src, server.py) |
| `TTDEV_ETH_CHECK_PYTHON` | unset (validator env, then fallbacks) | Interpreter for the eth probe (src) |
| `TTDEV_ETH_CHECK_PROBE` | built-in staged probe | Eth probe script override (src) |
| `TTDEV_ETH_CHECK_TIMEOUT` | src-defined budget | Eth probe timeout (src) |
| `TTDEV_ETH_CHECK_CACHE` | broker cache | Eth probe `TT_METAL_CACHE` (src) |
| `TTDEV_ETH_VENV` | unset | Venv for the eth probe (src) |
| `TTDEV_ETH_CHECK_ARMED` | set by the broker itself | Per-process marker: the startup arming read answered in budget (src; not an operator knob) |
| `TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN` | unset | Operator validation gate for the eth probe (src; pre-rename spelling honored) |
| `TTDEV_ETH_CHECK_HEARTBEAT_WINDOW_SEC` | 0.5 | Probe's heartbeat observation window (probe script) |
| `TTDEV_ETH_CHECK_POLL_SEC` | 0.01 | Probe's poll interval (probe script) |

## Design decisions

**One boot-cleared base for the whole per-user shape.** A persistent install base would only be
worth its complexity if something consumed the persistence, and nothing does: this shape is not a
systemd unit, and the container it usually runs in has no cron, so there is no boot recovery to
protect. Surviving a reboot would leave a stale venv for the next install to write over, and a
stale socket or pid resurrected after boot is a liveness lie. One tree under `/tmp` makes the
ephemerality uniform and honest — the next boot removes the whole footprint, and re-running the
installer is the documented way back.

**Nothing under `$HOME`.** Root deployments cannot write another user's home; and on many
shared-cluster hosts `$HOME` is a network share mounted by several machines, so two hosts would collide on the socket and pid file and interleave
each other's records — everything installed (compiled wheels, a socket, logs) is machine-specific.
The exceptions are deliberate, tiny, and per-user-preference only: the CLI's timezone file
(`~/.config`) and the legacy read-only host file.

**`/tmp` rather than `XDG_RUNTIME_DIR`.** `/run/user/<uid>` would be the better base — already
0700, machine-local, boot-cleared — but it does not exist in the tt-metalium container (no
systemd-logind session), and the container is a first-class deployment shape. The trade-off is
accepted factually: `/tmp/tt-device-mcp-<uid>` is a predictable name in a sticky world-writable
dir, which is why I6's chmod-or-refuse check exists: the daemon takes the directory only if it
owns it at the mode it expects, and refuses rather than trusting a name another local user could
have created first. `XDG_RUNTIME_DIR` is the better base where a session provides one, and is the
intended direction; the container shape, which has none, is why the fallback has to hold on its
own.

**Two defaults instead of one configurable path.** A single root-only default with an env knob was
the historical shape, and it failed silently: journal writes never raise (their own contract), so
the misconfiguration produced no error, only absent state discovered after the restart it was meant
to survive. Resolution by euid makes the safe path the default in both shapes; the env var remains
for tests and unusual layouts, never as the mechanism that makes a stock deployment correct.

## Test anchors

| Claim | Anchors (pytest node ids) |
|---|---|
| I1 (euid-split defaults) | `tests/test_state_paths.py::test_root_health_dir_default_is_unchanged`, `tests/test_state_paths.py::test_non_root_health_dir_default_lands_under_user_state_dir`, `tests/test_state_paths.py::test_root_textfile_dir_default_is_unchanged`, `tests/test_state_paths.py::test_non_root_textfile_dir_default_lands_under_user_state_dir` |
| I2 (env var beats both defaults) | `tests/test_state_paths.py::test_health_dir_env_override_beats_both_defaults[0]`, `tests/test_state_paths.py::test_health_dir_env_override_beats_both_defaults[1000]`, `tests/test_state_paths.py::test_textfile_dir_env_override_beats_both_defaults[0]`, `tests/test_state_paths.py::test_textfile_dir_env_override_beats_both_defaults[1000]`, `tests/test_install_modes.py::test_the_state_dir_is_overridable_so_tests_never_touch_the_live_one`, `tests/test_install_modes.py::test_install_dir_is_overridable_with_a_dedicated_env_var` |
| I2 (preset vars never clobbered) | `tests/test_install_modes.py::test_preset_state_dirs_are_left_alone` |
| I3 (per-user default genuinely durable) | `tests/test_state_paths.py::test_fsm_survives_restart_at_the_per_user_default_health_dir`, `tests/test_install_modes.py::test_per_user_daemon_keeps_its_state_somewhere_writable` |
| I4 (one base, machine-local, no $HOME) | `tests/test_install_modes.py::test_install_base_defaults_to_tmp_uid_not_home`, `tests/test_install_modes.py::test_per_user_install_writes_nothing_under_home`, `tests/test_install_modes.py::test_venv_and_cli_land_under_the_machine_local_install_base`, `tests/test_install_modes.py::test_all_per_user_state_is_machine_local` |
| I4 (no cron; a stale entry is removed, the user's own is not) | `tests/test_install_modes.py::test_no_cron_entry_is_installed`, `tests/test_install_modes.py::test_a_stale_self_update_cron_is_removed`, `tests/test_install_modes.py::test_removing_a_stale_entry_leaves_the_users_own_cron_alone` |
| I5 (late-bound accessors redirectable) | `tests/test_readopt.py::test_the_job_exit_dir_is_redirectable`, `tests/test_readopt.py::test_the_device_op_lock_is_redirectable`, `tests/test_install_modes.py::test_the_per_user_daemon_redirects_the_device_op_lock` |
| I5 (both entry points get the same env, pre-import) | `tests/test_install_modes.py::test_start_fg_gets_the_same_environment_as_start` |
| I6 (secure or refuse) | `tests/test_install_modes.py::test_a_state_dir_that_cannot_be_secured_stops_the_daemon_with_an_explanation` |
| I7 (preflight loud on unwritable health dir) | `tests/test_device_safety.py::test_preflight_still_requires_the_health_dir_when_health_is_on` |
| Jobexit perms (1777 only at the shared default) | `tests/test_readopt.py::test_a_redirected_job_exit_dir_is_not_made_world_writable` |
| Textfile write behavior (atomic, creates dir, never raises, logs once, recovers) | `tests/test_metrics.py::test_write_textfile_is_atomic_and_leaves_no_tmp`, `tests/test_metrics.py::test_write_textfile_creates_the_directory`, `tests/test_metrics.py::test_write_textfile_never_raises_when_directory_is_unwritable`, `tests/test_metrics.py::test_write_textfile_logs_the_failure_once_per_process`, `tests/test_metrics.py::test_write_textfile_recovers_once_the_directory_is_writable_again`, `tests/test_state_paths.py::test_non_root_textfile_writer_writes_and_leaves_no_tmp` |
| Per-user daemon health gating on by default, overridable off | `tests/test_install_modes.py::test_the_per_user_daemon_health_gates_like_any_other`, `tests/test_install_modes.py::test_an_operator_can_still_turn_the_gate_off` |
