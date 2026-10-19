"""
Gated-auto check suite — the automated half of the proposal approval gate.

A proposal that PASSES every check here is auto-STAGED for a one-click human
approval. A proposal that FAILS any check is HELD for manual review, with the
failing checks + reasons attached. The checks never approve anything on their
own — the human gate is always the final step. They exist so the clearly-safe
proposals don't cost a manual review each.

The three checks, in order of what they protect:

  1. schema_valid   — TWO halves. The LOADER half: a MANIFEST proposal's YAML
                      must parse into a Provider via the SAME loader the live
                      catalog uses (no divergent validator). The SET FORMAT
                      half: a proposal is a specification, not code, so
                      every field its kind's fixed schema names
                      must be present — a missing one fails intake with the
                      field named. The loader stays the schema for the manifest
                      BODY; the required-fields list sits on top of it.
  2. egress_allowlisted — the security-bearing one. A provider manifest that
                      declares an outbound endpoint (a realtime WSS url, a proxy
                      base url) must target a host on the reviewed allowlist. A
                      realtime-voice manifest is fundamentally "open a socket to
                      an arbitrary URL with a sealed token" — so an off-allowlist
                      target is exactly what must NOT auto-stage; it drops to
                      manual review. Adding a host to the allowlist is itself a
                      reviewed change (this file ships in the image).
  3. no_secrets     — no proposal file may embed a credential. A manifest names
                      an auth MODE and a vault cred_type; it must never carry a
                      literal key/token. A hit drops to manual review. Scans the
                      body and front-matter fields too (a feature request or a
                      bug report has no other bytes).

A BUG report (2026-09-23) passes the same three checks on its way to the
validator: the set format (front matter + fixed sections), egress n/a, and no
secret in the card or the user-approved shared context. Passing here is NOT
validation — the read-only validator's verdict is a separate, later step, and
only a validated report reaches the operator Host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

from server.proposals.model import Proposal, ProposalKind


# ── Reviewed egress allowlist ──────────────────────────────────────────────
# Hosts a proposed provider manifest may point its outbound endpoint at and
# still AUTO-STAGE. Anything else is not rejected — it drops to MANUAL review
# (a human can still approve a new provider host; it just doesn't skip review).
# Adding an entry here is a reviewed code change that ships in the image.
ALLOWED_EGRESS_HOSTS: frozenset[str] = frozenset({
    "api.x.ai",           # xAI Grok (incl. realtime voice)
    "api.openai.com",     # OpenAI (incl. Realtime)
    "api.elevenlabs.io",  # ElevenLabs
})


# ── Secret patterns ────────────────────────────────────────────────────────
# Pragmatic, high-signal formats. A manifest should reference a vault cred_type,
# never embed a secret, so any hit is disqualifying for auto-stage.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai/xai key",  re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("xai key",         re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("github token",    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws access key",  re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    # A real bearer token mixes letters and digits. Since the body of a
    # feature request or bug report is scanned too (2026-09-23), the plain
    # word "bearer" followed by prose ("bearer authentication for webhooks")
    # must not read as a leaked credential — two lookaheads require both.
    ("bearer token",    re.compile(r"[Bb]earer\s+(?=[A-Za-z0-9\-._~+/]*\d)(?=[A-Za-z0-9\-._~+/]*[A-Za-z])"
                                   r"[A-Za-z0-9\-._~+/]{20,}")),
)

# Manifest auth keys that must carry a REFERENCE (mode / cred_type / scopes),
# never a literal secret value. A value under one of these = an embedded key.
_FORBIDDEN_AUTH_VALUE_KEYS = ("api_key", "token", "secret", "client_secret", "password")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ProposalVerdict:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def auto_stage(self) -> bool:
        """True → auto-stage for one-click approval. False → hold for manual
        review. Identical to `passed`; named separately so callers read as the
        gate intends and so the policy has one obvious place to change."""
        return self.passed

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


# ── individual checks ──────────────────────────────────────────────────────

def _manifest_docs(proposal: Proposal) -> list[tuple[str, dict]]:
    """Parse every *.yaml file in the proposal into (path, mapping). Raises
    nothing — malformed YAML surfaces as a schema_valid failure below."""
    out: list[tuple[str, dict]] = []
    for path, content in proposal.files.items():
        if not path.endswith((".yaml", ".yml")):
            continue
        data = yaml.safe_load(content)
        out.append((path, data if isinstance(data, dict) else {}))
    return out


# ── Catalog-template structural rules (DoR §3.2 reject list, checkable
#    without the zone renderer). The FULL admission check — compose→skeleton
#    translation, magic-env grammar, image digest pinning, injection guards —
#    runs at the OPERATOR gate (/proposals CLI), where catalog_render.py and
#    docker live. This entity-side subset exists so a clearly-bad compose
#    never auto-stages and the submitter gets fast, specific feedback.
_CATALOG_REJECT_KEYS = frozenset({
    "privileged", "cap_add", "security_opt", "network_mode", "pid", "ipc",
    "build", "ports", "devices", "sysctls", "user",
})
# The set format for a catalog-template proposal: the upstream project and the
# version being proposed, plus the one-sentence gain, on top of the three
# header fields the zone renderer already needed. The image/install source and
# the ports are enforced structurally below (a literal pinnable `image:` per
# service; a `# port:` header or a SERVICE_URL/FQDN declaration) — volumes and
# env NAMES come from the compose itself, and env VALUES that look like a
# credential are held by check_no_secrets.
_CATALOG_HEADER_REQUIRED = ("documentation", "slogan", "category",
                            "upstream", "version", "gain")
_CATALOG_RESERVED_TOKENS = ("__SECRET_", "__SETTING_", "__SITE_ID__",
                            "__NODEPORT__", "__HOST__")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _catalog_header(text: str) -> dict:
    hdr: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("#"):
            break
        m = re.match(r"#\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m:
            hdr[m.group(1).lower()] = m.group(2).strip()
    return hdr


def _check_catalog_template(proposal: Proposal) -> CheckResult:
    import json as _json
    yaml_files = [p for p in proposal.files
                  if p.endswith((".yaml", ".yml")) and "/overrides/" not in p]
    if not yaml_files:
        return CheckResult("schema_valid", False,
                           "catalog proposal has no template .yaml")
    for path, content in proposal.files.items():
        for tok in _CATALOG_RESERVED_TOKENS:
            if tok in content:
                return CheckResult(
                    "schema_valid", False,
                    f"{path}: contains reserved placeholder text {tok!r}")
        if "/overrides/" in path:
            try:
                if not isinstance(_json.loads(content), dict):
                    raise ValueError("not an object")
            except Exception as e:
                return CheckResult("schema_valid", False,
                                   f"{path}: overrides must be a JSON object ({e})")
    for path in yaml_files:
        content = proposal.files[path]
        try:
            doc = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return CheckResult("schema_valid", False, f"{path}: invalid YAML ({e})")
        services = (doc or {}).get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict) or not services:
            return CheckResult("schema_valid", False,
                               f"{path}: compose must contain a non-empty "
                               "'services' map")
        hdr = _catalog_header(content)
        missing = [k for k in _CATALOG_HEADER_REQUIRED if not hdr.get(k)]
        if missing:
            return CheckResult("schema_valid", False,
                               f"{path}: header missing {missing} — the set "
                               "format requires # documentation, # slogan, "
                               "# category, # upstream, # version and # gain")
        has_url_decl = "SERVICE_URL" in content or "SERVICE_FQDN" in content
        if not hdr.get("port") and not has_url_decl:
            return CheckResult("schema_valid", False,
                               f"{path}: no '# port:' header and no "
                               "SERVICE_URL/FQDN declaration — web service unknown")
        for name, cfg in services.items():
            if not _DNS_LABEL_RE.match(str(name)):
                return CheckResult("schema_valid", False,
                                   f"{path}: service name {name!r} is not a "
                                   "DNS label")
            bad = sorted(_CATALOG_REJECT_KEYS & set(cfg or {}))
            if bad:
                return CheckResult("schema_valid", False,
                                   f"{path}: service {name!r} uses rejected "
                                   f"key(s) {bad} (not admitted in the zone)")
            image = (cfg or {}).get("image")
            if not isinstance(image, str) or not image or "$" in image:
                return CheckResult("schema_valid", False,
                                   f"{path}: service {name!r} needs a literal, "
                                   "pinnable image (no build:, no variables)")
    return CheckResult("schema_valid", True,
                       "structural checks; full render + digest pinning runs "
                       "at the operator gate")


# ── The set format ─────────────────────────────────────────────────────────
# A proposal is a SPECIFICATION in a fixed schema — exactly what the platform
# needs in order to add the capability — never contributor code. Every field below is
# required; a proposal missing one fails
# intake with the field named, which is the format enforcing itself.
#
# The manifest list is dotted paths into the YAML. It sits ON TOP of the live
# loader (_manifest_to_provider), which stays the schema for the manifest body
# — the loader ignores keys it does not know, so the two spec-only fields
# (vendor_api_reference, what_the_host_gains) travel with a manifest through
# promotion without changing what a Provider is.
# ── Well-formedness rules, shared with the authoring path ──────────────────
# server/proposals/authoring.py imports these, so what a Host may AUTHOR and
# what the gate ACCEPTS cannot drift apart. They live HERE because the gate is
# the authority: a proposal filed by hand — pushed straight onto a propose/*
# branch, never through authoring — meets exactly the same bar.
#
# Measured 2026-09-16: before this, `token_url: http://api.x.ai/...` passed all
# three checks. schema_valid only asked the live loader (which never looks at a
# URL) and egress_allowlisted only reads the HOSTNAME — so a plaintext mint
# endpoint on an allowlisted host sailed straight through. The kernel calls
# that URL with the user's sealed key.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _is_url(value: object, schemes: tuple[str, ...]) -> bool:
    """True when `value` uses one of `schemes` AND names a host. A bare scheme
    is not a URL: 'https://' parses fine and has no hostname."""
    if not isinstance(value, str) or not value.startswith(schemes):
        return False
    try:
        return bool(urlparse(value).hostname)
    except Exception:
        return False


# (dotted path, the rule in words, predicate). Checked only once the field is
# known to be present, so a missing field reads as missing and a malformed one
# reads as malformed.
_MANIFEST_WELLFORMED: tuple[tuple[str, str, object], ...] = (
    ("key",
     "a lowercase slug [a-z0-9-], 2-64 chars, starting alphanumeric",
     lambda v: isinstance(v, str) and bool(KEY_RE.match(v))),
    ("realtime.token_url",
     "an https:// URL — the kernel calls it with the user's sealed key, so "
     "plaintext is not acceptable",
     lambda v: _is_url(v, ("https://",))),
    ("realtime.ws_url_template",
     "a wss:// (or https://) URL",
     lambda v: _is_url(v, ("wss://", "https://"))),
    ("vendor_api_reference",
     "an https:// URL to the vendor's API reference",
     lambda v: _is_url(v, ("https://",))),
)


_MANIFEST_REQUIRED: tuple[tuple[str, str], ...] = (
    ("key",                        "the provider's key"),
    ("display_name",               "the provider's name"),
    ("connection_kind",            "the kind (realtime_voice is the only authorable kind today)"),
    ("auth.mode",                  "the auth scheme BY NAME — never a key or token value"),
    ("realtime.token_url",         "the base URL: the kernel-side ephemeral-token mint"),
    ("realtime.ws_url_template",   "the realtime socket URL — one of the exact egress hosts"),
    ("realtime.model",             "the model or voice identifier"),
    ("realtime.audio_format",      "the audio format"),
    ("vendor_api_reference",       "a URL to the vendor's API reference"),
    ("what_the_host_gains",        "one sentence: what the Host gains"),
)

# The feature-request set format, read off the file's front matter
# (intake._parse_feature_front_matter -> Proposal.fields).
_FEATURE_REQUIRED: tuple[tuple[str, str], ...] = (
    ("capability_miss",  "the capability miss, in one sentence"),
    ("situation",        "the situation that produced it"),
    ("done_looks_like",  "what 'done' looks like, observable from the Host"),
    ("source_thread",    "the board thread this came from"),
    ("popularity",       "the vote snapshot at escalation"),
)

# The bug-report set format (2026-09-23). Scalars in the front matter; the
# prose lives under fixed section headings in the body because a repro or a
# log excerpt is never one line. authoring.bug_report_proposal writes exactly
# this, and a report pushed by hand onto a propose/* branch meets the same bar.
_BUG_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("title",        "one line, the way the user would say it"),
    ("severity",     "low | normal | high (high = the user is blocked)"),
    ("host_version", "the Privatae Host version the reporter runs"),
    ("ticket",       "the reporter's local ticket uuid — the idempotency key"),
)
BUG_SEVERITIES = ("low", "normal", "high")
_BUG_REQUIRED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("## Problem",            "what is actually wrong, in one breath"),
    ("## Steps to reproduce", "numbered steps"),
    ("## Expected",           "what should have happened"),
    ("## Actual",             "what happened instead"),
    ("## Shared context",     "exactly the items the user approved for sharing"),
)


def _missing_bug_sections(body: str) -> list[str]:
    """Section headings absent from the body, each named with what it holds.
    Presence only: a heading followed by nothing is the reporter's problem to
    read back, not a schema failure — the validator judges substance."""
    lines = {ln.strip() for ln in (body or "").splitlines()}
    return [f"{h} ({what})" for h, what in _BUG_REQUIRED_SECTIONS if h not in lines]


def _dotted(doc: dict, path: str):
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _missing_manifest_fields(doc: dict) -> list[str]:
    out = []
    for path, what in _MANIFEST_REQUIRED:
        value = _dotted(doc if isinstance(doc, dict) else {}, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            out.append(f"{path} ({what})")
    return out


def _malformed_manifest_fields(doc: dict) -> list[str]:
    """Present-but-wrong fields, each named with the rule it broke."""
    out = []
    for path, rule, ok in _MANIFEST_WELLFORMED:
        value = _dotted(doc if isinstance(doc, dict) else {}, path)
        if value is None:
            continue                      # absence is _missing_manifest_fields'
        if not ok(value):
            out.append(f"{path} must be {rule} (got {value!r})")
    return out


def check_schema_valid(proposal: Proposal) -> CheckResult:
    # Intake's own refusal comes first: if the PR is not a single-kind
    # proposal, nothing below it can be meaningfully checked, and reporting a
    # manifest verdict on a PR whose other files were never read would be
    # worse than useless.
    if proposal.intake_error:
        return CheckResult("schema_valid", False, proposal.intake_error)
    if proposal.kind is ProposalKind.FEATURE_REQUEST:
        if not proposal.title.strip() or not proposal.body.strip():
            return CheckResult("schema_valid", False,
                               "feature request needs a title and body")
        missing = [f"{k} ({what})" for k, what in _FEATURE_REQUIRED
                   if not str(proposal.fields.get(k, "")).strip()]
        if missing:
            return CheckResult(
                "schema_valid", False,
                "feature request is missing required field(s): " + "; ".join(missing))
        return CheckResult("schema_valid", True)
    if proposal.kind is ProposalKind.CATALOG_TEMPLATE:
        return _check_catalog_template(proposal)
    if proposal.kind is ProposalKind.BUG:
        if len(proposal.files) != 1:
            return CheckResult("schema_valid", False,
                               f"a bug report is ONE bugs/*.md file (got {len(proposal.files)})")
        path = next(iter(proposal.files))
        if not path.endswith(".md"):
            return CheckResult("schema_valid", False,
                               f"{path}: a bug report is a markdown file under bugs/")
        missing = [f"{k} ({what})" for k, what in _BUG_REQUIRED_FIELDS
                   if not str(proposal.fields.get(k, "")).strip()]
        if missing:
            return CheckResult(
                "schema_valid", False,
                "bug report front matter is missing required field(s): " + "; ".join(missing))
        sev = str(proposal.fields.get("severity", "")).strip().lower()
        if sev not in BUG_SEVERITIES:
            return CheckResult("schema_valid", False,
                               f"severity must be one of {list(BUG_SEVERITIES)} (got {sev!r})")
        absent = _missing_bug_sections(proposal.body)
        if absent:
            return CheckResult(
                "schema_valid", False,
                "bug report is missing required section(s): " + "; ".join(absent))
        return CheckResult("schema_valid", True)
    # MANIFEST: every yaml must load into a Provider via the live loader, and
    # then carry every field of the set format.
    from server.integrations.core.manifests import _manifest_to_provider
    yaml_files = [p for p in proposal.files if p.endswith((".yaml", ".yml"))]
    if not yaml_files:
        return CheckResult("schema_valid", False, "manifest proposal has no .yaml file")
    for path in yaml_files:
        try:
            data = yaml.safe_load(proposal.files[path])
        except yaml.YAMLError as e:
            return CheckResult("schema_valid", False, f"{path}: invalid YAML ({e})")
        try:
            _manifest_to_provider(data)
        except Exception as e:
            return CheckResult("schema_valid", False, f"{path}: {e}")
        missing = _missing_manifest_fields(data)
        if missing:
            return CheckResult(
                "schema_valid", False,
                f"{path}: missing required field(s): " + "; ".join(missing))
        malformed = _malformed_manifest_fields(data)
        if malformed:
            return CheckResult("schema_valid", False,
                               f"{path}: " + "; ".join(malformed))
        # The file is named for the provider it declares. authoring.py writes
        # manifests/<key>.yaml and promote.py carries that basename into the
        # platform catalog, where the loader keys providers by the YAML's own
        # `key` — so a mismatch here would promote a file whose name says one
        # provider and whose contents say another.
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem != data["key"]:
            return CheckResult(
                "schema_valid", False,
                f"{path}: a manifest must be named <key>.yaml — this one "
                f"declares key {data['key']!r}, so the file must be "
                f"{data['key']}.yaml")
    return CheckResult("schema_valid", True)


def _endpoint_hosts(manifest: dict) -> list[str]:
    """Outbound endpoints a manifest declares: a realtime WSS url and/or a proxy
    base url. Returns the hostnames (lowercased), skipping any that don't parse."""
    urls: list[str] = []
    realtime = manifest.get("realtime") or {}
    if isinstance(realtime, dict) and realtime.get("ws_url_template"):
        urls.append(str(realtime["ws_url_template"]))
    # token_url is called by the KERNEL (routes/realtime.py), so it is egress
    # from the pod and must clear the same allowlist as the browser socket.
    if isinstance(realtime, dict) and realtime.get("token_url"):
        urls.append(str(realtime["token_url"]))
    proxy = manifest.get("proxy") or {}
    if isinstance(proxy, dict) and proxy.get("base_url"):
        urls.append(str(proxy["base_url"]))
    hosts: list[str] = []
    for u in urls:
        host = (urlparse(u).hostname or "").lower()
        if host:
            hosts.append(host)
    return hosts


def check_egress_allowlisted(proposal: Proposal) -> CheckResult:
    if proposal.kind is ProposalKind.CATALOG_TEMPLATE:
        # A catalog app's runtime egress is governed by the zone NetworkPolicy
        # (RFC1918 denied, own namespace + public internet allowed) — there is
        # no manifest-declared endpoint to allowlist here.
        return CheckResult("egress_allowlisted", True,
                           "n/a (catalog template — zone NetworkPolicy governs egress)")
    if proposal.kind is ProposalKind.BUG:
        # A report declares no endpoint. Whatever it describes is DATA for the
        # validator and the operator Host; nothing in it is ever dialled.
        return CheckResult("egress_allowlisted", True, "n/a (bug report — data, never dialled)")
    if proposal.kind is not ProposalKind.MANIFEST:
        return CheckResult("egress_allowlisted", True, "n/a (not a manifest)")
    offenders: list[str] = []
    for path, manifest in _manifest_docs(proposal):
        for host in _endpoint_hosts(manifest):
            if host not in ALLOWED_EGRESS_HOSTS:
                offenders.append(f"{path}: {host}")
    if offenders:
        return CheckResult(
            "egress_allowlisted", False,
            "off-allowlist egress target(s), holding for manual review: "
            + "; ".join(offenders),
        )
    return CheckResult("egress_allowlisted", True)


def check_no_secrets(proposal: Proposal) -> CheckResult:
    # Every byte the reviewer will read is scanned: the files, AND the body +
    # front-matter fields. Until 2026-09-23 only `files` was scanned, and
    # intake drops a feature request's files at the door (only body + fields
    # survive) — so feature-request text was never scanned at all. A bug
    # report carries log excerpts, which is exactly where a pasted key lands.
    scan: list[tuple[str, str]] = list(proposal.files.items())
    if proposal.body:
        scan.append(("(body)", proposal.body))
    for k, v in (proposal.fields or {}).items():
        scan.append((f"(field {k})", str(v)))
    for path, content in scan:
        for label, pat in _SECRET_PATTERNS:
            if pat.search(content):
                return CheckResult("no_secrets", False,
                                   f"{path}: looks like an embedded {label}")
    # A manifest auth block must reference, never embed, a secret value.
    for path, manifest in _manifest_docs(proposal):
        auth = manifest.get("auth") or {}
        if isinstance(auth, dict):
            for k in _FORBIDDEN_AUTH_VALUE_KEYS:
                if auth.get(k):
                    return CheckResult(
                        "no_secrets", False,
                        f"{path}: auth.{k} carries a literal value — reference a "
                        f"vault cred, don't embed the secret",
                    )
    return CheckResult("no_secrets", True)


_CHECKS = (check_schema_valid, check_egress_allowlisted, check_no_secrets)


def evaluate(proposal: Proposal) -> ProposalVerdict:
    """Run the full gated-auto suite. All pass → verdict.auto_stage is True
    (stage for one-click approval); any fail → held for manual review."""
    return ProposalVerdict(checks=[c(proposal) for c in _CHECKS])
