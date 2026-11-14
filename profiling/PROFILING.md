# Profiling the CPG build and the walk (2026-11-14)

Branch `profiling`. Everything here was measured on this machine: Tesla V100 instance, cgroup limit
~15 CPUs and 180 GiB RAM, Joern 4.0.627 on OpenJDK 21. One Joern JVM ran at a time throughout.

**Short answer.**

- **The walk's slowness is in the traversal rule's implementation, not in Joern or Scala.** The 19
  drafts that timed out in the published run spend their time on exponential re-walking inside
  `keepMostRoles`, on pure queries recomputed on every visit, and on an O(types × methods) index.
  Joern's own graph access is under 10 % of the walk's samples.
- **For the drafts that do finish, the cost is Joern's script tooling.** Each `joern --script`
  query spends 10–11 s before the walk starts (two JVMs, compiling ~1,300–2,000 lines of Scala 3,
  copying the CPG). The walk then takes 0.0–0.3 s.
- **Fix the rule first, then the tooling.** `walk_opt.sc` keeps the output identical and makes the
  walk polynomial on everything except the Camunda family. `walk_opt3.sc` adds an exact cut found
  while porting, which bounds the Camunda family too. The native engine (`joern-rust`) removes the
  tooling cost.
- **Migrating the frontends is not needed:** `joern-parse` stays (see §4).

## 1. How it was measured

| Script | What it does |
|---|---|
| `extract_jobs.py` | Captures the exact `(roles, behaviour, entry)` that the 2026-11-09 batch run sent for each of the 502 drafts. 379 drafts reach Joern; the other 123 have fewer than two grounded roles. Output: `jobs/jobs.jsonl`. |
| `prof_walk.py` | One instrumented JVM per repository, strictly sequential. Asserted text patches add phase timers (`exec`'s five steps), counters (bodies entered, memo hits, `keepMostRoles` calls, alternatives and nesting, `targetsOf` calls and distinct sites, CFG visits and distinct nodes) and an in-JVM watchdog. The watchdog prints progress every 10 s and, at the budget, dumps the main thread's stack and exits so JFR writes its recording. The patches do not change what the walk computes. |
| `startup_bench.py` | Splits the fixed cost of a query: a one-line script, the walk with no jobs, and the walk loading CPGs of 4–490 MB. |
| `jfr_analyze.py` | Execution-sample buckets, plus self and inclusive time per method of the walk script. |
| `compare_golden.py` | Compares raw walk outputs with the published orders, or with another run. |
| `prof_build.py` | `joern-parse` with JFR, Joern's INFO log, and the process tree's peak RSS and process count, under a pid/memory watchdog. |
| `warm_worker.py` | A long-lived `joern --script` that compiles the walk once, loads each CPG once with `CpgLoader` (read-only), and streams queries over stdin. This is the "fix the Joern client" baseline. |

## 2. Where a query's time goes (normal drafts)

`startup_bench.py`, sequential, two runs each:

| Launch | To script `main` | CPG load | Heap after load |
|---|---:|---:|---:|
| One-line script | 5.8 s (launcher JVM ~1.1 s + script JVM and console ~4.7 s) | – | – |
| walk.sc + profile, no jobs | 10.0–10.3 s (compiling the walk adds ~4.5 s) | 0.15 s (14 KB) | 0.14 GB |
| load factcast | 10.3–10.6 s | 0.34–0.41 s (4 MB) | 0.18 GB |
| load camunda-bpm-platform | 9.9–10.2 s | 0.62–0.71 s (37 MB) | 0.54 GB |
| load camunda | 10.3–10.6 s | 1.06–1.18 s (92 MB) | 1.1 GB |
| load langflow | 11.0–11.5 s | 2.0–2.4 s (200 MB) | 2.7 GB |
| load litellm | 11.1–11.4 s | 4.75–4.78 s (490 MB) | 6.6 GB |

- **`importCpg` copies the CPG** into a workspace ("Creating working copy of CPG to be safe") and
  saves the project on exit.
- **The query service does the same per query** (`joern-runner/app/runner.py` runs one
  `joern --script` each time).
- **Most CPGs are small:** median 3.8 MB, p90 47 MB.
- **So nearly every query is ~11 s of fixed cost wrapped around a walk of 0.0–0.3 s.** Example: the
  20-draft validation set (`opt-check2`) took 9.4 min of wall time, and its walks summed to 12 s.

## 3. Why the 19 drafts timed out

All 19 were rerun with the original walk, a 120 s budget each, and JFR on (`results/timeouts/`):

TIMEOUTS_TABLE

Three causes, all in the rule's implementation:

1. **`keepMostRoles` re-walks the same bodies exponentially often.**
   - It snapshots `enteredBodies`, runs each alternative, and restores the snapshot, so every
     alternative re-walks everything its siblings walked.
   - Alternatives nest 12–48 deep, which multiplies the re-walks.
   - Examples: factcast re-entered its 171 distinct bodies 930 k times (about 5,400× each); on
     EximeeBPMS, ≤ 930 keys were entered 643 k times.
   - The cloning itself added 130 M copied set elements on factcast.
2. **Pure queries are recomputed on every visit.**
   - `targetsOf` ran 1,226× per call site on factcast. 80 % of factcast's walk samples are
     `targetsOf → injected → providersOf → simpleName` (`String.split` over every `@Bean` provider,
     for every call).
   - On the Python drafts, 58–79 % of samples are in the profile's `Index.chain → toGraphFile`,
     which scans every graph file with `endsWith` and has no memo.
3. **`methodsOfType` is O(types × methods).**
   - It took 15–25 s on the Camunda family.
   - It ran past the whole 120 s budget on camunda_camunda, langflow and cadelphi, which never
     reached the walk.

Joern itself is not the bottleneck: flatgraph steps (`collectAll`, `getNeighbors`) are < 10 % of
samples, GC pauses 1–4.5 s per 120 s run, and loading even a 490 MB CPG takes under 5 s.

## 4. The CPG build

BUILD_TABLE

BUILD_NOTES

## 5. Optimising the rule (output kept identical)

Each change below was checked by running the full job set and diffing against the published orders.

| Version | Change | Why it is exact |
|---|---|---|
| `walk_opt.sc` | Memoise `targetsOf`, `providersOf`, `fieldsOf`, `methodsPassedTo`, `entryEdges`, CFG successors, `brief` | Pure functions of the graph |
| | Build `methodsOfType` from each method's dotted prefixes | Both profiles define `declares` as `member.startsWith(owner + ".")`; same per-type order |
| | Undo log instead of `enteredBodies.clone()` | Restores the same set |
| | Memoise walked bodies, keyed `(method, start, sources, |via|, dispatched, framework)` and replayed only when every entered-set lookup they made (present or absent) answers the same | `via`, `path` and `enteredBy` only flow into edges (stored relative to the caller); `keepMostRoles` reads only `via`'s length |
| | Skip bodies that can reach no role-carrying node (lazy Tarjan liveness) | A dead body emits nothing, and its entered keys are only ever tested by dead walks |
| `opt/python.sc` | Memoise `toGraphFile`, and `chain` when `seen` is empty | Pure; inner `chain` calls depend on the cycle cut, so they are not cached |
| `walk_opt3.sc` | In `keepMostRoles`, do not walk an alternative that provably emits nothing. Decided per (method, dispatched) state, with the second-override gate applied, without entering bodies already in the entered set. | Such an alternative cannot win (rank needs a target) and is rolled back without a trace. The entered set only grows while it runs. |

OPT_RESULTS

## 6. The native engine

JOERN_RUST

## 7. GPU

GPU_NOTES

## 8. Reproduce

```bash
/workspace/cpg-backfill/venv/bin/python -I profiling/extract_jobs.py              # jobs/jobs.jsonl
python3 profiling/startup_bench.py --repeat 2
python3 profiling/prof_walk.py --run timeouts --ids $(jq -r '.[]' profiling/jobs/timeouts.json) --budget 120 --jfr
python3 profiling/prof_walk.py --run opt-full --joern-dir profiling/opt --budget 180
python3 profiling/prof_walk.py --run opt3-timeouts --joern-dir profiling/opt3 --ids $(jq -r '.[]' profiling/jobs/timeouts.json) --budget 300
python3 profiling/compare_golden.py opt-full
python3 profiling/jfr_analyze.py profiling/results/timeouts/<repo>.L0.<pid>.jfr --thread main
python3 profiling/prof_build.py --run build --jfr --repos REPO:python REPO:java[:delombok=types-only] ...
python3 profiling/report_tables.py timeouts
```
