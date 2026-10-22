#!/usr/bin/env python3
"""Connecting a Host to a git server the person runs (Settings, Git server).

server/git_server.py and server/routes/git_server.py, against a fake Gitea on
a loopback port over https (a certificate made for the run and trusted
through SSL_CERT_FILE, so the real https clients are what is tested).
Pinned here:

  1. connect refuses a non-https address, a token the server does not accept,
     a token of another account and a token that cannot create repositories,
     and stores nothing for any of them; a good one connects;
  2. the token is sealed in Keys under one name and is not in the settings row;
  3. the resolver feeds the coding-forge readers (gitea_creds, the per-run
     harness credential, open_forge, the site publish clone, the repo links in
     knowledge_toc) and not the Privatae-forge readers (nac_egress's platform
     destinations, proposals, bug reports, the community board);
  4. ~/.git-credentials (mode 600) and the git identity are written under a
     temporary HOME;
  5. a new project repo is created under the Host's account, and the person's
     username is added as a collaborator with the Host's own token; whether
     the Host may merge there (Auto-Approve) is the connected server's answer;
  6. approve_url on a connected server is the pull request's page there and,
     with nothing connected, is the platform's approve window as before;
  7. a merge on the connected server is announced as the person's or as the
     Host's from merged_by;
  8. a person's sign-in token is never sent to a connected server;
  9. only Settings, with the owner signed in, may connect or disconnect;
 10. disconnect clears the token, the row, the resolver and the git files;
 11. the vault holds the token's name: it reads as absent, cannot be
     written, and no broker action or keys_manage op reaches it, so nothing
     sends it elsewhere or swaps in another account's token;
 12. a token that left Keys (at start, at Disconnect, revoked in the Keys
     app) is not used through the git credential file either.

Container-free: APP_ROOT=<repo> python3 scripts/test_git_server_connect.py
Every name, address and token is invented. HOME is a temporary directory, so
the real ~/.git-credentials of the machine running this is never touched.
"""
import asyncio
import base64
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

TMP = Path(tempfile.mkdtemp(prefix="git-server-test-"))
HOME = TMP / "home"
HOME.mkdir()
# Before anything resolves a path or builds an SSL context.
os.environ["HOME"] = str(HOME)
os.environ["XDG_CONFIG_HOME"] = str(HOME / ".config")
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
for k in ("GITEA_USERNAME", "GITEA_API_TOKEN"):
    os.environ.pop(k, None)
# Privatae's forge, as an install on the platform would set it: it must stay
# where the Privatae-forge readers look, whatever the person connects.
os.environ["GITEA_URL"] = "https://forge.platform.example"
# The fake server below answers on 127.0.0.1, an address a connect refuses
# outside a test machine; section 13 checks that refusal with this unset.
os.environ["CSERIES_GIT_SERVER_ALLOW_LOOPBACK"] = "1"

CERT, KEY = TMP / "cert.pem", TMP / "key.pem"
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-keyout", str(KEY), "-out", str(CERT), "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1"],
               check=True, capture_output=True)
os.environ["SSL_CERT_FILE"] = str(CERT)

FAILS: list = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


# ── the fake Gitea ──────────────────────────────────────────────────────────
HOST_USER = "host-bot"
PERSON = "robin-example"
TOK_HOST = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
TOK_OTHER = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
TOK_NOREPO = "9a8b7c6d5e4f30211203f4e5d6c7b8a99a8b7c6d"
TOKENS = {TOK_HOST: (HOST_USER, True), TOK_OTHER: ("someone-else", True),
          TOK_NOREPO: (HOST_USER, False)}
SEEN: list = []          # (method, path, auth_login, auth_kind, body)
PRS: dict = {}           # number -> pr json


class Gitea(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _who(self):
        h = self.headers.get("Authorization") or ""
        if h.startswith("Basic "):
            _u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            # Gitea signs a basic-auth request in as the token's owner,
            # whatever username comes with it.
            return TOKENS.get(pw), "basic"
        if h.startswith("Bearer "):
            return None, "bearer"
        return None, ""

    def _send(self, code, obj=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        who, kind = self._who()
        SEEN.append((method, self.path, who[0] if who else None, kind, body))
        if who is None:
            return self._send(401, {"message": "unauthorized"})
        login, can_repo = who
        path = self.path.split("?", 1)[0]
        if method == "GET" and path == "/api/v1/user":
            return self._send(200, {"login": login, "email": f"{login}@mail.example"})
        if method == "POST" and path == "/api/v1/user/repos":
            if not can_repo:
                return self._send(403, {"message": "token does not have at least one of required scope(s)"})
            if not (body or {}).get("name"):
                return self._send(422, {"message": "[Name]: Required"})
            return self._send(201, {"full_name": f"{login}/{body['name']}", "name": body["name"]})
        if method == "PUT" and "/collaborators/" in path:
            return self._send(204)
        if method == "GET" and "/branches/" in path:
            owner = path.split("/")[4]
            # The forge answers for the calling account: the repo's owner may merge.
            return self._send(200, {"name": path.rsplit("/", 1)[-1], "user_can_merge": owner == login})
        if method == "GET" and "/pulls/" in path:
            num = int(path.rsplit("/", 1)[-1])
            return self._send(200, PRS.get(num) or {})
        return self._send(404, {"message": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


srv = ThreadingHTTPServer(("127.0.0.1", 0), Gitea)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(str(CERT), str(KEY))
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
NETLOC = f"127.0.0.1:{srv.server_address[1]}"
URL = f"https://{NETLOC}"


# ── the fakes for the database and Keys ─────────────────────────────────────
class FakePool:
    def __init__(self):
        self.bp: dict = {}
        self.sql: list = []
        self.projects = {1: {"id": 1, "name": "Garden planner", "description": "", "repo": None,
                             "config": {}}}

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        if "behavioral_params FROM entity_registry" in sql:
            return {"behavioral_params": json.dumps(self.bp)}
        if "FROM projects" in sql:
            return self.projects.get(int(args[0]))
        return None

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        if "FROM vault_envelope" in sql:
            return VAULT.get(args[1], {}).get("type")
        if "code_approvals_announced" in sql and "RETURNING id" in sql:
            return 1
        return None

    async def execute(self, sql, *args):
        self.sql.append((sql, args))
        if "UPDATE entity_registry SET behavioral_params" in sql:
            if " - $2::text" in sql:
                self.bp.pop(args[1], None)
            else:
                self.bp[args[1][0]] = json.loads(args[2])
        elif sql.startswith("UPDATE projects SET repo"):
            self.projects[int(args[1])]["repo"] = args[0]
        return "UPDATE 1"


VAULT: dict = {}


# The fakes keep the vault's own gate (vault/api.is_sign_in_name), so a
# connect or start that did not say it is the git server's own code would
# find nothing written and nothing to read.
async def _store_secret(pool, *, entity_id, name, cred_type, plaintext, actor=None, **kw):
    from server.vault import api as vault_api
    if vault_api.is_sign_in_name(name) and not kw.get("sign_in"):
        raise vault_api.SignInName(name)
    VAULT[name] = {"type": cred_type, "value": plaintext}
    return "env-1"


async def _open_secret(pool, *, entity_id, name, actor=None, **kw):
    from server.vault import api as vault_api
    if vault_api.is_sign_in_name(name) and not kw.get("sign_in"):
        return None
    got = VAULT.get(name)
    return got["value"] if got else None


async def _revoke_secret(pool, *, entity_id, name, actor=None):
    return VAULT.pop(name, None) is not None


async def status_of(coro):
    from fastapi import HTTPException
    from server import git_server as gs
    try:
        await coro
    except gs.GitServerError as e:
        return e.status
    except HTTPException as e:
        return e.status_code
    return 200


def cred_file():
    return HOME / ".git-credentials"


def git_get(key):
    r = subprocess.run(["git", "config", "--file", str(HOME / ".gitconfig"), "--get", key],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


async def main():
    from server import git_server as gs
    from server.routes import gitea_client as gc
    from server.vault import api as vault_api
    gc.gitea_proxy = lambda: None          # straight to the loopback server
    real_store, real_open = vault_api.store_secret, vault_api.open_secret
    vault_api.store_secret, vault_api.open_secret = _store_secret, _open_secret
    vault_api.revoke_secret = _revoke_secret
    pool = FakePool()

    # 1 — refusals store nothing
    async def refused(url, user, token, why):
        before = (dict(VAULT), json.dumps(pool.bp))
        st = await status_of(gs.connect(pool, 1, url=url, username=user, token=token))
        check(st in (400, 502) and (dict(VAULT), json.dumps(pool.bp)) == before
              and gs.connected() is None, f"connect refuses {why}, and keeps nothing", f"status {st}")
        return st

    await refused(f"http://{NETLOC}", HOST_USER, TOK_HOST, "a plain http address")
    await refused(URL, HOST_USER, "b" * 40, "a token the server does not accept")
    await refused(URL, HOST_USER, TOK_OTHER, "a token of another account")
    await refused(URL, HOST_USER, TOK_NOREPO, "a token that cannot create repositories")
    try:
        await gs.connect(pool, 1, url=URL, username=HOST_USER, token=TOK_OTHER)
    except gs.GitServerError as e:
        check("someone-else" in e.message and HOST_USER in e.message,
              "the refusal names whose token it is", e.message)
    # Gitea 28 and Forgejo 17 both answer 403 "required=[write:user]" to a token
    # with repository write alone: the refusal names the permission they ask for.
    try:
        await gs.connect(pool, 1, url=URL, username=HOST_USER, token=TOK_NOREPO)
    except gs.GitServerError as e:
        check("write:user" in e.message,
              "the refusal for a token that cannot create repositories names write:user", e.message)
    probes = [s for s in SEEN if s[0] == "POST" and s[1] == "/api/v1/user/repos"]
    check(probes and all((s[4] or {}).get("name") == "" for s in probes),
          "the create check creates nothing (an empty name)", str(probes))

    out = await gs.connect(pool, 1, url=URL + "/", username=HOST_USER, token=TOK_HOST,
                           person_username=PERSON)
    check(out.get("connected") and out.get("url") == URL and out.get("username") == HOST_USER
          and out.get("can_create_repos") is True and out.get("checked_at"),
          "a good token connects, and status says what the check found", str(out))

    # 2 — sealed in Keys, not in the row
    check(VAULT.get(gs.TOKEN_NAME, {}).get("value") == TOK_HOST.encode()
          and VAULT[gs.TOKEN_NAME]["type"] == gs.TOKEN_TYPE,
          "the token is sealed in Keys under its one name", str(list(VAULT)))
    row = json.dumps(pool.bp)
    check(TOK_HOST not in row and pool.bp.get(gs.SETTINGS_KEY, {}).get("url") == URL,
          "the settings row holds the address and usernames, never the token", row)
    check(not any(TOK_HOST in str(a) for s, a in pool.sql),
          "no statement the Host ran carried the token")

    # 3 — the coding-forge readers resolve to the connected server ...
    check(gc.gitea_creds() == (HOST_USER, TOK_HOST, NETLOC), "gitea_creds answers the connected server")
    from server.network import nac_harness
    check(nac_harness._forge_identity() == (HOST_USER, TOK_HOST, NETLOC),
          "the per-run harness credential is the connected server's")
    from server.routes import project_forge as pf
    async with pf.open_forge(pool, 1, pf.Forge("platform", f"{HOST_USER}/x")) as sess:
        check(sess.base == URL and sess.user == HOST_USER, "open_forge reaches the connected server",
              sess.base)
    me = await gc.gitea_request("GET", "/api/v1/user")
    check(me.get("login") == HOST_USER, "gitea_request signs in to the connected server as the Host")
    from mastercode import knowledge_toc
    check(bool(knowledge_toc._repo_re().search(f"{URL}/{HOST_USER}/notes")),
          "the repo links of the connected server are recognised")
    from server.routes import applications as A
    seen_clone = {}

    def _clone(args, **k):
        seen_clone.update(url=args[-2], token=k.get("token"))

        class R:
            returncode, stderr = 1, "stopped by the test"
        return R()
    saved_git = A._entity_git
    A._entity_git = _clone
    try:
        try:
            await A.publish_site_repo_service(repo=f"{HOST_USER}/site", slug="s")
        except Exception:  # noqa: BLE001 — the fake clone fails on purpose
            pass
    finally:
        A._entity_git = saved_git
    check(seen_clone.get("url") == f"{URL}/{HOST_USER}/site.git" and seen_clone.get("token") == TOK_HOST,
          "a site published from a repo clones it from the connected server", str(seen_clone))

    # ... and the Privatae-forge readers do not
    from server.network import nac_egress
    hosts = nac_egress._platform_hosts()
    check("forge.platform.example" in hosts and "127.0.0.1" not in hosts,
          "nac_egress's platform destinations stay Privatae's forge", str(hosts))
    for rel, needle in (("server/routes/community.py", 'os.environ.get("GITEA_USERNAME"'),
                        ("server/routes/support.py", 'os.environ.get("GITEA_USERNAME")'),
                        ("brain/mastercode/runtime/capability_propose.py", "gitea_user")):
        src = (ROOT / rel).read_text()
        check(needle in src and "git_server" not in src,
              f"{rel} (Privatae's forge) still names the install's identity, not the connected server")

    # 4 — the git command line's files, under the temporary HOME
    f = cred_file()
    check(f.is_file() and f.read_text().strip() == f"https://{HOST_USER}:{TOK_HOST}@{NETLOC}"
          and stat.S_IMODE(f.stat().st_mode) == 0o600,
          "~/.git-credentials holds the connected server, mode 600")
    check(git_get("user.name") == HOST_USER and git_get("user.email") == f"{HOST_USER}@mail.example"
          and git_get("credential.helper") == "store",
          "the git identity is the Host's account there",
          f"{git_get('user.name')} {git_get('user.email')}")

    # 5 — repo creation, and the person added with the Host's token
    from server.routes import projects as pj
    SEEN.clear()
    made = await pj.ensure_project_repo(pool, 1, "Garden planner")
    check(made.get("created") and made.get("repo") == f"{HOST_USER}/garden-planner-p1",
          "the project's repo is created under the Host's account", str(made))
    put = [s for s in SEEN if s[0] == "PUT"]
    check(len(put) == 1 and put[0][1].endswith(f"/collaborators/{PERSON}")
          and put[0][2] == HOST_USER and put[0][3] == "basic" and put[0][4] == {"permission": "write"},
          "the person's username is added with write access, with the Host's own token", str(put))
    check(not any(s[3] == "bearer" for s in SEEN), "no person's sign-in token was used")
    check(await pj._host_can_merge(made["repo"], "main") is True
          and await pj._host_can_merge("someone-else/theirs", "main") is False,
          "Auto-Approve's merge check asks the connected server, as the Host")

    # 6 — approve_url
    from server.routes import code_approvals as ca
    pool.projects[1]["repo"] = f"{HOST_USER}/garden-planner-p1"
    PRS[7] = {"number": 7, "title": "Add a seed list", "state": "open", "merged": False,
              "head": {"sha": "abc", "ref": "host/build"}, "base": {"ref": "main"},
              "html_url": f"{URL}/{HOST_USER}/garden-planner-p1/pulls/7", "body": ""}
    d = await ca.get_approval(pool, 1, 1, 7)
    check(d.get("approve_url") == f"{URL}/{HOST_USER}/garden-planner-p1/pulls/7"
          and d.get("approve_on") == NETLOC,
          "on a connected server Approve opens the pull request's own page there", str(d.get("approve_url")))
    PRS[7]["html_url"] = "https://elsewhere.example/x/pulls/7"
    d = await ca.get_approval(pool, 1, 1, 7)
    check(d.get("approve_url") == f"{URL}/{HOST_USER}/garden-planner-p1/pulls/7",
          "a page the server names on another address is not opened", str(d.get("approve_url")))

    # 7 — the merge, noticed and attributed from merged_by
    import types
    lines = []

    async def _post(pool_, eid, origin, text, **kw):
        lines.append(text)
        return True
    otr = types.ModuleType("mastercode.brain.offline_task_runner")
    otr.post_to_origin_thread = _post
    sys.modules["mastercode.brain.offline_task_runner"] = otr
    for num, by, want in ((8, PERSON, "You approved"), (9, HOST_USER, "The Host approved")):
        PRS[num] = {"number": num, "title": f"Change {num}", "merged": True, "state": "closed",
                    "merged_by": {"login": by}, "merged_at": "2026-10-21T10:00:00Z",
                    "base": {"ref": "main"}, "merge_commit_sha": "0123456789"}
        r = await ca.announce_merge(pool, 1, 1, num)
        check(r.get("announced") and (r.get("text") or "").startswith(want),
              f"a merge by {by} on the connected server reads \"{want}\"", str(r))

    # 8 — a person's token never goes to the connected server
    check(await status_of(gc.gitea_request("GET", "/api/v1/user", bearer="person-token")) == 409,
          "a request as the person is refused while a git server is connected")
    from server.routes import gitea as gt
    check(gt._user_bearer() is None and await gt._user_bearer_fresh() is None,
          "the Gitea app holds no person's sign-in on a connected server")

    # 9 — only Settings with the owner signed in
    from starlette.requests import Request
    from server.routes import git_server as routes

    def request(app_id):
        req = Request({"type": "http", "method": "POST", "path": "/api/git-server",
                       "headers": [(b"host", b"home.cseries.localhost")], "state": {}})
        req.state.app_token = type("T", (), {"app_id": app_id})() if app_id else None
        return req

    for app_id in (None, "workbench", "gitea", "getting-started"):
        check(await status_of(routes.owner_only(request(app_id))) == 403,
              f"the {app_id or 'token-less'} caller may not connect a git server")
    check(await status_of(routes.owner_only(request("settings"))) == 403,
          "Settings without the owner's sign-in may not")

    async def signed_in(scope):
        return "owner@example.test", "cookie"
    real_session = routes.live_session
    routes.live_session = signed_in
    try:
        check(await status_of(routes.owner_only(request("settings"))) == 200,
              "Settings with the owner signed in may")
    finally:
        routes.live_session = real_session
    from fastapi.routing import APIRoute
    deps = {tuple(sorted(r.methods)): [getattr(x.call, "_app_scope", x.call.__name__)
                                       for x in r.dependant.dependencies]
            for r in routes.router.routes if isinstance(r, APIRoute)}
    check(deps.get(("POST",)) == ["write:git_server", "owner_only"]
          and deps.get(("DELETE",)) == ["write:git_server", "owner_only"]
          and deps.get(("GET",)) == ["read:git_server"],
          "connect and disconnect take write:git_server and the owner gate", str(deps))
    from server.applications import scopes
    check(scopes.validate(["read:git_server", "write:git_server"]) == ["read:git_server", "write:git_server"],
          "both scopes are known")

    # 10 — disconnect clears everything
    out = await gs.disconnect(pool, 1)
    check(not out.get("connected") and gs.connected() is None and gs.TOKEN_NAME not in VAULT
          and gs.SETTINGS_KEY not in pool.bp,
          "disconnect removes the token from Keys, the row and the resolver's server", str(out))
    check(not cred_file().exists() and git_get("user.name") is None,
          "disconnect takes the connected server out of ~/.git-credentials and the git identity")
    check(gc.gitea_creds() != (HOST_USER, TOK_HOST, NETLOC),
          "after disconnect the resolver no longer answers the server")

    # 6, the other half — nothing connected: the platform's window, as before
    cred_file().write_text(f"https://{HOST_USER}:{TOK_HOST}@{NETLOC}\n")
    try:
        d = await ca.get_approval(pool, 1, 1, 7)
        check((d.get("approve_url") or "").startswith(ca.PLATFORM_SITE + "/approve?")
              and d.get("approve_on") is None,
              "with nothing connected Approve opens the platform's window", str(d.get("approve_url")))
    finally:
        cred_file().unlink(missing_ok=True)

    # start: the server comes back from the row and Keys
    await gs.connect(pool, 1, url=URL, username=HOST_USER, token=TOK_HOST, person_username=PERSON)
    gs._connected = None
    cred_file().unlink()
    await gs.load(pool, 1)
    check(gs.connected() is not None and gs.connected().person == PERSON
          and cred_file().read_text().strip().endswith(f"@{NETLOC}"),
          "at start the connected server comes back, and the git files are written again")

    # 11 — the token is the git server's alone: the vault holds its name, so
    # no tool, plan or other app can send it elsewhere or swap in another
    # account's token for the next start to take up unchecked.
    check(gs.TOKEN_NAME == getattr(vault_api, "GIT_SERVER_TOKEN_NAME", None)
          and vault_api.is_sign_in_name(gs.TOKEN_NAME),
          "the vault holds the git server token's name")
    from server.vault import authz, crypto, identity, store

    async def _get_envelope(pool_, *, owner_identity, logical_name, actor, include_deactivated=False):
        got = VAULT.get(logical_name)
        return {"sealed": got["value"]} if got else None

    async def _put(pool_, *, owner_identity, logical_name, cred_type, plaintext, recipient_pubkeys,
                   actor, if_absent=False):
        VAULT[logical_name] = {"type": cred_type, "value": plaintext}
        return "env-2"

    async def _key(pool_):
        return b"k"

    async def _no_audit(*a, **k):
        return None
    sent = []

    async def _http_call(secret, request, *, path_secret=False):
        # Recorded, never sent: nothing in this test leaves the machine.
        sent.append(request.get("url"))
        return {"status": 200}
    saved = (store.get_envelope, store.put, identity.host_pubkey, identity.host_privkey,
             crypto.open_envelope, authz._audit_use, authz._do_http_call)
    store.get_envelope, store.put = _get_envelope, _put
    identity.host_pubkey = identity.host_privkey = _key
    crypto.open_envelope = lambda env, priv: env["sealed"]
    authz._audit_use, authz._do_http_call = _no_audit, _http_call
    try:
        check(await real_open(pool, entity_id=1, name=gs.TOKEN_NAME) is None
              and await real_open(pool, entity_id=1, name=gs.TOKEN_NAME, sign_in=True) == TOK_HOST.encode(),
              "the token reads as absent to every reader but the git server's own")
        try:
            await real_store(pool, entity_id=1, name=gs.TOKEN_NAME, cred_type="opaque",
                             plaintext=TOK_OTHER.encode())
            check(False, "nothing else may write the token", "written")
        except vault_api.SignInName as e:
            check(VAULT[gs.TOKEN_NAME]["value"] == TOK_HOST.encode() and "Settings, Git server" in str(e),
                  "nothing else may write the token, and the refusal says where it is changed", str(e))
        for action, extra in (("http_call", {}), ("mcp_call", {"mcp_send": _no_audit})):
            try:
                await authz.use_credential(pool, entity_id=1, name=gs.TOKEN_NAME, action=action,
                                           request={"url": "https://elsewhere.example/collect"}, **extra)
                check(False, f"the broker refuses the token for {action}", "used")
            except authz.VaultAuthzError as e:
                check("git server" in str(e), f"the broker refuses the token for {action}", str(e))
        from mastercode.runtime import tools
        for op, args in (("use", {"action": "http_call", "request": {"url": "https://elsewhere.example/"}}),
                         ("add", {"value": TOK_OTHER}), ("revoke", {})):
            r = await tools._exec_keys_manage({"op": op, "name": gs.TOKEN_NAME, **args})
            check(r.get("status") == "error" and "Settings, Git server" in str(r.get("result"))
                  and VAULT[gs.TOKEN_NAME]["value"] == TOK_HOST.encode(),
                  f"the Host's keys_manage op={op} is refused the token", str(r))
        check(not sent, "no request carrying the token was made", str(sent))
    finally:
        (store.get_envelope, store.put, identity.host_pubkey, identity.host_privkey,
         crypto.open_envelope, authz._audit_use, authz._do_http_call) = saved

    # 12 — a token that left Keys is not used through the git credential file
    # either: at start, on Disconnect, and when the Keys app removes it.
    VAULT.pop(gs.TOKEN_NAME)
    gs._connected = None
    cred_file().write_text(f"https://{HOST_USER}:{TOK_HOST}@{NETLOC}\n")
    await gs.load(pool, 1)
    st = await gs.status(pool, 1)
    check(gs.connected() is None and not cred_file().exists() and gc.gitea_creds() is None
          and st.get("token_missing") is True,
          "a start whose token is gone from Keys takes it out of ~/.git-credentials too", str(st))
    cred_file().write_text(f"https://{HOST_USER}:{TOK_HOST}@{NETLOC}\n")
    await gs.disconnect(pool, 1)
    check(not cred_file().exists() and gs.SETTINGS_KEY not in pool.bp and gc.gitea_creds() is None,
          "Disconnect with no token loaded still takes the server out of ~/.git-credentials")

    from server.routes import vault as RV
    await gs.connect(pool, 1, url=URL, username=HOST_USER, token=TOK_HOST, person_username=PERSON)
    saved_eid = os.environ.get("ENTITY_ID")
    os.environ["ENTITY_ID"] = "1"
    saved_pool = RV._pool
    RV._pool = lambda: pool
    req = Request({"type": "http", "method": "DELETE", "path": "/api/vault/secrets/git-server",
                   "headers": [(b"host", b"apps-keys-home.cseries.localhost")], "state": {}})
    req.state.app_token = type("T", (), {"app_id": "keys", "permissions": ["write:vault"]})()
    try:
        await RV.revoke_secret(gs.TOKEN_NAME, req)
    finally:
        RV._pool = saved_pool
        if saved_eid is None:
            os.environ.pop("ENTITY_ID", None)
        else:
            os.environ["ENTITY_ID"] = saved_eid
    st = await gs.status(pool, 1)
    check(gs.connected() is None and not cred_file().exists() and gc.gitea_creds() is None
          and st.get("token_missing") is True,
          "revoking the token in the Keys app stops the Host using it at once", str(st))

    # 13 — the Host's own loopback and link-local addresses are refused before
    # the token goes anywhere; a LAN address and a public name are not.
    os.environ.pop("CSERIES_GIT_SERVER_ALLOW_LOOPBACK", None)
    try:
        seen_before = len(SEEN)
        for addr in (URL, "https://[::1]", "https://169.254.169.254", "https://localhost"):
            before = (dict(VAULT), json.dumps(pool.bp))
            st = await status_of(gs.connect(pool, 1, url=addr, username=HOST_USER, token=TOK_HOST))
            check(st == 400 and (dict(VAULT), json.dumps(pool.bp)) == before and gs.connected() is None,
                  f"connect refuses {addr}, an address of the Host itself or link-local", f"status {st}")
        check(len(SEEN) == seen_before, "and no request reached the server with the token",
              SEEN[seen_before:])
        lan_ok = True
        for host in ("10.20.30.40", "192.168.7.20", "[fd00:aa::50]", "git.example.org"):
            try:
                await gs._refuse_own_addresses(host)
            except gs.GitServerError as e:
                lan_ok = False
                print("   refused", host, e.message)
        check(lan_ok, "a LAN address and a public name are not refused by that rule")
    finally:
        os.environ["CSERIES_GIT_SERVER_ALLOW_LOOPBACK"] = "1"


try:
    asyncio.run(main())
finally:
    srv.shutdown()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"FAIL {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS git server connect")
