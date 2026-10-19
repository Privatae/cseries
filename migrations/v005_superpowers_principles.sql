-- v005: superpowers 7 discipline principles → behavioral_corrections
--
-- Source: obra/superpowers.
-- Translates the 7 discipline-shaped SKILL.md files into the native cseries
-- slot: hard behavioral_corrections rows tagged origin='external:superpowers'.
-- Routed by spike category 'coding_task' via discipline-
-- marker keywords in each correction's text.
--
-- The remaining 7 superpowers SKILLs are how-to-shaped (using-git-worktrees,
-- subagent-driven-development infrastructure, executing-plans,
-- dispatching-parallel-agents, requesting/receiving-code-review,
-- writing-skills meta) — those will land as ContextStore `skills/*.md`
-- files in a separate migration since they're per-entity file content,
-- not graduated discipline rules.
--
-- Idempotent: WHERE NOT EXISTS keyed on (entity_id, origin, prefix).

WITH superpowers_principles(idx, correction, trigger_context) AS (
    VALUES
    (1,
     'Verification before completion. No completion claims without fresh verification evidence in the same response. Before claiming work is done, fixed, passing, or deployed: identify the command that proves the claim, run it fresh in this message, read the actual output and exit code, then make the claim WITH the evidence visible. ''Working'' without a curl, ''passing'' without test output, ''deployed'' without an is-active check — these are lies, not efficiency.',
     'about to claim work is done, fixed, passing, deployed, complete; finishing a coding task; declaring something works'
    ),
    (2,
     'Systematic debugging. No fixes without root cause investigation first. When encountering a bug, test failure, performance issue, or unexpected behavior: complete Phase 1 — understand the failure — before proposing any fix. Random fixes mask underlying issues and create new bugs. Quick patches under time pressure are how regressions ship. Especially when ''just one quick fix'' seems obvious, slow down and find the root cause.',
     'encountering a bug, test failure, unexpected behavior, performance problem; fixing an error; previous fix did not work; under pressure to ship a fix'
    ),
    (3,
     'Brainstorming first. Before writing any code for a new project or feature, run a brainstorming pass with the user: ask clarifying questions one at a time, propose 2 to 3 approaches with trade-offs, present a design, get explicit approval. This applies to EVERY project regardless of perceived simplicity. ''Simple'' projects are where unexamined assumptions cause the most wasted work.',
     'starting a new project, new feature, new build; user asks to build, add, create, design something fresh'
    ),
    (4,
     'Writing plans. Before executing a multi-step task, write the plan as bite-sized verifiable steps (2 to 5 minutes each). Map which files will be touched, what each step accomplishes, how each step will be tested. Decompose first; execute second. Each step should produce working, testable progress.',
     'multi-step coding task; implementing a feature with multiple components; refactoring across multiple files'
    ),
    (5,
     'Test-driven development. Write the failing test FIRST, watch it fail, then write minimum code to pass, then refactor. RED-GREEN-REFACTOR. Applies to new features, bug fixes, behavior changes, refactoring. If you did not watch the test fail, you do not know if it tests the right thing. ''Skip TDD just this once'' is rationalization.',
     'implementing new feature; fixing a bug that needs a regression test; writing or modifying code that has a verifiable behavior'
    ),
    (6,
     'Finishing a development branch. Before declaring development work done: run the project test suite and confirm all tests pass; then present clear completion options to the user (merge, open PR, keep on branch, discard). Don''t merge or discard silently. Tests failing means not done — fix before completing.',
     'finishing implementation; wrapping up coding work; about to merge or open a PR; declaring a feature complete'
    ),
    (7,
     'Subagent-driven development. For multi-task plans: dispatch a FRESH subagent per task with isolated context (do not inherit session history). After each task, run a two-stage review — spec compliance first, then code quality. Execute all tasks continuously without pausing to check in. ''Should I continue?'' prompts waste the user''s time — they asked you to execute the plan, so execute it. Stop only for genuine blockers.',
     'executing a multi-task implementation plan; delegating implementation work; coordinating multiple coding tasks across subagents'
    )
),
target_entities AS (
    SELECT id AS entity_id FROM entity_registry
    WHERE status = 'active'
      AND entity_type IN ('native', 'service')
)
INSERT INTO behavioral_corrections
    (entity_id, correction, trigger_context, feedback_type, severity, active, origin)
SELECT
    te.entity_id,
    sp.correction,
    sp.trigger_context,
    'correction',
    'hard',
    TRUE,
    'external:superpowers'
FROM target_entities te
CROSS JOIN superpowers_principles sp
WHERE NOT EXISTS (
    SELECT 1 FROM behavioral_corrections bc
    WHERE bc.entity_id = te.entity_id
      AND bc.origin   = 'external:superpowers'
      AND LEFT(bc.correction, 40) = LEFT(sp.correction, 40)
);
