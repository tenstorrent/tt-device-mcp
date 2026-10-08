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
(`deploy/tt-device-eth-heartbeat-probe.py`), client wiring (`deploy/tt-device-client-setup.sh`),
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
  bar is separate from the *busy/idle* gate, which is OFF by default: `TTDEV_MAX_DEFER_SEC=0`
  applies immediately even on a busy box (jobs are re-adopted across the restart); a host that
  wants an idle window sets it to a max wait, clocked from the FIRST pending update so a commit
  burst cannot starve a busy host forever.
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
  resolved and announced**, not `origin/$BRANCH` re-read afterwards: the idle gate can defer for
  minutes, and a branch that moved in that window must not be installed unannounced under a sha
  `installed.sha` would then misname.

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
`TTDEV_EXPECTED_CHIPS`, `TTDEV_AUTO_REBOOT`, `TTDEV_AUTO_POWER_CYCLE`, `TTDEV_AUTO_UBB_RESET`,
`TTDEV_PREJOB_DISPATCH`, `TTDEV_ETH_CHECK_PYTHON`) that `apply-host-config.sh` renders into
same-named `Environment=TT_DEVICE_MCP_*` (or `TTDEV_*`) unit lines. An unset knob renders **no
line** (the code default rules); a set knob reaches the unit verbatim. This rendering is the ONLY
route from `/etc/default` into the broker process — nothing else sources the file into the unit,
so a knob without a render line is unreachable.

**Systemd units and timers installed** (system shape):

| Unit | Installed by | Schedule / trigger |
|---|---|---|
| `tt-device-broker.service` | apply-host-config (rendered) | `Type=notify`, `WatchdogSec=300`, `Restart=on-failure`, socket-only (`--no-http`) |
| `tt-device-reconcile.timer` → `.service` | apply-host-config; enabled by installer | `OnBootSec=30s`, `OnUnitActiveSec=60s` — runs `/opt/tt-device-broker/reconcile.sh` |
| `tt-device-fabric-validator.timer` → `.service` | apply-host-config (enabled there) | `OnBootSec=10min`, `OnUnitInactiveSec=1h` — retries the pinned build (oneshot, `TimeoutStartSec=7200`) |
| `tt-device-buslock.service` | apply-host-config / install-crash-recorder | continuous `perf stat -e ls_locks.bus_lock -I 60000` to `buslock.log`; enabled only where the PMU event counts |
| `tt-crash-recorder.service` | install-crash-recorder | oneshot at boot (`After=multi-user.target`) |

Also installed by apply-host-config: `/usr/local/bin/tt-device-mcp-smi-ro` +
`/etc/sudoers.d/tt-device-mcp-smi` (spec 05), `/etc/logrotate.d/tt-device-broker`, a journald
retention drop-in (~30 days; journald restarted best-effort), `/etc/profile.d/tt-device-mcp-heal.sh`
(login-heal) and `/etc/profile.d/tt-device-mcp-client.sh` (runs client-setup at login).

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
`install-fabric-validator.sh`, `fabric-validator.pin`, `installed.sha`, `wheel/`, `venv/`,
`eth-venv/` (pinned `tt-exalens` interpreter for the heartbeat probe), `validator/<sha>/` +
`validator/current`, `slurm/prolog.sh` + `slurm/epilog.sh` (staged by BOTH
`install-tt-device-broker.sh` and `apply-host-config.sh`, like `fabric-check.sh` above — the
first install and every subsequent auto-update both refresh them, since auto-update never
re-runs the installer, only `apply-host-config.sh`).

## Behavior

**Mode dispatch (`install.sh`).** Resolve mode per I1; `--broker` re-checks root and systemd
explicitly; then `exec` the mode's installer. The per-user installer gets the repo path
as `$1`. Root running the per-user shape on a systemd host is allowed with a note.

**System install flow (`install-tt-device-broker.sh`), in order.** (1) Root check. (2) Venv
rebuild: `rm -rf $R/venv`, fresh venv, pip-install this tree, stage the wheel, `chmod -R a+rX`.
The removal happens while any previous broker is still running; live stdio adapters exec out of
that venv, so an adapter alive across the rebuild is executing a path that no longer exists. (3) `/usr/local/bin/tt-device-mcp` symlink into
the venv. (4) Lock policy: cooperative by default; `TTDEV_LOCK=1` opts in, applied only *after*
the broker is up via `tt-device-mcp lock` (self-checks and reverts); `TTDEV_NO_LOCK=1` forces
cooperative (back-compat). (5) Machine detection: `tt-smi -s` board_type containing "galaxy" (or
preset `TT_DEVICE_MCP_RESET_MODE`) selects Galaxy reset mode. (6) Stage `fabric-check.sh`
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
then backfills resolved values into `/etc/default` (`ensure_default`: missing or present-but-empty
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
ltx-host (`After=` the broker) come before multi-user.target, so the ordering is a cycle and
systemd breaks it at boot by deleting the broker's or ltx-host's start job. A drop-in cannot
remove the vendor's `After=`; the broker waits for the page count itself (spec 03 I33).

**Autoupdate flow (`autoupdate.sh`).** Invoked every reconcile lap; self-gated
(`TTDEV_AUTOUPDATE=1` and all config keys present, else silent no-op). Serialized by `flock` on
`$ROOT/.autoupdate.lock` (two pip installs racing into one venv leave un-uninstallable
dist-info). **The source needs no resolution** — it is the constant in the script (I16). Root
clones it into `$ROOT/src` under `umask 077` on the first lap (removing any non-repository
leftover first), fetches the tracked branch thereafter, and reads the sha with
`rev-parse --verify -q refs/remotes/origin/$TTDEV_BRANCH`; an unmakeable clone, an unreachable
network and a missing branch each name the fault and exit 0. If that sha equals `installed.sha`,
exit. Otherwise: device-op bar and reset-scope check
(I7 — the scope check asks systemd, because a reset outlives the broker by design); busy/idle
gate per I7; then materialize the tree — `reset --hard` to **the sha this lap resolved**, not to
`origin/<branch>` re-read after the gate, so a branch that moved during a defer is not installed
under a sha `installed.sha` would misname; pip install into the broker venv (on failure, clear broken
`dist-info` metadata and retry `--ignore-installed` — recovery from a half-installed venv that
would otherwise wedge every future update); stage the required scripts fail-closed (I8);
provision `$ROOT/eth-venv` with the pinned `tt-exalens` if missing (fail-OPEN: a box that cannot
build it reports the rung OFF, which is loud on its own); run `apply-host-config.sh` from the
pulled tree fail-closed (I8); write `installed.sha`; restart the broker.

**Reconcile semantics (`reconcile.sh`, per minute + 30s after boot).** Idempotent, quiet when
nothing drifts. (0) Run autoupdate (its own gates apply). (1) Broker liveness: the exact
predicate is `systemctl is-active tt-device-broker != "active"` → `reset-failed` + `start`. This
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
binaries exist for the SHA: re-assert the `current` symlink and exit (idempotent — the timer polls
freely; checking all three is what keeps a pre-dispatch-probe host from reporting "already built"
forever). Otherwise: shallow-fetch the exact SHA, verify the pinned toolchain and its compilers
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
| I7 (idle gate off by default; opt-in window; device-op bar unconditional) | `tests/test_deploy_autoupdate_idle_gate.py::test_the_idle_gate_is_off_by_default`, `tests/test_deploy_autoupdate_idle_gate.py::test_a_host_can_still_ask_for_an_idle_window`, `tests/test_deploy_autoupdate_idle_gate.py::test_an_in_flight_device_op_still_bars_the_update` |
| I8 (autoupdate required stages fail-closed; apply precedes installed.sha; staged sources exist) | `tests/test_deploy_required_install_failclosed.py::test_autoupdate_required_stages_fail_closed`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_host_config_apply_fails_closed`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_host_config_apply_precedes_installed_sha`, `tests/test_deploy_required_install_failclosed.py::test_autoupdate_staged_sources_exist_in_deploy` |
| Installer staging split (fabric fatal; heartbeat loud-not-fatal) | `tests/test_deploy_required_install_failclosed.py::test_installer_fabric_check_stage_is_fatal_not_swallowed`, `tests/test_deploy_required_install_failclosed.py::test_installer_eth_heartbeat_stage_is_loud_but_not_fatal` |
| I9 (stray broker: detected, not killed, deduped) | `tests/test_deploy_stray_broker_detected.py::test_reconcile_detects_a_stray_broker`, `tests/test_deploy_stray_broker_detected.py::test_reconcile_does_not_kill_the_stray`, `tests/test_deploy_stray_broker_detected.py::test_reconcile_dedups_a_persistent_stray` |
| I10 (per-user daemon-start fail-closed; status echo not swallowed into the verdict) | `tests/test_deploy_user_daemon_failclosed.py::test_daemon_start_failure_aborts_before_done`, `tests/test_deploy_user_daemon_failclosed.py::test_daemon_status_verdict_not_swallowed` |
| I11 (stop-before-replace; not symlink-gated; first install proceeds; broker restart on upgrade) | `tests/test_install_modes.py::test_reinstalling_stops_the_daemon_before_replacing_its_venv`, `tests/test_install_modes.py::test_the_stop_is_not_gated_on_the_bindir_symlink`, `tests/test_install_modes.py::test_a_first_install_with_nothing_to_stop_still_proceeds`, `tests/test_install_modes.py::test_broker_install_restarts_so_an_upgrade_takes_effect` |
| I12 (nothing under $HOME; own venv, never user-site; machine-local base) | `tests/test_install_modes.py::test_per_user_install_writes_nothing_under_home`, `tests/test_install_modes.py::test_per_user_install_owns_its_venv_and_never_uses_user_site`, `tests/test_install_modes.py::test_venv_and_cli_land_under_the_machine_local_install_base` |
| I13 (one daemon environment from every start path; nothing persisted) | `tests/test_install_modes.py::test_the_install_persists_no_authority_profile`, `tests/test_install_modes.py::test_the_daemon_env_declares_no_rung_policy`, `tests/test_install_modes.py::test_start_fg_gets_the_same_environment_as_start`, `tests/test_install_modes.py::test_the_per_user_daemon_health_gates_like_any_other` |
| Cron contract (none installed; no self-update; stale entries removed) | `tests/test_install_modes.py::test_no_cron_entry_is_installed`, `tests/test_install_modes.py::test_the_self_update_command_is_gone_not_merely_unscheduled`, `tests/test_install_modes.py::test_a_stale_self_update_cron_is_removed` |
| Branch defaults to `main` everywhere; no retired-branch default | `tests/test_deploy_default_branch.py::test_broker_installer_defaults_branch_to_main`, `tests/test_deploy_default_branch.py::test_user_installer_defaults_branch_to_main`, `tests/test_deploy_default_branch.py::test_no_in_repo_default_tracks_anything_but_main` |
| Unit render: unset knob → no line; set knob reaches the unit; existing render preserved | `tests/test_apply_host_config_render.py::test_seam_preserves_the_existing_render`, `tests/test_apply_host_config_render.py::test_eth_heartbeat_unset_renders_no_line`, `tests/test_apply_host_config_render.py::test_eth_heartbeat_set_reaches_the_unit`, `tests/test_apply_host_config_render.py::test_reset_floor_knobs_unset_render_no_line`, `tests/test_apply_host_config_render.py::test_reset_floor_knobs_set_reach_the_unit`, `tests/test_apply_host_config_render.py::test_expected_chips_unset_renders_no_line`, `tests/test_apply_host_config_render.py::test_expected_chips_set_reaches_the_unit`, `tests/test_apply_host_config_render.py::test_cold_rung_knobs_unset_render_no_line`, `tests/test_apply_host_config_render.py::test_cold_rung_knobs_set_reach_the_unit`, `tests/test_apply_host_config_render.py::test_ubb_reset_force_off_unset_renders_no_line`, `tests/test_apply_host_config_render.py::test_ubb_reset_force_off_reaches_the_unit`, `tests/test_apply_host_config_render.py::test_prejob_dispatch_unset_renders_no_line`, `tests/test_apply_host_config_render.py::test_prejob_dispatch_set_reaches_the_unit`, `tests/test_apply_host_config_render.py::test_eth_check_python_unset_renders_no_line`, `tests/test_apply_host_config_render.py::test_eth_check_python_reaches_the_unit` |
| Unit never ordered after `tenstorrent-hugepages.service` (boot ordering cycle) | `tests/test_apply_host_config_render.py::test_the_unit_never_orders_after_the_vendor_hugepages_service` |
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

**Unanchored (the claim stands on the script):** I2 (wheel/venv from this tree —
`install-tt-device-broker.sh`); the installer's flow ordering and lock opt-in; Galaxy detection
from `tt-smi` board_type; `/etc/default` write + `ensure_default` backfill and unit-read
fallbacks; `DEVICE_GROUP` preservation; autoupdate flock
serialization, sha short-circuit, dist-info recovery, eth-venv provisioning (fail-open);
reconcile steps 1–5 other than stray detection (revive predicate, socket perms, symlink repoint,
lock drift, log aging, per-user wiring cadence); the validator build flow and atomic flip
(`install-fabric-validator.sh`); the crash-recorder installer and `tt-crash-recorder.py`
capture logic; login-heal; client-setup's config-file writes; timer schedules (unit files).
