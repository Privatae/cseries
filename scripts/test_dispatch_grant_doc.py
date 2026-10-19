#!/usr/bin/env python3
"""
Dispatch-skill doc-truth gate (smoke).

`default-skills/dispatch-agent.md` teaches the host which capabilities each
tool needs ("Common tools and what they need"). That section is hand-written
prose; `META_TOOL_CAPS` is the enforcement table. When they drift, the host
composes a grant the doc promised would work and the dispatch door refuses it
— a refusal the host earned by BELIEVING its own manual.

Measured before this gate existed (2026-08-24, project 9213): the
table classed `list_projects` as READ_MEMORY while both sibling project reads
(`get_project`, `get_project_activity`) and the doc's own capability
definitions class projects as READ_CONTEXT — so a host granting read_context
for project work was refused at the door. Auditing the whole section against
the table found three more silent traps in the same shape: `workspace_file`,
`keys_manage` and `index_my_code` were each listed under ONE capability while
requiring a set — a grant composed from the doc line was refused at the door.

The invariant: every tool the section names exists in META_TOOL_CAPS, and the
OR of the capabilities the doc names for it equals the tool's declared
required_bits exactly — not a subset (the doc under-teaches and the dispatch
is refused), not a superset (the doc trains hosts to over-grant).

The section teaches exactly the tools an agent's scope may read foreign
content with — scoped_agent.agent_reader_tools,
the set the dispatch door admits on its own (the Host calls an agent only for
untrusted work outside the platform). The old sample led with
code_read/code_grep and listed the Host's own files, tickets and keys as
agent tools; a reader tool the door gains and the doc does not teach, or an
in-platform tool the doc teaches as an agent's, fails here.

Runs against the image (/app) or a source tree (APP_ROOT=<repo>); no pod, no
DB — pure table-vs-doc comparison.

Exit 0 = all pass; exit 1 + [dispatch-doc] FAIL lines otherwise.
"""
import os
import re
import sys

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    if ok:
        print(f"[dispatch-doc] OK    {name}")
    else:
        failed = 1
        print(f"[dispatch-doc] FAIL  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))

    # meta_tool_caps imports asyncpg at module level; in-image it exists, in a
    # bare source tree it may not. The gate never touches a DB — stub it.
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        import types
        sys.modules["asyncpg"] = types.ModuleType("asyncpg")

    from mastercode.runtime.agent_grant import NAME_TO_BIT, describe
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS

    doc_path = os.path.join(app_root, "default-skills", "dispatch-agent.md")
    with open(doc_path, encoding="utf-8") as f:
        doc = f.read()

    m = re.search(r"## Common tools and what they need\n(.*?)(?=\n## )",
                  doc, re.DOTALL)
    check("doc section 'Common tools and what they need' present", bool(m))
    if not m:
        return 1
    section = m.group(1)

    # Reassemble bullets: a bullet starts with '- ' and continues on indented
    # lines. Prose paragraphs between bullet groups end the current bullet.
    bullets: list[str] = []
    cur: list[str] = []
    for line in section.splitlines():
        if re.match(r"^[-*+] ", line):
            if cur:
                bullets.append(" ".join(cur))
            cur = [line[2:].strip()]
        elif line.startswith("  ") and cur:
            cur.append(line.strip())
        else:
            if cur:
                bullets.append(" ".join(cur))
            cur = []
    if cur:
        bullets.append(" ".join(cur))
    check("doc section parses into bullets", len(bullets) >= 1,
          f"got {len(bullets)}")

    # tool -> OR of every capability the doc names for it, across both bullet
    # shapes: "`cap` — `tool`, `tool`, ..." and "`tool` — `cap`, `cap`, ...".
    taught: dict[str, int] = {}
    for b in bullets:
        tokens = re.findall(r"`([^`]+)`", b)
        if not tokens:
            check("bullet names at least one backticked token", False, b[:80])
            continue
        head, rest = tokens[0], tokens[1:]
        if head in NAME_TO_BIT:
            # capability-group bullet: everything after the head is a tool
            for t in rest:
                check(f"'{t}' under `{head}` is a tool, not a capability",
                      t not in NAME_TO_BIT, b[:80])
                taught[t] = taught.get(t, 0) | NAME_TO_BIT[head]
        else:
            # tool bullet: everything after the head must be a capability
            bits = 0
            for c in rest:
                check(f"'{c}' in the `{head}` bullet is a capability name",
                      c in NAME_TO_BIT, b[:80])
                bits |= NAME_TO_BIT.get(c, 0)
            taught[head] = taught.get(head, 0) | bits

    # Required coverage, both ways: the section teaches every tool the
    # dispatch door admits as an agent's reader, and nothing else. A format
    # drift that drops a bullet from parsing fails here rather than passing on
    # the remainder (once '- ' → '* ' made three bullets vanish and the gate
    # stayed green).
    from mastercode.runtime.scoped_agent import agent_reader_tools
    readers = set(agent_reader_tools())
    for t in sorted(readers):
        check(f"doc teaches the agent reader {t}", t in taught,
              "bullet dropped from parsing — format drift?")
    for t in sorted(set(taught) - readers):
        check(f"doc teaches {t} as an agent's tool only if the door admits it",
              False, "an in-platform tool taught as an agent's scope — the Host "
              "does that work itself (read/operate/build)")

    for tool, doc_bits in sorted(taught.items()):
        spec = META_TOOL_CAPS.get(tool)
        check(f"{tool}: exists in META_TOOL_CAPS", spec is not None,
              "doc names a tool the enforcement table does not know — "
              "renamed or retired?")
        if spec is None:
            continue
        need = int(spec.get("required_bits") or 0)
        check(
            f"{tool}: doc grant == table requirement",
            doc_bits == need,
            f"doc teaches [{describe(doc_bits)}], table requires "
            f"[{describe(need)}] — a host following the doc is "
            + ("refused at the dispatch door"
               if need & ~doc_bits else "trained to over-grant"),
        )

    # The capabilities the skill teaches an agent can hold are exactly the
    # door's (agent_grant.AGENT_GRANT_NAMES), and every count of them in the
    # skill says so. 2026-10-13: the table went from seven names to three
    # while the typo refusal's line still said "the seven above".
    from mastercode.runtime.agent_grant import AGENT_GRANT_NAMES
    tm = re.search(r"## The (\w+) capabilities an agent can hold\n(.*?)(?=\n## )", doc, re.DOTALL)
    check("doc section 'The <n> capabilities an agent can hold' present", bool(tm))
    if tm:
        rows = re.findall(r"^\|\s*`([a-z_]+)`\s*\|", tm.group(2), re.M)
        check("the capability table lists exactly the grant the door admits",
              sorted(rows) == sorted(AGENT_GRANT_NAMES), f"{rows} vs {list(AGENT_GRANT_NAMES)}")
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}
    for m2 in re.finditer(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                          r"(capabilities|above)\b", doc, re.I):
        check(f"'{m2.group(0)}' counts the {len(AGENT_GRANT_NAMES)} capabilities an agent can hold",
              words[m2.group(1).lower()] == len(AGENT_GRANT_NAMES), m2.group(0))

    # Sibling coherence: tools reading the SAME data class must require the
    # same bits, or a host that learned the class from one tool is refused on
    # its sibling. This is exactly the 2026-08-24 incident: list_projects sat
    # at READ_MEMORY while get_project / get_project_activity (same table,
    # MORE detail) sat at READ_CONTEXT.
    for family in (("list_projects", "get_project", "get_project_activity"),):
        bits = {t: int((META_TOOL_CAPS.get(t) or {}).get("required_bits") or 0)
                for t in family}
        check(f"sibling coherence: {' == '.join(family)}",
              len(set(bits.values())) == 1, f"{bits}")

    return failed


if __name__ == "__main__":
    sys.exit(main())
