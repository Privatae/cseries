#!/usr/bin/env python3
"""
Harness-runtime golden gate (the 5-into-1 sidecar collapse).

system-apps/{codex,cursor,grok,opencode}/bin/main.py collapsed into
thin shims over system-apps/_harness_runtime/ (harness_core.py + specs.py).
This gate proves the spec-driven core produces EXACTLY the observable outputs
the five original mains produced. Every expected value below is a LITERAL
lifted from the pre-refactor main.py files (cited per block), so the test is
self-contained: if a spec drifts from what the original hardcoded, this fails.

Per harness:
  1. /run argv for (prompt="fix the bug", project_dir="/w/x") with the
     harness's real default model (None for the four vendor CLIs — they omit
     --model; opencode prefixes or raises when unconfigured) and one override
  2. subprocess env given a fake creds value
  3. creds path + shape parsing (+ codex's ~/.codex/auth.json derivation)
  4. default_model, version argv, bootstrap URL/binary-path-or-baked
  5. contract knobs: error strings, cwd flag, status field order, /sessions
Plus: live handler checks (401/400/500/success against a stub binary), the
shared claude_creds module, and wiring (shims/Dockerfiles/claude mains).

Runs without network. Exit 0 = all pass; exit 1 + FAIL lines otherwise.
  APP_ROOT=<repo> python3 scripts/test_harness_runtime.py
"""

import inspect
import dataclasses
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[harness-runtime] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    # /app in the image; APP_ROOT=<repo> to run against the source tree.
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))          # cseries_sidecar
    sys.path.insert(0, os.path.join(app_root, "system-apps"))  # _harness_runtime

    # The specs read their env knobs at import, exactly as the originals did.
    # Clear any ambient overrides FIRST so the goldens compare against the
    # hardcoded defaults the original files carried.
    for v in ("CODEX_RELEASE_TAG", "CODEX_DOWNLOAD_URL", "CODEX_DEFAULT_MAX_MINUTES",
              "CURSOR_RELEASE_TAG", "CURSOR_DOWNLOAD_URL", "CURSOR_DEFAULT_MAX_MINUTES",
              "GROK_CHANNEL_URL", "GROK_RELEASE_TAG", "GROK_DEFAULT_MAX_MINUTES",
              "GROK_RUN_FLAG", "OPENCODE_API_KEY_PATH", "OPENCODE_DEFAULT_MODEL",
              "OPENCODE_DEFAULT_MAX_MINUTES"):
        os.environ.pop(v, None)

    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import claude_creds as cc

    tmp = Path(tempfile.mkdtemp(prefix="harness-golden-"))

    def wfile(name: str, text: str) -> Path:
        p = tmp / name
        p.write_text(text)
        return p

    class FakeRequest:
        def __init__(self, body: bytes):
            self.body = body

    PROMPT, PDIR = "fix the bug", "/w/x"

    # ══ cross-harness table (each literal cited from the pre-refactor mains) ══
    check("workspace root shared by every spec (main.py: codex:64 cursor:52 grok:57 opencode:33)",
          all(str(s.workspace) == "/var/lib/cseries/projects" for s in specs.ALL_SPECS.values()))
    check("default max_minutes 10 everywhere (codex:130 cursor:67 grok:72 opencode:42)",
          all(s.default_max_minutes == 10 for s in specs.ALL_SPECS.values()))
    check("subprocess cwd flags (codex:307-313 no-cwd/--cd, cursor:224 cwd, "
          "grok:262 cwd, opencode:117 cwd, "
          "kimi cwd — the binary's root work dir IS the cwd)",
          {i: s.run_cwd for i, s in specs.ALL_SPECS.items()} ==
          {"codex": False, "cursor": True, "grok": True,
           "opencode": True, "kimi": True, "antigravity": True})
    check("only grok lets non-timeout /run errors propagate (grok:251-276 had no generic except)",
          {i: s.catch_run_errors for i, s in specs.ALL_SPECS.items()} ==
          {"codex": True, "cursor": True, "grok": False,
           "opencode": True, "kimi": True, "antigravity": True})
    check("/sessions envelope for all five (incl. grok — normalized shape, grok:279-281)",
          all(hc.make_sessions(s)() == {"sessions": []} for s in specs.ALL_SPECS.values()))
    check("/health per harness (codex:236-238 cursor:165-167 grok:189-191 opencode:61-63)",
          all(hc.make_health(s)() == {"ok": True, "app_id": i}
              for i, s in specs.ALL_SPECS.items()))
    # The stdlib sidecar dispatches on co_argcount: /run takes the request,
    # /health//status//sessions take none (cli/cseries_sidecar.py:240-241).
    check("handler arities match the dispatcher contract",
          all(hc.make_run(s).__code__.co_argcount == 1
              and hc.make_health(s).__code__.co_argcount == 0
              and hc.make_status(s).__code__.co_argcount == 0
              and hc.make_sessions(s).__code__.co_argcount == 0
              for s in specs.ALL_SPECS.values()))

    # ═════════════════════════════ codex ════════════════════════════════
    CX = specs.CODEX
    check("codex binary path (codex:59-61)",
          str(CX.binary) == "/var/lib/cseries/apps/codex/bin/codex")
    check("codex creds path+shape (codex:62, :198-216 oauth expires_at json)",
          str(CX.creds_path) == "/home/privatae/.codex/credentials.json"
          and CX.creds_shape == "oauth_json")
    # A vendor `codex login` writes ~/.codex/auth.json, NOT credentials.json.
    # Without alt_auth_path the status read saw only credentials.json and
    # reported not-connected right after a successful login, so /run 401'd on a
    # live grant. Pin both halves: the spec carries the path, and _creds_state
    # honours it for the oauth_json shape.
    check("codex alt_auth_path = the CLI's own session file",
          str(CX.alt_auth_path) == "/home/privatae/.codex/auth.json")
    _cx_auth = tmp / "cx-live-auth.json"
    _cx_missing = tmp / "cx-absent-creds.json"
    _cx_spec = dataclasses.replace(CX, creds_path=_cx_missing, alt_auth_path=_cx_auth)
    check("codex: no creds AND no auth.json -> not connected",
          hc._creds_state(_cx_spec).get("valid") is not True)
    _cx_auth.write_text(json.dumps({"tokens": {"access_token": "at-live"}}))
    _st = hc._creds_state(_cx_spec)
    check("codex: vendor login wrote auth.json -> CONNECTED (the 401 bug)",
          _st.get("valid") is True and _st.get("present") is True
          and _st.get("auth_mode") == "oauth", json.dumps(_st)[:160])
    check("codex: run_auth_ok now passes on that state (gates /run)",
          CX.run_auth_ok(CX, _st) is True)
    # ── vendor device-code login: URL + ONE-TIME CODE + grant read-back ──
    # (the reader once stopped at the URL, so codex's code — printed
    # three lines later — never reached the user; and the shared runtime had
    # no /login/status, so a completed grant died with the pod.) The stub
    # prints the vendor's exact banner (ANSI included, captured from
    # rust-v0.135.0), then blocks until a trigger file appears, then writes
    # auth.json the way `codex login` does and exits 0.
    _cx_home = tmp / "cx-login-home"
    _cx_home.mkdir()
    _cx_trigger = tmp / "cx-login-go"
    _cx_vendor_auth = _cx_home / "auth.json"
    _cx_login_bin = tmp / "cx-login-stub"
    _cx_login_bin.write_text(
        "#!/bin/sh\n"
        "printf 'Welcome to Codex [v\\033[90m0.135.0\\033[0m]\\n'\n"
        "printf '\\033[90mOpenAI'\"'\"'s command-line coding agent\\033[0m\\n\\n'\n"
        "printf 'Follow these steps to sign in with ChatGPT using device code authorization:\\n\\n'\n"
        "printf '1. Open this link in your browser and sign in to your account\\n'\n"
        "printf '   \\033[94mhttps://auth.openai.com/codex/device\\033[0m\\n\\n'\n"
        "printf '2. Enter this one-time code \\033[90m(expires in 15 minutes)\\033[0m\\n'\n"
        "printf '   \\033[94m2N41-F3AX9\\033[0m\\n\\n'\n"
        "printf '\\033[90mDevice codes are a common phishing target. Never share this code.\\033[0m\\n\\n'\n"
        f"while [ ! -e {_cx_trigger} ]; do sleep 0.1; done\n"
        f"if [ \"$(cat {_cx_trigger})\" = fail ]; then exit 3; fi\n"
        f"printf '%s' '{{\"OPENAI_API_KEY\": null, \"tokens\": {{\"id_token\": \"idt\", "
        f"\"access_token\": \"acc\", \"refresh_token\": \"ref\", \"account_id\": \"acct\"}}, "
        f"\"last_refresh\": \"x\"}}' > {_cx_vendor_auth}\n"
        "printf 'Successfully logged in\\n'\n"
        "exit 0\n")
    _cx_login_bin.chmod(0o755)
    _cx_login_spec = dataclasses.replace(
        CX, binary=_cx_login_bin, baked=True, workspace=tmp,
        creds_path=_cx_home / "credentials.json", alt_auth_path=_cx_vendor_auth)
    check("login handlers match the dispatcher arity contract (POST takes request, GET none)",
          hc.make_login(CX).__code__.co_argcount == 1
          and hc.make_login_status(CX).__code__.co_argcount == 0)
    _lo, _lc = hc.make_login(_cx_login_spec)(FakeRequest(b'{"wait_seconds": 10}'))
    check("codex /login: URL read back WITHOUT the vendor's ANSI tail",
          _lc == 200 and _lo.get("url") == "https://auth.openai.com/codex/device",
          json.dumps(_lo)[:300])
    check("codex /login: the ONE-TIME CODE rides back as user_code",
          _lo.get("user_code") == "2N41-F3AX9", json.dumps(_lo)[:300])
    check("codex /login: still waiting on the browser -> ok, done=False, no grant yet",
          _lo.get("ok") is True and _lo.get("done") is False
          and "rotated_creds" not in _lo and not _lo.get("needs_code"))
    _ls = hc.make_login_status(_cx_login_spec)()
    check("codex /login/status while the vendor waits: done=False, login_waiting=True",
          _ls.get("ok") is True and _ls.get("done") is False
          and _ls.get("login_waiting") is True, json.dumps(_ls))
    _cx_trigger.write_text("ok")
    import time as _time
    for _ in range(50):                       # the stub writes auth.json + exits
        if hc._LOGIN.get("proc") is not None and hc._LOGIN["proc"].poll() is not None:
            break
        _time.sleep(0.1)
    _ls = hc.make_login_status(_cx_login_spec)()
    _exp_flat = {"id_token": "idt", "access_token": "acc", "refresh_token": "ref",
                 "token_type": "Bearer"}
    check("codex /login/status after the vendor finished: done=True + the grant",
          _ls.get("ok") is True and _ls.get("done") is True
          and isinstance(_ls.get("rotated_creds"), dict), json.dumps(_ls)[:300])
    check("codex grant exported as the FLAT credentials.json the entity stores "
          "(auth.json tokens.* -> top-level; _ensure_codex_auth_json re-derives)",
          list((_ls.get("rotated_creds") or {}).keys()) == ["credentials.json"]
          and json.loads(_ls["rotated_creds"]["credentials.json"]) == _exp_flat,
          json.dumps(_ls.get("rotated_creds"))[:300])
    _ls2 = hc.make_login_status(_cx_login_spec)()
    check("codex /login/status reports the grant ONCE, then 'no login in flight'",
          _ls2.get("done") is False and _ls2.get("login_waiting") is False)
    # the export helper on its own: nested, flat, absent, no access_token
    _cx_vendor_auth.write_text(json.dumps({"id_token": "a", "access_token": "b",
                                           "refresh_token": "c"}))
    check("codex login export accepts a FLAT auth.json too",
          json.loads(specs._codex_login_export(_cx_login_spec)["credentials.json"])
          == {"id_token": "a", "access_token": "b", "refresh_token": "c",
              "token_type": "Bearer"})
    _cx_vendor_auth.write_text(json.dumps({"tokens": {"id_token": "only"}}))
    check("codex login export: no access_token -> {} (nothing to persist)",
          specs._codex_login_export(_cx_login_spec) == {})
    _cx_vendor_auth.unlink()
    check("codex login export: no auth.json -> {}",
          specs._codex_login_export(_cx_login_spec) == {})
    # vendor exits WITHOUT a grant -> status says so, never reports done+ok
    _cx_trigger.unlink()
    _lo, _lc = hc.make_login(_cx_login_spec)(FakeRequest(b'{"wait_seconds": 10}'))
    check("codex /login (2nd): a fresh login replaces the held one",
          _lc == 200 and _lo.get("user_code") == "2N41-F3AX9")
    _cx_trigger.write_text("fail")
    for _ in range(50):
        if hc._LOGIN.get("proc") is not None and hc._LOGIN["proc"].poll() is not None:
            break
        _time.sleep(0.1)
    _ls = hc.make_login_status(_cx_login_spec)()
    check("codex /login/status: vendor exited without a grant -> ok=False, done=True, exit_code",
          _ls.get("ok") is False and _ls.get("done") is True
          and _ls.get("exit_code") == 3 and "rotated_creds" not in _ls, json.dumps(_ls)[:300])
    # a spec WITHOUT login_code_re keeps the old contract: answer at the URL,
    # no user_code (grok prints no code for the user to type)
    _cx_trigger.unlink()
    _nocode_spec = dataclasses.replace(_cx_login_spec, login_code_re=None)
    _lo, _lc = hc.make_login(_nocode_spec)(FakeRequest(b'{"wait_seconds": 10}'))
    check("login without login_code_re: URL only, user_code absent (grok contract)",
          _lc == 200 and _lo.get("url") == "https://auth.openai.com/codex/device"
          and "user_code" not in _lo and _lo.get("done") is False, json.dumps(_lo)[:300])
    # already-signed-in short circuit: vendor exits inside the window having
    # written the grant -> /login itself returns done=True + rotated_creds
    _cx_trigger.write_text("ok")
    _lo, _lc = hc.make_login(_cx_login_spec)(FakeRequest(b'{"wait_seconds": 10}'))
    check("login: vendor finishes inside the window with a grant -> done=True + rotated_creds",
          _lc == 200 and _lo.get("done") is True and _lo.get("ok") is True
          and json.loads((_lo.get("rotated_creds") or {}).get("credentials.json", "{}"))
          == _exp_flat, json.dumps(_lo)[:300])
    _cx_trigger.unlink()
    try:
        _cx_vendor_auth.unlink()
    except FileNotFoundError:
        pass
    # rust-v0.160.1: its catalog adds gpt-6.1-sol / gpt-6-sol /
    # gpt-6-luna and drops gpt-5.2, and its no-model banner is gpt-6.1-sol.
    # 0.154.0 added gpt-6-astra (and bannered it); 0.149.0 was the gpt-5.6
    # (-sol/-terra/-luna) floor; 0.135.0 stopped at gpt-5.5.
    check("codex release tag + download URL (codex:116-121)",
          specs.CODEX_RELEASE_TAG == "rust-v0.160.1"
          and specs.CODEX_DOWNLOAD_URL ==
          "https://github.com/openai/codex/releases/download/rust-v0.160.1/"
          "codex-x86_64-unknown-linux-musl.tar.gz"
          and str(specs.CODEX_TAG_FILE) == "/var/lib/cseries/apps/codex/bin/.codex-release-tag")
    # The code-mode host ships as its own asset from 0.149.0 and is looked for
    # beside the binary; the Dockerfile bakes it, the re-acquire path fetches it
    # from the SAME tag (asset confirmed published at 0.160.1).
    check("codex code-mode host path + URL track the release tag",
          str(specs.CODEX_HOST_BINARY) == "/var/lib/cseries/apps/codex/bin/codex-code-mode-host"
          and specs.CODEX_HOST_DOWNLOAD_URL ==
          "https://github.com/openai/codex/releases/download/rust-v0.160.1/"
          "codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz")
    check("codex version argv", hc.version_argv(CX) ==
          ["/var/lib/cseries/apps/codex/bin/codex", "--version"])
    # /run argv (codex:300-304). Real default model = None (codex:287 "codex
    # defaults if unset") -> no --model. Naming no model resolves to the CLI's
    # default for the EFFORT lookup. Since rust-v0.160.1 that default is
    # gpt-6.1-sol, and CODEX_DEFAULT_EFFORTS extends to it
    # (medium, as astra), so the platform's medium still rides a
    # run that names no model — with nothing passed the CLI would banner
    # `reasoning effort: none`.
    check("codex argv, default model (codex:300-304) — no --model, the CLI default's medium rides",
          CX.build_argv(CX, PROMPT, PDIR, None, None) ==
          ["/var/lib/cseries/apps/codex/bin/codex", "exec", "--sandbox",
           "danger-full-access", "--skip-git-repo-check", "--cd", "/w/x",
           "-c", "model_reasoning_effort=medium", "fix the bug"])
    check("codex argv, model override (codex:302-304 — --model BEFORE prompt); a model "
          "with no platform default effort passes no -c",
          CX.build_argv(CX, PROMPT, PDIR, "model-x", None) ==
          ["/var/lib/cseries/apps/codex/bin/codex", "exec", "--sandbox",
           "danger-full-access", "--skip-git-repo-check", "--cd", "/w/x",
           "--model", "model-x", "fix the bug"])
    env = CX.build_env(CX, {"valid": True})
    exp_env = dict(os.environ); exp_env["HOME"] = "/home/privatae"
    check("codex env: HOME only, no creds in env (codex:312)", env == exp_env)
    check("codex 401 message (codex:271)",
          CX.auth_error == "codex not connected — connect it on your own "
                           "surface; the platform ships no sign-in")
    check("codex 504 message (codex:328)",
          CX.timeout_error(10) == "codex exceeded 10min cap")
    check("codex reports model or 'default' (codex:320)",
          CX.report_model(None) == "default" and CX.report_model("m") == "m")
    check("codex status tail order (codex:245-251 + default_model/models 2026-08-20)",
          CX.status_tail == ("bootstrap_error", "release_tag", "default_model", "models"))
    check("codex default model is the CLI's own (banner with no --model, 0.160.1)",
          CX.default_model == "gpt-6.1-sol" == specs.CODEX_DEFAULT_MODEL)
    check("codex /status models = the spec's own catalog callable",
          CX.models is specs._codex_model_catalog)
    # creds parsing (codex:198-216)
    r = hc._read_creds(dataclasses.replace(CX, creds_path=tmp / "absent"))
    check("codex creds: missing file (codex:202-203)",
          r == {"present": False, "valid": False, "expires_at": None})
    r = hc._read_creds(dataclasses.replace(CX, creds_path=wfile("cx-bad", "not json")))
    check("codex creds: unreadable json (codex:206-208)",
          r == {"present": True, "valid": False, "expires_at": None})
    r = hc._read_creds(dataclasses.replace(CX, creds_path=wfile(
        "cx-ok", json.dumps({"access_token": "at", "expires_at": 4102444800000}))))
    check("codex creds: valid unexpired (codex:209-216)",
          r == {"present": True, "valid": True, "expires_at": 4102444800000,
                "token_type": "Bearer"})
    r = hc._read_creds(dataclasses.replace(CX, creds_path=wfile(
        "cx-exp", json.dumps({"access_token": "at", "expires_at": 1000,
                              "token_type": "bearer"}))))
    check("codex creds: expired (codex:213)",
          r == {"present": True, "valid": False, "expires_at": 1000,
                "token_type": "bearer"})
    check("codex /run gates on creds['valid'] (codex:269-271)",
          CX.run_auth_ok(CX, {"valid": True}) is True
          and CX.run_auth_ok(CX, {"valid": False}) is False)
    # auth.json derivation (codex:68-107)
    import base64
    payload = base64.urlsafe_b64encode(json.dumps(
        {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}}
    ).encode()).decode()
    idt = f"hdr.{payload}.sig"
    cx_creds = wfile("cx-derive-creds", json.dumps({
        "id_token": idt, "access_token": "at-1", "refresh_token": "rt-1"}))
    cx_auth = tmp / "cx-auth.json"
    specs._ensure_codex_auth_json(creds_path=cx_creds, auth_path=cx_auth)
    derived = json.loads(cx_auth.read_text())
    check("codex auth.json derivation fields (codex:91-100)",
          derived.get("OPENAI_API_KEY") is None
          and derived.get("tokens") == {"id_token": idt, "access_token": "at-1",
                                        "refresh_token": "rt-1",
                                        "account_id": "acct-123"}
          and "last_refresh" in derived,
          json.dumps(derived)[:200])
    check("codex auth.json mode 0600 (codex:104)",
          stat.S_IMODE(cx_auth.stat().st_mode) == 0o600)
    check("codex derive is wired into /run (codex:274)",
          CX.derive_creds is specs._ensure_codex_auth_json)
    # /run handler: 401 on expired creds (bootstrap pre-marked done; binary
    # stubbed with a silent executable so --version yields stdout "" -> None,
    # like a real not-yet-run codex would never be — the point is the creds gate)
    silent_bin = tmp / "cx-silent-bin"
    silent_bin.write_text("#!/bin/sh\nexit 0\n"); silent_bin.chmod(0o755)
    st = hc._state(CX); st.done, st.error = True, None
    cx_live = dataclasses.replace(
        CX, binary=silent_bin,
        creds_path=wfile("cx-401", json.dumps({"access_token": "a", "expires_at": 1000})))
    out = hc.make_run(cx_live)(FakeRequest(json.dumps({"prompt": "x"}).encode()))
    check("codex /run 401 on expired creds (codex:269-271)",
          out == ({"ok": False,
                   "error": "codex not connected — connect it on your own "
                            "surface; the platform ships no sign-in"}, 401),
          repr(out))
    # /status full-dict + key order (codex:241-251)
    cx_status = dataclasses.replace(
        cx_live, creds_path=wfile("cx-st", json.dumps(
            {"access_token": "a", "expires_at": 4102444800000})))
    out = hc.make_status(cx_status)()
    # default_model + models ride after the two original fields;
    # with no binary on this box `models` is the pinned snapshot (copies).
    check("codex /status dict (codex:241-251 + default_model/models)",
          out == {"present": True, "valid": True, "expires_at": 4102444800000,
                  "token_type": "Bearer", "binary_present": True,
                  "binary_version": None, "bootstrap_error": None,
                  "release_tag": "rust-v0.160.1",
                  "default_model": "gpt-6.1-sol",
                  "models": [dict(m) for m in specs.CODEX_MODELS]},
          repr(out))
    check("codex /status key order (codex:241-251, then default_model, models)",
          list(out) == ["present", "valid", "expires_at", "token_type",
                        "binary_present", "binary_version", "bootstrap_error",
                        "release_tag", "default_model", "models"])

    # ═════════════════════════════ cursor ═══════════════════════════════
    CU = specs.CURSOR
    check("cursor binary path — launcher inside dist-package (cursor:50)",
          str(CU.binary) == "/var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent")
    check("cursor creds path+shape (cursor:51, :123-139 json api_key w/ raw fallback)",
          str(CU.creds_path) == "/home/privatae/.cursor/api_key"
          and CU.creds_shape == "json_then_raw")
    check("cursor release tag + download URL (cursor:60-65)",
          specs.CURSOR_RELEASE_TAG == "2026.10.01-e373342"
          and specs.CURSOR_DOWNLOAD_URL ==
          "https://downloads.cursor.com/lab/2026.10.01-e373342/linux/x64/"
          "agent-cli-package.tar.gz")
    check("cursor version argv", hc.version_argv(CU) ==
          ["/var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent", "--version"])
    check("cursor argv, default model (cursor:213-216)",
          CU.build_argv(CU, PROMPT, PDIR, None, None) ==
          ["/var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent",
           "-p", "--force", "--output-format", "text", "fix the bug"])
    check("cursor argv, model override (cursor:214-216 — --model BEFORE prompt)",
          CU.build_argv(CU, PROMPT, PDIR, "model-x", None) ==
          ["/var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent",
           "-p", "--force", "--output-format", "text",
           "--model", "model-x", "fix the bug"])
    env = CU.build_env(CU, "fake-key")
    exp_env = dict(os.environ)
    exp_env.update({"CURSOR_API_KEY": "fake-key", "HOME": "/home/privatae"})
    check("cursor env: CURSOR_API_KEY (cursor:225)", env == exp_env)
    check("cursor 401 message (cursor:193)",
          CU.auth_error == "Cursor API key not set — add it in the Keys app")
    check("cursor 504 message (cursor:238)",
          CU.timeout_error(10) == "cursor-agent exceeded 10min cap")
    # `models` APPENDED (the two originals keep their order and
    # position): the sidecar now serves the ACCOUNT's own catalog from
    # `cursor-agent --list-models`, because the coding matrix used to derive
    # cursor's list from the OpenRouter vendor filter and offered ids Cursor
    # has never served (fan-out 3ba241d3).
    check("cursor status tail order (cursor:174-180, + models 2026-08-21)",
          CU.status_tail == ("bootstrap_error", "release_tag", "models"))
    # creds parsing (cursor:123-139)
    rd = lambda name, text: hc._read_creds(
        dataclasses.replace(CU, creds_path=wfile(name, text)))
    check("cursor creds: json envelope (cursor:131-132)",
          rd("cu-json", json.dumps({"api_key": "k1", "stored_at": 1})) == "k1")
    check("cursor creds: plain-text fallback (cursor:134-137)",
          rd("cu-raw", "rawkey\n") == "rawkey")
    check("cursor creds: json without api_key -> None (cursor:132)",
          rd("cu-nokey", json.dumps({"other": "x"})) is None)
    check("cursor creds: missing file -> None (cursor:127-128)",
          hc._read_creds(dataclasses.replace(CU, creds_path=tmp / "absent")) is None)
    check("cursor creds_state present/valid (cursor:142-147)",
          hc._creds_state(dataclasses.replace(
              CU, creds_path=wfile("cu-st", json.dumps({"api_key": "k"}))))
          == {"present": True, "valid": True}
          and hc._creds_state(dataclasses.replace(CU, creds_path=tmp / "absent"))
          == {"present": False, "valid": False})
    # /run 500 JSON on a generic error (cursor:239-241 caught it; contrast grok)
    st = hc._state(CU); st.done, st.error = True, None
    noexec = tmp / "cu-noexec"; noexec.write_text("#!"); noexec.chmod(0o644)
    cu_live = dataclasses.replace(
        CU, binary=noexec, creds_path=wfile("cu-key", "k1"))
    proj = tmp / "proj-cu"
    out = hc.make_run(cu_live)(FakeRequest(json.dumps(
        {"prompt": "x", "project_dir": str(proj)}).encode()))
    check("cursor /run catches generic errors as 500 JSON (cursor:239-241)",
          isinstance(out, tuple) and out[1] == 500 and out[0]["ok"] is False,
          repr(out))

    # ══════════════════════════════ grok ════════════════════════════════
    GK = specs.GROK
    check("grok binary path (grok:47-49)",
          str(GK.binary) == "/var/lib/cseries/apps/grok/bin/grok")
    check("grok creds paths+shape+fields (grok:55-56, :136-151)",
          str(GK.creds_path) == "/home/privatae/.grok/api_key"
          and str(GK.alt_auth_path) == "/home/privatae/.grok/auth.json"
          and GK.creds_shape == "raw_or_json_envelope"
          and GK.creds_fields == ("api_key", "token"))
    check("grok channel + download URLs (grok:63-70)",
          specs.GROK_CHANNEL_URL == "https://x.ai/cli/stable"
          and specs.GROK_BIN_URL_PRIMARY == "https://x.ai/cli"
          and specs.GROK_BIN_URL_FALLBACK ==
          "https://storage.googleapis.com/grok-build-public-artifacts/cli")
    check("grok release tag auto-resolves (env unset -> None until bootstrap, grok:67,:200-205)",
          GK.release_tag() is None)
    check("grok version argv", hc.version_argv(GK) ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--version"])
    check("grok argv, default model (grok:246-249)",
          GK.build_argv(GK, PROMPT, PDIR, None, None) ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
           "--always-approve", "-p", "fix the bug"])
    check("grok argv, model override (grok:248-249 — --model AFTER prompt)",
          GK.build_argv(GK, PROMPT, PDIR, "model-x", None) ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
           "--always-approve", "-p", "fix the bug", "--model", "model-x"])
    # Effort reaches the CLI. _grok_argv once took `effort`
    # and threw it away, so the picker could not offer a control this binary has
    # had all along. Flag order MEASURED on the pinned 0.2.112: this exact argv
    # parses (it gets as far as grok's auth error), while a bogus flag in the
    # same slot gives "unexpected argument" — trailing flags after -p's value do
    # parse, which is what makes appending safe.
    check("grok argv, effort -> --reasoning-effort (specs.GROK_EFFORTS)",
          GK.build_argv(GK, PROMPT, PDIR, "model-x", "xhigh") ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
           "--always-approve", "-p", "fix the bug", "--model", "model-x",
           "--reasoning-effort", "xhigh"])
    check("grok argv, effort normalized before the accepted-set test",
          GK.build_argv(GK, PROMPT, PDIR, None, "  HIGH  ") ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
           "--always-approve", "-p", "fix the bug", "--reasoning-effort", "high"])
    # A per-model menu id (grok's own docs name `deep`) is not canonical and is
    # DROPPED rather than forwarded — the _KIMI_EFFORTS/codex contract, whose
    # reason is the cursor-catalog bug: never invent a vendor id list client-side.
    check("grok argv, a non-canonical effort is DROPPED, not forwarded",
          GK.build_argv(GK, PROMPT, PDIR, None, "deep") ==
          ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
           "--always-approve", "-p", "fix the bug"])
    check("grok argv, no effort is unchanged (the pre-2026-08-28 shape)",
          GK.build_argv(GK, PROMPT, PDIR, None, "") ==
          GK.build_argv(GK, PROMPT, PDIR, None, None))
    os.environ["GROK_RUN_FLAG"] = "--single"
    flag_argv = GK.build_argv(GK, PROMPT, PDIR, None, None)
    os.environ.pop("GROK_RUN_FLAG", None)
    check("grok GROK_RUN_FLAG override, read per call (grok:246)",
          flag_argv == ["/var/lib/cseries/apps/grok/bin/grok", "--cwd", "/w/x",
                        "--always-approve", "--single", "fix the bug"])
    env = GK.build_env(GK, "dk-1")
    exp_env = dict(os.environ)
    exp_env.update({"HOME": "/home/privatae", "GROK_DEPLOYMENT_KEY": "dk-1"})
    check("grok env: GROK_DEPLOYMENT_KEY when key present (grok:255-257)",
          env == exp_env)
    env = GK.build_env(GK, None)
    exp_env = dict(os.environ); exp_env["HOME"] = "/home/privatae"
    check("grok env: NO key injection in oauth mode (grok:253-257)", env == exp_env)
    check("grok 401 message (grok:220-221)",
          GK.auth_error == "grok not connected — provide a GROK_DEPLOYMENT_KEY "
                           "(console.x.ai) in the Keys app, or run `grok login`")
    check("grok 504 message — its own wording (grok:276)",
          GK.timeout_error(7) == "grok run timed out after 7m")
    check("grok status tail order — release_tag BEFORE bootstrap_error (grok:198-207)",
          GK.status_tail == ("release_tag", "bootstrap_error"))
    # creds: access_token is NOT accepted (grok:147)
    rd = lambda name, text: hc._read_creds(
        dataclasses.replace(GK, creds_path=wfile(name, text)))
    check("grok creds: api_key/token only, access_token ignored (grok:147)",
          rd("gk-1", json.dumps({"api_key": "k"})) == "k"
          and rd("gk-2", json.dumps({"token": "t"})) == "t"
          and rd("gk-3", json.dumps({"access_token": "a"})) is None
          and rd("gk-4", "xai-raw\n") == "xai-raw")
    # dual auth: key OR auth.json (grok:154-170, :218-221)
    oauth_file = wfile("gk-auth.json", json.dumps({"https://auth.x.ai::u": {}}))
    gk_alt = dataclasses.replace(GK, creds_path=tmp / "absent",
                                 alt_auth_path=oauth_file)
    check("grok run_auth_ok: oauth session alone passes (grok:218-221)",
          GK.run_auth_ok(gk_alt, None) is True
          and GK.run_auth_ok(dataclasses.replace(
              gk_alt, alt_auth_path=tmp / "absent"), None) is False
          and GK.run_auth_ok(GK, "tok") is True)
    st = hc._state(GK); st.done, st.error = True, None
    out = hc.make_status(dataclasses.replace(
        gk_alt, binary=tmp / "no-grok-binary"))()
    check("grok /status dict + oauth mode (grok:164-170, :194-207)",
          out == {"present": True, "valid": True, "auth_mode": "oauth",
                  "binary_present": False, "binary_version": None,
                  "release_tag": None, "bootstrap_error": None},
          repr(out))
    check("grok /status key order (grok:198-207)",
          list(out) == ["present", "valid", "auth_mode", "binary_present",
                        "binary_version", "release_tag", "bootstrap_error"])
    # generic /run errors PROPAGATE (grok:251-276 — dispatcher sends the 500)
    noexec = tmp / "gk-noexec"; noexec.write_text("#!"); noexec.chmod(0o644)
    gk_live = dataclasses.replace(GK, binary=noexec,
                                  creds_path=wfile("gk-key", "dk"))
    try:
        hc.make_run(gk_live)(FakeRequest(json.dumps(
            {"prompt": "x", "project_dir": str(tmp / "proj-gk")}).encode()))
        check("grok /run lets generic errors propagate (grok:251-276)", False,
              "no exception raised")
    except PermissionError:
        check("grok /run lets generic errors propagate (grok:251-276)", True)

    # ════════════════════════════ opencode ══════════════════════════════
    OC = specs.OPENCODE
    check("opencode binary path (opencode:31-32)",
          str(OC.binary) == "/var/lib/cseries/apps/opencode/bin/opencode")
    check("opencode creds path+shape (opencode:36-37, :45-49)",
          str(OC.creds_path) == "/home/privatae/.opencode/api_key"
          and OC.creds_shape == "plaintext_or_empty")
    check("opencode is BAKED — no runtime acquisition (opencode:12-14, Dockerfile)",
          OC.baked is True and OC.acquire is None and OC.cache_valid is None
          and OC.release_tag is None and OC.cache_dir is None)
    # No compile-time default (no-pins doctrine): the env knob is popped
    # above, so the spec reads "" and reports None = unconfigured.
    check("opencode default model is None when unconfigured (opencode:40-41)",
          OC.default_model is None)
    check("opencode version argv + loose style (opencode:52-58)",
          hc.version_argv(OC) ==
          ["/var/lib/cseries/apps/opencode/bin/opencode", "--version"]
          and OC.version_style == "loose")
    # Unconfigured (no per-run model, no env knob) RAISES — the run must name
    # the model picked in the Orchestrator Coding section.
    try:
        OC.normalize_model(None)
        oc_none_err = "no raise"
    except ValueError as e:
        oc_none_err = str(e)
    try:
        OC.normalize_model("  ")
        oc_blank_err = "no raise"
    except ValueError as e:
        oc_blank_err = str(e)
    check("opencode model normalization (opencode:99-104)",
          "Orchestrator Coding section" in oc_none_err
          and "Orchestrator Coding section" in oc_blank_err
          and OC.normalize_model("moonshotai/kimi-k2") == "openrouter/moonshotai/kimi-k2"
          and OC.normalize_model("openrouter/x/y") == "openrouter/x/y"
          and OC.normalize_model("no-slash-slug") == "no-slash-slug")
    check("opencode argv, explicit model (opencode:109 via :99)",
          OC.build_argv(OC, PROMPT, PDIR,
                        OC.normalize_model("qwen/qwen3-coder"), None) ==
          ["/var/lib/cseries/apps/opencode/bin/opencode", "run",
           "--model", "openrouter/qwen/qwen3-coder", "fix the bug"])
    check("opencode argv, model override (opencode:109)",
          OC.build_argv(OC, PROMPT, PDIR, OC.normalize_model("moonshotai/kimi-k2"), None) ==
          ["/var/lib/cseries/apps/opencode/bin/opencode", "run",
           "--model", "openrouter/moonshotai/kimi-k2", "fix the bug"])
    env = OC.build_env(OC, "or-key-1")
    exp_env = dict(os.environ)
    exp_env.update({"HOME": "/home/privatae", "OPENROUTER_API_KEY": "or-key-1"})
    check("opencode env: OPENROUTER_API_KEY (opencode:118-119)", env == exp_env)
    check("opencode 401 message (opencode:85-86)",
          OC.auth_error == "opencode not connected — add your OpenRouter API key "
                           "in the Keys app")
    check("opencode 504 message (opencode:134)",
          OC.timeout_error(10) == "opencode exceeded 10min cap")
    check("opencode reports the normalized model, never 'default' (opencode:127)",
          OC.report_model("openrouter/a/b") == "openrouter/a/b")
    check("opencode status tail = default_model, no bootstrap fields (opencode:66-75)",
          OC.status_tail == ("default_model",))
    check("opencode creds: unreadable -> '' not None (opencode:45-49)",
          hc._read_creds(dataclasses.replace(OC, creds_path=tmp / "absent")) == ""
          and hc._read_creds(dataclasses.replace(
              OC, creds_path=wfile("oc-key", "key-1\n"))) == "key-1")
    check("opencode loose version on missing binary -> 'unknown' (opencode:52-58)",
          hc.binary_version(dataclasses.replace(
              OC, binary=tmp / "no-opencode")) == "unknown")
    # live /run handler against a stub binary — the full success contract
    oc_key = wfile("oc-live-key", "or-key-1\n")
    oc_live = dataclasses.replace(OC, binary=Path("/bin/echo"), creds_path=oc_key)
    run = hc.make_run(oc_live)
    out = run(FakeRequest(b"{nope"))
    check("opencode /run 400 bad json (opencode:88-91)",
          out == ({"ok": False, "error": "bad json"}, 400), repr(out))
    out = run(FakeRequest(json.dumps({"prompt": "  "}).encode()))
    check("opencode /run 400 prompt required (opencode:93-95)",
          out == ({"ok": False, "error": "prompt required"}, 400), repr(out))
    out = hc.make_run(dataclasses.replace(oc_live, creds_path=tmp / "absent"))(
        FakeRequest(json.dumps({"prompt": "x"}).encode()))
    check("opencode /run 401 without key (opencode:84-86)",
          out == ({"ok": False,
                   "error": "opencode not connected — add your OpenRouter API "
                            "key in the Keys app"}, 401), repr(out))
    proj = tmp / "proj-oc"
    out = run(FakeRequest(json.dumps(
        {"prompt": PROMPT, "project_dir": str(proj),
         "model": "qwen/qwen3-coder"}).encode()))
    # The last five are the per-run measurement (_harness_runtime/measure.py).
    check("opencode /run success contract keys, in order (opencode:121-132)",
          isinstance(out, dict) and list(out) ==
          ["ok", "exit_code", "transcript", "stderr", "model",
           "elapsed_seconds", "diff",
           "peak_mem_bytes", "peak_anon_bytes", "cpu_s", "wall_s",
           "measure_scope"],
          repr(out)[:200])
    check("opencode /run success values (stub binary echoes the exact argv tail)",
          isinstance(out, dict) and out.get("ok") is True and out.get("exit_code") == 0
          and out.get("transcript") == "run --model openrouter/qwen/qwen3-coder fix the bug\n"
          and out.get("model") == "openrouter/qwen/qwen3-coder"
          and out.get("stderr") == "" and isinstance(out.get("diff"), str),
          repr(out)[:300])

    # No "model" in the body and no OPENCODE_DEFAULT_MODEL (popped above):
    # normalize_model raises OUTSIDE make_run's try (harness_core.py:406-409),
    # so the handler propagates and the sidecar dispatcher answers 500 via
    # _H._fail (cli/cseries_sidecar.py) — a JSON {ok:false,error} body, NOT a
    # handler-returned tuple. It used to be send_error(500, str(e)), which put
    # the message in the reason phrase and destroyed the response whenever it
    # held a non-ASCII character.
    try:
        run(FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(proj)}).encode()))
        check("opencode /run without a model surfaces the unconfigured error",
              False, "no exception raised")
    except ValueError as e:
        check("opencode /run without a model surfaces the unconfigured error",
              "Orchestrator Coding section" in str(e), str(e))

    # ═══════════════════════════ antigravity ════════════════════════════
    # Antigravity CLI (`agy`, Google). Every literal below was MEASURED on the
    # 1.1.17 binary (help output, stderr of an unauthenticated -p run, strace
    # of its token-file probe, a clean-env keyring fallback) — see specs.py.
    AG = specs.ANTIGRAVITY
    check("antigravity binary path (baked at image build) + strict --version",
          str(AG.binary) == "/var/lib/cseries/apps/antigravity/bin/agy"
          and AG.baked is True and AG.acquire is None and AG.cache_valid is None
          and AG.version_style == "strict")
    check("antigravity creds: Keys paste = api_key (raw_or_json_envelope), vendor "
          "token file = alt_auth_path under the relocated HOME",
          str(AG.creds_path) == "/home/privatae/.antigravity/api_key"
          and AG.creds_shape == "raw_or_json_envelope"
          and AG.creds_fields == ("api_key", "key")
          and str(AG.alt_auth_path) == ("/var/lib/cseries/projects/.agy-home/.gemini/"
                                        "antigravity-cli/antigravity-oauth-token"))
    check("antigravity login is the stdin-code shape (URL out, code in), with "
          "its own login env and an export for the vendor's token file",
          AG.login_stdin_code is True and AG.login_argv is not None
          and AG.login_env is not None and AG.login_export is not None
          and AG.login_code_re is None and AG.on_start is not None)
    check("/login/code handler exists for antigravity and takes the request "
          "(dispatcher arity contract), like the claude sidecar's",
          hc.make_login_code(AG).__code__.co_argcount == 1)
    check("STREAM_FLAGS: antigravity streams with --output-format stream-json",
          specs.apply_stream_flags("antigravity", [str(AG.binary), "-p", "x"])[-2:]
          == ["--output-format", "stream-json"])

    # Relocate the whole layout into tmp with ONE replace: the helpers derive
    # every path from the spec (vendor token -> HOME, key file -> creds dir).
    ag_home = tmp / "agy-home"
    ag_creds = tmp / "agy-creds"
    ag_creds.mkdir()
    ag_vendor_tok = ag_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    ag_entity_tok = ag_creds / "antigravity-oauth-token"
    ag_key = ag_creds / "api_key"
    ag = dataclasses.replace(AG, binary=Path("/bin/echo"),
                             creds_path=ag_key, alt_auth_path=ag_vendor_tok)

    # — neither lane: 401 at the gate
    out = hc.make_run(ag)(FakeRequest(b'{"prompt": "x"}'))
    check("antigravity /run 401 with neither a key nor a token",
          out == ({"ok": False, "error": AG.auth_error}, 401), repr(out))

    # — key lane: argv + env + settings.json modelProvider=gemini
    ag_key.write_text("AIza-test-key\n")
    argv = ag.build_argv(ag, PROMPT, PDIR, "gemini-3.1-pro-high", "high")
    check("antigravity argv: -p prompt, auto-approve, long print-timeout, --model, "
          "--effort (accepted value passes)",
          argv == ["/bin/echo", "-p", PROMPT, "--dangerously-skip-permissions",
                   "--print-timeout", specs.AGY_PRINT_TIMEOUT,
                   "--model", "gemini-3.1-pro-high", "--effort", "high"], repr(argv))
    argv = ag.build_argv(ag, PROMPT, PDIR, None, "ultra")
    check("antigravity drops an effort outside low|medium|high instead of passing it",
          "--effort" not in argv and "--model" not in argv, repr(argv))
    aenv = ag.build_env(ag, hc._read_creds(ag))
    settings = json.loads((ag_home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    check("antigravity KEY lane: HOME relocated, GEMINI_API_KEY in env, settings carry "
          "modelProvider=gemini and telemetry off",
          aenv["HOME"] == str(ag_home) and aenv.get("GEMINI_API_KEY") == "AIza-test-key"
          and settings.get("modelProvider") == "gemini"
          and settings.get("telemetryEnabled") is False,
          repr((aenv.get("HOME"), aenv.get("GEMINI_API_KEY"), settings)))
    check("antigravity key envelope {\"api_key\": …} reads too",
          (ag_key.write_text('{"api_key": "AIza-env"}') or True)
          and hc._read_creds(ag) == "AIza-env")
    ag_key.write_text("AIza-test-key\n")

    # — token lane wins over the key: the entity token the pod was woken with
    #   is SEEDED into the vendor's file, GEMINI_API_KEY leaves the env, and
    #   modelProvider is REMOVED from settings
    ag_entity_tok.write_text('{"access_token":"at-1","expiry":"2099-01-01T00:00:00Z"}')
    check("antigravity run_auth_ok accepts the entity token before it is seeded "
          "(the gate runs before build_argv)",
          ag.run_auth_ok(ag, None) is True and not ag_vendor_tok.exists())
    ag.build_argv(ag, PROMPT, PDIR, None, None)
    aenv = ag.build_env(ag, hc._read_creds(ag))
    settings = json.loads((ag_home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    check("antigravity TOKEN lane: entity token seeded into the vendor file (0600), "
          "GEMINI_API_KEY NOT in env, modelProvider gone, telemetry still off",
          ag_vendor_tok.read_text() == ag_entity_tok.read_text()
          and (ag_vendor_tok.stat().st_mode & 0o777) == 0o600
          and "GEMINI_API_KEY" not in aenv
          and "modelProvider" not in settings and settings.get("telemetryEnabled") is False,
          repr((ag_vendor_tok.exists(), aenv.get("GEMINI_API_KEY"), settings)))
    ag_vendor_tok.write_text('{"access_token":"at-2-refreshed"}')
    ag.build_argv(ag, PROMPT, PDIR, None, None)
    check("antigravity seed never overwrites a warm pod's own (fresher) token",
          ag_vendor_tok.read_text() == '{"access_token":"at-2-refreshed"}')
    check("antigravity login_export = the vendor's token under its own name "
          "(what rides back as rotated_creds / the login grant)",
          ag.login_export(ag) == {"antigravity-oauth-token": '{"access_token":"at-2-refreshed"}'}
          and hc._export_creds(ag) == ag.login_export(ag))
    check("antigravity /status: valid via the vendor token (auth_mode oauth) and "
          "via the key alone",
          hc._creds_state(ag).get("valid") is True
          and hc._creds_state(ag).get("auth_mode") in ("deployment_key", "oauth"))
    check("antigravity login env: relocated HOME, never GEMINI_API_KEY",
          ag.login_env(ag)["HOME"] == str(ag_home)
          and "GEMINI_API_KEY" not in ag.login_env(ag))
    check("antigravity login argv: print-mode sign-in, and the pod's token copy "
          "is dropped first so the vendor starts a REAL login",
          ag.login_argv(ag)[:2] == ["/bin/echo", "-p"] and "--output-format" in ag.login_argv(ag)
          and not ag_vendor_tok.exists() and ag_entity_tok.exists())
    check("antigravity login_export is {} once the vendor file is gone",
          ag.login_export(ag) == {})

    # — the relay, end to end against a FAKE vendor: prints the measured
    #   stderr lines, blocks on stdin, writes the token file, then keeps
    #   running (as `agy -p` would, into its prompt). make_login must answer
    #   needs_code with the URL; make_login_code must deliver the code, see
    #   the grant land, hand it back, and REAP the still-running vendor.
    # The fake mirrors the REAL binary's measured behaviour, including the one
    # that bit: a piped stdin is "non-interactive" and the vendor exits 1 with
    # "authentication required. Run 'agy' to log in, then retry." — only a tty
    # (or /dev/null) gets the URL loop. So the fake refuses a non-tty stdin the
    # same way, and a bad code is refused at once (invalid_grant, exit 1).
    fake = tmp / "fake-agy.py"
    fake.write_text(
        "import os, sys, time\n"
        "if not sys.stdin.isatty():\n"
        "    print(\"Error: authentication required. Run 'agy' to log in, then retry.\", flush=True)\n"
        "    sys.exit(1)\n"
        "print('Authentication required. Please visit the URL to log in:')\n"
        "print('  https://accounts.google.com/o/oauth2/auth?client_id=x&state=y')\n"
        "print('Waiting for authentication (timeout 60s)...')\n"
        "print('Or, paste the authorization code here and press Enter:', flush=True)\n"
        "code = sys.stdin.readline().strip()\n"
        "if code == 'bad':\n"
        "    print('Error: authentication failed: token exchange failed: oauth2: \"invalid_grant\"', flush=True)\n"
        "    sys.exit(1)\n"
        "p = os.path.join(os.environ['HOME'], '.gemini', 'antigravity-cli', 'antigravity-oauth-token')\n"
        "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
        "open(p, 'w').write('{\"access_token\":\"tok-' + code + '\"}')\n"
        "time.sleep(20)\n")
    # The login starts in the spec's workspace; the real one exists only on a
    # machine that has run a Host, so the fake gets a folder of its own.
    (tmp / "login-workspace").mkdir(exist_ok=True)
    ag_login = dataclasses.replace(
        ag, binary=Path(sys.executable), workspace=tmp / "login-workspace",
        login_argv=lambda s: [sys.executable, str(fake)])
    hc._clear_login()
    res, code_ = hc.make_login(ag_login)(FakeRequest(b'{"wait_seconds": 10}'))
    check("antigravity /login: the vendor's Google URL comes back, needs_code=True, "
          "held (done=False), no user_code — i.e. the login got a TTY stdin, not a pipe",
          code_ == 200 and res.get("ok") is True and res.get("done") is False
          and res.get("needs_code") is True and "user_code" not in res
          and str(res.get("url", "")).startswith("https://accounts.google.com/o/oauth2/auth"),
          repr(res)[:300])
    check("antigravity /login holds the pty master for the relay",
          hc._LOGIN.get("pty") is not None and hc._LOGIN.get("proc") is not None)
    # A refused code: the vendor exits at once -> done=True, not ok, exit code
    # carried, nothing persisted, and the held login (and its pty) is cleared.
    res, code_ = hc.make_login_code(ag_login)(FakeRequest(b'{"code": "bad", "wait_seconds": 15}'))
    check("antigravity /login/code with a code the vendor refuses -> done=True, ok=False, "
          "exit code carried, no grant",
          code_ == 200 and res.get("ok") is False and res.get("done") is True
          and res.get("exit_code") == 1 and "rotated_creds" not in res
          and "invalid_grant" in (res.get("output") or ""), repr(res)[:300])
    check("refused login cleared (no proc, pty closed)",
          hc._LOGIN.get("proc") is None and hc._LOGIN.get("pty") is None)
    # The real thing: a fresh /login, then the code the user brings back.
    res, code_ = hc.make_login(ag_login)(FakeRequest(b'{"wait_seconds": 10}'))
    check("antigravity second /login after a refusal starts clean (URL + needs_code again)",
          code_ == 200 and res.get("needs_code") is True and res.get("done") is False, repr(res)[:200])
    st = hc.make_login_status(ag_login)()
    check("antigravity /login/status while waiting: login_waiting, not done",
          st.get("done") is False and st.get("login_waiting") is True, repr(st))
    bad = hc.make_login_code(ag_login)(FakeRequest(b'{"code": "a\\nb"}'))
    check("antigravity /login/code refuses a multi-line code (400)",
          bad[1] == 400, repr(bad))
    res, code_ = hc.make_login_code(ag_login)(FakeRequest(b'{"code": "4/0AbC", "wait_seconds": 15}'))
    check("antigravity /login/code: code delivered, grant landed and handed back as "
          "rotated_creds under the vendor's file name",
          code_ == 200 and res.get("ok") is True and res.get("done") is True
          and res.get("rotated_creds") == {"antigravity-oauth-token": '{"access_token":"tok-4/0AbC"}'},
          repr(res)[:300])
    check("antigravity /login/code reaped the vendor process after the grant landed "
          "(no held login left; /login/status has nothing in flight)",
          hc._LOGIN.get("proc") is None
          and hc.make_login_status(ag_login)().get("error") == "no login in flight")
    nolog = hc.make_login_code(ag_login)(FakeRequest(b'{"code": "x"}'))
    check("antigravity /login/code with no login waiting -> 409", nolog[1] == 409, repr(nolog))
    # make_login_code is registered ONLY for the stdin-code spec: every other
    # spec keeps its 404 (review._CODE_RELAY_HARNESSES pins the kernel side).
    check("only antigravity declares login_stdin_code in ALL_SPECS",
          [i for i, s in specs.ALL_SPECS.items() if s.login_stdin_code] == ["antigravity"])

    # ══════════════════════════════ kimi ════════════════════════════════
    # Kimi Code (Moonshot AI) — NEW in the runtime, so these rows pin the
    # intended contract. Every literal below was read off the 0.32.0 binary
    # (help output, config schema, env names), not from docs.
    KM = specs.KIMI
    os.environ["KIMI_HOME"] = str(tmp / "kimi-home")   # keep /var/lib out
    check("kimi binary path (baked at image build)",
          str(KM.binary) == "/var/lib/cseries/apps/kimi/bin/kimi")
    check("kimi creds path+shape (Keys secret 'harness-kimi', provider_json contract)",
          str(KM.creds_path) == "/home/privatae/.kimi-code/provider.json"
          and KM.creds_shape == "provider_json")
    check("kimi is BAKED — no runtime acquisition",
          KM.baked is True and KM.acquire is None and KM.cache_valid is None
          and KM.release_tag is None and KM.cache_dir is None)
    # THE bare-key regression: kimi must wrap to Moonshot, never OpenRouter.
    # Pointing it elsewhere silently sends a working key to an endpoint that
    # will 401 — and the ingest side (harness_keys._BARE_KEY_CANONICAL) is
    # gated on the same pairing, so the two halves must agree.
    check("kimi bare-key wrap is Moonshot",
          KM.bare_key_base_url == "https://api.moonshot.ai/v1")
    # Per-key base resolution (measured): a Kimi-for-Coding
    # SUBSCRIPTION key 401s on api.moonshot.ai and lives on the coding base —
    # kimi declares it as an alt.
    check("kimi declares the subscription alt base",
          KM.bare_key_alt_base_urls == ("https://api.kimi.com/coding/v1",))
    check("kimi model normalization: native id, moonshot prefix stripped, "
          "a custom endpoint's Vendor/Model left intact",
          KM.normalize_model("k3") == "k3"
          and KM.normalize_model("moonshotai/kimi-k2-thinking") == "kimi-k2-thinking"
          and KM.normalize_model("Qwen/Qwen3-Coder") == "Qwen/Qwen3-Coder")
    try:
        KM.normalize_model("")
        check("kimi unconfigured model raises rather than guessing", False)
    except ValueError as e:
        check("kimi unconfigured model raises rather than guessing",
              "Orchestrator Coding section" in str(e), str(e))

    # Pin the probe seam: tests NEVER hit the network. This platform-class
    # key is accepted by the default (Moonshot) base.
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = (
        lambda base, key: base == KM.bare_key_base_url)
    kp = wfile("kimi-bare", "sk-moonshot-1")
    km_live = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=kp)
    argv = KM.build_argv(km_live, PROMPT, PDIR, KM.normalize_model("k3"), "high")
    # The binary REJECTS --prompt combined with --auto/--yolo/--plan at startup
    # ("Cannot combine --prompt with --auto" — measured on the 0.32.0 AND
    # 0.34.0 binaries, and stated in the vendor's flag-conflict rules; prompt
    # mode is auto by itself). And -m outranks the KIMI_MODEL_* provider, so no
    # --model either. Bare -p is the whole one-shot contract.
    check("kimi argv is bare -p (no --auto, no --model: the binary rejects the "
          "first with -p and the second would outrank the env provider)",
          argv == ["/bin/echo", "-p", PROMPT], repr(argv))
    kenv = KM.build_env(km_live, None)
    # KIMI_MODEL_NAME is the enable switch: without it the binary ignores the
    # rest of the family and the run fails "provider has no credential".
    # max_context_size stays required (number().int().min(1) in the binary's
    # schema, caught live against 0.32.0).
    check("kimi bare key -> Moonshot provider on the KIMI_MODEL_* env family, "
          "kimi wire, model + max_context_size + accepted effort present",
          kenv.get("KIMI_MODEL_NAME") == "k3"
          and kenv.get("KIMI_MODEL_PROVIDER_TYPE") == "kimi"
          and kenv.get("KIMI_MODEL_BASE_URL") == "https://api.moonshot.ai/v1"
          and kenv.get("KIMI_MODEL_API_KEY") == "sk-moonshot-1"
          and kenv.get("KIMI_MODEL_MAX_CONTEXT_SIZE") == "262144"
          and kenv.get("KIMI_MODEL_THINKING_EFFORT") == "high",
          repr({k: v for k, v in kenv.items() if k.startswith("KIMI_")}))
    # THE central invariant: the key rides the env, never a file on the
    # workspace volume — the run writes NOTHING under $KIMI_HOME itself (the
    # binary creates its own empty stub config; from 0.33.0 it reads keys only
    # from config.toml or KIMI_MODEL_*, and only the latter keeps it off disk).
    _kh = tmp / "kimi-home"
    _kh_files = [p for p in _kh.rglob("*") if p.is_file()] if _kh.exists() else []
    check("kimi NEVER writes the api key (or any config) under KIMI_HOME",
          not (_kh / ".kimi-code" / "config.toml").exists()
          and not any("sk-moonshot-1" in p.read_bytes().decode("utf-8", "replace")
                      for p in _kh_files),
          repr(_kh_files))
    check("kimi env: tethers off + brand home",
          kenv["KIMI_DISABLE_TELEMETRY"] == "1"
          and kenv["KIMI_DISABLE_CRON"] == "1"
          and "KIMI_API_KEY" not in kenv
          and kenv["HOME"] == str(tmp / "kimi-home"), repr(sorted(kenv))[:200])
    # An effort the binary does not accept is DROPPED, never passed — a
    # rejected provider would lose the whole run, not just the knob.
    KM.build_argv(km_live, PROMPT, PDIR, KM.normalize_model("k3"), "medium")
    kenv = KM.build_env(km_live, None)
    check("kimi drops an unsupported effort instead of passing it",
          "KIMI_MODEL_THINKING_EFFORT" not in kenv, repr(sorted(kenv))[:200])

    # SUBSCRIPTION-class key (the measured case): the default base
    # rejects it (401), the coding alt accepts — the env must carry the alt.
    hc._BARE_KEY_BASE_CACHE.clear()
    probes = []
    def _sub_probe(base, key):
        probes.append(base)
        return base == "https://api.kimi.com/coding/v1"
    hc._BARE_KEY_BASE_PROBE = _sub_probe
    ksub = wfile("kimi-sub", "sk-kfc-subscription-1")
    km_sub = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=ksub)
    KM.build_argv(km_sub, PROMPT, PDIR, KM.normalize_model("k3"), None)
    kenv = KM.build_env(km_sub, None)
    check("kimi SUBSCRIPTION bare key wraps to the coding base, kimi wire, "
          "key rides the env",
          kenv.get("KIMI_MODEL_BASE_URL") == "https://api.kimi.com/coding/v1"
          and kenv.get("KIMI_MODEL_PROVIDER_TYPE") == "kimi"
          and kenv.get("KIMI_MODEL_API_KEY") == "sk-kfc-subscription-1",
          repr({k: v for k, v in kenv.items() if k.startswith("KIMI_")}))
    n_probes = len(probes)
    KM.build_argv(km_sub, PROMPT, PDIR, KM.normalize_model("k3"), None)
    check("kimi base resolution caches per key (no re-probe on the next run)",
          len(probes) == n_probes, repr(probes))
    # A TRANSPORT failure gives no verdict — the default base stays (a blip
    # must never strand a working platform key on the subscription base).
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda base, key: None
    kblip = wfile("kimi-blip", "sk-anyclass-1")
    km_blip = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=kblip)
    KM.build_argv(km_blip, PROMPT, PDIR, KM.normalize_model("k3"), None)
    kenv = KM.build_env(km_blip, None)
    check("kimi probe transport-failure keeps the default (Moonshot) base",
          kenv.get("KIMI_MODEL_BASE_URL") == "https://api.moonshot.ai/v1",
          repr({k: v for k, v in kenv.items() if k.startswith("KIMI_")}))
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = None
    # JSON provider spec on the openai wire -> that provider type, same family.
    kj = wfile("kimi-json", json.dumps(
        {"base_url": "https://my.gw/v1", "api_key": "sk-gw", "type": "openai",
         "max_context_size": 131072}))
    km_json = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=kj)
    KM.build_argv(km_json, PROMPT, PDIR, KM.normalize_model("Qwen/Qwen3-Coder"), None)
    kenv = KM.build_env(km_json, None)
    check("kimi JSON spec: openai wire -> provider type openai, its base, "
          "its context size, no kimi-wire leftovers",
          kenv.get("KIMI_MODEL_PROVIDER_TYPE") == "openai"
          and kenv.get("KIMI_MODEL_BASE_URL") == "https://my.gw/v1"
          and kenv.get("KIMI_MODEL_API_KEY") == "sk-gw"
          and kenv.get("KIMI_MODEL_NAME") == "Qwen/Qwen3-Coder"
          and kenv.get("KIMI_MODEL_MAX_CONTEXT_SIZE") == "131072"
          and "OPENAI_API_KEY" not in kenv and "KIMI_API_KEY" not in kenv,
          repr({k: v for k, v in kenv.items() if k.startswith("KIMI_")}))
    # An unrecognised wire falls back to the Moonshot protocol (a bare key
    # wraps to Moonshot); the binary accepts exactly kimi/openai/anthropic.
    kw = wfile("kimi-wire", json.dumps(
        {"base_url": "https://odd.gw/v1", "api_key": "sk-odd", "type": "mistral"}))
    km_wire = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=kw)
    KM.build_argv(km_wire, PROMPT, PDIR, KM.normalize_model("m1"), None)
    check("kimi unknown provider type falls back to the kimi wire",
          KM.build_env(km_wire, None).get("KIMI_MODEL_PROVIDER_TYPE") == "kimi")
    # Cross-run stash: a multi-provider run must not leak its resolved provider
    # into a later single-spec run (_KIMI_RUN_PROVIDER clears first every time).
    kmp = wfile("kimi-multi", json.dumps({"providers": {
        "moon": {"base_url": "https://api.moonshot.ai/v1", "api_key": "sk-m", "type": "kimi"},
        "gw": {"base_url": "https://my.gw/v1", "api_key": "sk-g", "type": "openai"}},
        "default": "moon"}))
    km_multi = dataclasses.replace(KM, binary=Path("/bin/echo"), creds_path=kmp)
    KM.build_argv(km_multi, PROMPT, PDIR, KM.normalize_model("gw/some-model"), None)
    kenv = KM.build_env(km_multi, None)
    check("kimi multi: slug prefix routes to that provider + its own wire",
          kenv.get("KIMI_MODEL_API_KEY") == "sk-g"
          and kenv.get("KIMI_MODEL_PROVIDER_TYPE") == "openai"
          and kenv.get("KIMI_MODEL_NAME") == "some-model", "")
    KM.build_argv(km_json, PROMPT, PDIR, KM.normalize_model("Qwen/Qwen3-Coder"), None)
    kenv = KM.build_env(km_json, None)
    check("kimi single-spec after multi: no stale stash from the multi run",
          kenv.get("KIMI_MODEL_API_KEY") == "sk-gw"
          and kenv.get("KIMI_MODEL_NAME") == "Qwen/Qwen3-Coder",
          repr(sorted(kenv))[:200])
    check("kimi 504 + report_model is always the resolved id",
          KM.timeout_error(10) == "kimi exceeded 10min cap"
          and KM.report_model("k3") == "k3")
    check("kimi stream flags append (buffered argv passes no --output-format)",
          specs.apply_stream_flags("kimi", ["kimi", "-p", "x"]) ==
          ["kimi", "-p", "x", "--output-format", "stream-json"])
    out = hc.make_run(km_live)(FakeRequest(json.dumps(
        {"prompt": PROMPT, "project_dir": str(tmp / "proj-km"),
         "model": "k3"}).encode()))
    check("kimi /run success values (stub echoes the exact argv tail)",
          isinstance(out, dict) and out.get("ok") is True
          and out.get("transcript") == "-p fix the bug\n"
          and out.get("model") == "k3", repr(out)[:300])
    out = hc.make_run(dataclasses.replace(km_live, creds_path=tmp / "absent"))(
        FakeRequest(json.dumps({"prompt": "x"}).encode()))
    check("kimi /run 401 without a provider spec",
          out == ({"ok": False, "error": KM.auth_error}, 401), repr(out))

    # ═══════════ transient-failure retry ═══════════
    # One dropped provider connection failed a whole 296 s run; the runtime now
    # re-runs ONCE, narrowly: rc>0 + connection signature in the stderr TAIL +
    # budget left. These stubs count invocations via a file.
    KIMI_CUT = ("error: failed to run prompt: provider.connection_error: "
                "Connection error.")

    def retry_stub(name: str, script: str) -> Path:
        p = tmp / name
        p.write_text("#!/bin/sh\n" + script)
        p.chmod(0o755)
        return p

    def calls(cnt: Path) -> int:
        try:
            return int(cnt.read_text().strip())
        except Exception:
            return 0

    # the signature gate itself (window + rc guards), no subprocess needed
    check("retry gate: kimi's own failure line matches; rc guards hold",
          hc._retryable_failure(1, "junk\n" + KIMI_CUT + "\nSee log: /x\n")
          and not hc._retryable_failure(0, KIMI_CUT)      # success
          and not hc._retryable_failure(-9, KIMI_CUT)     # signal-killed
          and not hc._retryable_failure(None, KIMI_CUT))
    check("retry gate: signature outside the last %d chars does NOT match"
          % hc._RETRY_SCAN_CHARS,
          not hc._retryable_failure(1, KIMI_CUT + "\n" + ("prose " * 300)))

    cnt1 = tmp / "retry-cnt-1"
    stub1 = retry_stub("retry-once-then-ok", f"""
n=$(cat {cnt1} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt1}
if [ $n -le 1 ]; then
  echo "partial work"
  echo "model reasoning noise" >&2
  echo "{KIMI_CUT}" >&2
  exit 1
fi
echo "final work done"
exit 0
""")
    km_retry = dataclasses.replace(km_live, binary=stub1)
    out = hc.make_run(km_retry)(FakeRequest(json.dumps(
        {"prompt": PROMPT, "project_dir": str(tmp / "proj-retry1"), "model": "k3"}).encode()))
    check("retry /run: transient failure re-runs once and succeeds",
          isinstance(out, dict) and out.get("ok") is True
          and out.get("retries") == 1 and calls(cnt1) == 2
          and (out.get("first_attempt") or {}).get("exit_code") == 1
          and KIMI_CUT in (out.get("first_attempt") or {}).get("stderr_tail", ""),
          repr(out)[:300])
    check("retry /run: successful second attempt carries the marker at the END",
          isinstance(out, dict)
          and out.get("stderr", "").rstrip().endswith("]")
          and "[transient retry: attempt 1 exited 1" in out.get("stderr", ""),
          repr(out.get("stderr"))[:300] if isinstance(out, dict) else repr(out))

    cnt2 = tmp / "retry-cnt-2"
    stub2 = retry_stub("fail-not-transient", f"""
n=$(cat {cnt2} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt2}
echo "error: 401 unauthorized" >&2
exit 1
""")
    out = hc.make_run(dataclasses.replace(km_live, binary=stub2))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-retry2"), "model": "k3"}).encode()))
    check("retry /run: a non-transient failure runs exactly once, no retry keys",
          isinstance(out, dict) and out.get("ok") is False
          and "retries" not in out and calls(cnt2) == 1, repr(out)[:200])

    cnt3 = tmp / "retry-cnt-3"
    stub3 = retry_stub("fail-signal", f"""
n=$(cat {cnt3} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt3}
echo "{KIMI_CUT}" >&2
kill -9 $$
""")
    out = hc.make_run(dataclasses.replace(km_live, binary=stub3))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-retry3"), "model": "k3"}).encode()))
    check("retry /run: signal-killed child (negative rc) never retries",
          isinstance(out, dict) and out.get("ok") is False
          and out.get("exit_code") == -9
          and "retries" not in out and calls(cnt3) == 1, repr(out)[:200])

    # /run has no total time budget: it runs
    # on the same watched runner as /run/stream, so there is no "leftover
    # budget" left to gate the one re-run. The transient re-run happens.
    cnt1.unlink()   # reuse stub1
    _saved_budget = hc.RUN_RETRY_MIN_BUDGET_S
    hc.RUN_RETRY_MIN_BUDGET_S = 10 ** 9
    try:
        out = hc.make_run(km_retry)(FakeRequest(json.dumps(
            {"prompt": PROMPT,
             "project_dir": str(tmp / "proj-retry4"), "model": "k3"}).encode()))
    finally:
        hc.RUN_RETRY_MIN_BUDGET_S = _saved_budget
    check("retry /run: no total budget gates the re-run (no wall-clock cap on /run)",
          isinstance(out, dict) and out.get("ok") is True
          and out.get("retries") == 1 and calls(cnt1) == 2, repr(out)[:200])

    cnt5 = tmp / "retry-cnt-5"
    stub5 = retry_stub("fail-transient-twice", f"""
n=$(cat {cnt5} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt5}
echo "{KIMI_CUT}" >&2
exit 1
""")
    out = hc.make_run(dataclasses.replace(km_live, binary=stub5))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-retry5"), "model": "k3"}).encode()))
    check("retry /run: both attempts transient -> ONE retry, marker at FRONT, "
          "failure line stays last",
          isinstance(out, dict) and out.get("ok") is False
          and out.get("retries") == 1 and calls(cnt5) == 2
          and out.get("stderr", "").startswith("[transient retry:")
          and out.get("stderr", "").rstrip().endswith(KIMI_CUT),
          repr(out)[:300])

    cnt6 = tmp / "retry-cnt-6"
    stub6 = retry_stub("stream-retry-once", f"""
n=$(cat {cnt6} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt6}
if [ $n -le 1 ]; then
  echo "attempt one line"
  echo "{KIMI_CUT}" >&2
  exit 1
fi
echo "attempt two line"
exit 0
""")
    frames = list(hc.make_run_stream(dataclasses.replace(km_live, binary=stub6))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-retry6"), "model": "k3"}).encode())))
    statuses = [f for f in frames if f.get("kind") == "status"]
    final = statuses[-1]["payload"] if statuses else {}
    seqs = [f.get("seq") for f in frames]
    check("retry /run/stream: 'retrying' frame then a complete final with "
          "retries=1 and rising seq",
          calls(cnt6) == 2 and len(statuses) == 2
          and statuses[0]["payload"].get("state") == "retrying"
          and statuses[0]["payload"].get("attempt") == 2
          and final.get("state") == "complete" and final.get("ok") is True
          and final.get("retries") == 1
          and "[harness retry: transient provider failure"
              in final.get("transcript", "")
          and "attempt two line" in final.get("transcript", "")
          and seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
          repr([s["payload"].get("state") for s in statuses]) + " " +
          repr(final)[:200])

    # ═══ /run/stream pre-flight parity with /run ═══
    # run_stream gated on credentials.json and then ran the CLI WITHOUT calling
    # spec.derive_creds — codex's auth.json was never derived on the streaming
    # path, so every streamed codex build ran unauthenticated (401 at
    # api.openai.com "Missing bearer"). Pin: the stream path derives exactly
    # like /run, BEFORE the child starts, and only after the auth gate passes.
    _derive_marker = tmp / "derive-marker"
    _order: list = []

    def _derive_stub():
        _derive_marker.write_text("derived")
        _order.append("derive")

    stub_d = retry_stub("stream-derive", f"""
test -f {_derive_marker} && echo "DERIVED-BEFORE-CHILD" || echo "NOT-DERIVED"
exit 0
""")
    _derive_marker.unlink(missing_ok=True)
    frames = list(hc.make_run_stream(dataclasses.replace(
        km_live, binary=stub_d, derive_creds=_derive_stub))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-derive"), "model": "k3"}).encode())))
    final = next((f["payload"] for f in reversed(frames) if f.get("kind") == "status"), {})
    check("/run/stream calls spec.derive_creds BEFORE the child starts (the 302 bug)",
          _order == ["derive"] and final.get("ok") is True
          and "DERIVED-BEFORE-CHILD" in final.get("transcript", ""),
          repr(final)[:200])
    # Auth gate still runs first: with no creds the stream 401s and NEVER derives.
    _order.clear()
    _derive_marker.unlink(missing_ok=True)
    _no_creds = dataclasses.replace(km_live, binary=stub_d, derive_creds=_derive_stub,
                                    creds_path=tmp / "absent-kimi-creds")
    out = hc.make_run_stream(_no_creds)(FakeRequest(json.dumps(
        {"prompt": PROMPT, "project_dir": str(tmp / "proj-derive2")}).encode()))
    check("/run/stream: auth gate precedes derive (401, derive_creds not called)",
          isinstance(out, tuple) and out[1] == 401 and _order == []
          and not _derive_marker.exists(), repr(out)[:200])
    # Both paths share the contract: /run derives too (the path that already did).
    _order.clear()
    _derive_marker.unlink(missing_ok=True)
    out = hc.make_run(dataclasses.replace(km_live, binary=stub_d, derive_creds=_derive_stub))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-derive3"), "model": "k3"}).encode()))
    check("/run derives the same way (parity, both paths)",
          _order == ["derive"] and isinstance(out, dict)
          and "DERIVED-BEFORE-CHILD" in out.get("transcript", ""), repr(out)[:200])
    # A non-baked spec whose bootstrap failed 503s on the stream path like /run
    # does (it used to Popen a missing binary and yield an error frame).
    _nb = dataclasses.replace(km_live, binary=tmp / "no-such-binary", baked=False,
                              cache_valid=lambda: False,
                              acquire=lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    hc._STATES.pop(_nb.id, None)
    out = hc.make_run_stream(_nb)(FakeRequest(json.dumps(
        {"prompt": PROMPT, "project_dir": str(tmp / "proj-derive4")}).encode()))
    hc._STATES.pop(_nb.id, None)
    check("/run/stream: non-baked bootstrap failure -> 503 like /run (no Popen of a missing binary)",
          isinstance(out, tuple) and out[1] == 503 and "bootstrap failed" in out[0].get("error", ""),
          repr(out)[:200])

    # ═══ rotated_creds from the shared runtime (codex's in-pod token refresh) ═══
    # The vendor CLI may refresh its own session file during a run; the pod is
    # ephemeral, so /run and /run/stream snapshot spec.login_export before the
    # child starts and hand back `rotated_creds` ONLY when the child changed it
    # (the claude-code sidecar's contract; the shared runtime never had it).
    _grant = tmp / "rot-grant.json"
    _grant.write_text(json.dumps({"access_token": "old"}))

    def _exp(spec):
        try:
            return {"credentials.json": _grant.read_text()}
        except OSError:
            return {}

    stub_rot = retry_stub("rotate-grant", f"""
echo '{{"access_token": "NEW"}}' > {_grant}
echo "ran"
exit 0
""")
    stub_norot = retry_stub("keep-grant", """
echo "ran"
exit 0
""")
    out = hc.make_run(dataclasses.replace(km_live, binary=stub_rot, login_export=_exp))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-rot1"), "model": "k3"}).encode()))
    check("/run: a grant the child rewrote comes back as rotated_creds (the post-run export)",
          isinstance(out, dict) and out.get("ok") is True
          and json.loads((out.get("rotated_creds") or {}).get("credentials.json", "{}"))
          == {"access_token": "NEW"}, repr(out)[:200])
    _grant.write_text(json.dumps({"access_token": "old"}))
    out = hc.make_run(dataclasses.replace(km_live, binary=stub_norot, login_export=_exp))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-rot2"), "model": "k3"}).encode()))
    check("/run: an unchanged grant hands nothing back (no rotated_creds key)",
          isinstance(out, dict) and out.get("ok") is True and "rotated_creds" not in out,
          repr(out)[:200])
    out = hc.make_run(dataclasses.replace(km_live, binary=stub_rot))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-rot3"), "model": "k3"}).encode()))
    check("/run: a spec without login_export never carries rotated_creds (kimi/opencode unchanged)",
          isinstance(out, dict) and "rotated_creds" not in out, repr(out)[:200])
    _grant.write_text(json.dumps({"access_token": "old"}))
    frames = list(hc.make_run_stream(dataclasses.replace(km_live, binary=stub_rot, login_export=_exp))(
        FakeRequest(json.dumps(
            {"prompt": PROMPT, "project_dir": str(tmp / "proj-rot4"), "model": "k3"}).encode())))
    final = next((f["payload"] for f in reversed(frames) if f.get("kind") == "status"), {})
    check("/run/stream: the terminal frame carries rotated_creds the same way (the dispatcher persists it; "
          "review._persistable_frame keeps it off disk)",
          final.get("state") == "complete"
          and json.loads((final.get("rotated_creds") or {}).get("credentials.json", "{}"))
          == {"access_token": "NEW"}
          and all("rotated_creds" not in (f.get("payload") or {})
                  for f in frames if f.get("kind") != "status"), repr(final)[:200])
    check("codex exports the vendor's auth.json as the flat credentials.json (login_export wired)",
          CX.login_export is specs._codex_login_export)

    # ═══ codex: the CLI's OWN catalog drives models + per-model efforts ═══
    # (rust-v0.160.1 `codex debug models`). The OpenRouter
    # vendor filter offered gpt-5.3-codex after the CLI had dropped it — task
    # 302 dispatched a phantom model. The snapshot is the fallback when the
    # binary is absent (this gate box); the live read is covered with a stub.
    _ids = [m["id"] for m in specs.CODEX_MODELS]
    check("codex catalog snapshot = 0.160.1 listed models (astra/6.1-sol/6-sol/6-luna/5.6 x3/5.5), no 5.3-codex, no 5.2",
          _ids == ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
                   "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
          and "gpt-5.3-codex" not in _ids and specs.CODEX_DEFAULT_MODEL in _ids, repr(_ids))
    check("codex effort union = every listed model's supported levels, no `minimal`/`none`",
          specs._CODEX_EFFORTS == ("low", "medium", "high", "xhigh", "max", "ultra"),
          repr(specs._CODEX_EFFORTS))
    specs._CODEX_CATALOG_CACHE.update({"models": None, "failed_at": 0.0})
    check("codex catalog: binary absent -> the pinned snapshot (copies, not the tuple)",
          [m["id"] for m in specs._codex_model_catalog()] == _ids
          and specs._codex_model_catalog()[0] is not specs.CODEX_MODELS[0])
    check("codex_efforts_for: per model (ultra on sol, NOT on 5.5; None -> default model's set)",
          "ultra" in specs.codex_efforts_for("gpt-5.6-sol")
          and "ultra" not in specs.codex_efforts_for("gpt-5.5")
          and specs.codex_efforts_for("gpt-5.5") == ("low", "medium", "high", "xhigh")
          and specs.codex_efforts_for(None) == specs.codex_efforts_for(specs.CODEX_DEFAULT_MODEL)
          and specs.codex_efforts_for("made-up-model") == specs._CODEX_EFFORTS)
    _cx_sol = specs._codex_argv(CX, PROMPT, PDIR, "gpt-5.6-sol", "ultra")
    _cx_55 = specs._codex_argv(CX, PROMPT, PDIR, "gpt-5.5", "ultra")
    _cx_55x = specs._codex_argv(CX, PROMPT, PDIR, "gpt-5.5", "xhigh")
    _cx_min = specs._codex_argv(CX, PROMPT, PDIR, None, "minimal")
    _cx_def = specs._codex_argv(CX, PROMPT, PDIR, None, "max")
    check("codex argv effort gate is PER MODEL: ultra rides on sol, dropped on 5.5; "
          "xhigh rides on 5.5; minimal dropped; max on the default model rides",
          "model_reasoning_effort=ultra" in _cx_sol
          and "model_reasoning_effort=ultra" not in _cx_55 and "-c" not in _cx_55
          and "model_reasoning_effort=xhigh" in _cx_55x
          and "-c" not in _cx_min
          and "model_reasoning_effort=max" in _cx_def,
          repr((_cx_sol[-3:], _cx_55[-2:], _cx_def[-3:])))
    # The PLATFORM's per-model default (CODEX_DEFAULT_EFFORTS: astra at medium;
    # extended to gpt-6.1-sol, the CLI's no-model default since
    # rust-v0.160.1). It fires ONLY when the dispatch
    # names no effort, it goes through the same per-model gate, and it leaves
    # every other model on the prior behaviour of passing nothing — with no
    # effort set the CLI banners `reasoning effort: none`, which is what this
    # table exists to replace.
    _cx_astra_d = specs._codex_argv(CX, PROMPT, PDIR, "gpt-6-astra", None)
    _cx_61sol_d = specs._codex_argv(CX, PROMPT, PDIR, "gpt-6.1-sol", None)
    _cx_none_d = specs._codex_argv(CX, PROMPT, PDIR, None, None)
    _cx_sol_d = specs._codex_argv(CX, PROMPT, PDIR, "gpt-5.6-sol", None)
    check("codex default effort: astra and gpt-6.1-sol with no effort ride medium; so does "
          "model=None (gpt-6.1-sol IS the CLI's default since 0.160.1); a model with no "
          "platform default passes nothing; an explicit effort still wins",
          "model_reasoning_effort=medium" in _cx_astra_d
          and "model_reasoning_effort=medium" in _cx_61sol_d
          and "model_reasoning_effort=medium" in _cx_none_d
          and "-c" not in _cx_sol_d
          and "model_reasoning_effort=ultra" in specs._codex_argv(
              CX, PROMPT, PDIR, "gpt-6-astra", "ultra")
          and "model_reasoning_effort=ultra" in specs._codex_argv(
              CX, PROMPT, PDIR, "gpt-6.1-sol", "ultra")
          and "model_reasoning_effort=medium" not in specs._codex_argv(
              CX, PROMPT, PDIR, None, "ultra"),
          repr((_cx_astra_d[-3:], _cx_61sol_d[-3:], _cx_none_d[-3:], _cx_sol_d[-2:])))
    check("CODEX_DEFAULT_EFFORTS is astra + the CLI's default (gpt-6.1-sol), each at "
          "medium, and each value is in its own model's catalog row",
          specs.CODEX_DEFAULT_EFFORTS == {"gpt-6-astra": "medium", "gpt-6.1-sol": "medium"}
          and specs.CODEX_DEFAULT_MODEL in specs.CODEX_DEFAULT_EFFORTS
          and all(v in specs.codex_efforts_for(k)
                  for k, v in specs.CODEX_DEFAULT_EFFORTS.items()),
          repr(specs.CODEX_DEFAULT_EFFORTS))
    # Live read: a stub `codex` that renders a catalog JSON -> listed rows only,
    # efforts from supported_reasoning_levels, cached after the first read.
    # The stub reports whether it ran with an ISOLATED CODEX_HOME (an empty
    # scratch dir, not $HOME) — that is what keeps the read offline when a
    # grant sits in ~/.codex (with one, the real CLI refreshes from chatgpt.com
    # first; measured on 0.149.0).
    _cat_stub = retry_stub("codex-debug-models", """
if [ "$1" = "debug" ] && [ "$2" = "models" ]; then
  iso="no"
  if [ -n "$CODEX_HOME" ] && [ -d "$CODEX_HOME" ] && [ "$CODEX_HOME" != "$HOME" ] \\
     && [ -z "$(ls -A "$CODEX_HOME" 2>/dev/null)" ]; then iso="yes"; fi
cat <<EOF
{"models":[{"slug":"gpt-9","display_name":"GPT-9 iso=$iso","visibility":"list","priority":1,
 "default_reasoning_level":"low","supported_reasoning_levels":[{"effort":"low"},{"effort":"galactic"}]},
 {"slug":"gpt-old","display_name":"Old","visibility":"hide","priority":9,
 "default_reasoning_level":"medium","supported_reasoning_levels":[{"effort":"low"}]}]}
EOF
exit 0
fi
exit 2
""")
    specs._CODEX_CATALOG_CACHE.update({"models": None, "failed_at": 0.0})
    _live = specs._codex_catalog_from_binary(binary=_cat_stub)
    check("codex live catalog parse: listed rows only, label/default/efforts from the CLI's JSON, "
          "read under an empty isolated CODEX_HOME (offline even with a grant in ~/.codex)",
          _live == [{"id": "gpt-9", "label": "GPT-9 iso=yes", "default_effort": "low",
                     "efforts": ("low", "galactic")}], repr(_live))
    _live = [{**m, "label": "GPT-9"} for m in _live]
    specs._CODEX_CATALOG_CACHE.update({"models": _live, "failed_at": 0.0})
    _st = hc.make_status(dataclasses.replace(CX, baked=True))()
    check("codex /status carries default_model + models (cached live catalog) after the two original fields",
          list(_st)[-4:] == ["bootstrap_error", "release_tag", "default_model", "models"]
          and _st["default_model"] == "gpt-6.1-sol"
          and _st["models"] == [{"id": "gpt-9", "label": "GPT-9", "default_effort": "low",
                                 "efforts": ("low", "galactic")}], repr(_st)[:300])
    check("codex_efforts_for follows the LIVE catalog when it differs from the snapshot",
          specs.codex_efforts_for("gpt-9") == ("low", "galactic"))
    specs._CODEX_CATALOG_CACHE.update({"models": None, "failed_at": 0.0})

    # ═══════════════════ shared claude_creds module ══════════════════════
    check("claude_creds OAuth client id (was harness:80 == claude-code:183)",
          cc.OAUTH_CLIENT_ID == "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
    p = wfile("cl-1", json.dumps({"claudeAiOauth": {"accessToken": "t1"}}))
    check("claude_creds read: claudeAiOauth dict (was harness:107-110)",
          cc.read_local_creds(p) == {"accessToken": "t1"})
    check("claude_creds read: missing/null -> None",
          cc.read_local_creds(tmp / "absent") is None
          and cc.read_local_creds(wfile("cl-2", json.dumps({"claudeAiOauth": None}))) is None)
    p = wfile("cl-3", json.dumps({"claudeAiOauth": {
        "accessToken": "t2", "expiresAt": 4102444800000}}))
    check("claude_creds get_local_token: valid -> (token, expiresAt) (was harness:156-171)",
          cc.get_local_token(creds_path=p, user_agent="ua") == ("t2", 4102444800000))
    p = wfile("cl-4", json.dumps({"claudeAiOauth": {"accessToken": "t3"}}))
    check("claude_creds get_local_token: no expiry -> (token, None)",
          cc.get_local_token(creds_path=p, user_agent="ua") == ("t3", None))
    p = wfile("cl-5", json.dumps({"claudeAiOauth": {
        "accessToken": "t4", "expiresAt": 1000}}))
    check("claude_creds get_local_token: expired, no refreshToken -> None (was harness:114-119,:169-171)",
          cc.get_local_token(creds_path=p, user_agent="ua") is None)
    p = wfile("cl-6", json.dumps({"claudeAiOauth": {
        "accessToken": "t5", "expiresAt": 1000, "refreshToken": "rt"}}))
    # proxy at a closed loopback port: the refresh attempt fails instantly and
    # locally (no network egress from this gate), token stays expired -> None.
    check("claude_creds get_local_token: expired + refresh failure -> None",
          cc.get_local_token(creds_path=p, user_agent="ua",
                             proxy_url="http://127.0.0.1:1") is None)

    # ══ generic harness entry: register(ALL_SPECS[id]) wires the four routes ══
    # (phase 3b: the per-vendor bin/main.py shims were removed with their dock
    # apps; `python3 -m _harness_runtime <id>` is the single harness entrypoint.)
    # Path.mkdir stubbed so register() never touches /var/lib here.
    import pathlib
    orig_mkdir = pathlib.Path.mkdir
    pathlib.Path.mkdir = lambda self, *a, **k: None
    try:
        # Derived from ALL_SPECS, not a literal list: a harness added without a
        # matching entry here would otherwise ship with its routes unchecked.
        for hid in sorted(specs.ALL_SPECS):
            hc.register(specs.ALL_SPECS[hid])
            routes_ok = all((m, p) in hc.sidecar._routes for m, p in
                            (("GET", "/health"), ("GET", "/status"),
                             ("POST", "/run"), ("GET", "/sessions"),
                             ("POST", "/check")))
            health = hc.sidecar._routes[("GET", "/health")]()
            check(f"generic entry register({hid}) wires routes + own /health",
                  routes_ok and health == {"ok": True, "app_id": hid}, repr(health))
    finally:
        pathlib.Path.mkdir = orig_mkdir

    # ══ POST /check — the phase's proof, RUN by the pod (v124) ══
    # project 1: "verify backtesting runs end-to-end" was satisfied
    # by the harness writing verify.sh. /check executes the declared commands
    # in the run's worktree and returns exit codes; nothing else does.
    chk = hc.make_check(specs.ALL_SPECS["kimi"])
    check("/check handler takes the request (dispatcher arity contract)",
          chk.__code__.co_argcount == 1)
    wdir = tmp / "checkwt"
    wdir.mkdir()
    (wdir / "marker").write_text("x")
    body = json.dumps({"project_dir": str(wdir), "commands": [
        {"cmd": "test -f marker"},
        {"cmd": "exit 3", "expect_exit": 3},
        {"cmd": "echo boom >&2; exit 1"},
        {"cmd": "sleep 5", "timeout_s": 1},
    ]}).encode()
    out = chk(FakeRequest(body))
    res = out["results"]
    check("/check runs every command in the worktree and reports each exit",
          [r["exit"] for r in res[:3]] == [0, 3, 1]
          and [r["ok"] for r in res[:3]] == [True, True, False], repr(res))
    check("/check keeps the failing command's output tail (stderr folded in)",
          "boom" in res[2]["output_tail"], repr(res[2]))
    check("/check kills a command past its timeout and marks it not ok",
          res[3]["timed_out"] is True and res[3]["ok"] is False, repr(res[3]))
    check("/check envelope ok is the AND of every check",
          out["ok"] is False)
    good = chk(FakeRequest(json.dumps({"project_dir": str(wdir),
                                       "commands": ["test -f marker"]}).encode()))
    check("/check: all pass -> ok true (a bare string is a command)",
          good["ok"] is True and good["results"][0]["cmd"] == "test -f marker")
    bad_dir = chk(FakeRequest(json.dumps({"project_dir": str(tmp / "nope"),
                                          "commands": ["true"]}).encode()))
    check("/check refuses a missing worktree with 400 rather than running in cwd",
          isinstance(bad_dir, tuple) and bad_dir[1] == 400)
    empty = chk(FakeRequest(json.dumps({"project_dir": str(wdir),
                                        "commands": []}).encode()))
    check("/check refuses an empty command list (no proof is not a pass)",
          isinstance(empty, tuple) and empty[1] == 400)

    # ═════════════════════ wiring (source-tree checks) ═══════════════════
    def src(rel):
        fp = os.path.join(app_root, rel)
        return open(fp).read() if os.path.exists(fp) else None

    # phase 3b: the 5 vendor dock apps are DELETED — assert they're gone.
    gone_ok = all(src(f"system-apps/{hid}/bin/main.py") is None
                  for hid in ("codex", "cursor", "copilot", "grok", "opencode"))
    check("5 vendor harness dock apps removed (phase 3b)", gone_ok)
    check("generic harness entry present", src("system-apps/_harness_runtime/__main__.py") is not None)

    dock_ok = True
    # Derived from ALL_SPECS for the same reason as register() above.
    for hid in sorted(specs.ALL_SPECS):
        s = src(f"sidecars/harness-{hid}/Dockerfile")
        if s is None:
            continue
        # ships the runtime, runs the generic entry with its id, no deleted-shim COPY
        if ("COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/" not in s
                or f'"-m", "_harness_runtime", "{hid}"' not in s
                or f"system-apps/{hid}/bin/main.py" in s):
            dock_ok = False
            check(f"harness-{hid} Dockerfile uses generic entry, no dead shim COPY", False)
    # claude-code-harness keeps its own bin (it runs Anthropic's Agent SDK in the pod)
    s = src("sidecars/harness-claude-code/Dockerfile")
    if s and "COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/" not in s:
        dock_ok = False
        check("harness-claude-code Dockerfile ships _harness_runtime", False)
    check("harness Dockerfiles ship runtime + generic entry (no deleted shims)", dock_ok)

    s = src("Dockerfile.linux")
    if s is not None:
        check("entity image ships _harness_runtime for bundle-installed shims",
              "system-apps/_harness_runtime/ /app/cli/_harness_runtime/" in s)
    s = src("system-apps/claude-code-harness/bin/main.py")
    if s is not None:
        check("claude-code-harness imports the shared claude_creds",
              "claude_creds.get_local_token" in s
              and "_refresh_local_token" not in s)
    s = src("system-apps/claude-code/bin/main.py")
    if s is not None:
        # The frontend fallback is GONE and its ABSENCE is now the
        # assertion. _fetch_token_from_frontend pulled the token from the
        # platform's own Anthropic OAuth store — the surface deleted along with
        # the platform's Claude integration route. The entity's own on-disk
        # creds are the single source now, so requiring the fallback here would
        # pin in place the very thing that was removed.
        check("claude-code sidecar imports the shared claude_creds "
              "(single source: on-disk creds, no platform-OAuth fallback)",
              "claude_creds.get_local_token" in s
              and "_fetch_token_from_frontend" not in s
              # The ASSIGNMENT, not the bare word: the file explains in a
              # comment why FRONTEND_BASE was removed, and a substring check
              # cannot tell code from prose — it red-failed on its own
              # explanation.
              and "FRONTEND_BASE = " not in s
              and "_refresh_local_token" not in s)

    # ── events: kimi >= 0.36 role-based JSONL folds into terminal lines, not
    # raw JSON (the scrollback once showed the
    # CLI's chat-message objects verbatim because the 0.32-era parser only
    # spoke the Claude event vocabulary). Shapes below mirror the live 0.36.1
    # stream exactly. ─────────────────────────────────────────────────────────
    from _harness_runtime import events as EV

    l_version = json.dumps(
        {"role": "meta", "type": "system.version", "version": "0.36.1"})
    evs = EV.normalize_line("kimi", l_version, 0)
    check("kimi meta system.version -> one status frame",
          len(evs) == 1 and evs[0]["kind"] == "status"
          and evs[0]["payload"]["state"] == "system.version"
          and evs[0]["payload"]["detail"] == "0.36.1", str(evs))

    l_calls = json.dumps({"role": "assistant", "tool_calls": [
        {"type": "function", "id": "tool_a", "function":
            {"name": "Read", "arguments": "{\"path\":\"index.html\"}"}},
        {"type": "function", "id": "tool_b", "function":
            {"name": "Read", "arguments": "{\"path\":\"app.yaml\"}"}},
        {"type": "function", "id": "tool_c", "function":
            {"name": "Bash", "arguments": "{\"command\":\"ls -la /x\"}"}},
    ]})
    evs = EV.normalize_line("kimi", l_calls, 5)
    check("kimi assistant 3-tool_calls line -> 3 tool_call frames, rising seq",
          [e["kind"] for e in evs] == ["tool_call"] * 3
          and [e["seq"] for e in evs] == [5, 6, 7]
          and evs[0]["payload"]["tool"] == "Read"
          and evs[0]["payload"]["args"] == {"path": "index.html"}
          and evs[2]["payload"]["tool"] == "Bash", str(evs))

    l_result = json.dumps({"role": "tool", "tool_call_id": "tool_a",
                           "content": "1\t<!DOCTYPE html>\n2\t<html>..."})
    evs = EV.normalize_line("kimi", l_result, 8)
    check("kimi tool line -> one tool_result frame (body carried, ok status)",
          len(evs) == 1 and evs[0]["kind"] == "tool_result"
          and evs[0]["payload"]["status"] == "ok"
          and "<!DOCTYPE html>" in evs[0]["payload"]["result"], str(evs))

    l_mixed = json.dumps({"role": "assistant",
                          "content": "Fixing the subscriptions total.",
                          "tool_calls": [
        {"type": "function", "id": "t1", "function":
            {"name": "Edit", "arguments":
                "{\"path\":\"index.html\",\"old_string\":\"a\",\"new_string\":\"b\"}"}},
        {"type": "function", "id": "t2", "function":
            {"name": "Edit", "arguments": "{\"path\":\"app.yaml\"}"}},
    ]})
    evs = EV.normalize_line("kimi", l_mixed, 9)
    check("kimi prose + 2 Edits -> text frame + 2 file_edit frames (paths kept)",
          [e["kind"] for e in evs] == ["text", "file_edit", "file_edit"]
          and evs[0]["payload"]["text"].startswith("Fixing")
          and evs[1]["payload"]["path"] == "index.html"
          and evs[2]["payload"]["path"] == "app.yaml", str(evs))

    l_resume = json.dumps({"role": "meta", "type": "session.resume_hint",
                           "session_id": "session_x",
                           "command": "kimi -r session_x",
                           "content": "To resume: kimi -r session_x"})
    evs = EV.normalize_line("kimi", l_resume, 12)
    check("kimi resume_hint meta -> status frame naming the command",
          len(evs) == 1 and evs[0]["kind"] == "status"
          and evs[0]["payload"]["state"] == "session.resume_hint"
          and "kimi -r" in evs[0]["payload"]["detail"], str(evs))

    l_032 = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "old-writer prose"}]}})
    evs = EV.normalize_line("kimi", l_032, 0)
    check("kimi 0.32 claude-vocabulary line still parses (fallback dialect)",
          len(evs) == 1 and evs[0]["kind"] == "text"
          and evs[0]["payload"]["text"] == "old-writer prose", str(evs))

    evs = EV.normalize_line("kimi", "garbage { not json", 3)
    check("unparseable line -> one raw text frame (never-drop holds)",
          len(evs) == 1 and evs[0]["kind"] == "text"
          and evs[0]["payload"]["text"] == "garbage { not json", str(evs))

    l_grok = json.dumps({"type": "message", "message": {"content": [
        {"type": "text", "text": "hi"}]}})
    check("non-kimi harness: normalize_line == [normalize] (single-event parity)",
          EV.normalize_line("grok", l_grok, 4)[0]["payload"]
          == EV.normalize("grok", l_grok, 4)["payload"]
          and len(EV.normalize_line("grok", l_grok, 4)) == 1)

    check("normalize() compat shim survives a multi-event kimi line",
          EV.normalize("kimi", l_calls, 0)["kind"] == "tool_call")

    # ── events: codex item.* lines carry the content
    # (entity-9003 — the Workbench feed was 19 × "· item.completed"
    # because a .started/.completed SUFFIX test ran before the item branch).
    # Shapes below are the live rust-v0.149.0 stream verbatim. ────────────────
    cx_msg = json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message",
        "text": "I'll inspect the current project, then rebuild the static app."}})
    evs = EV.normalize_line("codex", cx_msg, 2)
    check("codex agent_message -> one text frame carrying the prose",
          len(evs) == 1 and evs[0]["kind"] == "text"
          and evs[0]["payload"]["text"].startswith("I'll inspect")
          and evs[0]["seq"] == 2, str(evs))
    cx_cmd0 = json.dumps({"type": "item.started", "item": {
        "id": "item_1", "type": "command_execution",
        "command": "/usr/bin/bash -lc \"pwd && rg --files | sed -n '1,120p'\"",
        "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    evs = EV.normalize_line("codex", cx_cmd0, 3)
    check("codex command_execution start -> tool_call, command in args",
          len(evs) == 1 and evs[0]["kind"] == "tool_call"
          and evs[0]["payload"]["tool"] == "shell"
          and evs[0]["payload"]["args"]["command"].startswith("/usr/bin/bash -lc"),
          str(evs))
    cx_cmd1 = json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution",
        "command": "/usr/bin/bash -lc \"pwd && rg --files | sed -n '1,120p'\"",
        "aggregated_output": "/var/lib/cseries/projects/codex\n"
                             "/usr/bin/bash: line 1: rg: command not found\n",
        "exit_code": 0, "status": "completed"}})
    evs = EV.normalize_line("codex", cx_cmd1, 4)
    check("codex command_execution end -> tool_result with the output",
          len(evs) == 1 and evs[0]["kind"] == "tool_result"
          and evs[0]["payload"]["status"] == "ok"
          and "rg: command not found" in evs[0]["payload"]["result"]
          and evs[0]["payload"]["exit_code"] == 0, str(evs))
    cx_patch = json.dumps({"type": "item.completed", "item": {
        "id": "item_7", "type": "file_change", "changes": [
            {"path": "/var/lib/cseries/projects/codex/app.yaml", "kind": "update"},
            {"path": "/var/lib/cseries/projects/codex/index.html", "kind": "update"}],
        "status": "completed"}})
    evs = EV.normalize_line("codex", cx_patch, 17)
    check("codex file_change end -> one file_edit per path, rising seq",
          [e["kind"] for e in evs] == ["file_edit", "file_edit"]
          and [e["seq"] for e in evs] == [17, 18]
          and evs[0]["payload"]["path"].endswith("app.yaml")
          and evs[1]["payload"]["path"].endswith("index.html"), str(evs))
    cx_turn = json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 177124, "cached_input_tokens": 144384,
        "cache_write_input_tokens": 0, "output_tokens": 1851,
        "reasoning_output_tokens": 444}})
    evs = EV.normalize_line("codex", cx_turn, 19)
    check("codex turn.completed -> status + usage frame",
          [e["kind"] for e in evs] == ["status", "usage"]
          and evs[0]["payload"]["state"] == "turn.completed"
          and evs[1]["payload"]["output_tokens"] == 1851, str(evs))
    cx_thread = json.dumps({"type": "thread.started",
                            "thread_id": "01a021ac-254f-77c3-9745-be922fc0a675"})
    evs = EV.normalize_line("codex", cx_thread, 0)
    check("codex thread.started stays a status frame naming the thread",
          len(evs) == 1 and evs[0]["kind"] == "status"
          and evs[0]["payload"]["detail"].startswith("01a021ac"), str(evs))
    for i, raw in enumerate((cx_msg, cx_cmd0, cx_cmd1, cx_patch)):
        check(f"codex item line {i} never folds to a bare item.* status frame",
              all(not str(e["payload"].get("state", "")).startswith("item.")
                  for e in EV.normalize_line("codex", raw, 0)), raw[:60])
    # Item-level error = NON-FATAL by the CLI's schema (config warnings,
    # deprecation notices, model reroutes): a warning status, never kind=error,
    # so a clean run does not wear a red failure strip in the run block.
    cx_warn = json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "error",
        "message": "model rerouted: gpt-5.3-codex -> gpt-5.6-terra"}})
    evs = EV.normalize_line("codex", cx_warn, 0)
    check("codex item-level error -> warning status carrying the message",
          len(evs) == 1 and evs[0]["kind"] == "status"
          and evs[0]["payload"]["state"] == "warning"
          and "rerouted" in evs[0]["payload"]["detail"], str(evs))
    evs = EV.normalize_line("codex", json.dumps(
        {"type": "turn.failed", "error": {"message": "rate limited"}}), 0)
    check("codex turn.failed stays a real error frame",
          len(evs) == 1 and evs[0]["kind"] == "error"
          and evs[0]["payload"]["message"] == "rate limited", str(evs))

    # ── claude-code-harness shared-runtime adoption ─────────────────────────
    # The last harness without /run/stream or transient retry. Behavior-tested
    # by loading the module (SDK import is guarded, so it loads anywhere) and
    # driving _msg_frames/_sdk_transient with stand-in SDK classes.
    import importlib.util as _ilu
    _cch_path = os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py")
    _spec = _ilu.spec_from_file_location("cch_main", _cch_path)
    CCH = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(CCH)

    check("claude-code-harness registers /run/stream (kernel streams it, "
          "no more buffered-only fallback)",
          ("POST", "/run/stream") in CCH.sidecar._routes
          and ("POST", "/run") in CCH.sidecar._routes)
    check("claude-code-harness imports the shared retry contract",
          CCH.RUN_RETRY_LIMIT >= 1 and CCH.RUN_RETRY_MIN_BUDGET_S > 0
          and CCH._RETRYABLE_STDERR.search("provider.connection_error: Connection error."))

    def _blk(clsname, **kw):
        """Stand-in SDK block/message: an object whose class NAME matches the
        SDK vocabulary (_msg_frames folds by class name, by design)."""
        o = type(clsname, (), {})()
        for k, v in kw.items():
            setattr(o, k, v)
        return o

    _chunks: list = []
    _hb = {"n": 0}
    _amsg = _blk("AssistantMessage", content=[
        _blk("TextBlock", text="working on it"),
        _blk("ThinkingBlock", thinking="deep thought here"),
        _blk("ToolUseBlock", name="Write",
             input={"file_path": "index.html", "content": "<html>"}),
        _blk("ToolUseBlock", name="Bash", input={"command": "ls"}),
    ])
    _frames = CCH._msg_frames(_amsg, _hb, _chunks)
    check("sdk assistant msg -> text + thinking + file_edit + tool_call frames",
          [f["kind"] for f in _frames] == ["text", "thinking", "file_edit", "tool_call"]
          and _frames[1]["payload"]["text"] == "deep thought here"
          and _frames[2]["payload"]["path"] == "index.html"
          and _frames[3]["payload"]["tool"] == "Bash", str(_frames))
    check("stream fold keeps the buffered transcript's chunk shapes",
          _chunks[0] == "working on it"
          and _chunks[1].startswith("[thinking: ")
          and _chunks[2].startswith("[tool: Write"), str(_chunks))
    _tres = CCH._msg_frames(
        _blk("UserMessage", content=[
            _blk("ToolResultBlock", content="ok done", is_error=False)]),
        _hb, _chunks)
    check("sdk tool result -> tool_result frame",
          len(_tres) == 1 and _tres[0]["kind"] == "tool_result"
          and _tres[0]["payload"]["status"] == "ok")

    # An EMPTY ThinkingBlock (the model kept its reasoning — claude-fable-5,
    # fan-out 3ba241d3, 5 of 5 blocks) must not become a blank
    # thinking frame: the run block would paint an empty dimmed block.
    _empty = CCH._msg_frames(_blk("AssistantMessage", content=[
        _blk("ThinkingBlock", thinking=""),
        _blk("TextBlock", text="after")]), _hb, [])
    check("sdk empty ThinkingBlock -> a named status frame, not an empty thinking frame",
          [f["kind"] for f in _empty] == ["status", "text"]
          and _empty[0]["payload"]["state"] == "thinking"
          and "not returned" in _empty[0]["payload"]["detail"], str(_empty))

    # ── claude-code /run/stream has NO total cap ─────────────────────────────
    # The stream path kept `deadline = started + max_minutes*60` after
    # harness_core dropped it, and cut a working Opus 5.5 build at 900 s as
    # "harness exited 1 with no output". Driven through the real handler with
    # a fake SDK: max_minutes 0.01 truncates to a 0 s budget (the old code's
    # 1 s wait_for floor), idle window 0.3 s.
    import asyncio as _aio
    _cch_saved = {k: getattr(CCH, k, None) for k in (
        "_SDK_AVAILABLE", "_get_token", "_ensure_claude_config", "_sdk_options",
        "_sdk_query", "worktree_diff", "_current_creds", "IDLE_FLOOR_S",
        "_stop_run_tree", "_ALIVE_FRAME_S", "_SdkOptions")}
    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    # The REAL _sdk_options runs; only the SDK's options class is a stand-in
    # that hands its kwargs through. The fake CLI reads the marker out of the
    # env it is started with, so a lost `env` line fails here.
    _markers: list = []
    CCH._sdk_options = _cch_saved["_sdk_options"]
    CCH._SdkOptions = lambda **kw: kw

    def _cli_marker(options):
        _markers.append(((options or {}).get("env") or {}).get(CCH._RUN_MARKER_ENV))
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH.IDLE_FLOOR_S = 0.1
    # The idle stop's process kill is RECORDED, never run: this gate runs on
    # the host, and real-signal checks live only in the private-PID-namespace
    # gate (scripts/test_claude_code_harness_run_tree.py).
    _stops: list = []
    _closed = [False]

    def _rec_stop(marker, **k):
        _stops.append((marker, _closed[0]))
        return []
    CCH._stop_run_tree = _rec_stop

    def _cch_run(script, max_minutes=0.01, keep=None):
        async def _fake_query(prompt, options):
            _cli_marker(options)
            _closed[0] = False
            try:
                for step in script:
                    if isinstance(step, (int, float)):
                        await _aio.sleep(step)
                    else:
                        yield step
            finally:
                _closed[0] = True    # the SDK's close: the CLI is gone from here
        CCH._sdk_query = _fake_query
        t0 = _time.time()
        frames = list(CCH.run_stream(FakeRequest(json.dumps({
            "prompt": "build it", "project_dir": tempfile.mkdtemp(),
            "max_minutes": max_minutes, "idle_minutes": 0.005}).encode())))
        if keep is not None:
            keep[:] = frames
        return frames[-1]["payload"], _time.time() - t0

    _done = _blk("ResultMessage", is_error=False, model="claude-opus-5-5",
                 usage=None, total_cost_usd=0.1)
    _hbeat = _blk("SystemMessage", subtype="thinking_tokens")
    try:
        # 1.6 s of steady activity: far past the (zero) budget, never quiet.
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("TextBlock", text="starting")])]
            + [x for _ in range(16) for x in (0.1, _hbeat)] + [_done])
        check("claude-code stream: a run past max_minutes that keeps talking "
              "completes (no total cap)",
              _p["state"] == "complete" and _p["ok"] and _p["exit_code"] == 0
              and "error" not in _p and _dt >= 1.5, f"{_p.get('state')} {_p.get('error')} {_dt:.1f}s")

        # A tool call, then silence: stopped on the idle window, named as such.
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("ToolUseBlock", name="Bash", input={"command": "sleep 99"})]), 30])
        check("claude-code stream: silence while a tool runs is stopped on the "
              "idle window, with a reason the kernel shows",
              _p["state"] == "timeout" and not _p["ok"] and _dt < 5
              and "while a tool was running" in (_p.get("error") or "")
              and "not stopped for running long" in _p["error"],
              f"{_p.get('state')} {_p.get('error')} {_dt:.1f}s")

        # The model spoke, no tool open, then silence: a model-turn stop.
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("TextBlock", text="writing index.html")]), 30])
        check("claude-code stream: a silent model turn is stopped as a model "
              "wait, not as 'exited 1 with no output'",
              _p["state"] == "timeout" and _dt < 5
              and "waited on the model" in (_p.get("error") or ""),
              f"{_p.get('state')} {_p.get('error')} {_dt:.1f}s")

        # A tool that returned closes its slot: the silence after it is a model turn.
        _p, _dt = _cch_run([
            _blk("AssistantMessage", content=[
                _blk("ToolUseBlock", name="Write", input={"file_path": "a.html"})]),
            _blk("UserMessage", content=[_blk("ToolResultBlock", content="ok",
                                              is_error=False)]),
            30])
        check("claude-code stream: a Write call + its result leave no tool open",
              _p["state"] == "timeout" and "waited on the model" in (_p.get("error") or ""),
              f"{_p.get('state')} {_p.get('error')}")

        # WHICH window applies (with max_minutes 0 both windows
        # are equal, so a swapped rule stayed green). max_minutes 1 gives a
        # model turn 60 s and a tool 0.3 s; a 1 s silence tells them apart.
        _stops.clear()
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("TextBlock", text="writing index.html")]), 1.0, _done], max_minutes=1)
        check("claude-code stream: a 1 s silent model turn inside a 60 s turn "
              "budget completes (a model turn is judged on max_minutes)",
              _p["state"] == "complete" and _p["ok"] and not _stops,
              f"{_p.get('state')} {_p.get('error')} stops={_stops}")
        _pdir: list = []
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("ToolUseBlock", id="toolu_1", name="Bash",
                 input={"command": "sleep 99"})]), 1.0, _done], max_minutes=1, keep=_pdir)
        check("claude-code stream: the same 1 s silence with a tool open is "
              "stopped on the idle window, and that run's processes are stopped",
              _p["state"] == "timeout" and "while a tool was running" in (_p.get("error") or "")
              and len(_stops) == 1,
              f"{_p.get('state')} {_p.get('error')} stops={_stops}")
        # MUTATION: kill after agen.aclose().  RESULT: red (closed=True).
        # MUTATION: kill with another value than the CLI's marker.  RESULT: red.
        check("claude-code stream: the idle stop kills BEFORE the SDK closes the "
              "CLI, by the marker the CLI was started with",
              bool(_markers[-1]) and _stops == [(_markers[-1], False)],
              f"stops={_stops} marker={_markers[-1:]}")

        # The reader goes away mid-run (kernel disconnect): the run ends with no
        # result, and its tools are stopped the same way.
        _stops.clear()

        async def _chatty(prompt, options):
            _cli_marker(options)
            _closed[0] = False
            try:
                yield _blk("AssistantMessage", content=[
                    _blk("ToolUseBlock", id="toolu_9", name="Bash", input={"command": "x"})])
                for i in range(200):
                    await _aio.sleep(0.02)
                    yield _blk("AssistantMessage", content=[_blk("TextBlock", text=f"t{i}")])
            finally:
                _closed[0] = True
        CCH._sdk_query = _chatty
        _g = CCH.run_stream(FakeRequest(json.dumps({
            "prompt": "build it", "project_dir": tempfile.mkdtemp(),
            "max_minutes": 1, "idle_minutes": 0.005}).encode()))
        next(_g)
        _g.close()                        # the reader is gone
        _t_end = _time.time() + 5
        while not _stops and _time.time() < _t_end:
            _time.sleep(0.05)
        check("claude-code stream: a run whose reader went away still stops its "
              "own processes, before the SDK closes the CLI",
              bool(_markers[-1]) and _stops == [(_markers[-1], False)], f"stops={_stops}")

        # A tool answered INSIDE an AssistantMessage (the SDK parses that shape;
        # _msg_frames writes no frame for it) still closes its call, by id.
        _stops.clear()
        _p, _dt = _cch_run([
            _blk("AssistantMessage", content=[
                _blk("ToolUseBlock", id="toolu_2", name="Bash", input={"command": "ls"})]),
            _blk("AssistantMessage", content=[
                _blk("ToolResultBlock", tool_use_id="toolu_2", content="ok", is_error=False)]),
            1.0, _done], max_minutes=1)
        check("claude-code stream: a tool result with no frame still closes its "
              "call (tracked by id, not by frame count)",
              _p["state"] == "complete" and _p["ok"] and not _stops,
              f"{_p.get('state')} {_p.get('error')} stops={_stops}")

        # SDK activity that writes no frame (heartbeats 2..20) still reaches the
        # kernel as a `working` frame, so its silence clock matches the pod's.
        CCH._ALIVE_FRAME_S = 0.3
        _fr: list = []
        _p, _dt = _cch_run([_blk("AssistantMessage", content=[
            _blk("TextBlock", text="thinking hard")]), _hbeat]
            + [x for _ in range(12) for x in (0.1, _hbeat)] + [_done],
            max_minutes=1, keep=_fr)
        _working = [f for f in _fr if f.get("kind") == "status"
                    and (f.get("payload") or {}).get("state") == "working"]
        check("claude-code stream: frame-less SDK activity sends `working` frames",
              _p["state"] == "complete" and len(_working) >= 2,
              f"{_p.get('state')} working={len(_working)}")
    finally:
        for _k, _v in _cch_saved.items():
            setattr(CCH, _k, _v)

    # ── stderr window keeps the HEAD too (fan-out 3ba241d3, cursor) ──────────
    # cursor-agent refused `--model gpt-5.6-luna-pro` with ONE long line whose
    # reason sits at the front and whose "Available models:" list ran past
    # 4096 chars; the tail-only window lost the words and the user saw a
    # list fragment. Head + marker + tail, and short stderr is untouched.
    _long = "Error: unknown model 'gpt-5.6-luna-pro'. Available models: " \
            + ", ".join(f"model-{i}" for i in range(1200)) + "\n"
    _win = hc._stderr_tail(_long)
    check("stderr window: long single line keeps its HEAD (the reason) and its tail",
          _win.startswith("Error: unknown model 'gpt-5.6-luna-pro'")
          and hc.STDERR_ELIDED_MARK in _win
          and _win.endswith("model-1199\n")
          and len(_win) <= hc.STDERR_HEAD_CHARS + hc.STDERR_TAIL_CHARS + len(hc.STDERR_ELIDED_MARK) + 2,
          _win[:80] + " … " + _win[-40:])
    check("stderr window: short stderr is returned whole (no marker)",
          hc._stderr_tail("error: one line\n") == "error: one line\n"
          and hc.STDERR_ELIDED_MARK not in hc._stderr_tail("x" * hc.STDERR_TAIL_CHARS))
    _mid = "".join(f"L{i:04d}\n" for i in range(800))   # 4800 chars: between tail and head+tail
    _mw = hc._stderr_tail(_mid)
    check("stderr window: a stderr that fits head+tail comes back whole, never duplicated",
          _mw == _mid and hc.STDERR_ELIDED_MARK not in _mw, len(_mw))
    import io as _io
    _drain = hc._StderrTail()
    _drain.start(_io.StringIO("HEAD-LINE first\n" + ("reasoning prose line\n" * 400)
                              + "error: the real failure line\n"))
    _dt = _drain.tail()
    check("stderr drain: threaded window keeps head + marker + the true last line",
          _dt.startswith("HEAD-LINE first")
          and hc.STDERR_ELIDED_MARK in _dt
          and _dt.rstrip().endswith("error: the real failure line")
          and hc._last_error_line(_dt) == "error: the real failure line", _dt[:60] + " … " + _dt[-60:])
    _small = hc._StderrTail()
    _small.start(_io.StringIO("only\ntwo lines\n"))
    check("stderr drain: a small stderr comes back verbatim",
          _small.tail() == "only\ntwo lines\n", _small.tail())
    _mdr = hc._StderrTail()
    _mdr.start(_io.StringIO(_mid))
    check("stderr drain: a stderr that fits head+tail comes back whole (no marker, no overlap)",
          _mdr.tail() == _mid, len(_mdr.tail()))

    # ── cursor's OWN model catalog ──────────────────────────────────────────
    # The sample below is built from the PINNED package's own printer
    # (2026.05.28-a70ca7c, dist-package/730.index.js "./src/commands/models.ts";
# re-read at 2026.10.01-e373342, 6436.index.js: unchanged apart from a longer Tip line):
    #   dim("Available models"), "", then per model
    #   `${cyan|green(displayModelId||modelId)}${dim(" - "+displayName)}${dim(" ("+marks+")")}`
    #   with marks ⊆ {current, default}, then "", then dim("Tip: use --model …").
    # The ids are REAL Cursor displayModelIds: they come from the CLI's own
    # refusal line captured on one Host fan-out 3ba241d3, which enumerates
    # exactly the field --list-models prints.
    _CY, _DIM, _GR, _RS = "\x1b[36m", "\x1b[2m", "\x1b[32m", "\x1b[0m"
    _cursor_out = "\n".join([
        f"{_DIM}Available models{_RS}",
        "",
        f"{_CY}claude-opus-5-thinking-medium{_RS}{_DIM} - Claude Opus 5 (Thinking){_RS}",
        f"{_GR}composer-2.5-fast{_RS}{_DIM} - Composer 2.5{_RS}{_DIM} (current, default){_RS}",
        f"{_CY}gpt-5.4-nano-xhigh{_RS}",
        f"{_CY}kimi-k3-max{_RS}{_DIM} - Kimi K3{_RS}{_DIM} (current){_RS}",
        "",
        f"{_DIM}Tip: use {_CY}--model <id>{_RS}{_DIM} (or /model <id> in interactive mode) to switch.{_RS}",
    ])
    _cm, _cdef = specs._cursor_parse_models(_cursor_out)
    check("cursor catalog: ids are the vendor's displayModelIds, in order, "
          "ANSI stripped, header/Tip lines dropped",
          [m["id"] for m in _cm] == ["claude-opus-5-thinking-medium",
                                     "composer-2.5-fast", "gpt-5.4-nano-xhigh",
                                     "kimi-k3-max"], str(_cm))
    check("cursor catalog: the display name rides as a label, never in the id",
          _cm[0].get("label") == "Claude Opus 5 (Thinking)"
          and _cm[2].get("label") is None, str(_cm[:3]))
    check("cursor catalog: a display name ENDING in ')' is not mistaken for the "
          "(current, default) marker",
          _cm[0]["id"] == "claude-opus-5-thinking-medium", str(_cm[0]))
    check("cursor catalog: the (default) row is marked and reported back",
          _cdef == "composer-2.5-fast" and _cm[1].get("default") is True
          and _cm[3].get("default") is None, f"{_cdef} {_cm[1]} {_cm[3]}")
    _empty, _edef = specs._cursor_parse_models(
        f"{_DIM}No models available for this account.{_RS}\n")
    check("cursor catalog: 'No models available for this account.' is an EMPTY "
          "list (a real answer), never a row parsed out of that sentence",
          _empty == [] and _edef is None, str(_empty))
    check("cursor catalog: empty output -> empty list, never a fabricated row",
          specs._cursor_parse_models("") == ([], None))
    check("cursor spec serves `models` on /status and bakes NO snapshot "
          "(per-ACCOUNT list — a baked one would assert someone else's plan)",
          "models" in specs.CURSOR.status_tail
          and specs.CURSOR.models is specs._cursor_model_catalog
          and not hasattr(specs, "CURSOR_MODELS"))
    _src = inspect.getsource(specs._cursor_catalog_from_binary)
    check("cursor catalog is an ACCOUNT read: no key -> it refuses instead of "
          "shelling the binary and parsing an auth error",
          "_read_creds" in _src and "not connected" in _src
          and "--list-models" in _src)

    # ── antigravity catalog ──────────────────────────────────────────
    # Its picker was empty because nothing served it a model list. The list is
    # per-account (`agy models` refuses without a session), so it is READ in the
    # pod and parsed here. This fixture is verbatim `agy models` output from
    # agy 1.1.17 in one Host's own pod — the parser must never be "improved"
    # past what the vendor actually prints.
    _AGY_REAL = ("Fetching available models...\n"
                 "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
                 "gemini-3.1-pro-low\tGemini 3.1 Pro (Low)\n"
                 "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
                 "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)")
    _agy_rows = specs._agy_parse_models(_AGY_REAL)
    check("antigravity: parses id + label off the measured `agy models` output",
          [r["id"] for r in _agy_rows] == ["gemini-3.7-flash-high",
                                           "gemini-3.1-pro-low",
                                           "claude-sonnet-4-6",
                                           "gpt-oss-120b-medium"]
          and _agy_rows[0]["label"] == "Gemini 3.7 Flash (High)",
          repr(_agy_rows))
    check("antigravity: the preamble line is never a model",
          all(not r["id"].startswith("Fetching") for r in _agy_rows))
    # The refusal prints at exit 1, but a parser that scraped prose would mint
    # an id from it — the class of bug that put phantom models in the picker.
    check("antigravity: a sign-in refusal yields NO models, never a fake id",
          specs._agy_parse_models(
              "Fetching available models...\nError: Please sign in to view "
              "available models. Launch the CLI without arguments to sign in.") == [])
    # The measured refusal happens to be tab-free, so the tab test alone would
    # pass it by luck. A future TABBED vendor error would mint `Error:` as a
    # model — and a non-empty list sails past the empty-catalog ambiguity guard
    # and caches for a day. The id charset is what actually closes that.
    check("antigravity: TABBED vendor prose still yields no model",
          specs._agy_parse_models("Error:\tPlease sign in to continue") == []
          and specs._agy_parse_models("WARN\trate limited") == []
          and specs._agy_parse_models("  \t  ") == []
          and specs._agy_parse_models(None) == [])
    check("antigravity: the spec serves its catalog on /status",
          specs.ANTIGRAVITY.models is specs._agy_model_catalog
          and "models" in specs.ANTIGRAVITY.status_tail)
    _agy_src = inspect.getsource(specs._agy_catalog_from_binary)
    check("antigravity catalog is an ACCOUNT read: no session -> it refuses "
          "instead of shelling the binary and parsing an auth error",
          "not connected" in _agy_src and '"models"' in _agy_src
          and "_agy_token_present" in _agy_src and "_read_creds" in _agy_src)

    check("sdk transient: connection-error text retries, timeout never does",
          CCH._sdk_transient({"timed_out": False, "ended_early": False},
                             "x provider.connection_error: Connection error.")
          and CCH._sdk_transient({"timed_out": False, "ended_early": True}, "")
          and not CCH._sdk_transient({"timed_out": True, "ended_early": False},
                                     "Connection error")
          and not CCH._sdk_transient({"timed_out": False, "ended_early": False},
                                     "SyntaxError: invalid syntax"))

    print(f"[harness-runtime] {'PASS' if not FAILS else 'FAIL'} "
          f"({len(FAILS)} failing)" + (f": {FAILS}" if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
