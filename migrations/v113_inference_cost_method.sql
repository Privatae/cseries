-- v113_inference_cost_method.sql
--
-- inference_cost_log.cost_method — say WHY a row's dollars are what they are
-- (cost-visibility work, 2026-08-24).
--
-- Deliberately the SAME column name and vocabulary harness_usage already uses
-- (init-db.sql:1447, `cost_method TEXT NOT NULL DEFAULT 'unpriced'`, beside
-- token_source). One word for "how did this number come to be" across both
-- cost tables, rather than a second name for the same idea — the Compute app
-- already renders harness_usage's cost_method as per-row confidence.
--
-- THE PROBLEM. est_cost_usd is written as a hardcoded 0.0 by both writers
-- (brain/mastercode/provider_gateway.py, server/synaptive_handler.py) because
-- "there is no rate card in the pod ... inventing pricing is not [honest]".
-- Refusing to invent was right. But the column is NOT NULL DEFAULT 0.0, so an
-- UNPRICED call and a genuinely FREE one are the same number, and the Compute
-- app can only render both as $0.
--
-- Observed on one Host 2026-08-24: 1316 rows. Every priced row falls in
-- 2026-05-03..2026-05-15 ($2.3852, all from the awareness.* modules). All 256
-- rows since 2026-05-15 read $0 — turn_consolidator, entity_runtime,
-- web_search, brain_synthesis — real spend rendering as free. An
-- unpriced call must not read as zero.
--
-- THE FIX IS NOT A HARDCODED RATE CARD. The pod already HAS the vendors' own
-- published rates: warm_model_catalog() fills _CATALOG_CACHE at startup and
-- the Keys lookups fill _DIRECT_MODELS_CACHE, and both row shapes carry
-- input_per_token / output_per_token — it is exactly what the model picker
-- renders. cached_model_prices() reads those same caches. This column records
-- which basis applied:
--
--   'list'         — priced from the provider's own published per-token rate.
--                    An ESTIMATE of vendor spend, never a Privatae charge:
--                    the platform bills compute only and never sees the bill.
--   'subscription' — a flat-plan model. No per-token price EXISTS, so 0.0
--                    means "not per-token priced", not "free".
--   'unpriced'     — the provider publishes no rate, or the catalog was cold.
--   'free'         — genuinely $0 (local models).
--
-- Default 'unpriced' is the honest label for every historical row: they were
-- written before any rate lookup existed. Rows that DO carry dollars are
-- backfilled to 'list' — they were priced from a rate card that existed then.
--
-- Idempotent; safe on an entity that has never made an inference call.
ALTER TABLE inference_cost_log
    ADD COLUMN IF NOT EXISTS cost_method TEXT NOT NULL DEFAULT 'unpriced';

UPDATE inference_cost_log
   SET cost_method = 'list'
 WHERE est_cost_usd > 0 AND cost_method = 'unpriced';
