-- v214: a model call whose provider reported no usage is stored as unknown.
--
-- Measured on one Host (2026-10-07): two xAI calls came back with
-- no usage object at all (the pod log printed in=None out=None) and were
-- written as 0 in and 0 out, which every rollup then read as two calls that
-- used nothing. The writer (provider_gateway._log_inference) now writes NULL
-- for a count the provider did not report and marks the row's metadata
-- {"usage": "not_reported"}; SUM() skips NULL, and the readers say how many
-- calls are unknown instead of adding zeros.
--
-- Defaults stay 0 for writers that never pass the column. Idempotent: DROP NOT
-- NULL on a nullable column is a no-op.
ALTER TABLE inference_cost_log ALTER COLUMN tokens_in DROP NOT NULL;
ALTER TABLE inference_cost_log ALTER COLUMN tokens_out DROP NOT NULL;
ALTER TABLE inference_cost_log ALTER COLUMN cache_read_tokens DROP NOT NULL;
ALTER TABLE inference_cost_log ALTER COLUMN cache_write_tokens DROP NOT NULL;
