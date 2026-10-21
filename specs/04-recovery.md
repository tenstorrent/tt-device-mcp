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
  cycling (that reset may yet succeed; the next caller adopts it). The cooldown never parks a hold:
  a measured fault the reset did not heal (e.g. class D, lost fabric links or a latched topology
  mismatch, spec 03 I42/I71) climbs to the host rung at the 600 s stuck-hold ceiling, so its time to
  recover is the failed pass, the reset (~4 min) and the power cycle within the 15 min class budget.
  The one stated exception is spec 03 I42's watchdog path: with no idle relift reset first, the reset
  starts at 600 s and the host rung follows about 4-6 min later.
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
  paths an incomplete scan counts as a tenant. A rung the forced pass calls (the per-tray walk
  included) reads the same forced guard as the pass: an incomplete scan does not stop it, a
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
- **I9 — The durable auto-recovery ledger.** Every host-level escalation (reboot, power cycle) is
  appended to `auto_recovery.jsonl` and fsync'd BEFORE the action fires; if the record cannot reach
  disk the action is aborted — an unrecorded reboot is one the next boot repeats. The ledger is the
  rate limiter and survives the reboot it records: a wall-clock min interval
  (`AUTO_RECOVERY_MIN_INTERVAL_SEC`, 3600 s, spanning the reboot itself) plus a per-boot cap
  (`AUTO_RECOVERY_MAX_PER_BOOT`, 1), counted over this boot's records within the action's interval,
  so it never holds past a stated time (I65). The power cycle is spaced by its cooldown (I65) instead
  of the min interval. An unreadable ledger denies (fail closed); a fire that raised
  without launching retracts its entry so the retry is not spent on a recovery that never happened.
  A power cycle whose following boot reached a verified-healthy release — a gate pass whose fabric
  check ran and passed (the startup gate's full check included), or a hold lifted on such a pass —
  healed the box, so it no longer anchors the power cycle's cooldown (I65), and neither does the
  host boot it brought up once that boot is verified; it no longer counts toward the reboot's min
  interval or per-boot cap either: a new whole-tray or all-off drop after it power-cycles at once,
  and a partial drop or present-mesh wedge after it takes the reboot the chooser picks at once (I10).
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
- **I10 — Per-severity rate limiting.** The min interval gates per rank (`reboot` < `power-cycle`):
  no rung re-fires itself inside it, and a stronger record blocks de-escalating to a weaker rung. An
  unclassifiable record counts as strongest. A power cycle after a failed reboot waits only for its
  own cooldown (I65), which counts from that reboot. A power cycle the cooldown, the per-boot cap or
  the I9 backstop refuses is asked again the moment that wait ends: the hold-deadline watchdog forces
  one ladder pass for the hold episode it was refused in (kind `power_cycle_cooldown`), instead of
  waiting for its next ceiling window. The governor still decides that pass, and it releases only
  through I32. A power cycle a later boot verified healthy (I9) blocks nothing: the fault after it is
  a new episode, not a boot loop, so the reboot is not held for the rest of the interval. That
  exemption ends at the power-cycle backstop (I9): there every cycle in the window blocks the reboot
  again, and the reboot's own interval and per-boot cap always apply, so a reboot fires at most once
  per `AUTO_RECOVERY_MIN_INTERVAL_SEC`. With both host rungs opted in, the chooser offers the reboot
  first and the power cycle once a reboot was tried in this episode (`reboot_tried_this_episode`): a
  broker reboot recorded on an earlier boot and stamped after the last verified-healthy release (I9).
  It counts until the next verified release, not for a fixed time: the power cycle's cooldown (I65)
  counts from the reboot's own boot and reaches 3600 s or more after two unhealed cycles, so a
  reboot that lapsed after `AUTO_RECOVERY_MIN_INTERVAL_SEC` was chosen again at the cooldown's end
  and a wedge no reboot heals looped on reboots while the cycle never fired. With no usable verified
  release on record (none, unreadable, or stamped later than now) a reboot counts for
  `REBOOT_EPISODE_UNVERIFIED_MAX_SEC` (the reboot interval plus the 4 h backoff cap: past the reboot,
  its boot and the longest cooldown). A release verified on this boot (with a stamp no later than
  now) ends the episode for every earlier-boot reboot, whatever the stamps say (a clock that stepped
  back across the boot). A reboot recorded on this boot, or with no readable or a future stamp, does
  not count. A power cycle that cannot launch on this boot (its command is missing or exits non-zero,
  so its ledger row is taken back; a timed-out launch may still act and does not count) lets the
  reboot back in once per episode: with exactly one reboot in the episode the chooser offers the
  reboot again, under its own interval and per-boot cap, so the hold keeps a host rung that can run;
  a second reboot in the episode spends it and the cycle is offered again. **Time to recover** (a present-mesh wedge a reset and a reboot do not heal):
  the ladder, one reboot and its boot, then the power cycle at the I65 cooldown's end (at most the
  current interval after that boot, 30 min to 4 h), then the startup gate's full check, fabric
  included; before, a reboot again at each cooldown end and no cycle (unbounded). Faults that skip the
  reboot (a whole-bus drop, a reboot proven futile) and episodes with no reboot tried are unchanged.
  Power cycle opted in but cannot launch: one fallback reboot at most `AUTO_RECOVERY_MIN_INTERVAL_SEC`
  after the first (then its boot and the full check, fabric included), then the cycle again on each
  pass, failing loud until it can launch; before, a reboot every `AUTO_RECOVERY_MIN_INTERVAL_SEC`
  (a reboot loop). Hosts that never stamp a verified release (no fabric check, or a single chip with
  both host rungs on): a reboot counts for `REBOOT_EPISODE_UNVERIFIED_MAX_SEC` instead of
  `AUTO_RECOVERY_MIN_INTERVAL_SEC`, so a new wedge 1 h to 5 h after a reboot that healed gets the
  power cycle (at its I65 cooldown's end) instead of a second reboot.
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
  expires after `HOLD_ESCALATION_REARM_SEC` so a hold never becomes permanent); the tray walk runs
  at most once per episode. Host rungs are reachable only after a `tt-smi` reset has already run
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
- **I14 — Host rungs are armed by default, opt-out, and fireable-or-off.** `_auto_reboot_enabled`
  and `_auto_power_cycle_enabled` default ON (`TT_DEVICE_MCP_AUTO_REBOOT=0` /
  `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` opt out); each additionally reads as OFF where the process
  could not run it (I17) — armed means armed AND fireable. A warm reboot is never fired where it
  provably cannot help: a whole-bus drop, a reset that regressed the mesh off the bus, or one that
  hard-exited leaving chips off it routes to the cold rung or holds loudly (`reboot_blocked`),
  never to a reboot that lands back in the dead state. On a multi-chip mesh with a chip off the bus
  and the power cycle armed, the cycle goes first and the reboot is only its fallback (I71).
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
  the snapshot is chip N": both transpose trays 3 and 4 and build a bitmap that re-powers a tray
  the drop never touched (issue #27). The tray map is keyed by chip index and built from the
  kernel's own `{chip index: PCI address}`, read from sysfs (`tenstorrent!N/device`); a positional
  list is refused. The bitmap bit is `tray - 1`; the tables are 1-based and the BMC's bits are
  not. Every log line that names a tray gives its BMC mask and its chips as well
  (`tray 4 (BMC mask 0x08, chips 24-31)`), so "tray N" cannot be read two ways.

  **The map is cached, and no map means no fire.** A chip that has left the bus is absent from the
  tt-smi snapshot and has no sysfs node, so its bus cannot be read at the moment the rung wants
  it — which is every moment this rung fires. The chip→bus map is therefore banked from a trusted
  full read and held until the next one: a full-count snapshot (every chip present, every `bus_id`
  non-empty) and, in the same pass, a sysfs map that names the same set of buses. When the two
  disagree (one read raced the bus) nothing is banked, `chip_bus_map_mismatch` journals both once,
  and a later full pass that agrees banks the map. A broker that restarted after the drop and
  never saw a full-mesh snapshot has no map, and then the tray rung DECLINES rather than fall back
  to arithmetic: the same rule as I15's mesh reset, which never fires on a guessed mode. A decline
  here is `not_applicable`, not `blocked` — it is a missing fact, not an operator's kill switch.

  **A trusted full read re-banks the map; no other read touches it.** A chip's bus does not move,
  but the index the kernel gives it can: tt-kmd derives a Galaxy chip's index from its PCI address
  but takes a free one when that index is still in use, and older drivers always take a free one,
  so a chip can come back from a drop, a rescan or a tray reset under another index — the cycles
  this rung exists for. A map that names the old index re-powers the wrong tray. So every trusted
  full read is compared with the banked map, and one that differs replaces it in the gate's own
  pass, before any rung computes a BMC mask from it, and journals `chip_bus_map_drift` with the
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
  in the kernel is bounded by I70.

- **I22 — No host rung without the full reset ladder.** A reboot or power cycle fires only after
  the last-chance sweep (SBR, per-tray re-power, mesh reset) ran COMPLETE and the one verify after
  it failed, or once this episode's ladder already ran its mesh reset and it failed on an unchanged
  off-bus set (I38: the sweep would repeat that reset, which I36 forbids). A sweep that was skipped (a reset already cycling in its own scope, a tenant) or
  incomplete (the I24 gate refused it, or the I23 envelope refused the tray fire) holds the host
  rung for that pass: `_settle_and_verify_before_host_rung` returns None and every caller holds.
  I70 is the other exception: under an unkillable leftover every reset is fenced (I21), so its host
  rung runs with no ladder and no sweep.

- **I23 — A per-tray re-power runs inside a host-safety envelope** (`pcie_guard.safe_tray_repower`).
  In order: the tray map is cross-checked against sysfs (each chip's bus against tt-smi's tray
  table) and a disagreement refuses the fire, so a mis-mapped tray is never cut (issue #27); it
  waits up to `TT_DEVICE_MCP_TRAY_REPOWER_HOLDER_WAIT_SEC` for every process to let go of EVERY
  chip on the box (`TrayPlan.box_chips`: each chip sysfs or tt-kmd's `/proc` list names, on the bus
  or not), not only the tray's, else refuses: one tray's pulse has dropped every other tray of a
  Galaxy within seconds, so the box must be as idle as for a whole-box cycle (the broker's own pid
  never counts); the device pollers are stopped as for a mesh reset; AER and DPC are
  masked on EVERY Tenstorrent root port, the sibling trays' included (a mask that cannot be applied
  is put back and refuses the fire, before any chip is touched); the chips are quiesced
  (USER_RESET); the tray's PCI functions are removed; the BMC pulse fires; then, even if the
  quiesce or the pulse failed, only the re-powered tray's root ports are rescanned (each port's
  `pci_bus/<bus>/rescan`; the global `/sys/bus/pci/rescan` only when no port of the tray is known),
  the chips re-inited (POST_RESET), and every chip on another tray that read a live heartbeat
  before the pulse and all-ones after it gets one `remove` and a rescan of its own root port
  (`tray_repower_sibling_offbus`; one still all-ones is left to the walk's regression check), then
  each port's error status is cleared and watched for `TT_DEVICE_MCP_AER_QUIET_CHECK_SEC`. On a
  Galaxy every per-tray re-power with a global rescan left the slot-6 chip of each other tray
  reading all-ones. "Every root
  port" includes the ports of chips already off the bus: the broker remembers each root port and
  chip arch it has seen (`tt_pci_topology.json` in the health dir) and masks a remembered port
  that still exists in sysfs. It looks at broker start and in every between-jobs check (sysfs
  reads; the file is rewritten, through a temp file and a rename, only when a look adds a port or
  an arch), so one healthy start covers the first tray drop after it and every later restart.
  Known limit: a root port never seen with its chip present on this host (the broker's first start
  here already found that chip off the bus) is not known and is not masked; every other port is.
  With every chip off the bus there is no healthy tray to cut and nothing to cross-check, so the
  fire goes ahead without a tray table. Sysfs that cannot be read (the PCI device list, the
  tenstorrent class list, or the vendor or address of a Tenstorrent function or of a port above
  one) is no answer, never "every chip off the bus": the plan refuses the fire and names what it
  could not read, and a look at start or between jobs keeps the recorded topology and leaves a
  `pci_topology_unreadable` event; the I24 gate counts every chip off the bus then, so `hold`
  holds, and a `guard` mesh-reset mask logs `aer_mask_failed` and masks the recorded ports. Any
  other entry it cannot read is skipped. PCI domains of 5+ hex digits (Intel VMD) are addresses,
  and a chip's root port is the first port above it in its own domain. A quiet port gets its
  exact saved settings back; one that errors again stays masked, starts the AER flood window and stops the tray
  walk. A refused fire re-powers nothing. `TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN=1` logs the planned
  sequence and refuses; `python -m tt_device_mcp.health.recovery.pcie_guard <bitmap> [chip ...]`
  prints the plan from sysfs and touches nothing.

- **I24 — A per-host gate on automatic host-affecting resets** (`TT_DEVICE_MCP_HOST_RESET_GATE`).
  `off` (default) keeps the behaviour above. `guard` masks AER on every Tenstorrent root port around
  each automatic mesh reset and refuses an automatic mesh reset, tray walk or last-chance sweep
  while the root ports' AER counters rise at `TT_DEVICE_MCP_AER_FLOOD_THRESHOLD` or more errors per
  `TT_DEVICE_MCP_AER_FLOOD_PERIOD_SEC` (a rate, so a trickle between looks hours apart is not a
  flood), or within `TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC` of one (the first look after a recent boot
  counts from zero at boot). `hold` also refuses while any chip has no PCI function, or keeps its
  function but its ARC heartbeat reads all-ones (its BAR reads return nothing: it has left the bus
  too), so a reset that dropped a chip is not re-issued over it. A chip whose heartbeat read fails
  with ENODATA while its KMD node is there and its PCI endpoint is bound to the driver is on the bus
  with its ARC not ready (mid-boot after a reset or re-power), not off it: the heartbeat verdict
  reads it UNHEALTHY and names it `arc_not_ready`, but no off-bus count includes it (this gate, the
  reset and reboot floors, the tray walk plan, the bridge and gone-chip rungs, the hold-deadline
  climbs, `/health` `off_bus`). An unbound endpoint, a missing node or any other read error still
  counts the chip off the bus. Under every mode, `off` included, an automatic reset is refused while
  the flood window an I23 port that stayed masked opened is still open: with the gate `off` that
  holds every automatic mesh reset, tray walk and last-chance sweep (and so the host rung above
  them, I22) for `TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC`. The one exception is inside a last-chance
  sweep that already started: its mesh reset still follows its tray fire, even one that left a port
  masked, because no host rung fires without the full ladder (I22); the erroring port stays masked
  across that reset. The gate is checked before
  the reset is recorded (`reset_begin`, the cooldown clock): a refused reset did not happen. A
  refusal is handed to an operator, never a climb (I22), bounded by I27 and, with no power cycle
  configured, by the I33 operator hand-off. Operator and tool resets are not gated.
  The per-chip bridge SBR is gated the same way, counting its target chips as off the bus: under
  `hold` (or the I25 latch) it does not fire, and the PCI rescan still runs.

- **I25 — Re-power only affected trays; latch hold after an off-bus reset hung the host.** The
  automatic tray walk and the last-chance sweep re-power only trays that hold an off-bus chip (read
  from the heartbeats when the caller names none), each through the I23 envelope and its sysfs
  cross-check. The sweep's `last_chance_reset_sweep` event and line name each off-bus chip it
  planned around, with its banked PCI address and its tray, so the trays it re-powers can be checked
  against the chips that drove them. A tray whose chips are all on the bus is never re-powered automatically; if chips are
  still off after their trays, the next rung runs as the I24 gate allows (no rung is skipped before
  a power cycle, I22). Re-powering tray X alone recovers chips off on tray X, as the old walk did.
  The walk never re-powers a single tray for a below-floor drop that sits on that one tray: a
  one-tray BMC re-power has taken the other trays off the bus with it (1 chip off -> 24 off), so it
  emits `ubb_reset_declined_single_tray`, spends no walk latch, holds for self-heal, and the stuck-hold
  ladder's next rung owns it. Nor does it re-power a tray whose chips are all off the bus in a
  below-floor drop, in the post-job gate or the idle escalation: a whole tray off the bus is a power
  problem a re-power has made worse (one tray re-power took 20 more chips off, three times on one
  Galaxy host in an hour), so it emits `ubb_reset_declined_whole_tray` and hands the hold straight to
  the I27 power cycle with no grace (`offbus_gated_hold` phase `whole_tray_down`). No walk ran, so
  the idle escalation journals it as `galaxy_reset_skipped_whole_tray` (`fault_class` `whole_tray`),
  never as a failed walk, and starts no walk-failed clock. A partial drop (no tray wholly off) keeps
  the walk. `TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK=1` turns the single-tray
  and whole-tray walks back on; the tray-down-no-window sweep is not affected.
  Before an automatic mesh reset, tray re-power or sweep with a chip off the bus, the broker writes
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
  when: the full ladder below the power cycle still runs first (I11), and the warm reboot does not
  run it. Unset, nothing runs and the timeline is unchanged.

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
  power-cycle command `TT_DEVICE_MCP_POWER_CYCLE_CMD` (argv split like shell words, no shell, the
  reason in `TT_DEVICE_MCP_POWER_CYCLE_REASON`, bounded by
  `TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC`, default 120 s) through `_fire_recovery_escalation`
  (ledger record, jobs-list row, the I26 hook). It holds instead while a tenant holds the device or
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
  `partial_offbus` for 1-3), `handoff` (`whole_tray`, `ipmi_unavailable`, `tray_walk_failed`,
  `tray_rung_regressed`, `gate_refused`) and `hold_age_sec`, the hold's time to this cycle.
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
  cycle fires at the grace and not at the next ceiling window. **A failed BMC IPMI is bounded the
  same way.** When the gate allows resets but the BMC's IPMI failed a tray re-power this episode
  (`BmcIpmiError`: `ipmitool` exits non-zero, cannot start or times out; a refused or mis-mapped
  re-power does not count), no IPMI rung can end the hold, so the first such failure starts a clock
  and, once the ladder ran once this episode (whether the failure came from its own tray rung or
  not), the same power cycle runs `TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC` (default 300 s) after
  it, with every check above; the watchdog forces one pass at that bound. The idle relift, the
  watchdog and the between-jobs gate's tray rung (for a whole tray off the bus) reach it; the device
  is already held there and no reset runs, so it adds no time to a job that could have run. **A failed tray
  walk is bounded the same way, and no below-floor mesh reset follows it.** When the I25 walk fired
  this episode and did not recover the mesh, or took chips on other trays off the bus with it, the
  idle off-bus escalation does not try the below-floor mesh reset (with a tray still off the bus it
  cannot re-enumerate it, and with 8 chips off it overran its 180 s and failed). It holds and starts
  a clock instead: the same power cycle runs `TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC` (default
  300 s) after the walk failed, with every check above, and the watchdog forces one pass at that
  bound. When the power cycle cannot fire (no command, or it is not guarded), the hold climbs to the
  host rung without the last-chance sweep, whose resets include the same mesh reset. The device
  stays held throughout: it is released only by the full health check, fabric included.
  **A regressed tray rung is bounded the same way.** When the gate allows resets but a tray
  re-power on this host (within the latch window below) took chips on other trays off the bus (the per-tray rung is declined, I25
  and the tray rung's walk below), no IPMI rung will run, so the off-bus hold takes the same grace
  and then the same power cycle, with every check above.
  Unset, the behaviour is I24's plus the I33 hand-off. `deploy/tt-manual-recovery-check.sh` is a template for the
  manual-recovery command: exit 0 when the sudo journal or auth.log shows a sudo-run reboot,
  poweroff, shutdown, halt, kexec or ipmitool power/reset in the last 30 min, 1 when not, 2 when it
  cannot read the journal.
- **I28 — An operator's outage hold holds every automatic mesh reset.** With
  `TT_DEVICE_MCP_AUTO_MESH_RESET=0`, or with it unset and `TT_DEVICE_MCP_AUTO_REBOOT`,
  `TT_DEVICE_MCP_AUTO_POWER_CYCLE` and `TT_DEVICE_MCP_AUTO_UBB_RESET` all `0` (an outage hold), no
  automatic mesh reset fires: not the between-jobs gate's, the idle relift's or the hold
  watchdog's, and no last-chance sweep. `TT_DEVICE_MCP_AUTO_MESH_RESET=1` arms it whatever the
  others say. Any one of the three alone is a per-host choice and still climbs through the mesh
  reset (the tray rung off only names its command). A held reset did not happen, as for I24: it is
  checked before `reset_begin` and the cooldown clock, sets no failed-reset flag, and the episode
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
  whose 77 output, detail or reason names the mismatch still climbs) never climbs to a reboot or power cycle: it holds,
  journals and pages `fabric_unverified_no_host_rung` once per episode, and the idle relift keeps
  re-checking on its backoff (spec 03 I48). With no such cause, a reset and a sweep that
  both still read 77 leave the host rung as the only path left (a reboot refills hugepages, reloads
  the driver and retrains the links), so the episode climbs to it once (`fabric_unverified_host_rung`,
  logged only as the rung fires, recorded in the reboot ledger under its own cause, every guard of I22
  and the host rungs kept). The climb after the sweep never waits for the ladder ceiling (I79). A
  climb that only waits (a reset still in its scope) pages no one and leaves no hand-off; the next
  escalation retries it. The boot that rung brought up reads that cause from the ledger: while no
  fabric pass has passed since the broker started, it never fires that rung again; a warm reboot's
  boot with a power cycle opted in climbs once more, to the cold rung (I79). Once the rung is spent, or when it could not
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
  (I83); this invariant still keeps any other forced run of that hold from resetting it.

- **I31 — A galaxy reset that regressed an off-bus mesh is not repeated in that hold episode.**
  When a galaxy reset leaves more chips off the bus than before it (`reset_regressed_offbus`, on the
  gate or on the idle ladder, including before the ceiling), the broker latches that hold episode
  (a regression before any hold opened latches the episode that opens next). While the latch holds
  and chips are still off the bus, neither the gate nor the idle off-bus ladder (forced or not)
  fires the bridge, tray or mesh reset rungs again: each journals `galaxy_reset_skipped_regressed`
  and goes straight to the post-reset verdict, with the drop treated as warm-reboot-futile. With a
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

- **I34 — Once a host reboot or power cycle is requested, the hold watchdog starts no device reset
  until the host acts or the request expires.** A cycle handed to a `TT_DEVICE_MCP_POWER_CYCLE_CMD`
  wrapper can run some time after the request returns; a forced escalation (the off-bus one-shot, a
  ceiling window, any other hold-deadline trigger) that ran a bridge reset, tray re-power or galaxy
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
  as it found it counts: a set that grew is I31, and one that shrank is progress another try may finish.
  While the set off the bus is still that recorded set, the idle off-bus ladder (forced or not) fires
  none of its reset rungs again, and a forced present-mesh window does not repeat the reset (the
  grace-cadence retries before the ceiling stay). It journals `reset_rung_skipped_failed_unchanged` and
  climbs to the host rung at once (I38), rate limits kept (a denied cycle holds, fires no reset and is
  asked again the next window). When no host rung is available
  (power cycle and reboot both off or unfireable) the hold goes to an operator once per episode through
  the I33 hand-off, latched (no deadline, `act_by` null), and no further reset fires on that set. Any
  later reset still owes the I32 fabric pass, so nothing releases the device before the full health
  check, fabric included, passes. The record is keyed by the episode, so a new episode resets again.
  A power cycle the rate limit denies, or one not opted in, never by itself ends resets for the rest of
  the hold: a set that changed or shrank is reset again, and the gate's cooldown paces its retries.
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
  read all-off (one mesh reset, then class C) or partial (the full ladder). With no map the tray
  rung itself runs the same presence read (EIO survivors included) for a below-floor drop: a clean
  whole-tray drop emits `ubb_reset_declined_whole_tray` and goes to the power cycle as in I25,
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
  episode): no mesh reset, no SBR, no tray re-power and no last-chance sweep, and the host rung's
  own sweep skips itself for these classes (`last_chance_sweep_skipped`). The request keeps every
  guard of the host rung: a reset cycling in
  its scope, a tenant or an unreadable holder scan, an operator stand-back marker or check, and the
  power-cycle rate limits (journaled as denied) each hold and fire nothing; the next window asks
  again. The hold-deadline climb (`offbus-forced`) waives an unreadable scan, as the tray rung does,
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
  host pair (the per-chip bridge reset, the tray rung where it applies, the mesh reset) has then had
  its one try, and I36 forbids the mesh reset again on that set, which is the last step of the I22
  sweep. So the climb that follows the failed reset (or any later window's I36 skip) journals
  `host_rung_not_deferred` (reason `mesh_reset_failed_unchanged`) when the hold is younger than the
  ceiling, and `reset_rung_skipped_failed_unchanged` with `rung=last_chance_sweep` in place of the sweep,
  then fires the rung the chooser picks (the power cycle for a below-floor or non-zero-exit drop). The
  rate limits (the I65 cooldown included), the tenant scan and the reset-scope check still apply; a
  denied cycle is asked again the next forced window or at the cooldown's end, never skipped for good. With no host rung the hold goes to an operator
  (I36, I33). The power cycle releases nothing: the startup pass after the host returns runs the full
  health check, fabric included, behind the hugepage gate, and a failure keeps the hold. A set that
  shrank or changed is not covered and keeps the ordinary ladder. Partial off-bus time to power cycle
  falls from the 600 s ceiling window plus a second reset and a sweep (924 s for one chip with a 132 s
  reset; 1186 s for four chips with 263 s resets) to the one-shot plus the one failed reset (about
  150 s and 280 s with the 10-20 s idle one-shot of spec 03 I23; 252 s and 383 s while a tenant holds
  the 120 s wait), when no cycle or boot fell within the last cooldown; within one, at the cooldown's end
  (I65). Bounds the idle 5-6 min one Galaxy host sat between a failed mesh reset and the ceiling
  window; a second mesh reset on the same chips after an exit 1 killed another host.
- **I39 — A failed mesh reset that brought chips back gets one early retry, not the ceiling wait.**
  When the mesh reset fails but the off-bus set after it is a strict subset of the set before it, the
  failure records `mesh_reset_progress_retry_armed` and arms one forced escalation for this hold
  episode, due at once when every chip is back, otherwise after `TT_DEVICE_MCP_PROGRESS_RETRY_SEC`
  (60; never past the hold ceiling). With every chip back the reset's own rescan and settle already
  ran, so a wait buys nothing; a glx_reset started while a chip was out of the kernel fails its
  full-mesh driver-load wait (rc 1) even though the rescan after it brings the chip back.
  The hold watchdog spawns it when due (trigger kind `progress_retry`); it is spent on a real spawn and
  never re-armed in the episode, and a new episode starts with none. The retry climbs the ordinary
  ladder on the new set (I36 allows one more mesh reset on a changed set); an unchanged set after it
  goes to I38. Releases nothing: the full health check, fabric included, still decides. Bounds the
  one-chip Galaxy case whose first reset exited 1 with the chip back on the bus and whose second reset,
  at the 600 s window, healed it: about 580 s from onset instead of about 740 s. The 60 s default is
  kept on measured evidence: in the Galaxy logs every second reset that healed came 99 s or more after
  the failed one, and every second reset 7-11 s after it failed; the retry also never waits past the
  ceiling. The stated bound for B1's first-reset-fails, second-heals pattern is about 10 min from
  onset (one-shot 10-20 s on an idle box, spec 03 I23, 120 s while a tenant holds the device; a failed
  reset of up to 262 s, the 60 s wait, a 62 s healing reset and a 63 s fabric check: about 485 s idle,
  about 585 s with a tenant, against a floor of 378-511 s with no wait at all). When the failed reset
  left every chip back the wait is gone: about 60 s sooner (about 425 s idle, about 525 s with a
  tenant; one Galaxy host's 627 s episode becomes about 567 s). A B1 drop that one reset heals keeps
  the 6 min bound; one with chips still off keeps the 60 s wait.

- **I81 — A mesh reset re-adds the chips the broker isolated at tt-smi's driver wait.**
  `tt-smi -glx_reset` re-powers every tray, counts a 30 s settle, then waits up to 100 s for every
  chip to show up in /dev/tenstorrent, and exits 1 with no re-init when one never does. A chip the
  broker isolated (its PCI endpoint removed) cannot show up on its own: only a PCI rescan adds it
  back, and the rescan `reset_with_quiesce` ends with runs after tt-smi has exited. So before I81
  every mesh reset over an isolated chip failed after about 263 s even when the re-power had healed
  the chip, and the I39 retry's second reset finished the job. Now, when a `-glx_reset` runs with at
  least one isolated chip, `reset_with_quiesce` watches the reset's output and, at the first marker
  of that wait (`Waiting for 30 seconds: 30`, the settle's end, or `Waiting for all N chips to show
  up`, matched across split reads), runs one global PCI rescan, journaled as
  `reset_driver_wait_rescan` with `ok` and the chips. It fires once per reset (I78 repeats it, bounded, while an isolated chip is still missing), never for
  another reset command or with no isolated chip, and a failed write is journaled and the reset runs
  on; the rescan after the reset still runs. A cancelled reset cancels a pending rescan and disarms the
  watcher, so output the backend still reads after the cancel starts none; a sysfs write already
  running in its worker thread cannot be stopped and finishes on its own. A cancel that lands after
  tt-smi exited, while the rescan's write finishes, still runs the reset's whole cleanup (the
  in-flight flag cleared, the rescan after the reset, returned chips dropped from the isolated set,
  the pollers back) and only then reaches the caller. A chip still dead stays off the bus, as before.
  The rescan comes at the start of the wait; an isolated chip whose link trains later in tt-smi's
  100 s wait is picked up by I78's repeat. Releases nothing: the full health check, fabric included, still
  decides (I32). TTR: class B1 with the chip isolated, from the hold, goes
  from about 505 s (one-shot 120 s, a failed 263 s reset, the 60 s I39 wait, a 62 s healing reset)
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
  check, health probes, aiclk ceiling) and every non-tenant holder the dead-chip rescue SIGKILLs is
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
  `auto_recovery_verified.json`) followed anchors nothing: a broker reboot or cycle verified on a
  later boot, the current boot once it is verified, an external cycle before that verification. A new
  drop after a healed cycle is a new episode and cycles at once, within the I9 backstop; a missing,
  non-finite or future verification stamp proves nothing and every anchor stays. At most one cycle
  fires per cooldown, and no hold waits on it without a stated time. Goal of 03 I4 holds after each
  cycle: the startup gate waits for the hugepage pool and passes the full check, fabric included,
  before any release; a failed check keeps the hold and the streak. A broker reboot in the episode anchors the
  cooldown from its boot like any other; once it ran, the rung asked again at the cooldown's end is
  the power cycle, never another reboot (I10). Time to the power-cycle request
  (the boot and the startup check follow it): class A with no cycle or boot in the last 30 min, at
  the first gate or idle pass (0 s on the tests' fake clock); class A within the cooldown, at its end
  (at most 30 min after the anchor; a boot 3 min before startup gives 27 min); class C, after its one
  mesh reset (262 s) or with none after the broker's own cycle, then as class A. A fault no cycle
  heals is cycled 30 min, 30 min, 1 h, 2 h, then every 4 h after each boot: at most 6 cycles a day,
  never a boot loop, never an open-ended hold. Class A follows the same spacing even when its cycles
  do not heal it and AC removal may be needed (I73): that state pages an operator but stops no cycle,
  and the full ladder runs 10 min after each cycle or boot inside the cooldown. Class A or C after a
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
  parent link with it; the record clears when the chip is back on the bus. The bridge rung resets
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
  reset re-powers every tray, so its driver-wait rescans (I81, I78) and the rescan after any reset follow a
  reset and stay. Recovered chips pass the full check, fabric included, before release (03 I4).
  Time to recover, removed all-ones chip: 0.1 s SBR + 1 s settle + rescan + up to 10 s for the
  driver to re-bind when the first try lands (formerly a 3 s rescan with no reset); at most 3 tries
  with a 4 s settle between and the 3 s closing rescan, 44.3 s of waits, when it never returns. A
  removed chip that cannot be reset holds and reaches the next rung 3 s sooner; a tray-down onset
  with a removed chip skips the prelude's rescan and its 3 s settle. Other classes are unchanged.
- **I71 — With a chip off the bus on a multi-chip mesh, the host rung is the power cycle; the
  warm reboot is its in-band fallback.** `_host_escalation_for_drop` names the power cycle first
  for a multi-chip climb with a chip off the bus (`off_bus > 0`: classes B1, B2) when
  `_auto_power_cycle_enabled` (I14). A warm reboot cannot re-power a UBB or re-enumerate a chip
  that left the bus (a whole-bus drop stayed 32/32 off after it); the BMC power cycle does all a
  reboot does and also re-powers the trays. A present mesh (`off_bus == 0`: a wedge, class D, or a
  fabric or dispatch fault the ladder could not clear, G2) and a single-chip host keep the older
  order: the warm reboot first (faster: no BMC, no I65 cooldown), the power cycle after it. The
  one exception is a present mesh on the boot an I29 77 reboot brought up (the I79 cold flag,
  passed as `warm_reboot_futile`): that reboot already ran and failed, so the multi-chip climb takes
  the power cycle when it is armed, holds loudly (`reboot_blocked`) when it is not, and never takes
  the fallback reboot below, even once the reboot's ledger interval has passed. The
  ladder below the host rung is unchanged: a partial drop still runs the bridge, tray and mesh
  resets first, and a present-mesh wedge takes its second `glx_reset` (`was_failing`) before the
  host rung; only a whole-tray or all-off drop goes straight to the cycle (I37). With no power
  cycle armed the older verdict stands, so no host loses a rung it had: a whole-bus drop, or a
  reset that regressed the mesh, hard-exited or left chips off below the floor, is
  `reboot_blocked` and holds loudly; anything else takes the warm reboot. A power cycle that the
  governor or the I65 cooldown denies, or that does not launch (unreachable BMC, failing command),
  falls back to the warm reboot at once, at the gate and on the idle climb
  (`power_cycle_reboot_fallback`, trigger `denied` or `launch_failed`), but only for a drop the
  reboot can recover (`_reboot_fallback_eligible`: the drops the no-cycle verdict hands the
  reboot), only with the reboot opted in, only within the reboot's own limits (one per boot,
  the ledger interval), and at most once per wedge: a broker reboot recorded since the last
  verified-healthy release (with none on record, within the longest I65 cooldown; an unreadable
  stamp counts) spends it (`reboot_fallback_spent`). A reboot that did not heal re-anchors the I65
  cooldown on its boot, and with an interval of an hour or more the reboot's own interval lapses
  first, so a second fallback would reboot hourly and the cycle would never fire; instead the hold
  asks for the cycle at that cooldown's end. A whole-bus or reboot-futile drop never falls back:
  it holds and asks for the cycle again at the deadline shown on /health. `_route` returns BLOCKED only when no reset
  scope is live; under a live scope it waits. The full health check, fabric included, runs after
  the cycle and after every fallback reboot before any release (03 I4). The I76 10-min hold bound
  is unchanged. Time to recover, armed multi-chip host, against the base: D and G2 unchanged
  (reboot, about 3-5 min, then the cycle); a below-floor partial drop unchanged (it was already
  reboot-futile and cycled); a non-futile partial drop with 16-31 chips off now cycles right after
  the ladder (about 5-8 min) instead of rebooting first and cycling after the reboot's boot and
  cooldown, and on a denied or failed cycle reboots at once instead of holding up to 30 min (4 h
  after repeated unhealed cycles) or retrying a cycle that cannot launch without bound; if that one
  reboot does not heal it, worst case the ladder, the reboot (about 3-5 min), its boot and the I65
  interval (30 min; 1 h, 2 h, then 4 h after repeated unhealed cycles), then the cycle, never a
  second fallback reboot; D and G2 take no second fallback reboot either; a D hold on the 77
  reboot's boot keeps I79's cold rung (one power cycle once the I65 cooldown allows it, never a
  second reboot), the same as the base; whole-bus
  and other reboot-futile drops, class A, C, single-chip hosts and hosts with no power cycle armed
  are unchanged.

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
  - while a host reboot or power cycle the broker requested is still pending, that request: every
    forced spawn declines until the pending window ends, so the time is the window's end (source
    `host_action_pending`);
  - otherwise the earliest of: the off-bus forced pass at the bound
    `offbus_power_cycle.watchdog_due_at` reports for this episode (the gated off-bus grace, the BMC
    IPMI hand-off bound, the failed tray walk hand-off bound, or with no power-cycle command the
    I33 operator hand-off deadline; each named in the source); the I65 retry of a power cycle the
    governor refused, only while that retry is armed for this hold episode (`power cycle`, source
    `power_cycle_cooldown`, or `power_cycle_missed_retry` / `ac_power_needed` when the refusal
    record says so); the I76 host rung (`power cycle`, or `warm reboot` where only that is opted
    in; source `hold bound host rung`) once the bound's ladder ran and no host rung was asked since,
    at once when the full check after that ladder failed or the rung is still pending, else at the
    end of that check's bound; once it is due the watchdog's branch chain stops there, so nothing
    below is a candidate; then, in the watchdog's own branch order, the off-bus one-shot (hold start
    plus the one-shot wait, while chips are off the bus and the episode's one-shot has not run), the
    I39 progress retry while armed for the episode, and the next unspent ceiling window
    (`device_hold_escalate_bucket + 1`, at least 1, at hold start plus that many ceilings). A branch
    stops the chain only once it is due (the one-shot's wait has passed, the retry's time has come),
    as the watchdog's own guard does; one not yet due is reported beside the branches after it, and
    the earliest wins, so an armed retry never hides a ceiling window that fires first;
  - the I73 class-A 10-min ladder (`forced ladder`, source `class A 10-min ladder`) while a whole tray
    is off and the cooldown refuses its cycle: 10 min after the later of the last cycle or boot and
    the hold's start, unless its pass for that anchor already started or the cooldown ends first;
  - the I70 host rung for an unkillable leftover (`host reboot or power cycle`, source
    `unkillable leftover budget`): at the end of its budget, then one retry interval after its last
    try; none once its episode closed or with no host rung opted in. It is the sampler's own rung,
    not a forced spawn, so the 10-min floor above does not move it;
  - every registered deadline feed.
  Each forced candidate above (the off-bus forced pass, the I65 retry, the class-A ladder, the I76
  host rung, the one-shot, the progress retry, the ceiling windows) is routed at its time: a
  startup-gate stand-down (spec 03 I75: a ceiling window or one-shot while the startup gate owns the
  hold) is reported as `startup health check, then <rung>` at the stand-down's bounded end (the
  re-adopted jobs' end plus `STARTUP_VERIFY_DEFER_MAX_SEC`), source `startup gate stand-down
  (<source>)`; a run that returns while the bound ladder's post-ladder check is pending is no
  candidate, so the next window or the host rung at that check's end is reported instead; a run
  routed to the I76 host rung reports `power cycle` (or `warm reboot`), source `hold bound host
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
  ceiling (600 s), source `hold bound (<ceiling>s)`, rung `full ladder, then power cycle` (`warm
  reboot` where only that is opted in; `full ladder` with no host rung opted in). Inside a
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
  by then fires nothing. The rung is chosen by `_host_escalation_for_drop` with the warm reboot
  futile whenever a chip is off the bus on a multi-chip host (a reboot does not bring an off-bus
  Galaxy chip back), so that case takes the power cycle; with every chip present the chooser's own
  order holds (the warm reboot, which ends every process, then the power cycle). No reset, sweep or
  tray re-power runs first (an exception to I22). Guards kept: a host action already pending (I34)
  asks nothing more (re-read under the device lock, so a try spawned before another path's request,
  such as the I76 bound's host rung, asks nothing: one power-cycle request per episode even when
  that launch timed out and retracted its governor record), a reset cycling in its scope, a running broker job or a readable tenant other
  than the leftovers themselves (they are discounted; an unreadable scan is waived as on the
  forced climb), the operator stand-back for the power cycle, and the governor (I9/I10 rate limits,
  the I65 cooldown). After the stand-back and the governor (the stand-back command can run for its
  whole timeout) the rung scans again and fires only if a reset scope still does not cycle, no
  tenant arrived and a leftover is still in that scan. A leftover that left at any point before the
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
  plus at most one 60 s retry tick to the request, then the host rung (a warm reboot about 3-5 min,
  a power cycle about 5-8 min) and the startup check (about 3 min): about 17-23 min (17-20 min by
  reboot, 19-23 min by power cycle). Later only while the governor holds the rung, each bounded and
  shown as `held` on `/health`: the I65 cooldown when a cycle or boot fell within its last 30 min,
  and, on a host with no power cycle opted in, the reboot loop guard
  (`AUTO_RECOVERY_MIN_INTERVAL_SEC`, 3600 s since the last automatic reboot). The `gone` stage and
  the second scan add no wait.

- **I78 — The I81 driver-wait rescan repeats, bounded, until every isolated chip is back.** I81's
  one rescan comes at the start of tt-smi's 100 s driver wait; a chip whose link trains later in that
  wait would still miss the reset, which then exits 1 and leaves the I39 retry to heal. So after the
  I81 rescan, while an isolated chip is still missing, `reset_with_quiesce` drops each returned chip
  from the isolated set (journaled `isolated_chips_returned`, context `mesh reset driver wait`) and
  rescans again every `DRIVER_WAIT_RESCAN_EVERY_SEC` (5 s), at most `DRIVER_WAIT_RESCAN_TRIES` (20)
  rescans in all, which span the 100 s wait. It stops as soon as no chip is isolated, the reset ends
  (its cleanup sets a stop event, so the reset never waits on a repeat), the reset is cancelled, or
  the tries are spent, journaled `reset_driver_wait_rescan_spent` with the tries and the chips still
  missing. The trigger is I81's: the settle's last count, never its first. tt-smi prints the first
  count as soon as the IPMI re-power returns, and a rescan then can enumerate a chip whose firmware
  is not up yet. Both reset backends deliver the last count only once the settle is over: the scoped
  backend reads by line, and tt-smi's countdown ends its line only after the last count; the local
  backend polls the output file and sees the last count when it is written. Releases nothing: the
  full health check, fabric included, still decides (I32), and a failed reset keeps I36, I38, I39 and
  the 10 min ladder bound, then the power cycle (I76). Time to recover, class B1 (one chip off the
  bus, isolated, healed by a mesh reset). Measured before, one episode, onset to release: about
  575 s (about 123 s to the first reset, a failed 263 s mesh reset, a 65 s wait for the I39 retry, a
  62 s healing reset, about 60 s of full check with fabric before release). With I81 and I78, est.:
  about 245 s (123 s, one 62 s healing reset, 60 s check); a chip that trains late in the wait adds
  at most the rest of the 100 s wait instead of a failed reset and a retry. Worst case, a chip that
  never comes back: unchanged, the failed 263 s reset, then I39, inside the 10 min ladder bound and
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
    ceiling wait (`reason=present_mesh_reset_failed`).
  - **An off-bus hold whose reset regressed the mesh** (class B1, I31): a reset on the gate or the
    idle ladder left more chips off the bus than before it. I31 bars the bridge, tray and mesh reset
    rungs for the rest of the episode, so the ceiling wait has no rung left to try. The climb runs
    the last-chance sweep first (I22), then the cold rung (warm reboot futile), without the ceiling
    wait (`reason=reset_regressed_offbus`). A cold rung `auto_recovery_allowed` holds off (the I65
    cooldown, a tenant, the rate limit) returns before the sweep, as at the ceiling, and is asked
    again on the next forced window; the I76 bound still runs the full ladder at 10 min. With no
    power cycle opted in it pages (I31) at once rather than at the ceiling. A sweep whose full check
    passes, fabric included, releases the device (I32); the boot after the power cycle runs the
    startup gate's full check (03 I4). Time to recover: one 8-chip hold whose post-job reset dropped
    one chip waited 512 s for the ceiling, swept for 110 s, power-cycled and was released at 904 s;
    the same episode now sweeps at about 98 s, cycles at about 210 s and is released at about 390 s.
    On a Galaxy, a healing mesh reset followed by a second chip's drop now sweeps (a per-tray
    re-power) at the drop instead of up to one forced window (80 s measured) later.
  - **The boot an I29 77 reboot brought up** (with no fabric pass since the broker started) and a
    power cycle opted in: the 77 is not handed to an operator. The episode owes the climb at once. The
    earlier episode's reset counts as its one reset, the sweep runs on the first re-check, and the
    climb takes the cold rung (warm reboot futile), once. The cold flag holds on a present mesh too:
    `_host_escalation_for_drop` names the power cycle for it, and I71's fallback reboot never
    replaces a cold rung the cooldown or the governor denies. The I40 fallback marks the episode's reset
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
  next 600 s backoff step). The post-reboot boot asks for the power cycle about 8 min after its hold
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
  refuses it. At the bound the I46 latch is lifted
  (`hold_bound_latch_cleared`), so the full health check after the ladder, fabric included, gets a
  real verdict and only a pass releases (goal of I8/spec 03 I4, I78); a job that fails the same way
  declares it again. The spec 03 I42 latch stays: it withholds no check (a latched hold already takes
  the ladder), it makes a topology mismatch FAIL the check, so the post-ladder check cannot pass on
  the links the lift's first job failed on. A post-ladder check that finds any descriptor port
  missing (spec 03 I71) or, under the I42 latch, any mismatch is a failed check: no release, the host
  rung is asked for (subject to the I65 cooldown). Below the bound the faster class paths stay as they are; a forced run that
  started below the bound (the off-bus one-shot, a progress retry, a gated grace) is not the bound's
  ladder, so at the bound the full ladder runs again before any host rung. When the ladder
  leaves the mesh unrecovered, the broker's own code asks for the host rung at once: the BMC power
  cycle, or the warm reboot where only that is opted in. When the ladder brings the mesh back, only its
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
  (`hold_bound_host_rung`). Only that bounded boot / post-power-cycle cooldown may hold longer than
  the bound, with one exception: a live readable tenant that is not a broker job's leftover (invariant
  4, I6/I7) still refuses every rung, so such a hold is bounded only by the tenant. It is never silent:
  at the bound the broker journals and pages `hold_bound_tenant_blocked` once per episode, naming the
  blocker (pid, user, command), `/health` shows `next_step` `tenant_blocked` with `blocked_by`, and
  on the first sampler tick after the tenant leaves the ladder runs, then the host rung if the full
  check fails. With both host rungs
  opted out (`TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` and `TT_DEVICE_MCP_AUTO_REBOOT=0`) the hold pages
  `hold_bound_no_host_rung` once per episode. The operator kill switch
  `TT_DEVICE_MCP_FORCE_ESCALATE=0` turns off only the forced runs below the bound; the bound's ladder
  and host rung ignore it, no env or config switches the bound off, and the boot rung inventory's
  `RUNG OFF forced-escalation` warning says so. `/health` carries `hold_bound` while a hold is open:
  `bound_sec`, `deadline`, `held_age_sec`, `next_step` (`ladder`, `power-cycle` or
  `tenant_blocked` with `blocked_by`) and, while the host rung waits on it, `cooldown_until`; until the
  bound's ladder runs, `next_rung`, `next_rung_at` and `next_rung_source` name its mark too (I75's
  deadline feed). One helper, `hold_bound_deadline()`, gives the open
  hold's escalation deadline for every hold kind: its start + the ceiling, or the end of a running
  power-cycle cooldown when that is later; `/health`'s `deadline` and the stuck-hold event's and
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
  with no rung run.
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
  on, whatever the boot does later: it may escalate only by reboot, or not at all, and its power-cycle
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
  | A or B1 after a cycle that landed late | doubles 30 min, 1 h, 2 h, 4 h | unchanged: the late cycle counts and doubles on every path, also when its boot escalates only by reboot (the startup mark) | I65 boot-loop guard (stated exception) |
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
  cooldown); the hold message says AC removal may be needed, that the ladder runs 10 min after the
  last cycle or boot and when the next cycle comes. Inside any class-A cooldown (two counted cycles
  or not), once the hold has stood `CLASS_A_LADDER_AFTER_SEC` (600 s) since the later of the last
  cycle or boot and the hold's start, the hold-deadline watchdog starts one forced pass at that mark
  (not at the next ceiling window) and the broker's own off-bus ladder runs once, as its gates allow:
  for a whole-tray drop that is the per-tray re-power only (`glx_reset` is suppressed after a failed
  tray walk below the galaxy-reset floor, and the last-chance sweep is skipped for class A and C,
  I37). It runs once per anchor (the later of the last cycle or boot and the hold's start). Each
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
  rung fires as it ends. The carry ends, and the next hold is a fresh episode with its own class
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
  host rung; only a hold whose start is unknown counts from the restart. The carry never crosses a
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
  | Every other class path below the bound | unchanged | unchanged |

- **I83 — A fabric link-short hold gets one ladder at hold start, then the power cycle.** A hold
  with every expected chip on the bus (none isolated) whose cause is a topology mismatch or a known
  fabric fault (I30's causes other than a 77: the full fabric check counts fewer links or ports than
  the cabling expects; such a count is a failed check, never a pass), and that I30 holds without
  measuring (no spec 03 I42 latch, no missing descriptor port), is the fabric link-short class. A
  latched or missing-port hold is measured and is not this class: it keeps the I76 path (the ladder at
  the 10-min bound with the I42 latch kept, then the host rung). On the hold's first watchdog tick the
  broker runs its full reset ladder once (`escalation_kind` `fabric_link_short`), lifting only spec 03
  I46's known-fabric-fault latch first as at the bound, and then the full health check, fabric
  included. A check that passes releases the device as any check does (spec 03); the hold never lifts
  without it. A check that still counts short, or a ladder that does not bring the mesh back, asks for
  the I76 host rung (the power cycle) in the same run. A run that finds the hold is not (or no longer)
  this class, e.g. a chip already off the bus by its heartbeat, declines once for the episode and is
  not spawned again; the hold keeps its own class's path. That pass stands in for the episode's bound
  ladder only once the host rung after it was granted or its cooldown retry armed: then no later tick
  (the 10-min bound, a later ceiling window, the hold deadline) runs a second glx_reset or ladder, and
  the host rung is next. The I82 carry and a broker restart keep the pass's age and its at-start
  mark but not the grant, so after either the bound still runs one more full ladder (about 220 s,
  bounded), then the host rung. When no host
  rung was granted (both host rungs opted out, or a denial other than the cooldown), the pass is a
  forced run below the bound and the full ladder runs again at the 10-min mark, then the I76 cadence.
  A re-hold of another class in the same episode below the bound (I82 carry or a broker restart)
  asks for the host rung once and runs no further forced ladder. A check still waiting on the 1G
  hugepage pool keeps the I76 check window, but that window ends at the 10-min bound at the latest.
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
  then I76's cadence. Latched and missing-port holds: the I76 path, ladder at 600 s then the host rung.
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
opt-in, two strikes, no live scope, and a mass drop or a still-unhealthy present mesh); a failed
tray walk → `WAIT` (the next rung is the cold one, never the suppressed mesh reset); a healthy
fault-free mesh → `RELEASE`, except a fabric pass that ran without a verdict on a dirty/multi-chip
host → `HOLD_FABRIC_UNVERIFIED`; eth-frozen → `WAIT`; cooling → `WAIT`; live scope → `DEFER`
(adopt, never start a second); SBR candidates → `bridge_reset`; below the floor → `ubb_tray`;
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
holds live silicon. The tray rung walks only the affected trays (I25), one at a time,
holding `reset_in_flight` and the device pollers stopped across the walk (restored after it, as in
I12), and never applies to a fully-off-bus mesh (that is the cold rung's case). After each tray's
re-power it rescans that tray's root ports (the whole bus only when none is known) and settles
(`UBB_WALK_RESCAN_SETTLE_SEC`) before re-verifying,
fabric included: a re-power does not undo a chip's sysfs `remove`. After a failed verify it re-reads
the bus. If a chip that was on the bus before the walk is now off, the walk stops at once
(`ubb_reset_regressed_offbus`) rather than re-power more trays, and the per-tray rung is declined
on this host for `TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS` (default 7) days (`ubb_tray_rung_disabled`,
with `latch_days` and `clear_with`, the operator's re-arm command). The decline is host-wide, not per
tray: a single-tray re-power has taken the other trays off the bus with it.
The record goes, fsync'd, to `ubb_tray_rung_disabled.json` in the health dir with the boot id, the
host name and the time. A broker restart or a host power cycle keeps it
(`ubb_tray_rung_disabled_restored`, with the boot id it was latched on): a power cycle does not
repair the UBB whose re-power regressed, and a per-boot latch let the rung regress the mesh again
after every cycle. A record older than the window, or written by another host, is removed and the
rung re-arms (`ubb_tray_rung_rearmed`); one with no time of its own is aged by the file's mtime, and
one whose age cannot be read counts. After the UBB is repaired an operator re-arms the rung with
`python -m tt_device_mcp.health.recovery.pcie_guard --clear-tray-rung-latch [--health-dir DIR]`
as root (`ubb_tray_rung_cleared`, `by` `operator`; exit 1 when the record cannot be removed, 2 on
bad usage); a running broker whose written record is gone re-arms on its next look
(`ubb_tray_rung_cleared`, `by` `file_removed`), without a restart. A delayed cross-tray drop latches the same record: when chips that were on the
bus before the last tray re-power, on trays it did not re-power, leave the bus within
`TRAY_PULSE_WINDOW_SEC` (900 s) of it and every one sits at in-tray position 5 (chips 5/13/21/29 on
a Galaxy), the next look (the tray rung's evidence or the sweep) disables the rung; any other drop
pattern, such as a mesh reset's, does not. The drop often lands after the pulse's own hold was
released, so the match spans hold episodes inside the window; the record names the pulse's episode
(`episode`) and, when it differs, the one the drop was seen in (`seen_in`), so an operator can tell
an outside drop from the pulse's and re-arm the rung. While the rung is declined the walk holds (the next
generic drop names the command), and the back-to-back sweep (TRAY_DOWN_NO_WINDOW and the
last-chance sweep before a host rung) skips the tray re-power (`ubb_reset_skipped_regressed`), as
when opted out: its SBR and mesh reset still run and the sweep still counts as complete (I22), so
the ladder is never cut short; the off-bus hold then takes the bounded end of I27. Skipping the
re-power changes no release rule: only a passing full health check, fabric included, after the
sweep's resets counts as recovered. A healthy tray is never swept (I25):
re-powering it cannot bring back a chip on another tray and can knock its own chips off, so a mesh
that still fails once every affected tray was re-powered (an off-bus chip or a fabric fault) goes to
the next rung. Either way the walk returns False, so the ladder climbs to the same next rungs as an
exhausted walk; no rung is skipped. Every tray it names — the affected
set, the walk order, the bitmap, and the chip ids the ioctl handshake quiesces — comes from the
banked chip→bus map of I16, so the trays in the `ubb_reset_required` event are the ones an operator
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

**The tray-down prelude (I19).** Every caller of the ladder (`escalate()`: the gate, the idle
relift, the hold-deadline watchdog) passes through `_tray_down_latch` first, which latches
`TRAY_DOWN` or `PARTIAL` at the first sighting (journalled as `tray_down_latched`, with whether a
reset came first). A `TRAY_DOWN` episode whose prelude has not run goes to `_tray_down_prelude`:
with no tenant it writes `1` to `/sys/bus/pci/rescan`; it then captures (`bmc_capture`, all reads
concurrent, each under its own timeout — 8 s for the BMC, which answers slowly while a tray browns
out — and the whole under the 10 s deadline); it waits what is left of
`TRAY_DOWN_RESCAN_SETTLE_SEC` and re-reads the heartbeats. The lspci target is the parent of the
chip's sysfs node, found from the banked PCI address (I16), or, once the node is gone, the bridge
whose secondary bus is the chip's bus, so the link status (LnkSta/LnkCap) is kept even when the
endpoint is gone; the endpoint is read too while it is still enumerated. One `TRAY-DOWN:` error
line names the trays, the chips off, the rescan result and the capture, and says the full ladder
follows; `tray_down_prelude` journals the same. Then `escalate()` dispatches the caller's own
ladder with the caller's own stage, indices, evidence and beats, so the `TRAY_DOWN_NO_WINDOW`
branch above, the generic walk, the mesh-wide reset, the last-chance sweep and the host rungs run
as they would have. When the `TRAY_DOWN_NO_WINDOW` verify fails, the branch re-reads the
heartbeats once and logs the count then off the bus as `off_bus_after` on
`tray_down_no_window_power_cycle` and in its blocked/denied log lines (`None` if the re-read
fails); it is log-only, and `off_bus`, the count at onset, still drives every decision.

**The last-chance reset sweep gates EVERY host rung.** A reboot or a power cycle takes the whole box
down and costs minutes, so before paying that the ladder re-issues every reset type once more,
back-to-back, then takes ONE settle and ONE verify — and fires the host rung only if the mesh is
still bad (`_settle_and_verify_before_host_rung` → `_issue_all_resets_back_to_back`, the same
implementation the `TRAY_DOWN_NO_WINDOW` branch uses, so the two can never drift). The sweep's
per-tray BMC re-power is auditable from the journal alone: before the fire it logs one line per tray
naming the tray, its BMC bit and its chips (from the I16 map) beside the exact `ipmitool` command;
after it, a `ubb_reset_fired` event (`trays`, `ubb_bitmap`, `chips`, `command`) on success, or a
`ubb_reset_failed` event with the same fields, the BMC's exit code as `rc` (null when ipmitool never
exited) and the error. The single-tray walk journals each clean pulse the same way (`tray` in place of
`trays`). This is logging only: it changes no tray fired, no timing and no hold, and the full health
check still has to pass after the sweep before anything is released. This holds on
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
scan is empty. Measured on box B 2026-09-17: the stuck-hold escalation read an empty scan 106 s into a running
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
that just failed), or holds loudly naming the BMC command when the cold rung is not armed. It runs
on the boot's first broker start only (`fsm.restarted_this_boot` false): the ledger entry stays
adjacent to the boot for its whole life, and a restart later in the same boot that finds a fresh
degradation is not that reboot's verdict — it stays held for the forced startup gate and its ladder.
The boot's jobs-list row is written once per boot, keyed on the kernel boot_id; where that is
unreadable, on `/proc/stat` btime matched within `_BTIME_DEDUP_SLACK_SEC` (10 s), because a
wall-clock step moves btime within one boot and an exact match would count one reboot twice.
Absence is never health: every blocked/exhausted path emits a loud actionable event
(`ubb_reset_required`, `all-off-bus power-cycle required`,
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
the poller stop and restart (30 s per `systemctl` call, 120 s with the two default services), the
reset (`DEVICE_RESET_TIMEOUT_SEC`, 600 s; a reset still running then is `reset_failed` with no
verify), the 3 s PCI-rescan settle, and the verify passes. A re-check follows only a 77, so every
pass but the last one finished: up to 361 s (heartbeat settle 0.5 s + snapshot 90 s + eth python
probe 3 × 10 s + eth read 60 s + fabric `FABRIC_CHECK_TIMEOUT_SEC` 180 s). The last pass can time
out and adds the 75 s kill sequence (`GRACEFUL_KILL_GRACE_SEC` + `SIGTERM_GRACE_SEC`): up to
436 s. With `POST_RESET_FABRIC_RETRIES` (1) re-checks, each after `POST_RESET_FABRIC_RETRY_SLEEP_SEC`
(60 s): 1579 s, about 26 minutes, at the defaults. Not counted: stopping a running job first (60 s
grace, then the kill), waiting for a broker operation already holding the lock, and the PCI-rescan
write itself; the bound also assumes a killed process exits. Typical on a healthy Galaxy:
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
- **The per-tray reset is the broker's own.** tt-smi offers no per-tray reset: `-glx_reset` is
  all-or-nothing, and UMD's tray entry point takes only the full 32-chip bitmap. So this rung
  issues `ipmitool raw 0x30 0x8b` itself and drives the USER_RESET/POST_RESET ioctls around it,
  which it can only do because the gate's pass already banked every chip's bus (I16).
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
| I22 a host rung holds when the last-chance sweep was skipped or gated; it fires after a full sweep | `tests/test_ladder_v2.py::test_settle_and_verify_gates_the_power_cycle_host_rung`, `tests/test_pcie_guard.py::test_a_gated_sweep_never_reaches_the_power_cycle`, `::test_a_refused_tray_fire_makes_the_sweep_incomplete` |
| I23 the tray map is cross-checked against sysfs; a mismatch or missing table refuses | `tests/test_pcie_guard.py::test_plan_refuses_a_tray_map_that_points_at_another_tray`, `::test_plan_refuses_without_a_tray_table`, `::test_envelope_refuses_a_mismatched_tray_map` |
| I23 the envelope's order: mask every port, quiesce, remove, fire, rescan, re-init, restore | `tests/test_pcie_guard.py::test_envelope_masks_every_port_removes_the_tray_fires_rescans_and_restores`, `::test_envelope_restores_and_rescans_even_when_the_pulse_fails`, `::test_pollers_are_stopped_across_the_tray_fire` |
| I23 only the re-powered tray's root ports are rescanned; the global rescan only when none is known | `tests/test_pcie_guard.py::test_the_envelope_rescans_only_the_re_powered_trays_root_ports`, `::test_the_envelope_falls_back_to_the_global_rescan_when_no_tray_port_is_known`, `tests/test_device_safety.py::test_the_walk_rescans_only_the_re_powered_trays_root_ports` |
| I23 a chip on another tray newly all-ones gets one remove + scoped rescan | `tests/test_pcie_guard.py::test_a_sibling_chip_newly_all_ones_gets_one_remove_and_scoped_rescan`, `::test_a_sibling_chip_already_all_ones_or_on_the_tray_is_not_touched` |
| I25 a regressed walk disables the tray rung on this host; the back-to-back sweep skips the tray re-power and stays complete | `tests/test_device_safety.py::test_a_regressed_walk_disables_the_tray_rung_for_the_process`, `tests/test_pcie_guard.py::test_a_disabled_tray_walk_leaves_the_envelope_armed`, `tests/test_ladder_v2.py::test_a_regressed_tray_rung_is_skipped_in_the_last_chance_sweep`, `::test_gate_tray_down_no_window_dispatches_the_back_to_back_sweep` |
| I25 the disabled tray rung survives a broker restart and a new boot on the same host, for a bounded window; another host's or an expired record re-arms it; a record with no time is aged by its file | `tests/test_pcie_guard.py::test_a_disabled_tray_rung_survives_a_broker_restart_on_the_same_boot`, `::test_a_new_boot_keeps_the_tray_rung_latch`, `::test_the_tray_rung_latch_expires_after_its_window`, `::test_a_tray_rung_record_from_another_host_never_counts`, `::test_a_tray_rung_record_without_a_time_is_aged_by_its_file`, `tests/test_offbus_power_cycle.py::test_a_regression_recorded_before_a_reboot_keeps_the_walk_off` |
| I25/I27 a below-floor drop with a whole tray off is never re-powered: the post-job gate and the idle escalation hand it to the power cycle at once and keep the device held; a partial drop still walks and is released only after the full check, fabric included | `tests/test_offbus_power_cycle.py::test_the_post_job_gate_hands_a_whole_tray_drop_to_the_power_cycle`, `::test_by_default_the_one_tray_drop_is_never_walked`, `::test_a_partial_drop_below_the_floor_still_walks_and_releases_only_after_the_full_check`, `tests/test_device_safety.py::test_a_whole_tray_below_the_floor_is_not_re_powered_alone_but_two_trays_still_walk` |
| I25 a tray pulse followed by position-5 chips on other trays leaving the bus disables the rung | `tests/test_device_safety.py::test_a_tray_pulse_that_drops_position_5_on_other_trays_disables_the_rung` |
| I25 an operator re-arms the declined tray rung after a repair (exit 1 when it cannot remove the record, 2 on bad usage); a running broker re-arms when its written record is removed | `tests/test_pcie_guard.py::test_the_operator_re_arms_the_tray_rung_after_a_repair`, `::test_a_tray_rung_latch_that_cannot_be_removed_is_reported`, `::test_a_running_broker_re_arms_when_the_operator_removes_the_record` |
| I25 a delayed position-5 drop in a later hold still latches and the record names both episodes | `tests/test_pcie_guard.py::test_a_position_5_drop_in_a_later_hold_still_latches_and_names_both_episodes` |
| I25/I27 an idle whole-tray decline is journaled as `whole_tray`, never a failed walk; the firing event names the class and hand-off | `tests/test_offbus_power_cycle.py::test_an_idle_whole_tray_decline_is_journaled_as_whole_tray_not_a_failed_walk` |
| I27 a whole tray that came back in part is not cycled at grace 0 and stays held, once per episode (a flapping tray fires on the next whole-tray read); one still wholly off, or a re-read the map cannot place, fires at once | `tests/test_offbus_power_cycle.py::test_a_whole_tray_that_comes_back_in_part_is_not_cycled_at_grace_0`, `::test_a_flapping_whole_tray_still_reaches_the_power_cycle`, `::test_the_whole_tray_deferral_is_per_episode`, `::test_a_tray_still_wholly_off_at_the_re_read_still_fires_at_once`, `::test_a_re_read_the_tray_map_cannot_place_keeps_the_whole_tray_verdict` |
| I25 / I32 a skipped tray re-power releases only after the full health check, fabric included | `tests/test_ladder_v2.py::test_a_skipped_tray_rung_releases_only_after_the_full_health_check` |
| I27 with a power cycle configured, a whole tray down goes straight to it; a partial drop gets one lifted ladder pass (the mesh reset) past the grace, spent only when its climb reaches the rungs (a pass declined by scope, tenant or the ceiling stays due), then the power cycle on the next forced pass only if the hold stands; each reset releases only after the full check, fabric included, and the cycle releases nothing | `tests/test_gated_hold_handoff.py::test_a_whole_tray_down_goes_straight_to_the_power_cycle`, `::test_a_partial_drop_gets_the_mesh_reset_first_and_the_power_cycle_only_if_it_fails`, `::test_a_partial_drop_the_mesh_reset_heals_never_gets_the_power_cycle`, `::test_a_partial_drop_with_no_tray_map_gets_the_mesh_reset_first`, `::test_before_the_grace_a_partial_drop_gets_no_lifted_reset`, `::test_a_declined_lifted_pass_is_not_spent_and_the_mesh_reset_still_runs_first`, `::test_the_partial_drops_mesh_reset_releases_only_after_a_passing_fabric_check`, `::test_the_power_cycle_releases_nothing`, `tests/test_startup_fabric_hugepages.py::test_startup_fabric_pass_waits_for_hugepages_then_runs` |
| I27 a regressed tray rung hands off to the power cycle past the grace | `tests/test_offbus_power_cycle.py::test_a_regressed_tray_rung_hands_off_past_the_grace` |
| I23 holders on every chip of the box, off-bus ones included: wait, then refuse | `tests/test_pcie_guard.py::test_envelope_waits_for_holders_then_refuses_without_touching_anything`, `::test_envelope_proceeds_once_the_holder_lets_go`, `::test_envelope_waits_for_holders_on_every_tray_not_just_the_re_powered_one`, `::test_envelope_counts_a_holder_on_a_chip_off_the_bus`, `::test_envelope_ignores_the_brokers_own_hold_on_other_trays` |
| I23 an erroring port stays masked and starts the flood window | `tests/test_pcie_guard.py::test_a_port_that_keeps_erroring_stays_masked_and_marks_a_flood` |
| I84 a dispatch-cap 77 after a reset re-checks at once (the full check, fabric included); any other 77 waits for eth training; a persisting one stays held | `tests/test_post_reset_gate.py::test_a_dispatch_cap_77_after_a_reset_rechecks_at_once_and_class_j_fits_its_budget`, `::test_a_no_trained_link_77_after_a_reset_still_waits_for_eth_training`, `::test_a_dispatch_cap_77_that_persists_never_releases`, `::test_the_wrapper_reason_names_the_dispatch_cap_over_a_validator_error_line` |
| I23 dry run and plan CLI touch nothing | `tests/test_pcie_guard.py::test_dry_run_logs_the_plan_and_touches_nothing`, `::test_cli_prints_the_plan` |
| I24 `hold` counts an all-ones chip still in sysfs as off the bus; the mesh reset and the sweep refuse over it | `tests/test_pcie_guard.py::test_chips_off_bus_counts_an_all_ones_chip_still_in_sysfs`, `::test_a_hold_gate_refuses_the_mesh_reset_and_the_sweep_over_an_all_ones_chip` |
| I24 ENODATA on a bound endpoint is ARC-not-ready, not off the bus; an unbound endpoint, a missing node or another errno is off | `tests/test_arc_not_ready.py::test_enodata_on_31_bound_chips_is_not_off_the_bus`, `tests/test_arc_not_ready.py::test_an_unbound_or_missing_chip_still_counts_off_the_bus` |
| I24 the per-chip bridge SBR obeys the hold gate and the latch | `tests/test_pcie_guard.py::test_the_per_chip_bridge_reset_obeys_the_hold_gate_and_the_latch` |
| I25 the last-chance sweep names each off-bus chip, its PCI address and tray, read fresh from the heartbeats | `tests/test_ubb_tray_map.py::test_the_last_chance_sweep_names_the_chip_it_re_powers_a_tray_for`, `::test_the_off_bus_placement_marks_what_the_map_cannot_place` |
| I25 the walk and the sweep re-power only affected trays; X alone recovers chips off on X; a failed walk climbs to the mesh reset | `tests/test_ubb_tray_map.py::test_the_walk_covers_only_the_affected_trays`, `tests/test_device_safety.py::test_ubb_tray_walk_plan_walks_only_the_affected_trays`, `::test_ubb_tray_reset_walk_recovers_chips_off_on_any_tray_by_re_powering_that_tray_alone`, `::test_ubb_tray_reset_walk_never_sweeps_healthy_trays_when_the_affected_tray_does_not_recover`, `::test_ubb_tray_reset_walk_falls_through_to_the_hold_when_the_affected_tray_stays_off`, `tests/test_pcie_guard.py::test_the_last_chance_sweep_re_powers_only_the_affected_trays` |
| I25 a below-floor drop on one tray is not re-powered alone; the hold goes to the next rung; an opted-in single-tray walk releases only on the full check | `tests/test_device_safety.py::test_a_one_chip_drop_on_a_full_galaxy_never_re_powers_its_tray`, `::test_a_whole_tray_below_the_floor_is_not_re_powered_alone_but_two_trays_still_walk`, `::test_gate_holds_a_one_chip_drop_without_a_tray_re_power`, `::test_a_declined_tray_walk_hands_the_stuck_hold_to_the_next_rung`, `::test_an_opted_in_single_tray_walk_releases_only_after_the_full_check`, `tests/test_offbus_power_cycle.py::test_by_default_the_one_tray_drop_is_never_walked` |
| I25 the host-hang latch: intent, latch on a new boot, not on the same boot, clear, opt-out | `tests/test_pcie_guard.py::test_an_off_bus_reset_writes_an_intent_and_removes_it_when_done`, `::test_an_intent_from_a_boot_that_died_latches_hold_until_cleared`, `::test_an_intent_from_this_boot_does_not_latch`, `::test_the_latch_can_be_switched_off`, `::test_a_tray_re_power_carries_an_intent_and_ends_it` |
| I25 the broker's own reboot or power cycle never latches: the intent is removed and synced before it fires; the next boot drops an intent older than its ledger row, leaves glx_reset ungated and still runs the full startup check after hugepages; an intent with no later host action, or one whose row did not bring this boot up (boot outside the attribution window, btime unreadable, a missed request past its pending window), still latches; a late cycle (I72) inside its window does not | `tests/test_pcie_guard.py::test_a_sweep_that_fails_its_verify_leaves_no_intent_for_the_power_cycle`, `::test_a_broker_host_action_closes_an_open_intent_durably_before_it_fires`, `::test_an_intent_older_than_the_brokers_own_cycle_does_not_latch`, `::test_an_intent_with_no_later_host_action_still_latches`, `::test_a_host_action_that_did_not_bring_this_boot_up_still_latches`, `::test_the_brokers_cycle_that_brought_this_boot_up_does_not_latch`, `tests/test_startup_fabric_hugepages.py::test_the_boot_after_the_brokers_own_power_cycle_latches_nothing_and_still_runs_the_full_check` |
| I24 gate modes: off by default, hold on an off-bus chip, guard on a flood | `tests/test_pcie_guard.py::test_gate_is_off_by_default`, `::test_hold_refuses_a_reset_over_chips_already_off_the_bus`, `::test_guard_refuses_during_an_aer_flood`, `::test_first_look_after_a_recent_boot_counts_errors_since_boot`, `::test_hold_gate_stops_an_automatic_mesh_reset_over_an_off_bus_chip`, `::test_guard_masks_the_root_ports_around_an_automatic_mesh_reset` |
| I23 an off-bus chip's remembered root port is masked (tray fire and mesh reset); a port gone from sysfs is not | `tests/test_pcie_guard.py::test_an_off_bus_chips_root_port_is_masked_for_its_trays_re_power`, `::test_guard_masks_an_off_bus_chips_port_around_a_mesh_reset`, `::test_a_remembered_port_gone_from_sysfs_is_not_masked` |
| I23 the topology is recorded at broker start and in each between-jobs check, rewritten only on change; a port never seen is not known | `tests/test_pcie_guard.py::test_broker_start_records_the_topology_for_the_first_tray_drop`, `::test_the_between_jobs_check_records_the_topology_for_the_first_tray_drop`, `::test_an_unchanged_topology_is_not_rewritten`, `::test_without_a_healthy_look_an_off_bus_chips_port_is_not_known` |
| I23 unreadable sysfs refuses the tray fire (all-off included) and a look keeps the recorded topology | `tests/test_pcie_guard.py::test_a_tray_re_power_refuses_when_sysfs_cannot_be_read`, `::test_an_all_off_mesh_behind_an_unreadable_sysfs_is_not_re_powered`, `::test_an_unreadable_look_keeps_the_recorded_topology`, `::test_an_unreadable_sysfs_counts_every_chip_off_so_a_hold_gate_holds` |
| I23 a 5+ digit PCI domain parses; an unrelated entry sysfs cannot read is skipped; a `guard` mask over unreadable sysfs logs `aer_mask_failed` | `tests/test_pcie_guard.py::test_a_5_digit_domain_beside_the_mesh_does_not_make_sysfs_unreadable`, `::test_a_chip_in_a_5_digit_domain_is_parsed_and_counted`, `::test_an_unrelated_entry_the_scan_cannot_read_is_skipped`, `::test_an_unreadable_sysfs_under_guard_logs_a_mask_failure` |
| I23 an all-off mesh is re-powered and its sweep is complete | `tests/test_pcie_guard.py::test_an_all_off_bus_mesh_still_gets_its_trays_re_powered`, `::test_an_all_off_bus_last_chance_sweep_is_complete` |
| I23 mask before quiesce; a mask failure refuses; a failed quiesce still re-inits and restores | `tests/test_pcie_guard.py::test_the_envelope_masks_before_it_quiesces_and_refuses_when_it_cannot_mask`, `::test_a_quiesce_that_raises_still_re_inits_and_restores` |
| I24 the flood is a rate; the flood window refuses even under `off`; a refused reset is not recorded | `tests/test_pcie_guard.py::test_a_steady_trickle_between_distant_looks_is_not_a_flood`, `::test_a_trickle_since_an_old_boot_is_not_a_flood`, `::test_the_flood_window_holds_automatic_resets_even_with_the_gate_off`, `::test_after_a_kept_masked_port_the_stuck_hold_mesh_reset_does_not_fire`, `::test_a_gated_mesh_reset_records_no_reset_and_arms_no_cooldown` |
| I25 the clear command finds the system broker's latch; one it cannot remove is reported | `tests/test_pcie_guard.py::test_the_clear_command_finds_the_system_brokers_latch`, `::test_a_latch_that_cannot_be_removed_is_reported_not_called_absent` |
| I25 the latch is scoped to its boot: a later boot ignores and drops it, and it never holds the I27 power cycle | `tests/test_pcie_guard.py::test_a_latch_holds_only_on_the_boot_it_was_written_in`, `::test_a_latch_from_an_ended_boot_never_gates_even_if_the_broker_never_restarted`, `tests/test_offbus_power_cycle.py::test_a_latch_never_blocks_a_rung_after_its_boot_nor_the_power_cycle_on_it` |
| I25 the latch is bounded: it lifts with every chip on the bus (a link that reads down is not) and host-pci OK, or at its ceiling with every chip on the bus; with a chip off or sysfs unreadable only at its ceiling with no power cycle configured (paged once, the gate falls back to its mode); one it cannot remove keeps holding; the hold names both boots | `tests/test_pcie_guard.py::test_the_latch_lifts_once_every_chip_is_back_and_host_pci_is_ok`, `::test_the_latch_never_lifts_while_a_chip_is_off_the_bus`, `::test_the_latch_never_lifts_while_a_listed_link_reads_down`, `::test_with_no_power_cycle_a_latch_with_a_chip_off_lifts_at_its_ceiling`, `::test_with_no_power_cycle_the_gate_itself_lifts_a_latch_past_its_ceiling`, `::test_with_a_power_cycle_a_latch_with_a_chip_off_still_holds_past_its_ceiling`, `::test_the_hold_message_names_both_boots`, `::test_the_latch_lifts_at_its_ceiling_with_every_chip_on_the_bus`, `::test_the_latch_ceiling_defaults_and_rejects_nonsense`, `::test_a_latch_that_cannot_be_removed_keeps_holding`, `::test_the_between_jobs_check_lifts_the_latch_once_the_mesh_is_back` |
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
| I18 reset refuses (busy) over a running or held job; force stops it (re-adopted included); idle proceeds | `tests/test_reset.py::test_reset_refuses_while_a_job_is_running_and_leaves_it_alone`, `tests/test_reset.py::test_reset_refuses_while_a_hung_job_is_still_held`, `tests/test_reset.py::test_reset_refuses_over_a_readopted_running_job`, `tests/test_reset.py::test_queued_jobs_do_not_block_a_reset`, `tests/test_reset.py::test_forced_reset_stops_the_running_job_and_logs_it`, `tests/test_reset.py::test_forced_reset_stops_a_readopted_job`, `tests/test_reset.py::test_streaming_forced_reset_stops_a_readopted_job`, `tests/test_reset.py::test_forced_reset_stops_a_readopted_job_seen_only_by_the_recheck`, `tests/test_reset.py::test_reset_refuses_while_a_stopped_readopted_job_is_still_being_torn_down`, `tests/test_reset.py::test_forced_reset_waits_for_the_readopted_job_to_be_finalized`, `tests/test_reset.py::test_a_readopted_job_killed_by_a_forced_reset_does_not_flag_the_device`, `tests/test_reset.py::test_idle_reset_proceeds_without_a_busy_refusal`, `tests/test_reset.py::test_streaming_reset_refuses_while_a_job_is_running`, `tests/test_reset.py::test_the_health_gate_reset_is_not_blocked_by_the_busy_check` |
| Anonymous (HTTP/privsep) gate fail-closed | `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_over_a_tenant_holder`, `tests/test_reset_gate.py::TestResetGateAnonymous::test_anonymous_denied_when_scan_incomplete`, `tests/test_reset.py::test_privsep_http_reset_refuses_over_a_foreign_holder`, `tests/test_reset.py::test_non_privsep_http_reset_keeps_the_legacy_skip` |
| Holder scan mechanics (deleted fd, permission gap) | `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_detects_self_holding_a_device_node`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_deleted_node_fd_is_not_a_holder`, `tests/test_device_holders.py::TestEnumerateDeviceHolders::test_permission_denied_marks_scan_incomplete` |
| I6 driver record: a free device scans complete without privilege | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_free_device_scans_complete_without_privilege` |
| I6 driver record: holders attributed, deduped across devices, junk ignored | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_driver_named_holder_is_attributed_to_its_uid`, `::test_a_process_holding_two_devices_is_listed_once`, `::test_junk_lines_are_ignored` |
| I6 driver record fails closed: unattributable pid, foreign pid namespace, unreadable file | `tests/test_device_holders.py::TestDriverHolderRecord::test_a_pid_we_cannot_see_is_unattributed_not_absent`, `::test_a_pid_that_holds_no_device_here_is_not_attributed`, `::test_an_unreadable_device_file_marks_the_scan_incomplete` |
| I6 driver record absent or wholly unreadable falls back to the walk | `tests/test_device_holders.py::TestDriverHolderRecord::test_an_absent_driver_dir_falls_back_to_the_walk`, `::test_a_driver_dir_with_no_devices_falls_back_to_the_walk`, `::test_wholly_unreadable_records_fall_back_rather_than_report_blind` |
| I6 driver record against real hardware, unprivileged | `tests/test_device_hardware.py::test_the_driver_holder_record_answers_completely_without_root`, `::test_a_free_device_passes_the_reset_gate_without_root` |
| I7 tenant blocks automatic action | `tests/test_device_safety.py::test_governor_never_reboots_over_a_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_under_a_foreign_tenant`, `tests/test_device_safety.py::test_stuck_hold_escalation_holds_when_the_holder_scan_is_incomplete`, `tests/test_device_safety.py::test_a_tenant_arriving_before_the_reboot_decision_blocks_it` |
| I7 the forced climb's tray rung reads the forced guard: an unreadable scan does not skip it, a tenant or running job does | `tests/test_ubb_tray_map.py::test_the_forced_tray_rung_does_not_read_an_unreadable_scan_as_a_tenant`, `::test_the_forced_tray_rung_still_holds_for_a_real_tenant_or_a_running_job`, `::test_the_hold_deadline_climb_walks_the_tray_on_an_unreadable_scan_not_glx_reset`, `::test_a_forced_tray_walk_whose_fabric_check_fails_keeps_the_hold` |
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
| I9 ledger durable-before-fire, abort on unwritable, fail-closed read, retract | `tests/test_device_safety.py::test_auto_reboot_records_durably_before_it_fires`, `tests/test_device_safety.py::test_auto_reboot_aborts_when_the_ledger_cannot_be_written`, `tests/test_device_safety.py::test_governor_fails_closed_when_the_ledger_is_unreadable`, `tests/test_device_safety.py::test_governor_blocks_a_reboot_inside_the_min_interval`, `tests/test_device_safety.py::test_record_auto_recovery_round_trips_through_the_durable_ledger`, `tests/test_device_safety.py::test_ledger_skips_a_corrupt_line_without_losing_the_rest`, `tests/test_device_safety.py::test_a_power_cycle_that_fails_to_launch_retracts_its_ledger_entry` |
| I9 a power cycle a later boot verified healthy does not gate the next; an unverified one still does; the backstop refuses and pages at the cap, for a stated time asked again at its end; a NaN, infinite or future stamp never feeds it; a host boot this boot verified does not gate | `tests/test_power_cycle_verified_interval.py::test_a_verified_cycle_does_not_gate_a_new_drop`, `::test_an_unverified_cycle_inside_the_interval_is_still_refused`, `::test_a_stamp_that_cannot_prove_a_later_boot_keeps_the_guard`, `::test_the_backstop_refuses_and_pages_once_at_the_cap`, `::test_the_backstop_is_a_stated_bounded_wait_retried_at_its_end`, `::test_a_corrupt_cycle_stamp_never_feeds_the_backstop`, `::test_a_host_boot_this_boot_verified_does_not_gate_a_new_drop`, `::test_the_gate_stamps_only_a_release_whose_fabric_passed`, `::test_a_relift_stamps_only_on_a_passing_fabric_check` |
| I9 G8 replay: a new episode after a verified cycle takes one power cycle at once; a host death is never a cycle; the cycle it takes still gates until verified | `tests/test_power_cycle_new_episode_g8.py::test_g8_a_cycle_a_later_boot_verified_does_not_hold_off_the_next_episodes_cycle`, `::test_g8_a_host_death_is_never_counted_as_a_cycle`, `::test_g8_the_loop_bound_stands_for_an_unverified_cycle`, `::test_g8_the_ladder_takes_the_power_cycle_instead_of_holding_for_the_interval` |
| I10 per-severity interval | `tests/test_device_safety.py::test_power_cycle_after_a_recent_reboot_waits_out_the_cooldown`, `tests/test_device_safety.py::test_power_cycle_does_not_escalate_past_a_recent_power_cycle`, `tests/test_device_safety.py::test_reboot_does_not_de_escalate_past_a_recent_power_cycle` |
| I10 a power cycle refused by its cooldown (or also by the per-boot cap) is retried as that wait ends, once per refusal, and releases only after a passing fabric check | `tests/test_power_cycle_interval_retry.py::test_a_cycle_refused_by_its_interval_is_due_again_at_expiry`, `::test_the_interval_is_still_enforced`, `::test_no_retry_for_a_reboot_or_outside_the_refused_episode`, `::test_a_cycle_the_per_boot_cap_also_refuses_is_still_asked_again_at_its_end`, `::test_the_watchdog_forces_one_pass_at_expiry_not_at_the_next_window`, `::test_the_retried_pass_releases_only_after_a_passing_fabric_check` |
| I10 a verified cycle does not block the reboot; the reboot keeps its own guards; past the backstop it blocks again | `tests/test_power_cycle_verified_interval.py::test_a_verified_cycle_does_not_block_the_reboot_rung`, `::test_a_reboot_after_a_verified_cycle_keeps_its_own_guards_and_hands_off_to_the_cycle`, `::test_past_the_backstop_a_verified_cycle_gates_the_reboot_again`, `::test_a_stamp_that_cannot_prove_a_later_boot_keeps_the_guard` |
| I10 a reboot counts as tried for its whole episode: after two unhealed cycles and a failed reboot the power cycle fires at the cooldown's end, not a second reboot; a reboot before the last verified release does not count; with no usable verified release it counts for a bounded time; a this-boot or unreadable record does not count; a release verified on this boot ends the episode; a cycle that cannot launch gets one fallback reboot per episode, a timed-out launch none | `tests/test_reboot_episode.py::test_after_two_unhealed_cycles_a_failed_reboot_climbs_to_the_cycle_at_the_cooldown_end`, `::test_a_reboot_before_the_last_verified_release_does_not_count`, `::test_with_no_usable_verified_release_a_reboot_counts_for_a_bounded_time`, `::test_this_boot_and_unreadable_reboot_records_still_do_not_count`, `::test_a_release_verified_on_this_boot_ends_the_episode_whatever_the_stamps`, `::test_a_power_cycle_that_cannot_launch_gets_one_fallback_reboot_per_episode`, `::test_a_cycle_launch_that_timed_out_gives_no_fallback_reboot`, `tests/test_device_safety.py::test_reboot_already_attempted_ignores_this_boot_and_stale_records` |
| I11 host rung only after exhausted reset (two strikes); once-per-episode latch | `tests/test_device_safety.py::test_cascade_router_escalates_to_the_host_rung_only_once_reset_is_exhausted`, `tests/test_device_safety.py::test_stuck_hold_escalation_retries_on_the_grace_cadence`, `tests/test_device_safety.py::test_ubb_tray_reset_fires_at_most_once_per_hold_episode`, `tests/test_device_safety.py::test_escalate_forced_suffix_bypasses_the_retry_pacing`, `tests/test_arc_not_ready.py::test_the_retry_is_bounded_to_one_and_a_second_failure_keeps_the_hold`, `tests/test_arc_not_ready.py::test_no_retry_outside_the_bus_back_arc_not_ready_idle_shape`, `tests/test_post_reset_gate.py::test_the_arc_not_ready_retry_reset`, `tests/test_post_reset_gate.py::test_the_arc_not_ready_retry_never_releases_on_fewer_links_than_the_cabling`, `tests/test_arc_not_ready.py::test_a_glx_reset_leaving_arc_not_ready_gets_one_more_reset_then_the_full_check`, `tests/test_arc_not_ready.py::test_a_retry_whose_fabric_fails_is_not_a_recovery` |
| I12 quiesce + in-flight flag + restore rules | `tests/test_reset.py::test_streaming_reset_quiesces_pollers_and_flags_in_flight`, `tests/test_reset.py::test_reset_quiesce_restores_pollers_even_if_the_rescan_is_cancelled`, `tests/test_reset.py::test_reset_quiesce_leaves_pollers_off_when_the_reset_times_out`, `tests/test_reset.py::test_reset_quiesce_restores_pollers_when_a_failed_launch_leaves_no_scope`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_defers_the_dead_chip_sampler_during_the_transient_drop`, `tests/test_device_safety.py::test_no_clock_cap_keeper_in_the_default_pollers`, `tests/test_device_safety.py::test_the_specs_name_the_default_pollers`, `tests/test_device_safety.py::test_pollers_quiesce_skips_a_default_poller_the_host_does_not_have`, `tests/test_device_safety.py::test_pollers_quiesce_stops_and_restarts_an_opted_in_fmax_cap`, `tests/test_startup_fabric_hugepages.py::test_the_startup_fabric_check_runs_with_the_pollers_stopped`, `tests/test_startup_fabric_hugepages.py::test_the_startup_quiesce_restarts_the_pollers_when_the_gate_raises`, `tests/test_startup_fabric_hugepages.py::test_a_reset_inside_the_startup_check_defers_its_poller_restart`, `tests/test_startup_fabric_hugepages.py::test_the_startup_quiesce_leaves_the_pollers_off_while_a_reset_cycles`, `tests/test_device_safety.py::test_a_failed_reset_on_a_held_mesh_leaves_the_pollers_stopped`, `tests/test_device_safety.py::test_deferred_pollers_start_once_the_device_is_verified_healthy`, `tests/test_device_safety.py::test_the_startup_quiesce_keeps_the_pollers_stopped_when_the_gate_holds`, `tests/test_device_safety.py::test_a_dead_chip_stop_owes_the_pollers_a_restart_on_the_next_close`, `tests/test_device_safety.py::test_owing_the_pollers_inside_a_wider_quiesce_leaves_their_restart_to_its_owner`, `tests/test_device_safety.py::test_a_wider_quiesce_left_stopped_while_a_reset_cycles_owes_the_pollers`, `tests/test_device_safety.py::test_owing_the_pollers_outside_a_quiesce_starts_them_on_the_next_close`, `tests/test_device_safety.py::test_owing_the_pollers_with_no_running_loop_does_not_raise`, `tests/test_no_duplicate_defs.py::test_no_module_defines_the_same_name_twice`, `tests/test_post_reset_gate.py::test_an_operator_reset_starts_the_pollers_only_once_its_fabric_verify_passes`, `tests/test_post_reset_gate.py::test_an_operator_reset_whose_fabric_stays_unverified_keeps_the_pollers_stopped`, `tests/test_device_safety.py::test_pollers_restore_leaves_a_poller_stopped_that_was_stopped_before`, `tests/test_device_safety.py::test_pollers_restore_starts_nothing_when_every_poller_was_already_stopped`, `tests/test_device_safety.py::test_pollers_restore_covers_a_poller_an_earlier_stop_took_down`, `tests/test_device_safety.py::test_pollers_restore_counts_a_crash_looping_poller_as_running`, `tests/test_device_safety.py::test_pollers_restore_covers_a_poller_whose_stop_outlasts_its_bound`, `tests/test_device_safety.py::test_pollers_restore_covers_a_poller_whose_stop_fails`, `tests/test_device_safety.py::test_pollers_restore_counts_a_poller_whose_state_cannot_be_read_as_running`, `tests/test_device_safety.py::test_pollers_restore_retries_a_start_that_failed`, `tests/test_device_safety.py::test_pollers_restore_record_survives_a_broker_restart` |
| I13 mesh reset verify = fabric pass (pass/fail/77), single chip and no-fabric-check light | `tests/test_reset.py::test_an_operator_reset_on_a_mesh_releases_only_on_a_fabric_pass`, `tests/test_reset.py::test_an_operator_reset_whose_fabric_fails_is_not_released`, `tests/test_reset.py::test_an_operator_reset_whose_fabric_cannot_verify_holds_fabric_unverified`, `tests/test_reset.py::test_a_single_chip_operator_reset_keeps_the_light_verify`, `tests/test_reset.py::test_a_mesh_with_no_fabric_check_keeps_the_light_verify`, `tests/test_reset.py::test_the_light_verify_checks_the_hosts_chip_count_not_the_survivors`, `tests/test_reset.py::test_a_blind_stream_verify_does_not_clear_the_reported_fault`, `tests/test_reset.py::test_the_stream_settles_an_operator_reset_like_the_tool` |
| I13 failed operator-reset verify is `operator_reset_unhealthy`; duration bound documented | `tests/test_reset.py::test_an_operator_reset_whose_fabric_fails_is_not_released`, `tests/test_reset.py::test_the_stream_settles_an_operator_reset_like_the_tool`, `tests/test_reset.py::test_the_reset_tools_documented_duration_matches_its_timeouts` |
| I13 light verify; unhealthy downgrade | `tests/test_reset.py::test_reset_tool_reports_health`, `tests/test_reset.py::test_verify_health_fails_on_short_chip_count`, `tests/test_reset.py::test_verify_health_fails_on_wedged_arc`, `tests/test_reset.py::test_verify_health_passes_on_over_count_from_stale_expected` |
| I20 the AICLK ceiling is re-applied after the rescan, before the pollers return, re-read within a bounded settle window | `tests/test_aiclk_ceiling.py::test_reset_reapplies_after_the_rescan_before_the_pollers_return`, `tests/test_aiclk_ceiling.py::test_reset_failed_rung_leaves_the_ceiling_owed`, `tests/test_aiclk_ceiling.py::test_reset_apply_error_never_breaks_the_rung`, `tests/test_aiclk_ceiling.py::test_reset_unconfigured_runs_no_ceiling`, `tests/test_aiclk_ceiling.py::test_reset_reapply_reads_back_within_the_settle_window`, `tests/test_aiclk_ceiling.py::test_post_reset_readback_that_settles_in_the_window_is_verified`, `tests/test_aiclk_ceiling.py::test_post_reset_readback_high_for_the_whole_window_is_unverified_once`, `tests/test_aiclk_ceiling.py::test_post_reset_settle_wait_is_bounded`, `tests/test_aiclk_ceiling.py::test_settle_never_reruns_a_verified_timed_out_or_unsupported_helper`, `tests/test_aiclk_ceiling.py::test_without_settle_a_high_readback_is_reported_at_once`, `tests/test_aiclk_ceiling.py::test_settle_window_config`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_realistic_run_is_well_inside_the_budget`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_hung_first_run_stops_at_the_helper_timeout`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_retry_hung_near_the_window_end_stays_in_budget`, `tests/test_aiclk_ceiling.py::test_post_reset_apply_worst_case_is_under_the_budget` |
| I21 every reset is refused over a reaped job's leftover, forced or not, through `reset_with_quiesce` too; it lifts once the holder is gone | `tests/test_reaped_leftover.py::test_the_reset_tool_is_refused_even_with_force`, `tests/test_reaped_leftover.py::test_the_streaming_reset_is_refused_even_with_force`, `tests/test_reaped_leftover.py::test_reset_with_quiesce_is_refused_through_the_fence`, `tests/test_reaped_leftover.py::test_the_broker_wires_the_fence_into_the_recovery_mechanism` |
| I70 an unkillable leftover asks for no host rung before its 600 s budget; past it, the reboot with every chip present, the power cycle with a chip off the bus | `tests/test_unkillable_leftover_escalation.py::test_no_rung_before_the_budget`, `tests/test_unkillable_leftover_escalation.py::test_a_reboot_once_past_the_budget_with_every_chip_present`, `tests/test_unkillable_leftover_escalation.py::test_a_power_cycle_with_chips_off_the_bus` |
| I70 the clock opens on an unkillable leftover only, closes when none is left (a leftover gone at the deadline fires nothing), and a scan error leaves it as it is; the idle-holder scan keeps it | `tests/test_unkillable_leftover_escalation.py::test_a_killable_leftover_starts_no_clock`, `tests/test_unkillable_leftover_escalation.py::test_a_leftover_gone_at_the_deadline_fires_nothing_and_closes_the_episode`, `tests/test_unkillable_leftover_escalation.py::test_a_scan_error_leaves_the_clock_as_it_is`, `tests/test_unkillable_leftover_escalation.py::test_the_idle_holder_scan_keeps_the_clock` |
| I70 guards: a governor/cooldown denial holds and the next try fires; the operator stand-back holds the power cycle and a later try fires it once the marker is gone; a pending host action or a device op asks nothing; another tenant or a running job blocks, the leftover itself does not; a leftover that leaves while the try waits on the lock, or a leftover leaving, a tenant arriving or a reset scope starting during the stand-back or governor check, fires nothing (`gone` closes the episode); no host rung opted in journals `no_host_rung` | `tests/test_unkillable_leftover_escalation.py::test_a_governor_denial_holds_then_fires_on_a_later_try`, `tests/test_unkillable_leftover_escalation.py::test_a_held_stage_is_journaled_once_while_it_stands`, `tests/test_unkillable_leftover_escalation.py::test_the_operator_stand_back_holds_the_power_cycle_until_it_is_gone`, `tests/test_unkillable_leftover_escalation.py::test_a_leftover_that_leaves_while_the_try_waits_on_the_lock_fires_nothing`, `tests/test_unkillable_leftover_escalation.py::test_a_change_during_the_last_checks_holds_the_rung`, `tests/test_unkillable_leftover_escalation.py::test_a_pending_host_action_asks_for_nothing`, `tests/test_unkillable_leftover_escalation.py::test_a_device_op_in_flight_defers_the_try`, `tests/test_unkillable_leftover_escalation.py::test_another_tenant_blocks_the_rung_but_the_leftover_itself_does_not`, `tests/test_unkillable_leftover_escalation.py::test_a_running_job_blocks_the_rung`, `tests/test_unkillable_leftover_escalation.py::test_no_host_rung_opted_in_journals_no_host_rung` |
| I70/I76 a killed job's leftover past 600 s and the 10-min hold bound request one power cycle per episode, in either order, with a leftover try spawned before the bound's request, and whether the launch ran or timed out; the device stays held with the full check owed | `tests/test_unkillable_leftover_escalation.py::test_replay_the_bound_and_the_leftover_budget_request_one_power_cycle` |
| I70 `/health` `leftover_fence`, the sampler wiring, and the bounded time to request (at most 660 s) with nothing on the path releasing the device | `tests/test_unkillable_leftover_escalation.py::test_the_health_fields`, `tests/test_unkillable_leftover_escalation.py::test_the_sampler_drives_the_escalation`, `tests/test_unkillable_leftover_escalation.py::test_time_to_request_is_bounded_and_nothing_releases_the_device` |
| I14 warm reboot never fired where futile; blocked climbs are loud | `tests/test_device_safety.py::test_host_escalation_for_drop_sends_all_off_bus_to_the_cold_rung`, `tests/test_device_safety.py::test_host_escalation_for_drop_routes_a_futile_reboot_to_the_cold_rung`, `tests/test_device_safety.py::test_gate_all_off_bus_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_reset_regression_holds_loudly_never_reboots`, `tests/test_device_safety.py::test_gate_all_off_bus_power_cycles_when_opted_in` |
| Cold rung fireable-or-off (ipmitool) | `tests/test_device_safety.py::test_a_host_without_ipmitool_serves_with_the_cold_rung_off`, `tests/test_device_safety.py::test_ipmitool_present_leaves_the_cold_rung_armed` |
| I26 pre-power-cycle hook: opt-in, runs between record and fire, bounded, never gates the cycle | `tests/test_pre_power_cycle_hook.py::test_unset_hook_runs_nothing_and_the_cycle_is_unchanged`, `tests/test_pre_power_cycle_hook.py::test_the_hook_runs_after_the_record_and_before_the_fire`, `tests/test_pre_power_cycle_hook.py::test_a_failing_hook_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_a_hook_that_cannot_run_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_a_hung_hook_is_killed_with_its_children_and_the_cycle_goes_ahead`, `tests/test_pre_power_cycle_hook.py::test_a_background_child_holding_output_does_not_stretch_the_wait`, `tests/test_pre_power_cycle_hook.py::test_the_hook_never_runs_when_the_ledger_aborts_the_cycle`, `tests/test_pre_power_cycle_hook.py::test_a_hook_that_raises_still_power_cycles`, `tests/test_pre_power_cycle_hook.py::test_cancelled_while_waiting_on_the_hook_retracts_the_ledger_entry`, `tests/test_pre_power_cycle_hook.py::test_the_warm_reboot_does_not_run_the_power_cycle_hook`, `tests/test_pre_power_cycle_hook.py::test_the_timeout_is_always_finite_and_positive` |
| I27 the gated off-bus hold: safe rungs first, grace, idle and nothing standing back, then one power cycle per interval with the attempt on disk first; otherwise hold or page once | `tests/test_offbus_power_cycle.py::test_a_gate_that_allows_resets_hands_back_to_the_ladder`, `tests/test_offbus_power_cycle.py::test_no_power_cycle_before_the_safe_rungs_ran`, `tests/test_offbus_power_cycle.py::test_an_unset_command_pages_once_and_the_hold_stands`, `tests/test_offbus_power_cycle.py::test_within_the_grace_it_holds`, `tests/test_offbus_power_cycle.py::test_past_the_grace_and_idle_it_fires_once_with_the_attempt_on_disk_first`, `tests/test_offbus_power_cycle.py::test_the_ladder_then_the_power_cycle_through_the_escalation`, `tests/test_offbus_power_cycle.py::test_a_tenant_or_an_unreadable_scan_holds`, `tests/test_offbus_power_cycle.py::test_a_reset_in_its_own_scope_holds`, `tests/test_offbus_power_cycle.py::test_anything_objecting_stands_back`, `tests/test_offbus_power_cycle.py::test_checks_that_say_no_let_it_fire`, `tests/test_offbus_power_cycle.py::test_an_attempt_within_the_interval_pages_instead`, `tests/test_offbus_power_cycle.py::test_an_attempt_older_than_the_interval_does_not_limit`, `tests/test_offbus_power_cycle.py::test_the_mechanism_limits_still_apply`, `tests/test_offbus_power_cycle.py::test_an_unwritable_attempt_record_never_fires`, `tests/test_offbus_power_cycle.py::test_a_failed_fire_keeps_its_error_for_the_next_page`, `tests/test_offbus_power_cycle.py::test_fire_command_raises_on_a_non_zero_exit_and_passes_the_reason`, `tests/test_offbus_power_cycle.py::test_due_for_the_watchdog_once_per_episode`, `tests/test_offbus_power_cycle.py::test_the_hold_watchdog_forces_one_pass_at_the_grace`, `tests/test_offbus_power_cycle.py::test_the_knobs_default_and_reject_nonsense`, `tests/test_offbus_power_cycle.py::test_a_corrupt_state_file_reads_empty_and_a_bad_stamp_counts_as_now` |
| I27 an attempt a later boot verified healthy does not count toward the off-bus interval; one it cannot prove healed still does | `tests/test_power_cycle_new_episode_g8.py::test_g8_the_off_bus_hold_interval_skips_a_cycle_a_later_boot_verified`, `::test_last_attempt_within_skips_only_what_healed_says` |
| I27 bounded: a blocked cycle pages once (high severity) past grace + the bound; a page the hook drops is retried | `tests/test_offbus_power_cycle.py::test_a_blocked_power_cycle_pages_once_past_its_bound`, `tests/test_offbus_power_cycle.py::test_a_page_the_alert_hook_drops_is_sent_again`, `tests/test_offbus_power_cycle.py::test_the_blocked_page_bound_defaults_and_rejects_nonsense` |
| I27 a failed BMC IPMI hands off to the power cycle at its bound, after the ladder ran once (also when the ladder's own tray rung failed it); only `BmcIpmiError` starts the clock | `tests/test_offbus_power_cycle.py::test_a_failed_bmc_ipmi_hands_off_past_its_bound`, `tests/test_offbus_power_cycle.py::test_within_the_ipmi_bound_it_holds_and_the_bound_is_configurable`, `tests/test_offbus_power_cycle.py::test_a_cycle_in_progress_stands_the_ipmi_hand_off_back`, `tests/test_offbus_power_cycle.py::test_the_whole_box_is_re_read_before_the_cycle`, `tests/test_offbus_power_cycle.py::test_only_a_bmc_ipmi_failure_starts_the_clock`, `tests/test_offbus_power_cycle.py::test_a_tray_sweep_the_bmc_failed_starts_the_clock`, `tests/test_offbus_power_cycle.py::test_the_watchdog_forces_a_pass_at_the_ipmi_bound`, `tests/test_offbus_power_cycle.py::test_the_ipmi_bound_defaults_and_rejects_nonsense`, `tests/test_offbus_power_cycle.py::test_the_ladder_noted_before_the_ipmi_failure_still_counts`, `tests/test_offbus_power_cycle.py::test_the_escalations_own_tray_rung_failing_over_ipmi_hands_off_at_the_bound` |
| I27 a failed or regressed tray walk runs no below-floor mesh reset; it holds and hands off to the power cycle at its bound (or climbs without the sweep when the power cycle cannot fire), never releasing the device | `tests/test_offbus_power_cycle.py::test_a_regressed_tray_walk_hands_off_to_the_power_cycle_not_the_mesh_reset`, `tests/test_offbus_power_cycle.py::test_a_walk_that_did_not_recover_skips_the_mesh_reset_on_every_later_pass`, `tests/test_offbus_power_cycle.py::test_with_no_power_cycle_command_a_failed_walk_climbs_without_the_sweep`, `tests/test_offbus_power_cycle.py::test_the_watchdog_forces_a_pass_at_the_tray_walk_bound`, `tests/test_offbus_power_cycle.py::test_the_tray_walk_bound_defaults_and_rejects_nonsense`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_falls_through_to_the_hold_when_the_affected_tray_stays_off` |
| I27 the manual-recovery guard fails closed: no check configured never fires (pages once), the installed template is the default, a check that is unreadable, raises or times out stands back, a clean check lets it fire, and the tenant is re-read after the checks | `tests/test_offbus_power_cycle.py::test_no_manual_recovery_check_never_fires_and_pages_once`, `tests/test_offbus_power_cycle.py::test_the_installed_check_is_the_default`, `tests/test_offbus_power_cycle.py::test_a_manual_recovery_check_that_cannot_answer_holds_until_the_bound`, `tests/test_offbus_power_cycle.py::test_checks_that_say_no_let_it_fire`, `tests/test_offbus_power_cycle.py::test_a_tenant_arriving_during_the_checks_holds`, `tests/test_offbus_power_cycle.py::test_the_pages_name_the_host` |
| I27 the manual-recovery check template: operator power actions count, read-only BMC queries do not, an unreadable journal or auth.log cannot answer | `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_template`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_on_an_unreadable_journal`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_when_auth_log_fails_to_read`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_never_globs_an_auth_log_line`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_cannot_answer_on_an_unreadable_auth_log`, `tests/test_offbus_power_cycle.py::test_the_manual_recovery_check_reads_auth_log_within_the_window` |
| I28 an outage hold or `TT_DEVICE_MCP_AUTO_MESH_RESET=0` holds the gate, relift, watchdog and sweep mesh reset, also with a chip off the bus in the startup and pre-job gates, and pages again while it stands; one rung off alone, or `=1`, still resets | `tests/test_mesh_reset_hold.py::test_the_hold_reason_follows_the_flags`, `::test_the_gate_holds_the_mesh_reset_under_an_outage_hold_and_pages_once`, `::test_the_gate_holds_with_the_explicit_knob_alone`, `::test_the_gate_still_resets_with_only_the_tray_rung_off`, `::test_an_explicit_opt_in_re_arms_the_mesh_reset_under_an_outage_hold`, `::test_the_last_chance_sweep_is_refused_under_an_outage_hold`, `::test_a_dropped_page_is_sent_again_and_a_lifted_hold_re_arms_it`, `::test_the_gate_holds_with_a_chip_off_the_bus_under_an_outage_hold`, `::test_without_the_hold_a_chip_off_the_bus_still_gets_the_mesh_reset`, `::test_a_standing_hold_pages_again_after_the_repage_interval`, `::test_the_repage_knob_defaults_and_rejects_nonsense` |
| I29 one unverified reset per 77-only episode; next escalation sweeps once, strict on 77, climbs on a measured failure; a sweep still reading 77 holds and pages once for a known no-reset cause, else climbs to the host rung once per episode (ledger-latched across the boot) and then hands off to an operator with a deadline, never releasing | `tests/test_device_safety.py::test_a_reset_that_leaves_the_fabric_unverified_counts_toward_the_relift_bound`, `tests/test_device_safety.py::test_a_second_escalation_after_an_unverified_reset_sweeps_instead_of_resetting`, `tests/test_device_safety.py::test_an_incomplete_sweep_after_an_unverified_reset_does_not_climb`, `tests/test_device_safety.py::test_a_sweep_verify_that_reads_77_is_not_a_release`, `tests/test_device_safety.py::test_a_sweep_that_still_reads_77_holds_and_pages_once_instead_of_climbing`, `tests/test_device_safety.py::test_a_sweep_still_reading_77_with_no_known_cause_climbs_to_the_host_rung_once`, `tests/test_device_safety.py::test_a_77_host_rung_that_could_not_run_hands_off_and_retries`, `tests/test_device_safety.py::test_a_boot_the_77_host_rung_brought_up_hands_off_instead_of_climbing_again`, `tests/test_device_safety.py::test_the_77_host_rung_fires_the_reboot_through_the_climbs_guards`, `tests/test_device_safety.py::test_an_owed_pass_77_reaches_the_host_rung_after_the_sweep_not_at_the_ceiling`, `tests/test_device_safety.py::test_a_77_host_rung_that_cannot_run_pages_once_and_a_later_rung_clears_the_hand_off`, `tests/test_device_safety.py::test_the_host_rung_climb_reports_fired_deferred_or_cannot_run`, `tests/test_startup_fabric_hugepages.py::test_the_first_job_hardware_fault_is_no_77_no_reset_cause`, `tests/test_startup_fabric_hugepages.py::test_a_latched_hold_whose_77_names_the_mismatch_is_no_77_no_reset_cause`, `tests/test_device_safety.py::test_a_latched_hold_whose_sweep_still_reads_a_mismatch_77_climbs_to_the_host_rung` |
| I79 an owed-pass 77 climbs at the sweep's end, not at the ceiling; the I40 bound is reached and the I48 backoff uses its first step; a first-time 77 keeps both; a present mesh's failed reset sweeps then climbs; a regressed off-bus hold sweeps then climbs before the ceiling; a 77 reboot's boot takes the cold rung once and a power cycle's boot hands off; never released without a passing full check | `tests/test_device_safety.py::test_an_owed_pass_77_reaches_the_host_rung_after_the_sweep_not_at_the_ceiling`, `tests/test_device_safety.py::test_an_owed_77_hold_skips_the_i40_retries_and_the_i48_backoff_and_a_first_time_77_keeps_them`, `tests/test_device_safety.py::test_present_mesh_failed_reset_sweeps_then_climbs_without_the_ceiling_wait`, `tests/test_device_safety.py::test_present_mesh_failed_reset_whose_sweep_recovers_does_not_climb`, `tests/test_device_safety.py::test_a_regressed_offbus_hold_sweeps_then_power_cycles_before_the_ceiling`, `tests/test_device_safety.py::test_a_regressed_offbus_hold_whose_sweep_recovers_does_not_power_cycle`, `tests/test_device_safety.py::test_a_boot_the_77_reboot_brought_up_climbs_cold_once_and_a_power_cycle_boot_hands_off` |
| I79 the 77 reboot's boot: the I40 fallback marks the episode before it escalates, so the ladder sweeps and takes one power cycle, no reset and no reboot; a cooldown-denied cold rung is retried on each re-check, pages no one, shows its deadline on /health, fires one power cycle once allowed and never again; a cold rung denied for any other reason pages once naming the broker as owner | `tests/test_device_safety.py::test_the_77_bound_fallback_on_the_77_reboots_boot_marks_the_episode_and_takes_the_cold_rung`, `tests/test_device_safety.py::test_a_cooldown_denied_cold_rung_is_retried_on_each_re_check_and_fires_one_power_cycle`, `tests/test_device_safety.py::test_a_cold_rung_denied_for_a_reason_other_than_the_cooldown_pages_once` |
| I79 a 77 climb the I65 cooldown denied below the bound fires at the cooldown's end through the watchdog retry; a 77 hold whose reset has not run is still never forced below the bound; relift and watchdog together ask for one power cycle | `tests/test_hold_bound.py::test_a_77_climb_the_cooldown_denied_below_the_bound_fires_at_the_cooldowns_end`, `::test_a_77_hold_whose_reset_has_not_run_is_still_never_forced_below_the_bound`, `::test_t2_a_cooldown_denied_77_climb_fires_one_power_cycle_whichever_path_runs_first` |
| I79 with I76: a 77 climb that fired before the bound is not driven again at the bound; a 77 on the boot a 77 power cycle brought up is handed to an operator and still gets the next power cycle at the bound | `tests/test_hold_bound.py::test_t1_a_77_climb_that_fired_before_the_bound_is_not_driven_again_at_the_bound`, `::test_t3_a_77_on_the_boot_a_77_power_cycle_brought_up_still_gets_the_next_cycle_at_the_bound` |
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
| I36 a changed off-bus set, a new episode or a refused (never run) reset leaves the reset open | `tests/test_no_repeat_failed_rung.py::test_a_changed_off_bus_set_lets_the_reset_run_again`, `::test_a_new_hold_episode_resets_again`, `::test_a_refused_reset_is_not_recorded` |
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
| I37/I25 the tray rung with no map runs the presence read: a whole-tray drop (EIO survivors included) is declined whole-tray and handed to the power cycle; a partial drop keeps the ladder | `tests/test_classac_power_cycle_direct.py::test_the_tray_rung_with_no_map_hands_a_whole_tray_to_the_power_cycle`, `::test_the_tray_rung_with_no_map_and_eio_survivors_hands_off_too`, `::test_the_tray_rung_with_no_map_keeps_a_partial_drop_on_the_ladder` |
| I38 partial off-bus (one chip, four chips): the power cycle fires right after the one failed reset, 252 s vs 924 s and 383 s vs 1186 s from onset; a denied cycle is asked again the next window with no reset between | `tests/test_no_repeat_failed_rung.py::test_partial_offbus_power_cycles_right_after_the_failed_reset`, `::test_a_denied_power_cycle_after_the_failed_reset_is_retried_next_window` |
| I38 with I65: a partial off-bus whose reset failed within the cooldown after a host boot skips the ceiling wait and the sweep, holds with the cooldown deadline set and no second reset, shows that deadline on /health (`power_cycle_cooldown.next_deadline`), is asked again by the watchdog at it, then cycles once the cooldown has run out | `tests/test_no_repeat_failed_rung.py::test_partial_offbus_after_the_failed_reset_waits_only_for_the_power_cycle_cooldown` |
| I36 / I38 for each reset class (B1 one or two chips off, B2 four, D present mesh) and host rung (power cycle, reboot, none) over six forced windows: the mesh reset runs first and once on an unchanged set, the next rung follows in that same window (or one latched operator hand-off), nothing releases the hold | `tests/test_no_repeat_failed_rung.py::test_e3_offbus_classes_reset_once_then_take_the_next_rung`, `::test_e3_present_mesh_resets_once_then_takes_the_next_rung` |
| I36 / I39 a set that shrank then stayed unchanged gets at most two resets (the one-shot and the progress retry) over six forced windows, then climbs or hands off once | `tests/test_no_repeat_failed_rung.py::test_e3_shrunk_then_unchanged_resets_at_most_twice_then_climbs` |
| I39 a failed reset that brought chips back arms one retry per episode; the watchdog spawns it when due, before the ceiling, and never twice | `tests/test_no_repeat_failed_rung.py::test_progress_retry_is_armed_once_per_episode_and_comes_due`, `::test_the_watchdog_spawns_the_due_progress_retry_before_the_ceiling` |
| I39 end to end: a failed reset that shrank the set arms the retry, the watchdog spawns it, it resets the changed set once and an unchanged set after it power-cycles (I38); an unchanged first set arms none; the retry releases only after the full verify, fabric included | `tests/test_no_repeat_failed_rung.py::test_a_failed_reset_that_shrank_the_set_retries_once_on_the_new_set_then_climbs`, `::test_a_failed_reset_on_an_unchanged_set_arms_no_retry_and_climbs_at_once`, `::test_the_progress_retry_releases_only_after_the_full_fabric_verify` |
| I39 a failed reset that left every chip back arms the retry at once and the watchdog's next sample spawns it; chips still off keep the 60 s wait; B1 first reset fails with every chip back, the second heals inside 600 s, verified (fabric) before release; the 600 s ceiling window still fires after the spent retry | `tests/test_no_repeat_failed_rung.py::test_a_failed_reset_that_left_every_chip_back_arms_the_retry_at_once`, `::test_a_failed_reset_that_left_chips_off_keeps_the_60s_wait`, `::test_b1_first_reset_fails_with_every_chip_back_second_heals_inside_ten_minutes`, `::test_b1_all_back_retry_whose_fabric_verify_fails_keeps_the_hold_for_the_ladder`, `::test_the_ten_minute_ceiling_window_still_fires_after_the_spent_retry` |
| I81 B1 replay: an isolated chip's tray is re-powered, the rescan at tt-smi's driver wait re-adds it, one mesh reset heals it inside the 6 min budget and the full verify, fabric included, runs before release | `tests/test_glx_reset_isolated_rescan.py::test_b1_replay_one_mesh_reset_heals_an_isolated_chip_inside_the_budget` |
| I81 the rescan fires once at the driver-wait marker (one line, split read, split inside the count) and the caller still sees every output chunk; never with no isolated chip or off a mesh reset; a failed rescan is journaled and the reset runs on; a cancelled reset cancels a pending rescan and output read after the cancel starts none; a cancel while the rescan finishes still clears the in-flight flag, rescans, drops returned chips and restores the pollers before it reaches the caller | `tests/test_glx_reset_isolated_rescan.py::test_the_rescan_fires_once_at_the_driver_wait_and_the_caller_still_sees_the_output`, `::test_no_early_rescan_without_an_isolated_chip_or_off_a_mesh_reset`, `::test_a_failed_driver_wait_rescan_is_journaled_and_the_reset_runs_on`, `::test_a_cancelled_reset_cancels_a_pending_driver_wait_rescan`, `::test_output_after_a_cancelled_reset_starts_no_driver_wait_rescan`, `::test_a_cancel_while_the_driver_wait_rescan_finishes_still_runs_the_reset_cleanup` |
| I58 the failed-reset record survives a broker restart but not a new boot; a new episode on the same set does not reset again; only a launched reset is recorded | `tests/test_no_repeat_failed_rung.py::test_the_failed_reset_survives_a_broker_restart_but_not_a_new_boot`, `::test_a_new_hold_episode_on_the_same_set_does_not_reset_again`, `::test_a_reset_that_never_launched_is_not_recorded` |
| I59 a gap or holder left by the broker's own exiting probe or killed holder is not a tenant; any other gap still fails closed; scoped holders and reused pids are never discounted | `tests/test_broker_internal_holders.py` |
| I65/I73 a boot 3 min before startup with a whole tray off: held with the deadline on /health and no rung before the 10-min mark, the full ladder started once at it, then one cycle at the deadline | `tests/test_power_cycle_cooldown.py::test_a_boot_3_min_before_startup_holds_to_a_deadline_then_cycles_once`, `::test_health_carries_the_cooldown_deadline_while_held` |
| I65 the broker's cycle then a boot with the tray still off: no second cycle before the cooldown ends, one at its end; an external cycler's history is an anchor; a future stamp is not | `tests/test_power_cycle_cooldown.py::test_after_the_broker_cycle_and_boot_no_second_cycle_before_the_cooldown_ends`, `::test_an_external_cycler_history_is_an_anchor`, `::test_a_boot_stamp_in_the_future_anchors_nothing` |
| I65 backoff 30 min, 30 min, 1 h, 2 h, 4 h cap; a verified release resets it | `tests/test_power_cycle_cooldown.py::test_the_backoff_doubles_after_two_failed_cycles_and_caps_at_4h`, `::test_cycles_that_heal_nothing_back_off_and_a_verified_release_resets_it` |
| I65 every denial ends at a stated time and is asked again then, in its own hold episode; the per-boot cap is bounded | `tests/test_power_cycle_cooldown.py::test_a_cycle_requested_this_boot_that_never_went_down_is_asked_again`, `::test_the_watchdog_retry_is_kept_to_its_own_hold_episode`, `tests/test_device_safety.py::test_governor_per_boot_cap_ends_with_the_interval`, `::test_power_cycle_after_a_recent_reboot_waits_out_the_cooldown` |
| I65 a whole tray still off hours after a cycle that never took the host down cycles at once; inside the cooldown it holds with the deadline on /health, starts the full ladder once at the hold's 10-min mark (I73) and cycles at its end; an external reboot counts toward the cooldown; the off-bus (I27) path holds with a deadline and no ladder; an operator attempt spacing is a stated wait capped at the backoff cap, default none; a whole tray seen again among partial reads still cycles | `tests/test_whole_tray_cycle_every_boot.py::test_a_restart_hours_after_a_cycle_that_never_went_down_cycles_at_once`, `::test_inside_the_cooldown_the_hold_shows_its_deadline_and_cycles_when_it_ends`, `::test_an_external_reboot_counts_toward_the_cooldown_and_the_cycle_follows_it`, `::test_the_off_bus_path_no_longer_holds_six_hours_on_an_attempt_that_never_went_down`, `::test_the_off_bus_path_inside_the_cooldown_holds_with_a_deadline_and_no_ladder`, `::test_an_operator_attempt_interval_is_a_stated_wait_capped_at_the_backoff_cap`, `::test_the_attempt_interval_default_adds_no_wait_beyond_the_cooldown`, `::test_a_whole_tray_seen_again_among_partial_reads_still_cycles_at_the_deadline` |
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
| I73 inside the cooldown the full ladder runs once 10 min after the last cycle or boot, the watchdog starts it at that mark, and the cycle follows at the cooldown's end | `tests/test_power_cycle_ac_needed.py::test_the_full_ladder_runs_10_min_after_the_last_cycle_or_boot_then_the_cycle`, `::test_the_watchdog_starts_the_class_a_ladder_at_the_10_min_mark` |
| I73/I76 at the 10-min mark a bound ladder whose full check failed keeps its power-cycle step; the class-A ladder does not replace it | `tests/test_power_cycle_ac_needed.py::test_at_the_10_min_mark_a_failed_bound_check_keeps_the_power_cycle_step` |
| I73 a returning tray leaves the AC state and is released only after the full check, fabric included; one that drops again before a verified release is cycled at the cooldown's end | `tests/test_power_cycle_ac_needed.py::test_a_returning_tray_leaves_the_ac_state`, `::test_a_returning_tray_is_released_only_after_the_full_fabric_check_passes`, `::test_a_tray_that_drops_again_before_any_verified_release_is_cycled_at_the_cooldowns_end` |
| I73 `/health` shows `ac_power_needed` with `escalation_stage` `power-cycle` and the next cycle as `next_deadline`; null on an ordinary cooldown | `tests/test_power_cycle_ac_needed.py::test_health_shows_the_ac_needed_state_and_the_next_cycle`, `::test_health_shows_no_ac_state_on_an_ordinary_cooldown` |
| I73 only broker cycles for this tray that took the host down since the last verified release count (not this boot's request, a missed one, another tray's, or the last one a much later boot followed); external cycles space the cooldown but never count; a partial off-bus never enters the AC state; each cycle row names its tray | `tests/test_power_cycle_ac_needed.py::test_only_broker_cycles_that_took_the_host_down_for_this_tray_count`, `::test_external_cycles_space_the_cooldown_but_are_never_attempts`, `::test_a_partial_off_bus_set_never_enters_the_ac_hold`, `::test_a_power_cycle_row_names_the_tray_it_was_for` |
| I65 a failed out-of-band cycle (a second line, same `at_epoch`, cause + ` (cycle failed)`) never counts and cancels its attempt line; an unmatched failure line is ignored; an attempt with no failure line still counts; host boots are unchanged | `tests/test_power_cycle_cooldown.py::test_a_matched_failure_line_drops_the_attempt`, `::test_an_unmatched_failure_line_is_ignored`, `::test_an_attempt_without_a_failure_line_still_counts` |
| I65 a future, non-finite or unreadable stamp (history file, ledger, heal file) never hides a valid past anchor, never holds the cycle past the stated gap, never keeps the streak past a verified release; every cap denial is on /health | `tests/test_power_cycle_cooldown.py::test_a_nan_or_future_history_line_does_not_hide_the_valid_past_one`, `::test_a_future_or_unreadable_ledger_record_does_not_hide_a_past_broker_cycle`, `::test_a_future_this_boot_stamp_holds_only_to_the_stated_gap_and_shows_on_health`, `::test_an_unreadable_this_boot_stamp_holds_the_cycle_to_a_fixed_end`, `::test_the_per_boot_cap_denial_shows_on_health`, `::test_a_future_record_does_not_keep_the_streak_past_a_verified_release` |
| I71 multi-chip with a chip off the bus names the power cycle over the reboot when armed; a present mesh and a single chip keep the reboot first; with no cycle armed the old verdict stands | `tests/test_offbus_reboot_futile.py::test_multi_chip_offbus_takes_the_power_cycle_over_the_reboot_when_armed`, `::test_a_present_mesh_keeps_the_reboot_first`, `::test_single_chip_keeps_the_reboot_first`, `::test_without_a_power_cycle_the_old_verdict_stands` |
| I71 with I79: a present mesh carrying the cold flag names the power cycle (or holds with none armed) and is not fallback-eligible; on the 77 reboot's boot a cooldown-denied cycle holds with no fallback reboot, then fires one power cycle | `tests/test_offbus_reboot_futile.py::test_the_i79_cold_flag_sends_a_present_mesh_to_the_power_cycle`, `::test_the_cold_rung_on_the_77_reboots_boot_never_falls_back_to_a_second_reboot` |
| I71 a partial drop past the full ladder cycles with no reboot; a present-mesh wedge reboots first with both rungs armed, and with no cycle armed | `tests/test_offbus_reboot_futile.py::test_partial_drop_past_the_ladder_cycles_without_a_reboot`, `::test_present_mesh_wedge_reboots_first_with_both_rungs_armed`, `::test_present_mesh_wedge_still_reboots_with_no_power_cycle`, `::test_next_stage_names_the_power_cycle_for_a_failed_reset_partial_drop`, `::test_next_stage_present_mesh_reboots_first_with_both_rungs_armed`, `::test_next_stage_present_mesh_with_no_power_cycle_still_reboots` |
| I71 a denied or unlaunched cycle falls back to the reboot only on a drop the reboot can recover, at the gate and on the idle climb; a futile drop and a cooldown denial on one hold; BLOCKED only with no live reset scope | `tests/test_offbus_reboot_futile.py::test_the_reboot_fallback_is_only_for_drops_a_reboot_can_recover`, `::test_a_denied_cycle_falls_back_to_the_reboot_on_a_recoverable_drop`, `::test_both_rungs_denied_holds`, `::test_a_cycle_that_does_not_launch_falls_back_to_the_reboot`, `::test_a_cycle_that_does_not_launch_on_a_futile_drop_never_reboots`, `::test_the_gate_falls_back_to_the_reboot_when_the_cycle_is_denied`, `::test_the_gate_falls_back_to_the_reboot_when_the_cycle_does_not_launch`, `::test_the_gate_cycles_a_partial_drop_when_allowed`, `::test_a_cooldown_denial_on_a_futile_drop_holds_and_never_reboots`, `::test_a_live_scope_waits_instead_of_blocking` |
| I71 the fallback reboot fires at most once per wedge: a broker reboot since the last verified release spends it at the gate and on the idle climb, denied or unlaunched; the cycle fires at the cooldown end that reboot anchored; the launch verdict the fallback reads | `tests/test_offbus_reboot_futile.py::test_reboot_fallback_spent_counts_a_reboot_since_the_last_verified_release`, `::test_a_denied_cycle_takes_no_second_fallback_reboot_on_the_idle_climb`, `::test_a_cycle_that_does_not_launch_takes_no_second_fallback_reboot`, `::test_the_gate_takes_no_second_fallback_reboot_when_the_cycle_is_denied`, `::test_the_gate_takes_no_second_fallback_reboot_when_the_cycle_does_not_launch`, `::test_an_unhealed_fallback_reboot_hands_the_next_rung_to_the_cycle_with_the_real_governor`, `::test_fire_recovery_escalation_reports_whether_the_action_may_have_launched` |
| I37/I65 class C at startup: one mesh reset with no failed reset or broker cycle; none after the broker's own cycle, which then waits only for the cooldown | `tests/test_power_cycle_cooldown.py::test_all_off_at_startup_with_no_failed_reset_or_broker_cycle_gets_one_reset`, `::test_all_off_after_the_broker_cycle_brought_this_boot_up_skips_the_reset`, `::test_a_cycle_requested_this_episode_counts_and_one_before_it_does_not` |
| I78 the driver-wait rescan repeats until every isolated chip is back and then stops; it gives up after its tries, journaled; it ends with the reset and never delays its cleanup; a cancelled reset stops it; no rescan while the settle counts; on both backends (line reader and file reader) the rescan runs after the settle and inside the driver wait | `tests/test_mesh_reset_readd.py::test_the_rescan_repeats_until_the_chip_is_back_and_then_stops`, `::test_the_repeat_gives_up_after_its_tries`, `::test_the_repeat_ends_with_the_reset_and_never_delays_its_cleanup`, `::test_a_cancelled_reset_stops_the_repeats`, `::test_no_rescan_while_the_settle_counts`, `::test_both_backends_rescan_only_once_the_settle_is_over` |
| I76 every hold kind (77-only, topology mismatch, known fabric fault, latched, generic) runs the full ladder at the 10-min bound, then the host rung; below the bound a no-reset hold keeps its fast path; a latched I42 hold escalates with its latch standing | `tests/test_hold_bound.py::test_every_hold_kind_runs_the_ladder_at_the_bound_then_the_host_rung`, `::test_below_the_bound_a_no_reset_hold_keeps_its_fast_path`, `::test_a_latched_i42_hold_still_escalates_and_its_latch_stands`, `::test_replay_a_latched_topology_mismatch_hold_gets_the_ladder_then_the_power_cycle` |
| I76 the I29 77 hold with no host rung (reset and sweep already ran this episode) gets the power cycle at the bound in the same run; a killed job's leftover holder (01 I16) does not stand the bound down: no reset runs over it and the host rung is asked for, which a live tenant still refuses | `tests/test_hold_bound.py::test_replay_a_77_held_with_no_host_rung_gets_the_power_cycle_at_the_bound`, `::test_a_killed_jobs_leftover_holder_does_not_stop_the_bound` |
| I76 a cooldown denial of the host rung arms the retry, which fires at the cooldown's end | `tests/test_hold_bound.py::test_a_cooldown_denial_arms_the_retry_and_the_host_rung_fires_at_its_end` |
| I76 a bound ladder that brings the mesh back but whose full check ends with no pass (77-only, topology mismatch / I42 latch, known fabric fault, generic) does not release and asks for the host rung in the same run; a relift check that fails after the ladder asks for it on the next tick; a check that passes asks for none | `tests/test_hold_bound.py::test_a_failed_check_after_the_bound_ladder_asks_for_the_host_rung_at_once`, `::test_a_relift_that_fails_after_the_bound_ladder_cycles_on_the_next_tick`, `::test_a_passing_check_after_the_bound_ladder_asks_for_no_host_rung` |
| I76 a check still waiting on hugepages (or hanging) after the bound ladder gets the 300 s check bound, then the host rung; another run inside that bound does not cut the check short | `tests/test_hold_bound.py::test_a_check_still_waiting_on_hugepages_is_bounded_by_the_check_window`, `::test_another_run_inside_the_check_bound_does_not_cut_the_full_check_short` |
| I76 a failed full check in any idle lift after the bound ladder (eth_frozen, off-bus floor, foreign holder) asks for the host rung on the next tick; one still waiting on hugepages asks for none | `tests/test_hold_bound.py::test_a_failed_lift_check_after_the_bound_ladder_asks_for_the_host_rung`, `::test_a_lift_check_still_waiting_on_hugepages_asks_for_no_host_rung`, `::test_a_failed_foreign_holder_check_after_the_bound_ladder_asks_for_the_host_rung` |
| I76 /health shows the bound; the env shortens it, never lengthens it; a stuck-hold event past the bound never reads `escalated` false | `tests/test_hold_bound.py::test_health_shows_the_bound`, `::test_env_shortens_the_bound_never_lengthens_it`, `::test_a_stuck_hold_event_past_the_bound_never_reads_escalated_false` |
| I76 the latch set by a fabric failure of the first job after a topology-mismatch lift: at the bound (watchdog window or tenant-hold bound) the full ladder runs, the full check with fabric still fails, the broker asks for the power cycle once, the latch stands; a tenant job parked at the gate behind that hold (holding no device fd) never dispatches, and its own tenant-hold bound refuses it with no second power cycle | `tests/test_hold_bound.py::test_replay_the_lift_then_job_fabric_failure_latch_gets_the_ladder_check_and_power_cycle`, `::test_replay_post_reset_lift_latch_with_gated_queued_job_runs_the_ladder_at_the_bound`; walked tick by tick with no idle relift reset, no rung runs below the bound, and the deadline event reads escalated: `tests/test_hold_bound.py::test_replay_a_disproven_lift_hold_gets_the_ladder_and_power_cycle_within_the_class_d_budget` |
| I76 a disproven I42 lift's bound is one sampler tick and the FORCE_ESCALATE=0 kill switch does not stop it; a hold with no disproven lift keeps the 600 s bound and its no-reset fast path; the early ladder does not count as the 10-min one, so a hold still standing at 600 s with no host action pending gets the full ladder again | `tests/test_hold_bound.py::test_a_disproven_lift_runs_the_bound_ladder_after_one_tick_whatever_the_kill_switch`, `::test_a_hold_with_no_disproven_lift_keeps_the_600s_bound_and_its_fast_path`, `::test_the_10_min_mark_reruns_the_full_ladder_after_an_early_disproven_lift_run` |
| I76 with I75: under the I42 latch the host rung the early ladder owes (its check bound ran out, or a denied rung still pending) runs below 600 s with no second ladder; `/health` names the ladder one sampler tick in, then that host rung, with or without the kill switch | `tests/test_hold_bound.py::test_the_host_rung_after_an_early_disproven_lift_ladder_runs_no_second_ladder`, `::test_health_names_a_disproven_lifts_early_ladder_then_its_host_rung` |
| I76 with spec 03 I71: a held 77 whose bound ladder's reset exits 0 but whose full check still finds one descriptor port missing (the traffic-only rerun passing) never releases and asks for exactly one power cycle, with or without the I42 latch | `tests/test_fabric_links_missing.py::test_replay_a_held_77_whose_bound_ladder_leaves_a_port_missing_power_cycles_once` |
| I76 every hold kind's escalation deadline is start + bound, or a running power-cycle cooldown's end when later; /health, the stuck-hold event and the alert read the one helper | `tests/test_hold_bound.py::test_every_hold_kind_reports_one_escalation_deadline` |
| I76 a forced run below the bound (off-bus one-shot) is not the bound's ladder: at the bound the full ladder runs again, then the host rung | `tests/test_hold_bound.py::test_a_ladder_below_the_bound_does_not_stand_in_for_the_bounds_ladder` |
| I76 a host rung or bound ladder a reset scope defers fires on the first sampler tick after the scope ends; a retry-less denial is asked again each tick and journaled once | `tests/test_hold_bound.py::test_a_host_rung_deferred_by_a_reset_scope_fires_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_a_bound_ladder_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_an_offbus_one_shot_past_the_bound_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it`, `tests/test_hold_bound.py::test_a_host_rung_denied_with_no_retry_is_asked_again_each_tick_and_journaled_once` |
| I76 a live tenant at the bound is paged once (`hold_bound_tenant_blocked`, naming it), /health shows `tenant_blocked`, and the ladder then the host rung run on the first tick after it leaves | `tests/test_hold_bound.py::test_a_live_tenant_at_the_bound_is_paged_once_and_the_ladder_runs_when_it_leaves`, `tests/test_hold_bound.py::test_a_killed_jobs_leftover_holder_does_not_stop_the_bound` |
| I82 a bound ladder's release then a failed first job (missing port, I42 latch, known fabric fault, 77-only, generic) goes to the power cycle at the next hold, not another ladder; a class change keeps the clock; a re-hold past the window with no job, or around a job started after it, is a new episode; the carry never crosses a reboot | `tests/test_hold_episode_carry.py::test_replay_a_release_after_the_bound_ladder_then_a_failed_first_job_goes_to_the_power_cycle`, `::test_a_class_change_on_the_rehold_keeps_the_clock`, `::test_a_rehold_after_the_carry_window_with_no_job_is_a_new_episode`, `::test_a_failed_first_job_keeps_the_carry_however_long_it_ran`, `::test_a_job_started_after_the_window_does_not_extend_it`, `::test_the_carry_does_not_cross_a_reboot` |
| I82 a broker restart keeps the clock: a released episode's re-hold goes to the power cycle, a hold open past its bound escalates at once, one past its bound ladder goes to the host rung | `tests/test_hold_episode_carry.py::test_replay_a_restart_whose_startup_check_releases_then_a_failed_first_job_goes_to_the_power_cycle`, `::test_a_hold_open_across_a_restart_escalates_at_once_when_past_the_bound`, `::test_a_hold_open_across_a_restart_after_its_bound_ladder_goes_to_the_host_rung`, `::test_replay_a_restart_whose_real_startup_gate_releases_keeps_the_carry` |
| I82 the carry ends when the device proves the release right: a successful first job, or a full check that passes with no hold open, gives the next fault a fresh episode and no cycle; the releasing pass keeps the carry it saves | `tests/test_hold_episode_carry.py::test_a_successful_first_job_ends_the_episode`, `::test_a_passing_full_check_with_no_hold_open_drops_the_carry`, `::test_a_passing_full_check_that_releases_a_hold_keeps_the_new_carry`, `::test_a_pass_whose_release_is_refused_keeps_the_carry` |
| I82 a tenant hold saves no carry and does not take or spend a fault hold's carry | `tests/test_hold_episode_carry.py::test_a_tenant_hold_leaves_no_carry`, `::test_a_tenant_hold_between_does_not_spend_a_fault_holds_carry` |
| I82 a tenant hold that turns into a fault hold takes a fault carry in its window, not one past it | `tests/test_hold_episode_carry.py::test_a_tenant_hold_that_turns_into_a_fault_hold_takes_the_carry_in_its_window`, `::test_a_tenant_hold_that_turns_into_a_fault_hold_past_the_window_is_a_new_episode` |
| I82 the first job's end closes the carry window even when a job-end check raises; a clean end drops the carry, a failed one keeps it | `tests/test_hold_episode_carry.py::test_the_first_jobs_end_closes_the_carry_window_even_when_a_job_end_check_raises`, `::test_the_first_jobs_verdict_still_decides_the_carry` |
| I82 on the carried path the I65 cooldown defers the host rung with no ladder and no cycle, the request fires at its end, once per episode | `tests/test_hold_episode_carry.py::test_the_carried_host_rung_waits_out_the_cooldown_and_asks_once` |
| I83 a fabric link-short hold (unlatched topology mismatch, known fabric fault) runs one ladder on its first tick and asks for the power cycle at once when the full check after it still fails or the mesh did not come back; nothing is released | `tests/test_fabric_link_short.py::test_one_ladder_at_hold_start_then_the_power_cycle_at_once` |
| I83 no later tick in the episode (the bound, a later window, the hold deadline) runs a second glx_reset or ladder or a second power-cycle request | `tests/test_fabric_link_short.py::test_no_second_glx_reset_or_ladder_in_the_episode` |
| I83 a full check that passes after the ladder asks for no power cycle; one still waiting on hugepages has until the 10-min bound, then the power cycle | `tests/test_fabric_link_short.py::test_a_full_check_that_passes_after_the_ladder_asks_for_no_power_cycle`, `::test_a_check_still_waiting_gets_the_power_cycle_by_the_bound` |
| I83 the cooldown delays the power cycle only until its end | `tests/test_fabric_link_short.py::test_the_cooldown_delays_the_cycle_only_until_its_end` |
| I83 other classes (77-only, off-bus, generic) get no ladder at hold start; an isolated chip is not this class | `tests/test_fabric_link_short.py::test_other_classes_get_no_ladder_at_hold_start`, `::test_an_offbus_chip_is_not_the_link_short_class` |
| I83 a latched mismatch is not this class: no ladder at hold start, the I42 latch kept, the ladder at the bound, the failed check asks for the power cycle, nothing released | `tests/test_fabric_link_short.py::test_a_latched_mismatch_keeps_the_latch_and_is_not_released` |
| I83 a run that found another class declines once and is not spawned again | `tests/test_fabric_link_short.py::test_an_at_start_run_that_found_another_class_is_not_spawned_again` |
| I83 the at-start pass stands in for the bound's ladder only after a granted host rung; otherwise the ladder runs at 10 min, then I76 | `tests/test_fabric_link_short.py::test_an_at_start_ladder_whose_host_rung_never_fired_does_not_stand_in_for_the_bounds_ladder`, `::test_a_granted_host_rung_after_the_at_start_ladder_runs_no_second_ladder_at_the_bound` |
| I83 a re-hold of another class after a carried at-start ladder (carry or broker restart) asks for the host rung once, no ladder per tick | `tests/test_fabric_link_short.py::test_a_rehold_of_another_class_after_a_carried_at_start_ladder_asks_for_the_host_rung_once` |
| I83 with spec 03 I75: on a startup hold the at-start ladder meets a running re-adopted job on `job_running`, stands down to the pending startup gate after the job ends, the gate's full check alone lifts; a failed gate gets the ladder then one power cycle; a below-bound host rung never fires over the running job | `tests/test_fabric_link_short.py::test_a_startup_hold_link_short_leaves_the_at_start_ladder_to_the_startup_gate`, `tests/test_fabric_link_short.py::test_a_below_bound_host_rung_never_fires_over_a_running_readopted_job` |
| I76 `TT_DEVICE_MCP_FORCE_ESCALATE=0` does not stop the bound, and the boot warning says so; a held job past the bound still starts its escalation and below it names the bound; no message claims the hold has no automatic exit | `tests/test_hold_bound.py::test_the_operator_kill_switch_does_not_stop_the_bound`, `tests/test_rung_privilege.py::test_the_force_escalate_kill_switch_warns_that_the_hold_bound_ignores_it`, `tests/test_hold_bound.py::test_a_held_job_past_its_bound_under_the_kill_switch_says_the_bound_still_runs`, `tests/test_device_safety.py::test_hold_bound_counts_from_the_episode_and_reuses_a_spent_escalation`, `tests/test_dispatch_failure_reset.py::test_floor_message_promises_escalation_only_when_armed` |
| I58 all off after a failed reset and a broker restart: the startup or a later gate cycles with no second reset; the sweep skips a failed-unchanged set | `tests/test_classac_power_cycle_direct.py::test_replay_all_off_after_a_restart_cycles_with_no_second_reset`, `::test_a_partial_drop_keeps_the_ladder` |
| I36 a power cycle that is rate-limited or not opted in never stops resets: a shrinking off-bus set resets again each forced window, and the gate retries its reset once its cooldown ran out | `tests/test_host_action_suppress.py::test_a_shrinking_offbus_set_resets_again_when_the_power_cycle_cannot_fire`, `::test_the_gate_keeps_resetting_a_drop_whose_power_cycle_is_not_opted_in` |
| Host-rung chooser order | `tests/test_device_safety.py::test_choose_escalation_prefers_reboot_then_power_cycle`, `tests/test_device_safety.py::test_choose_escalation_power_cycle_alone_goes_straight_to_it`, `tests/test_device_safety.py::test_choose_escalation_none_when_neither_opted_in` |
| Bridge reset: recovers, retries, inapplicable falls through, stale bus refused | `tests/test_device_safety.py::test_recovery_resets_through_the_bridge_and_clears_the_chip`, `tests/test_device_safety.py::test_bridge_reset_retries_before_escalating`, `tests/test_device_safety.py::test_an_inapplicable_bridge_reset_is_not_retried`, `tests/test_device_safety.py::test_an_all_inapplicable_bridge_reset_never_reads_as_recovered`, `tests/test_device_safety.py::test_a_reused_bus_refuses_the_bridge_so_a_stale_address_never_sbrs_live_silicon`, `tests/test_device_safety.py::test_bridge_reset_does_not_reach_a_gone_endpoint_by_default`, `tests/test_device_safety.py::test_a_bridge_less_chip_is_rescanned_before_anything_destructive` |
| I16 tray identity is the bus group, per architecture, never the chip index or list position | `tests/test_ubb_tray_map.py::test_a_blackhole_tray_comes_from_the_bus_group_not_the_chip_index`, `::test_each_blackhole_bus_range_maps_to_its_tray_and_bmc_bit`, `::test_a_positional_bus_list_is_refused_not_read_by_position`, `::test_a_wormhole_tray_uses_the_wormhole_table_for_the_same_buses` |
| I16 the map is all-or-nothing (unreadable bus, unknown group, non-Galaxy or unknown board) | `tests/test_ubb_tray_map.py::test_an_unreadable_bus_id_yields_no_map_rather_than_a_partial_one`, `::test_a_bus_group_outside_the_table_yields_no_map`, `::test_a_non_galaxy_board_type_yields_no_map`, `::test_an_unknown_board_type_yields_no_map` |
| I16 a missing chip never shifts another chip's tray | `tests/test_ubb_tray_map.py::test_a_missing_chip_does_not_shift_any_other_chips_tray` |
| I16 the bitmap, the affected set and the walk all key on the real tray | `tests/test_ubb_tray_map.py::test_a_whole_tray_drop_sets_the_bit_for_the_tray_that_is_down`, `::test_affected_trays_names_the_tray_an_operator_would_read_from_tt_smi`, `::test_the_walk_covers_only_the_affected_trays`, `::test_the_issue_27_walk_leads_with_the_tray_the_chip_is_on` |
| I16 no map means the rung declines, never arithmetic | `tests/test_ubb_tray_map.py::test_without_a_map_every_tray_decision_declines`, `::test_a_chip_the_map_does_not_place_declines_rather_than_resetting_the_rest`, `::test_a_broker_with_no_cached_bus_map_declines_the_walk` |
| I16 the chip→bus map comes from sysfs, is banked by the gate's own pass, and reaches the fire and the log line | `tests/test_ubb_tray_map.py::test_the_snapshot_caches_every_chips_bus_id`, `::test_the_tray_map_keys_on_the_kernels_chip_ids_not_the_snapshots_list_order`, `::test_the_walk_re_powers_the_tray_the_dropped_chips_actually_sit_on`, `::test_a_lone_off_bus_chip_24_re_powers_tray_4_first` |
| I16 sysfs and tt-smi disagreeing banks nothing and journals `chip_bus_map_mismatch` | `tests/test_ubb_tray_map.py::test_sysfs_and_tt_smi_disagreeing_banks_no_map_and_logs_both` |
| I16 a boot platform probe (`expected_count=0`), a degraded first pass, or any short snapshot never freezes the map | `tests/test_ubb_tray_map.py::test_a_partial_boot_snapshot_never_freezes_the_map`, `::test_a_degraded_first_normal_snapshot_cannot_freeze_the_map`, `tests/test_reset.py::test_bank_bus_ids_once_rejects_a_snapshot_shorter_than_the_mesh`, `::test_bank_bus_ids_once_rejects_a_boot_probe_that_did_not_ask_the_question`, `::test_bank_bus_ids_once_rejects_a_chip_enumerated_without_a_bus_id`, `::test_bank_bus_ids_once_rejects_an_empty_list`, `::test_bank_bus_ids_once_first_full_read_stands`, `::test_bank_bus_ids_once_stays_open_after_a_rejected_snapshot` |
| I16 the board type is normalized before selecting the table (WH ` L`/` R` suffix) | `tests/test_ubb_tray_map.py::test_a_wormhole_snapshot_with_l_r_suffixes_still_produces_a_tray_map`, `::test_a_wormhole_lookup_survives_the_snapshot_suffix_inside_tray_map` |
| I16 a trusted full read whose chip→bus map differs re-banks it, journals `chip_bus_map_drift` with the old and the new map, and the next mask follows the chip's new bus | `tests/test_ubb_tray_map.py::test_a_renumbered_full_read_re_banks_the_map_and_journals_old_and_new`, `::test_after_a_renumbering_the_walk_re_powers_the_tray_of_the_chips_new_bus` |
| I16 a short read, an unreadable chip, or a read tt-smi disagrees with never overwrites a banked map; a matching full read changes nothing | `tests/test_ubb_tray_map.py::test_a_short_or_disagreeing_read_never_overwrites_the_banked_map`, `::test_a_full_read_that_matches_the_banked_map_changes_nothing` |
| I16 tt-smi's bus list drifting journals `bus_map_drift`; the list stands | `tests/test_ubb_tray_map.py::test_a_drifted_snapshot_journals_but_leaves_the_cached_map_standing` |
| I16 every BMC re-power is journalled: the sweep logs one line per tray with the exact command, then `ubb_reset_fired` with mask and chips on success or `ubb_reset_failed` with `rc` on failure, from both its callers; the walk does the same per tray; a tray not fired journals no `ubb_reset_fired` | `tests/test_ubb_tray_map.py::test_the_sweep_journals_the_tray_4_mask_it_fired_for_chips_24_to_31`, `::test_a_failed_sweep_fire_journals_its_rc_and_no_fired_event`, `::test_a_sweep_that_does_not_fire_the_tray_journals_no_fired_event`, `::test_the_last_chance_sweep_labels_each_tray_from_the_map`, `::test_the_walk_journals_the_tray_4_mask_it_fired`, `::test_a_failed_walk_fire_journals_its_rc_and_no_fired_event`, `tests/test_ubb_reset_launch.py::test_a_pulse_the_bmc_did_not_take_raises_bmc_ipmi_error` |
| I16 a new GLX board type this build cannot map surfaces as `ubb_tray_table_missing` | `tests/test_ubb_tray_map.py::test_a_glx_board_type_with_no_matching_arch_suffix_journals_once` |
| I19 a tray with 4+ chips off at first sighting is an onset; no map, an unplaced chip, or fewer off gets no prelude | `tests/test_tray_down_prelude.py::test_a_tray_with_four_or_more_chips_off_is_a_tray_down_onset`, `::test_no_map_or_an_unplaced_chip_never_classifies_as_tray_down`, `::test_one_to_three_chips_off_get_no_prelude` |
| I19 replay: no recorded episode that recovered without a power cycle gets the prelude | `tests/test_tray_down_prelude.py::test_replay_the_prelude_runs_only_on_onsets_that_never_recovered_without_a_power_cycle` (fixture `tests/fixtures/tray_down_replay.tsv`) |
| I19 order: rescan, capture, the full ladder, the power cycle only when the ladder fails (a whole tray, class A, goes to the cycle after the capture: I37) | `tests/test_tray_down_prelude.py::test_a_tray_down_onset_rescans_captures_then_runs_its_ladder`, `::test_a_ladder_that_recovers_the_tray_fires_no_power_cycle`, `::test_the_last_chance_sweep_still_gates_the_power_cycle_of_a_tray_down` |
| I19 `off_bus_after` is log-only: the event carries both counts, the host rung uses the onset count | `tests/test_ladder_v2.py::test_tray_down_no_window_power_cycle_logs_the_off_bus_count_after_the_verify`, `tests/test_ladder_v2.py::test_tray_down_no_window_escalation_uses_the_onset_count_not_the_count_after` |
| I19 the prelude changes no ladder input (same stage, indices, evidence, beats and rungs as with it off) | `tests/test_tray_down_prelude.py::test_the_prelude_changes_no_ladder_input`, `::test_capture_off_runs_the_ladder_alone` |
| I19 once per episode from any caller; a tenant defers the rescan, never the capture | `tests/test_tray_down_prelude.py::test_the_prelude_runs_once_per_episode_from_any_caller`, `::test_a_tenant_defers_the_rescan_but_not_the_capture` |
| I19 after the rescan: every chip back runs the full verify and stops only if it passes; the runtime fault is kept and the chips leave the isolated set | `tests/test_tray_down_prelude.py::test_every_chip_back_after_the_rescan_runs_the_full_verify_and_stops`, `::test_a_rescan_recovery_keeps_the_runtime_fault_and_un_isolates_the_chips`, `::test_chips_back_on_a_mesh_that_fails_verify_still_get_the_full_ladder` |
| I19 an off set first seen after a reset (the ladder's, a manual `reset_with_quiesce`, one in flight, a live scope) is not an onset; the latch is fresh after the episode | `tests/test_tray_down_prelude.py::test_the_latch_does_not_change_after_a_reset_and_is_fresh_after_the_episode`, `::test_a_manual_reset_before_the_first_sighting_is_not_an_onset`, `::test_a_reset_in_flight_or_a_live_scope_is_not_an_onset` |
| I19 the latch is not persisted: a restarted broker latches a tray still missing afresh, and buys no power cycle | `tests/test_tray_down_prelude.py::test_a_restarted_broker_latches_a_tray_still_missing_afresh` |
| I19 the gate (`device_health_gate`) runs the prelude, then its own rung | `tests/test_tray_down_prelude.py::test_the_gate_runs_the_prelude_then_its_ladder_end_to_end` |
| I19 the whole bus off keeps its own route | `tests/test_tray_down_prelude.py::test_the_whole_bus_off_keeps_its_own_route` |
| I19 the capture is read-only, CPLD reads (every configured tray, the PDB) come only from config, BMC reads get the long timeout, it meets its deadline, fsyncs, and is written even with no incident bundle | `tests/test_tray_down_prelude.py::test_the_allow_list_refuses_anything_but_the_read_shapes`, `::test_cpld_reads_come_only_from_config_and_cover_every_configured_tray`, `::test_bmc_reads_get_the_long_timeout_inside_the_deadline`, `::test_the_capture_meets_its_deadline_survives_a_missing_tool_and_fsyncs_the_bundle`, `::test_a_missing_incident_bundle_still_gets_the_bmc_reads_written` |
| I19 lspci targets the upstream bridge of every off chip, by sysfs parent or secondary bus, plus the endpoint while present | `tests/test_tray_down_prelude.py::test_lspci_targets_the_bridge_above_each_off_chip`, `::test_the_capture_reads_the_bridge_of_every_off_chip` |
| Tray rung: plan/walk semantics, fabric-gated clear, decline cases | `tests/test_device_safety.py::test_ubb_reset_plan_maps_a_clean_whole_tray_drop_to_its_bitmap`, `tests/test_device_safety.py::test_ubb_tray_walk_plan_walks_only_the_affected_trays`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_stops_as_soon_as_the_mesh_is_healthy`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_does_not_clear_a_hold_on_an_unverified_fabric`, `tests/test_device_safety.py::test_ubb_tray_reset_declines_a_fully_off_bus_mesh_it_is_the_cold_rung`, `tests/test_device_safety.py::test_maybe_emit_ubb_reset_required_names_the_exact_bmc_command_on_a_tray_down`, `tests/test_device_safety.py::test_the_tray_reset_rung_is_armed_by_default` |
| Tray rung walk: rescan and settle after each re-power, pollers quiesced across it, stops on a regression | `tests/test_device_safety.py::test_ubb_tray_reset_walk_rescans_after_each_tray_before_the_verify`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_stops_when_a_step_takes_a_healthy_chip_off_the_bus`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_re_powers_every_affected_tray`, `tests/test_device_safety.py::test_ubb_tray_reset_walk_falls_through_to_the_hold_when_the_affected_tray_stays_off` |
| Tray fire argv/handshake (tt-smi compat, off-bus chip skipped) | `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_imports_a_symbol_the_installed_tt_smi_defines`, `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_pulses_the_tray_when_a_chip_is_already_off_the_bus`, `tests/test_ubb_reset_launch.py::test_fire_ubb_reset_falls_back_to_the_chip_reset_class_on_older_tt_smi` |
| Tray rung's two branches: no-window sweep vs generic walk, classified once from threaded evidence, opt-out honoured | `tests/test_ladder_v2.py::test_classify_hold_names_the_two_branches`, `tests/test_ladder_v2.py::test_gate_tray_down_no_window_dispatches_the_back_to_back_sweep`, `tests/test_ladder_v2.py::test_gate_partial_tray_or_a_bridge_window_takes_the_generic_walk`, `tests/test_ladder_v2.py::test_gate_tray_down_no_window_names_the_command_and_holds_when_not_opted_in`, `tests/test_ladder_v2.py::test_bridge_rung_records_no_bridge_and_it_survives_the_server_replace` |
| Last-chance sweep gates EVERY host rung (gate ladder, idle escalation, post-reboot climb); a sweep that recovers cancels the cycle; never over a tenant; no per-chip SBR on a mass drop | `tests/test_ladder_v2.py::test_gate_ladder_sweeps_every_reset_before_the_power_cycle`, `tests/test_ladder_v2.py::test_a_sweep_that_recovers_the_mesh_cancels_the_power_cycle`, `tests/test_ladder_v2.py::test_the_other_two_roads_to_a_power_cycle_go_through_the_sweep`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_tenant`, `tests/test_device_safety.py::test_a_mass_gone_drop_is_left_to_the_galaxy_reset` |
| A RUNNING broker job counts as a tenant even with an empty holder scan; `force` waives an unreadable scan but never a running job; no rung hand-rolls the fd-only check | `tests/test_ladder_v2.py::test_a_running_job_is_a_tenant_even_with_an_empty_holder_scan`, `tests/test_ladder_v2.py::test_force_waives_an_unreadable_scan_but_never_a_running_job`, `tests/test_ladder_v2.py::test_the_last_chance_sweep_never_resets_over_a_running_job`, `tests/test_ladder_v2.py::test_every_tenant_guard_goes_through_the_one_helper` |
| Gate rungs release without over-climbing | `tests/test_device_safety.py::test_a_recovered_bridge_reset_releases_without_the_mesh_wide_reset`, `tests/test_device_safety.py::test_a_recovered_ubb_tray_reset_releases_without_the_mesh_wide_reset`, `tests/test_device_safety.py::test_a_recovered_mass_drop_reset_releases_without_reaching_the_host_rungs` |
| escalate() outcome mapping; unknown phase rejected | `tests/test_device_safety.py::test_escalate_waits_when_nothing_ran`, `tests/test_device_safety.py::test_escalate_recovers_on_a_present_mesh_reset`, `tests/test_device_safety.py::test_escalate_reports_terminal_when_a_present_mesh_reset_fails_and_climbs`, `tests/test_device_safety.py::test_escalate_does_not_report_recovered_when_the_mesh_was_never_degraded`, `tests/test_device_safety.py::test_escalate_rejects_an_unknown_phase` |
| Forced ladder has teeth on every platform | `tests/test_device_safety.py::test_force_escalate_has_teeth_on_a_per_target_host`, `tests/test_device_safety.py::test_hold_past_ceiling_triggers_forced_escalation` |
| Post-reboot verify: climbs to cold rung, never a second warm reboot | `tests/test_device_safety.py::test_post_reboot_all_off_bus_climbs_to_the_power_cycle`, `tests/test_device_safety.py::test_post_reboot_verify_never_repeats_the_warm_reboot`, `tests/test_device_safety.py::test_post_reboot_all_off_bus_holds_loudly_with_no_cold_rung_opted_in`, `tests/test_device_safety.py::test_boot_attribution_rejects_a_stale_escalation`, `tests/test_device_safety.py::test_an_escalation_fired_on_this_same_boot_is_not_attributed_to_it` |
| Post-reboot verify runs on the boot's first broker start only, never on a same-boot restart | `tests/test_device_safety.py::test_post_reboot_verify_runs_on_the_boots_first_broker_start`, `tests/test_device_safety.py::test_a_restart_later_in_the_boot_does_not_rerun_the_post_reboot_verify` |
| Reset tool contract: steps/attribution/no-devices/job stop | `tests/test_reset.py::test_reset_success_reports_steps_and_command`, `tests/test_reset.py::test_reset_failure_surfaces_returncode_and_output`, `tests/test_reset.py::test_explicit_reset_leaves_one_row_attributed_to_the_caller`, `tests/test_reset.py::test_reset_no_devices_does_not_call_tt_smi`, `tests/test_reset.py::test_a_reset_stops_the_running_job_gracefully`, `tests/test_reset.py::test_reset_device_quiesce_scope_routes_a_live_privsep_job` |
| Rung inventory stated loudly at boot | `tests/test_rung_arming.py::test_a_rung_that_is_off_is_stated_loudly` |
| I69 a removed chip is reset through its recorded bridge before the rescan; a stale record is refused; no rescan for a removed un-reset chip or when the gate left it un-reset; the bare rescan stays for a chip the broker did not remove, gate refusal or not | `tests/test_removed_chip_rescan.py::test_isolate_records_the_parent_bridge_before_the_remove`, `::test_a_removed_chip_is_reset_through_its_recorded_bridge_then_rescanned`, `::test_a_stale_recorded_bridge_is_refused`, `::test_no_rescan_for_a_removed_chip_without_privilege`, `::test_no_rescan_when_the_host_reset_gate_refuses`, `::test_a_gate_refusal_keeps_the_bare_rescan_for_a_chip_the_broker_did_not_remove`, `::test_no_rescan_when_a_removed_chip_has_no_usable_bridge`, `::test_every_removed_chip_is_reset_before_the_first_rescan`, `::test_the_bare_rescan_stays_for_a_chip_the_broker_did_not_remove`, `::test_a_returned_chip_drops_its_removal_record` |
| I69 a removed chip's SBR owes the AICLK ceiling (03 I35); one 1 s retrain settle follows the last SBR before the rescan; the never-returns worst case is 44.3 s of waits | `tests/test_removed_chip_rescan.py::test_a_removed_chip_reset_owes_the_aiclk_ceiling`, `::test_the_link_retrains_after_the_last_sbr_before_the_rescan`, `::test_a_removed_chip_rung_stays_inside_its_stated_worst_case` |
| I69 the tray-down prelude withholds its rescan while a removed chip is un-reset and goes straight to the ladder; with no record it rescans as before | `tests/test_tray_down_prelude.py::test_the_prelude_rescan_never_re_enumerates_a_removed_un_reset_chip` |
| I69/03 I4 the bridge rung's gate path runs the full check, fabric included, before clearing dirty; a failed check holds | `tests/test_removed_chip_rescan.py::test_gate_bridge_stage_verifies_with_fabric_before_clearing_dirty`, `::test_gate_bridge_stage_failed_verify_holds` |
| Stage metrics: structural inapplicability is not "blocked" | `tests/test_metrics.py::test_ubb_tray_no_tray_on_this_platform_is_not_applicable_not_blocked`, `tests/test_metrics.py::test_bridge_reset_missing_pci_address_is_not_applicable_not_blocked` |
| I75 (the earliest armed deadline: cooldown end, missed retry, AC-power recheck, off-bus one-shot, ceiling window) | `tests/test_health_hold_report.py::test_class_a_inside_the_cooldown_reports_the_cycle_at_the_cooldown_end`, `tests/test_health_hold_report.py::test_missed_power_cycle_retry_is_named`, `tests/test_health_hold_report.py::test_ac_needed_reports_the_recheck`, `tests/test_health_hold_report.py::test_class_b1_isolated_chips_report_the_off_bus_one_shot`, `tests/test_health_hold_report.py::test_class_b1_after_its_one_shot_reports_the_ceiling`, `tests/test_health_hold_report.py::test_a_later_ceiling_window_is_reported_after_the_first`, `tests/test_health_hold_report.py::test_cooldown_candidate_and_next_rung` |
| I75 (a fabric-77-only hold or the forced escalation off: forced rungs no earlier than the I76 bound) | `tests/test_health_hold_report.py::test_fabric_77_only_hold_names_the_10_min_bound`, `tests/test_health_hold_report.py::test_force_escalate_off_names_the_10_min_bound` |
| I75 (a branch stops the chain only once due: an armed progress retry or a one-shot not yet due hides no earlier rung; the I76 host rung after the bound's ladder) | `tests/test_health_hold_report.py::test_an_armed_progress_retry_does_not_hide_a_due_ceiling_window`, `tests/test_health_hold_report.py::test_an_off_bus_one_shot_not_yet_due_does_not_hide_an_earlier_retry`, `tests/test_health_hold_report.py::test_the_bound_host_rung_follows_the_bound_ladder` |
| I75 (the off-bus forced pass bounds: gated grace, IPMI and tray-walk hand-offs, gated operator hand-off; only after the ladder ran; the I39 progress retry) | `tests/test_health_hold_report.py::test_the_off_bus_forced_pass_bounds_are_named_when_they_fire`, `tests/test_health_hold_report.py::test_the_off_bus_pass_waits_for_the_ladder_to_have_run`, `tests/test_health_hold_report.py::test_the_i39_progress_retry_is_named` |
| I75 (a declined deadline stays reported with `next_rung_due`; no episode clock names no forced rung) | `tests/test_health_hold_report.py::test_a_declined_window_stays_reported_as_due`, `tests/test_health_hold_report.py::test_no_episode_clock_names_no_forced_rung` |
| I75 (the I73 class-A 10-min ladder and the I70 unkillable-leftover host rung are named when the code fires them) | `tests/test_health_hold_report.py::test_the_class_a_10_min_ladder_is_named_and_fires_when_reported`, `tests/test_health_hold_report.py::test_the_class_a_ladder_is_not_named_when_the_cooldown_ends_first`, `tests/test_health_hold_report.py::test_the_unkillable_leftover_host_rung_is_named` |
| I75 (the I65 retry only when armed for the episode and not refused by a guard; a pending host action is reported, never dropped) | `tests/test_health_hold_report.py::test_the_cooldown_retry_is_named_only_when_the_watchdog_asks_it`, `tests/test_health_hold_report.py::test_the_cooldown_retry_fires_when_reported`, `tests/test_health_hold_report.py::test_a_pending_host_action_is_reported_and_left_alone` |
| I75 (one routing for the report and the forced run: a window inside the post-ladder check, a window after a ladder that did not recover, a class-A mark inside the check window, a startup-gate stand-down; each reported where the watchdog acts) | `tests/test_health_hold_report.py::test_the_report_names_the_rung_the_forced_run_routes_to`, `tests/test_health_hold_report.py::test_a_class_a_ladder_mark_inside_the_check_window_is_not_reported`, `tests/test_health_hold_report.py::test_a_startup_verify_stand_down_is_reported_with_its_bounded_end` |
| I75 (deadline feeds: the broker registers the I76 bound; its mark and source per fault class and latch, fired by the watchdog then; inside a cooldown the ladder stays at the mark and only the host rung names the end; silent once spent, past due, pending, tenant-blocked or stood down; names the opted-in host rung; nothing when HEALTHY; read-only; a broken feed reports nothing) | `tests/test_health_hold_report.py::test_the_broker_registers_its_hold_bound_feed`, `tests/test_health_hold_report.py::test_the_bound_mark_is_reported_for_each_fault_class`, `tests/test_health_hold_report.py::test_a_latched_hold_reports_the_same_mark`, `tests/test_health_hold_report.py::test_a_cooldown_hold_reports_the_ladder_at_the_mark_and_the_host_rung_at_its_end`, `tests/test_health_hold_report.py::test_the_hold_bound_candidate_names_the_cooldown_only_for_its_host_rung`, `tests/test_health_hold_report.py::test_after_the_bound_ladder_the_chain_names_the_host_rung`, `tests/test_health_hold_report.py::test_the_bound_feed_is_silent_when_its_mark_is_not_next`, `tests/test_health_hold_report.py::test_the_bound_feed_names_the_host_rung_the_box_opts_into`, `tests/test_health_hold_report.py::test_the_bound_feed_yields_to_a_startup_stand_down`, `tests/test_health_hold_report.py::test_a_healthy_device_reports_no_bound`, `tests/test_health_hold_report.py::test_the_bound_feed_moves_no_clock`, `tests/test_health_hold_report.py::test_a_broken_feed_reports_nothing` |
