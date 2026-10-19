"""Code-true, app-facing API contract — generated from the live FastAPI route table.

C-Series apps call their own origin's `/api/...` routes (see build_contract §3). Until
now the SDK contract told the host WHICH scopes exist, but not WHICH routes exist or
what scope each one needs — so a generated app could call a route that doesn't exist,
or a scope-gated route its manifest never declared, and still pass validation and ship.
That "silent contract drift" was the top finding of an app audit
(apps calling nonexistent endpoints; manifest/scope drift; reading response keys the
backend never returns).

This module is the missing half. It introspects the RUNNING app to produce, per
app-facing route: {method, path, scope, request/response key sets where declared}. It
cannot drift from enforcement — it IS the routes and their `requires_permission`
dependencies.

Two consumers:
  - GET /api/applications/api-contract   → the host reads the REAL surface at build
                                            time (server/routes/applications.py).
  - build_contract.validate_build        → the conformance gate diffs an app's own
                                            /api calls against it (route exists? scope
                                            declared?).

Design rule (inherited from build_contract): CLEAR TRUE-POSITIVES ONLY. A legitimate
app must never be falsely blocked, so every check here is conservative and skips when
it cannot be certain.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("privatae.applications.api_contract")

# Attribute stamped on the requires_permission() dependency callable (middleware.py)
# so a route's required scope is recoverable by introspection, not source parsing.
SCOPE_ATTR = "_app_scope"
# Stamped beside it by requires_any_permission(): every scope that admits the
# route (any one is enough). The contract carries them as `scopes_any`.
SCOPES_ATTR = "_app_scopes"

_SCAN_SUFFIXES = (".html", ".htm", ".js", ".mjs")
_SCAN_SKIP_DIRS = {"node_modules", "dist", "build", ".git", "bin"}
# Not a cap: every file is scanned. This is how much of one file is held in
# memory at a time; a bigger file is read in windows of this size.
_SCAN_WINDOW_BYTES = 2_000_000

# An own-origin /api call. Captures the STATIC path prefix (up to a template
# interpolation, query, or fragment) and, when the fetch options are a flat object,
# the method.
#   fetch('/api/x')                          -> ('/api/x', None)
#   fetch(`/api/x/${id}`, {method:'POST'})   -> ('/api/x/', 'POST')
#   new EventSource('/api/x/stream')         -> ('/api/x/stream', None)
# WebSocket is DELIBERATELY not an opener: a FastAPI WebSocketRoute carries no
# `.methods`, so build_api_contract() (which requires .methods) never emits it — a
# ws('/api/...') call would then match NO contract route and get falsely blocked. WS
# stays a false-negative (unscanned) until the contract can carry ws routes. EventSource
# is fine — an SSE endpoint is an ordinary GET route and IS in the contract.
_RE_API_CALL = re.compile(
    r"""(?:fetch|EventSource)\s*\(\s*['"`]"""               # opener + opening quote
    r"""(/api/[^'"`?#\s${]*)"""                             # 1: static path prefix (stops at ? or #)
    r"""([^)]*)""",                                         # 2: the rest of the call args
    re.IGNORECASE | re.DOTALL,
)
# HTTP method inside a fetch options object, extracted from group(2) of _RE_API_CALL.
_RE_METHOD = re.compile(r"""\bmethod\s*:\s*['"]([A-Za-z]+)['"]""", re.IGNORECASE)

# Strip comments BEFORE scanning so a documented/commented-out example
# (`// fetch('/api/foo')`, JSDoc route lists, `fetch('/api/...')` placeholders) is
# never mistaken for a live call — the gate must never falsely block a legit app.
# Over-stripping only drops calls (a safe false-negative), never invents a violation.
_RE_STRIP_COMMENTS = (
    re.compile(r"/\*.*?\*/", re.DOTALL),    # /* ... */ and /** JSDoc */
    re.compile(r"<!--.*?-->", re.DOTALL),   # <!-- HTML comments -->
    re.compile(r"(?<![:/])//[^\n]*"),       # // line comments (NOT :// in a URL)
)


# ---------------------------------------------------------------------------
# Contract generation (introspect the live app)
# ---------------------------------------------------------------------------

def _route_scope(route) -> Optional[str]:
    """The permission scope a route requires, or None. Walks the route's resolved
    dependency tree for the SCOPE_ATTR tag that requires_permission() stamps on its
    dependency callable. A route that takes any one of several scopes
    (requires_any_permission) answers its first; _route_scopes answers all."""
    scopes = _route_scopes(route)
    return scopes[0] if scopes else None


def _route_scopes(route) -> list[str]:
    """Every scope that admits a route, in its own order: one for
    requires_permission, each of them for requires_any_permission (its
    `_app_scopes`), none for an unscoped route."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return []
    stack = list(getattr(dependant, "dependencies", []) or [])
    seen = 0
    while stack and seen < 200:
        seen += 1
        d = stack.pop()
        call = getattr(d, "call", None)
        scope = getattr(call, SCOPE_ATTR, None)
        if scope:
            return list(getattr(call, SCOPES_ATTR, None) or (scope,))
        stack.extend(getattr(d, "dependencies", []) or [])
    return []


def template_to_regex(path: str) -> str:
    r"""FastAPI path template -> a full-match regex string.
    `/api/x/{id}` -> `^/api/x/[^/]+$`."""
    esc = re.escape(path)
    esc = re.sub(r"\\\{[^/]*?\\\}", r"[^/]+", esc)  # {param} -> one path segment
    return f"^{esc}$"


def _resolve_ref(schema: Any, spec: dict) -> dict:
    """Follow a single $ref into components/schemas; otherwise return the schema."""
    if isinstance(schema, dict) and "$ref" in schema:
        ref = str(schema["$ref"]).rsplit("/", 1)[-1]
        return ((spec.get("components") or {}).get("schemas") or {}).get(ref, {}) or {}
    return schema if isinstance(schema, dict) else {}


def _top_keys(schema: Any, spec: dict) -> Optional[list]:
    """Top-level property names of an (object) schema, resolving one $ref. Returns
    None when the shape is not a declared object (most raw-dict routes)."""
    s = _resolve_ref(schema, spec)
    props = s.get("properties")
    if isinstance(props, dict) and props:
        return sorted(props.keys())
    return None


def _op_schemas(op: dict, spec: dict) -> dict:
    req = None
    resp = None
    try:
        body = (((op.get("requestBody") or {}).get("content") or {})
                .get("application/json") or {}).get("schema")
        req = _top_keys(body, spec) if body else None
    except Exception:
        req = None
    try:
        for code in ("200", "201"):
            r = (op.get("responses") or {}).get(code)
            if not r:
                continue
            sch = ((r.get("content") or {}).get("application/json") or {}).get("schema")
            resp = _top_keys(sch, spec) if sch else None
            if resp is not None:
                break
    except Exception:
        resp = None
    return {"request_keys": req, "response_keys": resp}


def build_api_contract(app) -> dict:
    """Introspect a FastAPI `app` → the app-facing API contract (only `/api/` routes).
    Request/response key sets are pulled from the generated OpenAPI where a route
    declares a model; absent (None) for raw-dict routes. Never raises — degrades to a
    route+scope contract if OpenAPI generation fails."""
    schemas: dict[tuple, dict] = {}
    try:
        spec = app.openapi()
        for p, ops in (spec.get("paths") or {}).items():
            for method, op in (ops or {}).items():
                if not isinstance(op, dict):
                    continue
                schemas[(p, method.upper())] = _op_schemas(op, spec)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("api_contract: openapi introspection failed (route+scope only): %s", e)

    # Imported here, not at the top: scripts/test_app_conformance.py loads this
    # file by path, with no server package, for the parts that need no app.
    from server.route_table import effective_routes

    routes: list[dict] = []
    for r in effective_routes(app):
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods or not str(path).startswith("/api/"):
            continue
        # Skip routes the app deliberately keeps OUT of its schema. Exactly one
        # /api route sets include_in_schema=False today: the 404 catch-all
        # `@app.api_route("/api/{rest:path}")` (server/main.py) that turns an
        # unmatched /api call into a clean 404 instead of letting it fall through
        # to the SPA mount. It is not a callable endpoint — it is the absence of
        # one — and while it was in the contract BOTH conformance rules were dead:
        # its static prefix is "/api/", so every conceivable path prefix-matched it
        # (rule 1 could never see "no route matches") and every method matched an
        # extra unscoped row (rule 2's single-match requirement could never hold).
        # `include_in_schema=False` is the same predicate FastAPI uses to keep a
        # route out of /openapi.json, so anything excluded here is by definition
        # not part of the advertised surface an app may call.
        if getattr(r, "include_in_schema", True) is False:
            continue
        scopes = _route_scopes(r)
        scope = scopes[0] if scopes else None
        for m in sorted(methods):
            if m in ("HEAD", "OPTIONS"):
                continue
            sc = schemas.get((path, m), {})
            row = {
                "method": m,
                "path": path,
                "scope": scope,
                "request_keys": sc.get("request_keys"),
                "response_keys": sc.get("response_keys"),
            }
            if len(scopes) > 1:
                # Any one of these admits the call; `scope` is the first.
                row["scopes_any"] = scopes
            routes.append(row)
    routes.sort(key=lambda x: (x["path"], x["method"]))
    return {"version": 1, "count": len(routes), "routes": routes}


# ---------------------------------------------------------------------------
# Conformance gate (diff an app's own /api calls against the contract)
# ---------------------------------------------------------------------------

def _index(contract: dict) -> list[dict]:
    """Pre-compile the contract routes into matchers for the gate."""
    idx = []
    for r in contract.get("routes", []):
        idx.append({
            "method": r["method"],
            "path": r["path"],
            "regex": re.compile(template_to_regex(r["path"])),
            "scope": r.get("scope"),
            "scopes_any": r.get("scopes_any") or ([r["scope"]] if r.get("scope") else []),
        })
    return idx


def _extract_api_calls(project_dir: Path, exclude_top=frozenset()) -> list[tuple]:
    """Every own-origin /api call in the app's HTML/JS as (rel_file, path, method|None).
    `path` is the STATIC prefix (dynamic tails like `${id}` are dropped)."""
    calls: list[tuple] = []
    # Through the contained walk (tree_walk.scan_windows): a link out of the
    # project is never read, so a gate message can never quote one. Every
    # file is read, whatever its size or count; a big one in windows, where a
    # call counts in the window it starts in.
    from server.applications.tree_walk import scan_windows
    for rel, text, lo, hi in scan_windows(project_dir, suffixes=_SCAN_SUFFIXES,
                                          skip_dirs=_SCAN_SKIP_DIRS, exclude_top=exclude_top,
                                          window_bytes=_SCAN_WINDOW_BYTES):
        # Comments become spaces of the same length, so a call's position
        # still says which window it belongs to.
        for rgx in _RE_STRIP_COMMENTS:
            text = rgx.sub(lambda c: " " * len(c.group(0)), text)
        for m in _RE_API_CALL.finditer(text):
            if not lo <= m.start() < hi:
                continue
            path = (m.group(1) or "").split("?", 1)[0].rstrip()
            # "..." is an ellipsis placeholder, never a real route segment.
            if not path.startswith("/api/") or "..." in path:
                continue
            mm = _RE_METHOD.search(m.group(2) or "")
            method = mm.group(1).upper() if mm else None
            calls.append((rel, path, method))
    return calls


def _matches(idx: list[dict], path: str, method: Optional[str]) -> list[dict]:
    """Contract routes a concrete call could hit.

    EXACT WINS. When the call's path fully matches a route template, that route (and
    only routes it exactly matches) is the answer — a call to `/api/brain/memory` IS
    `/api/brain/memory`, not also `/api/brain/memory/semantic`. Without this the scope
    rule could almost never fire: every route that merely STARTS with the called path
    was counted as a candidate, so `len(method_match) == 1` was rarely true.

    Only when nothing matches exactly do we fall back to prefix candidates, which is
    the case the prefix rule exists for: a call whose static prefix is SHORTER than the
    route because a dynamic tail was stripped (`fetch(`/api/x/${id}`)` -> `/api/x/`), so
    we never false-flag it as nonexistent when `/api/x/{id}` exists.
    """
    exact_hits: list[dict] = []
    prefix_hits: list[dict] = []
    for r in idx:
        if method is not None and method != r["method"]:
            continue
        if r["regex"].match(path) is not None:
            exact_hits.append(r)
        elif r["path"].startswith(path) or path.startswith(r["path"].split("{", 1)[0]):
            prefix_hits.append(r)
    return exact_hits or prefix_hits


def check_conformance(project_dir: Path, manifest, app, exclude_top=frozenset()) -> list[str]:
    """Diff the app's own `/api/...` calls against the LIVE route contract (introspected
    from `app`). Never crashes a build — degrades to [] on any error."""
    try:
        contract = build_api_contract(app)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("api_contract: contract build failed, conformance skipped: %s", e)
        return []
    return conformance_against(project_dir, manifest, contract, exclude_top)


def conformance_against(project_dir: Path, manifest, contract: dict,
                        exclude_top=frozenset()) -> list[str]:
    """Diff the app's own `/api/...` calls against a given contract dict. Returns
    violation strings ([] = ok). Conservative: only clear true-positives.

      1. ROUTE EXISTS   — a call whose static prefix matches NO route (any method) is
                          a typo / nonexistent endpoint.
      2. SCOPE DECLARED — when a call's method is explicit and matches exactly ONE
                          scoped route, the manifest must declare that scope.

    Split from check_conformance so the diff logic is unit-testable without a live app
    (pass a hand-built contract)."""
    errors: list[str] = []
    try:
        idx = _index(contract)
        if not idx:
            return errors  # nothing to check against — never invent a violation
        declared = {str(s) for s in (getattr(manifest, "permissions", []) or [])}
        seen_missing: set = set()
        for rel, path, method in _extract_api_calls(Path(project_dir), exclude_top):
            any_match = _matches(idx, path, None)
            if not any_match:
                key = ("route", path)
                if key not in seen_missing:
                    seen_missing.add(key)
                    errors.append(
                        f"{rel}: calls {path!r} but no server route matches it — "
                        f"a typo or a nonexistent endpoint (check GET /api/applications/api-contract)"
                    )
                continue
            # scope check only when we can be certain: explicit method, one match, scoped.
            method_match = _matches(idx, path, method) if method else []
            if len(method_match) == 1 and method_match[0]["scope"]:
                need = method_match[0]["scope"]
                if not any(sc in declared for sc in method_match[0]["scopes_any"]):
                    key = ("scope", path, need)
                    if key not in seen_missing:
                        seen_missing.add(key)
                        anyof = method_match[0]["scopes_any"]
                        need_words = (repr(need) if len(anyof) < 2
                                      else "one of " + ", ".join(repr(x) for x in anyof))
                        errors.append(
                            f"{rel}: {method} {path!r} requires scope {need_words} but the "
                            f"manifest permissions[] don't declare it (it will 403 at runtime)"
                        )
    except Exception as e:  # pragma: no cover - the gate must never crash a build
        log.warning("api_contract: conformance scan skipped (non-fatal): %s", e)
    return errors
