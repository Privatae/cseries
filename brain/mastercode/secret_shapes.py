"""
mastercode/secret_shapes.py — catch pasted keys and credentials before inference.

The rules live in secret_shapes.json beside this file and are SHARED with the
composer in circles-web / workbench-web (src/lib/secret-shapes.ts): the browser
warns the user at paste/send time, this module is the enforcing copy in the pod.
Same file, same regexes, same ids — a hit named in the UI is the hit that gets
redacted here.

Why a second copy runs pod-side: text reaches send_message from more doors than
the two composers (the bastion, harness surfaces, the API). The browser strip
is a courtesy; this is the wall. It sits at the top of send_message, before the
input guard, the message array, the turn row and the memory write — so a hard
hit never lands anywhere.

Two severities (see the JSON header):
  hard — an unambiguous vendor/key shape. Redacted to `[REDACTED:<id>]`.
  soft — a heuristic (assignment, entropy). Reported by scan(), never redacted
         here: a git SHA or a random id is not a leak, and there is no user in
         the pod to ask. The browser nudges on these; the pod leaves them alone.

Pure: no network, no DB, no LLM. Fails toward "report nothing" only on a
malformed rules file, and that is a deploy-time bug the gate test catches.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret_shapes.json")

_HARD = "hard"
_SOFT = "soft"


@dataclasses.dataclass(frozen=True)
class Finding:
    id: str          # rule id, e.g. "openai-key"
    label: str       # human label from the rules file
    severity: str    # "hard" | "soft"
    start: int       # span of the SECRET (the redaction group), not the whole match
    end: int
    preview: str     # first 4 + "…" + last 4 chars — never the value


@dataclasses.dataclass(frozen=True)
class _Rule:
    id: str
    label: str
    severity: str
    regex: re.Pattern
    group: int


_rules: list[_Rule] | None = None
_entropy: dict | None = None
_redaction_format = "[REDACTED:{id}]"
# The marker redact() writes, for an id this rules file names. Built in _load.
_marker_re: re.Pattern | None = None


def _load() -> None:
    global _rules, _entropy, _redaction_format, _marker_re
    if _rules is not None:
        return
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        doc = json.load(f)
    rules: list[_Rule] = []
    # re.ASCII on every pattern: a flag-less JS RegExp treats only [A-Za-z0-9_]
    # as word characters, so \b / \w must mean the same here or "cafésk-ant-…"
    # is a hard hit in the browser and not in the pod. All patterns are
    # ASCII-oriented, so nothing is lost.
    for r in doc["rules"]:
        flags = re.ASCII | (re.IGNORECASE if "i" in (r.get("flags") or "") else 0)
        rules.append(_Rule(
            id=r["id"], label=r["label"], severity=r["severity"],
            regex=re.compile(r["pattern"], flags), group=int(r.get("group") or 0),
        ))
    ent = doc.get("entropy") or {}
    if ent:
        ent = dict(ent)
        ent["regex"] = re.compile(ent["charset_pattern"], re.ASCII)
    _redaction_format = doc.get("redaction_format") or _redaction_format
    # Only the ids this file names: a person's "[REDACTED:sk-ant-…]" is not a
    # marker, so a key typed inside brackets is still found.
    ids = [r.id for r in rules] + ([ent["id"]] if ent else [])
    head, has_id, tail = _redaction_format.partition("{id}")
    _marker_re = re.compile(re.escape(head) + (
        "(?:" + "|".join(re.escape(i) for i in ids) + ")" + re.escape(tail) if has_id else ""))
    _rules, _entropy = rules, ent or None


def preview(value: str) -> str:
    """Never the value: 4 + … + 4 for anything long enough, else just a mask."""
    if len(value) <= 12:
        return "•" * min(len(value), 8)
    return f"{value[:4]}…{value[-4:]}"


def shannon_bits_per_char(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _entropy_hit(token: str) -> bool:
    ent = _entropy
    if not ent or len(token) < int(ent["min_length"]):
        return False
    if not (re.search(r"[0-9]", token) and re.search(r"[A-Za-z]", token)):
        return False
    return shannon_bits_per_char(token) >= float(ent["min_bits_per_char"])


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """`text` with each span replaced by NULs of the same length, so every span
    still indexes the original. NUL is no rule's secret character and no word
    character, as a marker's brackets are not."""
    for s, e in spans:
        text = text[:s] + "\x00" * (e - s) + text[e:]
    return text


def scan(text: str | None) -> list[Finding]:
    """Every secret-shaped span in `text`, hard and soft, ordered by position.
    Overlapping hits collapse to the first rule that claimed the span (rule
    order in the JSON is precedence — `sk-ant-` is listed before `sk-`).

    A claimed span reads as one opaque run to every rule after the one that
    claimed it: a marker an earlier redact() wrote is claimed before any rule
    runs, and a span a rule claims in this pass is blanked before the next
    rule runs. So one pass sees what a second pass would, and cutting cut text
    finds nothing and changes nothing: a door cuts a message once and the turn
    cuts it again (server/secret_cut.py). Without the marker half,
    url-credential's password group re-claims `https://u:[REDACTED:github-token]@…`
    and its user slot splits `https://[REDACTED:github-token]@…` at the
    marker's own colon. Without the same-pass half, a Telegram token as a
    link's user name (it holds a colon) starts url-credential's password group
    inside the token, the overlap loses, and the password is left for a second
    pass that a door cutting once never runs."""
    if not text:
        return []
    _load()
    assert _rules is not None and _marker_re is not None
    taken: list[tuple[int, int]] = [m.span() for m in _marker_re.finditer(text)]
    out: list[Finding] = []
    seen = _blank(text, taken)

    def _free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    for rule in _rules:
        claimed: list[tuple[int, int]] = []
        for m in rule.regex.finditer(seen):
            a, b = m.span(rule.group)
            if a == b or not _free(a, b):
                continue
            claimed.append((a, b))
            out.append(Finding(rule.id, rule.label, rule.severity, a, b, preview(text[a:b])))
        taken.extend(claimed)
        seen = _blank(seen, claimed)
    if _entropy:
        for m in _entropy["regex"].finditer(seen):
            a, b = m.span()
            if not _free(a, b) or not _entropy_hit(text[a:b]):
                continue
            taken.append((a, b))
            out.append(Finding(_entropy["id"], _entropy["label"], _entropy["severity"],
                               a, b, preview(text[a:b])))
    out.sort(key=lambda f: f.start)
    return out


def redact(text: str | None, severities: tuple[str, ...] = (_HARD,)) -> tuple[str, list[Finding]]:
    """Replace every finding whose severity is in `severities` with the
    redaction marker. Returns (new_text, ALL findings incl. the ones left
    alone) so the caller can log soft hits without acting on them. Findings'
    spans refer to the ORIGINAL text."""
    findings = scan(text)
    if not text or not findings:
        return text or "", findings
    _load()
    pieces: list[str] = []
    cursor = 0
    for f in findings:
        if f.severity not in severities:
            continue
        pieces.append(text[cursor:f.start])
        pieces.append(_redaction_format.format(id=f.id))
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces), findings


def hard(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == _HARD]


def soft(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == _SOFT]
