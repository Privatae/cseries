"""The person, in Chromium, against this branch's real Host: first sign-in, an authenticator code,
a passkey added on /passkey (which asks for the code, because one is set up), then the machine
pairing card over the chat approved with a passkey tap, and a second card with the code instead.
The device is Chromium's virtual authenticator (CDP WebAuthn.addVirtualAuthenticator: internal,
user verification on). Invented names only. Screenshots to argv[2]."""
import base64, hashlib, hmac, json, re, struct, subprocess, sys, time

from playwright.sync_api import sync_playwright

W, SHOTS = sys.argv[1], sys.argv[2]
BASE = "http://home.cseries.localhost:8800"
EMAIL, PASSWORD = "owner@example.com", "correct horse battery staple"
FIRST, SECOND = ("aster1a2b3c4d.cloud.example", "aster"), ("birch5e6f7a8b.cloud.example", "birch")
FAILS = []


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def check(cond, what, extra=""):
    log(("PASS " if cond else "FAIL ") + what, extra if extra else "")
    if not cond:
        FAILS.append(what)


def totp(secret, at=None):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    d = hmac.new(key, struct.pack(">Q", int((at or time.time()) // 30)), hashlib.sha1).digest()
    o = d[-1] & 15
    return "%06d" % ((struct.unpack(">I", d[o:o + 4])[0] & 0x7fffffff) % 1000000)


def next_window():
    time.sleep(31 - time.time() % 30)


def beat(*args):
    cmd = open(f"{W}/beat.cmd").read() + " " + " ".join(args)
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd="/app")
    if out.returncode:
        log("beat failed:", out.stderr[-2000:])
        raise SystemExit(1)
    return json.loads(out.stdout.strip().splitlines()[-1])


def fetch(page, method, path, body=None, token=None):
    return page.evaluate(
        """async ([m, p, b, t]) => {
             const h = {'Content-Type': 'application/json'};
             if (t) h['X-Application-Token'] = t;
             const r = await fetch(p, {method: m, headers: h, body: b === null ? undefined : JSON.stringify(b),
                                      credentials: 'same-origin'});
             let j = null; try { j = await r.json(); } catch (e) {}
             return {status: r.status, body: j};
           }""", [method, path, body, token])


def circles_frame(page, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            if "/circles/" in f.url:
                return f
        time.sleep(0.5)
    return None


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    auth = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]

    code = re.findall(r"CSERIES SETUP CODE: ([0-9a-f-]+)", open(f"{W}/host.log").read())[-1]
    page.goto(BASE + "/setup")
    r = fetch(page, "POST", "/api/owner/setup", {"code": code, "email": EMAIL, "password": PASSWORD})
    check(r["status"] == 200, "first sign-in (setup)", r)
    log("cookies:", [(c["name"], c["domain"]) for c in ctx.cookies()])
    r2 = fetch(page, "GET", "/api/owner/status")
    log("status:", r2)

    # The authenticator code, set up in Network as a person does (its window's token).
    # The system apps install as the Host comes up; launch once Network is there.
    for _ in range(120):
        r = fetch(page, "POST", "/api/applications/network/launch")
        if r["status"] == 200 and (r["body"] or {}).get("token"):
            break
        if r["status"] == 403:
            break
        time.sleep(1)
    check(r["status"] == 200, "the Network window opens", r)
    nt = r["body"]["token"]
    r = fetch(page, "POST", "/api/network/nac/stepup/enroll", token=nt)
    secret = re.search(r"secret=([A-Z2-7]+)", r["body"]["otpauth_uri"]).group(1)
    r = fetch(page, "POST", "/api/network/nac/stepup/confirm", {"code": totp(secret)}, token=nt)
    check(r["status"] == 200, "an authenticator code is set up", r)

    # Adding a passkey while a code is set up takes the code too.
    next_window()
    page.goto(BASE + "/passkey")
    page.click("#add")
    page.wait_for_selector("#add-code-box:not([hidden])", timeout=15000)
    said = page.inner_text("body")
    check("needs the code from your authenticator" in said, "/passkey asks for the code before adding a passkey")
    page.screenshot(path=f"{SHOTS}/01-passkey-page-asks-for-the-code.png", full_page=True)
    page.fill("#add-code", totp(secret))
    page.click("#add")
    page.wait_for_function("document.body.innerText.includes('Passkey added')", timeout=20000)
    creds = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth})["credentials"]
    check(len(creds) == 1 and creds[0]["rpId"] == "home.cseries.localhost",
          "a passkey is added with the code, on the desktop's own name", [c["rpId"] for c in creds])
    page.screenshot(path=f"{SHOTS}/02-passkey-added.png", full_page=True)

    # Another Host asks to use a machine this Host is in charge of: the real heartbeat path files the card.
    log("beat:", beat("file", *FIRST))
    # Circles as the desktop opens it: the window's launch token, stamped on its own-origin fetches
    # exactly as the shell's shim does (dashboard/base/js/app-runner.js installTokenShim). The
    # desktop shell itself is left out: in this offline namespace it waits on its loading screen.
    r = fetch(page, "POST", "/api/applications/circles/launch")
    check(r["status"] == 200, "the Circles window opens", r["status"])
    ct = r["body"]["token"]
    cpage = ctx.new_page()
    cpage.add_init_script("""(() => { const orig = window.fetch.bind(window); const t = %s;
      window.fetch = (input, init) => { let same = true;
        try { same = new URL((input && input.url) || String(input), location.href).origin === location.origin; }
        catch (e) { same = false; }
        if (!same) return orig(input, init);
        init = init || {}; const h = new Headers(init.headers || {});
        if (!h.has('X-Application-Token')) h.set('X-Application-Token', t);
        init.headers = h; return orig(input, init); }; })();""" % json.dumps(ct))
    ccdp = ctx.new_cdp_session(cpage)
    ccdp.send("WebAuthn.enable")
    # The same device: the passkey made on /passkey, carried over by its private key.
    cred = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth})["credentials"][0]
    auth = ccdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
    ccdp.send("WebAuthn.addCredential", {"authenticatorId": auth, "credential": cred})
    cdp = ccdp
    creds = [cred]
    page, fr = cpage, cpage
    cpage.goto(BASE + "/system-apps/circles/index.html")
    check(True, "Circles is open on the desktop's own address")
    card = fr.wait_for_selector("[data-ask-ticket]", timeout=60000)
    text = card.inner_text()
    check("aster wants to use studio-desktop" in text.lower(), "the card is up over the chat", text[:160])
    check("with your passkey" in text and "Use a code instead" in text,
          "it asks for the passkey, the code behind 'Use a code instead'")
    check(fr.locator("input[aria-label='Authenticator code']").count() == 0, "and no code box until asked")
    # The person unticks the terminal: the tap approves exactly what is ticked.
    fr.click("[data-ask-choice='session']")
    page.screenshot(path=f"{SHOTS}/04-card-asks-for-the-passkey.png", full_page=True)
    before = creds[0]["signCount"]
    page.on("console", lambda m: log("console:", m.type, m.text[:300]))
    fr.click("[data-ask-option='let_in']")
    try:
        fr.wait_for_selector("[data-ask-ticket]", state="detached", timeout=30000)
    except Exception:
        page.screenshot(path=f"{SHOTS}/05-stalled.png", full_page=True)
        log("card says:", fr.inner_text("[data-ask-ticket]")[-600:])
        raise
    page.wait_for_timeout(1000)
    page.screenshot(path=f"{SHOTS}/05-let-in-with-a-tap.png", full_page=True)
    after = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth})["credentials"][0]["signCount"]
    got = beat("check", FIRST[0])
    caps = got["grant"]["caps"] if got["grant"] else None
    caps = json.loads(caps) if isinstance(caps, str) else caps
    check(after > before, "the device signed (its counter moved)", (before, after))
    check(got["grant"] is not None and "session" not in caps and "observe" in caps
          and got["grant"]["approved_by"] == "user",
          "the Host was let in with exactly what was ticked, as the person", got)
    check(got["ticket"]["status"] == "decided", "the card is decided", got["ticket"])

    # A second card, answered with the code instead.
    log("beat:", beat("file", *SECOND))
    page.reload()
    fr = page
    card = fr.wait_for_selector("[data-ask-ticket]", timeout=90000)
    check("birch wants to use studio-desktop" in card.inner_text().lower(), "a second card is up")
    fr.click("[data-ask-use-code]")
    next_window()
    fr.fill("input[aria-label='Authenticator code']", totp(secret))
    page.screenshot(path=f"{SHOTS}/06-second-card-with-the-code.png", full_page=True)
    fr.click("[data-ask-option='let_in']")
    fr.wait_for_selector("[data-ask-ticket]", state="detached", timeout=30000)
    got = beat("check", SECOND[0])
    check(got["grant"] is not None and got["ticket"]["status"] == "decided",
          "the code still lets a Host in, as before", got)
    page.screenshot(path=f"{SHOTS}/07-after-the-code.png", full_page=True)
    browser.close()

print("RESULT", "PASS" if not FAILS else "FAIL " + "; ".join(FAILS), flush=True)
sys.exit(1 if FAILS else 0)
