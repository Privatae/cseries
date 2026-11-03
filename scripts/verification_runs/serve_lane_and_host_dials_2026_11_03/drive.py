"""Run inside the namespace: first sign-in, Getting Started's offer, a chat on the self thread."""
import http.cookiejar, json, re, sys, time, urllib.request, urllib.error
BASE = "http://127.0.0.1:8800"
W = sys.argv[1]
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def call(method, path, body=None, token=None, timeout=60):
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
log = open(W + "/host.log").read()
code = re.findall(r"CSERIES SETUP CODE: ([0-9a-f-]+)", log)[-1]
s, b = call("POST", "/api/owner/setup", {"code": code, "email": "owner@example.com", "password": "correct horse battery staple"})
print("first sign-in (owner setup):", s)
assert s == 200, b
s, b = call("POST", "/api/applications/getting-started/launch")
print("launch getting-started:", s); gs = b.get("token") if isinstance(b, dict) else None
s, b = call("GET", "/api/inference/local-default", token=gs)
print("Getting Started's offer, GET /api/inference/local-default:", s, json.dumps(b))
s, b = call("POST", "/api/applications/circles/launch")
print("launch circles:", s); ct = b.get("token") if isinstance(b, dict) else None
t0 = time.time()
s, b = call("POST", "/api/circles/messages", {"target_kind": "self", "body": "Say hello in one short sentence."}, token=ct, timeout=180)
print(f"POST /api/circles/messages (self): {s} in {time.time()-t0:.1f}s")
print(b if isinstance(b, str) else json.dumps(b, indent=1)[:4000])
