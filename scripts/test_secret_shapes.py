#!/usr/bin/env python3
"""
Secret-shapes gate (smoke) — pasted keys/credentials never reach inference.

brain/mastercode/secret_shapes.json is the ONE rules file: the pod-side
redaction (secret_shapes.py, called at the top of send_message) and the two
composers (circles-web / workbench-web src/lib/secret-shapes.ts) all load it.

  1. every hard vendor shape is found, labelled by its rule id, and REDACTED
     — the value is gone from the text, the marker names the kind
  2. the redaction cuts only the SECRET span: a URL keeps its host, a bearer
     header keeps the word "Bearer"
  3. soft heuristics (assignment, entropy) are REPORTED but never redacted
     by default — a hash or a random id is not a leak
  4. hex-only strings (git SHAs) and UUIDs never trip the entropy catch;
     random base64 does
  5. precedence: `sk-ant-…` is an Anthropic key, not an OpenAI one
  6. benign chat is left byte-identical (no false-positive wall)
  7. previews never contain the value
  8. the rules file is portable: no inline flags, no lookbehind, no named
     groups — every pattern must compile the same way in JS `RegExp`
  9. the two TS copies are byte-identical and import THIS rules file
 10. send_message is wired: the redact call sits above the raw-message capture

From the repo root with node on PATH it also runs every case of section 13
through both TS copies (section 14): the composer must find, cut and find
again what this module does. In-container the web sources are absent and that
section is skipped, as section 9 is.

Exit 0 = all pass; exit 1 + FAIL lines otherwise. Pure — no pod, no DB.
Run from the repo root: python3 scripts/test_secret_shapes.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROOT = os.environ.get("APP_ROOT", ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode import secret_shapes as ss  # noqa: E402

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


# ── 1. hard shapes: found, labelled, redacted ───────────────────────────
HARD = {
    "anthropic-key": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij",
    "openai-key": "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "xai-key": "xai-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "google-api-key": "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "github-token": "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "gitlab-token": "glpat-AbCdEfGhIjKlMnOpQrSt",
    "aws-access-key": "AKIAIOSFODNN7EXAMPLE",
    "slack-token": "xoxb-1234567890-abcdefghijkl",
    # Synthetic: the shapes only, built so no real token is in the tree.
    "slack-app-token": "xapp-1-A0FAKE0000-0000000000000-" + "f" * 24,
    "telegram-bot-token": "1234567890:" + "AA" + "Fake" * 8 + "x",
    "stripe-key": "sk_live_AbCdEfGhIjKlMnOpQrStUvWx",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
}
for rid, value in HARD.items():
    text = f"here is my key: {value} please use it"
    out, findings = ss.redact(text)
    ids = [f.id for f in findings]
    check(f"hard {rid}: found", rid in ids, f"findings={ids}")
    check(f"hard {rid}: value gone", value not in out, out)
    check(f"hard {rid}: marker names the kind", f"[REDACTED:{rid}]" in out, out)
    check(f"hard {rid}: prose kept", out.startswith("here is my key: ") and out.endswith(" please use it"), out)

pem = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACBsZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2VhZ2Vh\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
out, findings = ss.redact(f"my ssh key:\n{pem}\nthanks")
check("hard private-key: whole block redacted", out == "my ssh key:\n[REDACTED:private-key]\nthanks", out)

# Messaging bot tokens: the shapes a user pasted while pairing a
# Telegram connector got no hard hit, so no "Store in Keys".
_tg = HARD["telegram-bot-token"]
check("telegram: the token is 35 chars after the id", len(_tg.split(":", 1)[1]) == 35, _tg)
out, findings = ss.redact(f"curl https://api.telegram.org/bot{_tg}/getMe")
check("telegram: inside the API URL, 'bot' kept and the token cut",
      out == "curl https://api.telegram.org/bot[REDACTED:telegram-bot-token]/getMe", out)
_tg_dash = "987654321:" + "B" * 34 + "-"
out, findings = ss.redact(f"token {_tg_dash} ok")
check("telegram: a token ending in '-' is still caught (no trailing \\b)",
      out == "token [REDACTED:telegram-bot-token] ok", out)
_ts_sha = "at 1696500000:3f786850e387550fdab836ed7e6dc881de23001b it broke"
check("telegram: a timestamp and a git sha is not a bot token",
      not ss.scan(_ts_sha), str([f.id for f in ss.scan(_ts_sha)]))
check("telegram: 36 chars after the colon is not a bot token",
      "telegram-bot-token" not in [f.id for f in ss.scan("1234567890:" + "C" * 36)])
_rot = "xoxe.xapp-1-" + "Z" * 30
out, findings = ss.redact(f"rotating {_rot} here")
check("slack: a rotating app-level token (xoxe.xapp-) is caught whole",
      out == "rotating [REDACTED:slack-app-token] here", out)
# A last set that holds '-' ends on a lookahead, never \b:
# \b after a trailing '-' wants a word character next, so these were missed or
# left their last '-' behind. All synthetic.
_gk_dash = "AIza" + "Q" * 34 + "-"
out, _ = ss.redact(f"key {_gk_dash} ok")
check("google: a key ending in '-' is caught", out == "key [REDACTED:google-api-key] ok", out)
check("google: 36 chars after AIza is not a 39-char key",
      "google-api-key" not in [f.id for f in ss.scan("AIza" + "Q" * 36)])
_jwt_dash = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5-"
out, _ = ss.redact(f"jwt {_jwt_dash} ok")
check("jwt: a signature ending in '-' is cut whole", out == "jwt [REDACTED:jwt] ok", out)
for _sl, _what in (("xoxe-1-" + "R" * 40, "refresh"), ("xoxe.xoxp-1-" + "R" * 40, "rotating user"),
                   ("xoxe.xoxb-1-" + "R" * 40, "rotating bot"), ("xwfp-" + "R" * 40, "workflow")):
    out, _ = ss.redact(f"slack {_sl} ok")
    check(f"slack: a {_what} token is caught whole", out == "slack [REDACTED:slack-token] ok", out)
# Ordinary text near the Telegram shape: times, ids with a colon, a ref:sha.
for _b in ("meeting at 10:30:45 on 2026-10-05T12:30:00Z", "order 12345678: shipped",
           "ticket 1234567890:see-the-notes-in-the-thread-please ok",
           "build 20261005:" + "a" * 34, "pid 123456789:" + "x" * 36):
    check(f"telegram: not a bot token: {_b[:40]!r}",
          "telegram-bot-token" not in [f.id for f in ss.scan(_b)], str([f.id for f in ss.scan(_b)]))
with open(os.path.join(APP_ROOT, "brain", "mastercode", "secret_shapes.json"), encoding="utf-8") as fh:
    _keys = {r["id"]: r.get("keys") for r in json.load(fh)["rules"]}
check("telegram: Store in Keys seals it as telegram-bot",
      _keys.get("telegram-bot-token") == {"name": "telegram-bot", "cred_type": "opaque"},
      str(_keys.get("telegram-bot-token")))
check("slack: Store in Keys seals the app-level token as slack-app-token",
      _keys.get("slack-app-token") == {"name": "slack-app-token", "cred_type": "opaque"},
      str(_keys.get("slack-app-token")))

# ── 2. group redaction keeps the surroundings ───────────────────────────
out, findings = ss.redact("connect with postgres://app:S3cretPassw0rd@db.internal:5432/prod")
check("url-credential: host kept, password cut",
      out == "connect with postgres://app:[REDACTED:url-credential]@db.internal:5432/prod", out)
out, findings = ss.redact("Authorization: Bearer AbCdEfGhIjKlMnOpQrStUvWxYz012345")
check("bearer-token: the word Bearer kept", out == "Authorization: Bearer [REDACTED:bearer-token]", out)

# ── 3. soft: reported, not redacted ─────────────────────────────────────
soft_text = "password = hunter2hunter2 and the blob is kJ8f2Lm9Qw3Rt5Yu7Io1Pa4Sd6Fg8Hj0Kl2Zx4Cv6Bn"
out, findings = ss.redact(soft_text)
soft_ids = [f.id for f in ss.soft(findings)]
check("soft: assignment reported", "assigned-secret" in soft_ids, str(soft_ids))
check("soft: entropy reported", "high-entropy-token" in soft_ids, str(soft_ids))
check("soft: nothing redacted by default", out == soft_text, out)
out2, _ = ss.redact(soft_text, severities=("hard", "soft"))
check("soft: redacted when asked", "hunter2hunter2" not in out2 and "kJ8f2Lm9" not in out2, out2)

# ── 4. entropy floor: hex and uuids pass, random base64 trips ───────────
sha = "3f786850e387550fdab836ed7e6dc881de23001b"
uuid = "0e7c9a4e-2b1f-4b7e-9c1d-2f3a4b5c6d7e"
check("entropy: git sha is not a secret", not ss.scan(f"commit {sha} fixed it"), str(ss.scan(sha)))
check("entropy: uuid is not a secret", not ss.scan(f"row {uuid} is missing"), str(ss.scan(uuid)))
rand64 = "Zx9Qw2Er4Ty6Ui8Op0As1Df3Gh5Jk7Lz9Xc2Vb4Nm6Qw8Er0"
check("entropy: random base64 trips", any(f.id == "high-entropy-token" for f in ss.scan(rand64)))
check("entropy: hex entropy really is below the floor", ss.shannon_bits_per_char(sha) < 4.3)

# ── 5. precedence ────────────────────────────────────────────────────────
f = ss.scan(HARD["anthropic-key"])
check("precedence: sk-ant- is anthropic, not openai", [x.id for x in f] == ["anthropic-key"], str([x.id for x in f]))

# ── 6. benign chat untouched ─────────────────────────────────────────────
BENIGN = [
    "can you look at /usr/local/lib/python3.12/site-packages/requests/adapters.py",
    "my password manager is fine, I use a passphrase of four words",
    "the sk- prefix is what openai uses, mine starts with sk-",
    "see https://example.com/docs/getting-started#authentication for the flow",
    "ThisIsAVeryLongVariableNameForTestingPurposesOnly",
    "1234567890123456789012345678901234567890",
    "",
]
for b in BENIGN:
    out, findings = ss.redact(b)
    check(f"benign untouched: {b[:40]!r}", out == b and not findings, f"findings={[x.id for x in findings]}")

# ── 7. previews never carry the value ───────────────────────────────────
for f in ss.scan(HARD["openai-key"]):
    check("preview: not the value", f.preview != HARD["openai-key"] and len(f.preview) <= 9, f.preview)
    check("preview: shape is 4…4", re.fullmatch(r".{4}….{4}", f.preview) is not None, f.preview)

# ── 8. rules file portability (must compile identically in JS) ──────────
rules_path = os.path.join(APP_ROOT, "brain", "mastercode", "secret_shapes.json")
with open(rules_path, encoding="utf-8") as fh:
    doc = json.load(fh)
pats = [r["pattern"] for r in doc["rules"]] + [doc["entropy"]["charset_pattern"]]
for p in pats:
    check(f"portable: no inline flags in {p[:30]!r}", "(?i" not in p and "(?m" not in p and "(?s" not in p)
    check(f"portable: no lookbehind in {p[:30]!r}", "(?<" not in p)
    check(f"portable: no named groups in {p[:30]!r}", "(?P<" not in p)
    check(f"portable: no \\A \\Z in {p[:30]!r}", "\\A" not in p and "\\Z" not in p)
for r in doc["rules"]:
    check(f"rules: {r['id']} severity is hard|soft", r["severity"] in ("hard", "soft"))
    check(f"rules: {r['id']} flags subset of 'i'", set(r.get("flags") or "") <= {"i"})
    g = int(r.get("group") or 0)
    check(f"rules: {r['id']} group exists", g <= re.compile(r["pattern"]).groups)
check("rules: entropy floor rejects hex", float(doc["entropy"]["min_bits_per_char"]) > 4.0)
# "Store in Keys": every hard rule says where it seals; vendor keys land on the
# canonical provider name so routing picks them up without a rename.
sys.path.insert(0, APP_ROOT)
from server.vault import providers as _pv  # noqa: E402
for r in doc["rules"]:
    if r["severity"] != "hard":
        continue
    k = r.get("keys") or {}
    check(f"keys: {r['id']} has a store hint", bool(k.get("name")) and k.get("cred_type") in ("api_key", "opaque"), str(k))
    if k.get("cred_type") == "api_key":
        check(f"keys: {r['id']} api_key name is a registry provider",
              k["name"] in _pv.PROVIDERS and _pv.canonical_name(k["name"]) == k["name"], k.get("name"))

# ── 9. the two TS copies are identical and import this file ─────────────
ts_a = os.path.join(APP_ROOT, "circles-web", "src", "lib", "secret-shapes.ts")
ts_b = os.path.join(APP_ROOT, "workbench-web", "src", "lib", "secret-shapes.ts")
if os.path.exists(ts_a) and os.path.exists(ts_b):
    a = open(ts_a, encoding="utf-8").read()
    b = open(ts_b, encoding="utf-8").read()
    check("ts: circles and workbench copies byte-identical", a == b)
    check("ts: imports the shared rules file",
          'from "../../../brain/mastercode/secret_shapes.json"' in a)
else:
    # In-container the web sources are not copied (only the built bundles);
    # the repo-root run covers this branch.
    print("SKIP  ts copies not present here (in-container run) — covered at repo root")

# ── 10. send_message is wired above the raw-message capture ──────────────
sess_path = os.path.join(APP_ROOT, "brain", "mastercode", "runtime", "session.py")
src = turn_source(APP_ROOT)
i_redact = src.find("_secret_shapes.redact(user_message)")
i_raw = src.find("_raw_user_message = user_message")
check("wired: send_message calls secret_shapes.redact", i_redact > 0)
check("wired: redaction precedes the raw-message capture", 0 < i_redact < i_raw, f"{i_redact} vs {i_raw}")

# ── 11. the nudge door is walled too ─────────────────────────────────────
from mastercode.runtime import session as _sess  # noqa: E402
_sess._pending_nudges.clear()
_sess.push_nudge("sess-x", f"stop, use {HARD['anthropic-key']} instead")
queued = _sess.consume_nudges("sess-x")
check("nudge: hard shape redacted at push_nudge",
      queued == ["stop, use [REDACTED:anthropic-key] instead"], str(queued))
_sess.push_nudge("sess-x", "keep going")
check("nudge: benign nudge untouched", _sess.consume_nudges("sess-x") == ["keep going"])

# ── 12. \b parity with JS: non-ASCII adjacency ──────────────────────────────
glued = f"café{HARD['anthropic-key']}"
check("parity: \\b next to a non-ASCII letter is a hard hit here as in JS",
      [x.id for x in ss.scan(glued)] == ["anthropic-key"], str([x.id for x in ss.scan(glued)]))

# ── 13. cutting cut text is a no-op ──────────────────────────────────────
# A door cuts the words (server/secret_cut.py) and the turn cuts them again
# (session.send_message, which logs whenever redact() returns ANY finding).
# The second pass must change nothing and find nothing, hard or soft; before
# this, url-credential re-claimed the marker in a link's password slot
# (logging a cut that did not happen) and split it in the user slot.
_once: dict[str, str] = {}
for rid, value in HARD.items():
    for where, x in (
        ("plain", f"here is my key: {value} please use it"),
        ("link password", f"git clone https://alex:{value}@github.com/x/y.git"),
        ("link user", f"git clone https://{value}@github.com/x/y.git"),
        ("link user+password", f"git clone https://{value}:x-oauth-basic@github.com/x/y.git"),
        ("assignment", f"api_key = {value}"),
        ("bearer", f"Authorization: Bearer {value}"),
    ):
        _once[f"{rid} in {where}"] = x
_once["private-key"] = f"my ssh key:\n{pem}\nthanks"
_once["bearer-token"] = "Authorization: Bearer AbCdEfGhIjKlMnOpQrStUvWxYz012345"
_once["url-credential"] = "connect with postgres://app:S3cretPassw0rd@db.internal:5432/prod"
_once["url-credential, both slots"] = "git clone https://alex:S3cretPassw0rd@github.com/x/y.git and https://u:S3cretPassw0rd@h"
# ONE pass cuts a key in a link's user slot AND the link's password, for every
# hard vendor shape: a door that cuts once (Start dialog search's embedding
# call) never runs a second pass. A span a rule claims reads, to the rules
# after it, as the opaque run a marker reads as. Before that, a Telegram token
# (it holds a colon) started url-credential's password group inside itself,
# the overlap lost, and the password went out; a PEM block (spaces) kept
# url-credential from matching the link at all.
_user_slot = dict(HARD, **{"private-key": pem})
for rid, value in _user_slot.items():
    x = f"git clone https://{value}:x-oauth-basic@github.com/x/y.git"
    y, f1 = ss.redact(x)
    want = f"git clone https://[REDACTED:{rid}]:[REDACTED:url-credential]@github.com/x/y.git"
    check(f"one pass, {rid} as a link's user name: the key and the password are both cut",
          y == want and [f.id for f in ss.hard(f1)] == [rid, "url-credential"],
          f"{y!r} findings={[f.id for f in f1]}")
    check(f"one pass, {rid} as a link's user name: a second pass changes nothing and finds nothing",
          ss.redact(y) == (y, []), f"{y!r} -> {ss.redact(y)!r}")
for label, x in _once.items():
    y, f1 = ss.redact(x)
    z, f2 = ss.redact(y)
    check(f"idempotent {label}: the first pass cut it (not vacuous)",
          bool(ss.hard(f1)) and y != x, f"findings={[f.id for f in f1]} out={y!r}")
    check(f"idempotent {label}: a second pass changes nothing",
          z == y, f"{y!r} -> {z!r}")
    check(f"idempotent {label}: a second pass finds nothing, hard or soft",
          not f2, f"findings={[(f.id, f.severity) for f in f2]} in {y!r}")
_both, _ = ss.redact(soft_text, severities=("hard", "soft"))
check("idempotent: soft markers too, when soft is cut",
      ss.redact(_both, severities=("hard", "soft")) == (_both, []), _both)
# Only a marker this file could have written is claimed; it never shields a
# secret beside or inside it.
_br = f"[REDACTED:{HARD['anthropic-key']}]"
check("marker: a key typed inside a look-alike marker is still found",
      [f.id for f in ss.scan(_br)] == ["anthropic-key"], str([f.id for f in ss.scan(_br)]))
_typed = "https://[REDACTED:github-token]:S3cretPassw0rd@db.internal/prod"
_out, _f = ss.redact(_typed)
check("marker: a real password after a marker in a link's user slot is still cut",
      "S3cretPassw0rd" not in _out and [f.id for f in ss.hard(_f)] == ["url-credential"]
      and _out.startswith("https://[REDACTED:github-token]:"), f"{_out!r} {[f.id for f in _f]}")

# ── 14. the composers find what the pod finds ────────────────────────────
# The browser warns from its own copy of scan/redact (the two TS files of
# section 9), so what a person is warned of before sending must be what the
# pod then cuts. Every case of section 13 (every hard rule in its six
# placements, each vendor token as a link's user name with a password, and
# the rest) goes through both TS copies under node and through this module:
# the findings (rule, severity, span, preview), the cut text, and the findings
# in the cut text must be the same. Before this, the TS scan blanked nothing:
# a link whose password slot was already a marker warned as a key, and a
# Telegram token or a PEM block as a link's user name hid the link's password
# from the warning. The cases are ASCII, so a JS offset (UTF-16) is a Python
# one.
_PARITY_JS = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const cases = JSON.parse(readFileSync(0, "utf8"));
const row = (f) => [f.id, f.severity, f.start, f.end, f.preview];
const out = {};
for (const p of process.argv.slice(1)) {
  const m = await import(pathToFileURL(p).href);
  out[p] = cases.map((t) => {
    const cut = m.redactSecrets(t).text;
    return { found: m.scanSecrets(t).map(row), cut, again: m.scanSecrets(cut).map(row) };
  });
}
process.stdout.write(JSON.stringify(out));
"""
_node = shutil.which("node")
if not (os.path.exists(ts_a) and os.path.exists(ts_b)):
    print("SKIP  composer parity: ts copies not present here (in-container run) — covered at repo root")
elif not _node:
    print("SKIP  composer parity: no node on PATH to run the ts copies — run from a checkout that has it")
else:
    _cases = dict(_once)
    for rid, value in _user_slot.items():
        _cases[f"{rid} as a link's user name, with a password"] = (
            f"git clone https://{value}:x-oauth-basic@github.com/x/y.git")
    _labels = list(_cases)
    _texts = [_cases[k] for k in _labels]

    def _row(f):
        return [f.id, f.severity, f.start, f.end, f.preview]

    _pod = []
    for t in _texts:
        cut, _ = ss.redact(t)
        _pod.append({"found": [_row(f) for f in ss.scan(t)], "cut": cut,
                     "again": [_row(f) for f in ss.scan(cut)]})
    try:
        r = subprocess.run([_node, "--input-type=module", "-e", _PARITY_JS, ts_a, ts_b],
                           input=json.dumps(_texts), capture_output=True, text=True, timeout=120)
        _ts = json.loads(r.stdout) if r.returncode == 0 else {}
        _err = "" if r.returncode == 0 else (r.stderr or r.stdout)[-600:]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        _ts, _err = {}, str(e)
    for name, path in (("circles-web", ts_a), ("workbench-web", ts_b)):
        got = _ts.get(path)
        ran = isinstance(got, list) and len(got) == len(_texts)
        for key, what in (("found", "finds what the pod finds (rule, severity, span, preview)"),
                          ("cut", "cuts each to the pod's text"),
                          ("again", "finds in the cut text what the pod finds there (nothing)")):
            diff = [lab for lab, js, py in zip(_labels, got or [], _pod) if js[key] != py[key]] if ran else []
            check(f"composer parity, {name}: in all {len(_texts)} section-13 cases the composer {what}",
                  ran and not diff,
                  _err if not ran else f"{len(diff)} differ, e.g. {diff[:5]}")

print()
print("secret-shapes gate: PASS" if _failures == 0 else f"secret-shapes gate: FAIL ({_failures})")
sys.exit(1 if _failures else 0)
