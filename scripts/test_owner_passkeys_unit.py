#!/usr/bin/env python3
"""The owner's passkeys and recovery codes (server/owner_passkeys.py),
driven through the real webauthn library by a software authenticator built
here with `cryptography` (ES256, "none" attestation).

  1. A signed-in owner adds a passkey: the options name this Host's desktop
     name as the relying party, ask for a discoverable credential and user
     verification, and the passkey is kept with its transports. Ten recovery
     codes come with the first one, kept only as hashes.
  2. Once a passkey exists the password no longer signs in.
  3. A passkey signs in: a new session on this Host's name, the counter
     moved on, and the sign-in counts as a fresh check.
  4. Refused, and counted as a wrong try: another origin, a sibling name as
     relying party, no user verification, a counter that went back, an
     unknown passkey, a forged signature. A used challenge, or one made for
     another purpose, another name or another session, is refused.
  5. Five wrong tries lock the client like wrong passwords do.
  6. A recovery code signs in once, typed loosely; a wrong one counts.
  7. A step-up holds for five minutes; adding another passkey or making new
     codes needs one (a recovery-code sign-in counts there, not for pairing).
  8. Pending challenges stay bounded per visitor.
  9. Through the routes on an https Host: the sign-in sets the __Host- cookie;
     /login offers the passkey and a recovery code instead of the password.
 10. Adding the first passkey ends every other session, the app-origin
     sessions joined from them included, even one checked a moment ago (no
     cached answer outlives it) and a join code made from one; this session
     and the app origins it opened stay.
 11. Removing a passkey needs a fresh passkey or recovery-code check, ends
     every other session, and never removes the last passkey, not even two
     removals at once.
 12. Signing out every other session needs the same fresh check, or before
     any passkey a password sign-in in the last five minutes.
 13. Every visitor's wrong passwords together, past their limit, never hold
     a passkey or recovery-code sign-in.

No database, no network: the store is a stand-in.

    python3 scripts/test_owner_passkeys_unit.py      (from the repo root)
"""
import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone

os.environ["CSERIES_URL"] = "https://home.box.example"
os.environ.pop("CSERIES_TRUSTED_PROXIES", None)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cbor2  # noqa: E402  (the webauthn library's own dependency)
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from server import owner_login as L  # noqa: E402
from server import owner_passkeys as P  # noqa: E402
from server.applications import app_origin as AO  # noqa: E402

failed = 0
HOST = "home.box.example"
ORIGIN = "https://home.box.example"
APP = AO.app_host("settings", HOST)
CLIENT = "198.51.100.10"


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """A passkey on a pretend device: one ES256 key, its counter, and the
    browser's half (clientDataJSON) folded in."""

    def __init__(self, counter_step=1):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.counter = 0
        self.step = counter_step

    def cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind, challenge_b64, origin):
        return json.dumps({"type": kind, "challenge": challenge_b64, "origin": origin,
                           "crossOrigin": False}).encode()

    def create(self, options, origin=ORIGIN, rp_id=None, flags=0x45):
        rp = rp_id or options["rp"]["id"]
        cdj = self._client_data("webauthn.create", options["challenge"], origin)
        auth = (hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
                + b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self.cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att),
                             "transports": ["internal", "hybrid", "carrier-pigeon"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def get(self, options, origin=ORIGIN, rp_id=None, flags=0x05, counter=None, forge=False):
        rp = rp_id or options["rpId"]
        if counter is None:
            self.counter += self.step
            counter = self.counter
        cdj = self._client_data("webauthn.get", options["challenge"], origin)
        auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", counter)
        signer = ec.generate_private_key(ec.SECP256R1()) if forge else self.key
        sig = signer.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth),
                             "signature": b64u(sig), "userHandle": b64u(P.USER_HANDLE)},
                "clientExtensionResults": {}}


class Store:
    """The four tables the module touches, as dicts."""

    def __init__(self):
        self.owner = {"email": "owner@example.com", "password_hash": "x", "cookie_suffix": "0a1b2c3d"}
        self.passkeys = {}      # credential id -> row
        self.codes = {}         # hash -> used_at or None
        self.sessions = {}      # token hash -> row

    async def fetchval(self, sql, *a):
        if sql.startswith("SELECT count(*) FROM owner_passkeys"):
            await asyncio.sleep(0)      # a real query yields
            return len(self.passkeys)
        if sql.startswith("SELECT count(*) FROM owner_recovery_codes"):
            return sum(1 for v in self.codes.values() if v is None)
        if sql.startswith("SELECT EXISTS (SELECT 1 FROM owner_passkeys)"):
            return bool(self.passkeys)
        if sql.startswith("INSERT INTO owner_passkeys"):
            if a[0] in self.passkeys:
                return None
            self.passkeys[a[0]] = {"credential_id": a[0], "public_key": a[1], "sign_count": a[2],
                                   "transports": list(a[3]), "label": a[4],
                                   "created_at": time.time(), "last_used_at": None}
            return a[0]
        if sql.startswith("UPDATE owner_recovery_codes SET used_at"):
            if a[0] in self.codes and self.codes[a[0]] is None:
                self.codes[a[0]] = time.time()
                return a[0]
            return None
        if sql.startswith("DELETE FROM owner_passkeys WHERE credential_id = $1 RETURNING"):
            await asyncio.sleep(0)      # a real query yields; let another removal run here
            return a[0] if self.passkeys.pop(a[0], None) else None
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql, *a):
        if sql.startswith("SELECT credential_id, transports FROM owner_passkeys") \
                or sql.startswith("SELECT credential_id, label, created_at, last_used_at, rp_id "
                                  "FROM owner_passkeys"):
            return sorted(self.passkeys.values(), key=lambda r: r["created_at"])
        if sql.startswith("DELETE FROM owner_sessions WHERE token_hash <> $1 AND parent IS DISTINCT FROM $1"):
            keep = a[0]
            gone = [h for h, r in self.sessions.items() if h != keep and r["parent"] != keep]
            for h in gone:
                del self.sessions[h]
            # ON DELETE CASCADE
            for h in [h for h, r in self.sessions.items() if r["parent"] in gone]:
                del self.sessions[h]
            return [{"token_hash": h} for h in gone]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql, *a):
        if sql.startswith("SELECT public_key, sign_count FROM owner_passkeys"):
            return self.passkeys.get(a[0])
        if sql.startswith("SELECT parent FROM owner_sessions WHERE token_hash = $1"):
            return self.sessions.get(a[0])
        if "FROM owner_account" in sql:
            return self.owner
        if "FROM owner_sessions s" in sql:
            return self.sessions.get(a[0])
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO owner_sessions"):
            self.sessions[a[0]] = {"last_seen_at": a[1], "expires_at": a[2], "origin": a[3],
                                   "parent": None, "parent_seen": None, "parent_expires": None}
            return "INSERT 0 1"
        if sql.startswith("UPDATE owner_passkeys SET sign_count"):
            self.passkeys[a[0]]["sign_count"] = a[1]
            self.passkeys[a[0]]["last_used_at"] = time.time()
            return "UPDATE 1"
        if sql.startswith("WITH gone AS (DELETE FROM owner_recovery_codes)"):
            self.codes = {h: None for h in a[0]}
            return "INSERT 0 10"
        if sql.startswith("UPDATE owner_sessions") or sql.startswith("DELETE FROM owner_sessions"):
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")


DB = Store()


def fresh_state():
    L._pool = lambda: DB
    L._owner_email, L._cookie_suffix, L._setup_code = DB.owner["email"], DB.owner["cookie_suffix"], None
    L._failures.clear()
    L._cache.clear()
    P._ceremonies.clear()
    P._step_ups.clear()


def password_session() -> str:
    token = "pw-" + os.urandom(8).hex()
    now = datetime.now(timezone.utc)
    DB.sessions[L._token_hash(token)] = {"last_seen_at": now, "expires_at": now + L.SESSION_MAX,
                                         "origin": HOST, "parent": None, "parent_seen": None,
                                         "parent_expires": None}
    return token


def app_session(parent: str) -> str:
    """An app origin's session joined from the desktop session `parent`."""
    token = "app-" + os.urandom(8).hex()
    now = datetime.now(timezone.utc)
    DB.sessions[L._token_hash(token)] = {"last_seen_at": now, "expires_at": now + L.SESSION_MAX,
                                         "origin": APP, "parent": L._token_hash(parent),
                                         "parent_seen": now, "parent_expires": now + L.SESSION_MAX}
    return token


async def live(token, host=HOST) -> bool:
    return await L.session_email(token, host) == DB.owner["email"]


async def refused(coro):
    try:
        await coro
        return None
    except L.LoginError as exc:
        return exc


async def register_one(token, dev):
    opts = await P.registration_options(token, HOST)
    return opts, await P.register(token, HOST, opts["ceremony"], dev.create(opts["options"]), "laptop")


async def main():
    fresh_state()
    import webauthn
    print(f"(webauthn {getattr(webauthn, '__version__', '?')})")
    dev = Authenticator()
    token = password_session()

    # 1. adding the first passkey
    check(isinstance(await refused(P.registration_options(token, APP)), P.PasskeyError),
          "a passkey is added only on the desktop's own name")
    # A session cookie is not the password: taken from the owner's browser, it
    # must not be able to add the first passkey, which would refuse the
    # owner's password from then on and hand the recovery codes to the taker.
    err = await refused(P.registration_options(token, HOST))
    check(isinstance(err, P.PasskeyError) and not isinstance(err, P.StepUpNeeded),
          "a session without a password sign-in in the last five minutes cannot add the "
          "first passkey", err)
    P.note_password_sign_in(token)
    check(not P.fresh_step_up(token) and not P.fresh_step_up(token, (P.PASSKEY, P.RECOVERY)),
          "a password sign-in is no passkey check: it opens no pairing port and adds no second passkey")
    at, kind = P._step_ups[L._token_hash(token)]
    P._step_ups[L._token_hash(token)] = (at - P.STEP_UP_S - 1, kind)
    check(isinstance(await refused(P.registration_options(token, HOST)), P.PasskeyError),
          "a password sign-in older than five minutes no longer adds the first passkey")
    P.note_password_sign_in(token)
    # Other sessions alive when the first passkey comes: one made by whoever
    # else knew the password, an app origin joined from it, and this
    # session's own app origin. Each is checked once first, so a cached
    # answer is in play.
    other = password_session()
    other_app = app_session(other)
    mine_app = app_session(token)
    join_code = L.start_join(other, APP)
    check(all([await live(other), await live(other_app, APP), await live(mine_app, APP)]),
          "before the first passkey the other sessions are live (and cached)")
    opts = await P.registration_options(token, HOST)
    o = opts["options"]
    check(o["rp"]["id"] == HOST and o["user"]["id"] == b64u(P.USER_HANDLE)
          and o["authenticatorSelection"].get("residentKey") == "required"
          and o["authenticatorSelection"].get("userVerification") == "required"
          and o.get("attestation") == "none" and not o.get("excludeCredentials"),
          "the options: this Host's own name, a discoverable credential, user verification", o)
    got = await P.register(token, HOST, opts["ceremony"], dev.create(o), "laptop")
    codes = got["recovery_codes"] or []
    # 10. every other session ended with it
    check(got.get("sessions_ended") == 2 and not await live(other) and not await live(other_app, APP),
          "the first passkey ends every other session, the app origin joined from it too, "
          "though both were checked a moment ago", got.get("sessions_ended"))
    check(L._token_hash(other) not in DB.sessions and L._token_hash(other_app) not in DB.sessions,
          "their rows are gone")
    check(await live(token) and await live(mine_app, APP),
          "this session and the app origin it opened stay")
    check(await L.finish_join(join_code, APP) is None,
          "a join code made from an ended session opens nothing")
    row = DB.passkeys.get(dev.cred_id) or {}
    check(row.get("transports") == ["internal", "hybrid"] and row.get("label") == "laptop"
          and row.get("sign_count") == 0, "the passkey is kept with the transports a browser can use", row)
    check(len(codes) == 10 and len(set(codes)) == 10
          and all(len(c) == 23 and c.count("-") == 3 for c in codes),
          "ten recovery codes come with the first passkey", codes)
    stored = set(DB.codes)
    check(len(stored) == 10 and not any(c in stored or c.replace("-", "") in stored for c in codes),
          "the codes are kept only as hashes")
    check(isinstance(await refused(P.register(token, HOST, opts["ceremony"], dev.create(o))), P.PasskeyError),
          "the same challenge cannot add a passkey twice")

    # 7. another passkey needs a fresh check
    err = await refused(P.registration_options(token, HOST))
    check(isinstance(err, P.StepUpNeeded), "adding a second passkey without a fresh check is refused", err)

    # 2. the password no longer signs in
    err = await refused(L.sign_in("owner@example.com", "a long password here", CLIENT, HOST))
    check(err is not None and "passkey" in str(err), "once a passkey exists the password is refused", err)

    # 3. signing in with the passkey
    lo = await P.login_options(CLIENT, HOST)
    check(lo["options"]["rpId"] == HOST and not lo["options"].get("allowCredentials")
          and lo["options"].get("userVerification") == "required",
          "sign-in options name no passkey and ask for user verification", lo["options"])
    tok = await P.passkey_sign_in(lo["ceremony"], dev.get(lo["options"]), CLIENT, HOST)
    sess = DB.sessions.get(L._token_hash(tok)) or {}
    check(sess.get("origin") == HOST and DB.passkeys[dev.cred_id]["sign_count"] == 1
          and DB.passkeys[dev.cred_id]["last_used_at"] is not None,
          "a passkey sign-in makes a session on this Host's name and moves the counter on", sess)
    check(await L.session_email(tok, HOST) == "owner@example.com", "that session is live")
    check(P.fresh_step_up(tok), "a passkey sign-in counts as a fresh check")

    # 4. refusals, each counted as a wrong try
    async def wrong(what, **kw):
        L._failures.clear()
        lo = await P.login_options(CLIENT, HOST)
        cred = kw.pop("cred", None) or (kw.pop("dev", dev)).get(lo["options"], **kw)
        err = await refused(P.passkey_sign_in(lo["ceremony"], cred, CLIENT, HOST))
        counted = L._failures.get(L._failure_key(CLIENT), (0,))[0] == 1
        check(err is not None and counted, f"refused and counted: {what}", (err, L._failures))
    await wrong("an answer made on an app origin", origin=f"https://{APP}")
    await wrong("an answer for a sibling name as relying party", rp_id=APP)
    await wrong("an answer without user verification", flags=0x01)
    await wrong("a counter that did not move on", counter=1)
    await wrong("a passkey this Host does not know", dev=Authenticator())
    await wrong("a forged signature", forge=True)
    await wrong("an answer that is no passkey's", cred={"id": "x", "rawId": "!!", "type": "public-key"})

    L._failures.clear()
    lo = await P.login_options(CLIENT, HOST)
    answer = dev.get(lo["options"])
    await P.passkey_sign_in(lo["ceremony"], answer, CLIENT, HOST)
    err = await refused(P.passkey_sign_in(lo["ceremony"], answer, CLIENT, HOST))
    check(err is not None and "expired" in str(err), "a used challenge is refused", err)
    lo2 = await P.login_options(CLIENT, HOST)
    err = await refused(P.passkey_sign_in(lo2["ceremony"], answer, CLIENT, HOST))
    check(err is not None, "an earlier answer does not fit a new challenge", err)
    lo3 = await P.login_options(CLIENT, HOST)
    err = await refused(P.step_up(tok, lo3["ceremony"], dev.get(lo3["options"]), CLIENT, HOST))
    check(err is not None and "expired" in str(err), "a sign-in challenge cannot be used for a step-up", err)
    lo4 = await P.login_options("198.51.100.99", HOST)
    err = await refused(P.passkey_sign_in(lo4["ceremony"], dev.get(lo4["options"]), CLIENT, HOST))
    check(err is not None, "a challenge made for one visitor is refused to another", err)

    # 5. five wrong tries lock the client
    L._failures.clear()
    outcomes = []
    for _ in range(7):
        lo = await P.login_options(CLIENT, HOST)
        err = await refused(P.passkey_sign_in(lo["ceremony"], dev.get(lo["options"], forge=True),
                                              CLIENT, HOST))
        outcomes.append("held" if err and "Too many" in str(err) else "wrong")
    check(outcomes == ["wrong"] * 5 + ["held"] * 2, "five wrong passkey tries, then a wait", outcomes)
    L._failures.clear()

    # 6. recovery codes
    loose = codes[0].lower().replace("-", " ")
    rtok = await P.recovery_sign_in(loose, CLIENT, HOST)
    check(DB.sessions.get(L._token_hash(rtok), {}).get("origin") == HOST,
          "a recovery code, typed in lower case with spaces, signs in")
    err = await refused(P.recovery_sign_in(codes[0], CLIENT, HOST))
    check(err is not None and L._failures.get(L._failure_key(CLIENT), (0,))[0] == 1,
          "a spent code is refused and counted", err)
    err = await refused(P.recovery_sign_in("AAAAA-BBBBB-CCCCC-DDDDD", CLIENT, HOST))
    check(err is not None, "a made-up code is refused", err)
    check(await P.unused_recovery_codes(DB) == 9, "nine codes are left")
    check(P.fresh_step_up(rtok, (P.PASSKEY, P.RECOVERY)) and not P.fresh_step_up(rtok),
          "a recovery-code sign-in counts for adding a passkey, not as a passkey check")
    L._failures.clear()

    # 7. step-up, and what it opens
    dev2 = Authenticator()
    opts2, got2 = await register_one(rtok, dev2)
    check(dev2.cred_id in DB.passkeys and got2["recovery_codes"] is None,
          "after a recovery-code sign-in a new passkey can be added; codes left are kept",
          got2)
    check(len(opts2["options"].get("excludeCredentials") or []) == 1,
          "the options exclude the passkey already on this Host")
    plain = password_session()
    check(isinstance(await refused(P.replace_recovery_codes(plain, HOST)), P.StepUpNeeded),
          "new codes need a fresh check")
    so = await P.step_up_options(plain, HOST)
    allowed = {c["id"] for c in so["options"].get("allowCredentials") or []}
    check(allowed == {b64u(dev.cred_id), b64u(dev2.cred_id)}, "a step-up names this Host's passkeys", allowed)
    await P.step_up(plain, so["ceremony"], dev2.get(so["options"]), CLIENT, HOST)
    check(P.fresh_step_up(plain), "a step-up is fresh at once")
    new = await P.replace_recovery_codes(plain, HOST)
    check(len(new) == 10 and await P.unused_recovery_codes(DB) == 10
          and await refused(P.recovery_sign_in(codes[1], CLIENT, HOST)) is not None,
          "new codes replace every earlier one")
    L._failures.clear()
    at, kind = P._step_ups[L._token_hash(plain)]
    P._step_ups[L._token_hash(plain)] = (at - P.STEP_UP_S - 1, kind)
    check(not P.fresh_step_up(plain), "a step-up is stale after five minutes")
    so = await P.step_up_options(plain, HOST)
    other = password_session()
    err = await refused(P.step_up(other, so["ceremony"], dev.get(so["options"]), CLIENT, HOST))
    check(err is not None, "a step-up challenge is bound to the session it was made for", err)

    # 13. a flood of wrong passwords never holds a passkey or a code
    now_m = time.monotonic()
    L._password_failures = (L.GLOBAL_FREE + 40, now_m + 900, now_m)
    L._failures.clear()
    lo = await P.login_options(CLIENT, HOST)
    held_tok = await refused(P.passkey_sign_in(lo["ceremony"], dev.get(lo["options"]), CLIENT, HOST))
    check(held_tok is None, "with every password sign-in held, a passkey still signs in", held_tok)
    spare = await P.new_recovery_codes(DB)
    held_code = await refused(P.recovery_sign_in(spare[0], CLIENT, HOST))
    check(held_code is None, "with every password sign-in held, a recovery code still signs in",
          held_code)
    L._password_failures = (0, 0.0, 0.0)

    # 14. wrong recovery codes from a new address each time (a relay writes
    # the source on the remote address) meet one wait for the whole Host,
    # and a passkey still signs in through it
    L._failures.clear()
    L._recovery_failures = (0, 0.0, 0.0)
    outcomes = []
    for i in range(L.GLOBAL_FREE + 2):
        err = await refused(P.recovery_sign_in("AAAAA-BBBBB-CCCCC-DDDDD", f"198.51.100.{i + 1}", HOST))
        outcomes.append("held" if err and "Too many" in str(err) else "wrong")
    check(outcomes == ["wrong"] * L.GLOBAL_FREE + ["held"] * 2,
          "wrong recovery codes from ever-new addresses: GLOBAL_FREE, then a wait for every source",
          outcomes)
    spare = await P.new_recovery_codes(DB)
    held_code = await refused(P.recovery_sign_in(spare[0], "198.51.100.200", HOST))
    check(held_code is not None and "Too many" in str(held_code),
          "while it holds, a right code from a new address waits too", held_code)
    lo = await P.login_options(CLIENT, HOST)
    held_tok = await refused(P.passkey_sign_in(lo["ceremony"], dev.get(lo["options"]), CLIENT, HOST))
    check(held_tok is None, "with every recovery code held, a passkey still signs in", held_tok)
    L._recovery_failures = (0, 0.0, 0.0)
    L._failures.clear()

    # 11. removing a passkey; 12. signing out every other session
    survivor = await removal(dev, dev2)

    # 8. bounded challenges
    P._ceremonies.clear()
    ids = [(await P.login_options("203.0.113.40", HOST))["ceremony"] for _ in range(5)]
    check(len(P._ceremonies) == P._PER_HOLDER and ids[0] not in P._ceremonies and ids[-1] in P._ceremonies,
          "one visitor holds at most three pending challenges; the oldest go", len(P._ceremonies))
    L._owner_email = None
    check(isinstance(await refused(P.login_options(CLIENT, HOST)), P.PasskeyError),
          "no passkey sign-in before there is an owner")
    L._owner_email = DB.owner["email"]

    # 9. through the routes
    await routes(survivor)


def b64id(dev) -> str:
    return b64u(dev.cred_id)


def stale(token):
    h = L._token_hash(token)
    at, kind = P._step_ups[h]
    P._step_ups[h] = (at - P.STEP_UP_S - 1, kind)


async def removal(dev, dev2):
    """dev and dev2 are this Host's two passkeys."""
    L._failures.clear()
    me = password_session()
    bystander = password_session()
    err = await refused(P.remove_passkey(me, HOST, b64id(dev2)))
    check(isinstance(err, P.StepUpNeeded) and dev2.cred_id in DB.passkeys,
          "removing a passkey without a fresh check is refused", err)
    P.note_password_sign_in(me)
    err = await refused(P.remove_passkey(me, HOST, b64id(dev2)))
    check(isinstance(err, P.StepUpNeeded), "a password sign-in is no check for removing a passkey", err)
    so = await P.step_up_options(me, HOST)
    await P.step_up(me, so["ceremony"], dev.get(so["options"]), CLIENT, HOST)
    check(isinstance(await refused(P.remove_passkey(me, APP, b64id(dev2))), P.PasskeyError),
          "a passkey is removed only on the desktop's own name")
    err = await refused(P.remove_passkey(me, HOST, b64u(b"no-such-passkey")))
    check(isinstance(err, P.PasskeyError) and len(DB.passkeys) == 2, "an unknown passkey: refused", err)
    err = await refused(P.remove_passkey(me, HOST, "!!not base64!!"))
    check(isinstance(err, P.PasskeyError), "an id that is no passkey's: refused", err)
    ended = await P.remove_passkey(me, HOST, b64id(dev2))
    check(dev2.cred_id not in DB.passkeys and ended >= 1 and not await live(bystander) and await live(me),
          "removing a passkey takes it off and ends every other session", ended)
    err = await refused(P.remove_passkey(me, HOST, b64id(dev)))
    check(isinstance(err, P.PasskeyError) and not isinstance(err, P.StepUpNeeded)
          and dev.cred_id in DB.passkeys and "only passkey" in str(err),
          "the last passkey is never removed", err)
    # Two removals at once of the two passkeys left: one goes, one stays.
    dev3 = Authenticator()
    await register_one(me, dev3)
    results = await asyncio.gather(P.remove_passkey(me, HOST, b64id(dev)),
                                   P.remove_passkey(me, HOST, b64id(dev3)), return_exceptions=True)
    check(len(DB.passkeys) == 1 and sum(1 for x in results if isinstance(x, P.PasskeyError)) == 1,
          "two removals at once of the last two passkeys leave one", (results, len(DB.passkeys)))
    left = next(iter(DB.passkeys))
    survivor = dev if left == dev.cred_id else dev3
    stale(me)
    check(isinstance(await refused(P.remove_passkey(me, HOST, b64id(survivor))), P.StepUpNeeded),
          "a check older than five minutes no longer removes a passkey")

    # 12. signing out every other session
    another = password_session()
    err = await refused(P.end_other_sessions(me, HOST))
    check(isinstance(err, P.StepUpNeeded) and await live(another),
          "signing out the other sessions without a fresh check is refused", err)
    so = await P.step_up_options(me, HOST)
    await P.step_up(me, so["ceremony"], survivor.get(so["options"]), CLIENT, HOST)
    child = app_session(me)
    n = await P.end_other_sessions(me, HOST)
    check(n >= 1 and not await live(another) and await live(me) and await live(child, APP),
          "with a fresh check every other session ends; this one and its app origin stay", n)
    err = await refused(L.end_other_sessions(child))
    check(isinstance(err, L.LoginError) and await live(me),
          "an app origin's session cannot end the others (it would end its own desktop session)", err)
    saved = dict(DB.passkeys)
    DB.passkeys.clear()
    fresh_pw, old_pw = password_session(), password_session()
    P.note_password_sign_in(fresh_pw)
    err = await refused(P.end_other_sessions(old_pw, HOST))
    check(isinstance(err, P.PasswordAgain) and "/login" in str(err),
          "before any passkey, a session with no recent password sign-in is sent to sign in again", err)
    check(await P.end_other_sessions(fresh_pw, HOST) >= 1 and not await live(old_pw),
          "before any passkey, a password sign-in in the last five minutes signs the others out")
    DB.passkeys.update(saved)
    return survivor


async def routes(dev):
    import httpx
    from fastapi import FastAPI
    from server.routes import owner as R
    app = FastAPI()
    app.include_router(R.router)
    L._failures.clear()
    transport = httpx.ASGITransport(app=app, client=(CLIENT, 41000))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as c:
        page = await c.get("/login")
        check(page.status_code == 200 and 'id="passkey"' in page.text
              and "/api/owner/recovery/login" in page.text and 'name="password"' not in page.text,
              "/login offers the passkey and a recovery code, not the password")
        o = (await c.post("/api/owner/passkey/login/options", json={})).json()
        r = await c.post("/api/owner/passkey/login/verify",
                         json={"ceremony": o["ceremony"], "credential": dev.get(o["options"])})
        sc = r.headers.get("set-cookie", "")
        check(r.status_code == 200 and r.json().get("next") == "/"
              and sc.startswith("__Host-cseries_session_0a1b2c3d=") and "Secure" in sc,
              "a passkey sign-in through the route sets the __Host- cookie", (r.status_code, sc))
        cookie = sc.split(";", 1)[0]
        c.cookies.clear()       # every later request names its cookie, or has none
        page = await c.get("/passkey", headers={"cookie": cookie})
        check(page.status_code == 200 and 'id="confirm"' in page.text and "1 passkey " in page.text
              and 'id="end-others"' in page.text and "data-remove=" not in page.text,
              "the passkey page shows a signed-in owner the confirm button, the sign-out of every "
              "other session, and no Remove for the only passkey", page.status_code)
        r = await c.post("/api/owner/passkey/remove", json={"id": b64id(dev)})
        check(r.status_code == 401, "removing a passkey without a session: 401", r.status_code)
        r = await c.post("/api/owner/passkey/remove", json={"id": b64id(dev)}, headers={"cookie": cookie})
        check(r.status_code == 403 and "only passkey" in r.json().get("error", "")
              and not r.json().get("step_up") and dev.cred_id in DB.passkeys,
              "the only passkey is not removed through the route either", (r.status_code, r.json()))
        dev4 = Authenticator()
        o4 = (await c.post("/api/owner/passkey/register/options", json={}, headers={"cookie": cookie})).json()
        r = await c.post("/api/owner/passkey/register/verify", headers={"cookie": cookie},
                         json={"ceremony": o4["ceremony"], "credential": dev4.create(o4["options"])})
        check(r.status_code == 200 and r.json().get("sessions_ended") == 0
              and r.json().get("recovery_codes") is None,
              "a second passkey ends no session and makes no codes", r.json())
        page = await c.get("/passkey", headers={"cookie": cookie})
        check(page.text.count("data-remove=") == 2 and f'data-remove="{b64id(dev4)}"' in page.text,
              "with two passkeys each has a Remove button naming it")
        bystander = password_session()
        r = await c.post("/api/owner/passkey/remove", json={"id": b64id(dev4)}, headers={"cookie": cookie})
        check(r.status_code == 200 and r.json().get("sessions_ended", 0) >= 1
              and dev4.cred_id not in DB.passkeys and not await live(bystander),
              "removing a passkey through the route ends the other sessions", r.json())
        stale_s = password_session()
        r = await c.post("/api/owner/sessions/end-others", json={},
                         headers={"cookie": f"{L.cookie_name()}={stale_s}"})
        check(r.status_code == 403 and r.json().get("step_up") is True,
              "signing the others out with no fresh check: 403, asking for a passkey check",
              (r.status_code, r.json()))
        r = await c.post("/api/owner/sessions/end-others", json={}, headers={"cookie": cookie})
        check(r.status_code == 200 and r.json().get("sessions_ended", 0) >= 1 and not await live(stale_s),
              "after a passkey sign-in the route signs every other session out", r.json())
        r = await c.post("/api/owner/passkey/register/options", json={})
        check(r.status_code == 401, "adding a passkey without a session: 401", r.status_code)
        r = await c.post("/api/owner/recovery/new", json={}, headers={"cookie": cookie})
        check(r.status_code == 200 and len(r.json().get("recovery_codes") or []) == 10,
              "new codes right after a passkey sign-in: allowed, shown once", r.status_code)
        DB.passkeys.clear()
        page = await c.get("/login")
        check('name="password"' in page.text and 'id="passkey"' not in page.text,
              "with no passkey, /login is the password form")
        # The first passkey through the routes: a password sign-in there opens
        # it; an older session (or a stolen cookie) gets told to sign in again,
        # with no passkey check asked for, since there is no passkey to check.
        DB.owner["password_hash"] = await L.hash_password("a long password here")
        r = await c.post("/api/owner/login", json={"email": "owner@example.com",
                                                   "password": "a long password here"})
        pw_cookie = r.headers.get("set-cookie", "").split(";", 1)[0]
        c.cookies.clear()
        check(r.status_code == 200 and r.json().get("next") == "/passkey" and pw_cookie,
              "a password sign-in on an https Host goes on to add a passkey", r.status_code)
        r = await c.post("/api/owner/passkey/register/options", json={}, headers={"cookie": pw_cookie})
        check(r.status_code == 200 and r.json().get("ceremony"),
              "right after a password sign-in the first passkey can be added", r.status_code)
        stale = password_session()
        r = await c.post("/api/owner/passkey/register/options", json={},
                         headers={"cookie": f"{L.cookie_name()}={stale}"})
        body = r.json()
        check(r.status_code == 403 and not body.get("step_up") and "/login" in body.get("error", ""),
              "a session with no recent password sign-in is sent to sign in again, not to a "
              "passkey check", (r.status_code, body))


asyncio.run(main())
sys.exit(failed)
