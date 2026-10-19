#!/usr/bin/env python3
"""gate_timing.py — what each pre-merge gate cost, the order the next run
starts them in, and how a run could have been faster (2026-10-06).

WHY (measured on the 447-gate run on main, 2026-10-06 12:26, 8 slots): 825 s
wall. One gate, test_connector_telegram_example.py, took 548 s, and the
gates' own time summed to 2,766 s, so 8 slots needed only 346 s; the run
waited on that one gate, which the list order started wherever it happened
to sit. Then tests/ ran 222 s on its own after every gate had finished.
Nothing measured any of this, so nothing got faster as the list grew from
55 gates to 447. Every run now records each gate's time, starts the gates
that took longest last time first, and ends with what held it up.

pre_merge_gates.sh calls it three ways:
  order  --history H          gate names on stdin, in the order to start them:
                              gates with no recorded time first (unknown, so
                              they may be long), in the order given, then the
                              rest longest first
  report --run R --history H --jobs N --started T [--dir D]
                              the "how this run could have been faster" section
  record --run R --dir D [--tree SHA]
                              H := H overlaid with this run (gates that did not
                              run keep their last time); D/<tree>.timings.tsv
                              for a clean tree; the newest 100 of those kept

A run file R has one row per gate or lane: name, start, end (epoch seconds),
exit code, kind (gate = in the parallel pool, alone = run by itself after it,
alone-retry = a load-sensitive gate's second run, alone, after it failed in the
pool, so that gate has two rows; lane = undefined names, run after the gates;
beside = tests/, run beside the gates from their start, its end the moment its
pytest exited), and the
gate's `times` output (user and system CPU of everything it ran). A history
file H: name, wall s, CPU s, when. A gate's history keeps its pool time, which
is what the order is for.
Anything unreadable is skipped: timing never fails a run.
"""
import os
import re
import sys
import tempfile
import time

KEEP_TREES = 100
SLOWER_RATIO, SLOWER_MIN_S = 1.5, 5.0


def _num(s):
    return float(s.replace(",", "."))   # EPOCHREALTIME follows the locale's decimal mark


def read_run(path):
    rows = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return rows
    for line in lines:
        f = line.split("\t")
        if len(f) < 5:
            continue
        try:
            start, end = _num(f[1]), _num(f[2])
        except ValueError:
            continue
        cpu = None
        if len(f) > 5:
            parts = re.findall(r"(\d+)m([\d.,]+)s", f[5])
            if len(parts) >= 2:      # `times`: the shell's own line, then its children's
                cpu = sum(int(m) * 60 + _num(s) for m, s in parts[-2:])
        rows.append({"name": f[0], "start": start, "end": end, "dur": max(0.0, end - start),
                     "rc": f[3], "kind": f[4], "cpu": cpu})
    return rows


def read_history(path):
    hist = {}
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except (OSError, TypeError):
        return hist
    for line in lines:
        if line.startswith("#"):
            continue
        f = line.split("\t")
        try:
            hist[f[0]] = (float(f[1]), float(f[2]) if f[2] else None, f[3] if len(f) > 3 else "")
        except (IndexError, ValueError):
            continue
    return hist


def order(hist, names):
    unknown = [n for n in names if n not in hist]
    known = sorted((n for n in names if n in hist), key=lambda n: -hist[n][0])
    return unknown + known


def _s(x):
    return f"{x:.0f}s" if x >= 10 else f"{x:.1f}s"


def _label(r):
    return r["name"] + (" (retried alone)" if r["kind"] == "alone-retry" else "")


def report(rows, hist, jobs, started, now, where):
    out = ["", "── How this run could have been faster ─────────────────────────────"]
    if not rows:
        return out + ["No gate ran, so there is nothing to measure."]
    wall = max(0.0, now - started)
    pool = [r for r in rows if r["kind"] == "gate"]
    after = [r for r in rows if r["kind"] in ("alone", "alone-retry", "lane")]
    # A lane run beside the gates is neither "one at a time" nor advice to move
    # it beside them: it is said with its own duration, next to the gate phase.
    beside = [r for r in rows if r["kind"] == "beside"]
    with_it = (", beside them: " + ", ".join(f"{r['name']} {_s(r['dur'])}" for r in beside)) if beside else ""
    every = sum(r["dur"] for r in rows)
    if pool:
        p0, p1 = min(r["start"] for r in pool), max(r["end"] for r in pool)
        pwall = max(p1 - p0, 1e-9)
        total = sum(r["dur"] for r in pool)
        cpus = [r["cpu"] for r in pool if r["cpu"] is not None]
        longest = max(pool, key=lambda r: r["dur"])
        floor = max(longest["dur"], total / jobs)
        shown = [r for r in after if r["dur"] >= 1]
        tail = f", then one at a time: {', '.join(_label(r) + ' ' + _s(r['dur']) for r in shown)}" if shown else ""
        out.append(f"Wall {_s(wall)}. Gates {_s(pwall)} ({len(pool)} gates, {jobs} at a time){with_it}{tail}.")
        out.append(f"Gate time {_s(total)}" + (f" (CPU {_s(sum(cpus))})" if cpus else "")
                   + f". The {jobs} slots were busy {100 * total / (pwall * jobs):.0f}% of the gate phase.")
        what = ("the longest gate, " + longest["name"] if floor == longest["dur"]
                else f"all gate time ÷ {jobs}")
        other = (f"all gate time ÷ {jobs} is {_s(total / jobs)}" if floor == longest["dur"]
                 else f"the longest gate is {_s(longest['dur'])}")
        out.append(f"Floor for the gate phase: {_s(floor)} ({what}; {other}). "
                   f"It took {_s(pwall)}, {_s(max(0.0, pwall - floor))} over.")
        last = max(pool, key=lambda r: r["end"])
        others_end = max((r["end"] for r in pool if r is not last), default=last["start"])
        alone = max(0.0, last["end"] - max(others_end, last["start"]))
        out.append(f"Finished last: {last['name']} ({_s(last['dur'])}), started at +{_s(last['start'] - p0)}, "
                   f"ran {_s(alone)} with no other gate running.")
    else:
        out.append(f"Wall {_s(wall)}; no gate ran in the parallel pool{with_it}.")
    # A retry runs alone on purpose, so it is never advice to run it beside the gates.
    lanes = [r for r in after if r["dur"] >= 1 and r["kind"] != "alone-retry"]
    if pool and lanes:
        idle = pwall * jobs - total
        out.append(f"Run beside the gates instead of after them, {', '.join(r['name'] for r in lanes)} "
                   f"could end the run up to {_s(sum(r['dur'] for r in lanes))} sooner "
                   f"(the gate phase left {_s(idle)} of slot time idle).")
    out.append("Slowest 10 (share of all gate and lane time):")
    for r in sorted(rows, key=lambda r: -r["dur"])[:10]:
        out.append(f"  {r['dur']:7.1f}s {100 * r['dur'] / every if every else 0:5.1f}%  {_label(r)}")
    if not hist:
        out.append("No previous run recorded " + (f"in {where}" if where else "here")
                   + " to compare against; the next run will have this one.")
    else:
        slower = sorted(((r["name"], hist[r["name"]][0], r["dur"]) for r in rows if r["name"] in hist
                         and r["kind"] != "alone-retry"
                         and r["dur"] > hist[r["name"]][0] * SLOWER_RATIO
                         and r["dur"] - hist[r["name"]][0] > SLOWER_MIN_S), key=lambda x: x[1] - x[2])
        if slower:
            shown = "; ".join(f"{n} {_s(a)} → {_s(b)}" for n, a, b in slower[:5])
            more = f"; and {len(slower) - 5} more" if len(slower) > 5 else ""
            out.append(f"Slower than the last recorded run (over 50% and 5 s): {shown}{more}.")
        else:
            out.append("No gate got markedly slower than the last recorded run (over 50% and 5 s).")
    return out


def _write(path, rows):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".timings-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# pre-merge gate timings: name, wall s, CPU s, recorded (UTC)\n")
        for name, (w, c, when) in sorted(rows.items()):
            f.write(f"{name}\t{w:.2f}\t{'' if c is None else f'{c:.2f}'}\t{when}\n")
    os.chmod(tmp, 0o644)    # mkstemp makes it 0600; any account's run may read the history
    os.replace(tmp, path)   # atomic: a run reading it sees the old file or the new one


def record(rows, d, tree):
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    latest = os.path.join(d, "timings-latest.tsv")
    hist = read_history(latest)
    mine = {}
    for r in rows:
        if r["kind"] == "alone-retry" and r["name"] in mine:
            continue    # keep the pool row: the order it feeds is the pool's
        mine[r["name"]] = (r["dur"], r["cpu"], when)
    hist.update(mine)
    _write(latest, hist)
    if tree and re.fullmatch(r"[0-9a-f]{40,64}", tree):
        _write(os.path.join(d, f"{tree}.timings.tsv"), mine)
        trees = sorted((os.path.join(d, n) for n in os.listdir(d) if n.endswith(".timings.tsv")),
                       key=lambda p: os.path.getmtime(p), reverse=True)
        for p in trees[KEEP_TREES:]:
            try:
                os.remove(p)
            except OSError:
                pass


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], {}
    it = iter(argv[1:])
    for a in it:
        if a.startswith("--"):
            args[a[2:]] = next(it, "")
    if cmd == "order":
        names = [l.strip() for l in sys.stdin if l.strip()]
        print("\n".join(order(read_history(args.get("history")), names)))
        return 0
    if cmd == "report":
        rows = read_run(args["run"])
        lines = report(rows, read_history(args.get("history")), max(1, int(args.get("jobs") or 1)),
                       _num(args.get("started") or "0"), time.time(), args.get("dir", ""))
        print("\n".join(lines))
        return 0
    if cmd == "record":
        record(read_run(args["run"]), args["dir"], args.get("tree", ""))
        return 0
    print(f"gate_timing: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
