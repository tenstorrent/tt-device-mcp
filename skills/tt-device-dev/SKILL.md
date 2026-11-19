---
name: tt-device-dev
description: Set up and run a development effort on a Tenstorrent host that shares its device through tt-device-mcp. Use when starting or resuming project work in a tt-metal workspace (a model bring-up, a demo, a kernel or test change), when planning that work, when filing or closing its GitHub issues, or when running anything on the device. Sets up the same dev environment every time, searches for existing work before planning, plans before implementing, tracks everything in GitHub issues with commit history as the source of truth, keeps project.md as the mission, and makes a hostile reviewer accept every issue before it closes.
---

<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# tt-device-dev

A fixed way to run a development effort on a Tenstorrent host that shares its device through
tt-device-mcp. Do the phases in order. Don't skip one because the task looks small.

## Ground rules (every phase)

- **GitHub issues track everything.** Each unit of work is an issue. Don't keep task lists only in
  chat or in local notes.
- **Commit history is the source of truth for active development.** To learn what is done or in
  flight, read `git log` and the commits linked from issues, not your memory or a summary. Every
  commit names its issue (`Refs #N`; `Closes #N` only in the commit or PR that passed review).
- **`project.md` holds the mission.** It records the goal, the success criteria and the hardware
  target. Every issue must serve a goal in it. When the goal changes, update `project.md` in its own
  commit that names the issue or decision that changed it.
- **Plan before implementing.** No code changes until the developer has approved the plan.
- **The device goes through the queue only** (see "Device work" below).

## Phase 0: Set up the environment

Run the setup script from the workspace root. The workspace is the folder that holds `tt-metal/`,
which is the layout the broker's defaults assume.

```bash
~/.claude/skills/tt-device-dev/scripts/setup-dev-env.sh --check   # report only, changes nothing
~/.claude/skills/tt-device-dev/scripts/setup-dev-env.sh           # fix what it can, report the rest
```

Options: `--workspace DIR` (default: the current directory), `--repo-dir DIR` (the git repo that
holds `project.md` and whose GitHub issues track the work; default: the workspace).

It checks the same things every time and prints one line per check, marked `OK`, `FIXED`,
`WOULD FIX` (`--check`), or `MISSING`:

| Check | It fixes it by |
|---|---|
| `tt-device-mcp` binary found | (can't; install it with `install.sh` from the tt-device-mcp repo) |
| Broker answers (`tt-device-mcp status`) | (can't; start the per-user daemon or ask the host admin) |
| Claude Code has the `tt-device-mcp` MCP server | `claude mcp add tt-device-mcp -s user -- <binary>` |
| `tt-metal/` and `tt-metal/python_env` exist | (can't; ask the developer which tt-metal ref to build) |
| `gh` installed and logged in | (can't; the developer runs `gh auth login`) |
| Repo dir is a git repo with a GitHub remote | (can't; creating a repo is the developer's call) |
| Workflow issue labels exist on the GitHub repo | `gh label create` for each missing one |
| `project.md` exists | copies `templates/project.md` |
| `CLAUDE.md` has the workflow block | adds or refreshes the block between its markers |
| `tt-device.env.yaml` exists with the required variables | writes it from the workspace layout |
| `.gitignore` keeps `tt-metal/` and its cache out of the repo | appends the missing lines |

Every `MISSING` line names the command that fixes it. Show those to the developer. Don't guess at
fixes the script refuses to make, and never create a GitHub repo or run `gh auth login` yourself.
Run the script again until nothing is `MISSING`, or until the developer says to go on without that
piece.

## Phase 1: Search before planning

Before you plan anything, find out what already exists. tt-device-mcp has no notion of a project,
so this search is how you find one. Do all of it, then write a short **Discovery** summary for the
developer.

1. **Existing mission.** Read `project.md` if it is there. If it already describes this effort,
   you are resuming, not starting.
2. **Existing issues.** `gh issue list --state all --limit 100`, plus
   `gh issue list --state all --search "<model or feature keywords>"`. Note open issues and their
   labels, and closed issues that already did part of the work.
3. **Commit history.** `git log --oneline -30` in the repo dir, and `git log --all --grep "#<N>"`
   for each open issue. This is what has actually landed.
4. **Existing work in tt-metal.** Search `tt-metal/models/` (including `models/demos/` and
   `models/tt_transformers/`) for the model or feature. Note its tests, demos and `PERF.md`. An
   existing bring-up changes the plan from "build" to "extend" or "validate".
5. **Prior device runs.** `tt_device_recent_jobs` (or `tt-device-mcp status`): earlier jobs from
   this workspace and how they ended.
6. **What the box can host.** `tt_device_queue_status`, then
   `tt_device_exec(command="tt-smi -ls")` for the chip count and board type. Record it in
   `project.md` under Hardware target. One n300 card is 2 chips.
7. **Environment.** The tt-metal commit (`git -C tt-metal rev-parse --short HEAD`) and the env
   file in use. Record both in `project.md`.

## Phase 2: Plan

1. Write or update `project.md` (template: `templates/project.md`). Fill in mission, goals,
   non-goals, success criteria, hardware target and environment. Leave no placeholder in it.
2. **Ask the developer which model and which demo or test to run. Never pick one yourself.** The
   right workload depends on what is being validated and what the box can host.
3. Break the work into issues (body: `templates/issue.md`). Each issue has one objective,
   acceptance criteria someone else can check, the `project.md` goal it serves, and its device
   needs.
4. Show the developer the plan: the `project.md` diff and the list of issues. Wait for approval.
   Only then create the issues (`gh issue create --label status:planned --body-file ...`) and
   commit `project.md` (`Refs` the first issue, or "project: initial mission").

## Phase 3: Implement one issue at a time

1. Label it `status:in-progress`. Branch: `issue-<N>-<short-slug>`.
2. Commit in small steps; every message names the issue (`Refs #N`).
3. Run device work through the queue and record the evidence (job id, both verdicts) in an issue
   comment.
4. If the work shows that `project.md` is wrong or incomplete, stop and raise it with the developer.
   Update `project.md` in its own commit once they agree.
5. When the acceptance criteria are met, label it `status:in-review` and go to Phase 4.

## Phase 4: Hostile review before acceptance

No issue closes without passing this review. Follow
[references/hostile-reviewer.md](references/hostile-reviewer.md) exactly. In short:

- Spawn a **fresh** reviewer subagent (not a fork) each round. Give it the issue, `project.md`, the
  diff and the raw evidence. Don't give it your own opinion of the work.
- It scores **ticket objective** 1–10 and **project.md alignment** 1–10. **Both must be 8 or
  higher** (over 7) to pass.
- On a fail it lists required changes. Make them, then review again. It may request changes **at
  most 5 times**. If the review after the 5th round of changes still fails, stop: label the issue
  `review:escalated`, leave it open, and bring it to the developer.
- Post every review as an issue comment, scores included.
- On a pass, label it `review:passed`. With the developer's go-ahead, push and open a PR (or merge)
  with `Closes #N`, and close the issue with a link to the final commit.

## Device work

The device is shared. Every device touch goes through the tt-device-mcp queue.

- Use `tt_device_job_run` / `tt_device_job_run_bg` (or `tt-device-mcp run` / `run-bg`) from the
  **workspace root**, with `env="tt-device.env.yaml"` / `-e tt-device.env.yaml` so every run sees
  the same environment. An env file **replaces** the environment wholesale, so add any extra
  variables you need (`HF_HOME`, `HF_HUB_OFFLINE=1`, `MESH_DEVICE`, ...) to that file, not to your
  shell.
- Never run bare `pytest`, `python` on a model, or `tt-smi` against the device. Diagnostics go
  through `tt_device_exec`. Resets go through `tt_device_reset`, never `tt-smi -r`.
- Settle a pytest `-k` selector with `--collect-only` first, still through the queue: collection
  can open the device.
- `MESH_DEVICE` must divide the model's KV heads.
- **Read both verdicts**: the job (`STATUS: completed`, `EXIT CODE: 0`) and the test (`N passed`).
  If either is missing, it did not pass.
- Keep the host quiet while a device job runs. Don't run unit suites or builds alongside it.
- Don't compare throughput against a `PERF.md` without matching tracing mode; those numbers are
  trace-enabled.
- If a job seems stuck for more than 5 minutes, ask the developer before killing it.
