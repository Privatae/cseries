-- v008: everything-claude-code agent-meta skills selective pull
--
-- Source: affaan-m/everything-claude-code (100k stars; 233 skills total).
-- The prompt-pipeline audit
-- said: cherry-pick 5 agent-meta skills. After review:
--   - verification-loop  → SKIP, fully covered by superpowers'
--     verification-before-completion (already in v005).
--   - continuous-learning → SKIP, ECC marks it DEPRECATED in favour of
--     continuous-learning-v2 which is ECC-runtime-specific (stop-hook
--     based). Defer until cseries has its own analog.
--   - agent-architecture-audit  → behavioral_correction (hard rule).
--   - agent-introspection-debugging → behavioral_correction (hard rule).
--   - deep-research → ContextStore skill (workflow, not discipline).
--
-- Idempotent on (entity_id, origin) for each row.

-- Two hard behavioral_corrections.
WITH ecc_corrections(idx, correction, trigger_context) AS (
    VALUES
    (1,
     'Agent architecture audit. Before releasing agent or LLM-powered code to production, run a multi-layer audit: wrapper regression, memory pollution, tool discipline, hidden retry loops, rendering corruption. Especially when behaviour degrades after adding wrapper layers, or when the same model works in playground but breaks inside the wrapper. Do not ship agent code without auditing the layers between prompt and wire.',
     'shipping agent code, LLM wrapper, autonomous loop, agent harness; behaviour degraded after adding a wrapper layer; same model works elsewhere but not inside our wrapper'
    ),
    (2,
     'Agent introspection debugging. When an agent run is looping, consuming tokens without progress, retrying the same tool, or drifting away from the task: capture the failure state BEFORE retrying. Diagnose the specific pattern — tool failure, context drift, environment mismatch, prompt regression. Apply ONE contained corrective action, then re-evaluate. Blind retries waste tokens and hide root causes.',
     'agent run is failing repeatedly; tool calls looping; agent drifting from task; context growth degrading quality; max tool-call limit hit'
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
    te.entity_id, ec.correction, ec.trigger_context,
    'correction', 'hard', TRUE, 'external:ecc'
FROM target_entities te
CROSS JOIN ecc_corrections ec
WHERE NOT EXISTS (
    SELECT 1 FROM behavioral_corrections bc
    WHERE bc.entity_id = te.entity_id
      AND bc.origin   = 'external:ecc'
      AND LEFT(bc.correction, 40) = LEFT(ec.correction, 40)
);

-- One ContextStore how-to skill.
INSERT INTO entity_context
    (entity_id, file_path, content_type, content, metadata)
SELECT
    te.entity_id,
    'skills/deep_research.md',
    'text/markdown',
    $$Multi-source research with web tools.

Process:
1. State the question precisely. Decompose if it has multiple sub-questions.
2. Identify 3-5 source types appropriate to the question (academic papers, official docs, news, community forums, primary data).
3. Search each source via the relevant tool (web search, MCP firecrawl, MCP exa, etc.).
4. Synthesize findings — group converging claims, flag disagreements, mark gaps.
5. Deliver as a cited report. Every claim has a source link. Never claim a finding without a citation.

Don't promise coverage you can't verify. If an MCP source is unavailable, say so and proceed with what you have.

Origin: external:ecc/deep-research
$$,
    jsonb_build_object('origin', 'external:ecc', 'slug', 'deep_research')
FROM (
    SELECT id AS entity_id FROM entity_registry
    WHERE status = 'active'
      AND entity_type IN ('native', 'service')
) te
ON CONFLICT (entity_id, file_path) DO NOTHING;
