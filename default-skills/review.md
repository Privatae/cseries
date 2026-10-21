---
name: review
description: Compare SOP and pull-request review. A compare runs the same coding task on the engines/models you NAME with build(action='run', variants=[…]), shows the candidates side by side, and lands the one the USER picks — no model picks the winner of a compare. A PR review reads a pull request with gitea, proves findings and fixes on your own engines, and posts them with gitea op='comment'.
allowed-tools: build, review.compare, review.propose_commit, review.adopt, review.adopt_undo, gitea, create_project, review_session
---

# Compare SOP

A compare is the platform's multi-perspective coding lane: the same
coding task, several engines or models, each in its own worktree, and the
user picks. Useful when:

- The user has 2+ coding harnesses connected (Claude Code / Codex /
  Cursor / Grok / opencode / Kimi Code / Antigravity
  CLI) and wants to see what each makes of the task
- The task is non-trivial enough that one engine's blind spots
  could miss something another catches
- The user said something like "have everyone take a shot" or
  "compare what each tool comes up with"

For ONE engine — including "use Claude with Opus 5" — call
`build(action='run', prompt, harness='<id>', model='<model>')`: that is
a build, and it takes the model (and `effort`) itself. A one-candidate
compare has nothing to compare. Never start a compare just to set a model.

## How it flows

1. `build(action='run', prompt, variants=[{harness, model?, effort?}, …],
   max_minutes?, seed?, context?, acceptance?)`
   - One `variants` entry per run. Several entries naming the SAME
     harness are several runs of it (e.g. four codex variants with four
     models) — they share that harness's pod, one release. Variant ids
     are `<harness>`, then `<harness>-s2`, `-s3` …
   - Each variant gets its own git worktree under the compare's session.
   - `seed` overrides the auto edit-vs-fresh decision (compare only).
     `acceptance=[{cmd, expect_exit?, timeout_s?}]` runs the same checks
     on every variant, on the pod, after its harness finishes.
   - An unknown harness is refused by name (400); a model/effort a
     harness cannot run is refused before any pod wakes.
   - **Returns IMMEDIATELY** with `{session_id, task_id, variants,
     harnesses_dispatched, poll_url}` — the work runs in the background.
     Per-variant state moves queued → running → done|failed.
   - Tell the user which engines and models are building; the bench shows
     each one's progress. **DO NOT block waiting for it and do not poll.**
     When every variant has landed, the outcome is delivered INTO THIS
     THREAD as your own turn (the same lane a build uses — the `task_id`
     is its bench row), listing each variant's result and check verdict.

2. `review.compare(session_id)` — SHOW, never pick
   - Returns the candidates side by side: per variant its engine, model,
     effort, state, error, run time, diff size, the files it touched, its
     check verdict (when checks were declared), and its `preview_url`.
     `files` maps each path to the variants that touched it.
   - No model is asked which is best. Present the candidates; the USER
     picks. Do not rank them or recommend a winner on your own judgement.
   - It refuses with 409 while the compare is still queued or running, so
     it is safe to call once the delivery arrives.

3. **The user picked one** — `review.adopt`

   When the user says "use the codex one" or "I like what run 12 built,
   use that one", they mean the files that run produced, unchanged.

   - `review.adopt(session_id, harness='<variant>')` or
     `review.adopt(run_id=639)` — copies that run's files into its
     project. No model, no rebuild, no spec.
   - `review.adopt(run_id=639, paths=["assets/styles.css"])` — just part
     of it. This is what "port over the design" means: name the design
     files, leave the working logic alone.
   - `review.adopt(run_id=639, dry_run=true)` — what would change, first.
     Cheap; prefer it when the project has work in it.
   - It is ADDITIVE: files the run does not contain are untouched, so a
     backend the run never had is not deleted.
   - It is REVERSIBLE: every overwrite is backed up and every created
     file recorded. `review.adopt_undo(project_id, backup_id?)` puts it
     back exactly. Say so when you adopt it, or when you put adopting
     it in the question box — "it undoes cleanly" is the honest fact and
     it is what makes trying cheap.
   - `run_id` is the number the user actually says. `session_id` +
     `harness` (the variant id) also work if that is what you have.

   Do NOT reach for compare/propose_commit to satisfy "use run N". compare
   shows candidates, propose_commit takes per-file picks — the user asked
   for one run's files, and adopt is that.

4. `review.propose_commit(session_id, picks, commit_message?, commit?)` —
   only when the USER picks per file ("the CSS from cursor, the JS from codex")
   - `picks={path: variant}` comes from the user's choices, read against
     `review.compare`'s `files` map — never from your own ranking
   - Applies each pick (copies the chosen variant's file into the main
     worktree at `project_dir`), stages everything, returns the unified
     staged diff
   - `commit=true` also commits the picked files as a save point of the
     project's repository, with `commit_message` (the result's
     `committed`). Without it the picks stay staged and nothing is
     committed.

## Reviewing a pull request

Reviewing a PR on a repository you can read (the platform repo, your own,
one your user granted you) is work inside the platform: do it yourself, in
your own turn. Here a model reviews
another's work on purpose; the compare rule above (the user picks between
candidates) is about compares only.

A build you land is yours to review, in the turn its result comes back:
read what it changed (the rolling build PR, with the steps below) before you
tell the person it is done or before its merge. Nothing else reviews it for
you. When the person wants a second opinion on builds, a stated preference,
have a coding engine review the PR as step 3 describes, on an engine other
than the one that built it.

1. Read it: `gitea op='pull'` (`repo`, `number`) for the body, head,
   base, mergeable and the conversation; `op='pull_files'` for the
   changed files and their patches (`path=` gives one whole patch, paged);
   `op='pull_reviews'` for earlier reviews; `op='commit_status'`
   (`number`) for its CI. `op='pulls'` lists a repo's open PRs.
2. Read the code around each change at the PR's head: `gitea op='read'` /
   `'grep'` with `ref=<head_sha>`, or the code-search tools for the
   platform's own repositories.
3. Prove what you claim. For a finding that needs running, make a project
   on the repository (`operate(tool='create_project', args={name,
   repo:'owner/name', fork:true})` for one you can only read) and `build(action='run', prompt, acceptance=[…])`
   on an engine other than the one that wrote the PR. A finding you could
   not prove is labelled unproven.
4. Post the findings where the author reads them: `gitea op='comment'`
   (`repo`, `number`, `body`), each with its file:line and how you proved it.
5. Fixes. On your own proposal, revise its branch: `gitea op='propose'`
   with the PR's `branch_name` and `from_project=<id>` (or `files` with
   `edits`) — the same PR updates. On someone else's PR you cannot push
   its branch: propose the fix as its own PR and link it in your comment.
   You never merge; the user does.

## Surfacing it to the user

- The VS Code app (in the dock) is the human-facing surface for
  inspecting and editing the proposed changes in the workspace before
  committing.
- For the whole session, `read(tool='review_session', args={session_id})`;
  `variant='<id>'` returns one variant whole, its diff included.

## When NOT to compare

- One-line edits — a compare is way too heavy
- Throwaway scratch work — burns subscription quota on every engine
  for something the user will discard
- Chat-shaped responses (no diff being produced) — there is nothing to
  put side by side
- User explicitly steered to a specific harness — respect that;
  run a build on it instead of comparing anyway

## Costs

Per compare: one task-run per variant you named. Naming 4 variants means
4x the per-run cost (though most are subscription passthroughs at $0
marginal). Variants of one harness run on its one warm pod, one metered
window. There is no reviewer-model cost — nothing is asked to pick.

## build(action='run', variants=…) — guidance

Use when the user wants multi-perspective output ("have everyone take a shot
at this", "compare kimi and claude on this", "try it on four codex models").
For ONE engine call build(action='run', harness=<id>, model=<id>) instead. It
409s on review.compare while in flight; do NOT sit and poll review_session
for it. `review_session(session_id)` is a line per variant (harness, model,
state, elapsed, diff size, files, outcome); `variant='<id or harness>'`
returns that one variant whole, its unified diff included.

- `variants`: name them every time (from the coding protocol's step, or the
  user's words); there is no stored selection to fall back on, and "every
  connected engine" is never assumed.
- `model` per variant: a model id is valid on exactly ONE provider's harness
  (claude-code→Claude, codex→OpenAI, cursor→multi, grok→xAI, kimi→Moonshot,
  antigravity→Google's own `agy models` catalog, opencode→any slug your key
  reaches). Omitted, the harness's saved model (or its default) runs.
- Several runs of ONE harness: repeat it in `variants` (same model for
  self-consistency shots, different models to compare models). Gains from
  repeated identical shots flatten past ~6.
- `project_id` (derived from the project conversation when you omit it) does
  two things: surfaces the runs in the Workbench, AND decides what the
  variants start from — each worktree is seeded with that project's current
  files, so they EDIT the real app instead of rebuilding it from your
  description. `seed=false` forces a fresh tree. A compare does NOT drive the
  project's PLAN: no phase or step changes state (the project can still be
  shipped — shippability depends on the TREE). Drive a plan with
  build(action='build'); use a compare to try engines on one task and
  review.compare to show the user the candidates.
- `context`: the variants get NO context unless you provide it — you (the
  host) control what they see. Put the project facts/specs/snippets they need.
- `acceptance`: the checks every variant must pass; the verdict is shown
  beside each candidate. A check the engine's image cannot run is refused
  before dispatch.

## ladder.run — guidance

Rung 1 attempts, a deterministic verify (test_cmd) or the human judges, and
only on verified failure does the task climb — carrying failure EVIDENCE
(failing tests, watchdog verdicts, rejection notes), never the failed diff.
Cost scales with difficulty; use this for routine tasks where the cheap rung
usually wins, and a compare (build(action='run', variants=[…])) when
approaches genuinely diverge. Pass
`rungs` from the coding protocol you are following — ONE rung per chained
step (protocol step {engines:[{engine, model, effort}]} → rung {harness:
engine, model, effort}), bottom up; omitted, the default ladder applies: the
user's DEFAULT engine first, then the whole bench ({"harness": "*"} = every
connected engine at once). There is no stored ladder policy any more — the
Orchestrator's rung editor became protocol files. Omit test_cmd and every
completed rung waits for the human gate; `ladder.decision` relays the USER's
explicit decision only — the human gate is theirs, not yours.

## review.adopt — guidance

This is the answer to "I like what run 12 built, use that one" and to the
user's pick after a compare. No model sits between the run and the project. Additive: files the run does not contain are
left alone (a backend the run never had is not deleted). Reversible: every
overwrite is backed up and every created file recorded. Name the run by
run_id (the number the user says), or by session_id + harness. `paths`
narrows it to part of the output ("just the design": name the files). Use
dry_run=true first when the project has work in it. A DIFFERENT project_id is
refused — adopt writes a run into its own project. `into=<module>` lands the
run in that module's directory as the module's next VERSION, and
`review.adopt_undo(project_id, module=…)` then moves the module back one
version (pointer-only; nothing is deleted).


## Parameter notes (full text of shortened argument descriptions)

- **build** `variants`: [{harness, model?, effort?}] — one entry per run; the same harness may repeat (runs share its pod). Omit for a single build.
- **build** `seed`: compare only. Omit for AUTO (start from the project's real files when it has content). false forces a fresh tree; true forces seeding.
- **build** `context`: Optional governed context prepended to the task for every run. Builders get no context unless you provide it here.
- **build** `acceptance`: Optional [{cmd, expect_exit?, timeout_s?}] — checks run on the pod after the harness; their results are reported as notes.
- **review.adopt** `paths`: Optional. Only these files/directories (relative to the run root), e.g. ["assets/styles.css", "index.html"]. Omit for the whole run.
- **ladder.run** `rungs`: Bottom-up [{harness, model?, effort?, samples?, max_minutes?}], one per protocol step; last may be {"harness": "*"}. Omit for the default ladder.
- **review.adopt** `project_id`: Optional. The run already knows its project; a DIFFERENT project is refused — adopt writes a run into its own project.
- **ladder.run** `test_cmd`: Deterministic verify command (the project's own tests). Omit = every completed rung waits for the human gate.
