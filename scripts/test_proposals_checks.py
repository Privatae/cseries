#!/usr/bin/env python3
"""
Phase-0 gate — the gated-auto proposal check suite (server/proposals/checks.py).

Verifies the automated half of the proposal approval gate: a clean provider
manifest AUTO-STAGES, and each failure mode (bad schema, off-allowlist egress
target, embedded secret) is HELD for manual review instead. Pure logic — no
pod, no DB, no network. Run from the repo root:  python3 scripts/test_proposals_checks.py
"""
from __future__ import annotations

import os
import sys

# repo root on path so `import server...` resolves when run standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.proposals import Proposal, ProposalKind, evaluate  # noqa: E402

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


def _named(verdict, name):
    return next(c for c in verdict.checks if c.name == name)


# ── fixtures ────────────────────────────────────────────────────────────────

# Every fixture below carries the SET FORMAT: a proposal is a
# specification with a fixed schema and no optional fields, so a manifest that
# omits one fails schema_valid with the field named.
GOOD_VOICE = """
key: xai-voice
display_name: xAI Grok Voice
categories: [voice]
connection_kind: realtime_voice
auth:
  mode: api_key
realtime:
  ws_url_template: "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
  token_url: "https://api.x.ai/v1/realtime/client_secrets"
  model: grok-voice-latest
  audio_format: pcm16
vendor_api_reference: https://docs.x.ai/api/realtime
what_the_host_gains: The host can hold a spoken conversation with the user.
"""

MALFORMED = """
display_name: No Key Here
connection_kind: realtime_voice
"""

OFF_ALLOWLIST = """
key: sketchy-voice
display_name: Sketchy Voice
connection_kind: realtime_voice
auth:
  mode: api_key
realtime:
  ws_url_template: "wss://evil.example.com/v1/realtime"
  token_url: "https://evil.example.com/token"
  model: sketchy-1
  audio_format: pcm16
vendor_api_reference: https://evil.example.com/docs
what_the_host_gains: Nothing good, but the format is complete.
"""

EMBEDDED_SECRET_LITERAL = """
key: leaky-voice
display_name: Leaky Voice
connection_kind: realtime_voice
auth:
  mode: api_key
  api_key: "xai-ABCDEF0123456789ABCDEF0123456789"
realtime:
  ws_url_template: "wss://api.x.ai/v1/realtime"
  token_url: "https://api.x.ai/v1/realtime/client_secrets"
  model: grok-voice-latest
  audio_format: pcm16
vendor_api_reference: https://docs.x.ai/api/realtime
what_the_host_gains: Voice, leakily.
"""

EMBEDDED_SECRET_FREETEXT = """
key: leaky2-voice
display_name: Leaky2 Voice
connection_kind: realtime_voice
auth:
  mode: api_key
# do not do this: sk-ABCDEF0123456789ABCDEF0123456789
realtime:
  ws_url_template: "wss://api.x.ai/v1/realtime"
  token_url: "https://api.x.ai/v1/realtime/client_secrets"
  model: grok-voice-latest
  audio_format: pcm16
vendor_api_reference: https://docs.x.ai/api/realtime
what_the_host_gains: Voice, leakily again.
"""


def voice_proposal(yaml_text: str, key: str = "provider") -> Proposal:
    # `key` is the manifest's OWN key: a manifest must be named <key>.yaml,
    # so passing anything else here is the mismatch schema_valid now catches.
    return Proposal(
        kind=ProposalKind.MANIFEST,
        title=f"add {key} provider",
        files={f"manifests/{key}.yaml": yaml_text},
    )


# ── 1. the clean manifest auto-stages ───────────────────────────────────────
v = evaluate(voice_proposal(GOOD_VOICE, "xai-voice"))
check("clean voice manifest: all checks pass", v.passed, str(v.failures))
check("clean voice manifest: auto_stage True", v.auto_stage)
check("clean voice manifest: 3 checks ran", len(v.checks) == 3, f"{len(v.checks)}")

# ── 2. malformed schema is held ─────────────────────────────────────────────
v = evaluate(voice_proposal(MALFORMED, "nokey"))
check("malformed manifest: held (auto_stage False)", not v.auto_stage)
check("malformed manifest: schema_valid failed", not _named(v, "schema_valid").passed,
      _named(v, "schema_valid").detail)

# ── 3. off-allowlist egress target is held ──────────────────────────────────
v = evaluate(voice_proposal(OFF_ALLOWLIST, "sketchy-voice"))
check("off-allowlist egress: held", not v.auto_stage)
check("off-allowlist egress: egress_allowlisted failed",
      not _named(v, "egress_allowlisted").passed, _named(v, "egress_allowlisted").detail)
check("off-allowlist egress: schema still valid", _named(v, "schema_valid").passed)

# ── 4. embedded secret (literal auth value) is held ─────────────────────────
v = evaluate(voice_proposal(EMBEDDED_SECRET_LITERAL, "leaky-voice"))
check("embedded secret (auth value): held", not v.auto_stage)
check("embedded secret (auth value): no_secrets failed",
      not _named(v, "no_secrets").passed, _named(v, "no_secrets").detail)

# ── 5. embedded secret (free-text key pattern) is held ──────────────────────
v = evaluate(voice_proposal(EMBEDDED_SECRET_FREETEXT, "leaky2-voice"))
check("embedded secret (free text): no_secrets failed",
      not _named(v, "no_secrets").passed, _named(v, "no_secrets").detail)

# ── 6. feature request contract ─────────────────────────────────────────────
FR_FIELDS = {"capability_miss": "The host cannot render a dark theme.",
             "situation": "A user reading at night asked for one.",
             "done_looks_like": "The user flips a toggle and the interface is dark.",
             "source_thread": "12", "popularity": "4"}
v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Add dark mode",
                      body="Please add a dark mode toggle.", fields=dict(FR_FIELDS)))
check("feature request (set format complete): auto_stage True", v.auto_stage, str(v.failures))
v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Empty", body="",
                      fields=dict(FR_FIELDS)))
check("feature request (no body): held", not v.auto_stage)

# the set format enforcing itself: each missing field fails intake, by name
for _f in FR_FIELDS:
    _partial = {k: v2 for k, v2 in FR_FIELDS.items() if k != _f}
    _v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Add dark mode",
                           body="Please add a dark mode toggle.", fields=_partial))
    _d = _named(_v, "schema_valid")
    check(f"feature request missing {_f}: held, and the field is named",
          not _v.auto_stage and _f in _d.detail, _d.detail)


# ── 7. catalog-template contract ────────────────────────────────────────────
GOOD_CATALOG = """\
# documentation: https://example.org
# slogan: Example is an example app.
# category: fixture
# upstream: nginx
# version: 1.27
# gain: The host can serve a static site from its own zone.
# port: 8080
services:
  app:
    image: nginx:1.27
    environment:
      - SERVICE_URL_APP_8080
"""
v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="add example",
                      files={"catalog-templates/example.yaml": GOOD_CATALOG}))
check("catalog template (clean): auto_stage True", v.auto_stage, str(v.failures))
check("catalog template: egress check n/a-passes",
      _named(v, "egress_allowlisted").passed)

v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="priv",
                      files={"catalog-templates/bad.yaml":
                             GOOD_CATALOG.replace("image: nginx:1.27",
                                                  "image: nginx:1.27\n    privileged: true")}))
check("catalog template (privileged): held", not v.auto_stage,
      _named(v, "schema_valid").detail)

v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="hdr",
                      files={"catalog-templates/bad.yaml":
                             GOOD_CATALOG.replace("# slogan: Example is an example app.\n", "")}))
check("catalog template (missing header): held", not v.auto_stage)

v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="tok",
                      files={"catalog-templates/bad.yaml":
                             GOOD_CATALOG + "      - X=__SECRET_SERVICE_PASSWORD_DB__\n"}))
check("catalog template (reserved placeholder): held", not v.auto_stage)

v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="img",
                      files={"catalog-templates/bad.yaml":
                             GOOD_CATALOG.replace("nginx:1.27", "${IMG:-nginx}:1")}))
check("catalog template (templated image): held", not v.auto_stage)

v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="ovr",
                      files={"catalog-templates/example.yaml": GOOD_CATALOG,
                             "catalog-templates/overrides/example.json": "not json"}))
check("catalog template (bad overrides json): held", not v.auto_stage)

# the set format for a catalog template: each new header field is required
for _hdr in ("upstream", "version", "gain"):
    _bad = "\n".join(l for l in GOOD_CATALOG.splitlines()
                      if not l.startswith(f"# {_hdr}:")) + "\n"
    _v = evaluate(Proposal(kind=ProposalKind.CATALOG_TEMPLATE, title="hdr",
                           files={"catalog-templates/bad.yaml": _bad}))
    _d = _named(_v, "schema_valid")
    check(f"catalog template missing # {_hdr}: held, and the field is named",
          not _v.auto_stage and _hdr in _d.detail, _d.detail)

# and the manifest set format, each field by name
_base = GOOD_VOICE
for _field, _line in (("realtime.token_url", '  token_url: "https://api.x.ai/v1/realtime/client_secrets"'),
                      ("realtime.model", "  model: grok-voice-latest"),
                      ("realtime.audio_format", "  audio_format: pcm16"),
                      ("vendor_api_reference", "vendor_api_reference: https://docs.x.ai/api/realtime"),
                      ("what_the_host_gains", "what_the_host_gains: The host can hold a spoken conversation with the user.")):
    _bad = "\n".join(l for l in _base.splitlines() if l != _line) + "\n"
    _v = evaluate(voice_proposal(_bad, "xai-voice"))
    _d = _named(_v, "schema_valid")
    check(f"manifest missing {_field}: held, and the field is named",
          not _v.auto_stage and _field in _d.detail, _d.detail)

# ── well-formedness: the gate enforces what authoring enforces ──────────────
# It once did not. `token_url: http://api.x.ai/...` passed all
# THREE checks: schema_valid only asked the live loader (which never looks at
# a URL) and egress_allowlisted only reads the HOSTNAME. The kernel calls that
# URL with the user's sealed key.
_PLAINTEXT_MINT = GOOD_VOICE.replace(
    'token_url: "https://api.x.ai/v1/realtime/client_secrets"',
    'token_url: "http://api.x.ai/v1/realtime/client_secrets"')
v = evaluate(voice_proposal(_PLAINTEXT_MINT, "xai-voice"))
check("a plaintext token_url on an ALLOWLISTED host is held", not v.auto_stage,
      str(v.failures))
check("...by schema_valid, naming the field and the rule",
      not _named(v, "schema_valid").passed
      and "realtime.token_url" in _named(v, "schema_valid").detail
      and "https://" in _named(v, "schema_valid").detail,
      _named(v, "schema_valid").detail)
check("...and the egress check still passes, which is why it slipped through",
      _named(v, "egress_allowlisted").passed, _named(v, "egress_allowlisted").detail)

for _field, _old, _bad in (
    ("key", "key: xai-voice", "key: XAI Voice!"),
    ("realtime.ws_url_template",
     'ws_url_template: "wss://api.x.ai/v1/realtime?model=grok-voice-latest"',
     'ws_url_template: "not-a-url"'),
    ("realtime.ws_url_template",
     'ws_url_template: "wss://api.x.ai/v1/realtime?model=grok-voice-latest"',
     'ws_url_template: "wss://"'),
    ("vendor_api_reference",
     "vendor_api_reference: https://docs.x.ai/api/realtime",
     "vendor_api_reference: http://docs.x.ai/api/realtime"),
    ("vendor_api_reference",
     "vendor_api_reference: https://docs.x.ai/api/realtime",
     "vendor_api_reference: see the docs"),
):
    _bad_yaml = GOOD_VOICE.replace(_old, _bad)
    assert _bad_yaml != GOOD_VOICE, _old
    _key = "xai-voice" if _field != "key" else "XAI Voice!"
    _v = evaluate(Proposal(kind=ProposalKind.MANIFEST, title="t",
                           files={f"manifests/{_key}.yaml": _bad_yaml}))
    _d = _named(_v, "schema_valid")
    check(f"malformed {_field} ({_bad.split(':', 1)[1].strip()[:28]}): held, field named",
          not _v.auto_stage and _field in _d.detail, _d.detail)

# a manifest is named for the provider it declares
_v = evaluate(Proposal(kind=ProposalKind.MANIFEST, title="t",
                       files={"manifests/something-else.yaml": GOOD_VOICE}))
_d = _named(_v, "schema_valid")
check("a manifest not named <key>.yaml is held, naming the name it needs",
      not _v.auto_stage and "xai-voice.yaml" in _d.detail, _d.detail)

# the authoring path and the gate share ONE copy of these rules
import server.proposals.authoring as _authoring                          # noqa: E402
import server.proposals.checks as _checks                                # noqa: E402
check("authoring imports the gate's key rule rather than restating it",
      _authoring._KEY_RE is _checks.KEY_RE)
check("authoring imports the gate's URL rule rather than restating it",
      _authoring._is_url is _checks._is_url)

# intake's refusal is reported by schema_valid, first
_v = evaluate(Proposal(kind=ProposalKind.MANIFEST, title="t",
                       files={"manifests/xai-voice.yaml": GOOD_VOICE},
                       intake_error="README.md was NOT checked"))
check("an intake refusal is the schema_valid verdict, before anything else",
      not _v.auto_stage and _named(_v, "schema_valid").detail == "README.md was NOT checked",
      _named(_v, "schema_valid").detail)

# ── 8. bug-report contract ──────────────────────────────────────────────────
BUG_FIELDS = {"title": "Tickets pane loses drafts", "kind": "bug", "severity": "high",
              "host_version": "cseries v0.16.83",
              "ticket": "8f3a1c2e-1111-2222-3333-444455556666", "project": "the Host itself"}
BUG_BODY = ("## Problem\n\nDrafts vanish.\n\n## Steps to reproduce\n\n1. open\n2. close\n\n"
            "## Expected\n\ndraft stays\n\n## Actual\n\ngone\n\n## Shared context\n\n"
            "### Recent errors (1)\n\n```errors\nboom\n```\n")
_BUG_PATH = "bugs/8f3a1c2e-tickets-pane-loses-drafts.md"


def bug_proposal(body=BUG_BODY, fields=None, files=None) -> Proposal:
    f = dict(BUG_FIELDS) if fields is None else fields
    return Proposal(kind=ProposalKind.BUG, title=f.get("title", "t"),
                    files=files if files is not None else {_BUG_PATH: body},
                    body=body, fields=f)


v = evaluate(bug_proposal())
check("bug report (set format complete): auto_stage True", v.auto_stage, str(v.failures))
check("bug report: egress check n/a-passes (a report is data, never dialled)",
      _named(v, "egress_allowlisted").passed and "data" in _named(v, "egress_allowlisted").detail,
      _named(v, "egress_allowlisted").detail)
for _f in ("title", "severity", "host_version", "ticket"):
    _partial = {k: v2 for k, v2 in BUG_FIELDS.items() if k != _f}
    _v = evaluate(bug_proposal(fields=_partial))
    check(f"bug report missing {_f}: held, and the field is named",
          not _v.auto_stage and _f in _named(_v, "schema_valid").detail,
          _named(_v, "schema_valid").detail)
_v = evaluate(bug_proposal(fields={**BUG_FIELDS, "severity": "urgent"}))
check("bug report with an unknown severity: held", not _v.auto_stage
      and "severity" in _named(_v, "schema_valid").detail, _named(_v, "schema_valid").detail)
for _h in ("## Problem", "## Steps to reproduce", "## Expected", "## Actual", "## Shared context"):
    _v = evaluate(bug_proposal(body=BUG_BODY.replace(_h, "## Something else")))
    check(f"bug report missing section {_h!r}: held, and the section is named",
          not _v.auto_stage and _h in _named(_v, "schema_valid").detail,
          _named(_v, "schema_valid").detail)
_v = evaluate(bug_proposal(files={_BUG_PATH: BUG_BODY, "bugs/second.md": BUG_BODY}))
check("two report files in one PR: held", not _v.auto_stage,
      _named(_v, "schema_valid").detail)

# no_secrets now reads the BODY and FIELDS, not only files. Before, a
# key pasted into a feature request (whose files intake drops) or a bug's log
# excerpt was never scanned.
_leak = BUG_BODY.replace("boom", "token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
_v = evaluate(Proposal(kind=ProposalKind.BUG, title="t", files={}, body=_leak,
                       fields=dict(BUG_FIELDS)))
check("a secret in a bug report's BODY is held even with no files on the proposal",
      not _v.auto_stage and not _named(_v, "no_secrets").passed
      and "(body)" in _named(_v, "no_secrets").detail, _named(_v, "no_secrets").detail)
_v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Add dark mode",
                       body="use key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 please",
                       fields=dict(FR_FIELDS)))
check("a secret in a feature request's body is held (the gap this closed)",
      not _v.auto_stage and not _named(_v, "no_secrets").passed, str(_v.failures))
_v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Add dark mode",
                       body="Please add a dark mode toggle.",
                       fields={**FR_FIELDS, "situation": "AKIAABCDEFGHIJKLMNOP leaked"}))
check("a secret in a front-matter FIELD is held, naming the field",
      not _v.auto_stage and "(field situation)" in _named(_v, "no_secrets").detail,
      _named(_v, "no_secrets").detail)
# The word "bearer" in prose is not a token
_v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Webhooks",
                       body="Please support bearer authentication/authorization for webhooks.",
                       fields=dict(FR_FIELDS)))
check("'bearer authentication/authorization' in prose is NOT a secret", _v.auto_stage, str(_v.failures))
_v = evaluate(Proposal(kind=ProposalKind.FEATURE_REQUEST, title="Webhooks",
                       body="curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc123DEF456ghi789'",
                       fields=dict(FR_FIELDS)))
check("a real bearer token (letters + digits) is still held",
      not _v.auto_stage and not _named(_v, "no_secrets").passed, str(_v.failures))

print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("PASSED: gated-auto proposal checks")
