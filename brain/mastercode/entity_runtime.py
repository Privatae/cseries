"""
Entity Runtime — backward-compatible import shim.

The implementation has been decomposed into mastercode.runtime.*
This file re-exports all public symbols so existing imports work unchanged.
"""
from mastercode.runtime import *  # noqa: F401,F403
from mastercode.runtime import (
    EntityRuntime,
    EntitySession,
    set_prompt_console,
    set_kill_switch,
    _prompt_console,
    _kill_switch,
    _call_entity_model,
    _build_tool_schemas,
    _execute_meta_tool,
    _build_xml_tool_prompt,
    _parse_xml_tool_calls,
    get_active_session_messages,
    _DIM_NAMES,
    _to_weight_keys,
    _to_dim_names,
    _BOT_TOOL_SCHEMAS,
    _META_TOOL_NAMES,
    _ARCHIVED_BOT_TOOL_SCHEMAS,
    PUBLIC_TOOL_SCHEMAS,
)
