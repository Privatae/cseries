#!/usr/bin/env python3
"""Adversarial gate: can the host actually ANSWER, from the capability system,
for everything C-Series can do?

WHY THIS EXISTS. `docs/CAPABILITIES.md` is a CLOSED WORLD — the host is told
that a capability absent from it does not exist, and `default-skills/explore.md`
forbids hedging. That rule is only safe if the corpus is complete AND findable
in the words a user actually uses. It was neither:

  - The five permanent orchestration verbs (dispatch/operate/recall/
    agent_status/escalate) were advertised to every host on every turn and
    appeared nowhere in the doc.
  - The whole 543-route surface rendered as bare prefix+count, so a search for
    "voice", "screenshot", "email" or "password" returned ZERO hits — while
    realtime voice, /api/screen/capture, the inbox lane and the vault's
    browser_type broker all shipped. The host denied working features.

Every other capability gate here checks STRUCTURE (does the doc regenerate, do
lanes resolve, do backticks point at real tools). None of them asks the only
question that matters to a user: *ask in plain words, do you get a usable
answer back*. That is this gate.

THE DIRECTION MATTERS. Ground truth is taken from CODE — the tool registries,
system-apps/ manifests, the live route table — and the DOC is checked against
it. Deriving the checklist from the doc would make it vacuous: a doc missing a
capability would also be missing the test for it.

WHAT "A USABLE ANSWER" MEANS. Finding a name is not enough. Users run small and
open-source models, so the capability cards need to be detailed and easy to
follow for older, smaller or open-source models. A card only counts as
usable when it carries enough for such a model to ACT: what it is, when to
reach for it, and how to call it.

Run against a tree:  APP_ROOT=<repo> python3 scripts/test_capability_answers.py
Run in-image:        docker exec <c> python3 /app/scripts/test_capability_answers.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
DOCS = Path(os.environ.get("CSERIES_DOCS", str(APP_ROOT / "docs")))
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "brain"))

failures: list[str] = []
notes: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# ── load the corpus ───────────────────────────────────────────────────
try:
    caps = json.loads((DOCS / "capabilities.json").read_text())
except Exception as e:  # noqa: BLE001
    print(f"FAIL: cannot read capabilities.json: {e}")
    sys.exit(1)

cards = caps.get("capability_cards") or []
host_ops = {o.get("name") for o in (caps.get("host_ops") or [])}
# Wrapped like check_host_parity.py's read: a bare .read_text() on a missing or
# half-generated doc exits with a traceback, which reads as "this gate is
# broken" rather than "the doc is absent" — and costs a diagnosis cycle before
# anyone looks at the real cause.
try:
    doc_md = (DOCS / "CAPABILITIES.md").read_text().lower()
except Exception as e:  # noqa: BLE001
    print(f"FAIL: cannot read {DOCS / 'CAPABILITIES.md'}: {e}")
    print("The capability doc is generated at image build — a missing one means "
          "the build half-ran, not that this gate misfired.")
    sys.exit(1)

if not cards:
    print("FAIL: capability_cards is empty — the corpus a small model reads does not exist")
    sys.exit(1)


def card_text(c: dict) -> str:
    """Everything a matcher could reasonably index on, lowercased."""
    parts = [
        str(c.get("id") or c.get("name") or ""),
        str(c.get("title") or ""),
        str(c.get("what") or c.get("purpose") or ""),
        " ".join(c.get("when") or []),
        " ".join(c.get("keywords") or []),
        str(c.get("how") or ""),
        str(c.get("category") or ""),
    ]
    return " ".join(parts).lower()


INDEX = [(c, card_text(c)) for c in cards]

# MATCH THE WAY THE SHIPPING TOOL MATCHES, or this gate grades a function no
# user ever reaches. The first version of this file used a flat substring over
# the concatenated card text; the real reader (`_caps_score_card`,
# brain/mastercode/runtime/tools.py) SCORES and applies a floor. The two
# disagree in the dangerous direction — measured during this build: an area
# card whose `when` list held the bare path segment "launch" scored 78 for
# "launch a satellite", a confident false capability claim, while the
# substring test passed it because that literal sentence appears in no card.
# Import the real scorer and its real floors; fall back only if the import
# fails, and say so loudly rather than silently grading against a weaker rule.
# Import the TOKENIZER too, not just the scorer. Getting this half wrong is
# how the first two versions of this gate gave false assurance: a home-rolled
# `re.split` has no stemming and no len>2 filter, so "order pizzas" looked
# clean here while the runtime stemmed it to a hit. The gate must be the
# shipping matcher end to end or it is measuring nothing.
_SCORER = None
_TOKENS = None
_MIN_SCORE = 25
_WEAK_SCORE = 50
try:
    from mastercode.runtime.tools import (  # noqa: E402
        _caps_score_card as _SCORER,
        _caps_tokens as _TOKENS,
        _CAPS_MIN_SCORE as _MIN_SCORE,
        _CAPS_WEAK_SCORE as _WEAK_SCORE,
    )
except Exception as _e:  # noqa: BLE001
    notes.append(f"REAL SCORER UNAVAILABLE ({type(_e).__name__}: {_e}) — fell back to "
                 "substring matching. This gate is WEAKER than the shipping "
                 "matcher; treat a pass as provisional.")


def find(phrase: str, floor: int | None = None) -> list[dict]:
    """Cards the SHIPPING matcher returns for this phrase, best first.

    `floor` defaults to the runtime's MIN. Pass _WEAK_SCORE to ask the
    stricter question "would the host present these as an ANSWER" rather than
    "did anything register at all".
    """
    p = phrase.lower().strip()
    if _SCORER is None or _TOKENS is None:
        return [c for c, t in INDEX if p in t]
    cut = _MIN_SCORE if floor is None else floor
    toks = _TOKENS(p)
    scored = []
    for c, _t in INDEX:
        try:
            score, _why = _SCORER(c, p, toks)
        except Exception:  # noqa: BLE001
            continue
        if score >= cut:
            scored.append((score, c, _why))
    scored.sort(key=lambda x: -x[0])
    return [c for _s, c, _w in scored]


def top_hit(phrase: str):
    """(score, card, reason) of the best match, or None. For adversarial
    checks that need to see HOW strong a wrong answer would be."""
    p = phrase.lower().strip()
    if _SCORER is None or _TOKENS is None:
        return None
    toks = _TOKENS(p)
    best = None
    for c, _t in INDEX:
        try:
            score, why = _SCORER(c, p, toks)
        except Exception:  # noqa: BLE001
            continue
        if best is None or score > best[0]:
            best = (score, c, why)
    return best


# ── 1. USER VOCABULARY — the measured false denials ───────────────────
# Each phrase is one a real user would type, paired with proof the capability
# ships. A miss here means the host tells the user "not supported today" about
# something the platform does.
print("\n1. user vocabulary — plain words must reach a capability")
VOCAB = [
    ("voice",       "server/routes/realtime.py, router mounted server/main.py"),
    ("screenshot",  "POST /api/screen/capture, scope read:screen"),
    ("email",       "the inbox lane (inbox_read / inbox routes)"),
    ("password",    "the vault's browser_type broker types credentials"),
    ("browser",     "system-apps/browser — live CDP viewport"),
    ("scan",        "system-apps/security — vulnerability scanning"),
    ("calendar",    "calendar_read + /api/calendar"),
    ("backup",      "/api/backup/*"),
    ("publish",     "publish_site / publish_app"),
    ("remember",    "the memory lane"),
    # The HTML desktop: the desktop surface as a host-authored
    # artifact — `artifact` op='set_desktop'.
    ("desktop theme",    "artifact op='set_desktop'"),
    ("change my desktop", "artifact op='set_desktop'"),
    ("wallpaper",        "Settings → Appearance swatches + the HTML desktop"),
]
for phrase, proof in VOCAB:
    hits = find(phrase)
    # Distinguish "no card but the prose mentions it" from "invisible
    # everywhere" — the first is a corpus gap a small model still trips on
    # (prose is not actionable), the second is a total denial. Both fail; the
    # message tells the fixer which.
    in_prose = phrase.lower() in doc_md
    if hits:
        detail = f"{len(hits)} card(s)"
    elif in_prose:
        detail = f"NO CARD (prose mentions it, but prose is not actionable) — ships as: {proof}"
    else:
        detail = f"INVISIBLE — no card, no prose. It ships as: {proof}"
    check(f"'{phrase}' finds a capability", bool(hits), detail)

# ── 2. COMPLETENESS — every host op the doc claims must have a card ───
print("\n2. completeness — every documented host op has a card")
card_ids = {str(c.get("id") or c.get("name") or "") for c in cards}
missing_cards = sorted(op for op in host_ops if op and op not in card_ids)
check("every host_op has a matching card", not missing_cards,
      f"missing: {missing_cards[:10]}" if missing_cards else f"{len(host_ops)} ops")

# The five permanent orchestration verbs: advertised on every turn
# (brain/mastercode/runtime/session.py builds them into the host's tool list),
# so a host that cannot find them cannot describe its own core behaviour.
print("\n3. the host's permanent orchestration verbs are documented")
for verb in ("dispatch", "operate", "recall", "agent_status", "escalate"):
    in_ops = verb in host_ops
    in_cards = bool(find(verb))
    check(f"'{verb}' is in host_ops and findable", in_ops and in_cards,
          f"host_ops={in_ops} card={in_cards}")

# ── 4. APPS — every shipped app must be answerable ────────────────────
# Ground truth from the tree, not the doc.
print("\n4. every shipped app is answerable")
apps_dir = APP_ROOT / "system-apps"
if apps_dir.is_dir():
    import yaml  # server deps guarantee pyyaml

    app_ids = []
    for y in sorted(apps_dir.glob("*/app.yaml")):
        try:
            raw = yaml.safe_load(y.read_text()) or {}
        except Exception:  # noqa: BLE001
            continue
        aid = str(raw.get("id") or "")
        if aid and aid.upper() != "TEMPLATE":
            app_ids.append(aid)
    unanswerable = [a for a in app_ids if not find(a) and a not in doc_md]
    check("every system app appears in the corpus", not unanswerable,
          f"missing: {unanswerable}" if unanswerable else f"{len(app_ids)} apps")
else:
    notes.append("system-apps/ not present (in-image run) — app coverage skipped")

# ── 5. USABLE BY A SMALL MODEL ────────────────────────────────────────
# A name alone is not an answer. A small model needs to know when to reach for
# a capability and how to call it, or it will find the card and still guess.
print("\n5. cards are actionable, not just named")
def has(c: dict, *fields: str) -> bool:
    return any(str(c.get(f) or "").strip() or (c.get(f) if isinstance(c.get(f), list) else None)
               for f in fields)

no_what = [c for c in cards if not has(c, "what", "purpose")]
no_when = [c for c in cards if not (c.get("when") or c.get("keywords"))]
no_how = [c for c in cards if not has(c, "how", "parameters")]
check("every card says WHAT it does", not no_what,
      f"{len(no_what)} without" if no_what else f"{len(cards)} cards")
check("every card says WHEN to use it", not no_when,
      f"{len(no_when)} without" if no_when else "")
check("every card says HOW to call it", not no_how,
      f"{len(no_how)} without" if no_how else "")

# ── 6. ADVERSARIAL: no false positives ────────────────────────────────
# A corpus that matches everything is as useless as one that matches nothing —
# it would make the host claim capabilities that do not exist. These are
# deliberately absent from the platform.
print("\n6. adversarial — a confident WRONG answer is the worst outcome")
# The bar is not "scored zero" — it is "the host would not present this as an
# ANSWER". The runtime calls a hit an answer at >= _CAPS_WEAK_SCORE and merely
# a candidate below it, so that is the line this checks. Every phrase below
# was MEASURED producing a confident wrong hit when the corpus was built:
# bare path segments and single-word aliases were matching by containment
# ("launch" from /applications/{id}/launch scored 78 for "launch a satellite";
# "tickets" from /tickets/_/patterns scored 78 for "buy tickets to a concert").
NONSENSE = [
    "launch a satellite",
    "order a pizza",
    "mine cryptocurrency",
    "drive my car",
    "buy tickets to a concert",       # support tickets != event tickets
    "restore my antique chair",       # backup 'restore'
    "write a speech for my wedding",  # realtime 'speech'
    "delete my account",              # token-coverage on stopwords
    "send a text message",            # SMS: deliberately not built
    "buy a new desktop computer",     # HTML-desktop aliases must not catch hardware
    "does my desktop look ok",        # …or an opinion about the current one
]
for phrase in NONSENSE:
    top = top_hit(phrase)
    confident = find(phrase, floor=_WEAK_SCORE)
    ok = not confident
    detail = ""
    if not ok and top:
        detail = (f"CONFIDENT WRONG ANSWER: {top[1].get('id')} "
                  f"score={top[0]} via {top[2]}")
    elif top:
        detail = f"best={top[0]} ({top[1].get('id')}) — below the answer floor, fine"
    check(f"'{phrase}' produces no confident answer", ok, detail)

# Mis-routing: a REAL capability phrased naturally must reach the RIGHT card,
# not a same-word neighbour. "snapshot" collided with backup while the user
# plainly meant the screen.
print("\n6b. real capabilities must route to the RIGHT card")
ROUTING = [
    ("take a snapshot of my screen", "screen"),
    ("let me talk to you out loud", "realtime"),
]
for phrase, want in ROUTING:
    top = top_hit(phrase)
    got = str(top[1].get("id", "")) if top else ""
    check(f"'{phrase}' routes to a {want} card", want in got.lower(),
          f"routed to {got!r} (score {top[0] if top else 0})")

# ── 7. the closed-world claim must not overreach ──────────────────────
# §3 renders one of four schema registries. If it still claims to be every
# operation a host can perform, the SOP turns that overclaim into a denial.
print("\n7. the doc does not overclaim its own completeness")
overclaims = [
    "every operation a host can perform",
    "lists everything cseries supports",
]
found_over = [s for s in overclaims if s in doc_md]
check("no unqualified 'this is everything' claim", not found_over,
      f"found: {found_over}" if found_over else "")

# ── verdict ───────────────────────────────────────────────────────────
print()
for n in notes:
    print(f"  note: {n}")
if failures:
    print(f"\nFAIL — {len(failures)} check(s):")
    for f in failures:
        print(f"  - {f}")
    print("\nA host consulting this corpus would give a wrong answer to a real user.")
    sys.exit(1)
print(f"\nPASS — the capability corpus answers for {len(cards)} capabilities, "
      f"in the words users actually use.")
