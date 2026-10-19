#!/usr/bin/env python3
"""Gate: the desktop contract names every design token it did not apply, and
a bar size outside the range is refused with the range stated plainly.

The defect: desktop_tokens.extract_tokens dropped unknown names,
unsafe or over-long values and everything past the 64th token SILENTLY. The
design mounted without them and the host was never told which of its tokens
did not land. The bounds stay (the values are appended to the CSS every app
loads); what changes is that each dropped declaration is named, with why, in
the contract's warnings — the part the host reads and fixes in the same turn.

And a well-formed cseries-bar outside 28–120 px was refused as "not an edge
and a size", which reads as malformed. It now says what size was asked for
and that a bar is 28–120 px thick.

Run: APP_ROOT=<repo> python3 scripts/test_desktop_contract_names_drops.py
"""
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT)]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[desktop-drops] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[desktop-drops] FAIL {label}" + (f" — {detail}" if detail else ""))


def doc(tokens: str, bar: str | None = None) -> str:
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"cseries-desktop\" content=\"desktop\">"
            + (f"<meta name=\"cseries-bar\" content=\"{bar}\">" if bar else "")
            + f"<meta name=\"cseries-tokens\" content=\"{tokens}\"></head><body></body></html>")


def main() -> int:
    from server.applications import desktop_tokens as DT
    from server.applications import desktop_contract as DC

    # ── the parser reports what it left out ──────────────────────────────
    parse = getattr(DT, "parse_tokens", None)
    check("desktop_tokens.parse_tokens returns (accepted, dropped)", callable(parse))
    bad = ("--cseries-ink:#0b0b10; --cseries-bad: url(http://x); --font-display: Georgia;"
           " --cseries-long: " + "a" * 130 + "; --cseries-ink2 #fff")
    if callable(parse):
        acc, dropped = parse(doc(bad))
        names = {d["name"]: d["why"] for d in dropped}
        check("accepted tokens are unchanged by the report", acc == {"--cseries-ink": "#0b0b10"}, repr(acc))
        check("an unsafe value is named, with why", "url()" in names.get("--cseries-bad", ""), repr(names))
        check("a non --cseries-* name is named", "--font-display" in names, repr(names))
        check("an over-long value is named with the limit",
              "120 characters" in names.get("--cseries-long", ""), repr(names))
        check("a declaration with no ':' is named", any("--cseries-ink2" in n for n in names), repr(names))
        big = ";".join(f"--cseries-t{i}:#{i:03x}" for i in range(70))
        acc, dropped = parse(doc(big))
        over = [d["name"] for d in dropped if "64-token limit" in d["why"]]
        check("the 64-token bound stays, and every token past it is named",
              len(acc) == 64 and over == [f"--cseries-t{i}" for i in range(64, 70)], repr(over))
    check("extract_tokens still returns just the accepted dict",
          DT.extract_tokens(doc(bad)) == {"--cseries-ink": "#0b0b10"})

    # ── the contract's warnings carry it to the host ─────────────────────
    rep = DC.check_document(doc(bad))
    w = " ".join(rep.get("warnings") or [])
    check("check_document warns that tokens were NOT applied, naming them",
          "NOT applied" in w and "--cseries-bad" in w and "--font-display" in w, w[:300])
    big = ";".join(f"--cseries-t{i}:#{i:03x}" for i in range(70))
    w = " ".join(DC.check_document(doc(big)).get("warnings") or [])
    check("...and names the tokens past the 64-token limit",
          "--cseries-t64" in w and "--cseries-t69" in w and "64-token limit" in w, w[:300])
    many_bad = ";".join(f"--cseries-u{i}: url(x)" for i in range(30))
    w = " ".join(DC.check_document(doc(many_bad)).get("warnings") or [])
    check("...a long list says how many more there are rather than cutting silently",
          "and 10 more" in w and "30 design token" in w, w[:300])
    clean = DC.check_document(doc("--cseries-ink:#0b0b10"))
    check("a document whose every token landed gets no such warning",
          not any("NOT applied" in x for x in clean.get("warnings") or []))

    # ── the bar range, stated plainly ────────────────────────────────────
    errs = " ".join(DC.check_document(doc("--cseries-ink:#0b0b10", bar="bottom 200")).get("errors") or [])
    check("a 200 px bar is refused naming the size and the 28–120 px range",
          "200 px" in errs and "28–120 px thick" in errs, errs[:300])
    errs = " ".join(DC.check_document(doc("--cseries-ink:#0b0b10", bar="middle 48")).get("errors") or [])
    check("a malformed bar still states the edges and the range",
          "not an edge and a size" in errs and "28–120 px" in errs, errs[:300])
    check("a bar inside the range parses", DC.parse_bar(doc("", bar="bottom 48")) == {"edge": "bottom", "size": 48})
    pb = DC.parse_bar(doc("", bar="bottom 200"))
    check("an out-of-range bar is still 'invalid' to every reader (desktop_render)",
          isinstance(pb, dict) and "invalid" in pb, repr(pb))

    if FAILED:
        print(f"FAIL: desktop contract drops — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: desktop contract drops — every token not applied is named; the bar range is stated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
