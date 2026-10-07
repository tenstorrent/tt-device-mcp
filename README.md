<div align="center">

# tt-device-mcp

### Share a Tenstorrent device between developers and AI agents.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

</div>

---

## Overview

A Tenstorrent device runs one job at a time. `tt-device-mcp` queues jobs and runs
them in order. Between jobs it checks the device and recovers it if needed, so
every job starts on a healthy device.

## Getting Started

#### Requirements

- Linux with a Tenstorrent device ([tt-kmd](https://github.com/tenstorrent/tt-kmd) installed)
- Python 3.10+
- [tt-smi](https://github.com/tenstorrent/tt-smi) on `PATH`
- for a shared host: `systemd` and `root`

#### Your own box, or a container

```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git
./tt-device-mcp/install.sh
```

A per-user daemon: no root, your jobs only. Pass `--user` to force this on a
host that would otherwise install the broker.

#### A shared host

```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git
sudo ./tt-device-mcp/install.sh
```

One system broker for everyone; jobs run as the user who submitted them. Pass
`--broker` to force this.

Never run a per-user daemon on a host where a system broker already serves the
same device.

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

The installer wires the user who ran it. Everyone else registers it once:

```bash
claude mcp add tt-device-mcp -s user -- tt-device-mcp   # Claude Code, then restart it
codex mcp add tt-device-mcp -- tt-device-mcp            # Codex
```

**Cursor** (`~/.cursor/mcp.json`), **Pi** (`~/.pi/agent/mcp.json`), and any other client
that reads an `mcpServers` config:

```json
{ "mcpServers": { "tt-device-mcp": { "command": "tt-device-mcp" } } }
```

The same binary is the CLI and the MCP adapter: with a piped stdin it speaks
MCP and finds the broker socket on its own.

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
| `tt_device_reset` | Reset the device; refuses while a job runs or another user holds it |
| `tt_device_recent_jobs` | Recent job history |

## How it works

Every submission enters one queue. The runner takes one job at a time.

Between jobs a gate probes the device (chip count, ARC heartbeat, ethernet
links, fabric traffic). If the device is unfit, the gate holds the queue and
climbs a reset ladder until the device recovers. It never resets while another
user's process holds the device.

Which rungs of the ladder a host gets depends on what the broker can run there;
it logs them at startup.

`specs/` states the rules in full, and the tests check the code against them.
Start with [00-overview](specs/00-overview.md).

## Configuration

Environment variables set paths, timeouts, and health thresholds. The full index
is in [06-state-paths](specs/06-state-paths.md).

## Slurm

The health gate is also reachable from outside the queue, as two root-only REST
calls over the broker's unix socket: `pre-step` (read-only; exits 0 if the
device is fit and free) and `post-step` (reclaims the allocation's leftover
processes, runs the full gate; exits 0 iff the device ends fit).

```
Prolog=/opt/tt-device-broker/slurm/prolog.sh
Epilog=/opt/tt-device-broker/slurm/epilog.sh
```

See [deploy/README.md](deploy/README.md) for the timeout arithmetic
(`PrologEpilogTimeout` vs. the steps' own deadlines).

## Troubleshooting

**A job is queued and nothing runs.**

- `tt-device-mcp status` names the running job and its owner.
- A held queue means the gate found the device unfit. The daemon log has the
  evidence.

**The device is hung.**

- `tt-device-mcp reset`. It scans for processes holding the device first, and
  refuses if another user is on it. It also refuses while a broker job is
  running, since a reset would kill it. Wait for the job, kill your own job
  first, or pass `--force` (the job it stops is logged).
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

The daemon does not run from your checkout. It runs from its own install, so
editing the code changes nothing until you reinstall:

```bash
./install.sh                    # your own box
sudo ./install.sh               # shared host
```

Then restart any open Claude or Cursor session, since the reinstall replaces the
binary they are running.

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
