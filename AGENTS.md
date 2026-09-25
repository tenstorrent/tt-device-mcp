# tt-device-mcp

MCP server that lets multiple Claude Code agents and developers safely share one Tenstorrent
device. Core principle: **serialize all device access through a job queue — one job at a time** —
and never dispatch onto a mesh the broker cannot prove fit.

## How to work in this repo — spec-driven

The specs under `specs/` are normative; the ~970-test suite enforces them. Before touching
a subsystem, **read its spec** (index below). To change behavior:

1. Edit the owning spec first — the Invariants/Behavior text states the new intent.
2. Implement against the spec.
3. Anchor every new/changed invariant to a test; update the spec's `## Test anchors` table.
4. Land spec diff + code diff + anchors in one PR.

Full workflow, conventions, and verification bar: [CONTRIBUTING.md](CONTRIBUTING.md).
Intent you cannot establish goes to a filed issue for the maintainer, never silently into
spec text.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Claude Agent 1 │     │  Claude Agent 2 │     │  Developer CLI  │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │ MCP (stdio shim)      │ MCP (stdio shim)      │ REST
         └───────────────────────┼───────────────────────┘
                                 │ unix socket (SO_PEERCRED)
                                 ▼
                    ┌────────────────────────┐
                    │    tt-device-mcp       │
                    │  ┌──────────────────┐  │
                    │  │    Job Queue     │  │
                    │  └────────┬─────────┘  │
                    │   health  ▼  gate      │
                    │  ┌──────────────────┐  │
                    │  │   Job Runner     │  │
                    │  │  (one at a time) │  │
                    │  └────────┬─────────┘  │
                    └───────────┼────────────┘
                                ▼
                    ┌────────────────────────┐
                    │   Tenstorrent Device   │
                    └────────────────────────┘
```

Inside the broker, `ServerFsm` (fsm.py) is the root system — `boot_broker()` constructs the
health subsystem exactly once at start — while the job runner and the between-job health gate
stay in `server.py` as the main loop. Details: spec 00 (map), 03 (root architecture).

## Spec index

| Spec | Owns |
|---|---|
| [00-overview](specs/00-overview.md) | System map, data flow, glossary |
| [01-jobs](specs/01-jobs.md) | Queue, runner, lifecycle, env resolution, re-adoption |
| [02-transports](specs/02-transports.md) | Unix-socket MCP + shim, REST, tools; HTTP/TCP (Target: remove) |
| [03-health](specs/03-health.md) | FSM, probes, sampler, the gate, holds & relift |
| [04-recovery](specs/04-recovery.md) | Ladder, platforms, mechanism, reset stages, reset gate |
| [05-security](specs/05-security.md) | SO_PEERCRED, privsep, authz, exec gating, device lock |
| [06-state-paths](specs/06-state-paths.md) | Path matrix, env-var index, both deployment shapes |
| [07-cli](specs/07-cli.md) | Command surface, exit codes, daemon lifecycle |
| [08-deployment](specs/08-deployment.md) | Installers, autoupdate/reconcile, validator pipeline |
| [09-testing](specs/09-testing.md) | Suite guarantees, tripwire, validation levels, CI |

## Non-negotiable invariants

The cross-cutting ones an agent must never violate (each anchored in its spec):

1. **One job at a time**; all submission funnels through `_queue_job` (01 I1/I2).
2. **`MAX_TIMEOUT_SEC` (1500 s) is a ceiling, not a default** — a run that will not fit is
   reshaped, never given a bigger number (01 I2).
3. **Never touch the device outside the queue** — no bare `pytest`, no side `tt-smi` while a
   job runs; diagnostics go through `tt_device_exec`, resets through `tt_device_reset`, never a
   bare `tt-smi -r` (03/04; the reset tool scans holders, quiesces pollers, audits).
4. **The gate never resets over a tenant**, and an incomplete holder scan counts as a tenant
   (03 I13, 04 I6/I7).
5. **The fabric traffic pass never runs on a submitter's clock** (03 I12).
6. **Health singletons are constructed once, at boot** — never per gate pass (03 I1).
7. **A restart is never trusted healthy** — the startup gate re-proves the mesh (03 I4).
8. **Under privsep, a job runs as its submitter or is refused — never root by fallback**
   (05 I3).
9. **The unmarked test suite stays hardware-independent** — the spawn tripwire is law; stub the
   argv-builder, assert on argv (09 I1–I3).
10. **Specs are normative** — a behavioral change without a spec diff is unfinished
    (CONTRIBUTING).

## Testing quickstart

```bash
pytest -q                  # 1100+ tests; hardware-independent unless @pytest.mark.device
pytest -q --no-device-tests # level 1 exactly, on a box that has chips
pytest -m device -q         # the device tests alone; skip clean without /dev/tenstorrent
```

Before claiming CI green, prove it device-free (docker check: spec 09, level 1). A test that
needs real silicon declares `@pytest.mark.device` and skips without it (09 I8); it may never be
the only coverage of something provable device-free. Hardware validation levels 2-4, in order:
spec 09.

## Device-job runbook (agents submitting real work)

- **Ask the developer which model and which demo/test to run. Never pick one.** The right
  workload depends on what is being validated and what the box can host; a wrong topology
  aborts at model construction.
- Submit from the **workspace root** (the CLI resolves `<cwd>/tt-metal/python_env`), through
  the queue: `tt-device-mcp run "..."` or `tt_device_job_run`.
- **`MESH_DEVICE` must divide the model's KV heads**, or the run aborts with
  `n_kv_heads must be divisible by num_devices`. Count what the host actually exposes — one
  n300 card is 2 chips.
- An env file (`-e`) **replaces** the environment wholesale — it must be complete
  (`TT_METAL_HOME`, `PYTHONPATH`, `PYTHON_ENV_DIR`, `TT_METAL_CACHE`, `TT_METAL_ENV`). Read the
  job header's `ENVIRONMENT VARIABLES` block on any new host; caller exports beat workspace
  defaults (01 I11).
- With weights already in `HF_HOME`, `HF_HUB_OFFLINE=1` avoids the token and the hub
  round-trip; verify the snapshot holds the processor/tokenizer configs, not just safetensors.
- Settle a pytest `-k` selector with `--collect-only` first — **still through the queue**:
  collection can open the device (eager `MESH_DEVICE` defaults evaluate
  `ttnn.get_device_ids()`).
- **Read both verdicts**: `STATUS: completed / EXIT CODE: 0` is the job, pytest's `N passed`
  is the test — both, or it did not pass.
- **Keep the host quiet while a device job is in flight** — a concurrent unit-suite run
  correlated with an unattributed SIGKILL of a vision demo; the same job passed alone.
- **Do not read model throughput as a broker signal**, and never compare against a model's
  PERF.md without matching tracing mode — those tables are trace-enabled; a `-notrace`
  selector lands several times lower (measured: gemma-3-4b-it vision on N300, 3.99 tok/s
  notrace vs 30.49 trace-enabled) and looks exactly like a regression.
- Device wedged? The signature and the sanctioned recovery path: spec 03 (Behavior — the
  eth/fabric-wedge signature) and spec 04 (the reset tool's contract, including what its
  verify does and does not prove).

## Terminology

"Device", never "accelerator" (the package is tt-**device**-mcp). "Galaxy", never "6U Galaxy".

## Tenant workspaces

Agents working in a *workspace* (tt-metal etc.) on a broker host route all device work through
the queue — the copy-in snippet for workspace CLAUDE.md files is
[add-to-AGENTS.md](add-to-AGENTS.md).
