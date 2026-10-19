"""
Proposals substrate — Phase 0 of the open-capability model.

The platform stays safe by a PROPOSAL-AND-APPROVAL gate, not by freezing
capability. Anyone (a host, a user) can PROPOSE a capability addition — most
importantly a new integration provider MANIFEST (e.g. an xAI / OpenAI /
ElevenLabs realtime-voice provider) — and it only reaches production after
review. This decouples what a host can offer from the core release cadence:
the ecosystem grows without the platform having to pre-bless every vendor.

Approval is GATED-AUTO: a proposal that passes the
automated checks in `checks.py` is auto-STAGED for a one-click human approval;
anything that fails is HELD for manual review with the reasons attached. The
human gate is never removed — the checks only remove the per-proposal busywork
for the clearly-safe ones. (Later evolution: once the gitea community lands,
`cseries/proposals` becomes the first community repo and staged proposals are
reviewed in popularity order — `Proposal.popularity` is the seed for that,
captured from day one.)

This package is PURE proposal logic (contract + checks). It performs no git
work and mutates nothing: the repo transport (push to the proposals repo) and
the promote-on-approval path are separate wiring that CALL into these checks.
"""

from server.proposals.checks import (
    CheckResult,
    ProposalVerdict,
    evaluate,
    ALLOWED_EGRESS_HOSTS,
)
from server.proposals.model import Proposal, ProposalKind

__all__ = [
    "Proposal",
    "ProposalKind",
    "CheckResult",
    "ProposalVerdict",
    "evaluate",
    "ALLOWED_EGRESS_HOSTS",
]
