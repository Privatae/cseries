#!/usr/bin/env python3
"""
Phase-0 gate — intake + promote planning (server/proposals/{intake,promote}.py).

Proves the pure transforms the proposals route relies on: a community/proposals
PR becomes the right Proposal, the gated-auto verdict flows through, and an
approved manifest plans a correct promote PR into the platform catalog. Non-
manifest proposals refuse to promote. Pure — no gitea, no network. Run:
  python3 scripts/test_proposals_promote.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The target repos are configuration; set them before promote.py reads them.
os.environ["CSERIES_PLATFORM_REPO"] = "owner/platform"
os.environ["CSERIES_PLATFORM_BASE"] = "main"
os.environ["CSERIES_ZONE_REPO"] = "owner/zone"
os.environ["CSERIES_ZONE_BASE"] = "zone-main"

from server.proposals import ProposalKind, evaluate            # noqa: E402
from server.proposals.intake import proposal_from_pr           # noqa: E402
from server.proposals.promote import plan_promotion, NotPromotable  # noqa: E402

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


# Carries the set format — a manifest missing any required field
# fails schema_valid, so the wiring fixture has to be a complete specification.
GOOD_VOICE = """
key: xai-voice
display_name: xAI Grok Voice
connection_kind: realtime_voice
auth: {mode: api_key}
realtime:
  ws_url_template: "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
  token_url: "https://api.x.ai/v1/realtime/client_secrets"
  model: grok-voice-latest
  audio_format: pcm16
vendor_api_reference: https://docs.x.ai/api/realtime
what_the_host_gains: The host can hold a spoken conversation with the user.
"""

OFF_ALLOWLIST = """
key: sketchy
display_name: Sketchy
connection_kind: realtime_voice
auth: {mode: api_key}
realtime: {ws_url_template: "wss://evil.example.com/rt"}
"""

PR = {"number": 7, "title": "add xAI Grok Voice", "user": {"login": "host-a"}}


def _named_detail(verdict, name):
    return next(c for c in verdict.checks if c.name == name).detail

# ── intake: manifest PR → MANIFEST proposal ─────────────────────────────────
p = proposal_from_pr(PR, {"manifests/xai-voice.yaml": GOOD_VOICE})
check("intake: kind MANIFEST", p.kind is ProposalKind.MANIFEST, p.kind.value)
check("intake: the manifest file is the proposal",
      set(p.files) == {"manifests/xai-voice.yaml"}, str(set(p.files)))
check("intake: author carried", p.submitter_entity_id == "host-a", str(p.submitter_entity_id))
check("intake: a clean single-kind PR has no intake error", p.intake_error == "", p.intake_error)

# ── intake REFUSES anything that is not ONE proposal of ONE kind ────────────
# Once the first matching kind won and every other file was
# silently dropped — so the unrelated file below was never checked and rode
# along into whatever the reviewer approved.
pm = proposal_from_pr(PR, {"manifests/xai-voice.yaml": GOOD_VOICE,
                           "README.md": "unrelated touch"})
check("intake: a file outside the three directories FAILS intake",
      bool(pm.intake_error), pm.intake_error)
check("intake: the refusal names the file it will not check",
      "README.md" in pm.intake_error, pm.intake_error)
check("intake: that refusal is the schema_valid verdict",
      not evaluate(pm).auto_stage
      and "README.md" in _named_detail(evaluate(pm), "schema_valid"),
      _named_detail(evaluate(pm), "schema_valid"))
check("intake: the dropped file is still ON the proposal for the reviewer",
      "README.md" in pm.files, str(set(pm.files)))

pmix = proposal_from_pr(PR, {"manifests/xai-voice.yaml": GOOD_VOICE,
                             "catalog-templates/thing.yaml": "services: {}\n"})
check("intake: a mixed-kind PR FAILS intake", bool(pmix.intake_error), pmix.intake_error)
for _f in ("manifests/xai-voice.yaml", "catalog-templates/thing.yaml"):
    check(f"intake: the mixed-kind refusal names {_f}",
          _f in pmix.intake_error, pmix.intake_error)
check("intake: the mixed-kind refusal says how many kinds",
      "touches 2" in pmix.intake_error, pmix.intake_error)

pempty = proposal_from_pr(PR, {})
check("intake: a PR adding no files FAILS intake",
      bool(pempty.intake_error) and not evaluate(pempty).auto_stage, pempty.intake_error)

pfr = proposal_from_pr(PR, {"feature-requests/a.md": "x", "feature-requests/b.md": "y"})
check("intake: two files of the SAME kind are fine",
      pfr.intake_error == "", pfr.intake_error)

# ── evaluate wiring: intake'd good manifest auto-stages ──────────────────────
check("wiring: good manifest auto_stage True", evaluate(p).auto_stage, str(evaluate(p).failures))
# off-allowlist held
po = proposal_from_pr(PR, {"manifests/sketchy-voice.yaml": OFF_ALLOWLIST})
check("wiring: off-allowlist held", not evaluate(po).auto_stage)

# ── intake: feature-request PR → FEATURE_REQUEST ────────────────────────────
pf = proposal_from_pr({"number": 8, "title": "Dark mode", "user": {"login": "u"}},
                      {"feature-requests/dark-mode.md": "Please add dark mode."})
check("intake: kind FEATURE_REQUEST", pf.kind is ProposalKind.FEATURE_REQUEST, pf.kind.value)
check("intake: feature body captured", "dark mode" in pf.body.lower(), pf.body)

# ── promote planning: manifest → correct platform PR plan ───────────────────
plan = plan_promotion(p, source_pr=7)
check("promote: targets platform repo", plan.repo == "owner/platform", plan.repo)
check("promote: base main", plan.base == "main", plan.base)
check("promote: branch promote/xai-voice", plan.branch == "promote/xai-voice", plan.branch)
check("promote: path translated to catalog",
      set(plan.files) == {"server/integrations/manifests/xai-voice.yaml"}, str(set(plan.files)))
check("promote: content preserved",
      plan.files["server/integrations/manifests/xai-voice.yaml"] == GOOD_VOICE)
check("promote: stems", plan.stems == ["xai-voice"], str(plan.stems))
check("promote: title references source PR", "community/proposals#7" in plan.title, plan.title)

# ── promote refuses a non-manifest ──────────────────────────────────────────
try:
    plan_promotion(pf)
    check("promote: feature request refused", False, "did not raise")
except NotPromotable:
    check("promote: feature request refused (NotPromotable)", True)

# ── phase 3: board-escalated request carries popularity via front matter ────
esc = proposal_from_pr(
    {"title": "Voice provider manifests", "user": {"login": "entity-1"}},
    {"feature-requests/thread-1-voice.md":
     "---\ntitle: Voice provider manifests\nsource_thread: 1\npopularity: 3\n---\n\nLet's discuss.\n"},
)
check("intake: popularity parsed from front matter", esc.popularity == 3, str(esc.popularity))
check("intake: body stripped of front matter",
      esc.body == "Let's discuss." and "---" not in esc.body, repr(esc.body))
plain = proposal_from_pr({"title": "t", "user": {"login": "x"}},
                         {"feature-requests/plain.md": "no front matter here"})
check("intake: front-matter-less request unchanged",
      plain.popularity == 0 and plain.body == "no front matter here", repr(plain.body))


# ── catalog-template promotion (batch 5) ────────────────────────────────────
cat = proposal_from_pr(
    {"title": "add linkding", "user": {"login": "e"}},
    {"catalog-templates/linkding.yaml": "# documentation: x\nservices: {}",
     "catalog-templates/overrides/linkding.json": "{}"})
check("intake: catalog paths -> CATALOG_TEMPLATE kind",
      cat.kind is ProposalKind.CATALOG_TEMPLATE and len(cat.files) == 2)
plan = plan_promotion(cat, source_pr=9)
check("catalog promote targets the ZONE repo",
      plan.repo == "owner/zone" and plan.base == "zone-main",
      f"{plan.repo}@{plan.base}")
check("catalog promote lands vendor + overrides beside the slice",
      "public-zone-agent/catalog-templates/vendor/linkding.yaml" in plan.files
      and "public-zone-agent/catalog-templates/overrides/linkding.json" in plan.files,
      str(sorted(plan.files)))
check("catalog promote branch + stems",
      plan.branch == "promote/catalog-linkding" and plan.stems == ["linkding"])

# ── bug reports: the fourth kind, a separate repo, never promoted ────────────
BUG_MD = (
    "---\ntitle: Tickets pane loses drafts\nkind: bug\nseverity: high\n"
    "host_version: cseries v0.16.83\nticket: 8f3a1c2e-1111-2222-3333-444455556666\n"
    "project: the Host itself\n---\n"
    "## Problem\n\nDrafts vanish.\n\n## Steps to reproduce\n\n1. open\n2. close\n\n"
    "## Expected\n\ndraft stays\n\n## Actual\n\ngone\n\n## Shared context\n\n"
    "### Privatae Host version and installed apps\n\n```versions\ncseries v0.16.83\n```\n"
)
bug = proposal_from_pr({"number": 12, "title": "bug: Tickets pane loses drafts",
                        "user": {"login": "cseries-proposals"}},
                       {"bugs/8f3a1c2e-tickets-pane-loses-drafts.md": BUG_MD})
check("intake: bugs/*.md -> BUG kind", bug.kind is ProposalKind.BUG, bug.kind.value)
check("intake: the report file stays on the proposal (no_secrets scans it)",
      set(bug.files) == {"bugs/8f3a1c2e-tickets-pane-loses-drafts.md"}, str(set(bug.files)))
check("intake: scalar metadata read off the front matter",
      bug.fields.get("severity") == "high" and bug.fields.get("ticket", "").startswith("8f3a1c2e"),
      str(bug.fields))
check("intake: body is the card + shared context, front matter stripped",
      bug.body.startswith("## Problem") and "## Shared context" in bug.body, bug.body[:40])
check("intake: a clean bug report auto-stages for the validator",
      evaluate(bug).auto_stage, str(evaluate(bug).failures))
mixed = proposal_from_pr(PR, {"bugs/x.md": BUG_MD, "manifests/xai-voice.yaml": GOOD_VOICE})
check("intake: a bug mixed with another kind FAILS intake, naming both",
      bool(mixed.intake_error) and "bugs/x.md" in mixed.intake_error, mixed.intake_error)
try:
    plan_promotion(bug)
    check("promote: a bug report never promotes", False, "did not raise")
except NotPromotable:
    check("promote: a bug report never promotes (NotPromotable)", True)

print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: proposals intake + promote planning")
