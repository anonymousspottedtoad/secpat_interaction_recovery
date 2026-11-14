#!/usr/bin/env python3
"""Profile the walk: one instrumented Joern JVM per repository, strictly one at a time.

The walk is the one `detector._script` assembles (walk.sc + the language profile). This
script applies a few asserted text patches to that source, none of which changes what
the walk computes:

* `exec` loses `@main` and runs its five steps under phase timers;
* `walkBody`, `keepMostRoles` and `targetsOf` count what they do (calls, memo hits,
  distinct methods, alternatives, nesting, the size of every `enteredBodies` clone);
* a new `@main batch` loads the CPG once, times the load, and runs each job;
* a daemon watchdog writes a progress line every 10 s and, past the per-job budget,
  writes the partial counters plus a sample of the main thread's stack, then exits the
  JVM (so JFR, when on, dumps its recording). The harness restarts the JVM with the
  jobs after the one that timed out.

    python3 profiling/prof_walk.py --run NAME [--ids ID ...] [--repos REPO ...]
        [--max-bytes N] [--min-bytes N] [--budget 300] [--jfr] [--xmx 48g]

Results go to profiling/results/NAME/: per job `<id>.prof.json` (phases, counters,
progress, stacks) and `<id>.result.json` (the walk's own output), per launch the JVM's
stdout and, with --jfr, a `.jfr` recording; `summary.csv` has one row per job.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOERN_DIR = HERE.parent / "src" / "secpat_interaction_order" / "joern"
JOERN = os.environ.get("JOERN_BIN", "/opt/joern/joern-cli/joern")
PROFILES = {"python": "python.sc", "java": "java.sc"}
CG = Path("/sys/fs/cgroup")
MAX_PIDS, MIN_FREE_GIB = 2500, 25


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def pids() -> int:
    return int((CG / "pids.current").read_text())


def free_gib() -> float:
    return (int((CG / "memory.max").read_text()) - int((CG / "memory.current").read_text())) / 2**30


def safe(job_id: str) -> str:
    return job_id.replace("/", "__")


# ── the instrumented script ───────────────────────────────────────────────────

def patch(src: str, old: str, new: str, required: bool = True) -> str:
    if not required and src.count(old) == 0:
        return src
    assert src.count(old) == 1, f"patch anchor not found exactly once: {old[:80]!r}"
    return src.replace(old, new)


EXEC_OLD = '''@main def exec(
  outputPath: String,
  inputPath: String = "",
  language: String = "",
  cpgPath: String = "",
  roles: String = "",
  entry: String = "",
  behaviour: String = ""
) = {
  loadGraph(cpgPath, inputPath, language)
  val repo = new Repo(inputPath)

  // 1. Which method does a call run?
  val graph = new CallGraph(repo)

  // 2. Which node does each citation name, and which role owns which node?
  val located   = new Locator(graph).locateAll(jarray(roles))
  val ownership = new Ownership(graph, located, jarray(behaviour))

  // 3. Where does a run of this code begin?
  val start = Entry.choose(graph, ownership, located, entry)

  // 4. Step the control-flow graph from there, recording each change of role.
  val walker = new Walker(graph, ownership)
  start.node.foreach { case (m, from) =>
    val roles = ownership.roleOfNode.getOrElse(m.id, List(start.role))
    walker.coLocatedAtStart(m, from, roles)
    walker.walkBody(m, from, roles, Nil, 0)
  }

  // 5. Report each interaction once, in the order it was first reached.
  val interactions = firstOccurrences(walker.edges.toList)
  writeResult(outputPath, render(graph, located, start, interactions))
  println(s"derived ${interactions.size} interaction(s) from ${located.size} role(s), entry=${start.role}")
}'''

EXEC_NEW = '''def exec(
  outputPath: String,
  inputPath: String = "",
  language: String = "",
  cpgPath: String = "",
  roles: String = "",
  entry: String = "",
  behaviour: String = ""
) = {
  if (cpgPath.nonEmpty) Prof.phase("load") { loadGraph(cpgPath, inputPath, language) }
  val repo = new Repo(inputPath)
  val graph = Prof.phase("1-callgraph") { new CallGraph(repo) }
  val locator = Prof.phase("2a-declarations") { new Locator(graph) }
  val located = Prof.phase("2b-locate") { locator.locateAll(jarray(roles)) }
  val ownership = Prof.phase("2c-ownership") { new Ownership(graph, located, jarray(behaviour)) }
  val start = Prof.phase("3-entry") { Entry.choose(graph, ownership, located, entry) }
  val walker = new Walker(graph, ownership)
  Prof.walker = walker
  Prof.phase("4-walk") {
    start.node.foreach { case (m, from) =>
      val roles = ownership.roleOfNode.getOrElse(m.id, List(start.role))
      walker.coLocatedAtStart(m, from, roles)
      walker.walkBody(m, from, roles, Nil, 0)
    }
  }
  val interactions = Prof.phase("5-render") {
    val i = firstOccurrences(walker.edges.toList)
    writeResult(outputPath, render(graph, located, start, i))
    i
  }
  Prof.set("graph.methods", graph.allMethods.size)
  Prof.set("graph.internalMethods", graph.internal.size)
  Prof.set("located", located.size)
  Prof.set("edges.raw", walker.edges.size)
  Prof.set("interactions", interactions.size)
  println(s"derived ${interactions.size} interaction(s) from ${located.size} role(s), entry=${start.role}")
}'''

PROF_OBJECT = r'''

// ── profiling (profiling/prof_walk.py) ───────────────────────────────────────
object Prof {
  import scala.collection.mutable
  val counters = mutable.LinkedHashMap[String, Long]()
  val phases = mutable.LinkedHashMap[String, Double]()
  val distinct = mutable.LinkedHashMap[String, mutable.Set[Long]]()
  @volatile var current = ""
  @volatile var walker: AnyRef = null
  var kmrDepth = 0
  def inc(k: String, n: Long = 1): Unit = counters(k) = counters.getOrElse(k, 0L) + n
  def set(k: String, v: Long): Unit = counters(k) = v
  def max(k: String, v: Long): Unit = if (v > counters.getOrElse(k, Long.MinValue)) counters(k) = v
  def see(k: String, id: Long): Unit = distinct.getOrElseUpdate(k, mutable.Set[Long]()) += id
  def phase[T](k: String)(body: => T): T = {
    val t = System.nanoTime; current = k
    try body finally { phases(k) = phases.getOrElse(k, 0.0) + (System.nanoTime - t) / 1e9; current = "" }
  }
  def reset(): Unit = { counters.clear(); phases.clear(); distinct.clear(); kmrDepth = 0; current = ""; walker = null }
  def snapshot(): ujson.Obj = {
    // Read from the watchdog thread while the walk mutates: retry on a concurrent change.
    var out: ujson.Obj = null
    var tries = 0
    while (out == null && tries < 20) {
      try {
        out = ujson.Obj(
          "phases" -> ujson.Obj.from(phases.toList.map { case (k, v) => k -> ujson.Num(v) }),
          "counters" -> ujson.Obj.from(counters.toList.map { case (k, v) => k -> ujson.Num(v.toDouble) }),
          "distinct" -> ujson.Obj.from(distinct.toList.map { case (k, v) => k -> ujson.Num(v.size.toDouble) }),
          "inPhase" -> current)
      } catch { case _: Throwable => tries += 1; Thread.sleep(5) }
    }
    if (out == null) ujson.Obj("error" -> "could not snapshot") else out
  }
}

object Watch {
  @volatile var job = ""
  @volatile var started = 0L
  @volatile var deadline = Long.MaxValue
  @volatile var profPath = ""
  val progress = new java.util.concurrent.ConcurrentLinkedQueue[ujson.Value]()
  var main: Thread = null
  def frames(t: Thread, n: Int): List[String] =
    t.getStackTrace.toList.take(n).map(f => s"${f.getClassName}.${f.getMethodName}:${f.getLineNumber}")
  def start(): Unit = {
    main = Thread.currentThread()
    val t = new Thread(() => {
      var lastProgress = System.nanoTime
      val stacks = scala.collection.mutable.ListBuffer[ujson.Value]()
      while (true) {
        Thread.sleep(250)
        val now = System.nanoTime
        if (job.nonEmpty && now - lastProgress > 10e9.toLong) {
          lastProgress = now
          val s = Prof.snapshot()
          val line = ujson.Obj("t" -> (now - started) / 1e9, "counters" -> s.obj.getOrElse("counters", ujson.Null),
                               "inPhase" -> s.obj.getOrElse("inPhase", ujson.Null))
          progress.add(line)
          println(s"BATCH-PROGRESS $job " + line.render())
          // A few spaced stack samples of the main thread, for a quick look without JFR.
          if (stacks.size < 12) stacks += ujson.Arr.from(frames(main, 60).map(ujson.Str(_)))
        }
        if (job.nonEmpty && now > deadline) {
          val s = Prof.snapshot()
          s("timeout") = true
          s("elapsed") = (now - started) / 1e9
          s("progress") = ujson.Arr.from(progress.toArray.toList.map(_.asInstanceOf[ujson.Value]))
          s("stackSamples") = ujson.Arr.from(stacks.toList)
          s("finalStack") = ujson.Arr.from(frames(main, 400).map(ujson.Str(_)))
          java.nio.file.Files.write(java.nio.file.Paths.get(profPath), s.render(indent = 1).getBytes("UTF-8"))
          println(s"BATCH-TIMEOUT $job")
          System.out.flush()
          Runtime.getRuntime.exit(124)
        }
        if (job.isEmpty) stacks.clear()
      }
    })
    t.setDaemon(true)
    t.start()
  }
}

@main def batch(cpgPath: String, inputPath: String, jobsPath: String, language: String = "",
                budgetS: String = "300") = {
  val upAtMain = java.lang.management.ManagementFactory.getRuntimeMXBean.getUptime
  println(s"BATCH-MAIN uptime_ms=$upAtMain")
  Watch.start()
  val tl = System.nanoTime
  loadGraph(cpgPath, inputPath, language)
  val loadS = (System.nanoTime - tl) / 1e9
  val heapAfterLoad = { val r = Runtime.getRuntime; System.gc(); (r.totalMemory - r.freeMemory) / 1048576 }
  println(f"BATCH-LOADED load_s=$loadS%.3f heap_mb=$heapAfterLoad")
  val jobs = ujson.read(new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(jobsPath)), "UTF-8")).arr
  jobs.foreach { j =>
    val id = j("id").str
    Prof.reset()
    Watch.progress.clear()
    Watch.profPath = j("profPath").str
    Watch.started = System.nanoTime
    Watch.deadline = Watch.started + (budgetS.toDouble * 1e9).toLong
    Watch.job = id
    println(s"BATCH-START $id")
    val t0 = System.nanoTime
    val status = try {
      exec(outputPath = j("outputPath").str, inputPath = inputPath, language = language, cpgPath = "",
           roles = j("roles").str, entry = j("entry").str, behaviour = j("behaviour").str)
      "done"
    } catch { case e: Throwable => s"error: ${e.toString.take(500)}" }
    val secs = (System.nanoTime - t0) / 1e9
    Watch.job = ""
    Watch.deadline = Long.MaxValue
    val s = Prof.snapshot()
    s("status") = status
    s("elapsed") = secs
    s("loadS") = loadS
    s("uptimeAtMainMs") = upAtMain.toDouble
    s("heapAfterLoadMb") = heapAfterLoad.toDouble
    s("progress") = ujson.Arr.from(Watch.progress.toArray.toList.map(_.asInstanceOf[ujson.Value]))
    java.nio.file.Files.write(java.nio.file.Paths.get(j("profPath").str), s.render(indent = 1).getBytes("UTF-8"))
    if (status == "done") println(f"BATCH-DONE $id $secs%.3f")
    else println(s"BATCH-ERROR $id ${status.take(300)}")
  }
  println("BATCH-END")
}
'''


WALK = JOERN_DIR / "walk.sc"

# Counter patches: (anchor, replacement). Each applies to whichever walk version has the anchor.
COUNTERS = [
    # original walk.sc: walkBody calls, memo hits, distinct methods, depth
    ("    val key = (m.id, from.map(_.id).getOrElse(-1L), sources.sorted.mkString(\" \"))\n"
     "    if (depth > MAX_DEPTH || !enteredBodies.add(key)) return\n",
     "    Prof.inc(\"walkBody.calls\")\n"
     "    val key = (m.id, from.map(_.id).getOrElse(-1L), sources.sorted.mkString(\" \"))\n"
     "    if (depth > MAX_DEPTH || !enteredBodies.add(key)) { Prof.inc(\"walkBody.memoHits\"); return }\n"
     "    Prof.inc(\"walkBody.entered\"); Prof.see(\"walkBody.methods\", m.id); Prof.max(\"walkBody.maxDepth\", depth)\n"
     "    Prof.max(\"enteredBodies.maxSize\", enteredBodies.size)\n"),
    # both versions: CFG nodes visited
    ("      if (visitedNodes.add(n.id)) {\n",
     "      if (visitedNodes.add(n.id)) {\n        Prof.inc(\"walk.nodesVisited\"); Prof.see(\"walk.distinctNodes\", n.id)\n"),
    # original keepMostRoles: calls, alternatives, nesting, clone cost
    ("    val (mark, entered) = (edges.size, enteredBodies.clone())\n",
     "    Prof.inc(\"kmr.calls\"); Prof.inc(\"kmr.alternatives\", alternatives.size)\n"
     "    Prof.kmrDepth += 1; Prof.max(\"kmr.maxNesting\", Prof.kmrDepth)\n"
     "    Prof.inc(\"kmr.clonedElements\", enteredBodies.size.toLong * (1 + 2 * alternatives.size))\n"
     "    try {\n"
     "    val (mark, entered) = (edges.size, enteredBodies.clone())\n"),
    ("    best.foreach { case (_, added, after) =>\n"
     "      edges ++= added\n"
     "      enteredBodies.clear(); enteredBodies ++= after\n"
     "    }\n"
     "  }\n",
     "    best.foreach { case (_, added, after) =>\n"
     "      edges ++= added\n"
     "      enteredBodies.clear(); enteredBodies ++= after\n"
     "    }\n"
     "    } finally { Prof.kmrDepth -= 1 }\n"
     "  }\n"),
    # original targetsOf
    ("  def targetsOf(c: Call): Resolution = {\n    val direct = calleesOf(c)\n",
     "  def targetsOf(c: Call): Resolution = {\n    Prof.inc(\"targetsOf.calls\"); Prof.see(\"targetsOf.sites\", c.id)\n"
     "    val direct = calleesOf(c)\n"),
    # original methodsOfType (timed)
    ("  lazy val methodsOfType: Map[Long, List[Long]] =\n    cpg.typeDecl.filterNot(_.isExternal).l.map { td =>",
     "  lazy val methodsOfType: Map[Long, List[Long]] = Prof.phase(\"lazy.methodsOfType\") {\n"
     "    cpg.typeDecl.filterNot(_.isExternal).l.map { td =>"),
    ("        .map(_.id)\n    }.toMap\n", "        .map(_.id)\n    }.toMap }\n"),
    # memoized walk: walkBody, keepMostRoles, memo, targetsOf
    ("    if (test(key)) return\n    addKey(key)\n",
     "    Prof.inc(\"walkBody.calls\")\n    if (test(key)) { Prof.inc(\"walkBody.memoHits\"); return }\n    addKey(key)\n"
     "    Prof.inc(\"walkBody.entered\"); Prof.see(\"walkBody.methods\", m.id); Prof.max(\"walkBody.maxDepth\", depth)\n"
     "    Prof.max(\"enteredBodies.maxSize\", enteredCount)\n"),
    ("    val (mark, logMark) = (edges.size, log.size)\n",
     "    Prof.inc(\"kmr.calls\"); Prof.inc(\"kmr.alternatives\", alternatives.size)\n"
     "    Prof.kmrDepth += 1; Prof.max(\"kmr.maxNesting\", Prof.kmrDepth)\n"
     "    try {\n"
     "    val (mark, logMark) = (edges.size, log.size)\n"),
    ("    best.foreach { case (_, added, keys) =>\n"
     "      edges ++= added\n"
     "      keys.foreach(addKey)\n"
     "    }\n"
     "  }\n",
     "    best.foreach { case (_, added, keys) =>\n"
     "      edges ++= added\n"
     "      keys.foreach(addKey)\n"
     "    }\n"
     "    } finally { Prof.kmrDepth -= 1 }\n"
     "  }\n"),
    ("      case Some(s) =>\n        memoHits += 1\n",
     "      case Some(s) =>\n        memoHits += 1; Prof.inc(\"memo.replays\")\n"),
    ("        memoMisses += 1\n", "        memoMisses += 1; Prof.inc(\"memo.computed\")\n"),
    ("    if (cands.isEmpty) missNoKey += 1\n",
     "    if (cands.isEmpty) missNoKey += 1\n    Prof.set(\"memo.missNoKey\", missNoKey); Prof.set(\"memo.missInvalid\", missInvalid); "
     "Prof.set(\"memo.missFull\", missFull); Prof.set(\"memo.keys\", memo.size)\n"),
    ("    if (!live(m)) { deadSkips += 1; return }\n",
     "    if (!live(m)) { deadSkips += 1; Prof.inc(\"walk.deadSkips\"); return }\n"),
    ("  def targetsOf(c: Call): Resolution = targetsMemo.getOrElseUpdate(c.id, resolveTargets(c))\n",
     "  def targetsOf(c: Call): Resolution = { Prof.inc(\"targetsOf.calls\"); Prof.see(\"targetsOf.sites\", c.id)\n"
     "    targetsMemo.getOrElseUpdate(c.id, resolveTargets(c)) }\n"),
    ("  lazy val methodsOfType: Map[Long, List[Long]] = {\n",
     "  lazy val methodsOfType: Map[Long, List[Long]] = Prof.phase(\"lazy.methodsOfType\") {\n"),
]


def instrumented_script(language: str, walk: Path | None = None) -> str:
    """walk.sc (or `walk`) + the profile, with phase timers, counters and the batch main."""
    src = (walk or WALK).read_text(encoding="utf-8")
    src = patch(src, EXEC_OLD, EXEC_NEW)
    for old, new in COUNTERS:
        src = patch(src, old, new, required=False)
    profile = (JOERN_DIR / PROFILES[language]).read_text(encoding="utf-8")
    return src + "\n" + profile + PROF_OBJECT


# ── running ───────────────────────────────────────────────────────────────────

def run_launch(job, jobs, workdir, out_dir, launch, budget, xmx, jfr, walk=None):
    """One JVM over `jobs`. Returns ({id: status}, meta)."""
    lang = jobs[0]["language"]
    script = workdir / "batch.sc"
    script.write_text(instrumented_script(lang, walk), encoding="utf-8")
    payload = [{
        "id": j["id"], "roles": j["roles"], "behaviour": j["behaviour"], "entry": j["entry"],
        "outputPath": str(out_dir / f"{safe(j['id'])}.result.json"),
        "profPath": str(out_dir / f"{safe(j['id'])}.prof.json"),
    } for j in jobs]
    (workdir / "jobs.json").write_text(json.dumps(payload), encoding="utf-8")
    repo = jobs[0]["repo"]
    tag = f"{repo}.L{launch}"
    opts = [f"-Xmx{xmx}"]
    if jfr:
        opts += [f"-XX:StartFlightRecording=filename={out_dir / (tag + '.%p.jfr')},dumponexit=true,settings=profile",
                 "-XX:FlightRecorderOptions=stackdepth=2048"]
    env = dict(os.environ, JAVA_TOOL_OPTIONS=" ".join(opts))
    cmd = [JOERN, "--script", str(script), "--param", f"cpgPath={jobs[0]['cpg']}",
           "--param", f"inputPath={jobs[0]['input']}", "--param", f"jobsPath={workdir / 'jobs.json'}",
           "--param", f"language={jobs[0]['frontend']}", "--param", f"budgetS={budget}"]
    out_txt = out_dir / f"{tag}.out"
    status, marks = {}, {}
    t_start = time.monotonic()
    killed = ""
    with open(out_txt, "w") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, cwd=str(workdir), start_new_session=True)
        seen = 0
        t_mark = time.monotonic()
        while True:
            done = proc.poll() is not None
            lines = out_txt.read_text(errors="replace").splitlines()
            for ln in lines[seen:]:
                p = ln.split(" ", 2)
                now = time.monotonic() - t_start
                if p[0] == "BATCH-MAIN": marks["main"] = now
                elif p[0] == "BATCH-LOADED": marks["loaded"] = now; t_mark = time.monotonic()
                elif p[0] == "BATCH-START": marks[f"start:{p[1]}"] = now; t_mark = time.monotonic()
                elif p[0] == "BATCH-DONE": status[p[1]] = "done"; marks[f"done:{p[1]}"] = now
                elif p[0] == "BATCH-ERROR": status[p[1]] = "error"; marks[f"done:{p[1]}"] = now
                elif p[0] == "BATCH-TIMEOUT": status[p[1]] = "timeout"; marks[f"done:{p[1]}"] = now
                elif p[0] == "BATCH-END": marks["end"] = now
            seen = len(lines)
            if done:
                break
            # Outer safety net: the in-JVM watchdog should fire first.
            if time.monotonic() - t_mark > budget + 600:
                killed = "outer-timeout"
            elif pids() > MAX_PIDS:
                killed = f"pids {pids()}"
            elif free_gib() < MIN_FREE_GIB:
                killed = f"free memory {free_gib():.0f} GiB"
            if killed:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                log(f"    killed the JVM ({killed})")
                break
            time.sleep(0.5)
    marks["exit"] = time.monotonic() - t_start
    current = next((j["id"] for j in jobs if j["id"] not in status), None)
    if killed and current:
        status[current] = "killed"
    timed_out = any(v == "timeout" for v in status.values())
    if not killed and not timed_out and current and status.get(current) is None and "end" not in marks:
        status[current] = f"crash(rc={proc.returncode})"
    meta = {"repo": repo, "launch": launch, "jobs": [j["id"] for j in jobs], "marks": marks,
            "returncode": proc.returncode, "killed": killed, "jfr": jfr, "xmx": xmx, "budget": budget}
    (out_dir / f"{tag}.meta.json").write_text(json.dumps(meta, indent=1))
    return status, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--jobs", type=Path, default=HERE / "jobs" / "jobs.jsonl")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--repos", nargs="*")
    ap.add_argument("--min-bytes", type=int, default=0)
    ap.add_argument("--max-bytes", type=int, default=1 << 62)
    ap.add_argument("--budget", type=float, default=300)
    ap.add_argument("--xmx", default="48g")
    ap.add_argument("--jfr", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="skip jobs that already have a prof.json")
    ap.add_argument("--walk", type=Path, default=None, help="walk.sc to profile (default: the package's)")
    ap.add_argument("--joern-dir", type=Path, default=None,
                    help="folder with walk.sc, java.sc and python.sc to use instead of the package's")
    a = ap.parse_args()
    if a.joern_dir:
        global JOERN_DIR, WALK
        JOERN_DIR, WALK = a.joern_dir.resolve(), a.joern_dir.resolve() / "walk.sc"

    jobs = [json.loads(l) for l in a.jobs.open()]
    jobs = [j for j in jobs if a.min_bytes <= j["cpg_bytes"] <= a.max_bytes
            and (not a.ids or j["id"] in a.ids) and (not a.repos or j["repo"] in a.repos)]
    out_dir = HERE / "results" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.skip_done:
        jobs = [j for j in jobs if not (out_dir / f"{safe(j['id'])}.prof.json").is_file()]
    groups: dict[str, list] = {}
    for j in jobs:
        groups.setdefault(j["repo"], []).append(j)
    order = sorted(groups, key=lambda r: (groups[r][0]["cpg_bytes"], r))
    log(f"{len(jobs)} job(s) over {len(order)} repo(s) -> {out_dir}")
    scratch_root = Path(os.environ.get("PROF_SCRATCH", tempfile.gettempdir()))
    for i, repo in enumerate(order, 1):
        while pids() > 1500 or free_gib() < 60:
            log(f"  waiting: pids {pids()}, free {free_gib():.0f} GiB")
            time.sleep(30)
        todo = groups[repo]
        log(f"[{i}/{len(order)}] {repo}: {len(todo)} job(s), CPG {todo[0]['cpg_bytes']/1e6:.1f} MB")
        launch = 0
        while todo:
            workdir = Path(tempfile.mkdtemp(prefix="prof-", dir=scratch_root))
            try:
                status, meta = run_launch(None, todo, workdir, out_dir, launch, a.budget, a.xmx, a.jfr, a.walk)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            m = meta["marks"]
            log(f"    L{launch}: main@{m.get('main', -1):.1f}s loaded@{m.get('loaded', -1):.1f}s "
                f"exit@{m.get('exit', -1):.1f}s  " + ", ".join(f"{k.split('/')[1]}={v}" for k, v in status.items()))
            todo = [j for j in todo if j["id"] not in status]
            launch += 1
            if launch > 20:
                break
    write_summary(out_dir)
    log("done")
    return 0


def write_summary(out_dir: Path) -> None:
    rows = []
    for p in sorted(out_dir.glob("*.prof.json")):
        d = json.loads(p.read_text())
        c, ph, ds = d.get("counters", {}), d.get("phases", {}), d.get("distinct", {})
        rows.append({
            "id": p.name[:-len(".prof.json")].replace("__", "/"),
            "status": "timeout" if d.get("timeout") else d.get("status", ""),
            "elapsed": round(d.get("elapsed", 0), 3),
            **{f"ph.{k}": round(v, 3) for k, v in ph.items()},
            **{k: int(v) for k, v in c.items()},
            **{f"distinct.{k}": int(v) for k, v in ds.items()},
            "inPhase": d.get("inPhase", ""),
        })
    cols = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    with (out_dir / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
