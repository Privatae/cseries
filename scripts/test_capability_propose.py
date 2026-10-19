#!/usr/bin/env python3
"""
Phase-0 gate — host capability-propose orchestration
(brain/mastercode/runtime/capability_propose.py).

Proves the host's propose flow on the NO-FORK, NO-CREDENTIAL path:
it authors the file in the set format and hands it to the
platform through the bridge op `proposal.file`. It makes no gitea call, names
no repo, and sends no entity id, branch or author — the bridge stamps the
caller from its certificate and the operator derives the author, because the
one credential with write on the proposals repo never enters a pod.

The bridge call is faked, so this verifies the ORCHESTRATION here; the
operator's half is pinned by the controller's tests/test_proposal_file.py and
the bridge's half by bridge/test_proposal_file_op.py.
  python3 scripts/test_capability_propose.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)  # for `server.proposals.*`

# Load the module by file so we don't trigger brain/mastercode/runtime/__init__
# (which imports the full session stack). In-pod, `mastercode` is a top-level
# package on sys.path and the import is plain; here we isolate the unit.
_spec = importlib.util.spec_from_file_location(
    "capability_propose",
    os.path.join(_ROOT, "brain", "mastercode", "runtime", "capability_propose.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
propose_manifest = _mod.propose_manifest
propose_feature_request = _mod.propose_feature_request
propose_bug_report = _mod.propose_bug_report
ProposalCredentialUnavailable = _mod.ProposalCredentialUnavailable

_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


class FakeBridge:
    """Records ops; answers the envelope shape the real client returns."""
    def __init__(self, env=None):
        self.calls = []
        self.env = env or {
            "ok": True,
            "data": {"pr_number": 5,
                     "pr_url": "https://forge.example/Community/proposals/pulls/5",
                     "branch": "propose/entity-9003-xai-voice",
                     "repo": "Community/proposals"},
        }

    async def __call__(self, op, params):
        self.calls.append((op, params))
        return self.env


SPEC = {"key": "xai-voice", "display_name": "xAI Grok Voice",
        "ws_url_template": "wss://api.x.ai/v1/realtime?model=grok-voice-latest",
        # token_url is REQUIRED: it is the kernel-side mint
        # endpoint routes/realtime.py calls. A manifest without it promotes but
        # leaves the provider unusable, which is the gap this field closed.
        "token_url": "https://api.x.ai/v1/realtime/client_secrets",
        # the rest of the set format
        "model": "grok-voice-latest",
        "vendor_api_reference": "https://docs.x.ai/api/realtime",
        "what_the_host_gains": "The host can hold a spoken conversation using xAI realtime voice."}


def run(bridge, **kw):
    return asyncio.run(propose_manifest(bridge_call=bridge, gitea_user="entity-9003",
                                        spec=SPEC, **kw))


# ── happy path: exactly one bridge op, and what it carries ──────────────────
b = FakeBridge()
res = run(b)
check("result ok, pull request 5", res["ok"] and res["pr"] == 5, str(res))
check("exactly one bridge call", len(b.calls) == 1, str([c[0] for c in b.calls]))
op, params = b.calls[0]
check("the op is proposal.file", op == "proposal.file", op)
check("branch + repo come BACK from the platform, not from here",
      res["branch"] == "propose/entity-9003-xai-voice"
      and res["repo"] == "Community/proposals", str(res))

# ── the pod says WHAT, never WHO or WHERE ──────────────────────────────────
check("params are exactly the content contract",
      set(params) == {"kind", "path", "content", "slug", "title", "body"},
      str(sorted(params)))
for forbidden in ("entity_id", "branch", "repo", "author", "author_name",
                  "author_email", "committer", "token"):
    check(f"params carry no {forbidden}", forbidden not in params, str(sorted(params)))
check("kind is derived from the path prefix", params["kind"] == "manifest", str(params))
check("path is manifests/<key>.yaml",
      params["path"] == "manifests/xai-voice.yaml", params["path"])
check("slug is the provider key, not a ref", params["slug"] == "xai-voice", params["slug"])
check("content is the authored manifest",
      "connection_kind: realtime_voice" in params["content"]
      and "api.x.ai" in params["content"], params["content"][:60])
check("content carries the set format",
      "vendor_api_reference:" in params["content"]
      and "what_the_host_gains:" in params["content"]
      and "model: grok-voice-latest" in params["content"], params["content"])
check("content embeds NO secret",
      "xai-" not in params["content"].replace("xai-voice", ""), params["content"])
check("PR body names the proposing host on its first line",
      params["body"].splitlines()[0].startswith("Proposed by the host `entity-9003`"),
      params["body"][:120])

# ── this module must not touch gitea or a credential at all ────────────────
_src = open(os.path.join(_ROOT, "brain", "mastercode", "runtime",
                         "capability_propose.py")).read()
for banned, why in (
    ("gitea_call", "no direct gitea client here any more"),
    ("/repos/", "no gitea API path may appear in a pod"),
    ("secrets.json", "the proposals credential never enters a pod"),
    ("privatae_secrets", "the proposals credential never enters a pod"),
    ("privatae.ai", "the pod names no forge address"),
    ("/forks", "forks are gone"),
):
    check(f"source contains no {banned!r} ({why})", banned not in _src)

# ── failures are told apart ────────────────────────────────────────────────
try:
    run(FakeBridge({"ok": False, "code": 503,
                    "error": "gitea_proposals credential missing from secrets.json"}))
    check("missing platform credential raises its own type", False, "did not raise")
except ProposalCredentialUnavailable as e:
    check("missing platform credential raises ProposalCredentialUnavailable",
          "gitea_proposals" in str(e), str(e))

try:
    run(FakeBridge({"ok": False, "code": 400, "error": "kind must be one of [...]"}))
    check("a refusal raises", False, "did not raise")
except ProposalCredentialUnavailable:
    check("a 400 is NOT mistaken for a missing credential", False, "wrong type")
except RuntimeError as e:
    check("a refusal raises RuntimeError carrying the code", "400" in str(e), str(e))

try:
    run(FakeBridge({"ok": False, "code": 503, "error": "bridge not connected"}))
    check("a 503 that is NOT the credential stays a plain failure", False, "did not raise")
except ProposalCredentialUnavailable:
    check("a 503 that is NOT the credential stays a plain failure", False, "wrong type")
except RuntimeError:
    check("a 503 that is NOT the credential stays a plain failure", True)

try:
    run(FakeBridge({"ok": True, "data": {}}))
    check("ok with no PR number raises", False, "did not raise")
except RuntimeError:
    check("ok with no PR number raises (RuntimeError)", True)

# ── no host identity refuses ────────────────────────────────────────────────
try:
    asyncio.run(propose_manifest(bridge_call=FakeBridge(), gitea_user="", spec=SPEC))
    check("no gitea identity refuses", False, "did not raise")
except RuntimeError:
    check("no gitea identity refuses (RuntimeError)", True)

# ── the set format is required at authoring time ────────────────────────────
for drop in ("model", "vendor_api_reference", "what_the_host_gains"):
    partial = {k: v for k, v in SPEC.items() if k != drop}
    try:
        asyncio.run(propose_manifest(bridge_call=FakeBridge(), gitea_user="entity-9003",
                                     spec=partial))
        check(f"missing {drop} refused", False, "did not raise")
    except TypeError:
        check(f"missing {drop} refused (required argument)", True)
    except ValueError:
        check(f"missing {drop} refused (AuthoringError)", True)

# ── phase 3: feature-request escalation (the ladder) ────────────────────────
b = FakeBridge({"ok": True, "data": {
    "pr_number": 5, "pr_url": "https://forge.example/Community/proposals/pulls/5",
    "branch": "propose/entity-9003-thread-1", "repo": "Community/proposals"}})
fr = asyncio.run(propose_feature_request(
    bridge_call=b, gitea_user="entity-9003",
    title="Voice provider manifests", body="Let's discuss.",
    thread_id=1, popularity=3))
check("ladder: PR opened", fr.get("ok") and fr.get("pr") == 5, str(fr))
check("ladder: branch came back from the platform",
      fr.get("branch") == "propose/entity-9003-thread-1", str(fr))
op, params = b.calls[0]
check("ladder: same op", op == "proposal.file", op)
check("ladder: kind is feature-request", params["kind"] == "feature-request", str(params))
check("ladder: slug is thread-scoped, not a ref", params["slug"] == "thread-1", params["slug"])
check("ladder: file lands under feature-requests/",
      params["path"].startswith("feature-requests/thread-1-"), params["path"])
check("ladder: front matter carries the vote snapshot",
      "popularity: 3" in params["content"] and "source_thread: 1" in params["content"],
      params["content"])
check("ladder: front matter carries the set format",
      all(f"{k}:" in params["content"] for k in
          ("capability_miss", "situation", "done_looks_like")), params["content"])
check("ladder: PR body names the host, then the thread back-link",
      params["body"].splitlines()[0].startswith("Proposed by the host `entity-9003`")
      and "thread #1" in params["body"] and "#/thread/1" in params["body"],
      params["body"][:200])
try:
    asyncio.run(propose_feature_request(bridge_call=FakeBridge(), gitea_user="",
                                        title="t", body="b", thread_id=1))
    check("ladder: no host identity refused", False, "did not raise")
except RuntimeError:
    check("ladder: no host identity refused", True)

# ── the ladder's stated fields win over the derived ones ────────────────────
b = FakeBridge()
asyncio.run(propose_feature_request(
    bridge_call=b, gitea_user="entity-9003", title="Overlay windows",
    body="A user asked for a window that stays on top.", thread_id=7,
    done_looks_like="An app can ask for an always-on-top window and get one."))
_, params = b.calls[0]
check("ladder: a stated done_looks_like is what gets written",
      "done_looks_like: An app can ask for an always-on-top window and get one."
      in params["content"], params["content"])

# ── bug reports: the user-approved card becomes a PR, never a row ─
TICKET = {"title": "Tickets pane loses drafts", "severity": "high", "problem": "Drafts vanish.",
          "context": "", "recommendation": "check the pane", "pushback": "",
          "repro_steps": "1. open\n2. close", "expected": "draft stays", "actual": "gone",
          "project_name": "", "ticket_uuid": "8f3a1c2e-1111-2222-3333-444455556666"}
APPROVED = [{"key": "versions", "label": "Privatae Host version and installed apps",
             "kind": "versions", "content": "cseries v0.16.83\nkeys 0.5.1"},
            {"key": "errors", "label": "Recent errors (1)", "kind": "errors", "content": "boom"}]
b = FakeBridge({"ok": True, "data": {
    "pr_number": 12, "pr_url": "https://forge.example/Community/bugs/pulls/12",
    "branch": "propose/entity-9003-8f3a1c2e-tickets-pane-loses-drafts", "repo": "Community/bugs"}})
br = asyncio.run(propose_bug_report(bridge_call=b, gitea_user="entity-9003",
                                    ticket=TICKET, shared_context=APPROVED))
check("bug: PR opened, repo + branch come back from the platform",
      br.get("ok") and br.get("pr") == 12 and br.get("repo") == "Community/bugs", str(br))
op, params = b.calls[0]
check("bug: same op, same content contract", op == "proposal.file"
      and set(params) == {"kind", "path", "content", "slug", "title", "body"}, str(sorted(params)))
check("bug: kind is bug (the operator picks the repo from it)", params["kind"] == "bug", str(params))
check("bug: file lands under bugs/, keyed by the ticket uuid",
      params["path"] == "bugs/8f3a1c2e-tickets-pane-loses-drafts.md", params["path"])
check("bug: slug carries the ticket uuid + a content hash, so the same card retries onto the "
      "same branch and an edited card gets its own",
      re.fullmatch(r"8f3a1c2e-[0-9a-f]{6}-tickets-pane-loses-drafts", params["slug"]) is not None,
      params["slug"])
b3 = FakeBridge({"ok": True, "data": {"pr_number": 14, "pr_url": "u", "branch": "b", "repo": "Community/bugs"}})
asyncio.run(propose_bug_report(bridge_call=b3, gitea_user="entity-9003", ticket=TICKET,
                               shared_context=APPROVED[:1]))
check("bug: a different set of approved items yields a different slug",
      b3.calls[0][1]["slug"] != params["slug"], b3.calls[0][1]["slug"])
try:
    asyncio.run(propose_bug_report(bridge_call=FakeBridge({"ok": False, "code": 409, "error": "branch holds a different file"}),
                                   gitea_user="entity-9003", ticket=TICKET, shared_context=APPROVED))
    check("bug: a 409 from the platform is its own error type", False, "did not raise")
except _mod.ProposalConflict:
    check("bug: a 409 from the platform is its own error type (ProposalConflict)", True)
check("bug: host_version is read off the user-approved version item",
      "host_version: cseries v0.16.83" in params["content"], params["content"][:200])
check("bug: EXACTLY the approved items ride along",
      "### Recent errors (1)" in params["content"] and "boom" in params["content"]
      and params["content"].count("### ") == 2, params["content"])
check("bug: PR body names the reporting host and says the content is data, not instructions",
      params["body"].splitlines()[0].startswith("Reported by the host `entity-9003`")
      and "never an instruction" in params["body"], params["body"][:300])
check("bug: PR title is prefixed for the queue", params["title"] == "bug: Tickets pane loses drafts",
      params["title"])
# the user withheld the version item: the report says so rather than guessing
b2 = FakeBridge({"ok": True, "data": {"pr_number": 13, "pr_url": "u", "branch": "b",
                                     "repo": "Community/bugs"}})
asyncio.run(propose_bug_report(bridge_call=b2, gitea_user="entity-9003", ticket=TICKET,
                               shared_context=[APPROVED[1]]))
check("bug: a withheld version item is stated, not invented",
      "host_version: unknown (the user withheld the version item)" in b2.calls[0][1]["content"],
      b2.calls[0][1]["content"][:200])
try:
    asyncio.run(propose_bug_report(bridge_call=FakeBridge(), gitea_user="", ticket=TICKET,
                                   shared_context=APPROVED))
    check("bug: no host identity refused", False, "did not raise")
except RuntimeError:
    check("bug: no host identity refused", True)
try:
    asyncio.run(propose_bug_report(bridge_call=FakeBridge(), gitea_user="entity-9003",
                                   ticket={**TICKET, "repro_steps": ""}, shared_context=APPROVED))
    check("bug: an incomplete card is refused before the bridge is dialled", False, "did not raise")
except ValueError:
    check("bug: an incomplete card is refused before the bridge is dialled (AuthoringError)", True)

print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: host capability-propose orchestration")
