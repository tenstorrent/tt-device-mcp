# tt-device-broker (Phase B): root arbiter + privsep + bare-metal block

Phase A made identity unspoofable (SO_PEERCRED) and the reset gate safe, but jobs
still ran as the daemon's uid and nothing physically prevented a user from
bypassing the MCP with bare-metal `pytest`. Phase B closes both:

- **Privsep.** When the broker runs as root (and `TT_DEVICE_MCP_PRIVSEP=1`), each
  job is launched via `systemd-run --scope --uid=<submitter> --slice=ttdev.slice`
  with `SupplementaryGroups=ttdev` — it runs as the real user, in their workspace,
  in a transient cgroup scope granted the device.
- **Bare-metal block (DAC).** A udev rule sets `/dev/tenstorrent/*` to
  `0660 root:ttdev`. Admitted jobs get `ttdev` as a supplementary group and can
  open the device; interactive shells (whose users are **not** in `ttdev`) are
  denied by plain unix permissions. The cgroup `DevicePolicy=closed` +
  `DeviceAllow` is defense-in-depth on top.

## Install (once per host, by a sudoer)

```bash
sudo deploy/install-tt-device-broker.sh
# or, if the launcher isn't on root's PATH:
sudo deploy/install-tt-device-broker.sh --device-mcp /path/to/tt-device-mcp
```

This creates the `ttdev` group, installs the udev rule + the systemd unit, and
enables + restarts the broker (a restart, so re-running it swaps in the new venv). Clients connect to `/run/tt-device-broker/broker.sock`
(auto-discovered, or override with `TT_DEVICE_MCP_SOCKET`). The MCP client spawns
the bare binary; with a piped stdin it runs as the stdio adapter:

```bash
tt-device-mcp    # piped stdin -> stdio MCP adapter; tty -> CLI help
```

**Never add interactive users to `ttdev`** — that re-opens bare-metal access.

## What is verified vs. what needs on-box validation

Unit-tested (no hardware): the privsep gating + `systemd-run` argv construction
(`tests/test_privsep.py`), and that privsep defaults OFF so the daemon behaves
exactly as Phase A unless explicitly enabled as root.

**Not yet validated on hardware** (requires root + a Tenstorrent host) — verify
before relying on it:

1. **udev match.** Confirm `SUBSYSTEM=="tenstorrent"` is the right key for this
   driver: `udevadm info -a -n /dev/tenstorrent/0`. Adjust the rule if the
   subsystem/kernel name differs, then check `ls -l /dev/tenstorrent/` shows
   `crw-rw---- root ttdev`.
2. **DeviceAllow spelling.** `DeviceAllow=/dev/tenstorrent/* rw` relies on systemd
   glob expansion; on older systemd you may need an explicit per-node path or a
   `char-<major>` class. Override via `TT_DEVICE_MCP_DEVICE_ALLOW`. (The DAC block
   in (1) is the primary guarantee; this is belt-and-suspenders.)
3. **Bare-metal actually blocked.** As a normal user (not in `ttdev`):
   `python -c "open('/dev/tenstorrent/0')"` must raise `PermissionError`, while a
   job submitted through the broker opens it fine.
4. **Kill path.** Confirm the existing `killpg(os.setsid)` cleanup still reaps the
   job under the `systemd-run --scope` wrapper; if a scope lingers, switch the
   kill path to `systemctl stop` the transient unit.

## Slurm

`deploy/install-tt-device-broker.sh` stages both hooks into `/opt/tt-device-broker/slurm/`;
`apply-host-config.sh` (the autoupdate/reconcile path, which never re-runs the installer) stages
them too, so a host that updates itself still receives hook changes. Point `slurm.conf` there:

```
Prolog=/opt/tt-device-broker/slurm/prolog.sh
Epilog=/opt/tt-device-broker/slurm/epilog.sh
PrologFlags=Alloc
PrologEpilogTimeout=900
SchedulerParameters=nohold_on_prolog_fail
```

- **`PrologEpilogTimeout` must exceed the CLI's client timeouts, not the broker's own step
  deadlines.** The hooks run `tt-device-mcp pre-step`/`post-step`, and what can kill the *script*
  before the broker even replies is the CLI's own HTTP client timeout — `TT_DEVICE_MCP_PRE_STEP_
  DEADLINE_SEC` (default 120) plus a 60 s margin for the prologue, `TT_DEVICE_MCP_POST_STEP_
  DEADLINE_SEC` (default 600) plus the same margin for the epilogue — not the broker-side deadline
  by itself. Below the client timeout, Slurm kills the script and the node drains with nothing
  said about what was slow; above it, the broker returns `inconclusive` and the reason is in the
  log.

  **The CLI reads this env var from its own process, not from the broker — the two do not share
  an environment.** The broker only ever sees the unit's own `Environment=` lines
  (`apply-host-config.sh`); the hooks run under slurmd, which builds Prolog/Epilog a fresh
  environment rather than propagating slurmd's own service environment — so the hooks' env
  comes from wherever THEY put it, not from wherever slurmd itself got it. `prolog.sh` and
  `epilog.sh` each `export` their own deadline var (`TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC`,
  `TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC`) immediately after `.`-sourcing
  `/etc/default/tt-device-broker` — which itself uses plain `KEY=value` with no `export` — so a
  value set in that ONE file reaches the CLI child each hook execs. The export is a no-op when
  the var is unset (nothing reaches the CLI's environment, and the CLI falls back to its own
  default), so leaving the file alone changes nothing.

  Raising the deadline is only real when the same value reaches **both** sides: set the var in
  `/etc/default/tt-device-broker` for the hooks/CLI side, *and* add it to the broker's own unit
  via a systemd drop-in (`Environment=`) for the server side — the hooks' `export` only carries it
  to the CLI, not to the broker process the CLI talks to over the socket. Raise only one side and
  they fall out of sync: raise the broker's alone (the drop-in, nothing in
  `/etc/default/tt-device-broker`) and the CLI still gives up at the old, shorter timeout and
  reports a transport failure for a step the broker is still resolving — the exact failure this
  margin exists to prevent. Raise the file's side alone and nothing breaks, but nothing is gained
  either — the broker still caps the gate at its old deadline, so the CLI's larger timeout never
  gets exercised. Lower the broker's deadline env vars rather than raising the site's
  `PrologEpilogTimeout` if you need a tighter budget, keeping the same both-sides rule in mind.
- **`nohold_on_prolog_fail` is not optional.** Without it, a prologue failure requeues the job
  *held*, and every wedged device becomes a ticket.
- **`PrologFlags=Alloc`** runs the prologue when the allocation is granted rather than at the
  first job step. Without it the health check happens later than you think it does.
- **No `HealthCheckProgram`.** slurmd kills it at 60 s, which cannot contain a `tt-smi -r`
  (~15 s) plus a fabric traffic pass (45–100 s), let alone a bridge or tray rung. A periodic
  SIGKILL mid-reset wedges the device it was meant to check.
- **Un-draining after a repair is manual.** Drain survives a reboot, so a node the broker's
  reboot rung fixed comes back still drained with a stale reason. Return it with
  `scontrol update NodeName=<node> State=RESUME`.
- **The hooks resolve `tt-device-mcp` themselves — PATH is a fallback, not the primary source.**
  Slurm's own docs: "these programs do not have a search path set." Because these hooks run as
  root, that absence is a feature worth keeping: trusting PATH first would let a directory ahead
  of the real install on a root process's PATH run as `tt-device-mcp` in its place, however it got
  there. Each hook first tries `${TTDEV_VENV:-/opt/tt-device-broker/venv}/bin` via
  `/etc/default/tt-device-broker` (written by the installer — the same lookup `deploy/tt-smi-ro.sh`
  uses), and only falls back to `command -v tt-device-mcp` for a per-user or dev install with no
  system config at all, failing loudly on stderr, naming the missing binary, if neither resolves.
  slurmd logs hook stderr, so that message — not a bare 127 — is what tells an operator "the CLI
  was missing" instead of "the mesh was sick."
- **The Epilog keys off `SLURM_JOB_DERIVED_EC` when it is non-zero, not just
  `SLURM_JOB_EXIT_CODE`.** The latter is only the wrapping batch script's own exit status and can
  read 0 even when a step genuinely failed (multiple `srun` steps, `|| true`, a trap); the former
  is the highest exit code across every step in the job, and is what should decide whether the
  fabric traffic pass runs. Outside a real Slurm Epilog neither variable is set, and both default
  to 0 — a deliberate fail-open for a manual invocation, where a script that was never a Slurm
  step should not pay for a fabric pass.
- **Precondition: the reclaim's authority rests on Slurm owning all device access on this host.**
  `post-step` SIGTERMs, then SIGKILLs, every tenant-uid process still holding the device once its
  own scan begins — it has no way to tell "a straggler of the allocation that just ended" apart
  from "some other tenant's job" beyond that. On a locked-down host where the queue (or Slurm
  itself) is the only path to the device, that is exactly the population there is to reclaim. On a
  **cooperative (unlocked) host** — a shape this repo supports (see "Bare-metal block" above) —
  nothing stops a user from holding the device outside of Slurm entirely, and `post-step` will
  SIGKILL that holder too, whether or not it ever belonged to the Slurm allocation. Do not point
  `slurm.conf` at these hooks on a cooperative host without understanding that tradeoff.
