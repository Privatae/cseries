#!/usr/bin/env python3
"""The cloud box's heartbeat (vm/cloud/overlay/usr/local/sbin/
cseries-cloud-heartbeat) and its signature.

  1. The signed statement is privatae-machine-heartbeat-v1, machine_id, ts and
     the sha256 of the exact body bytes posted, and the X-Machine-Signature a
     request carries verifies against the box's public key over exactly that.
     A changed body, machine id or ts does not verify.
  2. The key is made once (Ed25519, PKCS#8, mode 0600) and read back the same;
     its public half is base64 of the raw 32 bytes, as the provisioner records.
  3. The body has every field the platform reads, under those names; ts is
     RFC 3339 UTC to the second, and a value the box cannot read is null
     rather than a guess or an exception.
  4. A post that cannot be delivered changes nothing and exits 0.
  5. The answer: a JSON object of at most 1 MiB is read, anything else (or
     one cut off) is no news; a 2xx without access_log changes nothing.

Container-free: python3 scripts/test_cloud_box_heartbeat.py (needs cryptography).
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OVERLAY = os.path.join(ROOT, "vm/cloud/overlay")
sys.path.insert(0, os.path.join(OVERLAY, "usr/local/lib/cseries-cloud"))
import cseries_cloud as cc  # noqa: E402
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

loader = SourceFileLoader("cloud_heartbeat", os.path.join(OVERLAY, "usr/local/sbin/cseries-cloud-heartbeat"))
spec = importlib.util.spec_from_loader("cloud_heartbeat", loader)
hb = importlib.util.module_from_spec(spec)
loader.exec_module(hb)

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def verifies(pub_b64, sig_b64, machine_id, ts, body):
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    msg = (f"privatae-machine-heartbeat-v1\n{machine_id}\n{ts}\n"
           f"{hashlib.sha256(body).hexdigest()}\n").encode()
    try:
        pub.verify(base64.b64decode(sig_b64), msg)
        return True
    except InvalidSignature:
        return False


MID = "0b9e5f4c-3a21-4d6e-9f87-1c2d3e4f5a6b"

with tempfile.TemporaryDirectory() as tmp:
    paths = cc.Paths(tmp)
    kp = paths.heartbeat_key()
    key = cc.load_or_create_key(kp)
    check(stat.S_IMODE(os.stat(kp).st_mode) == 0o600, "the heartbeat key is written 0600")
    with open(kp, "rb") as f:
        check(f.read().startswith(b"-----BEGIN PRIVATE KEY-----"), "the key is PKCS#8 PEM")
    again = cc.load_or_create_key(kp)
    pub = cc.public_key_b64(key)
    check(cc.public_key_b64(again) == pub, "a second load reads the same key, it does not make a new one")
    check(len(base64.b64decode(pub)) == 32, "the public key is base64 of the raw 32 bytes")

    check(cc.heartbeat_statement(MID, "2026-01-02T03:04:05Z", b"{}") ==
          ("privatae-machine-heartbeat-v1\n" + MID + "\n2026-01-02T03:04:05Z\n" +
           hashlib.sha256(b"{}").hexdigest() + "\n").encode(),
          "the statement is the four lines the receiver rebuilds")

    # collect() on a root where nothing the box reports exists yet. No
    # podman, apt or Host here: every command answers nothing and the Host's
    # health probe answers down.
    hb._quiet = lambda cmd, timeout=20: None
    hb.host_health = lambda: "down"
    cfg = {"machine_id": MID, "heartbeat_url": "https://127.0.0.1:9/never"}
    body = hb.collect(paths, cfg, now=1767323045)
    want = {"machine_id", "ts", "version", "host", "disk", "cert", "backup", "patches", "uptime_s",
            "vault", "access_log"}
    check(set(body) == want, "the body has exactly the fields the platform reads", sorted(body))
    check(body["vault"] == {"state": "absent", "since": None},
          "a machine without a vault says so: vault.state absent, since null", body["vault"])
    check(body["access_log"] == {"count": 0, "head": "0" * 64, "mismatch": False},
          "an empty access log: count 0, the head 64 zeros, no mismatch", body["access_log"])

    # A vault that is locked: nothing of the Host runs, and podman is never
    # asked (its storage is in the vault; asking would make an empty one on
    # the plain disk).
    asked = []
    real_quiet = hb._quiet
    hb._quiet = lambda cmd, timeout=20: asked.append(cmd)
    vroot = os.path.join(tmp, "locked-box")
    os.makedirs(os.path.join(vroot, "etc/cseries-cloud"))
    with open(os.path.join(vroot, "etc/cseries-cloud/vault.json"), "w") as f:
        json.dump({"version": 1, "machine_id": MID, "salt": "x"}, f)
    lbody = hb.collect(cc.Paths(vroot), cfg, now=1767323045)
    hb._quiet = real_quiet
    check(lbody["vault"]["state"] == "locked" and lbody["host"] == {"health": "down", "deep_status": None}
          and lbody["disk"]["state_free_bytes"] is None and lbody["backup"] == {"last_ok_at": None, "paused": None},
          "a locked vault: vault.state locked, the Host down, nothing of its read", lbody)
    check(not any("podman" in " ".join(c) for c in asked if isinstance(c, list)),
          "and podman is never asked while the vault is locked", asked)
    check(set(body["host"]) == {"health", "deep_status"} and set(body["disk"]) == {"state_free_bytes", "root_free_bytes"}
          and set(body["cert"]) == {"not_after"} and set(body["backup"]) == {"last_ok_at", "paused"}
          and set(body["patches"]) == {"pending_security", "reboot_required", "last_upgrade_at"},
          "and every nested field, under its name", body)
    check(body["ts"] == "2026-01-02T03:04:05Z" and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", body["ts"]),
          "ts is RFC 3339 UTC to the second", body["ts"])
    check(body["version"] is None and body["host"]["deep_status"] is None and body["cert"]["not_after"] is None
          and body["backup"]["last_ok_at"] is None and body["backup"]["paused"] is None
          and body["disk"]["state_free_bytes"] is None
          and body["patches"]["pending_security"] is None and body["patches"]["last_upgrade_at"] is None,
          "what cannot be read is null", body)
    check(body["host"]["health"] == "down" and body["patches"]["reboot_required"] is False
          and isinstance(body["disk"]["root_free_bytes"], int),
          "what can be read is read: health, reboot flag, root free space", body)

    # A certificate and an update state where the box keeps them.
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.demo.cloud.example.test")])
    nb = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key())
         .serial_number(7).not_valid_before(nb).not_valid_after(nb + datetime.timedelta(days=90))
         .sign(k, hashes.SHA256()))
    os.makedirs(paths.tls)
    with open(os.path.join(paths.tls, "cert.pem"), "wb") as f:
        f.write(c.public_bytes(serialization.Encoding.PEM))
    cc.write_json(paths.update_state(), {"version": "0.4.0"})
    os.makedirs(paths.p("run"))
    open(paths.p("run/reboot-required"), "w").close()
    body = hb.collect(paths, cfg, now=1767323045)
    check(body["cert"]["not_after"] == "2026-04-01T00:00:00Z", "cert.not_after is the served certificate's",
          body["cert"])
    check(body["version"] == "0.4.0" and body["patches"]["reboot_required"] is True,
          "version is the installed cloud release; a pending reboot is reported", body)

    # backup.last_ok_at comes from the Host's database, which the Host can
    # shape: only the query's own RFC 3339 form is reported, anything else is
    # null rather than text the box would sign.
    for said, want in [("2026-01-01T02:03:04Z", "2026-01-01T02:03:04Z"), ("x" * 100000, None),
                       ("2026-01-01T02:03:04Z\nmore", None), ("yesterday", None)]:
        hb._quiet = lambda cmd, timeout=20, said=said: said if "psql" in cmd else None
        got = hb.collect(paths, cfg, now=1767323045)["backup"]["last_ok_at"]
        check(got == want, f"backup.last_ok_at for a database answer of {said[:24]!r}... is {want!r}",
              repr(got)[:60])
    # backup.paused: the query's own two answers, anything else null
    # (scripts/test_cloud_box_heartbeat_pg.py runs the query on the real schema).
    for said, want in [("t", True), ("f", False), ("true", None), ("t\nf", None), ("", None)]:
        hb._quiet = (lambda cmd, timeout=20, said=said:
                     said if "psql" in cmd and cmd[-1] == hb.BACKUP_PAUSED_SQL else None)
        got = hb.collect(paths, cfg, now=1767323045)["backup"]
        check(got["paused"] is want and got["last_ok_at"] is None,
              f"backup.paused for a database answer of {said!r} is {want!r}", got)
    hb._quiet = lambda cmd, timeout=20: None

    raw = hb.encode(body)
    check(raw == json.dumps(body, sort_keys=True, separators=(",", ":")).encode() and b" " not in raw[:50],
          "the body is compact JSON with sorted keys")
    req = hb.signed_request("https://example.test/api/machines/heartbeat", key, body)
    sig = req.get_header("X-machine-signature")
    check(req.get_method() == "POST" and req.data == raw and req.get_header("Content-type") == "application/json",
          "the request posts exactly those bytes as JSON")
    check(verifies(pub, sig, MID, body["ts"], raw), "X-Machine-Signature verifies against the box's key")
    check(not verifies(pub, sig, MID, body["ts"], raw.replace(b'"0.4.0"', b'"0.4.1"')),
          "a changed body does not verify")
    check(not verifies(pub, sig, "0b9e5f4c-3a21-4d6e-9f87-000000000000", body["ts"], raw),
          "another machine_id does not verify")
    check(not verifies(pub, sig, MID, "2026-01-02T03:04:06Z", raw), "another ts does not verify")

    # Undeliverable: the post fails, nothing on the box changes, exit 0.
    cc.write_json(paths.cloud_json(), cfg)
    before = sorted(os.listdir(paths.state))
    hb.cc.Paths = lambda root="/": paths
    rc = hb.main([])
    check(rc == 0 and sorted(os.listdir(paths.state)) == before,
          "a post that cannot be delivered exits 0 and writes nothing", (rc, before))

# 5. The platform's 2xx answer: only a JSON object of at most
# 1 MiB is read; anything else, and an answer cut off, is no news, and the
# box's copy of its access log stays as it was.
import http.client  # noqa: E402
import io  # noqa: E402


class Answer(io.BytesIO):
    pass


class CutOff:
    def read(self, n=-1):
        raise http.client.IncompleteRead(b'{"ok": tr', 100)


check(hb.read_answer(Answer(b'{"ok": true}')) == {"ok": True}, "a JSON object is read")
big = b'{"ok": true, "pad": "' + b"x" * (hb.MAX_ANSWER) + b'"}'
check(hb.read_answer(Answer(big)) is None, "an answer over 1 MiB is not read")
check(hb.read_answer(Answer(b"[1]")) is None and hb.read_answer(Answer(b"not json")) is None
      and hb.read_answer(Answer(b"\xff\xfe")) is None, "nor one that is not a JSON object")
check(hb.read_answer(CutOff()) is None, "an answer cut off is no news, not an exception")
with tempfile.TemporaryDirectory() as tmp:
    paths = cc.Paths(tmp)
    cfg = {"machine_id": MID, "name": "orchard", "address_id": "k7x2m9", "zone": "cloud.privatae.ai"}
    for answer in (None, {"ok": True}):
        hb.take_answer(paths, cfg, answer, False)
        st = hb.cm.access_log(paths, cfg).status()
        check(st == {"count": 0, "head": "0" * 64, "mismatch": False} and
              not os.path.exists(paths.p(hb.cm.ACCESS_STATE)),
              f"an answer {answer!r} (none, or 2xx without access_log) changes nothing", st)

sys.exit(failed)
