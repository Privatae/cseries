"""runtime/session_intake.py — back-compat re-export shim of triage.py.

Both modules existed in parallel during the 2026-04 session.py
decomposition. Their byte content was identical through the triage
classifier; only triage.py carried the schemas that landed later
(_BRIDGE_TOOL_SCHEMAS, the _meta blocks per tool, etc.).

As of 2026-05-25, triage.py is the canonical source. This file is a
re-export so existing imports keep working without touching the
callers — notably session.py (inference path) + prompt_schema.py + the
scripts/ test harnesses.

Direct edits should land in triage.py. If you change a constant or
function body here, you'll be editing dead code — the runtime will
keep using triage.py's version.
"""

# Star-import covers public names; explicit re-imports below cover the
# underscore-prefixed ones (which `*` deliberately skips).
from mastercode.runtime.triage import *  # noqa: F401,F403

from mastercode.runtime.triage import (  # noqa: F401
    # Logging / kill-switch hooks
    _prompt_console,
    _kill_switch,
    set_prompt_console,
    set_kill_switch,
    # Weight-key conversion helpers. These are the live reason this shim is
    # still here: prompt_schema.py:744 imports _to_dim_names from this module
    # lazily, so it must keep resolving.
    _DIM_NAMES,
    _to_weight_keys,
    _to_dim_names,
    # The triage primitives this shim also re-exported (_extract_chat_signals,
    # _triage_message, _triage_heuristic, _QUICK_PATTERNS, _MODE_LIMITS) went
    # with the classifier itself on 2026-09-19 — nothing had called them since
    # the Grok arm was removed, here or anywhere else.
    # Tool schemas
    _ARCHIVED_BOT_TOOL_SCHEMAS,
    _BOT_TOOL_SCHEMAS,
    _META_TOOL_NAMES,
    # Bridge schemas (were missing entirely from old session_intake.py —
    # session.py (inference path)'s import had been broken silently the whole
    # time. Reconciling brings them in scope alongside the other names).
    _BRIDGE_TOOL_NAME_TO_OP,
    _BRIDGE_TOOL_SCHEMAS,
    _BRIDGE_TOOL_NAMES,
    # Public registry re-exports
    PUBLIC_TOOL_SCHEMAS,
    PUBLIC_META_TOOL_NAMES,
)
