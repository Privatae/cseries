"""
Signal rule engine — deterministic entity activity classification.

No inference. Rules + thresholds + templates turn recent signals into a
third-person presence sentence ("Ada is reading your inbox · 3 new
since morning"). Model-free by design.

Architecture:
  - Rules live in /app/engine/rules.yaml (seeded defaults, user-editable
    via the overlay filesystem in future).
  - The engine subscribes to the in-container signal cache
    /app/ceigas/signals.cache.jsonl (already populated by the bridge via
    the tunnel) + /app/engine/local-signals.jsonl for container-local
    events (portal_access, backup_run, code_run, etc).
  - Every rule pattern-matches a window of recent signals; the first
    matching rule wins, with a priority tiebreak.
  - Templates use {entity_name} + slot-fills to produce the final
    sentence. Zero tokens.

This is C-Series's answer to "software so good you don't need AI" — the
entity's surfaced state is a projection of its signals, not a model
call per tick.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("privatae.pc.engine.rules")

RULES_PATH = Path("/app/engine/rules.yaml")
CEIGAS_SIGNALS = Path("/app/ceigas/signals.cache.jsonl")
LOCAL_SIGNALS = Path("/app/engine/local-signals.jsonl")

# Short activity ID → third-person sentence template. Templates use
# Python str.format() with whitelisted keys so user-written rules can't
# inject arbitrary attribute lookups.
DEFAULT_TEMPLATES = {
    # Google — full surface
    "reading_gmail": "{entity_name} is reading your Gmail · {count} since {since}",
    "drafting_gmail": "{entity_name} is drafting a reply in Gmail",
    "sending_gmail": "{entity_name} is sending an email",
    "searching_gmail": "{entity_name} is searching Gmail for {query}",
    "reading_drive": "{entity_name} is reading your Google Drive · {source}",
    "editing_drive": "{entity_name} is editing {source} in Drive",
    "searching_drive": "{entity_name} is searching Drive for {query}",
    "reading_calendar": "{entity_name} is checking your calendar",
    "scheduling": "{entity_name} is scheduling an event",
    "reading_contacts": "{entity_name} is looking up a contact",
    "watching_youtube": "{entity_name} is reading a YouTube transcript · {source}",
    "searching_youtube": "{entity_name} is searching YouTube for {query}",
    "google_auth": "{entity_name} is reconnecting Google",
    # Generic Portal sources
    "reading_folder": "{entity_name} is looking through {source} · last at {last}",
    "reading_file": "{entity_name} is reading {source}",
    # Apps
    "app_opened": "{entity_name} opened {source}",
    # Code
    "coding": "{entity_name} is coding in {project}",
    "code_ran": "{entity_name} just ran a build in {project}",
    # System
    "backing_up": "{entity_name} is saving a memory snapshot",
    "restoring": "{entity_name} is restoring a backup",
    "updating": "{entity_name} is applying the {version} update",
    "update_ready": "{entity_name} has a {version} update ready for you",
    # Governance
    "permission_pending": "{entity_name} is waiting for your approval on {count} change{plural}",
    # States
    "thinking": "{entity_name} is thinking it over",
    "idle": "{entity_name} is idle · last active {last}",
    "offline": "{entity_name} is offline",
}

DEFAULT_RULES: list[dict[str, Any]] = [
    # ——— Google: highest-priority actions first. Match on
    # connection_provider (per-provider key) — manifests' connection_kind
    # is a category (email/files/video) shared across providers. ———
    {"id": "sending_gmail", "priority": 130, "template": "sending_gmail",
     "match": {"signal_type": "portal_access", "window_s": 60, "min_count": 1,
               "metadata_match": {"connection_provider": "gmail", "operation": "send"}}},
    {"id": "drafting_gmail", "priority": 125, "template": "drafting_gmail",
     "match": {"signal_type": "portal_access", "window_s": 120, "min_count": 1,
               "metadata_match": {"connection_provider": "gmail", "operation": "draft"}}},
    {"id": "searching_gmail", "priority": 118, "template": "searching_gmail",
     "match": {"signal_type": "portal_access", "window_s": 60, "min_count": 1,
               "metadata_match": {"connection_provider": "gmail", "operation": "search"}}},
    {"id": "reading_gmail", "priority": 110, "template": "reading_gmail",
     "match": {"signal_type": "portal_access", "window_s": 90, "min_count": 1,
               "metadata_match": {"connection_provider": "gmail", "operation": "read"}}},

    # Drive
    {"id": "editing_drive", "priority": 120, "template": "editing_drive",
     "match": {"signal_type": "portal_access", "window_s": 120, "min_count": 1,
               "metadata_match": {"connection_provider": "drive", "operation": "write"}}},
    {"id": "searching_drive", "priority": 115, "template": "searching_drive",
     "match": {"signal_type": "portal_access", "window_s": 60, "min_count": 1,
               "metadata_match": {"connection_provider": "drive", "operation": "search"}}},
    {"id": "reading_drive", "priority": 108, "template": "reading_drive",
     "match": {"signal_type": "portal_access", "window_s": 120, "min_count": 1,
               "metadata_match": {"connection_provider": "drive", "operation": "read"}}},

    # Calendar / scheduling
    {"id": "scheduling", "priority": 115, "template": "scheduling",
     "match": {"signal_type": "portal_access", "window_s": 120, "min_count": 1,
               "metadata_match": {"connection_provider": "calendar", "operation": "write"}}},
    {"id": "reading_calendar", "priority": 105, "template": "reading_calendar",
     "match": {"signal_type": "portal_access", "window_s": 90, "min_count": 1,
               "metadata_match": {"connection_provider": "calendar", "operation": "read"}}},

    # Contacts
    {"id": "reading_contacts", "priority": 102, "template": "reading_contacts",
     "match": {"signal_type": "portal_access", "window_s": 60, "min_count": 1,
               "metadata_match": {"connection_provider": "contacts", "operation": "read"}}},

    # YouTube
    {"id": "searching_youtube", "priority": 112, "template": "searching_youtube",
     "match": {"signal_type": "portal_access", "window_s": 60, "min_count": 1,
               "metadata_match": {"connection_provider": "youtube", "operation": "search"}}},
    {"id": "watching_youtube", "priority": 106, "template": "watching_youtube",
     "match": {"signal_type": "portal_access", "window_s": 180, "min_count": 1,
               "metadata_match": {"connection_provider": "youtube", "operation": "read"}}},

    # OAuth handshake (any provider — caught from the audit signal
    # emitted by create_connection). connection_config records auth.
    {"id": "google_auth", "priority": 135, "template": "google_auth",
     "match": {"signal_type": "portal_access", "window_s": 30, "min_count": 1,
               "metadata_match": {"operation": "auth"}}},

    # ——— Generic Portal sources (non-connection folder mounts) ———
    {"id": "reading_file", "priority": 85, "template": "reading_file",
     "match": {"signal_type": "portal_access", "window_s": 90, "min_count": 1,
               "metadata_match": {"source_kind": "folder", "operation": "read"}}},
    {"id": "reading_folder", "priority": 80, "template": "reading_folder",
     "match": {"signal_type": "portal_access", "window_s": 120, "min_count": 1,
               "metadata_match": {"source_kind": "folder", "operation": "list"}}},

    # ——— App interactions ———
    {"id": "app_opened", "priority": 60, "template": "app_opened",
     "match": {"signal_type": "app_open", "window_s": 15, "min_count": 1}},

    # ——— Code ———
    {"id": "coding", "priority": 90, "template": "coding",
     "match": {"signal_type": "code_action", "window_s": 180, "min_count": 1}},
    {"id": "code_ran", "priority": 92, "template": "code_ran",
     "match": {"signal_type": "code_run_complete", "window_s": 60, "min_count": 1}},

    # ——— System lifecycle ———
    {"id": "backing_up", "priority": 95, "template": "backing_up",
     "match": {"signal_type": "backup_started", "window_s": 60, "min_count": 1}},
    {"id": "restoring", "priority": 97, "template": "restoring",
     "match": {"signal_type": "backup_restoring", "window_s": 60, "min_count": 1}},
    {"id": "updating", "priority": 96, "template": "updating",
     "match": {"signal_type": "pc_update_bundle_staged", "window_s": 120, "min_count": 1}},

    # ——— Fallbacks ———
    {"id": "idle", "priority": 1, "template": "idle",
     "match": {"window_s": 300, "min_count": 0}},
]


# ── Signal reader ───────────────────────────────────────────────

def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _iter_signals(paths: Iterable[Path], window_s: int) -> Iterable[dict]:
    """Yield signals from the given jsonl files newer than `window_s`."""
    cutoff = time.time() - max(1, int(window_s))
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open() as f:
                # Read all — caches are small (bridge rotates them)
                lines = f.readlines()[-2000:]
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    s = json.loads(raw)
                except Exception:
                    continue
                ts = _parse_iso(s.get("created_at") or s.get("ts"))
                if ts is None or ts >= cutoff:
                    yield s
        except Exception as e:
            logger.debug("signal read %s failed: %s", path, e)


# ── Matching ────────────────────────────────────────────────────

def _metadata_match(sig: dict, spec: dict) -> bool:
    md = sig.get("metadata") or {}
    for k, v in spec.items():
        if md.get(k) != v:
            return False
    return True


def _rule_matches(rule: dict, signals: list[dict]) -> tuple[bool, list[dict]]:
    m = rule.get("match", {}) or {}
    sigtype = m.get("signal_type")
    md_match = m.get("metadata_match") or {}
    matched = []
    for s in signals:
        if sigtype and s.get("signal_type") != sigtype:
            continue
        if md_match and not _metadata_match(s, md_match):
            continue
        matched.append(s)
    return len(matched) >= int(m.get("min_count", 1)), matched


# ── Template rendering ─────────────────────────────────────────

_TIME_RE = re.compile(r"%H|%M|%I|%p|%Z")


def _since_phrase(oldest_ts: float | None) -> str:
    if not oldest_ts:
        return "lately"
    delta = time.time() - oldest_ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        hrs = delta / 3600
        return f"{hrs:.0f}h ago" if hrs >= 1 else "this hour"
    return "earlier today"


def _render(template_key: str, matched: list[dict], entity_name: str) -> str:
    tmpl = DEFAULT_TEMPLATES.get(template_key, "{entity_name} is active")
    count = len(matched)
    oldest = min((_parse_iso(s.get("created_at") or s.get("ts")) or time.time() for s in matched), default=None) if matched else None
    latest = max((_parse_iso(s.get("created_at") or s.get("ts")) or 0 for s in matched), default=None) if matched else None

    # Slot values. Only a whitelisted vocabulary — rules never get to
    # fill arbitrary attributes.
    md = matched[0].get("metadata", {}) if matched else {}
    slots = {
        "entity_name": entity_name,
        "count": count,
        "since": _since_phrase(oldest),
        "last": _since_phrase(latest),
        "source": (md.get("source_display") or md.get("app_name") or md.get("file_name")
                   or md.get("app_id") or "source"),
        "project": md.get("project") or "workspace",
        "version": md.get("to_version") or md.get("tag") or "",
        "query": (md.get("query") or "")[:40] or "something",
        "plural": "s" if count != 1 else "",
    }
    try:
        return tmpl.format(**slots)
    except Exception:
        return f"{entity_name} is active"


# ── Public API ─────────────────────────────────────────────────

def evaluate(entity_name: str = "Your host") -> dict[str, Any]:
    """Run all rules against the current signal window. Return presence state."""
    # Find widest window needed across rules to read once
    rules = DEFAULT_RULES  # User rules override later via rules.yaml
    max_window = max((r.get("match", {}).get("window_s", 300) for r in rules), default=300)
    signals = list(_iter_signals([CEIGAS_SIGNALS, LOCAL_SIGNALS], max_window))

    # Sort by priority descending, first match wins
    best = None
    for rule in sorted(rules, key=lambda r: -r.get("priority", 0)):
        ok, matched = _rule_matches(rule, signals)
        if ok:
            best = (rule, matched)
            break

    if not best:
        return {
            "activity": "offline",
            "sentence": DEFAULT_TEMPLATES["offline"].format(entity_name=entity_name),
            "source_signals": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    rule, matched = best
    return {
        "activity": rule["id"],
        "sentence": _render(rule["template"], matched, entity_name),
        "source_signals": len(matched),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def emit_local_signal(signal_type: str, metadata: dict | None = None,
                      severity: str = "info", content: str | None = None) -> None:
    """Container-local signal emission. Appends to
    /app/engine/local-signals.jsonl — readable by the rule engine on next tick.
    """
    LOCAL_SIGNALS.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "signal_type": signal_type,
        "metadata": metadata or {},
        "severity": severity,
        "content": content or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with LOCAL_SIGNALS.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    # Simple rotation: keep last 2000 lines
    try:
        size = LOCAL_SIGNALS.stat().st_size
        if size > 2_000_000:  # 2 MB
            lines = LOCAL_SIGNALS.read_text().splitlines()[-2000:]
            LOCAL_SIGNALS.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
