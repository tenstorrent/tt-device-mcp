# Contributing

This repo is developed spec-first. The specs under `specs/` are normative — they state
what MUST hold, and the test suite enforces it. Most contributors here are humans working with
Claude Code agents: the human edits intent in the spec, the agent implements against it. The
workflow below is the contract either way.

## Reporting bugs

Report bugs through
[GitHub Issues](https://github.com/tenstorrent/tt-device-mcp/issues). Include the broker
version (`tt-device-mcp version`), the deployment shape (per-user daemon or system broker),
and the relevant slice of the job log or daemon log.

Do **not** report security vulnerabilities through public issues — see
[SECURITY.md](SECURITY.md) for the private reporting channels.

## Development setup

```bash
git clone https://github.com/tenstorrent/tt-device-mcp.git && cd tt-device-mcp
pip install -e ".[dev]"
pip install pre-commit && pre-commit install   # black + ruff on every commit
pytest -v                          # no device required
```

## The workflow

**A behavioral change starts as a spec diff.**

1. **Find the owning spec** — the map in [specs/00-overview.md](specs/00-overview.md).
2. **Edit the spec first.** Change the Invariants/Behavior text to say what the system should
   do now. If the change is big enough to argue about, open the PR with only the spec diff and
   settle the intent before any code exists.
3. **Implement against the spec.** The code change follows the spec text, not the other way
   around.
4. **Anchor it.** Every new or changed invariant gets a test, and the spec's `## Test anchors`
   table gets the new pytest node ids. An invariant with no anchor is unfinished work.
5. **One PR carries all three**: spec diff + code diff + test anchors. A reviewer should be able
   to read the spec diff as the statement of intent and check the code against it.

**Non-behavioral changes** (refactors, comments, docs, test-only work) need no spec diff — but
they MUST NOT break anchors. Renaming a test or changing a parametrize value renames node ids;
update the owning spec's anchor table in the same change.

**Targets — spec leads code.** A spec section marked `Target` declares intent ahead of the
implementation (e.g. spec 02's `Target: remove HTTP/TCP`). Targets are exempt from anchors but
carry a tracking pointer, and each is implemented as its own spec-driven PR — never folded into
unrelated work.

**Ambiguous intent goes to a filed issue, not into spec text.** If you find code doing
something definite that nothing proves was *meant*, file it as a question for the maintainer;
the resolution lands as a spec edit (plus a spec-driven PR when behavior changes). Never
canonize an accident by writing it into a spec as if it were intent.

## Verification before claiming done

- `pytest -q` — the suite is hardware-independent and must pass anywhere
  ([specs/09-testing.md](specs/09-testing.md)); the conftest spawn tripwire fails any
  test that would touch silicon, BMC, or host services. A test needing a forbidden command
  stubs the argv-builder and asserts on the argv (09 I3).
- **Device-free proof before claiming CI green** — passing on a device box proves nothing; run
  the docker check in 09, Validation level 1.
- **Anchors resolve** — every node id cited in a spec you touched must appear in
  `pytest --collect-only -q` output. No dead anchors.
- Hardware validation, when the change warrants it, follows 09's levels 2-4 **in order** —
  a broken install looks exactly like a broken device from the far end of a job log. A test that
  genuinely needs silicon carries `@pytest.mark.device` (09 I8) and skips without it.

## Code conventions

1. **Formatting and lint are mechanical**: black at 120 columns, ruff (`E4,E7,E9,F` + import
   sort) — configured in `pyproject.toml`, run by pre-commit, enforced in CI. SPDX headers are
   checked in CI by the org-wide `spdx-checker` action.
2. **No dead code** — delete, don't comment out.
3. **Imports at top** — no inline imports. (One documented consequence: `health.HEALTH_DIR`
   binds at import — see spec 06 I5 before touching path accessors; new accessors are
   late-bound.)
4. **Shared utilities in `utils.py`, shared constants in `constants.py`** — don't duplicate.
5. **Acquire the lock for job-state changes.**
6. **Properties for computed values** (`Job.runtime_sec`, `Job.wait_sec`).
7. **SPDX Apache-2.0 headers** on all Python files:
   ```python
   # SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
   #
   # SPDX-License-Identifier: Apache-2.0
   ```
8. **REST and MCP share helpers** — a limit or refusal lives in the shared `_get_*`/`_kill_job`
   helper, never per-transport (spec 02 I2).
9. **MCP tools**: Pydantic input models with `Field()` descriptions, `tt_device_` prefix,
   read-only/destructive annotations (spec 02 I6).
10. **Terminology**: "device", never "accelerator"; "Galaxy", never "6U Galaxy".

## Commits and PRs

- Changes are submitted through Pull Requests against `main` — one behavioral change per PR.
  Reviews happen on a weekly cadence by default; ping the PR if it has waited longer than that.
- The default branch takes squash merges only, and requires one approving review plus a green
  `test (3.10)` check.
- Subject: `<scope>: <verb> <what>`, ≤72 chars, verb-led. Body only when the why needs saying —
  the diff shows the what.
- One spec lands per commit where practical; spec + code + anchors travel in one PR.
- Code review checks spec conformance: does the code do what the spec diff says, and is every
  changed claim anchored? (Tenstorrent-internal contributors: `/tt:code-review` runs this as a
  five-reviewer pass.)

## Testing discipline

[specs/09-testing.md](specs/09-testing.md) is the authority: where a new test
belongs, the fixture seams (module-global aliases, `patch_recovery`, `fsm_dirty`/`fsm_healthy`,
`deploy_helpers` for shell), the tripwire rules, the `device` marker, naming
(behavior-sentence test names), and the four validation levels.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are
expected to uphold it.
