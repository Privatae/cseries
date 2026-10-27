"""Pins server/network/remote_protocol.py's tickets to the pinned vector,
vm/cloud/test/remote_ticket_vector.json (a copy of privatae
bridge/tests/fixtures/remote_ticket_vector.json, which pins the network's
member door to the same bytes):

  * sign_ticket over the vector's payload with its seed is the vector's
    ticket, byte for byte;
  * the seed's public key is the vector's public_pem;
  * verify_ticket with that key, at verify_at, gives back the payload, and
    ticket_matches holds for cert_serial_int (whose hex, unpadded, is the
    payload's serial; door_serial_hex is the same serial padded to an even
    length).

The VM test's stand-in member door makes the first check at its start too,
but only a full VM run reaches it; this runs on every change to either file.
No network, no container.
"""

import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server.network import remote_protocol as rp  # noqa: E402

VECTOR = os.path.join(ROOT, "vm/cloud/test/remote_ticket_vector.json")
fails = 0


def check(ok, what, detail=None):
    global fails
    if ok:
        print(f"ok   {what}")
    else:
        fails += 1
        print(f"FAIL {what}" + (f": {detail!r}" if detail is not None else ""))


with open(VECTOR) as f:
    v = json.load(f)
key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v["ed25519_seed_hex"]))
pub = key.public_key()

made = rp.sign_ticket(key, v["payload"])
check(made == v["ticket"], "sign_ticket makes the vector's ticket, byte for byte", made)
pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
check(pem == v["public_pem"], "the seed's public key is the vector's public_pem", pem)
try:
    got = rp.verify_ticket(v["ticket"], {v["kid"]: pub}, now=v["verify_at"])
except rp.TicketError as exc:
    got = None
    check(False, "verify_ticket takes the vector's ticket at verify_at", str(exc))
if got is not None:
    check({k: got[k] for k in v["payload"] if k != "limits"} == {k: x for k, x in v["payload"].items() if k != "limits"},
          "verify_ticket takes the vector's ticket at verify_at and gives back its payload", got)
    check(rp.ticket_matches(got, v["cert_serial_int"]), "ticket_matches holds for cert_serial_int",
          (got.get("serial"), v["cert_serial_int"]))
check(int(v["door_serial_hex"], 16) == v["cert_serial_int"] and len(v["door_serial_hex"]) % 2 == 0,
      "door_serial_hex is cert_serial_int, padded to an even length", v["door_serial_hex"])

print(f"{fails} failed")
sys.exit(1 if fails else 0)
