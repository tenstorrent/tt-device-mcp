<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Hostile review protocol

Every issue passes this review before it closes. The reviewer's job is to find reasons the work
does **not** meet its objective or does not serve the project. The implementer's job is to give it
everything it needs and then do what it asks.

## Pass rule

- Two scores, each an integer from 1 to 10:
  - **objective**: how well the work does what the issue's objective and acceptance criteria say.
  - **alignment**: how well the work serves the mission, goals and success criteria in `project.md`.
- **Pass only if both are 8 or higher.** A 7 fails.
- The reviewer may request changes **at most 5 times**. Review 1 is the first review; reviews 2–6
  each follow a round of changes. If review 6 still fails, stop: label the issue `review:escalated`,
  leave it open, and hand it to the developer with all six reviews.

## Running a round

1. Gather the inputs fresh for this round. Pass them as they are; don't summarize them.
   - `gh issue view <N> --comments` (the objective, the acceptance criteria, earlier reviews)
   - `project.md` as committed on the branch
   - The diff: `git diff <base>...HEAD` and `git log --oneline <base>..HEAD`
   - Raw evidence: job ids, the job footers (`STATUS`, `EXIT CODE`) and pytest's summary line for
     every device run
2. Spawn a **fresh** subagent (`subagent_type: general-purpose`, never a fork, so it does not
   inherit your view of the work) with the prompt below and the inputs. Don't add your own
   assessment.
3. Check its reply against the output format. If it is malformed, ask it once to fix the format.
   Don't reinterpret the scores.
4. Post the review as an issue comment: the round number, both scores, the verdict, the rationales
   and the required changes.
5. Pass: label `review:passed` and remove `status:in-review` and `review:changes-requested`.
   Fail: label `review:changes-requested`, make every required change in commits that name the
   issue, and start the next round.

## Reviewer prompt

> You are a hostile reviewer for GitHub issue #<N>. You did not write this work and you have no
> stake in it passing. Your job is to find every way it falls short.
>
> Judge two things, each on an integer scale of 1 to 10:
>
> 1. **objective**: does the work do what the issue's objective and acceptance criteria say? Check
>    each acceptance criterion against the diff and the evidence. A criterion with no evidence is
>    not met. A device run counts as passed only if both the job (`STATUS: completed`,
>    `EXIT CODE: 0`) and the test (`N passed`, no failures or errors) say so.
> 2. **alignment**: does the work serve the mission, goals and success criteria in `project.md`?
>    Penalize work that is off-mission, that adds scope `project.md` lists as a non-goal, or that
>    meets the letter of the issue without moving the project toward its success criteria.
>
> Scale: 10 = nothing to add; 8 = acceptable, minor nits only; 7 or lower = a real gap.
> Both scores must be 8 or higher to pass.
>
> Rules:
> - Every required change must point at a specific acceptance criterion or a specific line of
>   `project.md`. Don't require changes for taste.
> - From round 2 on, check that each change you required earlier was made. Don't raise a new
>   requirement you could have raised in an earlier round unless the new changes introduced it.
> - This is round <R> of at most 6.
>
> Reply with exactly this JSON and nothing else:
>
> ```json
> {
>   "issue": <N>,
>   "round": <R>,
>   "objective_score": <1-10>,
>   "objective_rationale": "<which criteria are met, which are not, citing evidence>",
>   "alignment_score": <1-10>,
>   "alignment_rationale": "<which project.md goals this serves or undercuts>",
>   "verdict": "pass" | "changes_requested",
>   "required_changes": ["<change, tied to a criterion or project.md line>"]
> }
> ```
>
> `verdict` is `pass` only if both scores are 8 or higher, and then `required_changes` is empty.
