"""
runtime/model_validation.py — which text-role models are VALIDATED, and what
an unvalidated one may hold.

"Validated" applies to text-role models, earned only
by passing the admission bench (runtime/model_bench.py); it is a platform
SAFETY gate, not a per-host product choice — the same list for every host,
maintained by the platform from its own bench runs, and every OpenAI id
validated one by one, none grandfathered.

THE LIST IS DATA THE PLATFORM SHIPS, beside this module:
brain/mastercode/runtime/validated_models.json. It lives in brain/ because
brain/ is what the image carries (Dockerfile.linux COPY brain/ /app/brain/);
an app bundle's payload is installed under /var/lib/cseries/apps and is not
on any path code reads (review of 16350e9f, B1 — the routing
recommendations file has the same gap, recorded in smoke_test.sh). Each
entry is a (provider, model) pair with the evidence — the suite version and
hash it passed under, the run id and date, and who ran it. Nothing in the
pod can add to it: a local `model_bench` pass is information for the user,
not a standing (the safety decision is the platform's). Changing the list
is a PR, reviewed like any other change to what ships to everyone.

A MODEL IS THE WEIGHTS IT RUNS ON. An entry for openrouter/X does not
validate local/X: the machine on the user's own network serves its own
build of X (often a different quantisation), which is a different model for
the bench's purposes, and the local-first flip in the writer is a routing
convenience, not an identity. Validate the local serving on its own.

WHAT THE GATE DOES. `chat` is the only role that calls tools since the
three-role cut (2026-09-19) — it runs the host's own turns and every agent
the host dispatches — so it is the only role the gate can take anything
from. When `enforce` is true and the chat model is not on the list, the host
keeps chat and its READ tools while `operate`, `dispatch` and its own build
doors are withheld from the tool list AND refused if named anyway
(runtime/session.py): the unvalidated model can talk and look, not act. A
dispatched agent does not start at all (runtime/scoped_agent.py), because an
agent is nothing but tool use; a dispatch naming its own model is gated on
that model rather than on the route.
When `enforce` is false the gate observes only: the catalog and the routing
status say which models are validated, the host's diagnostics raise a
fault for an unvalidated route, and nothing is withheld. It ships false so
that landing the gate cannot strip a running host's tools before the
platform has benched the models in use; flipping it is one field in the
data file, reviewed.

The bench must exist before the list can be honest, so this file's first
shipped version has an EMPTY list.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("cseries.model_validation")

# Beside this module, so it is in the image with the code that reads it.
DATA_FILE = "brain/mastercode/runtime/validated_models.json"

# The verbs an unvalidated chat model does not get. The host's own build
# doors (lane 'host') are withheld too — see session.py; they are named by
# lane there, not here.
WITHHELD_VERBS = frozenset({"operate", "dispatch"})

_lock = threading.Lock()
_cache: dict = {"path": None, "stamp": None, "doc": None}


def _candidates() -> list[Path]:
    return [Path(__file__).resolve().parent / "validated_models.json"]


def load(force: bool = False) -> dict:
    """The parsed data file, cached on (mtime, size, inode) — a rewrite that
    keeps the mtime (an rsync -t, an image layer) still moves the size or
    the inode. A missing or unreadable file is an EMPTY list with
    enforce=false and a logged warning — the gate fails open on its own
    data, never closed: a bad file must not strip every host's tools."""
    with _lock:
        for p in _candidates():
            try:
                st = p.stat()
            except OSError:
                continue
            stamp = (st.st_mtime, st.st_size, getattr(st, "st_ino", 0))
            if not force and _cache["path"] == str(p) and _cache["stamp"] == stamp:
                return _cache["doc"]
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("not an object")
            except Exception as e:  # noqa: BLE001
                log.warning("validated_models.json unreadable (%s) — treating as empty, enforce off", e)
                doc = {"enforce": False, "validated": [], "suite_version": None, "_error": str(e)}
            _cache.update({"path": str(p), "stamp": stamp, "doc": doc})
            return doc
        doc = {"enforce": False, "validated": [], "suite_version": None, "_error": "file absent"}
        _cache.update({"path": None, "stamp": None, "doc": doc})
        return doc


def enforced() -> bool:
    return bool(load().get("enforce"))


def _norm(v) -> str:
    return str(v or "").strip().lower()


def _key(provider: str, model: str) -> tuple[str, str]:
    """The pair as the writer stores it, made symmetric. normalize_provider
    coerces a Claude-shaped pick on openrouter to provider 'anthropic' with
    the vendor prefix dropped, but returns a direct anthropic pick's model
    string verbatim — so `anthropic` + `anthropic/claude-x` and
    `openrouter` + `anthropic/claude-x` would key differently (review
    of 16350e9f, B6). The vendor prefix that equals the provider is dropped
    here on both sides, so one entry matches every spelling of one model."""
    try:
        from mastercode.inference_config import normalize_provider
        p, m = normalize_provider(provider or "", model or "")
        if not (p and m):
            p, m = provider, model
    except Exception:  # noqa: BLE001
        p, m = provider, model
    p, m = _norm(p), _norm(m)
    if p == "anthropic":
        # EXACTLY what the anthropic wire sends: one anthropic_api_model_id
        # call on the stored id (provider_gateway._call_anthropic), BEFORE the
        # prefix strip. The earlier order tried the `anthropic/`-prefixed
        # spelling first, which the id map rewrites claude-sonnet-4-6 ->
        # claude-sonnet-5 — so a Sonnet 4.6 bench pass was filed under
        # Sonnet 5 and vice versa. That became a gate once the Host model
        # standard read this list (runtime/model_standard; review 2026-09-29).
        # canonical_model() there uses the same rule.
        try:
            from mastercode.inference_config import anthropic_api_model_id
            m = _norm(anthropic_api_model_id(m)) or m
        except Exception:  # noqa: BLE001
            pass
    if p and m.startswith(p + "/"):
        m = m[len(p) + 1:]
    return p, m


def entry_for(provider: str, model: str) -> dict | None:
    """The validated entry for (provider, model), or None. Only entries whose
    suite_version is the CURRENT bench suite count — a pass under an older
    matrix is history."""
    doc = load()
    try:
        from mastercode.runtime.model_bench import SUITE_VERSION
    except Exception:  # noqa: BLE001
        SUITE_VERSION = None  # noqa: N806
    want = _key(provider, model)
    for e in doc.get("validated") or []:
        if not isinstance(e, dict):
            continue
        if _key(e.get("provider"), e.get("model")) != want:
            continue
        if SUITE_VERSION is not None and str(e.get("suite_version")) != str(SUITE_VERSION):
            continue
        return e
    return None


def is_validated(provider: str, model: str) -> bool:
    return entry_for(provider, model) is not None


def route_validated(provider: str | None, model: str | None,
                    fallback_provider: str | None = None,
                    fallback_model: str | None = None) -> tuple[bool, tuple[str, str] | None]:
    """A ROUTE is validated only if every model that can answer on it is:
    the primary, and the failover pair when one is set — the gateway
    re-dials the fallback with the same tools when the primary errors, so
    an unvalidated fallback would hold everything the gate withheld (review
    review of 16350e9f, B4). Returns (validated, offending_pair)."""
    if not (provider and model):
        return False, (str(provider or ""), str(model or ""))
    if not is_validated(provider, model):
        return False, (str(provider), str(model))
    if fallback_provider:
        fm = fallback_model or model
        if not is_validated(fallback_provider, fm):
            return False, (str(fallback_provider), str(fm))
    return True, None


def status_for(provider: str | None, model: str | None) -> dict:
    """What the routing status and the catalog say about a pair."""
    if not (provider and model):
        return {"validated": False, "enforced": enforced(), "entry": None}
    e = entry_for(provider, model)
    return {"validated": e is not None, "enforced": enforced(), "entry": e}


def withhold_tools(tools: list[dict], *, validated: bool, host_lane_names: set[str] | None = None,
                   enforce: bool | None = None) -> tuple[list[dict], set[str]]:
    """Apply the gate to a tool list: when enforced and the model is not
    validated, drop the withheld verbs and the host's own build doors.
    Returns (tools, withheld_names). Pure; the callers supply the facts."""
    if enforce is None:
        enforce = enforced()
    if not enforce or validated:
        return list(tools), set()
    doors = set(host_lane_names or ())
    withheld = set()
    kept = []
    for s in tools:
        name = ((s.get("function") or {}).get("name")) if isinstance(s, dict) else None
        if name in WITHHELD_VERBS or name in doors:
            withheld.add(name)
            continue
        kept.append(s)
    return kept, withheld


def refusal(name: str, provider: str | None, model: str | None) -> dict:
    """The tool result an unvalidated chat model gets for naming a withheld
    verb anyway. Stamped for the ledger (not_advertised: a real tool the
    surface did not offer)."""
    return {
        "status": "denied", "reason": "not_advertised",
        "result": (f"`{name}` is not available on this route: {provider} / {model} is not a "
                   "validated model for tool use, so this host can talk and read but not act. "
                   "Say so plainly. The user can route the role to a validated model in the "
                   "Orchestrator; models are validated for tool use with the admission bench."),
    }
