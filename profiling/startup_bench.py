#!/usr/bin/env python3
"""Split the fixed cost of one `joern --script` run: JVMs, script compile, CPG load, exit.

Strictly sequential. Three kinds of launch, each repeated:
  hello   -- a one-line script: launcher JVM + script JVM + Joern console, nothing compiled
  empty   -- the instrumented walk with no jobs on the 14 KB CPG: adds compiling walk.sc
  load:X  -- the same on CPG X: adds importCpg (copy + load) and the project save on exit

    python3 profiling/startup_bench.py [--repeat 2]
Writes profiling/results/startup/startup.json.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, time, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prof_walk as pw

HELLO = '@main def hello() = println("BATCH-MAIN uptime_ms=" + java.lang.management.ManagementFactory.getRuntimeMXBean.getUptime)\n'


def launch(script: str, params: list[str], workdir: Path) -> dict:
    (workdir / "s.sc").write_text(script)
    cmd = [pw.JOERN, "--script", str(workdir / "s.sc")] + sum((["--param", p] for p in params), [])
    t0 = time.monotonic()
    marks = {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=workdir,
                            env=dict(os.environ, JAVA_TOOL_OPTIONS="-Xmx48g"), start_new_session=True)
    for ln in proc.stdout:
        now = time.monotonic() - t0
        p = ln.split()
        if not p:
            continue
        if p[0] == "BATCH-MAIN":
            marks["main"] = now; marks["uptime_main"] = int(p[1].split("=")[1]) / 1000
        elif p[0] == "BATCH-LOADED":
            marks["loaded"] = now; marks["load_s"] = float(p[1].split("=")[1]); marks["heap_mb"] = int(p[2].split("=")[1])
        elif p[0] == "BATCH-END":
            marks["end"] = now
    proc.wait()
    marks["exit"] = time.monotonic() - t0
    marks["rc"] = proc.returncode
    return marks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    a = ap.parse_args()
    jobs = [json.loads(l) for l in (pw.HERE / "jobs" / "jobs.jsonl").open()]
    by_repo = {j["repo"]: j for j in jobs}
    sizes = ["raphw_jenesis-repository", "factcast_factcast", "camunda_camunda-bpm-platform",
             "camunda_camunda", "langflow-ai_langflow", "BerriAI_litellm"]
    out = pw.HERE / "results" / "startup"
    out.mkdir(parents=True, exist_ok=True)
    res = []
    scratch = Path(os.environ.get("PROF_SCRATCH", tempfile.gettempdir()))
    plan = [("hello", None)] * (args_rep := a.repeat + 1) + [("empty", "raphw_jenesis-repository")] * a.repeat
    plan += [(f"load", r) for r in sizes[1:] for _ in range(a.repeat)]
    for kind, repo in plan:
        wd = Path(tempfile.mkdtemp(prefix="startup-", dir=scratch))
        try:
            if kind == "hello":
                m = launch(HELLO, [], wd)
            else:
                j = by_repo[repo]
                (wd / "jobs.json").write_text("[]")
                m = launch(pw.instrumented_script(j["language"]),
                           [f"cpgPath={j['cpg']}", f"inputPath={j['input']}", f"jobsPath={wd / 'jobs.json'}",
                            f"language={j['frontend']}"], wd)
                m["cpg_mb"] = j["cpg_bytes"] / 1e6
                m["repo"] = repo
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        m["kind"] = kind
        res.append(m)
        pw.log(f"{kind:6s} {repo or '':32s} " + " ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                                                         for k, v in m.items() if k not in ("kind", "repo")))
        (out / "startup.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
