#!/usr/bin/env python3
"""The headers server/main.py's security_headers middleware puts on every
response.

Kept: X-Content-Type-Options nosniff, Referrer-Policy no-referrer, and the
framing policy (an app origin's CSP frame-ancestors, else X-Frame-Options
SAMEORIGIN). Gone: X-XSS-Protection. Browsers dropped the filter it switched
on (Chrome, Edge and Safari removed it; Firefox never had it), and where it
lingered its block mode could itself be used to blank parts of a page; OWASP
advises leaving the header out.

Reads the middleware's own source (importing server.main needs the whole
runtime). Run: python3 scripts/test_security_headers.py
"""
import ast
import os
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILED.append(label)


src = open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8").read()
fn = next((n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "security_headers"), None)
check("server/main.py defines the security_headers middleware", fn is not None)
set_headers = {}
if fn is not None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str) and ast.unparse(t.value) == "response.headers"):
                    set_headers[t.slice.value.lower()] = ast.unparse(node.value)
check("every response says nosniff", set_headers.get("x-content-type-options") == "'nosniff'")
check("every response says Referrer-Policy no-referrer", set_headers.get("referrer-policy") == "'no-referrer'")
check("a response that is not an app's is SAMEORIGIN-framed", set_headers.get("x-frame-options") == "'SAMEORIGIN'")
check("an app origin's response gets its CSP (frame-ancestors)", "content-security-policy" in set_headers)
check("no response carries the retired X-XSS-Protection header", "x-xss-protection" not in set_headers)

if FAILED:
    print(f"test_security_headers: {len(FAILED)} FAILED")
    sys.exit(1)
print("test_security_headers: all checks passed")
