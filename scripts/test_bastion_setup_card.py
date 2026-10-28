#!/usr/bin/env python3
"""The "Set up a bastion" token card must survive the list reload that follows it.

Measured 2026-09-22 on one Host: the card opened and vanished, on every host
tried. The kernel side was fine — the log shows one pairing token minted per
click, forty in three minutes — but loadBastions() closes whatever inspector is
open (it renders a row snapshot; see its own comment), and setupBastion reloaded
the list right after opening the card, so the close landed on the reload's
continuation, after GET /api/network/bastions resolved. A single-use token
nobody can read is a pairing nobody can do.

The shape this pins: setupBastion opens the card FIRST (it must not wait on the
reload — fetchJSON has no timeout, and a hung bastion-latest would hide the token
by a different route), then reloads with `loadBastions({ keepInspector: true })`,
and loadBastions' close is guarded by that flag. Nothing after the card may close
the inspector or reload the list without the flag.

Source-reading, like scripts/test_network_map_routes.py. The behavioural side is
scripts/test_network_anchor_step.js (run by test_anchor_step_is_visible.py): it
drives the real setupBastion in a headless DOM and, since 2026-09-22, settles on
a real timer and asserts the inspector is not hidden afterwards — measured to
FAIL on the pre-fix app.js and pass on this one. Before that assertion it read
only innerHTML, which a close leaves intact, and passed on the broken code too.
Set NETWORK_APP_JS to check a different copy of the file (the mutation run that
proves this gate can fail).
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.environ.get("NETWORK_APP_JS") or os.path.join(_ROOT, "system-apps/network/app.js")

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def _fn_body(src: str, name: str) -> str:
    """The exact text of `async function NAME(...) { ... }`, brace-matched from
    its opening brace. Strings and comments are not parsed, so a brace inside a
    string literal would skew it — none of the two functions this reads has one,
    and a skew shows up as a wrong extraction, not a silent pass."""
    m = re.search(r"async function " + re.escape(name) + r"\([^)]*\)\s*\{", src)
    if not m:
        return ""
    depth, i = 0, m.end() - 1
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): i + 1]
        i += 1
    return ""


def _invented_ca_pem() -> str:
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Host CA invented")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def one_line_card() -> None:
    """The one-line card, driven headless (test_network_one_line_card.js) with
    a payload from the real pairing_code and one_line: the card's first line
    decodes to the redeem URL, CA SHA-256 and token the server minted."""
    import base64
    import hashlib
    import json
    import subprocess
    import tempfile
    sys.path.insert(0, _ROOT)
    from server.network import bastion_enroll as BE
    ca = _invented_ca_pem()
    der = base64.b64decode("".join(l for l in ca.splitlines() if "-----" not in l))
    enroll = "https://joey.example-id.cloud.example:8810/v1/local/redeem"
    token = "skb_" + "inventedTokenForThisTestOnly0123"
    code = BE.pairing_code(enroll, ca, token)
    payload = {
        "token": token, "expires_in_s": 900, "enroll": enroll, "pairing_code": code,
        # Never sent by the server (public_address); the card must show it as text anyway.
        "pairing_window": {"address": "<img src=x onerror=alert(1)>", "expires_in_s": 900, "browser": True},
        "run": {"install": "macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh",
                "pair": f"privatae-bastion pair --redeem-url {enroll} --host-ca host-ca.pem {token}",
                "note": "Run the pair command on the machine you want as a bastion.",
                "one_line": BE.one_line(code)},
    }
    given = {"payload": payload, "elsewhere": "203.0.113.7",
             "expect": {"u": enroll, "ca": hashlib.sha256(der).hexdigest(), "t": token}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(given, fh)
        path = fh.name
    try:
        proc = subprocess.run(["node", os.path.join(_ROOT, "scripts", "test_network_one_line_card.js"), path],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "NETWORK_APP_JS": _APP_JS})
        sys.stdout.write(proc.stdout)
        check(proc.returncode == 0, "the one-line card holds, driven headless with the real pairing code"
              + ("" if proc.returncode == 0 else ": " + proc.stderr.strip()[-400:]))
    finally:
        os.unlink(path)


def main() -> int:
    src = open(_APP_JS, encoding="utf-8").read()

    load = _fn_body(src, "loadBastions")
    check(bool(load), "loadBastions() exists in the network app")
    sig = re.match(r"async function loadBastions\(([^)]*)\)", load)
    check(bool(sig) and "keepInspector" in sig.group(1),
          "loadBastions takes a keepInspector option")
    check("if (!keepInspector) closeInspector();" in load,
          "loadBastions' close of the open inspector is guarded by keepInspector "
          "(an unconditional close is the 2026-09-12 shape that took the token off the screen)")
    check(load.count("closeInspector(") == 1,
          "loadBastions closes the inspector in exactly one place, the guarded one")

    setup = _fn_body(src, "setupBastion")
    check(bool(setup), "setupBastion() exists in the network app")
    card_at = setup.find("openInspector('Bastion ready")
    check(card_at != -1, "setupBastion opens the 'Bastion ready' token card "
                         "(a copy edit to that title must update this gate — loud on purpose)")
    tail = setup[card_at:] if card_at != -1 else ""
    check("loadBastions({ keepInspector: true })" in tail,
          "after the card, the list reload is told to keep the inspector")
    check(re.search(r"loadBastions\(\s*\)", tail) is None,
          "no bare loadBastions() after the card (that reload would close it)")
    check("closeInspector(" not in tail,
          "nothing after the card closes the inspector")
    check("await loadBastions" not in setup,
          "the card does not wait on the reload (a hung request would hide the token again)")

    one_line_card()

    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nok — setup card survives the reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
