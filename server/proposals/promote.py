"""
Promote — plan the platform-side landing of an APPROVED manifest proposal.

Approval promotes a manifest from `community/proposals` into the platform source
(`server/integrations/manifests/`) as a PULL REQUEST — it does NOT ship. The
manifest still rides the normal release gate after the promote PR
merges, so approval is "stage for the platform PR queue", a second gate before
production, never an auto-deploy.

Pure: `plan_promotion` computes the branch + target file paths + PR text from a
Proposal. The route executes the plan via gitea. Unit-testable without I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from server.proposals.intake import CATALOG_SRC_PREFIX, MANIFEST_SRC_PREFIX
from server.proposals.model import Proposal, ProposalKind

# Where an approved manifest lands in the platform repo (image-shipped catalog).
MANIFEST_DST_PREFIX = "server/integrations/manifests/"

# The platform source repo ("owner/name") the promote PR targets. No default:
# unset, nothing promotes (plan_promotion raises NotPromotable).
PLATFORM_REPO = os.environ.get("CSERIES_PLATFORM_REPO", "").strip()
PLATFORM_BASE = os.environ.get("CSERIES_PLATFORM_BASE", "main")

# A service-catalog template lands in the ZONE's source repo, beside the
# vendored slice — vendor compose under vendor/, an
# optional overrides sidecar, and (added by the operator gate at approve time)
# the rendered skeleton record. Admission to the live catalog_templates table
# is a separate operator step after the promote PR merges (catalog_admit.py) —
# same two-gate shape as manifests riding the release gate. No default repo:
# unset, a catalog template does not promote.
ZONE_REPO = os.environ.get("CSERIES_ZONE_REPO", "").strip()
ZONE_BASE = os.environ.get("CSERIES_ZONE_BASE", "main")
CATALOG_DST_PREFIX = "public-zone-agent/catalog-templates/"


class NotPromotable(ValueError):
    """The proposal has nothing to promote into the manifest catalog."""


@dataclass
class PromotionPlan:
    repo: str                      # platform repo the PR targets
    base: str                      # base branch
    branch: str                    # new branch the promote commit lands on
    files: dict[str, str]          # {platform_dst_path: content}
    title: str
    body: str
    stems: list[str] = field(default_factory=list)  # manifest keys promoted


def _dst_path(src_path: str) -> str:
    # manifests/xai-voice.yaml -> server/integrations/manifests/xai-voice.yaml
    return MANIFEST_DST_PREFIX + src_path[len(MANIFEST_SRC_PREFIX):]


def _plan_catalog_promotion(proposal: Proposal,
                            source_pr: int | None) -> PromotionPlan:
    """Promote PR for an approved CATALOG_TEMPLATE proposal: vendor yaml (and
    any overrides sidecar) land verbatim under the zone's catalog-templates/.
    The operator gate appends the rendered skeleton record to plan.files
    before executing (render + digest pinning happen there, not here — this
    stays pure)."""
    if not ZONE_REPO:
        raise NotPromotable("no zone repo configured (CSERIES_ZONE_REPO)")
    files: dict[str, str] = {}
    stems: list[str] = []
    for src_path, content in sorted(proposal.files.items()):
        rel = src_path[len(CATALOG_SRC_PREFIX):]
        if rel.startswith("overrides/"):
            files[CATALOG_DST_PREFIX + rel] = content
        else:
            files[CATALOG_DST_PREFIX + "vendor/" + rel] = content
            stems.append(rel.rsplit(".", 1)[0])
    slug = "-".join(stems) or "template"
    src_ref = f" (community/proposals#{source_pr})" if source_pr else ""
    return PromotionPlan(
        repo=ZONE_REPO,
        base=ZONE_BASE,
        branch=f"promote/catalog-{slug}",
        files=files,
        title=f"promote: {', '.join(stems)} catalog template{src_ref}",
        body=(
            "Approved service-catalog proposal, promoted from the community "
            "proposals repo into the zone's template source. Admission to the "
            "live catalog (catalog_admit.py on the rendered record) is the "
            "operator step AFTER this PR merges — a second gate, never an "
            "auto-deploy.\n\n"
            f"Templates: {', '.join(stems)}\n"
            f"Source: community/proposals" + (f"#{source_pr}" if source_pr else "")
        ),
        stems=stems,
    )


def plan_promotion(proposal: Proposal, *, source_pr: int | None = None) -> PromotionPlan:
    """Compute the platform promote PR for an approved MANIFEST or
    CATALOG_TEMPLATE proposal.

    Raises NotPromotable for a feature request (triaged by a human, nothing
    lands in any catalog)."""
    if proposal.kind is ProposalKind.CATALOG_TEMPLATE and proposal.files:
        return _plan_catalog_promotion(proposal, source_pr)
    if proposal.kind is not ProposalKind.MANIFEST or not proposal.files:
        raise NotPromotable(
            "only a manifest or catalog-template proposal promotes "
            f"(kind={proposal.kind.value}, files={len(proposal.files)})"
        )
    if not PLATFORM_REPO:
        raise NotPromotable("no platform repo configured (CSERIES_PLATFORM_REPO)")

    files: dict[str, str] = {}
    stems: list[str] = []
    for src_path, content in sorted(proposal.files.items()):
        dst = _dst_path(src_path)
        files[dst] = content
        stems.append(src_path[len(MANIFEST_SRC_PREFIX):].rsplit(".", 1)[0])

    slug = "-".join(stems) or "manifest"
    src_ref = f" (community/proposals#{source_pr})" if source_pr else ""
    title = f"promote: {', '.join(stems)} provider manifest{src_ref}"
    body = (
        "Approved capability proposal, promoted from the community proposals repo "
        "into the platform manifest catalog. Ships via the normal update gate after "
        "merge.\n\n"
        f"Manifests: {', '.join(stems)}\n"
        f"Source: community/proposals" + (f"#{source_pr}" if source_pr else "")
    )
    return PromotionPlan(
        repo=PLATFORM_REPO,
        base=PLATFORM_BASE,
        branch=f"promote/{slug}",
        files=files,
        title=title,
        body=body,
        stems=stems,
    )
