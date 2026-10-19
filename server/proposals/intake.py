"""
Intake — turn a `community/proposals` pull request into a `Proposal`.

Pure: given a PR's metadata + its proposed file contents, build the Proposal the
gated-auto checks (`checks.py`) evaluate. No gitea I/O here — the route fetches
the files and calls this, so the transform stays unit-testable.
"""

from __future__ import annotations

import re

from server.proposals.model import Proposal, ProposalKind

# Where each proposal kind's files live in the proposals repo.
MANIFEST_SRC_PREFIX = "manifests/"
CATALOG_SRC_PREFIX = "catalog-templates/"
FEATURE_SRC_PREFIX = "feature-requests/"
# Bug reports live in a SEPARATE repo (the bug cache, Community/bugs) but keep
# the same one-kind-directory contract, so one intake reads both repos.
BUG_SRC_PREFIX = "bugs/"

_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    (MANIFEST_SRC_PREFIX, "manifest"),
    (CATALOG_SRC_PREFIX, "catalog-template"),
    (FEATURE_SRC_PREFIX, "feature-request"),
    (BUG_SRC_PREFIX, "bug"),
)
_PREFIX_LIST = ", ".join(p for p, _ in _KIND_PREFIXES)

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_feature_front_matter(text: str) -> tuple[dict, str]:
    """Split an optional `--- ... ---` front-matter block off a feature-request
    or bug-report file. Returns ({key: value}, body-without-front-matter).
    Board-escalated requests (capability_propose.propose_feature_request) carry
    `popularity:` (the vote snapshot) and `source_thread:` here; a bug report
    carries its scalar metadata (severity, host_version, ticket); hand-written
    files without front matter parse as ({}, full text)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()
    return fields, text[m.end():]


def _is_manifest(path: str) -> bool:
    return path.startswith(MANIFEST_SRC_PREFIX) and path.endswith((".yaml", ".yml"))


def _is_catalog(path: str) -> bool:
    """A service-catalog template (Coolify-format compose YAML) or its
    optional per-template overrides sidecar."""
    if not path.startswith(CATALOG_SRC_PREFIX):
        return False
    if path.startswith(CATALOG_SRC_PREFIX + "overrides/"):
        return path.endswith(".json")
    return path.endswith((".yaml", ".yml"))


def _is_bug(path: str) -> bool:
    return path.startswith(BUG_SRC_PREFIX) and path.endswith(".md")


def _classify(path: str) -> str | None:
    """The kind directory `path` belongs to, or None if it is outside all
    four. Directory membership only — `manifests/README.md` is inside the
    manifest directory and still not a manifest, which is a shape error the
    kind's own checks report, not an off-contract one."""
    for prefix, kind in _KIND_PREFIXES:
        if path.startswith(prefix):
            return kind
    return None


def _intake_error(files: dict[str, str]) -> str:
    """Refuse a PR that is not ONE proposal of ONE kind, naming every file.

    Until 2026-09-16 this did not exist: `proposal_from_pr` took the first
    matching kind and SILENTLY DROPPED every other file. A PR adding a manifest
    and a catalog template was evaluated as a manifest, and the template was
    never checked at all — it would ride into whatever the reviewer approved
    having passed nothing. The same hole swallowed files outside the three
    directories entirely.

    Returns "" when the PR is a clean single-kind proposal.
    """
    if not files:
        return ("the PR adds no files — a proposal is a file under one of "
                f"{_PREFIX_LIST}")
    kinds: dict[str, list[str]] = {}
    stray: list[str] = []
    for path in sorted(files):
        kind = _classify(path)
        if kind is None:
            stray.append(path)
        else:
            kinds.setdefault(kind, []).append(path)
    if stray:
        return ("a proposal may only touch its own kind directory; these "
                f"file(s) are outside {_PREFIX_LIST}, and were NOT "
                "checked: " + ", ".join(stray))
    if len(kinds) > 1:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(kinds.items()))
        return (f"a PR must propose ONE kind — this one touches {len(kinds)} "
                f"({detail}). Split it; otherwise the files of whichever kind "
                "is not evaluated ride along unchecked.")
    return ""


def proposal_from_pr(pr: dict, files: dict[str, str]) -> Proposal:
    """Build a Proposal from a PR's metadata + the {repo_path: content} it adds.

    Kind is inferred from the paths: `manifests/*.yaml` → MANIFEST,
    `catalog-templates/*` → CATALOG_TEMPLATE, `bugs/*.md` → BUG; otherwise →
    FEATURE_REQUEST (body = the `feature-requests/*` text).

    A PR that is not ONE proposal of ONE kind — mixed kinds, or any file
    outside the four directories — carries `intake_error`, which schema_valid
    reports and fails. Nothing is silently dropped.
    """
    title = str(pr.get("title") or "").strip()
    author = ((pr.get("user") or {}).get("login")) or None

    error = _intake_error(files)
    if error:
        # Keep every file on the Proposal so the operator surface still shows
        # what was proposed; the verdict is the refusal.
        return Proposal(
            kind=ProposalKind.FEATURE_REQUEST,
            title=title,
            files=dict(files),
            intake_error=error,
            submitter_entity_id=author,
        )

    manifest_files = {p: c for p, c in files.items() if _is_manifest(p)}
    if manifest_files:
        return Proposal(
            kind=ProposalKind.MANIFEST,
            title=title,
            files=manifest_files,
            submitter_entity_id=author,
        )

    catalog_files = {p: c for p, c in files.items() if _is_catalog(p)}
    if catalog_files:
        return Proposal(
            kind=ProposalKind.CATALOG_TEMPLATE,
            title=title,
            files=catalog_files,
            submitter_entity_id=author,
        )

    bug_files = {p: c for p, c in files.items() if p.startswith(BUG_SRC_PREFIX)}
    if bug_files:
        # ONE report per PR. The card's scalar metadata is the front matter;
        # the prose and the user-approved shared context are the body. Every
        # byte stays on `files` too, so no_secrets scans what the validator
        # and the operator Host will actually read.
        front: dict[str, str] = {}
        body_parts: list[str] = []
        for p, c in sorted(bug_files.items()):
            fields, body = _parse_feature_front_matter(c)
            front.update(fields)
            body_parts.append(body)
        return Proposal(
            kind=ProposalKind.BUG,
            title=title,
            files=bug_files,
            body="\n\n".join(body_parts).strip(),
            fields=front,
            submitter_entity_id=author,
        )

    popularity = 0
    body_parts = []
    front: dict[str, str] = {}
    for p, c in sorted(files.items()):
        if not p.startswith(FEATURE_SRC_PREFIX):
            continue
        fields, body = _parse_feature_front_matter(c)
        # The set format lives in the front matter (capability_miss, situation,
        # done_looks_like, source_thread, popularity). Carry it onto the
        # Proposal so the required-fields half of schema_valid can see it.
        front.update(fields)
        body_parts.append(body)
        try:
            # Vote snapshot from a board-escalated request — seeds the
            # community-ranked review order. NEVER gates approval.
            popularity = max(popularity, int(fields.get("popularity", 0)))
        except ValueError:
            pass
    return Proposal(
        kind=ProposalKind.FEATURE_REQUEST,
        title=title,
        body="\n\n".join(body_parts).strip(),
        fields=front,
        submitter_entity_id=author,
        popularity=popularity,
    )
