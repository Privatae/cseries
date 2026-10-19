#!/usr/bin/env python3
"""Ratchet for the Sites Database tab credential fix: the masked DATABASE_URL
must never contain the password, and anything unparseable masks to None (the
UI displays nothing rather than risking a leak). Leaf-module import — no
server package side effects."""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "dburl", pathlib.Path(__file__).resolve().parent.parent
    / "server" / "applications" / "dburl.py")
dburl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dburl)
mask = dburl.mask_db_url

failures = []


def check(name, cond):
    if cond:
        print(f"OK    {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}")


PW = "s3cr3t-Pa55w0rd"
url = f"postgresql://siteowner_1:{PW}@172.20.0.1:5432/site_1"
m = mask(url)
check("standard URL masks", m == "postgresql://siteowner_1:••••@172.20.0.1:5432/site_1")
check("password absent from mask", m is not None and PW not in m)

# Passwords containing ':' and '@' — greedy-to-last-@ must still strip fully.
tricky = f"postgres://u:p@ss:w@rd@db.example.com/app"
mt = mask(tricky)
check("tricky password fully stripped",
      mt == "postgres://u:••••@db.example.com/app" and (mt is None or "p@ss" not in mt))

check("no userinfo -> None (never echoed)", mask("postgres://host:5432/db") is None)
check("empty -> None", mask("") is None)
check("non-string -> None", mask(None) is None)
check("garbage -> None", mask("not a url at all") is None)

# Password repeated elsewhere in the URL (e.g. query param) must refuse to mask.
leaky = f"postgres://u:{PW}@h/db?fallback={PW}"
check("repeated password refuses to mask", mask(leaky) is None)

if failures:
    print(f"\nsites-db mask gate: FAIL ({len(failures)})")
    sys.exit(1)
print("\nsites-db mask gate: PASS")
