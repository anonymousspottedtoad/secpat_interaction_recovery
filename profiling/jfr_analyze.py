#!/usr/bin/env python3
"""Aggregate a JFR recording's execution samples: where did the time go?

    python3 profiling/jfr_analyze.py REC.jfr [--top 40] [--thread main] [--json OUT]

Streams `jfr print --events jdk.ExecutionSample` and reports, per thread:
  * buckets: the first matching rule from the stack's root down (compile, cpg-load, walk, ...);
  * self: the top frame of each sample;
  * inclusive: every distinct method on the stack, once per sample -- for script methods
    (the walk's own Scala code) this is how much of the time each function accounts for.
Also sums GC pause time from jdk.GarbageCollection.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys

FRAME = re.compile(r"^\s+(.+?)\((.*?)\)\s+line: (-?\d+)")

# Bucket rules, checked against the whole stack (outermost frame first wins for "phase").
BUCKETS = [
    ("compile (dotty)", lambda fs: any(f.startswith("dotty.tools.") for f in fs)),
    ("cpg-load (flatgraph)", lambda fs: any(f.startswith("flatgraph.storage.") or "Deserialization" in f for f in fs)),
    ("cpg-copy/save", lambda fs: any("copyFile" in f or "Serialization" in f or "Files.copy" in f for f in fs)),
    ("walk", lambda fs: any(is_script(f) for f in fs)),
    ("classload", lambda fs: any(f.startswith("jdk.internal.loader.") or "ClassLoader.loadClass" in f for f in fs)),
]


def is_script(f: str) -> bool:
    # Joern scripts compile into classes like `batch$_.Walker...`, `batch$_$CallGraph`, `Lang$`, `Prof$`.
    return f.startswith("ScalaReplPP$") or bool(re.match(r"^(batch|s|Main|query)\$_", f)) \
        or f.startswith(("Lang$", "Prof$", "Watch$", "Entry$"))


def short(f: str) -> str:
    f = re.sub(r"^(ScalaReplPP\$|[a-zA-Z]+\$_\$?)", "", f)
    return f


def samples(path: str):
    p = subprocess.Popen(["jfr", "print", "--events", "jdk.ExecutionSample", "--stack-depth", "4096", path],
                         stdout=subprocess.PIPE, text=True, errors="replace")
    thread, frames, in_stack = "", [], False
    for line in p.stdout:
        s = line.strip()
        if s.startswith("sampledThread"):
            m = re.search(r'"(.*?)"', s)
            thread = m.group(1) if m else "?"
        elif s.startswith("stackTrace = ["):
            frames, in_stack = [], True
        elif in_stack:
            if s == "]":
                in_stack = False
                yield thread, frames
            elif s != "...":
                m = FRAME.match(line)
                if m:
                    frames.append(m.group(1))
    p.wait()


def gc_pauses(path: str) -> float:
    out = subprocess.run(["jfr", "print", "--events", "jdk.GarbageCollection", path],
                         capture_output=True, text=True).stdout
    total = 0.0
    for m in re.finditer(r"sumOfPauses = ([\d.]+) (ms|s|us)", out):
        v = float(m.group(1))
        total += v / 1000 if m.group(2) == "ms" else (v / 1e6 if m.group(2) == "us" else v)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jfr")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--thread", default=None, help="only this thread (default: all)")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    by_thread = collections.Counter()
    buckets = collections.Counter()
    self_ = collections.Counter()
    incl = collections.Counter()
    incl_script = collections.Counter()
    n = 0
    for thread, frames in samples(a.jfr):
        if a.thread and thread != a.thread:
            continue
        n += 1
        by_thread[thread] += 1
        b = next((name for name, rule in BUCKETS if rule(frames)), "other")
        buckets[b] += 1
        if frames:
            self_[frames[0]] += 1
        seen = set(frames)
        for f in seen:
            incl[f] += 1
            if is_script(f):
                incl_script[short(f)] += 1
    gc = gc_pauses(a.jfr)
    report = {
        "samples": n,
        "gc_pause_s": round(gc, 3),
        "threads": by_thread.most_common(10),
        "buckets": {k: round(v / max(n, 1), 4) for k, v in buckets.most_common()},
        "self_top": [(f, c, round(c / max(n, 1), 4)) for f, c in self_.most_common(a.top)],
        "script_inclusive_top": [(f, c, round(c / max(n, 1), 4)) for f, c in incl_script.most_common(a.top)],
    }
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(report, fh, indent=1)
    print(f"samples={n}  gc_pause={gc:.2f}s  threads={by_thread.most_common(4)}")
    print("buckets:", ", ".join(f"{k} {v:.1%}" for k, v in report["buckets"].items()))
    print("\n-- self (top frame) --")
    for f, c, r in report["self_top"][:a.top]:
        print(f"{r:7.2%} {c:7d}  {f}")
    print("\n-- inclusive, walk script methods --")
    for f, c, r in report["script_inclusive_top"][:a.top]:
        print(f"{r:7.2%} {c:7d}  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
