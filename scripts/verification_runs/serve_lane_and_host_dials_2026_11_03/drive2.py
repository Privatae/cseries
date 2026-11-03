"""Run in the home namespace: the person signs in, adds the server with the code, then the lanes."""
import base64, hashlib, hmac, http.cookiejar, json, os, re, struct, sys, time, urllib.parse, urllib.request, urllib.error
BASE = "http://127.0.0.1:8800"
W = sys.argv[1]
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def log(*a): print(time.strftime("%H:%M:%S"), "host-side", *a, flush=True)
def call(method, path, body=None, token=None, timeout=90):
    h = {"Origin": BASE, "Content-Type": "application/json", "Accept": "application/json"}
    if token: h["X-Application-Token"] = token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        r = op.open(req, timeout=timeout); raw = r.read()
        try: return r.status, json.loads(raw or b"null")
        except Exception: return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw[:300]
def wait_file(name, t=300):
    end = time.time() + t
    while not os.path.exists(os.path.join(W, name)):
        if time.time() > end: raise SystemExit(f"timeout waiting for {name}")
        time.sleep(0.3)
def totp(secret, at=None):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    c = int((at or time.time()) // 30)
    d = hmac.new(key, struct.pack(">Q", c), hashlib.sha1).digest()
    o = d[-1] & 15
    return "%06d" % ((struct.unpack(">I", d[o:o + 4])[0] & 0x7fffffff) % 1000000)

mode = sys.argv[2] if len(sys.argv) > 2 else "all"
if mode in ("all", "pair"):
    if not os.path.exists(W + "/net.token"):
        code = re.findall(r"CSERIES SETUP CODE: ([0-9a-f-]+)", open(W + "/host.log").read())[-1]
        s, b = call("POST", "/api/owner/setup", {"code": code, "email": "owner@example.com", "password": "correct horse battery staple"})
        log("first sign-in:", s); assert s == 200, b
    else:
        s, b = call("POST", "/api/owner/login", {"email": "owner@example.com", "password": "correct horse battery staple"})
        log("sign-in:", s)
    s, b = call("POST", "/api/applications/network/launch"); nt = b["token"]; open(W + "/net.token", "w").write(nt)
    wait_file("WINDOW")
    pcode = open(W + "/CODE").read().strip()
    s, b = call("POST", "/api/network/servers/pair", {"address": "203.0.113.1", "code": "WRONG-" + pcode[6:]}, token=nt)
    log("Add a server with a wrong code ->", s, b.get("detail") if isinstance(b, dict) else b)
    time.sleep(2.5)   # the window waits after a wrong code
    s, b = call("POST", "/api/network/servers/pair", {"address": "203.0.113.1", "code": pcode}, token=nt)
    log("Add a server, 203.0.113.1 and the code ->", s); print(json.dumps(b, indent=1), flush=True)
    open(W + "/PAIRED", "w").write(json.dumps(b))
    if s != 200: raise SystemExit(1)
if mode in ("all", "lanes"):
    nt = open(W + "/net.token").read().strip()
    wait_file("RUNNING")
    tid = json.loads(open(W + "/PAIRED").read())["token_id"]
    end = time.time() + 120; row = None
    while time.time() < end:
        s, b = call("GET", "/api/network/bastions", token=nt)
        rows = (b or {}).get("bastions") if isinstance(b, dict) else None
        row = next((r for r in rows or [] if r.get("id") == tid), None)
        if row and row.get("online"): break
        time.sleep(2)
    log("the server in Network:", json.dumps({k: row.get(k) for k in ("id", "hostname", "online", "last_seen_at", "llm_models", "capabilities")} if row else None))
    server = row["hostname"]
    s, b = call("POST", "/api/network/nac/stepup/enroll", token=nt)
    secret = urllib.parse.parse_qs(urllib.parse.urlparse(b["otpauth_uri"]).query)["secret"][0]
    s, b = call("POST", "/api/network/nac/stepup/confirm", {"code": totp(secret)}, token=nt)
    log("authenticator enrolled:", s, b if s != 200 else "")
    s, b = call("POST", "/api/network/nac/mgmt/grant/propose", {"server": server, "ops": ["uptime"], "note": "e2e"}, token=nt)
    log("grant proposed:", s, json.dumps(b)[:200]); gid = b.get("grant_id")
    time.sleep(31 - time.time() % 30)   # a fresh code: the one above is burned
    s, b = call("POST", f"/api/network/nac/mgmt/grant/{gid}/approve", {"stepup_code": totp(secret)}, token=nt)
    log("grant approved with the authenticator code:", s, json.dumps(b)[:300])
    s, b = call("POST", "/api/network/mgmt/run", {"server": server, "op": "uptime"}, token=nt)
    log("uptime on the server under that grant, over the Host's connections:", s, json.dumps(b)[:400])
    s, b = call("POST", "/api/applications/circles/launch"); ct = b["token"]
    # Not the person's own machine by the rule (no anchor names this Host), so its model is not
    # picked by default; the person chooses it for this thread, as the Orchestrator would.
    s, b = call("POST", "/api/circles/messages", {"target_kind": "self", "body": "Say hello in one short sentence.",
                                                  "model": {"provider": "local", "model_id": "standin-llama-1b"}}, token=ct, timeout=180)
    reply = re.findall(r'^0:(".*")$', b if isinstance(b, str) else "", re.M)
    prov = re.findall(r'"model": "([^"]+)", "provider": "([^"]+)"', b if isinstance(b, str) else "")
    log("chat, no key:", s, "reply:", [json.loads(x) for x in reply], "model/provider:", prov[:1])
if mode == "chat_pair":
    # The person asks their Host in chat to connect to the server with a new window's code; the
    # model (the server's own, chosen for this thread) calls manage_bastion add_server; the card
    # comes up; the person presses Connect (the route Circles uses); the server's words come back.
    s, b = call("POST", "/api/owner/login", {"email": "owner@example.com", "password": "correct horse battery staple"})
    log("sign-in:", s)
    wait_file("WINDOW2")
    pcode = open(W + "/CODE2").read().strip()
    s, b = call("POST", "/api/applications/circles/launch"); ct = b["token"]
    msg = f"Connect my Host to the server 203.0.113.1, the code is {pcode}"
    s, b = call("POST", "/api/circles/messages", {"target_kind": "self", "body": msg,
                                                  "model": {"provider": "local", "model_id": "standin-llama-1b"}},
                token=ct, timeout=180)
    reply = re.findall(r'^0:(".*")$', b if isinstance(b, str) else "", re.M)
    log("chat:", s, "the Host said:", "".join(json.loads(x) for x in reply)[:300])
    s, b = call("GET", "/api/tickets?status=open", token=ct)
    tickets = b.get("tickets") if isinstance(b, dict) else b
    card = next((t for t in tickets or [] if (t.get("metadata") or {}).get("kind") == "server_pair"), None)
    log("the card over the chat:", json.dumps({"id": card and card["id"], "sitrep": card and card.get("sitrep"),
                                                "options": card and [o["label"] for o in card.get("options", [])]}))
    s, b = call("POST", f"/api/tickets/{card['id']}/decide", {"option_id": "connect"}, token=ct, timeout=120)
    log("the person pressed Connect:", s, json.dumps(b)[:600])
    open(W + "/PAIRED2", "w").write(json.dumps(b))
if mode == "chat_pair_named":
    # As chat_pair, with the Host's model a model server the person named on their home network
    # (the Orchestrator's "Model servers on your network"), so no key and no model chosen.
    s, b = call("POST", "/api/owner/login", {"email": "owner@example.com", "password": "correct horse battery staple"})
    s, b = call("POST", "/api/applications/getting-started/launch"); ot = b["token"]
    s, b = call("PUT", "/api/inference/local-endpoints", {"endpoints": ["http://192.168.1.1:8080"]}, token=ot)
    log("the person names a model server on their home network (192.168.1.1:8080):", s, json.dumps(b)[:200])
    wait_file("WINDOW2")
    pcode = open(W + "/CODE2").read().strip()
    s, b = call("POST", "/api/applications/circles/launch"); ct = b["token"]
    msg = f"Connect my Host to the server 203.0.113.1, the code is {pcode}"
    s, b = call("POST", "/api/circles/messages", {"target_kind": "self", "body": msg}, token=ct, timeout=180)
    reply = re.findall(r'^0:(".*")$', b if isinstance(b, str) else "", re.M)
    prov = re.findall(r'"model": "([^"]+)", "provider": "([^"]+)"', b if isinstance(b, str) else "")
    log("chat (no key, no model chosen):", s, "the Host said:", "".join(json.loads(x) for x in reply)[:300], "model/provider:", prov[:1])
    s, b = call("GET", "/api/tickets?status=open", token=ct)
    tickets = b.get("tickets") if isinstance(b, dict) else b
    cards = [t for t in tickets or [] if (t.get("metadata") or {}).get("kind") == "server_pair"]
    card = max(cards, key=lambda t: t["id"]) if cards else None
    log("the card over the chat:", json.dumps({"id": card and card["id"], "sitrep": card and card.get("sitrep", "")[:120],
                                                "options": card and [o["label"] for o in card.get("options", [])]}))
    s, b = call("POST", f"/api/tickets/{card['id']}/decide", {"option_id": "connect"}, token=ct, timeout=120)
    log("the person pressed Connect:", s, json.dumps(b)[:700])
    open(W + "/PAIRED2", "w").write(json.dumps(b))
