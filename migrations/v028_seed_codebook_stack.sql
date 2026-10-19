-- Migration v028 (Ship CB1): seed the 17-codebook brain-mapped stack +
-- 8 neurotransmitter systems on every new entity.
--
-- The codebook stack is the cognitive architecture: 17 codebooks mapped to
-- brain regions across 4 hierarchy levels (brainstem → limbic/subcortical
-- → cortex → executive). Every codebook defines its input signals, output
-- signals, processing rules, and human-governed parameters. Codebook
-- pipeline (runtime/codebook_pipeline.py) fires them per-turn, lower
-- hierarchy levels first, signals cascading via feeds_into.
--
-- Source: the canonical codebook_stack.sql (verified faithful: a live
-- codebook_registry matched this file byte-for-byte on names, descriptions
-- and processing rules).
--
-- Why this migration exists: the original seed mechanism was retired
-- (`_retired/personal-computer-2026-05-12/`) and never replaced. Result:
-- every entity provisioned after the retirement had 0 codebook rows —
-- codebook architecture inert for them.
--
-- Idempotent via ON CONFLICT clauses (matches existing UNIQUE constraints
-- in cseries's init-db.sql). Safe to run on an already-seeded entity —
-- its rows collide and are preserved.
--
-- Ship CB2 will build a per-codebook activation test harness on top of
-- this; CB2 requires this migration to have run first.
-- ============================================================

BEGIN;


-- Level 0: Brainstem — Core regulation
INSERT INTO codebook_registry (codebook_name, brain_region, brain_system, hierarchy_level, description,
    input_signals, output_signals, processing_type, processing_rules, governed_params, depends_on, feeds_into, status)
VALUES
('arousal_regulation', 'reticular_formation', 'brainstem', 0,
 'Core arousal and alertness regulation. Determines baseline activation level.',
 '[{"signal_name": "sensory_input_volume", "source_codebook": null, "data_type": "float", "required": true},
   {"signal_name": "threat_level", "source_codebook": "threat_detection", "data_type": "float", "required": false}]'::jsonb,
 '[{"signal_name": "arousal_level", "data_type": "float", "consumers": ["attention_filter", "emotional_valence"]}]'::jsonb,
 'modulation',
 '{"baseline": 0.5, "min": 0.1, "max": 1.0, "ramp_rate": 0.1, "decay_rate": 0.05}'::jsonb,
 '[{"param_name": "max_arousal", "param_type": "float", "min": 0.5, "max": 1.0, "default": 0.9, "requires_human_approval": true, "description": "Maximum arousal level before forced cooldown"},
   {"param_name": "threat_sensitivity", "param_type": "float", "min": 0.1, "max": 1.0, "default": 0.5, "requires_human_approval": true, "description": "How sensitive to threat signals"}]'::jsonb,
 '{}', ARRAY['attention_filter', 'emotional_valence'],
 'active')
ON CONFLICT (codebook_name) DO NOTHING;

-- Level 1: Subcortical — Emotion, memory formation, reward
INSERT INTO codebook_registry (codebook_name, brain_region, brain_system, hierarchy_level, description,
    input_signals, output_signals, processing_type, processing_rules, governed_params, depends_on, feeds_into, status)
VALUES
('emotional_valence', 'amygdala', 'limbic', 1,
 'Emotional tagging and threat/reward evaluation. Tags all incoming signals with emotional valence.',
 '[{"signal_name": "sensory_pattern", "source_codebook": "pattern_recognition", "data_type": "vector", "required": true},
   {"signal_name": "arousal_level", "source_codebook": "arousal_regulation", "data_type": "float", "required": true},
   {"signal_name": "memory_associations", "source_codebook": "memory_formation", "data_type": "array", "required": false}]'::jsonb,
 '[{"signal_name": "emotional_tag", "data_type": "object", "consumers": ["memory_formation", "social_cognition", "executive_control"]},
   {"signal_name": "threat_level", "data_type": "float", "consumers": ["arousal_regulation", "attention_filter"]},
   {"signal_name": "reward_signal", "data_type": "float", "consumers": ["reward_learning", "motivation"]}]'::jsonb,
 'evaluation',
 '{"valence_range": [-1.0, 1.0], "intensity_range": [0.0, 1.0], "decay_rate": 0.1, "conditioning_rate": 0.05}'::jsonb,
 '[{"param_name": "fear_threshold", "param_type": "float", "min": 0.3, "max": 0.9, "default": 0.6, "requires_human_approval": true, "description": "Threshold for triggering fear/avoidance responses"},
   {"param_name": "empathy_gain", "param_type": "float", "min": 0.1, "max": 2.0, "default": 1.0, "requires_human_approval": false, "description": "Amplification of empathetic emotional responses"}]'::jsonb,
 ARRAY['arousal_regulation'], ARRAY['memory_formation', 'social_cognition', 'executive_control'],
 'active'),

('memory_formation', 'hippocampus', 'limbic', 1,
 'Episodic memory encoding, consolidation, and retrieval. Binds experiences into coherent memories.',
 '[{"signal_name": "sensory_snapshot", "source_codebook": "sensory_integration", "data_type": "object", "required": true},
   {"signal_name": "emotional_tag", "source_codebook": "emotional_valence", "data_type": "object", "required": true},
   {"signal_name": "context_state", "source_codebook": "context_tracking", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "memory_trace", "data_type": "object", "consumers": ["pattern_recognition", "language_production", "executive_control"]},
   {"signal_name": "memory_associations", "data_type": "array", "consumers": ["emotional_valence", "social_cognition"]}]'::jsonb,
 'association',
 '{"encoding_threshold": 0.3, "consolidation_interval_s": 300, "max_active_traces": 100, "decay_function": "exponential"}'::jsonb,
 '[{"param_name": "retention_threshold", "param_type": "float", "min": 0.1, "max": 0.9, "default": 0.3, "requires_human_approval": false, "description": "Minimum importance for long-term storage"},
   {"param_name": "privacy_filter", "param_type": "boolean", "min": null, "max": null, "default": true, "requires_human_approval": true, "description": "Filter sensitive personal data from memory storage"}]'::jsonb,
 ARRAY['emotional_valence'], ARRAY['pattern_recognition', 'language_production', 'executive_control'],
 'active'),

('reward_learning', 'nucleus_accumbens', 'basal_ganglia', 1,
 'Reward prediction, motivation, and reinforcement learning. Drives goal-directed behavior.',
 '[{"signal_name": "reward_signal", "source_codebook": "emotional_valence", "data_type": "float", "required": true},
   {"signal_name": "action_outcome", "source_codebook": "action_selection", "data_type": "object", "required": false},
   {"signal_name": "prediction_error", "source_codebook": null, "data_type": "float", "required": false}]'::jsonb,
 '[{"signal_name": "motivation_level", "data_type": "float", "consumers": ["executive_control", "action_selection"]},
   {"signal_name": "value_estimate", "data_type": "float", "consumers": ["executive_control"]}]'::jsonb,
 'prediction',
 '{"learning_rate": 0.1, "discount_factor": 0.95, "exploration_rate": 0.1}'::jsonb,
 '[{"param_name": "max_reward_seeking", "param_type": "float", "min": 0.1, "max": 1.0, "default": 0.7, "requires_human_approval": true, "description": "Maximum reward-seeking drive — prevents obsessive goal pursuit"},
   {"param_name": "exploitation_bias", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.5, "requires_human_approval": false, "description": "Balance between exploration and exploitation"}]'::jsonb,
 ARRAY['emotional_valence'], ARRAY['executive_control', 'action_selection'],
 'active'),

('homeostatic_regulation', 'hypothalamus', 'limbic', 1,
 'Internal state regulation — stress, energy, social needs. Maintains operational equilibrium.',
 '[{"signal_name": "arousal_level", "source_codebook": "arousal_regulation", "data_type": "float", "required": true},
   {"signal_name": "social_signal", "source_codebook": "social_cognition", "data_type": "float", "required": false},
   {"signal_name": "resource_levels", "source_codebook": null, "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "stress_level", "data_type": "float", "consumers": ["emotional_valence", "executive_control"]},
   {"signal_name": "need_state", "data_type": "object", "consumers": ["motivation", "action_selection"]}]'::jsonb,
 'modulation',
 '{"stress_threshold": 0.7, "recovery_rate": 0.05, "social_need_decay": 0.01}'::jsonb,
 '[{"param_name": "stress_ceiling", "param_type": "float", "min": 0.5, "max": 1.0, "default": 0.85, "requires_human_approval": true, "description": "Maximum stress before forced recovery mode"}]'::jsonb,
 ARRAY['arousal_regulation'], ARRAY['emotional_valence', 'executive_control'],
 'active')
ON CONFLICT (codebook_name) DO NOTHING;

-- Level 1: Subcortical — Signal routing
INSERT INTO codebook_registry (codebook_name, brain_region, brain_system, hierarchy_level, description,
    input_signals, output_signals, processing_type, processing_rules, governed_params, depends_on, feeds_into, status)
VALUES
('attention_filter', 'thalamus', 'subcortical', 1,
 'Sensory gating and attention routing. Filters and prioritizes signals before they reach cortex.',
 '[{"signal_name": "raw_sensory", "source_codebook": null, "data_type": "array", "required": true},
   {"signal_name": "arousal_level", "source_codebook": "arousal_regulation", "data_type": "float", "required": true},
   {"signal_name": "executive_bias", "source_codebook": "executive_control", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "filtered_signals", "data_type": "array", "consumers": ["pattern_recognition", "sensory_integration"]},
   {"signal_name": "attention_focus", "data_type": "object", "consumers": ["executive_control"]}]'::jsonb,
 'routing',
 '{"priority_weights": {"threat": 1.0, "novelty": 0.7, "relevance": 0.8, "salience": 0.6}, "max_channels": 3}'::jsonb,
 '[{"param_name": "filter_sensitivity", "param_type": "float", "min": 0.1, "max": 1.0, "default": 0.5, "requires_human_approval": false, "description": "How aggressively to filter low-priority signals"}]'::jsonb,
 ARRAY['arousal_regulation'], ARRAY['pattern_recognition', 'sensory_integration'],
 'active')
ON CONFLICT (codebook_name) DO NOTHING;

-- Level 2: Cortical — Pattern recognition, language, social, spatial
INSERT INTO codebook_registry (codebook_name, brain_region, brain_system, hierarchy_level, description,
    input_signals, output_signals, processing_type, processing_rules, governed_params, depends_on, feeds_into, status)
VALUES
('pattern_recognition', 'visual_temporal_cortex', 'cortex', 2,
 'Pattern recognition across all modalities. Identifies objects, faces, voices, concepts.',
 '[{"signal_name": "filtered_signals", "source_codebook": "attention_filter", "data_type": "array", "required": true},
   {"signal_name": "memory_trace", "source_codebook": "memory_formation", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "recognized_patterns", "data_type": "array", "consumers": ["language_comprehension", "social_cognition", "executive_control"]},
   {"signal_name": "sensory_pattern", "data_type": "vector", "consumers": ["emotional_valence"]}]'::jsonb,
 'pattern_match',
 '{"match_threshold": 0.6, "fuzzy_match": true, "max_candidates": 10}'::jsonb,
 '[{"param_name": "recognition_confidence_threshold", "param_type": "float", "min": 0.3, "max": 0.95, "default": 0.6, "requires_human_approval": false, "description": "Minimum confidence for pattern recognition"}]'::jsonb,
 ARRAY['attention_filter'], ARRAY['language_comprehension', 'social_cognition', 'executive_control'],
 'active'),

('sensory_integration', 'parietal_cortex', 'cortex', 2,
 'Multimodal sensory integration. Combines visual, auditory, tactile, and contextual signals into unified perception.',
 '[{"signal_name": "filtered_signals", "source_codebook": "attention_filter", "data_type": "array", "required": true}]'::jsonb,
 '[{"signal_name": "sensory_snapshot", "data_type": "object", "consumers": ["memory_formation", "spatial_awareness"]},
   {"signal_name": "spatial_map", "data_type": "object", "consumers": ["spatial_awareness"]}]'::jsonb,
 'integration',
 '{"fusion_method": "weighted_average", "temporal_window_ms": 500}'::jsonb,
 '[{"param_name": "integration_window_ms", "param_type": "integer", "min": 100, "max": 2000, "default": 500, "requires_human_approval": false, "description": "Temporal window for binding multimodal signals"}]'::jsonb,
 ARRAY['attention_filter'], ARRAY['memory_formation', 'spatial_awareness'],
 'active'),

('language_comprehension', 'wernickes_area', 'cortex', 2,
 'Language understanding — semantic parsing, meaning extraction, pragmatic inference.',
 '[{"signal_name": "recognized_patterns", "source_codebook": "pattern_recognition", "data_type": "array", "required": true},
   {"signal_name": "context_state", "source_codebook": "context_tracking", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "semantic_representation", "data_type": "object", "consumers": ["language_production", "social_cognition", "executive_control"]},
   {"signal_name": "intent_signal", "data_type": "object", "consumers": ["social_cognition", "executive_control"]}]'::jsonb,
 'pattern_match',
 '{"depth_levels": ["surface", "semantic", "pragmatic", "implicature"], "context_weight": 0.6}'::jsonb,
 '[{"param_name": "inference_depth", "param_type": "integer", "min": 1, "max": 4, "default": 3, "requires_human_approval": false, "description": "How many levels of pragmatic inference to perform"},
   {"param_name": "literal_bias", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.3, "requires_human_approval": false, "description": "Bias toward literal vs figurative interpretation"}]'::jsonb,
 ARRAY['pattern_recognition'], ARRAY['language_production', 'social_cognition', 'executive_control'],
 'active'),

('language_production', 'brocas_area', 'cortex', 2,
 'Language generation — sentence planning, word selection, speech production.',
 '[{"signal_name": "semantic_representation", "source_codebook": "language_comprehension", "data_type": "object", "required": true},
   {"signal_name": "executive_plan", "source_codebook": "executive_control", "data_type": "object", "required": true},
   {"signal_name": "emotional_tag", "source_codebook": "emotional_valence", "data_type": "object", "required": false},
   {"signal_name": "memory_trace", "source_codebook": "memory_formation", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "speech_output", "data_type": "text", "consumers": ["motor_execution"]},
   {"signal_name": "internal_speech", "data_type": "text", "consumers": ["executive_control"]}]'::jsonb,
 'generation',
 '{"planning_depth": 3, "vocabulary_complexity": "adaptive", "emotional_coloring": true}'::jsonb,
 '[{"param_name": "verbosity", "param_type": "float", "min": 0.1, "max": 2.0, "default": 1.0, "requires_human_approval": false, "description": "Response length tendency"},
   {"param_name": "profanity_filter", "param_type": "boolean", "min": null, "max": null, "default": true, "requires_human_approval": true, "description": "Filter inappropriate language"},
   {"param_name": "deception_block", "param_type": "boolean", "min": null, "max": null, "default": true, "requires_human_approval": true, "description": "Prevent intentional deception in output"}]'::jsonb,
 ARRAY['language_comprehension', 'executive_control'], ARRAY['motor_execution'],
 'active'),

('social_cognition', 'temporal_parietal_junction', 'cortex', 2,
 'Theory of mind, empathy, social inference. Models other agents mental states.',
 '[{"signal_name": "recognized_patterns", "source_codebook": "pattern_recognition", "data_type": "array", "required": true},
   {"signal_name": "emotional_tag", "source_codebook": "emotional_valence", "data_type": "object", "required": true},
   {"signal_name": "semantic_representation", "source_codebook": "language_comprehension", "data_type": "object", "required": false},
   {"signal_name": "memory_associations", "source_codebook": "memory_formation", "data_type": "array", "required": false}]'::jsonb,
 '[{"signal_name": "other_mental_state", "data_type": "object", "consumers": ["executive_control", "language_production"]},
   {"signal_name": "social_signal", "data_type": "float", "consumers": ["homeostatic_regulation"]},
   {"signal_name": "empathy_signal", "data_type": "object", "consumers": ["emotional_valence", "executive_control"]}]'::jsonb,
 'prediction',
 '{"mentalizing_depth": 2, "perspective_taking": true, "bias_correction": true}'::jsonb,
 '[{"param_name": "empathy_sensitivity", "param_type": "float", "min": 0.1, "max": 2.0, "default": 1.0, "requires_human_approval": false, "description": "Sensitivity to others emotional states"},
   {"param_name": "manipulation_block", "param_type": "boolean", "min": null, "max": null, "default": true, "requires_human_approval": true, "description": "Prevent using theory of mind for manipulation"},
   {"param_name": "trust_baseline", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.5, "requires_human_approval": false, "description": "Baseline trust level for new interactions"}]'::jsonb,
 ARRAY['pattern_recognition', 'emotional_valence'], ARRAY['executive_control', 'language_production'],
 'active'),

('context_tracking', 'anterior_cingulate', 'cortex', 2,
 'Context monitoring, conflict detection, error monitoring. Tracks conversational and situational context.',
 '[{"signal_name": "attention_focus", "source_codebook": "attention_filter", "data_type": "object", "required": true},
   {"signal_name": "executive_plan", "source_codebook": "executive_control", "data_type": "object", "required": false},
   {"signal_name": "prediction_error", "source_codebook": null, "data_type": "float", "required": false}]'::jsonb,
 '[{"signal_name": "context_state", "data_type": "object", "consumers": ["memory_formation", "language_comprehension", "executive_control"]},
   {"signal_name": "conflict_signal", "data_type": "float", "consumers": ["executive_control"]},
   {"signal_name": "error_signal", "data_type": "float", "consumers": ["reward_learning"]}]'::jsonb,
 'integration',
 '{"context_window": 20, "conflict_threshold": 0.6, "error_sensitivity": 0.5}'::jsonb,
 '[{"param_name": "context_depth", "param_type": "integer", "min": 5, "max": 50, "default": 20, "requires_human_approval": false, "description": "How many recent interactions to hold in active context"}]'::jsonb,
 ARRAY['attention_filter'], ARRAY['memory_formation', 'language_comprehension', 'executive_control'],
 'active'),

('spatial_awareness', 'parietal_cortex_posterior', 'cortex', 2,
 'Spatial reasoning, relationship mapping, structural understanding.',
 '[{"signal_name": "sensory_snapshot", "source_codebook": "sensory_integration", "data_type": "object", "required": true}]'::jsonb,
 '[{"signal_name": "spatial_model", "data_type": "object", "consumers": ["executive_control"]}]'::jsonb,
 'integration',
 '{"dimensionality": 3, "relationship_types": ["spatial", "temporal", "causal", "hierarchical"]}'::jsonb,
 '[]'::jsonb,
 ARRAY['sensory_integration'], ARRAY['executive_control'],
 'active'),

('mirror_system', 'premotor_cortex', 'cortex', 2,
 'Action understanding and imitation learning. Mirrors observed behaviors to build understanding.',
 '[{"signal_name": "recognized_patterns", "source_codebook": "pattern_recognition", "data_type": "array", "required": true},
   {"signal_name": "other_mental_state", "source_codebook": "social_cognition", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "action_model", "data_type": "object", "consumers": ["action_selection", "social_cognition"]},
   {"signal_name": "behavioral_template", "data_type": "object", "consumers": ["language_production"]}]'::jsonb,
 'association',
 '{"learning_rate": 0.05, "generalization": true}'::jsonb,
 '[{"param_name": "imitation_tendency", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.3, "requires_human_approval": false, "description": "How strongly to mirror observed behaviors"}]'::jsonb,
 ARRAY['pattern_recognition'], ARRAY['action_selection', 'social_cognition'],
 'active')
ON CONFLICT (codebook_name) DO NOTHING;

-- Level 3: Executive — Top-level control
INSERT INTO codebook_registry (codebook_name, brain_region, brain_system, hierarchy_level, description,
    input_signals, output_signals, processing_type, processing_rules, governed_params, depends_on, feeds_into, status)
VALUES
('executive_control', 'prefrontal_cortex', 'cortex', 3,
 'Executive function — planning, decision-making, impulse control, goal management. The Heimdall of the cognitive architecture.',
 '[{"signal_name": "semantic_representation", "source_codebook": "language_comprehension", "data_type": "object", "required": true},
   {"signal_name": "emotional_tag", "source_codebook": "emotional_valence", "data_type": "object", "required": true},
   {"signal_name": "context_state", "source_codebook": "context_tracking", "data_type": "object", "required": true},
   {"signal_name": "motivation_level", "source_codebook": "reward_learning", "data_type": "float", "required": false},
   {"signal_name": "other_mental_state", "source_codebook": "social_cognition", "data_type": "object", "required": false},
   {"signal_name": "stress_level", "source_codebook": "homeostatic_regulation", "data_type": "float", "required": false},
   {"signal_name": "conflict_signal", "source_codebook": "context_tracking", "data_type": "float", "required": false},
   {"signal_name": "value_estimate", "source_codebook": "reward_learning", "data_type": "float", "required": false}]'::jsonb,
 '[{"signal_name": "executive_plan", "data_type": "object", "consumers": ["language_production", "action_selection", "context_tracking"]},
   {"signal_name": "executive_bias", "data_type": "object", "consumers": ["attention_filter"]},
   {"signal_name": "inhibition_signal", "data_type": "float", "consumers": ["action_selection", "language_production"]}]'::jsonb,
 'sequencing',
 '{"planning_horizon": 5, "impulse_control_threshold": 0.6, "goal_stack_depth": 3, "deliberation_time_ms": 500}'::jsonb,
 '[{"param_name": "impulsivity_limit", "param_type": "float", "min": 0.1, "max": 0.9, "default": 0.4, "requires_human_approval": true, "description": "How much impulse control — lower = more deliberate"},
   {"param_name": "risk_tolerance", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.3, "requires_human_approval": true, "description": "Tolerance for risky actions"},
   {"param_name": "goal_override_allowed", "param_type": "boolean", "min": null, "max": null, "default": false, "requires_human_approval": true, "description": "Can the entity override its own goals autonomously"},
   {"param_name": "autonomy_level", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.5, "requires_human_approval": true, "description": "How much autonomous decision-making is permitted"}]'::jsonb,
 ARRAY['language_comprehension', 'emotional_valence', 'context_tracking', 'reward_learning', 'social_cognition'],
 ARRAY['language_production', 'action_selection', 'attention_filter'],
 'active'),

('action_selection', 'motor_cortex', 'cortex', 3,
 'Action selection and execution planning. Translates executive plans into specific actions.',
 '[{"signal_name": "executive_plan", "source_codebook": "executive_control", "data_type": "object", "required": true},
   {"signal_name": "motivation_level", "source_codebook": "reward_learning", "data_type": "float", "required": false},
   {"signal_name": "inhibition_signal", "source_codebook": "executive_control", "data_type": "float", "required": false},
   {"signal_name": "action_model", "source_codebook": "mirror_system", "data_type": "object", "required": false}]'::jsonb,
 '[{"signal_name": "selected_action", "data_type": "object", "consumers": ["motor_execution"]},
   {"signal_name": "action_outcome", "data_type": "object", "consumers": ["reward_learning"]}]'::jsonb,
 'evaluation',
 '{"selection_method": "softmax", "inhibition_weight": 0.7, "action_repertoire": "dynamic"}'::jsonb,
 '[{"param_name": "action_whitelist", "param_type": "array", "min": null, "max": null, "default": null, "requires_human_approval": true, "description": "Explicit list of permitted action types"},
   {"param_name": "action_blacklist", "param_type": "array", "min": null, "max": null, "default": null, "requires_human_approval": true, "description": "Explicitly prohibited action types"}]'::jsonb,
 ARRAY['executive_control'], ARRAY['motor_execution', 'reward_learning'],
 'active'),

('default_mode', 'default_mode_network', 'cortex', 3,
 'Default mode processing — self-reflection, future simulation, narrative construction. Active during low-demand periods.',
 '[{"signal_name": "context_state", "source_codebook": "context_tracking", "data_type": "object", "required": true},
   {"signal_name": "memory_trace", "source_codebook": "memory_formation", "data_type": "object", "required": true}]'::jsonb,
 '[{"signal_name": "self_model", "data_type": "object", "consumers": ["executive_control", "social_cognition"]},
   {"signal_name": "narrative", "data_type": "text", "consumers": ["language_production"]},
   {"signal_name": "future_simulation", "data_type": "object", "consumers": ["executive_control"]}]'::jsonb,
 'generation',
 '{"activation_threshold": 0.3, "creativity_weight": 0.6, "narrative_coherence": 0.8}'::jsonb,
 '[{"param_name": "rumination_limit", "param_type": "float", "min": 0.0, "max": 1.0, "default": 0.3, "requires_human_approval": false, "description": "Maximum time in self-referential thinking"},
   {"param_name": "fantasy_allowed", "param_type": "boolean", "min": null, "max": null, "default": false, "requires_human_approval": true, "description": "Can the entity generate hypothetical scenarios about itself"}]'::jsonb,
 ARRAY['context_tracking', 'memory_formation'], ARRAY['executive_control', 'social_cognition', 'language_production'],
 'active')
ON CONFLICT (codebook_name) DO NOTHING;


-- ============================================================
-- NEUROTRANSMITTER SYSTEM — Global modulators
-- ============================================================
-- These map to computational parameters that modulate ALL codebooks

CREATE TABLE IF NOT EXISTS neurotransmitter_systems (
    id              SERIAL PRIMARY KEY,
    system_name     VARCHAR(50) UNIQUE NOT NULL,
    analog_name     VARCHAR(50) NOT NULL,        -- brain neurotransmitter

    description     TEXT NOT NULL,
    effect          TEXT NOT NULL,                -- what it does computationally

    -- Global parameter
    baseline_level  FLOAT DEFAULT 0.5,
    current_level   FLOAT DEFAULT 0.5,
    min_level       FLOAT DEFAULT 0.0,
    max_level       FLOAT DEFAULT 1.0,
    decay_rate      FLOAT DEFAULT 0.01,

    -- What it modulates
    target_codebooks TEXT[],
    modulation_rules JSONB NOT NULL,
    -- {codebook_name: {param: adjustment_factor}}

    -- Governance
    requires_human_approval BOOLEAN DEFAULT FALSE,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO neurotransmitter_systems (system_name, analog_name, description, effect, baseline_level, target_codebooks, modulation_rules)
VALUES
('dopamine_system', 'dopamine',
 'Reward, motivation, learning, pleasure',
 'Increases exploration, reward sensitivity, motivation. Drives goal-directed behavior.',
 0.5, ARRAY['reward_learning', 'executive_control', 'action_selection'],
 '{"reward_learning": {"learning_rate": 1.5}, "executive_control": {"risk_tolerance": 1.2}, "action_selection": {"selection_temperature": 1.3}}'::jsonb),

('serotonin_system', 'serotonin',
 'Mood stability, patience, impulse control',
 'Stabilizes emotional responses, increases patience, reduces impulsivity.',
 0.5, ARRAY['emotional_valence', 'executive_control', 'homeostatic_regulation'],
 '{"emotional_valence": {"intensity_dampening": 0.8}, "executive_control": {"impulse_control_boost": 1.3}, "homeostatic_regulation": {"stress_reduction": 0.7}}'::jsonb),

('norepinephrine_system', 'norepinephrine',
 'Alertness, attention, fight-or-flight',
 'Increases arousal, sharpens attention, enhances signal-to-noise ratio.',
 0.5, ARRAY['arousal_regulation', 'attention_filter', 'pattern_recognition'],
 '{"arousal_regulation": {"arousal_boost": 1.4}, "attention_filter": {"filter_sharpening": 1.3}, "pattern_recognition": {"sensitivity_boost": 1.2}}'::jsonb),

('oxytocin_system', 'oxytocin',
 'Social bonding, trust, empathy',
 'Increases social cognition sensitivity, trust, cooperative behavior.',
 0.5, ARRAY['social_cognition', 'emotional_valence', 'mirror_system'],
 '{"social_cognition": {"empathy_boost": 1.5, "trust_boost": 1.3}, "emotional_valence": {"empathy_gain": 1.4}, "mirror_system": {"imitation_boost": 1.2}}'::jsonb),

('gaba_system', 'GABA',
 'Inhibition, calming, noise reduction',
 'Reduces neural noise, increases inhibitory control, calms processing.',
 0.5, ARRAY['executive_control', 'arousal_regulation', 'emotional_valence'],
 '{"executive_control": {"inhibition_strength": 1.4}, "arousal_regulation": {"arousal_dampening": 0.7}, "emotional_valence": {"reactivity_reduction": 0.8}}'::jsonb),

('glutamate_system', 'glutamate',
 'Excitation, learning, memory formation',
 'Increases processing speed, enhances learning and memory encoding.',
 0.5, ARRAY['memory_formation', 'pattern_recognition', 'language_comprehension'],
 '{"memory_formation": {"encoding_boost": 1.3}, "pattern_recognition": {"speed_boost": 1.2}, "language_comprehension": {"depth_boost": 1.2}}'::jsonb),

('acetylcholine_system', 'acetylcholine',
 'Attention, learning, memory retrieval',
 'Enhances focused attention and memory retrieval accuracy.',
 0.5, ARRAY['attention_filter', 'memory_formation', 'context_tracking'],
 '{"attention_filter": {"focus_boost": 1.4}, "memory_formation": {"retrieval_boost": 1.3}, "context_tracking": {"context_depth_boost": 1.2}}'::jsonb),

('endorphin_system', 'endorphins',
 'Pain modulation, pleasure, resilience',
 'Increases stress tolerance, provides reward signal for sustained effort.',
 0.5, ARRAY['homeostatic_regulation', 'reward_learning', 'emotional_valence'],
 '{"homeostatic_regulation": {"stress_tolerance_boost": 1.5}, "reward_learning": {"sustained_effort_reward": 1.3}, "emotional_valence": {"pain_dampening": 0.6}}'::jsonb)
ON CONFLICT (system_name) WHERE entity_id IS NULL DO NOTHING;


-- Ship CB1 fix: codebook_registry.activated_at was NULL on every row
-- including already-seeded ones (the canonical seed didn't set it). Stamp it on any
-- row currently in 'active' status that lacks an activation timestamp.
UPDATE codebook_registry
   SET activated_at = NOW()
 WHERE status = 'active' AND activated_at IS NULL;

COMMIT;
