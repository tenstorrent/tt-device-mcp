<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Recovery

Scope: the escalation ladder and its stages, platform recovery policy (`GalaxyRecovery` /
`PerTargetRecovery`), the platform-invariant `RecoveryMechanism` (scoped execution, quiesce,
cooldown, durable ledgers), the device-holders reset gate, and the `tt_device_reset` tool's
contract. Health probing, the between-job gate's hold/release policy, and the FSM are spec 03;
state-file paths are spec 06; tool schemas/transports are spec 02; the CLI surface is spec 07.

## Purpose

Recovery brings a degraded device back without making it worse: the wrong reset is how one wedged
chip becomes a mesh-wide drop, and a repeat reset at a dead endpoint is how a wedge becomes a host
reboot. The subsystem therefore separates *policy* (which rung the evidence justifies, which
command fits the board) from *execution and safety state* (the scoped reset run, cooldowns, the
durable rate-limit ledger), and climbs a gentlest-first ladder — PCI rescan / per-chip bridge
reset, per-tray BMC reset, `tt-smi` reset, warm host reboot, cold BMC chassis power cycle — where
each rung fires only when the gentler one failed or cannot apply.

## Invariants

- **I1 — Policy is separate from execution.** `Recovery` (health/recovery/`__init__.py`) is
  platform policy: `next_stage(Evidence)` names the single gentlest-first rung the evidence
  justifies; `_platform_reset_argv` names the reset command that fits the board. `RecoveryMechanism`
  (health/recovery/`base.py`) is platform-invariant execution and safety state: the PID-1-scoped
  reset run, poller quiesce, the failed-reset cooldown, the durable auto-recovery ledger. `Recovery`
  holds the mechanism by composition (`self.mechanism`), never inheritance, so constructing a
  `Recovery` can never reset the mechanism's process-lifetime state.
- **I2 — One mechanism, shared by both platforms.** `ServerFsm.boot()` constructs exactly one
  `RecoveryMechanism` per process and hands the same object to the one `GalaxyRecovery` and the one
  `PerTargetRecovery` (`_persistent_recovery`). A cooldown or reset-in-flight flag armed while one
  platform was selected therefore still gates a reset after `select_recovery` resolves to the other.
- **I3 — The 600 s failed-reset cooldown.** After a reset that ran and failed to revive the mesh,
  no further reset fires for `RESET_COOLDOWN_SEC` (600 s); the device stays flagged throughout. The
  cooldown is armed only by a real outcome — a non-zero exit, a clean exit whose verify failed, or
  an adopted foreign reset that verified unhealthy — never by a timeout whose scope is still
  cycling (that reset may yet succeed; the next caller adopts it).
- **I4 — Every reset survives its broker.** For root on a systemd host, `RecoveryMechanism.run_scoped` wraps
  reset argv in `systemd-run --scope --unit=ttdev-reset-<pid>-<seq>
  --property=KillMode=mixed`. Otherwise it starts a new-session child with an inherited
  exclusive `flock`; reset output goes to a file so an orphan cannot block on an unread pipe. Both
  backends survive broker restart. An overrunning reset is waited out and never killed mid-run;
  only a reset that never ends is a failure. Root on a systemd host gets the scope; everything
  else gets the local child. Whether a reset may fire at all is I17.
- **I5 — One reset at a time.** Before starting a reset, a live `ttdev-reset-*` scope or held local
  reset lock — including one owned by a predecessor broker process — is adopted and its result
  verified (`await_foreign_scope`); a second reset is never raced into a mesh one is already
  cycling. The surgical bridge reset and the tray walk likewise decline while a reset is live.
- **I6 — The reset gate.** An operator reset (`tt_device_reset`, `tt-device-mcp reset`) is refused
  while any foreign tenant — a holder with `uid >= MIN_TENANT_UID` (1000) other than the caller —
  holds `/dev/tenstorrent`, and an incomplete holder scan fails closed (an unreadable holder may be
  a tenant). `force=true` overrides both, but the decision still carries the foreign holders so the
  override is logged against what it ran over. Holders below `MIN_TENANT_UID` (root, telemetry
  daemons) are infrastructure, not tenants, and never block. A stale fd to a deleted device node is
  not a holder.

  **The scan asks tt-kmd before it infers.** `/proc/driver/tenstorrent/<n>/pids` is the driver's
  own record of who holds each device — one pid per line, empty while free, and world-readable.
  The fd walk needs `CAP_DAC_READ_SEARCH` to read another uid's fd table, so an unprivileged walk
  is blind to exactly the tenants this gate protects: it reported hundreds of unreadable processes
  on a four-chip host and the gate then failed closed on a device that was genuinely free. Failing
  closed on a blind scan is correct; the scan being blind for want of privilege is not, and it
  made the gate refuse for a reason that has nothing to do with the device. Attribution still
  comes from `/proc/<pid>`, whose stat is world-readable even where the fd table is not, so both
  halves work unprivileged. `HolderScan.source` records which route answered (`driver` / `proc`),
  because the two fail differently and a refusal must be readable back to its cause.

  Two fail-closed rules keep the driver route from reading more confidently than it knows. A pid
  the driver names but that `/proc` cannot attribute is unattributed, never absent — the scan goes
  incomplete rather than reporting a free device. A pid that `/proc` does attribute but that holds
  no device node in this process's own view is also refused, because the driver's pids are always
  the host's: read through a container's `/proc` the same number is an unrelated process, and
  charging its uid would let the gate mistake a stranger for the caller's own holder. A host whose
  driver does not publish the record at all — or whose every per-device file is unreadable, which
  is indistinguishable from not publishing — falls back to the walk, whose blind spots are the ones
  these rules were written against.

  The namespace rule is not hypothetical, it is the container shape's default: a container with no
  `/dev/tenstorrent` passed through still lists every device under `/proc/driver/tenstorrent`,
  because procfs driver entries are not namespaced the way `/dev` is. A per-user daemon in a
  container therefore reads the *host's* record. Free reads as free, which is true. A held device
  reads as held-but-unattributable and fails closed, which is also true. The residual case — a
  host holder's pid colliding with a container pid that genuinely holds a passed-through device —
  misnames a holder but cannot invent a free device, so it can never license a reset over a
  tenant.
- **I7 — No automatic action over a tenant.** The idle/forced escalation ladders and every
  host-level rung re-scan holders before acting and stand down on a live tenant; on the unforced
  paths an incomplete scan counts as a tenant. `auto_recovery_allowed` makes the tenant check
  absolute for reboot/power-cycle — a tenant outranks every other signal.

  A caller with the authority to declare an allocation over (spec 07 `post-step`) may *reclaim*
  stragglers before the gate runs — `device_holders.reclaim_foreign_holders`. That is a pre-gate
  action outside the gate: it removes the tenant so this rule passes on its own, and never
  relaxes it. A survivor or a blind re-scan leaves the fail-closed refusal in place. The reclaim
  signals only what its own first scan selected as a target, identified by **pid and `/proc`
  starttime together, not pid alone**, revalidated immediately before EVERY signal — the first
  one included, not only when narrowing `targets` between rounds. The grace window is long enough
  for the original holder to exit and the kernel to hand its pid to an unrelated new process
  before the very first kill call runs, let alone the next rescan, and a pid-only match (or a
  match checked only between rounds) would signal that unrelated process under the belief it is
  the ended allocation's straggler. A target whose starttime cannot be read at all — at selection
  or at signal time — is never signalled, on round 1 or any later round: fail-safe, never
  re-signalled on a guess. A pid that shows up only in a later rescan, or that fails the starttime
  match at any point, is a holder that opened the device after the reclaim began (or a reused pid)
  — not a straggler of the ended allocation — and is reported as a survivor rather than escalated
  onto (spec 05). A pid the reclaim lacked permission to signal (`PermissionError`) is likewise
  never recorded as signalled: the audit reflects what the kill call actually did, not what it
  merely attempted.
- **I8 — The galaxy-reset floor.** The mesh-wide `tt-smi -glx_reset` fires only when the wedged-chip
  count (`off_bus + frozen_chips`) reaches `_galaxy_reset_min_dead_chips(expected)` — default
  `ceil(expected * 0.5)`, floored at 2 so one dead chip never trips it. Below the floor the ladder
  takes the gentler rungs (bridge reset, tray reset) or holds; a below-floor drop is the measured
  case where the mesh reset inverts the mesh (1 chip off the bus → 31 at all-ones).
  `TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC` tunes it; only an explicit `<= 0` disables it; a value above
  1.0 (a chip count typed as a fraction) fails safe to the default.
- **I9 — The durable auto-recovery ledger.** Every host-level escalation (reboot, power cycle) is
  appended to `auto_recovery.jsonl` and fsync'd BEFORE the action fires; if the record cannot reach
  disk the action is aborted — an unrecorded reboot is one the next boot repeats. The ledger is the
  rate limiter and survives the reboot it records: a wall-clock min interval
  (`AUTO_RECOVERY_MIN_INTERVAL_SEC`, 3600 s, spanning the reboot itself) plus a per-boot cap
  (`AUTO_RECOVERY_MAX_PER_BOOT`, 1). An unreadable ledger denies (fail closed); a fire that raised
  without launching retracts its entry so the retry is not spent on a recovery that never happened.
- **I10 — Per-severity rate limiting.** The min interval gates per rank (`reboot` < `power-cycle`):
  a power cycle may escalate past a recent failed reboot inside the interval, but no rung re-fires
  itself inside it, and a stronger record blocks de-escalating to a weaker rung. An unclassifiable
  record counts as strongest.
- **I11 — One attempt, gentlest first, climb only on failure.** No rung fires twice back-to-back:
  the reset is one attempt then cooldown; the idle ladders latch once per hold episode (the latch
  expires after `HOLD_ESCALATION_REARM_SEC` so a hold never becomes permanent); the tray walk runs
  at most once per episode. Host rungs are reachable only after a `tt-smi` reset has already run
  and failed — at the gate additionally only with a prior failed cycle on record (`was_failing`,
  the two-strikes rule).
- **I12 — Quiesce around every reset.** `reset_with_quiesce` stops the MMIO pollers and sets
  `reset_in_flight` before the reset (a reset takes chips off the bus; a poller reading an off-bus
  endpoint is the MMIO stall that reboots the host, and the sampler would isolate mid-reset
  all-ones chips as dead), and restores them only when no reset scope is still cycling. A PCI
  rescan runs before restore so isolated endpoints re-enumerate.
- **I13 — The reset tool's verify is heartbeat + snapshot, not the fabric pass.** After
  `tt_device_reset`'s command exits 0, the tool verifies via `fsm.observe(..., run_fabric=False)`:
  chip enumeration/count and ARC state only. `health_ok: true` from the tool is not proof against
  an eth/fabric wedge — that pair scores a wedged mesh as fine; the next gate's fabric pass or a
  real job is the authority. (The *ladder's* internal post-reset verify does include the fabric
  pass — see Behavior.)
- **I14 — Host rungs are armed by default, opt-out, and fireable-or-off.** `_auto_reboot_enabled`
  and `_auto_power_cycle_enabled` default ON (`TT_DEVICE_MCP_AUTO_REBOOT=0` /
  `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` opt out); each additionally reads as OFF where the process
  could not run it (I17) — armed means armed AND fireable. A warm reboot is never fired where it
  provably cannot help: a whole-bus drop, a reset that regressed the mesh off the bus, or one that
  hard-exited leaving chips off it routes to the cold rung or holds loudly (`reboot_blocked`),
  never to a reboot that lands back in the dead state.
- **I15 — The mesh-wide reset never fires on a guessed mode.** On a multi-chip host whose reset
  mode could not be declared or derived, the per-target `-r` is still issued (a no-op on a Galaxy
  lets the cascade climb) but a loud `reset_mode_unknown` event marks it — it must never land
  silently.

  The reset's own output is read for the converse case, where the host itself says `-r` was the
  wrong command. On a Galaxy whose CPLD firmware predates v1.16, `tt-smi -r` does not merely fail:
  the chips re-enumerate and every register read then returns `0xffffffff` until a `-glx_reset`
  runs, and tt-smi prints a banner saying so before it tries. `_journal_cpld_too_old` matches that
  banner and emits `reset_cpld_too_old` (`host_at_risk`) plus an operator line naming the fix.
  `reset_with_quiesce` always returned that output and the caller discarded it, so the one warning
  a host gives about a reset that strands it went nowhere.

  The banner also **answers** the mode question rather than only warning about it. tt-smi prints
  it ONLY on a Galaxy, so its presence is positive evidence of the board class — and it arrives on
  exactly the hardware where `_is_galaxy` cannot tell, since a degraded Galaxy is where tt-smi
  reports `N/A` board types. The first paragraph forbids firing the mesh-wide reset on a *guess*;
  this is the host's own statement about itself. It latches on the shared `RecoveryMechanism`
  (`cpld_forces_galaxy`), so `select_recovery` resolves to the galaxy ladder from the next rung on
  and `reset_mode_known` stops reporting an open question that has been answered.

  Ranked as evidence, not an override: a declared `TT_DEVICE_MCP_RESET_MODE` still wins, because
  an operator may be deliberately holding a host on the per-target ladder and the banner must
  inform that choice rather than silently reverse it. Below the declaration it outranks the
  board-type derivation, which returns None on this hardware anyway.

  The latch is process lifetime, deliberately not durable. Persisting it would need a new state
  file for a signal whose real fix is the declaration and a CPLD update, both of which the event
  names; the cost of forgetting is bounded and known — one more `-r` after a broker restart, where
  before it was one per recovery episode. The match is deliberately looser than tt-smi's current
  wording (version and flag, case- and newline-insensitive): a missed match restores the old
  silence.
- **I16 — A tray is identified by its chips' PCI bus, never by their index.** The UBB tray holding
  a chip is derived from that chip's `bus_id`, masked to its tray group (`& 0xF0`) and looked up in
  tt-smi's own `WH_UBB_BUS_IDS` / `BH_UBB_BUS_IDS`, which are imported and never copied here (the
  same rule `_glx_board_types` follows for the board-type list). Chip-index arithmetic is not that
  mapping and must not stand in for it: on a Blackhole Galaxy chips 16-23 sit on bus `0x80`, which
  is tray 4, and chips 24-31 on `0xc0`, which is tray 3 — so `index // 8` transposes the two and
  builds a bitmap that re-powers a tray the drop never touched. The bitmap bit is `tray - 1`; the
  tables are 1-based and the BMC's bits are not.

  **The map is cached, and no map means no fire.** A chip that has left the bus is absent from the
  tt-smi snapshot and has no sysfs node, so its `bus_id` cannot be read at the moment the rung
  wants it — which is every moment this rung fires. The index→`bus_id` map is therefore taken from
  the first full-count snapshot (every chip present, every `bus_id` non-empty) and held for the
  life of the process (PCI topology does not move under a running broker), exactly as the board
  types are. A broker that restarted after the drop and never saw a full-mesh snapshot has no map,
  and then the tray rung DECLINES rather than fall back to arithmetic: the same rule as I15's mesh
  reset, which never fires on a guessed mode. A decline here is `not_applicable`, not `blocked` —
  it is a missing fact, not an operator's kill switch.

  Boot probes (`expected_count=0`) and short reads never fill the map. Board types are normalized
  (`_normalized_board` strips wormhole ` L`/` R`) before the table lookup. A later full snapshot
  that disagrees journals `bus_map_drift` once; the cached map stands.
- **I17 — A rung needs both its platform and its privilege.** `privileges.latch()` measures once
  in `ServerFsm.boot()` and every later read returns that record, so the `BOOT privilege:` line
  and the rung a wedge reaches hours later are the same measurement. The probes: `root` (euid 0),
  `systemd` (`/run/systemd/system`), `setpci` (on PATH and root), `ipmi` (`ipmitool` on PATH and
  an openable local IPMI node). Platform is `resolve_platform()`.

  | rung | platform | privilege |
  |---|---|---|
  | `tt-smi -r <present>` | per-target | — |
  | `tt-smi -glx_reset` | galaxy | — |
  | PCI rescan | any | root |
  | bridge SBR | any | root + setpci |
  | UBB tray reset | galaxy | ipmi |
  | host reboot | any | root + systemd |
  | chassis power cycle | any | ipmi |

  Each rung's opt-out env var still applies on top. The device-scoped resets need no privilege:
  they are ioctls on `/dev/tenstorrent`, and that directory is also what scopes `<present>` to the
  chips this process was given. Rungs the OS would refuse read OFF at boot instead of raising at
  the top of the ladder; the `-forced` escalation bypasses the defers, not the arming. Privilege
  states what this process may run, not that it is alone on the device — I6 and I7 are unchanged.

  The boot inventory applies both columns; a rung's arming predicate applies only privilege.
  Off-platform the fire paths already decline as `not_applicable` (a missing fact, not a kill
  switch), and a platform conjunct in the predicate would report that as `blocked`. A platform
  boot left unresolved counts as either: `_is_galaxy` answers None on exactly the degraded Galaxy
  the tray rung recovers. So only a host committed to per-target reads `auto-tray-reset` OFF, and
  its cause names the platform, never a privilege the host has.

  Privilege also picks the reset backend (I4), not just the rungs: `systemd-run --scope` refuses
  an unprivileged caller, and that rc would arm the cooldown against a reset that never ran.
  Reading a scope is a separate question from starting one — `scope_active` keys on systemd alone,
  so an unprivileged daemon still adopts a root broker's reset rather than racing it (I5).

## Interfaces

Class structure: see the diagram in 03-health.md.

- **`select_recovery(monitor, mechanism, deps) -> Recovery`** — the platform for this host: a
  declared `TT_DEVICE_MCP_RESET_MODE` wins (`galaxy` | `per-target` | `loudbox`, the last reading
  as per-target; an unknown value is journaled and ignored, never silently read as per-target);
  otherwise derived from the cached tt-smi board types (`_is_galaxy` — unanimous or unknown, never
  probing); with neither, the conservative per-target ladder. Instances are persistent
  (`_persistent_recovery`); only the choice is per-call. `ServerFsm.resolve_platform()` commits the
  choice once at boot when it can (logged as `BOOT platform:`); an unreadable mesh commits nothing
  and leaves the per-pass fallback, which self-corrects on the first successful snapshot.
  `server.select_recovery` is the module-global alias (`fsm.select_recovery`) the gate and the
  reset routes call.
- **`Recovery.next_stage(ev: Evidence) -> str`** — the one rung `ev` justifies: a stage name
  (`bridge_reset`, `smi_reset`, `ubb_tray`, `host_reboot`, `power_cycle` — `constants.STAGE_NAMES`)
  or a non-stage decision (`WAIT`, `DEFER`, `RELEASE`, `HOLD_FABRIC_UNVERIFIED`, `BLOCKED`). Pure:
  the caller acts. The gate steers on `GalaxyRecovery.next_stage` on every platform (the ladder at
  a job boundary is one ladder; only the reset argv is platform-specific);
  `PerTargetRecovery.next_stage` is the strictly smaller platform-own view (cooling → WAIT, live
  scope → DEFER, else `smi_reset`).
- **`Recovery.escalate(phase, indices, expected, log, *, stage, ev, beats) -> outcome`** — runs a
  rung and reports `recovered` / `waiting` / `terminal`. Phases: `"gate/<gate-phase>"` fires the one
  rung the gate's own `next_stage` chose (never reports `terminal` — a gate pass that failed has
  not exhausted the ladder); `"present"` / `"offbus"` are the idle stuck-hold ladders, with a
  `"-forced"` suffix for the hold-deadline watchdog's bypass of the fail-closed defers. The base
  (per-target) `escalate` always reports `waiting` — no ladder here, not a failed one — which is
  why the gate and the forced escalation route through the module-global `GalaxyRecovery` instance
  on every platform: the Galaxy-only rungs decline themselves off-platform (tray plans return
  None), and the reset argv is re-derived via `select_recovery` inside `_reset_and_verify_device`,
  so a per-target host still fires `tt-smi -r`, never `-glx_reset`. `server.py` folds every outcome
  into the FSM via `fsm.on_outcome(outcome)` (spec 03 owns the state semantics).
- **`Recovery.reset_argv(indices)`** — `TT_DEVICE_MCP_RESET_ARGS` (a full argv override) wins;
  otherwise `tt-smi -r <i,j,...>` (per-target) or `tt-smi -glx_reset` (Galaxy, all ASICs, no
  targets).
- **`RecoveryMechanism`** — `reset_with_quiesce(argv, log, owner)` is the one choke point every
  reset in the broker goes through (gate and tool alike, so "safe" cannot drift); `run_scoped`,
  `scope_active`, `await_foreign_scope`; `cooling()`; `auto_recovery_allowed(action,
  tenant_active)`, `record_auto_recovery`, `retract_last_auto_recovery`,
  `read_auto_recovery_ledger`, `boot_from_broker_escalation` (attributes a boot to the escalation
  that caused it only within `BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC`, 900 s — a stale entry must
  not stamp an external reboot as broker recovery); `_fire_recovery_escalation` (the shared
  record-then-fire body of both host rungs).
- **Stage fire functions (the argv seams).** Each stage isolates the destructive action in one
  function the test suite stubs and asserts argv on: `stages/bridge_reset.reset_chip_via_bridge`
  (setpci Secondary Bus Reset on the parent bridge; tri-state result — recovered / retryable /
  structurally inapplicable), `stages/ubb_tray._ubb_reset_argv` + `_fire_ubb_reset` (`ipmitool raw
  0x30 0x8b <bitmap> 0xff 0x00 0x0f`, wrapped in tt-smi's USER_RESET/POST_RESET ioctl handshake,
  compatible with both tt-smi surfaces), `stages/host_reboot._fire_host_reboot` (`systemctl
  reboot`), `stages/power_cycle._fire_power_cycle` (`ipmitool chassis power cycle`),
  `stages/smi_reset` (the per-chip reset ioctl binding). A fire that raises is a launch failure the
  ladder reports and falls past; it never masks as success.
- **`device_holders`** — `enumerate_device_holders() -> HolderScan` (tt-kmd's own
  `/proc/driver/tenstorrent/<n>/pids` record where published, else the best-effort /proc open-fd
  walk; `source` names which, and `complete=False` when a holder could not be attributed or a
  cross-uid fd table was unreadable),
  `evaluate_reset_gate(caller_uid, scan, force) -> ResetDecision`. `caller_uid=None` is an
  anonymous caller (HTTP, no SO_PEERCRED): it owns no holder, so every tenant is foreign.
- **`tt_device_reset` tool / `POST /api/tt_device_reset` / CLI `reset`** (contract only; schema in
  spec 02, CLI in 07): scan holders → apply the reset gate (caller-scoped over the socket;
  anonymous fail-closed over HTTP on a privsep host; legacy skip over HTTP off privsep) → interrupt
  the running job gracefully (scope-routed for privsep jobs; marked reset-killed first so its exit
  does not re-flag the device) → `select_recovery().reset_argv(present chips)` →
  `reset_with_quiesce` under the device-op lock, its one jobs-list row owned by
  `[broker]reset-tool` — the broker performs the reset, so it owns the action row; the requesting
  caller keeps their identity on their own job rows, never on this broker action → on rc 0, verify
  heartbeat+snapshot (I13) → status `reset_complete` /
  `reset_unhealthy` / `reset_failed` / `refused` / `no_devices`, with `health_ok`, `steps`, and the
  reset transcript. The streaming route (`/api/tt_device_reset_stream`) is the same reset — same
  gate, same lock, same restart-safe runner, same quiesce — plus live output. The backend follows
  I4.

## Behavior

**Rung inventory at boot.** `log_rung_inventory()` MUST state every optional rung ON or OFF at
startup (`RUNG INVENTORY:` line, plus a `RUNG OFF <name>:` warning naming the cause for each off
rung); an off rung is readable in the journal, never inferred from an absence of verdicts. The
power-cycle entry distinguishes the operator's `=0` opt-out from an armed rung whose `ipmitool` is
missing. `resolve_platform()` runs first (a declared mode without touching the device; otherwise
one tt-smi snapshot), so `BOOT platform:` precedes any preflight or gate line.

**Evidence → rung.** The gate builds an `Evidence` from its probe pass (spec 03 owns the probes;
what arrives here is: healthy/fault/fabric verdicts, off-bus and ARC-frozen counts, SBR candidates,
cooldown/scope/was-failing state, and — on the post-action ask — which action this pass already ran
and whether it recovered). `_route` (galaxy.py) is THE escalation policy, in order: a recovered
action → `RELEASE`; a failed `smi_reset` → host rung / `BLOCKED` / `WAIT` (host rung only with
opt-in, two strikes, no live scope, and a mass drop or a still-unhealthy present mesh); a failed
tray walk → `WAIT` (the next rung is the cold one, never the suppressed mesh reset); a healthy
fault-free mesh → `RELEASE`, except a fabric pass that ran without a verdict on a dirty/multi-chip
host → `HOLD_FABRIC_UNVERIFIED`; eth-frozen → `WAIT`; cooling → `WAIT`; live scope → `DEFER`
(adopt, never start a second); SBR candidates → `bridge_reset`; below the floor → `ubb_tray`;
otherwise `smi_reset`. The platform derives its own floors from `expected` — a caller cannot hand
it stale ones.

**Mechanism execution.** A fired `smi_reset` (or an adopted `DEFER`) goes through
`_reset_and_verify_device`: adopt any foreign scope; warn loudly on an unknown mode (I15); arm the
cooldown clock; `reset_with_quiesce` (I4, I12); exactly one attempt (I3, I11). On rc 0 the ladder
verifies with `_verify_device_after_reset`: the full probe pass *including* the fabric traffic
pass, where a post-reset "no trained link" (77) is retried after an eth-training sleep
(`POST_RESET_FABRIC_RETRIES` / `_SLEEP_SEC`) and a 77 that persists reads as NOT recovered — the
ladder climbs rather than clear a hold onto a fabric no pass ever proved. The bridge-reset rung
retries up to `BRIDGE_RESET_MAX_TRIES` with a settle (the first shot races link retrain), treats a
structurally inapplicable chip (no bridge, no setpci) as fall-through rather than spin, tries a
bare PCI rescan before anything heavier, and refuses a stale cached address whose target bus now
holds live silicon. The tray rung walks affected trays first then the rest, one at a time,
re-verifying (fabric included) between trays, holding `reset_in_flight` across the walk, and never
applies to a fully-off-bus mesh (that is the cold rung's case). Every tray it names — the affected
set, the walk order, the bitmap, and the chip ids the ioctl handshake quiesces — comes from the
cached bus map of I16, so the trays in the `ubb_reset_required` event are the ones an operator
would read off `tt-smi -glx_list_tray_to_device`.

**The tray rung's two branches.** The below-floor tray rung splits on the hold's *class*, chosen
once at fire time by `_classify_hold` (never in `_route`, which stays a pure function of its
inputs). A whole UBB tray off the bus with *no bridge window on any of its chips* — every off-bus
chip's per-chip SBR reported `no_bridge`, recorded by the bridge rung on
`Recovery.last_bridge_reset_reasons` and threaded onto `Evidence.bridge_reset_failed` by the gate's
`replace(ev, …)` *after* that rung fires — is the `TRAY_DOWN_NO_WINDOW` class: the one drop measured
never to return by any single reset (0/40 episodes). It fires every reset type back-to-back with no
verify between (`_fire_tray_down_no_window`) — SBR, per-tray BMC re-power of the affected trays,
then the mesh-wide reset — then exactly ONE settle and ONE verify, and if the mesh is still bad goes
straight to the cold power cycle (a whole tray off the bus is warm-reboot-futile — a Galaxy reboot
does not re-power the UBBs); no retries, no ceiling wait, no rung skipped, because there is nothing
to lose before the power cycle. Everything else — a partial-tray drop, a chip that still has a
bridge window, or a window left *unknown* because no bridge rung ran this pass — is
`HOLD_CLASS_GENERIC` and takes the verify-between per-tray walk above. The no-window branch honours
the same `TT_DEVICE_MCP_AUTO_UBB_RESET=0` name-and-hold opt-in, the tenant guard, and the
power-cycle cooldown/boot-loop denials as the generic path; the classification is journalled
(`hold_classified`, with the class, the off-bus set and the trays). A chip absent from
`bridge_reset_failed` reads as *unknown*, never `no_bridge`, so the aggressive sweep can never fire
on an unproven window.

**The last-chance reset sweep gates EVERY host rung.** A reboot or a power cycle takes the whole box
down and costs minutes, so before paying that the ladder re-issues every reset type once more,
back-to-back, then takes ONE settle and ONE verify — and fires the host rung only if the mesh is
still bad (`_settle_and_verify_before_host_rung` → `_issue_all_resets_back_to_back`, the same
implementation the `TRAY_DOWN_NO_WINDOW` branch uses, so the two can never drift). This holds on
every road to a host rung, not just the tray branch: the gate ladder's own rung, the idle/stuck-hold
escalation (`_climb_to_host_recovery_after_failed_reset` — the road most measured power cycles
actually took), and the post-reboot cold climb (`_verify_post_reboot_recovery`, where the warm reboot
just proved it cannot re-power the UBBs). Before this, those three settled and verified but never
re-tried the cheaper rungs, so a present-mesh eth wedge could be power-cycled having only ever seen
mesh resets. Details: the trays re-powered are those holding an off-bus chip, or — with no tray to
blame, e.g. a present-mesh fabric wedge — EVERY tray, since a tray re-power is strictly lighter than
the rung it is delaying; SBR fires only where a chip is actually isolated, so a mass drop is never
nibbled at with per-chip bridge resets; and the sweep is SKIPPED (the settle and verify still run)
under the two guards a destructive rung never crosses — a tenant on the mesh, or a reset already
cycling in its own scope. A sweep that brings the mesh back cancels the host rung outright.

**A RUNNING broker job is a tenant, whatever the holder scan says.** Every guard that asks "is anyone using this
device?" goes through one helper (`_tenant_active`), and it consults TWO independent sources: the holder scan (open
`/dev/tenstorrent` fds) and the broker's own queue (`deps.job_running`). The scan alone is not evidence of idleness —
a running job spends minutes off the device compiling, loading weights and between opens, and during that window the
scan is empty. Measured on g15blx02 2026-09-17: the stuck-hold escalation read an empty scan 106 s into a running
job, fired a mesh reset, and the job died of SIGPIPE the instant the reset released the device. `force` (the
hold-deadline backstop) still waives an UNREADABLE scan, because that guard protects against blindness, but it never
waives a known tenant — fd-held or queue-known.

**Host rungs and the ledger.** A host rung fires only through `_fire_recovery_escalation`: record
durably and fsync (abort on failure), leave the jobs-list row, then fire; a fire that raises
retracts the record (I9). `auto_recovery_allowed` enforces tenant-absolute, per-severity interval,
and per-boot cap (I7, I9, I10); a denied opted-in rung journals `auto_recovery_denied` once per
hold episode. `boot_from_broker_escalation` back-fills the boot that results; on coming back up
from a broker-fired warm reboot, `_verify_post_reboot_recovery` compares what returned to what
left and climbs a still-off-bus mesh to the power cycle (never a second warm reboot — the rung
that just failed), or holds loudly naming the BMC command when the cold rung is not armed.
Absence is never health: every blocked/exhausted path emits a loud actionable event
(`ubb_reset_required`, `all-off-bus power-cycle required`,
`reset_unrecoverable_power_cycle_required`) rather than a silent hold.

**The reset gate.** `evaluate_reset_gate` MUST allow only when no foreign-uid holder exists AND the
scan is complete (I6). Carve-outs: holders with uid < `MIN_TENANT_UID` are ignored; the caller's
own holders are not foreign (resetting your own wedged run is the point); deleted-node fds do not
count. `force` overrides foreign holders and the blind spot but the foreign list is still returned
for logging. Anonymous HTTP callers on a privsep host get the same fail-closed rules with every
tenant foreign; off privsep, HTTP keeps the legacy single-tenant skip.

**Reset verification scope.** Per I13: the operator tool's verify is chip enumeration + ARC
heartbeat via one `fsm.observe(run_fabric=False)` pass — `reset_complete` with `health_ok: true`
says the chips are back and ticking, not that the fabric is proven. Over-enumeration against a
stale degraded baseline is a recovery, not a failure. The gate ladder's own post-reset verify is
strictly stronger (fabric included, 77-retried) — the two scopes are different by design and MUST
NOT be conflated when reading results.

## Design decisions

- **Policy/execution split.** Which rung the evidence justifies is a per-platform judgment;
  cooldowns, scopes and ledgers are safety state that must survive whichever platform is asking.
  Splitting them (I1) means a platform bug cannot corrupt the safety state, and composition rather
  than inheritance means constructing a policy object can never zero a cooldown.
- **One shared mechanism.** `select_recovery` re-derives the platform per pass (the board type is
  not known at startup and a degraded Galaxy reads as unknown). If each platform carried its own
  mechanism, a selection flip mid-incident would launder away an armed cooldown — the exact repeat
  reset the cooldown exists to prevent. Hence ONE mechanism, two persistent wrappers (I2).
- **The Slurm grant is not a rung.** `sudo tt_reidle_downed_node.sh` is scheduler-state repair
  after recovery; it resets no silicon and reboots no host.
- **Privilege is measured, not declared.** What the ladder needs to know is what the kernel will
  permit this process, which no operator declaration can state reliably. Measuring also makes the
  refusal readable at boot rather than at the top of the ladder during a wedge.
- **Restart-safe execution.** A reset launched as an ordinary broker child sits in the broker's cgroup;
  `KillMode=control-group` SIGTERMs it on any broker restart, leaving 32 ASICs half-reset — worse
  than never starting. In its own scope the reset outlives the broker, the scope name is how the
  dead-chip sampler tells "resetting" from "dead", and how a restarted broker finds and adopts the
  reset its predecessor launched. Without systemd the same properties come from a new-session
  child whose inherited kernel lock remains held until that child exits.
- **tt-smi as a recovery dependency.** The reset stages are built on tt-smi's own commands and reset
  ioctls (`-r`, `-glx_reset`, the USER_RESET/POST_RESET handshake around the tray pulse); any host
  with health or recovery enabled requires it rather than half-working without. Generic `--user`
  mode can still serialize with both features off.
- **Platform command difference.** A Galaxy resets mesh-wide (`tt-smi -glx_reset` — per-target `-r`
  does not recover it: the other trays never take part), everything else per PCIe target (`tt-smi
  -r <indices>`). tt-smi does not validate `-glx_reset` against the hardware, so the broker's own
  derivation must be unanimous-or-unknown, and unknown defaults to the per-target ladder — the
  conservative command that never fires a tray reset on hardware that cannot take one.
- **Why the reset gate.** A reset is board-level: issued while a tenant still holds the device it
  aborts their run mid-op, and MMIO from a half-dead holder during the reset is how a wedge
  escalates to a bus fault and a reboot. Scanning holders, quiescing pollers, and refusing over an
  unprovable scan is what keeps an operator reset a repair instead of an outage.
- **The per-tray reset is the broker's own.** tt-smi offers no per-tray reset: `-glx_reset` is
  all-or-nothing, and UMD's tray entry point takes only the full 32-chip bitmap. So this rung
  issues `ipmitool raw 0x30 0x8b` itself and drives the USER_RESET/POST_RESET ioctls around it,
  which it can only do because the gate's snapshot already carries every chip's `bus_id` (I16).
  It exists because nothing else covers a below-floor tray-down — I8's floor suppresses the mesh
  reset and a warm reboot does not re-enumerate a dropped ASIC — so without it that drop holds
  until a human arrives. The cost is ownership: a BMC change that alters the command reaches us as
  a rung that stopped firing, not as a tt-smi release note.
- **Floors before resets.** Both floors exist because the heavy rungs are measured hazards, not
  hypothetical ones: a galaxy reset against a single-chip wedge took the other 31 chips to
  all-ones until a power cycle; a warm Galaxy reboot does not re-power the UBBs, so dropped ASICs
  stay dropped across it. The gentle-first order (rescan → bridge → tray → mesh → reboot → power
  cycle) is ordered by blast radius, and each hazard has a rung below it that avoids it.
- **Two named tray branches, split at fire not in the router.** The one drop the data says no single
  reset recovers — a whole tray off the bus with no bridge window on any chip (0/40 episodes) —
  earns a back-to-back reset sweep straight to the power cycle, where every other drop keeps the
  gentle verify-between walk. The class is decided from evidence the gate *already has* (the per-chip
  SBR `no_bridge` result the bridge rung produced, threaded on `Evidence.bridge_reset_failed`), not
  recomputed in `_route`: the router stays pure, and the destructive branch fires only on a *proven*
  no-window whole-tray drop — a chip whose window was never tested reads unknown and takes the
  generic walk, so the sweep can never fire on a guess. This is why the evidence is threaded through
  `replace(ev, …)` rather than re-derived: `_route` must not grow a side effect, and the fire must
  not re-probe a bus the gate just read.

## Test anchors

| Claim | Test(s) |
|---|---|
| I17 a rung the process cannot execute reads OFF (reboot, power cycle, tray, bridge) | `tests/test_rung_privilege.py::test_a_non_root_daemon_has_no_reboot_rung`, `::test_a_host_without_systemd_has_no_reboot_rung`, `::test_an_unreachable_bmc_has_no_power_cycle_rung`, `::test_an_unreachable_bmc_has_no_tray_rung`, `::test_a_non_root_daemon_has_no_bridge_rung` |
| I17 the inventory reads the tray rung OFF on a committed per-target host, naming the platform; unresolved or Galaxy keeps it | `tests/test_rung_privilege.py::test_a_per_target_host_has_no_tray_rung`, `::test_an_unresolved_platform_keeps_the_tray_rung`, `::test_a_galaxy_keeps_the_tray_rung` |
| I17 privilege is a second conjunct, never a replacement for the opt-out | `tests/test_rung_privilege.py::test_the_opt_out_still_wins_where_the_privilege_exists` |
| I17 the forced ladder bypasses the defers, never the arming | `tests/test_rung_privilege.py::test_the_forced_ladder_cannot_fire_a_rung_privilege_denies` |
| I17 privilege is latched at boot, so the boot line and the ladder cannot disagree | `tests/test_privileges.py::test_the_latch_holds_the_host_it_measured` |
| I17 a privilege shortfall is not a missing bridge, and does not license the sweep | `tests/test_rung_privilege.py::test_without_setpci_the_rescan_still_runs_and_no_chip_is_called_no_bridge` |
| I5 an unprivileged daemon still adopts a foreign reset scope | `tests/test_rung_privilege.py::test_an_unprivileged_daemon_still_sees_a_root_started_reset_scope` |
| I17 the boot line reports raw probes, not folded capabilities | `tests/test_privileges.py::test_the_boot_line_reports_a_present_binary_as_present` |
| I17 the inventory prints in both shapes, including the per-user daemon | `tests/test_rung_privilege.py::test_a_per_user_daemon_still_prints_its_rung_inventory` |
| I4/I17 the scope backend needs root as well as systemd | `tests/test_device_safety.py::test_the_scoped_backend_needs_systemd_running_not_a_binary_on_path`, `tests/test_device_safety.py::test_a_non_root_daemon_on_a_systemd_host_does_not_pick_the_scope_backend` |
| I17 the device-scoped resets need no privilege | `tests/test_rung_privilege.py::test_the_device_scoped_resets_survive_without_systemd` |
| I17 each probe measures a capability, not a declaration | `tests/test_privileges.py::test_root_is_euid_zero_not_the_login_user`, `::test_systemd_is_the_runtime_directory_not_a_binary_on_path`, `::test_setpci_needs_the_binary_and_root`, `::test_ipmi_needs_the_binary_and_a_node_it_can_open`, `::test_an_unopenable_ipmi_node_is_not_a_reachable_bmc` |
| I17 boot states the privileges, and each OFF rung names the one it lacked | `tests/test_rung_privilege.py::test_boot_states_the_privileges_it_measured`, `::test_the_inventory_names_the_privilege_a_rung_lacked`, `::test_the_bridge_rung_names_which_half_it_lacks`, `tests/test_privileges.py::test_the_snapshot_names_every_probe_for_the_boot_line` |
| I1/I2 platform selection: declared mode wins; unknown → per-target | `tests/test_recovery_select.py::test_declared_mode_wins`, `tests/test_recovery_select.py::test_unknown_falls_to_per_target_loud`, `tests/test_recovery_select.py::test_loudbox_declares_per_target` |
| Boot-time platform commit / per-pass fallback | `tests/test_boot_platform.py::test_a_declared_mode_commits_without_touching_the_device`, `tests/test_boot_platform.py::test_a_probe_that_reads_galaxy_boards_commits_the_galaxy_ladder`, `tests/test_boot_platform.py::test_an_unreadable_mesh_commits_nothing_and_keeps_the_per_pass_fallback`, `tests/test_boot_platform.py::test_a_committed_platform_short_circuits_per_pass_selection` |
| Galaxy derivation: unanimous-or-unknown from tt-smi | `tests/test_reset.py::test_an_unidentifiable_board_is_unknown_not_non_galaxy`, `tests/test_reset.py::test_boards_must_agree`, `tests/test_reset.py::test_the_galaxy_list_comes_from_tt_smi_not_from_here` |
| Reset argv per platform; env override wins | `tests/test_reset.py::test_reset_argv_is_machine_type_aware`, `tests/test_reset.py::test_reset_argv_is_derived_with_nothing_declared`, `tests/test_reset.py::test_a_declared_mode_still_wins_over_the_derivation` |
| I15 unknown-mode multi-chip reset is loud | `tests/test_reset.py::test_multichip_reset_with_unknown_mode_is_loud`, `tests/test_reset.py::test_singlechip_reset_with_undeclared_mode_is_silent` |
| I15 CPLD banner logged and journalled, silent on ordinary output | `tests/test_reset.py::TestCpldTooOldBanner::test_the_banner_is_logged_and_journalled`, `::test_ordinary_reset_output_is_silent` |
| I15 CPLD banner match survives rewording/wrapping | `tests/test_reset.py::TestCpldTooOldBanner::test_a_reworded_or_wrapped_banner_still_trips` |
| I15 CPLD banner: the check itself mutates nothing; the caller latches | `tests/test_reset.py::TestCpldTooOldBanner::test_the_check_itself_mutates_nothing` |
| I15 CPLD latch answers the mode and selects the galaxy ladder | `tests/test_reset.py::TestCpldTooOldBanner::test_the_latch_makes_the_reset_mode_known`, `::test_the_latch_selects_the_galaxy_ladder` |
| I15 an operator declaration still outranks the CPLD latch | `tests/test_reset.py::TestCpldTooOldBanner::test_an_operator_declaration_still_outranks_the_latch` |
| I3 cooldown arming rules | `tests/test_device_safety.py::test_a_timed_out_reset_does_not_arm_the_cooldown`, `tests/test_device_safety.py::test_a_reset_that_exits_nonzero_still_arms_the_cooldown`, `tests/test_device_safety.py::test_an_adopted_foreign_reset_that_failed_arms_the_cooldown`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_within_the_reset_cooldown` |
| I4 restart-safe scope/local child; overrun waited out; timeout leaves reset | `tests/test_device_safety.py::test_reset_runs_in_its_own_systemd_scope`, `tests/test_reset.py::test_a_reset_scope_argv_is_scoped_and_outlives_us`, `tests/test_reset.py::test_a_reset_without_systemd_runs_detached_and_returns_its_output`, `tests/test_reset.py::test_a_daemon_without_systemd_resets_through_the_local_backend`, `tests/test_reset.py::test_cancelling_the_waiter_does_not_kill_a_local_reset`, `tests/test_reset.py::test_a_reset_that_overruns_is_waited_out_not_failed`, `tests/test_reset.py::test_a_reset_that_never_ends_is_still_a_failure`, `tests/test_device_safety.py::test_reset_timeout_leaves_the_scope_running`, `tests/test_device_safety.py::test_the_reset_without_systemd_runs_bare_never_through_systemd_run` |
| I5 adopt, never race, a live scope or local lock | `tests/test_reset.py::test_a_restarted_daemon_adopts_the_local_reset_lock`, `tests/test_device_safety.py::test_bridge_reset_is_skipped_while_a_reset_scope_is_in_flight`, `tests/test_device_safety.py::test_stuck_hold_climb_defers_when_a_reset_is_still_cycling`, `tests/test_device_safety.py::test_stuck_offbus_hold_skips_the_surgical_reset_when_a_scope_opened` |
| I6 reset gate: foreign deny, incomplete fail-closed, force, carve-outs | `tests/test_reset_gate.py::TestResetGateDeny::test_denies_on_foreign_holder`, `tests/test_reset_gate.py::TestResetGateIncompleteScan::test_incomplete_scan_fails_closed_when_no_visible_foreign`, `tests/test_reset_gate.py::TestResetGateForce::test_force_overrides_foreign`, `tests/test_reset_gate.py::TestResetGateSystemHolders::test_allows_over_system_holder`, `tests/test_reset_gate.py::TestForeignHolders::test_ignores_system_holders`, `tests/test_reset_gate.py::TestResetGateAllow::test_allows_when_only_own_holders` |
| Anonymous (HTTP/privsep) gate fail-closed | `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_over_a_tenant_holder`, `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_when_scan_incomplete`, `tests/test_reset.py::test_privsep_http_reset_refuses_over_a_foreign_holder`, `tests/test_reset.py::test_non_privsep_http_reset_keeps_the_legacy_skip` |
| Holder scan mechanics (deleted fd, permission gap) | `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_detects_self_holding_a_device_node`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_deleted_node_fd_is_not_a_holder`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_permission_denied_marks_scan_incomplete` |
| I6 driver record: a free device scans complete without privilege | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_free_device_scans_complete_without_privilege` |
| I6 driver record: holders attributed, deduped across devices, junk ignored | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_driver_named_holder_is_attributed_to_its_uid`, `::test_a_process_holding_two_devices_is_listed_once`, `::test_junk_lines_are_ignored` |
| I6 driver record fails closed: unattributable pid, foreign pid namespace, unreadable file | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_pid_we_cannot_see_is_unattributed_not_absent`, `::test_a_pid_that_holds_no_device_here_is_not_attributed`, `::test_an_unreadable_device_file_marks_the_scan_incomplete` |
| I6 driver record absent or wholly unreadable falls back to the walk | `tests/test_device_holders.py::TestDriverHolderRecord::test_an_absent_driver_dir_falls_back_to_the_walk`, `::test_a_driver_dir_with_no_devices_falls_back_to_the_walk`, `::test_wholly_unreadable_records_fall_back_rather_than_report_blind` |
| I6 driver record against real hardware, unprivileged | `tests/test_device_hardware.py::test_the_driver_holder_record_answers_completely_without_root`, `::test_a_free_device_passes_the_reset_gate_without_root` |
| I7 tenant blocks automatic action | `tests/test_device_safety.py::test_governor_never_reboots_over_a_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_under_a_foreign_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_when_the_holder_scan_is_incomplete`, `tests/test_device_safety.py::test_a_tenant_arriving_before_the_reboot_decision_blocks_it` |
| Reclaim removes the tenant without relaxing I6/I7 | `tests/test_device_holders.py::test_reclaim_signals_only_tenant_holders`, `tests/test_device_holders.py::test_reclaim_escalates_term_to_kill`, `tests/test_device_holders.py::test_reclaim_reports_a_survivor_rather_than_claiming_success`, `tests/test_device_holders.py::test_reclaim_carries_the_rescan_completeness` |
| Reclaim never retargets onto a holder that appears after it began | `tests/test_device_holders.py::test_reclaim_never_retargets_onto_a_new_holder_that_appears_after_it_began` |
| Reclaim self-exclusion (never signals itself or its own process group) | `tests/test_device_holders.py::test_reclaim_never_signals_itself_or_its_own_process_group` |
| Reclaim identity is pid+starttime, not pid alone (a reused pid is never escalated onto) | `tests/test_device_holders.py::test_reclaim_never_escalates_onto_a_pid_reused_by_an_unrelated_process` |
| Reclaim never signals a target whose starttime was unreadable at selection | `tests/test_device_holders.py::test_reclaim_never_signals_a_target_whose_starttime_was_unreadable_at_selection` |
| Reclaim revalidates identity immediately before the FIRST signal too, not only between rounds | `tests/test_device_holders.py::test_reclaim_revalidates_identity_immediately_before_the_first_signal_too` |
| Reclaim never reports a permission-denied pid as signalled | `tests/test_device_holders.py::test_reclaim_does_not_report_a_permission_denied_pid_as_signalled` |
| I8 galaxy floor holds below / resets at floor; keys on frozen chips too | `tests/test_device_safety.py::test_galaxy_reset_floor_holds_a_single_chip_wedge`, `tests/test_device_safety.py::test_galaxy_reset_floor_holds_just_below_the_floor`, `tests/test_device_safety.py::test_galaxy_reset_floor_resets_exactly_at_the_floor`, `tests/test_device_safety.py::test_galaxy_reset_floor_still_resets_a_mass_drop`, `tests/test_device_safety.py::test_cascade_router_keys_the_galaxy_floor_on_frozen_chips_too`, `tests/test_device_safety.py::test_a_reset_frac_above_one_fails_safe_to_the_default_floor_not_disabled` |
| I9 ledger durable-before-fire, abort on unwritable, fail-closed read, retract | `tests/test_device_safety.py::test_auto_reboot_records_durably_before_it_fires`, `tests/test_device_safety.py::test_auto_reboot_aborts_when_the_ledger_cannot_be_written`, `tests/test_device_safety.py::test_governor_fails_closed_when_the_ledger_is_unreadable`, `tests/test_device_safety.py::test_governor_blocks_a_reboot_inside_the_min_interval`, `tests/test_device_safety.py::test_record_auto_recovery_round_trips_through_the_durable_ledger`, `tests/test_device_safety.py::test_ledger_skips_a_corrupt_line_without_losing_the_rest`, `tests/test_device_safety.py::test_a_power_cycle_that_fails_to_launch_retracts_its_ledger_entry` |
| I10 per-severity interval | `tests/test_device_safety.py::test_power_cycle_escalates_past_a_recent_reboot`, `tests/test_device_safety.py::test_power_cycle_does_not_escalate_past_a_recent_power_cycle`, `tests/test_device_safety.py::test_reboot_does_not_de_escalate_past_a_recent_power_cycle` |
| I11 host rung only after exhausted reset (two strikes); once-per-episode latch | `tests/test_device_safety.py::test_cascade_router_escalates_to_the_host_rung_only_once_reset_is_exhausted`, `tests/test_device_safety.py::test_stuck_hold_escalation_retries_on_the_grace_cadence`, `tests/test_device_safety.py::test_ubb_tray_reset_fires_at_most_once_per_hold_episode`, `tests/test_device_safety.py::test_escalate_forced_suffix_bypasses_the_retry_pacing` |
| I12 quiesce + in-flight flag + restore rules | `tests/test_reset.py::test_streaming_reset_quiesces_pollers_and_flags_in_flight`, `tests/test_reset.py::test_reset_quiesce_restores_pollers_even_if_the_rescan_is_cancelled`, `tests/test_reset.py::test_reset_quiesce_leaves_pollers_off_when_the_reset_times_out`, `tests/test_reset.py::test_reset_quiesce_restores_pollers_when_a_failed_launch_leaves_no_scope`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_defers_the_dead_chip_sampler_during_the_transient_drop` |
| I13 tool verify = heartbeat+snapshot; unhealthy downgrade | `tests/test_reset.py::test_reset_tool_reports_health`, `tests/test_reset.py::test_verify_health_fails_on_short_chip_count`, `tests/test_reset.py::test_verify_health_fails_on_wedged_arc`, `tests/test_reset.py::test_verify_health_passes_on_over_count_from_stale_expected` |
| I14 warm reboot never fired where futile; blocked climbs are loud | `tests/test_device_safety.py::test_host_escalation_for_drop_sends_all_off_bus_to_the_cold_rung`, `tests/test_device_safety.py::test_host_escalation_for_drop_routes_a_futile_reboot_to_the_cold_rung`, `tests/test_device_safety.py::test_gate_all_off_bus_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_reset_regression_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_all_off_bus_power_cycles_when_opted_in` |
| Cold rung fireable-or-off (ipmitool) | `tests/test_device_safety.py::test_a_host_without_ipmitool_serves_with_the_cold_rung_off`, `tests/test_device_safety.py::test_ipmitool_present_leaves_the_cold_rung_armed` |
| Host-rung chooser order | `tests/test_device_safety.py::test_choose_escalation_prefers_reboot_then_power_cycle`, `tests/test_device_safety.py::test_choose_escalation_power_cycle_alone_goes_straight_to_it`, `tests/test_device_safety.py::test_choose_escalation_none_when_neither_opted_in` |
| Bridge reset: recovers, retries, inapplicable falls through, stale bus refused | `tests/test_device_safety.py::test_recovery_resets_through_the_bridge_and_clears_the_chip`, `tests/test_device_safety.py::test_bridge_reset_retries_before_escalating`, `tests/test_device_safety.py::test_an_inapplicable_bridge_reset_is_not_retried`, `tests/test_device_safety.py::test_an_all_inapplicable_bridge_reset_never_reads_as_recovered`, `tests/test_device_safety.py::test_a_reused_bus_refuses_the_bridge_so_a_stale_address_never_sbrs_live_silicon`, `tests/test_device_safety.py::test_bridge_reset_does_not_reach_a_gone_endpoint_by_default`, `tests/test_device_safety.py::test_a_bridge_less_chip_is_rescanned_before_anything_destructive` |
| I16 tray identity is the bus group, per architecture, never the chip index | `tests/test_ubb_tray_map.py::test_a_blackhole_tray_comes_from_the_bus_group_not_the_chip_index`, `::test_a_wormhole_tray_uses_the_wormhole_table_for_the_same_buses` |
| I16 the map is all-or-nothing (unreadable bus, unknown group, non-Galaxy or unknown board) | `tests/test_ubb_tray_map.py::test_an_unreadable_bus_id_yields_no_map_rather_than_a_partial_one`, `::test_a_bus_group_outside_the_table_yields_no_map`, `::test_a_non_galaxy_board_type_yields_no_map`, `::test_an_unknown_board_type_yields_no_map` |
| I16 the bitmap, the affected set and the walk all key on the real tray | `tests/test_ubb_tray_map.py::test_a_whole_tray_drop_sets_the_bit_for_the_tray_that_is_down`, `::test_affected_trays_names_the_tray_an_operator_would_read_from_tt_smi`, `::test_the_walk_leads_with_the_affected_tray_then_sweeps_the_rest` |
| I16 no map means the rung declines, never arithmetic | `tests/test_ubb_tray_map.py::test_without_a_map_every_tray_decision_declines`, `::test_a_chip_the_map_does_not_place_declines_rather_than_resetting_the_rest`, `::test_a_broker_with_no_cached_bus_map_declines_the_walk` |
| I16 the bus map is banked by the gate's own snapshot and reaches the fire | `tests/test_ubb_tray_map.py::test_the_snapshot_caches_every_chips_bus_id`, `::test_the_walk_re_powers_the_tray_the_dropped_chips_actually_sit_on` |
| I16 a boot platform probe (`expected_count=0`), a degraded first pass, or any short snapshot never freezes the map | `tests/test_ubb_tray_map.py::test_a_partial_boot_snapshot_never_freezes_the_map`, `::test_a_degraded_first_normal_snapshot_cannot_freeze_the_map`, `tests/test_reset.py::test_bank_bus_ids_once_rejects_a_snapshot_shorter_than_the_mesh`, `::test_bank_bus_ids_once_rejects_a_boot_probe_that_did_not_ask_the_question`, `::test_bank_bus_ids_once_rejects_a_chip_enumerated_without_a_bus_id`, `::test_bank_bus_ids_once_rejects_an_empty_list`, `::test_bank_bus_ids_once_first_full_read_stands`, `::test_bank_bus_ids_once_stays_open_after_a_rejected_snapshot` |
| I16 the board type is normalized before selecting the table (WH ` L`/` R` suffix) | `tests/test_ubb_tray_map.py::test_a_wormhole_snapshot_with_l_r_suffixes_still_produces_a_tray_map`, `::test_a_wormhole_lookup_survives_the_snapshot_suffix_inside_tray_map` |
| I16 a warm-reset topology drift journals `bus_map_drift`; the cached map stands | `tests/test_ubb_tray_map.py::test_a_drifted_snapshot_journals_but_leaves_the_cached_map_standing` |
| I16 a new GLX board type this build cannot map surfaces as `ubb_tray_table_missing` | `tests/test_ubb_tray_map.py::test_a_glx_board_type_with_no_matching_arch_suffix_journals_once` |
| Tray rung: plan/walk semantics, fabric-gated clear, decline cases | `tests/test_device_safety.py::test_ubb_reset_plan_maps_a_clean_whole_tray_drop_to_its_bitmap`, `tests/test_device_safety.py::test_ubb_tray_walk_plan_orders_affected_trays_first_then_the_rest`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_stops_as_soon_as_the_mesh_is_healthy`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_does_not_clear_a_hold_on_an_unverified_fabric`, `tests/test_device_safety.py::test_ubb_tray_reset_declines_a_fully_off_bus_mesh_it_is_the_cold_rung`, `tests/test_device_safety.py::test_maybe_emit_ubb_reset_required_names_the_exact_bmc_command_on_a_tray_down`, `tests/test_device_safety.py::test_the_tray_reset_rung_is_armed_by_default` |
| Tray fire argv/handshake (tt-smi compat, off-bus chip skipped) | `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_imports_a_symbol_the_installed_tt_smi_defines`, `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_pulses_the_tray_when_a_chip_is_already_off_the_bus`, `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_falls_back_to_the_chip_reset_class_on_older_tt_smi` |
| Tray rung's two branches: no-window sweep vs generic walk, classified once from threaded evidence, opt-out honoured | `tests/test_ladder_v2.py::test_classify_hold_names_the_two_branches`, `tests/test_ladder_v2.py::test_gate_tray_down_no_window_dispatches_the_back_to_back_sweep`, `tests/test_ladder_v2.py::test_gate_partial_tray_or_a_bridge_window_takes_the_generic_walk`, `tests/test_ladder_v2.py::test_gate_tray_down_no_window_names_the_command_and_holds_when_not_opted_in`, `tests/test_ladder_v2.py::test_bridge_rung_records_no_bridge_and_it_survives_the_server_replace` |
| Last-chance sweep gates EVERY host rung (gate ladder, idle escalation, post-reboot climb); a sweep that recovers cancels the cycle; never over a tenant; no per-chip SBR on a mass drop | `tests/test_ladder_v2.py::test_gate_ladder_sweeps_every_reset_before_the_power_cycle`, `tests/test_ladder_v2.py::test_a_sweep_that_recovers_the_mesh_cancels_the_power_cycle`, `tests/test_ladder_v2.py::test_the_other_two_roads_to_a_power_cycle_go_through_the_sweep`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_tenant`, `tests/test_device_safety.py::test_a_mass_gone_drop_is_left_to_the_galaxy_reset` |
| A RUNNING broker job counts as a tenant even with an empty holder scan; `force` waives an unreadable scan but never a running job; no rung hand-rolls the fd-only check | `tests/test_ladder_v2.py::test_a_running_job_is_a_tenant_even_with_an_empty_holder_scan`, `tests/test_ladder_v2.py::test_force_waives_an_unreadable_scan_but_never_a_running_job`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_running_job`, `tests/test_ladder_v2.py::test_every_tenant_guard_goes_through_the_one_helper` |
| Gate rungs release without over-climbing | `tests/test_device_safety.py::test_a_recovered_bridge_reset_releases_without_the_mesh_wide_reset`, `tests/test_device_safety.py::test_a_recovered_ubb_tray_reset_releases_without_the_mesh_wide_reset`, `tests/test_device_safety.py::test_a_recovered_mass_drop_reset_releases_without_reaching_the_host_rungs` |
| escalate() outcome mapping; unknown phase rejected | `tests/test_device_safety.py::test_escalate_waits_when_nothing_ran`, `tests/test_device_safety.py::test_escalate_recovers_on_a_present_mesh_reset`, `tests/test_device_safety.py::test_escalate_reports_terminal_when_a_present_mesh_reset_fails_and_climbs`, `tests/test_device_safety.py::test_escalate_does_not_report_recovered_when_the_mesh_was_never_degraded`, `tests/test_device_safety.py::test_escalate_rejects_an_unknown_phase` |
| Forced ladder has teeth on every platform | `tests/test_device_safety.py::test_force_escalate_has_teeth_on_a_per_target_host`, `tests/test_device_safety.py::test_hold_past_ceiling_triggers_forced_escalation` |
| Post-reboot verify: climbs to cold rung, never a second warm reboot | `tests/test_device_safety.py::test_post_reboot_all_off_bus_climbs_to_the_power_cycle`, `tests/test_device_safety.py::test_post_reboot_verify_never_repeats_the_warm_reboot`, `tests/test_device_safety.py::test_post_reboot_all_off_bus_holds_loudly_with_no_cold_rung_opted_in`, `tests/test_device_safety.py::test_boot_attribution_rejects_a_stale_escalation`, `tests/test_device_safety.py::test_an_escalation_fired_on_this_same_boot_is_not_attributed_to_it` |
| Reset tool contract: steps/attribution/no-devices/job stop | `tests/test_reset.py::test_reset_success_reports_steps_and_command`, `tests/test_reset.py::test_reset_failure_surfaces_returncode_and_output`, `tests/test_reset.py::test_explicit_reset_leaves_one_row_attributed_to_the_caller`, `tests/test_reset.py::test_reset_no_devices_does_not_call_tt_smi`, `tests/test_reset.py::test_a_reset_stops_the_running_job_gracefully`, `tests/test_reset.py::test_reset_device_quiesce_scope_routes_a_live_privsep_job` |
| Rung inventory stated loudly at boot | `tests/test_rung_arming.py::test_a_rung_that_is_off_is_stated_loudly` |
| Stage metrics: structural inapplicability is not "blocked" | `tests/test_metrics.py::test_ubb_tray_no_tray_on_this_platform_is_not_applicable_not_blocked`, `tests/test_metrics.py::test_bridge_reset_missing_pci_address_is_not_applicable_not_blocked` |
