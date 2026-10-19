-- v006: karpathy EXAMPLES.md → entity_memory teaching rows
--
-- Source: forrestchang/andrej-karpathy-skills/EXAMPLES.md (523 lines,
-- 9 worked antipattern → fix examples paired with the 4 principles).
--
-- Translates each example into a concise teaching memory in the native
-- cseries slot: entity_memory rows with memory_type='teaching'. codebook_core
-- _retrieve_teaching already picks these up by embedding similarity when
-- the user message is about coding work.
--
-- Tagging via metadata JSONB (entity_memory has no `origin` column):
--   metadata = {"origin": "external:karpathy", "source": "EXAMPLES.md",
--               "principle": "...", "example_id": "..."}
-- so they're filterable / reversibly removable.
--
-- Idempotent: WHERE NOT EXISTS keyed on (entity_id, metadata->>'origin',
-- metadata->>'example_id'). Importance=1.5 (above default 1.0) —
-- foundational discipline examples.

WITH karpathy_examples(example_id, principle, content) AS (
    VALUES
    ('think_before__hidden_assumptions',
     'Think Before Coding',
     'When asked to "export user data" or any similarly vague request, do NOT assume scope, format, fields, or volume. The antipattern is silently picking "all users to a JSON file with all fields". The fix is to surface assumptions explicitly: ask whether the output is an API / file / email, which fields, which user subset, what volume. List interpretations before writing code.'
    ),
    ('think_before__multiple_interpretations',
     'Think Before Coding',
     'When asked to "make X faster" or any ambiguous-perf request, do NOT pick one interpretation silently. "Faster" can mean response time, throughput, or perceived speed — each has different fixes (indexes, async, progressive loading). The fix is to present 2-3 plausible interpretations with their trade-offs and ask which matters.'
    ),
    ('simplicity__over_abstraction',
     'Simplicity First',
     'When asked to add a function for a single use case (e.g. "calculate a discount"), do NOT introduce a strategy pattern, abstract base classes, or config dataclasses. The antipattern is 30-line scaffolding for what should be a 3-line function. The fix is to write the simplest function that solves THIS call site. Add abstraction only when a second use case appears.'
    ),
    ('simplicity__speculative_features',
     'Simplicity First',
     'When asked to save X to database (or any narrow request), do NOT add caching, validation, merging, or notification flags unless they were asked for. The antipattern is anticipating future needs by parameterising every dimension. The fix is one function that does exactly what was asked. Future needs become future PRs.'
    ),
    ('surgical__drive_by_refactoring',
     'Surgical Changes',
     'When asked to "fix bug X", do NOT also tighten unrelated validation, add docstrings, rewrite comments, or "improve" adjacent code. The antipattern is shipping a 30-line diff for a 2-line bug. The fix is touching ONLY the lines that fix the reported bug. Note other issues separately; do not sneak them in.'
    ),
    ('surgical__style_drift',
     'Surgical Changes',
     'When adding a feature to existing code, do NOT change quote style, add type hints, restructure whitespace, or refactor adjacent control flow. The antipattern is making the diff hard to review by mixing the requested change with style preferences. The fix is matching the existing style exactly — single quotes if that is the file convention, no type hints if those are not used.'
    ),
    ('goal_driven__vague_vs_verifiable',
     'Goal-Driven Execution',
     'When asked to "fix the X system", do NOT respond with "I will review the code, identify issues, make improvements." The antipattern is starting work with no success criteria. The fix is to first identify the SPECIFIC problem (e.g. "users stay logged in after password change") and write the test that reproduces it. Verifier first, then code.'
    ),
    ('goal_driven__multi_step_verification',
     'Goal-Driven Execution',
     'When implementing a feature with multiple components, do NOT ship all components in one commit. The antipattern is a 300-line PR mixing in-memory limiter + Redis backend + middleware + config in one breath. The fix is to decompose into independently-verifiable steps with a test for each, deployable one at a time.'
    ),
    ('goal_driven__test_first',
     'Goal-Driven Execution',
     'When asked to "fix the bug where X breaks on Y", do NOT immediately change the code. The antipattern is editing the algorithm without confirming the bug reproduces. The fix is: write the test that reproduces the bug FIRST, watch it fail, THEN change the code, watch the test pass. If you did not watch the test fail, you do not know if you fixed anything.'
    )
),
target_entities AS (
    SELECT id AS entity_id FROM entity_registry
    WHERE status = 'active'
      AND entity_type IN ('native', 'service')
)
INSERT INTO entity_memory
    (entity_id, content, memory_type, importance, visibility, metadata)
SELECT
    te.entity_id,
    ke.content,
    'teaching',
    1.5,
    'topic',
    jsonb_build_object(
        'origin',     'external:karpathy',
        'source',     'EXAMPLES.md',
        'principle',  ke.principle,
        'example_id', ke.example_id
    )
FROM target_entities te
CROSS JOIN karpathy_examples ke
WHERE NOT EXISTS (
    SELECT 1 FROM entity_memory em
    WHERE em.entity_id = te.entity_id
      AND em.memory_type = 'teaching'
      AND em.metadata->>'origin' = 'external:karpathy'
      AND em.metadata->>'example_id' = ke.example_id
);
