"""Acceptance-driven preference profiles — per-project + user-level taste store.

The adaptive-merge loop records EVERY human decision
(pick / merge-graft + free-form note + per-element picks) to
/var/lib/cseries/harness-preferences.jsonl. This module ACCUMULATES those decisions
into compact, readable profiles so a future fan_out can be pre-biased toward the
operator's demonstrated taste BEFORE round 1 runs.

PRINCIPLE (hard rule, see MEMORY: no LLM judges LLM / decision-substrate):
  We UNDERSTAND from acceptance. We NEVER judge or rank by model opinion.
  A profile is a tally of what the human ACCEPTED (picked base, grafted-from,
  per-element picks) + the verbatim preference notes/style cues they wrote — not
  a score the host invented. Higher counts = "the operator chose this more
  often", a frequency fact, not a quality verdict.

STATED preferences — the second channel. Acceptance only fires when
the user goes through review /pick or /merge, so everything they SAY ("never use
amazon nova", "I liked the colours in that one", "codex's layout is wrong") used
to land nowhere and every new project started blind. `record_stated` gives those
words a home in the SAME two scopes. This is not the host judging a model: the
user said it, verbatim, and it is stored as their words.

Three polarities, and they are NOT the same kind of thing:
  like / dislike  — a BIAS. Folded into the prompt summary, scoped as given.
  ban             — a RULE. Always GLOBAL (a "never use X" is not project-local),
                    and enforced at dispatch by `ban_for` before a pod wakes.
A bias argues; a rule refuses. Keeping them separate is the whole point — the
existing "bias, not a judge" doctrine holds for taste, and a ban is not taste.

DESIGN TOKENS — the user's own token-level deltas ON TOP of the
Host's own desktop design (the one every artifact wears,
server/applications/artifact_style.py): the handful of values they keep
restating. The base was once a per-project design system from the
asset catalog; that catalog and the project column naming it were removed
(migrations/v206), so the desktop is the only base there is. Resolved
global-then-project by `design_tokens_for` so a coder can be handed one flat
dict at dispatch. Storage + resolve only.

Stores (plain JSON, human-readable, append-merge on every decision):
  /var/lib/cseries/preference-profiles/project-<id>.json   (per Workbench project)
  /var/lib/cseries/preference-profiles/user.json           (cross-project, this entity)

A profile shape:
  {
    "scope": "project:7" | "user",
    "decisions": <int>,                  # total accepted decisions folded in
    "picked": {"<harness>": <int>, ...}, # base/winner the human chose
    "grafted_from": {"<harness>": <int>, ...}, # harnesses good bits were pulled FROM
    "element_picks": {"<harness>": <int>, ...},# per-element wins (feature → harness)
    "notes": ["<verbatim preference note>", ...],  # capped, most-recent-last
    "style_cues": {"<cue>": <int>, ...}, # light keyword tally over the notes
    "stated": {                          # things the user SAID (not inferred)
      "like":    {"<target>": {"subject": str, "count": int, "texts": [...]}},
      "dislike": {"<target>": {...}},
    },
    "bans": {"<target>": {"subject": str, "reason": str, "at": <epoch>}},  # user.json only
    # The model a dispatched agent runs on, per kind of untrusted work
    # (mastercode/agent_work.WORK_CLASSES). user.json only, like bans.
    "work_models": {"<work>": {"model": str, "by": "person"|"host", "text": str, "at": <epoch>}},
    "design_tokens": {"<token>": "<value>", ...},
    "updated_at": <epoch>,
    "first_seen": <epoch>
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("cseries.review.profile")

PROFILES_ROOT = Path("/var/lib/cseries/preference-profiles")

# What the user SAID is kept whole and kept forever. The store once deleted the 26th note FIFO,
# kept 5 texts per target, cut every note and stated text at 500 chars and every
# design-token value at 200 — all silently, so a preference the user gave could
# vanish with nothing said. The store is a JSON file on the Host's own disk; the
# real limit on it is that disk, not a count.
#
# What IS bounded is the summary RENDERED into a build brief — that is prompt,
# not storage. Every liked and disliked target is always NAMED there, and every
# design token is rendered (they are instructions to honour exactly). Only the
# quoted words — the latest text per target and the free-text notes — share
# this rendering budget, highest-ranked first; whatever does not fit is counted
# in the brief, with the tool call that reads it (preference op='list'). The
# budget is the size the older, cut summary already reached at its fullest
# (3 + 3 quotes and 3 notes at 500 chars, ~4.5k) with headroom, so a brief does
# not grow past what builds already carried, while nothing is cut silently.
_SUMMARY_QUOTE_BUDGET = 6000

# Light, dependency-free style-cue extraction over preference notes. These are
# common build-taste words the operator tends to repeat; tallying them lets the
# pre-bias surface "you usually prefer: minimal, dark, no-deps" without an LLM.
_STYLE_CUES = (
    "minimal", "minimalist", "simple", "clean", "dark", "light", "modern",
    "compact", "dense", "spacious", "accessible", "responsive", "mobile",
    "no deps", "no dependencies", "vanilla", "framework", "typescript",
    "tailwind", "verbose", "terse", "comments", "tested", "tests",
    "performance", "fast", "animation", "animated", "subtle", "bold",
    "rounded", "flat", "gradient", "monochrome", "colorful",
    # Stated preferences arrive as prose too, and these are the words that
    # actually show up in bake-off feedback ("I like the colors in this one",
    # "the timeline on this one", "I don't like codex's layout").
    "colors", "colours", "palette", "layout", "spacing", "timeline",
    "typography", "contrast", "whitespace",
)

_POLARITIES = ("like", "dislike", "ban")
_SUBJECTS = ("model", "harness", "design", "general")


def _profile_path(scope_filename: str) -> Path:
    return PROFILES_ROOT / scope_filename


def _empty_profile(scope: str) -> dict:
    now = time.time()
    return {
        "scope": scope,
        "decisions": 0,
        "picked": {},
        "grafted_from": {},
        "element_picks": {},
        "notes": [],
        "style_cues": {},
        "stated": {"like": {}, "dislike": {}},
        "bans": {},
        "work_models": {},
        "design_tokens": {},
        "updated_at": now,
        "first_seen": now,
    }


def _load(path: Path, scope: str) -> dict:
    """Load a profile JSON, or a fresh empty one. Tolerant of a corrupt/partial
    file (returns empty rather than raising) — a single bad write must never
    block the loop. Backfills any missing keys against the current shape."""
    if not path.exists():
        return _empty_profile(scope)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("preference profile %s unreadable, starting fresh: %s", path.name, e)
        return _empty_profile(scope)
    if not isinstance(data, dict):
        return _empty_profile(scope)
    base = _empty_profile(scope)
    base.update({k: v for k, v in data.items() if k in base})
    base["scope"] = scope  # never trust a stale scope from disk
    # Coerce the tally maps in case an older/partial file had them as non-dicts.
    for k in ("picked", "grafted_from", "element_picks", "style_cues",
              "bans", "work_models", "design_tokens"):
        if not isinstance(base.get(k), dict):
            base[k] = {}
    if not isinstance(base.get("notes"), list):
        base["notes"] = []
    # `stated` is newer than the profile format — an older profile lacks
    # it, so backfill both polarity buckets rather than letting a KeyError reach
    # the decision path.
    st = base.get("stated")
    if not isinstance(st, dict):
        st = {}
    for pol in ("like", "dislike"):
        if not isinstance(st.get(pol), dict):
            st[pol] = {}
    base["stated"] = st
    return base


def _atomic_write(path: Path, data: dict) -> None:
    """Write via a UNIQUE temp file, then rename.

    The temp name carries the pid so two writers can never share it. A fixed
    `.tmp` name is safe under today's single-worker uvicorn (one event loop, and
    every store function is synchronous between load and write), but the failure
    mode if that ever stops holding is severe and silent: two writers interleave
    on one temp path, the rename lands a torn file, and `_load` "starts fresh" —
    dropping the whole user profile, every ban in it included."""
    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _bump(tally: dict, key: Optional[str], n: int = 1) -> None:
    if not key:
        return
    key = str(key).strip()
    if not key:
        return
    tally[key] = int(tally.get(key, 0)) + n


def _extract_style_cues(note: str) -> list[str]:
    """Dependency-free keyword scan over a preference note. Lowercased substring
    match against the known build-taste vocabulary. Not NLP, not judgement — a
    repeatable tally the operator's own words drive."""
    if not note:
        return []
    low = note.lower()
    hits = []
    for cue in _STYLE_CUES:
        # word-ish boundary for single tokens; plain substring for multi-word cues
        if " " in cue:
            if cue in low:
                hits.append(cue)
        elif re.search(rf"\b{re.escape(cue)}\b", low):
            hits.append(cue)
    return hits


def _fold(profile: dict, record: dict) -> dict:
    """Fold ONE acceptance record into the profile. A record is the same dict the
    loop appends to harness-preferences.jsonl — accepts both the /pick shape and
    the /merge shape:
      pick:  {harness, note, element_picks?}
      merge: {base, graft_from, preference, element_picks}
    Only ACCEPTANCE signals are accumulated; nothing is scored."""
    profile["decisions"] = int(profile.get("decisions", 0)) + 1

    # Chosen base / winner — /pick uses 'harness', /merge uses 'base'.
    _bump(profile["picked"], record.get("harness") or record.get("base"))

    # Harnesses the human grafted good bits FROM (merge round).
    for h in (record.get("graft_from") or []):
        _bump(profile["grafted_from"], h)

    # Per-element picks: { element/feature -> harness_id }. We tally the harness
    # (which harness won elements), keeping the accumulation harness-centric.
    for _elem, h in (record.get("element_picks") or {}).items():
        _bump(profile["element_picks"], h)

    # Verbatim preference text — /pick='note', /merge='preference'.
    note = (record.get("note") or record.get("preference") or "").strip()
    if note:
        # Whole and kept: the user's own words (see _SUMMARY_QUOTE_BUDGET).
        profile["notes"].append({"at": time.time(), "text": note})
        for cue in _extract_style_cues(note):
            _bump(profile["style_cues"], cue)

    profile["updated_at"] = time.time()
    return profile


# ── Stated preferences — normalization ───────────────────────────────────────

def normalize_target(target: str) -> str:
    """A stated target reduced to a comparable key: lowercased, trimmed, inner
    whitespace collapsed. Deliberately NOT stripping the provider prefix — a user
    who bans 'amazon/nova-pro' and a user who bans 'nova' mean different widths,
    and `ban_for` handles the widening at match time, not here."""
    return re.sub(r"\s+", " ", str(target or "").strip().lower())


# A sub-token shorter than this is never matchable on its own. Without the
# floor, a family ban degenerates: 'v2', '4o' and 'pro' are sub-tokens of half
# the catalog, and one careless ban would refuse models the user never meant.
_MIN_SUBTOKEN = 3


def _segments(slug: str) -> list[str]:
    """A model slug split into every part a ban may legitimately name.

    'openrouter/amazon/nova-pro' →
      path parts      : openrouter, amazon, nova-pro
      suffix paths    : amazon/nova-pro, openrouter/amazon/nova-pro
      word sub-tokens : nova, pro          (split on - _ . and : )

    Suffix paths let a ban written at full width match the qualified slug the
    dispatcher ends up using. Word sub-tokens let a ban name a FAMILY the way a
    person does — someone says "never use amazon nova", not "amazon/nova-pro,
    amazon/nova-lite, amazon/nova-micro".

    Splitting on word boundaries rather than raw substrings is what keeps the
    family ban honest: 'nova' matches 'nova-pro' because 'nova' is a whole
    token there, and does NOT match 'supernova-v2', where it is only a tail of
    one. Sub-tokens under _MIN_SUBTOKEN characters are dropped entirely."""
    s = normalize_target(slug)
    if not s:
        return []
    parts = [p for p in s.split("/") if p]
    out = list(parts)
    for i in range(len(parts)):
        tail = "/".join(parts[i:])
        if tail not in out:
            out.append(tail)
    for p in parts:
        for tok in re.split(r"[-_.:]+", p):
            if len(tok) >= _MIN_SUBTOKEN and tok not in out:
                out.append(tok)
    if s not in out:
        out.append(s)
    return out


def _all_parts(slug: str) -> set:
    """Every part of a slug INCLUDING sub-tokens under the length floor.

    Used only for multi-word ban matching, where every word must be present.
    That conjunction is its own protection against over-breadth, so the floor
    would do nothing here except silently widen what the user typed: with it,
    "orbit 4" keeps only 'orbit' and takes out orbit-3 as well. Without it,
    "orbit 4" means orbit AND 4 — which covers orbit-4 and orbit-4-fast, and leaves
    orbit-3 alone. That is what the words were for."""
    s = normalize_target(slug)
    out = set()
    for p in (x for x in s.split("/") if x):
        out.add(p)
        out.update(t for t in re.split(r"[-_.:]+", p) if t)
    return out


def _version_tokens(slug: str) -> list[str]:
    """The all-numeric tokens of a slug, IN ORDER — its version path.

      claude-3-5-sonnet     → ['3', '5']
      anthropic/claude-opus-5 → ['5']
      gemini-2.5-pro        → ['2', '5']
      gpt-4o                → []          ('4o' is not all-numeric)

    Order carries meaning that a set does not: in `claude-3-5-sonnet` the 5 is
    a MINOR version of generation 3. Set-matching a spoken "claude 5" against
    that slug is how a ban on the expensive generation silently refuses the
    cheap one — see _ban_words_match."""
    out = []
    for p in (x for x in normalize_target(slug).split("/") if x):
        for tok in re.split(r"[-_.:]+", p):
            if tok.isdigit():
                out.append(tok)
    return out


def _ban_words_match(key: str, candidate: str) -> bool:
    """Does a MULTI-WORD ban key cover `candidate`?

    Two rules, because words carry two different kinds of meaning:

    ALPHA words are matched as an unordered SET — every one must appear among
    the candidate's parts. Requiring all of them is its own guard against
    over-breadth, which is why no length floor applies here (see _all_parts).

    NUMERIC words are matched as an ordered PREFIX of the candidate's version
    path (_version_tokens). A digit's POSITION is its meaning, and set-matching
    throws that away. Measured before this rule existed: a ban on "claude 5"
    matched claude-3-5-sonnet and claude-3.5-haiku — the 5 of a 3.5-generation
    model satisfied the word — so banning the expensive generation refused the
    cheap ones too, and on fan_out that 409s the whole batch. As a prefix:

      "claude 5"  → ['5'] vs ['5']      on claude-opus-5     → covers it
      "claude 5"  → ['5'] vs ['3','5']  on claude-3-5-sonnet → leaves it alone
      "claude 3 5"→ ['3','5'] prefix of ['3','5']            → covers it
      "orbit 4"    → ['4'] vs ['4']      on orbit-4-fast       → covers it
      "orbit 4"    → ['4'] vs ['3']      on orbit-3            → leaves it alone
      "gpt 4"     → ['4'] vs ['5','2']  on gpt-5.2           → leaves it alone

    One ambiguity is irreducible and left alone: "gemini 2" still covers
    gemini-2.5-pro, because a 2.5 model genuinely is a generation-2 model and
    words alone cannot settle whether the user meant to include it.
    """
    # Split on whitespace AND '/' — "amazon/nova pro" mixes both, and splitting
    # on space alone left 'amazon/nova' as a word no slug part ever equals, so
    # the ban stored, displayed as enforced, and never fired.
    words = [w for w in re.split(r"[\s/]+", key) if w]
    if not words:
        return False
    parts = _all_parts(candidate)
    if not all(w in parts for w in words):
        return False
    wanted = [w for w in words if w.isdigit()]
    if wanted:
        have = _version_tokens(candidate)
        if have[:len(wanted)] != wanted:
            return False
    return True


# ── Public API ───────────────────────────────────────────────────────────────

def load_project_profile(project_id) -> dict:
    """The accumulated taste profile for one Workbench project. Returns a fresh
    empty profile (never raises) when none exists yet or project_id is falsy."""
    if project_id is None:
        return _empty_profile("project:none")
    scope = f"project:{project_id}"
    return _load(_profile_path(f"project-{project_id}.json"), scope)


def load_user_profile() -> dict:
    """The entity-wide taste profile (cross-project). One file per entity pod
    (the pod IS the entity boundary), so no entity id is needed in the name."""
    return _load(_profile_path("user.json"), "user")


def update_profiles(project_id, record: dict) -> dict:
    """Fold one acceptance decision into BOTH the per-project profile (when
    project_id is set) and the user-level profile. Best-effort + atomic per file;
    a write failure on one never blocks the other or the caller. Returns
    {"project": <profile|None>, "user": <profile>} for callers that want to log/echo.

    `record` is the exact dict the loop already appends to
    harness-preferences.jsonl (pick or merge shape) — single source of truth, so
    the profile can never drift from the raw decision log."""
    out: dict = {"project": None, "user": None}
    if project_id is not None:
        try:
            p = load_project_profile(project_id)
            _fold(p, record)
            _atomic_write(_profile_path(f"project-{project_id}.json"), p)
            out["project"] = p
        except Exception as e:  # never block the decision path
            log.warning("project profile update failed (%s): %s", project_id, e)
    try:
        u = load_user_profile()
        _fold(u, record)
        _atomic_write(_profile_path("user.json"), u)
        out["user"] = u
    except Exception as e:
        log.warning("user profile update failed: %s", e)
    return out


def record_stated(project_id, *, polarity: str, target: str,
                  subject: str = "general", text: str = "") -> dict:
    """Record something the user SAID about a model / harness / design choice.

    polarity: 'like' | 'dislike' | 'ban'.
      like/dislike land in the given scope (the project when project_id is set,
      plus the user profile) and read out as a BIAS in the prompt summary.
      ban ALWAYS lands in the user profile regardless of project_id — "never use
      X" is a standing rule, and a rule that only held inside one project would
      be a trap. `ban_for` reads it back at dispatch.

    Returns {"ok": True, ...} or {"ok": False, "error": ...}. Raises nothing —
    the caller is a chat turn."""
    pol = (polarity or "").strip().lower()
    if pol not in _POLARITIES:
        return {"ok": False,
                "error": f"polarity must be one of {list(_POLARITIES)} (got {polarity!r})"}
    subj = (subject or "general").strip().lower()
    if subj not in _SUBJECTS:
        return {"ok": False,
                "error": f"subject must be one of {list(_SUBJECTS)} (got {subject!r})"}
    key = normalize_target(target)
    if not key:
        return {"ok": False,
                "error": "target is required (the model id, harness id, or the thing being described)"}
    said = str(text or "").strip()   # whole — the user's own words

    # A ban is global by construction; a bias honours the scope it was said in.
    global_only = pol == "ban"
    pid = None if global_only else project_id

    def _apply(prof: dict) -> None:
        if pol == "ban":
            prof["bans"][key] = {"subject": subj, "reason": said, "at": time.time()}
            # A ban supersedes a standing like — leaving both would make the
            # summary argue for a model the dispatcher now refuses.
            prof["stated"]["like"].pop(key, None)
        else:
            bucket = prof["stated"][pol]
            slot = bucket.get(key)
            if not isinstance(slot, dict):
                slot = {"subject": subj, "count": 0, "texts": []}
            slot["subject"] = subj
            slot["count"] = int(slot.get("count", 0)) + 1
            # When it was last said — the summary ranks by count, then by this.
            slot["at"] = time.time()
            if said:
                # Every distinct thing they said is kept; a repeat moves to the end.
                texts = [t for t in (slot.get("texts") or []) if t != said]
                texts.append(said)
                slot["texts"] = texts
            bucket[key] = slot
            # The opposite polarity is a correction, not a coexisting fact.
            prof["stated"]["dislike" if pol == "like" else "like"].pop(key, None)
        if said:
            for cue in _extract_style_cues(said):
                _bump(prof["style_cues"], cue)
        prof["updated_at"] = time.time()

    wrote = []
    if pid is not None:
        try:
            p = load_project_profile(pid)
            _apply(p)
            _atomic_write(_profile_path(f"project-{pid}.json"), p)
            wrote.append(f"project:{pid}")
        except Exception as e:
            log.warning("stated preference (project %s) failed: %s", pid, e)
    try:
        u = load_user_profile()
        _apply(u)
        _atomic_write(_profile_path("user.json"), u)
        wrote.append("user")
    except Exception as e:
        log.warning("stated preference (user) failed: %s", e)
        if not wrote:
            return {"ok": False, "error": f"could not write the preference: {e}"}

    return {"ok": True, "scope": " + ".join(wrote), "polarity": pol,
            "target": key, "subject": subj, "global": global_only}


def clear_stated(target: str) -> dict:
    """Drop every stored opinion about one target — like, dislike, and any ban —
    from the user profile. The undo for `record_stated`: a ban the user changes
    their mind about has to be removable by saying so, not by editing a file.
    Project-scoped bias for that target is left alone (say it in the project to
    change it there)."""
    key = normalize_target(target)
    if not key:
        return {"ok": False, "error": "target is required"}
    try:
        u = load_user_profile()
        removed = []
        if u["bans"].pop(key, None) is not None:
            removed.append("ban")
        for pol in ("like", "dislike"):
            if u["stated"][pol].pop(key, None) is not None:
                removed.append(pol)
        u["updated_at"] = time.time()
        _atomic_write(_profile_path("user.json"), u)
        return {"ok": True, "target": key,
                "removed": removed or ["nothing was stored"]}
    except Exception as e:
        log.warning("clear_stated failed for %s: %s", key, e)
        return {"ok": False, "error": str(e)}


# ── The model an agent runs on, per kind of untrusted work ───────────────────
# A Host needs an agent for untrusted work, and keeps a preference for which
# model handles each class of it. The kinds are
# mastercode/agent_work.WORK_CLASSES; the dispatch door reads these entries
# (agent_work.choose_agent_model) when a dispatch names no model. Global, in
# user.json beside the bans: which model reads untrusted content is a standing
# choice, not a project's taste.
_WORK_SETTERS = ("person", "host")


def _work_classes() -> dict:
    from mastercode.agent_work import WORK_CLASSES
    return WORK_CLASSES


def set_work_model(work: str, model: str, *, by: str = "host", text: str = "") -> dict:
    """Store the model agents run on for one kind of untrusted work. `by` is
    who chose it: the person (the Host recording their words) or the Host
    itself, in the open. A model a standing ban covers is refused here, so the
    store never holds a preference the dispatch door would refuse.
    Returns {"ok": True, ...} or {"ok": False, "error": ...}; never raises."""
    classes = _work_classes()
    w = str(work or "").strip()
    if w not in classes:
        return {"ok": False, "error": (f"work must be one of {list(classes)} (got {work!r})")}
    m = str(model or "").strip()
    if not m:
        return {"ok": False, "error": "target is required: the model agents should run on for "
                                      f"{w} work"}
    who = str(by or "host").strip().lower()
    if who not in _WORK_SETTERS:
        return {"ok": False, "error": f"by must be one of {list(_WORK_SETTERS)} (got {by!r})"}
    ban = ban_for(m)
    if ban:
        return {"ok": False, "error": ban_refusal(m, ban)}
    try:
        u = load_user_profile()
        u["work_models"][w] = {"model": m, "by": who, "text": str(text or "").strip(),
                               "at": time.time()}
        u["updated_at"] = time.time()
        _atomic_write(_profile_path("user.json"), u)
    except Exception as e:
        log.warning("work model preference failed (%s): %s", w, e)
        return {"ok": False, "error": f"could not write the preference: {e}"}
    return {"ok": True, "work": w, "model": m, "by": who, "covers": classes[w]}


def clear_work_model(work: str) -> dict:
    """Remove the model preference for one kind of work: agents for it run on
    the Host's own model again."""
    w = str(work or "").strip()
    if w not in _work_classes():
        return {"ok": False, "error": f"work must be one of {list(_work_classes())} (got {work!r})"}
    try:
        u = load_user_profile()
        had = u["work_models"].pop(w, None)
        u["updated_at"] = time.time()
        _atomic_write(_profile_path("user.json"), u)
    except Exception as e:
        log.warning("clear work model failed (%s): %s", w, e)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "work": w, "removed": (had or {}).get("model") or "nothing was set"}


def work_model_for(work: str) -> Optional[dict]:
    """The stored preference for one kind of work ({model, by, text, at}), or
    None. Fails open (None) when the profile cannot be read: the agent then
    runs on the Host's own route, as before preferences existed."""
    try:
        rec = (load_user_profile().get("work_models") or {}).get(str(work or "").strip())
    except Exception as e:
        log.warning("work model lookup failed (own route): %s", e)
        return None
    return dict(rec) if isinstance(rec, dict) and rec.get("model") else None


def list_preferences(project_id=None) -> dict:
    """Everything stored, both scopes, in one read — what the host shows when the
    user asks "what do you know about what I like?". Never raises."""
    user = load_user_profile()
    proj = load_project_profile(project_id) if project_id is not None else None

    def _view(p: dict) -> dict:
        return {
            "scope": p.get("scope"),
            "decisions": int(p.get("decisions") or 0),
            "picked": p.get("picked") or {},
            "grafted_from": p.get("grafted_from") or {},
            "style_cues": p.get("style_cues") or {},
            "stated": p.get("stated") or {"like": {}, "dislike": {}},
            # Every free-text note, whole, oldest first — the build summary
            # quotes only the newest that fit, so this is where the rest is read.
            "notes": p.get("notes") or [],
            "design_tokens": p.get("design_tokens") or {},
        }

    return {
        "user": _view(user),
        "project": _view(proj) if proj is not None else None,
        "bans": user.get("bans") or {},   # bans are global-only by construction
        # The model each kind of untrusted work's agents run on, and who set
        # it. A kind not listed runs on the Host's own model.
        "work_models": user.get("work_models") or {},
        "resolved_design_tokens": design_tokens_for(project_id),
        "note": ("acceptance tallies are frequencies of what you chose, never a "
                 "quality ranking; stated entries are your own words; bans are "
                 "rules enforced before dispatch; work_models are the models agents "
                 "run on per kind of untrusted work when a dispatch names none"),
    }


# ── Bans — the enforcement read ──────────────────────────────────────────────

# A ban whose subject is a design choice describes a LAYOUT the user disliked,
# not a machine to refuse. Letting it reach dispatch is how "I hate the composer
# layout" ends up refusing cursor, whose CLI serves a model called
# composer-2.5-fast. Only these subjects can stop a build.
_DISPATCH_BAN_SUBJECTS = frozenset({"model", "harness", "general"})


def ban_is_enforced(rec) -> bool:
    """Will THIS ban record actually stop a dispatch?

    The single source of truth for that question — every surface that describes
    a ban to the user or to the host must ask here rather than assuming, or it
    ends up asserting an enforcement dispatch won't perform. An unset subject
    means 'general', which enforces; only design-subject bans are advisory."""
    subject = (rec.get("subject") if isinstance(rec, dict) else None) or "general"
    return subject in _DISPATCH_BAN_SUBJECTS


def ban_for(candidate: str) -> Optional[dict]:
    """Does a standing ban cover `candidate` (a model id, or a harness id)?
    Returns the ban record with the matched key added, or None.

    Matching is on WHOLE PARTS, never substrings — a ban is a refusal, so a
    false positive costs the user a build they asked for:

      'nova'            covers openrouter/amazon/nova-pro   (sub-token)
      'amazon/nova-pro' covers openrouter/amazon/nova-pro   (suffix path)
      'amazon nova'     covers openrouter/amazon/nova-pro   (every word present)
      'nova'            does NOT cover acme/supernova-v2    (not a whole token)
      'orbit-4'         does NOT cover orbit-4-fast        (different token)

    The last line is the deliberate narrow edge: a hyphenated ban names one
    exact token, so banning a base id does not sweep up its variants. Say the
    family in words ('orbit 4') if that is what you mean.

    A MULTI-WORD target is matched as a SET: every word must appear among the
    candidate's parts, in any order. That is what makes the way people actually
    talk work — "never use amazon nova" reaches amazon/nova-pro — and it is why
    a spaced target is not simply a key that can never fire.

    Design-subject bans are skipped entirely (_DISPATCH_BAN_SUBJECTS): they are
    about how something looked, not about which machine may run.

    The matched key rides the return so a refusal can NAME the ban that fired
    instead of being mysterious. Fails OPEN (returns None) if the profile can't
    be read: a taste store must never be able to wedge every build on this host.
    """
    segs = set(_segments(candidate))
    if not segs:
        return None
    try:
        bans = load_user_profile().get("bans") or {}
    except Exception as e:
        log.warning("ban lookup failed (allowing the dispatch): %s", e)
        return None
    for key, rec in bans.items():
        rec = rec if isinstance(rec, dict) else {"reason": str(rec)}
        if (rec.get("subject") or "general") not in _DISPATCH_BAN_SUBJECTS:
            continue
        if " " in key:
            if not _ban_words_match(key, candidate):
                continue
        elif key not in segs:
            continue
        out = dict(rec)
        out["matched"] = key
        return out
    return None


def ban_refusal(candidate: str, ban: dict) -> str:
    """The user-facing sentence for a refused dispatch. Names the ban that fired
    and how to lift it, so a refusal is never a dead end."""
    reason = (ban or {}).get("reason") or ""
    matched = (ban or {}).get("matched") or candidate
    tail = f" — you said: \"{reason}\"" if reason else ""
    return (f"'{candidate}' is banned by your standing preference '{matched}'{tail}. "
            f"Nothing was dispatched and nothing was billed. Name a different "
            f"model, or say \"un-ban {matched}\" to lift it.")


# ── Design tokens — storage + resolve (deltas on the Host's desktop design) ──

def set_design_tokens(project_id, tokens: dict, *, merge: bool = True) -> dict:
    """Store the user's own design-token deltas. Scoped to the project when
    project_id is given, else to the user (the standing default). merge=True
    layers onto what is there; merge=False replaces the scope's set outright.

    Values are stored verbatim as strings — this is a carrier, not a CSS
    validator. The base they sit on is the Host's own desktop design; these
    are the deltas on top of it.

    Names are normalized (lowercased, whitespace → '-') so "Accent Color" and
    "accent-color" are the same token however they were typed. Names and values
    are stored WHOLE and the set is not counted down: these are the user's own
    design system (a colour ramp, a type scale and a spacing scale pass 40
    tokens; a font stack or a gradient passes 200 chars). A value was once cut
    at 200 chars silently and a 41st token refused. Every token is rendered
    into the brief, because the brief says to honour them exactly — a token
    left out could not be honoured."""
    if not isinstance(tokens, dict):
        return {"ok": False, "error": "tokens must be an object of {name: value}"}
    clean = {}
    for k, v in tokens.items():
        name = re.sub(r"\s+", "-", str(k or "").strip().lower())
        if not name:
            continue
        clean[name] = str(v).strip()
    if not clean and merge:
        return {"ok": False, "error": "no tokens given"}
    path = (_profile_path(f"project-{project_id}.json") if project_id is not None
            else _profile_path("user.json"))
    try:
        prof = (load_project_profile(project_id) if project_id is not None
                else load_user_profile())
        merged = ({**(prof.get("design_tokens") or {}), **clean}
                  if merge else clean)
        prof["design_tokens"] = merged
        prof["updated_at"] = time.time()
        _atomic_write(path, prof)
        return {"ok": True, "scope": prof["scope"],
                "design_tokens": prof["design_tokens"]}
    except Exception as e:
        log.warning("set_design_tokens failed (%s): %s", project_id, e)
        return {"ok": False, "error": str(e)}


def design_tokens_for(project_id=None) -> dict:
    """The token set a coder should be handed: the user's standing defaults,
    overlaid by this project's. One flat dict — the caller does not need to know
    there are two scopes. Empty dict when nothing is stored."""
    try:
        out = dict(load_user_profile().get("design_tokens") or {})
    except Exception:
        out = {}
    if project_id is not None:
        try:
            out.update(load_project_profile(project_id).get("design_tokens") or {})
        except Exception:
            pass
    return out


def design_tokens_block(project_id=None) -> str:
    """The resolved tokens rendered for a build brief — the form a coding harness
    can act on. Returns "" when nothing is stored (never prepend noise)."""
    tokens = design_tokens_for(project_id)
    if not tokens:
        return ""
    lines = ["DESIGN TOKENS (the user's own values — honour these exactly; they "
             "override the Host's desktop design where they overlap):"]
    for k in sorted(tokens):
        lines.append(f"  --{k}: {tokens[k]}")
    return "\n".join(lines)


def _top(tally: dict, n: int = 3) -> list[str]:
    return [k for k, _ in sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:n]]


def _weight_local(user_tally: dict, project_tally: dict) -> dict:
    """Add the project tally to the user tally — knowing they OVERLAP.

    Read this carefully, because the naming is a trap: `update_profiles` folds
    every decision into BOTH the project file and user.json, so the user tally
    is already a SUPERSET containing this project's decisions. Adding the
    project tally is therefore not a union — it counts a decision made in this
    project TWICE, once globally and once locally.

    That is deliberate and it is the whole mechanism: a decision made here
    weighs double against decisions made elsewhere, so the summary leans toward
    this project without ever losing the cross-project taste. There is no
    tunable constant — the 2x falls out of the two-scope structure rather than
    being a number anyone picked.

    What it replaced was worse: a project with a single
    decision REPLACED the user tally outright, blanking cross-project taste at
    exactly the moment a new project most needs it.

    Only the ORDER these produce is ever rendered (`_top`), never the counts —
    so the doubling shifts which entries lead, and cannot show the user an
    inflated number."""
    out = dict(user_tally or {})
    for k, v in (project_tally or {}).items():
        try:
            out[k] = int(out.get(k, 0)) + int(v)
        except (TypeError, ValueError):
            continue
    return out


def _rank_stated(bucket: dict) -> list[str]:
    """Stated targets, most-said first, then most recently said, then by name."""
    def _key(t):
        slot = bucket.get(t) if isinstance(bucket.get(t), dict) else {}
        try:
            n = int(slot.get("count") or 0)
        except (TypeError, ValueError):
            n = 0
        try:
            at = float(slot.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        return (-n, -at, t)
    return sorted(bucket, key=_key)


def _latest_text(slot) -> str:
    """The most recent thing said about one target (texts are most-recent-last)."""
    texts = (slot.get("texts") if isinstance(slot, dict) else None) or []
    return str(texts[-1]) if texts else ""


def summarize_for_prompt(project_id) -> str:
    """A SMALL 'learned preferences' summary to PREPEND to the governed context so
    round-1 of a fan_out (and a single build) is pre-biased toward the operator's
    demonstrated taste.

    Account-wide TALLIES (which harness was picked, what was grafted from
    where, style cues), bans and design tokens layer the project over the
    user-level profile. The user's own WORDS (free-text notes, stated likes
    and dislikes) come from THIS project's profile only. The user-level copy
    of a note does not say which project it was said in, and quoting it into
    every other project's brief put one build's instructions into the next:
    once a card game's harness got another project's review-tab notes, 2,805 of the 17,217 characters it was sent. With
    no project, no one's words are quoted. Everything stays readable through
    preference op='list'.

    Returns "" when there's no signal yet (don't prepend noise). Acceptance
    frequencies are never framed as "this harness is better", only "you have
    tended to prefer"; stated entries are quoted as the user's own words; bans
    are stated as refusals, because that is what they are."""
    proj = load_project_profile(project_id) if project_id is not None else None
    user = load_user_profile()

    # The user profile is the SUPERSET (every decision lands in both files);
    # adding the project profile weights this project's decisions double. See
    # _weight_local — the overlap is the mechanism, not a bug.
    picked = dict(user.get("picked") or {})
    grafted = dict(user.get("grafted_from") or {})
    cues = dict(user.get("style_cues") or {})
    notes: list = list((proj or {}).get("notes") or [])
    stated_like = dict(((proj or {}).get("stated") or {}).get("like") or {})
    stated_dislike = dict(((proj or {}).get("stated") or {}).get("dislike") or {})
    decisions = int(user.get("decisions") or 0)
    if proj:
        picked = _weight_local(picked, proj.get("picked"))
        grafted = _weight_local(grafted, proj.get("grafted_from"))
        cues = _weight_local(cues, proj.get("style_cues"))
        # `decisions` only gates emptiness below, so the overlap is harmless
        # here — but take the superset rather than a sum that means nothing.
        decisions = max(decisions, int(proj.get("decisions") or 0))

    bans = user.get("bans") or {}          # global by construction
    tokens = design_tokens_for(project_id)

    if not any((decisions, picked, cues, notes, stated_like, stated_dislike,
                bans, tokens)):
        return ""

    lines = ["LEARNED PREFERENCES (host-accumulated from this operator's past "
             "ACCEPTED decisions and their own stated words — a bias, not a rule; "
             "the build still stands on its own merits):"]
    if picked:
        lines.append(f"- Most-often-chosen approach/harness style: {', '.join(_top(picked))}")
    if grafted:
        lines.append(f"- Elements frequently grafted in: from {', '.join(_top(grafted))}")
    if cues:
        lines.append(f"- Recurring style cues: {', '.join(_top(cues, 5))}")
    # Every liked and disliked target is NAMED, ranked by how often it was said
    # and then by how recently. The quoted words share _SUMMARY_QUOTE_BUDGET,
    # claimed in priority order — dislikes first (what to avoid matters most),
    # then likes, then the free-text notes newest first. A quote is never cut:
    # it is rendered whole or counted as not quoted, and the count says where
    # the rest is read.
    like_rank = _rank_stated(stated_like)
    dislike_rank = _rank_stated(stated_dislike)
    note_texts = [(n.get("text") if isinstance(n, dict) else str(n)) or ""
                  for n in notes]
    note_texts = [t for t in note_texts if t]
    quoted: set = set()
    left = _SUMMARY_QUOTE_BUDGET
    not_quoted = 0
    candidates = ([("dislike", t, _latest_text(stated_dislike[t])) for t in dislike_rank]
                  + [("like", t, _latest_text(stated_like[t])) for t in like_rank]
                  + [("note", i, txt) for i, txt in reversed(list(enumerate(note_texts)))])
    for kind, ref, txt in candidates:
        if not txt:
            continue
        if len(txt) <= left or not quoted:
            quoted.add((kind, ref))
            left -= len(txt)
        else:
            not_quoted += 1
    if stated_like:
        lines.append(f"- Said they LIKE: {', '.join(like_rank)}")
        for t in like_rank:
            if ("like", t) in quoted:
                lines.append(f"    · {t}: \"{_latest_text(stated_like[t])}\"")
    if stated_dislike:
        lines.append("- Said they DISLIKE (avoid unless asked): "
                     + ", ".join(dislike_rank))
        for t in dislike_rank:
            if ("dislike", t) in quoted:
                lines.append(f"    · {t}: \"{_latest_text(stated_dislike[t])}\"")
    shown_notes = [txt for i, txt in enumerate(note_texts) if ("note", i) in quoted]
    if shown_notes:
        lines.append("- Recent stated preferences: " + " | ".join(shown_notes))
    if not_quoted:
        lines.append(f"- {not_quoted} more of the user's own stated words are "
                     f"not quoted here (this brief quotes the highest-ranked "
                     f"that fit); read every one with preference op='list'.")
    if bans:
        # Split by what dispatch will ACTUALLY do. A design-subject ban is
        # excluded from ban_for (it describes a layout, not a machine), so
        # listing it under "refused at dispatch" would assert an enforcement
        # that never happens — the same contradiction as a ban that stores and
        # silently never fires, just pointing the other way.
        enforced = sorted(t for t, r in bans.items() if ban_is_enforced(r))
        advisory = sorted(set(bans) - set(enforced))
        if enforced:
            lines.append("- BANNED (refused at dispatch, never propose these): "
                         + ", ".join(enforced))
        if advisory:
            lines.append("- Ruled out by the user (design choices — never "
                         "propose these, though nothing blocks them): "
                         + ", ".join(advisory))
    block = design_tokens_block(project_id)
    if block:
        lines.append(block)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
