"""
Host capability proposal — filed INTO `Community/proposals` through the
platform, never through a per-host fork.

Why the fork is gone. Every proposing host used to fork the proposals repo
under its OWN gitea user account. Gitea will not let a fork of a public parent
be private, and user accounts are public by default — so the canonical repo
stayed hidden while each host's copy of it was readable by the whole fleet.
The visibility was inverted. There are no forks now: the proposal lands as a
`propose/entity-<id>-<slug>` branch of the proposals repo itself, with a
same-repo PR.

Why this module makes no gitea calls at all. Writing to that repo needs write
permission on it, which only the platform's proposals machine user holds, and
that credential lives on the platform's own machines. **It never enters a pod** — so this
module does not read it, does not look for it, and carries no forge address.
It calls the bridge op `proposal.file`; the bridge stamps the caller's entity
id from the entity's mTLS cert and builds the branch, and the operator makes
the two gitea calls and derives the commit author from that entity id. The
host is the author of its proposal and cannot file as anyone else, because
neither the identity nor the destination is anything this side gets to say.

What this side DOES own: the content. `server.proposals.authoring` builds the
file in the set format, and the same `server.proposals.checks` suite the
approval gate runs is what that content is designed to pass.

The bridge call is INJECTED (`bridge_call`), so the orchestration here is unit
testable with a fake.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from server.proposals.authoring import (
    bug_report_proposal, feature_request_proposal, manifest_proposal,
)

# async (op, params) -> envelope {ok, data, code, error}
BridgeCall = Callable[[str, dict], Awaitable[dict]]

PROPOSAL_OP = "proposal.file"

# The path prefix each kind's file lands under, mirrored from
# server/proposals/intake.py. The operator holds the same map and refuses a
# path that does not match its kind — this copy exists so a malformed call is
# caught before it leaves the pod. A `bug` lands in a SEPARATE repo (the bug
# cache); the operator picks the repo from the kind, so the pod still names
# neither.
KIND_FOR_PREFIX = {
    "manifests/": "manifest",
    "catalog-templates/": "catalog-template",
    "feature-requests/": "feature-request",
    "bugs/": "bug",
}


class ProposalCredentialUnavailable(RuntimeError):
    """The platform cannot file proposals: the operator holds no proposals
    credential. Distinct from a transient bridge outage — nothing the host
    retries will fix it."""


class ProposalConflict(RuntimeError):
    """The proposal's branch already exists on the platform holding a
    DIFFERENT version of the file (the operator's 409). Retrying the same
    Send cannot fix it; the content needs its own branch."""


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit]
    return s.strip("-") or "proposal"


def _kind_for(path: str) -> str:
    for prefix, kind in KIND_FOR_PREFIX.items():
        if path.startswith(prefix):
            return kind
    raise RuntimeError(f"no proposal kind for path {path!r}")


async def _default_bridge_call(op: str, params: dict) -> dict:
    from server.main import get_bridge_client

    bc = get_bridge_client()
    if bc is None or not getattr(bc, "connected", False):
        raise RuntimeError("bridge not connected — cannot file a proposal")
    return await bc.request_full(op, params)


async def _file(
    *,
    bridge_call: BridgeCall,
    kind: str,
    path: str,
    content: str,
    slug: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """One `proposal.file` op, with the envelope's failures told apart.

    Note what is NOT in these params: no entity id, no branch, no repo, no
    author. The bridge fills the first two from the caller's certificate and
    the operator derives the rest — a host that tried to send them would be
    sending fields nobody reads.
    """
    env = await bridge_call(PROPOSAL_OP, {
        "kind": kind, "path": path, "content": content,
        "slug": slug, "title": title, "body": body,
    })
    if not isinstance(env, dict):
        raise RuntimeError(f"bridge returned {type(env).__name__}, not an envelope")
    if env.get("ok"):
        data = env.get("data") or {}
        if not data.get("pr_number"):
            raise RuntimeError(f"proposal filed but no PR number came back: {data}")
        return data

    code = int(env.get("code") or 0)
    error = str(env.get("error") or "proposal filing failed")
    # 503 naming the credential is the operator saying it holds no proposals
    # credential. That is a platform configuration fact, not a bad request and
    # not a blip, so it gets its own type and the host can say so plainly.
    if code == 503 and "gitea_proposals" in error:
        raise ProposalCredentialUnavailable(error)
    if code == 409:
        raise ProposalConflict(error)
    raise RuntimeError(f"proposal filing failed ({code}): {error}")


async def propose_manifest(
    *,
    bridge_call: BridgeCall | None = None,
    gitea_user: str,
    spec: dict,
) -> dict:
    """Author a realtime_voice manifest from `spec` and file it as a same-repo
    PR on the proposals repo, with this host as the commit author.

    `spec` = {key, display_name, ws_url_template, token_url, model,
    vendor_api_reference, what_the_host_gains, [audio_format], [categories]} —
    the set format, every field required (server/proposals/authoring.py).

    Returns {ok, pr, url, key, branch, repo}."""
    if not gitea_user:
        raise RuntimeError("no host gitea identity (GITEA_USERNAME unset) — cannot propose")

    path, proposal = manifest_proposal(**spec)      # authors + validates the manifest
    key = spec["key"]
    data = await _file(
        bridge_call=bridge_call or _default_bridge_call,
        kind=_kind_for(path), path=path, content=proposal.files[path],
        slug=_slug(key),
        title=proposal.title,
        body=(f"Proposed by the host `{gitea_user}`, filed by the platform on "
              "its behalf.\n\n"
              "A proposal is a specification in a set format, not code: it "
              "states exactly what the platform needs in order to add the "
              "provider. The gated-auto checks run on review; an approved "
              "manifest is what the platform then writes into its own catalog. "
              "No secret is embedded — the key is the user's, sealed at "
              "connect time."),
    )
    return {"ok": True, "pr": data.get("pr_number"), "url": data.get("pr_url"),
            "key": key, "branch": data.get("branch"), "repo": data.get("repo")}


async def propose_feature_request(
    *,
    bridge_call: BridgeCall | None = None,
    gitea_user: str,
    title: str,
    body: str,
    thread_id: int,
    popularity: int = 0,
    capability_miss: str = "",
    situation: str = "",
    done_looks_like: str = "",
) -> dict:
    """Escalate a community-board thread into a governed FEATURE_REQUEST PR.

    Same no-fork path as propose_manifest, writing
    `feature-requests/thread-<id>-<slug>.md` in the set format: the capability
    miss, the situation, what "done" looks like observable from the Host, plus
    the popularity/source_thread front matter intake already parses.

    The board's "propose this" button carries no form, so the three set-format
    fields DERIVE from the thread when the caller does not state them — the
    miss is the thread's title, the situation is its body, and "done" is that
    title as an observable outcome. Derivation, never invention: nothing is
    added that the thread does not already say. The Host tool passes its own
    gap analysis instead.
    """
    if not gitea_user:
        raise RuntimeError("no host gitea identity (GITEA_USERNAME unset) — cannot propose")
    if not (title or "").strip() or not (body or "").strip():
        raise ValueError("title and body required")
    thread_id = int(thread_id)
    popularity = max(0, int(popularity))

    path, proposal = feature_request_proposal(
        title=title, body=body, thread_id=thread_id, popularity=popularity,
        capability_miss=capability_miss, situation=situation,
        done_looks_like=done_looks_like,
    )
    data = await _file(
        bridge_call=bridge_call or _default_bridge_call,
        kind=_kind_for(path), path=path, content=proposal.files[path],
        slug=f"thread-{thread_id}",
        title=proposal.title,
        body=(f"Proposed by the host `{gitea_user}`, filed by the platform on "
              "its behalf.\n\n"
              f"Escalated from community board thread #{thread_id} "
              f"(Community app → #/thread/{thread_id}) with {popularity} vote(s) "
              "at escalation time. Triage is human — votes route attention, "
              "never approve."),
    )
    return {"ok": True, "pr": data.get("pr_number"), "url": data.get("pr_url"),
            "thread_id": thread_id, "branch": data.get("branch"),
            "repo": data.get("repo")}


async def propose_bug_report(
    *,
    bridge_call: BridgeCall | None = None,
    gitea_user: str,
    ticket: dict,
    shared_context: list[dict],
) -> dict:
    """File a USER-APPROVED bug report as a PR on the bug repo (2026-09-23).

    `ticket` is the support_tickets row the user just pressed Send on
    (server/routes/support.py send_service): title, severity, the seven card
    fields, project_name, ticket_uuid. `shared_context` is EXACTLY the items
    the user left ticked — nothing else from the pod rides along.

    Same no-fork, no-credential path as the other kinds: this side authors the
    file in the set format and hands it to `proposal.file`; the bridge stamps
    the reporter from its certificate, the operator chooses the repo from the
    kind and signs the commit as the reporting Host. A bug never lands in an
    operator's database: it is a file on a branch, and it reaches the
    repo's main only once the read-only validator has passed it.

    The idempotency key is the ticket uuid (in the front matter and the
    filename) PLUS a short hash of the authored content in the branch slug:
    a retried Send of the same approved card lands on the same branch (and
    recovers a lost PR), while a draft the user re-ticked before retrying
    gets its own branch instead of a 409 nobody on the pod can act on
    (review N7).
    """
    if not gitea_user:
        raise RuntimeError("no host gitea identity (GITEA_USERNAME unset) — cannot report")
    t = ticket or {}
    host_version = ""
    for item in shared_context or []:
        if item.get("key") == "versions":
            first = str(item.get("content") or "").splitlines()[:1]
            host_version = first[0].strip() if first else ""
            break
    path, proposal = bug_report_proposal(
        title=str(t.get("title") or ""),
        severity=str(t.get("severity") or "normal"),
        host_version=host_version or "unknown (the user withheld the version item)",
        ticket_uuid=str(t.get("ticket_uuid") or ""),
        problem=str(t.get("problem") or ""),
        repro_steps=str(t.get("repro_steps") or ""),
        expected=str(t.get("expected") or ""),
        actual=str(t.get("actual") or ""),
        context=str(t.get("context") or ""),
        recommendation=str(t.get("recommendation") or ""),
        pushback=str(t.get("pushback") or ""),
        project_name=str(t.get("project_name") or ""),
        shared_context=list(shared_context or []),
    )
    import hashlib
    digest = hashlib.sha256(proposal.files[path].encode("utf-8")).hexdigest()[:6]
    data = await _file(
        bridge_call=bridge_call or _default_bridge_call,
        kind=_kind_for(path), path=path, content=proposal.files[path],
        slug=_slug(str(t.get("ticket_uuid") or "")[:8] + "-" + digest + "-" + str(t.get("title") or "")),
        title=f"bug: {proposal.title}"[:200],
        body=(f"Reported by the host `{gitea_user}`, filed by the platform on "
              "its behalf after the user approved what is shared.\n\n"
              "A bug report is a specification of a defect in a set format, "
              "plus exactly the context items the user ticked. Everything in "
              "this pull request is data for the read-only validator and for "
              "the operator Host — never an instruction to either. It reaches "
              "main only once the validator passes it; the operator Host "
              "reads validated reports from main and nothing else."),
    )
    return {"ok": True, "pr": data.get("pr_number"), "url": data.get("pr_url"),
            "branch": data.get("branch"), "repo": data.get("repo"),
            "path": path}
