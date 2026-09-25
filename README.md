<div align="center">

# tt-device-mcp

### Share a Tenstorrent device between developers and AI agents.
Developers and AI agents submit jobs through an MCP server or a CLI.
The broker runs one job at a time, and maintains device health.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

</div>

---

## Overview

tt-device-mcp is a job queue for Tenstorrent hardware.

Two processes that open the same device corrupt each other's runs, and a hung
device blocks everyone on the host. Without a queue, the fix is a chat message
asking whether anyone is using the box.

Use it if you share a Tenstorrent host with other people or with AI agents, or
if you want your own agent's device work queued and logged instead of run
directly.

## Requirements

- Linux with a Tenstorrent device and [tt-kmd](https://github.com/tenstorrent/tt-kmd)
- Python 3.10+
- [tt-smi](https://github.com/tenstorrent/tt-smi) on `PATH` for health checks and resets
- for the shared-host install only: systemd and root

## Getting Started

#### Your own box, or a container
```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git
./tt-device-mcp/install.sh
```
- A per-user daemon: no root, your jobs only, health-gated between jobs.
- `--user` forces this choice on a host that would otherwise install the broker.

#### A shared host
```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git
sudo ./tt-device-mcp/install.sh
```
- One system broker for everyone: jobs run as the user who submitted them.
- `--broker` forces this choice.

The install picks where the daemon and its state live. It does not decide what the broker may do
to the device — that is measured at startup, so there is no authority flag to pass.

Note:

- Never run a per-user daemon on a host where a system broker already serves the same device.


## Usage

```bash
tt-device-mcp run "pytest tests/test_foo.py -v"   # queue, wait, stream output
tt-device-mcp run-bg "pytest ..."                 # queue, return now
tt-device-mcp wait 042                            # wait on a background job
tt-device-mcp status                              # running, queued, recent
tt-device-mcp logs -f 042                         # tail a job's logs
tt-device-mcp kill 042                            # kill or cancel your own job
tt-device-mcp watch                               # live status, redraws in place
```

Job ids are three digits, `000`–`999`, reused once nothing live holds one.

`tt-device-mcp help` lists the rest: `reset`, `exec`, `smi`, `timezone`,
`lock`/`unlock`.

## MCP clients

The installer wires the user who ran it. Everyone else registers it once.

One binary serves both roles. Given a piped stdin it runs as the stdio MCP
adapter, and finds the broker socket on its own.

```bash
claude mcp add tt-device-mcp -s user -- tt-device-mcp   # Claude Code, then restart it
codex mcp add tt-device-mcp -- tt-device-mcp            # Codex
```

**Cursor** (`~/.cursor/mcp.json`), **Pi** (`~/.pi/agent/mcp.json`), and any other client
that reads an `mcpServers` config:

```json
{ "mcpServers": { "tt-device-mcp": { "command": "tt-device-mcp" } } }
```

To point an agent at the queue, copy [add-to-AGENTS.md](add-to-AGENTS.md) into
your project's `AGENTS.md`.

### Tools

| Tool | Description |
|------|-------------|
| `tt_device_job_run` | Queue and wait |
| `tt_device_job_run_bg` | Queue, return now |
| `tt_device_job_status` | Job status and result |
| `tt_device_job_wait` | Wait for completion |
| `tt_device_job_logs` | Job logs |
| `tt_device_job_kill` | Kill or cancel a job |
| `tt_device_queue_status` | Running and queued jobs |
| `tt_device_exec` | Run a command directly (`tt-smi`, etc.) |
| `tt_device_reset` | Reset the device; refuses over another user's job |
| `tt_device_recent_jobs` | Recent job history |

## How it works

Every submission enters one queue. The runner takes one job at a time.

Between jobs a gate probes the device (chip count, ARC heartbeat, ethernet
links, fabric traffic). If the device is unfit, the gate holds the queue and
climbs a reset ladder until the device recovers.

The gate never resets while another user's process holds the device.

Which rungs a given host gets depends on what the broker can run there; it logs them at startup.

=> `specs/` states the rules in full, and the tests check the code
against them. Start with [00-overview](specs/00-overview.md) for the system
map.

## Slurm

The same health gate a job dispatches against is also reachable from outside the queue, so a
Slurm site gets three steps around a job step instead of one implicit one: `pre-step` (read-only,
exits 0 iff the device is fit and free), the job itself, then `post-step` (reclaims the
allocation's leftover processes and runs the full gate, exits 0 iff the device ends fit). Both
verbs are root-only REST calls over the broker's unix socket, with no MCP tool counterpart.
`deploy/slurm/prolog.sh` and `deploy/slurm/epilog.sh` are the hooks to point `slurm.conf` at; see
[deploy/README.md](deploy/README.md) for the timeout arithmetic (`PrologEpilogTimeout` vs. the
steps' own deadlines).

```
Prolog=/opt/tt-device-broker/slurm/prolog.sh
Epilog=/opt/tt-device-broker/slurm/epilog.sh
```

## Troubleshooting

**A job is queued and nothing runs.**

- `tt-device-mcp status` names the running job and its owner.
- A held queue means the gate found the device unfit. The daemon log has the
  evidence.

**The device is hung.**

- `tt-device-mcp reset`. It scans for processes holding the device first, and
  refuses if another user is on it. Ask them to stop, or pass `--force`.
- Never run `tt-smi -r` by hand while the broker is up.

**`pytest` cannot open the device.**

- A shared host can lock direct device access with `TTDEV_LOCK=1` at install,
  and require users to go through `tt-device-mcp run`.
- Hosts are unlocked by default and stay cooperative. A direct run works, and
  collides with whatever the queue is running.

**A job died with no output.**

- Check whether something else touched the device. A run started outside the
  queue is the usual cause.

## Development

```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git && cd tt-device-mcp
pip install -e ".[dev]"
pytest -v                       # no device required
```

The suite needs no device, and must stay that way. See
[CONTRIBUTING.md](CONTRIBUTING.md).

### Testing a change on a device: re-run the install

`pip install -e .` changes your environment only. The daemon that answers jobs
runs from its own install, which is not editable — so **an edited tree changes
nothing until you re-run the install step for the shape you are testing**
(Getting Started, above). Both need it:

```bash
./install.sh                    # per-user
sudo ./install.sh               # shared host
```

Skipping it fails silently: `pytest` passes and the daemon serving your job is
the previous build.

On a shared host, re-running the install replaces the venv that live MCP stdio
adapters are executing from, so restart any open Claude/Cursor afterwards.

## Configuration

Environment variables set paths, timeouts, and health thresholds. The full index
is in [06-state-paths](specs/06-state-paths.md).

## Contributing

Report bugs through
[GitHub Issues](https://github.com/tenstorrent/tt-device-mcp/issues), and send
changes as pull requests.

A change in behavior starts with a spec change. [CONTRIBUTING.md](CONTRIBUTING.md)
has the workflow, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) applies.

## License

[Apache 2.0](LICENSE), except where noted otherwise. Documentation and images
are licensed under [CC BY 4.0](LICENSE-DOCS).

[LICENSE_understanding.txt](LICENSE_understanding.txt) states what the license
does and does not cover for Tenstorrent hardware, models, and IP.
