#!/usr/bin/env python3
"""Test release signing for the cloud box's VM test, with a key made for the
test (never the release key, which stays on the computer that holds it).

  sign_manifest.py newkey <key.pem> <pub.txt>
  sign_manifest.py sign <key.pem> <kind> <manifest.json>
      writes <manifest.json>.<16 hex>.sig beside it and prints its path

The statement is the release-signature scheme the box verifies
(cseries_cloud.statement): privatae-release-signature-v1, kind, version,
sha256 of the manifest's bytes.
"""
import base64
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def newkey(key_path, pub_path):
    k = Ed25519PrivateKey.generate()
    with open(key_path, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with open(pub_path, "w", encoding="ascii") as f:
        f.write(base64.b64encode(raw).decode() + "\n")


def sign(key_path, kind, manifest_path):
    with open(key_path, "rb") as f:
        k = serialization.load_pem_private_key(f.read(), password=None)
    with open(manifest_path, "rb") as f:
        body = f.read()
    version = json.loads(body)["version"]
    sha = hashlib.sha256(body).hexdigest()
    msg = f"privatae-release-signature-v1\n{kind}\n{version}\n{sha}\n".encode()
    out = f"{manifest_path}.{sha[:16]}.sig"
    with open(out, "w", encoding="ascii") as f:
        f.write(base64.b64encode(k.sign(msg)).decode() + "\n")
    print(out)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "newkey":
        newkey(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 5 and sys.argv[1] == "sign":
        sign(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(__doc__)
