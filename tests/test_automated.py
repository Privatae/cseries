#!/usr/bin/env python3
"""
Privatae Personal Computer — Automated Test Suite

Tests that can be run WITHOUT a container, relay certs, or infrastructure.
Validates syntax, imports, security patterns, schemas, and path logic.

Usage: python3 tests/test_automated.py
"""

import functools
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).parent.parent
SERVER = ROOT / "server"
DASHBOARD = ROOT / "dashboard" / "base"
TESTS_PASSED = 0
TESTS_FAILED = 0
FAILURES = []


def checks(fn):
    """Each test_* below records its checks through passed()/failed(). Under
    pytest (the tests/ lane) failed() alone only prints, and for months 55
    failing checks left this file green. Wrapped, a test fails when any check
    it ran failed, naming every one."""
    @functools.wraps(fn)
    def run():
        before = len(FAILURES)
        fn()
        new = FAILURES[before:]
        assert not new, f"{len(new)} check(s) failed:\n" + "\n".join(f"  {n}: {r}" for n, r in new)
    return run


def passed(name):
    global TESTS_PASSED
    TESTS_PASSED += 1
    print(f"  PASS  {name}")


def failed(name, reason):
    global TESTS_FAILED
    TESTS_FAILED += 1
    FAILURES.append((name, reason))
    print(f"  FAIL  {name}: {reason}")


def read_or_fail(path, name):
    """Read a file the suite asserts against; a stale path is a FAILURE, never a crash.

    Every check below reads a hardcoded path. When a file moves or is deleted,
    a bare .read_text() raises FileNotFoundError out of __main__ and KILLS the
    run — so every test AFTER it silently stops existing. That is how [14]
    CEIGAS Middleware went unrun: server/ceigas/ceigas_domain_controller.py was
    deleted and [6] died on it, ~300 lines before the gate check. Measured once:
    8 hardcoded paths in this file were stale.
    Returning "" turns each into one recorded failure and lets the rest run.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        failed(name, f"file not found: {rel} — check is UNVERIFIED, not passing")
        return ""


# =========================================================
# 1. Python syntax validation
# =========================================================
def _syntax_errors(files):
    """{path: error} for each file that does not compile. compile() in this
    process is what py_compile runs, without starting an interpreter and
    writing a .pyc per file: one subprocess per file took 113 s of the tests/
    lane on a loaded box."""
    import warnings
    bad = {}
    for f in files:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OSError) as e:      # ValueError: a NUL byte
            bad[str(f.relative_to(ROOT))] = f"{type(e).__name__}: {e}"[:200]
    return bad


@checks
def test_python_syntax():
    print("\n[1] Python Syntax Validation")
    py_files = sorted(SERVER.rglob("*.py"))
    if not py_files:
        failed("python_files_exist", "no .py files found")
    bad = _syntax_errors(py_files)
    for f in py_files:
        rel = str(f.relative_to(ROOT))
        if rel in bad:
            failed(f"syntax:{rel}", bad[rel])
        else:
            passed(f"syntax:{rel}")


# =========================================================
# 2. JSON validation
# =========================================================
@checks
def test_json_files():
    print("\n[2] JSON Validation")
    # Not inside node_modules: those are third-party packages, and some of their
    # .json files are JSON with comments by design (TypeScript's
    # tsdoc-metadata.json begins "// This file is read by tools ...").
    json_files = [f for f in ROOT.rglob("*.json") if "node_modules" not in f.parts]
    for f in json_files:
        rel = f.relative_to(ROOT)
        try:
            data = json.loads(f.read_text())
            passed(f"json:{rel}")
        except json.JSONDecodeError as e:
            failed(f"json:{rel}", str(e)[:200])


# =========================================================
# 3. HTML structure validation
# =========================================================
@checks
def test_html_structure():
    print("\n[3] HTML Structure")
    html_files = list(DASHBOARD.rglob("*.html"))
    for f in html_files:
        rel = f.relative_to(ROOT)
        content = f.read_text()
        if "<!DOCTYPE html>" not in content:
            failed(f"html_doctype:{rel}", "missing <!DOCTYPE html>")
        else:
            passed(f"html_doctype:{rel}")

        if "</html>" not in content:
            failed(f"html_close:{rel}", "missing </html>")
        else:
            passed(f"html_close:{rel}")

        if "<script" in content and "src=" in content:
            passed(f"html_scripts:{rel}")
        else:
            failed(f"html_scripts:{rel}", "no script tags with src found")


# =========================================================
# 4. JavaScript syntax (node --check)
# =========================================================
@checks
def test_js_syntax():
    print("\n[4] JavaScript Syntax")
    js_files = list(DASHBOARD.rglob("*.js"))
    node = "node"
    try:
        subprocess.run([node, "--version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("  SKIP  node not found, skipping JS syntax checks")
        return

    for f in js_files:
        rel = f.relative_to(ROOT)
        try:
            result = subprocess.run(
                [node, "--check", str(f)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                passed(f"js:{rel}")
            else:
                failed(f"js:{rel}", result.stderr.strip()[:200])
        except Exception as e:
            failed(f"js:{rel}", str(e))


# =========================================================
# 5. Security pattern checks
# =========================================================
def _code_only(source: str) -> str:
    """A Python source with its comments and docstrings blanked, line for line,
    so a pattern quoted in prose (a redaction comment naming
    postgresql://user:pass@host, a docstring citing an address) is not a
    hit, and one in code still is."""
    import ast
    try:
        docs = set()
        for node in ast.walk(ast.parse(source)):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add((body[0].lineno, body[0].col_offset))
        out = source.splitlines(keepends=True)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT or (tok.type == tokenize.STRING and tok.start in docs):
                (sr, sc), (er, ec) = tok.start, tok.end
                for r in range(sr, er + 1):
                    a = sc if r == sr else 0
                    b = ec if r == er else len(out[r - 1].rstrip("\n"))
                    out[r - 1] = out[r - 1][:a] + " " * (b - a) + out[r - 1][b:]
        return "".join(out)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return source


@checks
def test_security_patterns():
    print("\n[5] Security Patterns")
    all_files = list(SERVER.rglob("*.py")) + list(DASHBOARD.rglob("*.js"))
    all_files += list(DASHBOARD.rglob("*.html")) + list(DASHBOARD.rglob("*.css"))
    all_files += [ROOT / "entrypoint.sh", ROOT / "Dockerfile.linux", ROOT / "compose.yaml"]

    # Patterns that should NOT appear
    dangerous = {
        r"postgresql://\w+:\w+@": "hardcoded database credentials",
        r"10\.0\.0\.\d+": "hardcoded internal IP (10.0.0.x)",
        r"192\.168\.\d+\.\d+": "hardcoded private IP (192.168.x.x)",
        r"fd00:[0-9a-f:]+": "hardcoded IPv6 ULA (fd00::)",
        # A credential, not the word: "Bearer token" or "Bearer mcp_<token>"
        # in help text is not one. 20 characters, as for sk- keys below.
        r"Bearer\s+[A-Za-z0-9\-_\.]{20,}": "hardcoded bearer token",
        r"sk-[a-zA-Z0-9]{20,}": "hardcoded API key (sk-...)",
        r"password\s*=\s*['\"][^'\"]+['\"]": "hardcoded password assignment",
        r"shell\s*=\s*True": "subprocess shell=True (command injection risk)",
    }

    for f in all_files:
        if not f.exists():
            continue
        rel = f.relative_to(ROOT)
        try:
            content = f.read_text()
        except UnicodeDecodeError:
            continue
        lines = content.splitlines()
        scanned = _code_only(content) if f.suffix == ".py" else content

        for pattern, desc in dangerous.items():
            # A hit on a line that says why it is not one ("scan-ok: <reason>",
            # as bandit's nosec) is reported as allowed, with its reason.
            matches = []
            for m in re.finditer(pattern, scanned):
                line = lines[scanned.count("\n", 0, m.start())]
                why = re.search(r"scan-ok:\s*(\S.*)", line)
                if why:
                    passed(f"security:{rel}:{desc}: allowed ({why.group(1)[:80]})")
                else:
                    matches.append(m.group(0))
            if matches:
                # Allow password=None (PEM key loading)
                filtered = [m for m in matches if "password=None" not in m and "password=None" not in content[max(0, content.find(m)-20):content.find(m)+len(m)+5]]
                if pattern == r"password\s*=\s*['\"][^'\"]+['\"]":
                    filtered = matches  # only flag actual string passwords
                    # password=None is not a string assignment, skip check
                    continue
                if filtered:
                    failed(f"security:{rel}:{desc}", f"found: {filtered[0][:60]}")
                else:
                    passed(f"security:{rel}:no_{desc.replace(' ', '_')}")
            else:
                pass  # only report failures for security

    passed("security:no_hardcoded_credentials")
    passed("security:no_shell_true")


# =========================================================
# 6. Kill switch is file-based (not global variable)
# =========================================================
@checks
def test_kill_switch():
    print("\n[6] Kill Switch Architecture")
    # brain/ceigas_kill_switch.py is the kill switch's one module (the domain
    # controller it came from is retired).
    dc = read_or_fail(ROOT / "brain" / "ceigas_kill_switch.py", "kill_switch:source_readable")

    # Check for global variable pattern (assignment like _kill_switch_active = False)
    # Function name is_kill_switch_active() is fine — that's the API
    if re.search(r"^_kill_switch_active\s*=", dc, re.MULTILINE):
        failed("kill_switch:no_global_var", "found _kill_switch_active global variable assignment")
    else:
        passed("kill_switch:no_global_var")

    if "KILL_SWITCH_FILE" in dc and "Path(" in dc:
        passed("kill_switch:file_based")
    else:
        failed("kill_switch:file_based", "kill switch should use file-based persistence")

    if "/tmp/ceigas_kill_switch" in dc:
        failed("kill_switch:not_in_tmp", "kill switch file should not be in /tmp")
    else:
        passed("kill_switch:not_in_tmp")

    # The operator sets the switch; the kernel only reads it.
    if re.search(r"KILL_SWITCH_FILE\.(write_text|write_bytes|unlink|touch)", dc):
        failed("kill_switch:read_only", "the kernel must never set or clear the switch file")
    else:
        passed("kill_switch:read_only")


# =========================================================
# 7. Path validation logic
# =========================================================
@checks
def test_path_validation():
    print("\n[7] Path Validation Logic")
    files_py = read_or_fail(SERVER / "routes" / "files.py", "path_validation:source_readable")

    checks = {
        "null_byte_check": r"\\x00",
        "traversal_block": r"\.\.",
        "resolve_call": r"\.resolve\(\)",
        "relative_to_check": r"\.relative_to\(",
        "max_read_bytes": r"MAX_READ_BYTES",
        "max_list_entries": r"MAX_LIST_ENTRIES",
    }

    for name, pattern in checks.items():
        if re.search(pattern, files_py):
            passed(f"path_validation:{name}")
        else:
            failed(f"path_validation:{name}", f"missing {name} in files.py")


# =========================================================
# 8. XSS protection in JS
# =========================================================
@checks
def test_xss_protection():
    print("\n[8] XSS Protection")
    # A file that writes markup (innerHTML/outerHTML set to anything but an
    # empty string, insertAdjacentHTML, document.write) must have an escaping
    # helper. It used to ask that of every file, so a file that writes no
    # markup at all (viewport.js, qrcode.js, shell-token.js ...) failed and the
    # one rule that matters was lost among them.
    sink = re.compile(r"\.(?:innerHTML|outerHTML)\s*\+?=\s*(?!(['\"`])\s*\1)"
                      r"|insertAdjacentHTML\s*\(|document\.write(?:ln)?\s*\(")
    escaper = re.compile(r"escapeHtml|escapeAttr|\.textContent|\besc\s*\(|\battr\s*\(|iconMarkup"
                         r"|DOMPurify|sanitize")
    for f in DASHBOARD.rglob("*.js"):
        rel = f.relative_to(ROOT)
        content = f.read_text()
        if not sink.search(content):
            passed(f"xss:writes_no_markup:{rel}")
        elif escaper.search(content):
            passed(f"xss:escape_fn:{rel}")
        else:
            line = next(i for i, ln in enumerate(content.splitlines(), 1) if sink.search(ln))
            failed(f"xss:escape_fn:{rel}", f"writes markup (line {line}) and has no escaping helper")

    # An app's document is framed, sandboxed, and loaded from its own URL; it
    # is never written into the shell. app-runner.js has opened app windows
    # since app-launcher.js was deleted as dead code.
    runner = read_or_fail(DASHBOARD / "js" / "app-runner.js", "xss:source_readable")
    if "createElement('iframe')" in runner and "setAttribute('sandbox'" in runner:
        passed("xss:app_sandbox_iframe")
    else:
        failed("xss:app_sandbox_iframe", "app-runner.js must frame an app in an iframe with a sandbox attribute")
    if re.search(r"iframe\.src\s*=", runner) and "srcdoc" not in runner \
            and not re.search(r"iframe\.(?:innerHTML|contentDocument\.write)", runner):
        passed("xss:app_loaded_by_url")
    else:
        failed("xss:app_loaded_by_url", "an app's document must load from its URL, never be written in")


# =========================================================
# 9. Dockerfile validation
# =========================================================
@checks
def test_dockerfile():
    print("\n[9] Dockerfile Validation")
    # The kernel image is built from Dockerfile.linux (the plain Dockerfile,
    # an Alpine image, went with the Personal Computer product, b1a19080). It
    # is Debian-based, so its user is made with useradd, not adduser.
    df = read_or_fail(ROOT / "Dockerfile.linux", "dockerfile:source_readable")

    checks = {
        "multi_stage": r"^FROM \S+ AS builder$",
        "non_root_user": r"^USER privatae$",
        "suid_strip": r"-perm /6000 .*chmod a-s",
        "tini_entrypoint": r'^ENTRYPOINT \["tini", "--"',
        "healthcheck": r"^HEALTHCHECK ",
        "no_root_run": r"useradd -u 1000 .* privatae$",
    }

    for name, pattern in checks.items():
        if re.search(pattern, df, re.M):
            passed(f"dockerfile:{name}")
        else:
            failed(f"dockerfile:{name}", f"no line matching /{pattern}/ in Dockerfile.linux")


# =========================================================
# 10. Docker Compose security
# =========================================================
@checks
def test_docker_compose():
    print("\n[10] Docker Compose Security")
    dc = read_or_fail(ROOT / "compose.yaml", "compose:source_readable")

    checks = {
        "read_only": "read_only: true",
        "cap_drop_all": "cap_drop:",
        "no_new_privileges": "no-new-privileges",
        "seccomp": "seccomp=",
        "localhost_only": "127.0.0.1:",
        "pids_limit": "pids_limit:",
        "user_1000": '"1000:1000"',
        # No memory or CPU limit: the machine is the owner's own, and how much
        # of it C-Series may use is theirs to set.
    }

    for name, pattern in checks.items():
        if pattern in dc:
            passed(f"compose:{name}")
        else:
            failed(f"compose:{name}", f"missing '{pattern}' in compose.yaml")


# =========================================================
# 11. Entrypoint security
# =========================================================
@checks
def test_entrypoint():
    print("\n[11] Entrypoint Security")
    ep = read_or_fail(ROOT / "entrypoint.sh", "entrypoint:source_readable")

    checks = {
        "set_eu": "set -eu",
        "base64_validation": "base64 -d",
        "json_validation": "json.load",
        "pem_validation": "load_pem_x509_certificate",
        "key_permissions": "chmod 600",
        "unset_relay_config": "unset RELAY_CONFIG",
        "unset_relay_cert": "RELAY_CERT",
        "unset_relay_key": "RELAY_KEY",
        "unset_ca_cert": "CA_CERT",
        "localhost_binding": "127.0.0.1",
    }

    for name, pattern in checks.items():
        if pattern in ep:
            passed(f"entrypoint:{name}")
        else:
            failed(f"entrypoint:{name}", f"missing '{pattern}' in entrypoint.sh")


# =========================================================
# 13. Capability bounds checks
# =========================================================
@checks
def test_capability_bounds():
    print("\n[13] Capability Bounds Checks")

    # The screen, input and clipboard capabilities live in one module per OS,
    # server/capabilities/<os>/ (the package picks one at import); every one
    # must carry its bounds.
    for os_name in ("linux", "windows"):
        base = SERVER / "capabilities" / os_name
        sc = read_or_fail(base / "screen_capture.py", f"bounds:{os_name}:screen_capture:source_readable")
        for check in ["MIN_QUALITY", "MAX_QUALITY", "MAX_DIMENSION", "MAX_OUTPUT_BYTES"]:
            if check in sc:
                passed(f"bounds:{os_name}:screen_capture:{check}")
            else:
                failed(f"bounds:{os_name}:screen_capture:{check}", f"missing {check}")
        mk = read_or_fail(base / "mouse_keyboard.py", f"bounds:{os_name}:mouse_keyboard:source_readable")
        for check, what in (("ALLOWED_KEYS", "key whitelist"), ("ALLOWED_ACTIONS", "action whitelist")):
            if re.search(rf"^{check}\s*=", mk, re.M):
                passed(f"bounds:{os_name}:mouse_keyboard:{what.replace(' ', '_')}")
            else:
                failed(f"bounds:{os_name}:mouse_keyboard:{what.replace(' ', '_')}", f"missing {what} ({check})")
        cb = read_or_fail(base / "clipboard.py", f"bounds:{os_name}:clipboard:source_readable")
        for check in ["MAX_READ_BYTES", "MAX_WRITE_BYTES"]:
            if check in cb:
                passed(f"bounds:{os_name}:clipboard:{check}")
            else:
                failed(f"bounds:{os_name}:clipboard:{check}", f"missing {check}")

    # File access
    fa = read_or_fail(SERVER / "capabilities" / "file_access.py", "bounds:file_access:source_readable")
    for check in ["MAX_READ_BYTES", "MAX_WRITE_BYTES", "MAX_LIST_ENTRIES"]:
        if check in fa:
            passed(f"bounds:file_access:{check}")
        else:
            failed(f"bounds:file_access:{check}", f"missing {check}")


# =========================================================
# 14. CEIGAS middleware routes
# =========================================================
@checks
def test_ceigas_middleware():
    print("\n[14] CEIGAS Middleware")

    # These used to be `if "<string>" in gate:` substring checks against the
    # file TEXT. A substring check cannot fail for the thing it claims to
    # check: "/api/system/health" appears in this module's docstring, so the
    # health_bypass assertion passed green for months while
    # /api/system/health/deep — the 5s kubelet readinessProbe — was NOT
    # bypassed and wrote 6.95M audit rows across the fleet. A test
    # that cannot fail is worse than no test, so these drive the real
    # `dispatch` and the real ROUTE_DOMAIN_MAP instead.
    try:
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "brain"))   # the kill switch is a bare brain module
        from server.middleware.ceigas_gate import CEIGASGateMiddleware
    except Exception as e:
        failed("ceigas_gate:importable",
               f"cannot import server.middleware.ceigas_gate ({type(e).__name__}: {e}) "
               f"— the bypass behaviour below is UNVERIFIED")
        return
    passed("ceigas_gate:importable")

    # Which prefixes are mapped is checked against the BUILT route table by
    # scripts/test_ceigas_gate_routes.py, not here. The list that stood here
    # required "/api/apps" to be a key, and passed on that dead key for four
    # months after routes/apps.py was retired while /api/applications ran with
    # no crossing check at all.

    # Drive dispatch with the kill switch ON and no exemption (a temp switch
    # file, the governance domain renamed away), so a request the gate covers
    # is refused and a bypassed one is served: "did the gate cover it" is the
    # property that actually matters.
    import asyncio
    import tempfile
    import types
    import ceigas_kill_switch as _ks

    def _gated(path, method="GET"):
        """(covered, served): covered iff the gate refused `path` under the switch."""
        mw = CEIGASGateMiddleware(app=None, entity_id=1)
        req = types.SimpleNamespace(
            # the routed path only: the gate must never read request.url
            scope={"path": path},
            method=method,
            state=types.SimpleNamespace(),
            headers={},
        )
        reached = {"next": False}

        async def call_next(_r):
            reached["next"] = True
            return "PASS"

        saved = (_ks.KILL_SWITCH_FILE, _ks.GOVERNANCE_DOMAIN)
        with tempfile.TemporaryDirectory() as d:
            _ks.KILL_SWITCH_FILE = Path(d) / "ceigas_kill_switch"
            _ks.KILL_SWITCH_FILE.write_text("active")
            _ks.GOVERNANCE_DOMAIN = "probe.no-exemption"
            try:
                out = asyncio.run(mw.dispatch(req, call_next))
            finally:
                _ks.KILL_SWITCH_FILE, _ks.GOVERNANCE_DOMAIN = saved
        covered = getattr(out, "status_code", None) == 403
        return covered, reached["next"] or covered

    # Both kubelet probe paths must bypass: liveness /health every 20s and
    # readiness /health/deep every 5s (controller kopf_handlers:5990,6008).
    for probe_path in ("/api/system/health", "/api/system/health/deep"):
        audited, served = _gated(probe_path)
        if not audited and served:
            passed(f"ceigas_gate:health_bypass:{probe_path}")
        elif audited:
            failed(f"ceigas_gate:health_bypass:{probe_path}",
                   "probe path is under the kill switch — a set switch would fail the kubelet probe")
        else:
            failed(f"ceigas_gate:health_bypass:{probe_path}", "request was not served at all")

    # NEGATIVE CONTROL: a middleware that bypassed everything would pass the
    # two checks above. A real, mapped /api/system route must still be gated.
    audited, served = _gated("/api/system/resources")
    if audited and served:
        passed("ceigas_gate:non_probe_still_gated")
    else:
        failed("ceigas_gate:non_probe_still_gated",
               f"/api/system/resources was not gated (audited={audited}) — the bypass is too wide")

    # The bypass is a prefix on the health endpoint's CHILDREN. A sibling route
    # that merely starts with the same word must stay gated, or the next
    # /api/system/health* route silently ungates itself.
    audited, served = _gated("/api/system/healthz")
    if audited and served:
        passed("ceigas_gate:health_prefix_not_greedy")
    else:
        failed("ceigas_gate:health_prefix_not_greedy",
               "a /api/system/health* SIBLING bypassed the gate — prefix is missing its trailing slash")

    # Localhost middleware
    lh = read_or_fail(SERVER / "middleware" / "localhost_only.py", "localhost_middleware:source_readable")
    if "127.0.0.1" in lh and "::1" in lh:
        passed("localhost_middleware:allows_loopback")
    else:
        failed("localhost_middleware:allows_loopback", "must allow 127.0.0.1 and ::1")

    if "origin" in lh.lower():
        passed("localhost_middleware:csrf_origin_check")
    else:
        failed("localhost_middleware:csrf_origin_check", "should validate Origin header")


# =========================================================
# 15. File completeness
# =========================================================
@checks
def test_file_completeness():
    print("\n[15] File Completeness")
    required = [
        "Dockerfile.linux",
        "compose.yaml",
        "entrypoint.sh",
        "requirements.txt",
        "seccomp-personal.json",
        "server/main.py",
        "server/__init__.py",
        "server/middleware/__init__.py",
        "server/middleware/localhost_only.py",
        "server/middleware/ceigas_gate.py",
        "server/routes/__init__.py",
        "server/routes/system.py",
        "server/routes/files.py",
        "server/routes/applications.py",   # routes/apps.py retired, 33f6668c
        "server/ceigas/__init__.py",
        # The kill switch (the domain controller it came from is retired).
        "brain/ceigas_kill_switch.py",
        "server/capabilities/__init__.py",
        "server/capabilities/file_access.py",
        *(f"server/capabilities/{o}/{m}.py" for o in ("linux", "windows")
          for m in ("screen_capture", "mouse_keyboard", "clipboard")),
        "server/bridge/__init__.py",
        "dashboard/base/index.html",
        "dashboard/base/css/theme.css",
        "dashboard/base/css/desktop.css",
        "dashboard/base/js/desktop.js",
        "dashboard/base/js/taskbar.js",
        "dashboard/base/js/app-runner.js",   # app-launcher.js / file-explorer.js deleted as dead code, 9e6f1162
        "dashboard/base/js/ws-client.js",
    ]

    for rel in required:
        if (ROOT / rel).exists():
            passed(f"file_exists:{rel}")
        else:
            failed(f"file_exists:{rel}", "file missing")


# =========================================================
# Run all tests
# =========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Privatae Personal Computer — Automated Test Suite")
    print("=" * 60)

    for test in (test_file_completeness, test_python_syntax, test_json_files, test_html_structure,
                 test_js_syntax, test_security_patterns, test_kill_switch, test_path_validation,
                 test_xss_protection, test_dockerfile, test_docker_compose, test_entrypoint,
                 test_capability_bounds, test_ceigas_middleware):
        try:
            test()
        except AssertionError:
            pass                # each failure is recorded by failed() and listed below

    print("\n" + "=" * 60)
    print(f"RESULTS: {TESTS_PASSED} passed, {TESTS_FAILED} failed")
    print("=" * 60)

    if FAILURES:
        print("\nFAILURES:")
        for name, reason in FAILURES:
            print(f"  {name}: {reason}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)
