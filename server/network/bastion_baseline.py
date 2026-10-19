"""server/network/bastion_baseline.py — verifying a bastion's signed accounting.

The bastion takes an accounting of what is normal on its machine
(`observe:baseline`), signs it, and mirrors it into its machine store, where the
oversight ingest reads it beside the findings that stand against it.

WHO SIGNED IT, AND WHY THE HOST CAN TELL. The bastion signs with its OPERATOR
key — the P-256 key it generated at pair time and never sent anywhere. The
certificate for that key was issued by this platform's CA from a CSR the device
signed, and that CSR carried the device fingerprint as a URI SAN, which the host
bound to the bastion's row at redeem (bastion_enroll._fp_from_csr) under the
invariant that the redeem CSR is the only binder. So the host holds everything
it needs and pins nothing new:

    1. the signer certificate chains to the CA in CEIGAS_CERT_DIR
    2. its fingerprint SAN is the fingerprint bound to this bastion's row
    3. the signature verifies under the certificate's key

Fail any one and the accounting is not this machine's word. The three are
reported separately because they mean different things: an unsigned file, a
certificate this CA never issued, a certificate for a different machine, and a
signature that does not match are four situations, and only the last means the
content moved after signing.

WHAT IS SIGNED. Not the bytes of the file — the two sides are different
languages and reproducing one JSON encoder in the other is a contract that
drifts. Both canonicalise identically: parse, drop the two derived keys
(`digest`, `attestation`), sort every key, no whitespace, no HTML escaping;
sign SHA-256 of that. `canonical()` names the SEVEN places the two encoders
differ and how each is closed — re-escaped, refused, aligned, or folded the
same way. The bastion's own self-digest is left alone — it is the
box's quick integrity check, not the host's proof of provenance.

WHEN VALIDITY IS JUDGED. At the moment the accounting was TAKEN, not now —
the same rule the box applies (client-go baselineVerifyBytes). This is
provenance over a document at rest: an accounting signed in March by a
certificate that expired in June is still genuinely that machine's word, and
judging at now() would make every accounting unverifiable at the next
certificate rotation. A certificate that was not valid when the accounting was
taken is refused, by name (`cert_not_valid`). An unreadable taken_at falls
back to now, the stricter choice.

WHAT IS DELIBERATELY NOT CHECKED: keyUsage, revocation, and basicConstraints on
the signer certificate itself. What makes that acceptable is the third test:
the certificate's fingerprint SAN must equal the value the HOST bound at
redeem, so forging an accounting needs the device's own private key whatever
else the certificate says. That argument holds only if nothing but the
platform's CAs can issue such a certificate, so an INTERMEDIATE carried in the
attestation must be a CA (_paths).

WHAT IS A FACT ABOUT THE HOST, NOT THE MACHINE. A pod with no CA file cannot
check anything, and that is `no_ca` — never `not_our_ca`. The first version
folded "I cannot check" into "a foreign CA signed this", which raised a high
finding against every machine in the fleet for a file missing on the host.

This module is pure (bytes in, verdict out) so scripts/test_bastion_baseline.py
can drive it against a fixture the GO side signed — one artefact verified by
both languages, rather than each side trusting its own fake.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

SIG_ALG = "ecdsa-p256-sha256"
FP_SAN_PREFIX = "urn:privatae:hwfp:"  # bastion_enroll._FP_SAN_PREFIX, pair.go hostFPSANPrefix

CERT_DIR = Path(os.environ.get("CEIGAS_CERT_DIR", "/etc/privatae/certs"))
CA_PATH = CERT_DIR / "ceigas_ca.crt"


class CanonicalError(ValueError):
    """The document holds a value the two encoders would not agree on."""
    # Not a refusal of the caller's input (our own encoders disagree): the platform's
    # (tool_call_ledger.refused_if_caller).
    caller_refusal = False


# Integers both encoders print identically. Go's map round-trip turns every
# number into a float64, exact up to 2^53; Python prints ints exactly at any
# size. Beyond this bound the two disagree and a signature over one is a false
# "tampered" verdict on the other.
_INT_EXACT = 2 ** 53


def _check_numbers(v, path="$"):
    if isinstance(v, bool):
        return
    if isinstance(v, float):
        raise CanonicalError(f"{path} is a float ({v!r}); the accounting may hold only "
                             "strings, small integers, booleans, lists and objects")
    if isinstance(v, int):
        if abs(v) > _INT_EXACT:
            raise CanonicalError(f"{path} = {v} exceeds 2^53; the two encoders would "
                                 "not agree on it")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            _check_numbers(x, f"{path}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _check_numbers(x, f"{path}[{i}]")


def canonical(doc: dict) -> bytes:
    """The byte string both sides sign — must match client-go baselineCanonical.

    SEVEN places Go's encoding/json and Python's json.dumps differ on this
    data, measured by the reviewer against the real Go canonicaliser over 33
    inputs; the rest (HTML characters, quotes, backslashes, control characters,
    non-BMP, nested ordering) agree exactly. Each is closed, and HOW is stated
    because "closed" has meant three different things:

      1. U+2028 / U+2029: Go escapes them, Python does not. RE-ESCAPED here.
      2. Integers beyond 2^53. REFUSED, both sides.
      3. Integral floats (1 vs 1.0). REFUSED, both sides.
      4. Small exponents (1e-7 vs 1e-07). REFUSED, both sides.
      5. `-0`: Python's parser makes it 0, Go keeps the literal. REFUSED on the
         Go side (the only side that can see it), so it is never signed.
      6. Numbers inside `digest`/`attestation`: Go used to check before
         dropping the two keys, Python after. Go now drops first — ALIGNED.
      7. A lone surrogate: Go's decoder folds it to U+FFFD; Python used to
         refuse. Python now FOLDS the same way — same bytes.

    A refusal here is a CanonicalError; the verifier reports it as `unreadable`
    rather than `bad_signature`, because a document the host cannot canonicalise
    is not evidence that the machine changed it.
    """
    m = {k: v for k, v in doc.items() if k not in ("digest", "attestation")}
    _check_numbers(m)
    text = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # A lone surrogate is FOLDED to U+FFFD, which is exactly what Go's decoder
    # does with an unpaired \ud800 escape — so both sides produce the same bytes
    # rather than the box signing what the host calls unreadable. (Refusing it
    # here, as an earlier version did, was the asymmetry.)
    text = "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)
    return text.encode("utf-8")


def _load_ca(ca_pem: bytes) -> list[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(ca_pem)
    except Exception:  # noqa: BLE001
        return []


def _cert_fingerprint(cert: x509.Certificate) -> str:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return ""
    for uri in san.value.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith(FP_SAN_PREFIX):
            return uri[len(FP_SAN_PREFIX):]
    return ""


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_rfc3339(s: str, *, assume_utc: bool = False):
    """An AWARE instant, or None.

    STRICT BY DEFAULT: a string with no zone is None, because a naive datetime
    compared against the certificate's aware bounds raised TypeError out of a
    verifier documented as never raising — before the signature check, so a
    certificate that merely chained was enough to reach it. None routes the
    certificate check to the stricter now() fallback.

    assume_utc=True is for the one caller where a wrong answer has no security
    consequence — the stale-mirror instant comparison — so that a naive
    timestamp compares as an instant there instead of falling back to string
    equality, which is the exact failure that comparison exists to avoid.
    """
    import datetime
    s = (s or "").strip()
    if not s:
        return None
    try:
        t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return t.replace(tzinfo=datetime.timezone.utc) if assume_utc else None
    return t


def _validity(cert: x509.Certificate):
    """(not_before, not_after) as aware datetimes, across cryptography versions."""
    nb = getattr(cert, "not_valid_before_utc", None)
    na = getattr(cert, "not_valid_after_utc", None)
    if nb is None or na is None:
        import datetime
        nb = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        na = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return nb, na


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    """One hop: `issuer`'s key signed `cert`."""
    if cert.issuer != issuer.subject:
        return False
    try:
        issuer.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                   ec.ECDSA(cert.signature_hash_algorithm))
        return True
    except (InvalidSignature, TypeError, ValueError):
        try:
            from cryptography.hazmat.primitives.asymmetric import padding
            issuer.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                       padding.PKCS1v15(), cert.signature_hash_algorithm)
            return True
        except Exception:  # noqa: BLE001
            return False


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _path_len_allows(ca: x509.Certificate, below: int) -> bool:
    """Whether `ca`'s pathLenConstraint permits `below` intermediates under it."""
    try:
        n = ca.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length
    except x509.ExtensionNotFound:
        return True
    return n is None or n >= below


def _signer_chain(text: str) -> list[x509.Certificate]:
    """The signer certificate text as [leaf, intermediates...]. Raises
    ValueError with the reason.

    THE TEXT MUST BE THE CANONICAL PEM OF ITS OWN CERTIFICATES, byte for byte:
    the certificates it holds, each re-encoded as standard PEM, concatenated,
    equal to what was carried. The box (client-go baselineSignerChain) applies
    the same test to the same bytes. Two PEM parsers never agree on every
    malformed input (Go's pem.Decode skips a block it cannot decode and
    promotes the next one to leaf; this library refuses), so neither side
    interprets a non-canonical text at all. What the bridge issues and every
    pairing holds is canonical, so nothing real is refused."""
    from cryptography.hazmat.primitives import serialization
    try:
        chain = x509.load_pem_x509_certificates(text.encode())
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"no certificate could be read: {e}") from e
    canon = "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in chain)
    if canon != text:
        raise ValueError("it is not the canonical PEM of its certificates")
    return chain


def _paths(cert: x509.Certificate, cas: list[x509.Certificate],
           intermediates: list[x509.Certificate] = ()) -> list[list[x509.Certificate]]:
    """Every path from `cert` to one of the CAs, as the certificates above the
    leaf: [root] for direct issuance, [intermediate, root] through ONE
    intermediate the attestation carries. Time is not judged here, so the caller
    can tell a certificate this CA never issued (no path) from one that was not
    valid when the accounting was taken (a path, with a hop out of its window).

    The bridge issues operator certificates from the CEIGAS Entity Intermediate
    while the host holds only the root, so a one-hop check refused every real
    accounting as not_our_ca.

    THE INTERMEDIATE MUST BE A CA, and must itself have signed the leaf. Without
    the first, any end-entity certificate the root issued could vouch for a
    certificate carrying ANOTHER machine's fingerprint; without the second, a
    self-signed leaf riding beside the genuine intermediate would pass. Either
    way the fingerprint test below would be satisfied by a forgery. The root's
    pathLenConstraint is honoured, as the box's x509.Verify honours it: a root
    minted with pathlen 0 permits no intermediate at all."""
    cas = [ca for ca in cas if _is_ca(ca)]  # a root must itself be a CA, as x509.Verify requires
    out = [[ca] for ca in cas if _issued_by(cert, ca)]
    for inter in intermediates:
        if not _is_ca(inter) or not _issued_by(cert, inter):
            continue
        out += [[inter, ca] for ca in cas
                if _issued_by(inter, ca) and _path_len_allows(ca, 1)]
    return out


def verify_signed_baseline(raw: bytes, *, ca_pem: bytes,
                           expected_fingerprint: Optional[str]) -> dict:
    """Judge a mirrored accounting. Returns a dict, never raises:

        ok            bool
        state         verified | unsigned | wrong_alg | bad_cert | no_ca |
                      not_our_ca | cert_not_valid | wrong_machine |
                      no_fingerprint | bad_signature | unreadable
        reason        one sentence for a person
        signed_by     the host named in the attestation, if any
        taken_at      the accounting's own timestamp, if readable
        fingerprint   the fingerprint the signer certificate carries, if any

    `expected_fingerprint` is the value bound on the bastion's row. None means
    the row has no binding (an old client paired bearer-only), in which case the
    fingerprint test cannot be applied and the result says so rather than
    passing it silently.
    """
    out = {"ok": False, "state": "unreadable", "reason": "", "signed_by": "",
           "taken_at": "", "fingerprint": ""}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        out["reason"] = f"the accounting is not valid JSON: {e}"
        return out
    if not isinstance(doc, dict):
        out["reason"] = "the accounting is not an object"
        return out
    out["taken_at"] = str(doc.get("taken_at") or "")

    att = doc.get("attestation")
    if not isinstance(att, dict):
        out["state"] = "unsigned"
        out["reason"] = "this accounting is not signed"
        return out
    out["signed_by"] = str(att.get("signed_by") or "")
    if att.get("alg") != SIG_ALG:
        out["state"] = "wrong_alg"
        out["reason"] = f"signed with {att.get('alg')!r}, which this host does not verify"
        return out

    # The operator certificate first, then the intermediate that issued it.
    try:
        chain = _signer_chain(str(att.get("signer_cert") or ""))
    except Exception as e:  # noqa: BLE001
        out["state"] = "bad_cert"
        out["reason"] = f"the signer certificate cannot be parsed: {e}"
        return out
    cert, intermediates = chain[0], chain[1:]

    cas = _load_ca(ca_pem)
    if not cas:
        # A FACT ABOUT THE HOST. No CA to check against is not "a foreign CA
        # signed this", and reporting it as one raised a high finding against
        # every machine for a file missing on the pod.
        out["state"] = "no_ca"
        out["reason"] = ("this host holds no platform CA to check the signer certificate "
                         f"against ({CA_PATH}); nothing about the machine is known")
        return out
    at = _parse_rfc3339(out["taken_at"]) or _now()

    # EVERY CERTIFICATE ON THE PATH is judged at taken_at — leaf, intermediate
    # and root — as the box's x509.Verify judges them. The leaf first and before
    # any path is looked for, in the box's order, so an out-of-window leaf is
    # named as such whoever issued it. Then a path whose hops were all valid is
    # enough; otherwise the first hop out of its window is named.
    def _out_of_window(c):
        nb, na = _validity(c)
        return nb is not None and na is not None and not (nb <= at <= na)

    bad = None
    if _out_of_window(cert):
        bad = cert
    else:
        paths = _paths(cert, cas, intermediates)
        if not paths:
            out["state"] = "not_our_ca"
            out["reason"] = "the signer certificate was not issued by this platform's CA"
            return out
        if not any(not any(_out_of_window(c) for c in p) for p in paths):
            bad = next(c for c in paths[0] if _out_of_window(c))
    if bad is not None:
        which = ("the signer certificate" if bad is cert else
                 "the certificate that issued it (" + bad.subject.rfc4514_string() + ")")
        out["state"] = "cert_not_valid"
        out["reason"] = (f"{which} was not valid at the time this accounting "
                         f"was taken ({out['taken_at'] or 'unknown'})")
        return out

    fp = _cert_fingerprint(cert)
    out["fingerprint"] = fp
    if not fp:
        out["state"] = "no_fingerprint"
        out["reason"] = "the signer certificate carries no device fingerprint"
        return out
    if expected_fingerprint is None:
        out["state"] = "no_fingerprint"
        out["reason"] = ("this bastion's row has no bound fingerprint, so the certificate "
                         "cannot be tied to the machine")
        return out
    if fp != expected_fingerprint:
        out["state"] = "wrong_machine"
        out["reason"] = "the signer certificate belongs to a different machine"
        return out

    try:
        sig = base64.b64decode(str(att.get("signature") or ""), validate=True)
    except (ValueError, TypeError):
        out["state"] = "bad_signature"
        out["reason"] = "the signature is not base64, so it cannot have been produced by any key"
        return out
    pub = cert.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey):
        out["state"] = "bad_cert"
        out["reason"] = "the signer certificate does not hold an EC key"
        return out
    try:
        digest = hashlib.sha256(canonical(doc)).digest()
    except CanonicalError as e:
        out["state"] = "unreadable"
        out["reason"] = f"the accounting cannot be canonicalised, so its signature cannot be judged: {e}"
        return out
    try:
        pub.verify(sig, digest, ec.ECDSA(utils_prehashed()))
    except InvalidSignature:
        out["state"] = "bad_signature"
        out["reason"] = (f"the signature does not match this accounting — its content has "
                         f"changed since {out['signed_by']}'s key signed it")
        return out
    out["ok"] = True
    out["state"] = "verified"
    out["reason"] = (f"signed by {out['signed_by']}'s pairing key; verified against the "
                     "platform CA and this machine's bound fingerprint")
    return out


def utils_prehashed():
    """The Go side signs SHA-256(canonical) directly (ecdsa.SignASN1 over the
    digest), so the verifier must be told the data is already hashed."""
    from cryptography.hazmat.primitives.asymmetric import utils
    return utils.Prehashed(hashes.SHA256())


def read_ca() -> bytes:
    try:
        return CA_PATH.read_bytes()
    except OSError:
        return b""
