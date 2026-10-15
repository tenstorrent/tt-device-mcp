<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Testing

Scope: the testing discipline — what the unit suite guarantees, the conftest seals and the
spawn tripwire, how tests for new behavior are written, the four validation levels, and CI
parity. The suite's subject is the whole broker; this spec's subject is the suite.

## Purpose

The suite (1131 collected tests across 42 files under `tests/`, per
`.venv/bin/pytest --collect-only -q`) is the executable form of the
invariants in specs 01–06: each spec's `## Test anchors` table names the pytest node ids that
pin its claims. For that to mean anything the suite must be runnable anywhere — a developer
box with a live Galaxy mid-incident, a root CI runner, a container with no `/dev/tenstorrent`
— and must be **incapable** of touching silicon, BMC, host power, or host service state.
`tests/conftest.py` enforces that by construction, not by convention: two autouse fixtures
seal every path from a test to the real host, and a spawn-layer tripwire fails any test that
gets through anyway.

## Invariants

- **I1** — The suite is hardware-independent by default. Every test that does NOT carry the
  `device` marker (I8) runs identically on a box with 32 live chips and on a runner with no
  Tenstorrent hardware at all: the autouse `isolate_device_state` fixture points every
  device-facing read and every host-writing path at per-test temp dirs, so no such test's
  verdict depends on which machine it lands on. What it actually seals:
  - **Sysfs / PCI**: `pci.SYSFS_CLASS_DIR` (the `/sys/class/tenstorrent` reader),
    `pci.PCI_DEVICES_DIR` (the real `/sys/bus/pci/devices`, read at import time so an env var
    would arrive too late), and `server.TT_DEV_DIR` (the real `/dev/tenstorrent`, read fresh by
    `_present_chip_indices()` on every gate pass) are all patched to empty temp dirs; an empty
    sysfs also makes `heartbeat_supported()` False unless a test populates the dir itself. A
    build host with real hardware and CI with none must read identically — without this,
    `device_health_gate` took its "no /dev/tenstorrent devices present" skip branch only on a
    device-less box, and 16 tests in `tests/test_slurm_steps.py` had never actually exercised
    the gate they claimed to (11 failed outright on CI; 5 more passed for a reason unrelated to
    what they asserted). A test that wants chips present populates the temp dir and points
    `TT_DEV_DIR` at it itself (the pattern `tests/test_device_safety.py`'s gate tests already
    use, `tests/test_slurm_steps.py`'s `_present_chips` helper now too).
  - **Fabric probe**: `TTDEV_VALIDATOR_ROOT` → empty temp dir; `TTDEV_FABRIC_BIN`,
    `TTDEV_FABRIC_RUNTIME_ROOT`, `TTDEV_FABRIC_DESCRIPTOR`, `TT_DEVICE_MCP_FABRIC_CHECK_CMD`
    all cleared; `TTDEV_FABRIC_CACHE` redirected (its fallback is a hardcoded real
    `/var/cache` path that `build_command()` would mkdir).
  - **Eth-heartbeat probe**: `TT_DEVICE_MCP_ETH_HEARTBEAT_CMD`, `TTDEV_ETH_CHECK_PYTHON`,
    `TTDEV_ETH_VENV`, `TTDEV_ETH_CHECK_PROBE`, `TTDEV_ETH_CHECK_TIMEOUT`,
    `TTDEV_ETH_CHECK_ARMED`, `TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN` all cleared;
    `TTDEV_ETH_CHECK_CACHE` redirected; `eth.DEFAULT_ETH_VENV_PYTHON` (hardcoded, no env
    override) patched to a path that cannot exist; `TTDEV_ROOT` and `eth._REPO_ROOT`
    sandboxed so neither the real broker install nor this checkout's own
    `deploy/tt-device-eth-heartbeat-probe.py` can resolve. The rule is by construction: no
    test may ever resolve a real, executable probe, not "this box happens not to have one".
  - **Reset/chip env**: `TT_DEVICE_MCP_RESET_ARGS`, `TT_DEVICE_MCP_RESET_MODE`,
    `TT_DEVICE_MCP_EXPECTED_CHIPS` cleared (all read live, so an operator's shell leaks
    straight into argv a test builds).
  - **Durable state**: `health.HEALTH_DIR` → temp; a fresh `ServerFsm` per test, on its own
    temp file, reconciled to HEALTHY (a test that wants a degraded device says so itself);
    `TT_DEVICE_MCP_TEXTFILE_DIR` → temp (the default is the real node-exporter dir, read
    fresh per write).
  - **Gate history**: every module-global the gate's decisions read — `last_reset_*`,
    `last_fabric_check_monotonic`, `last_job_end_monotonic`, `owner_submit_times`, the hold
    and skip-journal latches, the sampler's blackout strike counter, board-type caches — is
    reset per test so one test's outcome never steers the next test's gate.
  - **Host rungs**: `TT_DEVICE_MCP_AUTO_REBOOT=0`, `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0`
    (both default ON, so they are pinned off, never merely deleted),
    `TT_DEVICE_MCP_AUTO_UBB_RESET` cleared; `_fire_host_reboot` and `_fire_ubb_reset` are
    replaced with functions that raise `AssertionError` — loud tripwires, so a test that
    reaches a real fire fails instead of taking the box down. `shutil.which("tt-smi")` answers
    None: it is a device tool, so an unmarked test must not find one, and preflight verdicts keyed
    on it otherwise differ between a developer box and CI. A test whose subject needs it present
    says so by patching `srv.shutil.which`. The four `privileges` probes
    (`is_root`, `has_systemd`, `can_setpci`, `can_ipmi`) are pinned True, so a rung's arming
    does not follow whether the suite ran as root on a box with setpci and a BMC; a test about
    an unprivileged daemon patches them False. `_bmc_reachable` is
    pinned True (the default-armed shape the ladder tests assert) and
    `recovery_mechanism.scope_active` pinned False (unstubbed it asks the real host's
    systemd, which failed three sampler tests on a box mid-incident and nowhere else).
- **I2** — A spawn tripwire hard-fails any test that would hand a device-touching command
  to the kernel. The seam is the process-spawn layer — `subprocess.Popen.__init__` and
  `asyncio.create_subprocess_exec` are both wrapped — rather than each caller, so a caller
  written after the fixture cannot slip past. Two lists, basename-matched anywhere in the
  argv:
  - **Never spawnable, marker or not**: `setpci`, `ipmitool`, `reboot`, `shutdown`,
    `poweroff`. These power-cycle the box, reboot it, or write PCI config space directly. No
    marker lifts them; the only way to test a path that composes one is I3.
  - **Spawnable only under the `device` marker**: `tt-smi` and `systemd-run`. `tt-smi` is both
    the chip enumerator and the ladder's warm rung (`tt-smi -r`), the one destructive recovery
    step a device test may perform (I8). `systemd-run` is how that reset is actually issued —
    a transient `--scope … --collect` unit, so a broker restart cannot kill the reset partway.
    Permitting the wrapper is safe because the check is token-wise over the whole argv: a
    `systemd-run` wrapping anything in the never-list is refused on that token regardless, and
    `--collect` means the unit is reaped when it exits rather than leaked past the run.

  `systemctl`'s mutating verbs (`stop`, `start`, `restart`, `reload`, `enable`, `disable`,
  `mask`, `kill`) are refused, with exactly one exception: under the `device` marker, a verb
  naming **nothing but units in `DEVICE_POLLER_SERVICES`** is permitted. A real reset must land
  on a quiesced bus and quiescing *is* `systemctl stop` against those units, so refusing it
  would leave a device test able to exercise only an unquiesced reset — the configuration
  production never runs, and the more dangerous one on real silicon. The exception reads the
  broker's own constant rather than a literal, so it cannot drift wider than the set the broker
  actually manages; notably it excludes `tt-device-broker.service`, which a test stopping would
  pull the host out from under itself. Read-only queries (scope liveness) pass either way,
  because gate paths genuinely need them.
- **I3** — A test that needs a forbidden command MUST stub the function that composes the
  argv and assert on the argv itself — never on a mocked spawn. The established seams:
  `Recovery.reset_argv` (`health/recovery/__init__.py`), `_ubb_reset_argv`
  (`health/recovery/stages/ubb_tray.py`), `systemd_run_prefix` (`privsep.py`), and the
  stage fire fns (`_fire_host_reboot` in `stages/host_reboot.py`, `_fire_power_cycle` in
  `stages/power_cycle.py`, `_fire_ubb_reset` in `stages/ubb_tray.py` — a test exercising a
  fire path re-patches the fire itself).
- **I4** — The suite MUST pass device-free, with every `device`-marked test skipped and no
  other test skipped. Passing on a device box proves nothing about CI; the docker check under
  Validation level 1 is the proof, and CI (section CI) runs the suite on runners with no
  Tenstorrent hardware at all. A device-marked test may never be the only coverage of a
  behavior that can be proved device-free — CI would report green having skipped it.
- **I5** — No test touches the live per-user state dir. `TT_DEVICE_MCP_STATE_DIR` /
  `TT_DEVICE_MCP_INSTALL_DIR` are the override seams (spec 06 I2), and the health/textfile
  seals in I1 cover the in-process paths; tests of the installers and daemon set the
  overrides explicitly.
- **I6** — Importing `tt_device_mcp.server` never touches the device. The boot flow
  (`boot_broker()`) is construction only and runs off import; conftest calls it exactly once
  at collection so the seams (`srv.health_monitor`, `srv.recovery_mechanism`, the two
  platform aliases, `srv.sampler`) exist for tests to monkeypatch. `probe_platform` stays
  off: a test that cares about the platform declares `TT_DEVICE_MCP_RESET_MODE` or drives
  `resolve_platform` itself.
- **I7** — Process globals are isolated per test. The autouse `isolate_process_globals`
  fixture restores `os.environ` and `sys.argv` around every test, because code under test
  (`cmd_daemon_start`, `start-fg`) writes them directly and `monkeypatch` cannot undo a
  write to a name it did not set.
- **I8** — Real hardware is reachable only under the `device` marker, and only for the
  non-destructive-to-the-host subset. A bare-metal, privsep, systemd-managed broker cannot be
  proved correct entirely in a sandbox; `@pytest.mark.device` is how a test says it needs the
  silicon. The contract:
  - **Opt-in and self-skipping.** A marked test skips unless `/dev/tenstorrent` exists, and
    skips regardless when `--no-device-tests` is passed (the escape hatch for a quiet
    `pytest -q` on a shared box). An unmarked test's behavior is byte-for-byte unchanged by
    this invariant on every host.
  - **Lifted under the marker** — the seals whose only purpose was hiding real hardware:
    sysfs/PCI/`TT_DEV_DIR` and `TT_DEVICE_MCP_EXPECTED_CHIPS`; the fabric and eth-heartbeat
    probe-resolution env (so the real probes resolve and run); `TT_DEVICE_MCP_RESET_ARGS` /
    `TT_DEVICE_MCP_RESET_MODE`; `device_holders._read_proc_starttime` (so reclaim reads real
    `/proc/<pid>/stat` identity); the four `privileges` probes, `_bmc_reachable` and
    `recovery_mechanism.scope_active` (under the marker they should report the host's truth,
    not a pinned shape).
  - **Never lifted — durable broker state.** `health.HEALTH_DIR`, the per-test `ServerFsm` on
    its own temp file, and `TT_DEVICE_MCP_TEXTFILE_DIR` stay sandboxed, as does every gate
    module-global. A device test must never write the running broker's journal, share its FSM
    record, or publish to the real node-exporter dir: that is test hygiene, not hardware
    independence, and mixing a test's health history into production's is unrecoverable. The
    probe *caches* are the exception to the exception — they are build artifacts a real probe
    needs to run at usable speed, so they point at one session-scoped temp dir shared by all
    device tests rather than a fresh per-test one.
  - **Never lifted — the destructive rungs.** `TT_DEVICE_MCP_AUTO_REBOOT=0` and
    `TT_DEVICE_MCP_AUTO_POWER_CYCLE=0` stay pinned off, `TT_DEVICE_MCP_AUTO_UBB_RESET` stays
    cleared, and `_fire_host_reboot` / `_fire_ubb_reset` stay `AssertionError` tripwires. The
    ladder above the warm rung takes the whole host or a whole tray down; nothing in a pytest
    run may reboot or power-cycle the machine it is running on. `tt-smi -r` is the ceiling.

## What the suite guarantees

The index is the `## Test anchors` table in each of specs 01–06; this map says which files
carry which spec's anchors. Approximate collected counts in parentheses.

| File(s) | Pins | Spec |
|---|---|---|
| `test_device_safety.py` (450) | The safety corpus: gate hold/dirty/verify semantics, idle and fabric relift, stuck-hold escalation, galaxy floor, ledger/governor, bridge and tray rungs, dead-chip sampler, burst cap, cooldown, exec gating, preflight | 01, 03, 04, 05, 06 |
| `test_server.py` (70) | Job lifecycle, ids and wraparound, activation script / env resolution, clean-device gate ladder, MCP progress streaming, synthetic socket host | 01, 02 |
| `test_readopt.py` (25) | Restart survival: scope re-adoption, recovered deadlines and exit codes, jobexit dir redirection and perms | 01, 05, 06 |
| `test_recent_jobs.py` (37) | Job-log footer format, recent-history parsing, id reservation, kill audit | 01, 05 |
| `test_socket_transport.py` (8), `test_stdio_shim.py` (3), `test_cli.py` (34) | Unix-socket JSON-RPC, peer-uid scope, shim socket resolution, CLI entry behavior; `test_cli.py` also carries the lock and smi-wrapper anchors | 02, 05 |
| `test_authz.py` (7), `test_peercred.py` (8), `test_privsep.py` (14), `test_privsep_guard.py` (3) | SO_PEERCRED parse, identity over self-report, run-as-submitter prefix, refuse-never-root-fallback | 05 |
| `test_fsm.py` (20), `test_health_core.py` (13), `test_monitor.py` (9), `test_health_surface.py` (7) | Durable FSM record and boot merge, `HealthState` evidence round-trip, probe-pass ordering/short-circuit, facade-only import surface | 03 |
| `test_boot_platform.py` (7), `test_rung_arming.py` (9) | Boot-time platform commit (and import purity), eth-rung arming rules | 03, 04 |
| `test_eth_probe.py` (40), `test_fabric_probe.py` (8), `test_eth_heartbeat_probe.py` (21) | Exit-code laundering contracts (ok/frozen/cannot-check), hung reads are skips, the eth-heartbeat probe script's own verdicts | 03 |
| `test_broker_telemetry.py` (8) | Sampler snapshot/exposition: tri-state fabric verdict, last-pass-not-running-total, crash-tolerant snapshot | 03 |
| `test_reset.py` (56), `test_reset_gate.py` (18), `test_device_holders.py` (18), `test_recovery_select.py` (3), `test_ubb_reset_launch.py` (3) | Reset argv per platform, scoped execution and quiesce, holder scan and reset gate (foreign deny, fail-closed, force), platform selection, tray-fire tt-smi compat, reclaim pid+starttime identity | 04, 05 |
| `test_metrics.py` (37) | Stage-metrics classification (inapplicable ≠ blocked) and textfile writer behavior | 04, 06 |
| `test_state_paths.py` (10), `test_install_modes.py` (34) | euid-split defaults, env overrides beat both defaults, install/state base split, per-user daemon gate-on default | 06 |
| `test_deploy_*.py` (9 files, 42), `test_apply_host_config_render.py` (16) | Deployment-script fail-closed statements (via `deploy_helpers`) and host-config rendering | 08 |
| `test_slurm_steps.py` (54) | Externally-driven `pre-step`/`post-step` routes (deadline, verdict, reclaim/audit, the external-step device reservation, the whole-route reclaim+gate deadline), the `tt-device-mcp pre-step`/`post-step` CLI verbs, and the Slurm Prolog/Epilog hooks (resolution order, exit-code contract, installer/autoupdate staging, deadline-var export) | 02, 03, 05, 07, 08 |

The guarantee is directional: a change that breaks a spec invariant breaks a named anchor.
A change that adds an invariant without an anchor has not finished (see next section).

## Writing tests for new behavior

**Where it belongs.** Extend the file that owns the subsystem (map above). Gate, hold,
recovery-policy, and sampler behavior goes in `test_device_safety.py`; a new probe gets its
own `test_<probe>_probe.py`; a new deployment-script guarantee gets a
`test_deploy_<claim>.py` using `deploy_helpers`. New spec-level invariants add a row to the
owning spec's `## Test anchors` table with the new node ids.

**Seams and fixtures.**
- Monkeypatch the `server.py` module globals — `srv.health_monitor`,
  `srv.recovery_mechanism`, `srv.galaxy_recovery`, `srv.per_target_recovery`, `srv.sampler`,
  `srv.select_recovery` — which spec 03 documents as the deliberate monkeypatch seams;
  server-state flows into the subsystem through late-bound lambdas (`RecoveryDeps`), so a
  patch on the module name keeps taking effect.
- `patch_recovery(monkeypatch, name, fn)` (conftest) patches a platform-invariant `Recovery`
  method (`_verify_device`, `_reset_and_verify_device`, `_recover_isolated_chips`,
  `_verify_device_after_reset`) on BOTH persistent instances, because a test cannot know
  which platform `select_recovery` hands the gate for its setup.
- `patch_health_event(monkeypatch, fn)` patches all three modules that hold their own
  imported `health_event` binding (`server`, `health.recovery`, `health.recovery.galaxy`) —
  patching one copy never touches another's.
- Device state goes through the FSM, never raw flags: `fsm_dirty(srv, detail, why=...)` and
  `fsm_healthy(srv)` (conftest) are the replacements for the old `device_dirty` attribute
  pokes. Fake probe evidence is a plain dict folded through `HealthState.from_evidence`.
- `health_deps` (fixture) builds a `RecoveryDeps` late-bound onto `srv`'s own globals, so a
  `Recovery`/`HealthMonitor` constructed in a test still sees the test's monkeypatches.
- `clear_job_state` (fixture, not autouse) resets the jobs dict, queue, and async
  primitives for runner tests.
- Shell-script tests use `tests/deploy_helpers.py`: `_statements()` joins
  backslash-continued lines into logical statements, `_one(script, *needles)` asserts
  exactly one statement matches — tests assert on the statement, they never execute the
  script against the host.

**Tripwire rules a new test MUST obey.**
- Never spawn a forbidden program (I2) — stub the argv-builder (I3) and assert on the argv.
- A test of the reboot / power-cycle / tray-reset path re-patches the `_fire_*` fn itself;
  the conftest defaults are deliberate `AssertionError`s.
- A test that needs a platform or machine type sets `TT_DEVICE_MCP_RESET_MODE`, or sets
  `_board_types` / `_glx_cache` on `srv.health_monitor` — never relies on a probe.
- A test that populates sysfs/PCI builds its own temp dir; the conftest default is empty,
  and a scenario-specific `monkeypatch.setattr` in the test body overrides it cleanly.

**Naming.** Test names are behavior sentences read as claims:
`test_a_frozen_eth_verdict_is_held_not_reset`,
`test_reset_runs_in_its_own_systemd_scope`. Name the behavior and its stakes, not the
function under test. Classes (`TestResetGateDeny`, `TestPeerUidScope`) group variants of
one contract.

**Parametrization.** `pytest.mark.parametrize` for truth tables and classification
contracts (`test_exit_laundering[77-None]`,
`test_gate_fabric_pass_respects_the_stale_interval[540-False]`). Parametrized ids appear
verbatim in spec anchor tables, so changing parameter values renames anchors — update the
owning spec's table in the same change. `asyncio_mode = "auto"` (pyproject), so async tests
are plain `async def` with no marker.

## Validation levels

Four levels, in order. Never skip to level 4 — a broken install looks exactly like a broken
device from the far end of a job log. Deployment mechanics (installers, reconcile timer,
systemd units) are spec 08; this section is the validation discipline.

### 1. Unit suite (no device)

```bash
pytest -q                    # on a device box, add --no-device-tests for level 1 exactly
```

Must pass anywhere, per I1–I7. Passing on a device box proves nothing about CI; verify
device-free before claiming CI is green:

```bash
sudo docker run --rm -v /localdev:/localdev -e HOME=/tmp/cihome python:3.10 bash -lc '
  mkdir -p /run/systemd/system          # GitHub runners are VMs and have systemd; containers do not
  cp -r <repo> /tmp/src && cd /tmp/src && rm -rf .venv
  python -m venv /tmp/civenv && /tmp/civenv/bin/pip install -q -e ".[dev]"
  /tmp/civenv/bin/pytest -q'
```

Two environment facts this pins, both learned the hard way: several reset tests need
`/run/systemd/system` to exist (they fail loudly, not silently, if it does not), and the
image needs `git` for setuptools_scm — `python:3.10-slim` has neither.

### 2. Device suite (real silicon, in-process)

```bash
# unprivileged: topology, probe resolution, real /proc holder identity and reclaim
pytest -m device -q

# privileged: adds the warm reset rung and a complete holder scan. PATH must carry the broker
# venv's bin exactly as its unit does, or the reset resolves no tt-smi and exits 1
sudo -E env PYTHONDONTWRITEBYTECODE=1 PATH="/opt/tt-device-broker/venv/bin:$PATH" \
  .venv/bin/pytest -p no:cacheprovider -m device -q
```

The `device`-marked tests of I8: real chip enumeration, the real fabric and eth-heartbeat
probes, real `/proc` holder identity, real reclaim signalling, and the warm reset rung
(`tt-smi -r`). Still in-process — no installer, no systemd unit, no privsep — so a failure here
is broker logic against real hardware, not a deployment problem. That separation is the reason
this sits below level 3. Stop `tt-device-broker` and `tt-device-reconcile.timer` first: two
things managing one device is not a configuration any result from this level describes.

Three tests need more than the marker, and each says which in its skip reason rather than
passing silently:
- **the warm reset** needs a *complete* holder scan, which needs root — unprivileged, 448+
  processes are unreadable and the reset gate fails closed (04 I7), correctly and permanently;
- **the fabric traffic pass** needs its validator tree writable (the validator writes into its
  own `generated/`), so unprivileged it returns rc=-6 and no verdict;
- **the holder/reclaim tests** need a holder at a real tenant uid, so under `sudo` they borrow
  `SUDO_UID` — a root-owned holder sits below `MIN_TENANT_UID` and is invisible to the very
  code they exercise, which is how they once passed while proving nothing.

This level **perturbs the box**: it resets chips and signals device holders. Run it where you
own the hardware. It cannot reboot or power-cycle the host — I8 pins those rungs off and keeps
their fires as tripwires — so the worst case is a chip reset, which is what the broker does for
a living.

`pytest -q` on a device box runs levels 1 and 2 together, which is the intended default: the
marker exists so CI skips these, not so a developer with hardware has to remember a flag.
`--no-device-tests` forces the skip when you want level 1 exactly.

### 3. Bare-metal system broker

```bash
sudo ./install.sh                       # builds a wheel from THIS tree, not a clone
systemctl is-active tt-device-broker
grep -E "BOOT platform|RUNG INVENTORY|PREFLIGHT" /var/log/tt-device-broker/server.log | tail

# prove the broker is serving THIS tree — "builds from this tree" is a claim about the
# installer, not about what the running process imported
diff /opt/tt-device-broker/venv/lib/python3*/site-packages/tt_device_mcp/server.py \
     src/tt_device_mcp/server.py && echo "broker == tree"   # no sudo: installer chmods a+rX
```

Expect `BOOT platform: galaxy|per-target` before any PREFLIGHT line — the boot flow resolves
the host first. `/health` reports `fsm_state: recovering, fsm_why: startup_unverified` until
the startup pass finishes; wait for `healthy` before reading anything into a job result.

Then run a real job through the queue and read the gate lines in its log:

```bash
cd <workspace-root>                     # NOT the repo: the CLI resolves <cwd>/tt-metal/python_env
tt-device-mcp run "echo smoke; hostname; id -un"
```

`id -un` is the privsep check: under `TT_DEVICE_MCP_PRIVSEP=1` it must print the submitter,
not `root`.

**Stop `tt-device-reconcile.timer` — twice — when testing a branch.** It runs autoupdate
every 60s, which re-pulls the tracked branch and reinstalls over your build. Stop it before
the install so it cannot clobber mid-test, and again *after*:
`deploy/install-tt-device-broker.sh` ends with `enable --now` on it and hard-fails if it is
not active, so every install re-arms it. Leave a branch build with the timer `inactive` but
still `enabled` — starting it reverts the host within ~60s. (Timer mechanics: spec 08.)

### 4. Per-user daemon in a container

The per-user shape (`deploy/install-user.sh`) is what runs on a single-user box or inside a
container. On an LDAP host the container needs resolvable identity — mounted `/etc/passwd`
does not carry LDAP users (`grep -c "^$USER:" /etc/passwd` → 0 on this class of host):

```bash
mkdir -p /tmp/ttdev-container           # the two redirects below fail without it
{ getent passwd root; getent passwd "$USER"; } > /tmp/ttdev-container/passwd
{ getent group  root; getent group  "$USER"; } > /tmp/ttdev-container/group
```

Mount those as `/etc/passwd` / `/etc/group`, then inside the container:

```bash
<repo>/install.sh                       # its own venv: neither /opt/venv nor tt-metal's has pip
export PATH="/tmp/tt-device-mcp-$(id -u)/bin:$PATH"
tt-device-mcp daemon start
tt-device-mcp run "<job>"
```

`install.sh` picks per-user mode from the uid; `install-user.sh` runs `daemon start` itself
and fails the install if `/health` does not answer (spec 08 owns the installer contract).

Expected here — assert rather than chase:

- **The gate runs.** Health checks are on in both shapes. The probes it can run unprivileged
  (snapshot, heartbeat, PCI presence) all work.
- **`BOOT privilege:` reports what this daemon can execute**, and the rung inventory below it
  names every rung that is OFF with the privilege it lacked. On a container expect
  `root=False`, so bridge reset, tray reset, reboot and power cycle are OFF and `tt-smi -r`
  is the ladder.
- **A multi-chip container warns about the missing fabric validator.** The system installer
  stages it; `install-user.sh` cannot. A mesh the gate cannot prove holds rather than
  admitting tenants.
- **No tt-smi degrades to a serializer**: preflight warns and turns the gate off rather than
  refusing to serve.

`crontab` is absent from the tt-metalium image, so reboot-persistence warns and skips —
irrelevant to a `--rm` container. Drive the container detached (`sleep infinity` +
`docker exec`); an interactive `-it bash` needs a tty and dies with the driver.

**Stop the host broker first.** Two arbiters over one card is the documented footgun — the
container sees `/dev/tenstorrent/<N>` that the system broker is already serializing. Check
other containers holding the same node too
(`docker inspect <name> --format '{{json .HostConfig.Devices}}'`).

### Device-job discipline (levels 3 and 4)

- **Submit from the workspace root**, not `$HOME` — the CLI resolves
  `<cwd>/tt-metal/python_env`; from `$HOME` no tt-metal activation happens.
- **The caller's exports beat the workspace defaults** (`inherited_env`; spec 01 I11). Read
  the job header's `ENVIRONMENT VARIABLES` block on any new host; never assume the defaults
  applied — a profile-exported `TT_METAL_HOME` silently aims the job at a tree you did not
  build.
- **An env file (`-e`) REPLACES the inherited environment wholesale** (priority
  `env_file` > `inherited_env` > defaults), so it must be complete; `PYTHON_ENV_DIR` in it
  picks the venv to activate.
- **Trust the header, not the flag**: `MAX_TIMEOUT_SEC` (1500s) is clamped in `_queue_job`
  (spec 01 I2); the CLI accepts a larger `-t` without complaint and the header then reads
  `TIMEOUT: 1500s`. A run that will not fit gets reshaped, not a bigger number.
- **Read both verdicts**: `STATUS: completed / EXIT CODE: 0` is the job, pytest's own
  `N passed` is the test — both, or it did not pass. Confirm the gate too: pre-job shows
  `WAIT TIME: ~0s` and no fabric pass (spec 03 I12: the traffic pass is never on a
  submitter's clock); a failed job's post-job gate runs the full check including
  `fabric: OK`.
- **Keep the host quiet while a device job is in flight** — no concurrent `pytest -q` on
  the side; a vision demo died to an unattributed SIGKILL with the unit suite running
  concurrently and passed alone. Cause unproven, correlation cheap to avoid.

Workload selection (which model, which demo, mesh-vs-KV-head divisibility, PERF.md
trace-mode comparisons) is operator guidance, not repo contract — it lives with the
end-to-end runbook (CLAUDE.md), not in this spec.

## CI

One workflow: `.github/workflows/test.yml`. On `pull_request`, `push` to `main`, and
`workflow_dispatch`, it runs on `ubuntu-latest` (Python 3.10 matrix): create a venv — the
deployment shape, one venv owning the package — `pip install -e ".[dev]"`, then
`.venv/bin/pytest -v`. The `dev` extra (pyproject) is `pytest>=7.0.0`,
`pytest-asyncio>=0.21.0`, `pytest-timeout>=2.1.0`.

A hang fails fast instead of holding a runner for GitHub's 6 h default: pytest's `timeout =
120` (pyproject) fails any single test after 120 s and prints every thread's stack, and the
test job has `timeout-minutes: 20`. The whole device-free suite runs in a few minutes, so
both limits leave wide margin.

That is the whole pipeline: no device stage, no lint stage, no matrix beyond 3.10. CI
therefore proves exactly I4 — the suite passes device-free on a systemd-bearing VM — and the
docker command under level 1 is the local reproduction of it (the container needs
`/run/systemd/system` faked precisely because runners have systemd and containers do not).
Levels 2–4 are manual; nothing in CI touches hardware.

## Test anchors

The guarantee map needs no anchors — it is an index of the other specs' tables. This spec's
own claims that are pinned:

| Claim | Anchors (pytest node ids) |
|---|---|
| I5 (state dir overridable so tests never touch the live one) | `tests/test_install_modes.py::test_the_state_dir_is_overridable_so_tests_never_touch_the_live_one` |
| I6 (import purity — boot off import) | `tests/test_boot_platform.py::test_importing_the_server_never_touches_the_device` |
| I1 (probe reads sealed sysfs, not silicon) | `tests/test_device_safety.py::test_heartbeat_probe_touches_no_device` |
| I1 (env seal: armed flag never inherited from a stale environment) | `tests/test_rung_arming.py::test_the_armed_flag_is_not_inherited_from_a_stale_environment` |
| I2 (never-list refused even under the marker) | `tests/test_device_marker.py::test_the_never_spawn_list_is_refused_even_under_the_device_marker` |
| I2 (`tt-smi`/`systemd-run` refused unmarked, permitted marked) | `tests/test_device_marker.py::test_the_device_only_list_is_refused_unmarked_and_permitted_marked` |
| I2 (mutating systemctl still refused under the marker) | `tests/test_device_marker.py::test_mutating_systemctl_stays_refused_under_the_device_marker` |
| I2 (quiescing a declared poller is the one systemctl exception) | `tests/test_device_marker.py::test_quiescing_a_declared_poller_is_permitted_only_under_the_marker` |
| I2 (the poller exception cannot launder another unit) | `tests/test_device_marker.py::test_the_poller_exception_does_not_extend_to_other_units` |
| I2 (`systemd-run` cannot launder a never-list payload) | `tests/test_device_marker.py::test_systemd_run_is_permitted_under_the_marker_only_for_what_it_wraps` |
| I8 (the gate reads real `/dev`, never the sandboxed attribute) | `tests/test_device_marker.py::test_the_device_gate_reads_the_real_dev_dir_not_the_sandboxed_one` |
| I8 (no chips, or no dir, means no device) | `tests/test_device_marker.py::test_no_chips_means_no_device`, `::test_an_absent_dev_dir_means_no_device` |
| I8 (the marker is registered, so `-m device` selects it) | `tests/test_device_marker.py::test_the_device_marker_is_registered` |
| I8 (an unmarked test keeps its seals) | `tests/test_device_marker.py::test_this_test_is_not_device_marked` |
| I8 (real silicon: topology, probes, `/proc` identity, reclaim, warm reset) | every test in `tests/test_device_hardware.py` |
| Spec anchors resolve (a renamed or deleted test cannot leave a table asserting coverage) | `tests/test_spec_anchors.py::test_every_spec_anchor_names_a_test_that_exists`, `tests/test_spec_anchors.py::test_every_anchor_points_at_a_test_file_that_exists` |

`tests/test_device_marker.py` is device-free on purpose: it is the CI-side proof of the
marker's own machinery, because the hardware tests it guards skip on CI and can prove nothing
there. That is I4's rule applied to this spec's own invariants.

The seals and the spawn tripwire themselves are conftest fixtures, not tests — they have no
node ids. Their enforcement is structural: they run autouse under every one of the 1131
tests, and a violation is a test failure, which is the strongest anchor available.
