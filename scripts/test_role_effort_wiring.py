#!/usr/bin/env python3
"""Gate: a TEXT ROLE offers an effort control only where the gateway sends it.

The text-role mirror of scripts/test_harness_effort_wiring.py, and it exists for
the same reason that one does. Text roles had no effort levels, yet reasoning=low and
reasoning=max for kimi are two different models; the Orchestrator needs them
to find the sweet spot for host reasoning.

The trap here is sharper than on the coding side, because the gateway does NOT
treat providers alike:

  * provider_gateway._dial_wire passes reasoning_effort to _call_remote, whose
    unset value is route_effort's explicit "low".
  * It passes the PERSON's pick alone (call_model's chosen_effort) to
    _call_anthropic as `effort`, never reasoning_effort: an unset Anthropic
    route sends nothing and runs at the model's own default. It once passed
    nothing at all, and anthropic was listed here as
    unsendable; the doctrine moved with the wire, deliberately.
  * _call_local takes no effort parameter at all.

So listing local in TEXT_ROLE_EFFORTS would offer a setting the user can save,
the log can print, and the provider never sees. That is the exact failure the
harness gate was written to stop, one layer down. Anthropic's levels are per
MODEL; scripts/test_anthropic_effort.py pins that rule end to end.

LIMIT, stated rather than implied: this proves the effort reaches the provider
CALL and is put on the payload — not that the vendor honours it. Proving that
needs a live metered call. What this catches is the real regression: someone
adds a provider to TEXT_ROLE_EFFORTS whose dial path drops the value, or wires a
knob the turn path never reads.

Static: parses provider_gateway.py + session.py, reads inference.TEXT_ROLE_EFFORTS.
Run: APP_ROOT=<repo> python3 scripts/test_role_effort_wiring.py
"""
import ast
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


GATEWAY = (_ROOT / "brain" / "mastercode" / "provider_gateway.py").read_text()
SESSION = turn_source(_ROOT)
CONFIG = (_ROOT / "brain" / "mastercode" / "inference_config.py").read_text()
gw_tree = ast.parse(GATEWAY)
GW_FUNCS = {n.name: n for n in ast.walk(gw_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _takes_effort(fn_name: str, param: str = "reasoning_effort") -> bool:
    fn = GW_FUNCS.get(fn_name)
    if fn is None:
        return False
    args = fn.args
    names = [a.arg for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)]
    return param in names


def _anthropic_dial_effort() -> str:
    """The source of the `effort` keyword _dial_wire hands _call_anthropic."""
    fn = GW_FUNCS.get("_dial_wire")
    for node in ast.walk(fn) if fn else ():
        if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_call_anthropic"):
            for k in node.keywords:
                if k.arg == "effort":
                    return ast.unparse(k.value)
    return ""


# ── 1. which dial paths can carry an effort at all ──────────────────────────
check("_call_remote accepts reasoning_effort", _takes_effort("_call_remote"))
check("_call_remote puts the effort on the request payload",
      'payload["reasoning"] = {"effort": reasoning_effort}' in GATEWAY)
check("_call_anthropic takes the person's pick as `effort`",
      _takes_effort("_call_anthropic", "effort"))
check("...which _dial_wire feeds from chosen_effort, never reasoning_effort (whose "
      "unset value is the OpenAI wire's 'low')",
      _anthropic_dial_effort() == "kwargs.get('chosen_effort')", _anthropic_dial_effort())
check("_call_anthropic puts it on the request as output_config.effort",
      '"effort": _effort["sent"]' in GATEWAY and 'payload["output_config"]' in GATEWAY)
check("_call_local takes none", not _takes_effort("_call_local")
      and not _takes_effort("_call_local", "effort"))

# ── 2. the doctrine: only sendable providers are offered ───────────────────
from server.routes.inference import (  # noqa: E402
    HARNESS_EFFORTS, TEXT_ROLE_EFFORTS, text_role_efforts, validate_role_effort,
)

# Providers whose dial path cannot carry an effort. _dial_wire routes
# p=="anthropic" to _call_anthropic (which takes the person's pick) and
# everything else to _call_remote; local has its own, and it
# takes none.
UNSENDABLE = {"local"}
offered = set(TEXT_ROLE_EFFORTS)
check("no provider is offered an effort its dial path would drop",
      not (offered & UNSENDABLE),
      f"offered-but-unsendable: {sorted(offered & UNSENDABLE)}")
check("every offered provider has a non-empty effort set",
      all(TEXT_ROLE_EFFORTS[p] for p in offered), str(TEXT_ROLE_EFFORTS))
check("anthropic's set is the Messages API's effort enum, narrowed per model on write",
      TEXT_ROLE_EFFORTS.get("anthropic") == ("low", "medium", "high", "xhigh", "max"),
      str(TEXT_ROLE_EFFORTS.get("anthropic")))

# ── 3. kimi's set is the vendor's, not a second opinion ────────────────────
check("kimi's text-role set IS its harness set (one source, cannot drift)",
      TEXT_ROLE_EFFORTS.get("kimi") == HARNESS_EFFORTS.get("kimi"),
      f'{TEXT_ROLE_EFFORTS.get("kimi")} vs {HARNESS_EFFORTS.get("kimi")}')
check("...and it has no 'medium' — the reason an invented ladder was wrong",
      "medium" not in (TEXT_ROLE_EFFORTS.get("kimi") or ()),
      str(TEXT_ROLE_EFFORTS.get("kimi")))

# ── 4. 'none' is withheld, as on the coding side ───────────────────────────
for prov, efforts in TEXT_ROLE_EFFORTS.items():
    check(f"{prov}: 'none' is withheld — that is a model choice, not an effort",
          "none" not in efforts, str(efforts))

# ── 5. the validator refuses what cannot be sent ───────────────────────────
check("an effort on an unsendable provider is REFUSED",
      validate_role_effort("local", "high") is not None)
check("an Anthropic effort with no model is REFUSED (its levels are per model)",
      validate_role_effort("anthropic", "high") is not None)
check("an unknown effort on a valid provider is REFUSED",
      validate_role_effort("openrouter", "turbo") is not None)
check("a valid pair is accepted",
      validate_role_effort("openrouter", "xhigh") is None)
check("kimi's 'max' is accepted", validate_role_effort("kimi", "max") is None)
check("kimi's absent 'medium' is REFUSED",
      validate_role_effort("kimi", "medium") is not None)
check("an EMPTY effort is always allowed — it means the provider's default",
      validate_role_effort("anthropic", "") is None
      and validate_role_effort("openrouter", None) is None)
check("text_role_efforts is () for anything unlisted",
      text_role_efforts("openai") == () and text_role_efforts("") == ())

# ── 6. the value actually reaches the turn ─────────────────────────────────
check("the route shape carries an effort field",
      '"effort": effort or None' in CONFIG)
check("resolve_role_route reads the STORED effort",
      'effort=(routing.get("effort") or "").strip() or None' in CONFIG)
check("the chat turn reads the route's effort, not a literal",
      '_reasoning_effort = (_chat_route.get("effort") or "").strip() or "low"'
      in SESSION)
check("red/green — the hardcoded assignment is gone",
      '_reasoning_effort = "low"\n' not in SESSION)
check("an unset role still sends an EXPLICIT effort (the turn-5347 guard)",
      'or "low"' in SESSION)

# ── 7. the UI cannot silently clear a set effort ───────────────────────────
# PUT /api/inference/routing REPLACES the whole entry, so any save that omits
# `effort` clears it. Every routing save in the picker must therefore carry a
# set effort forward — otherwise changing a fallback would quietly reset the
# reasoning level, which is the kind of loss nobody reports because nobody sees
# it happen.
PICKER = (_ROOT / "dashboard" / "base" / "js" / "model-picker.js").read_text()
check("the picker reads the server's effort sets, never its own list",
      "got.routing.role_efforts" in PICKER)
check("the effort control exists and is per-role",
      "mp-role-effort" in PICKER)
check("every text-role save carries a set effort forward",
      PICKER.count("body.effort = r.effort") >= 3,
      f'{PICKER.count("body.effort = r.effort")} sites carry it')
check("a model pick only carries the effort when the PROVIDER is unchanged",
      "r.effort && r.provider === provider" in PICKER)
check("...and on Anthropic only when the new MODEL takes that level",
      "_effortCarries" in PICKER and "/api/inference/effort-options" in PICKER)
check("blank means the provider's default, not a stored empty string",
      "if (sel.value) body.effort = sel.value;" in PICKER)
check("an Anthropic row offers its model's own levels and names the default on the blank",
      "role_effort_options" in PICKER and "'Default · '" in PICKER)

# ── 8. the turn logs what it ran at, so a sweep is readable afterwards ─────
check("MODEL_ACTUAL prints the effort the turn used",
      "reasoning=%s" in SESSION)
check("...as what the wire sends (describe_effort), never a value it dropped",
      "_eff_label(_turn_effort)" in SESSION and "_turn_effort = _eff_desc(" in SESSION)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
