"""
The one function every provider calls to emit an integration event.

Thin wrapper over rules.emit_local_signal — keeps integration code
vocabulary small and consistent, and gives us one place to add shape
migrations later (CloudEvents envelope, fingerprinting, dedup, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("privatae.pc.integrations.signals")


def emit_portal_access(
    connection_id: str,
    connection_kind: str,
    connection_provider: str,
    operation: str,
    source_display: str | None = None,
    query: str | None = None,
    record_count: int = 1,
    extra: dict[str, Any] | None = None,
    content: str | None = None,
) -> None:
    """Emit a portal_access signal onto the local bus.

    The rule engine consumes this to classify activity (reading_gmail,
    editing_drive, etc.) deterministically. Signals NEVER contain raw
    tokens, bodies, or PII beyond the shown slot values.
    """
    metadata: dict[str, Any] = {
        "connection_id": connection_id,
        "connection_kind": connection_kind,
        "connection_provider": connection_provider,
        "operation": operation,
        "record_count": int(record_count),
    }
    if source_display is not None:
        metadata["source_display"] = str(source_display)[:120]
    if query is not None:
        metadata["query"] = str(query)[:120]
    if extra:
        # Whitelist a few safe keys to avoid ballooning signal metadata.
        SAFE = {"resource", "thread_id", "account_id", "project", "label"}
        for k, v in extra.items():
            if k in SAFE and v is not None:
                metadata[k] = v

    try:
        from server.engine import rules as rules_engine
        rules_engine.emit_local_signal(
            "portal_access",
            metadata=metadata,
            content=content,
            severity="info",
        )
    except Exception as e:
        logger.warning("emit_portal_access failed: %s", e)
