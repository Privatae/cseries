#!/usr/bin/env python3
"""Ratchet: the Compute view must never render "not measured" as a real zero.

WHY THIS EXISTS. Audited against one Host's live data on 2026-09-09, the view
was asserting three things nobody ever measured:

  * "Vendor spend $0.00 — vendor-reported" on a headline card, a daily chart
    line and three table columns, while vendor_cost_microdollars was NULL on
    100% of 73 runs. Never reported is not zero.
  * "0 tokens" for every kimi, cursor and opencode run — 58 of 73 runs store
    tokens_total=0 with token_source='unknown', so the zero meant "not
    captured" and read as "used none".
  * "$0.00" on 1,078 inference calls carrying 20.6M tokens, because
    est_cost_usd is 0 whenever no rate card exists. The headline
    "Estimated inference $10.06" was priced from 22 of 1,148 calls (1.9%) and
    read as the window's total.

The data always had the distinction — token_source, and cost_method with its
'list' / 'subscription' / 'unpriced' vocabulary. Both the SQL (coalesce) and
the JS formatters (`Number(n) || 0`) threw it away on the way to the screen.

Run: python3 scripts/test_compute_not_measured.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = ROOT / "server" / "routes" / "compute.py"
APP = ROOT / "system-apps" / "compute" / "app.js"

failures: list[str] = []



def _run_no_harness_row(api: str):
    """Actually build the chat-only row and see if it raises.

    Lifts _HU_MONEY_COLS, _HU_NULL_KEYS, _HU_ZERO_KEYS, _money_row and
    _no_harness_row out of compute.py by ast and execs just those, so no
    FastAPI/asyncpg/brain_service import is needed. Returns (ok, why).
    """
    import ast as _ast
    wanted_defs = {"_money_row", "_no_harness_row", "_vendor_cost"}
    wanted_assigns = {"_HU_MONEY_COLS", "_HU_NULL_KEYS", "_HU_ZERO_KEYS"}
    tree = _ast.parse(api)
    keep = []
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name in wanted_defs:
            keep.append(node)
        elif isinstance(node, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id in wanted_assigns
                for t in node.targets):
            keep.append(node)
    names = {getattr(n, "name", None) for n in keep} | {
        t.id for n in keep if isinstance(n, _ast.Assign)
        for t in n.targets if isinstance(t, _ast.Name)}
    for need in wanted_defs | wanted_assigns:
        if need not in names:
            return False, f"{need} is gone from compute.py"
    ns: dict = {"re": re}
    try:
        exec(compile(_ast.Module(body=keep, type_ignores=[]), "<compute>", "exec"), ns)
    except Exception as exc:                       # noqa: BLE001
        return False, f"could not evaluate the row helpers: {exc!r}"
    try:
        row = ns["_no_harness_row"]()
    except NameError as exc:                       # noqa: BLE001
        # A name the real module HAS but this probe did not lift. Production is
        # fine; the harness is short a symbol. Saying "/projects returns 500"
        # here would be the gate asserting something it did not measure — the
        # exact failure this whole file exists to stop.
        return False, (f"the probe's hermetic namespace lacks {exc!r} — add it "
                       "to wanted_defs/wanted_assigns in this file. This says "
                       "nothing about production.")
    except Exception as exc:                       # noqa: BLE001
        return False, (f"_no_harness_row() raises {exc!r} — /projects returns "
                       "500 for every project with chat turns and no harness "
                       "runs, which is the DEFAULT TAB")
    if row.get("tokens") is not None:
        return False, ("_no_harness_row() reports tokens="
                       f"{row['tokens']!r}; a project with no runs measured "
                       "nothing, so this must be None")
    if row.get("vendor_est_usd") is not None:
        return False, ("_no_harness_row() reports vendor_est_usd="
                       f"{row['vendor_est_usd']!r}; that puts '$0.00 "
                       "vendor-reported' back on the headline card")
    return True, ""


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def main() -> int:
    api = API.read_text()
    app = APP.read_text()

    # ── the API must not flatten "absent" into 0 ────────────────────────────
    check("coalesce(sum(vendor_cost_microdollars), 0)" not in api,
          "vendor_cost_microdollars is coalesced to 0 again — a NULL vendor "
          "cost would publish '$0.00 vendor-reported' as though it were a "
          "reading (it is NULL on 100% of entity 9003's runs)")
    check("sum(vendor_cost_microdollars)" in api,
          "the vendor sum vanished entirely")

    check("coalesce(sum(tokens_total), 0)" not in api,
          "tokens_total is coalesced to 0 again — runs with "
          "token_source='unknown' store 0 at ingest, so this makes 'no data' "
          "arithmetically identical to 'zero tokens'")
    check("FILTER (WHERE token_source <> 'unknown')" in api,
          "the token sum no longer excludes unknown-source runs")

    # None must survive serialization rather than being int()/round()ed to 0.
    check(re.search(r'"vendor_est_usd":\s*\(None if', api) is not None,
          "_money_row casts vendor_ud unconditionally — None becomes 0.0")
    check(re.search(r'"tokens":\s*None if', api) is not None,
          "_money_row casts tokens unconditionally — None becomes 0")
    check('"tokens_covered_runs"' in api,
          "tokens_covered_runs is gone — a token total computed from 2 of 20 "
          "runs is indistinguishable from the window's real total without it")
    # Coverage must be derived from what the token filter ACTUALLY excludes,
    # not from a hand-listed set of known labels. The sum form shipped wrong
    # once already: it omitted 'stream', so codex reported 2 covered runs as 0
    # while its 1.3M stream tokens were still counted in the total.
    check('runs - int(r["tok_unknown"])' in api,
          "tokens_covered_runs is back to summing named labels — it will "
          "undercount the moment token_source gains a value")
    check("token_source = 'stream'" in api,
          "the 'stream' provenance bucket is gone; its runs drop out of "
          "token_sources and the breakdown stops summing to runs")

    # ── the inference lane must keep the three cost states apart ────────────
    for col in ("priced_calls", "subscription_calls", "unpriced_calls",
                "unpriced_tokens"):
        check(col in api, f"inference aggregate lost {col}")
    check('"price_coverage"' in api,
          "price_coverage is gone — the 'Estimated inference' headline goes "
          "back to reading as the window total when it prices ~2% of calls")

    # ── the UI must not resurrect the zero-coercion ─────────────────────────
    # Comments are stripped first: this file's own explanation of the bug
    # quotes the offending pattern, and matching that made the check fail on a
    # correct tree — a false positive is how a gate gets disabled.
    app_code = "\n".join(l for l in app.splitlines()
                         if not l.lstrip().startswith(("//", "*", "/*")))
    check("Number(n) || 0" not in app_code,
          "a formatter is back to `Number(n) || 0`, which renders null as a "
          "confident 0 / $0.00")
    check("const isUnknown =" in app and 'const UNKNOWN = "—"' in app,
          "the UNKNOWN/isUnknown helpers are gone")
    for fmt in ("fmtUsd", "fmtNum", "fmtTokens", "fmtSecs", "fmtPct"):
        # each formatter must consult isUnknown before coercing
        m = re.search(rf"const {fmt} = \([^)]*\) =>(.*?)(?=\nconst |\n// |\Z)",
                      app, re.S)
        check(m is not None and "isUnknown" in m.group(1),
              f"{fmt} does not check isUnknown — it will render null as a number")

    check("unpriced" in app and "flat plan" in app,
          "the inference cost cell no longer distinguishes unpriced from a "
          "flat-plan zero")

    # ── the chat-only project row: EXECUTED, not grepped ───────────────────
    # A hand-copied key list here shipped a KeyError on /projects, the DEFAULT
    # TAB, for every project with chat turns and no harness runs — five of them
    # on one Host, so the app opened on an error card. The first version of
    # THIS FILE grepped for the fix and passed on the broken tree, because a
    # missing dict key is a runtime shape, not a string. So run the thing.
    #
    # _money_row and _no_harness_row are lifted out with ast and executed on
    # their own: that needs no FastAPI, no asyncpg and no brain_service, so the
    # gate stays hermetic while testing the real code.
    ok, why = _run_no_harness_row(api)
    check(ok, why)

    check('p["vendor_est_usd"] or 0.0' in api,
          "the /projects sort key compares vendor_est_usd raw — it is None "
          "wherever nothing reported, and Python will not order None against "
          "a float, so any tie on privatae_usd raises TypeError")

    # ── per-run lanes, not just the rollups ────────────────────────────────
    check('int(r["tokens_total"] or 0)' not in api,
          "the runs/detail lanes are back to `or 0` on tokens_total — an "
          "unknown-source run publishes a measured-looking 0")
    check('int(r["vendor_cost_microdollars"] or 0)' not in api,
          "the runs/detail lanes are back to `or 0` on vendor cost")

    # ── a partial vendor estimate must say so ──────────────────────────────
    # vendor_est_usd sums only the runs that reported; the project and harness
    # cells and the headline card carry vendor_est_runs, as tokens carry
    # tokens_covered_runs.
    check(app.count("${vendorChip(") >= 2 and app.count("vendorTip(") >= 3
          and "totVendorRuns < totRuns" in app,
          "a vendor estimate cell or the headline card no longer says how many "
          "runs it covers — a sum over 1 of 10 runs reads as the whole spend")
    check("r.vendor_est_runs" in app and "${dayVendorNote}" in app,
          "the daily vendor + inference chart no longer says how many runs its "
          "vendor share covers")

    # ── the harness tooltip must not hand-list sources ─────────────────────
    check("Object.keys(src)" in app,
          "srcTip hand-lists token sources again — that is what dropped "
          "'stream' and made codex read 9 sources against 11 runs")

    if failures:
        print("FAILED: the Compute view can report a measurement that never "
              "happened\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("PASSED: Compute distinguishes not-measured from zero "
          f"({len(failures)} failures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
