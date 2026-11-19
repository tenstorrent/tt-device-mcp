<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

## Development workflow (tt-device-dev)

This project follows the `tt-device-dev` skill. Load it before starting or resuming work.

- Plan before implementing; the developer approves the plan first.
- GitHub issues track all work. Commit history is the source of truth; every commit names its
  issue (`Refs #N`).
- `project.md` is the mission. Every issue serves a goal in it; change it only in its own commit.
- No issue closes without the hostile review: both scores (objective, project.md alignment) 8 or
  higher, at most 5 rounds of requested changes, then escalate to the developer.
- Ask the developer which model and which demo or test to run. Never pick one.

### Device access

**Never touch the device directly.** All device work goes through the tt-device-mcp queue
(`tt_device_job_run`, `tt_device_job_run_bg`, or `tt-device-mcp run`) from the workspace root, with
the env file `tt-device.env.yaml`. Diagnostics go through `tt_device_exec`, resets through
`tt_device_reset`; never a bare `pytest`, `tt-smi`, or `tt-smi -r`. A run passed only if both the
job (`STATUS: completed`, `EXIT CODE: 0`) and the test (`N passed`) say so.
