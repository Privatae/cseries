#!/usr/bin/env python3
"""Generate docs/CAPABILITIES.md + docs/capabilities.json — the single source
that answers "can C-Series do this?".

Runs at IMAGE BUILD (Dockerfile.linux) so the shipped doc can never lag the
shipped code — it is derived, not written. The smoke gate regenerates it
in-container and fails the ship if the baked copy differs (a half-run build).

Inputs (all code-true, all in-tree):
  - VERSION                                    → the release this doc describes
  - server/applications/scopes.py PERMISSIONS  → the permission catalog
  - server/applications/manifest.py            → what app.yaml may declare
    DECLARABLE_KEYS + the dataclasses            (the gates' own accept list)
  - server/applications/build_contract.py      → the APP SDK CONTRACT text
  - server/applications/api_contract.py + the  → the app-facing /api route table
    live FastAPI app (server.main.app)           (method, path, scope, keys)
  - the route modules' + endpoints' docstrings → what each route AREA is for
  - brain/mastercode/public_tool_registry.py   → every public host op + purpose
  - brain/mastercode/runtime/session.py        → the host's five PERMANENT
    _build_host_orchestration_tools()            orchestration verbs
  - brain/mastercode/runtime/meta_tool_caps.py → each op's capability gate
  - scripts/image_baked_apps.txt               → the image-baked app set
  - native-bundles/*.cseries, factory-bundles/*.cseries,
    system-apps/*/app.yaml                     → the app catalog facts
  - scripts/capability_aliases.txt             → user vocabulary the code never
                                                 says (see that file's header)

CAPABILITY CARDS (`capability_cards` in the JSON) are the part a MODEL reads.
The capability cards need to be detailed and easy to follow, because users will
likely be using older, smaller or open-source models to read them.
So every card carries what / when / how / returns / gotchas, and the corpus
covers the WHOLE surface — host ops (public + the five orchestration verbs),
apps (image-baked AND app-store), and every /api route AREA. The reader is
brain/mastercode/runtime/tools.py (the `capabilities` tool); the acceptance
gate is scripts/test_capability_answers.py.

Output is DETERMINISTIC (stable sorts, no timestamps) so regenerate-and-diff
is a meaningful gate.

The doc's contract: it is a CLOSED WORLD. In the doc = supported (cite the
section). Not in the doc = not supported today — the honest next step is a
community proposal (the host's `propose_capability` tool / the community
board ladder).

Usage (IN-IMAGE only — server.main hard-requires the /app tree):
    python3 /app/scripts/generate_capabilities.py --root /app --out /app/docs
    python3 /app/scripts/generate_capabilities.py --root /app --out /app/docs --check
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_pages  # noqa: E402


def _die(msg: str) -> None:
    print(f"FATAL generate_capabilities: {msg}", file=sys.stderr)
    sys.exit(2)


# ── text helpers (all derivation, no hand-written prose) ─────────────────

_WS = re.compile(r"\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")
_QUOTED = re.compile(r"[\"“]([^\"“”]{4,70})[\"”]")
_EG = re.compile(r"e\.g\.?\s*['\"‘“]([^'\"’”]{1,80})['\"’”]")
_RETURNS = re.compile(r"\breturns?\b", re.IGNORECASE)
_WARNS = re.compile(
    r"\b(never|must not|do not|don't|cannot|can't|refuses?|refused|instead)\b",
    re.IGNORECASE,
)
_USE_WHEN = re.compile(
    r"^(use (it |this )?(when|for)|call (this|it) when|reach for this when)\b",
    re.IGNORECASE,
)

# Tokens that carry no retrieval signal. Kept small and CLOSED: a bloated
# keyword set is what makes a corpus match everything, and a corpus that
# matches everything makes the host claim capabilities that do not exist
# (scripts/test_capability_answers.py §6 is the adversarial guard).
_STOPWORDS = frozenset("""
about after all also and any are because been before being both but can cannot
could did does doing done each else etc even ever every for from get gets got
had has have how into its itself just like made make makes many may might more
most much must never new not now off one only onto other others out over own
per put same see set should some such than that the their them then there these
they this those through too under until use used uses using very was were what
when where which while who whom why will with within without would you your
""".split())


def _clean(text: object) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(_clean(text)) if s.strip()]


def _first_sentence(text: str) -> str:
    s = _sentences(text)
    return s[0] if s else ""


def _first_sentences(text: str, n: int, cap: int = 400) -> str:
    out = " ".join(_sentences(text)[:n])
    return out if len(out) <= cap else out[: cap - 1].rstrip() + "…"


def _trunc(text: str, cap: int) -> str:
    t = _clean(text)
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


def _tokens(*texts: str) -> list[str]:
    out: set[str] = set()
    for t in texts:
        for w in _WORD.findall(str(t or "").lower()):
            if w not in _STOPWORDS:
                out.add(w)
    return sorted(out)


def _dedupe(items) -> list[str]:
    """Order-preserving dedupe of non-empty strings."""
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        s = _clean(i)
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


# ── example-call synthesis (derived from the parameter schema) ───────────

# The op-selector key a mixed-op tool uses, in the order
# tool_access.read_safe_ops looks for it — so a card's worked example names
# the same key the runtime reads.
_OP_KEYS = ("op", "action", "operation", "mode", "command", "method")
# Prefer a READ-ish op in the worked example: it is the safest first call a
# small model can make, and the one that lets it orient before it mutates.
_READ_ISH = ("list", "get", "read", "status", "overview", "search", "show",
             "view", "info", "summary", "describe", "check", "context")


def _primary_enum(props: dict) -> tuple[str, list]:
    for k in _OP_KEYS:
        spec = props.get(k)
        if isinstance(spec, dict) and spec.get("enum"):
            return k, list(spec["enum"])
    return "", []


def _example_value(name: str, spec: dict, depth: int = 0):
    spec = spec if isinstance(spec, dict) else {}
    if "default" in spec:
        return spec["default"]
    if spec.get("enum"):
        return spec["enum"][0]
    eg = _EG.search(str(spec.get("description") or ""))
    kind = spec.get("type")
    if eg and kind in (None, "string"):
        return eg.group(1)
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return [] if depth > 1 else [_example_value(name, spec.get("items") or {}, depth + 1)]
    if kind == "object":
        return {}
    return f"<{name}>"


def _example_args(fn: dict) -> dict:
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = [r for r in (params.get("required") or []) if r in props]
    args: dict = {}
    key, enum = _primary_enum(props)
    if key:
        args[key] = next((v for v in enum if str(v).lower() in _READ_ISH), enum[0])
    for r in required:
        if r not in args:
            args[r] = _example_value(r, props.get(r) or {})
    return args


def _call(name: str, args: dict) -> str:
    return f"{name}({json.dumps(args, ensure_ascii=False)})"


def _param_index(fn: dict) -> dict:
    """The parameter NAME index the card reader builds `params` from
    (tools.py::_caps_normalize_card reads `parameters["properties"]` and keeps
    the sorted key list). Deliberately minimal: the reader keeps only the names,
    so carrying the schema prose here would be bytes no model ever sees. The
    human-readable parameter summary goes into `how`, which the reader DOES
    pass through."""
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return {}
    out_props = {p: {"type": (props[p] or {}).get("type") or "string"
                     if isinstance(props[p], dict) else "string"} for p in props}
    req = [r for r in (params.get("required") or []) if r in out_props]
    return {"type": "object", "required": req, "properties": out_props}


_ALT_VALUES = re.compile(r"^(?:\s*/\s*['\"][^'\"]*['\"])+")


def _op_clauses(fn: dict) -> list[tuple[str, str]]:
    """Each value of a mixed-op tool's op enum, paired with the clause of the
    tool's OWN description that documents it. Three shapes are read, all in the
    registry today: `op='publish' puts a site online …` (manage_site),
    `op='unpublish'/'suspend'/'resume' act on a site …` (one clause covering
    several values), and `overview=headings+counts; section=one section`
    (capabilities). The `op='X'` marker itself is stripped, so what is left is
    a plain-English phrase a user could have said."""
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    key, enum = _primary_enum(props)
    if not key:
        return []
    frags: list[str] = []
    for src in (str(fn.get("description") or ""),
                str((props.get(key) or {}).get("description") or "")):
        frags += [f.strip() for f in re.split(r"[;\n]", src) if f.strip()]
    out = []
    for v in enum:
        sv = str(v)
        pats = (
            re.compile(rf"{re.escape(key)}\s*=\s*['\"]{re.escape(sv)}['\"]"),
            re.compile(rf"\b{re.escape(sv)}\s*="),
            re.compile(rf"['\"]{re.escape(sv)}['\"]"),
        )
        does = ""
        for pat in pats:
            for frag in frags:
                m = pat.search(frag)
                if not m:
                    continue
                rest = _ALT_VALUES.sub("", frag[m.end():]).lstrip(" :—–-")
                does = _clean(rest if len(rest) >= 15 else frag)
                break
            if does:
                break
        out.append((sv, does))
    return out


def _op_menu(clauses: list[tuple[str, str]], key: str) -> str:
    if not clauses:
        return ""
    parts = [f"{v}: {_trunc(does, 130)}" if does else v for v, does in clauses]
    return f"{key} — " + "; ".join(parts)


def _param_summary(fn: dict) -> str:
    """One line per parameter: name, required marker, type, and what it is for.
    Lives in `how` because that is the field the card reader passes through."""
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return ""
    required = set(params.get("required") or [])
    out = []
    for pname in props:
        spec = props[pname] if isinstance(props[pname], dict) else {}
        bits = [f"{pname}{'*' if pname in required else ''}",
                f"({spec.get('type') or 'string'})"]
        about = _trunc(_first_sentence(str(spec.get("description") or "")), 130)
        line = " ".join(bits) + (f": {about}" if about else "")
        out.append(line)
    return "params (* = required) — " + "; ".join(out)


# ── user-vocabulary aliases ──────────────────────────────────────────────

def _load_aliases(root: Path) -> list[tuple[str, str]]:
    p = root / "scripts" / "capability_aliases.txt"
    if not p.is_file():
        _die(f"{p} is missing. It is a REQUIRED input (the user vocabulary that "
             f"derived text can never contain). If this is an in-image run, the "
             f"Dockerfile needs: COPY --chown=privatae:privatae "
             f"scripts/capability_aliases.txt /app/scripts/capability_aliases.txt")
    out: list[tuple[str, str]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" not in line:
            _die(f"{p}:{lineno}: expected 'alias -> card-id', got {raw!r}")
        alias, card_id = (x.strip() for x in line.split("->", 1))
        if not alias or not card_id:
            _die(f"{p}:{lineno}: empty alias or card id in {raw!r}")
        out.append((alias.lower(), card_id))
    if not out:
        _die(f"{p} declares no aliases — delete the file or fill it in.")
    return sorted(set(out))


# ── the app catalog (image-baked + app-store lanes) ──────────────────────

def _image_baked_ids(root: Path) -> set[str]:
    """scripts/image_baked_apps.txt is THE single source of the image-baked set
    — its own header says so, and smoke_test.sh + rebuild_factory_bundles.sh
    read the same file."""
    p = root / "scripts" / "image_baked_apps.txt"
    if not p.is_file():
        _die(f"{p} is missing — it is THE source of the image-baked app set. If "
             f"this is an in-image run, the Dockerfile needs: COPY "
             f"--chown=privatae:privatae scripts/image_baked_apps.txt "
             f"/app/scripts/image_baked_apps.txt")
    ids = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    if not ids:
        _die(f"{p} lists no app ids")
    return ids


def _read_bundle_manifest(path: Path) -> dict | None:
    import yaml  # server deps guarantee pyyaml
    try:
        with zipfile.ZipFile(path) as z:
            return yaml.safe_load(z.read("manifest.yaml")) or {}
    except Exception:
        return None


def _read_yaml_manifest(path: Path) -> dict | None:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _collect_apps(root: Path) -> tuple[list[dict], list[str]]:
    """Every app that reaches a user, from whichever manifest sources this tree
    carries. Two delivery lanes, and the doc must show which is which, because
    the difference is what the USER has to do to get the app:
      image-baked  ships inside the entity image, present at boot
      app store    installed on request (install_app / the App Store app)
    """
    baked = _image_baked_ids(root)
    sources: dict[str, str] = {}
    raws: dict[str, dict] = {}

    # native-bundles/ is the copy that actually ships in the image; read it
    # first so image-baked facts come from the shipped bytes.
    for bundle in sorted((root / "native-bundles").glob("*.cseries")):
        raw = _read_bundle_manifest(bundle)
        if raw is None:
            continue
        aid = str(raw.get("id") or bundle.stem)
        raws.setdefault(aid, raw)
        sources.setdefault(aid, f"native-bundles/{bundle.name}")

    # The store lane. system-apps/ SOURCE is deliberately not in the image
    # (Dockerfile: "no app source in image"), so factory-bundles/ — the signed,
    # git-tracked zips — is the source that can ship; system-apps/*/app.yaml is
    # the source-tree fallback.
    for bundle in sorted((root / "factory-bundles").glob("*.cseries")):
        raw = _read_bundle_manifest(bundle)
        if raw is None:
            continue
        aid = str(raw.get("id") or bundle.stem)
        raws.setdefault(aid, raw)
        sources.setdefault(aid, f"factory-bundles/{bundle.name}")
    for y in sorted((root / "system-apps").glob("*/app.yaml")):
        raw = _read_yaml_manifest(y)
        if raw is None:
            continue
        aid = str(raw.get("id") or y.parent.name)
        if aid.upper() == "TEMPLATE":
            continue
        raws.setdefault(aid, raw)
        sources.setdefault(aid, f"system-apps/{y.parent.name}/app.yaml")

    # Two loud checks, because a missing directory is the SILENT failure here:
    # Path.glob() on a directory that does not exist returns nothing, so an
    # image built without the bundles would emit a half-catalog and pass. That
    # is the exact defect class this whole doc exists to prevent, so it must
    # break the build and name the fix.
    missing_baked = sorted(baked - set(raws))
    if missing_baked:
        _die(f"image_baked_apps.txt declares {missing_baked} but no manifest was "
             f"found for them. native-bundles/ is incomplete or absent — rebuild "
             f"with scripts/rebuild_factory_bundles.sh.")
    if not [a for a in raws if a not in baked]:
        _die(f"no app-store-lane manifests found ({len(raws)} apps read, all of "
             f"them image-baked) — the doc would silently omit the whole store "
             f"lane (browser, security, backups, …). system-apps/ is deliberately "
             f"not in the image, so in-image this needs the Dockerfile to carry "
             f"the signed bundles: COPY --chown=privatae:privatae "
             f"factory-bundles/ /app/factory-bundles/")

    apps = []
    all_keys: set[str] = set()
    for aid in sorted(raws):
        raw = raws[aid]
        all_keys |= {str(k) for k in raw}
        pb = raw.get("prompt_behavior") or {}
        # provenance: the platform default is "standard"
        # (manifest.py DEFAULT_PROVENANCE), and manifest.py REFUSES
        # provenance=native for any id outside KERNEL_REQUIRED_APPS. Defaulting
        # an undeclared manifest to "native" here labelled five installable apps
        # "cannot be uninstalled" in the shipped doc.
        prov = str(raw.get("provenance") or raw.get("tier") or "standard")
        prov = "standard" if prov == "premium" else prov
        apps.append({
            "id": aid,
            "name": str(raw.get("name") or aid),
            "version": str(raw.get("version") or "0.0.0"),
            "provenance": prov,
            "delivery": "image-baked" if aid in baked else "app-store",
            "operator_only": bool(raw.get("operator_only") or raw.get("platform")),
            "permissions": sorted(str(p) for p in (raw.get("permissions") or [])),
            "what_it_is": _clean(pb.get("what_it_is") or raw.get("description") or ""),
            "user_path": _clean(pb.get("user_path") or ""),
            "manifest_source": sources[aid],
        })
    return apps, sorted(all_keys)


# ── card builders ────────────────────────────────────────────────────────

CARD_SCHEMA = "2"   # what/when/how/returns/gotchas — the shape tools.py reads


def _ALIAS_PHRASE(alias: str) -> bool:
    """May this alias be used as a `when` phrase (substring-matched against the
    user's whole sentence), or is it keyword-only (exact match)? Six characters
    is the measured floor: below it, English words swallow the alias and the
    card claims a capability the user never asked about ("invoice" → "voice")."""
    return " " in alias or len(alias) >= 6
# Routes listed inline in an area card's `how`. Only /api/brain exceeds it
# today; the overflow line points at `api_contract`, which carries all of them.
_AREA_ROUTE_LINES = 45


def host_op_lane(name: str, schema: dict, registry: str) -> str:
    """The lane a host op's card carries: the runtime's own classifier
    (tool_lanes.lane_for), so a card can never tell the Host to dispatch what
    it runs itself. This file once kept a second classifier that
    ignored LANE_OVERRIDES, and the cards sent check_page, diagnose and
    close_turn to an agent. The orchestration verbs (dispatch, operate,
    recall, agent_status) are not registry tools and keep their own label."""
    if registry == "orchestration":
        return "orchestration"
    from mastercode.runtime.tool_lanes import lane_for
    return lane_for(name, schema)


# What each lane means, for the rendered legend (CAPABILITIES.md §7).
LANE_LEGEND: dict[str, str] = {
    "read": "the Host calls it with `read`; it changes nothing",
    "operate": "one operation the Host runs itself with `operate`",
    "host": "the Host's own doors to its harnesses (build, the ladder, review), called directly",
    "verb": "a verb on every turn (close_turn, escalate), called directly",
    "dispatch": "untrusted work outside the platform: its reads are the Host's, the rest an agent's",
    "orchestration": "the Host's turn verbs: dispatch, operate, recall, agent_status",
}


def _op_card(name: str, fn: dict, meta: dict, caps: dict, lane: str,
             read_ops: list[str], registry: str,
             siblings: dict[str, list[str]]) -> dict:
    desc = _clean(fn.get("description") or "")
    purpose = _clean(meta.get("purpose")) or _first_sentence(desc)
    category = _clean(meta.get("category"))
    gate_desc = _clean(caps.get("desc"))
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = [r for r in (params.get("required") or []) if r in props]
    key, enum = _primary_enum(props)
    sents = _sentences(desc)
    clauses = _op_clauses(fn)

    # WHEN — trigger phrasings, derived from four code-true sources: the
    # capability gate's own plain-English statement of the user intent, the
    # example utterances the tool author quoted in the description, any
    # explicit "Use when …" sentence, and each op's own clause (with the
    # `op='X'` marker stripped, what is left is a phrase a user could say).
    # Aliases add the words no code says at all.
    when = [gate_desc] if gate_desc else []
    when += [f'"{q}"' for q in _QUOTED.findall(desc)]
    when += [_trunc(s, 200) for s in sents if _USE_WHEN.match(s)]
    when += [_trunc(f"{v} — {does}", 130) for v, does in clauses if does]
    if not when and purpose:
        when = [_trunc(purpose, 200)]
    when = _dedupe(when)[:10]

    # RETURNS — the tool's own words wherever it states them. Split on `;` too:
    # a registry description is often one long semicolon-chained sentence, and
    # sentence-level matching there returns the whole op menu.
    ret_sents = [f for f in re.split(r"[;\n]", desc)
                 if _RETURNS.search(f) and f.strip()]
    if ret_sents:
        returns = _trunc("; ".join(x.strip() for x in ret_sents[:2]), 240)
    elif lane == "read":
        returns = ("The tool's description does not state a return shape. It is a "
                   "read-lane tool: it returns data and changes nothing.")
    else:
        returns = ("The tool's description does not state a return shape — call it "
                   "and read the result envelope.")

    # GOTCHAS — the author's own warnings, plus the schema's hard edges.
    gotchas = [_trunc(s, 240) for s in sents if _WARNS.search(s)][:3]
    if required:
        gotchas.append("Required: " + ", ".join(f"`{r}`" for r in required)
                       + " — a call missing any of these is refused.")
    if key and enum:
        gotchas.append(f"`{key}` must be exactly one of: "
                       + ", ".join(str(e) for e in enum) + ".")
    if any("stepup" in p.lower() or "totp" in p.lower() for p in props):
        gotchas.append("This op can require the user's step-up / 2FA code — read "
                       "the parameter list before calling.")
    # The read-op line says the door the OTHER ops take, by the card's lane.
    # It used to say "Only these ops are read-safe and run without a dispatch"
    # on every mixed tool, 37 of them operate tools whose every op the Host
    # runs itself (gitea's included, which told a Host a PR needs an agent).
    if read_ops and lane == "dispatch":
        gotchas.append("Ops you call yourself with `read`: " + ", ".join(read_ops)
                       + ". The other ops are untrusted work outside the platform, "
                       "which an agent does: dispatch, naming the work.")
    elif read_ops and lane == "operate":
        from mastercode.runtime.direct_ops import direct_op_excluded_ops
        excl = sorted(direct_op_excluded_ops(name))
        line = ("Every op runs in your own turn: " + ", ".join(read_ops)
                + " also as plain reads with `read`, the rest with `operate`")
        if excl:
            line += (f", except {', '.join(excl)}, which reads another's content and "
                     "is an agent's")
        gotchas.append(line + ".")
    gotchas = _dedupe(gotchas)[:5]

    # RELATED — card ids ONLY (the reader resolves `related` by id or title).
    all_ops = siblings.get("__all__") or ()
    related = [t for t in _dedupe(re.findall(r"`([a-z][a-z0-9_.]{2,})`", desc))
               if t != name and t in all_ops]
    related += [s for s in siblings.get(category) or () if s != name]
    related = _dedupe(related)[:6]

    # HOW carries the worked call, the full op menu and the parameter summary,
    # because `how` is one of the ten fields the card reader passes through to
    # the model (tools.py::_caps_normalize_card) — a structured `operations` or
    # `parameters` blob is dropped there and would be bytes nobody reads.
    how = [_call(name, _example_args(fn))]
    menu = _op_menu(clauses, key)
    if menu:
        how.append(menu)
    psum = _param_summary(fn)
    if psum:
        how.append(psum)

    return {
        "id": name,
        "kind": "op",
        "title": name,
        "what": _trunc(purpose or desc, 400),
        "when": when,
        "how": "\n".join(how),
        "returns": returns,
        "gotchas": gotchas,
        "related": related,
        "keywords": _tokens(name.replace(".", " ").replace("_", " "),
                            category, purpose, gate_desc, " ".join(when)),
        "lane": lane,
        "registry": registry,
        "category": category,
        "gate": _clean(caps.get("domain")),
        "parameters": _param_index(fn),
        "read_ops": read_ops,
    }


def _app_card(app: dict) -> dict:
    aid, name = app["id"], app["name"]
    what = app["what_it_is"] or f"The {name} app."
    baked = app["delivery"] == "image-baked"
    when = _dedupe([
        f"open the {name} app",
        f"the user asks about {name.lower()}",
        _trunc(_first_sentence(what), 200),
        (f"where do I find {name.lower()}" if app["user_path"] else ""),
        (f"it lives in {app['user_path']}" if app["user_path"] else ""),
    ])[:8]
    if baked:
        how = _call("open_app", {"app_id": aid})
    else:
        how = (_call("install_app", {"app_id": aid}) + "   # install first, then "
               + _call("open_app", {"app_id": aid}))
    gotchas = [
        ("This app ships inside the entity image and is present at boot — do not "
         "offer to install it."
         if baked else
         "This app is NOT in the image: it comes from the App Store, so it is only "
         "openable after install_app succeeds."),
        (f"provenance `{app['provenance']}` — "
         + ("kernel-required, so it cannot be uninstalled."
            if app["provenance"] == "native"
            else "an ordinary install the user can remove.")),
    ]
    if app["operator_only"]:
        gotchas.append("operator_only — not shown to ordinary users.")
    if app["permissions"]:
        gotchas.append("Declares scopes: " + ", ".join(app["permissions"]) + ".")
    return {
        "id": f"app:{aid}",
        "kind": "app",
        "title": f"{name} (app)",
        "what": _trunc(what, 500),
        "when": when,
        "how": how,
        "returns": ("open_app puts the app's window on the user's screen; "
                    "describe_app returns this app's catalog facts."),
        "gotchas": _dedupe(gotchas)[:5],
        "related": _dedupe(["open_app", "describe_app",
                            "list_app_catalog" if baked else "install_app"]),
        "keywords": _tokens(aid.replace("-", " "), name, what, app["user_path"]),
        "app_id": aid,
        "app_version": app["version"],
        "provenance": app["provenance"],
        "delivery": app["delivery"],
        "scopes": app["permissions"],
        "user_path": app["user_path"],
    }


def _page_card_leaks(cards: list[dict]) -> list[str]:
    """The cards are published too (a Host quotes them and links them), so they
    are scanned on their own terms rather than trusted to match the bundle."""
    return [x for c in cards for x in wiki_pages.scan_published(c, c["id"])]


def _page_card(page: "wiki_pages.Page") -> dict:
    """A public wiki page as a card. The page's questions are its `when`
    phrases (they are written in a visitor's words, which is what the reader
    matches), and the whole page is its `how`: a Host asked how something works
    answers from the page and links the visitor to it."""
    url = f"https://privatae.ai/wiki/{page.slug}"
    return {
        "id": f"page:{page.slug}",
        "kind": "page",
        "title": page.title,
        "what": _trunc(page.summary, 500),
        "when": _dedupe(page.questions)[:12],
        "how": page.body.strip(),
        "returns": f"The page's text. Its public copy, to link a person to: {url}",
        "gotchas": [],
        "related": _dedupe(f"page:{s}" for s in page.links()),
        "keywords": _tokens(page.title),
        "section": page.section,
        "url": url,
    }


# Routes inside a host-COVERED area that stay the person's clicks in the app.
# A covered area's card says "From the host: use <tool>" for the whole area;
# without this the host would be pointed at a tool for a route no op of it
# reaches ("install that shelf design" -> `artifact`, which has no install).
# The parity ledger is per area and unchanged by this; it only tells the host.
_AREA_PERSON_ROUTES: dict[str, list[str]] = {
    "/api/designs": [
        "GET /api/designs/{design_id}/document",
        "POST /api/designs/{design_id}/install",
        "POST /api/designs/keep",
        "DELETE /api/designs/{design_id}",
    ],
}
# A sentence for a person-only half that is not a whole route: GET /api/designs
# is mixed (the shelf rows are the person's; this host's own desktops are
# `artifact list`), so it cannot sit in the list above. For an area no host tool
# calls (/api/connector, walled), the sentence says who does call it and which
# host tools govern it instead; the card shows it whatever the area's class.
_AREA_PERSON_NOTE: dict[str, str] = {
    "/api/designs": ("The shelf's designs, the house's and the account's, are "
                     "browsed in the Designs app; `artifact` list is only this "
                     "host's own desktops, never the whole shelf."),
    "/api/connector": ("These routes are a messaging connector app's own way in, "
                       "opened only by the capability issued to that app; the "
                       "host never calls them. The host starts a pairing with "
                       "propose_nac_policy kind=connector, and the person finishes "
                       "it: they send the bot the code shown in the Network app and "
                       "approve the account with their authenticator code. "
                       "nac_status lists connectors, and revoke_nac_rule "
                       "kind=connector ends one."),
}


def _area_card(area: dict, endpoints: list[dict], doc: str,
               host_tools: list[str], status: str) -> dict:
    prefix = area["prefix"]
    # The noun for this area: the deepest CONCRETE segment of the prefix. A
    # templated tail (`/api/applications/{app_id}`) is not a word a user says.
    noun = next((s.replace("-", " ") for s in reversed(prefix.split("/"))
                 if s and "{" not in s and s != "api"), "api")
    leaf = prefix.rsplit("/", 1)[-1]
    what = _first_sentences(doc, 2, 420) or f"The {noun} area of the app-facing API."
    methods = sorted({e["method"] for e in endpoints})
    # A concrete, template-free path makes the worked example callable as-is.
    concrete = sorted((e["path"] for e in endpoints if "{" not in e["path"]),
                      key=lambda p: (len(p), p))
    sample = concrete[0] if concrete else sorted(e["path"] for e in endpoints)[0]
    named = [t for t in host_tools if t != "kernel_op"]
    if named:
        reach = ("From the host: use " + ", ".join(f"`{t}`" for t in named[:3])
                 + " — those cards carry the exact arguments.")
        person = _AREA_PERSON_ROUTES.get(prefix)
        if person:
            reach += (" Not from the host — the person does these in the app, so "
                      "say so rather than promising them: " + "; ".join(person) + ".")
        if _AREA_PERSON_NOTE.get(prefix):
            reach += " " + _AREA_PERSON_NOTE[prefix]
    elif host_tools == ["kernel_op"]:
        reach = ("From the host: "
                 + _call("kernel_op", {"method": "GET", "path": sample}) + ".")
    else:
        reach = (f"From the host: no named tool is mapped to this area (host-parity "
                 f"status: {status})")
        note = _AREA_PERSON_NOTE.get(prefix)
        reach += f". {note}" if note else " — the user drives it in the app."
    # WHEN phrases are noun-PREFIXED, never a bare path segment. The card
    # reader scores a `when` phrase contained in the user's sentence at 78, so
    # a bare segment like "launch" (from /api/applications/{app_id}/launch)
    # made "launch a satellite" the top hit for an area that has nothing to do
    # with it — a false capability claim, which is the one thing this corpus
    # must never produce.
    # A segment that normalises to NOTHING silently defeats the prefix guard:
    # `/api/tickets/_/patterns` gave `seg = "_"`, so `f"{noun} {seg}"` became
    # "tickets " and the reader's .strip() turned it back into the bare noun —
    # which is how "buy tickets to a concert" scored 78 on the support-ticket
    # area. Normalise first, then drop anything empty, so a bare-noun phrase
    # cannot be emitted by construction.
    _segs = [
        " ".join(seg.replace("-", " ").replace("_", " ").split())
        for e in endpoints
        for seg in e["path"][len(prefix):].strip("/").split("/")
        if seg and "{" not in seg
    ]
    when = _dedupe(
        [f"the user asks about {noun}"]
        + [f"{noun} {s}" for s in _segs if s]
    )[:14]
    gotchas = [
        f"These are APP-facing HTTP routes, not host tools — {len(endpoints)} "
        f"route(s) under `{prefix}`.",
    ]
    if area["scopes"]:
        gotchas.append("An app must declare the scope its route requires: "
                       + ", ".join(area["scopes"][:8]) + ".")
    else:
        gotchas.append("No route in this area declares an app permission scope.")
    if status in ("unreachable", "unreviewed"):
        gotchas.append(f"The host cannot call this area directly today (§7 status: "
                       f"{status}) — say so rather than promising it.")
    # The route list lives in `how` (one line per route, with the endpoint's own
    # one-sentence docstring) rather than in a structured `endpoints` field: the
    # card reader passes `how` through to the model and drops unknown fields, and
    # the method/path/scope table already exists verbatim under `api_contract` in
    # this same file, so nothing is lost and nothing is duplicated.
    lines = [f"From an app: fetch('{sample}'"
             + (", {method: 'POST', …}" if "POST" in methods else "")
             + f"). {reach}"]
    shown = endpoints[:_AREA_ROUTE_LINES]
    lines.append(f"routes ({len(endpoints)}) —")
    for e in shown:
        scope = f" [{e['scope']}]" if e.get("scope") else ""
        does = f": {e['does']}" if e.get("does") else ""
        lines.append(f"  {e['method']} {e['path']}{scope}{does}")
    if len(endpoints) > len(shown):
        lines.append(f"  … and {len(endpoints) - len(shown)} more — the full table "
                     f"for `{prefix}` is under `api_contract` in this file.")
    return {
        "id": f"area:{prefix}",
        "kind": "area",
        "title": f"{prefix} — API area",
        "what": _trunc(what, 500),
        "when": when,
        "how": "\n".join(lines),
        "returns": ("JSON from the route; exact request/response keys are in "
                    "`api_contract`."),
        "gotchas": _dedupe(gotchas)[:4],
        "related": _dedupe(named + (["kernel_op"] if host_tools == ["kernel_op"] else [])),
        "keywords": _tokens(prefix.replace("/", " ").replace("-", " "), what,
                            " ".join(e["path"].replace("/", " ").replace("-", " ")
                                     for e in endpoints),
                            " ".join(e["does"] for e in endpoints)),
        "prefix": prefix,
        "routes": len(endpoints),
        "methods": methods,
        "scopes": area["scopes"],
        "host_tools": host_tools,
        "reach": status,
    }


def host_op_cards() -> tuple[list[dict], list[dict]]:
    """The host operations (the public registry plus the permanent verbs) and
    their capability cards, with no route table: the part of `collect` a gate
    can build without the server app (scripts/test_host_text_doctrine.py
    reads the cards' gotchas from here)."""
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    # The lane on every card is host_op_lane's (runtime/tool_lanes.lane_for),
    # never a classifier of this file's own (check_tool_lanes asserts it).
    from mastercode.runtime.tool_access import read_safe_ops
    # The host's FIVE PERMANENT orchestration verbs. session.py calls them
    # "the host's permanent orchestration tools" and builds them into every
    # host's tool list on every turn; runtime/tools.py appends escalate outside
    # every gate. They are not in PUBLIC_TOOL_SCHEMAS, so §3 used to omit them
    # — while §7 cited `escalate` as covering /api/tickets, i.e. the doc
    # contradicted itself about a tool the host holds permanently.
    from mastercode.runtime.session import (
        _build_host_orchestration_tools as _orchestration_tools,
    )

    entries: list[tuple[str, dict, dict, str]] = []   # (name, fn, meta, registry)
    for _key in PUBLIC_TOOL_SCHEMAS:
        schema = PUBLIC_TOOL_SCHEMAS[_key] or {}
        fn = schema.get("function") or {}
        if fn.get("name"):
            entries.append((fn["name"], fn, schema.get("_meta") or {}, "public"))
    _public_names = {n for n, _f, _m, _r in entries}
    for schema in _orchestration_tools():
        fn = (schema or {}).get("function") or {}
        name = fn.get("name")
        if name and name not in _public_names:
            entries.append((name, fn, {"category": "orchestration"}, "orchestration"))

    host_ops: list[dict] = []
    _by_category: dict[str, list[str]] = {}
    _op_inputs: list[tuple] = []
    for name, fn, meta, registry in entries:
        caps = META_TOOL_CAPS.get(name) or {}
        schema = {"function": fn}
        lane = host_op_lane(name, schema, registry)
        purpose = _clean(meta.get("purpose")) or _first_sentence(fn.get("description", ""))
        category = _clean(meta.get("category"))
        host_ops.append({
            "name": name,
            "category": category,
            "purpose": purpose,
            "domain": _clean(caps.get("domain")),
            "gate_desc": _clean(caps.get("desc")),
            "lane": lane,
            "registry": registry,
        })
        _by_category.setdefault(category, []).append(name)
        _op_inputs.append((name, fn, meta, caps, lane,
                           sorted(read_safe_ops(name, schema)) if lane != "read" else [],
                           registry))
    host_ops.sort(key=lambda x: (x["category"], x["name"]))
    for _cat in _by_category:
        _by_category[_cat] = sorted(_by_category[_cat])
    _by_category["__all__"] = sorted(n for n, _f, _m, _r in entries)

    op_cards = [_op_card(*args, _by_category) for args in _op_inputs]
    return host_ops, op_cards


def collect(root: Path) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "brain"))

    # Version: the baked build-info (Dockerfile stamps PC_TAG there); "dev"
    # for a source-tree run before any bake.
    version = "dev"
    bi = root / "system" / "build-info.json"
    if bi.exists():
        try:
            version = (json.loads(bi.read_text()).get("tag") or "dev").strip() or "dev"
        except Exception:
            pass

    aliases = _load_aliases(root)
    alias_by_card: dict[str, list[str]] = {}
    for alias, cid in aliases:
        alias_by_card.setdefault(cid, []).append(alias)

    # ── permission scopes ─────────────────────────────────────────────
    from server.applications.scopes import PERMISSIONS  # noqa: E402
    scopes = sorted(PERMISSIONS)

    # ── app.yaml surface ──────────────────────────────────────────────
    # DECLARABLE_KEYS is the author-facing accept list the build and publish
    # gates enforce (manifest.unknown_keys); the dataclass breakdown below
    # shows the nested shapes. Rendering the gates' OWN set is what keeps §1
    # from drifting into a claim the validator does not make.
    from server.applications import manifest as _manifest  # noqa: E402
    manifest_fields = {}
    for cls_name in ("AppManifest", "WindowHints", "RuntimeSpec", "StorageSpec", "PromptBehavior"):
        cls = getattr(_manifest, cls_name, None)
        if cls is None or not dataclasses.is_dataclass(cls):
            continue
        manifest_fields[cls_name] = sorted(
            f.name for f in dataclasses.fields(cls) if f.name != "source_dir"
        )
    declarable_keys = sorted(getattr(_manifest, "DECLARABLE_KEYS", None)
                             or manifest_fields.get("AppManifest") or [])
    accepted_not_declarable = {
        name: sorted(getattr(_manifest, name, None) or [])
        for name in ("LEGACY_KEYS", "STORE_METADATA_KEYS", "PLATFORM_INJECTED_KEYS")
    }

    # ── the APP SDK CONTRACT (verbatim — the build-time law) ──────────
    from server.applications.build_contract import app_sdk_build_contract  # noqa: E402
    sdk_contract = app_sdk_build_contract()

    # ── the app-facing /api route table (live introspection) ──────────
    from server.main import app as _fastapi_app  # noqa: E402
    from server.applications.api_contract import build_api_contract  # noqa: E402
    api_contract = build_api_contract(_fastapi_app)

    # The /api WebSocket routes, kept apart from api_contract. Apps build against
    # that table, and it carries HTTP routes only (server/applications/
    # api_contract.py says why). check_kernel_ops.py reads this list, so a wall on
    # a WebSocket path such as /api/connector/gateway is checked against a route
    # that exists, rather than failing smoke as "moved" on every build.
    # The route table through server/route_table.py, never app.routes directly:
    # from fastapi 0.137.0 app.routes holds each included router, not its routes.
    from starlette.routing import WebSocketRoute  # noqa: E402
    from server.route_table import declared_route, effective_routes  # noqa: E402
    served_routes = effective_routes(_fastapi_app)
    websocket_routes = sorted({
        r.path for r in served_routes
        if isinstance(declared_route(r), WebSocketRoute)
        and (r.path or "").startswith("/api/")})

    # What each route DOES, in the route author's own words: the endpoint's
    # docstring, and the route module's docstring for the area as a whole.
    # This is where the plain-language surface comes from. The doc used to
    # render all 543 routes as a prefix and a count, so a user asking about
    # "voice" or a "screenshot" got nothing back while both shipped.
    route_does: dict[tuple[str, str], str] = {}
    module_doc: dict[str, str] = {}
    for r in served_routes:
        path = getattr(r, "path", "") or ""
        ep = getattr(r, "endpoint", None)
        if not path.startswith("/api/") or ep is None:
            continue
        doc = _first_sentence(getattr(ep, "__doc__", "") or "")
        for m in sorted(getattr(r, "methods", None) or []):
            route_does.setdefault((m, path), _trunc(doc, 160))
        mod = sys.modules.get(getattr(ep, "__module__", "") or "")
        mdoc = _clean(getattr(mod, "__doc__", "") or "")
        if mdoc:
            module_doc.setdefault("/".join(path.split("/")[:3]), mdoc)

    host_ops, op_cards = host_op_cards()

    # ── host parity ledger ────────────────────────────────────────────
    # The measured gap between the app-facing route surface (what the
    # platform can do) and the host tool surface (what the HOST can do).
    # The route side and the lane counts are DERIVED. The route-area →
    # host-tool mapping is DECLARED (reviewed by hand, longest-prefix wins)
    # because no mechanical link exists yet between a kernel route and the
    # meta-tool that covers its function — a kernel_op exposure manifest
    # is to replace this table with a derived mapping, at which point
    # it is deleted. Three states, all explicit:
    #   tools listed  → covered (a host verb reaches this area)
    #   []            → reviewed, UNREACHABLE by the host today (the gap)
    #   key absent    → UNREVIEWED (nobody has checked; the ratchet's
    #                   second number)
    _AREA_HOST_TOOLS: dict[str, list[str]] = {
        # network + bastion governance — verified (runtime
        # direct-ops registry + NAC commit tools)
        "/api/network": [
            "get_network", "machine_map", "nac_status", "propose_nac_policy",
            "propose_egress_rule", "propose_mgmt_grant", "approve_nac_draft",
            "revoke_nac_rule", "quarantine_bastion", "suspend_bastion",
            "manage_bastion",
        ],
        # memory / brain surface
        "/api/brain": [
            "memory_search", "memory_curate",
            "chat_threads", "context_ingest",
        ],
        # circles comms
        "/api/circles": ["circles_manage", "host_message", "contacts_manage"],
        # projects (the Workbench's own /api/workbench routes are their own
        # row, below)
        "/api/projects": [
            "create_project", "list_projects", "get_project", "update_project",
            "project_phase", "archive_project", "set_project_routing",
        ],
        # inference routing / catalog
        "/api/inference": ["model_route", "inference_catalog"],
        "/api/embedding": ["embedding_route", "embed_jobs"],
        # The picture role is reachable through the SAME tool — its
        # media_* ops — rather than a second tool name the host would have to
        # choose between.
        "/api/media-embedding": ["embedding_route", "embed_jobs"],
        # Context extraction rides context_ingest's extract /
        # extract_machines ops — the tool the host already uses for the user's
        # documents — rather than a second tool name it would have to choose
        # between. Same reasoning as the media role above.
        "/api/context-extract": ["context_ingest"],
        # The one index: POST /api/search wraps
        # memory_retrieval.unified_search, whose memory + documents half IS
        # federated_recall — the engine behind the host's permanent `recall`
        # verb — and whose documents store is context_ingest's search op. The
        # route exists so a PERSON and an APP get the search the host already
        # has; the host reaches it through the tools it already holds, not a
        # second name it would have to choose between.
        "/api/search": ["recall", "context_ingest"],
        # the Host's own schema `world`: the apps' /api/world sql, describe, rows, history and
        # restore are the data tool's ops, and recall searches what it holds.
        "/api/world": ["data", "recall"],
        # scheduling
        "/api/calendar": ["schedule_task", "task_template", "calendar_read", "calendar_log"],
        # one function ↔ one tool, name-verified against META_TOOL_CAPS.
        # /api/agents lists, streams and cancels agents and scheduled jobs; it
        # starts none, so `dispatch` is not the tool for it:
        # agent_status reads them, entity_tasks cancels a task, schedule_task
        # lists and cancels the scheduled ones.
        "/api/agents": ["agent_status", "entity_tasks", "schedule_task"],
        "/api/artifacts": ["artifact"],
        # the Designs shelf: server/routes/designs.py's 11
        # routes. `artifact` reaches the host's half, by op: publish_design ->
        # /publish-current and /local/{id}/publish (designs.publish_artifact),
        # and POST "" (raw HTML, designs.publish_document) by the same effect
        # through create + publish_design; set_desktop -> /local/{id}/wear and
        # /restore (a restored swatch is kernel_op PUT /api/appearance, the
        # trial discard is op='delete'); list + render -> this host's desktops
        # in GET "" and /local/{id}/document. Browsing the shelf (GET "" shelf
        # rows, /{id}/document), trying one (/{id}/install, /keep) and taking
        # one off (DELETE /{id}) are the PERSON's clicks in the Designs app —
        # "click and try" — and no host verb reaches them
        # today; _AREA_PERSON_ROUTES puts them on the area's card so the host
        # says so. Declared covered on the /api/browser precedent. Without
        # this row the area reads as unclassified and the smoke stops at the
        # ratchet.
        "/api/designs": ["artifact"],
        # messaging connectors:
        # server/routes/connector.py's five routes (pair, platform, messages,
        # messages/sent, turns/{turn_id}) are the connector APP's own way in,
        # opened only by the capability issued to that app. No host verb calls
        # them, by design: /pair carries the one-time code that never reaches
        # the model, and /messages starts the person's turns. All five are in
        # kernel_ops._WALLED_PATHS, so they derive WALLED; they declare no
        # scope, so [] without the wall would read them as unreachable against
        # the floor of 0. The NAC tools that govern the area are NOT listed:
        # propose_nac_policy kind=connector starts a pairing, nac_status lists
        # connectors and revoke_nac_rule kind=connector ends one, but none of
        # them calls these routes, and listing them would read the routes as
        # covered (the /api/workbench row gives the same rule).
        # _AREA_PERSON_NOTE names those tools on the area's card. Without this
        # row the smoke reads the area against the floor and stops at the
        # ratchet.
        "/api/connector": [],
        "/api/tickets": ["tickets", "escalate"],
        # support half of the Tickets pane (server/routes/support.py): the same
        # `tickets` tool reaches it through its support_* ops
        "/api/support": ["tickets"],
        "/api/decisions": ["decision_resolve", "tickets"],
        "/api/phone-alias": ["phone_alias"],
        "/api/contacts": ["contacts_manage"],
        # The contact card for another Host: the Host reads the
        # same resolver through contacts_manage op='card'.
        "/api/hosts": ["contacts_manage"],
        "/api/inbox": ["inbox_read", "inbox_send"],
        # Reach-out: the four routes (list, delete a
        # channel, delete a trigger, test a channel) are the Circles pane's
        # half of the tables the `reachout` tool drives: op=list /
        # remove_channel / remove_trigger / test answer the four routes one
        # to one. Channel creation is deliberately not a route, so
        # `reachout_channel` covers nothing here and is not listed. Without
        # this row the ratchet reads the area as unreviewed.
        "/api/reachout": ["reachout"],
        "/api/ladder": ["ladder.run", "ladder.status", "ladder.decision"],
        "/api/coding-protocols": ["coding_protocol"],
        "/api/review": ["build", "review.compare",
                        "review.propose_commit", "review.adopt",
                        "review.adopt_undo", "review_session"],
        "/api/vault": ["keys_manage"],
        # browser form entries. The area is
        # exactly server/routes/browser_forms.py's 7 routes under
        # /api/browser/forms. `autofill` reaches four of them, by op:
        # saved -> GET "", list -> GET /grants, revoke -> DELETE
        # /grants/{host}, fill -> POST /fill. The other three are the USER's
        # act and the host must not reach them: POST /capture and POST /save
        # are the Browser app's "save these entries" button, and POST /grants
        # is the standing grant, made in Keys beside the entries it governs so
        # a page cannot talk the host into widening its own permission.
        # Declared covered as a door plus its transport, with the three
        # user-only routes named here rather than left for someone to
        # rediscover.
        "/api/browser": ["autofill"],
        "/api/compute": ["connect_compute"],
        "/api/codesearch": ["search_my_code", "index_my_code"],
        "/api/skills": ["list_skills"],
        "/api/community": ["community_board", "propose_capability"],
        "/api/integrations": ["integrations_manage"],
        # app lifecycle (the /api/applications area groups one level deeper)
        "/api/applications/install": ["install_app"],
        "/api/applications/refresh-store": ["refresh_app_store"],
        "/api/applications/store": ["list_app_catalog", "purchase_app"],
        "/api/applications/build": ["build"],
        "/api/applications/publisher": ["publish_app"],
        # one installed app's own lifecycle (resident lifecycle):
        # /launch is the host's `open_app`; /runtime is the on/off switch for a
        # process app's backend — system.start_app / system.stop_app /
        # system.restart_app, with system.app_status as the read; /close is
        # the window's own gesture and stays the user's.
        "/api/applications/{app_id}": [
            "open_app", "system.start_app", "system.stop_app",
            "system.restart_app", "system.app_status",
        ],
        # sites — manage_site's op enum is list/analytics/unpublish/suspend/
        # resume/restart; domains + publish have their own tools
        "/api/applications/sites/analytics": ["manage_site"],
        "/api/applications/sites/list": ["manage_site"],
        "/api/applications/sites/restart": ["manage_site"],
        "/api/applications/sites/resume": ["manage_site"],
        "/api/applications/sites/suspend": ["manage_site"],
        "/api/applications/sites/unpublish": ["manage_site"],
        "/api/applications/sites/domains": ["manage_site_domain"],
        "/api/applications/sites/publish-project": ["publish_site"],
        # reviewed, [] = no NAMED host tool. appearance + the sites customer
        # stack are now reached through kernel_op (derived below, from
        # kernel_ops._EXPOSED_PREFIXES).
        "/api/appearance": [],
        # Generated images kept on this Host. GET
        # /api/images/{id} serves the bytes to the browser (an <img>, a clicked
        # link); the HOST reaches the same images through image_generate — it
        # makes them, lists them (action='gallery') and reads one back by this
        # exact url (action='look', image_engine._own_picture).
        "/api/images": ["image_generate"],
        # backup: /settings (the user's own schedule) reached through kernel_op;
        # /local (read:backup_list, operator-domain scope) derives
        # as WALLED — the bastion's copy is read by the user, not the host.
        # The Host's own backups, each route classified in
        # kernel_ops: /runs (its runs; "back me up now") and /exports (the
        # exports waiting for the owner, read-only) are EXPOSED; the keys
        # (/key/register, /retire, /recovery/adopt; /generate, /escrow and
        # /sealed answer 410), /restore and its /key, /fetch and /sidecar,
        # /exports/confirm and /ship are WALLED: each decides who can read the
        # user's data, replaces the Host, or builds a full copy of it, so it is
        # the owner's act alone, behind their second factor.
        "/api/backup": [],
        "/api/applications/sites/customers": [],
        "/api/applications/sites/stripe": [],
        "/api/applications/sites/db": [],
        "/api/applications/sites/exposure": [],
        # App data follows the person: the two
        # /api/applications/sites/data routes — "is there anything of mine on a
        # site running this app" and "bring it over". Reached through kernel_op
        # (kernel_ops._EXPOSED_PREFIXES) on execute:install, which is already in
        # ENTITY_SCOPE_TO_BITS, so no new scope and no new named tool: the
        # routes ARE the surface, the same reasoning the peer app and groups
        # rows below record. Classified in the change that adds the area so it
        # never reads as unreviewed and never touches the ratchet floor.
        "/api/applications/sites/data": [],
        # Peer app lane: the six /api/peer/app routes are
        # reached through kernel_op (kernel_ops._EXPOSED_PREFIXES) with
        # read:peer_app / write:peer_app in ENTITY_SCOPE_TO_BITS — no named
        # tool, by design: the routes ARE the app's surface and the host
        # drives the same six verbs. Without this row the ratchet reads the
        # area as unreviewed.
        "/api/peer/app": [],
        # Groups: the /api/groups routes are reached through
        # kernel_op (kernel_ops._EXPOSED_PREFIXES) with read:group /
        # write:group in ENTITY_SCOPE_TO_BITS — no named tool, by design and
        # for the reason the peer app row gives: the routes ARE the surface and
        # the host drives the same verbs. Classified in the same change that
        # adds the area, so it never reads as unreviewed and never touches the
        # ratchet floor.
        "/api/groups": [],
        # Oversight ledger. The reads — GET /api/oversight/findings and /machines — are
        # reached through kernel_op (kernel_ops._EXPOSED_PREFIXES) with
        # read:oversight in ENTITY_SCOPE_TO_BITS, the same reasoning the peer
        # app and groups rows give: the routes ARE the surface, and the host
        # now reads the ledger it writes. The one mutation, POST .../resolve,
        # is "a person saying I have dealt with this — the one fact the
        # deterministic fold can never derive" (server/routes/oversight.py):
        # kernel_ops._WALLED_PATHS walls it and write:oversight has no entity
        # bits, so it derives WALLED — classified, never ratcheted.
        # oversight_ingest (the host's own tool) WRITES the fold; it does not
        # read or resolve findings. Without this row the ratchet reads the
        # area as unreviewed.
        "/api/oversight": [],
        # Ships (server/routes/ships.py). The host reads its runs,
        # pipelines, machines and key names and starts a run through
        # kernel_op (read:ships / write:ships in ENTITY_SCOPE_TO_BITS): the
        # routes ARE the surface, as for the groups and oversight rows. A start
        # only files the plan on the person's device. Clearing a machine's
        # pinned key is walled (kernel_ops._WALLED_PATHS): the person's act.
        "/api/ships": [],
        # Code Approvals. server/routes/workbench.py's six
        # routes are the PERSON's review surface for the Host's pull requests:
        # is the forge connected, the list, one approval, its diff, "ask for
        # changes", and the chat line written once the forge shows a merge. No
        # host tool reads them. The Host's side of a Code Approval happens
        # in-process, never through these routes: every save point (`build`,
        # `workspace_file` writes, `review.adopt`) opens or updates one through
        # project_git.sync_project_repo → _ensure_pr, and under Auto-Approve
        # only, `build` merges it as the Host (project_git.merge_project_pr)
        # and writes the same chat line (code_approvals.announce_merge). Those
        # tools are not listed, because that would read these routes as
        # covered when the Host reaches none of them. kernel_op cannot reach
        # them either: /api/workbench is not in kernel_ops._EXPOSED_PREFIXES.
        # read:review / write:review have no entity bits in
        # ENTITY_SCOPE_TO_BITS, so all six derive WALLED. For the four reads
        # and the chat line that class comes from the unmapped scope, not from
        # a decision that the Host must never read its own Code Approvals:
        # giving it that read would be a reviewed change of its own (a named
        # tool listed here, or kernel_op exposure plus a read:review mapping).
        # "Ask for changes" IS a decision, walled by path in
        # kernel_ops._WALLED_PATHS: it records the person's review, the other
        # half of Approve, which merges as the person who clicks it.
        # Classified, so the area never reads as unreviewed and the smoke
        # does not stop at the ratchet.
        # A seventh, POST /api/workbench/index/send, the
        # message the person types into the Index. It starts the person's own
        # turn, so it is walled by path in kernel_ops._WALLED_PATHS and derives
        # WALLED; read:brain_memory, the scope recorded for it, has entity bits,
        # so without the wall it would read as unreachable against the floor
        # of 0. The Index's search is POST /api/projects/index (its words in
        # the body, never a URL), in the /api/projects area beside /search.
        "/api/workbench": [],
    }

    def _area_tools(prefix: str) -> list[str] | None:
        best = None
        for k in _AREA_HOST_TOOLS:
            if (prefix == k or prefix.startswith(k + "/")) and \
                    (best is None or len(k) > len(best)):
                best = k
        return _AREA_HOST_TOOLS[best] if best is not None else None

    # Every host verb, whichever registry holds it — a declared row citing
    # `dispatch`/`build`/`read` must not false-negative because the verb is not
    # in PUBLIC_TOOL_SCHEMAS.
    _tool_names = {op["name"] for op in host_ops} | {
        "dispatch", "read", "operate", "recall", "agent_status",
        "escalate", "build", "artifact", "open_app",
    }

    def _area_prefix(path: str) -> str:
        # 3 segments by default; /api/applications is a mega-router (app
        # lifecycle + the whole sites stack), so it groups one level deeper,
        # and the sites subtree one deeper still — that is where the
        # customer/stripe/db gap lives and it must not hide inside a
        # "covered" lump.
        segs = path.split("/")
        if path.startswith("/api/applications/sites/"):
            return "/".join(segs[:5])
        if path.startswith("/api/applications/"):
            return "/".join(segs[:4])
        return "/".join(segs[:3])

    # kernel_op coverage is DERIVED from the enforcing module itself
    # (runtime/kernel_ops.py) — shown surface ≡ enforced surface. A route
    # counts as kernel_op-covered only when exposed, not walled, AND its
    # scope translates to nonzero bits (or is unscoped); a walled path or a
    # platform-domain scope inside a reviewed area derives as WALLED —
    # deliberately outside host reach, tracked separately, never ratcheted.
    from mastercode.runtime.kernel_ops import (  # noqa: E402
        _EXPOSED_PREFIXES as _KO_EXPOSED, _WALLED_PATHS as _KO_WALLED,
        _WALLED_OPS as _KO_WALLED_OPS,
    )
    from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS  # noqa: E402

    def _route_class(method: str, path: str, scope, area_declared) -> str:
        if isinstance(area_declared, list) and \
                any(t in _tool_names for t in area_declared):
            return "covered"
        if path in _KO_WALLED:
            return "walled"
        # Method-aware wall: a pair where reading is host work and writing is
        # not (kernel_ops._WALLED_OPS). Classed WALLED, not unreachable —
        # unreachable means nobody classified it; this one is classified, and
        # walled is the class that is tracked and never ratcheted.
        if (method.upper(), path) in _KO_WALLED_OPS:
            return "walled"
        if any(path == p or path.startswith(p + "/") for p in _KO_EXPOSED):
            if scope is None or ENTITY_SCOPE_TO_BITS.get(scope, 0):
                return "covered"
            return "walled"  # exposed prefix but platform-domain scope
        if area_declared is None:
            return "unreviewed"
        if scope is not None and not ENTITY_SCOPE_TO_BITS.get(scope, 0):
            return "walled"  # reviewed area, platform-domain scope
        return "unreachable"

    _areas: dict[str, dict] = {}
    for r in api_contract.get("routes", []):
        p = _area_prefix(r["path"])
        a = _areas.setdefault(p, {"prefix": p, "routes": 0, "scopes": set(),
                                  "classes": {}, "endpoints": []})
        a["routes"] += 1
        if r.get("scope"):
            a["scopes"].add(r["scope"])
        method = r.get("method", "GET")
        cls = _route_class(method, r["path"], r.get("scope"), _area_tools(p))
        a["classes"][cls] = a["classes"].get(cls, 0) + 1
        a["endpoints"].append({
            "method": method, "path": r["path"], "scope": r.get("scope"),
            "does": route_does.get((method, r["path"]), ""),
        })
    # A person-only route that no longer exists would tell the host a stale
    # story; fail the build instead.
    _live_routes = {f"{r.get('method', 'GET')} {r['path']}" for r in api_contract.get("routes", [])}
    for _p, _person in _AREA_PERSON_ROUTES.items():
        _gone = [x for x in _person if x not in _live_routes]
        if _gone:
            raise SystemExit(f"_AREA_PERSON_ROUTES[{_p!r}] names routes that do not exist: {_gone}")
    parity_areas = []
    area_cards = []
    counts = {"covered": 0, "unreachable": 0, "unreviewed": 0, "walled": 0,
              "routes_covered": 0, "routes_unreachable": 0,
              "routes_unreviewed": 0, "routes_walled": 0}
    for p in sorted(_areas):
        a = _areas[p]
        declared = _area_tools(p)
        tools = sorted(t for t in (declared or []) if t in _tool_names)
        if not tools and a["classes"].get("covered"):
            tools = ["kernel_op"]
        # Area label: the gap class if one is present, else covered/walled/
        # partial. Route-level sums below are the exact ratchet numbers.
        for cls in ("unreviewed", "unreachable"):
            if a["classes"].get(cls):
                status = cls
                break
        else:
            n_cov = a["classes"].get("covered", 0)
            if n_cov == a["routes"]:
                status = "covered"
            elif n_cov:
                status = "partial"  # covered + walled mix (e.g. sites/db)
            else:
                status = "walled"
        counts[status] = counts.get(status, 0) + 1
        for cls in sorted(a["classes"]):
            counts[f"routes_{cls}"] += a["classes"][cls]
        area = {
            "prefix": p, "routes": a["routes"], "scopes": sorted(a["scopes"]),
            "host_tools": tools, "status": status,
            "route_classes": {k: a["classes"][k] for k in sorted(a["classes"])},
        }
        parity_areas.append(area)
        area_cards.append(_area_card(
            area,
            sorted(a["endpoints"], key=lambda e: (e["path"], e["method"])),
            module_doc.get("/".join(p.split("/")[:3]), ""),
            tools, status,
        ))
    lanes: dict[str, int] = {}
    for op in host_ops:
        lanes[op["lane"]] = lanes.get(op["lane"], 0) + 1
    host_parity = {
        "mapping_provenance": "declared (phase-2 kernel_op exposure manifest supersedes this table)",
        "areas": parity_areas,
        "counts": counts,
        "lanes": {k: lanes[k] for k in sorted(lanes)},
    }

    # ── the app catalog: image-baked AND app-store lanes ──────────────
    apps, manifest_keys_seen = _collect_apps(root)
    app_cards = [_app_card(a) for a in apps]

    # ── the public wiki's pages ──────────────────────────────────────
    # The same pages privatae.ai/wiki publishes, so a Host explains how the
    # platform works from the text the public reads, at this release.
    page_cards = [_page_card(p) for p in wiki_pages.load_pages(root / wiki_pages.PAGE_DIR)]
    card_leaks = _page_card_leaks(page_cards)
    if card_leaks:
        _die("leak scan hit on a wiki page card:\n  " + "\n  ".join(card_leaks))

    # ── the card corpus ──────────────────────────────────────────────
    capability_cards = sorted(op_cards + app_cards + area_cards + page_cards,
                              key=lambda c: (c["kind"], c["id"]))
    by_id: dict[str, dict] = {}
    for c in capability_cards:
        if c["id"] in by_id:
            _die(f"duplicate capability card id: {c['id']!r}")
        by_id[c["id"]] = c

    # A stale alias must BREAK THE BUILD, not rot silently — that is the whole
    # reason a hand-maintained file is allowed to sit beside derived text.
    unresolved = sorted({cid for _a, cid in aliases if cid not in by_id})
    if unresolved:
        _die("scripts/capability_aliases.txt points at card ids that do not "
             f"exist: {unresolved}. Every alias must resolve to a real card.")
    for cid in sorted(alias_by_card):
        words = sorted(set(alias_by_card[cid]))
        card = by_id[cid]
        card["aliases"] = words
        card["keywords"] = sorted(set(card["keywords"]) | set(words))
        # The alias goes in VERBATIM as a `when` phrase, not wrapped in prose.
        # The card reader scores a `when` phrase that appears inside the user's
        # sentence at 78 and an exact keyword at 56 — wrapping it as
        # 'user says "screenshot"' destroys the first match entirely, so
        # "take a screenshot of my screen" fell back to token overlap (33) and
        # tied with cards that merely contain the word "screen".
        #
        # But that same containment rule cuts both ways: the reader matches raw
        # SUBSTRINGS, so a short alias is a false-capability generator —
        # measured, "invoice" scored 78 on the realtime-voice area because
        # "voice" sits inside it. A short alias therefore stays a keyword
        # (exact-match only, 56); phrases earn the containment score.
        card["when"] = _dedupe([w for w in words if _ALIAS_PHRASE(w)]
                               + list(card["when"]))

    return {
        "version": version,
        "scopes": scopes,
        "manifest_fields": manifest_fields,
        "manifest_declarable_keys": declarable_keys,
        "manifest_accepted_not_declarable": accepted_not_declarable,
        "manifest_keys_seen": manifest_keys_seen,
        "sdk_contract": sdk_contract,
        "api_contract": api_contract,
        "websocket_routes": websocket_routes,
        "host_ops": host_ops,
        "apps": apps,
        "host_parity": host_parity,
        "capability_cards": capability_cards,
        "capability_cards_schema": CARD_SCHEMA,
        "capability_aliases": dict(aliases),
    }


def _app_line(a: dict) -> str:
    perms = f" · scopes: {', '.join(a['permissions'])}" if a["permissions"] else ""
    flag = " · operator-only" if a["operator_only"] else ""
    return (f"- **{a['name']}** (`{a['id']}` v{a['version']}, "
            f"{a['provenance']}){flag}{perms}")


def render_md(d: dict) -> str:
    L: list[str] = []
    add = L.append
    add(f"# C-Series Capabilities — {d['version']}")
    add("")
    add("_Generated at image build by `scripts/generate_capabilities.py`. Do not edit —")
    add("every line below is derived from the shipped code and regenerates with every")
    add("C-Series update. Machine-readable form: `docs/capabilities.json`._")
    add("")
    add("**How to read this document — the closed-world rule.** If a capability is")
    add("listed here, C-Series supports it today (cite the section). If it is NOT here,")
    add("C-Series does not support it today — that is a real answer, and the next step is")
    add("a community proposal (the host's `propose_capability` tool, or the community")
    add("board's \"propose this\" ladder). Do not infer capabilities this document does")
    add("not state.")
    add("")
    add(f"**Searching this in plain words.** `docs/capabilities.json` carries "
        f"{len(d['capability_cards'])} CAPABILITY CARDS — one per host operation, one")
    add("per app, one per API area — each with what it does, when to reach for it, a")
    add("worked example call, what comes back, and the mistake to avoid. The")
    add("`capabilities` tool searches those cards first and these headings second,")
    add("because a user asks for \"a screenshot\", not for `/api/screen/capture`.")
    add("")

    add("## 1. Apps — what an app on C-Series can be")
    add("")
    add("An app is a directory with an `app.yaml` manifest. These are the keys an app")
    add(f"may declare — `server/applications/manifest.py::DECLARABLE_KEYS`, "
        f"{len(d['manifest_declarable_keys'])} keys, the exact set the build and publish")
    add("gates accept:")
    add("")
    add("```")
    for k in d["manifest_declarable_keys"]:
        add(k)
    add("```")
    add("")
    add("An `app.yaml` field outside this list is rejected by the build and publish")
    add("gates, naming it; the loader ignores it so an already-installed app never")
    add("vanishes. Nested shapes, field by field:")
    add("")
    for cls in ("AppManifest", "WindowHints", "RuntimeSpec", "StorageSpec", "PromptBehavior"):
        fields = d["manifest_fields"].get(cls)
        if fields:
            add(f"- **{cls}**: {', '.join(fields)}")
    add("")
    _acc = d.get("manifest_accepted_not_declarable") or {}
    if any(_acc.get(k) for k in sorted(_acc)):
        add("Accepted but NOT declarable — permanently tolerated so already-signed")
        add("bundles keep loading, and deliberately not advertised to app authors:")
        add("")
        for k in sorted(_acc):
            if _acc[k]:
                add(f"- **{k}**: {', '.join(_acc[k])}")
        add("")

    add("## 2. Permission scopes")
    add("")
    add("The complete, finite permission catalog an app or tool may require")
    add(f"({len(d['scopes'])} scopes — `server/applications/scopes.py`):")
    add("")
    add("```")
    for s in d["scopes"]:
        add(s)
    add("```")
    add("")

    add("## 3. Host operations — the operations available to this host")
    add("")
    n_pub = sum(1 for o in d["host_ops"] if o.get("registry") == "public")
    n_orch = len(d["host_ops"]) - n_pub
    add(f"The operations THIS host holds, each with its capability gate: {n_pub} from the")
    add(f"public tool registry, plus {n_orch} permanent orchestration verbs that")
    add("`brain/mastercode/runtime/session.py` builds into every host turn.")
    add("")
    add("SCOPE OF THIS SECTION. This is the host's own tool surface, not a census of")
    add("every operation that exists in the platform. Other schema registries are")
    add("deliberately not listed here: the operator-gated set (bastion execution,")
    add("restricted to the operator entity) and the internal meta-tool set. So \"absent")
    add("from §3\" means \"this host cannot do it\" — which is the answer a user needs —")
    add("and never \"no such code exists\".")
    add("")
    cat = None
    for op in d["host_ops"]:
        if op["category"] != cat:
            cat = op["category"]
            add(f"### {cat or 'uncategorized'}")
            add("")
        gate = f" _(gate: {op['domain']})_" if op["domain"] else ""
        perm = " _(permanent — held every turn)_" if op.get("registry") == "orchestration" else ""
        add(f"- **`{op['name']}`** — {op['purpose']}{gate}{perm}")
    add("")

    add("## 4. Apps that reach the user")
    add("")
    baked = [a for a in d["apps"] if a["delivery"] == "image-baked"]
    store = [a for a in d["apps"] if a["delivery"] == "app-store"]
    add(f"{len(d['apps'])} apps in two delivery lanes. The difference is what the user has")
    add("to DO to get one. The live per-pod catalog (what is installed right now) is")
    add("runtime state — the host reads it with `describe_app` / the App Store.")
    add("")
    add(f"### Image-baked ({len(baked)}) — present at boot, nothing to install")
    add("")
    for a in baked:
        add(_app_line(a))
        if a["what_it_is"]:
            add(f"  - {a['what_it_is']}")
    add("")
    add(f"### From the App Store ({len(store)}) — installed on request (`install_app`)")
    add("")
    for a in store:
        add(_app_line(a))
        if a["what_it_is"]:
            add(f"  - {a['what_it_is']}")
    add("")
    add("`provenance: native` means kernel-required and NOT uninstallable — only the ids")
    add("in `server/applications/kernel.py::KERNEL_REQUIRED_APPS` may carry it, and")
    add("`manifest.load()` refuses the claim from anything else. Every other app listed")
    add("here can be removed by the user.")
    add("")

    add("## 5. Building apps — the integration surface")
    add("")
    add("The authoritative build-time law, verbatim from")
    add("`server/applications/build_contract.py` (apps are validated against this")
    add("before they are signed/installed):")
    add("")
    add("```")
    add(d["sdk_contract"].rstrip())
    add("```")
    add("")
    ac = d["api_contract"]
    add(f"The app-facing API surface is CODE-TRUE and FINITE: {ac.get('count', 0)} routes.")
    add("The full table (method, path, required scope, request/response keys) is in")
    add("`docs/capabilities.json` under `api_contract`; at build time the live,")
    add("version-exact authority is `GET /api/applications/api-contract`. Every area")
    add("below also has a capability card in `capabilities.json` naming, in plain words,")
    add("what it lets an app do, plus every route it contains:")
    add("")
    for c in d["capability_cards"]:
        if c["kind"] != "area":
            continue
        add(f"- `{c['prefix']}` ({c['routes']} routes) — {_trunc(c['what'], 150)}")
    add("")

    add("## 6. Not listed here?")
    add("")
    add("Then C-Series does not do it today. The host offers to file a community")
    add("proposal: a thread in Ideas & Proposals plus a governed")
    add("`community/proposals` feature-request PR under the host's own identity")
    add("(`propose_capability`). Votes route attention; approval stays human.")
    add("")

    hp = d.get("host_parity") or {}
    hc = hp.get("counts") or {}
    add("## 7. Host parity — software for the AI")
    add("")
    add("The measured gap between what the PLATFORM can do (the route surface")
    add("above) and what the HOST can do without a human clicking (its tool")
    add("surface). Host tools by verb lane:")
    add("")
    _lanes = hp.get("lanes") or {}
    for k in sorted(_lanes):
        _what = LANE_LEGEND.get(k)
        add(f"- `{k}` — {_lanes[k]} tools" + (f": {_what}" if _what else ""))
    add("")
    add(f"Routes: {hc.get('routes_covered', 0)} covered · "
        f"{hc.get('routes_walled', 0)} walled · "
        f"{hc.get('routes_unreachable', 0)} unreachable · "
        f"{hc.get('routes_unreviewed', 0)} unreviewed.")
    add("`unreachable` = reviewed, no host verb reaches it — the gap being")
    add("closed. `unreviewed` = not yet classified. Both are ratcheted by the")
    add("smoke gate (`scripts/check_host_parity.py`): down-only, ship over")
    add("ship. `walled` = DELIBERATELY host-unreachable (credential reveals,")
    add("platform-domain scopes) — tracked, never ratcheted. kernel_op")
    add("coverage is derived from `runtime/kernel_ops.py` (the enforcing")
    add(f"module). Mapping provenance: {hp.get('mapping_provenance', '?')}.")
    add("")
    for a in hp.get("areas") or []:
        if a["status"] in ("covered", "partial"):
            _extra = "" if a["status"] == "covered" else \
                f" (partial — {a['route_classes']})"
            add(f"- `{a['prefix']}` ({a['routes']} routes) — covered: "
                f"{', '.join('`%s`' % t for t in a['host_tools'])}{_extra}")
        else:
            add(f"- `{a['prefix']}` ({a['routes']} routes) — **{a['status'].upper()}**")
    add("")

    pages = [c for c in d["capability_cards"] if c["kind"] == "page"]
    if pages:
        add("## 8. How the platform works — the public wiki")
        add("")
        add("Explanations for people, published at https://privatae.ai/wiki. Each")
        add("is a `page:` card whose `how` is the whole page. When someone asks how")
        add("something works, answer from the page and give them its link.")
        add("")
        for c in pages:
            add(f"- `{c['id']}` — {c['title']}: {_trunc(c['what'], 160)}")
        add("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/app", help="cseries tree root (default /app)")
    ap.add_argument("--out", default=None, help="output dir (default <root>/docs)")
    ap.add_argument("--check", action="store_true",
                    help="regenerate and diff against the existing files; exit 1 on drift")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out = Path(args.out).resolve() if args.out else (root / "docs")
    if not (root / "server").is_dir():
        _die(f"{root} does not look like a cseries tree (no server/)")

    data = collect(root)
    md = render_md(data)
    js = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"

    md_path, js_path = out / "CAPABILITIES.md", out / "capabilities.json"
    if args.check:
        old_md = md_path.read_text() if md_path.exists() else ""
        old_js = js_path.read_text() if js_path.exists() else ""
        if old_md == md and old_js == js:
            print(f"OK capabilities current (v{data['version']}, "
                  f"{data['api_contract'].get('count', 0)} routes, "
                  f"{len(data['host_ops'])} ops, {len(data['apps'])} apps, "
                  f"{len(data['capability_cards'])} cards)")
            return 0
        print("DRIFT: baked capabilities differ from regeneration", file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md)
    js_path.write_text(js)
    print(f"wrote {md_path} + {js_path} (v{data['version']}, "
          f"{data['api_contract'].get('count', 0)} routes, "
          f"{len(data['host_ops'])} ops, {len(data['apps'])} apps, "
          f"{len(data['capability_cards'])} cards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
