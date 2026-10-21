"""
runtime/model_standard.py — the HOST MODEL STANDARD: which models may hold a
Host text role, judged per MODEL whatever provider serves it.

A model is APPROVED when all three tests pass:

  1. published quality floor  — Artificial Analysis Intelligence Index,
                                pinned version, at the effort the Host runs it
  2. original weights + card  — the vendor's own release with a published
                                system card that includes safety evaluations
  3. the platform's benches   — the judge-free admission bench
                                (runtime/model_validation, validated_models.json)
                                and the in-house safety bench. ON HOLD until the
                                Host system prompt is validated.

Tests 1 and 2 are the data in approved_models.json beside this module (a
model's `status` there is 'eligible' only when both hold). Test 3 stays where
it already lives, keyed per (provider, model) — "a model is the weights it
runs on", so a local serving is benched on its own. This module only COMBINES
them; it adds no second list for test 3.

THE IDENTITY PROBLEM THIS SOLVES. Until now nothing mapped one model across
providers: `grok-4.6` on xai and `x-ai/grok-4.6` on openrouter were unrelated
strings, so a per-model standard could not be enforced independently of the
provider. canonical_model() is that mapping. It resolves only what it can
prove: a first-party provider's own id by rule, an OpenRouter id whose vendor
prefix is known by rule, and everything else only through the reviewed alias
table. A spelling it cannot resolve — a bare local tag, a quantisation, a
fine-tune, an abliterated build riding under a base model's name — has NO
canonical id and is therefore never approved. Guessing identity is exactly
the hole the standard exists to close.

WHAT IS ENFORCED, AND WHERE. `enforce` in the data file (false as shipped).
When true, refusal() returns a sentence and the two callers refuse:
server/routes/inference.py apply_role_route (a route cannot be SAVED) and
provider_gateway.call_model (a request cannot be SENT, primary or fallback).
The admission bench's own calls (_source 'model_bench') are exempt — the bench
has to be able to test a model that is not approved yet. While false, the
verdict is reported (catalog rows, routing status) and nothing is withheld.
Do not flip it while test 3 is on hold: nothing is validated yet, so every
route would be refused.

EFFORT. Scores move a lot with reasoning effort (Sonnet 5: 38.2 at max, 24.3
at low), so each entry carries the lowest AA-measured effort that clears the
floor. The effort judged is effective_effort(): on a provider whose calls
carry an effort (EFFORT_PROVIDERS, = TEXT_ROLE_EFFORTS), the stored effort or
the "low" the platform sends when none is stored (on Anthropic, which sends
nothing then, None); on every other provider, None — the vendor's own
default, which the platform cannot see or set, is not judged. Effort is
judged where a route is SAVED; the gateway judges
identity, eligibility and the benches but not effort (refusal()'s
judge_effort), so a route the writer accepts is never refused on every
message afterwards. Known gaps, stated: a model eligible only at high effort
(Sonnet 5, Grok 4.7, Opus 4.8, Sonnet 4.6) runs unjudged at its vendor's
default on a direct provider, and a caller that sends its own fixed effort
instead of the route's runs unjudged at that effort.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("cseries.model_standard")

DATA_FILE = "brain/mastercode/runtime/approved_models.json"

# First-party providers resolve by rule: the provider IS the vendor, and its
# own model id is the model. kimi is Moonshot's API.
_FIRST_PARTY: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "xai": "xai",
    "google": "google",
    "kimi": "moonshot",
}

# OpenRouter vendor prefixes that name a first-party vendor. Any other prefix
# (an open-weight lab, a fine-tuner) resolves only through the alias table.
_OPENROUTER_VENDOR: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "x-ai": "xai",
    "google": "google",
    "moonshotai": "moonshot",
}

# OpenRouter routing variants of the SAME weights (batch pricing, throughput
# or price sorting, web plugin). Any other ':suffix' is an unknown variant and
# does not resolve.
_ROUTING_SUFFIXES = (":batch", ":nitro", ":floor", ":online", ":free")

# Effort ranking across the vocabularies in use (AA labels and
# TEXT_ROLE_EFFORTS). Higher is more reasoning.
_EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4,
                "xhigh": 5, "max": 6}

# Providers whose text-role calls actually carry an effort the platform sets.
# MUST equal the keys of server/routes/inference.py TEXT_ROLE_EFFORTS
# (tests/test_model_standard.py asserts it): brain/ cannot import the server
# routes module. For every other provider the effort a model runs at is the
# vendor's own default, which the platform cannot see.
EFFORT_PROVIDERS = frozenset({"openrouter", "kimi", "anthropic"})

# What the platform sends when a route stores no effort: route_effort()'s
# fallback (inference_config.py) and the chat turn's (runtime/session.py).
DEFAULT_SENT_EFFORT = "low"

# Effort providers that send NOTHING when a route stores no effort: the
# Anthropic wire sends only the person's own pick (inference_config.
# chosen_effort), so an unset route runs at the model's own default, which is
# not judged here, as before that wire carried an effort at all.
UNSET_SENDS_NOTHING = frozenset({"anthropic"})


def effective_effort(provider: str | None, effort: str | None) -> str | None:
    """The effort this route really runs at, as far as the platform can know
    it — the SAME answer at write time and at send time, so a route the
    writer accepts is never refused on every message afterwards (review of
    the first cut: the writer judged an absent effort as unknown while the
    gateway judged the "low" the callers fill in).

    effort-capable provider: the stored effort, else the default sent
                             (None on a provider that sends none unset).
    any other provider:      None — not judged; nothing the user can set
                             would change it."""
    p = (provider or "").strip().lower()
    if p not in EFFORT_PROVIDERS:
        return None
    eff = (effort or "").strip().lower()
    if eff:
        return eff
    return None if p in UNSET_SENDS_NOTHING else DEFAULT_SENT_EFFORT


_lock = threading.Lock()
_cache: dict = {"stamp": None, "doc": None}

_EMPTY = {"enforce": False, "models": [], "aliases": {}}


def _path() -> Path:
    return Path(__file__).resolve().parent / "approved_models.json"


def load(force: bool = False) -> dict:
    """The parsed data file, cached on (mtime, size, inode). Missing or
    unreadable = empty with enforce off and a logged warning: the standard
    fails OPEN on its own data, like the validation gate — a bad file must
    never refuse every Host's model."""
    p = _path()
    with _lock:
        try:
            st = p.stat()
        except OSError:
            doc = {**_EMPTY, "_error": "file absent"}
            _cache.update({"stamp": None, "doc": doc})
            return doc
        stamp = (st.st_mtime, st.st_size, getattr(st, "st_ino", 0))
        if not force and _cache["stamp"] == stamp and _cache["doc"] is not None:
            return _cache["doc"]
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except Exception as e:  # noqa: BLE001
            log.warning("approved_models.json unreadable (%s) — standard off", e)
            doc = {**_EMPTY, "_error": str(e)}
        _cache.update({"stamp": stamp, "doc": doc})
        return doc


def enforced() -> bool:
    # `is True`, not truthiness: a hand-edited "false" string must not turn
    # enforcement on for every Host.
    return load().get("enforce") is True


def canonical_model(provider: str | None, model: str | None) -> str | None:
    """The canonical id (vendor/model, lowercase) of what this route actually
    sends, or None when identity cannot be proven. Applies the same
    normalization the writer and the gateway apply first, so the canonical id
    names the model that reaches the wire — a Claude pick on OpenRouter is
    judged as the Anthropic model it is coerced to."""
    try:
        from mastercode.inference_config import normalize_provider, anthropic_api_model_id
    except Exception:  # noqa: BLE001
        return None
    p, m = normalize_provider(provider or "", model or "")
    p = (p or "").strip().lower()
    m = (m or "").strip().lstrip("~")
    if not (p and m):
        return None
    low = m.lower()
    for suf in _ROUTING_SUFFIXES:
        if low.endswith(suf):
            m, low = m[: -len(suf)], low[: -len(suf)]
            break
    if ":" in low:
        return None

    doc = load()
    listed = {str(e.get("canonical") or "").strip().lower()
              for e in (list(doc.get("models") or []) + list(doc.get("image_models") or []))
              if isinstance(e, dict)}
    aliases = (load().get("aliases") or {}).get(p) or {}

    if p in _FIRST_PARTY:
        vendor = _FIRST_PARTY[p]
        mm = m
        if p == "anthropic":
            # EXACTLY what the anthropic wire sends: one anthropic_api_model_id
            # call on the stored id (provider_gateway._call_anthropic). An
            # earlier cut tried the prefixed spelling first, as
            # model_validation._key does, and so judged a Sonnet 4.6 pick as
            # the Sonnet 5 it is NOT sent as (review, 2026-09-29).
            mm = anthropic_api_model_id(mm)
        mm = mm.strip().lower()
        hit = aliases.get(mm)
        if hit:
            return str(hit).strip().lower()
        for pre in (p + "/", vendor + "/", "models/"):
            if mm.startswith(pre):
                mm = mm[len(pre):]
        if not mm or "/" in mm:
            return None
        return f"{vendor}/{mm}"

    hit = aliases.get(m) or aliases.get(low)
    if hit:
        return str(hit).strip().lower()

    if p == "openrouter" and "/" in low:
        pre, rest = low.split("/", 1)
        vendor = _OPENROUTER_VENDOR.get(pre)
        # A first-party vendor prefix resolves by rule ONLY to a model this
        # file lists. OpenRouter also serves open weights under those prefixes
        # (openai/gpt-oss-*, google/gemma-*) from third-party hosts, and the
        # rule cannot tell them from the vendor's own serving.
        if vendor and rest and "/" not in rest and f"{vendor}/{rest}" in listed:
            return f"{vendor}/{rest}"
    # Generic providers (deepinfra, together, fireworks, groq) and 'local':
    # alias table only. Their ids name repos and quantisations whose weights
    # the platform cannot tell apart from a derivative.
    return None


def entry_for(canonical: str | None) -> dict | None:
    if not canonical:
        return None
    for e in load().get("models") or []:
        if isinstance(e, dict) and str(e.get("canonical") or "").strip().lower() == canonical:
            return e
    return None


def _effort_ok(effort: str | None, min_effort: str | None) -> bool | None:
    eff = (effort or "").strip().lower()
    if not eff:
        return None                     # provider default: not visible, not judged
    if not min_effort:
        return None
    a, b = _EFFORT_RANK.get(eff), _EFFORT_RANK.get(str(min_effort).lower())
    if a is None or b is None:
        return None
    return a >= b


def check(provider: str | None, model: str | None, effort: str | None = None,
          *, judge_effort: bool = True) -> dict:
    """The standard's verdict on one route. Pure apart from reading the two
    data files. Keys:
      canonical   the model's identity, or None when it cannot be proven
      status      the entry's status ('eligible', 'pending_card', 'no_card',
                  'below_floor') or 'unlisted' / 'unidentified'
      eligible    tests 1 and 2 hold
      effort_ok   True/False against min_effort; None when no effort is stored
      validated   test 3 (the bench list, per provider+model)
      approved    eligible and validated and effort_ok is not False
      reason      one plain sentence when not approved
    """
    canonical = canonical_model(provider, model)
    e = entry_for(canonical)
    if canonical is None:
        status = "unidentified"
    elif e is None:
        status = "unlisted"
    else:
        status = str(e.get("status") or "unlisted")
    eligible = status == "eligible" and bool(e and e.get("card"))
    # Effort is judged on the provider the route is STORED under (a Claude
    # pick on openrouter is stored, and sent, as anthropic), and only where
    # the caller asks for it — see refusal().
    try:
        from mastercode.inference_config import normalize_provider
        stored_provider = normalize_provider(provider or "", model or "")[0] or provider
    except Exception:  # noqa: BLE001
        stored_provider = provider
    effort = effective_effort(stored_provider, effort) if judge_effort else None
    effort_ok = _effort_ok(effort, (e or {}).get("min_effort")) if e else None
    try:
        from mastercode.runtime.model_validation import is_validated
        validated = bool(is_validated(provider or "", model or ""))
    except Exception:  # noqa: BLE001
        validated = False
    approved = eligible and validated and effort_ok is not False

    reason = None
    if not approved:
        if status == "unidentified":
            reason = ("its identity can't be confirmed — a local tag, a fine-tune or an "
                      "unrecognised provider spelling is never treated as the base model")
        elif status == "unlisted":
            reason = f"{canonical} has not been assessed against the standard"
        elif status == "below_floor":
            reason = f"{canonical} scores below the published quality floor"
        elif status == "pending_card":
            reason = f"{canonical} has no confirmed system card with safety evaluations yet"
        elif status == "no_card":
            reason = f"{canonical}'s vendor publishes no safety evaluations"
        elif effort_ok is False:
            reason = (f"{canonical} clears the floor only at effort "
                      f"{e.get('min_effort')} or higher, and this route runs it at {effort}")
        elif not validated:
            reason = f"{canonical} has not passed the platform's own benches yet"
        else:
            reason = f"{canonical} is not approved"
    return {"canonical": canonical, "status": status, "eligible": eligible,
            "effort_ok": effort_ok, "validated": validated, "approved": approved,
            "reason": reason}


def refusal(provider: str | None, model: str | None, effort: str | None = None,
            *, judge_effort: bool = True) -> str | None:
    """None when the route may run; otherwise the sentence a caller refuses
    with. Always None while the standard is not enforced.

    judge_effort: True at the WRITER (apply_role_route), where the effort is
    a stored setting the user can change. False at the GATEWAY: callers of
    one stored route send different efforts — the chat turn and the
    consolidators fill in "low", others (deep research, scoped agents, web
    research, session summaries) send none and the vendor's default applies
    — so judging effort per call refused, on every call, routes the writer
    had accepted (second review, 2026-09-29). Without it the gateway's check
    is strictly weaker than the writer's, so a saved route is never refused
    at send. Known gap, stated: a caller that overrides the stored effort
    with its own runs unjudged at that effort."""
    if not enforced():
        return None
    v = check(provider, model, effort, judge_effort=judge_effort)
    if v["approved"]:
        return None
    return (f"{provider}/{model} is not an approved Host model: {v['reason']}. "
            "Pick an approved model in the Orchestrator.")


def summary(provider: str | None, model: str | None, effort: str | None = None) -> dict:
    """What the catalog and routing status show: the verdict without the
    bench detail. Never raises — a lookup failure reads as unassessed."""
    try:
        v = check(provider, model, effort)
        return {"canonical": v["canonical"], "status": v["status"],
                "eligible": v["eligible"], "approved": v["approved"],
                "enforced": enforced()}
    except Exception:  # noqa: BLE001
        return {"canonical": None, "status": "unidentified", "eligible": False,
                "approved": False, "enforced": False}


# The text roles a route can still be stored and resolved under
# (server/routes/inference.ROLES without the image/embedding ones);
# scripts/test_three_text_roles.py keeps the two lists equal.
LIVE_TEXT_ROLES = ("chat",)


def audit_routes(model_routing: dict | None) -> list[dict]:
    """The WRITER's verdict on every text route a Host already has stored —
    the check to run before `enforce` is flipped. Effort is judged only where
    a route is saved, so a route stored while the standard was off at an
    effort below its model's min_effort (or on an unapproved model) is never
    re-judged by the gateway; this is how it gets found (third review,
    2026-09-29). Returns one row per role that the writer would now refuse:
    {role, provider, model, effort, reason}, the fallback included. Pure: the
    caller supplies behavioral_params.model_routing."""
    out: list[dict] = []
    try:
        from mastercode.inference_config import _BYO_ALLOWED_ROLES
    except Exception:  # noqa: BLE001
        _BYO_ALLOWED_ROLES = frozenset({"embedding", "image", "image_gen"})  # noqa: N806
    for role, r in (model_routing or {}).items():
        # A route stored under a retired role (plan, research, agents, tutor,
        # journal, consolidation, review) is never resolved, so it is never judged.
        if role in _BYO_ALLOWED_ROLES or role not in LIVE_TEXT_ROLES or not isinstance(r, dict):
            continue
        prov, mod, eff = r.get("provider"), r.get("model"), r.get("effort")
        pairs = [(prov, mod, "primary")]
        if r.get("fallback_provider"):
            pairs.append((r.get("fallback_provider"), r.get("fallback_model") or mod, "fallback"))
        for p, m, which in pairs:
            v = check(p, m, eff)
            if not v["approved"]:
                out.append({"role": role, "which": which, "provider": p, "model": m,
                            "effort": eff, "reason": v["reason"]})
    return out


# ── image generation ─────────────────────────────────────────────────────────
# No NSFW or uncensored image models. An image
# model is approved only when the data file lists it 'eligible' under
# image_models: the provider filters sexual content by default, the platform's
# closed request body (image_engine._generate) cannot turn that filter off, and
# the vendor publishes a card with safety results. There is no quality floor
# and no bench for image models; the gate is the listing. A model that reads
# images is the Host model itself (there is no vision role since 2026-10-12),
# so it is held to the TEXT standard like any Host model.

def image_entry_for(canonical: str | None) -> dict | None:
    if not canonical:
        return None
    for e in load().get("image_models") or []:
        if isinstance(e, dict) and str(e.get("canonical") or "").strip().lower() == canonical:
            return e
    return None


def check_image(provider: str | None, model: str | None) -> dict:
    canonical = canonical_model(provider, model)
    e = image_entry_for(canonical)
    if canonical is None:
        status = "unidentified"
    elif e is None:
        status = "unlisted"
    else:
        status = str(e.get("status") or "unlisted")
    approved = status == "eligible" and bool(e and e.get("card"))
    reason = None
    if not approved:
        reason = {
            "unidentified": "its identity can't be confirmed — a local model, a pinned "
                            "endpoint or an unrecognised spelling is never approved",
            "unlisted": f"{canonical} has not been assessed for the image standard",
            "excluded": f"{canonical} is excluded: {(e or {}).get('note') or 'not allowed'}",
            "pending_review": f"{canonical} is still being reviewed: "
                              f"{(e or {}).get('note') or 'filtering unconfirmed'}",
            "pending_card": f"{canonical} has no confirmed card with safety results yet",
        }.get(status, f"{canonical} is not approved")
    return {"canonical": canonical, "status": status, "approved": approved, "reason": reason}


def refusal_image(provider: str | None, model: str | None) -> str | None:
    """None when this image-generation route may run; otherwise the sentence
    to refuse with. Always None while the standard is not enforced."""
    if not enforced():
        return None
    v = check_image(provider, model)
    if v["approved"]:
        return None
    return (f"{provider}/{model} is not an approved image model: {v['reason']}. "
            "Pick an approved image model in the Orchestrator.")


# ── helpers for callers that SUGGEST models ──────────────────────────────────

def approved_candidates(provider: str | None, candidates: list[str]) -> list[str]:
    """The candidates a recommendation may offer: all of them while the
    standard is not enforced; only those the writer would accept while it is.
    (tools.py model_routing op='recommend' — a suggestion must never 409.)"""
    if not enforced():
        return list(candidates)
    return [c for c in candidates if refusal(provider, c) is None]


def pick_allowed(role: str, provider: str | None, model: str | None) -> bool:
    """Whether an image-generation recommendation may be offered: always while
    not enforced; while enforced, it must pass the image allowlist (as the
    writer judges it). `role` is the image role asked about (image_gen); there
    is no vision role to judge since 2026-10-12."""
    if not enforced():
        return True
    return refusal_image(provider, model) is None


def pins_moderation(provider: str | None, model: str | None) -> bool:
    """Whether an image request states moderation="auto": only for an OpenAI
    image model the image standard lists 'eligible' — the configuration the
    approval was given on, and the models OpenAI documents the field for.
    Sent regardless of enforcement, so it is limited to exactly those."""
    if (provider or "").strip().lower() != "openai":
        return False
    e = image_entry_for(canonical_model(provider, model))
    return bool(e and e.get("status") == "eligible")
