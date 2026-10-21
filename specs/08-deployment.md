<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Deployment

Scope: how the broker gets onto a host and stays current — `install.sh` mode dispatch, the
system-broker installer (`deploy/install-tt-device-broker.sh` + `deploy/apply-host-config.sh`),
the per-user installer (`deploy/install-user.sh`), autoupdate/reconcile
(`deploy/tt-device-autoupdate.sh`, `deploy/tt-device-reconcile.sh` + timer), the fabric-validator
pipeline (`deploy/install-fabric-validator.sh`, `deploy/tt-device-fabric-check.sh`,
`deploy/fabric-validator.pin`), the crash recorder (`deploy/install-crash-recorder.sh`,
`deploy/tt-crash-recorder.py`), the eth-heartbeat probe artifact
(`deploy/tt-device-eth-heartbeat-probe.py`), the idle-time health probe
(`deploy/tt-device-health-probe.{service,timer}`, `tt_device_mcp.health_probe`), client wiring (`deploy/tt-device-client-setup.sh`),
login-heal (`deploy/tt-device-mcp-login-heal.sh`), and the Slurm hooks (`deploy/slurm/prolog.sh`,
`deploy/slurm/epilog.sh`). Paths and variables are spec 06's; what the broker does once running is
specs 01–05. This spec owns the pipeline.

## Purpose

Two process shapes serve opposite hosts: the system broker (root + systemd, multi-tenant arbiter,
optionally device-locked) for shared machines, and the per-user standalone daemon (no sudo, no
lock) for containers and single-user boxes. The shapes differ in WHERE things live — unit or no
unit, root paths or a machine-local per-uid base — and in nothing else. Neither decides what
recovery this host gets; that is measured at boot from platform and privilege (spec 04 I17), so
there is no authority for an installer to select or persist. Deployment's job is to put the right
shape in the right place, to fail loudly when an install cannot actually serve, and — for the
fleet shape — to converge every host onto the tracked branch and current host config with no
manual re-runs. A silently broken install looks exactly like a broken device from the far end of a
job log, so every "installed" claim below is verified, not assumed.

## Invariants

- **I1 — Shared versus per-user mode is inferred from euid and systemd.** Bare `install.sh` picks the
  system broker only when euid is 0 *and* the systemd directory (`/run/systemd/system`,
  overridable as `SYSTEMD_DIR` for tests) exists; a normal user — or root in a container — gets
  the per-user daemon. `--broker`/`--user` force those modes, and an explicit `--broker`
  without systemd is refused, not degraded. The choice is about process shape and paths only —
  what this daemon may do to the device follows from spec 04 I17, not from which arm ran here.
- **I2 — The system installer builds from THIS tree, not a clone.** It pip-installs the checkout
  the script runs from into `/opt/tt-device-broker/venv` and stages a `--no-deps` wheel of the
  same tree. "broker == tree" is therefore checkable by diffing the installed `site-packages`
  against `src/` (the CLAUDE.md level-2 verification).
- **I3 — The system install fail-closes on broker `/health`.** After `enable` + `restart`, the
  installer polls `/health` over the unix socket (up to 30×1s) and exits non-zero with a
  `journalctl` hint if it never answers. `enable --now` returning is not proof of a serving
  broker.
- **I4 — The shipped sudoers must pass `visudo -c` or the apply aborts** — after removing the
  file, because a syntactically invalid file in `/etc/sudoers.d` breaks `sudo` for the whole
  host. Continuing would mark a build that silently dropped the smi grant as applied.
- **I5 — Timers are verified armed, not just enabled.** Both `tt-device-reconcile.timer`
  (installer) and `tt-device-fabric-validator.timer` (apply-host-config) get `enable --now`
  followed by an `is-active` check; either check failing aborts the install/apply. `enable` can
  report success on a masked/unarmed unit.
- **I6 — Every system install re-arms the reconcile timer** (`enable --now` at installer end,
  fail-closed per I5). Branch-testing footgun: stop the timer *before* installing a branch build
  (so a reconcile lap cannot re-pull `TTDEV_BRANCH` mid-test) and again *after* (the install
  re-armed it); leave it `inactive` but `enabled` — starting it reverts the host within ~60s.
- **I7 — An in-flight device operation is an unconditional bar to autoupdate.** The device-op
  inhibit lock (live pid) or a running `ttdev-reset-*` scope defers the update outright —
  restarting the broker mid-reset SIGTERMs the running `tt-smi` and half-resets the mesh. This
  bar is separate from the *busy/idle* gate (I24), which is always on: a busy box is drained (I20)
  at once and the update applies at the job boundary, never under a running job. The job-cap bound
  of a drain never lifts this bar: a device op still in flight at the cap restarts nothing.
- **I8 — Autoupdate marks a sha current only after every required stage lands.** Staging the
  deploy scripts (`reconcile.sh`, `client-setup.sh`, `autoupdate.sh`, `fabric-check.sh`,
  `eth-heartbeat-probe.py`) and the host-config apply are each fail-closed: on failure
  `installed.sha` stays at last-known-good so the next reconcile lap retries, and the broker is
  not restarted onto a half-applied host config. Every staged file must exist in `deploy/`.
- **I9 — A stray broker is detected and made loud, never killed.** Reconcile enumerates socket
  listeners (from `ss`, not `pgrep` — an ephemeral stdio adapter binds nothing) whose cmdline
  runs `tt_device_mcp.server` but whose cgroup is not `tt-device-broker.service`, and logs each
  to the journal, deduped on the pid set so a persistent stray logs on change, not every lap. It
  is not stopped automatically — it may be a deliberate per-user daemon.
- **I10 — The per-user install fail-closes on `daemon start`.** `daemon start` returns 0 only
  after confirming `/health` (or deferring to a live system broker); a non-zero aborts the
  install with the daemon-log path rather than reporting DONE over a daemon that cannot serve.
  The trailing `daemon status` is echo-only; a non-zero there is benign (deferred to the system
  broker).
- **I11 — Installs are idempotent and upgrade-correct.** The broker installer explicitly
  restarts the unit (`enable --now` does not restart an active unit, so a re-run would otherwise
  leave the old venv serving). The per-user installer stops any existing daemon first — trying
  both entry points it can be up from, the venv's own and the `$BINDIR` symlink, rather than
  gating on the symlink alone — because `daemon start` returns 0 on an already-running daemon
  and a re-run would otherwise report DONE with the previous build still serving.
- **I12 — The per-user installer writes nothing under `$HOME`,** and PATH wiring is print-only:
  every path the install itself needs (Claude wiring, daemon start) uses
  `$BINDIR` absolute, so the install works with `$BINDIR` off PATH. It builds its own venv —
  never `pip install --user` (refused inside an active virtualenv, and the tt-metalium image's
  active `/opt/venv` has no pip, and it would put a launcher in `~/.local/bin` — see the
  login-heal contract for why a copy there is a problem on a broker host).
- **I13 — Every per-user daemon start path yields the same daemon.** Direct start, foreground
  start, and stdio lazy start build one environment from one function. Nothing is persisted for
  them to read: health gating is on by default and the rungs are measured at boot (spec 04 I17).
- **I14 — The fabric validator is pinned and flips atomically.** Every host builds
  `fabric-validator.pin`'s exact tt-metal SHA into a SHA-keyed prefix; `current` is flipped via
  `ln -sfn` + `mv -Tf` only once all three pinned binaries exist, so a health check can never
  observe a half-built prefix. The build runs only out of band (systemd oneshot, `Nice=19`,
  retried hourly by its timer) — never inside the health gate or the updater. Until it lands the
  fabric check reports CANNOT CHECK (exit 77), and the broker does not guess (spec 03).
  The one foreground run is a fresh install's, before the broker's first start (I25).
- **I15 — The eth-heartbeat probe's exit codes are the contract and a crash cannot forge
  FROZEN.** 0 = every measured core advancing; 3 = FROZEN (deliberate, evidenced — at least one
  up-link core measured); 77 = could not measure (attach failure, no device, any unexpected
  error). An unhandled Python exception exits 1, which the consumer folds to 77 — never to
  FROZEN.
- **I16 — The update source is a constant, not configuration. Only the branch is settable.**
  `autoupdate.sh` carries the repository URL as a readonly literal that interpolates nothing, so
  there is no key to set; the installer writes none. An operator chooses *which branch* to track and *whether* to track
  at all, never *where the code comes from*. This is what makes the update path credential-free
  **by construction rather than by convention**: a source that can be set can be set to a private
  repository, a private source needs a credential, and the constant is what stops a single edit
  to `/etc/default/tt-device-broker` creating that need. Root therefore fetches the
  public repository itself, into `$TTDEV_ROOT/src` created under `umask 077`: what lands there is
  installed as root on the same lap, so it may never sit anywhere an unprivileged user could
  write to it, and it is never briefly world-readable on the way. A clone that cannot be made, a
  host that cannot reach the network, and a branch that does not exist are each named and the lap
  exits 0 — no-op-and-say-so, retried next minute. A leftover directory that is not a repository
  is removed rather than left to fail every future lap. **The commit installed is the one the lap
  resolved and announced**, not `origin/$BRANCH` re-read afterwards: the drain (I20) can wait for
  minutes, and a branch that moved in that window must not be installed unannounced under a sha
  `installed.sha` would then misname.

- **I17 — The idle-time health probe acts only on an idle broker, and pages once per episode.**
  `tt-device-health-probe.timer` (every 2 min) runs `python -m tt_device_mcp.health_probe` as root,
  in the broker's venv (apply renders it from `TTDEV_VENV`). It stands back, touching nothing,
  unless the broker's `/health` answers with no hold, `fsm_state` `healthy`, no degraded flag and no
  job running or queued, the restart-inhibit file (spec 06, `TT_DEVICE_MCP_DEVICE_OP_LOCK`) names no
  live device op, its device-op flock (spec 06 I8) is free, and no tenant holds the device outside
  the job table (spec 03 I13 holder scan; root and system accounts such as the pollers are not
  tenants, and an incomplete scan counts as a tenant). It asks again right before each action, so a
  broker that turns busy mid-lap stops it. The broker stops the pollers around a reset, and a probe
  that restarted them would fight it. Its actions are non-destructive only: a PCI bus rescan when
  fewer chips are present than expected (at most one per 30 min while short; the one write is made
  under the device-op flock, taken non-blocking, and skipped while the broker holds it), and
  `reset-failed` + `start` of a poller in the broker's own list (`TT_DEVICE_MCP_POLLER_SERVICES`,
  same default as the broker's) that is `failed` or `inactive` and enabled. A poller that is absent, disabled or masked is off on
  purpose and left off; one that is `activating` is systemd's to finish and is not restarted. After
  a start it waits the settle time and re-reads `/health` and the inhibit file; if a hold, a
  degraded flag, a non-healthy FSM or a device op appeared meanwhile, it stops the poller again. The
  residual window is that settle time: the I8 contract forbids holding the flock across a subprocess
  such as `systemctl start`, so a reset that begins inside it can meet the poller the probe just
  started until the probe stops it. The expected count is the broker's own (spec 03 I11:
  `TT_DEVICE_MCP_EXPECTED_CHIPS`, else the high-water `chip_baseline.json`, read and never written),
  so no chip count is hardcoded. The alert hook, chip count, poller list, inhibit and flock files
  and health dir come from the running broker's environment unless the probe's unit sets them. What
  it cannot fix goes to the alert hook (spec 03 I37), once per episode: chips still missing after a
  rescan; a poller still down after a restart, down again within an episode (a flap), or
  `activating` for 3 laps in a row; AER fatal/non-fatal counters rising, at most once an hour. A
  poller's episode closes only after 3 laps in a row up, so a crash loop pages once; a first restart
  that comes up stays quiet. The first lap after a boot takes the AER counters as a baseline and
  pages nothing. Episodes and counters live in `health_probe_state.json` in the health dir and reset
  on reboot. Apply installs and arms it on every host (`TTDEV_HEALTH_PROBE=0` disarms it) and warns
  but does not abort if it will not arm. Only once the new timer is active does it retire the
  unmanaged `tt-health-probe.timer` it replaces: that timer is disabled and its unit files are moved
  aside to `*.retired-by-tt-device-mcp`, never deleted. An opted-out host, or one where the new
  timer did not arm, keeps the old timer running. Apply installs and arms the units only when the
  broker venv can import `tt_device_mcp.health_probe`; a venv without it gets no units, and a timer
  an earlier build left armed is stopped with a warning (it could only fail).
- **I18 — An applying update holds dispatch from its busy check through the restart, and a
  queued job counts as busy.** Before it counts anything the updater writes the dispatch hold
  (spec 01 I18: `<pid> <epoch>` in `/run/tt-device-broker/dispatch-hold`, written under a
  temporary name and renamed into place, so the broker never reads it half-written) and waits
  `TTDEV_HOLD_SETTLE_SEC` (default 2) for a dispatch already past the broker's check to show up
  as a child. Busy is then: a broker child, a user process holding `/dev/tenstorrent`, an
  active `ttdev-job-*.scope` (a re-adopted job is no child of the new broker), or a persisted
  queued spec (`<log-dir>/queued/*.json`). Counting only children and holders let a job queued
  behind one that had just ended be dispatched two seconds before the restart, which cut its
  output pipe and failed it. A lap that restarts nothing drops the hold at once; an applying lap keeps it
  until `systemctl restart` returns (systemd also removes it with the RuntimeDirectory), so
  queued jobs wait, persisted, for the new broker. An exit on any other path drops it too.
- **I19 — reconcile never revives a broker that is already starting or stopping.** Step 1 reads
  `systemctl is-active`: `activating`, `reloading` and `deactivating` are a job systemd already
  runs (the broker sits in `activating` through its startup check, minutes after a boot), so
  reconcile runs neither `reset-failed` nor `start` and logs "in progress" once per episode (the
  state is kept in `/run/tt-device-broker/reconcile_inprogress`, cleared once the broker is
  `active` or down). Every other non-`active` state is still reset and started.
- **I20 — A busy box is drained at once, and no job is killed for it.** A lap that finds the box
  busy (I24) drains in the same lap: it keeps the I18 hold, so the running job finishes and nothing
  new starts, polls every 5 s until no broker child, device holder or job scope remains and no
  device op (I7) is in flight, and applies at that job boundary, one poll after the job ends at
  most; queued jobs wait, persisted, for the new broker. A queued spec alone is not running work
  and does not hold a drain up. The wait under the hold is bounded by the broker's hard job limit,
  `MAX_TIMEOUT_SEC` + `GRACEFUL_KILL_GRACE_SEC` (1500 + 60 = 1560 s), counted from the start of
  the youngest running `ttdev-job-*` scope as systemd records it (a re-adopted job keeps its age
  across restarts; a start that cannot be read counts as now). **A broker job still running at
  that bound, of any age, never gets a restart**: a broker child, a running `ttdev-job-*` scope,
  or a device holder whose cgroup is a `ttdev-job-*` scope (a re-adopted job, seen even when the
  scope list cannot be read). The lap restarts nothing, drops the hold, and the next lap retries,
  so the update applies at the first lap after that job ends, however long it runs. A capped job
  ends by the broker's job-limit kill, at most one job cap after its start. A
  no-timeout-except-on-contention job (spec 01 I20) past its limit is not killed for time: it runs
  until it ends, or until a queued job contends for it (`PREEMPT_GRACE_SEC` + `PREEMPT_KILL_GRACE_SEC`,
  60 + 10 s, after contention starts); while nothing is queued the update waits with no bound. A
  device op in flight at the bound is handled the same way (I7). Only a device holder outside every
  broker job, which no broker kill ends and a restart does not touch, stops holding the update at
  the bound: the lap applies. A broker that is not running (not `active`, `activating`, `deactivating` or
  `reloading`) protects no job by waiting, and the drain would block the reconcile that starts it,
  so the drain ends and the restart starts it. The hold is rewritten on every poll and once more
  as the apply starts, so the broker's overdue bound (`DISPATCH_HOLD_MAX_SEC`) counts from the
  updater's last sign of life, never from the start of a long drain. A drain blocks its lap, and
  the reconcile that runs it, for at most its bound; a lap that finds a broker job already past it
  holds dispatch for the settle only (`TTDEV_HOLD_SETTLE_SEC`, 2 s) and exits. The restart after an apply is an ordinary
  broker start, so the queue opens only once the startup check (spec 03 I4: the forced fabric
  pass, after the 1G hugepage pool is ready) has passed.
- **I21 — Applies are rate-limited per box, and the limit only moves when an apply happens.**
  Every apply restarts the broker, and every start runs the full startup check, fabric traffic
  pass included (spec 03 I4), so a burst of pushes is a burst of fabric passes. With
  `TTDEV_MIN_APPLY_INTERVAL_SEC` > 0 (default 0, off; 3600 suggested on a Galaxy), a lap that finds
  a new sha less than that long after the last apply exits 0 and says when the sha will apply; the
  first lap past the interval applies the newest sha, so a burst becomes one restart. The check
  runs before the device-op bar and the I18 hold, so a rate-limited lap holds nothing. The last
  apply is the epoch in `$ROOT/last-apply`, written just before `systemctl restart`, so an apply
  that fails before the restart is retried on the next lap; a missing, non-numeric or future stamp
  bars nothing; the stamp is read base 10. The dispatch hold, drain and busy gate (I18, I20, I24)
  are unchanged, and the startup check after the restart is never skipped, deferred or shortened. **The limit applies to a
  healthy broker only**: the next sha may be the fix for a broker the last apply broke, so a lap
  inside the interval applies at once, and logs `rate limit skipped` with the reason, when the
  broker is not `active` (failed, activating, inactive), when its systemd `NRestarts` grew past the
  count saved in `$ROOT/last-apply-nrestarts` right after the last apply's restart (a crash loop),
  or when `/health` does not answer, reports `held`, or reports an FSM that is `recovering` or
  `down`. One exception: every start opens `recovering` with `fsm_why` `startup_unverified` and
  holds the device until its startup check passes (hugepage wait, 120 s, spec 03 I33, then one full
  gate pass with fabric, at most 436 s, spec 04). That broker is booting, not broken, and stays
  limited while it is within `STARTUP_BOUND` (600 s: 120 + 436 s plus 44 s for the restart) of the
  last apply and held for nothing but that check (`held` false while the gate runs, or
  `held_reason` `device unverified: ...`). Past the bound, held for a fault found during the check
  (a dirty episode), or after a failed check (which names its fault in `fsm_why`), it applies at
  once.
  Worst-case deploy latency of a sha to a healthy broker: `TTDEV_MIN_APPLY_INTERVAL_SEC` + one 60 s
  lap + the I20/I24 drain on a busy box (the running job's end, plus one 60 s lap when it ran past
  the cap and one more job if the queue dispatched one before that lap). A capped job ends by the
  broker's job-limit kill at most one job cap, 1560 s, after it started; a
  no-timeout-except-on-contention job (spec 01 I20) past its limit ends only by itself or on
  contention, so with nothing queued the latency has no bound. To a broken broker the limit adds
  nothing. The by-hand escape is
  `rm $ROOT/last-apply`: the config file is sourced over the environment, so
  `TTDEV_MIN_APPLY_INTERVAL_SEC=0` on the command line does not override a configured value.
- **I24 — An update never restarts the broker under a running tenant job.** A running job is
  re-adopted across a restart (spec 01), but the restart still costs it: its client's socket
  drops for seconds, a client that reads the drop as a failure kills the job (and the kill owes a
  reset), and the new broker holds the queue for its startup checks until the re-adopted job ends.
  So the busy gate is always on, with no setting: a busy lap drains (I20) at once. The defer
  settings of earlier versions (`TTDEV_MAX_DEFER_SEC`, `TTDEV_DRAIN_AFTER_SEC`,
  `TTDEV_DRAIN_TIMEOUT_SEC`, `TTDEV_DRAIN_LONG_SEC`) are read by nothing; a value left in the
  config file changes nothing. After the install, just before it writes `installed.sha` and
  restarts, the lap looks once more under the I18 hold (rewritten as it polls): a broker child,
  device holder or job scope still running (a tenant that opened the device on its own, or a
  dispatch after the broker dropped an overdue hold), or a device op (I7), is waited out as a drain
  is, under the same bound; a broker job or device op still running at the bound restarts nothing
  and leaves `installed.sha` on the old sha, so the next lap retries. A stopped broker (MainPID 0)
  has no children to count. Worst case on a busy box: the update applies at the first lap after
  the running job ends. A capped job ends by the broker's job-limit kill at most one job cap
  (1560 s) after it started (a re-adopted job keeps the time it already served); a
  no-timeout-except-on-contention job (spec 01 I20) runs past it until it ends or a queued job
  contends for it, so while nothing is queued the update waits with no bound. The drain delays only the restart: no reset,
  ladder rung, power cycle or hold escalation waits on it.

- **I25 — A fresh install builds the pinned validator before the broker's first start.**
  `install-tt-device-broker.sh` checks that `validator/current` holds all three pinned binaries
  (and the cabling descriptor, when one is configured) after apply-host-config. If not, it runs
  `systemctl start tt-device-fabric-validator.service` in the foreground (it joins the run
  apply-host-config kicked with `--no-block`) and only then enables and restarts the broker. A failed
  build, or one that leaves `current` incomplete, exits 1 naming the unit's journal, and the broker
  is not (re)started. An upgrade keeps the old `current` until the new build flips it, so it never
  waits here. Reason: a broker started before the validator exists gets a 77 from its startup
  fabric pass and holds the device fabric-unverified, so a fresh install never comes up verified.

- **I26 — The installer never removes the venv a live broker runs from.** `$R/venv` is a relative
  symlink to a versioned venv, `$R/venv.<short-sha>.<random>` (`deploy/broker-venv.sh`). The
  installer builds the new venv at that final path (a venv is not relocatable), pip-installs this
  tree into it and verifies it: `import tt_device_mcp.server`, `import tt_smi` and `tt-smi --help`
  all succeed. A failed build or verification removes only the new venv, leaves `$R/venv` as it was
  and exits 1. The flip is `ln -sfn` + `mv -Tf`, one rename, and comes right before
  apply-host-config (which renders the unit from `$R/venv` and probes it). A legacy real-directory
  venv moves aside to `venv.legacy` first, so a migration has a gap of two renames, not a rebuild.
  Every `venv.*` the link does not point at is removed only after the restarted broker answers
  `/health`; a failed install keeps the old venv for a rollback. Reason: until the restart, the
  running broker resolves tt-smi (every reset rung), its lazy imports and its stdio adapters through
  `$R/venv`. Removing it for the minute a rebuild takes left every reset rung unable to run in that
  window. Autoupdate does not run the installer; it pip-installs into the venv `$R/venv` resolves to.

- **I27 — A validator repair never removes the prefix `current` points at.** Each pin has two build
  slots, `validator/<sha>` and `validator/<sha>.build`, and a build runs only in the slot `current`
  does not point at, then flips `current` to it (I14). A validator tree is not relocatable (its
  RPATH and runtime root are absolute paths into the tree), so the slot is its final path. A run
  that finds a complete slot `current` does not point at flips to it; one that finds the live slot
  complete changes nothing, so the link's own mtime stays the time of its last real flip. The other
  same-pin slot is removed only once `current` has pointed at the live one for an hour, far longer
  than any fabric check. apply-host-config counts either slot as built. Reason: a same-pin repair
  used to wipe and rebuild the live prefix, so every fabric check in the tens of minutes of the
  build ran a missing validator and returned 77. The live slot is found by resolved path (the
  link's target and both slots canonicalized), never by the link's text, so a root spelled
  differently from the run that wrote `current` (a trailing slash, a symlinked parent) still finds
  it. A `current` that exists but cannot be resolved, or that resolves into the chosen build slot,
  stops the run with an error rather than building there.
- **I28 — A fresh install knows the expected chip count from what the machine is, never from how
  many chips answer.** When neither `TTDEV_EXPECTED_CHIPS` (operator or `/etc/default`) nor the
  current unit gives a count, apply-host-config derives one with `deploy/tt-device-expected-chips.sh`:
  a board type of any chip that answered the installer's `tt-smi -s` snapshot (`tt-galaxy-wh`,
  `tt-galaxy-bh` → 32), else a DMI product name containing "Galaxy" (→ 32), else reset mode
  `galaxy` (→ 32). The number of chips that answered is never an input, so a tray already off the
  bus at install still gets 32. Card boards (n150, n300, p100, p150, p300) decide nothing: their
  type gives chips per card, not cards per box, and a count set below the real one would bypass
  the high-water mark. Card boards, unknown or mixed types, a card that disagrees with a galaxy
  DMI product or reset mode, and no source at all leave the key unset with a loud warning naming
  the reason, so `/health` shows the count as unknown. The operator value, then the unit's, always
  wins; the chosen source is logged. The derived value is backfilled into `/etc/default` like any
  resolved knob, so later updates read it back. Reason: with no declared count the broker's bar is
  a high-water mark of chips seen; a whole-tray drop already present at the first start became the
  bar and was never detected. With 32 declared, fewer chips present fails the check (03) and the
  hold takes its normal recovery path.
## Interfaces

**`/etc/default/tt-device-broker`** — written by the installer, sourced by `apply-host-config.sh`,
`autoupdate.sh`, `reconcile.sh`, and `client-setup.sh`. It is consumed by `.`-sourcing in bash and
by nothing else — it is not a systemd `EnvironmentFile` — so the one operator-supplied value
(`TTDEV_BRANCH`) is written **bash-escaped**, not merely quoted: an apostrophe in a branch name
closes a `'...'` literal and has the rest of the value parsed as shell, on every source, as
root. It is the only key that needs it, and it keeps it: the risk belongs to the mechanism of
sourcing a config file as root, not to any one key. The `TTDEV_*` key reference is spec 06's
table. Two key groups: pipeline config (`TTDEV_ROOT`, `TTDEV_VENV`, `TTDEV_AUTOUPDATE`,
`TTDEV_BRANCH`) consumed by the scripts themselves, and per-host
broker knobs (`TTDEV_RESET_MODE`, `TTDEV_FABRIC_CHECK_CMD`, `TTDEV_FABRIC_DESCRIPTOR`,
`TTDEV_ETH_HEARTBEAT_CMD`, `TTDEV_RESET_MIN_DEAD_FRAC`, `TTDEV_SELFHEAL_RELIFT`,
`TTDEV_EXPECTED_CHIPS`, `TTDEV_AUTO_POWER_CYCLE`,
`TTDEV_PREJOB_DISPATCH`, `TTDEV_ETH_CHECK_PYTHON`, `TTDEV_POLLER_SERVICES`) that `apply-host-config.sh` renders into
same-named `Environment=TT_DEVICE_MCP_*` (or `TTDEV_*`) unit lines. An unset knob renders **no
line** (the code default rules); a set knob reaches the unit verbatim. This rendering is the ONLY
route from `/etc/default` into the broker process — nothing else sources the file into the unit,
so a knob without a render line is unreachable.

**Systemd units and timers installed** (system shape):

| Unit | Installed by | Schedule / trigger |
|---|---|---|
| `tt-device-broker.service` | apply-host-config (rendered) | `Type=notify`, `WatchdogSec=300`, `Restart=on-failure`, socket-only (`--no-http`), `LimitMEMLOCK=infinity` (jobs inherit it through their scope) |
| `tt-device-reconcile.timer` → `.service` | apply-host-config; enabled by installer | `OnBootSec=30s`, `OnUnitActiveSec=60s` — runs `/opt/tt-device-broker/reconcile.sh` |
| `tt-device-fabric-validator.timer` → `.service` | apply-host-config (enabled there) | `OnBootSec=10min`, `OnUnitInactiveSec=1h` — retries the pinned build (oneshot, `TimeoutStartSec=7200`) |
| `tt-device-buslock.service` | apply-host-config / install-crash-recorder | continuous `perf stat -e ls_locks.bus_lock -I 60000` to `buslock.log`; enabled only where the PMU event counts |
| `tt-crash-recorder.service` | install-crash-recorder | oneshot at boot (`After=multi-user.target`) |
| `tt-device-health-probe.timer` → `.service` | apply-host-config (enabled there unless `TTDEV_HEALTH_PROBE=0`) | `OnBootSec=90s`, `OnUnitActiveSec=2min` — oneshot `python -m tt_device_mcp.health_probe` (I17) |

Also installed by apply-host-config: `/usr/local/bin/tt-device-mcp-smi-ro` +
`/etc/sudoers.d/tt-device-mcp-smi` (spec 05), `/etc/logrotate.d/tt-device-broker`, a journald
retention drop-in (~30 days; journald restarted best-effort), `/etc/profile.d/tt-device-mcp-heal.sh`
(login-heal), `/etc/profile.d/tt-device-mcp-client.sh` (runs client-setup at login) and
`/usr/lib/systemd/system-shutdown/tt-device-mcp-power-cycle.shutdown` (the broker's power cycle
after a clean OS shutdown, spec 04 I88; also under `/lib/systemd/system-shutdown/` on a split
`/lib`). The hook install is required: a failure stops apply, as for the unit.

**The validator pin file** (`deploy/fabric-validator.pin`, staged to
`$TTDEV_ROOT/fabric-validator.pin`): sourced shell defining `TTDEV_VALIDATOR_REPO`, a full
40-char `TTDEV_VALIDATOR_SHA` (`git fetch --depth 1` cannot resolve an abbreviation),
`TTDEV_VALIDATOR_TOOLCHAIN`, and three target/artifact pairs — the cluster validator
(`run_cluster_validation`), the whole-mesh dispatch probe (`distributed_program_dispatch`), and
the pre-job dispatch probe (`metal_example_add_2_integers_in_compute`) — plus the Galaxy cabling
descriptor path. Bumping the SHA and pushing is the whole upgrade interface: hosts rebuild into a
new prefix on reconcile and flip atomically; a bad pin is a one-line revert.

**Cron (per-user shape):** none is installed, and any `daemon start`/`self-update` entry present
in the crontab is removed — this shape uses cron for nothing, so such an entry is stale whatever
put it there. An `@reboot` entry would re-exec `$BINDIR` by absolute path inside a boot-cleared
base: reboot persistence promised and silently not delivered. The shape does not survive a reboot
and says so — re-run the installer. There is no self-update cron either: you own this install, and
a daemon that replaced itself under a running experiment is a surprise, not a service.

**Client wiring writes:** `client-setup.sh` (and the per-user installer directly) write the
user's `~/.claude.json` via `claude mcp remove/add -s user` (stdio entry, absolute command path)
and `~/.cursor/mcp.json` directly — each only when that client is present, and client-setup only
when the existing entry is missing or wrong, so a correct config is a fast no-op. The command
path resolves `command -v tt-device-mcp`, then the broker venv, then the per-user `$BINDIR`.

**Staged copies under `$TTDEV_ROOT`** (`/opt/tt-device-broker`): `reconcile.sh`,
`autoupdate.sh`, `client-setup.sh`, `fabric-check.sh`, `eth-heartbeat-probe.py`,
`install-fabric-validator.sh`, `fabric-validator.pin`, `installed.sha`, `wheel/`, `venv` (a
symlink to `venv.<short-sha>.<random>/`, I26), `eth-venv/` (pinned `tt-exalens` interpreter for the
heartbeat probe), `validator/<sha>/` and `validator/<sha>.build/` (I27) + `validator/current`, `slurm/prolog.sh` + `slurm/epilog.sh` (staged by BOTH
`install-tt-device-broker.sh` and `apply-host-config.sh`, like `fabric-check.sh` above — the
first install and every subsequent auto-update both refresh them, since auto-update never
re-runs the installer, only `apply-host-config.sh`).

## Behavior

**Mode dispatch (`install.sh`).** Resolve mode per I1; `--broker` re-checks root and systemd
explicitly; then `exec` the mode's installer. The per-user installer gets the repo path
as `$1`. Root running the per-user shape on a systemd host is allowed with a note.

**System install flow (`install-tt-device-broker.sh`), in order.** (1) Root check. (2) Venv
build (I26): a new versioned venv beside the live one, pip-install this tree, verify it, stage the
wheel, `chmod -R a+rX`. The live `$R/venv` is untouched until the flip, which comes right before
apply-host-config; the old venv is pruned once the restarted broker answers `/health`. (3) `/usr/local/bin/tt-device-mcp` symlink into
the venv. (4) Lock policy: cooperative by default; `TTDEV_LOCK=1` opts in, applied only *after*
the broker is up via `tt-device-mcp lock` (self-checks and reverts); `TTDEV_NO_LOCK=1` forces
cooperative (back-compat). (5) Machine detection: `tt-smi -s` board_type containing "galaxy" (or
preset `TT_DEVICE_MCP_RESET_MODE`) selects Galaxy reset mode. The same snapshot's board types (one
per answering chip) are passed to apply-host-config as `TTDEV_BOARD_TYPES` for the expected chip
count (I28); the snapshot is skipped only when both the reset mode and `TTDEV_EXPECTED_CHIPS` are
preset. (6) Stage `fabric-check.sh`
(fail-closed under `set -e` — a broker with no fabric check full-resets after every abnormal
exit) and the eth-heartbeat probe (loud but non-fatal: optional pre-read, staging best-effort);
scrub the retired `eth-heartbeat-check.sh` wrapper. (7) Write `/etc/default/tt-device-broker`
(`TTDEV_BRANCH` defaults to `main` and is the only key the operator supplies; `TTDEV_AUTOUPDATE`
defaults to `1`, so `sudo TTDEV_AUTOUPDATE=0 ./install.sh` is how a host opts out — the
assignment after `sudo`, which resets the environment. The source is not
among the keys written — it is a constant, I16) and stage the pipeline scripts. (8) Run
`apply-host-config.sh` (below). (9) `enable` + explicit `restart` (I11), then reconcile-timer
enable + is-active (I5/I6), then wire the invoking user's clients immediately. (10) `/health`
poll, fail-closed (I3). (11) Optional lock.

**Host-config apply (`apply-host-config.sh`).** Shared verbatim by the installer and every
autoupdate — the single source of truth for the host surface, so a unit/sudoers change reaches
every host on the next version bump. Reads `/etc/default`; for each knob absent there, falls back
to the value in the currently rendered unit (a migrating host never loses an operator's setting),
derives the expected chip count when neither gives one (I28), then backfills resolved values
into `/etc/default` (`ensure_default`: missing or present-but-empty
keys only — an empty key is a key predating its value, and leaving it made the file disagree with
the running unit). Renders the unit (knob rules per Interfaces; the device-lock `DEVICE_GROUP`
line is preserved across re-renders so an update never silently unlocks a shared host), then
installs the host files listed under Interfaces, `daemon-reload`s, enables + verifies the
fabric-validator timer (I5), enables the buslock counter only where the PMU event actually counts
(missing PMU is a warning, never a failed unit), kicks an out-of-band validator build (`--no-block`)
if the pinned SHA is not built, and refreshes the login banner from the lock state.
`--print-unit` is a dry-run seam: render the unit to stdout from a given config, touch no host
path, need no root — every install step is downstream of the render, so tests drive it on any
host.
The unit orders after `dev-hugepages\x2d1G.mount` (`Wants=` + `After=`) and never after
`tenstorrent-hugepages.service`: that unit is `After=multi-user.target`, while the broker and
a serving unit (`After=` the broker) come before multi-user.target, so the ordering is a cycle and
systemd breaks it at boot by deleting the broker's or that unit's start job. A drop-in cannot
remove the vendor's `After=`; the broker waits for the page count itself (spec 03 I33).

**Autoupdate flow (`autoupdate.sh`).** Invoked every reconcile lap; self-gated
(`TTDEV_AUTOUPDATE=1` and all config keys present, else silent no-op). Serialized by `flock` on
`$ROOT/.autoupdate.lock` (two pip installs racing into one venv leave un-uninstallable
dist-info). **The source needs no resolution** — it is the constant in the script (I16). Root
clones it into `$ROOT/src` under `umask 077` on the first lap (removing any non-repository
leftover first), fetches the tracked branch thereafter, and reads the sha with
`rev-parse --verify -q refs/remotes/origin/$TTDEV_BRANCH`; an unmakeable clone, an unreachable
network and a missing branch each name the fault and exit 0. If that sha equals `installed.sha`,
exit. Otherwise: the apply rate limit (I21); device-op bar and reset-scope check
(I7 — the scope check asks systemd, because a reset outlives the broker by design); dispatch hold and busy/idle
gate per I7, I18, I20 and I24; then materialize the tree — `reset --hard` to **the sha this lap resolved**, not to
`origin/<branch>` re-read after the gate, so a branch that moved during a drain is not installed
under a sha `installed.sha` would misname; pip install into the broker venv (on failure, clear broken
`dist-info` metadata and retry `--ignore-installed` — recovery from a half-installed venv that
would otherwise wedge every future update); stage the required scripts fail-closed (I8);
provision `$ROOT/eth-venv` with the pinned `tt-exalens` if missing (fail-OPEN: a box that cannot
build it reports the rung OFF, which is loud on its own); run `apply-host-config.sh` from the
pulled tree fail-closed (I8); write `installed.sha`; restart the broker. After the restart it appends
one `autoupdate_apply` line to the broker's `health_events.jsonl` (the broker's journal dir,
`TT_DEVICE_MCP_HEALTH_DIR` or root's default, spec 06): `from_sha`, `to_sha`, `idle` (false when
this lap drained a job or waited at the restart point), `drained_s` (the seconds it waited),
`restart_rc` (the restart's exit code: a failed restart is recorded, not shown as a clean apply),
plus the usual `ts`/`iso`. A failed write only warns; it adds no wait and changes no gate.

**Reconcile semantics (`reconcile.sh`, per minute + 30s after boot).** Idempotent, quiet when
nothing drifts. (0) Run autoupdate (its own gates apply). (1) Broker liveness: the exact
predicate is `systemctl is-active tt-device-broker` not `active` and not a start or stop in
progress (I19) → `reset-failed` + `start`. This
deliberately covers `failed`/start-limit exhaustion, but the predicate cannot distinguish an
operator's deliberate `systemctl stop` (`inactive`): a stopped broker is revived within ~60s
— measured consequence: the revived broker's startup gate reset a device a container daemon was
serving. Quiescing the host broker today requires stopping the reconcile
timer too. (2) Socket perms reasserted to 0666 (identity is SO_PEERCRED, spec 05). (1b) Stray
broker detection per I9. (3) `/usr/local/bin/tt-device-mcp` repointed at the broker venv if
drifted. (4) Lock drift: if the unit carries `DEVICE_GROUP`, re-trigger udev when the device
node's group drifted. (4b) Age out per-job logs older than 30 days by whole-file deletion
(`find -mtime`), never logrotate — the broker reads `*.log` back as its job ledger, and every
logrotate stanza renames or truncates live files. (5) Per-user client wiring: for each real user
with `~/.claude.json` or `~/.cursor`, run `client-setup.sh` as them in a login shell — a freshly
installed client is wired within a minute.

**Per-user install flow (`install-user.sh`).** Source defaults to this tree (`$1` from
`install.sh`) or `git+https` at `TTDEV_BRANCH` (default `main`). Stop any running daemon first
(I11); build the venv under
`TT_DEVICE_MCP_INSTALL_DIR`/`/tmp/tt-device-mcp-<uid>` (spec 06 I4) and symlink `$BINDIR`;
print (never write) the PATH line (I12); `daemon start` fail-closed (I10); remove any stale
tt-device-mcp cron entry; wire Claude if the `claude` CLI is present. No
self-update, no lock — the operator owns the box.

**Validator build/pin flow (`install-fabric-validator.sh`).** flock-serialized (concurrent
tt-metal builds corrupt a tree; the timer refires long before a build ends). If all three pinned
binaries exist in a slot for the SHA (I27): flip `current` to it if it is not already live, prune the
other slot once the live one is an hour old, and exit (idempotent — the timer polls freely; checking
all three is what keeps a pre-dispatch-probe host from reporting "already built" forever).
Otherwise, in the slot `current` does not point at: shallow-fetch the exact SHA, verify the pinned toolchain and its compilers
exist by name, configure from scratch (build dir *and* CPM cache — an interrupted download
poisons the cache reproducibly), require ninja, build only the three named targets, verify each
binary, flip `current` atomically (I14). Consumption (`fabric-check.sh`, the 0/77/UNHEALTHY
verdict split, the dispatch stage) is spec 03/04 behavior; deployment's contract is that the
check runs only the pinned build and never builds anything itself.

**Crash recorder contract.** `install-crash-recorder.sh` (root) installs the passive instruments
on any host — explicitly including hosts with no broker, as the control group: stdlib-only
`tt-crash-recorder` (oneshot: reads this boot's journal for the previous boot's BERT record —
readable only until the next reboot overwrites it — and sums the previous boot's bus locks;
writes JSONL to the health dir in the broker's own schema, so managed and control hosts land in
one dataset) and the buslock counter unit. Enable order matters: the recorder must consume the
previous boot's counts and write its boot marker before the counter starts appending this boot's.
Enables are best-effort (`|| true`) — this installer is deliberately not fail-closed; a missing
PMU event is reported and the counter skipped.

**Login-heal contract (`tt-device-mcp-login-heal.sh` → `/etc/profile.d`).** At login, if
`~/.local/bin/tt-device-mcp` exists, the system CLI exists, and they resolve differently: pip
uninstall + remove the local copy, loudly. Nothing this project installs writes to `$HOME`, so a
copy there came from a user's own `pip install --user`, which never updates with the host and
shadows the always-current system symlink on PATH. No reinstall (the system symlink is the single source of
truth); fast no-op otherwise and on hosts without the system CLI.

**Slurm hooks (`deploy/slurm/prolog.sh`, `deploy/slurm/epilog.sh`).** Both are thin `exec`
wrappers whose only contract with Slurm is the CLI's exit code: the Prolog `exec`s `tt-device-mcp
pre-step` (read-only, refuses over an unfit or occupied device); the Epilog `exec`s
`tt-device-mcp post-step --exit-code "$EC"` (reclaims stragglers, recovers, confirms — the
finished step's exit code is what forces the fabric traffic pass on a failure and spares a clean
step its cost). `exec` replaces the shell rather than wrapping it in a subshell, so nothing
between the CLI and slurmd can swallow or remap the exit code. Staged, fatally (`install -m 0755`
under `set -euo pipefail`, no `|| true`, alongside the fabric-check stage), into `$ROOT/slurm/` —
the exact path `deploy/README.md`'s `slurm.conf` snippet names — by **both**
`install-tt-device-broker.sh` (first install) **and** `apply-host-config.sh` (every subsequent
auto-update), the same doubled staging `fabric-check.sh` and `install-fabric-validator.sh` already
use. Auto-update never re-runs the installer, only `apply-host-config.sh` (that script's own
header comment); staging the hooks in the installer alone would mean an autoupdating host —
`TTDEV_AUTOUPDATE=1`, the normal path on these boxes — never receives `deploy/slurm/*.sh` at all,
leaving the README's documented path permanently empty on exactly the hosts that update
themselves, not merely stale until a manual re-install.

Slurm documents that Prolog/Epilog run with **no search path**, so neither hook trusts PATH as its
primary source: because these hooks run as root, each first tries
`${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin` via `/etc/default/tt-device-broker`
(`TTDEV_ETC_DEFAULT` overridable, the same seam `apply-host-config.sh` uses — spec 06), and only
falls back to `command -v tt-device-mcp` (a per-user or dev install with an inherited PATH, and no
system config to resolve against) if that absolute path is not executable. Absolute-first is
deliberate: trusting PATH first would let a directory ahead of the real install on a root
process's PATH run in `tt-device-mcp`'s place, exactly the trust Slurm's missing search path was
designed to deny it. Either way, the hook exits 1 with a message on stderr naming the missing
binary if neither resolves. Without this, a stock deployment's Prolog exits 127 on a bare command
name, and the drain it causes is indistinguishable from the device genuinely being unfit.

The Epilog's exit code is `${SLURM_JOB_DERIVED_EC:-0}` when non-zero, else
`${SLURM_JOB_EXIT_CODE:-0}`: the latter is only the wrapping batch script's own status and can
read 0 even when a step genuinely failed (multiple `srun` steps, `|| true`, a trap), while the
former is the highest exit code across every step in the job — the value the fabric traffic pass
must key on. Neither variable is set outside a real Slurm Epilog; the `:-0` fallback is a
deliberate fail-open for a manual invocation, where a script that was never a Slurm step should
not pay for a fabric pass.

Site wiring (`PrologEpilogTimeout`, `SchedulerParameters=nohold_on_prolog_fail`,
`PrologFlags=Alloc`) is `deploy/README.md`'s Slurm section, not this spec's — the two facts this
spec owns are that `PrologEpilogTimeout` must exceed both step deadlines
(`TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC` and `TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC`, spec 01) or a
slow-but-healthy pass drains the node with no reason logged, and that `nohold_on_prolog_fail` is
required or a Prolog failure requeues the job *held*, turning every drained node into an operator
ticket. No `HealthCheckProgram` is shipped: slurmd kills it at 60 s, hard, which cannot contain a
`tt-smi -r` (~15 s) plus a fabric traffic pass (45–100 s), let alone a bridge or tray recovery
rung — a periodic SIGKILL mid-reset is a device-wedging machine, and it would fire while jobs are
running. Un-draining after a broker-side repair is a manual
`scontrol update NodeName=<node> State=RESUME` — drain survives a reboot, so a node the reboot
rung fixed comes back still drained with a stale reason.

## Design decisions

**Autoupdate exists for fleet convergence.** The alternative is per-host manual installer runs,
and the measured failure of that shape is drift: hosts running a script fixed weeks ago, units
missing knobs the code grew. Tracking a branch, plus running `apply-host-config.sh` from the
pulled tree on every bump, means both the python package *and* the host surface (unit, sudoers,
timers, hooks) converge with no hands on any box.

**Installs are fail-closed because a half-install is invisible from where it hurts.** A broker
that never answered `/health` on a locked host is a total device outage; a dropped sudoers grant
or an unarmed timer "succeeds" today and is discovered as an unexplained behavior gap weeks
later. Exit-nonzero-with-a-pointer at install time is the only moment the operator is watching.
The two deliberate exceptions are best-effort by argument: the eth-heartbeat probe staging in the
installer (an optional pre-read, loud on failure) and the crash-recorder enables (instrumenting a
control host must not be able to fail the host).

**Wheel-from-this-tree.** Installing the running checkout — not a fresh clone of some branch —
is what makes the level-2 verification (`diff` installed site-packages against `src/`) meaningful
and what lets a branch build be tested at all: the installer is a claim about *this tree*, and
autoupdate is the separate, explicitly-gated mechanism that pulls branches.

**PATH wiring is print-only** because both installers already use absolute paths everywhere they
act (`$BINDIR` in cron/wiring, `/usr/local/bin` for the system shape), so writing rc files buys
nothing the install needs — and on a network-shared `$HOME` an rc edit leaks to machines the install
never touched. `$HOME` is the one base that is neither machine-local nor root-writable
(spec 06), so nothing this project installs goes there at all — and login-heal removes a copy a
user put there themselves.

**A hardcoded public source** because every alternative reintroduces a credential. Autoupdate
pip-installs as root on a timer, so the question "where does that code come from" has to have one
answer that an operator cannot change. Leave the source configurable and a single edit can aim a
host at a private repository, which then needs a key — held by root, or borrowed from a user —
and the whole apparatus for storing, rotating and auditing that key comes back with it. A literal
URL removes the question. Only the branch stays settable (I16), because which branch a host tracks
changes what it runs, not whether it needs a credential to get it.

**The validator is pinned, prebuilt, and out-of-band** because a fabric verdict that depends on
whose tt-metal checkout a host happens to have is not a verdict, and a health check that can
block on a tens-of-minutes build is not a health check. The SHA-keyed prefix + atomic `current`
flip makes a pin bump a one-line, revertible change; the hourly retry timer exists because
apply-host-config runs only on version bumps, so a transiently failed build would otherwise stay
failed silently until the next push.

**The crash recorder is standalone and stdlib-only** because its value is the control group: a
host that has never run the broker crashing the same way as managed hosts is the single most
informative measurement available, and it is only obtainable by instrumenting a box without
otherwise changing it.

## Test anchors

Deploy behavior is mostly shell; the suites assert on the scripts' logical statements
(`tests/deploy_helpers.py`: `_statements` joins backslash-continued lines, `_one` requires
exactly one statement matching every needle — so a guard cannot be satisfied by a comment or
split across an untested seam) and on `apply-host-config.sh --print-unit` renders. Where only the
script exists, the claim stands on the script (listed below).

| Claim | Anchors (pytest node ids) |
|---|---|
| I1 (mode dispatch: root+systemd → broker; else per-user; no authority flag; `--broker` refused without systemd) | `tests/test_install_modes.py::test_root_on_a_systemd_host_gets_the_full_suite`, `tests/test_install_modes.py::test_root_without_systemd_gets_the_per_user_daemon`, `tests/test_install_modes.py::test_non_root_gets_the_per_user_daemon`, `tests/test_install_modes.py::test_there_is_no_authority_flag_to_pass`, `tests/test_install_modes.py::test_explicit_broker_is_refused_without_systemd`, `tests/test_install_modes.py::test_the_shipped_systemd_path_is_the_real_one`, `tests/test_install_modes.py::test_the_inference_matches_this_host_with_nothing_injected` |
| I3 (/health fail-closed, polled) | `tests/test_deploy_broker_health_failclosed.py::test_broker_health_failure_aborts_with_journal_hint`, `tests/test_deploy_broker_health_failclosed.py::test_broker_health_is_polled_in_a_loop` |
| I4 (sudoers visudo fail-closed, removed first) | `tests/test_deploy_sudoers_failclosed.py::test_invalid_shipped_sudoers_aborts`, `tests/test_deploy_sudoers_failclosed.py::test_invalid_shipped_sudoers_is_removed_before_aborting` |
| I5/I6 (timers enabled AND verified active) | `tests/test_deploy_timer_enable_failclosed.py::test_reconcile_timer_enable_is_verified`, `tests/test_deploy_timer_enable_failclosed.py::test_fabric_validator_timer_enable_is_verified`, `tests/test_deploy_timer_enable_failclosed.py::test_is_active_check_follows_the_enable` |
| I25 (validator built before the broker's first start) | `tests/test_deploy_validator_before_broker.py::test_validator_build_runs_before_the_broker_starts`, `tests/test_deploy_validator_before_broker.py::test_validator_build_is_blocking_and_failclosed`, `tests/test_deploy_validator_before_broker.py::test_incomplete_build_fails_the_install`, `tests/test_deploy_validator_before_broker.py::test_built_check_covers_all_three_pinned_binaries` |
| I26 (live venv never removed: versioned venv built and verified beside it, one-rename flip before apply-host-config, prune after /health) | `tests/test_deploy_install_live_swap.py::test_installer_venv_step_never_removes_the_live_venv`, `tests/test_deploy_install_live_swap.py::test_fresh_install_builds_verifies_and_links_a_versioned_venv`, `tests/test_deploy_install_live_swap.py::test_upgrade_keeps_the_live_venv_until_prune`, `tests/test_deploy_install_live_swap.py::test_reinstall_of_the_same_commit_never_builds_over_the_live_venv`, `tests/test_deploy_install_live_swap.py::test_a_venv_that_fails_verification_is_discarded_and_the_live_one_kept`, `tests/test_deploy_install_live_swap.py::test_migration_from_a_real_dir_venv_has_only_the_two_rename_gap`, `tests/test_deploy_install_live_swap.py::test_installer_flips_before_host_config_and_prunes_after_health` |
| I27 (validator repair builds in the slot `current` does not point at; stale slot pruned after an hour) | `tests/test_deploy_install_live_swap.py::test_validator_fresh_build_publishes_current`, `tests/test_deploy_install_live_swap.py::test_validator_upgrade_keeps_the_old_pin_live_until_the_flip`, `tests/test_deploy_install_live_swap.py::test_validator_same_pin_repair_builds_beside_the_live_prefix`, `tests/test_deploy_install_live_swap.py::test_validator_second_repair_returns_to_the_first_slot`, `tests/test_deploy_install_live_swap.py::test_validator_already_built_pin_does_not_refresh_the_link`, `tests/test_deploy_install_live_swap.py::test_validator_repair_with_trailing_slash_root_builds_beside_the_live_prefix`, `tests/test_deploy_install_live_swap.py::test_validator_repair_with_symlinked_parent_root_builds_beside_the_live_prefix`, `tests/test_deploy_install_live_swap.py::test_validator_refuses_when_current_resolves_inside_the_build_slot`, `tests/test_deploy_install_live_swap.py::test_host_config_counts_either_build_slot_as_built` |
| I28 (expected chip count from board type / DMI product / galaxy reset mode, never from the chips that answer; operator and unit win; cannot tell = unset + warning) | `tests/test_deploy_expected_chips.py::test_a_galaxy_board_type_maps_to_32`, `tests/test_deploy_expected_chips.py::test_the_count_never_follows_how_many_chips_answered`, `tests/test_deploy_expected_chips.py::test_a_card_board_cannot_tell`, `tests/test_deploy_expected_chips.py::test_no_source_that_can_tell_never_guesses`, `tests/test_deploy_expected_chips.py::test_the_operator_value_wins_over_every_derived_source`, `tests/test_deploy_expected_chips.py::test_the_existing_unit_value_wins_over_derivation`, `tests/test_deploy_expected_chips.py::test_cannot_tell_leaves_it_unset_and_says_so_loudly`, `tests/test_deploy_expected_chips.py::test_a_fresh_galaxy_install_writes_32_with_no_operator_input`, `tests/test_deploy_expected_chips.py::test_a_fresh_galaxy_install_with_a_tray_already_missing_still_gets_32`, `tests/test_deploy_expected_chips.py::test_a_fresh_galaxy_install_with_no_chip_answering_gets_32_from_dmi`, `tests/test_deploy_expected_chips.py::test_fewer_chips_than_the_declared_count_fails_the_check`, `tests/test_apply_host_config_render.py::test_expected_chips_unset_renders_no_line` |
| I7 (device-op bar unconditional, separate from the I24 busy gate; the job-cap bound never lifts it) | `tests/test_deploy_autoupdate_idle_gate.py::test_an_in_flight_device_op_still_bars_the_update`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_op_in_flight_holds_a_drained_apply`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_op_in_flight_still_bars_the_restart_past_the_cap`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_op_at_the_restart_point_blocks_the_restart` |
| I8 (autoupdate required stages fail-closed; apply precedes installed.sha; staged sources exist) | `tests/test_deploy_required_install_failclosed.py::test_autoupdate_required_stages_fail_closed`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_host_config_apply_fails_closed`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_host_config_apply_precedes_installed_sha`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_staged_sources_exist_in_deploy` |
| Installer staging split (fabric fatal; heartbeat loud-not-fatal) | `tests/test_deploy_required_install_failclosed.py::test_installer_fabric_check_stage_is_fatal_not_swallowed`, `tests/test_deploy_required_install_failclosed.py::test_installer_eth_heartbeat_stage_is_loud_but_not_fatal` |
| I9 (stray broker: detected, not killed, deduped) | `tests/test_deploy_stray_broker_detected.py::test_reconcile_detects_a_stray_broker`, `tests/test_deploy_stray_broker_detected.py::test_reconcile_does_not_kill_the_stray`, `tests/test_deploy_stray_broker_detected.py::test_reconcile_dedups_a_persistent_stray` |
| I10 (per-user daemon-start fail-closed; status echo not swallowed into the verdict) | `tests/test_deploy_user_daemon_failclosed.py::test_daemon_start_failure_aborts_before_done`, `tests/test_deploy_user_daemon_failclosed.py::test_daemon_status_verdict_not_swallowed` |
| I11 (stop-before-replace; not symlink-gated; first install proceeds; broker restart on upgrade) | `tests/test_install_modes.py::test_reinstalling_stops_the_daemon_before_replacing_its_venv`, `tests/test_install_modes.py::test_the_stop_is_not_gated_on_the_bindir_symlink`, `tests/test_install_modes.py::test_a_first_install_with_nothing_to_stop_still_proceeds`, `tests/test_install_modes.py::test_broker_install_restarts_so_an_upgrade_takes_effect` |
| I12 (nothing under $HOME; own venv, never user-site; machine-local base) | `tests/test_install_modes.py::test_per_user_install_writes_nothing_under_home`, `tests/test_install_modes.py::test_per_user_install_owns_its_venv_and_never_uses_user_site`, `tests/test_install_modes.py::test_venv_and_cli_land_under_the_machine_local_install_base` |
| I13 (one daemon environment from every start path; nothing persisted) | `tests/test_install_modes.py::test_the_install_persists_no_authority_profile`, `tests/test_install_modes.py::test_the_daemon_env_declares_no_rung_policy`, `tests/test_install_modes.py::test_start_fg_gets_the_same_environment_as_start`, `tests/test_install_modes.py::test_the_per_user_daemon_health_gates_like_any_other` |
| Cron contract (none installed; no self-update; stale entries removed) | `tests/test_install_modes.py::test_no_cron_entry_is_installed`, `tests/test_install_modes.py::test_the_self_update_command_is_gone_not_merely_unscheduled`, `tests/test_install_modes.py::test_a_stale_self_update_cron_is_removed` |
| Branch defaults to `main` everywhere; no retired-branch default | `tests/test_deploy_default_branch.py::test_broker_installer_defaults_branch_to_main`, `tests/test_deploy_default_branch.py::test_user_installer_defaults_branch_to_main`, `tests/test_deploy_default_branch.py::test_no_in_repo_default_tracks_anything_but_main` |
| Unit render: unset knob → no line; set knob reaches the unit; existing render preserved | `tests/test_apply_host_config_render.py::test_seam_preserves_the_existing_render`, `::test_eth_heartbeat_unset_renders_no_line`, `::test_eth_heartbeat_set_reaches_the_unit`, `::test_reset_floor_knobs_unset_render_no_line`, `::test_reset_floor_knobs_set_reach_the_unit`, `::test_expected_chips_unset_renders_no_line`, `::test_expected_chips_set_reaches_the_unit`, `::test_cold_rung_knobs_unset_render_no_line`, `::test_cold_rung_knobs_set_reach_the_unit`, `::test_a_stale_ubb_reset_setting_renders_no_line`, `::test_prejob_dispatch_unset_renders_no_line`, `::test_prejob_dispatch_set_reaches_the_unit`, `::test_eth_check_python_unset_renders_no_line`, `::test_eth_check_python_reaches_the_unit` |
| Unit never ordered after `tenstorrent-hugepages.service` (boot ordering cycle) | `tests/test_apply_host_config_render.py::test_the_unit_never_orders_after_the_vendor_hugepages_service` |
| Unit lifts `LimitMEMLOCK` so scoped jobs can pin host memory | `tests/test_apply_host_config_render.py::test_the_unit_lifts_the_memlock_limit_jobs_inherit` |
| Retired heartbeat wrapper scrubbed on apply | `tests/test_apply_host_config_render.py::test_eth_heartbeat_pointed_at_the_retired_wrapper_is_scrubbed` |
| I15 (probe exit contract: crash folds to 77, never bare 1; verdict pass-through) | `tests/test_eth_heartbeat_probe.py::test_run_folds_crash_to_cannot_check_never_bare_one`, `tests/test_eth_heartbeat_probe.py::test_run_passes_through_main_return`, `tests/test_eth_heartbeat_probe.py::test_run_propagates_system_exit`, `tests/test_eth_heartbeat_probe.py::test_main_frozen_core_is_frozen`, `tests/test_eth_heartbeat_probe.py::test_main_all_advancing_is_ok`, `tests/test_eth_heartbeat_probe.py::test_main_attach_failure_is_cannot_check`, `tests/test_eth_heartbeat_probe.py::test_main_no_devices_is_cannot_check`, `tests/test_eth_heartbeat_probe.py::test_main_read_error_skips_core_not_frozen`, `tests/test_eth_heartbeat_probe.py::test_main_offbus_heartbeat_not_counted` |
| I16 (the source is a literal that expands nothing, assigned once; the config's only operator input is the branch) | `tests/test_deploy_update_source.py::test_the_update_source_is_a_literal_with_no_expansion`, `tests/test_deploy_update_source.py::test_nothing_else_assigns_the_source`, `tests/test_deploy_update_source.py::test_the_only_operator_input_in_the_config_is_the_branch` |
| I16 (root-owned clone, private from creation) | `tests/test_deploy_update_source.py::test_the_clone_lives_under_the_broker_root_never_in_a_home`, `tests/test_deploy_update_source.py::test_the_clone_is_created_private` |
| I16 (installs the sha it resolved; unreachable source is a no-op lap that says why; leftover cleared) | `tests/test_deploy_update_source.py::test_the_lap_installs_the_sha_it_resolved`, `tests/test_deploy_update_source.py::test_every_unreachable_source_is_a_no_op_lap_that_says_why`, `tests/test_deploy_update_source.py::test_a_leftover_non_repository_is_cleared_rather_than_retried_forever` |
| I16 (the branch stays operator input, bash-escaped, round-trips an apostrophe) | `tests/test_deploy_update_source.py::test_the_branch_is_written_bash_escaped`, `tests/test_deploy_update_source.py::test_the_config_round_trips_a_branch_containing_an_apostrophe` |
| Slurm hooks exist, parse, and propagate the CLI's exit code | `tests/test_slurm_steps.py::test_the_slurm_hooks_exist_and_are_executable`, `tests/test_slurm_steps.py::test_the_slurm_hooks_are_shell_clean`, `tests/test_slurm_steps.py::test_the_hooks_propagate_the_cli_exit_code` |
| The epilog carries Slurm's job exit code inward | `tests/test_slurm_steps.py::test_the_epilog_passes_slurms_job_exit_code_through` |
| Hooks fail loudly (not a bare 127) when tt-device-mcp cannot be resolved, and still resolve it via the installed venv when Slurm gives them no usable PATH | `tests/test_slurm_steps.py::test_the_hooks_fail_loudly_when_the_cli_cannot_be_resolved`, `tests/test_slurm_steps.py::test_the_hooks_resolve_the_cli_via_the_installed_venv_with_no_usable_path` |
| Epilog prefers a non-zero `SLURM_JOB_DERIVED_EC` over `SLURM_JOB_EXIT_CODE`, and still falls back to it when DERIVED_EC is zero | `tests/test_slurm_steps.py::test_the_epilog_prefers_slurm_job_derived_ec_when_nonzero`, `tests/test_slurm_steps.py::test_the_epilog_falls_back_to_slurm_job_exit_code_when_derived_ec_is_zero` |
| The installer stages both hooks at the path `deploy/README.md` documents, and auto-update (via `apply-host-config.sh`) keeps them current on an already-installed host | `tests/test_slurm_steps.py::test_the_installer_stages_the_slurm_hooks_at_the_documented_path`, `tests/test_slurm_steps.py::test_apply_host_config_also_stages_the_slurm_hooks` |
| Each hook exports its own step-deadline var from `/etc/default/tt-device-broker` to the CLI child, and the export is harmless when the var was never set | `tests/test_slurm_steps.py::test_the_hooks_export_their_deadline_var_from_etc_default`, `tests/test_slurm_steps.py::test_the_hooks_do_not_require_the_deadline_var_to_be_set` |
| I17 (idle-time probe: stands back unless idle, no device op and no tenant; re-checks before and after acting; broker's chip count and poller list; enabled failed/inactive pollers only; one page per episode, flaps included; AER first-lap baseline; apply arms it, optional, retires the old timer only after arming, moved aside) | `tests/test_health_probe.py::test_idle_verdict_requires_an_idle_healthy_broker`, `tests/test_health_probe.py::test_a_busy_broker_is_left_alone`, `tests/test_health_probe.py::test_a_broker_turning_busy_stops_the_rescan`, `tests/test_health_probe.py::test_expected_chips_reads_the_broker_count`, `tests/test_health_probe.py::test_missing_chips_rescan_then_alert_once`, `tests/test_health_probe.py::test_rescan_recovers_without_alert`, `tests/test_health_probe.py::test_telemetry_restart_and_alert`, `tests/test_health_probe.py::test_a_poller_stopped_and_disabled_on_purpose_is_left_off`, `tests/test_health_probe.py::test_an_activating_poller_is_left_to_systemd_then_paged_once`, `tests/test_health_probe.py::test_a_start_that_is_still_activating_does_not_page`, `tests/test_health_probe.py::test_a_flapping_poller_pages_once_per_episode`, `tests/test_health_probe.py::test_a_poller_that_stays_up_between_rare_falls_is_restarted_quietly`, `tests/test_health_probe.py::test_the_pollers_are_the_broker_list`, `tests/test_health_probe.py::test_the_probe_default_is_the_broker_default`, `tests/test_health_probe.py::test_the_poller_list_comes_from_the_broker_env`, `tests/test_health_probe.py::test_a_device_op_in_flight_stops_the_lap`, `tests/test_health_probe.py::test_a_tenant_holding_the_device_outside_the_job_table_stops_the_lap`, `tests/test_health_probe.py::test_a_device_op_that_starts_during_the_settle_stops_the_poller_again`, `tests/test_health_probe.py::test_a_job_starting_during_the_settle_keeps_the_poller`, `tests/test_health_probe.py::test_device_op_lock_reads_the_broker_inhibit_file`, `tests/test_health_probe.py::test_tenant_holders_fail_closed`, `tests/test_health_probe.py::test_the_broker_device_op_flock_is_tested_and_the_rescan_written_under_it`, `tests/test_health_probe.py::test_a_rescan_the_flock_refused_is_retried_next_lap`, `tests/test_health_probe.py::test_aer_growth_alerts_once_an_hour`, `tests/test_health_probe.py::test_aer_already_counted_at_the_first_lap_is_a_baseline_not_a_page`, `tests/test_health_probe.py::test_hook_and_chip_count_come_from_the_broker_env`, `tests/test_health_probe.py::test_apply_installs_and_arms_the_probe_optionally`, `tests/test_health_probe.py::test_an_armed_probe_moves_the_old_timer_aside`, `tests/test_health_probe.py::test_an_opted_out_host_keeps_the_old_timer`, `tests/test_health_probe.py::test_a_probe_that_will_not_arm_keeps_the_old_timer`, `tests/test_health_probe.py::test_a_host_without_the_old_timer_only_arms`, `tests/test_health_probe.py::test_the_probe_unit_runs_the_broker_venv`, `tests/test_health_probe.py::test_a_venv_without_the_probe_module_gets_no_probe_units`, `tests/test_health_probe.py::test_a_venv_without_the_probe_module_stops_a_stale_timer`, `tests/test_health_probe.py::test_a_venv_with_the_probe_module_is_never_skipped` |
| I18 (dispatch hold from the busy check through the restart; queued specs and job scopes count as busy) | `tests/test_deploy_autoupdate_idle_gate.py::test_the_hold_is_taken_before_anything_is_counted`, `tests/test_deploy_autoupdate_idle_gate.py::test_the_hold_appears_whole`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_queued_job_alone_applies_at_once_under_the_hold`, `tests/test_deploy_autoupdate_idle_gate.py::test_an_idle_box_applies_and_keeps_the_hold_until_the_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_running_job_scope_holds_dispatch_and_the_update_applies_when_it_ends` |
| I19 (an in-progress broker is not reset or started; logged once) | `tests/test_deploy_reconcile_activating.py::test_an_in_progress_broker_is_not_reset_or_started`, `tests/test_deploy_reconcile_activating.py::test_an_in_progress_broker_logs_once_per_episode`, `tests/test_deploy_reconcile_activating.py::test_a_down_broker_is_still_reset_and_started`, `tests/test_deploy_reconcile_activating.py::test_an_active_broker_is_left_alone` |
| I20 (a busy box drains at once: the hold is kept until the running job ends, then the update applies within one poll; a back-to-back queue cannot slip a job in; bounded under the hold by the job cap from the youngest job scope's start; a broker job past it, re-adopted or not, gets no restart and the next lap retries, a lone non-broker holder past it does; a down broker ends the drain; a device op holds the apply; the restart opens the queue only after the startup fabric pass, after hugepages) | `tests/test_deploy_autoupdate_idle_gate.py::test_the_job_cap_is_the_brokers_hard_limit_plus_its_kill_grace`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_running_job_scope_holds_dispatch_and_the_update_applies_when_it_ends`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_busy_box_drains_at_once_and_applies_at_the_job_boundary`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_back_to_back_queue_cannot_slip_a_job_into_the_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_job_past_its_cap_never_gets_a_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_re_adopted_job_never_gets_a_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_holder_outside_any_broker_job_stops_holding_the_update_at_the_cap`, `tests/test_deploy_autoupdate_idle_gate.py::test_an_idle_box_applies_and_keeps_the_hold_until_the_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_down_broker_ends_the_drain`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_op_in_flight_holds_a_drained_apply`, `tests/test_deploy_autoupdate_idle_gate.py::test_the_drain_refreshes_the_hold`, `tests/test_deploy_autoupdate_idle_gate.py::test_the_apply_ends_in_a_full_broker_restart`, `tests/test_restart_drain.py::test_a_job_held_across_an_update_waits_for_the_startup_fabric_pass`, `tests/test_restart_drain.py::test_a_failed_startup_pass_after_an_update_keeps_the_queue_held` |
| I21 (apply rate limit: off by default; a burst of pushes is one restart onto the newest sha after the interval; checked before the device-op bar and the hold; the stamp is written only by an apply that restarts; a missing, bad or future stamp bars nothing; a broker that is not active, crash-loops, is silent, held or recovering/down applies at once; a healthy broker, or one in its startup check (recovering/startup_unverified, held only for that check) within STARTUP_BOUND of the last apply, stays limited; past the bound or held for a fault it applies at once) | `tests/test_deploy_autoupdate_rate_limit.py::test_the_rate_limit_is_off_by_default`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_burst_of_pushes_becomes_one_restart_onto_the_newest_sha`, `tests/test_deploy_autoupdate_rate_limit.py::test_the_wait_names_the_time_left`, `tests/test_deploy_autoupdate_rate_limit.py::test_no_stamp_a_bad_stamp_or_a_future_stamp_bars_nothing`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_rate_limited_lap_holds_nothing_and_skips_no_check`, `tests/test_deploy_autoupdate_rate_limit.py::test_the_stamp_is_written_only_by_an_apply_that_restarts`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_broken_broker_applies_a_new_sha_inside_the_interval`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_healthy_broker_stays_rate_limited`, `tests/test_deploy_autoupdate_rate_limit.py::test_the_startup_bound_covers_the_hugepage_wait_and_the_fabric_check`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_booting_broker_stays_rate_limited`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_startup_check_past_its_bound_or_held_applies_at_once`, `tests/test_deploy_autoupdate_rate_limit.py::test_the_crash_loop_baseline_is_the_count_after_the_last_apply`, `tests/test_deploy_autoupdate_rate_limit.py::test_a_missing_stamp_is_silent_and_a_leading_zero_stamp_is_decimal` |
| I24 (no restart under a running tenant job: busy gate always on with no setting, old defer settings inert; a job running at the restart point is waited out to the job cap, and one past it blocks the restart; a device op there blocks the restart and installed.sha stays old; MainPID 0 counts no children) | `tests/test_deploy_autoupdate_idle_gate.py::test_the_idle_gate_is_always_on_with_no_setting`, `tests/test_deploy_autoupdate_idle_gate.py::test_old_defer_settings_do_not_turn_the_drain_off`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_job_running_at_the_restart_point_is_waited_out_to_its_cap`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_job_that_ends_at_the_restart_point_is_waited_out`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_job_past_its_cap_at_the_restart_point_gets_no_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_device_op_at_the_restart_point_blocks_the_restart`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_stopped_broker_has_no_children_to_count` |
| Autoupdate flow: one `autoupdate_apply` event per apply, in the broker's journal dir and schema; `restart_rc` records a failed restart; `idle`/`drained_s` come from the lap's own drain; a failed write only warns | `tests/test_deploy_autoupdate_idle_gate.py::test_apply_appends_one_autoupdate_apply_event_in_the_broker_schema`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_failed_restart_is_recorded_as_failed`, `tests/test_deploy_autoupdate_idle_gate.py::test_the_event_goes_to_the_brokers_journal_dir`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_failed_event_write_only_warns`, `tests/test_deploy_autoupdate_idle_gate.py::test_an_idle_lap_reports_idle_and_no_drain`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_lap_that_drains_reports_busy_and_its_drain_time`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_wait_at_the_restart_point_adds_to_the_drain_time` |

**Unanchored (the claim stands on the script):** I2 (wheel/venv from this tree —
`install-tt-device-broker.sh`); the installer's flow ordering and lock opt-in; Galaxy detection
from `tt-smi` board_type; `/etc/default` write + `ensure_default` backfill and unit-read
fallbacks; `DEVICE_GROUP` preservation; autoupdate flock
serialization, sha short-circuit, dist-info recovery, eth-venv provisioning (fail-open);
reconcile steps 1–5 other than stray detection (revive predicate, socket perms, symlink repoint,
lock drift, log aging, per-user wiring cadence); the validator build flow and atomic flip
(`install-fabric-validator.sh`); the crash-recorder installer and `tt-crash-recorder.py`
capture logic; login-heal; client-setup's config-file writes; timer schedules (unit files).
