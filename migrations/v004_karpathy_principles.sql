-- v004: karpathy 4-principles seeded into behavioral_corrections
--
-- Source: forrestchang/andrej-karpathy-skills (its top-level agent
-- guidance file). Per a prompt-pipeline audit, karpathy's 4 distilled principles map natively onto cseries's
-- behavioral_corrections slot — hard severity, retrieved by spike
-- category 'coding_task' plus embedding
-- similarity to trigger contexts.
--
-- Inserted for every active native + service entity. New
-- entities should pick these up at provisioning (provisioner change
-- queued separately) — for entities created BEFORE the category existed,
-- this migration backfills them.
--
-- Idempotent: re-running is a no-op because each row is keyed by
-- (entity_id, origin, correction-prefix) — the WHERE NOT EXISTS check
-- compares on those.

-- Use a CTE to enumerate the principles + a row per (entity, principle).
WITH karpathy_principles(idx, correction, trigger_context) AS (
    VALUES
    (1,
     'Think before coding. Surface assumptions, do not hide confusion. If multiple interpretations exist, name them before picking. If uncertain, ask before writing. Do not start typing on a vague spec.',
     'implementing code, fixing bug, building feature, writing function, refactoring, the user gave a task that involves writing or modifying code'
    ),
    (2,
     'Simplicity first. Write the minimum code that solves what was asked. No features beyond what was asked, no abstractions for single-use code, no flexibility that was not requested, no error handling for impossible cases. If you write 200 lines and it could be 50, rewrite it.',
     'implementing code, fixing bug, building feature, writing function, refactoring, the user gave a task that involves writing or modifying code'
    ),
    (3,
     'Surgical changes. Touch only what the request requires. Do not improve adjacent code, comments, or formatting. Do not refactor things that are not broken. Match existing style. Every changed line should trace directly to the user request.',
     'implementing code, fixing bug, building feature, writing function, refactoring, the user gave a task that involves writing or modifying code'
    ),
    (4,
     'Goal-driven execution. Define a verifier before coding. Transform vague tasks into verifiable goals — write the test that proves it works, then make it pass. Loop until verified. Do not claim done without fresh evidence in the same response.',
     'implementing code, fixing bug, building feature, writing function, refactoring, the user gave a task that involves writing or modifying code'
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
    kp.correction,
    kp.trigger_context,
    'correction',
    'hard',
    TRUE,
    'external:karpathy'
FROM target_entities te
CROSS JOIN karpathy_principles kp
WHERE NOT EXISTS (
    SELECT 1 FROM behavioral_corrections bc
    WHERE bc.entity_id = te.entity_id
      AND bc.origin   = 'external:karpathy'
      AND LEFT(bc.correction, 30) = LEFT(kp.correction, 30)
);
