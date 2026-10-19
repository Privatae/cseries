"""The Host's half of the "vendor refused this CLI version" signal.

THE DECISION. A Host may send a content-free signal that a
vendor refused the CLI version one of its harness runs used. It carries ONLY
{harness, cli_version, min_version_named, hour}, matched from the vendor CLI's
own refusal error (system-apps/_harness_runtime/vendor_refusal.py, in the harness
pod), rides once on the heartbeat this Host already sends (bridge_client.
_heartbeat_params), and the operator counts it with no Host id. Its one use: the
harness auto-update job skips its 3-day wait for that harness.

CONTENT-FREE. clean() admits exactly those four keys, each a closed shape: a
harness id from a fixed set, two version strings in the form the auto-update job
parses, and an hour ("YYYY-MM-DDTHH", UTC). No run id, prompt, output, path, user,
Host or entity id, address, or time finer than the hour can be in a record,
because a record with any other key, or any value outside those shapes, is
dropped whole rather than trimmed.

ONCE AN HOUR. A given (harness, cli_version) is sent at most once per UTC hour.
note() keeps one pending record per pair; take_due() hands each pending record to
the heartbeat once, stamped with the hour it leaves in, and marks the pair sent
for that hour whether or not the beat is answered (at most once, never retried;
the heartbeat goes every 30 s, so a refusal leaves within about half a minute,
and a later one in the same hour is dropped). The marks are kept on the Host's own
volume (SENT_PATH, on the entity-apps claim mounted at /var/lib/cseries), so a
restart inside the hour does not send the pair again.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("cseries.harness_refusals")

# The coding harnesses (the auto-update job's VENDORS). Anything else is dropped.
HARNESSES = frozenset({"antigravity", "claude-code", "codex", "cursor", "grok",
                       "kimi", "opencode"})
# A whole version string as the auto-update job parses one (the harness runtime's
# vendor_refusal.VERSION_RE): a cursor build stamp, or a dotted version with an
# optional suffix. Anchored and bounded, so nothing else can ride in it.
_VERSION = re.compile(r"(?:\d{4}\.\d{2}\.\d{2}-[0-9a-f]{1,40}"
                      r"|\d{1,6}\.\d{1,6}\.\d{1,6}(?:[-.][0-9A-Za-z.]{1,32})?)")
_HOUR = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}")
KEYS = ("harness", "cli_version", "min_version_named", "hour")
# The harness pod's part of the record; the Host adds the hour.
_POD_KEYS = frozenset({"harness", "cli_version", "min_version_named"})
# One beat carries at most this many (one per pair; seven harnesses).
MAX_PER_BEAT = 8

SENT_PATH = Path(os.environ.get("CSERIES_HARNESS_REFUSALS_SENT",
                                "/var/lib/cseries/harness-refusals-sent.json"))

_lock = threading.Lock()
_pending: dict = {}          # (harness, cli_version) -> record
_sent: dict | None = None    # (harness, cli_version) -> hour it was sent in


def hour_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(time.time() if ts is None else ts))


def _version_ok(v) -> bool:
    return isinstance(v, str) and len(v) <= 64 and _VERSION.fullmatch(v) is not None


def clean(rec) -> dict | None:
    """The record, or None when it is anything but the four keys in their shapes."""
    if not isinstance(rec, dict) or set(rec) != set(KEYS):
        return None
    h, cv, mv, hr = (rec[k] for k in KEYS)
    if not (isinstance(h, str) and h in HARNESSES and _version_ok(cv)
            and (mv is None or _version_ok(mv))
            and isinstance(hr, str) and _HOUR.fullmatch(hr)):
        return None
    return {"harness": h, "cli_version": cv, "min_version_named": mv, "hour": hr}


def _load_sent() -> dict:
    global _sent
    if _sent is None:
        _sent = {}
        try:
            raw = json.loads(SENT_PATH.read_text())
            for k, hr in (raw.items() if isinstance(raw, dict) else ()):
                h, _, cv = str(k).partition("|")
                if isinstance(hr, str) and _HOUR.fullmatch(hr):
                    _sent[(h, cv)] = hr
        except (OSError, ValueError):
            pass
    return _sent


def _save_sent(sent: dict) -> None:
    try:
        SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SENT_PATH.with_name(SENT_PATH.name + ".tmp")
        tmp.write_text(json.dumps({f"{h}|{cv}": hr for (h, cv), hr in sent.items()}))
        os.replace(tmp, SENT_PATH)
    except OSError as e:
        log.warning("harness refusals: sent marks not saved (%s)", e)


def note(harness_id: str, run_out, now: float | None = None) -> bool:
    """Queue the refusal a harness run's result reports, if it reports one for the
    harness that ran. True when a record was queued. Never raises."""
    try:
        pod = run_out.get("vendor_refusal") if isinstance(run_out, dict) else None
        if not isinstance(pod, dict) or set(pod) != _POD_KEYS or pod.get("harness") != harness_id:
            return False
        hour = hour_of(now)
        rec = clean({**pod, "hour": hour})
        if rec is None:
            return False
        key = (rec["harness"], rec["cli_version"])
        with _lock:
            if _load_sent().get(key) == hour:
                return False              # this pair already went out this hour
            _pending[key] = {k: rec[k] for k in _POD_KEYS}
        return True
    except Exception as e:  # noqa: BLE001 — a run's result must never fail here
        log.warning("harness refusals: not noted (%s)", e)
        return False


def take_due(now: float | None = None) -> list:
    """The records to carry on this heartbeat, each pair at most once an hour. A
    record's hour is the hour it is sent in, so "once an hour" holds on the wall
    clock, across the turn of an hour too. Marks them sent before they leave.
    Never raises."""
    try:
        cur = hour_of(now)
        with _lock:
            if not _pending:
                return []
            sent = _load_sent()
            for key in [k for k, hr in sent.items() if hr != cur]:
                sent.pop(key, None)       # marks from hours now past protect nothing
            out = []
            for key in sorted(_pending):
                if sent.get(key) == cur or len(out) >= MAX_PER_BEAT:
                    continue
                rec = clean({**_pending[key], "hour": cur})
                if rec is not None:
                    out.append(rec)
                    sent[key] = cur
            # Sent, or already sent this hour: either way nothing more goes out
            # for these pairs this hour. Past MAX_PER_BEAT waits for the next beat.
            for key in [k for k in _pending if sent.get(k) == cur]:
                _pending.pop(key, None)
            if out:
                _save_sent(sent)
        return out
    except Exception as e:  # noqa: BLE001 — the heartbeat must go out regardless
        log.warning("harness refusals: none taken (%s)", e)
        return []
