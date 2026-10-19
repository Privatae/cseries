"""vendor_refusal — recognise a vendor refusing the CLI version this pod runs.

THE DECISION (2026-10-06). A Host may send a content-free "vendor refused
this CLI version" signal. It carries ONLY {harness, cli_version,
min_version_named, hour}, matched from the vendor CLI's OWN refusal error, rides
once on the heartbeat the Host already sends, and is counted by the operator
with no Host id. Its one use: the operator's harness auto-update job skips its 3-day wait for that harness. A
signed-out run never reaches a vendor's version check (grok 0.2.112 and 1.0.46
both just say "Not signed in"), so only a Host's own run can see a refusal.

This module is the matching half, in the harness pod. It returns at most
{harness, cli_version, min_version_named} for the run's terminal frame; the Host
adds the hour (server/harness_refusals.py) and sends it.

WHAT IS READ. Only the messages of the run's `error` events: the CLI's own error
frames, as events.normalize_line classifies them (grok's {"type":"error",
"message":...}, events.parse_grok). Model text, tool output, unparsed stdout and
stderr are never read, because a model or a command can print anything, and
stderr is not only the CLI's own (kimi streams its whole reasoning there,
harness_core's stderr drain). And only on a run that FAILED: a run that
succeeded was not refused.

MATCHING IS PER VENDOR AND CONSERVATIVE. Every fragment a matcher needs is the
vendor's own text, and all of them must be present. An error that does not match
exactly is not a refusal, and nothing is sent.

  grok   xAI refused grok 0.2.112 on 2026-10-06 with HTTP 426. The text recorded
         from that run is "426 Upgrade Required ... update to version 1.0.13 or
         later". A refusal is
         "426 Upgrade Required" and "update to version <X> or later" in the same
         message, X being min_version_named. The record elides the text between
         the two fragments, so nothing about it is assumed.

  codex, opencode, kimi: no refusal text in the vendors' source at the pinned
         versions (searched 2026-10-07 for 426, upgrade-required, minimum/client
         version, too old, outdated, please update: codex rust-v0.160.1 meets 426
         only on its websocket handshake and falls back to HTTP silently;
         opencode v1.18.35 and kimi-code 2.1.1 only send their version). No
         matcher, so nothing is ever sent for them.
  cursor, antigravity, claude-code: closed source, and no refusal from them has
         been seen. No matcher.

A new vendor is one entry in MATCHERS, with the vendor's own text cited beside it.
"""
from __future__ import annotations

import re
import subprocess
import threading

# The version a CLI states about itself, parsed exactly as the auto-update job
# parses it (the job's version probe:
# `--version`, stdout then stderr, first match of this pattern). The job compares a
# refusal's cli_version with that parsed value, so both sides must read the same
# string the same way: grok 1.0.46 prints "grok 1.0.46 (2765805b9442)" -> "1.0.46".
VERSION_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2}-[0-9a-f]+|\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)")

# How much of each message, and how many messages, are looked at. A refusal is one
# short line; this only bounds the work on a run that failed with a flood.
_MAX_TEXT = 4096
_MAX_MESSAGES = 32

_SEMVER = r"v?(\d+\.\d+\.\d+)"


def _match_grok(text: str) -> str | None:
    if not re.search(r"\b426 Upgrade Required\b", text, re.I):
        return None
    m = re.search(r"\bupdate to version " + _SEMVER + r" or later\b", text, re.I)
    return m.group(1) if m else None


# harness id -> fn(one error text) -> min_version_named, or None for no refusal.
MATCHERS = {
    "grok": _match_grok,
}

_version_cache: dict = {}
_version_lock = threading.Lock()


def parse_version(raw: str) -> str | None:
    m = VERSION_RE.search(raw or "")
    return m.group(1) if m else None


def cli_version(spec) -> str | None:
    """The pod's CLI version, as the auto-update job reads it; None when the CLI
    cannot say. Read once per binary path: a baked image's binary never changes
    while the pod runs."""
    key = str(spec.binary)
    with _version_lock:
        if key in _version_cache:
            return _version_cache[key]
    try:
        r = subprocess.run([key, "--version"], capture_output=True, text=True, timeout=10)
        v = parse_version((r.stdout or "") + (r.stderr or ""))
    except Exception:  # noqa: BLE001 — no version means no record, never a failed run
        v = None
    with _version_lock:
        _version_cache[key] = v
    return v


def match(harness_id: str, texts) -> str | None:
    """min_version_named when one of `texts` is this vendor's refusal, else None."""
    fn = MATCHERS.get(harness_id)
    if fn is None:
        return None
    for t in list(texts or [])[:_MAX_MESSAGES]:
        if isinstance(t, str) and t:
            got = fn(t[:_MAX_TEXT])
            if got:
                return got
    return None


def detect(spec, *, ok: bool, errors) -> dict | None:
    """The refusal record for a run's terminal frame, or None. `errors` are the
    messages of the run's `error` events. Never raises: this runs at the end of
    every failed run and must not be able to fail one."""
    try:
        if ok or spec.id not in MATCHERS:
            return None
        named = match(spec.id, errors)
        if not named:
            return None
        version = cli_version(spec)
        if not version:
            return None
        return {"harness": spec.id, "cli_version": version, "min_version_named": named}
    except Exception:  # noqa: BLE001
        return None
