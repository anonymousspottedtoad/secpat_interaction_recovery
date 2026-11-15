#!/usr/bin/env python3
"""Profile CPG builds: one `joern-parse` at a time, with a watchdog.

    python3 profiling/prof_build.py --run NAME --repos REPO[:java|python][:delombok=MODE] ...
        [--xmx 32g] [--jfr] [--timeout 3600]

Builds into a scratch folder (never the CPG store). For each build it records wall time,
the peak resident memory and peak process count of the whole process tree (delombok runs
child JVMs), Joern's own INFO log (SL_LOGGING_LEVEL=INFO: one line per pass), and, with
--jfr, one recording per JVM. A build is killed when the tree exceeds the pid or memory
guard -- the 2026-11-09 freezes came from builds that did.
Writes profiling/results/NAME/<repo>.build.json and .log (+ .jfr).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOERN_PARSE = os.environ.get("JOERN_PARSE_BIN", "/opt/joern/joern-cli/joern-parse")
ROOTS = {"python": Path("/workspace/mining-sec-arcs-tactics/build/volumes/data/cloned_repos"),
         "java": Path("/workspace/mining-sec-arcs-tactics/build/volumes/data_java/cloned_repos")}
FE = {"python": "PYTHONSRC", "java": "JAVASRC"}
CG = Path("/sys/fs/cgroup")
MAX_TREE_PIDS = 300
MIN_FREE_GIB = 60


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def free_gib():
    return (int((CG / "memory.max").read_text()) - int((CG / "memory.current").read_text())) / 2**30


def tree(pid: int) -> list[int]:
    """`pid` and all its descendants."""
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            kids.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def rss_mb(pids: list[int]) -> float:
    total = 0
    for p in pids:
        try:
            for line in Path(f"/proc/{p}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
        except OSError:
            pass
    return total / 1024


def java_count(pids: list[int]) -> int:
    n = 0
    for p in pids:
        try:
            if Path(f"/proc/{p}/comm").read_text().strip() == "java":
                n += 1
        except OSError:
            pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--repos", nargs="+", required=True, help="REPO:lang[:delombok=MODE]")
    ap.add_argument("--xmx", default="32g")
    ap.add_argument("--jfr", action="store_true")
    ap.add_argument("--timeout", type=int, default=3600)
    a = ap.parse_args()
    out = HERE / "results" / a.run
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ.get("PROF_SCRATCH", tempfile.gettempdir()))
    for spec in a.repos:
        parts = spec.split(":")
        repo, lang = parts[0], parts[1]
        delombok = next((p.split("=", 1)[1] for p in parts[2:] if p.startswith("delombok=")), "")
        src = ROOTS[lang] / repo
        tag = repo + (f".delombok-{delombok}" if delombok else "")
        cpg = Path(tempfile.mkdtemp(prefix="build-", dir=scratch)) / "out.cpg"
        cmd = [JOERN_PARSE, str(src), "-o", str(cpg), "--language", FE[lang]]
        if lang == "java":
            cmd += ["--frontend-args", "--delombok-mode", delombok or "no-delombok"]
        opts = [f"-Xmx{a.xmx}"]
        if a.jfr:
            opts += [f"-XX:StartFlightRecording=filename={out / (tag + '.%p.jfr')},dumponexit=true,settings=profile",
                     "-XX:FlightRecorderOptions=stackdepth=512"]
        env = dict(os.environ, JAVA_TOOL_OPTIONS=" ".join(opts), SL_LOGGING_LEVEL="INFO")
        n_files = sum(1 for _ in src.rglob("*.java" if lang == "java" else "*.py"))
        log(f"{tag}: {n_files} source file(s); {' '.join(cmd)}")
        t0 = time.monotonic()
        peak_rss = peak_pids = peak_java = 0
        killed = ""
        with open(out / f"{tag}.log", "w") as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=env, cwd=str(cpg.parent), start_new_session=True)
            while proc.poll() is None:
                pids = tree(proc.pid)
                peak_rss = max(peak_rss, rss_mb(pids))
                peak_pids = max(peak_pids, len(pids))
                peak_java = max(peak_java, java_count(pids))
                if len(pids) > MAX_TREE_PIDS:
                    killed = f"tree pids {len(pids)}"
                elif free_gib() < MIN_FREE_GIB:
                    killed = f"free memory {free_gib():.0f} GiB"
                elif time.monotonic() - t0 > a.timeout:
                    killed = "timeout"
                if killed:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    break
                time.sleep(0.5)
        wall = time.monotonic() - t0
        size = cpg.stat().st_size if cpg.is_file() else 0
        text = (out / f"{tag}.log").read_text(errors="replace")
        passes = []
        for m in re.finditer(r"(\S+)\s+-?\s*(?:pass )?(\S*?Pass\S*).*?(\d+(?:\.\d+)?)\s*(ms|s)\b", text):
            pass
        # Joern's x2cpg passes log e.g. "... INFO  CpgPass  Pass io.joern...AstLinker completed in 12 ms ..."
        for m in re.finditer(r"([\w.$]+Pass|[\w.$]+Linker|[\w.$]+Creator)[^\n]*?(?:completed|finished|took)[^\n]*?(\d+(?:\.\d+)?)\s*(ms|s)\b", text):
            secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
            passes.append((m.group(1).rsplit(".", 1)[-1], round(secs, 3)))
        rec = {"repo": repo, "language": lang, "delombok": delombok or "off", "files": n_files,
               "wall_s": round(wall, 2), "rc": proc.returncode, "killed": killed,
               "cpg_mb": round(size / 1e6, 2), "peak_tree_rss_mb": round(peak_rss), "peak_tree_pids": peak_pids,
               "peak_java_procs": peak_java, "passes": passes}
        (out / f"{tag}.build.json").write_text(json.dumps(rec, indent=1))
        log(f"  -> {wall:.1f}s rc={proc.returncode} {killed} cpg={size/1e6:.1f}MB peakRSS={peak_rss/1024:.1f}GB "
            f"pids={peak_pids} java={peak_java} passes={len(passes)}")
        try:
            cpg.unlink(missing_ok=True)
            cpg.parent.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
