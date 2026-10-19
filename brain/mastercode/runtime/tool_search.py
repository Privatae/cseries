"""runtime/tool_search.py — find a Host's tools by what they do.

A turn should pay for the
tools it uses, not for every tool the platform has: the Host searches for the
tools a job needs and only those schemas enter its context. This module is
the search. Deterministic, no model, no network, no embeddings — BM25 over
each tool's own text — so it works on every Host and every provider, and the
same message always finds the same tools.

What a tool is indexed on, with how many times each field counts:

  name           3   split on dots and underscores as well as whole
  purpose        2   the registry's one-line `_meta.purpose`
  description    1
  arguments      1   names and descriptions of its parameters, enum values
  group          1   the group key and its label
  skill          1   headings of the tool's section in its group's skill

A field counted three times is three times the evidence, which is how the
name outranks a word that appears once in a long description.

The same index holds the entries that are not tools (runtime/
discovery_index.py: skills, apps, capability cards, turn types, harnesses),
on the same fields: the entry's own name as `name`, its one line as
`purpose`, its other words (a skill's description, a card's phrasings) as
`description`, and its kind as `group`. A query that names a tool still
returns that tool first; a query that is exactly an entry's name returns the
entry next; everything else ranks by score.

BM25 constants k1=1.2, b=0.75: the Okapi defaults every reference
implementation ships (Robertson & Zaragoza 2009, "The Probabilistic Relevance
Framework"). Nothing here was tuned to make a test pass; the replay rig
(scripts/rig_tool_surface.py) measures recall on real recorded calls.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

K1 = 1.2
B = 0.75

FIELD_WEIGHTS: dict[str, int] = {
    "name": 3,
    "purpose": 2,
    "description": 1,
    "arguments": 1,
    "group": 1,
    "skill": 1,
}

# Words that carry no meaning about which tool is wanted. The prefire pick's
# own stop list (prefire_tools._STOP) plus the filler that tool text is made
# of ("the tool", "returns", "optional"), which would otherwise make every
# description match every query.
_STOP: frozenset[str] = frozenset({
    "the", "and", "for", "you", "your", "with", "this", "that", "what", "when",
    "how", "can", "could", "would", "should", "please", "thanks", "just", "now",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "about",
    "from", "into", "onto", "some", "any", "all", "one", "two", "let", "lets",
    "get", "got", "make", "made", "want", "need", "like", "is", "it", "to",
    "of", "in", "on", "or", "an", "be", "by", "as", "at", "if", "do", "me",
    "my", "we", "our", "us", "so", "no", "yes", "a", "i",
    "tool", "tools", "returns", "return", "optional", "default", "use", "used",
    "e.g", "eg", "via", "per", "each", "only", "also", "which", "will",
})

_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str | None) -> list[str]:
    """Lowercased alphanumeric words, stop words and single characters
    dropped. `system.app_status` gives `system`, `app`, `status`: a name is
    found by any of its parts, the way a person would say it."""
    return [w for w in _WORD.findall((text or "").lower())
            if len(w) >= 2 and w not in _STOP]


def _argument_text(parameters: dict | None) -> str:
    """Every parameter's name, description and enum values, nested objects
    included, as one string."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for pname, pschema in props.items():
                    out.append(str(pname))
                    walk(pschema)
            desc = node.get("description")
            if isinstance(desc, str):
                out.append(desc)
            enum = node.get("enum")
            if isinstance(enum, list):
                out.extend(str(e) for e in enum)
            items = node.get("items")
            if isinstance(items, (dict, list)):
                walk(items)
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(parameters or {})
    return " ".join(out)


@dataclass
class ToolDoc:
    name: str
    group: str
    terms: Counter = field(default_factory=Counter)
    length: int = 0
    kind: str = "tool"
    # The entry's own name when its key carries a kind prefix
    # ("skill:desktop" -> "desktop"); the key itself for a tool.
    title: str = ""


# The words people use for each kind of entry that is not a tool, indexed in
# its `group` field (the way GROUP_KEYWORDS words index a tool's group): the
# kind in the singular and the plural, so "skills" finds a skill.
KIND_WORDS: dict[str, str] = {
    "skill": "skill skills",
    "app": "app apps",
    "capability": "capability capabilities",
    "turn_type": "turn type types",
    "harness": "harness harnesses",
}


def build_entry_doc(key: str, *, title: str, kind: str, line: str = "",
                    words: str = "") -> ToolDoc:
    """One entry that is not a tool, indexed on the tool fields: `title` (its
    own name) as name, `line` as purpose, `words` as description, its kind
    as group. `key` is what search returns."""
    fields = {
        "name": title,
        "purpose": line,
        "description": words,
        "arguments": "",
        "group": KIND_WORDS.get(kind, kind),
        "skill": "",
    }
    terms: Counter = Counter()
    for fname, text in fields.items():
        w = FIELD_WEIGHTS[fname]
        for t in tokens(text):
            terms[t] += w
    return ToolDoc(name=key, group=kind, terms=terms, length=sum(terms.values()),
                   kind=kind, title=title)


def build_doc(name: str, schema: dict, *, group: str | None = None,
              group_label: str | None = None,
              group_words: tuple[str, ...] | list[str] | None = None,
              skill_headings: list[str] | None = None) -> ToolDoc:
    """One tool's index entry from its registry schema. `group_words` are
    the words people use for the tool's group — the prefire pick's own
    keyword table (prefire_tools.GROUP_KEYWORDS) — so "my harness" finds the
    build group's tools though no tool says "harness" itself."""
    fn = (schema or {}).get("function") or {}
    meta = (schema or {}).get("_meta") or {}
    grp = group or meta.get("category") or "other"
    fields = {
        "name": name,
        "purpose": meta.get("purpose") or "",
        "description": fn.get("description") or "",
        "arguments": _argument_text(fn.get("parameters")),
        "group": " ".join([grp, group_label or "", *(group_words or ())]),
        "skill": " ".join(skill_headings or []),
    }
    terms: Counter = Counter()
    for fname, text in fields.items():
        w = FIELD_WEIGHTS[fname]
        for t in tokens(text):
            terms[t] += w
    return ToolDoc(name=name, group=grp, terms=terms, length=sum(terms.values()))


class ToolIndex:
    """BM25 over a fixed set of tools. Build it once per change in what the
    Host holds; searching is a pure read."""

    def __init__(self, docs: list[ToolDoc]):
        self.docs: dict[str, ToolDoc] = {d.name: d for d in docs}
        n = len(self.docs)
        self.avg_len = (sum(d.length for d in self.docs.values()) / n) if n else 0.0
        df: Counter = Counter()
        for d in self.docs.values():
            df.update(d.terms.keys())
        # BM25's idf with the +1 inside the log (the Lucene form): never
        # negative, so a word in more than half the tools still counts a
        # little instead of counting against a match.
        self.idf: dict[str, float] = {
            t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    @classmethod
    def from_schemas(cls, schemas: dict[str, dict], *,
                     groups: dict[str, str] | None = None,
                     group_labels: dict[str, str] | None = None,
                     group_words: dict[str, tuple[str, ...]] | None = None,
                     skill_headings: dict[str, list[str]] | None = None,
                     entries: list[dict] | None = None) -> "ToolIndex":
        """`schemas`: {name: registry schema} for the tools to index.
        `groups` overrides a schema's own `_meta.category` (the session's
        group map); `group_labels` are the map's labels per group;
        `group_words` the words people use for each group;
        `skill_headings` are each tool's section headings in its skill.
        `entries`: the entries that are not tools, each
        {key, title, kind, line, words} (build_entry_doc)."""
        docs = []
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("key") or e["key"] in (schemas or {}):
                continue
            docs.append(build_entry_doc(
                str(e["key"]), title=str(e.get("title") or e["key"]),
                kind=str(e.get("kind") or "other"), line=str(e.get("line") or ""),
                words=str(e.get("words") or "")))
        for name, schema in (schemas or {}).items():
            if not isinstance(schema, dict):
                continue
            grp = (groups or {}).get(name) or (schema.get("_meta") or {}).get("category") or "other"
            docs.append(build_doc(
                name, schema, group=grp,
                group_label=(group_labels or {}).get(grp),
                group_words=(group_words or {}).get(grp),
                skill_headings=(skill_headings or {}).get(name)))
        return cls(docs)

    def score(self, query_terms: list[str], name: str) -> float:
        d = self.docs.get(name)
        if d is None or not d.length:
            return 0.0
        s = 0.0
        norm = K1 * (1 - B + B * d.length / (self.avg_len or 1.0))
        for t in set(query_terms):
            tf = d.terms.get(t)
            if not tf:
                continue
            s += self.idf.get(t, 0.0) * tf * (K1 + 1) / (tf + norm)
        return s

    def name_hits(self, query: str) -> set[str]:
        """Tools the query NAMES: the whole name as a word, unless it is a
        one-part name that is also an everyday word (prefire_tools.WEAK_WORDS:
        "context", "status" — "in this context" is not a request for the
        tool), or every part of a name of two or more parts. A word keeps its
        dots and underscores and loses a full stop or hyphen at either end,
        so "in gitea." names gitea. A name's parts may be written joined
        ("system app_status" names system.app_status), and a query that is
        nothing but a tool's name always names it. An entry that is not a
        tool is named only by a query that is exactly its key
        ("skill:desktop") or its own name ("desktop")."""
        from mastercode.runtime.prefire_tools import WEAK_WORDS
        raw = (query or "").strip().lower()
        words = {w.strip(".-") for w in re.findall(r"[a-z0-9][a-z0-9_.\-]*", raw)}
        parts_in_query = set(_WORD.findall(raw))
        out: set[str] = set()
        for name, doc in self.docs.items():
            low = name.lower()
            if raw == low:
                out.add(name)
                continue
            if doc.kind != "tool":
                if raw and raw == (doc.title or "").strip().lower():
                    out.add(name)
                continue
            parts = {p for p in re.split(r"[._]", low) if len(p) >= 3}
            if low in words and (low not in WEAK_WORDS or len(parts) >= 2):
                out.add(name)
            elif len(parts) >= 2 and parts <= parts_in_query:
                out.add(name)
        return out

    def search(self, query: str, *, limit: int = 5,
               allowed: set[str] | frozenset[str] | None = None) -> list[tuple[str, float]]:
        """The best `limit` tools for `query` as [(name, score)], best first,
        ties broken by name so the same query always returns the same list.
        A tool the query names (name_hits) ranks above every tool it only
        describes: a Host that knows a tool's name — from a skill, from its
        own trail — gets that tool, not a neighbour whose description repeats
        the word. An entry that is not a tool and that the query names comes
        next, then everything by score. `allowed`, when given, is the set the caller may see (what
        the Host holds and the turn type admits); nothing outside it is ever
        returned. An empty or stop-word-only query returns []."""
        q = tokens(query)
        if limit <= 0:
            return []
        named = self.name_hits(query)
        if not q and not named:
            return []
        scored = []
        for name in self.docs:
            if allowed is not None and name not in allowed:
                continue
            s = self.score(q, name)
            if s > 0 or name in named:
                scored.append((name, s))
        def rank(name: str) -> int:
            if name not in named:
                return 2
            return 0 if self.docs[name].kind == "tool" else 1

        scored.sort(key=lambda ns: (rank(ns[0]), -ns[1], ns[0]))
        return scored[:limit]
