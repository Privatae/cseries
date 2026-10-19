#!/usr/bin/env python3
"""Gate: the Simple-mode recommendations file stays applicable and honest.

system-apps/orchestrator/recommended-routes.json is the DATA half of Simple
mode: one click applies a researched {provider, model, effort?} per
text role, written through the ordinary routing PUT — never a hidden pin. The
resolver does not read the file, so the only way it can rot is silently: a
provider the registry dropped, a role that doesn't exist, an effort the write
surface refuses, a Claude id on a non-anthropic card (which normalize_provider
would coerce onto a DIFFERENT key than the one the user clicked). Each of
those turns the one-click into a 400 or a lie — this gate fails first.

LIMIT, stated rather than implied: candidate MODEL ids are not checked here —
they are checked live at apply time against the provider's own catalog under
the user's key (that is the design: vendor lineups move, candidates are
ordered, unlisted ones are skipped). What this gate pins is everything the
platform itself is authoritative for.

Static: reads the JSON + the registry/validators it must agree with.
Run: APP_ROOT=<repo> python3 scripts/test_recommended_routes.py
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


DATA_PATH = _ROOT / "system-apps" / "orchestrator" / "recommended-routes.json"
data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

from server.routes.inference import ROLES, validate_role_effort, text_role_efforts  # noqa: E402
from server.vault.providers import PROVIDERS  # noqa: E402
from mastercode.inference_config import _is_claude_model, anthropic_api_model_id  # noqa: E402

# image_gen is a ROLE, but it is picked from a provider's image-capable
# catalog, not from a researched chat plan, so it is excluded here exactly like
# embedding and image — otherwise this gate would demand a one-click TEXT
# recommendation for an image model.
TEXT_ROLES = [r for r in ROLES if r not in ("embedding", "image", "image_gen")]
check("no vision role and no vision recommendation entry",
      "vision" not in ROLES and "vision" not in (data.get("image_roles") or {}))
recs = data.get("providers") or {}
no_recs = data.get("no_recommendations") or {}

# ── 1. every named provider is real, and the registry is fully placed ──────
check("every recommended provider is in the Keys registry",
      all(p in PROVIDERS for p in recs), f"unknown: {sorted(set(recs) - set(PROVIDERS))}")
check("'local' never carries recommendations (we can't rank unseen models)",
      "local" not in recs)
covered = set(recs) | set(no_recs)
missing = (set(PROVIDERS) | {"local"}) - covered
check("every registry provider (+ local) is either recommended or explicitly "
      "not — a new provider must be placed, not forgotten",
      not missing, f"unplaced: {sorted(missing)}")
check("no provider is in both maps",
      not (set(recs) & set(no_recs)), f"both: {sorted(set(recs) & set(no_recs))}")

# ── 2. roles: complete coverage, nothing invented ──────────────────────────
for prov, spec in recs.items():
    roles = spec.get("roles") or {}
    check(f"{prov}: covers every text role — a partial plan leaves silent gaps",
          set(roles) == set(TEXT_ROLES),
          f"missing={sorted(set(TEXT_ROLES) - set(roles))} extra={sorted(set(roles) - set(TEXT_ROLES))}")

# ── 3. candidates: ordered, non-empty, no duplicates ───────────────────────
for prov, spec in recs.items():
    for role, entry in (spec.get("roles") or {}).items():
        cands = entry.get("candidates")
        ok = (isinstance(cands, list) and cands
              and all(isinstance(c, str) and c.strip() for c in cands)
              and len(set(cands)) == len(cands))
        check(f"{prov}.{role}: candidates are a non-empty, duplicate-free list",
              ok, str(cands))

# ── 4. efforts: only what the write surface accepts ────────────────────────
for prov, spec in recs.items():
    offers = text_role_efforts(prov)
    for role, entry in (spec.get("roles") or {}).items():
        eff = entry.get("effort")
        if eff is None:
            continue
        check(f"{prov}.{role}: effort {eff!r} would survive the routing PUT",
              validate_role_effort(prov, eff) is None,
              f"provider offers {offers or 'no effort control'}")

# ── 5. Claude ids only on the anthropic card ───────────────────────────────
# normalize_provider coerces a Claude-shaped model on openrouter to provider
# 'anthropic' (Claude never transits OpenRouter) — so a Claude candidate on any
# other card would silently route to a key the user did not click.
for prov, spec in recs.items():
    if prov == "anthropic":
        continue
    offenders = [c for role in (spec.get("roles") or {}).values()
                 for c in (role.get("candidates") or []) if _is_claude_model(c)]
    check(f"{prov}: no Claude-shaped candidates (they'd coerce onto the "
          "anthropic key)", not offenders, str(offenders))

# anthropic's own candidates must already be API-native ids — an alias would
# store fine but read back different from what the plan preview showed.
_anth = [c for role in (recs.get("anthropic", {}).get("roles") or {}).values()
         for c in (role.get("candidates") or [])]
check("anthropic candidates are API-native ids (normalize to themselves)",
      all(anthropic_api_model_id(c) == c for c in _anth),
      str([c for c in _anth if anthropic_api_model_id(c) != c]))

# ── 6. the consumers actually consume it ───────────────────────────────────
PICKER = (_ROOT / "dashboard" / "base" / "js" / "model-picker.js").read_text()
APPJS = (_ROOT / "system-apps" / "orchestrator" / "app.js").read_text()
check("the app fetches the data and passes it to the picker",
      "recommended-routes.json" in APPJS and "recommendations" in APPJS)
check("the picker gates efforts against the server's role_efforts, so a "
      "data-file effort the provider dropped is never sent",
      "efforts.indexOf(entry.effort) >= 0" in PICKER)
check("apply writes through the ordinary routing PUT (no side channel)",
      "_applyPlan" in PICKER and PICKER.count("'/api/inference/routing'") >= 2)
check("apply is sequential — parallel PUTs would race the behavioral_params "
      "read-modify-write and lose routes",
      "for (const row of rows)" in PICKER and "await fetchDetail" in PICKER)

# The local-first flip vs efforts: every openrouter recommendation carries an
# effort, and the flip moves the route to 'local', whose dial path takes none.
# The write must DROP the effort (annotated), never 400 the exact pick
# local-first exists to reward — and the one-click must show the drop.
INF = (_ROOT / "server" / "routes" / "inference.py").read_text()
check("the local-first flip drops a requested effort instead of refusing "
      "the write, and annotates it",
      "if effort and defaulted_local:" in INF
      and 'out["effort_dropped"] = True' in INF)
check("the one-click surfaces the dropped effort on the plan row",
      "effort_dropped" in PICKER and "effDropped" in PICKER)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
