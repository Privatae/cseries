"""
mastercode.runtime — Decomposed entity runtime package.

The EntityRuntime engine split into focused modules:
  triage.py          — message classification and signal extraction
  tools.py           — tool schemas, meta-tool execution, model calling
  context.py         — context window management and compression
  prompt_builder.py  — system prompt assembly from personality + memory + codebooks
  session.py         — EntitySession + EntityRuntime class (session lifecycle + inference)

The inference methods (send_message and its pre/post pipelines) live on
EntityRuntime in session.py. A placeholder inference.py used to sit here as a
marker for a future extraction; it held no code and nothing imported it, so it
was removed 2026-07-25.
"""

from mastercode.runtime.session import EntityRuntime, EntitySession
from mastercode.runtime.triage import (
    set_prompt_console,
    set_kill_switch,
    _prompt_console,
    _kill_switch,
    _DIM_NAMES,
    _to_weight_keys,
    _to_dim_names,
    _BOT_TOOL_SCHEMAS,
    _META_TOOL_NAMES,
    _ARCHIVED_BOT_TOOL_SCHEMAS,
    PUBLIC_TOOL_SCHEMAS,
)
from mastercode.runtime.tools import (
    _call_entity_model,
    _build_tool_schemas,
    _execute_meta_tool,
    _build_xml_tool_prompt,
    _parse_xml_tool_calls,
)
from mastercode.runtime.context import get_active_session_messages
