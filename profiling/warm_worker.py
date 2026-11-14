#!/usr/bin/env python3
"""A warm Joern worker: compile the walk once, keep the JVM, stream queries over a named pipe.

`joern --script` per query pays ~10 s before the walk starts (two JVMs + compiling the
~1,300-2,000-line script) and copies + re-saves the CPG; the walk itself takes 0.0-0.3 s on
almost every draft. This worker is one `joern --script` that stays up: it reads one JSON job
per stdin line, loads the job's CPG with CpgLoader only when it differs from the loaded one
(read-only, no workspace copy), runs the unchanged walk, and answers on stdout.

    python3 profiling/warm_worker.py --run NAME [--walk profiling/walk_opt.sc] [--repos ...]
        [--max-bytes N] [--budget 300] [--limit N]

Jobs are grouped by language (one worker per language, since the profile differs) and sent
smallest CPG first. A job past the budget kills the worker; it is restarted for the rest.
Writes profiling/results/NAME/<id>.result.json and timings.json.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prof_walk as pw  # noqa: E402

WORKER_MAIN = r'''

// ── warm worker (profiling/warm_worker.py) ───────────────────────────────────
// Shadows the console's `cpg`: walk.sc's references resolve to this one, loaded read-only.
var cpg: io.shiftleft.codepropertygraph.generated.Cpg = null
var loadedPath = ""

// `joern --script` runs the script in a second JVM and does not hand it stdin, so jobs arrive
// through a named pipe instead.
@main def serve(fifo: String) = {
  println(s"WORKER-READY uptime_ms=${java.lang.management.ManagementFactory.getRuntimeMXBean.getUptime}")
  val in = new java.io.BufferedReader(new java.io.InputStreamReader(new java.io.FileInputStream(fifo), "UTF-8"))
  var line = in.readLine()
  while (line != null) {
    if (line.trim.nonEmpty) {
      val j = ujson.read(line)
      val id = j("id").str
      try {
        val path = j("cpgPath").str
        if (path != loadedPath) {
          val t = System.nanoTime
          cpg = null; loadedPath = ""
          cpg = io.shiftleft.codepropertygraph.cpgloading.CpgLoader.load(path)
          loadedPath = path
          println(f"WORKER-LOADED $id ${(System.nanoTime - t) / 1e9}%.3f")
        }
        val t0 = System.nanoTime
        exec(outputPath = j("outputPath").str, inputPath = j("inputPath").str, language = j("language").str,
             cpgPath = "", roles = j("roles").str, entry = j("entry").str, behaviour = j("behaviour").str)
        println(f"WORKER-DONE $id ${(System.nanoTime - t0) / 1e9}%.4f")
      } catch {
        case e: Throwable => println(s"WORKER-ERROR $id ${e.toString.replace('\n', ' ').take(400)}")
      }
      System.out.flush()
    }
    line = in.readLine()
  }
}
'''


def worker_script(language: str, walk: Path | None) -> str:
    src = (walk or pw.WALK).read_text(encoding="utf-8")
    src = pw.patch(src, "@main def exec(", "def exec(")
    src = pw.patch(src, "  loadGraph(cpgPath, inputPath, language)\n  val repo = new Repo(inputPath)",
                   "  if (cpgPath.nonEmpty) loadGraph(cpgPath, inputPath, language)\n  val repo = new Repo(inputPath)")
    profile = (pw.JOERN_DIR / pw.PROFILES[language]).read_text(encoding="utf-8")
    return src + "\n" + profile + WORKER_MAIN


class Worker:
    def __init__(self, language: str, walk: Path | None, workdir: Path, xmx: str, log):
        self.workdir = workdir
        script = workdir / f"worker_{language}.sc"
        script.write_text(worker_script(language, walk), encoding="utf-8")
        self.fifo = workdir / f"jobs_{language}.fifo"
        if not self.fifo.exists():
            os.mkfifo(self.fifo)
        self.t_spawn = time.monotonic()
        self.proc = subprocess.Popen(
            [pw.JOERN, "--script", str(script), "--param", f"fifo={self.fifo}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(workdir), start_new_session=True,
            env=dict(os.environ, JAVA_TOOL_OPTIONS=f"-Xmx{xmx}"))
        self.log = log
        self.ready_s = None
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        line = self._read_until(("WORKER-READY",), 600)
        if line is None:
            raise RuntimeError("worker did not start")
        self.ready_s = time.monotonic() - self.t_spawn
        # The reader has the pipe open once `serve` runs; open the write end without blocking.
        fd = None
        for _ in range(600):
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:
                time.sleep(0.1)
        if fd is None:
            raise RuntimeError("worker never opened its job pipe")
        os.set_blocking(fd, True)
        self.jobs = os.fdopen(fd, "w", buffering=1)

    def _pump(self):
        # One thread owns the pipe: select() on a buffered text stream misses lines that
        # readline() already pulled into the buffer.
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read_until(self, prefixes, timeout):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            self.log.write(line)
            self.log.flush()
            if line.startswith(prefixes):
                return line.rstrip("\n")

    def run(self, job: dict, timeout: float):
        self.jobs.write(json.dumps(job) + "\n")
        self.jobs.flush()
        load_s = 0.0
        t0 = time.monotonic()
        while True:
            line = self._read_until(("WORKER-LOADED", "WORKER-DONE", "WORKER-ERROR"), timeout - (time.monotonic() - t0))
            if line is None:
                return "timeout", load_s, None, time.monotonic() - t0
            p = line.split(" ", 2)
            if p[0] == "WORKER-LOADED":
                load_s = float(p[2])
                continue
            if p[0] == "WORKER-DONE":
                return "done", load_s, float(p[2]), time.monotonic() - t0
            return "error:" + (p[2] if len(p) > 2 else ""), load_s, None, time.monotonic() - t0

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--walk", type=Path, default=None)
    ap.add_argument("--repos", nargs="*")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--max-bytes", type=int, default=1 << 62)
    ap.add_argument("--budget", type=float, default=300)
    ap.add_argument("--xmx", default="48g")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--joern-dir", type=Path, default=None,
                    help="folder with walk.sc, java.sc and python.sc to use instead of the package's")
    a = ap.parse_args()
    if a.joern_dir:
        pw.JOERN_DIR, pw.WALK = a.joern_dir.resolve(), a.joern_dir.resolve() / "walk.sc"
    jobs = [json.loads(l) for l in (pw.HERE / "jobs" / "jobs.jsonl").open()]
    jobs = [j for j in jobs if j["cpg_bytes"] <= a.max_bytes and (not a.repos or j["repo"] in a.repos)
            and (not a.ids or j["id"] in a.ids)]
    if a.limit:
        jobs = jobs[:a.limit]
    out = pw.HERE / "results" / a.run
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ.get("PROF_SCRATCH", tempfile.gettempdir()))
    timings = []
    t_all = time.monotonic()
    for lang in ("java", "python"):
        todo = sorted([j for j in jobs if j["language"] == lang], key=lambda j: (j["cpg_bytes"], j["id"]))
        if not todo:
            continue
        workdir = Path(tempfile.mkdtemp(prefix="warm-", dir=scratch))
        logf = open(out / f"worker_{lang}.log", "a")
        w = None
        for j in todo:
            if w is None:
                w = Worker(lang, a.walk, workdir, a.xmx, logf)
                pw.log(f"{lang} worker ready in {w.ready_s:.1f}s")
                timings.append({"event": "worker-start", "language": lang, "ready_s": round(w.ready_s, 2)})
            payload = {"id": j["id"], "cpgPath": j["cpg"], "inputPath": j["input"], "language": j["frontend"],
                       "roles": j["roles"], "entry": j["entry"], "behaviour": j["behaviour"],
                       "outputPath": str(out / f"{pw.safe(j['id'])}.result.json")}
            status, load_s, walk_s, wall = w.run(payload, a.budget + 120)
            timings.append({"id": j["id"], "status": status, "load_s": load_s, "walk_s": walk_s,
                            "wall_s": round(wall, 3), "cpg_mb": round(j["cpg_bytes"] / 1e6, 2)})
            pw.log(f"  {status:8s} load={load_s:6.2f}s walk={walk_s if walk_s is not None else -1:8.3f}s {j['id']}")
            if status != "done" and not status.startswith("error"):
                w.kill()
                w = None
            (out / "timings.json").write_text(json.dumps(timings, indent=1))
        if w is not None:
            w.jobs.close()
            try:
                w.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                w.kill()
        logf.close()
    pw.log(f"all done in {time.monotonic() - t_all:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
