#!/usr/bin/env python3
"""Markdown tables for PROFILING.md, read from profiling/results/.

    python3 profiling/report_tables.py [startup|timeouts|run NAME|build NAME]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

R = Path(__file__).resolve().parent / "results"


def startup():
    rows = json.loads((R / "startup" / "startup.json").read_text())
    print("| launch | CPG MB | to script main (s) | of which script JVM (s) | CPG load (s) | heap after load (MB) | process exit (s) |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['kind']} {r.get('repo', '')} | {r.get('cpg_mb', 0):.1f} | {r['main']:.1f} | {r['uptime_main']:.1f} | "
              f"{r.get('load_s', 0):.2f} | {r.get('heap_mb', '')} | {r['exit']:.1f} |")


def timeouts():
    print("| draft | stopped in | bodies entered | distinct keys | kmr nesting | targetsOf calls / sites | CFG visits / distinct | methodsOfType (s) |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for p in sorted((R / "timeouts").glob("*.prof.json")):
        d = json.loads(p.read_text())
        c, ds, ph = d["counters"], d.get("distinct", {}), d["phases"]
        lang, pat, repo = p.name[:-len(".prof.json")].split("__")
        print(f"| {lang} {repo} | {d.get('inPhase') or '-'} | {c.get('walkBody.entered', 0):,} | {c.get('enteredBodies.maxSize', 0):,} | "
              f"{c.get('kmr.maxNesting', 0)} | {c.get('targetsOf.calls', 0):,} / {ds.get('targetsOf.sites', 0):,} | "
              f"{c.get('walk.nodesVisited', 0):,} / {ds.get('walk.distinctNodes', 0):,} | {ph.get('lazy.methodsOfType', 0):.1f} |")


def run(name):
    profs = [json.loads(p.read_text()) | {"_id": p.name} for p in (R / name).glob("*.prof.json")]
    done = [d for d in profs if d.get("status") == "done"]
    to = [d for d in profs if d.get("timeout")]
    walk = sorted(d["elapsed"] for d in done)
    load = sorted(d.get("loadS", 0) for d in done)
    metas = [json.loads(p.read_text()) for p in (R / name).glob("*.meta.json")]
    wall = sum(m["marks"].get("exit", 0) for m in metas)
    main = [m["marks"]["main"] for m in metas if "main" in m["marks"]]
    print(f"- {name}: {len(profs)} drafts, {len(done)} done, {len(to)} timed out; {len(metas)} JVM launches, "
          f"total wall {wall/60:.1f} min")
    if walk:
        q = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))]
        print(f"- per-draft walk (exec): median {statistics.median(walk):.3f} s, p90 {q(walk, .9):.2f} s, "
              f"max {walk[-1]:.2f} s, sum {sum(walk)/60:.1f} min")
        print(f"- CPG load: median {statistics.median(load):.2f} s, max {load[-1]:.2f} s; "
              f"JVM to script main: median {statistics.median(main):.1f} s")
    for d in to:
        print(f"  - timeout: {d['_id']}")


def build(name):
    print("| repo | lang | delombok | source files | wall (s) | CPG MB | peak RSS (GB) | peak procs (java) | killed |")
    print("|---|---|---|---:|---:|---:|---:|---:|---|")
    for p in sorted((R / name).glob("*.build.json")):
        b = json.loads(p.read_text())
        print(f"| {b['repo']} | {b['language']} | {b['delombok']} | {b['files']} | {b['wall_s']:.0f} | {b['cpg_mb']:.1f} | "
              f"{b['peak_tree_rss_mb']/1024:.1f} | {b['peak_tree_pids']} ({b['peak_java_procs']}) | {b['killed'] or '-'} |")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "startup"
    {"startup": startup, "timeouts": timeouts}.get(what, lambda: None)() if what in ("startup", "timeouts") else \
        (run if what == "run" else build)(sys.argv[2])
