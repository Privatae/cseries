"""
runtime/tools.py — Tool schema building and execution helpers.

Contains the functions that build per-entity tool schemas (CEIGAS-filtered),
parameterize descriptions with entity config, execute meta-tools inline,
and the thin _call_entity_model wrapper over provider_gateway.

Also re-exports XML tool prompt helpers from provider_gateway.
"""

import asyncio
import collections
import difflib
import json
import logging
import os
import re
from pathlib import Path

from mastercode.provider_gateway import (
    call_model as _call_entity_model_gateway,
    build_xml_tool_prompt as _build_xml_tool_prompt_gateway,
    parse_xml_tool_calls as _parse_xml_tool_calls_gateway,
)

# The one shared refusal of the model's own input (tool_call_ledger.refused):
# every executor below that refuses an argument, an op, a name the model gave
# or a wrong state returns through it, so the error lane files the model's
# outcome as the model's (fault 'model') and keeps 'platform' for real faults.
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.triage import (
    _BOT_TOOL_SCHEMAS,
    _META_TOOL_NAMES,
    _BRIDGE_TOOL_SCHEMAS,
    _BRIDGE_TOOL_NAMES,
    _BRIDGE_TOOL_NAME_TO_OP,
    PUBLIC_TOOL_SCHEMAS,
)

log = logging.getLogger("cseries.mastercode.entity_runtime")


# Each tool's handler lives in runtime/tools_<category>.py, named by the tool's category in
# the tool registry; every name is re-exported here for the callers that import it from here.
from mastercode.runtime.tools_shared import (  # noqa: E402,F401
    _REPO_READ_PAGE_CHARS, _active_review_hold, _bytes_as_chunks, _ci_merge_gate,
    _host_route_write, _read_window, _review_hold_reason, _window_result,
)
from mastercode.runtime.tools_apps import (  # noqa: E402,F401
    _MANAGE_SITE_DOMAIN_OPS, _MANAGE_SITE_OPS, _PROBE_BODY_MAX, _PROBE_LOG_MAX,
    _PUBLISH_IN_BAND_MAX, _PUBLISH_PART_BYTES, _exec_app_probe, _exec_browser_agent,
    _exec_install_app, _exec_list_app_catalog, _exec_manage_site, _exec_manage_site_domain,
    _exec_publish_app, _exec_publish_site, _exec_purchase_app, _exec_refresh_app_store,
    _exec_ship_project_app, _exec_uninstall_app, _publish_bundle_body,
)
from mastercode.runtime.tools_brain import (  # noqa: E402,F401
    _exec_entity_tasks, _exec_explanation_level, _exec_personality_state, _exec_turn_type,
)
from mastercode.runtime.tools_build import (  # noqa: E402,F401
    BUILD_MODEL_WAIT_MAX_S, BUILD_MODEL_WAIT_S, PHASE_AUTO_REPAIRS, _ACCEPTANCE_DEFAULT_TIMEOUT_S,
    _ACCEPTANCE_MAX_CMDS, _ACCEPTANCE_MAX_TIMEOUT_S, _AUTOADVANCE_MAX_CHAIN, _CUT_MARK,
    _CUT_MARK_ROOM, _DELIVERY_HOLD_S, _FACTORY_BUILD_TASKS, _FAN_OUT_TASKS, _KilledBeforeStart,
    _OUTCOME_LOCKS, _PHASE_ARCHIVED, _PHASE_COLS, _PHASE_DONE, _PHASE_FAILED, _PHASE_GATE_USER,
    _PHASE_OPEN_SQL, _PHASE_READY, _PHASE_REVIEW, _PHASE_RUNNING, _PLATFORM_FAILURE_RE,
    _PROJECTS_ROOT, _REPORT_KEEP_CHARS, _RETRY_SEED_MARK, _STEP_DONE, _STEP_FAILED, _STEP_RUNNING,
    _SeedPending, _acceptance_tool_refusal, _append_build_activity, _autoadvance_pick_next,
    _autoadvance_start_phase, _build_usage_for_task, _build_wait_cap_s, _build_wait_s, _chain_stop,
    _ci_delivery_budget, _claim_phase, _compose_build_prompt, _compose_default_plan,
    _compose_phase_intent, _current_turn_type_id, _delivery_targets, _engine_counts, _exec_build,
    _exec_build_compare, _exec_build_kill, _exec_build_run, _explain_idle,
    _factory_build_task_finish, _factory_build_task_start, _failed_build_kind, _fan_out_task_start,
    _find_cycle, _first_line, _get_runtime_dispatch, _held_text, _hold_report, _image_tools,
    _is_platform_failure, _known_efforts, _launch_phase_build, _links_out_refusal,
    _load_research_context, _mark_cut, _normalize_acceptance, _normalize_gate, _outcome_lock,
    _persist_plan, _phase_awaits_approval, _phase_deps, _phase_engine, _phase_failed_builds,
    _phase_retry_pick, _phase_validation_for, _pool_build, _raise_phase_gate_tickets,
    _release_phase_claim, _resolve_plan_dependencies, _root_build_target, _row_get,
    _run_phase_build, _run_report_where, _running_phase_text, _schedule_phases, _set_phase_status,
    _set_phase_steps_status, _split_conflict_text, _summarize_fan_out, _ticket_phase_outcome,
    _turn_is_woken, _validate_built_project, _validate_plan, _watch_fan_out, continue_plan,
    phase_repair_refusal, start_approved_phase,
)
from mastercode.runtime.tools_projects import (  # noqa: E402,F401
    GITEA_READ_OPS_ALL, GITEA_WRITE_OPS, PIPELINE_PROPOSAL_KIND, POOL_HARNESS_OPEN_WEB,
    _CLONE_IDLE_S, _DESKTOP_WORD_RE, _FORGE_CRED_HELPER, _GITEA_BINARY_EXT,
    _GITEA_GREP_CONCURRENCY, _GITEA_GREP_FILES, _GITEA_GREP_FILE_BYTES, _GITEA_GREP_HITS,
    _GITEA_PR_PAGE, _GITEA_PR_PATCH_CHARS, _GITEA_READ_CAP, _GITEA_REPO_RE, _GITEA_TREE_CAP,
    _GITEA_TREE_MAX_PAGES, _GITEA_TREE_PAGE, _MATRIX_ROW_DROP, _REVIEW_FILES_MAX,
    _REVIEW_OUTCOME_CHARS, _REVIEW_PROMPT_CHARS, _ROUTE_ALIASES, _SEARCH_READ_NOTE,
    _SEARCH_SNIPPET_DEFAULT, _SEARCH_SNIPPET_MAX, _SEARCH_SNIPPET_MIN, _STARTED_FROM_KIND, _UNSET,
    _artifact_words, _attach_desktop_contract, _bench_routing_view, _codesearch_index_tasks,
    _conversation_label, _desktop_from_url, _exec_archive_project, _exec_artifact,
    _exec_bug_review, _exec_coding_bench, _exec_coding_protocol, _exec_create_project,
    _exec_credential_wiring, _exec_get_project, _exec_get_project_activity, _exec_gitea,
    _exec_harness_connections, _exec_harness_costs, _exec_harness_transcript,
    _exec_harness_usage_windows, _exec_import_repo, _exec_index_my_code, _exec_integrations_manage,
    _exec_list_projects, _exec_list_skills, _exec_pipeline, _exec_project_adopt_thread,
    _exec_project_phase, _exec_review_session, _exec_search_my_code, _exec_set_project_manifest,
    _exec_set_project_routing, _exec_support_collect, _exec_update_project, _forge_clone_auth,
    _forge_repo_foreign, _get_codesearch_db, _gitea_comment, _gitea_foreign, _gitea_http_failure,
    _gitea_login, _gitea_mark_foreign_authors, _gitea_mark_if_foreign, _gitea_pr_number,
    _gitea_pr_read, _gitea_raw_chunks, _gitea_user_login, _harness_matrix_row,
    _harness_matrix_view, _is_html_entry, _mark_harness_target, _note_missing_images,
    _own_repo_hits, _phase_deps_arg, _phase_engine_arg, _pipeline_code_servers,
    _pipeline_last_answered, _pipeline_open_proposal, _pipeline_stepup_enrolled,
    _project_of_thread, _prospect_report, _review_session_view, _review_variant_line,
    _run_with_progress_liveness, _search_locator, _search_locators, _split_pr_diff,
    _write_started_from, started_from_text,
)
from mastercode.runtime.tools_codespace import (  # noqa: E402,F401
    _CODE_GREP_OPERATOR_MAX, _CODE_GREP_PAGE_CHARS, _CODE_GREP_PAGE_MAX, _CODE_SOURCE_TOOLS,
    _ENTITY_CODESPACE_ROOT, _LOCAL_GREP_LINE_CHARS, _LOCAL_GREP_PAGE_MAX, _code_grep_page,
    _code_grep_request, _code_source_from_forge, _exec_local_grep, _grep_excerpt,
)
from mastercode.runtime.tools_comms import (  # noqa: E402,F401
    _exec_calendar_read, _exec_circles_manage, _exec_contacts_manage, _exec_host_message,
    _exec_inbox_read, _exec_phone_alias, _exec_reachout, _exec_reachout_channel,
    _exec_schedule_task, _exec_task_template, _exec_tickets, _planned_text, _support_elsewhere,
)
from mastercode.runtime.tools_community import (  # noqa: E402,F401
    _exec_community_board,
)
from mastercode.runtime.tools_context import (  # noqa: E402,F401
    _INT64_MAX, _NOT_WHOLE, _exec_dcn, _whole_arg, file_access_codec_refusal,
)
from mastercode.runtime.tools_environment import (  # noqa: E402,F401
    _CAPS_CARD_LIMIT, _CAPS_CARD_LIMIT_MAX, _CAPS_DOC_DIR, _CAPS_DOC_LIMIT, _CAPS_ID_LIST_LIMIT,
    _CAPS_MIN_SCORE, _CAPS_PAGE_EXCERPT, _CAPS_SECTION_LIMIT, _CAPS_STOPWORDS, _CAPS_WEAK_SCORE,
    _caps_as_list, _caps_doc_search, _caps_load_cards, _caps_normalize_card, _caps_score_card,
    _caps_search_view, _caps_stem, _caps_tokens, _exec_capabilities, _exec_check_page,
    _exec_describe_app, _exec_explore_environment, _exec_image_generate, _exec_open_app,
    _first_sentence, _load_image_bot,
)
from mastercode.runtime.tools_files import (  # noqa: E402,F401
    TURN_ATTACHMENTS, WORKSPACE_READ_PAGE_BYTES, _attachment_miss, _exec_workspace_file, _int_arg,
    _ws_refused,
)
from mastercode.runtime.tools_governance import (  # noqa: E402,F401
    _exec_decision_resolve, _exec_propose_capability,
)
from mastercode.runtime.tools_mcp import (  # noqa: E402,F401
    _MCP_OPS, _exec_mcp_connections,
)
from mastercode.runtime.tools_memory import (  # noqa: E402,F401
    _ALIAS_MAX, _CATALOG_DEFAULT_LIMIT, _CATALOG_MAX_LIMIT, _CURATE_OPS, _CURATE_TYPES,
    _HELD_FOR_REPLACE, _MEMORY_SCOPABLE_TYPES, _MEMORY_SCOPE_KINDS, _NotAbsorbed,
    _SCOPE_HISTORY_KEEP, _absorbs, _compact_catalog, _correction_scope, _curate_add,
    _curate_add_slot, _curate_replace, _curate_scope, _curate_slot_scope, _decided_about,
    _exec_chat_threads, _exec_context, _exec_context_backend, _exec_context_ingest,
    _exec_embed_jobs, _exec_embedding_route, _exec_inference_catalog, _exec_links,
    _exec_media_embedding_route, _exec_memory_alias, _exec_memory_curate, _exec_memory_key,
    _exec_memory_scope, _exec_model_route, _exec_preference, _exec_training_memory, _json,
    _learned_in, _memory_scope_list, _memory_scope_names, _memory_scope_ref, _replace_problem,
    _scope_label, _taught_places, _with_taught_places, apply_memory_scope,
)
from mastercode.runtime.tools_network import (  # noqa: E402,F401
    AGENT_OBSERVATION_CAP, ALERTS_STALE_AFTER_S, ALERTS_STATES, DETECT_COMPARED_STATES,
    MESH_HARNESS_OPEN_WEB, OPERATOR_ENTITY_ID, PAIRING_LIST_CAP, _CONNECTOR_PAIRING_NEXT,
    _DETECT_CLASS_COVERAGE_KEY, _DETECT_CLASS_WORDS, _EXEC_FENCE_MARK, _NETWORK_LISTS, _RFC3339_RE,
    _agent_line, _agent_summary, _alerts_counts, _alerts_read_lag_s, _alerts_reading,
    _bastion_alerts_state, _bastion_detect_state, _bastion_embed_readiness,
    _bastion_enforcing_summary, _bastion_list_view, _bastion_pairing_summary, _bastion_summary,
    _bastion_watch_state, _cap_agents, _cap_pairings, _detect_compared, _detect_counts,
    _detect_reading, _detect_skip_causes, _exec_approve_nac_draft, _exec_get_network,
    _exec_lab_run, _exec_machine_exec, _exec_machine_map, _exec_manage_bastion,
    _exec_manage_pairing, _exec_mesh_harness_run, _exec_mgmt_run, _exec_nac_status,
    _exec_propose_egress_rule, _exec_propose_mgmt_grant, _exec_propose_nac_policy,
    _exec_quarantine_bastion, _exec_revoke_nac_rule, _exec_suspend_bastion, _fence_command_output,
    _get_network_view, _nac_bastion_row, _nac_status_view, _network_bastion_row,
    _operator_entity_id, _peer_id_for, _propose_next_hint, _refused_or_ok, _rfc3339, lab_rate_text,
)
from mastercode.runtime.tools_research import (  # noqa: E402,F401
    RESEARCH_FETCH_CONCURRENCY, _GH_READ_CAP, _exec_github, _exec_github_repo, _exec_github_search,
    _exec_read_url, _exec_research, _exec_task_record, _exec_web_search, _fenced_passage, _gh_get,
    _github_repo_token, _keep_open, _keep_search, _research_fetch_whole,
    _research_find_coverage, _safe_passage, _safe_reason, _safe_source, _safe_title, _task_project,
)
from mastercode.runtime.tools_review import (  # noqa: E402,F401
    _exec_ladder_decision, _exec_ladder_run, _exec_ladder_status, _exec_review_adopt,
    _exec_review_adopt_undo, _exec_review_compare, _exec_review_propose_commit,
)
from mastercode.runtime.tools_settings import (  # noqa: E402,F401
    _exec_limits, _exec_model_bench, _exec_model_routing, _pick_image_recommendation,
    _preferred_image_model,
)
from mastercode.runtime.tools_system import (  # noqa: E402,F401
    _LOG_ERROR_RE, _exec_diagnose, _exec_system_app_status, _exec_system_refresh_sdk_auth,
    _exec_system_restart_app, _exec_system_start_app, _exec_system_stop_app,
)
from mastercode.runtime.tools_vault import (  # noqa: E402,F401
    _COMPUTE_BATCH_ROLES, _COMPUTE_KINDS, _KEYS_MANAGE_USE_ACTIONS, _compute_connections,
    _exec_autofill, _exec_connect_compute, _exec_keys_manage,
)

# A caller that rebinds one of these names on tools.py (a test standing in for a
# helper) rebinds it in the module that now holds it too, so the code there reads
# the value it read when every tool lived in this file.
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

_SPLIT_MODULES = tuple(_sys.modules["mastercode.runtime.tools_" + _m] for _m in (
    "shared",
    "apps",
    "brain",
    "build",
    "projects",
    "codespace",
    "comms",
    "community",
    "context",
    "environment",
    "files",
    "governance",
    "mcp",
    "memory",
    "network",
    "research",
    "review",
    "settings",
    "system",
    "vault",
))
_NOT_HELD = object()
# Names a delete on tools.py took from a split module, so that putting one back
# puts it back there too: {module: {name, ...}}.
_TAKEN: dict = {}
# Names a delete on tools.py took from tools.py itself: tools.py's to put back,
# even while a split module holds a stand-in of its own.
_DELETED_HERE: set = set()


def _own_attribute(name):
    # A module's own attributes (__name__, __file__, __spec__, ...) stay its
    # own: importlib.reload sets them on tools.py, and passing them on would
    # rename every split module to tools.py.
    return name.startswith("__") and name.endswith("__")


class _ToolsModule(_types.ModuleType):
    def __setattr__(self, name, value):
        held = self.__dict__.get(name, _NOT_HELD)
        if held is _NOT_HELD and not _own_attribute(name) and name not in _DELETED_HERE:
            # A name a split module keeps for itself (rebound there with
            # `global`, so not re-exported): set here it would reach nothing.
            for _mod in _SPLIT_MODULES:
                if name in _mod.__dict__:
                    raise AttributeError(f"{name} is {_mod.__name__}'s own; set it there")
        super().__setattr__(name, value)
        if _own_attribute(name):
            return
        _DELETED_HERE.discard(name)
        # Passed on only where the module holds the very object tools.py held, or
        # where a delete here took it: a name a test creates on tools.py is
        # tools.py's alone, and must not stay behind elsewhere once it is removed.
        for _mod in _SPLIT_MODULES:
            taken = _TAKEN.get(_mod, set())
            if name in taken or (held is not _NOT_HELD and _mod.__dict__.get(name, _NOT_HELD) is held):
                setattr(_mod, name, value)
                taken.discard(name)

    def __delattr__(self, name):
        held = self.__dict__.get(name, _NOT_HELD)
        super().__delattr__(name)
        if _own_attribute(name):
            return
        _DELETED_HERE.add(name)
        for _mod in _SPLIT_MODULES:
            if held is not _NOT_HELD and _mod.__dict__.get(name, _NOT_HELD) is held:
                delattr(_mod, name)
                _TAKEN.setdefault(_mod, set()).add(name)


if __name__ in _sys.modules:
    _sys.modules[__name__].__class__ = _ToolsModule

#: The fields that name the PERSON on a message (bridge adapter
#: _INBOX_SENDER_FIELDS; memory_handlers.handle_inbox_append). A bridge tool
#: call never carries them: see the bridge-op branch of _execute_meta_tool.
_TOOL_NEVER_NAMES_A_PERSON = ("from_user_email", "to_user_email")

# Aliases for code that still references these directly
_build_xml_tool_prompt = _build_xml_tool_prompt_gateway
_parse_xml_tool_calls = _parse_xml_tool_calls_gateway


def _json_safe(obj):
    """Recursively coerce a tool result into JSON-serializable form.

    Tool handlers return DB rows verbatim (e.g. get_network embeds NAC
    policy/egress/grant rows), which carry datetime / Decimal / UUID /
    bytes — none JSON-serializable by default. Every downstream consumer
    (tool_call_log, the live SSE tool-result frame, the model's
    tool message) json.dumps the result, and one unserializable value
    there surfaced to the user as "Object of type datetime is not JSON
    serializable". Sanitize once at the dispatch boundary so no tool can
    break serialization regardless of what its handler returns.
    """
    import datetime as _dt
    import decimal as _dec
    import uuid as _uuid
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dec.Decimal):
        return float(obj)
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _resolve_surface(interaction_context: str | None) -> str | None:
    """Map a session's interaction_context to the active app id used for
    tool-surface filtering. Mirrors prompt_builder._classify_surface but
    returns the app id (or None) instead of a UI label."""
    ic = (interaction_context or "").strip()
    if ic == "chat":
        return "circles"
    if ic.startswith("project:"):
        return "workbench"
    return ic or None


def _surface_allows(schema: dict, surface: str | None) -> bool:
    """Read `_meta.surfaces` and decide whether this tool is exposed in
    the current surface. Rules:
      - missing or None  → exposed everywhere (default)
      - empty list `[]`  → exposed nowhere (effectively hidden)
      - non-empty list   → exposed only when surface is in the list

    Surface=None (no interaction_context — typically background tasks,
    cron, voice, etc.) bypasses the filter so non-chat code paths
    aren't accidentally muted."""
    meta = schema.get("_meta") or {}
    if "surfaces" not in meta:
        return True
    allowed = meta.get("surfaces")
    if surface is None:
        return True
    if not allowed:  # explicitly empty list
        return False
    return surface in allowed


async def _build_tool_schemas(
    pool,
    entity_id: int,
    entity_config=None,
    *,
    interaction_context: str | None = None,
) -> list[dict]:
    """Build the list of tool schemas for an entity based on CEIGAS authorization.

    Parameterizes tool descriptions with entity-specific config to avoid
    hardcoded references to specific handlers or entities.

    Surface filtering: each tool schema may carry
    `_meta.surfaces`, a list of app ids the tool is exposed in. The
    builder reads `interaction_context` and drops tools whose surfaces
    list excludes the current surface. Missing field = available
    everywhere; empty list = hidden everywhere. Background callers
    (no interaction_context) get every tool to avoid muting cron and
    similar non-chat paths.

    All entities get public tools. The operator Host also gets active private tools (meta-tools).
    """
    surface = _resolve_surface(interaction_context)
    import copy

    # Resolve entity config for description parameterization. The
    # container ships without entity_config_resolver (it's a platform-side
    # module). When it isn't available — or fails for any other reason —
    # fall back to a stub config so schemas still build with literal
    # placeholders left in place. This lets always-on tools like
    # `escalate` reach every entity even on stripped runtimes.
    class _StubEntityConfig:
        handler_name = "your handler"
        handler_possessive = "your handler's"
        approval_label = "approval"
        entity_name = "you"

    if entity_config is None:
        try:
            from entity_config_resolver import EntityConfigResolver
            resolver = EntityConfigResolver(pool)
            entity_config = await resolver.resolve(entity_id)
        except Exception:
            entity_config = _StubEntityConfig()

    # Template vars for description parameterization
    template_vars = {
        "handler_name": getattr(entity_config, "handler_name", "your handler"),
        "handler_possessive": getattr(entity_config, "handler_possessive", "your handler's"),
        "approval_label": getattr(entity_config, "approval_label", "approval"),
        "entity_name": getattr(entity_config, "entity_name", "you"),
    }

    def _parameterize(schema):
        """Deep-copy schema, resolve {placeholders} in descriptions,
        and strip our internal `_meta` block (category/priority/purpose)
        before the schema crosses the wire to the LLM. The LLM only
        sees the standard OpenAI tool-schema shape; _meta is read by
        the prompt builder for the inventory section."""
        s = copy.deepcopy(schema)
        s.pop("_meta", None)
        desc = s["function"].get("description", "")
        try:
            s["function"]["description"] = desc.format(**template_vars)
        except (KeyError, ValueError):
            pass  # leave as-is if template vars missing
        return s

    # All entities get public schemas
    schema_sources = [PUBLIC_TOOL_SCHEMAS]

    # The operator Host also gets active private schemas (currently just meta-tools)
    if entity_id == OPERATOR_ENTITY_ID:
        schema_sources.insert(0, _BOT_TOOL_SCHEMAS)

    # Bridge-op tools (operator-retirement primitives — code_read, code_grep,
    # code_tree, k8s_rollout_status) are exposed to any entity with
    # infra.codebase admin. The bridge ops themselves enforce per-op
    # gates (registry/repo/service allowlists), so this catalog-gate is
    # the "do you see them at all" layer. The query checks live state.
    bridge_authorized = False
    try:
        bridge_authorized = bool(await pool.fetchval(
            """SELECT 1 FROM entity_domain_access
                WHERE entity_id = $1
                  AND domain_id = 'infra.codebase'
                  AND access_level = 'admin'
                LIMIT 1""",
            entity_id,
        ))
    except Exception:
        bridge_authorized = False
    if bridge_authorized:
        schema_sources.insert(0, _BRIDGE_TOOL_SCHEMAS)

    tools = []
    seen_names = set()

    # A bot-filename-keyed loop ran here, admitting only keys present in the
    # CEIGAS-authorized filename set from BotExecutor.list_bots(). It admitted
    # NOTHING: _BOT_TOOL_SCHEMAS' non-_meta_ keys are an empty set, and the
    # public/bridge dicts are keyed by tool NAME, so the filename test could
    # never match them (they are added by their own catalog gates below).
    # Removed with the bot lane — verified behaviour-preserving by
    # diffing this function's output for the operator Host before and after.

    # PUBLIC tools (`local_grep`, `web_search`, `github_search`, `sdk_build`)
    # are catalog-public — every entity sees them. The per-tool capability
    # bit check in `_execute_meta_tool` still gates execution against the
    # entity's effective bits at call time (e.g. `web_search` needs the
    # EXTERNAL_API bit). Surfacing them here is a "do you see them at all"
    # decision, not an authorization decision.
    for filename, schema in PUBLIC_TOOL_SCHEMAS.items():
        if filename.startswith("_meta_"):
            continue
        func_name = schema["function"]["name"]
        if func_name in seen_names:
            continue
        if not _surface_allows(schema, surface):
            continue
        tools.append(_parameterize(schema))
        seen_names.add(func_name)

    # BRIDGE tools (operator-retirement primitives — code_read, code_grep,
    # code_tree, k8s_rollout_status) are exposed only when the entity has
    # `infra.codebase=admin`. The bridge
    # ops themselves enforce per-op gates (registry/repo/service
    # allowlists) on the platform side, and `_execute_meta_tool` enforces
    # the capability bits on the entity side. This catalog gate just
    # decides whether the tool name appears in the model's `tools=` list.
    if bridge_authorized:
        for filename, schema in _BRIDGE_TOOL_SCHEMAS.items():
            if filename.startswith("_meta_"):
                continue
            func_name = schema["function"]["name"]
            if func_name in seen_names:
                continue
            if not _surface_allows(schema, surface):
                continue
            tools.append(_parameterize(schema))
            seen_names.add(func_name)

    # Operator-ONLY platform tools (the lab, the bug cache, the support queue) —
    # strictly gated to the operator Host so they never leak into the userbase's
    # tool surface. Bastion execution (mgmt_run, mesh_harness_run) is in the
    # public set, not this one: people manage their own servers.
    if entity_id == OPERATOR_ENTITY_ID:
        try:
            from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS as _OPERATOR_PLAT
        except Exception:
            _OPERATOR_PLAT = {}
        for filename, schema in _OPERATOR_PLAT.items():
            func_name = schema["function"]["name"]
            if func_name in seen_names:
                continue
            if not _surface_allows(schema, surface):
                continue
            tools.append(_parameterize(schema))
            seen_names.add(func_name)

    # Always include meta-tools (state_store, state_load) for the operator Host
    if entity_id == OPERATOR_ENTITY_ID:
        for filename, schema in _BOT_TOOL_SCHEMAS.items():
            if filename.startswith("_meta_") and filename != "_meta_escalate":
                func_name = schema["function"]["name"]
                if func_name in seen_names:
                    continue
                if not _surface_allows(schema, surface):
                    continue
                tools.append(_parameterize(schema))
                seen_names.add(func_name)

    # escalate() is universally available. Every entity can file a ticket
    # to its handler — it's the "I need a decision" lane that makes
    # Entity-as-Endpoint coherent.
    if "_meta_escalate" in _BOT_TOOL_SCHEMAS:
        esc_name = _BOT_TOOL_SCHEMAS["_meta_escalate"]["function"]["name"]
        if esc_name not in seen_names:
            tools.append(_parameterize(_BOT_TOOL_SCHEMAS["_meta_escalate"]))
            seen_names.add(esc_name)

    # local_grep says the platform source is not in the codespace. A Host that
    # holds the platform-source tools is also told where it is: a Host
    # once grepped its codespace ten times for the shell's source with
    # code_grep unloaded, because nothing it was shown named the right door.
    # Keyed on the tools actually being in THIS list (after the surface
    # filter), not on the gate alone, so the pointer never names a tool the
    # list does not carry.
    # search_my_code is told the same: the platform repos it can read are in
    # its index, and code_* read a hit's range from the platform's own copy.
    if all(n in seen_names for n in _PLATFORM_SOURCE_TOOLS):
        for s in tools:
            suffix = _PLATFORM_POINTERS.get(s["function"]["name"])
            if suffix:
                s["function"]["description"] += suffix

    return tools


# The tools local_grep's platform pointer names; it is added only to a list
# that carries every one of them (here and drop_unheld_platform_pointer).
_PLATFORM_SOURCE_TOOLS = ("search_my_code", "code_grep", "code_read", "code_tree")

# Search first, then grep, then read: semantic search finds the place, grep
# pins every occurrence, a range read gives the context. A Host made about 118
# code_grep calls and 12 search_my_code calls in two weeks while this pointer
# named only the grep.
_LOCAL_GREP_PLATFORM_POINTER = (
    " For the platform source: find where something is with "
    "search_my_code first (meaning and keywords; each hit names its repo, file and "
    "lines), then pin every exact occurrence with code_grep and read the range with "
    "code_read; list a directory with code_tree. Each runs through "
    "read(tool='code_grep', args={...}). code_* take a repo as the forge names "
    "it (owner/name)."
)
_SEARCH_PLATFORM_POINTER = (
    " The platform repos you can read on the forge "
    "are in this index too, refreshed about every 10 minutes: search the platform here "
    "first, then pin exact lines with code_grep and read the range with code_read.")
_PLATFORM_POINTERS = {"local_grep": _LOCAL_GREP_PLATFORM_POINTER,
                      "search_my_code": _SEARCH_PLATFORM_POINTER}


def drop_unheld_platform_pointer(schemas: list) -> list:
    """Take the platform pointers (local_grep's and search_my_code's) back out
    of a tool list narrowed below _build_tool_schemas' output — a dispatched
    agent's tool_scope, a typed turn's groups — when that list no longer
    carries every tool the pointers name. Returns a new list; the
    narrowed-out case gets copies with the base descriptions, so the caller's
    schemas are never mutated."""
    held = {(s.get("function") or {}).get("name")
            for s in schemas if isinstance(s, dict)}
    if all(n in held for n in _PLATFORM_SOURCE_TOOLS):
        return list(schemas)
    out = []
    for s in schemas:
        fn = s.get("function") if isinstance(s, dict) else None
        desc = (fn or {}).get("description")
        suffix = _PLATFORM_POINTERS.get((fn or {}).get("name"))
        if suffix and isinstance(desc, str) and desc.endswith(suffix):
            s = {**s, "function": {**fn, "description": desc[:-len(suffix)]}}
        out.append(s)
    return out


_GRANT_HINT_WARNED = False


def _grant_requirement(tool_name: str) -> str | None:
    """The capability hint a host needs to write `grant` for this tool.

    Two numbers exist and they answer different questions.

    `min_required_bits_for` is the CHEAPEST op — what `unreachable_tools`
    (agent_grant.py:100) judges a dispatch against, because dispatch-time
    reachability asks "can this agent use this tool AT ALL", and refusing a
    legitimately read-only decomposition is the thing READ_SAFE_OPS exists to
    prevent. `required_bits_for` is the UNION — enough for every op.

    Printing the minimum alone is a trap, and a worse one than the bug it was
    meant to fix. Measured across META_TOOL_CAPS: 22 of 117 entries have
    min != union, and they are almost exactly the tools whose name is a verb —
    contacts_manage, schedule_task, workspace_file, artifact, keys_manage. Those
    are precisely what a host scopes when it wants an agent to DO something.

    A host that scopes contacts_manage, reads `read_memory`, and grants it gets
    a dispatch that PASSES the wall (the wall uses min too) and then denies
    mid-run on op='create' — budget spent, task row created, and the denial
    carries no corrected grant. The dispatch-time refusal this whole change
    exists to prevent is self-healing in one turn: scoped_agent.py:89 appends
    "Retry with grant: [...]" and says no task was created. So printing bare min
    trades a cheap recoverable failure for an expensive unrecoverable one, while
    carrying platform authority that discourages second-guessing.

    So: print the minimum, and when the union differs, print that too. The
    minimum keeps the read-only decomposition discoverable; the union is what a
    host whose task needs more than the cheap op should paste. Both are exact
    lists — no arithmetic asked of the reader.

    "OTHER ops", not "write ops". The extra bits are not always writes, and
    saying so was false for 5 of the 22 split tools — measured: dcn's extra bit
    is external_api alone with no write involved, and artifact / editor /
    keys_manage / connect_compute all add execute_tool. A host reading "write
    ops need ..." for a READ task through dcn would grant the cheap half, pass
    the wall (which uses min), and be denied mid-run — the exact failure this
    hint exists to prevent, caused by the hint. "other ops" is true by
    construction: an op in READ_SAFE_OPS resolves to min, and any op outside it
    falls back to the union (agent_grant.required_bits_for).

    Never raises: prompt_schema.py wraps the whole builder and would drop the
    ENTIRE "Your Tools" section for the entity, not just one hint. Per-tool
    swallowing is strictly safer — but it logs, because the silent version
    means every hint can vanish fleet-wide with no signal.
    """
    try:
        from mastercode.runtime.agent_grant import (
            describe, min_required_bits_for, required_bits_for)
        low = min_required_bits_for(tool_name)
        if not low:
            return None
        full = required_bits_for(tool_name)
        if full and full != low:
            return f"{describe(low)} — other ops need {describe(full)}"
        return describe(low) or None
    except Exception as e:                                          # noqa: BLE001
        # WARNING, not debug: server/main.py sets the root logger from
        # LOG_LEVEL defaulting to INFO and entrypoint runs uvicorn at
        # --log-level info, so a debug line is invisible in production — which
        # is precisely the silence this except was supposed to stop. Guarded to
        # once per process because it can otherwise fire per tool per prompt
        # rebuild (~117 lines).
        global _GRANT_HINT_WARNED
        if not _GRANT_HINT_WARNED:
            _GRANT_HINT_WARNED = True
            log.warning("grant hints unavailable (first: %s: %s) — the tool "
                        "inventory will render without them", tool_name, e)
        return None


async def _collect_authorized_tool_meta(pool, entity_id: int, *, interaction_context: str | None = None) -> list[dict]:
    """Return [{name, category, priority, purpose, requires}, ...] for every tool
    this entity is authorized to see IN THE GIVEN SURFACE. Same gating
    logic as `_build_tool_schemas` — public schemas always, bridge
    schemas if infra.codebase=admin, _BOT_TOOL_SCHEMAS items for the operator Host —
    AND the same `_meta.surfaces` filter, so the prompt's "Your Tools"
    inventory exactly mirrors the `tools=` array the LLM can actually
    call (no "told about a tool I can't call here", no hidden-plumbing
    tools like state_* leaking into the host's chat view).

    Read by the prompt builder's tool-inventory section. The LLM never
    sees this — `tools=` continues to come from `_build_tool_schemas`
    with `_meta` stripped per-schema."""
    surface = _resolve_surface(interaction_context)

    try:
        bridge_authorized = bool(await pool.fetchval(
            """SELECT 1 FROM entity_domain_access
                WHERE entity_id = $1
                  AND domain_id = 'infra.codebase'
                  AND access_level = 'admin'
                LIMIT 1""",
            entity_id,
        ))
    except Exception:
        bridge_authorized = False

    out: list[dict] = []
    seen: set[str] = set()

    def _absorb(schema: dict) -> None:
        fn = (schema.get("function") or {}).get("name")
        if not fn or fn in seen:
            return
        if not _surface_allows(schema, surface):
            return  # mirror _build_tool_schemas: don't list what the LLM can't call in this surface
        m = schema.get("_meta") or {}
        category = m.get("category") or "other"
        priority = int(m.get("priority") or 99)
        purpose = m.get("purpose")
        if not purpose:
            desc = (schema.get("function") or {}).get("description") or ""
            purpose = desc.split(".")[0].strip()
        out.append({
            "name": fn,
            "category": category,
            "priority": priority,
            "purpose": purpose,
            # What a DISPATCHED agent must be granted to call this tool.
            #
            # Derived from the same function the dispatch wall enforces with
            # (agent_grant.min_required_bits_for, via unreachable_tools), so
            # what the host reads here cannot drift from what it is judged
            # against. A hand-maintained table drifts on the first new tool.
            #
            # Why this exists: writing `grant` requires knowing each tool's
            # capability, and NOTHING the host read at that moment carried it.
            # The prompt inventory rendered purpose and category;
            # CAPABILITIES.md uses a different taxonomy ("gate: core.entity")
            # and names the seven grant capabilities zero times; the
            # dispatch-agent skill lists a partial sample and says "call
            # capabilities for the full catalog", which does not have it
            # either. So the host guessed, and dispatches were refused at the
            # door over a fact the platform already held — repeatedly, e.g.
            # publish_site/manage_site scoped with `execute_tool` when they
            # need `write_context`.
            "requires": _grant_requirement(fn),
            # The skill that carries this tool's doctrine (registry `_meta.skill`,
            # phase 2 of the host prompt map). The per-turn "Tools loaded this
            # turn" block names it per group, so a group's rules have ONE copy
            # — the skill — and a pointer, not a restatement in the surface
            # prompt (a surface block that repeats most of build-workflow is
            # a second copy).
            "skill": m.get("skill") or None,
        })
        seen.add(fn)

    # Walk in the same order _build_tool_schemas does.
    sources: list[dict] = [PUBLIC_TOOL_SCHEMAS]
    if entity_id == OPERATOR_ENTITY_ID:
        sources.insert(0, _BOT_TOOL_SCHEMAS)
    if bridge_authorized:
        sources.insert(0, _BRIDGE_TOOL_SCHEMAS)
    for src in sources:
        for filename, schema in src.items():
            if filename.startswith("_meta_"):
                continue
            _absorb(schema)
    for filename, schema in PUBLIC_TOOL_SCHEMAS.items():
        if filename.startswith("_meta_"):
            continue
        _absorb(schema)
    if bridge_authorized:
        for filename, schema in _BRIDGE_TOOL_SCHEMAS.items():
            if filename.startswith("_meta_"):
                continue
            _absorb(schema)
    # Operator-ONLY platform tools — mirrors _build_tool_schemas.
    if entity_id == OPERATOR_ENTITY_ID:
        try:
            from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS as _OPERATOR_PLAT
        except Exception:
            _OPERATOR_PLAT = {}
        for filename, schema in _OPERATOR_PLAT.items():
            _absorb(schema)
    if entity_id == OPERATOR_ENTITY_ID:
        for filename, schema in _BOT_TOOL_SCHEMAS.items():
            if filename.startswith("_meta_") and filename != "_meta_escalate":
                _absorb(schema)
    if "_meta_escalate" in _BOT_TOOL_SCHEMAS:
        _absorb(_BOT_TOOL_SCHEMAS["_meta_escalate"])

    return out


# ── Structural build-research gate ────────────
#
# Tool names in this set cannot execute without a `research_id` in
# their args. If the entity calls a gated tool without one, the
# executor auto-invokes skills/deep_research.run() first, injects the
# returned research_id, and re-runs the original tool. The entity
# cannot bypass this — the gate is in the executor, not the prompt.
#
# Seed names are the build-flavored verbs that exist today (or will
# exist soon). The list is extended at runtime via introspection over
# PUBLIC_TOOL_SCHEMAS + _BOT_TOOL_SCHEMAS — any tool whose name starts
# with a known build prefix gets gated automatically as new ones are
# added.
def _is_build_gated(name: str, args: dict | None = None) -> bool:
    """Return True if the tool requires a research_id before running.

    Exactly the one real code-emitting tool, never a set + prefix matching.

    The ONLY build door is `build`, and research grounds the
    PLAN — so gate build(action='plan') (research fires, informs the
    composed phases), NOT build(action='build') (phases already exist;
    research already ran at plan time — re-firing it per phase is waste).
    The old sdk_build/sdk_continue names retired with the sidecar path.
    """
    if name == "build":
        return (args or {}).get("action") == "plan"
    return False


def _derive_research_intent(tool_name: str, args: dict | None) -> str:
    """Compose an intent string for deep_research from a build-tool
    invocation. Caller-supplied 'intent' wins; falls back to the
    description-shaped args (task, description, prompt, name).
    """
    a = args or {}
    for key in ("intent", "task", "description", "prompt", "summary"):
        v = a.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:500]
    # Fallback: tool name + name/file hint
    hint = a.get("name") or a.get("file") or a.get("path") or ""
    if hint:
        return f"{tool_name}: {hint}"[:500]
    return tool_name


# ── Invariant-checked meta-tool capability gate ──
#
# Every meta-tool dispatch validates the caller's effective capability
# bits against the tool's declared requirement via the existing
# `ceigas_invariants.validate_capability_bits` invariant — the same gate
# bot_executor uses for filename-prefixed bots. Kill-switch + trust-
# floor checks fire here too. Every decision logs to
# entity_behavioral_log (event_type='bot_execution', existing enum).
#
# Default-deny for any meta-tool not in META_TOOL_CAPS. Platform-domain
# scopes are absent from ENTITY_SCOPE_TO_BITS by design (the
# "interwoven firewall" — platform tools never appear in the entity's
# bit vocabulary).

async def _authorize_meta_tool(
    name: str,
    args: dict,
    entity_id: int,
    pool,
    session_id: str | None,
    app_permissions: list[str] | None = None,
    grant_bits: int | None = None,
    turn_type: dict | None = None,
    interaction_context: str | None = None,
) -> tuple[bool, dict | None]:
    """Capability gate for every meta-tool dispatch.

    `turn_type` is
    the profile the calling Host turn runs under — None for an untyped turn,
    which is every turn before turn types existed and stays byte-identical.
    A typed turn is walled by runtime/turn_types.check_call (tool group, key
    name, server name) before any other check, and its capability bits are
    the type's grant, intersected with an agent's grant when both apply.

    `app_permissions` (chat-initiated calls carrying an app_token) does NOT
    narrow the entity's effective bits to entity_bits ∩ app_bits — see the
    long note below; app_permissions is audit-only.

    `grant_bits` is a different axis and IS enforced: the
    capability grant a host minted for ONE dispatched agent. None means
    the caller is acting AS the entity (the host itself, or an internal
    flow) and keeps the entity's bits. An agent passes its grant, and
    effective bits become entity_bits ∩ grant_bits — never wider than
    its host, never wider than its task. See runtime/agent_grant.py.

    Returns:
        (True, None) when authorized — caller proceeds to dispatch.
        (False, denial_envelope) when denied — caller returns the
        envelope directly without invoking the tool branch.
    """
    from mastercode.runtime.meta_tool_caps import (
        META_TOOL_CAPS,
        _resolve_entity_scopes,
        scopes_to_bits,
    )
    from mastercode.ceigas_invariants import validate_capability_bits
    # Every denial this gate returns is a WALL the caller could not act
    # through, so each envelope carries CAP_DENIAL_KEY: invariant 2 with the bits
    # the op needed (a grant can fix it), every other wall with 0 (no grant
    # can). A dispatched agent's run scores 'blocked' on either, never 'pass'
    # for a run that did nothing.
    from mastercode.runtime.agent_grant import CAP_DENIAL_KEY

    spec = META_TOOL_CAPS.get(name)
    if spec is None:
        # A connected MCP server's tool, mcp:<server>/<tool>: one row for all
        # of them (external_api). Any other unregistered name stays denied.
        from mastercode.runtime.meta_tool_caps import mcp_tool_caps
        spec = mcp_tool_caps(name)
    if spec is None:
        # A function the Host wrote in its own database, world:<name>: one
        # row for all of them (meta_tool_caps.WORLD_TOOL_CAPS).
        from mastercode.runtime.meta_tool_caps import world_tool_caps
        spec = world_tool_caps(name)
    if spec is None:
        return False, {
            "status": "denied", CAP_DENIAL_KEY: 0,
            # Machine-readable for runtime/tool_call_ledger: a name NO registry
            # knows is a call the model invented — the admission bench's
            # headline count. The sentence below is for the model.
            "reason": "unknown_name",
            "result": f"meta-tool {name!r} not registered in capability registry",
        }

    # ── Invariant 7: kill switch. Only core.governance survives. ──
    # Fail CLOSED: if the kill-switch state can't be read (import/raise),
    # we cannot prove the platform is NOT in kill-switch, so deny every
    # meta-tool except core.governance. A swallowed exception here would
    # let the kill switch be bypassed by breaking its own check.
    try:
        from ceigas_kill_switch import is_kill_switch_active, kill_switch_lets_through
        kill_active = is_kill_switch_active()
    except Exception as e:
        if spec["domain"] == "core.governance":
            log.warning(
                "kill-switch check failed (%s) — allowing core.governance only", e,
            )
            kill_active = False
        else:
            return False, {
                "status": "denied", CAP_DENIAL_KEY: 0, "reason": "policy_denied",
                "result": "kill-switch state unavailable — denied (fail closed)",
            }
    if kill_active and spec["domain"] != "core.governance":
        # The one exemption, named on the tool (meta_tool_caps operation
        # "stop") and decided by the kill-switch module's own table, so the
        # HTTP gate, the MCP lane and this gate cannot drift apart. The stop
        # still runs every check below.
        if not (spec.get("operation") == "stop" and kill_switch_lets_through(
                "core.entity", spec["domain"], "stop")):
            return False, {"status": "denied", CAP_DENIAL_KEY: 0, "reason": "policy_denied",
                           "result": "kill switch active"}

    # ── The TURN TYPE wall ──
    # Inside a type, everything not listed is denied: the tool's group, the
    # credential name a door is given, the server a bastion door is given.
    # Placed here, ahead of the scope read, so a refused call costs no DB
    # round trip and so `operate` and a dispatched agent hit the same wall
    # as a direct call. The one read before it is resolve_targets, and only
    # for a door whose arguments do not name what it reaches (a bastion by
    # token id, a browser agent by name). The refusal names the ACTIVE types that hold the
    # item — a lookup, not a decision: the Host leaves itself a note for a
    # turn of that type or asks the user to widen this one.
    from mastercode.runtime import turn_types as _tt
    # Who is calling decides the wording of a capability refusal below: an
    # agent reports back to its host; a typed Host turn is told about its
    # type; an untyped Host turn about its own domain wall. Captured before
    # the type folds into grant_bits, or an agent inside a typed turn would
    # read as the Host.
    _caller_is_agent = grant_bits is not None
    if turn_type is not None:
        _type_targets = await _tt.resolve_targets(pool, entity_id, name, args,
                                                  interaction_context=interaction_context)
        _type_denial = _tt.check_call(turn_type, name, args, targets=_type_targets)
        if _type_denial is not None:
            try:
                _kind = _type_denial.get("kind")
                _item = _type_denial.get("item")
                _type_denial["holders"] = await _tt.holders_of(
                    pool, entity_id,
                    group=_item if _kind == "group" else None,
                    key=_item if _kind == "key" else None,
                    server=_item if _kind == "server" else None,
                )
            except Exception as _h_err:  # noqa: BLE001
                log.debug("turn type holders lookup failed (non-fatal): %s", _h_err)
            return False, _type_denial
        # The type's bits are the turn's bits. An agent dispatched from a
        # typed turn carries its own grant too; it can never exceed the turn.
        _type_bits = _tt.grant_bits_for(turn_type)
        grant_bits = _type_bits if grant_bits is None else (int(grant_bits) & int(_type_bits))

    # ── Resolve caller's effective scopes + bit mask ──
    entity_scopes = await _resolve_entity_scopes(pool, entity_id)
    entity_bits = scopes_to_bits(entity_scopes)

    # ── App-permission audit (no longer narrows; see note) ────────
    #
    # Narrowing effective_bits to entity_bits ∩ app_bits on every
    # chat-initiated tool call conflates two different gates:
    #
    #   * App-token gating on KERNEL ROUTES (e.g. /api/brain/messages
    #     checking the calling app can talk to the brain at all). This
    #     is correct and lives in the FastAPI dependency
    #     `requires_permission`.
    #
    #   * App-token narrowing on ENTITY TOOL DISPATCH. The brain's
    #     meta-tools (local_grep, web_search, sdk_build, create_project,
    #     etc.) are exercised by the MODEL mid-response — the entity
    #     acting on her own scopes, not the app acting through her. An
    #     app like Circles whose manifest declares only social-graph
    #     scopes (read:circles, write:tickets) would mask off the
    #     entity's read:files / execute:code / write:projects bits and
    #     deny her own tools.
    #
    # A Host hit exactly this:
    # local_grep denied with
    #   "insufficient bits: have 00010110, need 00000001
    #    (app-narrowed: entity=01111111 ∩ app=00010110)"
    # because Circles doesn't declare `read:files`. The defense-in-depth
    # argument for narrowing didn't hold up — capability is about WHO
    # is acting (the entity), not WHERE the conversation happens.
    #
    # `app_permissions` is still captured in the audit row so we can
    # answer "this tool was called during an X-app chat" without
    # rebuilding state.
    # ── The agent GRANT narrowing ────────────────────
    # This is NOT a reversal of the app-narrowing removal above. That
    # change was about WHERE a conversation happens (an app surface)
    # and rightly concluded capability is about WHO is acting. A
    # dispatched agent is a different WHO: ephemeral, holding only what
    # its host minted for one task. Before this, it inherited the
    # entity's bits wholesale, so a host telling an agent "read-only,
    # do not write memory" was making a REQUEST — the agent held
    # WRITE_MEMORY throughout. Now it is a capability it never had.
    from mastercode.runtime.agent_grant import (
        effective_bits as _effective_bits, describe as _describe_bits,
    )
    effective_bits = _effective_bits(entity_bits, grant_bits)
    app_bits: int | None = (
        scopes_to_bits(app_permissions) if app_permissions is not None else None
    )

    audit_ctx = {
        "entity_bits": entity_bits,
        "app_bits": app_bits,
        "grant_bits": grant_bits,
        "effective_bits": effective_bits,
        # app narrowing stays off; grant narrowing is the live one.
        "narrowing_active": grant_bits is not None,
        "app_permissions": app_permissions,
    }
    if turn_type is not None:
        # Only a typed turn stamps the row; an untyped turn's audit row is
        # byte-identical to before turn types existed.
        audit_ctx["turn_type"] = turn_type.get("name")

    # ── Invariant 1: capability-bits invariant on the effective mask ──
    bits_ok, bits_violations = validate_capability_bits(effective_bits)
    if not bits_ok:
        return False, {
            "status": "denied", CAP_DENIAL_KEY: 0, "reason": "policy_denied",
            "result": f"capability bits violate invariant: {bits_violations}",
        }

    # ── Invariant 2: required bits must be subset of effective bits ──
    # Resolved PER OP where the tool declares a read-safe one:
    # `required_bits` is the union of everything a tool's ops can do, so
    # context_ingest(op='search') used to demand WRITE_MEMORY. An agent
    # decomposed to read-only can now make the read call and still be refused
    # the write one. Falls back to the union for any op not declared read-safe,
    # so this can only tighten.
    from mastercode.runtime.agent_grant import required_bits_for as _req_for
    required_bits = _req_for(name, args)
    if required_bits is None:
        required_bits = int(spec["required_bits"])
    required_bits = int(required_bits)
    if (effective_bits & required_bits) != required_bits:
        return False, {
            "status": "denied",
            # Marks this as the CAPABILITY wall, so a dispatched agent's run
            # scores 'blocked' and its report carries the retry grant;
            # every other denial in this gate is an ordinary tool result.
            CAP_DENIAL_KEY: required_bits,
            "reason": "capability_denied",
            "result": (
                (
                    # A typed HOST turn: the wall is the type the user
                    # approved, and only the user widens it.
                    f"'{name}' needs capability {_describe_bits(required_bits)}; this "
                    f"turn's turn type {turn_type.get('name')!r} holds "
                    f"{_describe_bits(effective_bits)}. You cannot widen the type "
                    f"yourself. Say in the thread what you could not do; the user can "
                    f"take it to a type that holds it, or widen this one."
                ) if (turn_type is not None and not _caller_is_agent)
                else (
                    f"'{name}' needs capability {_describe_bits(required_bits)}, "
                    f"which your host did not grant you for this task. You hold: "
                    f"{_describe_bits(effective_bits)}. Report back what you needed "
                    f"and why — elevating a grant is your host's decision, not yours."
                ) if _caller_is_agent else (
                    # The host's OWN wall, in the words it can repeat to the
                    # user. As bits ("have 00000101, need 00000010") a host
                    # denied a read could
                    # not tell the user whether a machine was offline or a
                    # permission was missing — bit strings name neither.
                    f"'{name}' needs capability {_describe_bits(required_bits)}; this host "
                    f"holds {_describe_bits(effective_bits)}. This is the host's own "
                    f"domain-access wall — a capability this host was not granted — not "
                    f"a machine, network or tool failure, and a retry cannot change it."
                )
            ),
        }

    # ── All checks passed ──
    return True, None


def _find_tool_schema(name: str) -> dict | None:
    """Look up a tool's schema by function name across all registries.
    Used by the dispatch-time surface gate so a hallucinated tool name
    (one the entity remembers from training/prior chats but that isn't
    in the current tools= list) gets denied with a useful message
    instead of executed."""
    for src in (PUBLIC_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS):
        for _filename, schema in src.items():
            if (schema.get("function") or {}).get("name") == name:
                return schema
    return None


def host_tool_allowed_on(name: str, interaction_context: str | None) -> bool:
    """Whether a host-held tool may be ADVERTISED to the host in this session —
    the same `_meta.surfaces` test the exec-time surface gate in
    `_execute_meta_tool` applies to host-lane calls, so what the host is shown
    is what it can run (`build` is workbench-only). A tool with no schema or no
    declared surfaces is allowed; interaction_context=None (background, cron)
    bypasses, as everywhere."""
    if interaction_context is None:
        return True
    schema = _find_tool_schema(name)
    if schema is None:
        return True
    return _surface_allows(schema, _resolve_surface(interaction_context))


def _suggest_alternate(name: str) -> str:
    """Pick a sensible alternative when a tool is denied by surface.
    Keeps the message actionable instead of just 'denied'."""
    swaps = {
        "github_search": "github (op='search' for repos by criteria, op='code' to find repos by their code)",
        "github_repo": "github (same ops — tree/read/import — under one name since 2026-08-23)",
        "create_project": "this surface already IS a project — don't create a new one",
    }
    return swaps.get(name, "use 'research' for source-cited research, or put the choice of "
                           "tool in the question box (escalate)")


def _codes_count_as_host(fn):
    """A second-factor code tried inside a Host tool call counts on the Host's own
    attempt record, never the owner's (server/network/nac_stepup.py, attempt
    limit), so the Host can hold up only itself with wrong codes."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            from server.network import nac_stepup
        except Exception:  # noqa: BLE001 — without it no code can be tried here at all
            return await fn(*args, **kwargs)
        with nac_stepup.host_attempts():
            return await fn(*args, **kwargs)
    return wrapper


@_codes_count_as_host
async def _execute_meta_tool(name: str, args: dict, entity_id: int, pool,
                            prompt_console=None, session_id: str | None = None,
                            app_permissions: list[str] | None = None,
                            interaction_context: str | None = None,
                            grant_bits: int | None = None,
                            origin_context: str | None = None,
                            # Circles threads: the circles_threads
                            # row the CALLING turn is in — EntitySession.thread_id.
                            # Only chat_threads reads it today (op='toc' and the
                            # recall link). None on every other lane and on
                            # every caller that has no turn behind it, which is
                            # the legacy app:circles scope and changes nothing.
                            thread_id: str | None = None,
                            # Turn types: the profile the calling
                            # turn runs under (runtime/turn_types.py), or None
                            # for an untyped turn — today's Host, unchanged.
                            # Walled inside the capability gate below.
                            turn_type: dict | None = None) -> dict:
    """Execute meta-tools (context, state_store, state_load, bridge-op
    tools, and every other host tool) inline.

    Wraps build-flavored tools with the deep_research gate.
    If _is_build_gated says so (today: build(action='plan')) AND args
    lacks a research_id, fire deep_research first, inject the resulting
    research_id into args, and continue.

    The invariant-checked capability gate fires BEFORE any
    dispatch. Validates the entity's effective bits include the
    tool's declared requirement via ceigas_invariants.validate_capability_bits;
    kill-switch + trust-floor checks fire here too. Default-deny
    for any meta-tool not in META_TOOL_CAPS. Every decision logs to
    entity_behavioral_log.

    `app_permissions` (chat-initiated calls carrying an app_token) is
    recorded for audit and never narrows the effective bits.

    Surface gate — the entity can call a tool name by
    HALLUCINATION (it remembers web_search from training/prior chats
    even after we hide it from tools=). Without a dispatcher check,
    the runtime gladly executes any name it knows. This block reads
    the tool's _meta.surfaces and denies execution when the current
    surface isn't allowed. Returns a status='denied' result with a
    suggested alternative so the model gets useful feedback instead
    of crashing into the capability gate further down."""

    # ── Surface gate (runs BEFORE the capability gate so a denied call
    # never even spins up the capability lookup).
    if interaction_context is not None:
        surface = _resolve_surface(interaction_context)
        schema = _find_tool_schema(name)
        if schema is not None and not _surface_allows(schema, surface):
            log.info(
                "surface-denied: tool=%s entity=%d surface=%s",
                name, entity_id, surface,
            )
            return {
                "status": "denied",
                # A real tool on the wrong surface — a model-side miss the
                # ledger counts apart from an invented name.
                "reason": "surface_denied",
                "result": (
                    f"Tool '{name}' is not available in this surface "
                    f"({surface or 'unknown'}). Use {_suggest_alternate(name)} instead."
                ),
            }

    # ── Invariant-checked capability gate ─────────────────
    # Runs BEFORE the build-research gate so a denied call never
    # spins up deep_research's SDK subagent.
    allowed, denial = await _authorize_meta_tool(
        name, args, entity_id, pool, session_id,
        app_permissions=app_permissions,
        # None = acting AS the entity (host / internal flow). A dispatched
        # agent passes its host-minted grant and is narrowed to it.
        grant_bits=grant_bits,
        turn_type=turn_type,
        # A typed turn's wall reads the project a door takes from the
        # conversation when the call names none (turn_types.resolve_targets).
        interaction_context=interaction_context,
    )
    if not allowed:
        return denial

    # ── Turn types step 5: scheduled work carries the turn's type ──
    # calendar_log (a scheduled_task row) and reachout_channel (a runbook) both
    # create work the offline runner fires later; from a typed turn it carries
    # that turn's type. Untyped: unchanged.
    from mastercode.runtime.turn_types import stamp_scheduled_work as _stamp_work
    args, _stamp_denial = _stamp_work(turn_type, name, args)
    if _stamp_denial is not None:
        return _stamp_denial

    # ── The host's own machine (runtime/diagnostics.py) ──
    # A call to any configuration tool drops the cached fault reading, so
    # the "Your machine needs attention" block re-measures on the next turn
    # instead of showing a fault the host just fixed for up to 120 s.
    try:
        from mastercode.runtime.diagnostics import (
            CONFIG_TOOLS as _diag_config_tools, invalidate as _diag_invalidate)
        if name in _diag_config_tools:
            _diag_invalidate(entity_id)
    except Exception:  # noqa: BLE001 - never a failed call
        pass

    # ── Structural build-research gate ──────────────────────────
    if _is_build_gated(name, args) and not (args or {}).get("research_id"):
        intent = _derive_research_intent(name, args)
        project_id = (args or {}).get("project_id")
        try:
            from mastercode.skills.deep_research import run as _deep_research_run
            research = await _deep_research_run(
                intent=intent,
                project_id=project_id,
                entity_id=entity_id,
                pool=pool,
            )
            rid = research.get("research_id")
            if rid:
                args = dict(args or {})
                args["research_id"] = rid
                log.info(
                    "build gate fired deep_research for tool=%s intent=%r → research_id=%s (reused=%s)",
                    name, intent[:80], rid, research.get("reused", False),
                )
        except Exception as e:
            log.warning("build-gate deep_research failed (proceeding without): %s", e)

    # Bridge-op tools dispatch through the entity's bridge client to the
    # operator. Each op enforces its own allowlist (registry/repo/
    # service/resource) at the operator side; the catalog-gate above
    # decided "should this entity see the tool at all." Return shape
    # mirrors meta-tool convention: {status, result|error}.
    if name in _BRIDGE_TOOL_NAMES:
        try:
            from server.main import get_bridge_client
        except Exception as exc:
            return _refused_if_caller(exc, f"bridge unavailable: {exc}")
        bc = get_bridge_client()
        if bc is None or not getattr(bc, "_connected", False):
            return {"status": "error", "result": "bridge not connected"}
        op = _BRIDGE_TOOL_NAME_TO_OP[name]
        # A tool call never names a PERSON. The bridge's inbox.send keeps a
        # from_user_email that is an operator of the sending Host, and the
        # receiving Host admits a message from a person through a block on
        # their Host (server/routes/contacts.py, the Block section). So the
        # model naming its own operator here would carry the Host's words past
        # that block. The person on a message comes only from the human's own
        # request (the router's X-Authentik-Email: the Circles composer and the
        # contacts routes); the Host's own sends carry none, as host_message's
        # (messages.send_message_service).
        if isinstance(args, dict) and any(k in args for k in _TOOL_NEVER_NAMES_A_PERSON):
            log.info("%s: dropped person fields from a tool call", name)
            args = {k: v for k, v in args.items() if k not in _TOOL_NEVER_NAMES_A_PERSON}
        # The platform-source readers take a repo by its forge name and
        # default to the platform's own (triage.CODE_DEFAULT_REPO) when one is
        # configured: a code_grep with only a pattern otherwise costs a round.
        # With none configured, a call that names no repo is refused.
        _code_args = None
        if name in _CODE_SOURCE_TOOLS:
            from mastercode.runtime.triage import CODE_DEFAULT_REPO
            args = dict(args or {})
            args["repo"] = str(args.get("repo") or "").strip().strip("/") or CODE_DEFAULT_REPO
            if not args["repo"]:
                return _refused(f"{name}: name a repo as owner/name; this Host has no "
                                "default platform repo configured")
            _code_args = dict(args)
        # code_tree is PAGED here (runtime/code_tree_page.py): the operator
        # walks the whole repo (≤6,000 files), so the page args stay on this
        # side and the host gets one subtree to one depth, capped — not a
        # 76 KB tree. `path` and `depth` also go to the
        # operator, which starts its walk there once it takes them; one that
        # does not yet ignores them, and the page is cut here either way.
        _page = None
        if name == "code_tree":
            _page = {"path": args.get("path") or "",
                     "depth": args.get("depth"),
                     "max_entries": args.get("max_entries")}
            args = {k: v for k, v in (("repo", args.get("repo")), ("path", args.get("path")),
                                      ("depth", args.get("depth"))) if v not in (None, "")}
        # code_grep is PAGED here too (_code_grep_page): the operator returns
        # every matched line whole, so one hit in a minified bundle is the
        # whole bundle line (measured: 173,784 chars for
        # max_results 40, 167,451 of them two lines of a built circles
        # bundle). The operator is asked for the matches up to the end of the
        # page; offset stays on this side.
        _grep_page = None
        if name == "code_grep":
            args, _grep_page = _code_grep_request(args)

        async def _from_forge(why: str) -> dict | None:
            """The same read from the forge, through the gitea tool and
            its own gate (_code_source_from_forge), or None when that is
            refused too."""
            async def _gitea(gargs: dict) -> dict:
                return await _execute_meta_tool(
                    "gitea", gargs, entity_id, pool, prompt_console=prompt_console,
                    session_id=session_id, app_permissions=app_permissions,
                    interaction_context=interaction_context, grant_bits=grant_bits,
                    origin_context=origin_context, thread_id=thread_id, turn_type=turn_type)
            return await _code_source_from_forge(name, _code_args, _gitea, why=why)

        try:
            envelope = await bc.request_full(op, args)
        except Exception as exc:
            return _refused_if_caller(exc, f"bridge call failed: {exc}")
        if envelope.get("ok"):
            data = envelope.get("data") or {}
            if _grep_page is not None and isinstance(data, dict):
                return {"status": "ok", "result": _code_grep_page(data, **_grep_page)}
            if _page is not None and isinstance(data, dict):
                from mastercode.runtime.code_tree_page import page_tree
                paged = page_tree(data, path=_page["path"], depth=_page["depth"] or 0,
                                  max_entries=_page["max_entries"] or 0)
                if paged.get("error"):
                    # A directory past the operator's 6,000-file walk: the
                    # forge lists any directory by its prefix.
                    if data.get("truncated") and _page["path"]:
                        served = await _from_forge("the platform controller's walk of this repo "
                                                   "stops at its file cap before this directory")
                        if served is not None:
                            return served
                    return _refused(paged["error"])
                return {"status": "ok", "result": paged}
            return {"status": "ok", "result": data}
        err = envelope.get("error") or f"{op} returned no data"
        if _code_args is not None and "unknown repo" in str(err):
            # A repo the controller holds no checkout of: read it where it
            # lives, on the forge, by the name the Host gave.
            served = await _from_forge("the platform controller holds no checkout of this repo")
            if served is not None:
                return served
        return {"status": "error", "result": err}

    if name == "context":
        # The turn's thread and session ride along: a `skills/<name>` write
        # records where it was written (runtime/host_skills.py).
        return await _exec_context(args, entity_id, pool, thread_id=thread_id,
                                   session_id=session_id)

    elif name == "links":
        return await _exec_links(args, entity_id, pool)

    elif name == "state_store":
        key = args.get("key", "")
        value = args.get("value", "")
        if not key:
            return _refused("key is required")
        file_path = f"swarm/{key}"
        await pool.execute("""
            INSERT INTO entity_context (entity_id, file_path, content, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (entity_id, file_path)
            DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
        """, entity_id, file_path, value)
        return {"status": "ok", "result": f"Stored at {file_path}", "key": key}

    elif name == "state_load":
        key = args.get("key", "")
        if not key:
            return _refused("key is required")
        file_path = f"swarm/{key}"
        row = await pool.fetchrow("""
            SELECT content, updated_at FROM entity_context
            WHERE entity_id = $1 AND file_path = $2
        """, entity_id, file_path)
        if not row:
            return {"status": "not_found", "result": f"No state stored at key '{key}'"}
        return {
            "status": "ok",
            "key": key,
            "value": row["content"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    elif name == "escalate":
        # File a ticket (or auto-resolve via a learned pattern) via the
        # in-container escalation helper. Returns the helper's dict
        # verbatim so the LLM sees ticket_id + (when auto-resolved) the
        # decision_template to apply.
        try:
            from server.applications.escalation import escalate as _escalate
        except Exception as e:
            return _refused_if_caller(e, f"escalation module unavailable: {e}")
        # The join to a Workbench phase: project_id + phase_id
        # ride the ticket's metadata; the escalation helper verifies the phase
        # is this host's own before anything is written.
        _md: dict = {"opened_via": "llm_tool"}
        # The turn that asked: deciding the ticket answers into
        # that turn while it runs (tickets.decide_ticket_service ->
        # session.answer_into_turn), whatever surface the person answers on —
        # the Workbench card, the Circles card or the Tickets pane. A
        # dispatched agent's session has no turn to answer into.
        if session_id and not str(session_id).startswith("agent:"):
            _md["asked_in_session"] = str(session_id)
        for _k in ("project_id", "phase_id"):
            if args.get(_k) is not None:
                try:
                    _md[_k] = int(args[_k])
                except (TypeError, ValueError):
                    return _refused(f"{_k} must be an integer")
        try:
            result = await _escalate(
                source=str(args.get("source") or "runtime"),
                sitrep=str(args.get("sitrep") or "").strip(),
                options=args.get("options") or [],
                action_class=args.get("action_class"),
                circle_id=args.get("circle_id"),
                requester=args.get("requester") or {},
                context=args.get("context") or {},
                priority=int(args.get("priority", 50)),
                source_ref=args.get("source_ref"),
                metadata=_md,
            )
        except ValueError as e:
            return _refused(str(e))
        except Exception as e:
            log.error("escalate meta-tool failed: %s", e, exc_info=True)
            return _refused_if_caller(e, f"escalate failed: {e}")
        return {"status": "ok", "result": result}

    # ── Chat-search meta-tools ─────────────
    # Three single-leg tools the entity calls mid-stream when it
    # needs to look something up. Heavyweight three-leg research
    # lives in skills/deep_research.py and is fired by the build
    # gate, not by these tools.

    elif name == "community_board":
        return await _exec_community_board(entity_id, args)

    elif name == "local_grep":
        return await _exec_local_grep(args)

    elif name == "research":
        return await _exec_research(args, entity_id=entity_id, pool=pool,
                                    project_id=_task_project(args, interaction_context),
                                    thread_key=thread_id or session_id)

    elif name == "task_record":
        # The task record: goal checks, coverage and quote-checked findings,
        # per project.
        return await _exec_task_record(args, entity_id, pool, interaction_context,
                                       thread_id=thread_id, session_id=session_id)

    elif name == "mcp_connections":
        # MCP connections: the Host's remote MCP servers and their tools. A server's own
        # tools never come here: they run through operate (direct_ops).
        return await _exec_mcp_connections(args, entity_id, pool)

    elif name == "data":
        # The Host's own database. world_db runs every op on the host_world pool; the brain pool
        # handed to this function is never used for it.
        from mastercode import world_db
        return await world_db.run_op(args)

    elif name == "read_url":
        # The missing half of the web lane. web_search/research
        # both return the PROVIDER's synthesized answer; neither can read a
        # page the user names. Tiered fetch (aiohttp → curl_cffi with a real
        # Chrome TLS fingerprint) + trafilatura extraction, in-pod, no sidecar.
        return await _exec_read_url(args, entity_id=entity_id, pool=pool,
                                    project_id=_task_project(args, interaction_context))

    elif name == "web_search":
        # web_search is back — but model-aware now. The
        # old name was killed because it was hardwired to one broken path and
        # the model called it constantly and always failed. This version
        # routes to a search-capable model automatically and ALWAYS returns
        # results, so it can't hammer failures on a lane that can't serve it.
        # `research` stays the heavy multi-source synthesis path.
        return await _exec_web_search(args, entity_id=entity_id, pool=pool,
                                      project_id=_task_project(args, interaction_context))

    # web_answer: retired; its stub branch was UNREACHABLE (no caps entry ->
    # default-deny fires first) so it was dead code and is deleted. A
    # hallucinated call gets the standard denial.

    # github_search: its dispatch branch was deleted with the name.
    # It routed through _exec_research with a site:github.com hint, and became
    # UNREACHABLE the moment its META_TOOL_CAPS entry went — default-deny fires
    # at _authorize_meta_tool before this ladder is consulted. That is precisely
    # the web_answer / F3 class described above, so it is deleted rather than
    # left as a branch that reads like a live route. A hallucinated call gets
    # the standard denial. `_exec_github_search` (the function) survives for
    # deep_research and the chat pre-fetch leg, which call it directly.

    elif name == "github":
        # ONE name for public GitHub — search / code / repo / tree /
        # read / import. Absorbs the old `github_repo` schema (whose executor it
        # still calls for tree/read/import) and supersedes `github_search`,
        # which never carried a schema and so was never callable at all.
        return await _exec_github(args)

    elif name == "bug_review":
        # The operator Host's door onto the bug cache. `grant_bits`
        # is the structural wall: None means the host acting as itself (may
        # list, read VALIDATED reports, mark a fix); an int means a dispatched
        # validator agent (may read a PENDING report and record ONE verdict,
        # never mark a fix). runtime/bug_review.py enforces both directions.
        return await _exec_bug_review(args, agent_grant=grant_bits, session_id=session_id)

    elif name == "support_collect":
        # The operator Host's standing collect of the platform's
        # support queue (runtime/support_collect.py) — one bridge read, one
        # operations ticket per received row, no model. Fired by the seeded
        # 5-minute task through run_direct_op, or by the host by hand.
        return await _exec_support_collect(args)

    elif name == "gitea":
        # The host reads its own forge — repos it owns and
        # repos the user granted it in the Gitea app; the consumer of
        # grant_host_access (server/routes/gitea.py). op='propose' is its
        # one write — fork + branch + PR to a repo the host does not own
        # (server/routes/gitea_propose.py); the reads stay reads. entity_id +
        # pool let the propose branch check the write:gitea scope itself —
        # the bit gate above cannot, bit 32 is granted at read level too.
        return await _exec_gitea(args, entity_id=entity_id, pool=pool)

    # ── build: the plan→approve→build door ─────────
    # The ONLY build tool, on every surface. action='plan' composes +
    # shows the phased plan; action='build' refuses without phases, then
    # runs the phase build detached and wakes the host on completion;
    # action='continue' is a follow-up seeded rerun. The old sdk_build/
    # sdk_continue names retired with the in-pod sidecar build path —
    # every route to the pool now passes the plan gate in _pool_build.
    elif name == "build":
        return await _exec_build(args, entity_id, pool, session_id, interaction_context,
                                 thread_id=thread_id)

    # ── create_project meta-tool ────────────────────
    # Creates the project row and leaves a marker in its thread pointing
    # back to the conversation it was started from (the conversation itself
    # stays whole where it was typed).
    elif name == "create_project":
        # The calling THREAD decides whether creation is allowed at all: a
        # project thread already is a project (see _exec_create_project).
        # origin_context is what `operate` passes (direct_ops.run_direct_op);
        # the direct lane passes the session's interaction_context. thread_id
        # is the Circles thread the calling turn is in — what the marker
        # links back to.
        return await _exec_create_project(
            args, entity_id, pool,
            origin_context=origin_context or interaction_context,
            thread_id=thread_id)
    elif name == "project_phase":
        return await _exec_project_phase(args, entity_id, pool)
    elif name == "archive_project":
        return await _exec_archive_project(args, entity_id, pool)
    elif name == "project_adopt_thread":
        return await _exec_project_adopt_thread(args, entity_id, pool)
    elif name == "coding_bench":
        return await _exec_coding_bench(args, entity_id, pool)
    # Orchestrator → Multi-Agent Coding: the protocol files.
    elif name == "coding_protocol":
        return await _exec_coding_protocol(args)
    # The project's build pipeline: the bench
    # read and the draft the user approves; never a direct write.
    elif name == "turn_type":
        return await _exec_turn_type(args, entity_id, pool, thread_id=thread_id)
    # The person's limits (runtime/user_limits): a read and a card, never a write.
    elif name == "limits":
        return await _exec_limits(args, entity_id, pool)
    # The Host's end of turn. session.py decides
    # whether the call ends the turn and stores the signal on the turn row;
    # this runs the writes it drives, against the calling turn's own thread.
    elif name == "close_turn":
        from mastercode.runtime.close_turn import execute as _close_turn_execute
        return await _close_turn_execute(args, entity_id=entity_id, pool=pool,
                                         session_id=session_id, thread_id=thread_id,
                                         turn_type=turn_type)
    elif name == "pipeline":
        return await _exec_pipeline(args, entity_id, pool)
    # The user's taste, written from conversation.
    elif name == "preference":
        return await _exec_preference(args)

    # ── list_projects meta-tool ─────────────────────
    # Read-side companion. The model calls this when the user
    # references a project by name and the model needs its id.
    elif name == "list_projects":
        return await _exec_list_projects(args, entity_id, pool)

    # ── system.* tool family ────────────────────
    # Entity-owned operational control over its own pod's apps. Same
    # authorization tier as app install/uninstall — does NOT touch
    # kernel/platform-level state. See default-skills/system.md for
    # the SOP that drives these calls.
    elif name == "system.restart_app" or name == "system_restart_app":
        return await _exec_system_restart_app(args)
    elif name == "system.start_app" or name == "system_start_app":
        return await _exec_system_start_app(args)
    elif name == "system.stop_app" or name == "system_stop_app":
        return await _exec_system_stop_app(args)
    elif name == "system.app_status" or name == "system_app_status":
        return await _exec_system_app_status(args)
    elif name == "app_probe" or name == "system.app_probe":
        return await _exec_app_probe(args)
    elif name == "browser_agent":
        return await _exec_browser_agent(args)
    # system.list_apps: retired (dup of explore_environment); its branch is
    # deleted.
    # system.refresh_sdk_auth: no longer a host tool — the
    # internal helper below stays for the kernel's own token refresher.

    # ── The host's own machine (no LLM) ──────────────────
    elif name == "diagnose":
        return await _exec_diagnose(args, entity_id, pool)

    # ── Environment knowledge (host-environment awareness) ──────────
    elif name == "explore_environment":
        return await _exec_explore_environment(args)
    elif name == "describe_app":
        return await _exec_describe_app(args)
    elif name == "check_page":
        return await _exec_check_page(args)
    elif name == "open_app":
        return await _exec_open_app(args)

    # ── Network action layer (read own network + propose changes) ───
    elif name == "get_network":
        return await _exec_get_network(args)
    elif name == "machine_map":
        return await _exec_machine_map(args)
    elif name == "propose_egress_rule":
        return await _exec_propose_egress_rule(args)
    elif name == "propose_mgmt_grant":
        return await _exec_propose_mgmt_grant(args)
    elif name == "propose_nac_policy":
        return await _exec_propose_nac_policy(args)
    elif name == "dcn":
        return await _exec_dcn(args)

    # ── Bastion EXECUTION (commit-side of the mesh): the host can
    #    now OPERATE a server / RUN an agent on a server it has a signed grant for.
    #    The signed-grant default-deny wall lives in the route; these just dispatch. ──
    elif name == "mgmt_run":
        return await _exec_mgmt_run(args)
    elif name == "machine_exec":
        # A root command on a machine whose bastion granted this Host root
        # commands (bastion 0.49.0). grant_bits is passed so an agent's call
        # is refused here as well as stripped from its scope (scoped_agent).
        return await _exec_machine_exec(args, entity_id, pool, grant_bits=grant_bits)
    elif name == "mesh_harness_run":
        return await _exec_mesh_harness_run(args, entity_id)
    elif name == "lab_run":
        return await _exec_lab_run(args)


    # ── Network/bastion GOVERNANCE commit-side: approve/revoke/contain/
    #    resolve — the per-host commit companions to propose_* + get_network. PUBLIC. ──
    elif name == "approve_nac_draft":
        return await _exec_approve_nac_draft(args)
    elif name == "revoke_nac_rule":
        return await _exec_revoke_nac_rule(args)
    elif name == "nac_status":
        return await _exec_nac_status(args)
    elif name == "quarantine_bastion":
        return await _exec_quarantine_bastion(args)
    elif name == "suspend_bastion":
        return await _exec_suspend_bastion(args)
    elif name == "manage_bastion":
        return await _exec_manage_bastion(args)
    elif name == "decision_resolve":
        return await _exec_decision_resolve(args)
    # ── Pairing grants: the anchor host admits / refuses / removes other
    #    hosts on a machine it anchored. approve widens (step-up in the service). ──
    elif name == "manage_pairing":
        return await _exec_manage_pairing(args)
    elif name == "publish_app":
        return await _exec_publish_app(args)
    elif name == "publish_site":
        return await _exec_publish_site(args)
    elif name == "ship_project_app":
        return await _exec_ship_project_app(args)
    elif name == "artifact":
        return await _exec_artifact(args)
    elif name == "tickets":
        # grant_bits: op='import' writes projects and phases as well as
        # tickets, so it is the Host's own act only — runtime/ticket_import.py
        # refuses it under any dispatched agent's grant.
        return await _exec_tickets(args, agent_grant=grant_bits)
    elif name == "host_message":
        return await _exec_host_message(args)
    elif name == "contacts_manage":
        return await _exec_contacts_manage(args)
    elif name == "personality_state":
        return await _exec_personality_state(args)
    elif name == "explanation_level":
        return await _exec_explanation_level(args)
    elif name == "host_changelog":
        # The weekly "state of the host" changelog — body lives in
        # runtime/host_changelog.py (_exec_host_changelog); this is also
        # the op the standing task fires through run_direct_op.
        from mastercode.runtime.host_changelog import _exec_host_changelog
        return await _exec_host_changelog(args)
    elif name == "oversight_ingest":
        # The oversight ledger's standing op
        # — body lives in runtime/oversight.py (_exec_oversight_ingest). This is
        # also the op the 5-minute standing task and the heartbeat wake both
        # fire through run_direct_op.
        from mastercode.runtime.oversight import _exec_oversight_ingest
        return await _exec_oversight_ingest(args)
    elif name == "entity_tasks":
        return await _exec_entity_tasks(args)
    elif name == "capabilities":
        return await _exec_capabilities(args)
    elif name == "kernel_op":
        # The generic kernel-route surface (runtime/kernel_ops.py): exposure
        # allowlist + per-call scope bits + walled paths enforced inside.
        from mastercode.runtime.kernel_ops import run_kernel_op
        return await run_kernel_op(args)
    elif name == "propose_capability":
        return await _exec_propose_capability(args)
    elif name == "context_ingest":
        return await _exec_context_ingest(args)
    elif name == "context_backend":
        return await _exec_context_backend(args)
    elif name == "set_project_manifest":
        return await _exec_set_project_manifest(args)
    elif name == "get_project":
        return await _exec_get_project(args)
    elif name == "update_project":
        return await _exec_update_project(args)
    elif name == "get_project_activity":
        return await _exec_get_project_activity(args)
    elif name == "set_project_routing":
        return await _exec_set_project_routing(args)
    elif name == "keys_manage":
        return await _exec_keys_manage(args)
    elif name == "calendar_read":
        return await _exec_calendar_read(args)
    elif name == "schedule_task":
        # The ORIGIN rides the call. A timer the host sets while
        # talking to someone in a project thread has to report back INTO that
        # thread, and the tool cannot know which thread that is on its own.
        #
        # `origin_context` FIRST, and it is the one that actually arrives:
        # schedule_task is a direct op, so op='create' comes through
        # `operate` → run_direct_op, which passes interaction_context=None on
        # purpose (it would otherwise switch on the surface gate).
        #
        # The `interaction_context` half of this fallback is NOT live today,
        # and it is labelled rather than deleted so nobody later reads it as
        # evidence that some caller supplies one. Traced: the three
        # chat-turn call sites are escalate (hardcoded name), the host's own
        # build doors (_host_own_tools — schedule_task is not one) and the
        # generic meta branch, which passes None; scoped_agent passes no
        # interaction_context at all. So every path to schedule_task arrives
        # with None. It stays as the contract for a future caller that does
        # pass one — the exact plumbing-that-looks-live mistake this whole
        # branch was fixed for, so it gets a label instead of a guess.
        return await _exec_schedule_task(
            args, session_id=session_id,
            interaction_context=(origin_context or interaction_context),
            # The calling turn's type decides what the task may carry.
            turn_type=turn_type,
            # ...and the plan is written into the thread it was set in.
            thread_id=thread_id)
    elif name == "task_template":
        return await _exec_task_template(
            args, turn_type=turn_type, session_id=session_id,
            interaction_context=(origin_context or interaction_context),
            thread_id=thread_id)
    elif name == "reachout":
        return await _exec_reachout(args, turn_type=turn_type)
    elif name == "reachout_channel":
        return await _exec_reachout_channel(args, turn_type=turn_type)
    elif name == "model_route":
        return await _exec_model_route(args)
    elif name == "inference_catalog":
        return await _exec_inference_catalog(args)
    elif name == "image_generate":
        return await _exec_image_generate(args)
    elif name == "import_repo":
        return await _exec_import_repo(args)
    elif name == "circles_manage":
        return await _exec_circles_manage(args)
    elif name == "embed_jobs":
        return await _exec_embed_jobs(args)
    elif name == "embedding_route":
        return await _exec_embedding_route(args)
    elif name == "harness_connections":
        return await _exec_harness_connections(args)
    elif name == "model_routing":
        return await _exec_model_routing(args)
    elif name == "model_bench":
        return await _exec_model_bench(args)
    elif name == "inbox_read":
        return await _exec_inbox_read(args)
    elif name == "index_my_code":
        return await _exec_index_my_code(args)
    elif name == "install_app":
        return await _exec_install_app(args)
    elif name == "integrations_manage":
        return await _exec_integrations_manage(args)
    elif name == "connect_compute":
        return await _exec_connect_compute(args)
    elif name == "list_app_catalog":
        return await _exec_list_app_catalog(args)
    elif name == "list_skills":
        return await _exec_list_skills(args, entity_id=entity_id, pool=pool)
    elif name == "manage_site":
        return await _exec_manage_site(args)
    elif name == "manage_site_domain":
        return await _exec_manage_site_domain(args)
    elif name == "chat_threads":
        # The turn's own identity, not the process's: which entity, which
        # session, and which Circles thread this turn is in. op='toc' scopes on
        # it and a `read` of another thread records the link from it.
        return await _exec_chat_threads(
            args, entity_id=entity_id, pool=pool, session_id=session_id,
            thread_id=thread_id)
    elif name == "memory_curate":
        return await _exec_memory_curate(args, session_id=session_id)
    elif name == "memory_scope":
        return await _exec_memory_scope(args, session_id=session_id)
    elif name == "phone_alias":
        return await _exec_phone_alias(args)
    elif name == "purchase_app":
        return await _exec_purchase_app(args)
    elif name == "refresh_app_store":
        return await _exec_refresh_app_store(args)
    elif name == "review_session":
        return await _exec_review_session(args)
    elif name == "search_my_code":
        return await _exec_search_my_code(args)
    elif name == "training_memory":
        return await _exec_training_memory(args)
    elif name == "uninstall_app":
        return await _exec_uninstall_app(args)
    elif name == "workspace_file":
        return await _exec_workspace_file(args)

    # ── review.* — the compare's read side and the pick ──────────────
    # A compare is STARTED by build(action='run', variants=[…]); review.*
    # shows its candidates and lands the user's pick. One build door, no
    # alias.
    elif name == "review.compare" or name == "review_compare":
        return await _exec_review_compare(args)
    elif name == "review.propose_commit" or name == "review_propose_commit":
        return await _exec_review_propose_commit(args)
    elif name == "review.adopt" or name == "review_adopt":
        a = dict(args or {})
        # Ambient project_id ONLY when the run cannot name its own. A run knows
        # which project it belongs to, and injecting the conversation's project
        # made "use run 12" from project 7 write run 12 over project 7 —
        # while every docstring said "into its project". The service refuses a
        # mismatch too; this keeps the common path from ever raising one.
        if (not a.get("project_id") and not a.get("run_id") and not a.get("run")
                and (interaction_context or "").startswith("project:")):
            try:
                a["project_id"] = int((interaction_context or "").split(":", 1)[1])
            except (ValueError, IndexError):
                pass
        return await _exec_review_adopt(a)
    elif name == "review.adopt_undo" or name == "review_adopt_undo":
        a = dict(args or {})
        if not a.get("project_id") and (interaction_context or "").startswith("project:"):
            try:
                a["project_id"] = int((interaction_context or "").split(":", 1)[1])
            except (ValueError, IndexError):
                pass
        return await _exec_review_adopt_undo(a)

    # ── The Ladder: escalation engine as host tools ──
    elif name == "ladder.run" or name == "ladder_run":
        # Same project derivation as review.adopt: the host rarely passes
        # project_id explicitly inside a project conversation.
        a = dict(args or {})
        if not a.get("project_id") and (interaction_context or "").startswith("project:"):
            try:
                a["project_id"] = int(interaction_context.split(":", 1)[1])
            except (ValueError, IndexError):
                pass
        return await _exec_ladder_run(a)
    elif name == "ladder.status" or name == "ladder_status":
        return await _exec_ladder_status(args)
    elif name == "ladder.decision" or name == "ladder_decision":
        return await _exec_ladder_decision(args)
    elif name == "harness_usage_windows":
        return await _exec_harness_usage_windows(args, pool)
    elif name == "harness_costs":
        return await _exec_harness_costs(args, pool)
    elif name == "harness_transcript":
        return await _exec_harness_transcript(args, pool)
    elif name == "credential_wiring":
        return await _exec_credential_wiring(args, entity_id, pool)
    elif name == "autofill":
        return await _exec_autofill(args, entity_id, pool)

    # <id>.run (codex.run / cursor.run) RETIRED: they POSTed to
    # the in-pod harness sidecar the pool replaced, and the
    # ".run" suffix made them code-exec — held by nobody, stripped from every
    # dispatch, refused by operate: unreachable by construction, yet still
    # advertised in the catalog and in review.md ("call <id>.run directly").
    # A single-harness run is build(action='run', harness=<id>) on the pool.
    # The names stay in
    # code_exec_guard.RETIRED_CODE_TOOLS so a stray call is refused loudly.

    return {"status": "error", "result": f"Unknown meta-tool: {name}"}


from mastercode.runtime.tool_call_ledger import INPUT_REFUSED as _INPUT_REFUSED  # noqa: E402


import contextvars as _contextvars  # noqa: E402


# ---------------------------------------------------------------
# Model calling — delegates to provider_gateway.call_model()
# ---------------------------------------------------------------

async def _call_entity_model(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int | None = None,
    session_id: str = None,
    fast: bool = False,
    tools: list[dict] = None,
    model_override: str = None,
    provider: str = None,
    provider_model: str = None,
    reasoning_effort: str = None,
    stream_cb=None,
    base_url: str = None,
    fallback_provider: str = None,
    fallback_model: str = None,
    fallback_base_url: str = None,
    reasoning_inline: bool = False,
    on_alive=None,
    cache_key: str = None,
    tool_choice: str | None = None,
    chosen_effort: str | None = None,
    _source: str = "entity_runtime",
) -> dict:
    """Thin wrapper — routes all inference through provider_gateway.call_model().

    When `stream_cb` is provided it is awaited with each visible text delta as
    the provider streams it. The return value is identical to the buffered
    call, so the tool-loop is unchanged.

    `base_url` = the route's endpoint (the local provider's live-looked-up
    server, or a BYO-endpoint role's pin; None otherwise). The chat path has
    passed it since the 3-lane router landed — this wrapper dropping it was
    the "unexpected keyword argument 'base_url'" crash on every chat turn.

    `fallback_provider`/`fallback_model`/`fallback_base_url` = the route's
    optional failover pair — the gateway retries an errored primary on them.
    Every route field the session turn forwards MUST be accepted here — this
    wrapper dropping a field is a repeat crasher (base_url above, then
    direct_provider on every chat turn). direct_provider is gone with the
    3-lane router — `provider` IS the route's provider.

    `reasoning_inline` = the route's model writes its reasoning inline in
    the text, closed by </think> (inference_config._route): the
    OpenAI-wire adapter holds the stream back until the close.

    `on_alive` = called on every streamed event (text, reasoning, tool-call
    argument deltas) so the turn registry sees a long write as alive. The
    gateway hands it to the primary dial AND the failover dial
    (provider_gateway.call_model `_dial_wire`). A session passing it while
    this wrapper did not accept it once crashed every chat turn.

    `tool_choice` "none" sends `tools` and allows no call (a search-mode
    Host's spend stop keeps its cached list);
    None sends what every call always sent.

    `chosen_effort` = the person's own effort pick for the route
    (inference_config.chosen_effort; None when they made none), beside
    `reasoning_effort`, which carries the OpenAI wire's "low" fallback. The
    Anthropic wire sends only this one (provider_gateway._dial_wire).

    `max_tokens` None (the default) is the routed model's own output limit:
    passed through as None, provider_gateway.call_model resolves it per dial
    (context_budget.output_limit_for, held by call_output_limit to the room
    the window leaves), so a failover model gets its own limit too. A fixed
    16384 default here would hold every caller that names no number to a
    ceiling the Host turn does not have. A caller wanting a short reply for a fixed purpose
    passes its own number, which is sent as given.

    `_source` = the cost log's source_module; the Host's own turn is
    'entity_runtime' (session `_chat_call_kw` carries it, so the turn's calls
    that go to provider_gateway.call_model directly are labelled the same)."""
    kwargs = {"_source": _source or "entity_runtime"}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if stream_cb is not None:
        kwargs["stream_cb"] = stream_cb
    if base_url:
        kwargs["base_url"] = base_url
    if fallback_provider:
        kwargs["fallback_provider"] = fallback_provider
    if fallback_model:
        kwargs["fallback_model"] = fallback_model
    if fallback_base_url:
        kwargs["fallback_base_url"] = fallback_base_url
    if reasoning_inline:
        kwargs["reasoning_inline"] = True
    if on_alive is not None:
        kwargs["on_alive"] = on_alive
    if cache_key:
        kwargs["cache_key"] = cache_key
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if chosen_effort:
        kwargs["chosen_effort"] = chosen_effort
    return await _call_entity_model_gateway(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        session_id=session_id,
        fast=fast,
        tools=tools,
        model_override=model_override,
        provider=provider,
        provider_model=provider_model,
        **kwargs,
    )
