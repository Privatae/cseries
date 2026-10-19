#!/usr/bin/env python3
"""Verifying a bastion's signed accounting — against a document GO signed.

Runs as pytest (`pytest scripts/test_bastion_baseline.py`) AND as a plain
script (`APP_ROOT=<repo> python3 scripts/test_bastion_baseline.py`). No DB, no
network.

THE FIXTURE IS THE POINT. tests/fixtures/signed-baseline/ holds an accounting
signed by the bastion's own Go code (the bastion's client-go
baselinesign_test.go, TestWriteCrossLanguageFixture), with the CA that issued
the signer certificate and the fingerprint bound in its SAN. This file verifies
THAT artefact. Two languages, one signed document — so the claim that both
sides canonicalise identically is tested rather than asserted, and the class of
bug where each side's test passes against its own fake cannot happen here.

To regenerate after a deliberate change to the canonical form:
    cd <bastion source>/client-go && \\
    BASELINE_FIXTURE_OUT=<cseries>/tests/fixtures/signed-baseline \\
    go test -run TestWriteCrossLanguageFixture -count=1 .
"""
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network.bastion_baseline import (  # noqa: E402
    canonical, verify_signed_baseline,
)

FIX = os.path.join(_ROOT, "tests", "fixtures", "signed-baseline")


def _fixture():
    with open(os.path.join(FIX, "baseline.json"), "rb") as f:
        raw = f.read()
    with open(os.path.join(FIX, "ca.pem"), "rb") as f:
        ca = f.read()
    with open(os.path.join(FIX, "fingerprint"), encoding="utf-8") as f:
        fp = f.read().strip()
    return raw, ca, fp


def test_the_go_signed_fixture_verifies():
    raw, ca, fp = _fixture()
    v = verify_signed_baseline(raw, ca_pem=ca, expected_fingerprint=fp)
    assert v["ok"], f"the Go-signed accounting did not verify: {v}"
    assert v["state"] == "verified"
    assert v["signed_by"] == "fixture.host.example.test"
    assert v["fingerprint"] == fp


def test_the_fixture_exercises_every_escaping_difference():
    """The sample the Go side signs carries a quote, a backslash, a non-ASCII
    rune and the HTML characters Go escapes by default. If the fixture lost
    those, a canonicalisation drift on exactly those characters would go
    unnoticed — so the test checks the fixture is still the hard one."""
    raw, _, _ = _fixture()
    doc = json.loads(raw)
    exes = [e.get("exe", "") for e in doc["entries"]]
    hard = [e for e in exes if "<" in e and "&" in e and '"' in e and "\\" in e and "ï" in e]
    assert hard, f"the fixture no longer carries the hard path: {exes}"


def test_editing_content_fails_with_the_content_reason():
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["unreadable"] = doc["unreadable"] + 1
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert not v["ok"] and v["state"] == "bad_signature", v
    assert "content has changed" in v["reason"], v["reason"]


def test_reformatting_without_editing_still_verifies():
    """Whitespace, key order and Python's default escaping are not content."""
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    reformatted = json.dumps(doc, indent=4, sort_keys=False).encode()
    assert reformatted != raw
    v = verify_signed_baseline(reformatted, ca_pem=ca, expected_fingerprint=fp)
    assert v["ok"], f"a byte-different but content-identical file failed: {v}"


def test_a_certificate_from_another_ca_is_refused():
    raw, _, fp = _fixture()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "some other CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    other = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
             .public_key(key.public_key()).serial_number(1)
             .not_valid_before(now - datetime.timedelta(hours=1))
             .not_valid_after(now + datetime.timedelta(hours=1))
             .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
             .sign(key, hashes.SHA256()))
    other_pem = other.public_bytes(serialization.Encoding.PEM)
    v = verify_signed_baseline(raw, ca_pem=other_pem, expected_fingerprint=fp)
    assert not v["ok"] and v["state"] == "not_our_ca", v


def test_a_certificate_for_another_machine_is_refused():
    raw, ca, _ = _fixture()
    v = verify_signed_baseline(raw, ca_pem=ca, expected_fingerprint="some-other-box")
    assert not v["ok"] and v["state"] == "wrong_machine", v


def test_an_unbound_row_cannot_pass_the_fingerprint_test():
    """None means the bastion row has no bound fingerprint (bearer-only). The
    test cannot be applied, and the result must say so — never pass silently."""
    raw, ca, _ = _fixture()
    v = verify_signed_baseline(raw, ca_pem=ca, expected_fingerprint=None)
    assert not v["ok"] and v["state"] == "no_fingerprint", v


def test_an_unsigned_accounting_is_named_unsigned():
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc.pop("attestation")
    doc["unsigned_reason"] = "several hosts are paired and none is the anchor"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "unsigned" and not v["ok"], v


def test_a_tampered_signature_fails_not_crashes():
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["attestation"]["signature"] = "AAAA"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert not v["ok"] and v["state"] == "bad_signature", v
    doc["attestation"]["signature"] = "not base64 at all!"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert not v["ok"] and v["state"] == "bad_signature", v


def test_garbage_is_unreadable_not_an_exception():
    _, ca, fp = _fixture()
    for junk in (b"", b"\xff\xfe", b"[1,2,3]", b'{"a":'):
        v = verify_signed_baseline(junk, ca_pem=ca, expected_fingerprint=fp)
        assert not v["ok"] and v["state"] == "unreadable", (junk, v)


def test_canonical_form_re_escapes_the_two_go_differs_on():
    """Go escapes U+2028/U+2029 even with HTML escaping off; Python does not.
    The host re-escapes them so a process whose argv carried a line separator
    verifies the same on both sides."""
    c = canonical({"x": "a b c", "digest": "drop", "attestation": {"drop": 1}})
    assert c == b'{"x":"a\\u2028b\\u2029c"}', c



def test_a_missing_ca_is_a_fact_about_the_host_not_the_machine():
    """No CA on the pod must NEVER read as 'a foreign CA signed this'."""
    raw, _, fp = _fixture()
    v = verify_signed_baseline(raw, ca_pem=b"", expected_fingerprint=fp)
    assert v["state"] == "no_ca" and not v["ok"], v
    assert "foreign" not in v["reason"] and "not issued" not in v["reason"], v["reason"]
    from server.network import bastion_baseline as bb
    from unittest import mock
    with mock.patch.object(bb, "CA_PATH", bb.Path("/nonexistent/ceigas_ca.crt")):
        assert bb.read_ca() == b"", "read_ca did not degrade to empty on a missing file"


def test_a_float_in_the_accounting_is_unreadable_not_tampered():
    """The two encoders disagree on floats; a document the host cannot
    canonicalise is not evidence the machine changed it."""
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["load_average"] = 1.0
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "unreadable", v
    assert "canonicalise" in v["reason"], v["reason"]
    doc = json.loads(raw)
    doc["unreadable"] = 2 ** 60
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "unreadable", v


def test_a_lone_surrogate_never_raises_and_is_judged_like_go():
    """A lone surrogate was once refused as `unreadable`; it now folds to
    U+FFFD exactly as Go's decoder does, so the two sides agree on the bytes.
    Here the surrogate is an EDIT to a signed document, so the honest verdict
    is a content change — and never an exception."""
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["entries"][0]["exe"] = "/bin/\ud800bad"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "bad_signature", v


def test_an_unknown_algorithm_is_refused_by_name():
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["attestation"]["alg"] = "rsa-pss-sha512"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "wrong_alg" and "rsa-pss-sha512" in v["reason"], v


def test_editing_the_unsigned_reason_on_a_signed_file_is_a_content_change():
    """unsigned_reason is covered by the signature (only digest and attestation
    are dropped). A signed file whose reason is edited must fail."""
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["unsigned_reason"] = "planted"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "bad_signature", v


def _py_ca_and_leaf(fp, not_before, not_after):
    """A CA and a leaf issued by it, entirely in Python, for the validity tests
    the Go fixture cannot express (its certificate is ten years wide)."""
    import datetime  # noqa: F401
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "py test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(not_before).not_valid_after(not_after)
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(not_before).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("urn:privatae:hwfp:" + fp)]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return (ca.public_bytes(serialization.Encoding.PEM),
            leaf.public_bytes(serialization.Encoding.PEM), leaf_key)


def _sign_doc(doc, leaf_pem, leaf_key, signed_by="py.host.example.test"):
    import hashlib
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    digest = hashlib.sha256(canonical(doc)).digest()
    sig = leaf_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    import base64
    doc = dict(doc)
    doc["attestation"] = {"alg": "ecdsa-p256-sha256", "signed_by": signed_by,
                          "signer_cert": leaf_pem.decode(), "signature": base64.b64encode(sig).decode()}
    return json.dumps(doc).encode()


def test_validity_is_judged_when_the_accounting_was_taken():
    """A certificate that has since EXPIRED still verifies an accounting taken
    while it was valid; one not yet valid at taken_at is refused by name."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    ca, leaf, key = _py_ca_and_leaf("fp-py", now - datetime.timedelta(days=90),
                                    now - datetime.timedelta(days=30))
    base = {"version": "privatae-baseline-v2", "coverage": "processes=all,schedulers=all",
            "entries": [], "unreadable": 0}
    # Taken 60 days ago: inside the window, though the cert is expired NOW.
    taken = dict(base, taken_at=(now - datetime.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    v = verify_signed_baseline(_sign_doc(taken, leaf, key), ca_pem=ca, expected_fingerprint="fp-py")
    assert v["ok"], f"an accounting taken while the cert was valid failed: {v}"
    # Taken 100 days ago: before the cert existed.
    early = dict(base, taken_at=(now - datetime.timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    v = verify_signed_baseline(_sign_doc(early, leaf, key), ca_pem=ca, expected_fingerprint="fp-py")
    assert v["state"] == "cert_not_valid", v


def test_a_python_signed_document_verifies_the_same_way():
    """The verifier is not tied to the Go fixture: a document signed here under
    the same recipe verifies, and its edited form fails."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    ca, leaf, key = _py_ca_and_leaf("fp-py", now - datetime.timedelta(days=1),
                                    now + datetime.timedelta(days=1))
    doc = {"version": "privatae-baseline-v2", "taken_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "coverage": "processes=all,schedulers=all", "entries": [], "unreadable": 0}
    signed = _sign_doc(doc, leaf, key)
    assert verify_signed_baseline(signed, ca_pem=ca, expected_fingerprint="fp-py")["ok"]
    tampered = json.loads(signed); tampered["unreadable"] = 9
    v = verify_signed_baseline(json.dumps(tampered).encode(), ca_pem=ca, expected_fingerprint="fp-py")
    assert v["state"] == "bad_signature", v



def test_a_naive_or_odd_taken_at_never_raises():
    """A timezone-naive taken_at raised TypeError out of a verifier documented
    as never raising — before the signature check, so a certificate that merely
    chained was enough to reach it. Every shape must come back as a dict."""
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    for odd in ("2026-09-19T10:00:00", "2026-09-19", "not a time", "", None, 42):
        d = dict(doc); d["taken_at"] = odd
        v = verify_signed_baseline(json.dumps(d).encode(), ca_pem=ca, expected_fingerprint=fp)
        assert isinstance(v, dict) and "state" in v, (odd, v)
        # taken_at is inside the signature, so any edit lands as bad_signature
        # (or cert_not_valid if the now() fallback is outside the window).
        assert v["state"] in ("bad_signature", "cert_not_valid"), (odd, v)


def test_a_lone_surrogate_folds_to_replacement_like_go():
    """Go's decoder folds an unpaired \\ud800 to U+FFFD; the host must produce
    the SAME bytes rather than refuse, or the box signs what the host calls
    unreadable."""
    c = canonical({"v": "\ud800"})
    assert c == b'{"v":"\xef\xbf\xbd"}', c


def test_read_ca_returns_the_file_bytes():
    """The one function that touches the filesystem, in the direction that
    matters: a present file comes back as its bytes. (Asserting only that a
    missing file gives b'' was satisfied by a read_ca that ALWAYS gave b''.)"""
    import tempfile
    from unittest import mock

    from server.network import bastion_baseline as bb
    with tempfile.TemporaryDirectory() as d:
        p = bb.Path(d) / "ceigas_ca.crt"
        p.write_bytes(b"-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n")
        with mock.patch.object(bb, "CA_PATH", p):
            assert bb.read_ca() == p.read_bytes()
        with mock.patch.object(bb, "CA_PATH", bb.Path(d) / "absent.crt"):
            assert bb.read_ca() == b""


def test_a_non_base64_signature_is_named_as_such():
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    # "AAAA!!!!" is the input that tells validate=True from validate=False:
    # with validation off the "!" characters are silently discarded and the
    # rest decodes, so the reason would be the generic one. ("not base64 at
    # all!" fails the padding check either way and could not tell them apart —
    # the mutation survived it.)
    doc["attestation"]["signature"] = "AAAA!!!!"
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "bad_signature" and "not base64" in v["reason"], v
    doc["attestation"]["signature"] = "AAAA"  # valid base64, wrong signature
    v = verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)
    assert v["state"] == "bad_signature" and "not base64" not in v["reason"], v


def test_the_surrogate_fold_covers_both_ends_of_the_range():
    """The upper bound is load-bearing: `< 0xDFFF` instead of `<=` reopens the
    round-5 raise for exactly one code point, U+DFFF, silently."""
    for ch in ("\ud800", "\udfff", "\udc00", "\udbff"):
        c = canonical({"v": ch})
        assert c == b'{"v":"\xef\xbf\xbd"}', (hex(ord(ch)), c)
    # A valid pair followed by an orphan: the pair survives, the orphan folds.
    c = canonical({"v": "\U0001F600\ud800"})
    assert c == b'{"v":"\xf0\x9f\x98\x80\xef\xbf\xbd"}', c


def test_the_ca_path_is_the_platform_ca_in_the_cert_dir():
    """The constant the read_ca test cannot see: the filename must be the one
    genesis writes (server/routes/genesis.py: CERT_DIR / 'ceigas_ca.crt')."""
    from server.network import bastion_baseline as bb
    assert bb.CA_PATH.name == "ceigas_ca.crt", bb.CA_PATH
    assert bb.CA_PATH.parent == bb.CERT_DIR


def test_the_certificate_check_stays_strict_while_the_instant_compare_is_tolerant():
    from server.network.bastion_baseline import _parse_rfc3339
    assert _parse_rfc3339("2026-09-19T10:00:00") is None
    t = _parse_rfc3339("2026-09-19T10:00:00", assume_utc=True)
    assert t is not None and t.tzinfo is not None


def _py_issue(subject, issuer_name, pub, signer_key, *, ca, fp=None, window=None, pathlen="default"):
    # A self-signed root gets no pathLenConstraint unless asked; an intermediate
    # gets 0, as the fleet's does.
    if pathlen == "default":
        pathlen = None if subject == issuer_name else (0 if ca else None)
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    nb, na = window or (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                        datetime.datetime(2036, 1, 1, tzinfo=datetime.timezone.utc))
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
         .public_key(pub).serial_number(x509.random_serial_number())
         .not_valid_before(nb).not_valid_after(na)
         .add_extension(x509.BasicConstraints(ca=ca, path_length=pathlen), critical=True))
    if fp:
        b = b.add_extension(x509.SubjectAlternativeName(
            [x509.UniformResourceIdentifier("urn:privatae:hwfp:" + fp)]), critical=False)
    return b.sign(signer_key, hashes.SHA256())


def _pem(*certs):
    from cryptography.hazmat.primitives import serialization
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


_SAMPLE = {"version": "privatae-baseline-v2", "taken_at": "2026-09-19T10:00:00Z",
           "coverage": "processes=all,schedulers=all", "entries": [], "unreadable": 0}


def test_the_go_fixture_is_the_fleets_chained_shape():
    """The fixture must stay the shape machines send — a leaf issued by an
    intermediate, carried after it, against a root-only CA file. A fixture
    issued straight from the CA is how the one-hop verifier shipped while every
    real accounting read as not_our_ca."""
    from cryptography import x509
    raw, ca, _ = _fixture()
    chain = x509.load_pem_x509_certificates(json.loads(raw)["attestation"]["signer_cert"].encode())
    roots = x509.load_pem_x509_certificates(ca)
    assert len(chain) >= 2, "the fixture's signer certificate carries no intermediate"
    assert all(chain[0].issuer != r.subject for r in roots), \
        "the fixture's leaf is issued straight from the CA, not through an intermediate"


def test_an_intermediate_that_is_not_a_ca_cannot_vouch_for_a_forged_fingerprint():
    """A CA:FALSE certificate the root issued (any end-entity certificate whose
    key someone else holds) signs a certificate carrying machine A's
    fingerprint and rides along as the 'intermediate'. It chains to the root in
    one hop and the fingerprint test would pass; only the CA requirement on the
    intermediate stops it."""
    from cryptography.hazmat.primitives.asymmetric import ec
    root_k, inter_k, b_k, forged_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(4))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True)
    b_leaf = _py_issue("end-entity-b", "root", b_k.public_key(), root_k, ca=False, fp="fp-b")
    forged = _py_issue("operator", "end-entity-b", forged_k.public_key(), b_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(forged, b_leaf), forged_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "not_our_ca", v
    # The same machinery with a genuine chain verifies — the refusal above is
    # about the non-CA hop, not a harness that refuses everything.
    a_k = ec.generate_private_key(ec.SECP256R1())
    a_leaf = _py_issue("operator", "inter", a_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(a_leaf, inter, inter), a_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert v["ok"], v


def test_an_intermediate_not_valid_when_the_accounting_was_taken_is_refused():
    import datetime
    from cryptography.hazmat.primitives.asymmetric import ec
    utc = datetime.timezone.utc
    root_k, inter_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    # Expired before _SAMPLE's taken_at (2026-09-19).
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True,
                      window=(datetime.datetime(2026, 1, 1, tzinfo=utc),
                              datetime.datetime(2026, 6, 1, tzinfo=utc)))
    leaf = _py_issue("operator", "inter", leaf_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, inter), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    # Named as a validity failure, as the box names it: the path exists, a hop
    # on it was out of its window when the accounting was taken.
    assert not v["ok"] and v["state"] == "cert_not_valid", v
    assert "issued it" in v["reason"], v


def test_an_intermediate_not_yet_valid_when_the_accounting_was_taken_is_refused():
    import datetime
    from cryptography.hazmat.primitives.asymmetric import ec
    utc = datetime.timezone.utc
    root_k, inter_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    # Starts after _SAMPLE's taken_at (2026-09-19).
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True,
                      window=(datetime.datetime(2026, 10, 1, tzinfo=utc),
                              datetime.datetime(2036, 1, 1, tzinfo=utc)))
    leaf = _py_issue("operator", "inter", leaf_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, inter), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "cert_not_valid", v


def test_a_root_not_valid_when_the_accounting_was_taken_is_refused():
    import datetime
    from cryptography.hazmat.primitives.asymmetric import ec
    utc = datetime.timezone.utc
    root_k, inter_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True,
                     window=(datetime.datetime(2026, 1, 1, tzinfo=utc),
                             datetime.datetime(2026, 6, 1, tzinfo=utc)))
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True)
    leaf = _py_issue("operator", "inter", leaf_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, inter), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "cert_not_valid", v


def test_a_self_signed_leaf_beside_the_genuine_intermediate_is_refused():
    """The intermediate must have SIGNED the leaf. An attacker's own leaf,
    self-signed with the victim's fingerprint, carried next to the real (public)
    intermediate, must not verify."""
    from cryptography.hazmat.primitives.asymmetric import ec
    root_k, inter_k, forged_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True)
    # Named as issued by the intermediate, but signed by the forger's own key.
    forged = _py_issue("operator", "inter", forged_k.public_key(), forged_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(forged, inter), forged_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "not_our_ca", v


def test_a_root_with_pathlen_zero_permits_no_intermediate():
    """As the box's x509.Verify: pathLenConstraint 0 on the root forbids any
    intermediate beneath it."""
    from cryptography.hazmat.primitives.asymmetric import ec
    root_k, inter_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True, pathlen=0)
    inter = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True)
    leaf = _py_issue("operator", "inter", leaf_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, inter), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "not_our_ca", v


def test_a_chain_through_two_intermediates_is_refused():
    """One intermediate at most, as the box's verifier now requires, so the
    two sides never disagree about the same accounting."""
    from cryptography.hazmat.primitives.asymmetric import ec
    root_k, i1_k, i2_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(4))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    i1 = _py_issue("i1", "root", i1_k.public_key(), root_k, ca=True, pathlen=None)
    i2 = _py_issue("i2", "i1", i2_k.public_key(), i1_k, ca=True)
    leaf = _py_issue("operator", "i2", leaf_k.public_key(), i2_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, i2, i1), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "not_our_ca", v


def _edit_signer(edit):
    raw, ca, fp = _fixture()
    doc = json.loads(raw)
    doc["attestation"]["signer_cert"] = edit(doc["attestation"]["signer_cert"])
    return verify_signed_baseline(json.dumps(doc).encode(), ca_pem=ca, expected_fingerprint=fp)


def _after_leaf(s):
    i = s.index("-----END CERTIFICATE-----\n") + len("-----END CERTIFICATE-----\n")
    return s[:i], s[i:]


def test_a_root_that_is_not_a_ca_anchors_nothing():
    """x509.Verify on the box will not anchor on a root without CA:TRUE."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    root_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "root")])
    utc = datetime.timezone.utc
    root = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(root_k.public_key()).serial_number(1)
            .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=utc))
            .not_valid_after(datetime.datetime(2036, 1, 1, tzinfo=utc))
            .sign(root_k, hashes.SHA256()))  # no basicConstraints at all
    leaf = _py_issue("operator", "root", leaf_k.public_key(), root_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "not_our_ca", v


def test_any_one_valid_path_is_enough():
    """Certificate rotation: the same intermediate key reissued, the expired
    copy carried FIRST. The valid path must win, as it does on the box."""
    import datetime
    from cryptography.hazmat.primitives.asymmetric import ec
    utc = datetime.timezone.utc
    root_k, inter_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _py_issue("root", "root", root_k.public_key(), root_k, ca=True)
    old = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True,
                    window=(datetime.datetime(2025, 1, 1, tzinfo=utc),
                            datetime.datetime(2026, 6, 1, tzinfo=utc)))
    new = _py_issue("inter", "root", inter_k.public_key(), root_k, ca=True)
    leaf = _py_issue("operator", "inter", leaf_k.public_key(), inter_k, ca=False, fp="fp-a")
    raw = _sign_doc(_SAMPLE, _pem(leaf, old, new), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(root), expected_fingerprint="fp-a")
    assert v["ok"], v


def test_an_expired_leaf_is_named_before_its_issuer_is_looked_for():
    """The box judges the leaf's window first, so an out-of-window leaf reads
    cert_not_valid even under a foreign root. Same verdict here."""
    import datetime
    from cryptography.hazmat.primitives.asymmetric import ec
    utc = datetime.timezone.utc
    root_k, other_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    other = _py_issue("other", "other", other_k.public_key(), other_k, ca=True)
    leaf = _py_issue("operator", "somewhere", leaf_k.public_key(), root_k, ca=False, fp="fp-a",
                     window=(datetime.datetime(2025, 1, 1, tzinfo=utc),
                             datetime.datetime(2026, 6, 1, tzinfo=utc)))
    raw = _sign_doc(_SAMPLE, _pem(leaf), leaf_k)
    v = verify_signed_baseline(raw, ca_pem=_pem(other), expected_fingerprint="fp-a")
    assert not v["ok"] and v["state"] == "cert_not_valid", v


_KEY_BLOCK = "-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----\n"
_NON_CANONICAL = {
    "leading text": lambda s: "operator certificate:\n" + s,
    "leading space": lambda s: " " + s,
    "key block first": lambda s: _KEY_BLOCK + s,
    "key block inside": lambda s: _after_leaf(s)[0] + _KEY_BLOCK + _after_leaf(s)[1],
    "other label": lambda s: _after_leaf(s)[0] + _after_leaf(s)[1].replace("CERTIFICATE-----", "X509 CERTIFICATE-----"),
    "indented intermediate": lambda s: _after_leaf(s)[0] + "  " + _after_leaf(s)[1],
    "malformed trailing": lambda s: s + "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    "undecodable decoy": lambda s: "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n" + s,
    "header in leaf": lambda s: s.replace("-----BEGIN CERTIFICATE-----\n",
                                          "-----BEGIN CERTIFICATE-----\nProc-Type: 4,ENCRYPTED\n\n", 1),
    "space after BEGIN": lambda s: s.replace("-----BEGIN CERTIFICATE-----\n", "-----BEGIN CERTIFICATE----- \n", 1),
    "text after leaf END": lambda s: _after_leaf(s)[0][:-1] + " trailing\n" + _after_leaf(s)[1],
    "CRLF": lambda s: s.replace("\n", "\r\n"),
    "no final newline": lambda s: s[:-1],
    "re-wrapped base64": lambda s: s.replace("\n", "", 2),
    "just a BEGIN line": lambda s: "-----BEGIN CERTIFICATE-----",
    "empty": lambda s: "",
    "extra final newline": lambda s: s + "\n",
    "text after last END": lambda s: s + "trailing text\n",
}


def test_a_non_canonical_signer_text_is_refused():
    """The signer text must be the canonical PEM of its certificates. Every
    shape here was once read one way by Go's pem.Decode and another here —
    some accepted on one side only. Each is refused on both; the box's suite
    (TestANonCanonicalSignerTextIsRefused) holds the same list. None may
    raise: 'just a BEGIN line' used to reach an IndexError."""
    for name, edit in _NON_CANONICAL.items():
        v = _edit_signer(edit)
        assert not v["ok"] and v["state"] == "bad_cert", (name, v)


def _tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    print("bastion signed baseline (Go-signed fixture)")
    failed = 0
    tests = _tests()
    import re
    declared = len(re.findall(r"^def (test_\w+)", open(__file__, encoding="utf-8").read(), re.M))
    if declared != len(tests):
        print(f"FAIL  {declared} tests defined, {len(tests)} collected — move the runner to the end")
        return 1
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
