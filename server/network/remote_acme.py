"""The Host's own ACME account, for its remote address's certificate.

Remote access gives a member's Host a wildcard certificate for
*.<address_id>.<zone>, issued by Let's Encrypt to an ACME account of the
Host's own: the network pins that account in the zone's CAA record
(accounturi, RFC 8657), so no other account can be issued a certificate for
the Host's names. The member door wants the account's URL before it draws the
address (remote.enable, privatae bridge/member_remote.py), and the certificate
job (sidecars/remote-door/door-cert, the cloud box's own cseries-cert with
lego) is given its acme-dns credential only after that. lego makes an account
only inside `lego run`, which needs the names first, so the account is made
here, once, with one newAccount request (RFC 8555 section 7.3), and kept in the
shape lego 4.29 keeps its own:

    <dir>/accounts/<server host>/<label>/account.json
    <dir>/accounts/<server host>/<label>/keys/<label>.key     (EC P-256, 0600)

door-cert copies them into lego's --path, so `lego run` finds the account and
registers nothing (lego cmd/accounts_storage.go LoadAccount: a registration
with a status is used as it is). <label> is the --email cseries-cert passes,
which lego uses only to name that directory; the account is registered with no
contact, so the CA is given no address of anyone's.

The key is made on this machine and never leaves it: only the account's URL
goes to the network.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

log = logging.getLogger("cseries.remote_acme")

# The cloud box's defaults (vm/cloud/overlay/usr/local/lib/cseries-cloud/
# cseries_cloud.py DEFAULT_ACME_SERVER, DEFAULT_ACME_EMAIL), so the certificate
# job reads the same cloud.json on both.
ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
ACCOUNT_LABEL = "info@privatae.ai"
TIMEOUT_S = 30.0
USER_AGENT = "cseries-remote-access/1"


class AcmeError(Exception):
    """The account could not be made; the message says why, for Settings."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def server_dir(directory_url: str) -> str:
    """lego's folder for a server: the URL's host (with its port), ':' as '_'."""
    return (urlsplit(directory_url).netloc or "").replace(":", "_")


def account_paths(base: Path, directory_url: str, label: str = ACCOUNT_LABEL) -> tuple[Path, Path]:
    user = Path(base) / "accounts" / server_dir(directory_url) / label
    return user / "account.json", user / "keys" / f"{label}.key"


def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _jwk(key: ec.EllipticCurvePrivateKey) -> dict:
    n = key.public_key().public_numbers()
    return {"crv": "P-256", "kty": "EC", "x": _b64(n.x.to_bytes(32, "big")),
            "y": _b64(n.y.to_bytes(32, "big"))}


def _jws(key: ec.EllipticCurvePrivateKey, url: str, nonce: str, payload: dict) -> dict:
    protected = _b64(json.dumps({"alg": "ES256", "jwk": _jwk(key), "nonce": nonce, "url": url},
                                separators=(",", ":"), sort_keys=True).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{protected}.{body}".encode("ascii"),
                                         ec.ECDSA(hashes.SHA256())))
    return {"protected": protected, "payload": body,
            "signature": _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


def stored_account(base: Path, directory_url: str = ACME_DIRECTORY) -> Optional[str]:
    """The account URL kept here, or None when there is none yet."""
    acct, key = account_paths(base, directory_url)
    try:
        doc = json.loads(acct.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reg = doc.get("registration") if isinstance(doc, dict) else None
    uri = reg.get("uri") if isinstance(reg, dict) else None
    if not isinstance(uri, str) or not uri.startswith("https://") or not key.is_file():
        return None
    return uri


def _load_or_make_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
            return key
        log.warning("remote access: the ACME account key here is not P-256; making a new one")
    except FileNotFoundError:
        pass
    except ValueError:
        log.warning("remote access: the ACME account key here does not load; making a new one")
    key = ec.generate_private_key(ec.SECP256R1())
    # lego writes its own account keys as SEC1 ("EC PRIVATE KEY",
    # certcrypto.PEMBlock); its loader takes that form.
    _write(path, key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()), 0o600)
    return key


async def ensure_account(base: Path, *, directory_url: str = ACME_DIRECTORY,
                         session: Optional[aiohttp.ClientSession] = None,
                         allow_http: bool = False) -> str:
    """The URL of this Host's ACME account, made now if there is none.

    The key is made first and kept, so a request whose answer is lost is
    asked again with the same key, and the CA answers with the same account
    (RFC 8555 7.3.1: a newAccount for a known key returns it). Raises
    AcmeError."""
    have = stored_account(base, directory_url)
    if have:
        return have
    scheme = urlsplit(directory_url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise AcmeError("the ACME directory must be an https URL")
    acct_path, key_path = account_paths(base, directory_url)
    key = _load_or_make_key(key_path)
    own = session is None
    http = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                                            headers={"User-Agent": USER_AGENT})
    try:
        try:
            async with http.get(directory_url) as r:
                if r.status != 200:
                    raise AcmeError(f"the ACME directory answered {r.status}")
                directory = await r.json(content_type=None)
            new_nonce, new_account = directory["newNonce"], directory["newAccount"]
            uri, body = None, None
            for _attempt in range(3):
                async with http.head(new_nonce) as r:
                    nonce = r.headers.get("Replay-Nonce")
                if not nonce:
                    raise AcmeError("the ACME server gave no nonce")
                req = _jws(key, new_account, nonce, {"termsOfServiceAgreed": True})
                async with http.post(new_account, data=json.dumps(req),
                                     headers={"Content-Type": "application/jose+json"}) as r:
                    answer = await r.json(content_type=None)
                    if r.status in (200, 201):
                        uri, body = r.headers.get("Location"), answer
                        break
                    kind = answer.get("type", "") if isinstance(answer, dict) else ""
                    if r.status == 400 and kind.endswith(":badNonce"):
                        continue
                    detail = answer.get("detail") if isinstance(answer, dict) else None
                    raise AcmeError(f"the ACME server refused the account ({r.status}"
                                    + (f": {detail}" if detail else "") + ")")
        except (aiohttp.ClientError, OSError, ValueError, KeyError, TypeError) as exc:
            raise AcmeError(f"could not reach the ACME server: {exc.__class__.__name__}") from None
    finally:
        if own:
            await http.close()
    if not isinstance(uri, str) or not uri.startswith(("https://", "http://" if allow_http else "https://")):
        raise AcmeError("the ACME server gave no account URL")
    body = body if isinstance(body, dict) else {}
    body.setdefault("status", "valid")
    doc = {"email": ACCOUNT_LABEL, "registration": {"body": body, "uri": uri}}
    _write(acct_path, (json.dumps(doc, indent="\t") + "\n").encode(), 0o600)
    log.info("remote access: this Host's ACME account is %s", uri)
    return uri
