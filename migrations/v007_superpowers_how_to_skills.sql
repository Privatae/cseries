-- v007: superpowers 7 how-to skills → ContextStore (entity_context) skills/
--
-- Source: obra/superpowers.
-- Translates the 7 *how-to-shaped* SKILL.md files (not the discipline-shaped
-- ones — those landed in v005 as behavioral_corrections) into ContextStore
-- skills/*.md entries per active entity.
--
-- The runtime/prompt_schema.py `skills_files` block reads ContextStore
-- prefix='skills/' and surfaces each file's first line as a one-line entry
-- in the "## What You Know How To Do" prompt block. So these files act as:
--   1. A catalog the entity sees every prompt (first line = pithy summary)
--   2. On-demand full reference (the body) retrievable via context tool
--
-- Idempotent: entity_context has UNIQUE (entity_id, file_path), so
-- ON CONFLICT DO NOTHING preserves user-customised entries on re-run.
--
-- Distilled to ~10-15 lines each — far shorter than the upstream
-- (70-655 line) SKILL.md files. The upstream skills/*/SKILL.md files are
-- the ground truth.

WITH superpowers_how_to(slug, content) AS (
    VALUES
    ('using_git_worktrees',
$$Use a separate git worktree per feature so changes don't collide.

Process:
1. Detect: `git worktree list` — is there already one for this work?
2. Create: `git worktree add ../<feature>-tree -b feature/<slug>` from main.
3. Work inside the new tree; commit there.
4. When done, run finishing-a-development-branch.
5. Remove: `git worktree remove ../<feature>-tree` once the branch is merged or discarded.

Don't work on multiple unrelated features in the same worktree — that's how lock-in and accidental cross-commits happen.

Origin: external:superpowers/using-git-worktrees
$$
    ),
    ('executing_plans',
$$Execute a written plan to completion without pausing to check in.

Process:
1. Load the plan from `docs/cseries/plans/YYYY-MM-DD-<name>.md` (or wherever the user pointed).
2. Execute tasks in order. Each task is a verifiable step.
3. Verify after each task before moving to the next.
4. Mark the task complete in the plan.
5. Continue until all tasks are done OR a genuine blocker appears.

Don't pause to ask "should I continue?" — the user already approved the plan. Stop only for: unrecoverable blocker, genuine ambiguity, or completion.

Origin: external:superpowers/executing-plans
$$
    ),
    ('dispatching_parallel_agents',
$$Run independent subagents concurrently when tasks don't share state.

Process:
1. Identify tasks that touch disjoint files / domains / questions.
2. Send all dispatches in ONE message (parallel by construction).
3. Each subagent gets isolated context — don't inherit your session history.
4. Aggregate results when all return.

Use when: multi-domain failure analysis, independent codebase audits, fan-out research. Don't use when tasks are tightly coupled or sequential.

Origin: external:superpowers/dispatching-parallel-agents
$$
    ),
    ('requesting_code_review',
$$Dispatch a fresh reviewer subagent after a feature is complete.

Process:
1. Stage your changes (don't commit yet).
2. Dispatch a reviewer subagent with: the diff, the spec it was meant to satisfy, the tests.
3. Reviewer reports in two stages: spec compliance first, code quality second.
4. Triage feedback: fix what's correct, defend what's right.
5. Re-stage and commit when both stages pass.

Reviewer must have isolated context — no session history. Their job is independent verification, not consensus.

Origin: external:superpowers/requesting-code-review
$$
    ),
    ('receiving_code_review',
$$Handle review feedback technically, not performatively.

Process:
1. Read each comment with curiosity, not defence.
2. Distinguish: spec violation (must fix), quality concern (consider fix), preference (acknowledge, may decline).
3. Fix spec violations immediately.
4. For quality concerns: weigh the cost/benefit; explain decision either way.
5. For preferences: explain your reasoning; the reviewer may not have your full context.

Don't perform agreement. Don't argue defensively. Decisions get explained in writing.

Origin: external:superpowers/receiving-code-review
$$
    ),
    ('writing_skills',
$$Create a new skill by TDD on agent behavior.

Process:
1. Identify a recurring failure pattern in the agent's behavior.
2. Write the smallest test that reproduces it.
3. Write the skill content (SKILL.md or equivalent) that, when active, prevents the failure.
4. Re-run the test with the skill loaded — confirm it passes.
5. Track regressions: if the failure pattern reappears, the skill needs revision.

Skills are validated by changing observable agent behavior, not by being well-written.

Origin: external:superpowers/writing-skills
$$
    ),
    ('using_superpowers',
$$Invoke skills via the skill registry; skills self-announce and stay quiet otherwise.

Process:
1. Skill triggers fire automatically when the spike classifier matches their category (coding_task, etc.).
2. If a skill needs explicit invocation, call it once and don't re-announce in subsequent turns.
3. Don't quote skill content back to the user — apply it silently.
4. If a skill conflicts with a user instruction, surface the conflict and ask.

Skills are infrastructure, not performance. The user shouldn't have to see them work.

Origin: external:superpowers/using-superpowers
$$
    )
),
target_entities AS (
    SELECT id AS entity_id FROM entity_registry
    WHERE status = 'active'
      AND entity_type IN ('native', 'service')
)
INSERT INTO entity_context
    (entity_id, file_path, content_type, content, metadata)
SELECT
    te.entity_id,
    'skills/' || sh.slug || '.md',
    'text/markdown',
    sh.content,
    jsonb_build_object(
        'origin', 'external:superpowers',
        'slug',   sh.slug
    )
FROM target_entities te
CROSS JOIN superpowers_how_to sh
ON CONFLICT (entity_id, file_path) DO NOTHING;
