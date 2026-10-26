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
reset, `tt-smi` reset, last-chance sweep, cold BMC chassis power cycle — where
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
  cycling (that reset may yet succeed; the next caller adopts it). The cooldown never parks a hold:
  a measured fault the reset did not heal (e.g. class D, lost fabric links or a latched topology
  mismatch, spec 03 I42/I71) climbs to the host rung at the 600 s stuck-hold ceiling, so its time to
  recover is the failed pass, the reset (~4 min) and the power cycle within the 15 min class budget.
  The one stated exception is spec 03 I42's watchdog path: with no idle relift reset first, the reset
  starts at 600 s and the host rung follows about 4-6 min later.
  An operator reset (`tt_device_reset`, tool and stream) settles the cooldown the same way, on the
  clock of its start: `reset_failed` (a non-zero exit) and `reset_unhealthy` (I13) arm it and set
  `last_reset_failed`, so a failed operator reset is the first of the gate's two strikes (I11);
  `reset_complete` and `reset_unverified` (a persistent 77 is unknown, not failed, as for the broker's own resets) clear the
  flag; a refused or timed-out one changes neither. Before, a failed operator reset armed nothing:
  the next gate fired a back-to-back broker reset at the mesh it had just failed on, and that
  broker reset's failure counted as the first strike, not the second. A failed operator mesh reset
  (`-glx_reset`, `-glx_reset_auto`) is also the episode's one mesh reset, marked as the broker marks
  its own (I37 class C, I87); a failed chip reset (`-r`) is not.
  The cooldown stops a repeat MMIO reset only, and a power cycle is not one: on its WAIT the gate
  first runs the I37 class check (no rung, no tray-down prelude), and the idle off-bus escalation
  runs it before its own cooldown check. Time to recover, per class, after a failed operator reset:
  class A (whole trays off) and class C (all off, the failed mesh reset spent) go to the power cycle
  on the next gate or idle pass, as without the cooldown, or faster where the failed operator mesh
  reset now counts as class C's one reset; a partial drop or a present mesh (class D) waits out the
  rest of the cooldown, re-checked on the hold's ~120 s grace, with no repeat reset, then climbs as
  above; the hold bound (I76) is unchanged for every class, since its forced ladder and power cycle
  run through the cooldown. Nothing lifts the hold on any of these paths without a just-passed full
  check, fabric included (I13; the boot after a cycle runs the startup gate's).
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
  cycling. The surgical bridge reset likewise declines while a reset is live.
- **I6 — The reset gate.** An operator reset (`tt_device_reset`, `tt-device-mcp reset`) is refused
  while any foreign tenant — a holder with `uid >= MIN_TENANT_UID` (1000) other than the caller —
  holds `/dev/tenstorrent`, and an incomplete holder scan fails closed (an unreadable holder may be
  a tenant). `force=true` overrides both, but the decision still carries the foreign holders so the
  override is logged against what it ran over. Holders below `MIN_TENANT_UID` (root, telemetry
  daemons) are infrastructure, not tenants, and never block, with two exceptions that count as
  tenants whatever their uid (`device_holders.is_tenant`): a process in a broker job or exec scope
  (`ttdev-job-*.scope`, `ttdev-exec-*.scope` — a killed or finished job's straggler still winding
  down is still that job), and a uid named in `TT_DEVICE_MCP_TENANT_UIDS`. A stale fd to a deleted
  device node is not a holder.

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
  paths an incomplete scan counts as a tenant. A rung the forced pass calls reads the same forced guard as the pass: an incomplete scan does not stop it, a
  readable tenant or a RUNNING broker job does. `auto_recovery_allowed` makes the tenant check
  absolute for reboot/power-cycle — a tenant outranks every other signal.

  A ceiling escalation that defers says what blocked it, in its log line and in a
  `stuck_hold_deferred` health event with the same fields: a running broker job, the tenant holder
  (pid, uid, user, comm), or `holder scan incomplete (source=..., gaps)`. `HolderScan.gaps` lists
  each blind spot that made the scan incomplete (a pid the driver names that has no `/proc` entry
  here, an unreadable fd table, a pid that holds no device fd here — also a process mid-exit). The
  forced pass waives an incomplete scan as before and emits no deferral. The broker's own eth
  reader is reaped before such a scan runs (spec 03 I16), so it never blocks its own escalation.

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
- **I9 — The durable auto-recovery ledger.** Every host-level escalation (the power cycle, the only host rung, I71) is
  appended to `auto_recovery.jsonl` and fsync'd BEFORE the action fires; if the record cannot reach
  disk the action is aborted — an unrecorded power cycle is one the next boot repeats. The ledger is the
  rate limiter and survives the host restart it records: a wall-clock min interval
  (`AUTO_RECOVERY_MIN_INTERVAL_SEC`, 3600 s, spanning the restart itself) plus a per-boot cap
  (`AUTO_RECOVERY_MAX_PER_BOOT`, 1), counted over this boot's records within the action's interval,
  so it never holds past a stated time (I65). The power cycle is spaced by its cooldown (I65) instead
  of the min interval. An unreadable ledger denies (fail closed); a fire that raised
  without launching retracts its entry so the retry is not spent on a recovery that never happened.
  A power cycle whose following boot reached a verified-healthy release — a gate pass whose fabric
  check ran and passed (the startup gate's full check included), or a hold lifted on such a pass —
  healed the box, so it no longer anchors the power cycle's cooldown (I65), and neither does the
  host boot it brought up once that boot is verified: a new drop or wedge after it power-cycles at
  once (I10). A `reboot` row an older broker wrote still reads as a broker recovery (boot
  attribution, the loop guard); this broker writes none.
  The proof is stamped in
  `auto_recovery_verified.json` (boot id, time; the first per boot); a missing or unreadable stamp,
  a stamp from the cycle's own boot, or a row with no boot id keeps the row gating, so a cycle no
  later boot verified stays the boot-loop guard. A hard backstop counts every power cycle, verified
  or not: at `POWER_CYCLE_BACKSTOP_MAX` (3) inside `POWER_CYCLE_BACKSTOP_WINDOW_SEC` (7200 s) the
  power cycle is refused, journalled `power_cycle_backstop` (`host_at_risk`, `allowed_at_epoch`)
  once per cap reached and paged until the alert hook takes it, so a flapping tray reaches its owners
  instead of cycling a serving host forever. The refusal is bounded like every I65 denial: it holds
  until the oldest counted cycle leaves the window (at most 2 h), names that time, shows on `/health`
  as `power_cycle_cooldown` with cause `power-cycle backstop`, and is asked again then. A NaN,
  infinite or future stamp counts as nothing, so no corrupt record holds the cycle for good.
  A host death is never a cycle: only the broker's own requested escalations write the ledger, so a
  boot the broker did not request neither gates a rung nor spends the per-boot cap, and it proves
  nothing about an unverified cycle before it. **Time to recover:** a whole-tray or all-off drop in
  a new episode within 30 min of a cycle a later boot verified goes from 30+ min (the interval,
  while the mesh reset and the sweep repeat) to about 5-8 min (one power cycle, then the startup
  gate's full check, fabric included, after hugepages are ready, before any release).
- **I10 — Per-severity rate limiting; the power cycle has no reboot fallback.** The min interval
  gates per rank (`reboot` < `power-cycle`; only an older broker's `reboot` row still carries the
  lower rank): no rung re-fires itself inside it, and a stronger record blocks de-escalating to a
  weaker rung. An unclassifiable record counts as strongest. A power cycle the cooldown (I65), the
  per-boot cap or the I9 backstop refuses is asked again the moment that wait ends: the
  hold-deadline watchdog forces one ladder pass for the hold episode it was refused in (kind
  `power_cycle_cooldown`), instead of waiting for its next ceiling window. The governor still
  decides that pass, and it releases only through I32. A power cycle a later boot verified healthy
  (I9) blocks nothing: the fault after it is a new episode, not a boot loop. There is no warm
  reboot rung and nothing reboots the host in the power cycle's place: a cycle the governor denies
  holds and is asked again at the deadline `/health` shows; a cycle that cannot launch (its command
  is missing or exits non-zero, so its ledger row is taken back) keeps the hold, pages, and is asked
  again on the next pass and at the I76 bound. No flag, env var, config key or CLI option brings a
  reboot back: `TT_DEVICE_MCP_AUTO_REBOOT`, `TT_DEVICE_MCP_POST_REBOOT_VERIFY` and
  `TTDEV_AUTO_REBOOT` are no longer read. **Time to recover** (a present-mesh wedge the ladder does
  not heal, classes D and G2): the ladder, then one power cycle (about 5-8 min with its boot), or
  the I65 cooldown's end when a cycle or boot fell inside it, then the startup gate's full check,
  fabric included; before, a reboot and its boot (about 3-5 min) came first and the cycle only after
  that boot and its cooldown (30 min to 4 h). A cycle that cannot launch: held, paged and retried
  each pass with no reboot; before, a fallback reboot every `AUTO_RECOVERY_MIN_INTERVAL_SEC`.
- **I11 — One attempt, gentlest first, climb only on failure.** No rung fires twice back-to-back:
  the reset is one attempt then cooldown (one exception: a Galaxy `-glx_reset` that exits non-zero
  with every chip back on the bus but some ARC not ready, on an idle box, gets exactly one more
  reset after `ARC_NOT_READY_RETRY_SETTLE_SEC`, through the same gated path and full verify; a chip
  still off the bus never does. Only the full check after that retry, fabric included, releases: a
  fabric pass that finds fewer links than the cabling descriptor expects is no pass, and the hold
  stays. A second failure is a failed reset like any other, so the ladder climbs and the I76 bound
  still runs the full ladder and then the host rung. Time to recover for an ARC-not-ready mesh after
  a failed `-glx_reset`: about one settle plus one reset and its full check (measured: the next
  reset recovered at once), instead of the hold's next window (10 min); when the retry does not heal,
  the next rung is late by at most the settle plus one reset (`DEVICE_RESET_TIMEOUT_SEC`) if the retried reset exits non-zero, and by that plus the full check (bounded by the existing verify and fabric-retry limits) if it exits 0 but the check fails. No other
  class changes: a chip off the bus, a per-target `-r`, a running job or a tenant gets no retry); the
  idle ladders latch once per hold episode (the latch
  expires after `HOLD_ESCALATION_REARM_SEC` so a hold never becomes permanent). Host rungs are reachable only after a `tt-smi` reset has already run
  and failed — at the gate additionally only with a prior failed cycle on record (`was_failing`,
  the two-strikes rule).
- **I12 — Quiesce around every reset.** `reset_with_quiesce` stops the MMIO pollers and sets
  `reset_in_flight` before the reset (a reset takes chips off the bus; a poller reading an off-bus
  endpoint is the MMIO stall that reboots the host, and the sampler would isolate mid-reset
  all-ones chips as dead), and restores them only when no reset scope is still cycling. A PCI
  rescan runs before restore so isolated endpoints re-enumerate. The poller list defaults to
  `tt-telemetry` and `tt-metrics-exporter` on every host. It names no clock-cap keeper, which every
  reset would otherwise stop and restart on every host. A host that runs another unit that reads or
  writes the chips adds it with `TT_DEVICE_MCP_POLLER_SERVICES`. A listed unit the host does not
  have is skipped and never fails or blocks the recovery. The restore starts only the pollers the
  stop found running (or `activating`): one stopped before the reset stays stopped. A poller is
  recorded before its stop is sent, so one whose stop fails or outlasts its bound (a poller stuck
  on a hung chip) is still restored, and a start that fails or times out stays recorded for the
  next restore. A state probe that fails or times out counts the unit as running. The record is
  kept on disk, so a broker restart between a stop and its restore loses nothing.
  The startup fabric check is quiesced the same way, as one block: the pollers stop before the
  forced startup gate and start again on every exit (pass, fail, raise), except while a reset scope
  is still cycling; then their start is owed to the next close to HEALTHY, as after a failed
  reset. A reset the gate climbs to inside that block defers its own restore to it, so
  its post-reset verify runs on the same quiet bus. The block is the pollers' one owner: a step
  inside it that owes them (spec 03 I66) records no owe of its own, and nothing inside it starts
  them; the owner applies the rule below on exit, or owes them itself. The added time is one `systemctl stop` before
  the check and one `start` after the verdict, each bounded at 30 s per unit.
  A restore starts the pollers only on a HEALTHY FSM. While the device is held, recovering or down
  (a failed reset, a reset whose verify has not passed, a startup gate that held the mesh) they stay
  stopped, and the next close to HEALTHY, reached only through a passing full check, fabric
  included, starts them, off the release path. A poller started on a held mesh re-opens every chip,
  off-bus ones included, and crash-loops under `Restart=on-failure`. An operator reset leaves the
  FSM HEALTHY, so a restore also waits while the reset still owes its fabric pass
  (`FABRIC_DEBT.blocks_release`, I32): the I13 verify runs on a quiet bus, and the passing fabric
  pass that settles the debt starts them. The dead-chip valve (spec 03) stops the pollers with no
  paired restore; it marks the restart owed the same way, so an episode the idle relift closes
  without a reset still starts them on its close to HEALTHY. No recovery or release waits on any
  of this, so no fault class's time-to-recover changes.
- **I13 — An operator reset on a mesh proves the fabric before it releases.** After
  `tt_device_reset`'s command exits 0 (tool and stream alike), a mesh (expected > 1 chip) on a host
  with a fabric check installed is verified exactly like the ladder's own resets:
  `_verify_device_after_reset` — eth read and fabric pass, a 77 re-checked
  `POST_RESET_FABRIC_RETRIES` times. Fabric pass → released (`health_ok: true`, the runtime-reported
  fault retired). A failed probe → `reset_unhealthy`, marked dirty (`why=operator_reset_unhealthy`), never
  HEALTHY. Still 77 after the retries → `reset_unverified`, held `fabric_unverified` (not dirty),
  the reported fault kept. A single chip, or a mesh with no fabric check installed (which could never
  produce that verdict), keeps the light verify — `fsm.observe(..., run_fabric=False)` against the
  host's expected chip count (never the survivors), chip enumeration/count and ARC state only — and
  the mesh case says so in its output; there `health_ok: true` is not proof against an eth/fabric
  wedge. A light pass still settles like the full one (released and the reported fault retired,
  or dirty): it is the strongest verify that host has, and holding for one it cannot run would
  strand it after every reset.
- **I14 — The host rung is armed by default, opt-out, and fireable-or-off.**
  `_auto_power_cycle_enabled` defaults ON (`TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` opts out) and
  additionally reads as OFF where the process could not run it (I17) — armed means armed AND
  fireable. The power cycle is the only host rung (I71); no switch restores the removed warm reboot.
  With the power cycle off or unfireable, a hold that needs the host rung goes to an operator (I33,
  I36), paged, still held.
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
- **I16 — A tray is identified by its chips' PCI bus, never by their index or list position.** The
  UBB tray holding a chip is derived from that chip's PCI bus, masked to its tray group (`& 0xF0`)
  and looked up in tt-smi's own `WH_UBB_BUS_IDS` / `BH_UBB_BUS_IDS`, which are imported and never
  copied here (the same rule `_glx_board_types` follows for the board-type list). Neither the
  chip index nor a position in a list stands in for that mapping. The chip index (the `N` of
  `/dev/tenstorrent/N`, sysfs `tenstorrent!N` and the heartbeat read) is the kernel's enumeration
  order, which need not follow PCI order: on a Blackhole Galaxy chips 16-23 sit on buses `0xc1`-`0xc8`
  (tray 3) and chips 24-31 on `0x81`-`0x88` (tray 4), while tt-smi's snapshot lists chips in PCI
  order (`0x8X` before `0xCX`). So `index // 8` is not the map, and neither is "the Nth bus id in
  the snapshot is chip N": both transpose trays 3 and 4 and name a tray
  the drop never touched (issue #27). The tray map is keyed by chip index and built from the
  kernel's own `{chip index: PCI address}`, read from sysfs (`tenstorrent!N/device`); a positional
  list is refused. The bitmap bit is `tray - 1`; the tables are 1-based and the BMC's bits are
  not. Every log line that names a tray gives its BMC mask and its chips as well
  (`tray 4 (BMC mask 0x08, chips 24-31)`), so "tray N" cannot be read two ways.

  **The map is cached, and no map means no fire.** A chip that has left the bus is absent from the
  tt-smi snapshot and has no sysfs node, so its bus cannot be read at the moment the rung wants
  it — which is every moment a tray must be named. The chip→bus map is therefore banked from a trusted
  full read and held until the next one: a full-count snapshot (every chip present, every `bus_id`
  non-empty) and, in the same pass, a sysfs map that names the same set of buses. When the two
  disagree (one read raced the bus) nothing is banked, `chip_bus_map_mismatch` journals both once,
  and a later full pass that agrees banks the map. A broker that restarted after the drop and
  never saw a full-mesh snapshot has no map, and then tray placement DECLINES rather than fall back
  to arithmetic: the same rule as I15's mesh reset, which never fires on a guessed mode. A decline
  here is `not_applicable`, not `blocked` — it is a missing fact, not an operator's kill switch.

  **A trusted full read re-banks the map; no other read touches it.** A chip's bus does not move,
  but the index the kernel gives it can: tt-kmd derives a Galaxy chip's index from its PCI address
  but takes a free one when that index is still in use, and older drivers always take a free one,
  so a chip can come back from a drop, a rescan or a tray reset under another index — the cycles
  recovery handles. A map that names the old index names the wrong tray. So every trusted
  full read is compared with the banked map, and one that differs replaces it in the gate's own
  pass, before anything names a tray from it, and journals `chip_bus_map_drift` with the
  old and the new map — on every change, not once per process, since each one moves a tray. A
  read that is short, has a chip without a readable bus, or disagrees with the snapshot never
  overwrites a banked map: a chip off the bus has no node to read, so that read cannot place it,
  and the last trusted map stands.

  Boot probes (`expected_count=0`) and short reads never fill the map. A chip missing from the map
  leaves every other chip's tray where it was, and a decision that needs the missing chip
  declines. Board types are normalized (`_normalized_board` strips wormhole ` L`/` R`) before the
  table lookup. tt-smi's own bus list is banked once beside the map and no tray is read from it:
  a later full snapshot whose list differs journals `bus_map_drift` once, and the list stands.
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
  | chassis power cycle | any | ipmi |

  Each rung's opt-out env var still applies on top. The device-scoped resets need no privilege:
  they are ioctls on `/dev/tenstorrent`, and that directory is also what scopes `<present>` to the
  chips this process was given. Rungs the OS would refuse read OFF at boot instead of raising at
  the top of the ladder; the `-forced` escalation bypasses the defers, not the arming. Privilege
  states what this process may run, not that it is alone on the device — I6 and I7 are unchanged.

  The boot inventory applies both columns; a rung's arming predicate applies only privilege.
  Off-platform the fire paths already decline as `not_applicable` (a missing fact, not a kill
  switch), and a platform conjunct in the predicate would report that as `blocked`.

  Privilege also picks the reset backend (I4), not just the rungs: `systemd-run --scope` refuses
  an unprivileged caller, and that rc would arm the cooldown against a reset that never ran.
  Reading a scope is a separate question from starting one — `scope_active` keys on systemd alone,
  so an unprivileged daemon still adopts a root broker's reset rather than racing it (I5).
- **I18 — An operator reset never kills a running job unasked.** A reset is not queued, so
  `tt_device_reset` (and its REST and streaming routes) refuses with `status: refused`,
  `reason: busy` and the job's id, owner and status while a broker job owns the device: the job in
  the runner's ownership window (`current_job_id`, which also covers a job gone `HUNG` and still
  being torn down) or a re-adopted job still `RUNNING`. Queued jobs do not block. The job is left
  untouched. `force=true` keeps the old behaviour — the runner's current job is interrupted
  (scope-routed, marked reset-killed) and the reset proceeds — but the job it runs over is logged
  first. A re-adopted job is stopped too: it has no process of its own in this broker, so its scope
  is stopped (SIGINT, then `systemctl stop` after `GRACEFUL_KILL_GRACE_SEC`), and it is marked
  `KILLED` and reset-killed (in memory only, so a restart re-adopts a scope still running) first so
  its end does not flag the device for another reset. A re-adopted job blocks every reset until its
  scope ends, whatever its status, and a forced reset waits (up to `READOPTED_FINALIZE_WAIT_SEC`)
  for its monitor to finalize it, so a fault in its log is recorded before the reset. The check
  is read under the job lock together with the pid or scope a forced reset would stop. The broker's own
  resets (the health gate's ladder, idle relift, forced escalation) call `reset_with_quiesce`
  directly, never through the tool, and are not subject to it.

- **I19 — A tray that is down at its first sighting gets one rescan and a read-only capture, then
  the full ladder.** On a Galaxy (`expected` at least two trays' worth of chips), the first off-bus
  sighting of an episode is latched before any reset fires: if the I16 tray map places every off
  chip and puts 4 or more (`TRAY_DOWN_MIN_CHIPS`) on one tray, the episode is a tray-down onset.
  Before the ladder's first rung, an onset gets, in this order: (1) one PCI rescan; (2) a read-only
  capture of the BMC, CPLD and PCIe state, under one deadline (`CAPTURE_DEADLINE_SEC`, 10 s); then
  (3) the full reset ladder, exactly as it runs without the prelude — the same stage from the
  router, the same evidence, the same rungs, the same last-chance sweep; and (4) the power cycle
  only from the ladder's own host rung, when the ladder has failed, under the same guard (opt-in,
  tenant guard, cooldown, boot loop). The prelude never picks, skips or adds a rung and never
  power-cycles. The one exception is a rescan that brings every chip back onto a mesh that passes
  the full verify (fabric included): the ladder is not needed and the chips leave the isolated set.
  As after the bridge rung, a runtime-reported fault is kept: a rescan re-inits no eth core and the
  verify cannot see a stuck one, so the fault escalates to the galaxy reset at the next gate. Every
  chip back but not verifying, or any chip still off, runs the ladder. The rescan writes to the PCI
  subsystem, so it waits while a tenant holds the device (or the holder scan is incomplete); the
  capture only reads and never waits. Each runs once per episode, before that pass's ladder.
  Everything else gets no prelude and keeps the ladder: the whole bus off (it has its own route
  straight to the cold rung), no banked map, a chip the map does not place, fewer than 4 off on
  every tray, or an off set first seen after a reset — one fired by the ladder this episode, any
  `reset_with_quiesce` since the mesh was last released (the reset tool and the HTTP reset
  included), one still in flight, or a live reset scope (a reset can itself turn a 1-chip drop
  into a whole tray off). The latch is held in memory until the gate releases the mesh and is
  deliberately not persisted: a restarted broker starts with none and takes the latch again the
  first time it sees the episode, so a tray still missing then gets a second rescan and capture
  (into a new bundle) before its ladder. That buys no extra power cycle: the ladder and its guard
  are the same, and the guard's interval and per-boot cap live in the on-disk auto-recovery
  ledger. The capture is read-only by construction (a fixed allow-list of argv shapes) and is
  fsync'd to disk before the first rung: into the `tray_down` incident bundle, or, when none was
  written, a `<stamp>_tray_down_bmc` directory of its own under the incidents root. It reads the
  BMC SEL, sensors (`sdr elist`) and controller state (`mc info`), `lspci -vv` of the upstream
  bridge of every off chip, the kernel log since the onset, and, when configured, the CPLD of
  every configured tray (the trays that stayed up are the baseline) and of the power-distribution
  board. CPLD bus, address and register numbers come only from the broker's config
  (`TT_DEVICE_MCP_TRAY_CPLD_*`, `TT_DEVICE_MCP_PDB_CPLD_*`, spec 06); without them those reads are
  skipped and journalled while the rest still run. A CPLD read is one register, never an
  index/data pair: that needs an index write to a tray that has just dropped, and costs time
  against the deadline. The last tt-smi telemetry is not read again: the gate's `unhealthy` bundle,
  written just before, holds the sampler's telemetry ring, and the `tray_down` bundle's
  `incident.json` holds the live per-chip PCI snapshot. Because the prelude changes no ladder
  decision, it is safe on any Galaxy the tray map covers. `TT_DEVICE_MCP_TRAY_DOWN_CAPTURE=0`
  turns the prelude off: every drop takes the ladder alone.

- **I20 — A reset puts an operator's AICLK ceiling back before anything else touches the chips.**
  Any reset clears the firmware clock cap (spec 03 I35), so `reset_with_quiesce` marks it owed
  before the rung runs and, after a reset that exited 0, re-applies it right after the PCI rescan —
  before the pollers return and before the caller's verify runs its traffic pass. Freshly reset
  firmware can still read its old limit seconds after the cap was sent, so this apply re-runs the
  helper every 0.5 s while a chip reads above the ceiling or unreadable, within a settle window
  (`TT_DEVICE_MCP_AICLK_CEILING_SETTLE_SEC`, default 3 s, 0 off, max 10) measured from the first
  run; only the last result is reported, so a readback that settles in the window journals no
  `aiclk_ceiling_unverified`, and one still high at its end journals exactly one. No run starts
  past the window, and a re-run's timeout is capped to the time left in the window plus 4 s, so
  the whole apply ends within max(helper timeout, window + 4 s): 8 s at the defaults, inside the
  ~10 s a post-reset step may add. A timed-out or cannot-run helper is never re-run. A failed rung leaves it owed; an apply that fails or raises
  never breaks the rung, and the verify's probe pass retries it and fails closed. Unset, no step
  runs.

- **I21 — No reset runs over a reaped job's leftover.** While a process a finished job left
  holding the device stands (spec 01 I16), every reset is refused: the reset tool and the
  streaming reset refuse with `force` too (`status: refused`, the holders named, and a hint
  that only a host reboot clears a process stuck in the kernel), and `reset_with_quiesce`
  checks the broker's `reset_fence` first and returns without a rung (`reset_fenced` event), so
  no caller (the gate, the ladder, a tool) can reset over one. A reset cannot free such a
  process and only hits the chips under it. The fence lifts once the holder is gone; one stuck
  in the kernel is bounded by I70 (I77 when it held a dead chip: 120 s).

- **I22 — No host rung without the full reset ladder.** The power cycle fires only after
  the last-chance sweep (SBR, mesh reset) ran COMPLETE and the one verify after
  it failed, or once this episode's ladder already ran its mesh reset and it failed on an unchanged
  off-bus set (I38: the sweep would repeat that reset, which I87 forbids). A sweep that was skipped (a reset already cycling in its own scope, a tenant) or
  incomplete (the I24 gate refused it) holds the host
  rung for that pass: `_settle_and_verify_before_host_rung` returns None and every caller holds.
  I70 is the other exception: under an unkillable leftover every reset is fenced (I21), so its host
  rung runs with no ladder and no sweep (also I77's).

- **I23 — No BMC tray re-power.** The broker never re-powers a single UBB tray: no
  `ipmitool raw 0x30 0x8b`, and no other per-tray UBB or BMC power action, on any path (the
  between-jobs gate, the idle and stuck-hold escalations, the last-chance sweep, the I76 ladder at
  10 min, startup). No flag, env var, config key or CLI option brings it back. Measured across
  Galaxy hosts it ran 190 times with 0 heals, and 4 times it broke the box: it took other trays off
  the bus with it or took host storage down. Below the galaxy floor the router answers
  `BELOW_FLOOR`, a decision, not a rung: a whole-tray drop goes to the I27 power cycle with no
  grace and no back-to-back reset sweep, its only wait the I65 cooldown (I37); anything else holds, and the I76 bound runs
  the full ladder at 10 min and requests the power cycle if the full check, fabric included, still
  fails. Only a passing full health check, fabric included, lifts the hold.
  The broker remembers each Tenstorrent root port and chip arch it has seen
  (`tt_pci_topology.json` in the health dir), so an AER mask (I24 `guard`) also covers a remembered
  port that still exists in sysfs. It looks at broker start and in every between-jobs check (sysfs
  reads; the file is rewritten, through a temp file and a rename, only when a look adds a port or
  an arch), so one healthy start covers the first drop after it and every later restart.
  Known limit: a root port never seen with its chip present on this host (the broker's first start
  here already found that chip off the bus) is not known and is not masked; every other port is.
  Sysfs that cannot be read (the PCI device list, the
  tenstorrent class list, or the vendor or address of a Tenstorrent function or of a port above
  one) is no answer, never "every chip off the bus": a look at start or between jobs keeps the recorded topology and leaves a
  `pci_topology_unreadable` event; the I24 gate counts every chip off the bus then, so `hold`
  holds, and a `guard` mesh-reset mask logs `aer_mask_failed` and masks the recorded ports. Any
  other entry it cannot read is skipped. PCI domains of 5+ hex digits (Intel VMD) are addresses,
  and a chip's root port is the first port above it in its own domain.

- **I24 — A per-host gate on automatic host-affecting resets** (`TT_DEVICE_MCP_HOST_RESET_GATE`).
  `off` (default) keeps the behaviour above. `guard` masks AER on every Tenstorrent root port around
  each automatic mesh reset and refuses an automatic mesh reset or last-chance sweep
  while the root ports' AER counters rise at `TT_DEVICE_MCP_AER_FLOOD_THRESHOLD` or more errors per
  `TT_DEVICE_MCP_AER_FLOOD_PERIOD_SEC` (a rate, so a trickle between looks hours apart is not a
  flood), or within `TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC` of one (the first look after a recent boot
  counts from zero at boot). `hold` also refuses while any chip has no PCI function, or keeps its
  function but its ARC heartbeat reads all-ones (its BAR reads return nothing: it has left the bus
  too), so a reset that dropped a chip is not re-issued over it. ENODATA alone proves nothing:
  the driver returns it while the chip still needs its post-reset init, whether or not the chip
  has power. A chip whose heartbeat read fails with ENODATA is on the bus with its ARC not ready
  (mid-boot after a reset), not off it, only while its KMD node is there, its PCI endpoint is
  bound to the driver, its link does not read down (`hostpci.link_down`) and a live read of its
  PCI config space (`hostpci.config_vendor_state`: the first two bytes of
  `/sys/bus/pci/devices/<bdf>/config`, read-only, never through the device node) returns the
  Tenstorrent vendor id 0x1e52: the heartbeat
  verdict reads it UNHEALTHY and names it `arc_not_ready`, but no off-bus count includes it (this gate, the
  reset and reboot floors, the bridge and gone-chip rungs, the hold-deadline
  climbs, `/health` `off_bus`). A config read of 0xffff or a link that reads down means the chip
  is off the bus, so a whole tray of such chips is class A (I37) and goes straight to the power
  cycle, with no ARC-not-ready wait. An unbound endpoint, a missing node or any other read error
  still counts the chip off the bus. A config read that fails or returns anything else is a third
  state, `config_unreadable`: like an EIO chip it counts off the bus for every rung and floor, so
  its hold stays and never lifts on it, and it is on the bus only for the class A read (I37); the
  heartbeat verdict and `/health` name it. The config read is made only for a chip missing from
  the heartbeat read with ENODATA, so a healthy pass reads no config space. Under every mode, `off` included, an automatic reset is refused while
  the flood window a port that errored again after its mask opened is still open: with the gate `off` that
  holds every automatic mesh reset and last-chance sweep (and so the host rung above
  them, I22) for `TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC`. The one exception is inside a last-chance
  sweep that already started: its mesh reset still follows its bridge reset, because no host rung fires without the full ladder (I22); the erroring port stays masked
  across that reset. The gate is checked before
  the reset is recorded (`reset_begin`, the cooldown clock): a refused reset did not happen. A
  refusal is handed to an operator, never a climb (I22), bounded by I27 and, with no power cycle
  configured, by the I33 operator hand-off. Operator and tool resets are not gated.
  The per-chip bridge SBR is gated the same way, counting its target chips as off the bus: under
  `hold` (or the I25 latch) it does not fire, and the PCI rescan still runs.

- **I25 — Latch hold after an off-bus reset hung the host.** The last-chance sweep's
  `last_chance_reset_sweep` event and line name each off-bus chip it planned around, with its
  banked PCI address and its tray. A whole tray off the bus in a below-floor drop, in the post-job
  gate or the idle escalation, hands the hold straight to the I27 power cycle with no grace
  (`offbus_gated_hold` phase `whole_tray_down`); the idle escalation journals it as
  `galaxy_reset_skipped_whole_tray` (`fault_class` `whole_tray`). A partial drop holds, and the I76
  bound runs the full ladder at 10 min. No tray is re-powered (I23).
  Before an automatic mesh reset or sweep with a chip off the bus, the broker writes
  `offbus_reset_intent.json` (with the boot id) to the health dir and removes it after the reset's
  verify, fsyncing the directory so a power loss right after cannot undo the removal. Before the
  broker's own reboot or power cycle fires (after its ledger row), it removes any intent still there
  the same way (`offbus_reset_intent_closed`): the host going down is the broker's, not a hang. A
  broker that starts on another boot id and finds an intent with an `auto_recovery.jsonl` reboot or
  power-cycle row at or after it, on the intent's boot, that brought this boot up, drops it
  (`offbus_reset_intent_closed`, `at_boot`) and latches nothing. The row brought this boot up only
  when this boot started 0 to `BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC` after it (plus the pending
  window for a request marked missed or late, I72), the rule every boot attribution uses: a request
  the cycler missed, then a real hang in a later reset and an outside power-off, is a hang. An
  unreadable btime attributes nothing, so the latch stays. Any other intent it finds on another boot id makes it write
  `offbus_reset_hold.json`, emit `offbus_reset_hold_latched` (host at risk), and from then on run
  the I24 gate as `hold`, whatever it is configured to. The latch is scoped to the boot it was
  written on (`latched_on_boot`): it survives broker restarts on that boot, and it is bounded. It
  lifts on its own (`offbus_reset_hold_expired`, naming why) once a health pass sees every expected
  chip on the bus and host-pci OK (a listed endpoint whose link reads down, 03 I9, is not on the bus), or once every chip is on the bus and the latch is older than
  `TT_DEVICE_MCP_OFFBUS_HANG_LATCH_MAX_SEC` (default 21600 s; the gate checks that
  ceiling whenever it is asked with no chip off the bus). While a chip is off the bus or sysfs cannot
  be read it lifts only where `TT_DEVICE_MCP_POWER_CYCLE_CMD` is unset, at the same ceiling: there no
  I27 power cycle would ever end the hold. That expiry pages once (`offbus_reset_hold_expired_off_bus`,
  host at risk), and the gate, which then checks the ceiling on every ask, falls back to its configured
  `TT_DEVICE_MCP_HOST_RESET_GATE` mode. Where a power cycle is configured the hold is the I27 power
  cycle's to end, and the boot after it drops the latch. A held gate names both boots: the one the
  reset ran in and the one the hold is on; so does the CLI's status line. One it cannot remove keeps holding (`offbus_reset_hold_unremovable`, host at
  risk). An operator may lift it sooner with `sudo <broker python> -m tt_device_mcp.health.recovery.pcie_guard
  --clear-hang-latch [--health-dir DIR]` (the logged hint names the broker's own python and health
  dir). Without `--health-dir` it clears the caller's health dir and the system broker's
  (`/var/lib/tt-device-broker/health`); a latch it cannot remove exits 1 and is never reported as
  absent. Once the host boots again (the I27 power cycle or an operator's) it no longer holds any
  rung: a latch from an earlier boot, or one naming no boot, is ignored and dropped at broker start
  (`offbus_reset_hold_expired`); with this boot's id unreadable it cannot be shown stale and holds.
  `TT_DEVICE_MCP_OFFBUS_HANG_LATCH=0` turns the intent and the latch off.

- **I26 — The pre-power-cycle hook is opt-in, bounded, and never gates the cycle.** With
  `TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK` set, `_fire_recovery_escalation` runs that command (argv
  split like shell words, no shell, its own session, the cycle's reason in
  `TT_DEVICE_MCP_POWER_CYCLE_REASON`) after the ledger record and jobs-list row and just before the
  BMC power cycle, and waits for it — so other workloads on the host can be drained first. The wait
  is bounded by `TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC` (default 300 s; a non-positive or
  malformed value reads as the default, anything above 3600 s is capped); past it the hook's process
  group is killed. A hook that is unset, unparseable, not startable, exits non-zero or times out is
  logged (`pre_power_cycle_hook` health event: rc, timed_out, seconds, output tail) and the cycle
  fires anyway. A cycle the ledger aborts never runs the hook; a broker stopped while waiting on
  it retracts the ledger entry (nothing fired). The hook changes nothing about which rung fires or
  when: the full ladder below the power cycle still runs first (I11); the power cycle is the only
  host rung. Unset, nothing runs and the timeline is unchanged.

- **I27 — An off-bus hold the I24 gate refuses to reset is bounded.** Once the gate refuses every
  reset because chips are off the bus, the hold no longer stands until an operator power-cycles the
  host. The first idle escalation of the episode still climbs the ladder's own rungs as the gate
  allows (none is skipped, I22). After that, once the episode has stood
  `TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC` (default 300 s), a partial off-bus drop (any drop that is
  not exactly one or more whole trays by the I16 map, or with no map) first gets one pass of the
  ladder's own rungs, the mesh reset included, with the `hold` gate read as `guard` for that pass
  only, once per episode, unless an I25 latch, `TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC=off` or a
  stand-back keeps the gate shut. The pass is spent only once its climb has passed the climb's own
  guards (the ceiling for a pass the watchdog did not force, the episode latch, a reset cycling in
  its own scope, a tenant, the reset cooldown) and reached the rungs: a pass those guards decline
  runs no reset, re-arms nothing and leaves the lifted pass due for the next pass that runs, so no
  decline sends a partial drop to the power cycle without its mesh reset. Each reset in it owes the
  full health check, fabric included, before any release; if the hold still stands, the hold watchdog forces one more pass at once and
  that pass runs the power cycle with the gate shut again. A whole tray down (every off-bus chip's
  tray fully off, nothing off outside those trays) gets no reset first. Then the broker runs the host's own
  power-cycle command `TT_DEVICE_MCP_POWER_CYCLE_CMD` (argv split like shell words, no shell)
  through `_fire_recovery_escalation` (ledger record, jobs-list row, the I26 hook), always after a
  clean OS shutdown (I88): the shutdown hook and the fallback unit both run it with the reason in
  `TT_DEVICE_MCP_POWER_CYCLE_REASON`. `TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC` (default 120 s)
  bounds the fallback's run; the hook clamps it to 5-15 s per try so its 3 tries fit
  systemd-shutdown's 90 s. A cycle that cannot start that way fires nothing and holds (I88). It holds instead while a tenant holds the device or
  the holder scan is incomplete, while a reset runs in its own scope, and while anything stands
  back: a path in `TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE` (`:`-separated) exists, the
  manual-recovery check exits 0 (an operator is recovering the host) or
  `TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD` exits 0. Exit 1 clears a check; any other exit, a
  timeout (10 s), a command that cannot start or a check that raises stands back. **The
  manual-recovery guard fails closed:** evidence it cannot read is a possible operator recovery,
  never "none". The check is `TT_DEVICE_MCP_MANUAL_RECOVERY_CMD`, else the installed
  `$TTDEV_ROOT/manual-recovery-check.sh` (`deploy/tt-manual-recovery-check.sh`, staged by
  `apply-host-config.sh`; it exits 2 when the sudo journal or an existing auth.log cannot be read,
  or the journal hides other users' entries). With neither, the power cycle never fires and the
  broker pages once (`offbus_power_cycle_unguarded`). A stand-back is re-checked on every pass, and
  the tenant and reset-scope checks run again after the stand-back checks, just before the cycle.
  Then the whole box is re-read: with every chip back on the bus no cycle runs (phase
  `back_on_bus`). A cycle due at no grace because a whole tray was off re-checks that a tray is
  still wholly off; when the tray map places the drop and none is, it does not fire, clears the
  whole-tray clock (phase `whole_tray_back`) and holds, so the partial drop goes back to the ladder
  and the other bounded clocks. That deferral runs once per hold episode: a whole tray seen off again
  in the same episode (a tray whose read flaps) fires at grace 0 on that pass without the re-check, so
  the deferral moves a flapping whole tray's power-cycle request from grace 0 to the
  hold bound's ladder plus its check (I76). At hold age 600 s the broker first runs the full ladder
  again, then asks for the cycle only when the full check after it fails: at once on a failed
  verdict, or up to 300 s after the ladder for a check still running. Worst case: about 600 s +
  ladder time + 300 s after hold start. The I65 cooldown and a live tenant may delay it further, as
  the bounded exceptions stated in I65 and I76. A read the map cannot place keeps the
  whole-tray verdict. The
  `firing` event names `fault_class` (`whole_tray`, `all_off`, `tray_partial` for 4 or more chips,
  `partial_offbus` for 1-3), `handoff` (`whole_tray`, `gate_refused`) and `hold_age_sec`, the hold's time to this cycle.
  Each attempt is written, fsync'd, to
  `offbus_power_cycle.json` in the health dir before the command runs; a failed command keeps its
  error there. Attempts are spaced by the I65 cooldown alone (the gated fire goes through the
  ledger, so I65 counts it), whatever the per-boot count. An operator may add a spacing with
  `TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC` (default unset: none), across restarts and the
  cycle itself, capped at the I65 backoff cap (4 h); it is a stated wait like the cooldown, on
  /health as `power_cycle_cooldown` with its deadline and asked again by the watchdog at its end.
  Inside either wait the hold stands with no reset (no fall-through to the mesh-reset ladder on a
  whole tray). Each attempt carries the boot it was
  made on; an attempt a later boot verified healthy (I9) does not count toward that spacing, so a
  new off-bus episode after it takes its cycle at the grace. An attempt with no boot id or no
  stamp to prove it healed still counts, and the I9 interval and backstop still apply. With the command unset, an attempt
  within the interval, or the mechanism's limits (I9, I10) denying it, the broker pages once per
  episode (`offbus_gated_hold_page`, `offbus_power_cycle_rate_limited`,
  `offbus_power_cycle_denied`, `offbus_power_cycle_unguarded`, naming the host, host at risk,
  `severity: high`, through the alert hook; a page the hook drops while busy is sent again on the
  next pass) and the hold stands as before; with the command unset, I33 bounds it. A cycle held
  back by a tenant, a reset in its own scope or a stand-back for
  `TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC` (default 1800 s) past the grace pages once
  (`offbus_power_cycle_blocked`, naming the blocker), so every gated hold ends in a power-cycle
  attempt or a page within grace + that bound, never an open-ended silent hold. The gate's refusal,
  including the I25 latch, never holds this rung: it is what the rung answers.
  The hold watchdog forces one escalation pass per episode the moment the grace runs out, so the
  cycle fires at the grace and not at the next ceiling window.
  Unset, the behaviour is I24's plus the I33 hand-off. `deploy/tt-manual-recovery-check.sh` is a template for the
  manual-recovery command: exit 0 when the sudo journal or auth.log shows a sudo-run reboot,
  poweroff, shutdown, halt, kexec or ipmitool power/reset in the last 30 min, 1 when not, 2 when it
  cannot read the journal.
- **I28 — An operator's outage hold holds every automatic mesh reset.** With
  `TT_DEVICE_MCP_AUTO_MESH_RESET=0` (an outage hold), no
  automatic mesh reset fires: not the between-jobs gate's, the idle relift's or the hold
  watchdog's, and no last-chance sweep. Unset or `=1` arms it. It is the only switch that holds the
  mesh reset: with the power cycle the only host rung, `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` alone is a
  per-host choice (no BMC power cycle) and still climbs through the mesh reset. Time to recover
  under the hold: B1/B2 (partial off-bus), D (fabric or health-check failure, a dispatch failure
  included), H (tenant-held verify) and J (a failed or timed-out job) lose the automatic mesh reset
  and stay HELD, paged, until an operator acts; `TT_DEVICE_MCP_AUTO_MESH_RESET=1` restores it. The
  broker names this at boot: it logs `RUNG GAP` with the held present-mesh reset and serves it on
  /health as `hold_exit_gap`.
  A held reset did not happen, as for I24: it is checked before `reset_begin` and the cooldown clock, sets no failed-reset flag, and the episode
  holds. This covers a chip off the bus in the startup and pre-job gates: under the hold they run no
  `tt-smi -glx_reset` and leave the device HELD (a mesh reset over an off-bus mesh is what drops
  the rest of it). It is logged, journaled (`mesh_reset_held`, host at risk, with the chips off the
  bus) and paged through the alert hook, naming the chips off the bus. The hold is bounded by its
  page, never silent: the gate returns at once with no wait, and while the hold stands the page
  goes out again every `TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC` (default 3600; `0` pages once per
  hold), and again after a pass that found no hold; a page the hook drops is sent again on the next
  pass. An already-running reset in its own scope is still adopted, and the operator's reset tool
  is never held. The same holds the one present-mesh reset a green-fabric dispatch
  failure earns (spec 03 I55): the gate holds and pages, logs the held reset by name and never
  promises an escalation. A clean exit's timed-out eth read (spec 03 I61) does not take that
  reset under the hold either: it keeps the fabric pass in the same probe. Nor does the reset a
  timed-out or killed job owes (spec 03 I69): the debt is cleared and the dirty exit's fabric pass
  runs as before. Pollers the broker owns (`tt_telemetry_server` as uid 0 in
  `system.slice`, `tt-metrics-exporter`) are infrastructure, not tenants (`is_tenant`): they never
  block that reset, which stops them for its window and starts them again after it.

- **I29 — One unverified reset per 77-only episode; the next escalation sweeps once, then climbs
  only on a measured failure.** A galaxy reset whose post-reset fabric is still a 77 after the
  eth-training retries (UNKNOWN, spec 03 I40) records its hold episode and counts its passes toward
  that episode's I40 bound. When the I40 fallback then refuses and the idle escalation runs again on
  the same episode, it fires no second identical reset: it runs the last-chance sweep (I22) once for
  that episode, strict on a 77 (a sweep whose verify still reads a 77 is no recovery and clears
  nothing). A sweep that measured a failure climbs to the host rungs with the sweep marked spent, and
  a later escalation of the episode climbs without sweeping again. A sweep whose verify still reads a
  77 for a known no-reset cause (a known fabric fault or missing cable, spec 03 I46/I47, a topology
  mismatch, a missing validator, or a hugepage pool not yet filled; never the spec 03 I42 latch,
  whose first job measured the fabric bad: it is read before the mismatch checks, so a latched hold
  whose 77 output, detail or reason names the mismatch still climbs) never climbs to the power cycle: it holds,
  journals and pages `fabric_unverified_no_host_rung` once per episode, and the idle relift keeps
  re-checking on its backoff (spec 03 I48). With no such cause, a reset and a sweep that
  both still read 77 leave the host rung as the only path left (a power cycle refills hugepages, reloads
  the driver and retrains the links), so the episode climbs to it once (`fabric_unverified_host_rung`,
  logged only as the rung fires, recorded in the auto-recovery ledger under its own cause, every guard of I22
  and the host rungs kept). The climb after the sweep never waits for the ladder ceiling (I79). A
  climb that only waits (a reset still in its scope) pages no one and leaves no hand-off; the next
  escalation retries it. The boot that rung brought up reads that cause from the ledger: while no
  fabric pass has passed since the broker started, it never fires that rung again. Once the rung is spent, or when it could not
  run (not opted in, rate-limited, a tenant), the hold goes to an operator once per episode (I33
  shape: one `fabric_unverified_operator_handoff` page, a `FABRIC_77_HANDOFF_SEC` (1200 s) act-by,
  `/health` `operator_handoff`; `owner=broker` while a held-off rung is still retried, I79; a power
  cycle only the bounded I65 cooldown holds off pages no one, I79); a rung that could not run is tried again on the next escalation, and once it fires it ends that hand-off. The
  device stays held until a passing full check with fabric releases it, never on the hand-off. Before
  this, such a hold re-ran the fabric pass every 1200 s with no end. A sweep that could not run in full holds that pass and
  fires no host rung (I22); the episode's next escalation tries it again. Bounds the loop of
  identical ten-minute resets one Galaxy host ran (eight, about 65 min) before it ever swept. All of this applies below the I76 bound; at the bound I76 runs the
  full ladder and then the host rung.

- **I30 — The deadline escalation never resets a fabric-only present mesh.** Past the escalation
  ceiling, when the fresh heartbeat read shows every expected chip present with a live ARC
  heartbeat, no chip is isolated, and the hold names only the fabric — a topology mismatch (the
  hold's detail or reason starts `topology mismatch`, or a fabric-unverified hold's last pass
  printed the validator's `Connection mismatch detected`), or a fabric pass that could not run (a 77) with no runtime fault
  reported and no pass that measured the fabric bad — the forced escalation runs no galaxy reset
  and no ladder rung. It holds, journals `hold_deadline_no_reset` and sends the alert hook once per
  episode; later ceiling windows stay quiet. A reset cannot fix cabling or a missing verdict, and
  on one Galaxy host a galaxy reset on a cabling mismatch took a chip off the bus and the ladder
  then re-powered its tray. An off-bus or dead-ARC chip, a runtime-reported fault, or a measured-bad
  fabric keeps the ladder. A known fabric fault (spec 03 I46) on a fabric-unverified hold is held the
  same way (`known fabric fault`), even when an older pass measured the fabric bad. A hold or pass
  that names the wrapper's `dispatch FAILED on a green fabric` is a measured fault, never a topology
  mismatch, even when the descriptor run before it printed `Connection mismatch detected`. Nor is
  a pass that found descriptor ports missing (spec 03 I71), nor any hold while the spec 03 I42
  first-job latch stands: that job measured the fault, so the mismatch line the lift left in the
  last fabric output names no cabling-only hold, and the forced window runs the ladder. All of this
  applies below the I76 bound; at the bound I76 runs the full ladder and then the host rung. A
  topology-mismatch or known-fabric-fault hold that this invariant holds with every chip on the bus
  is the I83 fabric link-short class: its watchdog gets one ladder at hold start, then the host rung
  (I83); this invariant still keeps any other forced run of that hold from resetting it. A latched
  or missing-port hold with every chip on the bus is that class too: its one reset is I83's ladder.

- **I31 — A galaxy reset that regressed an off-bus mesh is not repeated in that hold episode.**
  When a galaxy reset leaves more chips off the bus than before it (`reset_regressed_offbus`, on the
  gate or on the idle ladder, including before the ceiling), the broker latches that hold episode
  (a regression before any hold opened latches the episode that opens next). While the latch holds
  and chips are still off the bus, neither the gate nor the idle off-bus ladder (forced or not)
  fires the bridge or mesh reset rungs again: each journals `galaxy_reset_skipped_regressed`
  and goes straight to the post-reset verdict, with the drop left to the power cycle. With a
  power cycle opted in that is the cold rung, still behind the last-chance sweep (I22), so the full
  ladder runs once more before any power cycle; the climb does not wait for the hold ceiling (I79). Without one it pages
  (`reset_unrecoverable_power_cycle_required` or `all_chips_off_bus_power_cycle_required`) and holds.
  The latch clears when the device is released; it is in memory only, so a broker restart costs at
  most one more reset. Bounds the loop one Galaxy host ran: three mesh resets, one per forced
  window, took a 4-chip drop to 31 and then 32 off the bus.

- **I32 — No reset releases the device until a fabric pass has passed since.** Every reset action
  records a post-reset fabric debt (`post_reset_fabric_owed`): a `tt-smi -r` or `-glx_reset` through
  `reset_with_quiesce` (operator reset, ladder rungs, the last-chance sweep), a reset this broker
  adopted from a foreign scope, a bridge chip reset, a UBB tray reset or re-power, a PCI rescan (the
  tray-down prelude, I19), and every broker start (after a boot or a restart). Only a fabric pass
  that returns a healthy verdict settles it (`post_reset_fabric_settled`); a 77, a measured failure,
  or a verify that ran no fabric pass (enum+ARC only, the foreign-holder fast verify) leaves it
  owed. Every verified release goes through one funnel; while the debt is owed that funnel refuses
  (`post_reset_release_refused`) and holds the device fabric-unverified instead, so the fabric
  relift, the I40 bound and the deadline escalation settle it — bounded, never an open-ended hold.
  No rung outcome lifts that hold: a rung whose verify passed enum+ARC but whose release the funnel
  refused (the last-chance sweep before a host rung on a fabric 77, no verdict, or
  `fabric_check_unavailable`) ends the climb for that pass with no reboot or power cycle and journals
  `host_rung_sweep_unverified`, never a healthy gate; the gate, the idle relift and the hold
  escalation fold every outcome through one helper that keeps the fabric-unverified hold, and a rung
  that ran on a 77-only hold leaves it RECOVERING for 03 I48, never DOWN.
  The I40 startup-checks release does not apply while a reset owes a pass: that pass climbs the
  present-mesh rung instead (spec 03 I40). The post-job gate runs the full pass while the debt is
  owed and settles it in place; a job boundary that followed no reset adds no time, and the pre-job
  gate never runs the pass (03 I12). The startup pass runs only once the hugepage pool is ready
  (03 I48). Exempt only where no fabric pass can run at all: a single chip, or no fabric check
  configured on the host. There is no switch that turns it off.
- **I32a — A reset that ran drops the fabric verdict measured before it.** Once a mesh reset exits 0
  (or an adopted foreign scope ends), the last fabric verdict is cleared before the post-reset verify:
  the reset re-inited every eth core, so a FAIL measured before it no longer describes the mesh. The
  post-reset pass sets it again (healthy or failed); a post-reset 77 leaves it unknown, so the hold is
  the bounded 77 relift (03 I40), not the terminal DOWN a stale FAIL took it to. Release is unchanged:
  only the I32 debt's passing pass settles it.

- **I33 — With no power cycle configured, the gated off-bus hold is handed to an operator with a
  deadline, then ends in the broker's own ladder.** When the I24 `hold` gate refuses every reset of an
  idle off-bus hold and `TT_DEVICE_MCP_POWER_CYCLE_CMD` is unset (no I27 cycle can end it), the first
  escalation of the episode hands the hold to an operator, once per episode: one high-severity page
  (`offbus_gated_hold_page`) that names the host, says no power cycle is configured, the time the
  operator must recover the host by, what the broker does then, and that every reset is followed by the
  full health check, fabric included; the gauge `tt_device_broker_offbus_operator_deadline_seconds`
  (that time as Unix seconds, 0 once no hold is handed off); and `/health` `operator_handoff` (the
  reason, who must act, `act_by`/`act_by_epoch`, what happens then, and whether the manual-recovery
  check saw an operator at work). The deadline is the episode start plus
  `TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC` (default 1200 s, two 600 s ceiling windows; a non-positive
  or malformed value reads as the default). Past it the hold watchdog forces one escalation pass, and
  that pass runs with the gate's off-bus refusal lifted for that pass only (`hold` reads as `guard`:
  the AER flood checks still refuse): the ladder's own rungs (the bridge reset, then the mesh reset)
  may fire as I22 allows, and outside the pass the gate holds again. Nothing lifts the gate while an
  operator owns the hold: `TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC=off` (the operator's latch: the page
  says the hold stands until an operator recovers the host, and `act_by` is null), the I25 host-hang
  latch, a stand-back path or command (I27), or a manual-recovery check that exits 0; a stand-back
  past the deadline pages once (`offbus_power_cycle_blocked`). A missing manual-recovery check does
  not stand back here: this lifts a mesh reset, not a power cycle. Every reset a lifted pass fires owes
  the I32 fabric debt, so the device is released only after the full health check, fabric included,
  passes; a failed check keeps it held for the next bounded rung, and a reset the gate still refuses
  releases nothing. The hand-off ends (gauge to 0, `operator_handoff` gone) when the hold ends.

- **I34 — Once a host power cycle is requested, the hold watchdog starts no device reset
  until the host acts or the request expires.** A cycle handed to a `TT_DEVICE_MCP_POWER_CYCLE_CMD`
  wrapper can run some time after the request returns; a forced escalation (the off-bus one-shot, a
  ceiling window, any other hold-deadline trigger) that ran a bridge reset or galaxy
  reset in that gap would race the host going down. The request is marked once its I9 ledger record
  is written; while it is pending the forced ladder declines without spending its window latch and
  logs one `hold_escalation_suppressed_host_action` line per request. The device stays held and the
  I32 fabric debt stands: it is settled by the startup pass after the host returns, behind the
  hugepage gate (03 I33). Bounded: a request whose ledger write failed or whose fire did not launch
  never marks; one the host has not acted on within `TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC` (default
  900 s, capped at 3600 s; malformed or non-positive reads as the default) is journaled as
  `host_action_request_expired` and the next tick resumes the forced ladder.
  The same rule holds on every path that could start a device reset, not only the watchdog:
  `GalaxyRecovery.escalate` returns WAITING at its entry (before the tray-down prelude) for every
  caller and phase; the health gate's ladder (any phase, pre-job, post-job or startup) runs no rung,
  marks a healthy device dirty and holds it; an operator reset (`tt_device_reset` and its stream) is
  refused unless forced, and a forced one is logged. Each path journals one
  `reset_suppressed_host_action` event and one log line per request. A power-cycle command that timed
  out, rather than exited non-zero or failed to start, may have launched its detached cycler, so it
  keeps the request pending (`auto_power_cycle_failed` with `timed_out` true) under the same bound.
  A job held past its bound while a request is pending says so (the action, its age, the bound)
  instead of naming a device op in flight.
  The idle relift also runs no check while a request is pending (it checks before it spends its
  slot, and again under the device lock): a fabric or full check in that gap would load a host that
  is about to go down, and a pass there could release the device just before the cycle. The device
  stays held; the startup check after the boot (fabric included, behind the hugepage gate) decides.
  This adds no wait to any class: the relift waits only as long as the reset suppression above,
  bounded by the same `TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC` window, and resumes on its next tick
  once the request expires (I72 then retries a missed request).

- **I36 — A mesh reset that failed with the off-bus set unchanged is not repeated in that hold
  episode; the next window takes the next stronger rung.** The broker records, per hold episode, each
  mesh reset that RAN and failed, with the off-bus chip set before and after it; a reset the host gate
  or an outage hold refused did not run and is not recorded. Only a failure that left the set exactly
  as it found it counts here; I87 extends the rule to a set that shrank or changed, and a set that grew is also I31.
  While the set off the bus is still that recorded set, the idle off-bus ladder (forced or not) fires
  none of its reset rungs again, and a forced present-mesh window does not repeat the reset (the
  grace-cadence retries before the ceiling stay). It journals `reset_rung_skipped_failed_unchanged` and
  climbs to the host rung at once (I38), rate limits kept (a denied cycle holds, fires no reset and is
  asked again the next window). When no host rung is available
  (the power cycle off or unfireable) the hold goes to an operator once per episode through
  the I33 hand-off, latched (no deadline, `act_by` null), and no further reset fires on that set. Any
  later reset still owes the I32 fabric pass, so nothing releases the device before the full health
  check, fabric included, passes. The record is keyed by the episode, so a new episode resets again.
  A power cycle the rate limit denies, or one not opted in, buys no second mesh reset (I87): the hold
  waits for the cycle, or for an operator with none.
  Bounds the loop one Galaxy host ran: six identical mesh resets in one hold, each exiting 1 after a
  263 s overrun, one per forced window.
- **I37 — A whole-tray or all-off drop goes to the power cycle with no reset first (classes A
  and C).** An exception to I19, I22 and I27 for two off-bus classes no further reset has been seen
  to heal. Class A: the off-bus set is exactly one or more whole UBB trays (the I16 tray map places
  every chip) and at least one chip is still on the bus. A broker with no tray map (it never read a
  full mesh, as on a boot that came up short) reads class A from presence instead: the chips still
  on the bus, read live from sysfs and grouped by tt-smi's UBB bus groups (`bus & 0xF0`), fill whole
  trays, at least one tray has none, and those trays' chips are exactly the off-bus count, and the
  chip ids missing from that read are exactly the off-bus ids (a heartbeat read and a sysfs read that
  raced never pair the wrong chips); anything else, or no tt-smi tables, is not class A. A chip the
  heartbeat calls off the bus is not present even if the kernel still lists its node, nor is a node
  whose link reads down (speed `Unknown` or width 63, 03 I9), so neither fills a tray it left. When
  the set as read is not class A, its chips that are enumerated and bound to tt-kmd but whose
  heartbeat read fails with an error other than ENODATA (EIO, a missing attribute), and whose link
  does not read down (03 I9), are taken as on the bus, on the bus but unhealthy, and the set is
  classified again, for class A only
  (`class_a_unreadable_survivors`, naming them); every other answer, and every rung's own off-bus
  count, is the one the set as read gives. Without this, such survivors of a whole-tray drop make it
  read all-off (one mesh reset, then class C) or partial (the full ladder). With no map the below-floor
  branch runs the same presence read (EIO survivors included): a clean
  whole-tray drop goes to the power cycle as in I25,
  where before it declined for want of a map and held to the ceiling; a partial drop keeps the
  ladder. So a whole-tray drop at the startup gate or at the hold
  deadline requests the cycle at its first pass, not after the hold ceiling and a mesh reset; inside
  the I65 cooldown the denial puts the deadline on /health at that pass and the watchdog asks again
  then. Class C: every chip is off the bus after a
  mesh reset already ran and failed (nonzero exit or unhealthy verify) since the mesh was last
  released (or this boot, I58), or after a host reboot or power cycle the broker started for this
  fault: the boot it brought up, or one requested this boot since the hold episode began. A
  whole-bus drop with no failed reset gets its one mesh reset first, at the startup gate as at run
  time: the startup gate also runs after a broker restart, and one mesh reset has healed an all-off
  bus there and at run time. When the power cycle is opted in, `escalate()` requests it for these
  classes before any rung fires (after the I19 rescan and capture, which still run once per
  episode): no mesh reset, no SBR and no last-chance sweep, and the host rung's
  own sweep skips itself for these classes (`last_chance_sweep_skipped`). The request keeps every
  guard of the host rung: a reset cycling in
  its scope, a tenant or an unreadable holder scan, an operator stand-back marker or check, and the
  power-cycle rate limits (journaled as denied) each hold and fire nothing; the next window asks
  again. The governor counts each requested cycle once: once the gate's rung has requested it (or
  any host action is pending), the gate's own host-rung step that follows in the same pass asks the
  governor nothing, so the cycle under way is never counted against the I65 backstop as a fourth,
  paged or retried. The hold-deadline climb (`offbus-forced`) waives an unreadable scan,
  but never a readable tenant or a running job. A missing manual-recovery check does not hold it, as
  it does not hold the host rung this replaces. With no power cycle opted in, the ordinary ladder runs as before (I36 still stops a repeat
  of a failed reset). The host that comes back runs the startup gate, which waits for the hugepage
  pool and passes the full health check, fabric included, before any release (03 I4); a failed check
  keeps the hold and climbs. Partial drops (a single chip, or part of one tray) keep the full ladder.
  Evidence: no whole-tray drop healed without a power cycle (0 of 50, 0 of 23 and 0 of 90 on three
  Galaxy hosts); on all-off drops whose first mesh reset failed, each repeat on the unchanged set ran
  130-262 s and exited 1.
- **I38 — Once this hold episode's mesh reset has failed on an unchanged off-bus set, the host rung
  fires in the same pass: no hold-ceiling deferral and no last-chance sweep.** Every rung below the
  host rung (the per-chip bridge reset, the mesh reset) has then had
  its one try, and I36 forbids the mesh reset again on that set, which is the last step of the I22
  sweep. So the climb that follows the failed reset (or any later window's I36 skip) journals
  `host_rung_not_deferred` (reason `mesh_reset_failed_unchanged`) when the hold is younger than the
  ceiling, and `reset_rung_skipped_failed_unchanged` with `rung=last_chance_sweep` in place of the sweep,
  then fires the rung the chooser picks (the power cycle for a below-floor or non-zero-exit drop). The
  rate limits (the I65 cooldown included), the tenant scan and the reset-scope check still apply; a
  denied cycle is asked again the next forced window or at the cooldown's end, never skipped for good. With no host rung the hold goes to an operator
  (I36, I33). The power cycle releases nothing: the startup pass after the host returns runs the full
  health check, fabric included, behind the hugepage gate, and a failure keeps the hold. A set that
  shrank or changed climbs the same way (I87). Partial off-bus time to power cycle
  falls from the 600 s ceiling window plus a second reset and a sweep (924 s for one chip with a 132 s
  reset; 1186 s for four chips with 263 s resets) to the one-shot plus the one failed reset (about
  150 s and 280 s with the 10-20 s idle one-shot of spec 03 I23; 252 s and 383 s while a tenant holds
  the 120 s wait), when no cycle or boot fell within the last cooldown; within one, at the cooldown's end
  (I65). Bounds the idle 5-6 min one Galaxy host sat between a failed mesh reset and the ceiling
  window; a second mesh reset on the same chips after an exit 1 killed another host.
- **I39 — Retired: no progress retry.** A failed mesh reset that brought chips back no longer gets
  a second reset: the hold's one mesh reset is spent (I87), and the climb goes to the last-chance
  sweep, then the power cycle. `TT_DEVICE_MCP_PROGRESS_RETRY_SEC` is no longer read and the
  `progress_retry` trigger kind is gone. Time to recover, class B1 whose first reset fails with chips
  back: the failed reset, the sweep (SBR and mesh reset, once in this phase, I87), then the power
  cycle and the startup gate's full check, fabric included, unless the I65 cooldown holds it; before,
  a second reset 60 s later healed some episodes (about 8-10 min) and repeated resets on a changed set
  ran without bound on the rest. I81 and I78 now rescan an isolated chip back inside the first
  reset's driver wait, the case the retry was for.

- **I87 — One mesh reset per hold episode; the last-chance sweep at most once per phase.** Once a
  mesh reset (`tt-smi -glx_reset`) has run in a hold episode and left the mesh unhealthy, no path
  runs another standalone mesh reset in that episode, whatever the off-bus set did after it (shrank,
  changed or stayed): not the between-jobs gate, the idle relift, the stuck-hold escalation, the
  hold-deadline watchdog's forced windows, the I65 cooldown retry or a startup check. Each such skip
  journals `reset_rung_skipped_spent` and the episode climbs: the last-chance sweep (I22), then the
  power cycle (I71). The sweep (SBR, then the mesh reset, then one full check, fabric included) runs
  at most once per hold phase: once below the I76 10-min bound and once more as the bound's own
  ladder; a repeat in the same phase climbs instead. A sweep skipped because its mesh reset already
  failed on the same chips still runs its SBR when this hold has run none on them, below the galaxy
  floor only and never for a whole tray (A) or all chips off (C), which go straight to the power cycle
  (I89, I37). The only other mesh resets in an episode are the
  I11 ARC-not-ready retry inside the same reset call and I97's one reset of the whole mesh before the
  power cycle, once an SBR has every chip back on the bus. A power cycle the governor denies, the I65
  cooldown holds or that is not opted in buys no second reset: the hold waits for the cycle (asked
  again at its deadline), or goes to an operator (I33) when there is none. No flag, env var, config
  key or CLI option brings a repeat back: `TT_DEVICE_MCP_AUTO_UBB_RESET` and
  `TT_DEVICE_MCP_PROGRESS_RETRY_SEC` are no longer read. A new episode (after a verified release or a
  power cycle) gets its own one reset. Releases nothing: only a full health check, fabric included,
  that has just passed lifts the hold (I32). Time to recover: a fabric, dispatch or hold-deadline
  hold whose reset failed reaches the power cycle at once instead of after 8-10 min of repeated
  mesh resets (one replayed Galaxy episode: 1186 s before, 586 s now); a
  present-mesh wedge the reset does not heal: the sweep, then the cycle, then the full check; a
  whole-tray drop: unchanged here (I37, straight to the cycle at the cooldown's end). Most mesh
  resets one episode can run: the first reset, then the sweep's at most once per phase (one below the
  10-min bound, one more as the bound's ladder when the cycle is held off past it), plus the I11
  ARC-not-ready retry inside a reset call and I97's one reset of the whole mesh before the cycle. A spent mesh reset also lets the power cycle fire before
  the 10-min ceiling: the stuck-hold escalation journals `host_rung_not_deferred` (reason
  `mesh_reset_spent`), runs the sweep once if this phase has not, then cycles, instead of waiting
  out the ceiling with no rung left to try.

- **I81 — A mesh reset re-adds the chips the broker isolated at tt-smi's driver wait.**
  `tt-smi -glx_reset` re-powers every tray, counts a 30 s settle, then waits up to 100 s for every
  chip to show up in /dev/tenstorrent, and exits 1 with no re-init when one never does. A chip the
  broker isolated (its PCI endpoint removed) cannot show up on its own: only a PCI rescan adds it
  back, and the rescan `reset_with_quiesce` ends with runs after tt-smi has exited. So before I81
  every mesh reset over an isolated chip failed after about 263 s even when the re-power had healed
  the chip, and only a second reset (the retired I39 retry) finished the job. Now, when a `-glx_reset` runs with at
  least one isolated chip, `reset_with_quiesce` watches the reset's output and, at the first marker
  of that wait (`Waiting for 30 seconds: 30`, the settle's end, or `Waiting for all N chips to show
  up`, matched across split reads), runs one global PCI rescan, journaled as
  `reset_driver_wait_rescan` with `ok` and the chips. It fires once per reset (I78 repeats it, bounded, while an isolated chip is still missing, and I97 adds one SBR after four), never for
  another reset command or with no isolated chip, and a failed write, or one still blocked in the
  kernel after 30 s, is journaled and the reset runs on; the rescan after the reset still runs (also
  bounded at 30 s). Spec 03 I83's cut removes chips only after tt-smi has exited, never inside this
  wait, so it costs I81 no chip; a chip it removes is re-added by the closing rescan if it answers. A cancelled reset cancels a pending rescan and disarms the
  watcher, so output the backend still reads after the cancel starts none; a sysfs write already
  running in its worker thread cannot be stopped and finishes on its own. A cancel that lands after
  tt-smi exited, while the rescan's write finishes, still runs the reset's whole cleanup (the
  in-flight flag cleared, the rescan after the reset, returned chips dropped from the isolated set,
  the pollers back) and only then reaches the caller. A chip still dead stays off the bus, as before.
  The rescan comes at the start of the wait; an isolated chip whose link trains later in tt-smi's
  100 s wait is picked up by I78's repeat. Releases nothing: the full health check, fabric included, still
  decides (I32). TTR: class B1 with the chip isolated, from the hold, goes
  from about 505 s (one-shot 120 s, a failed 263 s reset, the 60 s retry wait, a 62 s healing reset)
  to about 182 s (one-shot 120 s and one 62 s reset), inside the 6 min budget; both figures leave out
  the full check before release (about 60 s, I78 gives the measured episode). Mass or whole-tray
  drops with isolated chips that a mesh reset heals save the same ~320 s. Paths with no isolated chip
  or no `-glx_reset` are unchanged.

- **I58 — A mesh reset that failed is remembered for the boot, across broker restarts.** Every mesh
  reset that launched and failed (an exit code was read: nonzero, or zero with an unhealthy verify)
  is recorded with its exit code, the off-bus set before and after, its hold episode and its time in
  `reset_record.json` in the health dir, written atomically (temp file, fsync, rename) before any
  next rung runs. The record is keyed by the kernel boot id: a broker restart loads it, a new boot
  (the one a power cycle or reboot brings) starts empty. A reset that never launched or was still
  cycling at its timeout (no exit code) is not recorded; the reset clock, stamped before launch,
  cannot tell. Once a mesh reset failed with the off-bus set unchanged, no path fires it again on
  that same set in this boot: not a gate (startup, post-job, relift), not the idle or hold-deadline
  ladder, not a new hold episode, and not the last-chance sweep, whose last step is that reset (it
  skips itself with `last_chance_sweep_skipped`, reason `mesh_reset_failed_unchanged`). The gate
  journals `reset_rung_skipped_failed_unchanged` and routes on as after a failed reset, to the host
  rung its chooser picks; with no host rung opted in, the ordinary ladder runs and I36's one latched
  operator hand-off ends it. A set that shrank or changed gets one reset again. An empty set (a
  present mesh) matches only within its own hold episode, as in I36. Class C of I37 reads the record
  too, so an all-off mesh whose reset failed before a broker restart goes to the power cycle with no
  second reset. Resets stay bounded at one failed mesh reset per distinct off-bus set per boot. A
  power cycle the rate limit denies holds and is asked again at the next window, within the limit's
  interval. Goal of 03 I4 holds: after the cycle the startup gate waits for the hugepage pool and
  passes the full health check, fabric included, before any release. Time to the power-cycle
  request, on the fake clock of the tests: class C after a failed reset and a broker restart, at
  the first gate after the restart instead of after a second reset (262 s); class B (part of a tray)
  after one failed reset instead of a reset and the sweep (585 s to 262 s).

- **I59 — The broker's own device work is never a tenant.** Every probe the broker spawns (fabric
  check, health probes, the full check's `tt-smi` snapshot, the eth self-test, aiclk ceiling) and every non-tenant holder the dead-chip rescue SIGKILLs is
  registered as broker-internal for 10 min, by pid and starttime (a pid gone from /proc matches by
  number; a member of a registered probe's process group matches too). Before a tenant check (the
  galaxy climb's `_tenant_blocker`, the gate's host rung) the holder scan drops those holders and the
  gaps they leave while exiting; any other gap still fails closed, and a holder in a broker job or
  exec scope is never dropped. Without this, a scan taken while the killed fabric check was still
  exiting read incomplete, the power cycle was denied as "a tenant holds the device" and the last-chance
  sweep was skipped: on one partial off-bus that held the device 8 extra minutes, to the next ceiling
  window. Time to recover for B1 (partial off-bus) and post-reset regressions: the next rung runs at
  the same tick instead of the next window.

- **I65 — The power cycle waits out a cooldown counted from the last cycle or boot of any cause,
  then fires.** No power cycle fires within the cooldown of the latest of: the broker's own last
  reboot or power cycle (the I9 ledger), the host's current boot (whatever caused it: a manual, BMC,
  datacenter or external cycle shows here), and the last record of an out-of-band power-cycle log:
  `TT_DEVICE_MCP_POWER_CYCLE_HISTORY_FILE` when set, else `power_cycle_history.jsonl` in the broker's
  state dir (the parent of the health dir; `/var/lib/tt-device-broker` on a system broker). JSON lines
  with `at_epoch` and an optional `cause`; a missing or unreadable file adds no anchor, and a malformed
  line is skipped with one warning. A stamp later than now (a clock
  that disagrees), non-finite or unreadable anchors nothing and never hides an earlier valid one:
  each source's anchor is its latest valid past record, so no stamp postpones a cycle past the stated
  gaps or skips the cooldown. A power-cycle record stamped later than now does not count toward the
  backoff streak, and a heal stamped later than now reads as no heal (the longer, capped gaps). The
  cooldown is the power cycle's own interval (`POWER_CYCLE_MIN_INTERVAL_SEC`, 1800 s),
  the governor's one check for this rung, not a second gate. After two consecutive broker power
  cycles with no verified release between them it doubles for each further one, capped at 4 h:
  30 min, 30 min, 1 h, 2 h, then 4 h. A verified release (the full health check, fabric included,
  with no I32 debt) ends the streak (`power_cycle_healed.json` in the health dir). The streak starts
  at the later of that heal file and the last verified release (I9, `auto_recovery_verified.json`),
  heal file or not: a cycle a verified release on a later boot followed healed the box and never
  counts (a release written before the heal file existed left none). While the cooldown
  runs the device stays held, the journal says `power cycle in cooldown until <time>, <n>s more
  (last cycle/boot <time>, <cause>; <n>s cooldown) — this is the boot-loop guard`, and `/health` carries `power_cycle_cooldown` with
  `escalation_stage` `power-cycle`, `next_deadline`, the anchor, its cause and the interval. No other
  rung runs in its place: classes A and C stay on the I37 path. At the deadline the hold-deadline
  watchdog asks once (a forced escalation in the same hold episode), and the cycle fires then if the
  fault persists, behind I37's stand-back, tenant and scope guards. Every governor denial names its
  end: the per-boot cap counts only records within the interval and never a missed request (I72),
  which is asked again as its window ends (later misses 5 min after) rather than never. There an unreadable
  stamp counts as the boot's start (a fixed end, not now), a stamp later than now does not count for
  the power cycle, and with the boot's start known an unreadable power-cycle record never refuses the
  rung for good. A cap or backstop (I9) denial of the power cycle shows on `/health` like the
  cooldown's, and is asked again at its end the same way. An anchor a verified-healthy release (I9,
  `auto_recovery_verified.json`) followed anchors nothing: a broker cycle (or an older broker's reboot) verified on a
  later boot, the current boot once it is verified, an external cycle before that verification. A new
  drop after a healed cycle is a new episode and cycles at once, within the I9 backstop; a missing,
  non-finite or future verification stamp proves nothing and every anchor stays. At most one cycle
  fires per cooldown, and no hold waits on it without a stated time. Goal of 03 I4 holds after each
  cycle: the startup gate waits for the hugepage pool and passes the full check, fabric included,
  before any release; a failed check keeps the hold and the streak. Any boot in the episode anchors the
  cooldown from that boot; the rung asked at the cooldown's end is the power cycle (I10). Time to the power-cycle request
  (the boot and the startup check follow it): class A with no cycle or boot in the last 30 min, at
  the first gate or idle pass (0 s on the tests' fake clock); class A within the cooldown, at its end
  (at most 30 min after the anchor; a boot 3 min before startup gives 27 min); class C, after its one
  mesh reset (262 s) or with none after the broker's own cycle, then as class A. A fault no cycle
  heals is cycled 30 min, 30 min, 1 h, 2 h, then every 4 h after each boot: at most 6 cycles a day,
  never a boot loop, never an open-ended hold. Class A follows the same spacing even when its cycles
  do not heal it and AC removal may be needed (I73): that state pages an operator but stops no cycle,
  and the full ladder runs in the 10-min hold bound's run (I76), once per cycle or boot inside the
  cooldown. Class A or C after a
  cycle a later boot verified, at
  once (0 s), for at most `POWER_CYCLE_BACKSTOP_MAX` cycles in 2 h; past that, when the oldest leaves
  the window (at most 2 h).
  For one hold episode the power-cycle deadline set at the first cooldown denial never moves later (beyond the stated post-boot settle).
  The first denial writes that deadline (`power_cycle_deadline.json` in the health dir: `until`,
  `set_at`, the anchor, its cause and the interval), so it outlives a broker restart and a host boot;
  the hold episode the server tracks does not cross a boot, this record does. A later boot of any
  cause (a warm or operator reboot, an external cycle) only raises the deadline to that boot plus
  `POWER_CYCLE_POST_BOOT_SETTLE_SEC` (300 s: the startup gate's own bound, the 120 s hugepage wait
  plus the 180 s fabric pass), and only when that is later; it never restarts the interval. An
  earlier plain anchor still wins. The journal adds `fixed for this hold at <time>` (and the boot plus
  settle when that applies); `/health` shows the deadline as `next_deadline` and the record as
  `fixed_at`. The episode ends at a verified release with no I32 debt (the record is removed; a
  release refused for fabric debt keeps it and the hold), at the broker's next power cycle (that
  cycle's boot starts a fresh interval with the streak, the boot-loop guard above), or 4 h past the
  deadline (stale). A record that is unreadable, non-finite, set later than now or incomplete is
  ignored; one set later than now (the clock went back) is also kept as it is, never rewritten with
  a later deadline. A broker cycle row whose stamp cannot be read ends the episode (the longer wait)
  when it is new: the record keeps how many such rows the ledger held when it was fixed. The
  settle is a wait on the cycle only: no reset or release happens in it, and 03 I4's startup gate
  still holds the device until its full check passes. Time to recover, classes A and B1 held across
  reboots: before, each boot inside the hold restarted the full interval (up to 4 h at the cap), so
  repeated reboots deferred the cycle without bound (one incident sat about 640 min); after, the
  cycle fires at the deadline fixed at the first denial (at most one interval after the anchor), or
  at most 300 s after the last boot when that boot came at or after it.
- **I69 — A chip the broker removed is reset before anything re-enumerates it.** Isolating a dead
  chip (sysfs `remove`) records its parent bridge first, because the remove takes the node and its
  parent link with it. The records are also kept in the health dir (`removed_chips.json`) with the
  kernel boot id, and reloaded at broker start, where each reloaded chip is isolated again (sysfs
  gives it no node, so no scan would): a broker restart does not make a removed, un-reset chip look
  like one the broker never touched, and the bridge rung targets it. Every bridge-rung pass also
  targets every recorded chip, whichever chips it was asked for, and the bare rescan is withheld
  while any record, targeted or not, was not reset this pass: every rescan is global. A record from
  another boot is dropped at start (the boot reset every endpoint and may renumber the bus); a start
  that drops no record leaves the file as it was. A record clears whenever any device is enumerated
  again at its recorded address, whatever brought it back (the bridge rung, a mesh reset, a tray
  re-power, a power cycle); a node reusing the chip's index alone does not clear it. It is checked
  on every read, so a stale record can neither withhold a later rescan nor aim a bridge reset at
  live silicon. Clearing a record releases nothing: the returned chip still passes the full check,
  fabric included, before any release (03 I4). The bridge rung resets
  such a chip through that recorded bridge, only while the bridge is still enumerated, its
  secondary bus is the chip's bus and nothing is enumerated on that bus (a stale record is refused
  and the chip holds). Every removed chip of the pass is reset (owing the AICLK ceiling, 03 I35),
  then one 1 s link-retrain settle follows the last reset's release, then the pass's first rescan.
  No PCI rescan that resets nothing runs while a still-dead chip the broker removed was not reset
  (no usable bridge, no root/setpci, a failed write, or the per-host reset gate (I24) refused the
  reset): the bridge rung and the tray-down prelude (I37) journal `pci_rescan_withheld` with the
  reason (`removed_chip_not_reset`, or `host_reset_gated` when the gate's refusal left it un-reset),
  the chips stay isolated and the hold goes on at once to the next rung, with no added wait. A
  rescan re-enumerates a hung, un-reset chip and lets the driver probe it, the step that preceded
  host deaths on two Galaxy hosts. A pass with no removed, un-reset chip keeps the bare 3 s rescan,
  gate refusal or not: it recovers a chip whose /dev node vanished without a broker remove. A mesh
  reset reaches a removed chip only through its BMC tray re-power on op_mode 0x3, so its rescans follow
  that re-power: the driver-wait rescan (I81, I78) starts at tt-smi's wait for every chip only when the
  output so far shows no 0x0 fallback, guard refusal or unarmed guard, and the rescan after the reset
  runs when tt-smi exited 0 or its output shows the re-power went out (its 30 s settle count or its
  wait for every chip, and no fallback, refusal or unarmed guard). Otherwise, while the broker has a
  removed chip, both are withheld: `pci_rescan_withheld` (reason `mesh_reset_failed`), the chips stay
  isolated and the failed reset sends the hold on to its next rung at once. With no removed chip the
  rescan after any reset stays. Time to recover, per class: no change on a mesh reset that re-powered
  the trays on op_mode 0x3. B1, B2 and D with a removed chip, when the BMC step failed, the guard
  refused the 0x0 fallback or the guard was not armed: the next rung comes 3 s sooner (no rescan and
  settle). B1 and B2 with a removed chip, when tt-smi fell back to op_mode 0x0 and the guard let it
  through: the next rung comes up to tt-smi's driver wait (about 100 s) later than before. The
  withheld driver-wait rescan no longer re-adds the chip mid-wait, so tt-smi waits out its whole
  driver wait before it exits 1 instead of exiting early; a 0x0 retimer reset never reached the
  removed chip, and re-adding it un-reset is what I69 forbids. Recovered chips pass the full check,
  fabric included, before release (03 I4).
  Time to recover, removed all-ones chip: 0.1 s SBR + 1 s settle + rescan + up to 10 s for the
  driver to re-bind when the first try lands (formerly a 3 s rescan with no reset); at most 3 tries
  with a 4 s settle between and the 3 s closing rescan, 44.3 s of waits, when it never returns. A
  removed chip that cannot be reset holds and reaches the next rung 3 s sooner; a tray-down onset
  with a removed chip skips the prelude's rescan and its 3 s settle. With N removed chips that never
  return, the first pass asserts every SBR (0.1 s each) before one 1 s settle and one 10 s re-bind
  poll, the retries then run serially per chip (30.2 s each), then the closing 3 s rescan:
  14 + 30.3 N s of waits (44.3 s for one chip, 74.6 s for two). A removed chip whose recorded-bridge
  resets all fired and left it off the bus reports `removed_not_reenumerated`, which
  `_classify_hold` reads as no window, like `no_bridge`: a whole-tray drop of removed chips keeps
  its off-bus class (tray down, no window) and its budget instead of the generic one. A removed
  chip after a broker restart (class B1, partial off-bus) gets this recorded-bridge rung, at most
  14 + 30.3 N s of waits, instead of the full ladder. Other classes are unchanged; the I76 10-min bound (full ladder, then a power cycle) holds as
  before.
- **I71 — The power cycle is the only host rung.** `_host_escalation_for_drop` names the BMC power
  cycle for every climb that reaches the host rung when `_auto_power_cycle_enabled` (I14): a chip
  off the bus (`off_bus > 0`: classes B1, B2), a whole-tray or all-off drop (class A, I37), a present
  mesh (`off_bus == 0`: a wedge, class D, or a fabric or dispatch fault the ladder could not clear,
  G2), an unkillable leftover (I70) and a single-chip host. The warm host reboot rung is gone: it
  cannot re-power a UBB or re-enumerate a chip that left the bus (a whole-bus drop stayed 32/32 off
  after it), and the power cycle does all it did. No flag, env var, config key or CLI option brings
  it back (I10). The ladder below the host rung is unchanged: a partial drop still runs the bridge
  and mesh resets first and a present-mesh wedge its one `glx_reset` (I87) before the host rung;
  only a whole-tray or all-off drop goes straight to the cycle (I37). A power cycle that the governor
  or the I65 cooldown denies holds and is asked again at the deadline `/health` shows; one that does
  not launch (unreachable BMC, failing command) keeps the hold and is asked again on every pass: the
  I76 bound leaves the rung pending, so the watchdog asks each tick, unpaced, and the cycle fires on
  the first pass the BMC answers (a failed launch costs one `ipmitool` call, at most 30 s; a timed-out
  one stays pending under I72). Every failure is journaled (`power_cycle_launch_failed_holding`, with
  `paged`); the page goes out once per hold episode and again at most every `CYCLE_LAUNCH_REPAGE_SEC`
  (3600 s) while it keeps failing, never once per tick. When the cycle could not start with the OS
  brought down first, the I88 `power_cycle_not_launched` page is that episode's one page, and only
  the re-page follows. Neither path reboots. The hold is bounded by
  the BMC answering; the cycle itself and its time to recover are unchanged. With no power cycle armed the hold goes to an
  operator (I33), paged, still held. `_route` returns BLOCKED only when no reset scope is live; under
  a live scope it waits. The full health check, fabric included, runs after the cycle before any
  release (03 I4). The I76 10-min hold bound is unchanged. Time to recover, armed multi-chip host:
  D and G2 cycle right after the ladder (about 5-8 min with the boot) instead of a reboot (3-5 min)
  and then the cycle after the reboot's boot and the I65 cooldown (30 min to 4 h); a partial drop
  unchanged (it already cycled); a denied cycle holds to its stated deadline instead of rebooting; a
  cycle that cannot launch holds and pages instead of one fallback reboot an hour; class A and C
  unchanged; an unkillable leftover (I70) with every chip present cycles at the end of its budget
  (about 5-8 min with the boot) where it rebooted (3-5 min), about 2-3 min more, inside its 23 min
  bound. Hosts with no power cycle armed (no root or `ipmitool`, or `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0`):
  every class that used to end in a reboot now holds and pages (`hold_bound_no_host_rung`, the
  `all_chips_off_bus_power_cycle_required` and `reset_unrecoverable_power_cycle_required` events,
  I70's `no_host_rung` stage) until an operator acts. That is the intended effect of the removal:
  the reboot could not re-power a UBB or bring back an off-bus chip (above), and a hold that pages
  replaces a reboot loop.

- **I75 — The next rung `/health` names is the earliest one some code will fire.** One helper
  (`health/hold_report.py`, used by spec 03 I72) reads the hold-deadline watchdog's clocks, takes the
  earliest armed deadline that acts, and reports its rung, time and source. What a forced run does
  with a deadline comes from one pure routing function, `_forced_run_route(kind, age, mono)`, which
  `_force_escalate_stuck_hold` itself calls and the report evaluates at each candidate's time; the
  report never keeps its own copy of the forced run's guards:
  - with no hold-episode clock (the one the watchdog keys on), no forced candidate: the watchdog
    fires none, so only a registered feed may name one;
  - with the forced escalation off or a fabric-77-only hold, every forced candidate below fires no
    earlier than the 10-min bound (hold start plus the ceiling): below it the watchdog forces no
    77-only hold and the kill-switch declines every forced spawn; from the bound on neither holds a
    rung back (I76);
  - while a power cycle the broker requested is still pending, that request: every
    forced spawn declines until the pending window ends, so the time is the window's end (source
    `host_action_pending`);
  - otherwise the earliest of: the off-bus forced pass at the bound
    `offbus_power_cycle.watchdog_due_at` reports for this episode (the gated off-bus grace, or with no power-cycle command the
    I33 operator hand-off deadline; each named in the source); the I65 retry of a power cycle the
    governor refused, only while that retry is armed for this hold episode (`power cycle`, source
    `power_cycle_cooldown`, or `power_cycle_missed_retry` / `ac_power_needed` when the refusal
    record says so); the I76 host rung (`power cycle`; source `hold bound host rung`) once the bound's ladder ran and no host rung was asked since,
    at once when the full check after that ladder failed or the rung is still pending, else at the
    end of that check's bound; once it is due the watchdog's branch chain stops there, so nothing
    below is a candidate; then, in the watchdog's own branch order, the off-bus one-shot (hold start
    plus the one-shot wait, while chips are off the bus and the episode's one-shot has not run), and the next unspent ceiling window
    (`device_hold_escalate_bucket + 1`, at least 1, at hold start plus that many ceilings). A branch
    stops the chain only once it is due (the one-shot's wait has passed, the retry's time has come),
    as the watchdog's own guard does; one not yet due is reported beside the branches after it, and
    the earliest wins, so an armed retry never hides a ceiling window that fires first;
  - the I70 host rung for an unkillable leftover (`power cycle`, source
    `unkillable leftover budget`): at the end of its budget, then one retry interval after its last
    try; none once its episode closed or with no host rung opted in. It is the sampler's own rung,
    not a forced spawn, so the 10-min floor above does not move it;
  - every registered deadline feed.
  A power-cycle candidate (any source above) the governor would still refuse at its time is
  reported at the end of the latest governor wait instead: the power-cycle backstop and the boot /
  post-power-cycle cooldown (I65), read through `RecoveryMechanism.power_cycle_gates`, which marks,
  journals and pages nothing; the source then names each wait (`<source>, held by the power-cycle
  backstop until <t>, then the power-cycle cooldown (<cause>) until <t>`). A ladder or check
  candidate keeps its time, and the governor still decides at the rung. `hold_escalation`'s
  `next_deadline`, built from the same next rung, moves with it.
  Each forced candidate above (the off-bus forced pass, the I65 retry, the I76
  host rung, the one-shot, the ceiling windows) is routed at its time: a
  startup-gate stand-down (spec 03 I75: a ceiling window or one-shot while the startup gate owns the
  hold) is reported as `startup health check, then <rung>` at the stand-down's bounded end (the
  re-adopted jobs' end plus `STARTUP_VERIFY_DEFER_MAX_SEC`), source `startup gate stand-down
  (<source>)`; a run that returns while the bound ladder's post-ladder check is pending is no
  candidate, so the next window or the host rung at that check's end is reported instead; a run
  routed to the I76 host rung reports `power cycle`, source `hold bound host
  rung at <source>`, or nothing when no host rung is opted in; a ceiling window that does not act
  hands over to the next one. The pending host request, the feeds and the I70 leftover rung are not
  forced runs and are not routed.
  A deadline that has passed but whose spawn was declined (a device op or an escalation in flight)
  is reported at its own time with `next_rung_due: true`, never moved to a later window: the
  watchdog retries it every tick. With no armed deadline the fields are `null`. A feed that raises
  reports nothing, so `/health` always answers. The helper never claims, moves or expires a clock (it
  reads a pending host request without `host_action_pending()`, which drops an expired one). The
  broker registers the I76 hold bound under `hold_bound` through `register_deadline_feed`; it stands
  in for ceiling window 1 and, being the watchdog's last branch, loses a tie at the same time to any
  other candidate. Its candidate (`hold_bound_candidate`) is the bound's mark, hold start plus the
  ceiling (600 s), source `hold bound (<ceiling>s)`, rung `full ladder, then power cycle` (`full ladder` with the power cycle not opted in). Inside a
  boot/post-cycle cooldown the mark does not move, because the watchdog runs the bound's ladder there;
  only the host rung waits, so the rung reads `... at the cooldown's end (<time>)`. The feed is silent,
  and the chain names what fires next, once window 1 is spent or past due, once the bound's ladder ran,
  its host rung was asked or is pending, or a tenant blocks it; while a requested host action is
  pending; and when the forced run's routing sends the mark anywhere but the ladder (the startup
  gate's stand-down). It reads the bound's state and claims, moves or expires no clock. TTR effect 0
  for every class (A, B1, B2, C, D, AC-needed): it only reports; when the ladder and the host rung
  fire, and the full health check (fabric included) every lift needs, are unchanged.
- **I70 — An unkillable leftover fences the device for at most 600 s; then the broker takes the
  host rung (fault class `J-unkillable-leftover`).** The class: a reaped job's leftover (01 I16)
  stuck in the kernel (state D, or SIGKILL pending) still holds `/dev/tenstorrent`. No reset can
  heal it: I21 refuses every reset over it, and a reset would not free the process anyway; only a
  host reboot or power cycle ends it. Its clock starts at the first scan that finds such a holder (the dispatch
  and reset-fence scans, and the idle-holder scan while idle) and stops at the first scan that finds none (the episode closes, the next one starts afresh; a scan
  error neither starts nor stops it). The budget is `LEFTOVER_UNKILLABLE_BUDGET_SEC`, 600 s, a
  constant: the runner has already spent its kill ladder (SIGINT 60 s, SIGTERM 15 s, SIGKILL) before
  the fence opens; a kernel wait that does finish (a driver release unpinning its buffers, a slow
  writeback) ends in seconds to about two minutes, and the kernel reports a task blocked past 120 s
  as hung; 600 s is five such periods and the same as the stuck-hold ceiling, while the motivating
  incident sat about an hour without clearing. Past the budget the idle sampler asks for the rung at
  most once per `LEFTOVER_HOST_RUNG_RETRY_SEC` (60 s); each try first re-scans, and a leftover gone
  by then fires nothing. The rung is the power cycle, the only host rung (I71), which ends every process. No reset or sweep
  runs first (an exception to I22). Guards kept: a host action already pending (I34)
  asks nothing more (re-read under the device lock, so a try spawned before another path's request,
  such as the I76 bound's host rung, asks nothing: one power-cycle request per episode even when
  that launch timed out and retracted its governor record), a reset cycling in its scope, a running broker job or a readable tenant other
  than the leftovers themselves (they are discounted; an unreadable scan is waived as on the
  forced climb), the operator stand-back for the power cycle, and the governor (I9/I10 rate limits,
  the I65 cooldown). After the stand-back and the governor (the stand-back command can run for its
  whole timeout) the rung scans again and fires only if a reset scope still does not cycle, no
  tenant arrived and a leftover is still in that scan (a dead chip's stuck holder, I77, counts while
  its own fresh check still finds it stuck: it may map only the removed chip's BAR or node, which the
  holder scan does not list). A leftover that left at any point before the
  fire (between the try's first scan and the device lock, or during the last checks) fires nothing:
  the stage is `gone` and the try's own re-scan closes the episode. A guard holds and fires nothing; the next try asks again, so a denial ends when
  its cause does (the cooldown at its stated end). With no host rung opted in the episode goes to an
  operator, journaled once. Journal: `leftover_unkillable_deadline` once per episode at the budget,
  then `leftover_host_rung` each time its stage changes (`requested` with the action, `held` with
  why, `gone`, `no_host_rung`). While an unkillable leftover stands `/health` carries `leftover_fence`:
  `fault_class`, `since`, `budget_sec`, `deadline`, `escalation_stage` (`waiting-budget`,
  `requested`, `held`, `gone`, `no-host-rung`), `detail` and the leftovers; `null` otherwise. The rung
  releases nothing: the boot that follows runs the startup gate, which waits for the 1G hugepage
  pool and passes the full health check, fabric included, before the queue is released (03 I4); a
  failed check keeps the hold and climbs the bounded ladder. The clock lives in memory: a broker
  restart starts it again at its first scan (one more budget at most). Time to recover for this
  class: before, unbounded (until an operator rebooted the host; about an hour in the incident);
  after, up to `IDLE_HOLDER_SCAN_SEC` (60 s) while idle before the first scan opens the clock, 600 s
  plus at most one 60 s retry tick to the request, then the power cycle (about 5-8 min) and the startup check (about 3 min): about 19-23 min. Later only while the governor holds the rung, each bounded and
  shown as `held` on `/health`: the I65 cooldown when a cycle or boot fell within its last 30 min.
  With no power cycle opted in the episode goes to an operator (journal stage `no_host_rung`, held
  and paged). The power cycle replaced the removed warm reboot as this class's rung (I71): with every
  chip present that costs about 2-3 min more (the cycle's 5-8 min against the reboot's 3-5 min),
  inside the 23 min bound above; a host with no power cycle armed now holds where the reboot used to
  clear the leftover. The `gone` stage and the second scan add no wait.

- **I78 — The I81 driver-wait rescan repeats, bounded, until every isolated chip is back.** I81's
  one rescan comes at the start of tt-smi's 100 s driver wait; a chip whose link trains later in that
  wait would still miss the reset, which then exits 1 and spends the episode's one reset (I87). So after the
  I81 rescan, while an isolated chip is still missing, `reset_with_quiesce` drops each returned chip
  from the isolated set (journaled `isolated_chips_returned`, context `mesh reset driver wait`) and
  rescans again every `DRIVER_WAIT_RESCAN_EVERY_SEC` (5 s), at most `DRIVER_WAIT_RESCAN_TRIES` (20)
  rescans in all, which span the 100 s wait. After `DRIVER_WAIT_SBR_AFTER_TRIES` (4) rescans that left
  a chip missing, one SBR of its recorded bridge comes first and the rescans go on (I97). It stops as
  soon as no chip is isolated, the reset ends
  (its cleanup sets a stop event, so the reset never waits on a repeat), the reset is cancelled, or
  the tries are spent, journaled `reset_driver_wait_rescan_spent` with the tries and the chips still
  missing. The trigger is I81's: the settle's last count, never its first. tt-smi prints the first
  count as soon as the IPMI re-power returns, and a rescan then can enumerate a chip whose firmware
  is not up yet. Both reset backends deliver the last count only once the settle is over: the scoped
  backend reads by line, and tt-smi's countdown ends its line only after the last count; the local
  backend polls the output file and sees the last count when it is written. Releases nothing: the
  full health check, fabric included, still decides (I32), and a failed reset keeps I36, I38, I87 and
  the 10 min ladder bound, then the power cycle (I76). Time to recover, class B1 (one chip off the
  bus, isolated, healed by a mesh reset). Measured before, one episode, onset to release: about
  575 s (about 123 s to the first reset, a failed 263 s mesh reset, a 65 s wait for the second reset, a
  62 s healing reset, about 60 s of full check with fabric before release). With I81 and I78, est.:
  about 245 s (123 s, one 62 s healing reset, 60 s check); a chip that trains late in the wait adds
  at most the rest of the 100 s wait instead of a failed reset and a retry. Worst case, a chip that
  never comes back: the failed 263 s reset, then the sweep and the power cycle (I87), inside the 10 min ladder bound and
  then the power cycle; the repeat adds no time to it, since it ends with the reset.
- **I79 — A hold whose reset and full check already failed climbs at once, not at the ceiling.**
  The stuck-hold ceiling (600 s) bounds how long the host rungs wait for the gentler rungs to have
  their try; it never adds a wait once they had it. Three cases climb as soon as their last gentler
  rung has failed, at any hold age:
  - **A 77 hold that owes its fabric pass** (class D after a reset): the episode's one reset came back
    UNKNOWN (I29) and the full last-chance sweep, every reset type once and then the full check with
    fabric, still failed. The I40 bound counts as reached on the first re-check and the I48 backoff
    uses its first step only (120 s), so the sweep runs on the first re-check after the reset and the
    host rung fires right after it. The `host_rung_not_deferred` event records
    `reason=reset_and_sweep_failed`.
  - **A present mesh whose galaxy reset failed its full check** (an eth/fabric wedge, classes D and J):
    the climb still runs the last-chance sweep first (I22), then fires the host rung without the
    ceiling wait (`reason=present_mesh_reset_failed`). Below the bound the sweep's mesh reset first
    waits out I93's gap after the failed first reset (at most 200 s, never past the 600 s bound).
  - **An off-bus hold whose reset regressed the mesh** (class B1, I31): a reset on the gate or the
    idle ladder left more chips off the bus than before it. I31 bars the bridge and mesh reset
    rungs for the rest of the episode, so the ceiling wait has no rung left to try. The climb runs
    the last-chance sweep first (I22), then the power cycle, without the ceiling
    wait (`reason=reset_regressed_offbus`). A cold rung `auto_recovery_allowed` holds off (the I65
    cooldown, a tenant, the rate limit) returns before the sweep, as at the ceiling, and is asked
    again on the next forced window; the I76 bound still runs the full ladder at 10 min. With no
    power cycle opted in it pages (I31) at once rather than at the ceiling. A sweep whose full check
    passes, fabric included, releases the device (I32); the boot after the power cycle runs the
    startup gate's full check (03 I4). Time to recover: one 8-chip hold whose post-job reset dropped
    one chip waited 512 s for the ceiling, swept for 110 s, power-cycled and was released at 904 s;
    the same episode now sweeps at about 98 s, cycles at about 210 s and is released at about 390 s.
    On a Galaxy, a healing mesh reset followed by a second chip's drop now sweeps at the drop
    instead of up to one forced window (80 s measured) later.
  - **The boot an I29 77 power cycle brought up** (with no fabric pass since the broker started) and a
    power cycle opted in: the 77 is not handed to an operator. The episode owes the climb at once. The
    earlier episode's reset counts as its one reset, the sweep runs on the first re-check, and the
    climb takes the power cycle again, once; a cycle the cooldown or the governor denies holds and is
    asked again (I71). The I40 fallback marks the episode's reset
    as spent before it escalates, so the ladder sweeps rather than resetting again. The boot that power
    cycle brought up is spent: the hold goes to an operator with a deadline (I29), but the operator
    hand-off does not stop the I76 bound: at 600 s the bound's ladder runs and asks for the next
    power cycle, or at the end of the I65 doubling cooldown when that is later (I76 deadline
    helper). A cold rung that
    `auto_recovery_allowed` holds off (the I65 cooldown, the rate limit, a tenant) is not spent: it is
    retried on every 120 s re-check and fires once allowed, at most once per episode. A denial that is
    the bounded I65 cooldown (its `power_cycle_cooldown_state`, cap or backstop included, with an
    `until` still ahead) pages no one and opens no hand-off: the broker journals
    `fabric_unverified_host_rung_cooldown` once per episode and deadline, and `/health`
    `power_cycle_cooldown.next_deadline` (and `hold_bound.cooldown_until`) shows when the cycle is
    asked again. Once the episode's reset and sweep have run, the hold-deadline watchdog's I65
    cooldown retry asks for it the moment the cooldown ends, below the bound too; otherwise it fires
    on the first re-check after the cooldown. Whichever of the relift re-check and the watchdog retry
    runs first fires the one request, and the other finds the host action pending (I34). Any other
    denial (a tenant, the rate limit with no cooldown deadline) pages once; the page and `/health`
    `operator_handoff` name the reason, say the broker retries, and read `owner=broker`; the operator
    deadline still stands in case it never can.
  A first-time 77 (no reset yet on the episode, and not a 77-rung boot) keeps the I40 retries and the
  I48 backoff unchanged. Nothing here releases a device: only a full check that passes, fabric
  included, after the last reset releases it (03 I32, goal of I22). Time to recover, from the hold
  opening after the UNKNOWN reset: the host rung fires at about 8 min (120 s re-check plus a sweep of
  about 5 to 6.5 min), where it used to fire at about 18.5 min (the 600 s ceiling, reached on the
  next 600 s backoff step). The boot after that cycle asks for the next power cycle about 8 min after its hold
  opens, or, if the I65 cooldown ends later, at the cooldown's end (watchdog retry); not paging
  for that cooldown changes neither time. A 77 on the boot a 77 power cycle brought up gets its next
  cycle at 600 s or the doubling cooldown's end, whichever is later. A present mesh's failed reset climbs about
  8 min earlier than before; the hold age no longer matters.

- **I76 — No hold goes past 10 min without the full ladder, then the power cycle.** When an open
  hold episode reaches the stuck-hold ceiling (`TT_DEVICE_MCP_STUCK_HOLD_SEC`, 600 s; a larger value
  reads as 600, so the env may shorten the bound, never lengthen it), the hold-deadline watchdog
  forces the broker's own full reset ladder, whatever the hold's class or latch: a 77-only hold
  (I29, spec 03 I40), a fabric-only present mesh (I30), a topology-mismatch hardware fault (spec 03
  I42, including the latch the first job after a topology-mismatch lift sets when it fails on the
  fabric; that disproven lift's bound is one sampler tick, not 600 s, and the kill switch does not
  stop it; the forced-run routing and the I75 next rung key on that same bound, so the host rung
  its early ladder owes runs no second ladder first and `/health` names the ladder one tick in, then
  that host rung) or a known fabric fault (spec 03 I46). This holds whichever trigger starts the forced run
  past the bound: the watchdog's ceiling window or a held job's tenant-hold bound; neither returns
  at the 77-only early exit any more. The exits that end a hold with no rung below the bound, and
  that the bound overrides: the watchdog's 77-only early return; the I30 no-reset hold
  (`_hold_without_reset`: a topology mismatch or missing cable, a known fabric fault, a 77 with no
  fault measured), which still pages and then runs the ladder; the I29 77 hold with no host rung
  after the episode's reset and last-chance sweep, which the bound's ladder reaches and follows with
  the host rung in the same run (the reset and sweep it already ran are not repeated, I36); the I46
  latch; and a killed job's leftover holder (spec 01 I16). That leftover read as a tenant,
  so the ladder stood down and re-armed every window; from the bound on it is no tenant. Its fence
  keeps every reset off the device and only a host rung clears it, so the bound asks for the host
  rung at once (event `hold_bound_leftover`); a running job or any other readable tenant still
  refuses it. A holder the spec 03 I84 dead-chip valve refused to kill (a failed or missing PCI
  remove, so its fd close would reach an attached chip off the bus) is no tenant at the bound
  either: the valve records the episode's refused pids (pid and start time; a recycled pid does
  not match) and the running job it refused, a passing full check clears the record, and at the
  bound the broker skips the ladder (its reset would kill those holders) and asks for the host rung
  at once (event `hold_bound_kill_refused`), killing nobody and never rebooting. Before, the
  running job never timed out with no waiter and the bound only paged `hold_bound_tenant_blocked`,
  so that hold had no bound. A process the valve did not refuse still blocks the bound. Time to
  recover, class refused dead-chip kill: the 10-min mark plus the power cycle (no bound before);
  every other class unchanged. At the bound the I46 latch is lifted
  (`hold_bound_latch_cleared`), so the full health check after the ladder, fabric included, gets a
  real verdict and only a pass releases (goal of I8/spec 03 I4, I78); a job that fails the same way
  declares it again. The spec 03 I42 latch stays: it withholds no check (a latched hold already takes
  the ladder), it makes a topology mismatch FAIL the check, so the post-ladder check cannot pass on
  the links the lift's first job failed on. A post-ladder check that finds any descriptor port
  missing (spec 03 I71) or, under the I42 latch, any mismatch is a failed check: no release, the host
  rung is asked for (subject to the I65 cooldown). Below the bound the faster class paths stay as they are; a forced run that
  started below the bound (the off-bus one-shot, a gated grace) is not the bound's
  ladder, so at the bound the full ladder runs again before any host rung. When the ladder
  leaves the mesh unrecovered, the broker's own code asks for the host rung at once: the BMC power
  cycle (I71). When the ladder brings the mesh back, only its
  full check, fabric included, releases the hold. A full check that ends with no pass (a failed
  verdict, or a fabric pass that could not run for any cause but a 1G hugepage pool still filling)
  asks for the host rung at once (event `hold_bound_check_failed`): inside the forced run when the
  ladder's own check gave it, on the next sampler tick when a relift check did (any idle lift's
  `_full_check_gate`: eth_frozen, off-bus, foreign holder, DOWN off-bus; spec 03 I78). 300 s
  (`_HOLD_BOUND_CHECK_SEC`) bounds only a check that is still running, hangs or waits on hugepages;
  no other run in that window starts a ladder or a host rung over it, and if the hold still stands
  at its end the watchdog asks for the host rung (`hold_bound_cycle`). A later bound window whose last window already ran a
  ladder goes straight to the host rung. The governor still decides that rung. A reset still
  cycling in its own scope defers the bound's ladder or host rung only until that scope ends
  (bounded by the scope's own limit): the rung stays pending and the first sampler tick after the
  scope asks again, never a whole window later. An off-bus one-shot a scope defers gives back
  both its one-shot and the window it claimed, so the bound's ladder runs on that next tick whether
  the chip is still off or came back meanwhile. A cooldown, per-boot cap or backstop denial (I65, I9)
  arms the retry the watchdog fires the moment that wait ends; a denial that arms no retry keeps the
  rung pending, is asked again every tick and is journaled once per distinct reason
  (`hold_bound_host_rung`). A rung still pending from a denial below the bound is spent by the
  bound's ladder: the full check after that ladder decides, so the host rung waits for its verdict
  (a failure asks at once; no verdict, the 300 s check bound) even when the denial clears
  meanwhile, and never fires at the ladder's end without one. Of those retries only the cooldown's
  (the boot / post-power-cycle wait) stands in for the 10-min ladder (I83): a missed request's
  retry, the per-boot cap's or the backstop's follows no cycle that ran, so the hold still gets its
  ladder at 600 s. Only that bounded boot / post-power-cycle cooldown may hold longer than
  the bound, with one exception: a live readable tenant that is not a broker job's leftover (invariant
  4, I6/I7) still refuses every rung, so such a hold is bounded only by the tenant. It is never silent:
  at the bound the broker journals and pages `hold_bound_tenant_blocked` once per episode, naming the
  blocker (pid, user, command), `/health` shows `next_step` `tenant_blocked` with `blocked_by`, and
  on the first sampler tick after the tenant leaves the ladder runs, then the host rung if the full
  check fails. With the power cycle
  opted out (`TT_DEVICE_MCP_AUTO_POWER_CYCLE=0`) the hold pages
  `hold_bound_no_host_rung` once per episode. The operator kill switch
  `TT_DEVICE_MCP_FORCE_ESCALATE=0` turns off only the forced runs below the bound; the bound's ladder
  and host rung ignore it, no env or config switches the bound off, and the boot rung inventory's
  `RUNG OFF forced-escalation` warning says so. `/health` carries `hold_bound` while a hold is open:
  `bound_sec`, `deadline`, `held_age_sec`, `next_step` (`ladder`, `power-cycle` or
  `tenant_blocked` with `blocked_by`, or `startup_check` on a startup hold once its re-adopted jobs
  have ended, spec 03 I75) and, while the host rung waits on it, `cooldown_until`; until the
  bound's ladder runs, `next_rung`, `next_rung_at` and `next_rung_source` name its mark too (I75's
  deadline feed). One helper, `hold_bound_deadline()`, gives the open
  hold's escalation deadline for every hold kind: its start + the ceiling (under the spec 03 I42
  latch, one sampler tick until the early ladder or its host rung has run, then the ceiling again,
  whose full ladder still runs), or the end of a running power-cycle cooldown when that is later;
  for a `startup_unverified` hold whose startup gate is still pending, at least the re-adopted
  jobs' end + `STARTUP_VERIFY_DEFER_MAX_SEC` (spec 03 I75; no hold is open while such a job still
  serves inside its stated end);
  `bound_sec` is the same mark; `/health`'s `deadline` and the stuck-hold event's and
  alert's `escalation_deadline` all read it. A stuck-hold event past the bound reports `escalated`
  true once the bound ran a ladder or asked for the host rung. Time to the power-cycle request for a
  hold no class path heals (e.g. a topology-mismatch or 77-only hold, class D): at most 600 s +
  the ladder when the ladder or its full check fails (+ one 10 s sampler tick when a relift check
  gave the verdict), or 600 s + the ladder + 300 s while the check hangs or waits on hugepages; then the cycle, the boot and
  the startup check. Per class: a fabric-only hold (77, topology mismatch, known fabric fault,
  latched I46; class D), as above; a hold under the spec 03 I42 latch (class D, moved tray whose
  first job then failed on the fabric), one sampler tick + the ladder (host rung no later than
  ~10.7 min into the hold, was 14-16 min on the watchdog path); a 77 the episode's reset and sweep already left unverified
  (I29), at most 600 s + one sampler tick, with no second reset; a killed job's leftover stuck in the
  kernel (class J), was open-ended (the hold stood until an operator rebooted), now at most 600 s +
  one sampler tick; an off-bus hold (class B1/B2) whose one-shot below the bound left it unhealed
  now gets the bound's own ladder at 600 s and then the host rung (before, the one-shot stood in for
  it); a hold under `TT_DEVICE_MCP_FORCE_ESCALATE=0` was open-ended, now bounded as above; a host
  rung deferred by a reset scope or a retry-less denial fires one sampler tick after the deferral
  ends (before, a whole window later); class A (whole tray) and C keep the I37 direct cycle, well inside the bound. Each
  waits only on a running boot / post-power-cycle cooldown (I65) and fires as it ends. Bounds the about 70 min one Galaxy host sat in a latched topology-mismatch hold
  with no rung run. Blocking PCI sysfs writes (a remove/isolate, a scoped PCI rescan (I95), a
  bridge reset, the valve's `isolate_chip`, the post-reset cut's remove) go through one in-flight
  writer per broker (`PCI_WRITER`): each write runs in a tracked worker thread with its bound
  (rescan `PCI_SYSFS_WRITE_TIMEOUT_SEC`, 30 s; bridge reset about 91 s); while an earlier
  timed-out write is still alive, a new one is refused at once and starts no thread. The first
  timeout or refusal latches PCI-wedged (`pci_wedged`, `host_at_risk`): the pass stops (the rung
  fails, the remaining chips stay lost or withheld with reason `pci_wedged`), later PCI rungs are
  skipped at once (`pci_write_refused`), the dead-chip valve skips its chips
  (`chip_isolation_skipped`) and holds the device, and the power cycle is requested at once
  through the host rung; only the I65 cooldown may delay it, and the request fires as the
  cooldown ends, not at the 10-min mark. That request's reason names the skip, never a failed
  ladder: `PCI-wedged hold: held <n>s and the ladder was skipped (<latch reason>)`, or, for any
  other request asked with no ladder run (leftover chips, a refused kill), `the ladder was skipped:
  <why>`, kept for the episode's later retries of it until a bound ladder runs. A request after a
  bound ladder that ran says so: `the ladder ran and a PCI write blocked (<latch reason>)` when one of
  its rungs latched the wedge (the first timeout stops the pass), else that the full ladder did not
  heal it. This is report text only: when and whether the cycle is asked is unchanged. No hold lifts while latched unless no write is pending
  and the full check with fabric has just passed; that lift clears the latch
  (`pci_wedged_cleared`). Bound: one PCI timeout per ladder, at most one leaked thread per host.
  Time to recover for a blocked PCI lock: one timeout (about 91 s for a bridge reset, 30 s for a
  rescan), then the power-cycle request (cooldown permitting), instead of one timeout per chip
  plus the 10-min wait. Other classes are unchanged. Accepted gaps: a thread blocked in the
  kernel cannot be killed and stays until the power cycle; the latch is held in memory, so a
  broker restart loses it (at most one more timeout and one more thread); an operator reset is
  refused while latched; the valve neither kills holders nor stops pollers while latched (the
  driver's fd-close path reads the chip, spec 03 I83); the forced-escalation kill switch can
  delay the early request until the 10-min bound.
- **I72 — A power-cycle request that never took the host down is a missed attempt, asked again
  as its window ends.** The power-cycle command can return while the cycle never happens (an
  unreachable BMC, a cycler that failed after it detached). A request whose pending window
  (`host_action_pending_sec`) ran out with the host still up on the same boot is marked
  `took_host_down: false` in the I9 ledger, on disk, so a later boot still reads it as no cycle; a
  same-boot row older than the window reads as missed after a broker restart too. A missed request
  anchors no I65 cooldown, counts toward neither its doubling nor the per-boot cap nor the I9 backstop,
  and is not a broker escalation that brought a later boot up. A cycle that lands after its window
  (a slow cycler) is a cycle that ran: when the last escalation row before this boot's start is a
  missed power-cycle row from an earlier boot, and this boot started within `host_action_pending_sec`
  plus the boot attribution window (`TT_DEVICE_MCP_BOOT_ATTRIBUTION_WINDOW_SEC`) of it, that row
  counts for the doubling, the backstop and the boot's attribution (not the per-boot cap, which counts
  only this boot's rows), and is marked `took_host_down: true, late: true` on disk so every later boot
  counts it too (`power_cycle_request_late`). Broker startup writes that mark for the boot it starts
  on, whatever the boot does later: an older broker may have escalated it by reboot, or nothing at all, and its power-cycle
  gate never runs. The mark is idempotent, and a ledger the broker cannot read or rewrite is left as
  it is and logged. Counting it is the boot-loop-safe side. The first miss is asked again
  as its window ends, with no backoff on top: the window already gave the cycler its time. Later
  misses are asked again `POWER_CYCLE_MISS_RETRY_SEC` (5 min) after their window, never stopping.
  Before a retry goes out the governor checks the boot and the earlier request: a boot since the
  request makes it a cycle that ran (above), so the I65 cooldown it anchors applies and no second
  cycle lands on it; a request still inside its pending window in memory (it counts from the
  hand-off, after any I26 pre-cycle hook) refuses the retry with a stated deadline at that window's
  end. Each miss journals `power_cycle_request_missed` with the count and the retry time; from the
  second miss on an operator is paged (about once an hour) to check the BMC and the power-cycle
  command while the retries go on. The retry is a stated deadline like the cooldown's: `/health`
  `power_cycle_cooldown` shows it with cause `missed power-cycle request` and `missed_requests` (0
  for a plain cooldown), and the hold-deadline watchdog asks again at it, through the same I37 path,
  stand-back, tenant and scope guards. The count runs from the later of this boot and the last
  verified release; a verified release clears it. A cycle that did take the host down still anchors
  and doubles the cooldown (I65), and its release waits for the full post-reset check, fabric
  included (03 I4, I32).

  Stated bound: once the BMC answers again, the next request goes out at most
  `host_action_pending_sec()` after the request it missed for the first miss (15 min by default), and
  `host_action_pending_sec()` + 5 min for later ones (20 min by default; 65 min at the 3600 s cap),
  plus the I26 pre-cycle hook's run time when one is set. This is a stated missed-request exception
  to the class A and B2 recovery budgets. Time to recover per fault class (missed request = BMC
  unreachable or cycler lost; times run from the BMC answering again to the next request):

  | Fault class | Before I72 | After I72 | Budget |
  |---|---|---|---|
  | A whole tray, missed request | 30 min-4 h (each miss anchored and doubled the cooldown) | first miss 15 min, later 20 min (window + 0 / + 5 min) | 10 min; missed-request exception: window + 5 min |
  | B2 four chips off, missed request | 30 min-4 h (same) | first miss 15 min, later 20 min | 12 min; same exception |
  | A or B1 after a cycle that landed late | doubles 30 min, 1 h, 2 h, 4 h | unchanged: the late cycle counts and doubles on every path, also when its boot never cycles (the startup mark) | I65 boot-loop guard (stated exception) |
  | A, B1 or B2 after a cycle that took the host down | I65 cooldown, doubling | unchanged | unchanged |

  I72 adds no wait before the first power-cycle request and gates only the power-cycle rung, never a
  reset rung: the hold-deadline escalation (the reset ladder, then the power cycle if the check
  after it still fails) runs as before. Its waits come after a request: the
  pending window, which already held off device resets, and at most 5 min more for a repeated miss,
  each a stated `/health` deadline the watchdog asks at.

- **I73 — A whole tray still off the bus after two broker power cycles that did not heal it pages
  that AC removal may be needed, and recovery keeps running.** On this hardware an in-band power cycle
  healed a whole-tray drop (class A) on some dates and not on others; the drops it did not heal came
  back only after AC power was removed from the host, which no broker-reachable path can do. So once
  a whole tray is off after `POWER_CYCLE_AC_NEEDED_AFTER` (2) broker power cycles for that tray, with
  no verified release (I9) since the streak began (I65), the broker flags the AC-needed state and
  pages, but never stops recovering: no hold passes 10 min without the full ladder, and the next power
  cycle fires the moment its I65 cooldown (or the I9 backstop) ends. Only that bounded spacing (30 min,
  1 h, 2 h, then 4 h after each boot) keeps cycles from looping the host; there is no cycle limit. A
  cycle counts only when the broker's own ledger shows it was requested for that tray (`off_bus` on
  the row names one of its chips; a row with no `off_bus` counts) and took the host down: the request
  was before this boot and is not a missed request (I72), and the last one is followed by a later
  request under another boot or by this boot beginning within its pending window
  (`host_action_pending_sec`). A boot long after the last request was some other cycle, not that
  attempt. External cycles (a manual, BMC or datacenter cycle, the out-of-band history file) count
  toward the I65 cooldown but never toward the two: their kind and off-time are unknown, and external
  events are not broker evidence. A partial off-bus set (B1) never enters this state. The journal
  records `power_cycle_did_not_heal` once per counted cycle and `power_cycle_ac_power_needed` once per
  episode, and an operator is paged once and then every `AC_POWER_REPAGE_SEC` (1 h). `/health`
  `power_cycle_cooldown` keeps `escalation_stage` `power-cycle`, its `next_deadline` is the next
  cycle, and `ac_power_needed` carries the chips, attempts and last cycle (null on any other
  cooldown); the hold message says AC removal may be needed, that the full ladder runs at the 10-min
  hold bound after each cycle or boot and when the next cycle comes. Inside any class-A cooldown (two
  counted cycles or not), the I76 10-min hold bound's forced run is the one trigger (no per-class
  watchdog start beside it): that run, and no other forced pass inside the cooldown, lets the
  broker's own off-bus ladder run once, as its gates allow: for a whole-tray drop that is the kept
  sweep, the bridge reset then the mesh reset, with no tray step (I23). It runs once per anchor (the
  later of the last cycle or boot and the hold's start; an episode never outlives its boot, so after
  a boot that is the hold's start and the bound's mark). Each
  reset is followed by the full health check, fabric included; a check that fails keeps the hold,
  and the cycle follows at the cooldown's end. The bounded I65 cooldown is the only longer hold. A tray that came back takes the
  ordinary reset path, and the device is released only after the full post-reset health check,
  fabric included, passes (03 I4, I32). A tray that drops again before any verified release is
  cycled again at its cooldown's end. Time to recover for class A (budget 1 h): unchanged when one
  cycle heals it (the cycle at once plus a boot); when the ladder heals it, 10 min after the last
  cycle or boot plus the ladder; when neither does, the device is never left in an open-ended hold:
  the ladder at 10 min after each boot and cycles 30 min, 1 h, 2 h and then every 4 h apart, until a
  cycle heals it or AC power is removed; the device is back within the next gate plus the full check
  once the tray returns. A fault that needs AC removal is over the 1 h budget by nature: it is paged,
  not hidden.
- **I84 — A post-reset 77 from the dispatch-probe cap on a proven fabric re-checks at once, with no
  eth-training wait.** `_verify_device_after_reset` waits `POST_RESET_FABRIC_RETRY_SLEEP_SEC` (60 s)
  before re-checking a 77 because a reset returns before the eth links re-train. When the 77 is the
  fabric wrapper's dispatch-probe cap (`dispatch probe hit its <N>s cap without a verdict`), the
  wrapper ran that probe only after its validator had already moved traffic across every link, so the
  links were trained and the wait cannot help: the re-check runs at once. It is the same full check,
  fabric included, and the retry count is unchanged; a re-check that still reaches no verdict is
  UNKNOWN and held exactly as before (03 I40), never a release (I32). Any other 77 keeps the wait. The
  wrapper's reason names the cap line ahead of any validator error line above it, so the test reads
  the cap and not an earlier line. Time to recover: classes H/J (dirty after a job) and every other
  class whose reset is followed by this verify drop by the 60 s wait when the first pass hits the
  dispatch cap (a galaxy job kill: 321 s, over the 5 min class J budget; 261 s with this). The
  worst-case bound is unchanged: the 60 s wait is dropped only where its stated purpose (eth
  training) is already proven, and a re-check that stalls again is the 77-only hold of 03 I40 with
  or without the wait.

- **I82 — A release followed by a quick re-hold does not restart the 10-min clock.** The I76
  bound counts from the first moment the device stopped being healthy in the episode. A release
  (a latch lift, a relift check, the bound ladder's own check, a startup check after a broker
  restart) that a new hold follows within `_HOLD_CARRY_SEC` (300 s), or during the first job
  started in that window when that job fails, or within `_HOLD_CARRY_SEC` after that failed job
  ends, continues the episode: the new hold keeps the released hold's `held_since` and the
  bound's ladder state, whatever its class or latch (a re-latch, a failed first job, a class
  change). A bound ladder whose check released a device that is held again did not heal it, so it
  counts as a failed check: the host rung is due at once (`hold_bound_carried`, `host_rung_due`
  true), not after another ladder; the I65 boot / post-power-cycle cooldown still applies and the
  rung fires as it ends. The first job extends the window only while the release is in doubt: the
  release followed no just-passed full check on the full cabling (no fabric verdict, a failed or
  short count, or a pass older than 300 s; the carry records this as `verified`), or the episode's
  bound ladder had run. After a verified release with no bound ladder, a hold that opens more than
  300 s after the release is a new episode whatever its first job did (`hold_carry_ended`): its
  clock starts at zero and its class path, 10-min ladder and power-cycle escalation run on that
  clock. A re-hold within 300 s of any release still continues the episode, so a hold that flaps
  open again right after a release keeps its clock. A carry an older broker wrote has no `verified`
  and counts as in doubt. The carry ends, and the next hold is a fresh episode with its own class
  path, as soon as the device proves the release right: the first job after it ends completed,
  exit 0, naming no device or fabric fault; or a full health check (fabric included) passes with
  no hold open. The pass that releases a hold is the release itself and does not end the carry it
  saves, including the startup check that releases a hold a dead broker left open; a pass that does
  not release (a reset still owes its fabric pass, I32) ends nothing. A tenant hold (a foreign process holds the device) is not fault time: its release saves
  no carry, it neither takes nor continues one, and a fault hold's carry stays in force across it
  for a later fault hold in the carry's own window (tenant holds are never persisted, I76). A
  tenant hold that turns into a fault hold (the holder left the device still faulted) takes, at
  that switch, a fault carry still in its window, as a fault hold opened then would
  (`hold_turned_fault`). The first job's end is noted before the job-end checks run, so one of
  them raising cannot leave the window open; only a clean end drops the carry. The
  carry (`hold_carry.json`) and the bound ladder's age (in the episode record) are on disk, so a
  broker restart keeps them: a hold the last broker left open escalates no later than its own
  start + 600 s (at once when that is past), and a hold that one past its bound ladder goes to the
  host rung; only a hold whose start is unknown counts from the restart. The startup hold a restart
  opens (`startup_unverified`, spec 03 I4) names no fault: it keeps the start of a hold still open at
  the restart, and like a tenant hold it neither takes nor spends the carry of an episode a release
  already ended. `device_held` names that carry as `carry_deferred`, and the hold's clock starts when
  it opens (a second restart during it restores that start and defers again). Otherwise a restart
  over a healthy job that started in the carry window (the window stays open while that job runs)
  was born as old as the released episode: past the deadline, so the watchdog flagged it, forced an
  escalation and paged a tenant block on a device whose job was healthy. Once the startup hold names
  a fault (its `why` turns into a fault class: the startup gate's full check fails), it is a fault
  hold and takes a carry still in its window, as a fault hold opened then would, and the bound
  resumes: a release after the bound ladder whose first job, re-adopted across a restart, fails on
  the same fault goes to the host rung at once. A re-adopted first job's clean exit 0 naming no fault
  drops the carry. Any other end of it (a named fault, a failing exit, no exit status, a kill)
  leaves the startup hold's `why` as it was, so the hold names its fault only at the startup gate's
  verdict, and the gate's reset and full check can outlast the window counted from the job's end (a
  failing reset 130-263 s, the fabric check up to 180 s, its retry). That end therefore pins the
  carry to the startup hold: it stays open, however long the gate runs, until the hold turns into a
  fault hold (which takes it) or a pass releases the hold (which saves its own). The pin resumes no
  bound and asks no rung while the gate owns the hold (spec 03 I75); a gate that never reaches a
  verdict leaves the hold to its own 10-min mark. A startup hold that spec 03 I75 deferred while the
  re-adopted job ran (no episode open, so no I76 clock) is a startup hold for the pin too: that job's
  failing end pins the carry the same way, and the hold that opens after it starts a fresh clock at
  that end (`carry_deferred`), with no escalation until its own bound. The I76 bound itself starts
  only when that hold opens: a job running on the device at the restart is not a hold. The startup
  hold still lifts only on the startup gate's full check, fabric included (a short link count
  fails); a pass that releases it saves its own carry, and its own 10-min mark still runs the bound's
  ladder. The carry never crosses a
  reboot or power cycle (the boot id must match): a cycle ends the episode, and the startup check
  after hugepages gates release as before (spec 03). A re-hold after the window with no job, or
  around a job started after it, is a new episode. At most one power-cycle request per episode
  (I76, I65). The full health check, fabric included, still runs after every reset and gates
  every release; the carry adds no lift path. Time to recover (TTR), from the re-hold:

  | Case | Before I82 | With I82 |
  |---|---|---|
  | Bound ladder's check releases, first job fails on the same fault, re-hold (missing port, I42 latch, known fabric fault, 77-only, generic) | open-ended: ladder, release, failed job, new 10-min clock, repeated (one Galaxy host on 2026-10-17: about every 13 min for about 4 h) | carried: no ladder, power-cycle request at max(episode start + 600 s + ladder, I65 cooldown end) + one sampler tick |
  | Release, re-hold within 300 s with no job, below the bound | new 10-min clock | carried: the class path runs as before; the clock keeps the episode start, so the bound fires no later than episode start + 600 s |
  | First job after the release succeeds, or a full check passes with no hold open, then a new fault | new episode | new episode: unchanged; the class's own fast path runs (a whole-tray drop still goes straight to a power cycle) |
  | Tenant hold released, then a fault hold | new episode | new episode: unchanged; the tenant's time is not counted |
  | Tenant hold turns into a fault hold within 300 s of a bound ladder's release | one more ladder at 10 min (about 10-13 min) before the host rung | carried: no ladder, power-cycle request at once (I65 cooldown still applies) |
  | Hold open across a broker restart | 10-min clock restarted at the restart | escalates no later than episode start + 600 s |
  | Release, then a broker restart opens the startup hold (a re-adopted job, no hold open at the restart) | the startup hold took the released episode's start: deadline flag, forced escalation and tenant-blocked page at once, over a healthy job | fresh clock while it names no fault: the startup gate decides (spec 03 I75), the bound fires no earlier than the startup hold's start + 600 s; a real hold open across the restart is unchanged |
  | Release after the bound ladder, then a restart during the first job, then that job or the startup check fails on the same fault (every class: missing port, I42 latch, known fabric fault, 77-only, generic) | host rung at once (the startup hold took the carry) | unchanged: the hold that names the fault takes the carry, host rung at once, no second ladder (I65 cooldown still applies) |
  | As above, the re-adopted first job ends on a fault, a failing exit or no exit status, and the startup gate's reset and full check fail more than 300 s after that end: link-short classes (missing port, I42 latch, known fabric fault) | host rung right after the failed gate (a startup hold that only deferred the carry lost it once the window closed: fresh clock and one more I83 at-start ladder and its full check before the cycle) | unchanged from base: the pinned carry is taken at the failed gate, host rung at once, no further ladder (I65 cooldown still applies) |
  | The same, 77-only and generic | host rung right after the failed gate (a startup hold that only deferred the carry lost it: fresh clock, a second ladder at the startup hold's start + 600 s, about 10-13 min more, before the cycle) | unchanged from base: host rung at once after the failed gate, no second ladder (I65 cooldown still applies) |
  | The same job ends that way and the startup gate passes, fabric included | release, the first job after it decides | unchanged: the pass releases the hold and saves its own carry |
  | Verified release (full check passed, fabric on the full cabling), no bound ladder, first job runs past 300 s and fails or is killed, re-hold | n/a | new episode: fresh clock, the class path runs (an off-bus hold's one-shot at its own 120 s), the full ladder at its own 10-min mark, then the power cycle; before this rule the re-hold took the released start and escalated at once (one Galaxy host: a 29-min first job killed for its time limit, re-hold born 32 min old, power cycle 2 min later) |
  | Unverified release (no fabric verdict, failed or short count, stale pass), first job runs past 300 s and fails, re-hold | carried | unchanged: carried |
  | Every other class path below the bound | unchanged | unchanged |

- **I83 — A fabric link-short hold gets one ladder at hold start, then the power cycle.** A hold
  with every expected chip on the bus (none isolated) whose cause is a topology mismatch or a known
  fabric fault (I30's causes other than a 77: the full fabric check counts fewer links or ports than
  the cabling expects; such a count is a failed check, never a pass), is the fabric link-short class.
  So are the measured holds of the same shape: one under spec 03 I42's latch, and one whose last full
  fabric check failed on missing descriptor ports (spec 03 I71; ports that
  `TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS` excuses never fail it, so they stay excused). On the
  hold's first watchdog tick the broker runs its full reset ladder once (`escalation_kind`
  `fabric_link_short`; under the I42 latch, past its one-tick bound, the same one ladder runs as that
  bound's), lifting only spec 03 I46's known-fabric-fault latch first as at the bound, never the I42
  latch, and then the full health check, fabric included. Under the I42 latch a short count fails, so
  only a clean pass on the full topology clears it. Single flight: once that pass has run or is
  running, the idle relift's measured-fault present-mesh retries (spec 03 I56) run no other glx_reset
  in the episode, and the sweep takes no I93 wait; that ladder's check, then the host rung, decide the hold. A check that passes releases the device as any check does (spec 03); the hold never lifts
  without it. A check that still counts short, or a ladder that does not bring the mesh back, asks for
  the I76 host rung (the power cycle) in the same run. A run that finds the hold is not (or no longer)
  this class, e.g. a chip already off the bus by its heartbeat, declines once for the episode and is
  not spawned again; the hold keeps its own class's path. That pass stands in for the episode's bound
  ladder only once the host rung after it was granted or its boot / post-power-cycle cooldown retry
  armed: then no later tick
  (the 10-min bound, a later ceiling window, the hold deadline) runs a second glx_reset or ladder, and
  the host rung is next. The I82 carry and a broker restart keep the pass's age and its at-start
  mark but not the grant, so after either the bound still runs one more full ladder (about 220 s,
  bounded), then the host rung. When no host
  rung was granted (the power cycle opted out, or a denial other than the cooldown, including a
  retry armed by a missed request, the per-boot cap or the backstop), the pass is a
  forced run below the bound and the full ladder runs again at the 10-min mark, then the I76 cadence.
  The same holds under the I42 latch: its one-tick bound counts the pass as the bound's ladder, but a
  latch shortens the bound, never the 10-min mark. A power cycle denied there for any cause but the
  cooldown (the rung stays pending, asked each tick) still gets the full ladder at 10 min, with the
  latch kept, then its full check: only a pass releases; a failure asks for the power cycle again.
  That ladder spends the pending rung on every link-short path (latched, topology mismatch, missing
  ports): a denial that clears while its check still waits (e.g. on hugepages) does not cycle the
  box before the verdict or the I76 check bound (ladder end + 300 s). A
  cooldown retry armed across the 10-min mark runs no ladder there; the cycle fires as the cooldown ends.
  A re-hold of another class in the same episode below the bound (I82 carry or a broker restart)
  asks for the host rung once and runs no further forced ladder. A check still waiting on the 1G
  hugepage pool keeps the I76 check window (under the I42 latch too), but that window ends at the
  10-min bound at the latest.
  The governor still decides the host rung: the existing stand-back checks and a live tenant refuse
  it as in I76, and the I65 boot / post-power-cycle cooldown denies it and arms the retry, which
  fires on the first tick after the cooldown ends. Other classes are unchanged: a whole-tray drop goes
  straight to the power cycle, partial off-bus keeps its ladder, a 77-only hold keeps its idle relift
  until the bound. Evidence: on one Galaxy host on 2026-10-17 a one-missing-port hold got four
  bound-fired glx_resets (each rc 0, the same port missing after each) and was healed only by a power
  cycle, after which the startup check counted every link. Time to the power-cycle request for this
  class: was 10 min plus repeated ladders (open-ended while the post-ladder check passed on the short
  count), now one ladder plus one full check from hold start, at most 600 s, or the cooldown's end
  when later. With no host rung grantable it adds one ladder plus check at the bound (about 220 s),
  then I76's cadence; under the I42 latch too (was: no ladder at the 10-min mark, only the denied rung
  asked each tick until the denial cleared, open-ended). Latched and missing-port holds: were a measured-fault present-mesh glx_reset at
  once, failed-check relift retries about every 120 s, then the I76 ladder at the bound and the host
  rung (about 11-16 min to the power-cycle request, with 3-4 futile resets); now the same one ladder
  plus one full check from hold start (about 2-5 min to the request), at most 600 s, or the
  cooldown's end when later.
  A non-cooldown denial before the 10-min ladder (all link-short holds): the power cycle waits for
  the full check after that ladder, at most 300 s more (was: fired at the ladder's end, about
  600 s, with no verdict); a failed check asks at once, a pass releases with no cycle. A retry
  armed by a missed request, the per-boot cap or the backstop: the 10-min ladder runs at 600 s
  (was: deferred to that retry's end, 30 min or more). The cooldown and a cycle that fired:
  unchanged.
  A carried re-hold of another class: the host rung at once instead of a ladder on every tick.
  A run that declines (a heartbeat read once showed a chip off the bus) declines for the whole
  episode, so a link-short hold whose heartbeat glitched once waits for the I76 bound: its ladder at
  600 s, then the host rung (bounded). On a startup hold (spec 03 I75) the at-start pass stands down
  like the ceiling window: while a re-adopted job runs it meets it on `job_running`; from the job's
  end to the startup gate's verdict it leaves the hold to the gate (blocker `startup_verify`, its
  window given back) and runs no rung. Only the gate's full check, fabric included, lifts the hold; a
  gate that fails gets the at-start ladder on the next tick, then the power cycle if the check after
  it still fails. A host rung due below the bound meets a running re-adopted job as a tenant
  (`job_running`) and fires on the first tick after the job ends. Time to recover (link-short on a
  startup hold): job end to the gate's verdict (about 1-2 min, at most `STARTUP_VERIFY_DEFER_MAX_SEC`)
  when the gate passes; plus one ladder and check, then the power-cycle request, when it fails, inside
  the 10-min bound.

- **I86 — The last-chance sweep re-probes the bus after SBR and runs the full check once it is
  whole.** When the sweep before a host rung (I22) fires SBR, it re-reads the bus right after it
  (`pcie_guard.chips_off_bus` plus the isolated set: sysfs and heartbeats, no device access). If
  every expected chip is back, no further rung fires: the full health check, fabric included, runs at
  once (no settle; the bus is already enumerated). A pass releases through the same funnel as any
  sweep (I32 still applies). A failure fires the remaining rung (the mesh reset), then one settle and
  one full check; if that fails the caller climbs to the power cycle as before (I76: ladder at
  10 min, then power cycle). A bus that is still short, or a probe that cannot read it, fires the next
  rung at once, as before. The probe never lifts a hold by itself. Whole-tray and all-off-bus classes
  still skip the sweep (I37), and the I65 cooldown is unchanged. Time to recover (TTR):

  | Case | Before I86 | With I86 |
  |---|---|---|
  | Partial off-bus, SBR brings every chip back, full check passes | SBR + mesh reset + 60 s settle + full check | SBR + full check (saves the mesh reset and the settle, about 2 min) |
  | Partial off-bus, SBR brings every chip back, full check fails | SBR + mesh reset + settle + check, then power cycle | SBR + check + mesh reset + settle + check, then power cycle (one more full check, about 45-100 s) |
  | Partial off-bus, SBR does not bring every chip back | SBR + mesh reset + settle + check | unchanged (one sysfs read added) |
  | No isolated chip (present-mesh eth/fabric hold, 77) | mesh reset + settle + check | unchanged |
  | Whole tray, all chips off the bus | sweep skipped, power cycle (I37) | unchanged |

- **I88 — A broker power cycle brings the host OS down cleanly first, within a bound, and never
  cuts power under a live OS.** Both host power cycles the broker fires (the auto power cycle,
  `ipmitool chassis power cycle`, and the configured `TT_DEVICE_MCP_POWER_CYCLE_CMD` of I27) go
  through `fire_os_first`. No broker path runs the cycle command while the OS is up. The order,
  after the ledger record, the jobs row and the I26 hook:
  1. Checks. The clean path needs systemd as PID 1, the installed hook, `systemctl`, `systemd-run`,
     `timeout`, `/bin/bash`, an argv the request can carry and a cycle command on `PATH`; `argv[0]`
     is then made absolute, so neither the hook's nor the fallback's `PATH` decides what runs. For
     an `ipmitool ... chassis power cycle` command a read-only pre-flight, `ipmitool [same options]
     mc info`, must exit 0 within 10 s (`PREFLIGHT_TIMEOUT_SEC`): a BMC that does not answer cannot
     cycle, and the host would only reboot warm. Any other command gets only the executable check.
  2. The broker writes `/run/tt-device-mcp-power-cycle.request`: the epoch, the reason, the hook's
     per-try timeout, then one argv word per line (mode 0600, fsynced). `/run` is empty on every
     boot, so a request never outlives it.
  3. It starts a transient fallback unit (`systemd-run`, `DefaultDependencies=no`, so the shutdown
     transaction does not stop it, the reason in `TT_DEVICE_MCP_POWER_CYCLE_REASON`). It runs under
     bash with argv[0] `@tt-device-mcp-power-cycle-fallback`, which systemd-shutdown's killall
     spares, records its pid in `/run/tt-device-mcp-power-cycle.fallback.pid`, waits the bound in
     its own process (`read -t`, no child to kill), flushes for 2 s without blocking on it, has the
     installed hook restore the IPMI device (`--prepare-ipmi`, below; 10 s cap, at once when
     `/dev/ipmi0` is there), since a shutdown that hangs may hang past the service stop that took it
     away, and fires the same argv as `@tt-device-mcp-power-cycle-fire` under the command's timeout.
  4. It asks for a reboot (`systemctl --no-block -i reboot`).
  5. Once every service has stopped and the filesystems are read-only, systemd-shutdown runs the
     installed hook `tt-device-mcp-power-cycle.shutdown`. On the `reboot` verb with a request less
     than an hour old, it sets a standard `PATH`, exports the reason in
     `TT_DEVICE_MCP_POWER_CYCLE_REASON` and first stops the fallback by its recorded pid (after
     checking its cmdline), before any wait, so it never fires alongside the hook: it freezes each
     generation of the fallback's process tree, then SIGKILLs the process group the fallback leads
     and every pid found, so a `timeout` and cycle command the fallback already started cannot cut
     power later. Then it starts a `sync` it does not wait on and, when no IPMI device node is there
     (`/dev/ipmi0`, `/dev/ipmi/0` or `/dev/ipmidev/0`), restores in-band IPMI beside the RAID wait:
     a service may have stopped it during the shutdown (the OpenIPMI init script's stop unloads
     `ipmi_si` and `ipmi_devintf` and deletes `/dev/ipmi0`), and an in-band `ipmitool chassis power
     cycle` then cannot reach the BMC, so the host would only reboot warm. It loads
     `ipmi_msghandler`, `ipmi_devintf` and `ipmi_si` and makes `/dev/ipmi0` from
     `/sys/class/ipmi/ipmi0/dev` if devtmpfs has not (50 looks 0.2 s apart, 10 s cap). It waits for
     RAID arrays to mark clean (`mdadm --wait-clean`, 10 s cap) and for that preparation; a
     preparation that fails is said loudly and the cycle is still tried (the command may be
     out-of-band). For an `ipmitool [options] chassis power cycle` command it adds an OEM SEL record
     (Add SEL Entry, type C0h, the hook's timestamp, manufacturer 0, data `TTPC`, stage 01h, detail
     1 when it restored the IPMI device, else 0; 2 s cap) through the same options, the hook's trace
     that outlives the reboot. Then it fires the argv: up to 3 tries at the per-try timeout with 2 s
     between them, then 25 s for the power to drop. When all 3 fail it says so loudly (the reboot
     goes on warm) and adds a stage 02h record (detail 3). The per-try timeout is
     `TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC` (30 s for the auto cycle) clamped to 5-15 s, so the
     hook's worst case, 10 + 2 + 3 x 15 + 2 x 2 + 25 = 86 s, stays inside systemd-shutdown's 90 s
     hook timeout. Any other verb, or no request, does nothing.

  The bound is sized from the host's own systemd timeouts (I99): 90 s + the longer of
  `DefaultTimeoutStartSec` and `DefaultTimeoutStopSec` + 30 s, 210 s with systemd's defaults, at
  most 600 s. `TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC` may only raise it (to the same cap);
  a lower or malformed value is ignored, and no value turns it off. The fallback's clock starts at
  the request and lives through the hand-over to systemd-shutdown, so a shutdown that hangs at any
  stage before the hook (a stuck service, umount, another hook) is cycled at the bound + 2 s.

  **A cycle that cannot start fires nothing.** When a check fails or the request write, the
  fallback unit or the reboot request fails, the broker undoes the steps it took (stops the unit,
  removes the request and pid file) and raises `PowerCycleNotLaunched`. The escalation retracts the
  ledger entry (I26), clears the pending host action, keeps the hold and pages
  `power_cycle_not_launched` once per hold episode with the reason (every attempt is journalled);
  the I71 launch-failure page stays quiet for that episode, so it gets one page, then I71's hourly
  re-page while it keeps failing.
  The I76 bound asks for the cycle again on the next pass, so it fires on the first pass whose clean
  path can start. Nothing stands in for it: there is no warm reboot rung (I71), so a cycle that did
  not launch never leads to a reboot, and the hold lifts only on a just-passed full check, fabric
  included. The one exception: if the reboot request failed and the fallback unit
  cannot be stopped (`systemctl stop`, then `kill`, and `is-active` still says active or cannot
  answer), it may still fire at its bound, so `fire_os_first` returns `fallback_pending`, the ledger
  entry stays and the host action stays pending (I34). A `power_cycle_os_first` event records each
  path (`reboot_requested` with the bound, `not_launched` or `fallback_pending` with why).

  **The next boot checks that the cycle happened.** At broker startup, before the I72
  late-power-cycle mark, `confirm_power_cycle_on_boot` looks at the last escalation recorded before
  this boot. If it is a power-cycle row from an earlier boot, not yet checked, and this boot started
  within its pending plus attribution window, the broker reads the raw Get System Restart Cause,
  `ipmitool [same options] raw 0x00 0x07` (read-only, 10 s), once, names its code per IPMI 2.0
  (0x0-0xb; `chassis restart_cause` prints `invalid` for 0xa soft reset and 0xb RTC wakeup) and marks
  the row `cycle_checked`, with `host_rebooted: true` and `power_cycled` (true, false, or null when
  the BMC cannot tell) as separate fields:
  - `confirmed`: a cause that means the power went off and on (a chassis power control command, a
    PEF power cycle, a power-up, RTC wakeup included). It counts as before.
  - `refuted`: any other cause, or `unknown` from a BMC that has reported a real cause before (the
    stamp `power_cycle_restart_cause_capable.json` in the health dir, written the first time it
    does). The hook's command failed and the host rebooted warm. The row gets
    `took_host_down: false` and `cycle_refuted`, and the late-cycle check never counts it again.
    It is an I72 missed attempt, counted from its earlier boot until a verified release ends the
    streak: the next request goes out at the refuted request + the pending window (900 s by
    default) + 0 s, a second miss 300 s after its window, and the operator is paged from the second
    miss on, then about hourly. The warm boot it caused anchors no I65 cooldown or doubling and no
    I73 streak. Because that boot was still a host restart, the row counts toward the I9 backstop
    (at most 3 host restarts in 2 h, then the broker pages instead of cycling).
  - `unconfirmed`: no answer, a command with no BMC query to derive, `unknown` from a BMC never
    seen to report a cause, or a reserved code (0xc-0xf, recorded as `unmapped code 0x..`; it never
    writes the capability stamp). It is journalled and counts as a cycle, the boot-loop-safe side
    (one I65 cooldown).
  - A null `restart_cause` never stands alone: the row and its `power_cycle_confirmed` or
    `power_cycle_unconfirmed` event carry `restart_cause_unavailable`, a short reason (no BMC query
    for the command, no executable, no answer within the 10 s, the BMC's error line such as
    `rsp=0xff): Unspecified error`, or an answer that does not parse), at most 160 characters.
  - SEL fallback: where the cause would leave the row `unconfirmed`, the broker reads the same BMC's
    SEL, `ipmitool [same options] sel time get` then `sel list last 40` (read-only, 5 s each). A Power
    Unit `Power off/down` or `Power cycle` record, asserted, between the request and this boot (30 s
    slack, the record's time moved onto the host's clock by the BMC clock's offset from `sel time
    get`, so the BMC's zone and skew do not matter) makes it `confirmed`, with the record's time as
    `sel_power_off` and `restart_cause` as read. The SEL only confirms: no record, or a SEL or clock
    that cannot be read, leaves it `unconfirmed`, never `refuted`. It adds about 2 s to that one
    startup (measured on a Galaxy BMC: `sel list last 30` 1.2 s, `sel time get` 0.1 s) and changes no
    recovery step, since `confirmed` and `unconfirmed` both count as a cycle.

  A refuted cycle's boot is a warm reboot: the FSM's boot attribution and the back-filled jobs-list
  row say so (`warm reboot (the broker-fired power-cycle did not happen: ...)`), never "boot
  attributed to a broker-fired power-cycle". What follows depends on the device, not on the old hold
  episode:
  - The startup check (03 I4), fabric included, passes: that verified release ends the I72 streak,
    so no further cycle fires. It is logged and journalled once as `power_cycle_miss_healed`
    (`warm_reboot: true`).
  - The device is held (the startup check failed, or a later hold before a verified release): the
    first time a hold episode is seen with an I72 miss outstanding, the broker arms the I72 retry
    for that episode at the miss's `until`, so the watchdog forces one ladder pass then and the
    governor decides as for any cycle (the I65 cooldown and boot-loop guard, the I9 backstop).
    The retry is never lost because the episode it was missed in closed with the old boot.

  The configured command now runs from systemd-shutdown, after the network is down: it must be an
  in-band command (for example local `ipmitool`). If the hook's 3 tries all fail, systemd-shutdown
  reboots warm. That is not a recovery rung and never stands in for the cycle: the next boot's
  startup check (03 I4) finds the device as it is, the check above marks the row, and the I65
  cooldown and the I76 10-min bound run again. I65, I76, I27's guards and the lift gate (a hold
  lifts only on a just-passed full check, fabric included) are unchanged. `apply-host-config.sh`
  installs the hook (spec 08), so a fresh install has it from the first start.

  Stated limits. A BMC that never reports a restart cause (some Galaxy BMCs
  answer none) cannot refute a cycle, and confirms one only from its SEL: there the pre-flight is
  the only guard, and a warm
  reboot in place of a cycle still counts as one (one I65 cooldown, at most 30 min, at most 4 h
  after repeated unhealed cycles). A configured command that is not `ipmitool ... chassis power
  cycle` gets only the executable check and is never confirmed. A BMC that answers `unknown` after a
  real cycle would refute it falsely; this needs the capability stamp first, so it only bites on a
  BMC that has reported a cause before. A false refute loses that cycle's I65 cooldown, so the next
  cycle can come sooner than I65 allows, but the backstop still counts it: at most 3 host restarts in
  2 h, then a page.

  Time to recover (TTR), for every class that ends in a power cycle (class A whole-tray drop, the
  I76 10-min escalation, the I27 off-bus cycle):

  | Case | Before I88 | With I88 |
  |---|---|---|
  | Clean shutdown | cycle fires at once | + the pre-flight (under 10 s) + the host's stop time (about 90 s measured on a Galaxy host, most of it unmounting the root volume) |
  | Shutdown hangs before the hook (before or after the hand-over to systemd-shutdown) | cycle fires at once | + the bound (120 s default, 30-300 s) + 2 s flush |
  | Clean path cannot start (a check, the pre-flight, the request, the fallback or the reboot fails) | cycle fires at once under a live OS | no cycle and no reboot; hold + page once per episode; the cycle is asked again on the next I76 pass and fires on the first pass whose clean path starts; the hold lifts only on a just-passed full check, fabric included |
  | Reboot failed and the fallback cannot be stopped | n/a | cycle at the bound + 2 s from the fallback; action kept pending |
  | Hook's command fails 3 times | the live cycle may also fail | warm reboot after at most 86 s of hook time; then refuted: an I72 miss, the next request at the refuted request + 900 s pending + 0 s, then + 300 s per later miss, paged from the 2nd miss, no I65 cooldown, counted toward the backstop (3 restarts in 2 h, then a page); or unconfirmable: counted (one I65 cooldown, at most 30 min, at most 4 h after repeated cycles) |

- **I89 — A below-floor off-bus hold gets its kept rungs at once, and the full ladder at the
  10-min bound.** A generic below-floor off-bus hold (`BELOW_FLOOR`, `HOLD_CLASS_GENERIC`, not a
  whole tray) no longer waits for the I76 bound for its gentle rungs. The gate's first pass runs
  them (`_below_floor_gate_rungs`): a PCI rescan (withheld while a removed chip still owes its I69
  reset), SBR of the chips the defaults allow, and, once every chip is back on the bus, the full
  health check, fabric included. Only a pass lifts the hold (I32); a failed check, or a fabric check
  that finds fewer links than the cabling expects, keeps it (`below_floor_kept_rungs_unverified`) for
  the bound. The pass runs once per hold episode and off-bus set; a new set or a new episode runs it
  again. A tenant or a live reset scope skips it, and the next gate pass retries.

  Before the bound the SBR defaults do not change. A chip reading all-ones (isolated) is SBR'd. A
  chip gone from the bus (absent from two heartbeat reads, its PCIe node gone) is SBR'd only when
  `TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET` is set; by default it gets no SBR and is left to the hold,
  which still arms the off-bus one-shot and its mesh reset (I96). A stalled chip
  that still enumerates with its PCIe link up and a heartbeat that is not all-ones is on the bus and is
  never SBR'd: its bridge hosts live silicon. It still arms the off-bus one-shot (I96). A chip off the
  bus whose node is still present but whose PCIe link reads down (host PCI, `link_down`) or whose
  heartbeat reads all-ones is not live silicon: at the gate and at the bound the rung first stops the
  broker's own device readers (I86 of spec 03), then PCI-removes it as the one in-flight PCI write,
  all removes inside one 15 s detach budget (`below_floor_linkdown_isolated`). It is then a removed
  chip and the SBR resets it through its recorded bridge (I69). Once every remove worked the stopped
  readers are SIGKILLed; after a failed one they stay stopped until their own timeouts end them (I86 of
  spec 03). A chip whose remove fails or wedges is not reset, and the hold stands. Every SBR the galaxy ladder fires is recorded for the hold episode.

  At the 10-min bound (`escalate(..., bound=True)` from the I76 watchdog) every kept rung runs: PCI
  rescan, then SBR of the bridged off-bus chips. A chip is bridged when the rung has a bridge to write
  to: it is isolated, the broker removed it (its recorded bridge, I69), or it is gone and
  `TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET` is set (its bridge found by the secondary bus). The bound
  does not widen the opt-in: without it a gone chip has no bridge, so no SBR is tried, the skip is
  journaled (`below_floor_sbr_skipped`) and the chip is not queued. A queued chip the rung reports it
  could not fire on (no bridge, gated, no privilege, rescan withheld) is un-queued
  (`gone_chip_bridge_reset_unqueued`), so no later gate pass routes to a no-op bridge rung; a chip still
  gone is re-detected at the next run. Chips the stuck-hold ladder's own bridge rung already reset in
  the same run are not SBR'd a second time. If they bring every chip
  back, the full check with fabric runs at once and a pass lifts the hold with no mesh reset.
  Otherwise the ladder goes on: its one mesh reset and the full check with fabric when this hold has
  not run one yet; else the I87 climb (the sweep once per phase, or the power cycle at once when the
  reset failed on the same chips, I22). When that climb skips the sweep because its mesh reset
  already failed on the same chips, the sweep's SBR still runs if the drop is below the galaxy floor,
  is not class A or C (I37) and this hold has not SBR'd those chips (an isolated or removed chip, a node-present chip whose link reads down or whose heartbeat reads
  all-ones, or a gone chip with the opt-in), with the rescan and the full check with fabric after it; only a pass lifts, else the power
  cycle follows (`reset_rung_skipped_failed_unchanged` carries `sbr_owed`). The climb logs that every
  rung below the host pair has run only when no such SBR is owed. The same SBR is owed when the mesh reset
  itself dropped the chip (off before [], off after [n]: a regressed reset, I31) or no reset failed at
  all. Below the floor and for neither class A nor C, the last-chance sweep first removes each off-bus
  chip whose node is still present and whose link reads down or heartbeat reads all-ones (readers
  stopped first, as at the gate), so its SBR runs even when nothing was isolated before. And before the
  host rung, whether the sweep ran, was skipped or was spent, any such chip this hold has not SBR'd (one
  the dead-chip valve isolated during the sweep's settle, say) gets the rescan, its remove and the SBR,
  then the full check with fabric (`host_rung_sbr_owed`); only a pass lifts, else the power cycle. One
  SBR per chip per hold episode. A drop at or above the floor, all
  chips off included, owes no SBR: it stops no reader, removes no chip and goes straight to the power
  cycle, never nibbled at with per-chip bridge resets. A pass lifts the hold; a failure requests the power cycle (I76), or pages and
  holds when none is opted in (I31). An enumerated chip is still not SBR'd at the bound; the mesh
  reset covers it. An earlier forced run is not the bound's ladder: the rescan and SBR run again at
  the bound; a spent mesh reset is not repeated (I87). Below the bound a forced run keeps its own path:
  the mesh reset, with no rescan or SBR (the gate ran those at once). It keeps I58 (a mesh reset that
  failed on the same chips is not repeated and the host rung fires at once); otherwise its host rung
  waits for the ceiling (I38) or the bound. A power-cycle cooldown at the bound still
  runs the ladder, and the power cycle fires when the cooldown ends (I65). Time to recover (TTR):

  | Case | Effect of I89 |
  |---|---|
  | B1 below-floor partial off-bus that a PCI rescan brings back (e.g. a chip off the bus at broker start) | recovered at the gate's first pass: rescan (about 3 s settle) + full check (about 45-100 s), instead of holding to the 10-min bound |
  | B1 gone chip, `TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET` set, SBR brings it back | rescan + SBR (about 10 s) + full check at the first pass, instead of the 10-min bound |
  | B1 that the gate rungs do not heal | still the bound's ladder, then the power cycle; one rescan (about 3 s) added, once per episode and off-bus set |
  | B1 at the 10-min bound, bridged chips | rescan (about 3 s settle) + SBR (about 10-20 s) ahead of the one mesh reset and its full check (or of the power cycle when this hold's mesh reset is spent, I87), then the power cycle: about 15-30 s more; an SBR that brings every chip back lifts on the full check with no mesh reset (I86) |
  | B1 at the 10-min bound, a gone chip without the opt-in | rescan (about 3 s settle) only, ahead of the mesh reset or power cycle; no SBR is tried (no bridge) and no no-op bridge rung follows at later gate passes |
  | B1 forced run below the bound after a mesh reset failed on the same chips | the host rung fires at once (I58), after the sweep's SBR when this hold has not SBR'd those chips (below the floor only, never class A or C): about 15-60 s more; a drop at or above the floor, all chips off (C) or a whole tray (A) gets no SBR and no time added |
  | B1 regressed mesh reset (off before [], after [n]) or a valve-isolated chip, node present, below the floor | reader stop + PCI remove (at most 15 s) + SBR (about 10-20 s) + full check with fabric before the host rung: a pass lifts with no power cycle; else about 15-60 s more before the power cycle, well inside the 15 min B1 budget; class A and C unchanged |
  | B1 single chip off the bus with its node present, link down or heartbeat all-ones | reader stop + PCI remove (at most 15 s) + SBR (about 10-20 s) + full check at the gate's first pass, instead of only a rescan and then the mesh reset and power cycle; about 15-60 s more when it does not heal, well inside the B1 budget |
  | B1 at the bound inside a power-cycle cooldown | ladder at the bound, power cycle at the cooldown's end (I65); budget exception unchanged |
  | Whole tray (A), `TRAY_DOWN_NO_WINDOW`, at or above the floor | unchanged (I37, I27) |

- **I90 — The bridge-reset rung narrates as a device reset, and /status carries a structured
  resetting field.** Every reset step's device-op detail starts with `device reset:`
  (`DEVICE_RESET_DETAIL_PREFIX`): the mesh reset (`device reset: tt-smi -glx_reset ...`), the AICLK
  re-apply, and now the per-chip bridge reset (`device reset: bridge reset of chip(s) <ids> (~10s)`;
  it used to read `recovering chip(s) ... via bridge reset`). `/status` (`tt_device_queue_status`) reads that
  prefix while a device op is active: the `[broker]` op row has `resetting: true` and the top level
  `device_resetting: true`; both are false otherwise, so a reader need not match row text. Narration
  only: no rung, gate, wait or order changes. Time to recover (TTR):

  | Case | Effect of I90 |
  |---|---|
  | Every fault class, the I76 bound included | none: the rungs, their order and their waits are unchanged |

- **I91 — A broker mesh reset never sends the BMC's legacy retimer reset.** tt-smi's Galaxy tray
  reset (`-glx_reset`, `-glx_reset_auto` and their long forms, tt-smi 6.4.0 and later) re-powers the
  trays with `ipmitool raw 0x30 0x8B <ubb> <dev> 0x3 <time>`. On any failure of that call (a
  non-zero exit, a busy or wedged BMC, completion code 0xc9 on BMC firmware older than 0.05.22) it
  sends the same command again with op_mode `0x0`, although its own source says not to. op_mode
  `0x0` also asserts the PCIe retimer reset: it can take every device behind those retimers off the
  bus at once, NVMe drives included, which can take down the host's own storage. tt-smi has no
  switch for this fallback, so the broker closes it:
  - **The guard.** Every mesh reset the broker launches, through either reset backend (the
    `systemd-run` scope and the local no-systemd child), runs as `unshare --mount --propagation
    private -- <python> -I ipmi_guard.py --wrap -- <tt-smi argv>` (`ipmi_guard.guarded_argv`).
    Inside that private mount namespace the wrapper mounts a small owner-only (root) guard, read-only, over every
    `ipmitool` that sudo's secure path or the broker's PATH resolves, checks each mount by reading
    it back, then execs tt-smi. The host's mounts are not changed. The guard refuses (exit 97,
    `tt-device-mcp ipmi guard: REFUSED ...` on stderr) any `raw 0x30 0x8B` whose op_mode byte reads
    as zero in any format ipmitool accepts (`0x0`, `0x00`, `0`, `00`, any case) or cannot be read,
    and the `exec` and `shell` subcommands, which could carry the same request unseen. Everything
    else, op_mode `0x3` and every read-only call included, execs the real binary unchanged. A
    wrapper that cannot arm the guard (not root, no mount namespace, a mount that fails or does not
    read back) does not run the reset: it prints `tt-device-mcp ipmi guard: NOT ARMED` and exits 1,
    and the broker counts that reset as not fired (so does `unshare: unshare failed`). No environment variable, flag or config turns
    the guard off.
  - **What counts as a mesh reset (`ipmi_guard.is_mesh_reset`).** Any argv with a Galaxy tray reset
    flag (`glx_reset` or `galaxy_6u_t` inside any word), wherever it sits and whatever launches it.
    `TT_DEVICE_MCP_RESET_ARGS` is a full argv override, so its first word proves nothing:
    `sudo tt-smi -glx_reset`, `env X=1 tt-smi -glx_reset`, `python3 -m tt_smi -glx_reset` and a
    wrapper script given the flag all run under the guard, after the preflight. An override that
    is not a mesh reset runs only when one of its words is tt-smi itself (`tt-smi`, `tt_smi`, or
    either with `.py`, under any directory), as in `sudo tt-smi -r`, a path that never sends the
    BMC UBB reset. Any other override (e.g. a wrapper script given no Galaxy flag) is one the guard
    cannot classify: it is not run (fail closed). The reset returns rc None (not fired, no
    failed-reset cooldown), the journal gets `reset_override_unclassified`, `/health`
    `mesh_reset_bmc_guard` shows `override_unclassified`, and the caller climbs to its next rung
    as for a preflight refusal.
  - **The preflight.** Before it quiesces the bus for a mesh reset, the broker asks the BMC
    `ipmitool mc info` (read-only, at most `BMC_PREFLIGHT_TIMEOUT_SEC` = 10 s). If the BMC does not
    answer in time, `ipmitool` is missing, or the firmware is provably older than 0.05.22 (firmware
    revision below 0.05, or 0.05 with the first aux revision byte below 0x16, read in either its
    decimal or BCD meaning), the mesh reset is not run: the reset returns rc None (a reset that
    never fired, so no failed-reset cooldown is armed), the journal gets
    `mesh_reset_bmc_preflight_refused` with the reason, `/health` `mesh_reset_bmc_guard` shows it,
    and the caller climbs to its next rung exactly as for any reset that did not recover the mesh.
    A firmware version it cannot read is not a refusal: the guard still stops a `0x0` retry.
  - **Detection.** After every mesh reset the broker reads its output. The guard's refusal, or
    tt-smi's `Falling back to legacy retimer reset`, journals `bmc_legacy_reset_attempted`
    (`host_at_risk: true`, `sent: false` when the guard refused it, `true` when the fallback text
    appears without a refusal), pages once per episode (until a mesh reset passes without either),
    shows in `/health` `mesh_reset_bmc_guard`, and turns an rc 0 into rc 1.
  - **The post-reset check is unchanged (03 I52, I78).** A refused, unarmed or preflight-refused mesh reset never
    releases the device or lifts a hold. Every reset that ran is still followed by the full health
    check, fabric included, before any release; the hold climbs as before (I76: the ladder at the
    10-min bound, then the power cycle, which takes the host OS down first, I88).

  Time to recover (TTR):

  | Case | Effect of I91 |
  |---|---|
  | Any class whose ladder runs a mesh reset, BMC answers and op_mode `0x3` works | the preflight's `mc info`, typically under 1 s, at most 10 s per mesh reset; the guard's mount setup, well under 1 s |
  | Same, op_mode `0x3` fails | the reset ends rc 1 at once instead of tt-smi's `0x0` retry; the next rung (the I76 bound's power cycle) follows; the 30 s settle and up-to-100 s driver wait of a `0x0` reset are not spent |
  | Same, BMC silent or firmware below 0.05.22 | the reset is not run (at most 10 s spent); the next rung follows at once |
  | Same, run through a `TT_DEVICE_MCP_RESET_ARGS` override that names the Galaxy flag | as the three rows above (the override was run unguarded before) |
  | Same, an override the guard cannot classify | the reset rung is not run (no time spent); the next rung follows at once |
  | Classes with no mesh reset (per-chip bridge reset, `tt-smi -r`, power cycle) | none |

- **I92 — Before every reset, the device is cleared of readers.** A reset takes chips off the bus,
  and a process still reading one gets no completion: each read is a completion timeout, and under
  firmware-first error handling a reader in a loop can take the host down. So `reset_with_quiesce`
  (the mesh reset, the sweep, the operator reset), right after the device pollers stop and before
  the reset fires, runs the broker's holder scan (`pre_reset_scan.clear_device_readers`):
  - Every holder other than the broker itself is either a tenant (`is_tenant`, or the running job
    and its children) or a reader.
  - A tenant refuses an automatic reset (owner `[broker]...`): nothing is signalled, the reset does
    not run (`reset refused: ...`), the tenant is named in
    the journal and in `/health` `pre_reset_blocked_by`, and the hold goes on to the existing
    tenant handling of the I76 bound. An operator reset's own gate (I6/I7) has already ruled on
    tenants, so there they are only recorded.
  - A chip still bound to the driver whose heartbeat reads all-ones is detached first
    (`isolate_chip`, the driver's PCI remove, the only operation that marks a chip detached). The
    driver sends a bound chip a power-state message as soon as a holder's fd closes, so killing a
    reader of an off-bus, still-bound chip would make the kernel read that chip. If any such chip
    cannot be detached, no reader is signalled and the reset is refused; the hold stays and the
    bounded recovery (the ladder at 10 min, then the power cycle, I76) runs. This is looked at only
    when readers are found. A sysfs remove can block in the kernel, so the detaches run off the
    event loop under one deadline for all the chips (`PRE_RESET_DETACH_TIMEOUT_SEC`, 30 s), each
    the one in-flight PCI write (I76); one that has not returned by then, or is refused behind an
    earlier blocked write, counts as not detached (`detach_timed_out`) and latches PCI-wedged (the
    power cycle is asked at once, I65 cooldown permitting), and `/health`, the queue and the I76
    timers keep running meanwhile.
  - A reset already in flight is never signalled: a running reset scope (`ttdev-reset-*`), a holder
    inside one, or a reset the broker launched outside a scope (registered by pid, its children
    included) refuses the new reset before any signal (`reset_in_flight`). Killing a reset partway
    through the chips is worse than any reader. The operator reset (tool and stream) refuses too:
    `a reset is already in flight (<unit>)`.
  - A holder the driver names that the scan cannot read (often a pid mid-exit) is looked for once
    more after `PRE_RESET_RESCAN_SETTLE_SEC` (1 s). Still unseen, it refuses an automatic reset
    (`scan_incomplete`): an unseen reader is the one the scan exists to remove. An operator reset
    records the gap and goes on; a gap no pid explains is recorded and the reset goes on.
  - A gap pid that is a zombie (state Z or X: a killed job's leader) is classified before any rescan
    by its thread group in `/proc/<pid>/task`:
    - **exited**: only the leader is left. do_exit releases the mm and the fd table, and runs the
      deferred fput that ends in the driver's release, before it marks the task a zombie, so it holds
      no fd and no mapping and is not a holder. The driver names an opener's group leader until the
      file's last reference goes, though, and a forked child inherits the fd and the mapping. So the
      scan then walks every other process for a device fd or mapping. If none is found and every process
      could be read, the gap is cleared and the owed reset runs at once, with no rescan and no wait.
      If one is found (`open_elsewhere`) or a process could not be read (`walk_incomplete`), the gap
      stands as below.
    - **holds**: a live thread still shares the fd table and shows a device fd under
      `/proc/<pid>/task/<tid>/fd` (or a device mapping): a real holder. It gets the one rescan and
      refuses as above.
    - **unknown**: live threads, none of whose fds or maps shows the device (the release may be running
      in one of them). When every gap pid is such a zombie, the scan looks again every 1 s until each
      clears or turns exited, for at most the zombie wait. For an automatic reset that is the time left
      to the hold's 10-min ladder mark (I76) minus `pre_reset_zombie_ladder_reserve_sec(chips)`, never
      past the mark. The reserve is all that a zombie which clears on the last poll still runs under the
      device-op lock the ladder needs: that poll (1 s), the reader phases the scan may still run (detach
      30 s, SIGTERM grace 10 s, release wait 30 s), a healthy mesh reset (60 s) and its full check with
      fabric (`FABRIC_CHECK_TIMEOUT_SEC` 180 s + 3 s per chip): 407 s on a 32-chip Galaxy, which is also
      the count assumed while the host's expected chips are unknown. The mark is the hold's start plus
      its ladder mark, not the I76 deadline a power-cycle cooldown extends: the ladder runs at the mark
      inside a cooldown and only the power cycle waits for its end. For an operator reset, or with no hold
      clock, it is `PRE_RESET_ZOMBIE_WAIT_SEC` (30 s, the release bound of 01 I67). Still named after
      that, the refusal above stands, and the hold runs the full ladder at 10 min, then the power cycle.
      Each zombie (pid and start time) gets this wait once, so one that outlives it adds nothing to
      later rungs.
    A gap on a live pid, or beside one, gets only the one rescan. The `pre_reset_holder_scan` event
    lists each zombie gap: its pid, state, thread count, live threads, verdict, wait, and its parent's
    pid, comm and state (a parent that does not reap its children shows here). Nothing new is
    signalled and a zombie's parent is never killed. Whatever the verdict, no hold lifts unless the
    full health check with fabric, all cabled links up, has just passed (03 I78), and every hold still
    gets the full ladder at 10 min, then the power cycle (I76).
  - Each reader gets SIGTERM, `PRE_RESET_TERM_GRACE_SEC` (10 s, the preemption grace) to let go,
    SIGKILL, then up to `HOLDER_RELEASE_WAIT_SEC` for the driver's release (01 I67). Each signal
    re-checks pid and start time, so a reused pid is never signalled.
  - A reader still holding after that and stuck in the kernel (state D or a pending SIGKILL) is
    registered as a reaped leftover (01 I16): the leftover fence and the I70 clock take it to the
    host rung. Any other reader left holding refuses the reset with its name.
  - Every reset journals one `pre_reset_holder_scan` event (outcome `clear`, `killed`,
    `reset_in_flight`, `tenant_blocked`, `scan_incomplete`, `offbus_undetached`, `unkillable`,
    `survivor` or `scan_error`). A scan that itself fails (the holder read errors, or the scan
    raises: outcome `scan_error`) lets the reset run, as before the scan existed; recovery must not
    stop on a scan bug, and the reset's own gates still apply.
    The event's `identities` names each holder found (the 03 I43 holder identity, read before
    any signal, off the loop within the same fixed bound); logging only, it never delays the
    scan past that bound or changes its outcome.
  - A refusal leaves the device pollers stopped, owed to the next close to HEALTHY (03 I66), which
    only a passing full check, fabric included, reaches: never restarted onto chips a refused
    reset left as they were.
  - The bridge rung runs no scan: its targets are already cut out of the kernel, so no process can
    map them and closing a holder sends them nothing. It journals the event with outcome
    `targets_detached`.

  A refused reset is a rung that did not run: it is journalled as `reset_refused` (never
  `reset_failed` or a timeout), the cooldown clock goes back to the last reset that ran, a kill-reset
  debt stays owed, the AER mask and the off-bus intent taken for it are released, `FABRIC_DEBT`
  stays owed and no hold lifts until the full health check, fabric included, passes (03). The hold
  goes on to the I76 bound: the ladder at 10 min and, while the full check with fabric still fails,
  the power cycle. A tenant bounds it only as the I76 tenant exception states. Queued jobs and the
  post-job gate are unchanged.
  Time to recover (TTR):

  | Case | Effect of I92 |
  |---|---|
  | Every fault class, nothing but the broker holding the device | one holder scan before each reset, well under 1 s; no other change |
  | Any reset with a reader present | + up to 10 s (SIGTERM grace) + up to 30 s (release wait), only then |
  | A reader of an off-bus, still-bound chip | + up to 30 s for the detaches (one deadline, off the event loop), then the reader case above: at most 70 s in all; refused if a detach fails or times out, then the I76 bound (ladder at 10 min, then the power cycle) |
  | A reset already in flight | the new reset is refused at once (no wait); the running reset's own verify decides, and the hold stays for the I76 bound |
  | A holder the scan cannot see | + 1 s for one rescan; still unseen: an automatic reset is refused, then the I76 bound |
  | A zombie the driver still names (killed job) | Whole thread group exited and the file open nowhere else: + 0 s, the owed reset runs at once instead of a refusal followed by a pre-job full check (~143 s on a 32-chip Galaxy) and the reset after it. A live thread holds the device: refused, then the I76 bound. Live threads that cannot be told: + 1 s polls, once per zombie, ending when it clears, and at most until the reserve (407 s on a 32-chip Galaxy: the scan's last phases, the mesh reset and a full check with fabric) before the 10-min ladder mark, so the reset and check after a zombie that clears last still end before it (30 s for an operator reset); still named: refused, then the I76 bound |
  | Any refused reset (reader left, chip not detached, reader stuck) | no wait of its own: the rung is skipped and the I76 bound runs the ladder at 10 min, then the power cycle if the full check with fabric fails |
  | Tenant under an automatic reset | the reset waits for the tenant, as the I76 tenant exception already states |
  | Reader stuck in the kernel | the I70 host rung and its budget |
  | Bridge rung | none |

- **I93 — The sweep's mesh reset is spaced from a failed first one, never past the bound.** On a
  present Galaxy mesh whose first mesh reset (`tt-smi -glx_reset`) in a hold episode exited non-zero
  or failed its full check, the climb's last-chance sweep (I22) runs the episode's second mesh reset,
  as I87 states. A second mesh reset heals about a quarter of the meshes the first one did not, mostly
  on op_mode `0x3` boxes, and the heals came after a gap of roughly 200 s at the short end and about
  6 min at the median; one right after the first re-hits the same settling mesh. So before the sweep
  runs below the bound, the climb waits until `MESH_RESET_MIN_GAP_SEC` (200 s) after the first reset
  COMMAND ended, cut so the wait ends by the I76 10-min bound (`gap_wait`). The wait is inline: the
  device stays held, no job runs, and the hold never lifts during it. It journals `mesh_reset_spaced`
  (the wait, the time since the first reset ended, the hold age). After the wait the sweep runs as
  before (its tenant and reset-in-scope guards are read then), and its full check, fabric included,
  decides: a pass lifts the hold (I52, 03 I78), a fail asks for the power cycle (I71). There is no
  separate repeat and no third mesh reset; I87 is unchanged. `tt-smi -r` (LoudBox and other
  non-Galaxy boards) gets no wait. The gap is a code constant in `health/recovery/mesh_repeat.py`: no
  env var, config key or CLI option sets it or turns it off. The sweep waits only when ALL of:
  - a Galaxy mesh reset on more than one chip, the hold clock known, and this hold phase's sweep not
    spent (I87);
  - not a fabric link-short hold (I83: a topology mismatch, a known fabric fault, the 03 I42 latch or
    missing descriptor ports): that class keeps its one ladder, then the power cycle at once;
  - every expected chip on the bus, by heartbeat and by `pcie_guard`, none isolated and no removed chip
    a reset would rescan (I69): classes A, B and C keep their own paths;
  - no BMC guard finding in the episode (I91);
  - the first mesh reset belongs to this episode: launched under its stamp, or ended inside it (a
    reset from an earlier episode spaces nothing).

  At the bound (I76) the wait is zero: the bound's ladder runs the sweep at once, so the power-cycle
  request is never later than a ladder at 600 s. A first reset that ended late waits only to the
  bound.

  Time to recover (TTR). The gap counts from the first reset command's end, and the post-reset check
  (about 20-155 s) runs inside it, so the wait is 200 s minus the check time when the climb follows
  the check at once.

  | Fault class | Before | After |
  |---|---|---|
  | Fabric/health-check failure or dispatch failure (03 I55), present mesh, first reset failed (D/J/H/F) | sweep at once, then the power cycle | the sweep waits out the gap (about 45-180 s); a heal lifts at about first reset end + 200 s + the sweep (~62 s) + settle + check, with no power cycle; a fail asks for the power cycle up to 200 s later than before, never later than a ladder at 600 s |
  | Partial off-bus (B/C), whole tray (A) | unchanged | unchanged (no wait) |
  | ARC not ready (I11) | the I11 retry, then climb | unchanged (a chip off the bus: no wait) |
  | Fabric link-short (I83) | one ladder at hold start, then the power cycle | unchanged (no wait) |
  | Hold-deadline expiry (I76) | ladder at 10 min, then power cycle | unchanged; the bound's ladder never waits |
  | Dirty boot / startup check (03 I75) | unchanged | unchanged |

  New wait: at most `MESH_RESET_MIN_GAP_SEC` (200 s) after the first reset ended, and always over by
  the 600 s hold bound.

- **I94 — A chip the bridge rung brings back counts recovered only with a live ARC heartbeat.** A bridge
  reset can re-create a chip's sysfs node with its ARC dead (no telemetry). Back on the bus is not
  recovered: after the rung, each chip whose node returned is read for its heartbeat, up to
  `BRIDGE_RESET_ARC_WAIT_SEC` (5 s) at `BRIDGE_RESET_ARC_POLL_SEC`. A chip whose heartbeat is missing,
  unreadable (ENODATA) or all-ones at the end stays in `isolated_chips`, its rung counts failed and a
  `bridge_reset_arc_dead` event names it, so the off-bus one-shot (spec 03 I23) still fires on its
  short clock instead of the hold waiting for the 10-min ladder. The check only reads sysfs: it adds no
  PCI write, reads nothing once the pass has latched PCI-wedged (I76), and it can only keep a chip held, never lift a hold (the lift still needs the full
  post-reset check, fabric included). The 10-min ladder and power-cycle escalation are unchanged.
  Each read is a decision read of only those chips, so a chip marked lost (spec 03 I66) is read for
  real. On a host whose KMD exposes no `tt_heartbeat` attr the check cannot answer: it marks no chip,
  says so once (`bridge_reset_arc_check_skipped`), and the rung's other checks and the full post-reset
  check still decide. An attr that is there but unreadable (ENODATA) is a dead ARC, not a skip.

- **I95 — Every PCI rescan is scoped to the Tenstorrent root ports; nothing writes the global
  `/sys/bus/pci/rescan`.** A global rescan walks every root bus of the host and can reassign the
  bridge windows of root ports unrelated to the device (for example a host's NVMe drives). Every
  rescan the broker and its probe make (the reset path's rescan, the bridge reset's closing rescan,
  the bridge-reset batch rescan, the bare rescan of dead chips, the tray-down prelude, the
  below-floor and last-chance rescans, 03's closing and driver-wait rescans, and the health probe's
  rescan) goes through `pcie_guard.rescan_tt_root_ports()`. It writes `1` only to
  `/sys/bus/pci/devices/<root port>/pci_bus/<bus>/rescan` of the root ports above Tenstorrent
  endpoints (vendor 0x1e52): those on the bus now, plus those remembered in the recorded topology
  (I24) whose port is still in sysfs, so a chip already off the bus, and a broker that starts with
  the tray off, still rescan its port. A remembered port is trusted only while no other device
  sits below it: one with no Tenstorrent function below it now whose subtree holds any other
  function that is not a PCI bridge (class 0x0604) is skipped and journaled as
  `pci_rescan_port_skipped` with reason `non_tt_function_below` (its slot may since hold another
  card, an NVMe drive for example, whose bus a rescan would resize). A remembered port with an
  empty subtree, or only bridges below it, is still rescanned. The kernel rescans and resizes only
  that port's bus. There is no global fallback: with no Tenstorrent root port to rescan, the call
  writes nothing, journals `pci_rescan_no_tt_root_port` and raises, so its rung fails and the
  ladder goes on (I76: the ladder at 10 min, then the power cycle). The broker's rescans still run through the one in-flight writer
  (`PCI_WRITER`, I76) with the same bound and latch; the probe's rescan still runs under the
  device-op flock. Only the target changed.

  Time to recover (TTR): no fault class is slower. A scoped rescan touches one bus per Tenstorrent
  root port instead of every root bus, so it returns no later than the global one; with no
  Tenstorrent root port it fails at once instead of walking the host, and the next rung or the
  bounded power cycle follows as before. Timeouts, the latch and the lift rules are unchanged.
  Skipping a remembered port that now holds another device removes no rung on a host whose chips
  sit below their ports. A host with no recorded topology that starts with its chips off the bus
  has no port to rescan, so it loses the rescan rung (there is no global fallback) and goes on to
  the next rung.

- **I96 — A below-floor off-bus hold arms the off-bus one-shot for every chip the gate counted off the
  bus, isolated or not.** The one-shot (spec 03 I23) and the hold report's next-rung candidate key on
  `isolated_chips | _offbus_hold_chips`. When the gate holds `BELOW_FLOOR` with chips off the bus, it
  sets `_offbus_hold_chips` to the chips still off the bus (`offbus_chip_ids`) by a read taken after
  its kept rungs, so a chip their rescan brought back does not arm it. Three holds that used to wait
  for the 600 s bound now get the one-shot on its short clock: a chip that enumerates but whose
  heartbeat read fails (not ENODATA), which is counted off the bus but never isolated; a chip gone
  from the bus (no PCIe node, absent from the heartbeat reads) while
  `TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET` is unset, which I89 leaves without an SBR; and any off-bus
  chip of a hold a broker restart carried, whose new process starts with both sets empty (its startup
  gate fills the set again). The set only arms the one-shot. It routes nothing: these chips are not
  added to `isolated_chips`, so the bridge rung never SBRs an enumerated chip, and a gone chip gets
  no SBR without the opt-in (I89). A whole tray gone still goes to the power cycle with no reset first
  (I37).

  A chip back on the bus leaves the set: the one-shot's forced run drops every chip its own read finds
  back, and the idle DOWN re-check does the same. When no chip is left to arm it (and none is
  isolated) before the 10-min bound, the run gives the one-shot and its claimed window back,
  journals `offbus_one_shot_dropped` (`reason=chips_returned`) and runs no reset. Dropping it never
  lifts the hold: the hold stays until the full check passes, and the bound's full ladder, then the
  power cycle (I76), still run. At or past the bound nothing is dropped. The set is cleared when the
  hold episode closes. The one-shot's reset lifts the hold only through the full post-reset check,
  fabric included (all cabled links); a failed check keeps the hold for the 10-min ladder and then
  the power cycle (I76).

  Time to recover (TTR): such a hold reaches its first reset one sampler tick after it opened (on a
  tenant-free box), not at the 600 s bound. No other class changes.

- **I77 — A killed holder stuck on a dead chip withholds every PCI re-enumeration and escalates in
  120 s (fault class `J-dead-chip-stuck-holder`).** The class: the dead-chip path (03 I19) killed a
  process that mapped the chip, and once the release wait ends the process is still alive, stuck in
  the kernel (state D, or SIGKILL pending), with a `/dev/tenstorrent` fd or mapping, or a mapping of
  one of the chip's PCI BARs. Detection, `note_stuck_holders` right after the release wait: each
  killed pid is checked once (`job_reap.stuck_device_holder`); its BARs are read once, before the
  removal (a removed chip's sysfs link is gone), and its maps read is bounded by
  `STUCK_MAPS_READ_SEC` (2 s, a D-state task can hold its mmap lock), with at most one reader
  thread out per pid: while one is out, a further read of that pid counts as unanswered at once. A
  pid that exited, is not stuck, or is stuck on nothing of ours is not the class; one whose fds name
  nothing of ours and whose maps are unreadable or unanswered counts (fail closed). The dead-chip
  kill names every member of a process group it signals (killpg), not only the leader, so a member
  that maps a BAR with no device fd is checked too. Each such pid is journaled `dead_chip_holder_stuck` (`host_at_risk`,
  `severity: high`, the chips, the holders, the class and budget) and paged through the alert hook
  once; a page the hook drops is retried on the next scan. The process is registered (pid,
  starttime, uid, BARs) as a reaped leftover (01 I16), so I21 fences every reset and dispatch over
  it; a reused pid (other starttime) or a process that let go is pruned
  (`dead_chip_holder_released`) and the fences lift with it. The registered entry wins over the same
  pid found as a job's leftover (its job's scope outlived the job, or the sweep named it a
  survivor): it keeps the dead-chip label, so the budget, the class and the fence below hold. The
  registry is recorded under the health dir (`dead_chip_stuck.json`, atomic rename, with the boot id
  and the episode's wall-clock start; one snapshot and write at a time, so a slower save never puts
  back a registry a later one emptied) and removed once empty; a broker restart reloads it before
  anything else at startup, keeps only an entry on the same boot whose pid still has the recorded
  starttime, and resumes the episode from its recorded start (journaled
  `dead_chip_holder_stuck_restored`), so neither the fence nor the 120 s clock restarts; an
  unreadable record restores nothing, and a recorded start that is missing, not finite or out of
  range restores the holders with the episode starting now (logged; the budget stays bounded and
  startup goes on). On top of I21, the broker's
  `reenumerate_fence` withholds every rung that re-enumerates PCI: the bridge reset (the chip stays
  isolated, its bridge reason `stuck_holder`), the TRAY_DOWN prelude rescan (it runs once the holder is gone) and
  the back-to-back sweep (an incomplete sweep, so the ladder does not climb on it, I22). Each refusal
  is logged and journaled `reenumerate_withheld` (`host_at_risk`) once per reason. The fence scans
  at most once per sampler tick: within the tick, rungs reuse its reason while the registry is
  unchanged and each holder it named is still stuck in the kernel (a /proc stat read), so a holder
  that exits or leaves the kernel lifts the fence at once. The whole-tray
  power-cycle hand-off is not withheld: it re-powers nothing itself and its cycle clears the holder.
  Escalation is I70's, with the budget `DEAD_CHIP_STUCK_BUDGET_SEC`, 120 s, while a dead-chip
  holder is among the unkillable leftovers (a job's leftover alone keeps 600 s): the holder already
  outlived its kill and the 30 s release wait, a re-enumeration under it can take the host down, and
  120 s is the kernel's own hung-task bound; the slot cannot heal before the holder is gone. Past
  the budget the host rung is requested on I70's terms (the power cycle when a chip is off the bus,
  as here once the chip is removed; at most one 60 s retry tick; every guard, the I65 cooldown
  included, holds and retries). The rung's running-job guard does not count a RUNNING job the
  broker itself killed for recovery (`reset_killed_job_ids`, which the dead-chip path's victim
  joins): its process is the stuck holder, so it stays RUNNING for as long as the host lives and
  would hold the rung forever; any other RUNNING job still holds it. This holds across repeated
  restarts: the victim's scope stays active while its process is stuck, and once the first restart
  has used the job's device-fault record (01 I6) a later one re-adopts the scope as RUNNING with no
  in-memory kill marker. A re-adopted job whose scope's live processes are all registered stuck
  holders (pid and starttime) rejoins `reset_killed_job_ids` (journaled
  `scope_readopted_dead_chip_victim`); a scope with any other live process, or none, is a running
  job and still holds the rung. `/health` `leftover_fence` carries the class, `budget_sec` 120 and
  its deadline. Nothing on the path releases the device: the boot runs the startup gate, which waits
  for the 1G hugepage pool and passes the full health check, fabric included, before any release
  (03 I4); a failed check keeps the hold and climbs the bounded ladder. Every new wait is bounded:
  2 s per maps read, the 120 s budget, the 60 s retry. Time to recover for this class: before,
  either a bridge reset or rescan re-enumerated PCI under the stuck reader (a host-stall risk) or the
  hold stayed open until an operator rebooted; after, the 30 s release wait, 120 s, at most one
  sampler tick, then the power cycle (about 5-8 min) and the startup check (about 3 min): about
  10-13 min, later only by the bounded I65 cooldown. The same 120 s holds for a stuck holder in its
  job's scope or named a job's survivor (before: that job's 600 s leftover budget), for one whose
  killed job is still RUNNING (before: unbounded, the running-job guard held the rung), and across a
  broker restart (before: the registry and the clock were lost, so the fence lifted and a restart
  delayed or dropped the request), and across a second restart that re-adopts the victim's scope
  (before: unbounded, the re-adopted RUNNING victim held the rung).

- **I97 — Before the power cycle, a mesh that has every chip back gets one mesh reset; tt-smi's driver
  wait gets one SBR; a driver wait that gave up is a failed reset.** Seen on a Galaxy (class B1): a
  mesh reset ran with one chip isolated whose link needed a Secondary Bus Reset to retrain after the
  re-power. The rescans of I81 and I78 never brought it back, so tt-smi gave up after its 100 s driver
  wait (exit 1, no POST_RESET, no board re-init) and left every chip it had found ARC-not-ready, while
  the reset's line said "no POST_RESET failure". The SBR owed before the power cycle (I89) then brought
  the chip back, all chips were on the bus, and the full check failed on ARC-not-ready. The ladder
  requested the power cycle with no reset of the now-whole mesh, and its reason still named the old
  off-bus count. Four parts:

  (a) One mesh reset of the whole mesh before the power cycle. When the SBR before the host rung (the
  one a hold owes, and the idle escalation's SBR of a sweep whose mesh reset already failed on the
  same chips, I87) has every chip back on the bus (none off the bus, isolated or removed), and either
  the heartbeat reads chips ARC-not-ready (`arc_not_ready_chips`: missing from the read, their
  endpoint's config still readable) or the full check's heartbeat probe failed, the broker runs one
  `tt-smi -glx_reset` of the whole mesh, then a 10 s settle and the full health check, fabric
  included, every cabled link. The reset goes through the funnel every mesh reset takes: the reset
  gate (I24), the outage hold (I28), none over a tenant or an unreadable holder scan, none while a
  reset scope still cycles the chips. Only that check passing lifts the hold, through I32: a lift
  the funnel refuses for want of a fabric pass stays held (`host_rung_sweep_unverified`), with no
  power cycle on a check that passed; the next escalation pass, or the bound's own, runs no second
  (a), and when its check fails it requests the power cycle by the bound. When that check passes and
  the funnel refuses the lift again, the hold stays fabric-unverified and I32's fabric relift owns it,
  as after the last-chance sweep before a host rung. A refused reset or a failed check requests the
  power cycle in the same pass. It runs once per hold episode, on Galaxy only, and never for a
  whole tray off (A) or all chips off (C), which go straight to the power cycle (I37). Journaled
  `whole_mesh_reset_before_host_rung` (`fired` with the ARC-not-ready chips and the hold's age, or
  `fired=false` with the skip `reason`), and `whole_mesh_reset_before_host_rung_unverified` when the
  check fails or is skipped (with its `reason`).

  (b) One SBR in tt-smi's driver wait. When `DRIVER_WAIT_SBR_AFTER_TRIES` (4) of I78's rescans have
  left an isolated chip missing, the rescan loop fires one Secondary Bus Reset through each such
  chip's recorded bridge (`reset_removed_chip`, as in I69: no rescan of its own, the loop's next
  rescan adds the chip back), waits the link-retrain settle, and the rescans go on to their tries.
  Once per reset, only with the SBR capability (I17), never after the reset ended. Each SBR owes the
  fabric pass and the AICLK ceiling like every bridge reset, and is journaled `reset_driver_wait_sbr`
  with the chip and `ok`. A write still in flight when tt-smi exits finishes before the reset's
  closing rescan, and no further rescan runs in the wait.

  (c) A driver wait that gave up is a failed reset. When tt-smi prints that it did not find all the
  chips and is giving up, the reset's one line (spec 03 I83) reads `mesh reset FAILED (rc N): tt-smi
  gave up at its driver wait ...` instead of "no POST_RESET failure", journaled
  `reset_driver_wait_gave_up` with the exit code, and the mechanism keeps
  `last_reset_driver_wait_failed` set until the next mesh reset starts. The line and the journal say
  whether tt-smi's tray re-power ran (its IPMI UBB reset, the 30 s settle or the driver wait in its
  output; the IPMI call alone does not count when tt-smi reported it failed in both of its modes and
  exited before the settle): `the tray re-power ran, but tt-smi gave up ...` and `tray_repower` on
  `reset_driver_wait_gave_up` and on every `-glx_reset`'s `reset_done`. A mesh reset that exits non-zero
  with no re-power in its output reset nothing: its line reads `mesh reset FAILED (rc N): reset did not
  run` and says why (tt-smi exited before the re-power, or its IPMI re-power failed), journaled
  `reset_did_not_run` (with `ipmi_failed` for the latter), never "no POST_RESET failure". This holds for a
  lone chip and for a partial drop of several alike. Logging only: the ladder treats
  both as the failed reset they are (I36, I38, I87), and the bridge rung's escalation line names the
  power cycle that follows a chip the re-power does not bring back, never "no power-cycle required".

  (d) The power-cycle reason reads the bus as it is now. Unless its caller passed a cause, the host
  rung's reason is rebuilt from a heartbeat read taken when it is requested: `N/M chip(s) off the bus,
  K ARC not ready after the reset ladder`, or the previous text when that read fails. A chip the SBR
  brought back no longer shows as off the bus.

  The bound (I76): one runtime deadline, not a sum of probe timeouts. (a) runs against a hold-age
  deadline, `_mesh_reset_before_host_rung_deadline_sec()`: the stuck-hold ceiling (600 s;
  `TT_DEVICE_MCP_STUCK_HOLD_SEC` only shortens it) less the kill grace
  (`MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC`, 10 s) and the reap (`MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC`,
  5 s), so 585 s. It is a constant in code, never a per-box setting. (a) starts only when a reset that
  gives up at its driver wait (`MESH_RESET_BEFORE_HOST_RUNG_GAVE_UP_SEC`, 300 s; about 263 s seen), or
  one in its budget (`MESH_RESET_BEFORE_HOST_RUNG_SEC`, 60 s; about 35 s seen) with the settle
  (`MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC`, 10 s) and the check's allowance
  (`MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC`, 180 s), still ends before the deadline: a hold age of
  285 s at most. The allowance is the slowest full check with fabric measured on a 32-chip Galaxy
  (119.5 s; p95 106.9 s, median 95.7 s, n=53) plus a margin. It only decides whether a check is worth
  starting; the deadline, not the allowance, ends a check that runs longer. After the reset, a tt-smi
  that gave up at its driver wait again, or less than the settle and the allowance left before the
  deadline, gets no check: the reset's off-bus intent ends and the power cycle is requested at once.
  A check still running at the deadline counts as failed, whatever it would have returned: its
  readers (the fabric pass, the tt-smi snapshot, the AICLK ceiling helper) get SIGTERM and, still
  alive after the grace, SIGKILL, each marked no verdict; the check task is reaped, or cancelled when
  it has not ended within the reap; a reader it started after the cut gets SIGKILL; its result is
  never read. The hold stays and the power cycle is requested at once, by 600 s. This covers every
  term inside the check (each probe's own timeout and kill sequence, the eth reads, I45's
  traffic-only rerun) without counting any of them. Accepted gaps: (1) a reset stuck past 300 s is
  never killed (I4), up to the 600 s reset timeout, as for every mesh-reset rung; a reset started at
  285 s that runs to its timeout requests the power cycle at about 885 s, so class B1 then recovers
  in about 19-20 min, past its 15 min budget. Tightening it would mean killing a reset mid-run, which
  I4 forbids. (2) A check cut at the deadline may have been about to pass; the device is power-cycled
  rather than lifted, never lifted on a late pass. (3) The eth-heartbeat read has no tracked process
  to stop: one still running at the cut ends under its own timeout in the background, and its result
  is ignored. A hold older than 285 s skips (a), journaled with its reason, and requests the power
  cycle at
  once, as before. The boot and post-cycle cooldown (I65) is unchanged. Releases nothing: only a
  just-passed full check, fabric included, lifts (I32). Time to recover, class B1 with a chip that
  needs an SBR to retrain: one episode before, about 14.3 min through the power cycle, inside its
  15 min budget. With (a) and (b), est.: when the driver-wait SBR retrains the chip, the reset
  succeeds and the failed 263 s reset is gone. Otherwise the best case is the climb, then (a)'s reset
  (about 35 s), the 10 s settle and the full check (about 95-120 s), with no power cycle (about
  5-8 min) or startup check (about 3 min). The worst case is the power cycle requested by 10 min plus
  its about 4.3-5 min, within 15 min, apart from the stuck-reset tail above.

- **I98 — A chip SBR'd this hold and still off the bus gets no second SBR round, per chip; every
  bridge reset path records it in the hold's SBR record, a chip seen back on the bus leaves it, and a
  lift clears it.** Seen on a Galaxy (class B1, one chip off the bus): the
  post-job gate, the off-bus escalation and the last-chance sweep each ran their own three-try SBR
  round on the same chip in one hold (about 44 s each), and the sweep logged that no SBR had run on it
  this hold. The record kept only chips the rung gave no reason for, and a chip reset on every try and
  still off the bus is tagged `removed_not_reenumerated` (I69). The power cycle then healed the chip.

  The bridge rung reports every chip a bridge write reached (`last_bridge_reset_fired`), whatever came
  of it. The hold's SBR record (`_sbr_hold`: hold episode, chips, wall time, each chip's back-on-bus
  count) keeps those still off the bus when the path records them, from every path that resets a chip through its bridge: the gate's rung, the off-bus escalation,
  the bound's kept rungs, the last-chance sweep's owed SBR (I87, I89) and the driver-wait SBR (I97 b).
  A chip with no bridge to write to (`no_bridge`) counts too: no retry in the hold changes that. A chip
  the rung declined (gated, no privilege, PCI wedged, a stuck holder) is not recorded. A chip seen back
  on the bus by any path leaves the record, so a later drop in the same hold gets its own round: the
  bridge rung, a rescan (including the driver wait's rescans after its SBR, I97 b, which run after
  the record is written) and the mesh reset all drop the chip's removal record
  (`pci.forget_removed_chip`, or `pci.forget_returned_removed_chips` when a record read finds the chip
  back by an outside path), which bumps its back-on-bus count (`pci.chip_back_seq`); a recorded chip
  whose count moved is no longer spent. The record is keyed by the hold episode: a new hold starts
  empty. A hold lifted on a just-passed full check clears it, so a hold that carries the same episode
  after the lift (I82) also starts empty. One made in the pass that opened the hold,
  before its start was set, counts for a hold that started at most `SBR_RECORD_ADOPT_SEC` (120 s)
  after it.

  Per chip: an SBR call writes no bridge reset to a target that already had its round this hold
  (`_recover_isolated_chips(exclude=...)`, reason `sbr_spent_this_hold`) and journals
  `sbr_skipped_spent` with those chips and the count of the others; the others get their round. A
  spent removed chip that a bridge write reached this hold was reset, so it does not withhold the
  batch rescan (I69); the rescan still runs for the new chips. A spent removed chip that no bridge
  write reached (`no_bridge`) was never reset: the record keeps it apart (`_sbr_hold`'s fifth field,
  `_recover_isolated_chips(not_reset=...)`), and while it is off the bus it withholds every rescan
  of the call, as I69 says, though it still gets no second round. A bridge write that reaches it
  later in the hold moves it to the reset ones. While a spent chip is still off the bus
  the call reports not recovered; when every target is spent, no bridge write runs at all. Either way
  the caller goes on to its next rung (the mesh reset or the power cycle request) as the ladder orders
  it now. The last-chance sweep's owed SBR goes through the same call. The order and the
  rungs are unchanged; no removed rung comes back. Releases nothing: a skipped round is not a recovery,
  and only a just-passed full check, fabric included, every cabled link, lifts (I32). The 10-min ladder
  and the power cycle after it (I76) are unchanged. Time to recover, class B1 with one chip off the bus
  that no SBR brings back: one episode 9.6 min through the power cycle; est. about 1.5 min less (the
  escalation's and the sweep's rounds). A second chip that drops later in the hold, or a chip that
  came back and drops again, gets its own round (about 44 s) instead of going straight to the mesh
  reset or the power cycle.
- **I99 — The OS-first fallback outlasts one unit timeout, and the shutdown it bounded is timed.**
  A clean shutdown (I88) that hits one stuck unit waits that unit's systemd timeout before going on:
  mount units default to `DefaultTimeoutStartSec`, services to `DefaultTimeoutStopSec`, 90 s each
  on a stock host. A fixed 120 s fallback left 30 s for everything else: on a host whose snapd mount
  unmount timed out at 90 s, the power went off 116 s after the request, 4 s inside the bound, so the
  fallback, not the hook, may have cut it before the filesystems were read-only. The bound is
  therefore `SHUTDOWN_CLEAN_SEC` (90 s; a clean reboot of a Galaxy host measured 86 s) + the host's
  unit timeout (the longer of the two defaults, read once per cycle with `systemctl show`, bounded
  like the BMC pre-flight; 90 s when systemd does not answer or a value does not parse, the cap when
  either is `infinity`) + `SHUTDOWN_MARGIN_SEC` (30 s), capped at `SHUTDOWN_BOUND_MAX_SEC` (600 s)
  so a hung shutdown is still cycled within 10 min of the request (I76). It is computed only once
  every check passed, so a cycle that cannot start still runs no step. Why size it rather than arm
  the fallback at `shutdown.target`: a hang before that target (a unit whose stop never returns, a
  job queue that never reaches it) would then have no bound, and the fallback is kept out of the
  shutdown transaction on purpose (`DefaultDependencies=no`) so nothing in it can stop or delay the
  unit. The `reboot_requested` event carries `bound_sec` and `unit_timeout_sec`.

  The reboot request is timed: once `systemctl reboot` is accepted, the broker writes
  `power_cycle_os_first.json` in the health dir (request time, bound, unit timeout, unit), fsynced,
  best effort. On the next boot the I88 confirmation reads and removes it; when the request lies
  between the ledger row and this boot, the row gets `reboot_requested_at`, `shutdown_bound_sec`,
  `host_unit_timeout_sec` and, for a confirmed cycle, `shutdown_sec` (the BMC's SEL power-off minus
  the request; the SEL read that confirmed the cycle is reused, else one bounded read from the
  request on) with `shutdown_within_bound`. A refuted or unconfirmed cycle, or no SEL record, gives
  `shutdown_sec` null with `shutdown_sec_unavailable` saying why. A shutdown within 15 s of its bound
  logs a warning. The confirmed event carries `shutdown_sec` and `shutdown_bound_sec`. Nothing here
  changes what is released or when: the startup full check with fabric still decides (03 I4, I32).
- **I100 — The full ladder runs right before every power cycle on a partial drop, whatever failed
  earlier in the hold.** Seen on a Galaxy: a partial drop whose mesh reset had failed on the same chips
  was power-cycled with no last-chance sweep (I38's skip), and a sweep at the 10-min bound skipped a
  chip's SBR because the chip's round was spent (I98). For every drop that is not class A or C (I37;
  those keep the direct power cycle), the last-chance sweep runs immediately before the broker
  requests the power cycle: readers stopped, the dead-link isolation, the SBR of every isolated or
  removed chip (the hold's SBR record does not limit the sweep's own SBR), the bus re-probe, the mesh
  reset with its POST_RESET and board re-init (repeated when it already failed in this hold,
  `last_chance_sweep_repeats_failed_reset`), then the full check with fabric. A pass releases the
  device through the lift funnel; a failure keeps the hold and requests the power cycle. Where I36,
  I38, I58, I87 or I98 say a failed rung or a spent round skips the sweep or one of its rungs, this
  invariant wins for the sweep before the power cycle. The repeated mesh reset runs at once as the I97
  mesh reset, after the check readers are stopped (I87's host-hang note): under the I97 deadline when
  it and its check still end by it, else under a deadline of the hold's age at the repeat's start plus
  the same allowance (the reset that gives up, 300 s, or the reset, settle and check, 250 s). It never
  waits idle for the 10-min bound, and the I97 reset having run this hold does not stop it. A check
  still running at that deadline is cut as in I97. So the power cycle is requested at most 315 s (the
  allowance, the kill grace and the reap) after the repeat starts. A repeat that did not run (a tenant
  or a reset cycling in its scope when it would start, or a refused reset) is not a failed one: the
  sweep holds (`host_rung_held`), requests no power cycle that pass (I22) and is not spent, so the next
  pass runs it in full. It runs at most once per sweep record (I87): once below the bound and once at
  it, so at most twice per hold episode. An outage hold (I28) still refuses the whole sweep and holds
  for the operator (I22). Worst case from the hold's start to the power cycle request, per class
  (estimates for the steps before the repeat: readers stopped, up to 10 s; isolation and SBR,
  up to about 20 s; the bus re-probe and, once the bus is whole, one full check, up to about 3 min):
  - **B1/B2 partial drop** (the climb's sweep below the bound): the sweep's start plus about 3.5 min
    plus 315 s; with the climb at 5-6 min (after a mesh reset that gave up) that is about 14-15 min,
    and at most about 19 min for a sweep that starts just before the bound.
  - **Hold-deadline expiry at the 10-min bound** (I76): 600 s, the bound's kept rungs and their full
    check (about 3.5 min), then the sweep as above: about 22 min at most. Before this invariant the
    sweep's repeat could wait up to 315 s idle for the bound first.
  Remaining risk: a second mesh reset whose tray re-power hangs the host; the I25 latch records it.

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
  (`bridge_reset`, `smi_reset`, `power_cycle` — `constants.STAGE_NAMES`)
  or a non-stage decision (`BELOW_FLOOR`, `WAIT`, `DEFER`, `RELEASE`, `HOLD_FABRIC_UNVERIFIED`, `BLOCKED`). Pure:
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
- **`Recovery.reset_argv(indices)`** — `TT_DEVICE_MCP_RESET_ARGS` (a full argv override) wins
  (I91 classifies it: guarded when it names the Galaxy flag, refused when it names neither that
  nor tt-smi); otherwise `tt-smi -r <i,j,...>` (per-target) or `tt-smi -glx_reset` (Galaxy, all ASICs, no
  targets).
- **`RecoveryMechanism`** — `reset_with_quiesce(argv, log, owner)` is the one choke point every
  reset in the broker goes through (gate and tool alike, so "safe" cannot drift); `run_scoped`,
  `scope_active`, `await_foreign_scope`; `cooling()`; `auto_recovery_allowed(action,
  tenant_active)`, `record_auto_recovery`, `retract_last_auto_recovery`,
  `read_auto_recovery_ledger`, `boot_from_broker_escalation` (attributes a boot to the escalation
  that caused it only within `BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC`, 900 s — a stale entry must
  not stamp an external reboot as broker recovery); `_fire_recovery_escalation` (the shared
  record-then-fire body of the power cycle).
- **Stage fire functions (the argv seams).** Each stage isolates the destructive action in one
  function the test suite stubs and asserts argv on: `stages/bridge_reset.reset_chip_via_bridge`
  (setpci Secondary Bus Reset on the parent bridge; tri-state result — recovered / retryable /
  structurally inapplicable), `stages/power_cycle._fire_power_cycle` (`ipmitool chassis power cycle`),
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
  anonymous fail-closed over HTTP on a privsep host; legacy skip over HTTP off privsep) → refuse
  `reason: busy` while a broker job owns the device unless forced (I18) → with force, interrupt
  the running job gracefully (scope-routed for privsep and re-adopted jobs; marked reset-killed
  first so its exit does not re-flag the device) → `select_recovery().reset_argv(present chips)` →
  `reset_with_quiesce` under the device-op lock, its one jobs-list row owned by
  `[broker]reset-tool` — the broker performs the reset, so it owns the action row; the requesting
  caller keeps their identity on their own job rows, never on this broker action → on rc 0, verify
  under the same lock (I13: fabric on a mesh, heartbeat+snapshot otherwise) → status
  `reset_complete` / `reset_unhealthy` / `reset_unverified` / `reset_failed` / `refused` /
  `no_devices`, with `health_ok`, `steps`, and the
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
opt-in, two strikes, no live scope, and a mass drop or a still-unhealthy present mesh); a healthy
fault-free mesh → `RELEASE`, except a fabric pass that ran without a verdict on a dirty/multi-chip
host → `HOLD_FABRIC_UNVERIFIED`; eth-frozen → `WAIT`; cooling → `WAIT`; live scope → `DEFER`
(adopt, never start a second); SBR candidates → `bridge_reset`; below the floor → `BELOW_FLOOR` (not a rung, I23);
otherwise `smi_reset`. The platform derives its own floors from `expected` — a caller cannot hand
it stale ones.

**Mechanism execution.** A fired `smi_reset` (or an adopted `DEFER`) goes through
`_reset_and_verify_device`: adopt any foreign scope; warn loudly on an unknown mode (I15); arm the
cooldown clock; `reset_with_quiesce` (I4, I12); exactly one attempt (I3, I11), plus the one
ARC-not-ready retry I11 allows (`_arc_not_ready_retry_due`). On rc 0 the ladder
verifies with `_verify_device_after_reset`: the full probe pass *including* the fabric traffic
pass, where a post-reset "no trained link" (77) is retried after an eth-training sleep
(`POST_RESET_FABRIC_RETRIES` / `_SLEEP_SEC`; a dispatch-cap 77 on a proven fabric re-checks with
no sleep, I84) and a 77 that persists is UNKNOWN — never a
recovery (no hold is cleared onto a fabric no pass ever proved) and never a failed reset (no
cooldown, no two strikes, no host-rung climb): it settles as a 77-only fabric-unverified hold that
spec 03 I40 bounds. The bridge-reset rung
retries up to `BRIDGE_RESET_MAX_TRIES` with a settle (the first shot races link retrain), treats a
structurally inapplicable chip (no bridge, no setpci) as fall-through rather than spin, tries a
bare PCI rescan before anything heavier, and refuses a stale cached address whose target bus now
holds live silicon.

**Below the floor.** `BELOW_FLOOR` is not a rung: there is no tray rung (I23) and no
back-to-back reset sweep. A whole UBB tray off the bus goes to the bounded I27 power cycle at once,
its only wait the I65 cooldown; it runs no reset and holds the device, and the post-boot startup
gate re-proves the mesh. Anything else gets the I89 gate rungs at once (PCI rescan, SBR per its
defaults, a lift only on a passing full check) and otherwise holds; the I76 bound runs the full
ladder at 10 min.
The power cycle honours the same tenant guard and the cooldown/boot-loop denials on every path.

**The tray-down prelude (I19).** Every caller of the ladder (`escalate()`: the gate, the idle
relift, the hold-deadline watchdog) passes through `_tray_down_latch` first, which latches
`TRAY_DOWN` or `PARTIAL` at the first sighting (journalled as `tray_down_latched`, with whether a
reset came first). A `TRAY_DOWN` episode whose prelude has not run goes to `_tray_down_prelude`:
with no tenant it rescans the Tenstorrent root ports (I95); it then captures (`bmc_capture`, all reads
concurrent, each under its own timeout — 8 s for the BMC, which answers slowly while a tray browns
out — and the whole under the 10 s deadline); it waits what is left of
`TRAY_DOWN_RESCAN_SETTLE_SEC` and re-reads the heartbeats. The lspci target is the parent of the
chip's sysfs node, found from the banked PCI address (I16), or, once the node is gone, the bridge
whose secondary bus is the chip's bus, so the link status (LnkSta/LnkCap) is kept even when the
endpoint is gone; the endpoint is read too while it is still enumerated. One `TRAY-DOWN:` error
line names the trays, the chips off, the rescan result and the capture, and says the full ladder
follows; `tray_down_prelude` journals the same. Then `escalate()` sends a class A or C off-bus set
straight to the power cycle (I37, `_power_cycle_direct_from_escalate`); anything else gets the
caller's own ladder with the caller's own stage, indices, evidence and beats, so the generic walk,
the mesh-wide reset, the last-chance sweep and the power cycle run as they would have.

**The last-chance reset sweep gates the power cycle.** A power cycle takes the whole box down and
costs minutes, so before paying that the ladder re-issues every reset type once more, back-to-back,
then takes ONE settle and ONE verify — and fires the power cycle only if the mesh is still bad
(`_settle_and_verify_before_host_rung` → `_issue_all_resets_back_to_back`, the same implementation
the class A 10-min sweep (I73) uses, so the two can never drift). It runs at most once per hold
phase (I87): once below the 10-min bound, and once more as part of the bound's own ladder. The
sweep fires no tray step (I23): its resets are the bridge reset and the mesh reset, and the full
health check, fabric included, still has to pass after the sweep before anything is released. This
holds on every road to the power cycle: the gate ladder's own rung, the idle/stuck-hold escalation
(`_climb_to_host_recovery_after_failed_reset` — the road most measured power cycles actually took)
and the I29 sweep after an unverified reset. Before this, those roads settled and verified but never re-tried the cheaper rungs, so a
present-mesh eth wedge could be power-cycled having only ever seen mesh resets. Details: SBR fires only where a chip is actually isolated, so a mass drop is never
nibbled at with per-chip bridge resets; and the sweep is SKIPPED (the settle and verify still run)
under the two guards a destructive rung never crosses — a tenant on the mesh, or a reset already
cycling in its own scope. A sweep that brings the mesh back cancels the host rung outright.

**A RUNNING broker job is a tenant, whatever the holder scan says.** Every guard that asks "is anyone using this
device?" goes through one helper (`_tenant_active`), and it consults TWO independent sources: the holder scan (open
`/dev/tenstorrent` fds) and the broker's own queue (`deps.job_running`). The scan alone is not evidence of idleness —
a running job spends minutes off the device compiling, loading weights and between opens, and during that window the
scan is empty. Measured on box B 2026-09-17: the stuck-hold escalation read an empty scan 106 s into a running
job, fired a mesh reset, and the job died of SIGPIPE the instant the reset released the device. `force` (the
hold-deadline backstop) still waives an UNREADABLE scan, because that guard protects against blindness, but it never
waives a known tenant — fd-held or queue-known.

**Host rungs and the ledger.** A host rung fires only through `_fire_recovery_escalation`: record
durably and fsync (abort on failure), leave the jobs-list row, then fire; a fire that raises
retracts the record (I9). `auto_recovery_allowed` enforces tenant-absolute, per-severity interval,
and per-boot cap (I7, I9, I10); a denied opted-in rung journals `auto_recovery_denied` once per
hold episode. `boot_from_broker_escalation` back-fills the boot that results; the startup gate
then re-proves the mesh on that boot (spec 03 I4), and a mesh still bad there holds and climbs its
own ladder like any other hold. The boot's jobs-list row is written once per boot, keyed on the kernel boot_id; where that is
unreadable, on `/proc/stat` btime matched within `_BTIME_DEDUP_SLACK_SEC` (10 s), because a
wall-clock step moves btime within one boot and an exact match would count one reboot twice.
Absence is never health: every blocked/exhausted path emits a loud actionable event
(`all-off-bus power-cycle required`,
`reset_unrecoverable_power_cycle_required`) rather than a silent hold.

**The reset gate.** `evaluate_reset_gate` MUST allow only when no foreign-uid holder exists AND the
scan is complete (I6). Carve-outs: holders with uid < `MIN_TENANT_UID` are ignored unless
`is_tenant` counts them (broker scope, `TT_DEVICE_MCP_TENANT_UIDS`); the caller's
own holders are not foreign (resetting your own wedged run is the point); deleted-node fds do not
count. `force` overrides foreign holders and the blind spot but the foreign list is still returned
for logging. Anonymous HTTP callers on a privsep host get the same fail-closed rules with every
tenant foreign; off privsep, HTTP keeps the legacy single-tenant skip.

**The busy check.** After the gate, the reset tool MUST refuse (`reason: busy`) while a broker job
owns the device, whoever owns that job — including the caller: a caller resetting over their own
run kills it with `tt_device_job_kill` first or passes `force`. This is independent of the holder
scan; on a non-privsep host the broker's own job runs at the broker's uid and the gate alone would
never see it (I18).

**Reset verification scope.** Per I13: on a mesh with a fabric check installed, the operator
reset's verify is the ladder's own post-reset verify (eth read + fabric pass, 77-retried), so
`reset_complete` there means the fabric moved traffic. This costs nothing per job; it lengthens the
operator's reset by one fabric pass (about 45-75 s on a healthy mesh), plus the retry sleep and a second pass on a 77.
**Duration bound.** From the moment the reset holds the device-op lock, the call takes at most:
the BMC check a mesh reset starts with (`BMC_PREFLIGHT_TIMEOUT_SEC`, 10 s); the pre-reset holder
clear (in-flight detach `PRE_RESET_DETACH_TIMEOUT_SEC` 30 s + one rescan `PRE_RESET_RESCAN_SETTLE_SEC`
1 s + the wait for a zombie's release `PRE_RESET_ZOMBIE_WAIT_SEC` 30 s (an operator reset's; an automatic one's ends `pre_reset_zombie_ladder_reserve_sec(chips)`, 407 s on a 32-chip Galaxy, before the hold's 10-min ladder mark, so it adds nothing past the mark) + SIGTERM grace
`PRE_RESET_TERM_GRACE_SEC` 10 s + the wait after SIGKILL `HOLDER_RELEASE_WAIT_SEC` 30 s: 101 s); the poller stop and restart (30 s per `systemctl` call, 120 s with the two default
services); the reset (`DEVICE_RESET_TIMEOUT_SEC`, 600 s; a reset still running then is
`reset_failed` with no verify); the post-reset cut of a chip that failed (detach
`INFLIGHT_DETACH_TIMEOUT_SEC` 15 s + one poller stop per service, 60 s + the reader kill
`INFLIGHT_KILL_TIMEOUT_SEC` 30 s: 105 s); the 3 s PCI-rescan settle; the clock-cap re-apply when a
cap is set (one helper run, 8 s, plus its settle retries, at most max(8 s, settle 3 s + 4 s margin):
16 s); and the verify passes. A re-check follows only a 77, so every
pass but the last one finished: up to 361 s (heartbeat settle 0.5 s + snapshot 90 s + eth python
probe 3 × 10 s + eth read 60 s + fabric `FABRIC_CHECK_TIMEOUT_SEC` 180 s). The last pass can time
out and adds the 75 s kill sequence (`GRACEFUL_KILL_GRACE_SEC` + `SIGTERM_GRACE_SEC`): up to
436 s. With `POST_RESET_FABRIC_RETRIES` (1) re-checks, each after `POST_RESET_FABRIC_RETRY_SLEEP_SEC`
(60 s): 1811 s, about 30 minutes, at the defaults. Not counted, each bounded on its own: stopping a
running job first, including a killed re-adopted job still ending its scope (60 s grace, the stop,
then up to `READOPTED_FINALIZE_WAIT_SEC` 15 s for its record); waiting for a broker operation
already holding the lock (that operation's own bound); the `/proc` holder scans and the cut's
heartbeat read and reader scan (one pass each, no wait); and the PCI-rescan and PCI-remove writes
themselves (the remove within its detach deadline above). The bound also assumes a killed process
exits. Typical on a healthy Galaxy:
about 2 minutes, about 4 with a 77 retry. The tool's docstring states this bound and a test
recomputes it from the timeouts. The MCP tool sends nothing until it returns, which is safe through
the stdio shim: its 300 s read timeout bounds the gap between reads, and the SSE keepalives fill
it (spec 02).
Before this, the tool released a mesh on heartbeat + snapshot — the pair that scores an eth/fabric
wedge as fine — and the stream also retired the runtime's fault report on that blind verdict. On a
single chip, or a mesh with no fabric check, the verify stays one `fsm.observe(run_fabric=False)`
pass: `reset_complete` there says the chips are back and ticking, not that the fabric is proven.
Over-enumeration against a stale degraded baseline is a recovery, not a failure. The stream's verify
runs under the device-op lock and streams its progress lines, so the CLI's per-read timeout never
sees a silent fabric pass.

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
- **No per-tray re-power.** tt-smi offers no per-tray reset, and the broker's own BMC tray
  re-power (`ipmitool raw 0x30 0x8b`) was removed: across Galaxy hosts it ran 190 times with 0 heals
  and 4 times broke the box (other trays dropped with it, host storage went down). A below-floor
  drop the bridge reset cannot fix now holds or goes to the power cycle (I23), which is the step
  that heals a whole-tray drop.
- **Floors before resets.** Both floors exist because the heavy rungs are measured hazards, not
  hypothetical ones: a galaxy reset against a single-chip wedge took the other 31 chips to
  all-ones until a power cycle; a warm Galaxy reboot does not re-power the UBBs, so dropped ASICs
  stay dropped across it. The gentle-first order (rescan → bridge → mesh → reboot → power
  cycle) is ordered by blast radius, and each hazard has a rung below it that avoids it.
- **Two named below-floor branches, split at fire not in the router.** The one drop the data says no single
  reset recovers — a whole tray off the bus with no bridge window on any chip (0/40 episodes) —
  earns a back-to-back reset sweep straight to the power cycle, where every other drop holds for the
  I76 ladder or, for a whole tray, takes the I27 power cycle. The class is decided from evidence the gate *already has* (the per-chip
  SBR `no_bridge` result the bridge rung produced, threaded on `Evidence.bridge_reset_failed`), not
  recomputed in `_route`: the router stays pure, and the destructive branch fires only on a *proven*
  no-window whole-tray drop — a chip whose window was never tested reads unknown and takes the
  generic class, so the sweep can never fire on a guess. This is why the evidence is threaded through
  `replace(ev, …)` rather than re-derived: `_route` must not grow a side effect, and the fire must
  not re-probe a bus the gate just read.

## Test anchors

| Claim | Test(s) |
|---|---|
| I22 a host rung holds when the last-chance sweep was skipped or gated; it fires after a full sweep | `tests/test_ladder_v2.py::test_settle_and_verify_gates_the_power_cycle_host_rung` |
| I23 no BMC tray re-power: no shipped file builds a tray power command, the tray rung module is gone, and no switch or hook for it is left | `tests/test_no_tray_repower.py::test_no_shipped_file_builds_a_tray_power_command`, `::test_the_tray_rung_module_is_gone`, `::test_no_switch_or_hook_for_the_tray_rung_is_left` |
| I25/I27 a below-floor drop with a whole tray off is never re-powered: the post-job gate and the idle escalation hand it to the power cycle at once and keep the device held; a partial drop still walks and is released only after the full check, fabric included | `tests/test_offbus_power_cycle.py::test_the_post_job_gate_hands_a_whole_tray_drop_to_the_power_cycle` |
| I27 a whole tray that came back in part is not cycled at grace 0 and stays held, once per episode (a flapping tray fires on the next whole-tray read); one still wholly off, or a re-read the map cannot place, fires at once | `tests/test_offbus_power_cycle.py::test_a_whole_tray_that_comes_back_in_part_is_not_cycled_at_grace_0`, `::test_a_flapping_whole_tray_still_reaches_the_power_cycle`, `::test_the_whole_tray_deferral_is_per_episode`, `::test_a_tray_still_wholly_off_at_the_re_read_still_fires_at_once`, `::test_a_re_read_the_tray_map_cannot_place_keeps_the_whole_tray_verdict` |
| I27 with a power cycle configured, a whole tray down goes straight to it; a partial drop gets one lifted ladder pass (the mesh reset) past the grace, spent only when its climb reaches the rungs (a pass declined by scope, tenant or the ceiling stays due), then the power cycle on the next forced pass only if the hold stands; each reset releases only after the full check, fabric included, and the cycle releases nothing | `tests/test_gated_hold_handoff.py::test_a_whole_tray_down_goes_straight_to_the_power_cycle`, `::test_a_partial_drop_gets_the_mesh_reset_first_and_the_power_cycle_only_if_it_fails`, `::test_a_partial_drop_the_mesh_reset_heals_never_gets_the_power_cycle`, `::test_a_partial_drop_with_no_tray_map_gets_the_mesh_reset_first`, `::test_before_the_grace_a_partial_drop_gets_no_lifted_reset`, `::test_a_declined_lifted_pass_is_not_spent_and_the_mesh_reset_still_runs_first`, `::test_the_partial_drops_mesh_reset_releases_only_after_a_passing_fabric_check`, `::test_the_power_cycle_releases_nothing`, `tests/test_startup_fabric_hugepages.py::test_startup_fabric_pass_waits_for_hugepages_then_runs` |
| I84 a dispatch-cap 77 after a reset re-checks at once (the full check, fabric included); any other 77 waits for eth training; a persisting one stays held | `tests/test_post_reset_gate.py::test_a_dispatch_cap_77_after_a_reset_rechecks_at_once_and_class_j_fits_its_budget`, `::test_a_no_trained_link_77_after_a_reset_still_waits_for_eth_training`, `::test_a_dispatch_cap_77_that_persists_never_releases`, `::test_the_wrapper_reason_names_the_dispatch_cap_over_a_validator_error_line` |
| I24 `hold` counts an all-ones chip still in sysfs as off the bus; the mesh reset and the sweep refuse over it | `tests/test_pcie_guard.py::test_chips_off_bus_counts_an_all_ones_chip_still_in_sysfs`, `::test_a_hold_gate_refuses_the_mesh_reset_and_the_sweep_over_an_all_ones_chip` |
| I24 ENODATA on a bound endpoint is ARC-not-ready, not off the bus, only with a live link and a config vendor read of 0x1e52; 0xffff or a down link is off the bus and a whole tray of it is class A; an unreadable config is held, never ARC-not-ready, never lifts; healthy chips get no config read; an unbound endpoint, a missing node or another errno is off | `tests/test_arc_not_ready.py::test_enodata_on_31_bound_chips_is_not_off_the_bus`, `tests/test_arc_not_ready.py::test_an_unbound_or_missing_chip_still_counts_off_the_bus`, `::test_enodata_with_vendor_ffff_is_off_the_bus`, `::test_enodata_with_the_link_down_is_off_the_bus`, `::test_enodata_with_config_unreadable_is_held_and_named`, `::test_a_healthy_read_makes_no_config_read`, `tests/test_hostpci.py::test_config_vendor_state_reads_the_first_two_bytes`, `tests/test_classac_power_cycle_direct.py::test_an_enodata_tray_reading_ffff_is_class_a`, `::test_an_enodata_tray_with_unreadable_config_never_lifts` |
| I24 the per-chip bridge SBR obeys the hold gate and the latch | `tests/test_pcie_guard.py::test_the_per_chip_bridge_reset_obeys_the_hold_gate_and_the_latch` |
| I25 the last-chance sweep names each off-bus chip, its PCI address and tray, read fresh from the heartbeats | `tests/test_ubb_tray_map.py::test_the_off_bus_placement_marks_what_the_map_cannot_place` |
| I25 the host-hang latch: intent, latch on a new boot, not on the same boot, clear, opt-out | `tests/test_pcie_guard.py::test_an_off_bus_reset_writes_an_intent_and_removes_it_when_done`, `::test_an_intent_from_a_boot_that_died_latches_hold_until_cleared`, `::test_an_intent_from_this_boot_does_not_latch`, `::test_the_latch_can_be_switched_off` |
| I25 the broker's own reboot or power cycle never latches: the intent is removed and synced before it fires; the next boot drops an intent older than its ledger row, leaves glx_reset ungated and still runs the full startup check after hugepages; an intent with no later host action, or one whose row did not bring this boot up (boot outside the attribution window, btime unreadable, a missed request past its pending window), still latches; a late cycle (I72) inside its window does not | `tests/test_pcie_guard.py::test_a_sweep_that_fails_its_verify_leaves_no_intent_for_the_power_cycle`, `::test_a_broker_host_action_closes_an_open_intent_durably_before_it_fires`, `::test_an_intent_older_than_the_brokers_own_cycle_does_not_latch`, `::test_an_intent_with_no_later_host_action_still_latches`, `::test_a_host_action_that_did_not_bring_this_boot_up_still_latches`, `::test_the_brokers_cycle_that_brought_this_boot_up_does_not_latch`, `tests/test_startup_fabric_hugepages.py::test_the_boot_after_the_brokers_own_power_cycle_latches_nothing_and_still_runs_the_full_check` |
| I24 gate modes: off by default, hold on an off-bus chip, guard on a flood | `tests/test_pcie_guard.py::test_gate_is_off_by_default`, `::test_hold_refuses_a_reset_over_chips_already_off_the_bus`, `::test_guard_refuses_during_an_aer_flood`, `::test_first_look_after_a_recent_boot_counts_errors_since_boot`, `::test_hold_gate_stops_an_automatic_mesh_reset_over_an_off_bus_chip`, `::test_guard_masks_the_root_ports_around_an_automatic_mesh_reset` |
| I23 an off-bus chip's remembered root port is masked (tray fire and mesh reset); a port gone from sysfs is not | `tests/test_pcie_guard.py::test_guard_masks_an_off_bus_chips_port_around_a_mesh_reset`, `::test_a_remembered_port_gone_from_sysfs_is_not_masked` |
| I23 the topology is recorded at broker start and in each between-jobs check, rewritten only on change; a port never seen is not known | `tests/test_pcie_guard.py::test_an_unchanged_topology_is_not_rewritten` |
| I23 unreadable sysfs refuses the tray fire (all-off included) and a look keeps the recorded topology | `tests/test_pcie_guard.py::test_an_unreadable_look_keeps_the_recorded_topology`, `::test_an_unreadable_sysfs_counts_every_chip_off_so_a_hold_gate_holds` |
| I23 a 5+ digit PCI domain parses; an unrelated entry sysfs cannot read is skipped; a `guard` mask over unreadable sysfs logs `aer_mask_failed` | `tests/test_pcie_guard.py::test_a_5_digit_domain_beside_the_mesh_does_not_make_sysfs_unreadable`, `::test_a_chip_in_a_5_digit_domain_is_parsed_and_counted`, `::test_an_unrelated_entry_the_scan_cannot_read_is_skipped`, `::test_an_unreadable_sysfs_under_guard_logs_a_mask_failure` |
| I24 the flood is a rate; the flood window refuses even under `off`; a refused reset is not recorded | `tests/test_pcie_guard.py::test_a_steady_trickle_between_distant_looks_is_not_a_flood`, `::test_a_trickle_since_an_old_boot_is_not_a_flood`, `::test_after_a_kept_masked_port_the_stuck_hold_mesh_reset_does_not_fire`, `::test_a_gated_mesh_reset_records_no_reset_and_arms_no_cooldown` |
| I25 the clear command finds the system broker's latch; one it cannot remove is reported | `tests/test_pcie_guard.py::test_the_clear_command_finds_the_system_brokers_latch`, `::test_a_latch_that_cannot_be_removed_is_reported_not_called_absent` |
| I25 the latch is scoped to its boot: a later boot ignores and drops it, and it never holds the I27 power cycle | `tests/test_pcie_guard.py::test_a_latch_holds_only_on_the_boot_it_was_written_in`, `::test_a_latch_from_an_ended_boot_never_gates_even_if_the_broker_never_restarted`, `tests/test_offbus_power_cycle.py::test_a_latch_never_blocks_a_rung_after_its_boot_nor_the_power_cycle_on_it` |
| I25 the latch is bounded: it lifts with every chip on the bus (a link that reads down is not) and host-pci OK, or at its ceiling with every chip on the bus; with a chip off or sysfs unreadable only at its ceiling with no power cycle configured (paged once, the gate falls back to its mode); one it cannot remove keeps holding; the hold names both boots | `tests/test_pcie_guard.py::test_the_latch_lifts_once_every_chip_is_back_and_host_pci_is_ok`, `::test_the_latch_never_lifts_while_a_chip_is_off_the_bus`, `::test_the_latch_never_lifts_while_a_listed_link_reads_down`, `::test_with_no_power_cycle_a_latch_with_a_chip_off_lifts_at_its_ceiling`, `::test_with_no_power_cycle_the_gate_itself_lifts_a_latch_past_its_ceiling`, `::test_with_a_power_cycle_a_latch_with_a_chip_off_still_holds_past_its_ceiling`, `::test_the_hold_message_names_both_boots`, `::test_the_latch_lifts_at_its_ceiling_with_every_chip_on_the_bus`, `::test_the_latch_ceiling_defaults_and_rejects_nonsense`, `::test_a_latch_that_cannot_be_removed_keeps_holding`, `::test_the_between_jobs_check_lifts_the_latch_once_the_mesh_is_back` |
| I17 a rung the process cannot execute reads OFF (power cycle, bridge) | `tests/test_rung_privilege.py::test_an_unreachable_bmc_has_no_power_cycle_rung`, `::test_a_non_root_daemon_has_no_bridge_rung` |
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
| I3 cooldown arming rules | `tests/test_device_safety.py::test_a_timed_out_reset_does_not_arm_the_cooldown`, `tests/test_device_safety.py::test_a_reset_that_exits_nonzero_still_arms_the_cooldown`, `tests/test_device_safety.py::test_an_adopted_foreign_reset_that_failed_arms_the_cooldown`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_within_the_reset_cooldown`, `tests/test_reset.py::test_a_failed_operator_reset_arms_the_cooldown_and_the_two_strikes`, `tests/test_reset.py::test_an_operator_reset_that_did_not_finish_leaves_the_cooldown_alone`, `tests/test_hold_bound.py::test_a_failed_resets_cooldown_never_delays_the_ladder_at_the_bound`, `tests/test_reset.py::test_a_failed_operator_mesh_reset_spends_the_episodes_mesh_reset`, `tests/test_reset.py::test_a_failed_operator_chip_reset_does_not_spend_the_mesh_reset` |
| I3 the cooldown never delays a class A/C power cycle; other sets wait | `tests/test_classac_power_cycle_direct.py::test_a_whole_tray_in_a_failed_resets_cooldown_cycles_on_the_next_gate_pass`, `tests/test_classac_power_cycle_direct.py::test_all_off_after_a_failed_operator_mesh_reset_cycles_in_its_cooldown`, `tests/test_classac_power_cycle_direct.py::test_any_other_set_still_waits_out_the_cooldown`, `tests/test_classac_power_cycle_direct.py::test_an_idle_off_bus_hold_in_a_failed_resets_cooldown_cycles`, `tests/test_hold_bound.py::test_a_failed_resets_cooldown_never_delays_the_ladder_at_the_bound` |
| I4 restart-safe scope/local child; overrun waited out; timeout leaves reset | `tests/test_device_safety.py::test_reset_runs_in_its_own_systemd_scope`, `tests/test_reset.py::test_a_reset_scope_argv_is_scoped_and_outlives_us`, `tests/test_reset.py::test_a_reset_without_systemd_runs_detached_and_returns_its_output`, `tests/test_reset.py::test_a_daemon_without_systemd_resets_through_the_local_backend`, `tests/test_reset.py::test_cancelling_the_waiter_does_not_kill_a_local_reset`, `tests/test_reset.py::test_a_reset_that_overruns_is_waited_out_not_failed`, `tests/test_reset.py::test_a_reset_that_never_ends_is_still_a_failure`, `tests/test_device_safety.py::test_reset_timeout_leaves_the_scope_running`, `tests/test_device_safety.py::test_the_reset_without_systemd_runs_bare_never_through_systemd_run` |
| I5 adopt, never race, a live scope or local lock | `tests/test_reset.py::test_a_restarted_daemon_adopts_the_local_reset_lock`, `tests/test_device_safety.py::test_bridge_reset_is_skipped_while_a_reset_scope_is_in_flight`, `tests/test_device_safety.py::test_stuck_hold_climb_defers_when_a_reset_is_still_cycling`, `tests/test_device_safety.py::test_stuck_offbus_hold_skips_the_surgical_reset_when_a_scope_opened` |
| I6 reset gate: foreign deny, incomplete fail-closed, force, carve-outs | `tests/test_reset_gate.py::TestResetGateDeny::test_denies_on_foreign_holder`, `tests/test_reset_gate.py::TestResetGateIncompleteScan::test_incomplete_scan_fails_closed_when_no_visible_foreign`, `tests/test_reset_gate.py::TestResetGateForce::test_force_overrides_foreign`, `tests/test_reset_gate.py::TestResetGateSystemHolders::test_allows_over_system_holder`, `tests/test_reset_gate.py::TestForeignHolders::test_ignores_system_holders`, `tests/test_reset_gate.py::TestResetGateAllow::test_allows_when_only_own_holders` |
| I18 reset refuses (busy) over a running or held job; force stops it (re-adopted included); idle proceeds | `tests/test_reset.py::test_reset_refuses_while_a_job_is_running_and_leaves_it_alone`, `tests/test_reset.py::test_reset_refuses_while_a_hung_job_is_still_held`, `tests/test_reset.py::test_reset_refuses_over_a_readopted_running_job`, `tests/test_reset.py::test_queued_jobs_do_not_block_a_reset`, `tests/test_reset.py::test_forced_reset_stops_the_running_job_and_logs_it`, `tests/test_reset.py::test_forced_reset_stops_a_readopted_job`, `tests/test_reset.py::test_streaming_forced_reset_stops_a_readopted_job`, `tests/test_reset.py::test_forced_reset_stops_a_readopted_job_seen_only_by_the_recheck`, `tests/test_reset.py::test_reset_refuses_while_a_stopped_readopted_job_is_still_being_torn_down`, `tests/test_reset.py::test_forced_reset_waits_for_the_readopted_job_to_be_finalized`, `tests/test_reset.py::test_a_readopted_job_killed_by_a_forced_reset_does_not_flag_the_device`, `tests/test_reset.py::test_idle_reset_proceeds_without_a_busy_refusal`, `tests/test_reset.py::test_streaming_reset_refuses_while_a_job_is_running`, `tests/test_reset.py::test_the_health_gate_reset_is_not_blocked_by_the_busy_check` |
| Anonymous (HTTP/privsep) gate fail-closed | `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_over_a_tenant_holder`, `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_when_scan_incomplete`, `tests/test_reset.py::test_privsep_http_reset_refuses_over_a_foreign_holder`, `tests/test_reset.py::test_non_privsep_http_reset_keeps_the_legacy_skip` |
| Holder scan mechanics (deleted fd, permission gap) | `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_detects_self_holding_a_device_node`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_deleted_node_fd_is_not_a_holder`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_permission_denied_marks_scan_incomplete` |
| I6 driver record: a free device scans complete without privilege | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_free_device_scans_complete_without_privilege` |
| I6 driver record: holders attributed, deduped across devices, junk ignored | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_driver_named_holder_is_attributed_to_its_uid`, `::test_a_process_holding_two_devices_is_listed_once`, `::test_junk_lines_are_ignored` |
| I6 driver record fails closed: unattributable pid, foreign pid namespace, unreadable file | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_pid_we_cannot_see_is_unattributed_not_absent`, `::test_a_pid_that_holds_no_device_here_is_not_attributed`, `::test_an_unreadable_device_file_marks_the_scan_incomplete` |
| I6 driver record absent or wholly unreadable falls back to the walk | `tests/test_device_holders.py::TestDriverHolderRecord::test_an_absent_driver_dir_falls_back_to_the_walk`, `::test_a_driver_dir_with_no_devices_falls_back_to_the_walk`, `::test_wholly_unreadable_records_fall_back_rather_than_report_blind` |
| I6 driver record against real hardware, unprivileged | `tests/test_device_hardware.py::test_the_driver_holder_record_answers_completely_without_root`, `::test_a_free_device_passes_the_reset_gate_without_root` |
| I7 tenant blocks automatic action | `tests/test_device_safety.py::test_governor_never_reboots_over_a_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_under_a_foreign_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_when_the_holder_scan_is_incomplete` |
| I7 a deferral names its blocker (job, tenant, or incomplete scan with its gaps); the forced pass emits none | `tests/test_device_safety.py::test_stuck_hold_deferral_names_an_incomplete_scan_and_its_gaps`, `::test_stuck_hold_deferral_names_the_tenant_holder`, `::test_stuck_hold_deferral_names_a_running_broker_job`, `::test_forced_stuck_hold_waives_an_incomplete_scan_and_emits_no_deferral`, `::test_stuck_offbus_hold_deferral_names_an_incomplete_scan`, `tests/test_device_holders.py::test_holder_comm_and_state_read_unknown_for_a_gone_pid` |
| I6 a broker-scoped holder or a `TT_DEVICE_MCP_TENANT_UIDS` uid is a tenant below the uid floor, in the gate, reclaim, the ladder and dispatch | `tests/test_tenant_scope.py::test_the_driver_record_marks_a_scoped_holder`, `::test_the_reset_gate_refuses_over_a_scoped_service_account_holder`, `::test_the_reset_gate_refuses_over_a_configured_tenant_uid`, `::test_reclaim_signals_a_scoped_service_account_holder`, `::test_the_ladder_counts_a_scoped_straggler_of_a_killed_job_as_a_tenant`, `::test_dispatch_waits_for_a_scoped_service_account_holder`, `::test_tenant_uids_env_never_makes_root_a_tenant`, `::test_an_unresolved_tenant_name_is_retried_not_cached` |
| Reclaim removes the tenant without relaxing I6/I7 | `tests/test_device_holders.py::test_reclaim_signals_only_tenant_holders`, `tests/test_device_holders.py::test_reclaim_escalates_term_to_kill`, `tests/test_device_holders.py::test_reclaim_reports_a_survivor_rather_than_claiming_success`, `tests/test_device_holders.py::test_reclaim_carries_the_rescan_completeness` |
| Reclaim never retargets onto a holder that appears after it began | `tests/test_device_holders.py::test_reclaim_never_retargets_onto_a_new_holder_that_appears_after_it_began` |
| Reclaim self-exclusion (never signals itself or its own process group) | `tests/test_device_holders.py::test_reclaim_never_signals_itself_or_its_own_process_group` |
| Reclaim identity is pid+starttime, not pid alone (a reused pid is never escalated onto) | `tests/test_device_holders.py::test_reclaim_never_escalates_onto_a_pid_reused_by_an_unrelated_process` |
| Reclaim never signals a target whose starttime was unreadable at selection | `tests/test_device_holders.py::test_reclaim_never_signals_a_target_whose_starttime_was_unreadable_at_selection` |
| Reclaim revalidates identity immediately before the FIRST signal too, not only between rounds | `tests/test_device_holders.py::test_reclaim_revalidates_identity_immediately_before_the_first_signal_too` |
| Reclaim never reports a permission-denied pid as signalled | `tests/test_device_holders.py::test_reclaim_does_not_report_a_permission_denied_pid_as_signalled` |
| I8 galaxy floor holds below / resets at floor; keys on frozen chips too | `tests/test_device_safety.py::test_galaxy_reset_floor_holds_a_single_chip_wedge`, `tests/test_device_safety.py::test_galaxy_reset_floor_holds_just_below_the_floor`, `tests/test_device_safety.py::test_galaxy_reset_floor_resets_exactly_at_the_floor`, `tests/test_device_safety.py::test_galaxy_reset_floor_still_resets_a_mass_drop`, `tests/test_device_safety.py::test_cascade_router_keys_the_galaxy_floor_on_frozen_chips_too`, `tests/test_device_safety.py::test_a_reset_frac_above_one_fails_safe_to_the_default_floor_not_disabled` |
| I9 ledger durable-before-fire, abort on unwritable, fail-closed read, retract | `tests/test_device_safety.py::test_auto_power_cycle_records_durably_before_it_fires`, `tests/test_device_safety.py::test_auto_power_cycle_aborts_when_the_ledger_cannot_be_written`, `tests/test_device_safety.py::test_governor_fails_closed_when_the_ledger_is_unreadable`, `tests/test_device_safety.py::test_governor_blocks_a_reboot_inside_the_min_interval`, `tests/test_device_safety.py::test_record_auto_recovery_round_trips_through_the_durable_ledger`, `tests/test_device_safety.py::test_ledger_skips_a_corrupt_line_without_losing_the_rest`, `tests/test_device_safety.py::test_a_power_cycle_that_fails_to_launch_retracts_its_ledger_entry` |
| I9 a power cycle a later boot verified healthy does not gate the next; an unverified one still does; the backstop refuses and pages at the cap, for a stated time asked again at its end; a NaN, infinite or future stamp never feeds it; a host boot this boot verified does not gate | `tests/test_power_cycle_verified_interval.py::test_a_verified_cycle_does_not_gate_a_new_drop`, `::test_an_unverified_cycle_inside_the_interval_is_still_refused`, `::test_a_stamp_that_cannot_prove_a_later_boot_keeps_the_guard`, `::test_the_backstop_refuses_and_pages_once_at_the_cap`, `::test_the_backstop_is_a_stated_bounded_wait_retried_at_its_end`, `::test_a_corrupt_cycle_stamp_never_feeds_the_backstop`, `::test_a_host_boot_this_boot_verified_does_not_gate_a_new_drop`, `::test_the_gate_stamps_only_a_release_whose_fabric_passed`, `::test_a_relift_stamps_only_on_a_passing_fabric_check` |
| I9 G8 replay: a new episode after a verified cycle takes one power cycle at once; a host death is never a cycle; the cycle it takes still gates until verified | `tests/test_power_cycle_new_episode_g8.py::test_g8_a_cycle_a_later_boot_verified_does_not_hold_off_the_next_episodes_cycle`, `::test_g8_a_host_death_is_never_counted_as_a_cycle`, `::test_g8_the_loop_bound_stands_for_an_unverified_cycle`, `::test_g8_the_ladder_takes_the_power_cycle_instead_of_holding_for_the_interval` |
| I10 per-severity interval | `tests/test_device_safety.py::test_power_cycle_after_a_recent_reboot_waits_out_the_cooldown`, `tests/test_device_safety.py::test_power_cycle_does_not_escalate_past_a_recent_power_cycle`, `tests/test_device_safety.py::test_reboot_does_not_de_escalate_past_a_recent_power_cycle` |
| I10 a power cycle refused by its cooldown (or also by the per-boot cap) is retried as that wait ends, once per refusal, and releases only after a passing fabric check | `tests/test_power_cycle_interval_retry.py::test_a_cycle_refused_by_its_interval_is_due_again_at_expiry`, `::test_the_interval_is_still_enforced`, `::test_no_retry_for_a_reboot_or_outside_the_refused_episode`, `::test_a_cycle_the_per_boot_cap_also_refuses_is_still_asked_again_at_its_end`, `::test_the_watchdog_forces_one_pass_at_expiry_not_at_the_next_window`, `::test_the_retried_pass_releases_only_after_a_passing_fabric_check` |
| I10 a verified cycle does not block the reboot; the reboot keeps its own guards; past the backstop it blocks again | `tests/test_power_cycle_verified_interval.py::test_a_verified_cycle_does_not_block_the_reboot_rung`, `::test_a_reboot_after_a_verified_cycle_keeps_its_own_guards_and_hands_off_to_the_cycle`, `::test_past_the_backstop_a_verified_cycle_gates_the_reboot_again`, `::test_a_stamp_that_cannot_prove_a_later_boot_keeps_the_guard` |
| I10 a power cycle the cooldown, the per-boot cap or the backstop refuses is asked again when that wait ends | `tests/test_power_cycle_interval_retry.py::test_a_cycle_the_per_boot_cap_also_refuses_is_still_asked_again_at_its_end`, `tests/test_power_cycle_verified_interval.py::test_the_backstop_is_a_stated_bounded_wait_retried_at_its_end`, `tests/test_power_cycle_host_rung.py::test_a_cooldown_held_cycle_fires_at_the_cooldown_end_with_the_real_governor` |
| I11 host rung only after exhausted reset (two strikes); once-per-episode latch | `tests/test_device_safety.py::test_cascade_router_escalates_to_the_host_rung_only_once_reset_is_exhausted`, `::test_stuck_hold_escalation_retries_on_the_grace_cadence`, `::test_escalate_forced_suffix_bypasses_the_retry_pacing`, `tests/test_arc_not_ready.py::test_the_retry_is_bounded_to_one_and_a_second_failure_keeps_the_hold`, `::test_no_retry_outside_the_bus_back_arc_not_ready_idle_shape`, `tests/test_post_reset_gate.py::test_the_arc_not_ready_retry_reset`, `::test_the_arc_not_ready_retry_never_releases_on_fewer_links_than_the_cabling`, `tests/test_arc_not_ready.py::test_a_glx_reset_leaving_arc_not_ready_gets_one_more_reset_then_the_full_check`, `::test_a_retry_whose_fabric_fails_is_not_a_recovery` |
| I12 quiesce + in-flight flag + restore rules | `tests/test_reset.py::test_streaming_reset_quiesces_pollers_and_flags_in_flight`, `::test_reset_quiesce_restores_pollers_even_if_the_rescan_is_cancelled`, `::test_reset_quiesce_leaves_pollers_off_when_the_reset_times_out`, `::test_reset_quiesce_restores_pollers_when_a_failed_launch_leaves_no_scope`, `tests/test_device_safety.py::test_no_clock_cap_keeper_in_the_default_pollers`, `::test_the_specs_name_the_default_pollers`, `::test_pollers_quiesce_skips_a_default_poller_the_host_does_not_have`, `::test_pollers_quiesce_stops_and_restarts_an_opted_in_fmax_cap`, `tests/test_startup_fabric_hugepages.py::test_the_startup_fabric_check_runs_with_the_pollers_stopped`, `::test_the_startup_quiesce_restarts_the_pollers_when_the_gate_raises`, `::test_a_reset_inside_the_startup_check_defers_its_poller_restart`, `::test_the_startup_quiesce_leaves_the_pollers_off_while_a_reset_cycles`, `tests/test_device_safety.py::test_a_failed_reset_on_a_held_mesh_leaves_the_pollers_stopped`, `::test_deferred_pollers_start_once_the_device_is_verified_healthy`, `::test_the_startup_quiesce_keeps_the_pollers_stopped_when_the_gate_holds`, `::test_a_dead_chip_stop_owes_the_pollers_a_restart_on_the_next_close`, `::test_owing_the_pollers_inside_a_wider_quiesce_leaves_their_restart_to_its_owner`, `::test_a_wider_quiesce_left_stopped_while_a_reset_cycles_owes_the_pollers`, `::test_owing_the_pollers_outside_a_quiesce_starts_them_on_the_next_close`, `::test_owing_the_pollers_with_no_running_loop_does_not_raise`, `tests/test_no_duplicate_defs.py::test_no_module_defines_the_same_name_twice`, `tests/test_post_reset_gate.py::test_an_operator_reset_starts_the_pollers_only_once_its_fabric_verify_passes`, `::test_an_operator_reset_whose_fabric_stays_unverified_keeps_the_pollers_stopped`, `tests/test_device_safety.py::test_pollers_restore_leaves_a_poller_stopped_that_was_stopped_before`, `::test_pollers_restore_starts_nothing_when_every_poller_was_already_stopped`, `::test_pollers_restore_covers_a_poller_an_earlier_stop_took_down`, `::test_pollers_restore_counts_a_crash_looping_poller_as_running`, `::test_pollers_restore_covers_a_poller_whose_stop_outlasts_its_bound`, `::test_pollers_restore_covers_a_poller_whose_stop_fails`, `::test_pollers_restore_counts_a_poller_whose_state_cannot_be_read_as_running`, `::test_pollers_restore_retries_a_start_that_failed`, `::test_pollers_restore_record_survives_a_broker_restart` |
| I13 mesh reset verify = fabric pass (pass/fail/77), single chip and no-fabric-check light | `tests/test_reset.py::test_an_operator_reset_on_a_mesh_releases_only_on_a_fabric_pass`, `tests/test_reset.py::test_an_operator_reset_whose_fabric_fails_is_not_released`, `tests/test_reset.py::test_an_operator_reset_whose_fabric_cannot_verify_holds_fabric_unverified`, `tests/test_reset.py::test_a_single_chip_operator_reset_keeps_the_light_verify`, `tests/test_reset.py::test_a_mesh_with_no_fabric_check_keeps_the_light_verify`, `tests/test_reset.py::test_the_light_verify_checks_the_hosts_chip_count_not_the_survivors`, `tests/test_reset.py::test_a_blind_stream_verify_does_not_clear_the_reported_fault`, `tests/test_reset.py::test_the_stream_settles_an_operator_reset_like_the_tool` |
| I13 failed operator-reset verify is `operator_reset_unhealthy`; duration bound documented | `tests/test_reset.py::test_an_operator_reset_whose_fabric_fails_is_not_released`, `tests/test_reset.py::test_the_stream_settles_an_operator_reset_like_the_tool`, `tests/test_reset.py::test_the_reset_tools_documented_duration_matches_its_timeouts` |
| I13 light verify; unhealthy downgrade | `tests/test_reset.py::test_reset_tool_reports_health`, `tests/test_reset.py::test_verify_health_fails_on_short_chip_count`, `tests/test_reset.py::test_verify_health_fails_on_wedged_arc`, `tests/test_reset.py::test_verify_health_passes_on_over_count_from_stale_expected` |
| I20 the AICLK ceiling is re-applied after the rescan, before the pollers return, re-read within a bounded settle window | `tests/test_aiclk_ceiling.py::test_reset_reapplies_after_the_rescan_before_the_pollers_return`, `tests/test_aiclk_ceiling.py::test_reset_failed_rung_leaves_the_ceiling_owed`, `tests/test_aiclk_ceiling.py::test_reset_apply_error_never_breaks_the_rung`, `tests/test_aiclk_ceiling.py::test_reset_unconfigured_runs_no_ceiling`, `tests/test_aiclk_ceiling.py::test_reset_reapply_reads_back_within_the_settle_window`, `tests/test_aiclk_ceiling.py::test_post_reset_readback_that_settles_in_the_window_is_verified`, `tests/test_aiclk_ceiling.py::test_post_reset_readback_high_for_the_whole_window_is_unverified_once`, `tests/test_aiclk_ceiling.py::test_post_reset_settle_wait_is_bounded`, `tests/test_aiclk_ceiling.py::test_settle_never_reruns_a_verified_timed_out_or_unsupported_helper`, `tests/test_aiclk_ceiling.py::test_without_settle_a_high_readback_is_reported_at_once`, `tests/test_aiclk_ceiling.py::test_settle_window_config`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_realistic_run_is_well_inside_the_budget`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_hung_first_run_stops_at_the_helper_timeout`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_retry_hung_near_the_window_end_stays_in_budget`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_worst_case_is_under_the_budget` |
| I21 every reset is refused over a reaped job's leftover, forced or not, through `reset_with_quiesce` too; it lifts once the holder is gone | `tests/test_reaped_leftover.py::test_the_reset_tool_is_refused_even_with_force`, `tests/test_reaped_leftover.py::test_the_streaming_reset_is_refused_even_with_force`, `tests/test_reaped_leftover.py::test_reset_with_quiesce_is_refused_through_the_fence`, `tests/test_reaped_leftover.py::test_the_broker_wires_the_fence_into_the_recovery_mechanism` |
| I70 an unkillable leftover asks for no host rung before its 600 s budget; past it, the power cycle whether every chip is present or one is off the bus | `tests/test_unkillable_leftover_escalation.py::test_no_rung_before_the_budget`, `tests/test_unkillable_leftover_escalation.py::test_a_power_cycle_once_past_the_budget_with_every_chip_present`, `tests/test_unkillable_leftover_escalation.py::test_a_power_cycle_with_chips_off_the_bus` |
| I70 the clock opens on an unkillable leftover only, closes when none is left (a leftover gone at the deadline fires nothing), and a scan error leaves it as it is; the idle-holder scan keeps it | `tests/test_unkillable_leftover_escalation.py::test_a_killable_leftover_starts_no_clock`, `tests/test_unkillable_leftover_escalation.py::test_a_leftover_gone_at_the_deadline_fires_nothing_and_closes_the_episode`, `tests/test_unkillable_leftover_escalation.py::test_a_scan_error_leaves_the_clock_as_it_is`, `tests/test_unkillable_leftover_escalation.py::test_the_idle_holder_scan_keeps_the_clock` |
| I70 guards: a governor/cooldown denial holds and the next try fires; the operator stand-back holds the power cycle and a later try fires it once the marker is gone; a pending host action or a device op asks nothing; another tenant or a running job blocks, the leftover itself does not; a leftover that leaves while the try waits on the lock, or a leftover leaving, a tenant arriving or a reset scope starting during the stand-back or governor check, fires nothing (`gone` closes the episode); no host rung opted in journals `no_host_rung` | `tests/test_unkillable_leftover_escalation.py::test_a_governor_denial_holds_then_fires_on_a_later_try`, `tests/test_unkillable_leftover_escalation.py::test_a_held_stage_is_journaled_once_while_it_stands`, `tests/test_unkillable_leftover_escalation.py::test_the_operator_stand_back_holds_the_power_cycle_until_it_is_gone`, `tests/test_unkillable_leftover_escalation.py::test_a_leftover_that_leaves_while_the_try_waits_on_the_lock_fires_nothing`, `tests/test_unkillable_leftover_escalation.py::test_a_change_during_the_last_checks_holds_the_rung`, `tests/test_unkillable_leftover_escalation.py::test_a_pending_host_action_asks_for_nothing`, `tests/test_unkillable_leftover_escalation.py::test_a_device_op_in_flight_defers_the_try`, `tests/test_unkillable_leftover_escalation.py::test_another_tenant_blocks_the_rung_but_the_leftover_itself_does_not`, `tests/test_unkillable_leftover_escalation.py::test_a_running_job_blocks_the_rung`, `tests/test_unkillable_leftover_escalation.py::test_no_host_rung_opted_in_journals_no_host_rung` |
| I76 with spec 03 I83: one in-flight PCI writer; the first timeout or refusal latches PCI-wedged, stops the pass, skips later PCI rungs and the valve, asks the power cycle at once (cooldown permitting), and no hold lifts while latched without a just-passed full check | `tests/test_pci_wedged_latch.py::test_a_blocked_bridge_reset_stops_the_pass_after_one_call`, `tests/test_pci_wedged_latch.py::test_b_a_later_rescan_behind_a_blocked_write_fails_fast_and_starts_no_thread`, `tests/test_pci_wedged_latch.py::test_c_the_valve_skips_a_dead_chip_while_pci_wedged`, `tests/test_pci_wedged_latch.py::test_d_the_latch_asks_for_the_power_cycle_at_once_outside_the_cooldown`, `tests/test_pci_wedged_latch.py::test_d_inside_the_cooldown_the_latch_asks_when_it_ends`, `tests/test_pci_wedged_latch.py::test_e_no_hold_lifts_while_pci_wedged_without_a_just_passed_full_check`, `tests/test_below_floor_offbus_ladder.py::test_a_remove_blocked_in_the_kernel_still_lets_the_bound_reach_the_power_cycle`, `tests/test_device_safety.py::test_an_isolated_chip_rung_blocked_in_the_kernel_fails_in_bounded_time` |
| I76 with spec 03 I83: a power-cycle request asked with no ladder run names the skip and its cause (the PCI-wedge latch, leftover chips, a refused kill), never a failed ladder | `tests/test_pci_wedged_latch.py::test_the_wedged_power_cycle_request_names_the_wedge_not_a_failed_ladder`, `tests/test_pci_wedged_latch.py::test_a_skipped_ladder_is_named_in_the_power_cycle_reason` |
| I70/I76 a killed job's leftover past 600 s and the 10-min hold bound request one power cycle per episode, in either order, with a leftover try spawned before the bound's request, and whether the launch ran or timed out; the device stays held with the full check owed | `tests/test_unkillable_leftover_escalation.py::test_replay_the_bound_and_the_leftover_budget_request_one_power_cycle` |
| I70 `/health` `leftover_fence`, the sampler wiring, and the bounded time to request (at most 660 s) with nothing on the path releasing the device | `tests/test_unkillable_leftover_escalation.py::test_the_health_fields`, `tests/test_unkillable_leftover_escalation.py::test_the_sampler_drives_the_escalation`, `tests/test_unkillable_leftover_escalation.py::test_time_to_request_is_bounded_and_nothing_releases_the_device` |
| I77 detection: D or SIGKILL pending with the node, a node map or a BAR map; not stuck, stuck on nothing of ours, zombie or gone is not the class; unreadable fails closed; the maps read is bounded | `tests/test_dead_chip_stuck_holder.py::test_refs_name_device_fds_node_maps_and_bar_maps`, `tests/test_dead_chip_stuck_holder.py::test_stuck_device_holder`, `tests/test_dead_chip_stuck_holder.py::test_a_gone_pid_is_not_stuck`, `tests/test_dead_chip_stuck_holder.py::test_a_maps_read_that_blocks_is_bounded_and_fds_still_count` |
| I77 one maps reader thread per pid; an unanswered maps read with no device fd fails closed; the fence scans once per sampler tick and a holder that exits or leaves the kernel lifts it within the tick; every member of a killed process group is checked | `tests/test_dead_chip_stuck_holder.py::test_one_outstanding_maps_reader_per_pid`, `tests/test_dead_chip_stuck_holder.py::test_an_unanswered_maps_read_fails_closed_when_the_fds_name_nothing`, `tests/test_dead_chip_stuck_holder.py::test_the_reenumeration_fence_scans_once_per_tick`, `tests/test_dead_chip_stuck_holder.py::test_a_holder_that_leaves_the_kernel_lifts_the_fence_within_the_tick`, `tests/test_dead_chip_stuck_holder.py::test_every_member_of_a_killed_process_group_is_checked` |
| I77 journaled host_at_risk and paged once (a dropped page retried), the I70 episode opens with the class and 120 s in `/health`; a holder that exited or is not stuck names nothing; BARs read before the removal | `tests/test_dead_chip_stuck_holder.py::test_a_stuck_holder_is_journaled_host_at_risk_paged_once_and_opens_the_episode`, `tests/test_dead_chip_stuck_holder.py::test_a_dropped_page_is_sent_on_the_next_scan`, `tests/test_dead_chip_stuck_holder.py::test_a_killed_holder_that_exited_or_is_not_stuck_names_nothing`, `tests/test_dead_chip_stuck_holder.py::test_the_bars_are_read_at_registration_not_after_the_removal` |
| I77 the re-enumeration fence withholds the bridge reset, the prelude rescan and the sweep (incomplete), not the whole-tray power-cycle hand-off; `reset_with_quiesce` is fenced; a released or reused pid lifts it | `tests/test_dead_chip_stuck_holder.py::test_the_broker_wires_both_fences`, `tests/test_dead_chip_stuck_holder.py::test_the_bridge_rung_is_withheld`, `tests/test_dead_chip_stuck_holder.py::test_the_tray_down_rescan_is_withheld`, `tests/test_dead_chip_stuck_holder.py::test_the_back_to_back_sweep_is_withheld_and_reports_incomplete`, `tests/test_dead_chip_stuck_holder.py::test_a_whole_tray_still_goes_to_the_power_cycle_hand_off`, `tests/test_dead_chip_stuck_holder.py::test_reset_with_quiesce_is_fenced_until_the_holder_is_gone`, `tests/test_dead_chip_stuck_holder.py::test_a_released_holder_is_pruned_and_lifts_the_fence`, `tests/test_dead_chip_stuck_holder.py::test_a_reused_pid_is_not_the_stuck_holder` |
| I77 no rung before 120 s, then the power cycle with the chip off the bus (the I70 fresh-scan check counts a stuck holder the holder scan misses, and one that exits before the fire fires nothing); the I65 cooldown holds and the rung fires once it ends; time to request 120 s with nothing releasing the device; a job leftover alone keeps 600 s; the dead-chip path calls it | `tests/test_dead_chip_stuck_holder.py::test_no_rung_before_120s_then_a_power_cycle_with_the_chip_off_the_bus`, `tests/test_dead_chip_stuck_holder.py::test_the_i65_cooldown_holds_the_rung_and_it_fires_once_the_cooldown_ends`, `tests/test_dead_chip_stuck_holder.py::test_time_to_request_is_bounded_and_nothing_releases_the_device`, `tests/test_dead_chip_stuck_holder.py::test_a_job_leftover_alone_keeps_the_600s_budget`, `tests/test_dead_chip_stuck_holder.py::test_the_dead_chip_path_notes_stuck_holders_and_says_the_host_is_still_at_risk`, `tests/test_dead_chip_stuck_holder.py::test_the_rung_counts_a_stuck_holder_the_scan_misses_and_fires_nothing_once_it_left` |
| I77 a stuck holder that is also a job's leftover (job scope or survivor) keeps the dead-chip label and 120 s; the dead-chip victim job still RUNNING does not hold the rung, any other running job does | `tests/test_dead_chip_stuck_holder.py::test_a_dead_chip_holder_that_is_also_a_job_leftover_keeps_the_dead_chip_label`, `tests/test_dead_chip_stuck_holder.py::test_the_dead_chip_victim_job_still_running_does_not_hold_the_rung`, `tests/test_dead_chip_stuck_holder.py::test_a_live_job_the_broker_did_not_kill_still_holds_the_rung` |
| I77 the registry is recorded and removed once empty; a restart reloads it first, keeps the fence and resumes the 120 s clock; another boot, a reused pid, no starttime or an unreadable record restores nothing | `tests/test_dead_chip_stuck_holder.py::test_the_registry_is_recorded_and_removed_once_the_holder_is_gone`, `tests/test_dead_chip_stuck_holder.py::test_a_restart_keeps_the_fence_and_resumes_the_120s_clock`, `tests/test_dead_chip_stuck_holder.py::test_a_restart_drops_holders_that_are_not_the_recorded_process`, `tests/test_dead_chip_stuck_holder.py::test_an_unreadable_record_restores_nothing`, `tests/test_dead_chip_stuck_holder.py::test_the_broker_start_restores_before_anything_else` |
| I77 the record's snapshot and write are serialised; a bad recorded start restores the holders with the clock from now and never aborts startup; across repeated restarts the re-adopted victim (all its scope's live pids registered stuck holders) does not hold the rung, the request comes by the original start + 120 s + one tick (at once if past), and a re-adopted job with its own live process still holds it; boot_broker wires the pending endpoint removal into both fences | `tests/test_dead_chip_stuck_holder.py::test_a_concurrent_save_never_puts_back_a_registry_a_later_one_emptied`, `tests/test_dead_chip_stuck_holder.py::test_a_bad_episode_start_restores_the_holders_with_the_clock_from_now`, `tests/test_dead_chip_stuck_holder.py::test_a_bad_episode_start_does_not_abort_the_broker_start`, `tests/test_dead_chip_stuck_holder.py::test_a_second_restart_readopts_the_victim_and_the_rung_still_fires`, `tests/test_dead_chip_stuck_holder.py::test_a_scope_with_any_other_live_process_is_not_the_victim`, `tests/test_dead_chip_stuck_holder.py::test_the_broker_wires_the_isolation_wait_into_both_fences` |
| I14 the host rung is the power cycle or a loud hold; blocked climbs are loud | `tests/test_device_safety.py::test_host_escalation_for_drop_is_the_power_cycle_or_a_loud_hold`, `tests/test_device_safety.py::test_gate_all_off_bus_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_reset_regression_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_all_off_bus_power_cycles_when_opted_in` |
| Cold rung fireable-or-off (ipmitool) | `tests/test_device_safety.py::test_a_host_without_ipmitool_serves_with_the_cold_rung_off`, `tests/test_device_safety.py::test_ipmitool_present_leaves_the_cold_rung_armed` |
| I26 pre-power-cycle hook: opt-in, runs between record and fire, bounded, never gates the cycle | `tests/test_pre_power_cycle_hook.py::test_unset_hook_runs_nothing_and_the_cycle_is_unchanged`, `tests/test_pre_power_cycle_hook.py::test_the_hook_runs_after_the_record_and_before_the_fire`, `tests/test_pre_power_cycle_hook.py::test_a_failing_hook_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_a_hook_that_cannot_run_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_a_hung_hook_is_killed_with_its_children_and_the_cycle_goes_ahead`, `tests/test_pre_power_cycle_hook.py::test_a_background_child_holding_output_does_not_stretch_the_wait`, `tests/test_pre_power_cycle_hook.py::test_the_hook_never_runs_when_the_ledger_aborts_the_cycle`, `tests/test_pre_power_cycle_hook.py::test_a_hook_that_raises_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_cancelled_while_waiting_on_the_hook_retracts_the_ledger_entry`, `tests/test_pre_power_cycle_hook.py::test_the_timeout_is_always_finite_and_positive` |
| I27 the gated off-bus hold: safe rungs first, grace, idle and nothing standing back, then one power cycle per interval with the attempt on disk first; otherwise hold or page once | `tests/test_offbus_power_cycle.py::test_a_gate_that_allows_resets_hands_back_to_the_ladder`, `tests/test_offbus_power_cycle.py::test_no_power_cycle_before_the_safe_rungs_ran`, `tests/test_offbus_power_cycle.py::test_an_unset_command_pages_once_and_the_hold_stands`, `tests/test_offbus_power_cycle.py::test_within_the_grace_it_holds`, `tests/test_offbus_power_cycle.py::test_past_the_grace_and_idle_it_fires_once_with_the_attempt_on_disk_first`, `tests/test_offbus_power_cycle.py::test_the_ladder_then_the_power_cycle_through_the_escalation`, `tests/test_offbus_power_cycle.py::test_a_tenant_or_an_unreadable_scan_holds`, `tests/test_offbus_power_cycle.py::test_a_reset_in_its_own_scope_holds`, `tests/test_offbus_power_cycle.py::test_anything_objecting_stands_back`, `tests/test_offbus_power_cycle.py::test_checks_that_say_no_let_it_fire`, `tests/test_offbus_power_cycle.py::test_an_attempt_within_the_interval_pages_instead`, `tests/test_offbus_power_cycle.py::test_an_attempt_older_than_the_interval_does_not_limit`, `tests/test_offbus_power_cycle.py::test_the_mechanism_limits_still_apply`, `tests/test_offbus_power_cycle.py::test_an_unwritable_attempt_record_never_fires`, `tests/test_offbus_power_cycle.py::test_a_failed_fire_keeps_its_error_for_the_next_page`, `tests/test_offbus_power_cycle.py::test_fire_command_never_runs_the_command_under_a_live_os`, `tests/test_offbus_power_cycle.py::test_due_for_the_watchdog_once_per_episode`, `tests/test_offbus_power_cycle.py::test_the_hold_watchdog_forces_one_pass_at_the_grace`, `tests/test_offbus_power_cycle.py::test_the_knobs_default_and_reject_nonsense`, `tests/test_offbus_power_cycle.py::test_a_corrupt_state_file_reads_empty_and_a_bad_stamp_counts_as_now` |
| I27 an attempt a later boot verified healthy does not count toward the off-bus interval; one it cannot prove healed still does | `tests/test_power_cycle_new_episode_g8.py::test_g8_the_off_bus_hold_interval_skips_a_cycle_a_later_boot_verified`, `::test_last_attempt_within_skips_only_what_healed_says` |
| I27 bounded: a blocked cycle pages once (high severity) past grace + the bound; a page the hook drops is retried | `tests/test_offbus_power_cycle.py::test_a_blocked_power_cycle_pages_once_past_its_bound`, `tests/test_offbus_power_cycle.py::test_a_page_the_alert_hook_drops_is_sent_again`, `tests/test_offbus_power_cycle.py::test_the_blocked_page_bound_defaults_and_rejects_nonsense` |
| I27 the manual-recovery guard fails closed: no check configured never fires (pages once), the installed template is the default, a check that is unreadable, raises or times out stands back, a clean check lets it fire, and the tenant is re-read after the checks | `tests/test_offbus_power_cycle.py::test_no_manual_recovery_check_never_fires_and_pages_once`, `tests/test_offbus_power_cycle.py::test_the_installed_check_is_the_default`, `tests/test_offbus_power_cycle.py::test_a_manual_recovery_check_that_cannot_answer_holds_until_the_bound`, `tests/test_offbus_power_cycle.py::test_checks_that_say_no_let_it_fire`, `tests/test_offbus_power_cycle.py::test_a_tenant_arriving_during_the_checks_holds`, `tests/test_offbus_power_cycle.py::test_the_pages_name_the_host` |
| I27 the manual-recovery check template: operator power actions count, read-only BMC queries do not, an unreadable journal or auth.log cannot answer | `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_template`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_on_an_unreadable_journal`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_when_auth_log_fails_to_read`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_never_globs_an_auth_log_line`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_on_an_unreadable_auth_log`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_reads_auth_log_within_the_window` |
| I28 an outage hold or `TT_DEVICE_MCP_AUTO_MESH_RESET=0` holds the gate, relift, watchdog and sweep mesh reset, also with a chip off the bus in the startup and pre-job gates, and pages again while it stands, and the boot inventory names the outage hold; one host rung off alone, or `=1`, still resets | `tests/test_mesh_reset_hold.py::test_the_hold_reason_follows_the_flags`, `::test_the_gate_holds_the_mesh_reset_under_an_outage_hold_and_pages_once`, `::test_the_gate_holds_with_the_explicit_knob_alone`, `::test_the_gate_still_resets_with_one_host_rung_off`, `::test_an_outage_hold_logs_the_held_mesh_reset_at_boot`, `::test_an_explicit_opt_in_re_arms_the_mesh_reset_under_an_outage_hold`, `::test_the_last_chance_sweep_is_refused_under_an_outage_hold`, `::test_a_dropped_page_is_sent_again_and_a_lifted_hold_re_arms_it`, `::test_the_gate_holds_with_a_chip_off_the_bus_under_an_outage_hold`, `::test_without_the_hold_a_chip_off_the_bus_still_gets_the_mesh_reset`, `::test_a_standing_hold_pages_again_after_the_repage_interval`, `::test_the_repage_knob_defaults_and_rejects_nonsense` |
| I29 one unverified reset per 77-only episode; next escalation sweeps once, strict on 77, climbs on a measured failure; a sweep still reading 77 holds and pages once for a known no-reset cause, else climbs to the host rung once per episode (ledger-latched across the boot) and then hands off to an operator with a deadline, never releasing | `tests/test_device_safety.py::test_a_reset_that_leaves_the_fabric_unverified_counts_toward_the_relift_bound`, `tests/test_device_safety.py::test_a_second_escalation_after_an_unverified_reset_sweeps_instead_of_resetting`, `tests/test_device_safety.py::test_an_incomplete_sweep_after_an_unverified_reset_does_not_climb`, `tests/test_device_safety.py::test_a_sweep_verify_that_reads_77_is_not_a_release`, `tests/test_device_safety.py::test_a_sweep_that_still_reads_77_holds_and_pages_once_instead_of_climbing`, `tests/test_device_safety.py::test_a_sweep_still_reading_77_with_no_known_cause_climbs_to_the_host_rung_once`, `tests/test_device_safety.py::test_a_77_host_rung_that_could_not_run_hands_off_and_retries`, `tests/test_device_safety.py::test_a_boot_the_77_host_rung_brought_up_hands_off_instead_of_climbing_again`, `tests/test_device_safety.py::test_an_owed_pass_77_reaches_the_host_rung_after_the_sweep_not_at_the_ceiling`, `tests/test_device_safety.py::test_a_77_host_rung_that_cannot_run_pages_once_and_a_later_rung_clears_the_hand_off`, `tests/test_device_safety.py::test_the_host_rung_climb_reports_fired_deferred_or_cannot_run`, `tests/test_startup_fabric_hugepages.py::test_the_first_job_hardware_fault_is_no_77_no_reset_cause`, `tests/test_startup_fabric_hugepages.py::test_a_latched_hold_whose_77_names_the_mismatch_is_no_77_no_reset_cause`, `tests/test_device_safety.py::test_a_latched_hold_whose_sweep_still_reads_a_mismatch_77_climbs_to_the_host_rung` |
| I79 an owed-pass 77 climbs at the sweep's end, not at the ceiling; the I40 bound is reached and the I48 backoff uses its first step; a first-time 77 keeps both; a present mesh's failed reset sweeps then climbs; a regressed off-bus hold sweeps then climbs before the ceiling; never released without a passing full check | `tests/test_device_safety.py::test_an_owed_pass_77_reaches_the_host_rung_after_the_sweep_not_at_the_ceiling`, `tests/test_device_safety.py::test_an_owed_77_hold_skips_the_i40_retries_and_the_i48_backoff_and_a_first_time_77_keeps_them`, `tests/test_device_safety.py::test_present_mesh_failed_reset_sweeps_then_climbs_without_the_ceiling_wait`, `tests/test_device_safety.py::test_present_mesh_failed_reset_whose_sweep_recovers_does_not_climb`, `tests/test_device_safety.py::test_a_regressed_offbus_hold_sweeps_then_power_cycles_before_the_ceiling`, `tests/test_device_safety.py::test_a_regressed_offbus_hold_whose_sweep_recovers_does_not_power_cycle` |
| I79 a cooldown-denied cold rung is retried on each re-check, pages no one, shows its deadline on /health, fires one power cycle once allowed and never again; a cold rung denied for any other reason pages once naming the broker as owner |, `tests/test_device_safety.py::test_a_cooldown_denied_cold_rung_is_retried_on_each_re_check_and_fires_one_power_cycle`, `tests/test_device_safety.py::test_a_cold_rung_denied_for_a_reason_other_than_the_cooldown_pages_once` |
| I79 a 77 climb the I65 cooldown denied below the bound fires at the cooldown's end through the watchdog retry; a 77 hold whose reset has not run is still never forced below the bound; relift and watchdog together ask for one power cycle | `tests/test_hold_bound.py::test_a_77_climb_the_cooldown_denied_below_the_bound_fires_at_the_cooldowns_end`, `::test_a_77_hold_whose_reset_has_not_run_is_still_never_forced_below_the_bound`, `::test_t2_a_cooldown_denied_77_climb_fires_one_power_cycle_whichever_path_runs_first` |
| I79 with I76: a 77 climb that fired before the bound is not driven again at the bound; a 77 on the boot a 77 power cycle brought up is handed to an operator and still gets the next power cycle at the bound | `tests/test_hold_bound.py::test_t1_a_77_climb_that_fired_before_the_bound_is_not_driven_again_at_the_bound`, `::test_t3_a_77_on_the_boot_a_77_power_cycle_brought_up_still_gets_the_next_cycle_at_the_bound` |
| I79 with I31/I22 on a host with no bridge: a partial off-bus hold whose one mesh reset regressed it does not defer the power cycle to the ceiling; the SBR (inapplicable, no bridge), the sweep's one mesh reset and the full check with fabric run once, then one power cycle; a passing full check is the only release; a cycle held off below the ceiling runs no sweep and keeps the hold, and the 10-min pass sweeps then cycles | `tests/test_b1_regressed_no_bridge.py::test_no_bridge_regressed_hold_power_cycles_on_the_first_pass_after_the_kept_rungs`, `::test_no_bridge_regressed_hold_released_only_by_a_passing_full_check`, `::test_no_bridge_regressed_hold_with_the_cycle_held_off_is_asked_again_at_the_ten_minute_mark` |
| I34 the idle relift runs no check while a host power cycle is pending | `tests/test_hold_bound.py::test_the_idle_relift_runs_no_check_while_a_host_power_cycle_is_pending` |
| I30 deadline escalation holds a fabric-only present mesh without a reset; real faults keep the ladder | `tests/test_device_safety.py::test_the_deadline_escalation_holds_a_fabric_only_present_mesh_without_a_reset`, `tests/test_device_safety.py::test_the_deadline_escalation_keeps_the_ladder_for_real_faults`, `tests/test_device_safety.py::test_the_deadline_escalation_holds_a_known_fabric_fault_without_a_reset`, `tests/test_device_safety.py::test_the_deadline_escalation_climbs_a_present_mesh_eth_frozen_self_heal_hold` |
| I30 a dispatch-failure hold is never classed a topology mismatch | `tests/test_dispatch_failure_reset.py::test_a_dispatch_failure_hold_is_not_a_topology_mismatch` |
| I30 a lost-link hold or a hold under the spec 03 I42 latch is never a no-reset hold; the forced window runs the ladder | `tests/test_fabric_links_missing.py::test_a_links_missing_hold_is_not_a_no_reset_topology_hold`, `tests/test_fabric_links_missing.py::test_a_latched_hold_no_relift_reset_takes_the_ladder_at_the_ceiling` |
| I28 a uid-0 broker poller never blocks the reset; pollers stop and restart around it | `tests/test_dispatch_failure_reset.py::test_a_uid0_telemetry_holder_does_not_block_the_reset` |
| I31 a regressed galaxy reset is not repeated in its hold episode; climb or page instead | `tests/test_device_safety.py::test_forced_offbus_ladder_does_not_repeat_a_regressed_galaxy_reset`, `tests/test_device_safety.py::test_forced_offbus_ladder_after_a_regression_sweeps_then_power_cycles_when_opted_in`, `tests/test_device_safety.py::test_an_early_regression_is_latched_before_the_ceiling_deferral`, `tests/test_device_safety.py::test_a_new_hold_episode_resets_again_after_a_regression`, `tests/test_device_safety.py::test_gate_skips_a_repeat_galaxy_reset_after_a_regression`, `tests/test_device_safety.py::test_gate_latches_a_regression_seen_before_any_hold` |
| I32 every reset path releases only after a passing fabric pass: the debt settles only on a healthy verdict, the funnel holds fabric-unverified, exemptions | `tests/test_post_reset_gate.py::test_the_debt_settles_only_on_a_healthy_fabric_verdict`, `::test_a_single_chip_or_a_host_with_no_fabric_check_is_exempt`, `::test_a_verified_release_after_a_reset_holds_fabric_unverified`, `::test_no_debt_releases_as_before` |
| I32 per path: operator reset, bridge rung, no-window sweep, idle off-bus bridge, adopted foreign scope, foreign-holder fast verify, broker start, tray-down prelude rescan | `tests/test_post_reset_gate.py::test_an_operator_reset_without_a_passing_fabric_pass_holds`, `::test_an_operator_reset_with_a_passing_fabric_pass_releases`, `::test_the_bridge_rung`, `::test_the_tray_down_no_window_sweep`, `::test_the_idle_off_bus_bridge_reset`, `::test_a_reset_adopted_from_a_foreign_scope`, `::test_the_foreign_holder_fast_verify_after_a_reset_holds`, `::test_a_broker_start_owes_a_fabric_pass_before_any_release`, `tests/test_tray_down_prelude.py::test_a_rescan_recovery_releases_only_on_a_passing_fabric_pass` |
| I32 with an AICLK ceiling armed (spec 03 I35), every reset path (operator reset, glx_reset rung, bridge rung, UBB tray sweep, idle off-bus escalation, hold-deadline escalation, broker start) runs reset -> ceiling -> full pass, fabric included -> release; a fabric FAIL keeps the hold for the bounded ladder; a disarmed ceiling still runs the full pass | `tests/test_aiclk_ceiling_goal8.py::test_every_reset_path_runs_ceiling_then_full_fabric_pass_then_releases`, `::test_fabric_fail_after_any_reset_keeps_the_hold_and_enters_bounded_recovery`, `::test_disarmed_ceiling_still_runs_the_full_fabric_pass` |
| I32 no rung outcome lifts the funnel's hold: the last-chance sweep on a fabric 77, no verdict or an unavailable check holds fabric-unverified with no host rung; a 77-only hold never goes DOWN | `tests/test_post_reset_gate.py::test_the_gate_ladders_last_chance_sweep`, `::test_no_rung_outcome_lifts_a_fabric_unverified_hold`, `::test_a_terminal_rung_on_a_measured_fault_still_goes_down`, `::test_every_rung_outcome_folds_through_the_helper` |
| I32a a reset drops the pre-reset fabric FAIL: a post-reset 77 holds RECOVERING (bounded), never DOWN | `tests/test_post_reset_gate.py::test_the_measured_fabric_failure_reset_on_the_first_relift` |
| I32 the post-job gate runs the pass only while a reset owes one, never pre-job; the I40 fallback climbs instead of releasing an owed reset | `tests/test_post_reset_gate.py::test_a_clean_post_job_gate_with_no_debt_runs_no_fabric_pass`, `::test_a_post_job_gate_owed_a_fabric_pass_runs_it_and_settles`, `::test_a_pre_job_gate_never_runs_the_owed_pass`, `::test_the_77_fallback_never_releases_a_reset_with_no_passing_fabric_pass` |
| I33 no power cycle configured: the gated off-bus hold is handed to an operator once per episode (one page, the deadline gauge, `/health` `operator_handoff`), which end with the hold; the knob's default, `off` and malformed values | `tests/test_gated_hold_handoff.py::test_the_hold_is_handed_to_an_operator_once_with_a_deadline`, `::test_the_handoff_knob` |
| I33 before the deadline the gate holds; past it the watchdog forces one pass, and only that pass runs the ladder with the gate lifted | `tests/test_gated_hold_handoff.py::test_before_the_deadline_the_gate_holds_and_no_reset_fires`, `::test_the_watchdog_forces_one_pass_at_the_handoff_bound`, `::test_past_the_deadline_the_ladder_runs_with_the_gate_lifted_for_that_pass_only` |
| I33 an operator latch (`off`, the I25 latch) or an operator at work keeps the hold past the deadline | `tests/test_gated_hold_handoff.py::test_an_operator_latch_keeps_the_hold_past_the_deadline`, `::test_an_operator_at_work_keeps_the_gate_shut` |
| I33 / I32 a lifted reset releases only after a passing full health check, fabric included; a reset the gate still refuses releases nothing | `tests/test_gated_hold_handoff.py::test_the_lifted_mesh_reset_releases_only_after_a_passing_fabric_check`, `::test_a_reset_the_gate_still_refuses_releases_nothing` |
| I34 a requested power cycle or reboot suppresses every hold-deadline tick and a forced escalation waiting on the lock; the device stays held with the check owed; one line per request | `tests/test_host_action_suppress.py::test_power_cycle_request_suppresses_every_deadline_tick`, `::test_reboot_request_suppresses_the_deadline_tick`, `::test_escalation_waiting_on_the_lock_runs_no_rung` |
| I34 bounded: a refused or failed request never suppresses; an expired one resumes the ladder; the window is always finite | `tests/test_host_action_suppress.py::test_power_cycle_that_fails_to_launch_does_not_suppress`, `::test_power_cycle_refused_by_the_ledger_does_not_suppress`, `::test_a_request_the_host_never_acted_on_expires_and_the_ladder_resumes`, `::test_pending_window_is_always_finite` |
| I34 every path: escalate starts no rung for any phase, the gate ladder runs no rung and holds, an operator reset is refused on both routes unless forced, a timed-out fire stays pending, a held job names the pending action | `tests/test_host_action_suppress.py::test_escalate_starts_no_rung_while_a_host_action_is_pending`, `::test_the_gate_runs_no_rung_and_holds_while_a_host_action_is_pending`, `::test_an_operator_reset_is_refused_while_a_host_action_is_pending`, `::test_a_power_cycle_command_that_timed_out_stays_pending`, `::test_a_job_held_past_its_bound_names_the_pending_host_action` |
| I36 a mesh reset that failed on an unchanged off-bus set is not repeated in its hold; with no host rung the hold is handed to an operator once, latched | `tests/test_no_repeat_failed_rung.py::test_a_failed_unchanged_reset_is_not_repeated_and_with_no_host_rung_is_handed_off_once` |
| I36 / I38 the failed reset climbs at once: no sweep, the power cycle; a rate-limited cycle holds and fires no reset | `tests/test_no_repeat_failed_rung.py::test_with_a_power_cycle_the_failed_reset_climbs_at_once`, `::test_a_rate_limited_power_cycle_fires_no_reset` |
| I36/I87 a changed off-bus set gets no second mesh reset in the episode; a refused (never run) reset is not recorded, so the reset stays open | `tests/test_no_repeat_failed_rung.py::test_a_changed_off_bus_set_gets_no_second_reset_in_the_episode`, `::test_a_refused_reset_is_not_recorded` |
| I36 present mesh: a forced window skips the failed reset; the grace-cadence retry still resets | `tests/test_no_repeat_failed_rung.py::test_a_forced_present_mesh_window_skips_the_failed_reset`, `::test_a_non_forced_grace_retry_still_resets_a_present_mesh` |
| I36 / I32 every reset on this path is followed by the full verify, fabric included; a failed verify keeps the hold and climbs | `tests/test_no_repeat_failed_rung.py::test_every_reset_is_verified_with_the_fabric_and_a_failed_verify_keeps_the_hold_and_climbs` |
| I36 time to recover (fake-clock replay): power cycle at 263 s vs 1186 s, one reset vs one per window | `tests/test_no_repeat_failed_rung.py::test_replay_the_power_cycle_fires_right_after_the_failed_reset`, `::test_replay_with_no_host_rung_the_reset_loop_ends_in_one_hand_off` |
| I37 the fault class: whole trays with a chip still on the bus (A), all off after a failed mesh reset (C), at startup or at run time; partial drops and no tray map are neither | `tests/test_classac_power_cycle_direct.py::test_the_fault_class_of_an_off_bus_set` |
| I37 class A (gate and idle hold): the rescan and capture, then the power cycle with no reset, tray re-power or sweep | `tests/test_classac_power_cycle_direct.py::test_a_whole_tray_goes_to_the_power_cycle_with_no_reset`, `::test_an_idle_whole_tray_hold_cycles_with_no_reset` |
| I37 class C: all off at startup (a broker restart) with no failed reset gets exactly one mesh reset, then cycles; all off after a failed mesh reset cycles with no second reset and no sweep, at startup or at run time | `tests/test_classac_power_cycle_direct.py::test_all_off_at_startup_gets_one_mesh_reset_first`, `::test_all_off_at_startup_after_a_failed_reset_cycles_with_no_reset`, `::test_all_off_after_a_failed_mesh_reset_cycles_with_no_second_reset` |
| I37 the failed-reset mark: set by a mesh reset that exits nonzero or verifies unhealthy, not by a healthy one; cleared at the episode end | `tests/test_classac_power_cycle_direct.py::test_the_failed_reset_mark_is_set_by_a_failed_reset_and_cleared_at_the_episode_end` |
| I37 partial drops (one chip, part of one tray) keep the full ladder | `tests/test_classac_power_cycle_direct.py::test_a_partial_drop_keeps_the_ladder` |
| I37 a reset in scope, a tenant, a stand-back marker or a rate-limit denial holds and fires nothing; no power cycle opted in keeps the ladder | `tests/test_classac_power_cycle_direct.py::test_a_guard_holds_the_power_cycle_and_fires_nothing`, `::test_with_no_power_cycle_opted_in_the_ladder_runs` |
| I37 the hold-deadline climb waives an unreadable holder scan and cycles; a running job or a readable tenant still holds it, and nothing is released before the startup check | `tests/test_classac_power_cycle_direct.py::test_the_deadline_climb_cycles_past_an_unreadable_scan_but_never_a_tenant` |
| I37 the host rung's last-chance sweep skips itself for classes A and C | `tests/test_classac_power_cycle_direct.py::test_the_last_chance_sweep_skips_itself_for_a_and_c` |
| I37 / goal of 03 I4: after the cycle the startup gate waits for hugepages and runs the fabric pass; a failed check keeps the hold | `tests/test_classac_power_cycle_direct.py::test_after_the_cycle_the_startup_gate_awaits_hugepages_and_the_fabric`, `::test_a_failed_startup_check_after_the_cycle_keeps_the_hold` |
| I37 class A with no tray map: read from the buses still on the bus; a partial tray, a stray chip, unknown groups or no tables are not class A | `tests/test_classac_power_cycle_direct.py::test_whole_trays_absent_reads_class_a_from_bus_presence_alone`, `::test_the_class_reads_presence_only_with_no_map` |
| I37 class A with no tray map (a short boot): the first gate pass, startup or run time, and the hold-deadline climb request the cycle with no mesh reset, tray re-power or sweep; a partial drop keeps the ladder | `tests/test_classac_power_cycle_direct.py::test_a_whole_tray_drop_with_no_map_cycles_at_the_first_gate_pass`, `::test_the_hold_deadline_climb_with_no_map_cycles_with_no_reset`, `::test_a_partial_drop_with_no_map_keeps_the_ladder` |
| I37/I65 class A with no map inside the cooldown: the first pass is denied with the deadline on /health and in the hold message, and nothing resets | `tests/test_classac_power_cycle_direct.py::test_inside_the_cooldown_the_deadline_shows_at_once_and_no_reset_runs` |
| I37 time to recover (fake clock): class A requests the cycle in the first window, class C after one failed reset | `tests/test_classac_power_cycle_direct.py::test_time_to_recover_on_a_fake_clock` |
| I37 with no map the missing chip ids must be exactly the off-bus ids; a lingering node the heartbeat calls off, or a node whose link reads down, never fills a tray | `tests/test_classac_power_cycle_direct.py::test_whole_trays_absent_needs_the_off_bus_ids_to_be_the_missing_ones`, `::test_a_no_map_tray_of_lingering_nodes_is_class_a`, `::test_a_listed_node_whose_link_is_down_does_not_fill_a_tray` |
| I37 EIO survivors: enumerated, bound chips whose heartbeat read fails are on the bus for the class A read only (map or no map), unless their link reads down; every chip unreadable is not class A; a partial drop with such chips keeps the ladder; the rungs still count them off; lingering link-down EIO nodes leave an all-off bus its one mesh reset, and a tray of them with one link-up EIO survivor is class A | `tests/test_classac_power_cycle_direct.py::test_a_whole_tray_with_eio_survivors_cycles_with_the_map`, `::test_a_whole_tray_with_eio_survivors_cycles_with_no_map`, `::test_all_chips_unreadable_is_not_class_a`, `::test_a_partial_drop_with_eio_survivors_keeps_the_ladder`, `::test_lingering_link_down_nodes_leave_an_all_off_bus_its_mesh_reset`, `::test_a_tray_of_link_down_nodes_with_one_eio_survivor_is_class_a`, `tests/test_arc_not_ready.py::test_unreadable_on_bus_chips_names_bound_eio_chips_only`, `::test_unreadable_on_bus_chips_skips_a_node_whose_link_is_down`, `::test_eio_chips_still_count_off_the_bus_for_the_rungs` |
| I38 partial off-bus (one chip, four chips): the power cycle fires right after the one failed reset, 252 s vs 924 s and 383 s vs 1186 s from onset; a denied cycle is asked again the next window with no reset between | `tests/test_no_repeat_failed_rung.py::test_partial_offbus_power_cycles_right_after_the_failed_reset`, `::test_a_denied_power_cycle_after_the_failed_reset_is_retried_next_window` |
| I38 with I65: a partial off-bus whose reset failed within the cooldown after a host boot skips the ceiling wait and the sweep, holds with the cooldown deadline set and no second reset, shows that deadline on /health (`power_cycle_cooldown.next_deadline`), is asked again by the watchdog at it, then cycles once the cooldown has run out | `tests/test_no_repeat_failed_rung.py::test_partial_offbus_after_the_failed_reset_waits_only_for_the_power_cycle_cooldown` |
| I36 / I38 for each reset class (B1 one or two chips off, B2 four, D present mesh) and host rung (power cycle, reboot, none) over six forced windows: the mesh reset runs first and once on an unchanged set, the next rung follows in that same window (or one latched operator hand-off), nothing releases the hold | `tests/test_no_repeat_failed_rung.py::test_e3_offbus_classes_reset_once_then_take_the_next_rung`, `::test_e3_present_mesh_resets_once_then_takes_the_next_rung` |
| I87 one mesh reset per hold episode: a failed reset that shrank the set is not repeated and climbs; a non-forced grace pass does not reset a present mesh again; the stuck-hold escalation never repeats a failed mesh reset in the episode | `tests/test_no_repeat_failed_rung.py::test_a_failed_reset_that_shrank_the_set_is_not_repeated_and_climbs`, `::test_a_non_forced_grace_pass_does_not_reset_a_present_mesh_again`, `tests/test_device_safety.py::test_stuck_hold_escalation_never_repeats_a_failed_mesh_reset_in_the_episode` |
| I87 the last-chance sweep runs at most once per hold phase (below the 10-min bound, then once at it); a repeat in the same phase climbs | `tests/test_removed_rungs_stay_gone.py::test_the_last_chance_sweep_runs_at_most_once_per_phase` |
| I71/I87 the reboot rung, the tray step and a second mesh reset are gone for good: no source reads their old switches, the power cycle is the only host rung, and stale `AUTO_REBOOT`, `POST_REBOOT_VERIFY`, `AUTO_UBB_RESET`, `PROGRESS_RETRY_SEC` or `TTDEV_AUTO_REBOOT` settings change nothing | `tests/test_removed_rungs_stay_gone.py::test_no_switch_for_a_removed_rung_is_read`, `::test_the_reboot_rung_is_gone_and_the_power_cycle_is_the_only_host_rung`, `::test_stale_switches_never_make_the_chooser_name_a_reboot`, `::test_stale_switches_bring_back_no_reboot_tray_step_or_second_mesh_reset` |
| I81 B1 replay: an isolated chip's tray is re-powered, the rescan at tt-smi's driver wait re-adds it, one mesh reset heals it inside the 6 min budget and the full verify, fabric included, runs before release | `tests/test_glx_reset_isolated_rescan.py::test_b1_replay_one_mesh_reset_heals_an_isolated_chip_inside_the_budget` |
| I81 the rescan fires once at the driver-wait marker (one line, split read, split inside the count) and the caller still sees every output chunk; never with no isolated chip or off a mesh reset; a failed rescan is journaled and the reset runs on; a cancelled reset cancels a pending rescan and output read after the cancel starts none; a cancel while the rescan finishes still clears the in-flight flag, rescans, drops returned chips and restores the pollers before it reaches the caller | `tests/test_glx_reset_isolated_rescan.py::test_the_rescan_fires_once_at_the_driver_wait_and_the_caller_still_sees_the_output`, `::test_no_early_rescan_without_an_isolated_chip_or_off_a_mesh_reset`, `::test_a_failed_driver_wait_rescan_is_journaled_and_the_reset_runs_on`, `::test_a_cancelled_reset_cancels_a_pending_driver_wait_rescan`, `::test_output_after_a_cancelled_reset_starts_no_driver_wait_rescan`, `::test_a_cancel_while_the_driver_wait_rescan_finishes_still_runs_the_reset_cleanup` |
| I58 the failed-reset record survives a broker restart but not a new boot; a new episode on the same set does not reset again; only a launched reset is recorded | `tests/test_no_repeat_failed_rung.py::test_the_failed_reset_survives_a_broker_restart_but_not_a_new_boot`, `::test_a_new_hold_episode_on_the_same_set_does_not_reset_again`, `::test_a_reset_that_never_launched_is_not_recorded` |
| I59 a gap or holder left by the broker's own exiting probe or killed holder is not a tenant; any other gap still fails closed; scoped holders and reused pids are never discounted | `tests/test_broker_internal_holders.py`; the snapshot and eth self-test, each in its own session, are registered: `tests/test_offbus_abort_readers.py::test_a_running_snapshot_is_broker_internal_not_a_tenant`, `::test_an_eth_selftest_the_valve_stopped_mid_run_arms_nothing` |
| I65/I73 a boot 3 min before startup with a whole tray off: held with the deadline on /health and no rung before the 10-min mark, the full ladder once in the I76 bound's run there, then one cycle at the deadline | `tests/test_power_cycle_cooldown.py::test_a_boot_3_min_before_startup_holds_to_a_deadline_then_cycles_once`, `::test_health_carries_the_cooldown_deadline_while_held` |
| I65 the broker's cycle then a boot with the tray still off: no second cycle before the cooldown ends, one at its end; an external cycler's history is an anchor; a future stamp is not | `tests/test_power_cycle_cooldown.py::test_after_the_broker_cycle_and_boot_no_second_cycle_before_the_cooldown_ends`, `::test_an_external_cycler_history_is_an_anchor`, `::test_a_boot_stamp_in_the_future_anchors_nothing` |
| I65 backoff 30 min, 30 min, 1 h, 2 h, 4 h cap; a verified release resets it | `tests/test_power_cycle_cooldown.py::test_the_backoff_doubles_after_two_failed_cycles_and_caps_at_4h`, `::test_cycles_that_heal_nothing_back_off_and_a_verified_release_resets_it` |
| I65 every denial ends at a stated time and is asked again then, in its own hold episode; the per-boot cap is bounded | `tests/test_power_cycle_cooldown.py::test_a_cycle_requested_this_boot_that_never_went_down_is_asked_again`, `::test_the_watchdog_retry_is_kept_to_its_own_hold_episode`, `tests/test_device_safety.py::test_governor_per_boot_cap_ends_with_the_interval`, `::test_power_cycle_after_a_recent_reboot_waits_out_the_cooldown` |
| I65 a whole tray still off hours after a cycle that never took the host down cycles at once; inside the cooldown it holds with the deadline on /health, runs the full ladder once in the hold's 10-min bound run (I76 with I73) and cycles at its end; an external reboot counts toward the cooldown; the off-bus (I27) path holds with a deadline and no ladder; an operator attempt spacing is a stated wait capped at the backoff cap, default none; a whole tray seen again among partial reads still cycles | `tests/test_whole_tray_cycle_every_boot.py::test_a_restart_hours_after_a_cycle_that_never_went_down_cycles_at_once`, `::test_inside_the_cooldown_the_hold_shows_its_deadline_and_cycles_when_it_ends`, `::test_an_external_reboot_counts_toward_the_cooldown_and_the_cycle_follows_it`, `::test_the_off_bus_path_no_longer_holds_six_hours_on_an_attempt_that_never_went_down`, `::test_the_off_bus_path_inside_the_cooldown_holds_with_a_deadline_and_no_ladder`, `::test_an_operator_attempt_interval_is_a_stated_wait_capped_at_the_backoff_cap`, `::test_the_attempt_interval_default_adds_no_wait_beyond_the_cooldown`, `::test_a_whole_tray_seen_again_among_partial_reads_still_cycles_at_the_deadline` |
| I65 no recent cycle or boot: the cycle fires at once | `tests/test_power_cycle_cooldown.py::test_with_no_recent_cycle_or_boot_the_cycle_fires_at_once` |
| I65 only a verified release with no fabric debt ends the streak; a refused one keeps the hold | `tests/test_power_cycle_cooldown.py::test_only_a_verified_release_with_no_fabric_debt_ends_the_streak` |
| I65 for one hold episode the power-cycle deadline set at the first cooldown denial never moves later (beyond the stated post-boot settle): boots at +20, +40 and +60 min keep it, the last adds the 300 s settle, and the cycle is allowed then | `tests/test_power_cycle_cooldown.py::test_reboots_inside_one_hold_never_move_its_power_cycle_deadline_later` |
| I65 a boot inside the hold: the startup gate holds with no rung or release, /health shows the same deadline, the cycle fires at it | `tests/test_power_cycle_cooldown.py::test_an_operator_shutdown_inside_the_hold_keeps_the_deadline_and_the_cycle_fires_at_it` |
| I65 the broker's next cycle ends the episode deadline (a fresh interval with the streak); a verified release removes it; a refused release keeps it | `tests/test_power_cycle_cooldown.py::test_the_brokers_next_cycle_spends_the_deadline_and_the_boot_loop_guard_holds`, `::test_a_verified_release_ends_the_episode_deadline`, `::test_only_a_verified_release_with_no_fabric_debt_ends_the_streak` |
| I65 an unusable deadline record leaves the plain cooldown; a broker cycle with an unreadable stamp ends the episode, an older one does not; a record fixed later than now is kept; the settle is within the startup gate's bound | `tests/test_power_cycle_cooldown.py::test_an_unusable_deadline_record_leaves_the_plain_cooldown`, `::test_a_broker_cycle_with_an_unreadable_stamp_spends_the_deadline`, `::test_an_old_unreadable_stamp_does_not_void_a_later_deadline`, `::test_the_first_denial_records_how_many_cycles_had_no_stamp`, `::test_a_deadline_fixed_later_than_now_is_kept_not_rewritten`, `::test_the_post_boot_settle_is_within_the_startup_gates_own_bound` |
| I65 a verified release on a later boot ends the streak with no heal file; one before the cycles does not | `tests/test_power_cycle_missed_request.py::test_a_verified_release_on_a_later_boot_ends_the_streak_without_a_heal_file`, `::test_a_verified_release_before_the_cycles_does_not_end_their_streak` |
| I72 a missed request is marked on disk and asked again as its window ends, later ones 5 min after theirs, through the whole-tray path; a restarted broker reads an old same-boot row as missed | `tests/test_power_cycle_missed_request.py::test_a_request_the_host_never_acted_on_is_asked_again_as_its_window_ends`, `::test_later_retries_wait_5_min_after_their_window_and_never_stop`, `::test_a_whole_tray_drop_still_goes_to_the_power_cycle_after_a_missed_request`, `::test_a_restarted_broker_still_reads_an_old_same_boot_request_as_missed` |
| I72 stated bound: the next request at most the window (first miss) or the window + 5 min after one the BMC missed; no retry while the earlier request is still pending; a late cycle before the retry is not cycled again on top | `tests/test_power_cycle_missed_request.py::test_the_retry_comes_at_most_the_window_plus_5_min_after_a_request_the_bmc_missed`, `::test_no_retry_while_the_earlier_request_is_still_pending`, `::test_a_late_cycle_before_the_retry_is_never_cycled_again_on_top` |
| I72/I65 a cycle that lands after its window counts as a cycle that ran on every later boot: the cooldown doubles, the backstop and the boot's attribution count it; broker startup writes the late mark, so a boot that only reboots keeps it; an unreadable ledger is left as it is | `tests/test_power_cycle_missed_request.py::test_cycles_that_land_after_their_window_still_double_the_cooldown_and_count_for_the_backstop`, `::test_a_late_cycle_whose_boot_only_reboots_still_doubles_the_cooldown_on_later_boots`, `::test_the_startup_mark_leaves_an_unreadable_ledger_as_it_is` |
| I72 missed requests neither double nor anchor the cooldown nor count toward the cap or backstop, on this boot or the next; the second pages; a real cycle still anchors and doubles | `tests/test_power_cycle_missed_request.py::test_two_missed_requests_leave_the_interval_at_base_and_page`, `::test_a_missed_row_still_counts_for_nothing_on_the_next_boot`, `::test_a_cycle_that_took_the_host_down_still_anchors_the_cooldown_and_doubles` |
| I72 a retried cycle that heals releases only after the full fabric check, which clears the retry; /health shows the missed count and the next retry | `tests/test_power_cycle_missed_request.py::test_a_retried_cycle_that_heals_releases_only_after_the_full_fabric_check`, `::test_health_shows_the_missed_count_and_the_next_retry`, `::test_health_shows_no_missed_requests_for_a_plain_cooldown` |
| I73 two broker cycles that do not heal a whole tray flag AC power needed; the cooldown still spaces the two; cycles 3, 4 and 5 fire at each cooldown's end (no cycle limit, waits at most 4 h); the page goes once, then hourly | `tests/test_power_cycle_ac_needed.py::test_cycles_continue_at_cooldown_spacing_past_the_ac_needed_state`, `::test_the_cooldown_is_honoured_between_the_two_cycles`, `::test_the_ac_hold_pages_once_then_hourly` |
| I73/I76 inside the cooldown the full ladder runs once per cycle or boot in the 10-min hold bound's run, the only trigger (one forced start at the mark; other forced passes run no ladder), and the cycle follows at the cooldown's end; end to end through the watchdog's forced run, the sweep (SBR, then the mesh reset; no tray step) and its one full check (fabric included) run once at the bound, a failed check asks for the host rung and arms the retry at the cooldown's end; a passing check alone releases; a tenant or a refused sweep runs no check and leaves the cycle to follow | `tests/test_power_cycle_ac_needed.py::test_the_full_ladder_runs_at_the_10_min_hold_bound_then_the_cycle`, `::test_the_hold_bound_is_the_one_trigger_of_the_class_a_ladder`, `::test_the_watchdog_run_at_the_bound_runs_the_class_a_ladder_once`, `::test_a_class_a_sweep_releases_only_on_a_passing_full_check`, `::test_a_class_a_sweep_a_guard_holds_runs_no_check_and_the_cycle_still_follows` |
| I73/I76 at the 10-min mark a bound ladder whose full check failed keeps its power-cycle step, the one run the watchdog starts | `tests/test_power_cycle_ac_needed.py::test_at_the_10_min_mark_a_failed_bound_check_keeps_the_power_cycle_step` |
| I73 a returning tray leaves the AC state and is released only after the full check, fabric included; one that drops again before a verified release is cycled at the cooldown's end | `tests/test_power_cycle_ac_needed.py::test_a_returning_tray_leaves_the_ac_state`, `::test_a_returning_tray_is_released_only_after_the_full_fabric_check_passes`, `::test_a_tray_that_drops_again_before_any_verified_release_is_cycled_at_the_cooldowns_end` |
| I73 `/health` shows `ac_power_needed` with `escalation_stage` `power-cycle` and the next cycle as `next_deadline`; null on an ordinary cooldown | `tests/test_power_cycle_ac_needed.py::test_health_shows_the_ac_needed_state_and_the_next_cycle`, `::test_health_shows_no_ac_state_on_an_ordinary_cooldown` |
| I73 only broker cycles for this tray that took the host down since the last verified release count (not this boot's request, a missed one, another tray's, or the last one a much later boot followed); external cycles space the cooldown but never count; a partial off-bus never enters the AC state; each cycle row names its tray | `tests/test_power_cycle_ac_needed.py::test_only_broker_cycles_that_took_the_host_down_for_this_tray_count`, `::test_external_cycles_space_the_cooldown_but_are_never_attempts`, `::test_a_partial_off_bus_set_never_enters_the_ac_hold`, `::test_a_power_cycle_row_names_the_tray_it_was_for` |
| I65 a failed out-of-band cycle (a second line, same `at_epoch`, cause + ` (cycle failed)`) never counts and cancels its attempt line; an unmatched failure line is ignored; an attempt with no failure line still counts; host boots are unchanged | `tests/test_power_cycle_cooldown.py::test_a_matched_failure_line_drops_the_attempt`, `::test_an_unmatched_failure_line_is_ignored`, `::test_an_attempt_without_a_failure_line_still_counts` |
| I65 a future, non-finite or unreadable stamp (history file, ledger, heal file) never hides a valid past anchor, never holds the cycle past the stated gap, never keeps the streak past a verified release; every cap denial is on /health | `tests/test_power_cycle_cooldown.py::test_a_nan_or_future_history_line_does_not_hide_the_valid_past_one`, `::test_a_future_or_unreadable_ledger_record_does_not_hide_a_past_broker_cycle`, `::test_a_future_this_boot_stamp_holds_only_to_the_stated_gap_and_shows_on_health`, `::test_an_unreadable_this_boot_stamp_holds_the_cycle_to_a_fixed_end`, `::test_the_per_boot_cap_denial_shows_on_health`, `::test_a_future_record_does_not_keep_the_streak_past_a_verified_release` |
| I71 the power cycle is the only host rung: armed, every drop names it; not armed, no host rung (hold and page) | `tests/test_power_cycle_host_rung.py::test_the_power_cycle_is_the_only_host_rung` |
| I71 a partial drop past the full ladder (one mesh reset) takes the power cycle, at the gate and on the idle climb; the router names STAGE_POWER_CYCLE | `tests/test_power_cycle_host_rung.py::test_partial_drop_past_the_ladder_cycles`, `::test_the_gate_cycles_a_partial_drop_when_allowed`, `::test_next_stage_names_the_power_cycle_for_a_failed_reset_partial_drop` |
| I71 a denied cycle holds and asks only for the cycle; one that does not launch holds, pages and is asked for again; BLOCKED only with no live reset scope; the launch verdict the hold reads | `tests/test_power_cycle_host_rung.py::test_a_denied_cycle_holds_and_asks_only_for_the_cycle`, `::test_a_cycle_that_does_not_launch_holds_pages_and_retries`, `::test_a_live_scope_waits_instead_of_blocking`, `::test_fire_recovery_escalation_reports_whether_the_action_may_have_launched` |
| I71 with I65: a cooldown-held cycle fires at the cooldown end, with the real governor and ledger | `tests/test_power_cycle_host_rung.py::test_a_cooldown_held_cycle_fires_at_the_cooldown_end_with_the_real_governor` |
| I37/I65 class C at startup: one mesh reset with no failed reset or broker cycle; none after the broker's own cycle, which then waits only for the cooldown | `tests/test_power_cycle_cooldown.py::test_all_off_at_startup_with_no_failed_reset_or_broker_cycle_gets_one_reset`, `::test_all_off_after_the_broker_cycle_brought_this_boot_up_skips_the_reset`, `::test_a_cycle_requested_this_episode_counts_and_one_before_it_does_not` |
| I37/I65 the gate counts a class A/C direct cycle once: one pass with two cycles already in the backstop window requests one cycle, journals no backstop denial, pages nobody and arms no retry; a pass while it is pending asks nothing; a real fourth cycle in the window is still denied, paged and retried | `tests/test_power_cycle_backstop_once.py::test_one_gate_pass_on_all_off_counts_its_cycle_once`, `::test_a_second_pass_while_the_cycle_is_pending_asks_nothing`, `::test_the_backstop_still_denies_a_real_fourth_cycle` |
| I78 the driver-wait rescan repeats until every isolated chip is back and then stops; it gives up after its tries, journaled; it ends with the reset and never delays its cleanup; a cancelled reset stops it; no rescan while the settle counts; on both backends (line reader and file reader) the rescan runs after the settle and inside the driver wait | `tests/test_mesh_reset_readd.py::test_the_rescan_repeats_until_the_chip_is_back_and_then_stops`, `::test_the_repeat_gives_up_after_its_tries`, `::test_the_repeat_ends_with_the_reset_and_never_delays_its_cleanup`, `::test_a_cancelled_reset_stops_the_repeats`, `::test_no_rescan_while_the_settle_counts`, `::test_both_backends_rescan_only_once_the_settle_is_over` |
| I76 every hold kind (77-only, topology mismatch, known fabric fault, latched, generic) runs the full ladder at the 10-min bound, then the host rung; below the bound a no-reset hold keeps its fast path; a latched I42 hold escalates with its latch standing | `tests/test_hold_bound.py::test_every_hold_kind_runs_the_ladder_at_the_bound_then_the_host_rung`, `::test_below_the_bound_a_no_reset_hold_keeps_its_fast_path`, `::test_a_latched_i42_hold_still_escalates_and_its_latch_stands`, `::test_replay_a_latched_topology_mismatch_hold_gets_the_ladder_then_the_power_cycle` |
| I76 the I29 77 hold with no host rung (reset and sweep already ran this episode) gets the power cycle at the bound in the same run; a killed job's leftover holder (01 I16) does not stand the bound down: no reset runs over it and the host rung is asked for, which a live tenant still refuses | `tests/test_hold_bound.py::test_replay_a_77_held_with_no_host_rung_gets_the_power_cycle_at_the_bound`, `::test_a_killed_jobs_leftover_holder_does_not_stop_the_bound` |
| I76 a holder the spec 03 I84 valve refused to kill (a running broker job or a tenant fd) does not stand the bound down: the ladder is skipped and the host rung asked for, nobody is killed; a process the valve did not refuse, or a recycled pid, still blocks | `tests/test_deadchip_refused_bound.py::test_a_refused_holder_gets_the_power_cycle_at_the_bound`, `::test_a_tenant_the_valve_never_refused_still_blocks_the_bound` |
| I76 a cooldown denial of the host rung arms the retry, which fires at the cooldown's end | `tests/test_hold_bound.py::test_a_cooldown_denial_arms_the_retry_and_the_host_rung_fires_at_its_end` |
| I76 a bound ladder that brings the mesh back but whose full check ends with no pass (77-only, topology mismatch / I42 latch, known fabric fault, generic) does not release and asks for the host rung in the same run; a relift check that fails after the ladder asks for it on the next tick; a check that passes asks for none | `tests/test_hold_bound.py::test_a_failed_check_after_the_bound_ladder_asks_for_the_host_rung_at_once`, `::test_a_relift_that_fails_after_the_bound_ladder_cycles_on_the_next_tick`, `::test_a_passing_check_after_the_bound_ladder_asks_for_no_host_rung` |
| I76 a check still waiting on hugepages (or hanging) after the bound ladder gets the 300 s check bound, then the host rung; another run inside that bound does not cut the check short | `tests/test_hold_bound.py::test_a_check_still_waiting_on_hugepages_is_bounded_by_the_check_window`, `::test_another_run_inside_the_check_bound_does_not_cut_the_full_check_short` |
| I76 a failed full check in any idle lift after the bound ladder (eth_frozen, off-bus floor, foreign holder) asks for the host rung on the next tick; one still waiting on hugepages asks for none | `tests/test_hold_bound.py::test_a_failed_lift_check_after_the_bound_ladder_asks_for_the_host_rung`, `::test_a_lift_check_still_waiting_on_hugepages_asks_for_no_host_rung`, `::test_a_failed_foreign_holder_check_after_the_bound_ladder_asks_for_the_host_rung` |
| I76 /health shows the bound; the env shortens it, never lengthens it; a stuck-hold event past the bound never reads `escalated` false | `tests/test_hold_bound.py::test_health_shows_the_bound`, `::test_env_shortens_the_bound_never_lengthens_it`, `::test_a_stuck_hold_event_past_the_bound_never_reads_escalated_false` |
| I76 the latch set by a fabric failure of the first job after a topology-mismatch lift: at the bound (watchdog window or tenant-hold bound) the full ladder runs, the full check with fabric still fails, the broker asks for the power cycle once, the latch stands; a tenant job parked at the gate behind that hold (holding no device fd) never dispatches, and its own tenant-hold bound refuses it with no second power cycle | `tests/test_hold_bound.py::test_replay_the_lift_then_job_fabric_failure_latch_gets_the_ladder_check_and_power_cycle`, `::test_replay_post_reset_lift_latch_with_gated_queued_job_runs_the_ladder_at_the_bound`; walked tick by tick with no idle relift reset, no rung runs below the bound, and the deadline event reads escalated: `tests/test_hold_bound.py::test_replay_a_disproven_lift_hold_gets_the_ladder_and_power_cycle_within_the_class_d_budget` |
| I76 a disproven I42 lift's bound is one sampler tick and the FORCE_ESCALATE=0 kill switch does not stop it; a hold with no disproven lift keeps the 600 s bound and (77-only) its no-reset fast path, while an unlatched mismatch gets I83's at-start ladder; the early ladder does not count as the 10-min one, so a hold still standing at 600 s with no host action pending gets the full ladder again | `tests/test_hold_bound.py::test_a_disproven_lift_runs_the_bound_ladder_after_one_tick_whatever_the_kill_switch`, `::test_a_hold_with_no_disproven_lift_keeps_the_600s_bound_and_its_fast_path`, `::test_the_10_min_mark_reruns_the_full_ladder_after_an_early_disproven_lift_run` |
| I76 with I75: under the I42 latch the host rung the early ladder owes (its check bound ran out, or a denied rung still pending) runs below 600 s with no second ladder; `/health` names the ladder one sampler tick in, then that host rung, with or without the kill switch | `tests/test_hold_bound.py::test_the_host_rung_after_an_early_disproven_lift_ladder_runs_no_second_ladder`, `::test_health_names_a_disproven_lifts_early_ladder_then_its_host_rung`; `/health`'s `bound_sec` and `deadline` name the one-tick mark until that early ladder ran, then the 600 s one: `::test_a_disproven_lifts_deadline_moves_to_the_10_min_mark_once_its_early_ladder_ran` |
| I76 with spec 03 I71: a held 77 whose bound ladder's reset exits 0 but whose full check still finds one descriptor port missing (the traffic-only rerun passing) never releases and asks for exactly one power cycle, with or without the I42 latch | `tests/test_fabric_links_missing.py::test_replay_a_held_77_whose_bound_ladder_leaves_a_port_missing_power_cycles_once` |
| I76 every hold kind's escalation deadline is start + bound, or a running power-cycle cooldown's end when later; /health, the stuck-hold event and the alert read the one helper | `tests/test_hold_bound.py::test_every_hold_kind_reports_one_escalation_deadline` |
| I76 a forced run below the bound (off-bus one-shot) is not the bound's ladder: at the bound the full ladder runs again, then the host rung | `tests/test_hold_bound.py::test_a_ladder_below_the_bound_does_not_stand_in_for_the_bounds_ladder` |
| I76 a host rung or bound ladder a reset scope defers fires on the first sampler tick after the scope ends; a retry-less denial is asked again each tick and journaled once | `tests/test_hold_bound.py::test_a_host_rung_deferred_by_a_reset_scope_fires_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_a_bound_ladder_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_an_offbus_one_shot_past_the_bound_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_a_host_rung_denied_with_no_retry_is_asked_again_each_tick_and_journaled_once` |
| I76 a live tenant at the bound is paged once (`hold_bound_tenant_blocked`, naming it), /health shows `tenant_blocked`, and the ladder then the host rung run on the first tick after it leaves | `tests/test_hold_bound.py::test_a_live_tenant_at_the_bound_is_paged_once_and_the_ladder_runs_when_it_leaves`, `tests/test_hold_bound.py::test_a_killed_jobs_leftover_holder_does_not_stop_the_bound` |
| I82 a bound ladder's release then a failed first job (missing port, I42 latch, known fabric fault, 77-only, generic) goes to the power cycle at the next hold, not another ladder; a class change keeps the clock; a re-hold past the window with no job, or around a job started after it, is a new episode; the carry never crosses a reboot | `tests/test_hold_episode_carry.py::test_replay_a_release_after_the_bound_ladder_then_a_failed_first_job_goes_to_the_power_cycle`, `::test_a_class_change_on_the_rehold_keeps_the_clock`, `::test_a_rehold_after_the_carry_window_with_no_job_is_a_new_episode`, `::test_a_failed_first_job_keeps_the_carry_however_long_it_ran`, `::test_a_job_started_after_the_window_does_not_extend_it`, `::test_the_carry_does_not_cross_a_reboot` |
| I82 a broker restart keeps the clock: a released episode's re-hold goes to the power cycle, a hold open past its bound escalates at once, one past its bound ladder goes to the host rung | `tests/test_hold_episode_carry.py::test_replay_a_restart_whose_startup_check_releases_then_a_failed_first_job_goes_to_the_power_cycle`, `::test_a_hold_open_across_a_restart_escalates_at_once_when_past_the_bound`, `::test_a_hold_open_across_a_restart_after_its_bound_ladder_goes_to_the_host_rung`, `::test_replay_a_restart_whose_real_startup_gate_releases_keeps_the_carry` |
| I82 the startup hold a restart opens neither takes nor spends a released episode's carry (fresh clock, no false deadline flag, forced escalation or tenant-blocked page; a second restart defers again); a hold open across the restart keeps its start under a startup why; the fresh clock still runs the ladder at its own 10-min mark and lifts only on a full check pass, fabric included | `tests/test_hold_episode_carry.py::test_a_startup_hold_after_a_released_episode_starts_its_own_clock`, `::test_a_startup_hold_restored_across_a_second_restart_still_defers_the_carry`, `::test_a_hold_open_across_a_restart_keeps_its_clock_even_under_a_startup_why`, `::test_a_startup_hold_below_its_own_bound_runs_the_ladder_at_its_own_10_min_mark`, `::test_a_startup_hold_that_deferred_a_carry_lifts_only_on_a_full_check_pass` |
| I82 a startup hold that turns into a fault hold (the real startup gate's full check fails on a short link count, or the re-adopted job fails) takes a carry in its window: a release after the bound ladder then a restart during the first job then a failure goes to the host rung at once, every class; a re-adopted first job's end closes the window, a clean exit 0 drops the carry | `tests/test_hold_episode_carry.py::test_replay_a_restart_during_the_first_job_after_the_bound_ladder_then_a_failed_check_goes_to_the_host_rung`, `::test_a_startup_hold_whose_real_startup_check_fails_takes_the_carry`, `::test_a_readopted_first_job_closes_the_carry_window` |
| I82 a re-adopted first job that ends on a fault, a failing exit or no exit status pins the carry to the startup hold: a startup gate that fails more than 300 s after that end still goes to the host rung at once, every class; the pin asks no rung while the gate owns the hold; only a full pass, fabric included, lifts it | `tests/test_hold_episode_carry.py::test_a_readopted_first_job_that_fails_keeps_the_carry_through_a_slow_startup_gate`, `::test_the_pinned_carry_leaves_the_startup_hold_to_its_gate`, `::test_a_slow_real_startup_gate_after_a_failed_readopted_job_goes_to_the_host_rung_and_lifts_only_on_a_pass` |
| I82 with spec 03 I75: a startup hold deferred behind a re-adopted job (no episode open) is pinned by the job's failing end like an open one; the hold opened after it starts a fresh clock (`carry_deferred`) and escalates nothing | `tests/test_readopt_hold_clock.py::test_the_deferred_startup_hold_does_not_take_a_closed_episodes_carry` |
| I82 the carry ends when the device proves the release right: a successful first job, or a full check that passes with no hold open, gives the next fault a fresh episode and no cycle; the releasing pass keeps the carry it saves | `tests/test_hold_episode_carry.py::test_a_successful_first_job_ends_the_episode`, `::test_a_passing_full_check_with_no_hold_open_drops_the_carry`, `::test_a_passing_full_check_that_releases_a_hold_keeps_the_new_carry`, `::test_a_pass_whose_release_is_refused_keeps_the_carry` |
| I82 a verified release (fresh full pass on the full cabling) whose first job outruns the window starts the next hold's clock at zero, with nothing escalated early and its own 10-min ladder then power cycle on time; an unverified release (no verdict, failed, short or stale pass) still carries; a re-hold within the window of a verified release still carries; a verified release by the bound ladder still goes to the power cycle | `tests/test_hold_episode_carry.py::test_replay_a_hold_after_a_verified_release_and_a_long_first_job_is_a_new_episode`, `::test_a_new_episode_after_a_verified_release_runs_its_ladder_at_its_own_10_min_mark_then_the_power_cycle`, `::test_a_long_first_job_after_an_unverified_release_keeps_the_episode`, `::test_a_quick_rehold_after_a_verified_release_keeps_the_episode`, `::test_a_verified_release_by_the_bound_ladder_then_a_long_failed_first_job_still_goes_to_the_power_cycle` |
| I82 a tenant hold saves no carry and does not take or spend a fault hold's carry | `tests/test_hold_episode_carry.py::test_a_tenant_hold_leaves_no_carry`, `::test_a_tenant_hold_between_does_not_spend_a_fault_holds_carry` |
| I82 a tenant hold that turns into a fault hold takes a fault carry in its window, not one past it | `tests/test_hold_episode_carry.py::test_a_tenant_hold_that_turns_into_a_fault_hold_takes_the_carry_in_its_window`, `::test_a_tenant_hold_that_turns_into_a_fault_hold_past_the_window_is_a_new_episode` |
| I82 the first job's end closes the carry window even when a job-end check raises; a clean end drops the carry, a failed one keeps it | `tests/test_hold_episode_carry.py::test_the_first_jobs_end_closes_the_carry_window_even_when_a_job_end_check_raises`, `::test_the_first_jobs_verdict_still_decides_the_carry` |
| I82 on the carried path the I65 cooldown defers the host rung with no ladder and no cycle, the request fires at its end, once per episode | `tests/test_hold_episode_carry.py::test_the_carried_host_rung_waits_out_the_cooldown_and_asks_once` |
| I83 a fabric link-short hold (topology mismatch, known fabric fault, I42-latched, missing descriptor ports) runs one ladder on its first tick and asks for the power cycle at once when the full check after it still fails or the mesh did not come back; nothing is released | `tests/test_fabric_link_short.py::test_one_ladder_at_hold_start_then_the_power_cycle_at_once` |
| I83 no later tick in the episode (the bound, a later window, the hold deadline) runs a second glx_reset or ladder or a second power-cycle request | `tests/test_fabric_link_short.py::test_no_second_glx_reset_or_ladder_in_the_episode`, `tests/test_latched_hold_replay.py::test_an_unlatched_mismatch_hold_gets_its_ladder_at_start_and_none_again_across_a_restart` |
| I83 a full check that passes after the ladder asks for no power cycle; one still waiting on hugepages has until min(ladder end + 300 s, the 10-min bound), then the power cycle, under the I42 latch too (its one-tick bound does not shorten that window) | `tests/test_fabric_link_short.py::test_a_full_check_that_passes_after_the_ladder_asks_for_no_power_cycle`, `::test_a_check_still_waiting_gets_the_power_cycle_by_the_bound` |
| I83 the cooldown delays the power cycle only until its end | `tests/test_fabric_link_short.py::test_the_cooldown_delays_the_cycle_only_until_its_end` |
| I83 other classes (77-only, off-bus, generic) get no ladder at hold start; an isolated chip is not this class | `tests/test_fabric_link_short.py::test_other_classes_get_no_ladder_at_hold_start`, `::test_an_offbus_chip_is_not_the_link_short_class` |
| I83 a latched mismatch is this class: one ladder at hold start with the I42 latch kept, the short count asks for the power cycle in the same run, no second ladder, nothing released | `tests/test_fabric_link_short.py::test_a_latched_mismatch_gets_one_ladder_at_hold_start_and_keeps_the_latch` |
| I83 single flight: once the at-start pass ran, or while it runs, the idle relift's measured-fault present-mesh retries run no other glx_reset (missing-port and latched holds, driven end to end through the watchdog tick and the relift) | `tests/test_fabric_links_missing.py::test_the_idle_relift_runs_no_glx_reset_after_the_at_start_pass`, `::test_the_idle_relift_runs_no_glx_reset_while_the_at_start_pass_runs`, `tests/test_fabric_link_short.py::test_the_idle_relift_runs_no_glx_reset_once_the_at_start_pass_ran` |
| I83 the latch makes no off-bus hold this class (no ladder at hold start, the off-bus one-shot keeps its time; an isolated chip or whole tray never is); a missing-port hold needs a failed full check (accept-listed ports never fail it) | `tests/test_fabric_link_short.py::test_an_offbus_hold_under_the_latch_is_not_this_class`, `::test_a_missing_port_hold_needs_a_failed_full_check` |
| I83 a run that found another class declines once and is not spawned again | `tests/test_fabric_link_short.py::test_an_at_start_run_that_found_another_class_is_not_spawned_again` |
| I83 the at-start pass stands in for the bound's ladder only after a granted host rung; otherwise the ladder runs at 10 min, then I76 | `tests/test_fabric_link_short.py::test_an_at_start_ladder_whose_host_rung_never_fired_does_not_stand_in_for_the_bounds_ladder`, `::test_a_granted_host_rung_after_the_at_start_ladder_runs_no_second_ladder_at_the_bound` |
| I83 under the I42 latch a power cycle denied for a cause other than the cooldown still gets the full ladder at 10 min, latch kept; only a passing full check releases, a failing one asks for the cycle again; a cooldown retry armed across the 10-min mark runs no ladder there and the cycle fires at the cooldown's end | `tests/test_fabric_link_short.py::test_a_latched_hold_whose_power_cycle_is_denied_reruns_the_full_ladder_at_ten_minutes`, `::test_a_latched_hold_cooling_past_the_ten_minute_mark_waits_only_for_the_cooldown` |
| I83/I76 a power cycle denied below the bound and still pending (latched, topology mismatch, missing ports) waits for the full check after the 10-min ladder: a failed check asks for it inside the check window, no verdict asks at ladder end + 300 s, a pass releases with no cycle | `tests/test_fabric_link_short.py::test_a_power_cycle_denied_before_the_ten_minute_ladder_waits_for_that_ladders_check` |
| I83/I76 a retry after no cycle that ran (missed request, backstop, per-boot cap) does not stand in for the 10-min ladder; only the boot / post-power-cycle cooldown's retry does | `tests/test_fabric_link_short.py::test_a_retry_after_no_cycle_that_ran_does_not_defer_the_ten_minute_ladder`, `tests/test_power_cycle_verified_interval.py::test_the_backstop_is_a_stated_bounded_wait_retried_at_its_end`, `tests/test_power_cycle_cooldown.py::test_the_per_boot_cap_denial_shows_on_health`, `::test_a_boot_3_min_before_startup_holds_to_a_deadline_then_cycles_once`, `tests/test_power_cycle_missed_request.py::test_a_request_the_host_never_acted_on_is_asked_again_as_its_window_ends` |
| I83 a re-hold of another class after a carried at-start ladder (carry or broker restart) asks for the host rung once, no ladder per tick | `tests/test_fabric_link_short.py::test_a_rehold_of_another_class_after_a_carried_at_start_ladder_asks_for_the_host_rung_once` |
| I83 with spec 03 I75: on a startup hold the at-start ladder meets a running re-adopted job on `job_running`, stands down to the pending startup gate after the job ends, the gate's full check alone lifts; a failed gate gets the ladder then one power cycle; a below-bound host rung never fires over the running job | `tests/test_fabric_link_short.py::test_a_startup_hold_link_short_leaves_the_at_start_ladder_to_the_startup_gate`, `tests/test_fabric_link_short.py::test_a_below_bound_host_rung_never_fires_over_a_running_readopted_job` |
| I76 `TT_DEVICE_MCP_FORCE_ESCALATE=0` does not stop the bound, and the boot warning says so; a held job past the bound still starts its escalation and below it names the bound; no message claims the hold has no automatic exit | `tests/test_hold_bound.py::test_the_operator_kill_switch_does_not_stop_the_bound`, `tests/test_rung_privilege.py::test_the_force_escalate_kill_switch_warns_that_the_hold_bound_ignores_it`, `tests/test_hold_bound.py::test_a_held_job_past_its_bound_under_the_kill_switch_says_the_bound_still_runs`, `tests/test_device_safety.py::test_hold_bound_counts_from_the_episode_and_reuses_a_spent_escalation`, `tests/test_dispatch_failure_reset.py::test_floor_message_promises_escalation_only_when_armed` |
| I58 all off after a failed reset and a broker restart: the startup or a later gate cycles with no second reset; the sweep skips a failed-unchanged set | `tests/test_classac_power_cycle_direct.py::test_replay_all_off_after_a_restart_cycles_with_no_second_reset`, `::test_a_partial_drop_keeps_the_ladder` |
| I36/I87 a power cycle that is rate-limited or not opted in buys no second mesh reset: a shrinking off-bus set holds for the cycle, and the gate does not run the reset again once its cooldown ran out | `tests/test_host_action_suppress.py::test_a_shrinking_offbus_set_gets_no_second_reset_when_the_power_cycle_cannot_fire`, `::test_the_gate_runs_one_reset_per_episode_when_the_power_cycle_is_not_opted_in` |
| Host-rung chooser: the power cycle is the only host rung; none when it is not armed | `tests/test_device_safety.py::test_choose_escalation_power_cycle_is_the_only_host_rung`, `tests/test_device_safety.py::test_choose_escalation_none_when_the_power_cycle_is_not_armed` |
| Bridge reset: recovers, retries, inapplicable falls through, stale bus refused | `tests/test_device_safety.py::test_recovery_resets_through_the_bridge_and_clears_the_chip`, `tests/test_device_safety.py::test_bridge_reset_retries_before_escalating`, `tests/test_device_safety.py::test_an_inapplicable_bridge_reset_is_not_retried`, `tests/test_device_safety.py::test_an_all_inapplicable_bridge_reset_never_reads_as_recovered`, `tests/test_device_safety.py::test_a_reused_bus_refuses_the_bridge_so_a_stale_address_never_sbrs_live_silicon`, `tests/test_device_safety.py::test_bridge_reset_does_not_reach_a_gone_endpoint_by_default`, `tests/test_device_safety.py::test_a_bridge_less_chip_is_rescanned_before_anything_destructive` |
| I16 tray identity is the bus group, per architecture, never the chip index or list position | `tests/test_ubb_tray_map.py::test_a_blackhole_tray_comes_from_the_bus_group_not_the_chip_index`, `::test_a_positional_bus_list_is_refused_not_read_by_position`, `::test_a_wormhole_tray_uses_the_wormhole_table_for_the_same_buses` |
| I16 the map is all-or-nothing (unreadable bus, unknown group, non-Galaxy or unknown board) | `tests/test_ubb_tray_map.py::test_an_unreadable_bus_id_yields_no_map_rather_than_a_partial_one`, `::test_a_bus_group_outside_the_table_yields_no_map`, `::test_a_non_galaxy_board_type_yields_no_map`, `::test_an_unknown_board_type_yields_no_map` |
| I16 a missing chip never shifts another chip's tray | `tests/test_ubb_tray_map.py::test_a_missing_chip_does_not_shift_any_other_chips_tray` |
| I16 the affected set keys on the real tray | `tests/test_ubb_tray_map.py::test_affected_trays_names_the_tray_an_operator_would_read_from_tt_smi` |
| I16 no map means tray placement declines, never arithmetic | `tests/test_ubb_tray_map.py::test_without_a_map_every_tray_decision_declines`, `::test_a_chip_the_map_does_not_place_declines_rather_than_resetting_the_rest` |
| I16 the chip→bus map comes from sysfs, is banked by the gate's own pass, and reaches the log line | `tests/test_ubb_tray_map.py::test_the_snapshot_caches_every_chips_bus_id`, `::test_the_tray_map_keys_on_the_kernels_chip_ids_not_the_snapshots_list_order` |
| I16 sysfs and tt-smi disagreeing banks nothing and journals `chip_bus_map_mismatch` | `tests/test_ubb_tray_map.py::test_sysfs_and_tt_smi_disagreeing_banks_no_map_and_logs_both` |
| I16 a boot platform probe (`expected_count=0`), a degraded first pass, or any short snapshot never freezes the map | `tests/test_ubb_tray_map.py::test_a_partial_boot_snapshot_never_freezes_the_map`, `::test_a_degraded_first_normal_snapshot_cannot_freeze_the_map`, `tests/test_reset.py::test_bank_bus_ids_once_rejects_a_snapshot_shorter_than_the_mesh`, `::test_bank_bus_ids_once_rejects_a_boot_probe_that_did_not_ask_the_question`, `::test_bank_bus_ids_once_rejects_a_chip_enumerated_without_a_bus_id`, `::test_bank_bus_ids_once_rejects_an_empty_list`, `::test_bank_bus_ids_once_first_full_read_stands`, `::test_bank_bus_ids_once_stays_open_after_a_rejected_snapshot` |
| I16 the board type is normalized before selecting the table (WH ` L`/` R` suffix) | `tests/test_ubb_tray_map.py::test_a_wormhole_snapshot_with_l_r_suffixes_still_produces_a_tray_map`, `::test_a_wormhole_lookup_survives_the_snapshot_suffix_inside_tray_map` |
| I16 a trusted full read whose chip→bus map differs re-banks it, journals `chip_bus_map_drift` with the old and the new map, and the next mask follows the chip's new bus | `tests/test_ubb_tray_map.py::test_a_renumbered_full_read_re_banks_the_map_and_journals_old_and_new` |
| I16 a short read, an unreadable chip, or a read tt-smi disagrees with never overwrites a banked map; a matching full read changes nothing | `tests/test_ubb_tray_map.py::test_a_short_or_disagreeing_read_never_overwrites_the_banked_map`, `::test_a_full_read_that_matches_the_banked_map_changes_nothing` |
| I16 tt-smi's bus list drifting journals `bus_map_drift`; the list stands | `tests/test_ubb_tray_map.py::test_a_drifted_snapshot_journals_but_leaves_the_cached_map_standing` |
| I16 a new GLX board type this build cannot map surfaces as `ubb_tray_table_missing` | `tests/test_ubb_tray_map.py::test_a_glx_board_type_with_no_matching_arch_suffix_journals_once` |
| I19 a tray with 4+ chips off at first sighting is an onset; no map, an unplaced chip, or fewer off gets no prelude | `tests/test_tray_down_prelude.py::test_a_tray_with_four_or_more_chips_off_is_a_tray_down_onset`, `::test_no_map_or_an_unplaced_chip_never_classifies_as_tray_down`, `::test_one_to_three_chips_off_get_no_prelude` |
| I19 replay: no recorded episode that recovered without a power cycle gets the prelude | `tests/test_tray_down_prelude.py::test_replay_the_prelude_runs_only_on_onsets_that_never_recovered_without_a_power_cycle` (fixture `tests/fixtures/tray_down_replay.tsv`) |
| I19 order: rescan, capture, the full ladder, the power cycle only when the ladder fails (a whole tray, class A, goes to the cycle after the capture: I37) | `tests/test_tray_down_prelude.py::test_a_tray_down_onset_rescans_captures_then_runs_its_ladder`, `::test_a_ladder_that_recovers_the_tray_fires_no_power_cycle`, `::test_the_last_chance_sweep_still_gates_the_power_cycle_of_a_tray_down` |
| I19 the prelude changes no ladder input (same stage, indices, evidence, beats and rungs as with it off) | `tests/test_tray_down_prelude.py::test_the_prelude_changes_no_ladder_input`, `::test_capture_off_runs_the_ladder_alone` |
| I19 once per episode from any caller; a tenant defers the rescan, never the capture | `tests/test_tray_down_prelude.py::test_the_prelude_runs_once_per_episode_from_any_caller`, `::test_a_tenant_defers_the_rescan_but_not_the_capture` |
| I19 after the rescan: every chip back runs the full verify and stops only if it passes; the runtime fault is kept and the chips leave the isolated set | `tests/test_tray_down_prelude.py::test_every_chip_back_after_the_rescan_runs_the_full_verify_and_stops`, `::test_a_rescan_recovery_keeps_the_runtime_fault_and_un_isolates_the_chips`, `::test_chips_back_on_a_mesh_that_fails_verify_still_get_the_full_ladder` |
| I19 an off set first seen after a reset (the ladder's, a manual `reset_with_quiesce`, one in flight, a live scope) is not an onset; the latch is fresh after the episode | `tests/test_tray_down_prelude.py::test_the_latch_does_not_change_after_a_reset_and_is_fresh_after_the_episode`, `::test_a_manual_reset_before_the_first_sighting_is_not_an_onset`, `::test_a_reset_in_flight_or_a_live_scope_is_not_an_onset` |
| I19 the latch is not persisted: a restarted broker latches a tray still missing afresh, and buys no power cycle | `tests/test_tray_down_prelude.py::test_a_restarted_broker_latches_a_tray_still_missing_afresh` |
| I19 the gate (`device_health_gate`) runs the prelude, then its own rung | `tests/test_tray_down_prelude.py::test_the_gate_runs_the_prelude_then_its_ladder_end_to_end` |
| I19 the whole bus off keeps its own route | `tests/test_tray_down_prelude.py::test_the_whole_bus_off_keeps_its_own_route` |
| I19 the capture is read-only, CPLD reads (every configured tray, the PDB) come only from config, BMC reads get the long timeout, it meets its deadline, fsyncs, and is written even with no incident bundle | `tests/test_tray_down_prelude.py::test_the_allow_list_refuses_anything_but_the_read_shapes`, `::test_cpld_reads_come_only_from_config_and_cover_every_configured_tray`, `::test_bmc_reads_get_the_long_timeout_inside_the_deadline`, `::test_the_capture_meets_its_deadline_survives_a_missing_tool_and_fsyncs_the_bundle`, `::test_a_missing_incident_bundle_still_gets_the_bmc_reads_written` |
| I19 lspci targets the upstream bridge of every off chip, by sysfs parent or secondary bus, plus the endpoint while present | `tests/test_tray_down_prelude.py::test_lspci_targets_the_bridge_above_each_off_chip`, `::test_the_capture_reads_the_bridge_of_every_off_chip` |
| Last-chance sweep gates EVERY host rung (gate ladder, idle escalation); a sweep that recovers cancels the cycle; never over a tenant; no per-chip SBR on a mass drop | `tests/test_ladder_v2.py::test_gate_ladder_sweeps_every_reset_before_the_power_cycle`, `tests/test_ladder_v2.py::test_a_sweep_that_recovers_the_mesh_cancels_the_power_cycle`, `tests/test_ladder_v2.py::test_the_idle_escalation_road_to_a_power_cycle_goes_through_the_sweep`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_tenant`, `tests/test_device_safety.py::test_a_mass_gone_drop_is_left_to_the_galaxy_reset` |
| A RUNNING broker job counts as a tenant even with an empty holder scan; `force` waives an unreadable scan but never a running job; no rung hand-rolls the fd-only check | `tests/test_ladder_v2.py::test_a_running_job_is_a_tenant_even_with_an_empty_holder_scan`, `tests/test_ladder_v2.py::test_force_waives_an_unreadable_scan_but_never_a_running_job`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_running_job`, `tests/test_ladder_v2.py::test_every_tenant_guard_goes_through_the_one_helper` |
| Gate rungs release without over-climbing | `tests/test_device_safety.py::test_a_recovered_bridge_reset_releases_without_the_mesh_wide_reset`, `::test_a_recovered_mass_drop_reset_releases_without_reaching_the_host_rungs` |
| escalate() outcome mapping; unknown phase rejected | `tests/test_device_safety.py::test_escalate_waits_when_nothing_ran`, `tests/test_device_safety.py::test_escalate_recovers_on_a_present_mesh_reset`, `tests/test_device_safety.py::test_escalate_reports_terminal_when_a_present_mesh_reset_fails_and_climbs`, `tests/test_device_safety.py::test_escalate_does_not_report_recovered_when_the_mesh_was_never_degraded`, `tests/test_device_safety.py::test_escalate_rejects_an_unknown_phase` |
| Forced ladder has teeth on every platform | `tests/test_device_safety.py::test_force_escalate_has_teeth_on_a_per_target_host`, `tests/test_device_safety.py::test_hold_past_ceiling_triggers_forced_escalation` |
| Boot attribution: a stale escalation or one fired on this same boot is not attributed to it | `tests/test_device_safety.py::test_boot_attribution_rejects_a_stale_escalation`, `tests/test_device_safety.py::test_an_escalation_fired_on_this_same_boot_is_not_attributed_to_it` |
| Reset tool contract: steps/attribution/no-devices/job stop | `tests/test_reset.py::test_reset_success_reports_steps_and_command`, `tests/test_reset.py::test_reset_failure_surfaces_returncode_and_output`, `tests/test_reset.py::test_explicit_reset_leaves_one_row_attributed_to_the_caller`, `tests/test_reset.py::test_reset_no_devices_does_not_call_tt_smi`, `tests/test_reset.py::test_a_reset_stops_the_running_job_gracefully`, `tests/test_reset.py::test_reset_device_quiesce_scope_routes_a_live_privsep_job` |
| Rung inventory stated loudly at boot | `tests/test_rung_arming.py::test_a_rung_that_is_off_is_stated_loudly` |
| I69 a removed chip is reset through its recorded bridge before the rescan; a stale record is refused; no rescan for a removed un-reset chip or when the gate left it un-reset; the bare rescan stays for a chip the broker did not remove, gate refusal or not | `tests/test_removed_chip_rescan.py::test_isolate_records_the_parent_bridge_before_the_remove`, `::test_a_removed_chip_is_reset_through_its_recorded_bridge_then_rescanned`, `::test_a_stale_recorded_bridge_is_refused`, `::test_no_rescan_for_a_removed_chip_without_privilege`, `::test_no_rescan_when_the_host_reset_gate_refuses`, `::test_a_gate_refusal_keeps_the_bare_rescan_for_a_chip_the_broker_did_not_remove`, `::test_no_rescan_when_a_removed_chip_has_no_usable_bridge`, `::test_every_removed_chip_is_reset_before_the_first_rescan`, `::test_the_bare_rescan_stays_for_a_chip_the_broker_did_not_remove`, `::test_a_returned_chip_drops_its_removal_record` |
| I69 removal records survive a broker restart in the same boot, their chips are isolated again and reset through the recorded bridge before any rescan, also on a pass asked for another chip; any un-reset record withholds the bare rescan; an earlier boot's record is dropped; an unchanged start does not rewrite the file; a record clears whatever re-enumerates its address, not on a reused index; a failed save keeps the record; a removed chip left off the bus keeps the no-window class | `tests/test_removed_chip_rescan.py::test_a_removed_chip_record_survives_a_broker_restart`, `::test_boot_broker_reloads_and_isolates_the_removed_chips`, `::test_a_reloaded_record_still_withholds_the_bare_rescan`, `::test_a_pass_for_another_chip_resets_the_reloaded_chip_before_any_rescan`, `::test_an_unreset_record_outside_the_targets_withholds_the_bare_rescan`, `::test_a_record_from_an_earlier_boot_is_dropped_at_start`, `::test_an_unchanged_reload_does_not_rewrite_the_file`, `::test_a_record_clears_whatever_brings_the_chip_back`, `::test_a_record_clears_when_its_address_is_enumerated_again`, `::test_a_record_is_kept_while_only_its_index_is_reused`, `::test_a_failed_save_never_breaks_recovery`, `::test_a_removed_chip_that_stays_off_bus_keeps_the_no_window_class` |
| I69 a removed chip's SBR owes the AICLK ceiling (03 I35); one 1 s retrain settle follows the last SBR before the rescan; the never-returns worst case is 44.3 s of waits, 14 + 30.3 N s for N chips | `tests/test_removed_chip_rescan.py::test_a_removed_chip_reset_owes_the_aiclk_ceiling`, `::test_the_link_retrains_after_the_last_sbr_before_the_rescan`, `::test_a_removed_chip_rung_stays_inside_its_stated_worst_case`, `::test_the_removed_chip_rung_worst_case_for_two_chips` |
| I69 the tray-down prelude withholds its rescan while a removed chip is un-reset and goes straight to the ladder; with no record it rescans as before | `tests/test_tray_down_prelude.py::test_the_prelude_rescan_never_re_enumerates_a_removed_un_reset_chip` |
| I69 a failed mesh reset (rc!=0 and no seen op_mode 0x3 re-power) withholds both rescans while a removed chip exists, journals `mesh_reset_failed` and climbs to the next rung; a seen re-power or rc 0 rescans as before; no removed chip keeps the bare rescan; the driver-wait rescan starts only past the re-power; a failed or unproven full check after the reset keeps the hold, only a passing one, fabric included, releases; the 10-min ladder still fires | `tests/test_mesh_reset_rescan_withhold.py::test_bmc_step_ok_reads_the_re_power_from_the_output`, `::test_a_failed_mesh_reset_withholds_the_rescan_of_an_unreset_removed_chip`, `::test_a_failed_mesh_reset_climbs_to_the_next_rung_and_keeps_the_hold`, `::test_a_mesh_reset_that_re_powered_the_trays_rescans_as_before`, `::test_a_chip_the_broker_never_removed_keeps_the_bare_rescan`, `::test_the_driver_wait_rescan_starts_only_after_the_bmc_re_power_went_out`, `::test_a_clean_mesh_reset_still_owes_the_full_check_before_release`, `::test_a_withheld_rescan_does_not_stop_the_ten_minute_ladder` |
| I69/03 I4 the bridge rung's gate path runs the full check, fabric included, before clearing dirty; a failed check holds | `tests/test_removed_chip_rescan.py::test_gate_bridge_stage_verifies_with_fabric_before_clearing_dirty`, `::test_gate_bridge_stage_failed_verify_holds` |
| Stage metrics: structural inapplicability is not "blocked" | `tests/test_metrics.py::test_bridge_reset_missing_pci_address_is_not_applicable_not_blocked` |
| I75 (the earliest armed deadline: cooldown end, missed retry, AC-power recheck, off-bus one-shot, ceiling window) | `tests/test_health_hold_report.py::test_class_a_inside_the_cooldown_reports_the_cycle_at_the_cooldown_end`, `tests/test_health_hold_report.py::test_missed_power_cycle_retry_is_named`, `tests/test_health_hold_report.py::test_ac_needed_reports_the_recheck`, `tests/test_health_hold_report.py::test_class_b1_isolated_chips_report_the_off_bus_one_shot`, `tests/test_health_hold_report.py::test_class_b1_after_its_one_shot_reports_the_ceiling`, `tests/test_health_hold_report.py::test_a_later_ceiling_window_is_reported_after_the_first`, `tests/test_health_hold_report.py::test_cooldown_candidate_and_next_rung` |
| I75 (a fabric-77-only hold or the forced escalation off: forced rungs no earlier than the I76 bound) | `tests/test_health_hold_report.py::test_fabric_77_only_hold_names_the_10_min_bound`, `tests/test_health_hold_report.py::test_force_escalate_off_names_the_10_min_bound` |
| I75 (a branch stops the chain only once due: the I76 host rung after the bound's ladder) |, `tests/test_health_hold_report.py::test_the_bound_host_rung_follows_the_bound_ladder` |
| I75 (the off-bus forced pass bounds: gated grace, IPMI hand-off, gated operator hand-off; only after the ladder ran) | `tests/test_health_hold_report.py::test_the_off_bus_forced_pass_bounds_are_named_when_they_fire`, `tests/test_health_hold_report.py::test_the_off_bus_pass_waits_for_the_ladder_to_have_run` |
| I75 (a declined deadline stays reported with `next_rung_due`; no episode clock names no forced rung) | `tests/test_health_hold_report.py::test_a_declined_window_stays_reported_as_due`, `tests/test_health_hold_report.py::test_no_episode_clock_names_no_forced_rung` |
| I75 (the I70 unkillable-leftover host rung is named when the code fires it; a class-A hold in the cooldown has no second 10-min start beside the I76 bound's) | `tests/test_health_hold_report.py::test_the_unkillable_leftover_host_rung_is_named`, `tests/test_health_hold_report.py::test_a_class_a_hold_in_the_cooldown_has_no_second_10_min_trigger` |
| I75 (the I65 retry only when armed for the episode and not refused by a guard; a pending host action is reported, never dropped) | `tests/test_health_hold_report.py::test_the_cooldown_retry_is_named_only_when_the_watchdog_asks_it`, `tests/test_health_hold_report.py::test_the_cooldown_retry_fires_when_reported`, `tests/test_health_hold_report.py::test_a_pending_host_action_is_reported_and_left_alone` |
| I75 (one routing for the report and the forced run: a window inside the post-ladder check, a window after a ladder that did not recover, a class-A hold inside the check window, a startup-gate stand-down; each reported where the watchdog acts) | `tests/test_health_hold_report.py::test_the_report_names_the_rung_the_forced_run_routes_to`, `tests/test_health_hold_report.py::test_a_class_a_hold_inside_the_check_window_reports_the_host_rung`, `tests/test_health_hold_report.py::test_a_startup_verify_stand_down_is_reported_with_its_bounded_end` |
| I75 (deadline feeds: the broker registers the I76 bound; its mark and source per fault class and latch, fired by the watchdog then; inside a cooldown the ladder stays at the mark and only the host rung names the end; silent once spent, past due, pending, tenant-blocked or stood down; names the opted-in host rung; nothing when HEALTHY; read-only; a broken feed reports nothing) | `tests/test_health_hold_report.py::test_the_broker_registers_its_hold_bound_feed`, `tests/test_health_hold_report.py::test_the_bound_mark_is_reported_for_each_fault_class`, `tests/test_health_hold_report.py::test_a_latched_hold_reports_the_same_mark`, `tests/test_health_hold_report.py::test_a_cooldown_hold_reports_the_ladder_at_the_mark_and_the_host_rung_at_its_end`, `tests/test_health_hold_report.py::test_the_hold_bound_candidate_names_the_cooldown_only_for_its_host_rung`, `tests/test_health_hold_report.py::test_after_the_bound_ladder_the_chain_names_the_host_rung`, `tests/test_health_hold_report.py::test_the_bound_feed_is_silent_when_its_mark_is_not_next`, `tests/test_health_hold_report.py::test_the_bound_feed_names_the_host_rung_the_box_opts_into`, `tests/test_health_hold_report.py::test_the_bound_feed_yields_to_a_startup_stand_down`, `tests/test_health_hold_report.py::test_a_healthy_device_reports_no_bound`, `tests/test_health_hold_report.py::test_the_bound_feed_moves_no_clock`, `tests/test_health_hold_report.py::test_a_broken_feed_reports_nothing` |
| I75 (a power-cycle step the governor still gates is reported at the latest of the backstop and the cooldown, each named; a ladder step keeps its time; the gates read marks nothing) | `tests/test_health_hold_report.py::test_a_power_cycle_the_governor_still_gates_is_reported_at_the_latest_gate`, `tests/test_health_hold_report.py::test_the_governor_gates_move_only_a_power_cycle_step`, `tests/test_health_hold_report.py::test_the_power_cycle_gates_read_marks_nothing` |
| I86 the last-chance sweep re-probes the bus after SBR: a whole bus runs the full check before any later rung and lifts only on its pass; a failed check runs the remaining rung then the power cycle; a short or unreadable bus fires every rung; a whole tray still skips the sweep | `tests/test_sweep_reprobe.py::test_healed_after_sbr_runs_no_later_rung_and_lifts_only_on_the_full_check`, `::test_bus_whole_but_full_check_fails_runs_the_remaining_rung_then_the_caller_climbs`, `::test_bus_whole_check_fails_then_the_mesh_reset_heals_it_lifts_on_the_second_check`, `::test_never_healed_runs_every_rung_back_to_back_with_one_check`, `::test_an_unreadable_bus_probe_fires_the_next_rung`, `::test_the_gate_ladder_power_cycles_only_when_the_full_check_still_fails`, `::test_a_whole_tray_drop_still_skips_the_sweep`; every hold kind still reaches the ladder at 10 min: `tests/test_hold_bound.py::test_every_hold_kind_runs_the_ladder_at_the_bound_then_the_host_rung` |
| I88 a broker power cycle brings the OS down first and never cuts power under a live OS: BMC pre-flight, request (with the per-try timeout), fallback unit outside the shutdown that survives its killall under an `@` argv[0], then the reboot; the fallback fires the same argv at the clamped bound; a missing hook or command, a failed pre-flight, an unwritable request, a fallback that cannot start or a failed reboot undo their steps and raise PowerCycleNotLaunched; a fallback that will not stop keeps the action pending; the escalation retracts, holds and pages once per episode and never reboots; a refuted cycle is an I72 miss, anchors no I65 cooldown and counts toward the backstop; the next boot confirms, refutes (missed, never late) or leaves the cycle unconfirmed from the BMC restart cause, and a BMC with no cause confirms it from a SEL power-off between the request and the boot (on the BMC clock's offset), never refutes from the SEL; the hook fires only on reboot with a fresh request, stops the fallback and its process tree before any wait, 3 tries at the 5-15 s clamped timeout within systemd-shutdown's 90 s, then a warm reboot; the installer installs it | `tests/test_power_cycle_os_first.py::test_clean_path_checks_the_bmc_writes_the_request_then_the_fallback_then_reboots`, `::test_the_fallback_unit_stays_out_of_the_shutdown_and_survives_its_killall`, `::test_a_hung_shutdown_reaches_the_cycle_once_the_bound_passes`, `::test_the_fallback_fires_under_an_at_sign_argv0_and_its_timeout`, `::test_the_shutdown_bound_is_always_finite_and_clamped`, `::test_no_hook_installed_raises_not_launched_and_runs_no_step`, `::test_an_unwritable_request_raises_not_launched`, `::test_a_fallback_unit_that_cannot_start_never_reboots`, `::test_a_failed_reboot_stops_the_fallback_and_raises_not_launched`, `::test_a_fallback_that_will_not_stop_keeps_the_action_pending`, `::test_an_unanswered_is_active_reads_as_alive`, `::test_a_stopped_fallback_after_a_failed_stop_raises_not_launched`, `::test_an_argv_the_hook_cannot_carry_raises_not_launched`, `::test_a_missing_cycle_command_raises_not_launched`, `::test_a_bmc_that_fails_the_preflight_never_reboots`, `::test_the_preflight_keeps_the_configured_commands_bmc_options`, `::test_an_opaque_configured_command_gets_only_the_executable_check`, `::test_the_configured_command_never_runs_under_a_live_os`, `::test_unavailable_without_systemd_or_the_hook`, `::test_installed_shutdown_hook_needs_an_executable_file`, `::test_the_escalation_is_unchanged_around_the_clean_path`, `::test_a_cycle_that_cannot_launch_retracts_holds_and_pages_once_per_episode`, `::test_a_preflight_failure_retracts_and_never_reboots`, `::test_a_fallback_left_alive_keeps_the_action_pending_and_the_ledger`, `::test_a_confirmed_cycle_counts_as_before`, `::test_a_refuted_cycle_is_counted_missed_never_late`, `::test_a_bmc_that_never_reports_a_cause_leaves_the_cycle_unconfirmed_and_counted`, `::test_a_cause_the_bmc_cannot_give_says_why_in_the_row_and_the_event`, `::test_a_cause_the_bmc_gives_carries_no_unavailable_reason`, `::test_unknown_from_a_bmc_never_seen_to_report_a_cause_is_unconfirmed`, `::test_a_boot_long_after_the_request_is_not_checked`, `::test_the_hooks_worst_case_fits_systemd_shutdowns_hook_timeout`, `::test_the_per_try_timeout_honours_the_configured_one_within_the_hook_budget`, `::test_hook_fires_the_requested_cycle_after_flushing_on_reboot`, `::test_hook_never_cycles_on_any_other_verb`, `::test_hook_does_nothing_without_a_request`, `::test_hook_ignores_a_stale_request`, `::test_hook_ignores_a_request_with_no_command`, `::test_hook_retries_a_failing_cycle_then_lets_the_reboot_continue`, `::test_the_installer_installs_the_hook_where_the_broker_looks`, `::test_hook_stops_the_fallback_and_its_children_before_any_wait`, `::test_hook_stops_the_fallback_first_and_kills_its_process_group`, `::test_a_refuted_cycle_is_retried_under_i72_and_pages_from_the_second`, `::test_refuted_cycles_count_toward_the_backstop`, `::test_restart_cause_names_the_raw_code`, `::test_rtc_wakeup_confirms_the_cycle`, `::test_an_unmapped_code_is_unconfirmed_and_stamps_nothing`, `::test_a_warm_reboot_that_healed_asks_no_further_cycle`, `::test_a_warm_reboot_still_held_asks_the_cycle_again_in_the_new_episode`, `::test_a_warm_reboot_still_held_under_a_cooldown_or_the_backstop_waits_for_the_governor`, `::test_the_missed_retry_leaves_a_cooldown_retry_of_the_same_episode`, `::test_a_bmc_with_no_restart_cause_confirms_the_cycle_from_its_sel`, `::test_the_sel_is_read_on_the_bmc_clock_whatever_its_zone`, `::test_no_power_off_in_the_window_leaves_the_cycle_unconfirmed_never_refuted`, `::test_a_failed_sel_read_leaves_the_cycle_unconfirmed`, `::test_the_power_off_is_found_under_later_records_in_the_whole_query_output`, `::test_the_sel_queries_keep_the_cycle_commands_bmc_options`, `::test_a_cause_the_bmc_answers_never_reads_the_sel`, `tests/test_fsm.py::test_boot_merge_names_a_refuted_cycle_a_warm_reboot`, `tests/test_power_cycle_host_rung.py::test_a_cycle_that_cannot_launch_os_first_never_reboots_and_holds` |
| I89 a below-floor off-bus hold: the gate rescans at once and lifts only on a passing full check with fabric (a check short of the cabled links keeps the hold); SBR keeps its defaults before the bound (a gone chip only when opted in, never an enumerated chip), runs once per episode and off-bus set and waits out a tenant; at the bound every kept rung runs in order (rescan, SBR of the bridged off-bus chips, the one mesh reset, full check) then the power cycle; without the opt-in a gone chip gets no SBR try and is not queued, and a chip the rung could not fire on is un-queued, a spent mesh reset is not repeated (I87, I22), a cooldown arms the retry; below the bound I58 still climbs at once | `tests/test_below_floor_offbus_ladder.py::test_the_bound_runs_every_kept_rung_in_order_then_the_power_cycle`, `::test_the_bound_rescans_and_sbrs_before_the_cycle_after_a_failed_mesh_reset`, `::test_a_forced_run_below_the_bound_repeats_a_failed_mesh_reset_before_the_power_cycle`, `::test_sbr_that_heals_at_the_bound_lifts_on_the_full_check_with_no_mesh_reset`, `::test_an_enumerated_chip_is_never_sbrd_even_at_the_bound`, `::test_below_the_bound_a_gone_chip_waits_for_the_opt_in_and_the_host_rung_for_the_bound`, `::test_below_the_bound_a_mesh_reset_that_failed_on_the_same_chips_climbs_at_once`, `::test_a_cooldown_denial_at_the_bound_still_runs_the_ladder_and_arms_the_retry`, `::test_the_gate_rescans_at_once_and_lifts_only_on_the_full_check`, `::test_the_gate_sbrs_a_gone_chip_when_opted_in_once_per_episode_and_set`, `::test_the_gate_keeps_the_sbr_defaults`, `::test_the_gate_waits_and_retries_when_a_tenant_holds_the_device`, `::test_a_fabric_check_short_of_the_cabled_links_keeps_the_hold`, `::test_without_the_opt_in_the_bound_tries_no_sbr_on_a_gone_chip_and_queues_nothing`, `::test_a_bridged_gone_chip_reaches_the_bridge_write_at_the_bound`, `::test_an_opted_in_gone_chip_with_no_bridge_is_unqueued`; a node-present chip whose link reads down or whose heartbeat reads all-ones is removed with the readers stopped (killed once every remove worked, left stopped after a failed one), then SBR'd, and a failed check after it keeps the hold for the power cycle; a chip whose remove fails is not reset; the climb's sweep runs its SBR when none ran this hold, lifting only on a passing full check, and owes none once one ran, nor at or above the floor (all chips off included): `::test_a_dead_link_chip_with_its_node_present_is_isolated_then_sbrd_at_the_bound`, `::test_a_dead_link_chip_whose_remove_fails_is_not_reset`, `::test_the_sweep_runs_its_sbr_when_none_ran_this_hold`, `::test_the_sweep_owes_no_sbr_once_this_hold_ran_one`, `::test_the_sweep_sbr_is_below_the_floor_only`; SBR defaults before the bound: `tests/test_device_safety.py::test_a_stalled_but_enumerated_chip_is_not_treated_as_gone`, `tests/test_device_safety.py::test_a_gone_chip_is_left_to_the_hold_by_default` |
| I90 the bridge rung's detail starts `device reset:`; /status during it has `device_resetting` and the op row's `resetting` true, false for a non-reset op and after it | `tests/test_removed_chip_rescan.py::test_status_shows_the_bridge_rung_as_a_device_reset` |
| I91 the guard refuses op_mode `0x0`/`0x00`/`0`/`00` (any case, decimal, octal, over lanplus), a missing or unreadable op byte, `exec` and `shell`; it passes `0x3` and read-only calls; a refused call never execs the real binary; the mounted copy refuses as ipmitool; no env/flag/config is read | `tests/test_ipmi_guard.py::test_the_guard_refuses_the_legacy_retimer_reset`, `tests/test_ipmi_guard.py::test_the_guard_passes_op_mode_3_and_read_only_calls`, `tests/test_ipmi_guard.py::test_a_refused_call_never_execs_the_real_binary`, `tests/test_ipmi_guard.py::test_the_mounted_copy_refuses_as_ipmitool`, `tests/test_ipmi_guard.py::test_nothing_switches_the_guard_off` |
| I91 arming mounts the guard read-only over every target and fails closed (a mount that fails or does not read back, the host namespace); a wrapper that cannot arm runs no reset | `tests/test_ipmi_guard.py::test_arm_mounts_the_guard_over_every_target_read_only`, `tests/test_ipmi_guard.py::test_arm_fails_closed_when_a_target_is_not_the_guard_after_mounting`, `tests/test_ipmi_guard.py::test_arm_fails_closed_when_a_mount_fails`, `tests/test_ipmi_guard.py::test_the_wrapper_runs_no_reset_when_it_cannot_arm`, `tests/test_ipmi_guard.py::test_the_wrapper_refuses_to_mount_in_the_host_namespace` |
| I91 a `TT_DEVICE_MCP_RESET_ARGS` override that names the Galaxy flag (sudo, env, `python -m tt_smi`, a wrapper script) gets the preflight and the guard; one naming neither that nor tt-smi is refused (rc None, no launch, no cooldown), journaled, shown in /health, and the ladder climbs with the hold kept until a full check passes | `tests/test_ipmi_guard.py::test_is_mesh_reset`, `tests/test_ipmi_guard.py::test_override_unclassified`, `tests/test_ipmi_guard.py::test_an_override_mesh_reset_in_any_form_gets_the_preflight_and_the_guard`, `tests/test_ipmi_guard.py::test_an_unclassifiable_override_is_refused_and_the_ladder_climbs_with_the_hold_kept`, `tests/test_ipmi_guard.py::test_a_plain_tt_smi_override_runs_unguarded` |
| I91 both reset backends launch a mesh reset (not `-r`) under the guard; every reset path (gate, ladder, sweep, operator reset, startup) reaches them through `reset_with_quiesce` and nothing else builds a mesh-reset argv | `tests/test_ipmi_guard.py::test_the_scope_backend_launches_a_mesh_reset_under_the_guard`, `tests/test_ipmi_guard.py::test_the_local_backend_launches_a_mesh_reset_under_the_guard`, `tests/test_ipmi_guard.py::test_every_reset_path_goes_through_the_guarded_backends` |
| I91 the preflight (read-only `mc info`, 10 s bound, firmware >= 0.05.22) refuses a silent BMC or old firmware; a refusal runs no reset, arms no cooldown, keeps the hold, climbs and shows in /health | `tests/test_ipmi_guard.py::test_the_preflight`, `tests/test_ipmi_guard.py::test_the_preflight_is_read_only_and_bounded`, `tests/test_ipmi_guard.py::test_firmware_older_than_0_05_22_is_refused`, `tests/test_ipmi_guard.py::test_a_preflight_refusal_is_a_rung_that_did_not_fire_and_keeps_the_hold` |
| I91 tt-smi's fallback text or the guard's refusal: rc 0 becomes 1, `bmc_legacy_reset_attempted` host_at_risk, paged once; an unarmed guard (or `unshare` failing) is a reset that did not fire; a guarded reset still owes the full check, fabric included, before release | `tests/test_ipmi_guard.py::test_the_fallback_never_reads_as_a_passing_reset_and_pages_once`, `tests/test_ipmi_guard.py::test_a_guard_that_cannot_arm_is_a_reset_that_did_not_fire`, `tests/test_ipmi_guard.py::test_a_guarded_reset_still_owes_the_full_check_before_release` |

| I92 before every reset (mesh reset, sweep, operator reset) the holder scan runs after the pollers stop: a reader gets SIGTERM, 10 s, SIGKILL; a tenant refuses an automatic reset and is named in the event and /health; an operator reset is not refused by a tenant; an off-bus bound chip is detached before any kill and an undetachable one keeps every reader alive; a D-state reader is fenced for the host rung; a refused reset keeps the fabric owed; a zombie the driver names whose thread group exited is no holder unless its file is open elsewhere or the walk is incomplete; one whose live thread holds the device refuses; one with live threads that cannot be told is polled every 1 s, once per pid and start time, until the reserve (the scan's last phases, the mesh reset and a full check with fabric sized to the chips) before the 10-min mark, measured from the mark and not the cooldown-extended deadline (30 s for an operator reset), so a zombie that clears on the last poll has its reset and full check done before the mark, then refuses and the hold still gets the ladder at 10 min; the scan event names its parent and thread count; real processes show the kernel facts; a scan bug lets the reset run; the bridge rung journals `targets_detached` and kills nothing | `tests/test_pre_reset_holder_scan.py::test_every_reset_kills_the_reader_after_the_pollers_stop_and_before_the_reset`, `::test_a_reader_gets_sigterm_then_sigkill_after_the_10s_grace`, `::test_a_tenant_refuses_an_automatic_reset_named_in_health_and_the_fabric_stays_owed`, `::test_an_operator_reset_is_not_refused_by_a_tenant`, `::test_an_off_bus_chip_is_detached_before_any_reader_is_killed`, `::test_an_off_bus_chip_that_cannot_be_detached_keeps_its_readers_alive`, `::test_on_bus_chips_readers_are_killed_with_no_detach`, `::test_an_unkillable_reader_is_fenced_for_the_host_rung`, `::test_a_scan_bug_does_not_stop_the_reset`, `::test_the_bridge_rung_journals_its_detached_targets_and_kills_nothing`; `::test_a_detach_that_blocks_in_the_kernel_is_bounded_and_refuses_the_reset`, `::test_a_holder_inside_a_reset_is_never_signalled_and_the_reset_is_refused`, `::test_a_reset_already_running_refuses_before_the_scan`, `::test_a_registered_local_reset_and_its_child_read_as_in_reset`, `::test_a_refused_mesh_reset_is_journalled_as_refused_and_settles_no_debt`, `::test_a_refused_sweep_reset_is_journalled_and_releases_the_aer_mask`, `::test_a_pid_gap_is_rescanned_once_then_refuses_an_automatic_reset`, `::test_a_pid_gap_that_clears_on_the_rescan_lets_the_reset_proceed`, `::test_an_operator_reset_records_a_pid_gap_and_still_clears_the_readers`, `::test_a_gap_no_pid_explains_is_recorded_and_the_reset_proceeds`, `::test_a_zombie_whose_whole_thread_group_exited_is_no_holder_and_the_reset_runs_at_once`, `::test_an_exited_zombie_whose_file_may_be_open_elsewhere_still_refuses`, `::test_a_zombie_leader_with_a_live_thread_holding_a_device_fd_is_refused`, `::test_a_zombie_leader_whose_live_threads_cannot_be_read_is_rescanned_until_it_settles_45s_later`, `::test_a_zombie_leader_that_settles_as_exited_lets_the_reset_run`, `::test_a_zombie_that_never_settles_hits_the_bound_refuses_and_is_waited_for_once`, `::test_the_wait_is_once_per_pid_and_start_time_not_per_pid`, `::test_a_live_pid_gap_gets_no_zombie_wait_and_still_refuses`, `::test_an_unknown_zombie_beside_a_live_gap_gets_no_wait`, `::test_a_live_holder_still_blocks_beside_an_exited_zombie_and_the_job_is_never_signalled`, `::test_an_automatic_resets_zombie_wait_ends_before_the_hold_ladder_mark`, `::test_a_zombie_cleared_on_the_last_poll_has_its_reset_and_full_check_done_before_the_ladder_mark`, `::test_a_hold_begun_in_a_power_cycle_cooldown_ends_the_wait_one_reserve_before_the_ladder_mark`, `::test_the_scan_event_names_the_zombie_its_parent_and_its_threads`, `tests/test_zombie_info_proc.py::test_a_killed_unreaped_child_is_a_zombie_with_no_thread_left_and_no_holder`, `::test_a_zombie_leader_with_a_live_thread_holding_the_node_is_a_holder`, `::test_a_process_that_is_no_zombie_has_no_zombie_info`, `::test_an_automatic_reset_past_an_unseen_holder_is_refused_and_the_fabric_stays_owed`, `::test_a_scan_that_raises_lets_the_reset_proceed`; the operator reset refuses while another is in flight: `tests/test_reset.py::test_an_operator_reset_refuses_while_another_reset_is_in_flight`, `::test_a_streaming_operator_reset_refuses_while_another_reset_is_in_flight`; a refused reset keeps the hold and the I76 bound still runs the ladder at 10 min, then the power cycle: `tests/test_hold_bound.py::test_every_hold_kind_runs_the_ladder_at_the_bound_then_the_host_rung`, `::test_a_reset_the_holder_scan_refuses_keeps_the_hold_and_the_bound_cycles`, `::test_a_tenant_the_holder_scan_finds_bounds_the_hold_and_the_ladder_runs_when_it_leaves` |

| I93 the gap: 200 s after the first reset ended, cut so the wait ends by the 600 s bound; no wait without the first reset or the hold clock; no env/config knob | `tests/test_mesh_reset_repeat.py::test_the_gap_wait_ends_by_the_hold_bound`, `tests/test_mesh_reset_repeat.py::test_the_gap_wait_needs_the_first_reset_and_the_hold_clock`, `tests/test_mesh_reset_repeat.py::test_no_env_or_config_knob_changes_the_gap` |
| I93 a failed first reset: the sweep's mesh reset waits out the gap (the hold stands), its full check runs; a fail asks for the power cycle, a pass lifts the hold; a first reset ending late waits only to the bound | `tests/test_mesh_reset_repeat.py::test_a_failed_first_reset_spaces_the_sweeps_mesh_reset_then_asks_for_the_power_cycle`, `tests/test_mesh_reset_repeat.py::test_a_spaced_sweep_whose_full_check_passes_lifts_the_hold`, `tests/test_mesh_reset_repeat.py::test_a_first_reset_ending_late_waits_only_to_the_bound`, `tests/test_dispatch_failure_reset.py::test_a_failed_post_reset_check_stays_held_and_climbs`, `tests/test_job_kill_reset.py::test_a_failed_post_kill_check_stays_held_and_climbs` |
| I93 no wait on a fabric link-short hold (I83), a chip off the bus, a BMC guard finding, a first reset from another episode or a spent sweep | `tests/test_mesh_reset_repeat.py::test_the_sweep_waits_only_on_a_present_mesh_after_this_episodes_reset`, `tests/test_fabric_links_missing.py::test_a_lost_link_that_a_reset_does_not_heal_stays_held`, `tests/test_fabric_links_missing.py::test_a_latched_hold_resets_and_a_post_reset_mismatch_fails_and_climbs` |
| I93 at the 600 s bound the ladder runs at once, never waits for the gap, and a failing check asks for the power cycle | `tests/test_mesh_reset_repeat.py::test_the_bound_runs_the_ladder_and_asks_for_the_power_cycle_at_600s` |
| I94 a chip the bridge rung re-creates with no live ARC heartbeat (missing, ENODATA, all-ones) stays isolated, its rung counts failed; a live heartbeat within the bounded wait recovers it; the hold reaches the off-bus one-shot in one sampler tick and lifts only through the full post-reset check, fabric included | `tests/test_bridge_reset_arc_dead.py::test_a_chip_back_on_the_bus_with_a_dead_arc_stays_isolated`, `tests/test_bridge_reset_arc_dead.py::test_an_all_ones_heartbeat_is_not_a_live_arc`, `tests/test_bridge_reset_arc_dead.py::test_a_chip_back_with_a_live_heartbeat_is_recovered`, `tests/test_bridge_reset_arc_dead.py::test_an_arc_that_comes_up_within_the_wait_is_recovered`, `tests/test_bridge_reset_arc_dead.py::test_an_arc_dead_reenumeration_reaches_the_one_shot_on_its_short_clock`, `tests/test_bridge_reset_arc_dead.py::test_the_arc_wait_is_bounded`, `tests/test_bridge_reset_arc_dead.py::test_a_pci_wedged_pass_reads_no_heartbeat` |
| I95 no source string names the global rescan file; the scoped rescan writes only the Tenstorrent root port's bus, also for an off-bus chip's remembered port and at startup with the tray already off | `tests/test_scoped_pci_rescan.py::test_no_source_string_names_the_global_rescan_file`, `::test_the_scoped_rescan_writes_only_the_tenstorrent_root_ports_bus`, `::test_an_off_bus_chips_remembered_root_port_is_still_rescanned`, `::test_at_startup_with_the_tray_already_off_the_bus_the_recorded_port_is_rescanned` |
| I95 every rescan rung (reset path, bridge batch, bare rescan of dead chips, tray-down and below-floor), the bridge reset and the health probe write only the Tenstorrent root port; with none, each fails, writes nothing and does not latch | `tests/test_scoped_pci_rescan.py::test_every_rescan_rung_reaches_only_the_tenstorrent_root_port`, `::test_with_no_tenstorrent_root_port_every_rescan_rung_fails_without_a_global_write`, `::test_the_bridge_reset_rescans_only_the_tenstorrent_root_port`, `::test_a_bridge_reset_with_no_tenstorrent_root_port_fails_without_a_global_write`, `::test_the_health_probe_rescans_only_the_tenstorrent_root_port`, `::test_the_health_probe_with_no_tenstorrent_root_port_writes_nothing` |
| I95 a remembered port is trusted only while no other device sits below it: one whose slot now holds a non-Tenstorrent function is skipped and journaled, alone or beside a Tenstorrent port; one with an empty subtree or only a bridge below is still rescanned | `tests/test_scoped_pci_rescan.py::test_a_remembered_port_whose_slot_now_holds_another_device_is_not_rescanned`, `::test_a_stale_remembered_port_is_skipped_while_the_tenstorrent_port_is_rescanned`, `::test_a_remembered_port_with_no_other_device_below_it_is_still_rescanned` |
| I95 the 10-min ladder with the real rescan: scoped, and a failed rescan still climbs to the mesh reset, the full check and the power cycle without lifting the hold | `tests/test_scoped_pci_rescan.py::test_the_10_min_ladders_rescan_is_scoped_and_a_failed_one_still_reaches_the_power_cycle` |
| I96 a below-floor hold on an enumerated chip with an unreadable heartbeat and nothing isolated runs the off-bus one-shot on the next sampler tick, also for a hold carried across a broker restart; the chip is never isolated or SBR'd, and the hold lifts only through the full post-reset check, fabric included | `tests/test_offbus_one_shot_unisolated.py::test_an_enumerated_dead_arc_chip_arms_the_one_shot_on_its_short_clock`, `::test_a_hold_carried_across_a_restart_arms_the_one_shot_for_its_dead_arc_chip` |
| I96 a gone chip with the SBR opt-in unset arms the one-shot and gets no SBR; its reset lifts only on a full check that passes on every cabled link (a 77, a missing verdict or a link-short fabric keeps the hold); a whole tray gone still goes to the power cycle with no reset | `tests/test_offbus_one_shot_gone_chip.py::test_a_gone_chip_arms_the_one_shot_without_an_sbr`, `::test_a_whole_gone_tray_goes_to_the_power_cycle_with_no_reset` |
| I96 a chip back on the bus no longer arms the one-shot (the gate reads after its rescan; the forced run and the idle DOWN re-check prune it); no reset runs, the hold stays, and the 10-min bound's ladder still runs with its full check, fabric included | `tests/test_offbus_one_shot_returned.py::test_a_dead_arc_chip_back_on_the_rescan_does_not_arm_the_one_shot`, `::test_a_chip_back_while_a_tenant_waited_drops_the_one_shot`, `::test_the_down_offbus_recheck_drops_a_returned_chip_and_keeps_the_hold` |
| I97 the SBR before the power cycle has every chip back and the full check fails on ARC-not-ready chips: one mesh reset of the whole mesh, then the full check with fabric; only its pass lifts, and a failure requests the power cycle in the same pass, its reason read from the bus as it is now | `tests/test_below_floor_offbus_ladder.py::test_an_sbr_that_has_every_chip_back_on_arc_not_ready_chips_gets_one_mesh_reset_first` |
| I97 a refused mesh reset lifts nothing and requests the power cycle in the same pass (the sweep's repeat, refused, holds instead: I100) | `tests/test_below_floor_offbus_ladder.py::test_the_i97_reset_keeps_its_deadline_and_a_refusal_power_cycles_but_the_repeat_holds` |
| I97 the reset starts only when a give-up (300 s), or the reset, settle and the check's 180 s allowance, still ends before the 585 s hold-age deadline; an older hold gets the power cycle at once | `tests/test_below_floor_offbus_ladder.py::test_the_i97_reset_keeps_its_deadline_and_a_refusal_power_cycles_but_the_repeat_holds`, `::test_the_repeated_mesh_reset_runs_at_once_under_the_deadline_or_one_from_its_start` |
| I97 started at the latest age the deadline allows, the power cycle is requested by 600 s and the hold stays: after a failed check, and with no check after a reset that runs long, runs past the deadline or gives up at its driver wait again | `tests/test_below_floor_offbus_ladder.py::test_the_power_cycle_after_the_mesh_reset_before_the_host_rung_is_requested_by_the_bound` |
| I97 a full check still running at the deadline is failed: its reader gets SIGTERM, then SIGKILL after the grace, nothing lifts on its late pass, and the power cycle is requested by deadline + grace + reap | `tests/test_below_floor_offbus_ladder.py::test_a_full_check_still_running_at_the_deadline_fails_and_power_cycles` |
| I97 a fabric pass after the mesh reset short of the cabled links fails the check: the hold stays and the power cycle follows | `tests/test_below_floor_offbus_ladder.py::test_a_fabric_check_short_of_the_cabled_links_after_the_mesh_reset_before_the_host_rung_power_cycles` |
| I97 a passed check whose lift the funnel refuses keeps the hold with no power cycle; the next pass, or the bound's own, runs no second I97 reset and, on a failed check, requests the power cycle by the bound | `tests/test_below_floor_offbus_ladder.py::test_a_refused_lift_after_the_mesh_reset_before_the_host_rung_still_power_cycles_by_the_bound` |
| I97 once per hold episode; a new episode gets its own | `tests/test_below_floor_offbus_ladder.py::test_the_mesh_reset_before_the_host_rung_runs_once_per_hold_episode` |
| I97 a whole tray off still goes straight to the power cycle with no reset, also with chips ARC-not-ready on the other trays | `tests/test_below_floor_offbus_ladder.py::test_a_whole_tray_off_with_arc_not_ready_chips_still_power_cycles_with_no_reset` |
| I97 the power-cycle reason counts the chips off the bus and ARC-not-ready from a read taken now; an unreadable bus keeps the previous text | `tests/test_below_floor_offbus_ladder.py::test_the_power_cycle_reason_reads_the_bus_as_it_is_now` |
| I97 one SBR in tt-smi's driver wait after four rescans that left a chip out: a healed chip is back by the next rescan, one still out keeps the rescans to their tries; none without the SBR capability | `tests/test_mesh_reset_readd.py::test_an_sbr_follows_the_rescans_that_left_a_chip_out_in_the_driver_wait`, `::test_no_sbr_in_the_driver_wait_without_the_capability` |
| I97 a reset that ends while the driver-wait SBR's write is in flight lets it finish and rescans no more in the wait | `tests/test_mesh_reset_readd.py::test_a_reset_that_ends_during_the_driver_wait_sbr_takes_no_more_rescans_in_it` |
| I97 a tt-smi that gave up at its driver wait is logged and journaled as a FAILED mesh reset, never as "no POST_RESET failure" | `tests/test_mesh_reset_readd.py::test_a_reset_whose_tt_smi_gave_up_at_its_driver_wait_is_logged_as_failed` |
| I98 a chip the gate SBR'd that is still off the bus gets no second round from the off-bus escalation or the last-chance sweep; the sweep reads the gate's round, the hold climbs to the power cycle and stays held | `tests/test_sbr_record_per_hold.py::test_a_chip_the_gate_reset_is_not_reset_again_by_the_escalation_or_the_sweep` |
| I98 the record is keyed by the hold: a new hold gets exactly one round of its own | `tests/test_sbr_record_per_hold.py::test_the_record_starts_empty_for_each_new_hold` |
| I98 a skipped round is not a recovery: only a just-passed full check with fabric lifts, a failed one keeps the hold | `tests/test_sbr_record_per_hold.py::test_after_a_skipped_round_only_the_full_check_with_fabric_lifts` |
| I98 an SBR found inapplicable (no bridge) counts as the hold's round: the sweep tries none again before the power cycle | `tests/test_b1_regressed_no_bridge.py::test_no_bridge_regressed_hold_power_cycles_on_the_first_pass_after_the_kept_rungs` |
| I98 per chip: a partial call writes no bridge reset to the spent chip, the new chip gets its round, the rung reports not recovered while the spent chip is off the bus | `tests/test_sbr_record_per_hold.py::test_a_partial_round_writes_no_bridge_reset_to_the_spent_chip` |
| I98 with I69: in a partial call the spent removed chip gets no bridge write and does not withhold the rescan that brings the new chip back | `tests/test_removed_chip_rescan.py::test_a_partial_round_resets_only_the_new_chip_and_the_spent_one_does_not_withhold_the_rescan` |
| I98 with I69: a removed chip spent with no bridge write (no_bridge) withholds every rescan of a later round in the hold and gets no second try; both chips stay isolated, the rung is not recovered | `tests/test_sbr_no_bridge_rescan.py::test_probe_no_bridge_spent_chip_must_still_withhold_rescan`, `tests/test_sbr_no_bridge_rescan.py::test_a_no_bridge_spent_chip_withholds_the_rescan_and_gets_no_second_round` |
| I98 the record keeps the no-bridge chips apart until a bridge write reaches one | `tests/test_sbr_no_bridge_rescan.py::test_a_chip_a_bridge_write_reaches_leaves_the_not_reset_set` |
| I98 a removed chip back on the bus by an outside path leaves the record: a later drop gets a round | `tests/test_sbr_no_bridge_rescan.py::test_a_chip_back_by_an_outside_path_leaves_the_record` |
| I98 a chip the driver-wait SBR's rescans brought back leaves the record: when it drops again it gets a round | `tests/test_sbr_record_per_hold.py::test_a_chip_the_driver_wait_sbr_healed_gets_a_round_when_it_drops_again` |
| I98 a chip the mesh reset brought back leaves the record: when it drops again it gets a round | `tests/test_sbr_record_per_hold.py::test_a_chip_the_mesh_reset_healed_gets_a_round_when_it_drops_again` |
| I98 a lift on a just-passed full check clears the record: a hold that carries the episode (I82) starts empty | `tests/test_sbr_record_per_hold.py::test_a_hold_carried_after_a_verified_lift_starts_with_an_empty_record` |
| I99 the bound is clean shutdown + the host's unit timeout + margin, capped; the setting only raises it; never off | `tests/test_power_cycle_os_first.py::test_the_shutdown_bound_clears_a_unit_timeout_and_stays_finite`, `::test_the_fallback_bound_is_the_hosts_and_the_request_is_kept_for_the_next_boot` |
| I99 the unit timeout is the longer of systemd's start and stop defaults; an unreadable or unparsable answer reads as 90 s | `tests/test_power_cycle_os_first.py::test_the_host_unit_timeout_is_the_longer_of_start_and_stop`, `::test_an_unreadable_systemd_reads_as_its_default_timeout`, `::test_systemd_timespans_parse` |
| I99 a cycle that cannot start queries nothing and keeps no request marker | `tests/test_power_cycle_os_first.py::test_no_hook_installed_raises_not_launched_and_runs_no_step`, `::test_a_request_that_never_reached_reboot_keeps_no_marker` |
| I99 the next boot records the shutdown time from the request to the SEL power-off, against the bound, and warns near it | `tests/test_power_cycle_os_first.py::test_the_shutdown_time_is_the_sel_power_off_after_the_reboot_request`, `::test_a_shutdown_past_its_bound_is_recorded_as_such`, `::test_a_cause_confirmed_cycle_reads_the_sel_only_for_the_shutdown_time` |
| I99 a refuted cycle has no shutdown time; a marker outside the row's window adds nothing and is removed | `tests/test_power_cycle_os_first.py::test_a_refuted_cycle_has_no_shutdown_time`, `::test_a_request_from_another_window_adds_nothing_and_is_dropped` |
| I100 at the 10-min bound, a chip whose SBR this hold already spent and a mesh reset that already failed on it: the sweep stops the readers, isolates the chip, writes its SBR again, repeats the mesh reset and runs the full check with fabric; only its failure requests the power cycle and the hold stays | `tests/test_below_floor_offbus_ladder.py::test_at_the_bound_the_sweep_reruns_a_spent_sbr_and_the_failed_mesh_reset_before_the_power_cycle` |
| I100 below the bound, the sweep SBRs a chip whose round this hold already spent and repeats the failed mesh reset before the power cycle | `tests/test_below_floor_offbus_ladder.py::test_the_sweep_sbrs_a_chip_whose_round_this_hold_already_spent` |
| I100 a forced run below the bound no longer skips a mesh reset that failed on the same chips: the sweep repeats it with its full check | `tests/test_below_floor_offbus_ladder.py::test_a_forced_run_below_the_bound_repeats_a_failed_mesh_reset_before_the_power_cycle`, `tests/test_below_floor_offbus_ladder.py::test_below_the_bound_a_mesh_reset_that_failed_on_the_same_chips_climbs_at_once` |
| I100 the bound runs every kept rung, then the sweep repeats the SBR and the failed mesh reset and its full check before the power cycle | `tests/test_below_floor_offbus_ladder.py::test_the_bound_runs_every_kept_rung_in_order_then_the_power_cycle`, `tests/test_below_floor_offbus_ladder.py::test_the_bound_rescans_and_sbrs_before_the_cycle_after_a_failed_mesh_reset`, `tests/test_scoped_pci_rescan.py::test_the_10_min_ladders_rescan_is_scoped_and_a_failed_one_still_reaches_the_power_cycle` |
| I100 the sweep's repeat of the failed mesh reset runs at once, under the I97 deadline or, when that is too close, one from its own start; it never waits for the bound | `tests/test_below_floor_offbus_ladder.py::test_the_repeated_mesh_reset_runs_at_once_under_the_deadline_or_one_from_its_start` |
| I100 a hold that crosses the I97 deadline during the sweep's SBR still gets the repeated mesh reset and its full check; the power cycle is requested within 315 s of the repeat's start and nothing lifts on a failed check | `tests/test_below_floor_offbus_ladder.py::test_a_deadline_crossed_during_the_sweep_sbr_still_runs_the_repeated_mesh_reset` |
| I100 a repeat that did not run (a tenant before it, a refused reset) holds: no power cycle, no lift, the sweep not spent | `tests/test_below_floor_offbus_ladder.py::test_a_tenant_that_arrives_before_the_repeated_mesh_reset_holds_with_no_power_cycle`, `::test_a_refused_repeated_mesh_reset_holds_with_no_power_cycle` |
| I100 the I97 reset having run this hold does not skip the sweep's repeat; only its full check passing lifts, once | `tests/test_below_floor_offbus_ladder.py::test_the_repeated_mesh_reset_runs_after_this_holds_i97_reset_already_ran` |
| I100 an outage hold refuses the whole sweep: nothing lifts and no power cycle is requested; the device stays held for the operator | `tests/test_below_floor_offbus_ladder.py::test_an_outage_hold_refuses_the_sweep_and_holds_for_the_operator` |
| I100 the sweep never skips a chip's SBR as spent and repeats the failed mesh reset | `tests/test_sbr_record_per_hold.py::test_a_chip_the_gate_reset_is_not_reset_again_by_the_escalation_or_the_sweep` |
