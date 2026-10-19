"""
Identity-scoped credential vault.

Every credential is sealed into an ENVELOPE (crypto.py) owned by exactly one
identity — a host, a user, or the platform (identity.py) — and stored as
ciphertext + public keys only (store.py). Use is authorized by the bridge-signed
permission slip (authz.py): the default is to BROKER the use (mint/refresh/proxy)
so the requester never sees the raw bytes.

Doctrine: host != user is constitutional; the platform stores ciphertext it is
structurally unable to read for user/host-owned secrets (their private keys never
leave their own space).
"""
from server.vault import crypto, identity, store  # noqa: F401
