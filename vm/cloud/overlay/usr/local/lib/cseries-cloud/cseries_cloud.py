"""Shared code for the C-Series Cloud box: its settings, the signed release
manifest, the overlay, and the heartbeat's signature.

Used by cseries-cloud-bootstrap, cseries-cloud-update, cseries-cloud-heartbeat
and cseries-cert (all in /usr/local/sbin). Python 3 standard library plus
python3-cryptography, both from Debian.

Every path is under a root (Paths), "/" on the box, so the tests can run any of
this in a temporary directory.
"""

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

# ── the release signature ───────────────────────────────────────────────────
#
# The scheme packages/cseries/src/verify.js uses for the bastion's manifest,
# under its own kind: beside <manifest> sits <manifest>.<first 16 hex of its
# sha256>.sig, the base64 Ed25519 signature over
#
#   privatae-release-signature-v1
#   <kind>
#   <version, as the manifest says>
#   <sha256 of the manifest's exact bytes, hex>
#
# and it must verify against one of the pinned keys. The keys are the
# bastion's release keys (the same two verify.js pins), so a release is signed
# on the computer that holds that key and control of the code host alone
# cannot move a box to anything.

KIND = "cseries-cloud"
BASTION_KIND = "bastion"
RELEASE_KEYS = (
    "tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=",  # primary
    "CKL+J0VSdqrfY4AmxChvylJ1ha4e8GvcrYF2o17Ah+g=",  # spare, kept offline
)
STATEMENT_PREFIX = "privatae-release-signature-v1"

BASTION_MANIFEST_URL = "https://privatae.ai/bastion/version.json"
DEFAULT_RELEASE_BASE = "https://github.com/Privatae/cseries/releases/latest/download"
MANIFEST_NAME = "cloud-release.json"
DEFAULT_ZONE = "cloud.privatae.ai"
DEFAULT_HEARTBEAT_URL = "https://api.privatae.ai/api/machines/heartbeat"
DEFAULT_ACME_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
DEFAULT_ACME_EMAIL = "info@privatae.ai"
DEFAULT_REBOOT_WINDOW = "04:00"
# The first bastion release that reads the pairing token from `pair -`'s
# standard input (0.53.2), so the token is in no process's command line (any
# account on the box can read those) and in no log line; it also carries
# `switch` (0.53.1 was the first), which the box needs to turn families off.
# The same floor as the cseries command's (packages/cseries/release.json).
BASTION_MIN_VERSION = "0.53.2"

MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIG_BYTES = 4096
MAX_OVERLAY_BYTES = 64 * 1024 * 1024
MAX_BASTION_BYTES = 256 * 1024 * 1024

# The containers' network, fixed so the Host's address and the address it
# sees Caddy connect from are known (cseries.network.d/cloud.conf sets the
# same values; door.nft names them too).
NET_SUBNET = "10.89.77.0/24"
NET_GATEWAY = "10.89.77.1"
HOST_IP = "10.89.77.2"
HOST_PORT = 8800
# The address uvicorn in the Host container sees for Caddy's connections:
# Caddy runs on the box and connects to HOST_IP, so the packets come from
# the box's own address on the containers' bridge, the gateway.
TRUSTED_PROXY = NET_GATEWAY

HOST_UID = 1000

# ── names ───────────────────────────────────────────────────────────────────

NAME_RE = re.compile(r"^[a-z][a-z0-9]{2,19}$")
HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
# The machine's address ID (machines.address_id): six characters the
# provisioner generates once per machine and never reuses or changes, so two
# machines whose buyers chose the same name still have different addresses.
# The alphabet leaves out l, o, 0 and 1, so it can be read aloud and typed; the
# first character is a letter and at least one is a digit, so it is never a word.
ADDRESS_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
ADDRESS_ID_RE = re.compile(r"^[a-km-np-z][a-km-np-z2-9]{5}$")
_ADDRESS_ID_DIGIT_RE = re.compile(r"[2-9]")
RESERVED_NAMES = frozenset({
    "acme", "api", "app", "apps", "auth", "admin", "cloud", "dl", "git", "host", "mail",
    "net", "enrol", "ns", "ns1", "ns2", "root", "status", "support", "test", "www",
})
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
SETUP_CODE_RE = re.compile(r"^[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6}$")
REBOOT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# An image name with an optional tag and no digest, as release.js takes it,
# in the characters cseries-guest's valid_ref accepts.
IMAGE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?$")


class CloudError(Exception):
    """A refusal with a sentence the log and the bootstrap result carry."""


def validate_name(name):
    # fullmatch: re's $ also matches before a final newline, and the name is
    # written into the Caddyfile and the Host's unit.
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise CloudError("name must be 3 to 20 characters, a lowercase letter first, then "
                         "lowercase letters and digits")
    if HEX16_RE.match(name):
        raise CloudError("a name of 16 hex characters is the form of a network Host's handle")
    if name in RESERVED_NAMES or name.startswith("apps"):
        raise CloudError(f"the name {name!r} is reserved")
    return name


def validate_address_id(value):
    if not isinstance(value, str) or not ADDRESS_ID_RE.fullmatch(value) or \
            not _ADDRESS_ID_DIGIT_RE.search(value):
        raise CloudError("address_id must be 6 characters of a-z and 2-9 without l, o, 0 or 1, "
                         "a letter first and at least one digit")
    return value


def validate_dns_name(value, what):
    if not isinstance(value, str) or len(value) > 253:
        raise CloudError(f"{what} is not a DNS name")
    labels = value.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(lab) for lab in labels):
        raise CloudError(f"{what} is not a DNS name: {value!r}")
    return value


def validate_https_url(value, what):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise CloudError(f"{what} must be an https URL")
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or not u.hostname:
        raise CloudError(f"{what} must be an https URL, not {value!r}")
    return value


def host_name(cfg):
    """The Host's own address, <name>.<address_id>.<zone>: the buyer's name is
    the Host's handle (the first label of CSERIES_URL), the machine's zone
    (zone_host) the rest."""
    return f"{cfg['name']}.{zone_host(cfg)}"


def zone_host(cfg):
    """The machine's zone, <address_id>.<zone>: the base of its wildcard
    certificate and its acme-dns registration. The Host and each of its
    isolated apps (apps-<key>-<name>.<address_id>.<zone>) live one label
    under it. A settings file without a valid address_id is refused here, so
    no template is rendered with the address left out."""
    return f"{validate_address_id(cfg.get('address_id'))}.{cfg['zone']}"


# ── bootstrap.json ──────────────────────────────────────────────────────────

BOOTSTRAP_KEYS = frozenset({
    "name", "address_id", "zone", "machine_id", "acme_dns", "acme_email", "acme_server", "heartbeat_url",
    "release_base", "images", "image_archive", "overlay_archive", "bastion_binary",
    "reboot_window", "setup_code",
})


def _image_pair(images, what):
    if not isinstance(images, dict) or set(images) != {"host", "postgres"}:
        raise CloudError(f"{what} must name exactly host and postgres")
    for which in ("host", "postgres"):
        img = images[which]
        if not isinstance(img, dict):
            raise CloudError(f"{what}.{which} must be an object")
        if not isinstance(img.get("ref"), str) or not IMAGE_REF_RE.match(img["ref"]):
            raise CloudError(f"{what}.{which}.ref must be an image name without a digest")
        if not isinstance(img.get("digest"), str) or not DIGEST_RE.match(img["digest"]):
            raise CloudError(f"{what}.{which}.digest must be sha256:<64 hex>")


def validate_bootstrap(raw):
    """The provisioner's bootstrap.json, checked and with every default
    filled. Raises CloudError naming the first problem."""
    if not isinstance(raw, dict):
        raise CloudError("bootstrap.json must hold one JSON object")
    unknown = sorted(set(raw) - BOOTSTRAP_KEYS)
    if unknown:
        raise CloudError(f"bootstrap.json has keys this bootstrap does not know: {', '.join(unknown)}")
    cfg = {}
    cfg["name"] = validate_name(raw.get("name"))
    cfg["address_id"] = validate_address_id(raw.get("address_id"))
    cfg["zone"] = validate_dns_name(raw.get("zone", DEFAULT_ZONE), "zone")
    mid = raw.get("machine_id")
    try:
        if not isinstance(mid, str) or str(uuid.UUID(mid)) != mid.lower():
            raise ValueError
    except ValueError:
        raise CloudError("machine_id must be a UUID") from None
    cfg["machine_id"] = mid.lower()

    acme = raw.get("acme_dns")
    if acme is not None:
        if not isinstance(acme, dict) or set(acme) != {"api_base", "credential"}:
            raise CloudError("acme_dns must hold exactly api_base and credential")
        validate_https_url(acme["api_base"], "acme_dns.api_base")
        cred = acme["credential"]
        if not isinstance(cred, dict):
            raise CloudError("acme_dns.credential must be an object")
        for k in ("username", "password", "subdomain"):
            if not isinstance(cred.get(k), str) or not cred[k] or any(c.isspace() for c in cred[k]):
                raise CloudError(f"acme_dns.credential.{k} must be a non-empty string")
        validate_dns_name(cred.get("fulldomain"), "acme_dns.credential.fulldomain")
        allow = cred.get("allowfrom", [])
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            raise CloudError("acme_dns.credential.allowfrom must be a list of networks")
        for a in allow:
            try:
                ipaddress.ip_network(a, strict=False)
            except ValueError:
                raise CloudError(f"acme_dns.credential.allowfrom holds {a!r}, not a network") from None
        cfg["acme_dns"] = {"api_base": acme["api_base"], "credential": dict(cred)}
    else:
        cfg["acme_dns"] = None
    email = raw.get("acme_email", DEFAULT_ACME_EMAIL)
    if not isinstance(email, str) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise CloudError("acme_email must be an email address")
    cfg["acme_email"] = email
    cfg["acme_server"] = validate_https_url(raw.get("acme_server", DEFAULT_ACME_SERVER), "acme_server")
    cfg["heartbeat_url"] = validate_https_url(raw.get("heartbeat_url", DEFAULT_HEARTBEAT_URL),
                                              "heartbeat_url")
    cfg["release_base"] = validate_https_url(raw.get("release_base", DEFAULT_RELEASE_BASE),
                                             "release_base").rstrip("/")

    if "images" in raw and "image_archive" in raw:
        raise CloudError("bootstrap.json names images and image_archive; it takes one")
    cfg["images"] = None
    cfg["image_archive"] = None
    if "images" in raw:
        _image_pair(raw["images"], "images")
        cfg["images"] = raw["images"]
    if "image_archive" in raw:
        a = raw["image_archive"]
        if not isinstance(a, dict) or set(a) != {"path", "host", "postgres"}:
            raise CloudError("image_archive must hold exactly path, host and postgres")
        for k in ("path", "host", "postgres"):
            if not isinstance(a[k], str) or not a[k]:
                raise CloudError(f"image_archive.{k} must be a non-empty string")
        if not os.path.isabs(a["path"]):
            raise CloudError("image_archive.path must be an absolute path on this machine")
        for k in ("host", "postgres"):
            if not IMAGE_REF_RE.match(a[k]):
                raise CloudError(f"image_archive.{k} must be the image's name in the archive")
        cfg["image_archive"] = dict(a)
    for k in ("overlay_archive", "bastion_binary"):
        v = raw.get(k)
        if v is not None and (not isinstance(v, str) or not os.path.isabs(v)):
            raise CloudError(f"{k} must be an absolute path on this machine")
        cfg[k] = v
    rw = raw.get("reboot_window", DEFAULT_REBOOT_WINDOW)
    if not isinstance(rw, str) or not REBOOT_RE.match(rw):
        raise CloudError("reboot_window must be HH:MM, in UTC")
    cfg["reboot_window"] = rw
    code = raw.get("setup_code")
    if code is not None and (not isinstance(code, str) or not SETUP_CODE_RE.match(code)):
        raise CloudError("setup_code must be three groups of 6 lowercase hex characters")
    cfg["setup_code"] = code
    return cfg


# What the box keeps of bootstrap.json after bootstrap, in cloud.json: no
# acme-dns password (that is in acme-dns.json, 0600) and no setup code (that is
# in setup-code, 0640 root:cseries).
CLOUD_JSON_KEYS = ("name", "address_id", "zone", "machine_id", "acme_email", "acme_server",
                   "heartbeat_url", "release_base", "reboot_window")


def cloud_json(cfg):
    return {k: cfg[k] for k in CLOUD_JSON_KEYS}


# ── paths ───────────────────────────────────────────────────────────────────

class Paths:
    def __init__(self, root="/"):
        self.root = root

    def p(self, *parts):
        return os.path.join(self.root, *[x.lstrip("/") for x in parts])

    @property
    def etc(self):
        return self.p("etc/cseries-cloud")

    @property
    def state(self):
        return self.p("var/lib/cseries-cloud")

    @property
    def units(self):
        return self.p("etc/containers/systemd")

    @property
    def tls(self):
        return self.p("etc/caddy/tls")

    def cloud_json(self):
        return os.path.join(self.etc, "cloud.json")

    def bootstrap_json(self):
        return os.path.join(self.etc, "bootstrap.json")

    def acme_dns_json(self):
        return os.path.join(self.etc, "acme-dns.json")

    def setup_code(self):
        return os.path.join(self.etc, "setup-code")

    def door_secret(self):
        return os.path.join(self.etc, "door-secret")

    def door_secret_env(self):
        return os.path.join(self.etc, "door-secret.env")

    def update_state(self):
        return os.path.join(self.state, "update.json")

    def heartbeat_key(self):
        return os.path.join(self.state, "heartbeat.key")

    def overlay_dir(self, version):
        return os.path.join(self.state, "overlay", version)

    def image_conf(self, which):
        return os.path.join(self.units, f"cseries-{which}.container.d", "image.conf")

    def release_file(self):
        return self.p("etc/cseries/release")

    def runtime_images(self):
        return self.p("etc/cseries-runtime/images.json")

    def bastion_bin(self):
        return self.p("usr/local/bin/privatae-bastion")


def load_cloud(paths):
    with open(paths.cloud_json(), encoding="utf-8") as f:
        return json.load(f)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def atomic_write(path, data, mode=0o644, uid=None, gid=None):
    """Write data (bytes or str) to path through a temporary file in the same
    directory and a rename, so a reader sees the old file or the new one."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if uid is not None or gid is not None:
            os.chown(tmp, -1 if uid is None else uid, -1 if gid is None else gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path, obj, mode=0o644, uid=None, gid=None):
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode, uid, gid)


# ── the door key ────────────────────────────────────────────────────────────
#
# Caddy asks the Host about every request at GET /__host/door (forward_auth),
# and the Host answers only a question that carries the door key, once, in
# X-Cseries-Door (the Host's CSERIES_DOOR_SECRET_FILE). Caddy sets that header
# on its own question and strips it from everything it passes on, so no
# client can send it through. The box keeps the key in two files, written
# here and nowhere else (no overlay file names either, so an update never
# touches them):
#
#   door-secret      the key alone, 0640 root:cseries, mounted read-only into
#                    the Host container (cseries-host.container.d/cloud.conf)
#   door-secret.env  CSERIES_DOOR_SECRET=<key>, 0600 root: Caddy's
#                    EnvironmentFile (caddy.service.d/cloud.conf), which
#                    systemd reads as root before each start and each reload
#
# The Host container has door-secret as a single-file bind mount, and such a
# mount keeps the inode it was made with: a file renamed over the path is
# never seen inside, while bytes written into the same inode are (both
# measured with a container on 2026-10-19). So door-secret is created once
# and from then on only ever rewritten in place.

DOOR_KEY_ENV = "CSERIES_DOOR_SECRET"
# What the Host accepts as a key (owner_session._DOOR_KEY_RE), after
# whitespace is stripped.
DOOR_KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{32,512}$")


def new_door_key():
    """32 random bytes in hex, 64 characters."""
    return secrets.token_hex(32)


def read_door_key(path):
    """The key in path, or None when the file is missing or does not hold
    one the Host would accept."""
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            key = f.read(1024).strip()
    except FileNotFoundError:
        return None
    return key if DOOR_KEY_RE.match(key) else None


def write_in_place(path, data, mode, uid, gid):
    """Write data into the file at path without replacing its inode
    (creating it when there is none), with exactly that mode and owner.
    The modification time is set again once every byte is in, past the one
    the write itself left: the Host reads the key again when the file's
    mtime or size changes, so even a read that met the file half written is
    followed by one of the whole key."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.pwrite(fd, data, 0)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
        ns = max(time.time_ns(), os.fstat(fd).st_mtime_ns + 1)
        os.utime(fd, ns=(ns, ns))
    finally:
        os.close(fd)


def _owned(path, mode, uid, gid):
    st = os.stat(path)
    return (st.st_mode & 0o7777) == mode and st.st_uid == uid and st.st_gid == gid


def ensure_door_key(paths, gid, uid=0, root_gid=0, make=new_door_key):
    """The box's door key: the one door-secret holds, or a new one when it
    holds none. A key already in place is kept (a bootstrap run again, an
    update and the seal never change it). Each file that is missing, holds
    something else or has another owner or mode is put right: door-secret
    uid:gid 0640, door-secret.env uid:root_gid 0600 (root and root on the
    box; a test passes its own). Returns (key, made), made True when this
    call generated the key."""
    secret, env = paths.door_secret(), paths.door_secret_env()
    os.makedirs(paths.etc, exist_ok=True)
    key = read_door_key(secret)
    made = key is None
    if made:
        key = make()
        if not DOOR_KEY_RE.match(key):
            raise CloudError("a new door key is not of the form the Host accepts")
        write_in_place(secret, key + "\n", 0o640, uid, gid)
    elif not _owned(secret, 0o640, uid, gid):
        os.chown(secret, uid, gid)
        os.chmod(secret, 0o640)
    _write_door_env(env, key, uid, root_gid)
    return key, made


def rotate_door_key(paths, gid, uid=0, root_gid=0, make=new_door_key):
    """Replace the door key in both files: door-secret in place, so the
    running Host reads the new one, then Caddy's file. Caddy takes it at
    its next reload (cseries-cloud-bootstrap rotate-door-key reloads it).
    Returns the new key."""
    key = make()
    if not DOOR_KEY_RE.match(key):
        raise CloudError("a new door key is not of the form the Host accepts")
    os.makedirs(paths.etc, exist_ok=True)
    write_in_place(paths.door_secret(), key + "\n", 0o640, uid, gid)
    _write_door_env(paths.door_secret_env(), key, uid, root_gid)
    return key


def _write_door_env(env, key, uid, gid):
    """Caddy's file, by rename: systemd opens it by path at each start and
    reload, so no inode needs keeping."""
    want = f"{DOOR_KEY_ENV}={key}\n"
    try:
        with open(env, encoding="ascii", errors="replace") as f:
            same = f.read() == want and _owned(env, 0o600, uid, gid)
    except FileNotFoundError:
        same = False
    if not same:
        atomic_write(env, want, 0o600, uid, gid)


# ── manifest ────────────────────────────────────────────────────────────────

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def signature_url(manifest_url, body):
    u = urllib.parse.urlsplit(manifest_url)
    return urllib.parse.urlunsplit(u._replace(path=f"{u.path}.{sha256_hex(body)[:16]}.sig"))


def statement(kind, version, sha256hex):
    return f"{STATEMENT_PREFIX}\n{kind}\n{version}\n{sha256hex}\n".encode("utf-8")


def _b64(text):
    try:
        s = "".join(str(text).split())
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (ValueError, TypeError):
        return None


def verify_signed_manifest(kind, body, sig_text, keys=RELEASE_KEYS):
    """The parsed manifest when sig_text is a valid signature over body for
    kind; raises CloudError otherwise. Never falls back to an unsigned
    manifest. The same checks, in the same order, as verify.js."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not keys:
        raise CloudError("no release key is pinned, so no manifest can be verified")
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CloudError("the release manifest is not valid JSON") from None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not version or "\r" in version or "\n" in version:
        raise CloudError("the release manifest names no usable version")
    sig = _b64(sig_text)
    if sig is None or len(sig) != 64:
        raise CloudError("the release signature is malformed")
    msg = statement(kind, version, sha256_hex(body))
    for k in keys:
        raw = _b64(k)
        if raw is None or len(raw) != 32:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, msg)
            return manifest
        except InvalidSignature:
            continue
    raise CloudError("the release signature does not verify against any pinned release key")


def parse_version(v):
    """(major, minor, patch) of X.Y.Z, or of the X.Y.Z a version starts with
    (a build such as 0.53.0-rc.1). None when there is none."""
    m = re.match(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


RUNTIME_FAMILIES = ("python", "creative", "helper", "browser")


def _validate_runtime(rt):
    """The manifest's optional runtime section: the local runtime's images by
    family, each {ref, digest}, and the coding engines' sources {url, sha256}."""
    if not isinstance(rt, dict):
        raise CloudError("runtime must be an object")
    unknown = sorted(set(rt) - set(RUNTIME_FAMILIES) - {"harness_src"})
    if unknown:
        raise CloudError(f"runtime names what this box does not know: {', '.join(unknown)}")
    for fam in RUNTIME_FAMILIES:
        if fam not in rt:
            continue
        img = rt[fam]
        if not isinstance(img, dict) or not isinstance(img.get("ref"), str) or \
                not IMAGE_REF_RE.match(img["ref"]):
            raise CloudError(f"runtime.{fam}.ref must be an image name without a digest")
        if not isinstance(img.get("digest"), str) or not DIGEST_RE.match(img["digest"]):
            raise CloudError(f"runtime.{fam}.digest must be sha256:<64 hex>")
    hs = rt.get("harness_src")
    if hs is not None:
        if not isinstance(hs, dict) or not isinstance(hs.get("url"), str) or \
                not re.match(r"^https://[A-Za-z0-9._~:/%+-]+$", hs["url"]):
            raise CloudError("runtime.harness_src.url must be an https URL")
        if not isinstance(hs.get("sha256"), str) or not SHA256_RE.match(hs["sha256"]):
            raise CloudError("runtime.harness_src.sha256 must be 64 hex characters")


def runtime_lines(manifest):
    """What cseries-guest configure takes for the manifest's runtime section
    (python=<ref>@<digest> ..., harness_src_url=, harness_src_sha256=), or
    "" when it has none."""
    rt = (manifest or {}).get("runtime") or {}
    lines = [f"{fam}={pinned(rt[fam])}" for fam in RUNTIME_FAMILIES if fam in rt]
    hs = rt.get("harness_src")
    if hs:
        lines += [f"harness_src_url={hs['url']}", f"harness_src_sha256={hs['sha256']}"]
    return "".join(line + "\n" for line in lines)


# ── the Host's memory ceiling ───────────────────────────────────────────────
#
# A cloud box runs the local runtime, so its Host and the runtime's
# containers share one budget: the runtime admits containers only into what
# the system's reserve, the database's ceiling and the Host's ceiling leave
# (cseries_runtime/guard.py, held_mb). The Host's ceiling is the one the
# cseries command gives a guest with the runtime (packages/cseries/src/
# host.js memoryMax, runtime: true; the reasons for each number are there),
# computed the same way so the two always agree
# (scripts/test_runtime_guard.py): the room is the box's memory less GKE's
# node reserve and the database's 512 MiB (cseries-postgres.container), and
# the Host gets 1 GiB (what the busiest live Host was measured to use, with
# room to spare), or all of the room when that is less, or a quarter of it
# when that is more; never under 512 MiB nor over 2 GiB. It goes
# to cseries-guest configure as memory_max=, which writes it to
# cseries-host.container.d/10-memory.conf, as on a person's machine; without
# it the base unit's 70% would stand and leave the containers nothing.
_MIB = 1024 ** 2
_GIB = 1024 ** 3
DATABASE_MAX = 512 * _MIB
HOST_FLOOR = 512 * _MIB
HOST_NEED = 1 * _GIB
HOST_CAP = 2 * _GIB
HOST_SHARE = 1 / 4


def system_reserve(total):
    """host.js systemReserve: 25% of the first 4 GiB, 20% of the next 4, 10%
    of the next 8, 6% of the next 112, 2% above, and 100 MiB for eviction."""
    left = total / _GIB
    reserve = 0.0
    for size, share in ((4, 0.25), (4, 0.20), (8, 0.10), (112, 0.06), (float("inf"), 0.02)):
        take = min(left, size)
        reserve += take * share
        left -= take
    return reserve * _GIB + 100 * _MIB


def host_memory_max(mem_total_kb):
    """The Host's MemoryMax ("615M") for a box whose MemTotal is this many
    kB, or None for anything that is not a count."""
    s = str(mem_total_kb if mem_total_kb is not None else "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", s):
        return None
    total = int(s) * 1024
    room = total - system_reserve(total) - DATABASE_MAX
    host = min(HOST_CAP, max(HOST_FLOOR, min(HOST_NEED, room), room * HOST_SHARE))
    return f"{int(host // _MIB)}M"


def host_limit_lines(paths):
    """configure's memory_max= line for this box's own memory, or "" when
    /proc/meminfo cannot be read."""
    try:
        with open(paths.p("proc/meminfo"), encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"^MemTotal:\s*([0-9]+) kB$", text, re.M)
    mem = host_memory_max(m.group(1)) if m else None
    return f"memory_max={mem}\n" if mem else ""


def runtime_lines_installed(paths):
    """The same lines for the image table now on the box, so a rollback puts
    back exactly what the previous release pinned. "" when there is none."""
    raw = read_json(paths.runtime_images(), None)
    if not isinstance(raw, dict):
        return ""
    lines = []
    for fam in RUNTIME_FAMILIES:
        ref = (raw.get("images") or {}).get(fam)
        if isinstance(ref, str) and "@sha256:" in ref:
            lines.append(f"{fam}={ref}")
    hs = raw.get("harness_src")
    if isinstance(hs, dict) and hs.get("url") and hs.get("sha256"):
        lines += [f"harness_src_url={hs['url']}", f"harness_src_sha256={hs['sha256']}"]
    return "".join(line + "\n" for line in lines)


def validate_manifest(m):
    """A cloud-release.json, checked: {version, images:{host:{ref,digest},
    postgres:{ref,digest}}, overlay:{url,sha256}, bastion:{min_version}}, and
    optionally runtime (the local runtime's images and the engines' sources)."""
    if not isinstance(m, dict):
        raise CloudError("the cloud release manifest is not an object")
    if not isinstance(m.get("version"), str) or not VERSION_RE.match(m["version"]):
        raise CloudError("the cloud release manifest's version is not X.Y.Z")
    _image_pair(m.get("images"), "images")
    ov = m.get("overlay")
    if not isinstance(ov, dict):
        raise CloudError("the cloud release manifest names no overlay")
    if not isinstance(ov.get("url"), str) or not re.match(r"^https://\S+$", ov["url"]):
        raise CloudError("overlay.url must be an https URL")
    if not isinstance(ov.get("sha256"), str) or not SHA256_RE.match(ov["sha256"]):
        raise CloudError("overlay.sha256 must be 64 hex characters")
    b = m.get("bastion")
    if not isinstance(b, dict) or not isinstance(b.get("min_version"), str) or \
            not VERSION_RE.match(b["min_version"]):
        raise CloudError("bastion.min_version must be X.Y.Z")
    if "runtime" in m:
        _validate_runtime(m["runtime"])
    return m


def pinned(img):
    return f"{img['ref']}@{img['digest']}"


# ── fetching ────────────────────────────────────────────────────────────────

USER_AGENT = "cseries-cloud/1"


def fetch(url, max_bytes, timeout=60):
    """The body at url, refused when it is larger than max_bytes. Redirects
    are followed (a GitHub release's latest/download answers with one)."""
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise CloudError(f"cannot fetch {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except OSError as e:
        raise CloudError(f"could not fetch {url}: {e}") from None
    if len(data) > max_bytes:
        raise CloudError(f"{url} is larger than {max_bytes} bytes")
    return data


def fetch_to_file(url, path, max_bytes, timeout=120):
    """Download url to path, returning its sha256; refused past max_bytes."""
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise CloudError(f"cannot fetch {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    n = 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise CloudError(f"{url} is larger than {max_bytes} bytes")
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        raise CloudError(f"could not fetch {url}: {e}") from None
    return h.hexdigest()


def fetch_signed(url, kind, keys=RELEASE_KEYS, fetcher=fetch):
    """(manifest, body, sig) for the signed manifest at url."""
    body = fetcher(url, MAX_MANIFEST_BYTES)
    sig = fetcher(signature_url(url, body), MAX_SIG_BYTES)
    manifest = verify_signed_manifest(kind, body, sig.decode("ascii", "replace"), keys)
    return manifest, body, sig


# ── the overlay ─────────────────────────────────────────────────────────────
#
# The overlay tarball (build-overlay.sh) holds VERSION, bootstrap.sh and root/,
# the files the box installs under /. A file whose name ends in .in is a
# template: @NAME@ in it is replaced by the value named NAME, and it is
# installed without the .in.

TOKEN_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def render(text, values):
    missing = sorted({m.group(1) for m in TOKEN_RE.finditer(text)} - set(values))
    if missing:
        raise CloudError(f"no value for {', '.join(missing)}")
    return TOKEN_RE.sub(lambda m: str(values[m.group(1)]), text)


def template_values(cfg, gids):
    return {
        "HOST_NAME": host_name(cfg),
        "ZONE_HOST": zone_host(cfg),
        "HOST_IP": HOST_IP,
        "HOST_UPSTREAM": f"{HOST_IP}:{HOST_PORT}",
        "TRUSTED_PROXIES": TRUSTED_PROXY,
        "PRIVATAE_HOST_GID": gids["privatae-host"],
        "CSERIES_GID": gids["cseries"],
        "REBOOT_TIME": cfg["reboot_window"],
    }


def overlay_files(src_root):
    """(relative path, absolute source path) of every file under src_root,
    sorted, so an overlay applies in the same order everywhere."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            out.append((os.path.relpath(full, src_root), full))
    return out


def apply_overlay(src_root, paths, values, backup=None):
    """Install every file of the overlay tree src_root under paths.root,
    rendering templates. A file already holding the same bytes and mode is
    left alone. Returns the installed paths (relative, without .in) that
    changed. Each file is replaced by a rename, so none is ever half written;
    nothing is deleted.

    With backup (a dict), what each changed path held before is recorded in
    it before the path is written: (bytes, mode), or None when the path did
    not exist. restore_files puts exactly that back, so an overlay that adds
    a file or replaces one the previous overlay never named can still be
    undone."""
    changed = []
    files = overlay_files(src_root)
    dests = [rel[:-3] if rel.endswith(".in") else rel for rel, _ in files]
    twice = sorted({d for d in dests if dests.count(d) > 1})
    if twice:
        raise CloudError(f"the overlay installs {', '.join(twice)} from a file and a template")
    for rel, src in files:
        mode = 0o755 if os.stat(src).st_mode & 0o111 else 0o644
        with open(src, "rb") as f:
            data = f.read()
        if rel.endswith(".in"):
            rel = rel[:-3]
            data = render(data.decode("utf-8"), values).encode("utf-8")
        dest = paths.p(rel)
        try:
            st = os.stat(dest)
            with open(dest, "rb") as f:
                old = f.read()
            prev = (old, st.st_mode & 0o777)
            same = old == data and prev[1] == mode
        except FileNotFoundError:
            prev = None
            same = False
        if not same:
            if backup is not None and rel not in backup:
                backup[rel] = prev
            atomic_write(dest, data, mode)
            changed.append(rel)
    return changed


def restore_files(paths, backup):
    """Put back what apply_overlay recorded in backup: each path's previous
    bytes and mode, or no file where there was none. Returns the paths it
    touched, sorted."""
    for rel, prev in sorted(backup.items()):
        dest = paths.p(rel)
        if prev is None:
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass
        else:
            atomic_write(dest, prev[0], prev[1])
    return sorted(backup)


def remove_files(paths, rels, backup):
    """Delete each path in rels (relative, as apply_overlay names them),
    first recording what it held in backup the way apply_overlay does, so
    restore_files brings it back. Returns the paths it deleted."""
    removed = []
    for rel in rels:
        dest = paths.p(rel)
        try:
            st = os.stat(dest)
            with open(dest, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            continue
        if rel not in backup:
            backup[rel] = (data, st.st_mode & 0o777)
        os.unlink(dest)
        removed.append(rel)
    return removed


# ── the overlay's units ─────────────────────────────────────────────────────
#
# A unit file the overlay installs in /etc/systemd/system is enabled when it
# has an [Install] section (WantedBy=, RequiredBy=, Alias= or Also=), which is
# what `systemctl enable` acts on. A unit without one is started by another,
# its socket or its timer, and is never enabled itself. An enabled unit that a
# target of BOOT_TARGETS wants is also started at once, since every running
# box has reached those targets and the next boot would start it anyway; one
# wanted only by another target (shutdown, a rescue target) is only enabled.
# Bootstrap and the updater both decide by this.

UNIT_DIR = "etc/systemd/system"
UNIT_SUFFIXES = (".service", ".socket", ".timer", ".path")
BOOT_TARGETS = frozenset({"sysinit.target", "basic.target", "sockets.target", "timers.target",
                          "paths.target", "multi-user.target", "default.target"})


def unit_install(text):
    """(enable, start) for a unit file's text, by its [Install] section."""
    section, keys = None, {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
        elif section == "Install" and "=" in s:
            k, v = s.split("=", 1)
            keys.setdefault(k.strip(), []).extend(v.split())
    enable = any(keys.get(k) for k in ("WantedBy", "RequiredBy", "Alias", "Also"))
    wanted = set(keys.get("WantedBy", ())) | set(keys.get("RequiredBy", ()))
    return enable, enable and bool(wanted & BOOT_TARGETS)


def overlay_units(src_root):
    """{unit name: (enable, start)} for every unit file the overlay tree
    src_root installs directly in /etc/systemd/system (drop-in directories
    are not units); {} when it installs none or src_root is missing."""
    d = os.path.join(src_root, UNIT_DIR)
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return {}
    out = {}
    for fn in names:
        name = fn[:-3] if fn.endswith(".in") else fn
        full = os.path.join(d, fn)
        if not name.endswith(UNIT_SUFFIXES) or not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8") as f:
            out[name] = unit_install(f.read())
    return out


# ── commands ────────────────────────────────────────────────────────────────

def run(cmd, check=True, input=None, timeout=None, env=None):
    """cmd (a list, never a shell line), its stdout as text."""
    r = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout,
                       env=env, check=False)
    if check and r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-5:]
        raise CloudError(f"{' '.join(cmd)} exited {r.returncode}: {' / '.join(tail)}")
    return r.stdout


# ── the heartbeat's signature ───────────────────────────────────────────────

HEARTBEAT_PREFIX = "privatae-machine-heartbeat-v1"


def heartbeat_statement(machine_id, ts, body):
    return f"{HEARTBEAT_PREFIX}\n{machine_id}\n{ts}\n{sha256_hex(body)}\n".encode("utf-8")


def load_or_create_key(path):
    """The box's Ed25519 heartbeat key (PKCS#8 PEM, 0600), made on first use."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        pass
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    atomic_write(path, pem, 0o600)
    return key


def public_key_b64(key):
    from cryptography.hazmat.primitives import serialization
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign_heartbeat(key, machine_id, ts, body):
    return base64.b64encode(key.sign(heartbeat_statement(machine_id, ts, body))).decode("ascii")
