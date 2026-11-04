#!/usr/bin/env python3
"""Sign in to the Host as its owner and save the browser's session to a jar.

usage: login.py <jar.json>
Reads CSERIES_HOST_URL, CSERIES_OWNER_EMAIL and CSERIES_OWNER_PASSWORD from the
environment (never from the command line, which other users can read)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ct_lib import test_host_or_exit  # noqa: E402
B = test_host_or_exit()   # before playwright: a refused run touches nothing
from playwright.sync_api import sync_playwright  # noqa: E402

email = os.environ.get("CSERIES_OWNER_EMAIL", "")
password = os.environ.get("CSERIES_OWNER_PASSWORD", "")
if not email or not password:
    sys.exit("set CSERIES_OWNER_EMAIL and CSERIES_OWNER_PASSWORD")
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 1000})
    page = ctx.new_page()
    page.goto(B + "/")
    page.wait_for_timeout(3000)
    if "/login" in page.url:
        page.locator("input[type=email]:visible").fill(email)
        page.locator("input[type=password]:visible").fill(password)
        page.locator("button[type=submit]:visible").first.click()
        page.wait_for_timeout(5000)
    ok = "/login" not in page.url
    print(f"signed in: {ok} ({page.url})")
    ctx.storage_state(path=sys.argv[1])
    b.close()
    sys.exit(0 if ok else 1)
