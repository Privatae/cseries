#!/usr/bin/env python3
"""Run the release click-through against one Host, in one command.

usage: run.py <outdir> [--steps "state race simple local ..."] [--sweep] [--sweep-modes "normal hold-token"]

Signs in (login.py), runs the click-through steps (drive.py), and with --sweep
the every-app token sweep (token_sweep.py), writing every transcript under
<outdir>. Exit 1 when a step's script failed; read <outdir>/*/steps.log and the
sweep's *.json for what each step saw. See README.md."""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STEPS = ("state race simple local kimi-models connect-cursor "
                 "claude-signin connect-claude chat")

ap = argparse.ArgumentParser()
ap.add_argument("outdir")
ap.add_argument("--steps", default=DEFAULT_STEPS)
ap.add_argument("--sweep", action="store_true")
ap.add_argument("--sweep-modes", default="normal")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
jar = os.path.join(a.outdir, "jar.json")
fails = 0


def run(args, timeout):
    global fails
    t = time.time()
    r = subprocess.run([sys.executable] + args, timeout=timeout)
    print(f"-- {os.path.basename(args[0])} {' '.join(args[1:])[-80:]}: exit {r.returncode} "
          f"in {time.time() - t:.0f} s", flush=True)
    if r.returncode:
        fails += 1
    return r.returncode


if run([os.path.join(HERE, "login.py"), jar], 120):
    sys.exit("could not sign in (CSERIES_HOST_URL, CSERIES_OWNER_EMAIL, CSERIES_OWNER_PASSWORD)")
steps_dir = os.path.join(a.outdir, "clickthrough")
os.makedirs(steps_dir, exist_ok=True)
run([os.path.join(HERE, "drive.py"), jar, steps_dir] + a.steps.split(), 3600)
if a.sweep:
    sweep_dir = os.path.join(a.outdir, "token-sweep")
    os.makedirs(sweep_dir, exist_ok=True)
    for m in a.sweep_modes.split():
        run([os.path.join(HERE, "token_sweep.py"), jar, sweep_dir, m], 3600)
os.remove(jar)   # a live session: never left beside the transcripts
sys.exit(1 if fails else 0)
