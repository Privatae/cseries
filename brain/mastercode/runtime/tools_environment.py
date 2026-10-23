"""The environment tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import json
import logging
import re
from pathlib import Path

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.entity_runtime")


# (_exec_system_list_apps deleted — tool retired, branch removed with it;
#  explore_environment covers the same read.)


# ── Environment knowledge handlers (host-environment awareness) ─────
# Read-only. The host reads its OWN installed apps + each app's code-true
# catalog facts (manifest prompt_behavior) so it can tell the user, in plain
# language, what it has and what each thing does — the depth the lean "Your
# Environment" prompt block points at.

def _first_sentence(text: str) -> str:
    import re as _re
    text = _re.sub(r"\s+", " ", (text or "").strip())
    m = _re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


async def _exec_explore_environment(args: dict) -> dict:
    """List this host's installed apps + a one-line summary of each."""
    try:
        from server.applications import registry, manifest as _manifest
        from server import brain_service
        from pathlib import Path as _Path
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        apps = await registry.list_active(pool)
        out = []
        for a in apps:
            summary = ""
            if getattr(a, "source_dir", None):
                try:
                    m = _manifest.load(_Path(a.source_dir) / "app.yaml")
                    summary = _first_sentence(m.prompt_behavior.what_it_is)
                except Exception:
                    summary = ""
            out.append({"app_id": a.id, "name": a.name, "summary": summary})
        return {"status": "ok", "result": {
            "apps": out,
            "hint": "Call describe_app(app_id) for what any of these does and where the user finds it.",
        }}
    except Exception as e:
        return _refused_if_caller(e, f"explore_environment failed: {e}")


async def _exec_describe_app(args: dict) -> dict:
    """Describe one installed app from its catalog facts (manifest
    prompt_behavior: what_it_is / user_path / step_up)."""
    app_id = ((args or {}).get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    try:
        from server.applications import registry, manifest as _manifest
        from server import brain_service
        from pathlib import Path as _Path
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        apps = await registry.list_active(pool)
        match = next((a for a in apps if a.id == app_id), None)
        if match is None:
            return {"status": "ok", "result": {
                "app_id": app_id, "installed": False,
                "note": "Not installed on this host. Call explore_environment to see what is.",
            }}
        result = {"app_id": match.id, "name": match.name, "installed": True,
                  "provenance": getattr(match, "tier", None)}
        if getattr(match, "source_dir", None):
            try:
                m = _manifest.load(_Path(match.source_dir) / "app.yaml")
                pb = m.prompt_behavior
                result.update({
                    "what_it_is": pb.what_it_is,
                    "where_to_find_it": pb.user_path,
                    "needs_user_approval": bool(pb.step_up),
                    "approval_note": pb.step_up,
                    # How to work in an app is a skill (find_tools, kind
                    # skill), not a manifest field.
                })
            except Exception:
                pass
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"describe_app failed: {e}")


async def _exec_check_page(args: dict) -> dict:
    """Load one of the host's own app pages in the real browser and report
    what a browser sees.

    Origin: a user said an app's UI did not work.
    The host had no way to look: its only web tools are `browser_agent`
    (replays what the USER recorded) and `web_search`. The Browser app's own
    card said it plainly — "you have no tool that reads the viewport" — so
    the host opened a browser it could not read, and fell back to asking the
    user to hard-refresh, four times, over a day and a half. The platform
    owned the capability the whole time (sidecars/browser/control/verify.py)
    and only the delivery proof could reach it.

    This is the TROUBLESHOOTING door onto that same check, held by the host.
    It is deliberately NOT the delivery gate: the proof stays behind
    behavioral_params.browser_proof (a cost decision), while this runs when a
    user reports a broken page and the host needs to SEE it. One wake, one
    load, released immediately — the same one-shot shape the proof uses.
    """
    app_id = ((args or {}).get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    path = ((args or {}).get("path") or "").strip()
    try:
        from server.applications import registry, manifest as _manifest
        from server import brain_service
        from pathlib import Path as _Path
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        apps = await registry.list_active(pool)
        match = next((a for a in apps if a.id == app_id), None)
        if match is None:
            return {"status": "ok", "result": {
                "app_id": app_id, "installed": False,
                "note": "Not installed on this host. Call explore_environment "
                        "to see what is.",
            }}
        # `path` lets the host look at a DEEP route, which is the whole point:
        # on one Host the entrypoint rendered perfectly while /trade and
        # /dashboard were empty. Default to the manifest entrypoint.
        target = path
        if not target:
            target = "index.html"
            if getattr(match, "source_dir", None):
                try:
                    m = _manifest.load(_Path(match.source_dir) / "app.yaml")
                    target = m.entrypoint or "index.html"
                except Exception:
                    pass
        from mastercode.runtime.app_verify import _browser_verify
        # require_html=False: `path` is a ROUTE, not a file. See _browser_verify.
        rep = await _browser_verify(app_id, target, require_html=not path)
        if not rep.get("checked"):
            # Honest third state, same as the proof: not checked is never a
            # pass. Say why, so the host reports a real blocker instead of
            # guessing at the page.
            return {"status": "ok", "result": {
                "app_id": app_id, "checked": False,
                "reason": rep.get("reason"),
                "note": "The browser could not be asked, so nothing is known "
                        "about this page. Do not report it as working.",
            }}
        out = {
            "app_id": app_id, "checked": True,
            "url": rep.get("page_url"), "ok": rep.get("ok"),
            "verdict": rep.get("why"),
            "http_status": rep.get("http_status"),
            "title": rep.get("title"),
        }
        # Only the signals that FIRED, so a clean page reads short.
        for k in ("escaped_mount", "api_failures", "api_notes", "failed_resources",
                  "uncaught", "console_errors", "routes_walked",
                  "blocked_resources"):
            if rep.get(k):
                out[k] = rep[k]
        return {"status": "ok", "result": out}
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"check_page failed: {e}")


async def _exec_open_app(args: dict) -> dict:
    """Open an app window for the user — from ANY execution context.

    Two delivery paths, deliberately redundant (AppRunner re-focuses instead
    of double-opening, so both firing is harmless):
      1. The /ws broadcast (server-side, same channel the MCP-lane open_app
         at server/routes/mcp.py uses). This is what makes the window open
         when the call happens OUTSIDE a streamed chat turn — a dispatched
         SDK agent, a background loop. The first artifact live-test
 failed exactly here: the agent called open_app, no
         chat client ever saw the tool frame, no window.
      2. The chat clients' stream relay on the tool-CALL frame (Circles /
         Workbench / dashboard chat) — kept because it also carries `view`
         through surfaces where the shell ws might not be connected."""
    app_id = ((args or {}).get("app_id") or "").strip()
    view = ((args or {}).get("view") or "").strip()
    if not app_id:
        return _refused("app_id is required")
    try:
        from server.applications import registry
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        apps = await registry.list_active(pool)
        match = next((a for a in apps if a.id == app_id), None)
        if match is None:
            return {"status": "ok", "result": {
                "app_id": app_id, "opened": False,
                "note": ("Not installed on this host — can't open it. Say so; it may be "
                         "in the App Store, where it can be installed when the user's "
                         "request is to have it."),
            }}
        # Server-side push — works regardless of which loop ran this tool.
        try:
            from server.main import broadcast as ws_broadcast
            data = {"app_id": match.id}
            if view:
                data["view"] = view
            await ws_broadcast({"type": "open_app", "module": "windows", "data": data})
        except Exception as be:
            # Non-fatal: the stream relay may still open it on chat surfaces.
            log.debug("open_app ws broadcast failed (non-fatal): %s", be)
        result = {
            "app_id": match.id, "name": match.name, "opened": True,
            "note": "Opening it on the user's screen now — tell them it's open.",
        }
        if view:
            result["view"] = view
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"open_app failed: {e}")


_IMAGE_BOT_MOD = None


def _load_image_bot():
    """The image engine. Was load-by-path out of public_bots/ because the
    `00100001_` filename prefix made it un-importable by name; it is
    mastercode.image_engine and imports normally."""
    global _IMAGE_BOT_MOD
    if _IMAGE_BOT_MOD is None:
        from mastercode import image_engine
        _IMAGE_BOT_MOD = image_engine
    return _IMAGE_BOT_MOD


async def _exec_image_generate(args: dict) -> dict:
    """Generate, look at, or browse images via the host's image engine. Wraps the
    image engine (mastercode/image_engine.py) with its own context object.
    generate runs on the user's OWN routed image model and key — the 'image_gen'
    role — with no platform key and no platform charge: the platform billing
    path is removed (image_engine.py header). An unrouted role
    returns a "set one up" message, not a billing refusal. Actions:
      generate -> create image(s) from a prompt (prompt; optional n — passed to
                  the provider as asked, it refuses past its own maximum;
                  optional size, quality)
      look     -> the Host's own model looks at pictures (images: a list of
                  /api/images/<id>, gallery entries, file:<name>, http(s) urls;
                  question). Its own pictures are shown in its own turn
                  (runtime/turn_pictures); with a picture from the web among
                  them, one call of its own model with no tools answers the
                  question instead. There is no vision route: image
                  understanding is the host's own.
      gallery  -> list the host's images newest first, paged (offset, limit;
                  total + next_offset) (free, local read)
    """
    a = args or {}
    action = (a.get("action") or a.get("op") or "generate").strip()
    if action == "understand":
        return _refused("action='understand' was removed 2026-10-12: your own model looks at "
                        "pictures. Use action='look' images=[...] (and question= for a "
                        "picture from the web)")
    if action not in ("generate", "look", "gallery"):
        return _refused(f"action must be one of generate/look/gallery (got {action!r})")
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "image_generate failed: pool unavailable"}
        eid = brain_service._entity_id
        mod = _load_image_bot()
        # NOTE: the raw pool, exactly as before — this handler never wrapped the
        # engine in a SandboxedPool. Preserved deliberately; changing it here
        # would be an unrelated behaviour change riding a retirement.
        ctx = mod.EngineContext(
            entity_id=eid, entity_name=f"entity_{eid}",
            capabilities=["read_context", "external_api"], pool=pool,
            args={"action": action,
                  "prompt": a.get("prompt", ""), "n": a.get("n", 1),
                  "size": a.get("size"), "quality": a.get("quality"),
                  "images": a.get("images"), "question": a.get("question", ""),
                  "offset": a.get("offset"), "limit": a.get("limit")},
        )
        res = await mod.run(ctx)
        # A picture the Host looked at marks its turn as the picture's source
        # would have (runtime/turn_taint): an app's render is MCP output, a
        # file the person gave it is an upload, a picture from the web is the
        # web — even though a web picture was only seen by a call with no tools,
        # its answer is the web's words.
        _origins = (res or {}).pop("origins", None) or []
        if _origins:
            from mastercode.runtime import turn_taint as _tt
            _marks = {"app": (_tt.MCP, "a picture an app returned"),
                      "upload": (_tt.UPLOAD, "a picture the person gave the Host"),
                      "web": (_tt.WEB, "a picture from the web")}
            for _o in _origins:
                if _o in _marks:
                    _tt.mark(*_marks[_o])
        if (res or {}).get("status") == "ok":
            return {"status": "ok", "result": {k: v for k, v in res.items() if k != "status"}}
        return {"status": "error", "result": (res or {}).get("result") or "image op failed"}
    except Exception as e:
        return _refused_if_caller(e, f"image_generate failed: {getattr(e, 'detail', None) or e}")


_CAPS_DOC_DIR = Path("/app/docs")

# ── capability-card index ─────────────────────────────────────────────
# capabilities.json carries a `capability_cards` corpus baked at image
# build. It is the PRIMARY search index: the markdown renders most of the
# surface as bare identifiers (route prefixes, tool names) with no human
# words, so a literal doc grep for a user's plain phrasing ("voice",
# "screenshot") misses capabilities that ship. The cards carry the words.
#
# TWO shapes exist in the wild and both are handled:
#   rich   {id, kind, title, what, when[], how, returns, gotchas[],
#           related[], keywords[]}
#   legacy {name, lane, category, purpose, keywords, parameters, read_ops}
# Anything else is normalised best-effort. A card that cannot be read is
# skipped, never raised — this tool degrades and SAYS SO in the result.
_CAPS_MIN_SCORE = 25          # below this a "hit" is noise, not a match
_CAPS_WEAK_SCORE = 50         # below this the hits are candidates, not answers
# Filler that carries no capability meaning. Without this, term-coverage
# scoring counted "the", "you", "what" as matched terms, so a sentence made
# mostly of filler reached full coverage and was presented as an answer.
# `_caps_tokens` already drops anything <=2 chars, so this only needs the
# longer ones.
_CAPS_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "yours", "can", "could", "would",
    "will", "with", "how", "what", "when", "where", "why", "who",
    "are", "was", "were", "have", "has", "had", "please", "help", "want",
    "need", "give", "get", "got", "make", "does", "did", "this", "that",
    "these", "those", "from", "into", "about", "some", "any", "all", "let",
    "our", "out", "not", "but", "his", "her", "their", "them", "they",
})
_CAPS_CARD_LIMIT = 8          # cards returned by op='search' (override: limit)
_CAPS_CARD_LIMIT_MAX = 25
_CAPS_DOC_LIMIT = 80          # doc lines returned by op='search'
_CAPS_ID_LIST_LIMIT = 400     # ids returned by op='card' with no id
_CAPS_SECTION_LIMIT = 40_000  # chars returned by op='section'


def _caps_as_list(v) -> list[str]:
    """Coerce a card field to a list of non-empty strings. Never raises."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple, set)):
        out: list[str] = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                t = x.get("phrase") or x.get("text") or x.get("name") or x.get("id")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
            elif x is not None:
                out.append(str(x))
        return out
    return [str(v)]


def _caps_stem(t: str) -> str:
    """Crude singulariser so 'screenshots' finds 'screenshot'."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("ses"):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _caps_tokens(s: str) -> set[str]:
    return {_caps_stem(t) for t in re.findall(r"[a-z0-9_]+", str(s or "").lower())
            if len(t) > 2}


def _caps_normalize_card(raw) -> dict | None:
    """Best-effort normalisation of one card of EITHER shape → the rich shape.
    Returns None for anything unusable rather than raising."""
    if not isinstance(raw, dict):
        return None
    legacy = "id" not in raw and "name" in raw
    cid = str(raw.get("id") or raw.get("name") or raw.get("slug") or "").strip()
    title = str(raw.get("title") or raw.get("name") or cid).strip()
    what = str(raw.get("what") or raw.get("purpose") or raw.get("description") or "").strip()
    if not (cid or title or what):
        return None
    card: dict = {
        "id": cid or title,
        "kind": str(raw.get("kind") or ("op" if legacy else "") or "unknown").strip(),
        "title": title or cid,
        "what": what,
        "when": _caps_as_list(raw.get("when") or raw.get("user_phrasings")),
        "how": raw.get("how") or raw.get("example") or "",
        "returns": raw.get("returns") or "",
        "gotchas": _caps_as_list(raw.get("gotchas")),
        "related": _caps_as_list(raw.get("related")),
        "keywords": [k.lower() for k in _caps_as_list(raw.get("keywords"))],
    }
    # Legacy extras — a flat-card image still answers usefully with these.
    for k in ("lane", "category", "read_ops"):
        if raw.get(k):
            card[k] = raw[k]
    # A wiki page card carries its public link, so the host can hand it over.
    if card["kind"] == "page" and raw.get("url"):
        card["url"] = str(raw["url"])
    params = raw.get("parameters")
    if isinstance(params, dict) and isinstance(params.get("properties"), dict):
        card["params"] = sorted(params["properties"])
    missing = [f for f in ("what", "when", "how") if not card.get(f)]
    if missing:
        card["_incomplete"] = missing
    return card


_CAPS_PAGE_EXCERPT = 900      # chars of a wiki page's text in a search hit


def _caps_search_view(card: dict) -> dict:
    """A search hit as returned. A wiki page's `how` is the whole page, and
    eight of them would crowd out everything else in the result, so a hit
    carries the opening and op='card' returns the rest."""
    if card.get("kind") != "page":
        return card
    how = str(card.get("how") or "")
    if len(how) <= _CAPS_PAGE_EXCERPT:
        return card
    cut = how[:_CAPS_PAGE_EXCERPT].rsplit("\n", 1)[0].rstrip()
    return dict(card, how=cut + f"\n\n… (the whole page: op='card', id='{card.get('id')}')")


def _caps_load_cards(js_path: Path) -> tuple[list[dict], str, str]:
    """→ (cards, shape, degraded_note). Never raises.
    shape: 'cards' (rich) | 'flat-legacy' | 'absent' | 'unreadable'."""
    try:
        data = json.loads(js_path.read_text())
    except FileNotFoundError:
        return [], "absent", "capabilities.json is not baked into this image — doc lines only"
    except Exception as e:
        return [], "unreadable", f"capabilities.json could not be parsed ({e}) — doc lines only"
    raw = data.get("capability_cards") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return [], "absent", ("this image's capabilities.json carries no capability_cards — "
                              "doc lines only, so plain-word misses are expected")
    cards: list[dict] = []
    for r in raw:
        c = _caps_normalize_card(r)
        if c:
            cards.append(c)
    if not cards:
        return [], "unreadable", "capability_cards present but no card was readable — doc lines only"
    rich = sum(1 for c in cards if c.get("when") or c.get("how"))
    if rich:
        return cards, "cards", ""
    return cards, "flat-legacy", ("older flat card shape: cards carry a purpose and keywords but "
                                  "no worked example or user phrasings — 'how' is unavailable here")


def _caps_score_card(card: dict, q: str, q_tokens: set[str]) -> tuple[int, str]:
    """Rank one card against the query. exact id/title > `when` phrase >
    keyword > prose substring > partial term coverage."""
    best, why = 0, ""

    def bump(score: int, reason: str) -> None:
        nonlocal best, why
        if score > best:
            best, why = score, reason

    def norm(s: str) -> str:
        return re.sub(r"[._\-/]+", " ", str(s or "").lower()).strip()

    cid, title = str(card.get("id", "")).lower(), str(card.get("title", "")).lower()
    ids = {cid, title, norm(cid), norm(title)}
    ids.discard("")
    if q in ids:
        bump(100, "exact id/title")
    elif any(q in s for s in ids):
        bump(72, "id/title")
    for phrase in card.get("when") or []:
        p = phrase.lower().strip()
        if not p:
            continue
        if q == p:
            bump(90, "when-phrase (exact)")
        elif p in q:
            # The user's sentence contains this whole phrasing. A MULTI-WORD
            # phrase is specific enough to be an answer. A SINGLE common word
            # is not, and treating it as one produced confident wrong answers
            # (measured): "restore my antique chair" hit the backup
            # area on `restore`, "write a speech for my wedding" hit realtime
            # on `speech`, "buy tickets to a concert" hit support tickets.
            # A lone word is only an answer when it is essentially the whole
            # query ("voice"); inside a sentence it is a candidate, so it
            # lands below _CAPS_WEAK_SCORE and the host offers it as such.
            # Defended here as well as in the generator because the corpus is
            # data and this is the rule that data cannot violate.
            if len(p.split()) >= 2 or len(q.split()) <= 2:
                bump(78, "when-phrase")
            else:
                bump(48, "when-phrase (single word in a sentence)")
        elif q in p:
            # the query is only a fragment of the phrasing — weaker than an
            # exact keyword hit, so 'screenshot' prefers the screenshot card
            # over a card that merely mentions screenshots in a `when` line.
            bump(52, "when-phrase (partial)")
    kws = {str(k).lower() for k in (card.get("keywords") or [])}
    if q in kws:
        bump(56, "keyword")
    prose_parts = [card.get("what"), card.get("returns"), card.get("how"),
                   card.get("kind"), card.get("category"), card.get("lane")]
    prose_parts += list(card.get("gotchas") or []) + list(card.get("related") or [])
    prose = " ".join(str(p) for p in prose_parts if p).lower()
    if q and q in prose:
        bump(30, "prose")
    if q_tokens:
        strong_hay = _caps_tokens(" ".join([cid, title] + list(card.get("when") or [])))
        strong_hay |= {_caps_stem(k) for k in kws}
        hay = strong_hay | _caps_tokens(prose)
        # Scattered term overlap is the WEAKEST evidence there is — it says
        # some words co-occur, not that the card is the answer. It used to top
        # out at exactly _CAPS_WEAK_SCORE, so full coverage was presented as an
        # answer: "delete my account" landed confidently on the Orchestrator
        # and "send a text message" on contacts_manage (SMS is deliberately
        # not built). Cap it strictly BELOW the answer floor so this arm can
        # only ever offer candidates. Stopwords are excluded so filler like
        # "the/you/what/are" cannot inflate coverage.
        hit = (q_tokens - _CAPS_STOPWORDS) & hay
        denom = len(q_tokens - _CAPS_STOPWORDS)
        if hit and denom:
            cov = len(hit) / denom
            base = (_CAPS_WEAK_SCORE - 2) if (q_tokens & strong_hay) else 26
            bump(int(base * cov), "terms " + ",".join(sorted(hit)))
    return best, why


def _caps_doc_search(lines: list[str], q: str, q_tokens: set[str],
                     cap: int) -> tuple[list[str], int, str]:
    """Secondary index: the markdown lines. → (hits, total, mode)."""
    def scan(pred) -> tuple[list[str], int]:
        hits: list[str] = []
        total, head = 0, ""
        for ln in lines:
            if ln.startswith("## "):
                head = ln[3:].strip()
                continue
            if pred(ln):
                total += 1
                if len(hits) < cap:
                    hits.append(f"[{head}] {ln.strip()}")
        return hits, total

    hits, total = scan(lambda ln: q in ln.lower())
    if total:
        return hits, total, "substring"
    if len(q_tokens) > 1:
        hits, total = scan(lambda ln: q_tokens <= _caps_tokens(ln))
        if total:
            return hits, total, "all-terms"
    return [], 0, "substring"


async def _exec_capabilities(args: dict) -> dict:
    """Read the generated platform-capabilities index (docs/capabilities.json's
    capability_cards + docs/CAPABILITIES.md, baked at image build — version-exact
    by construction). THE first stop for any 'can Privatae Host do X?' question.

    op='search' searches the CARDS first (id/title/when-phrases/keywords/prose)
    and falls back to the markdown lines. A miss means THESE WORDS were not
    found — not that the platform lacks the capability: the markdown renders
    most of the route surface as bare identifiers, so plain-word misses are
    routine. Say what you searched; never convert a vocabulary miss into a
    denial. Read-only, in-pod file access only."""
    a = args or {}
    op = (a.get("op") or "overview").strip().lower()
    md_path = _CAPS_DOC_DIR / "CAPABILITIES.md"
    js_path = _CAPS_DOC_DIR / "capabilities.json"
    if not md_path.exists():
        return _refused("capabilities doc not baked in this image (pre-capabilities build) "
                          "— answer from describe_app/explore_environment and say the doc is absent")
    try:
        md = md_path.read_text()
        lines = md.splitlines()
        heads = [ln for ln in lines if ln.startswith("## ")]
        if op == "overview":
            data = {}
            try:
                data = json.loads(js_path.read_text())
            except Exception:
                pass
            cards, shape, degraded = _caps_load_cards(js_path)
            return {"status": "ok", "result": {
                "version": data.get("version") or (lines[0] if lines else ""),
                "sections": heads,
                "section_count": len(heads),
                "counts": {
                    "api_routes": (data.get("api_contract") or {}).get("count"),
                    "host_ops": len(data.get("host_ops") or []),
                    "scopes": len(data.get("scopes") or []),
                    "image_apps": len(data.get("apps") or []),
                    "capability_cards": len(cards),
                },
                "card_index": {"shape": shape, "count": len(cards),
                               "degraded": degraded},
                "rule": ("search the cards first (op='search', plain user words). A hit is a YES — "
                         "cite the card. A miss means those WORDS were not found in the cards or "
                         "the doc; retry with the user's own phrasing and check op='api_routes' "
                         "before saying anything is unsupported."),
            }}
        if op == "section":
            want = str(a.get("section") or "").strip().lower()
            if not want:
                return _refused(f"section required (1-{len(heads)} or a heading substring) "
                                  f"— op='overview' lists them")
            out: list[str] = []
            grab = False
            picked = ""
            for ln in lines:
                if ln.startswith("## "):
                    if grab:
                        break
                    head = ln[3:].strip().lower()
                    grab = head.startswith(f"{want}.") or want in head
                    if grab:
                        picked = ln[3:].strip()
                if grab:
                    out.append(ln)
            if not out:
                return _refused(f"no section matches '{want}' — op='overview' lists all "
                                  f"{len(heads)} sections")
            text = "\n".join(out)
            return {"status": "ok", "result": {
                "section": picked,
                "text": text[:_CAPS_SECTION_LIMIT],
                "chars": len(text),
                "truncated": len(text) > _CAPS_SECTION_LIMIT,
            }}
        if op == "card":
            cards, shape, degraded = _caps_load_cards(js_path)
            want = str(a.get("id") or a.get("q") or "").strip().lower()
            if not cards:
                return {"status": "error",
                        "result": f"no capability cards in this image ({degraded}) — "
                                  f"use op='search' (doc lines) or op='api_routes'"}
            if not want:
                listed = sorted(cards, key=lambda c: str(c.get("id", "")))
                return {"status": "ok", "result": {
                    "count": len(cards), "shape": shape, "degraded": degraded,
                    "cards": [{"id": c["id"], "kind": c["kind"], "title": c["title"]}
                              for c in listed[:_CAPS_ID_LIST_LIMIT]],
                    "truncated": len(cards) > _CAPS_ID_LIST_LIMIT,
                    "note": "op='card' with id=<one of these> returns the whole card",
                }}
            exact = [c for c in cards
                     if str(c.get("id", "")).lower() == want or str(c.get("title", "")).lower() == want]
            if exact:
                return {"status": "ok", "result": {"cards": exact, "exact": True,
                                                   "shape": shape, "degraded": degraded}}
            q_tokens = _caps_tokens(want)
            scored = []
            for c in cards:
                s, why = _caps_score_card(c, want, q_tokens)
                if s > 0:
                    scored.append((s, why, c))
            scored.sort(key=lambda t: (-t[0], str(t[2].get("id", ""))))
            # Near matches are candidates, so a page among them carries its
            # opening like a search hit; its exact id returns the whole page.
            near = [_caps_search_view(dict(c, match=why, score=s)) for s, why, c in scored[:5]]
            return {"status": "ok", "result": {
                "cards": near, "exact": False, "shape": shape, "degraded": degraded,
                "note": (f"no card has id/title '{want}'"
                         + ("; nearest by relevance above" if near
                            else "; nothing scored — try op='search' with plain words")),
            }}
        if op == "search":
            q = str(a.get("q") or "").strip().lower()
            if not q:
                return _refused("q required for op='search'")
            try:
                limit = int(a.get("limit") or _CAPS_CARD_LIMIT)
            except Exception:
                limit = _CAPS_CARD_LIMIT
            limit = max(1, min(limit, _CAPS_CARD_LIMIT_MAX))
            cards, shape, degraded = _caps_load_cards(js_path)
            q_tokens = _caps_tokens(q)
            scored = []
            for c in cards:
                s, why = _caps_score_card(c, q, q_tokens)
                if s >= _CAPS_MIN_SCORE:
                    scored.append((s, why, c))
            scored.sort(key=lambda t: (-t[0], str(t[2].get("id", ""))))
            card_hits = [_caps_search_view(dict(c, match=why, score=s))
                         for s, why, c in scored[:limit]]
            hits, doc_total, doc_mode = _caps_doc_search(lines, q, q_tokens, _CAPS_DOC_LIMIT)
            searched = {"cards": len(cards), "card_shape": shape,
                        "doc_lines": len(lines), "doc_sections": len(heads),
                        "doc_match_mode": doc_mode}
            if degraded:
                searched["degraded"] = degraded
            if card_hits:
                if card_hits[0]["score"] >= _CAPS_WEAK_SCORE:
                    note = ("card hits are the answer — quote the card's `what`, use its `how`, "
                            "and read its `gotchas` before you promise anything")
                else:
                    # Do NOT say "none is a real hit" — that is frequently
                    # false. A correct card lands here whenever the user's
                    # phrasing was a lone word inside a longer sentence, which
                    # is the common case ("take a screenshot of this page").
                    # Telling the host the top candidate is wrong makes it
                    # deny a capability it just found.
                    note = ("CANDIDATES, not a confirmed answer — the top one may well be "
                            "right. Open it with op='card' to confirm, or check "
                            "op='api_routes', before you answer yes or no")
                if shape == "flat-legacy":
                    note += "; this image's cards are the older flat shape (no `how`/`when`)"
            elif doc_total:
                note = ("no card matched, but the doc did — these are raw doc lines, so confirm "
                        "with op='section' or op='api_routes' before answering")
            else:
                note = (f"no match for '{q}' in the capability cards ({len(cards)} searched) or in "
                        f"CAPABILITIES.md ({len(lines)} lines, {len(heads)} sections). That means "
                        f"THESE WORDS were not found — it is NOT proof the capability is missing. "
                        f"Most of the doc lists bare route prefixes and tool names with no plain "
                        f"words. Retry with the user's own phrasing or a synonym, and check "
                        f"op='api_routes' (prefix='/api/...'). Only after that, say what you "
                        f"searched and that you could not find it — never that it cannot be done.")
                if degraded:
                    note += f" NOTE: {degraded}."
            return {"status": "ok", "result": {
                "query": q,
                "cards": card_hits,
                "card_count": len(scored),
                "cards_truncated": len(scored) > len(card_hits),
                "matches": hits,
                "count": doc_total,
                "truncated": doc_total > len(hits),
                "searched": searched,
                "note": note,
            }}
        if op == "api_routes":
            data = json.loads(js_path.read_text())
            routes = (data.get("api_contract") or {}).get("routes") or []
            prefix = str(a.get("prefix") or "").strip()
            if prefix:
                routes = [r for r in routes if str(r.get("path", "")).startswith(prefix)]
            return {"status": "ok", "result": {"count": len(routes), "routes": routes[:200],
                    "truncated": len(routes) > 200}}
        return _refused(f"unknown op '{op}' — use overview|section|search|card|api_routes")
    except Exception as e:
        return _refused_if_caller(e, f"capabilities failed: {e}")
