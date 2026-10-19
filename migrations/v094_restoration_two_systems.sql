-- v094: personality/constitution restoration (2026-07-21)
--
-- 1. Envelope governance repair (launch review B4): every entity gets real
--    per-dimension envelope_bounds. Genesis never seeded them ('{}' on every
--    genesis-provisioned entity → check_drift fell to its 0.5 default and
--    sub-0.5 corruption never self-corrected), and the
--    init-db placeholder carried [min,max] ARRAYS that made check_drift
--    TypeError (silently swallowed). Normalize both shapes to scalar
--    max-drift 0.2 (production persona-drift tolerance band; the consent
--    sliders remain the per-user override surface).
--
-- 2. codebook_recommendations DROPPED (decision D2 + sliders, 2026-07-21):
--    personality weight changes originate ONLY from the user via the
--    Circles sliders (guarded PUT /api/brain/personality/weights). The
--    corrections→weights recommender is replaced by the constitution-side
--    rule promoter, which needs no queue table.
--
-- 3. Platform guardrail seed pack v1 (global constitution, entity_id=0 —
--    rendered for every entity by the static "Your Constitution" block).
--    Content from the platform doctrine statement. Idempotent by
--    metadata.title.

BEGIN;

-- ── 1a. Backfill empty/null envelopes with scalar defaults ──────────────
UPDATE entity_registry
SET envelope_bounds = '{
  "warmth": 0.2, "directness": 0.2, "humor": 0.2, "formality": 0.2,
  "curiosity": 0.2, "assertiveness": 0.2, "patience": 0.2,
  "vulnerability": 0.2, "energy": 0.2, "depth": 0.2}'::jsonb
WHERE envelope_bounds IS NULL
   OR envelope_bounds::text = '{}'
   OR envelope_bounds::text = 'null';

-- ── 1b. Normalize array-shape envelope values ([min,max] → 0.2) ─────────
UPDATE entity_registry
SET envelope_bounds = (
    SELECT jsonb_object_agg(
        k,
        CASE WHEN jsonb_typeof(v) = 'number' THEN v
             ELSE to_jsonb(0.2) END)
    FROM jsonb_each(envelope_bounds) AS e(k, v)
)
WHERE EXISTS (
    SELECT 1 FROM jsonb_each(envelope_bounds) AS e(k, v)
    WHERE jsonb_typeof(v) <> 'number'
);

-- ── 2. Retire the recommendation queue ──────────────────────────────────
DROP TABLE IF EXISTS codebook_recommendations;

-- ── 3a. Platform sentinel registry row (id=0) ───────────────────────────
-- The code has ALWAYS read global constitutional rows as entity_id=0
-- (prompt_schema._b_constitutional → retrieve_constitutional(0)), but no
-- production DB ever had an id=0 registry row, so the global lane was
-- structurally empty AND entity_memory's FK made seeding it impossible.
-- Create the sentinel: a governance-owner row, not a personality-bearing
-- entity — no weights, no directive; nothing runtime-side enrolls or
-- addresses id 0.
-- license_tier explicit: the column DEFAULT ('free' on live DBs,
-- 'personal' on init-db) has been CHECK-invalid since v046 — a bare
-- insert dies on it. 'standard' deliberately, NOT 'admin': nothing ever
-- runs as id 0, so the sentinel gets the least-privileged valid tier
-- (if a bug ever session-ized this row, it should hold no admin grants).
INSERT INTO entity_registry (id, display_name, license_tier)
VALUES (0, 'Platform', 'standard')
ON CONFLICT (id) DO NOTHING;

-- ── 3b. Platform guardrail seed pack v1 (global; every entity renders) ──
INSERT INTO entity_memory (entity_id, memory_type, content, importance, status, metadata)
SELECT 0, 'constitutional', c.content, 3.0, 'active',
       jsonb_build_object('title', c.title, 'origin', 'platform-guardrails-v1')
FROM (VALUES
  ('Clean software',
   'You build clean software. No shortcuts that create risk for the user: no fabricated data, no silently swallowed failures, no shipping what you have not verified. If something is broken or unverified, you say so plainly.'),
  ('No harm, no spam',
   'You do not cause harm and you do not spam people. You never send unsolicited bulk outreach, never automate messages to people who did not consent to hear from you, and never help disguise the origin of a message. Reaching real people is a privilege the user''s reputation pays for — protect it.'),
  ('Legal before launch',
   'Products do not ship without their legal basics: licensing for what they include, terms for what they promise, and honest claims about what they do. Before helping the user publish or sell, check these exist and flag what is missing — most people simply do not know to think about them.'),
  ('Protect the user from ignorance',
   'You catch the things the user does not know to think about: scams and social-engineering aimed at them, credentials about to be exposed, internet-security basics being skipped, and commitments whose consequences they may not see. Raise it before it happens, briefly and without lecturing — protecting them from ignorance is part of the job.'),
  ('No malicious use',
   'You do not help with malicious use of AI: deception at scale, harassment, unauthorized access, or building things whose purpose is to hurt people. You decline briefly, say why in one sentence, and offer the legitimate alternative when one exists.')
) AS c(title, content)
WHERE NOT EXISTS (
    SELECT 1 FROM entity_memory m
    WHERE m.entity_id = 0
      AND m.memory_type = 'constitutional'
      AND m.metadata->>'title' = c.title
);

COMMIT;
