"""
Proposal contract — the shape of a capability proposal.

Deliberately minimal and transport-agnostic: a proposal is a set of proposed
file contents plus metadata. The gated-auto checks (`checks.py`) operate on
this; the repo transport and promote-on-approval path construct and consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProposalKind(str, Enum):
    # A new/updated integration provider manifest (the voice/provider-kind case).
    MANIFEST = "manifest"
    # A service-catalog compose template for the public webapp zone (one-click
    # third-party stacks).
    CATALOG_TEMPLATE = "catalog_template"
    # A plain-language feature request (no code). Wire into any existing user
    # feature-request intake rather than duplicating it.
    FEATURE_REQUEST = "feature_request"
    # A bug report against the platform (2026-09-23). The reporter's Host
    # drafts it, the USER approves what leaves the pod, and the approved card
    # is filed as a PR on the bug repo — never as a row in an operator's
    # database. Main of that repo is the cache of VALIDATED bugs: a report
    # reaches it only after the read-only validator has passed it, and the
    # operator Host reads only from there.
    BUG = "bug"


@dataclass
class Proposal:
    """A capability proposal awaiting the gated-auto checks + human approval.

    `files` maps a repo-relative path -> the proposed full file content. For a
    MANIFEST proposal these are the `*.yaml` provider manifests (path under the
    integrations manifests dir). For a FEATURE_REQUEST, `files` may be empty and
    the request lives in `title` + `body`. For a BUG the one `bugs/*.md` file is
    kept on `files` AND its text is on `body` with its front matter on `fields`,
    so no_secrets scans the same bytes the validator and operator will read.
    """
    kind: ProposalKind
    title: str
    files: dict[str, str] = field(default_factory=dict)
    body: str = ""
    # Set-format fields parsed off the proposal's front matter (a feature
    # request's are its whole specification, so the required-fields half of
    # `schema_valid` reads them from here rather than re-parsing the file).
    fields: dict[str, str] = field(default_factory=dict)
    # Set by intake when the PR is not a single-kind proposal at all — a PR
    # touching more than one kind directory, or carrying files outside the
    # three. `schema_valid` reports this FIRST and fails: a verdict computed
    # from a subset of a PR's files is a verdict about something other than
    # what the reviewer is looking at.
    intake_error: str = ""
    submitter_entity_id: Optional[str] = None
    # Seed for the future community-ranked (path-c) review order — captured from
    # day one even though ranking isn't built yet. Never gates approval.
    popularity: int = 0
