# Profiling the CPG build and the traversal (2026-11-14)

Branch `profiling`. Everything here was measured on this machine: Tesla V100 instance, cgroup limit
~15 CPUs and 180 GiB RAM, Joern 4.0.627 on OpenJDK 21. One Joern JVM ran at a time throughout.

**Short answer.**

- **The traversal's slowness is in the rule's implementation, not in Joern or Scala.** The 19
  drafts that timed out in the published run spend their time on exponential re-traversal inside
  `keepMostRoles`, on pure queries recomputed on every visit, and on an O(types × methods) index.
  Joern's own graph access is under 10 % of the traversal's samples.
- **For the drafts that do finish, the cost is Joern's script tooling.** Each `joern --script`
  query spends 10–11 s before the traversal starts (two JVMs, compiling ~1,300–2,000 lines of Scala 3,
  copying the CPG). The traversal then takes 0.0–0.3 s.
- **Fix the rule first, then the tooling.** `walk_opt.sc` keeps the output identical and makes the
  traversal polynomial on everything except the Camunda family. `walk_opt3.sc` adds an exact cut found
  while porting, which bounds the Camunda family too. The native engine (`joern-rust`) removes the
  tooling cost.
- **Migrating the frontends is not needed:** `joern-parse` stays (see §4).

## 1. How it was measured

| Script | What it does |
|---|---|
| `extract_jobs.py` | Captures the exact `(roles, behaviour, entry)` that the 2026-11-09 batch run sent for each of the 502 drafts. 379 drafts reach Joern; the other 123 have fewer than two grounded roles. Output: `jobs/jobs.jsonl`. |
| `prof_walk.py` | One instrumented JVM per repository, strictly sequential. Asserted text patches add phase timers (`exec`'s five steps), counters (bodies entered, memo hits, `keepMostRoles` calls, alternatives and nesting, `targetsOf` calls and distinct sites, CFG visits and distinct nodes) and an in-JVM watchdog. The watchdog prints progress every 10 s and, at the budget, dumps the main thread's stack and exits so JFR writes its recording. The patches do not change what the traversal computes. |
| `startup_bench.py` | Splits the fixed cost of a query: a one-line script, the traversal with no jobs, and the traversal loading CPGs of 4–490 MB. |
| `jfr_analyze.py` | Execution-sample buckets, plus self and inclusive time per method of the traversal script. |
| `compare_golden.py` | Compares raw traversal outputs with the published orders, or with another run. |
| `prof_build.py` | `joern-parse` with JFR, Joern's INFO log, and the process tree's peak RSS and process count, under a pid/memory watchdog. |
| `warm_worker.py` | A long-lived `joern --script` that compiles the traversal once, loads each CPG once with `CpgLoader` (read-only), and streams queries over stdin. This is the "fix the Joern client" baseline. |

## 2. Where a query's time goes (normal drafts)

`startup_bench.py`, sequential, two runs each:

| Launch | To script `main` | CPG load | Heap after load |
|---|---:|---:|---:|
| One-line script | 5.8 s (launcher JVM ~1.1 s + script JVM and console ~4.7 s) | – | – |
| walk.sc + profile, no jobs | 10.0–10.3 s (compiling the traversal adds ~4.5 s) | 0.15 s (14 KB) | 0.14 GB |
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
- **So nearly every query is ~11 s of fixed cost wrapped around a traversal of 0.0–0.3 s.** Example: the
  20-draft validation set (`opt-check2`) took 9.4 min of wall time, and its traversals summed to 12 s.

## 3. Why the 19 drafts timed out

All 19 were rerun with the original traversal, a 120 s budget each, and JFR on (`results/timeouts/`):

| draft | stopped in | bodies entered | distinct keys | kmr nesting | targetsOf calls / sites | CFG visits / distinct | methodsOfType (s) |
|---|---|---:|---:|---:|---:|---:|---:|
| java EximeeBPMS_eximeebpms | 4-walk | 642,614 | 930 | 34 | 893,947 / 10,256 | 7,010,491 / 62,253 | 15.4 |
| java OrqueIO_OrqueIO | 4-walk | 647,142 | 910 | 31 | 916,030 / 10,120 | 7,392,282 / 60,991 | 20.2 |
| java cadenzaflow_cadenzaflow-bpm-platform | 4-walk | 500,774 | 910 | 31 | 674,203 / 10,196 | 5,471,416 / 61,467 | 22.8 |
| java camunda_camunda-bpm-platform | - | 595,384 | 903 | 30 | 836,130 / 10,081 | 6,753,450 / 60,935 | 17.7 |
| java camunda_camunda | lazy.methodsOfType | 0 | 0 | 0 | 5 / 5 | 0 / 0 | 0.0 |
| java cibseven_cibseven | 4-walk | 489,312 | 874 | 27 | 761,439 / 9,633 | 6,104,701 / 58,136 | 20.7 |
| java factcast_factcast | 4-walk | 930,160 | 171 | 19 | 2,194,578 / 1,790 | 13,132,676 / 9,514 | 0.6 |
| java operaton_operaton | 4-walk | 620,172 | 1,248 | 31 | 928,009 / 15,479 | 7,112,599 / 92,140 | 24.7 |
| java DSpace_DSpace | 4-walk | 250,848 | 261 | 13 | 1,074,282 / 3,448 | 5,919,716 / 20,354 | 6.4 |
| python BilgisayarKavramlari_cadelphi | lazy.methodsOfType | 0 | 0 | 0 | 0 / 0 | 0 / 0 | 0.0 |
| python agno-agi_agno | 4-walk | 261,902 | 434 | 30 | 492,159 / 7,631 | 6,222,687 / 89,783 | 0.0 |
| python terrene-foundation_kailash-py | 4-walk | 17,577 | 178 | 27 | 77,471 / 4,733 | 956,392 / 58,426 | 0.0 |
| python langflow-ai_langflow | lazy.methodsOfType | 0 | 0 | 0 | 0 / 0 | 0 / 0 | 0.0 |
| python terrene-foundation_kailash-py | 4-walk | 12,869 | 176 | 26 | 56,740 / 4,698 | 700,022 / 57,883 | 0.0 |
| python topoteretes_cognee | 4-walk | 644,211 | 902 | 44 | 1,356,706 / 9,405 | 12,021,254 / 100,134 | 0.0 |
| python CIRISAI_CIRISAgent | 4-walk | 719,411 | 360 | 48 | 1,437,018 / 3,322 | 11,475,076 / 38,762 | 0.0 |
| python TrueSelph_jvspatial | 4-walk | 842,287 | 329 | 12 | 5,593,692 / 3,930 | 66,362,154 / 46,599 | 0.0 |
| python BerriAI_litellm | 4-walk | 441,893 | 800 | 36 | 711,998 / 7,671 | 10,095,387 / 89,681 | 0.0 |
| python ThalesGroup_fred | 4-walk | 629,845 | 172 | 14 | 2,606,316 / 2,336 | 30,961,036 / 26,103 | 0.0 |

Three causes, all in the rule's implementation:

1. **`keepMostRoles` re-traverses the same bodies exponentially often.**
   - It snapshots `enteredBodies`, runs each alternative, and restores the snapshot, so every
     alternative re-traverses everything its siblings traversed.
   - Alternatives nest 12–48 deep, which multiplies the re-traversals.
   - Examples: factcast re-entered its 171 distinct bodies 930 k times (about 5,400× each); on
     EximeeBPMS, ≤ 930 keys were entered 643 k times.
   - The cloning itself added 130 M copied set elements on factcast.
2. **Pure queries are recomputed on every visit.**
   - `targetsOf` ran 1,226× per call site on factcast. 80 % of factcast's traversal samples are
     `targetsOf → injected → providersOf → simpleName` (`String.split` over every `@Bean` provider,
     for every call).
   - On the Python drafts, 58–79 % of samples are in the profile's `Index.chain → toGraphFile`,
     which scans every graph file with `endsWith` and has no memo.
3. **`methodsOfType` is O(types × methods).**
   - It took 15–25 s on the Camunda family.
   - It ran past the whole 120 s budget on camunda_camunda, langflow and cadelphi, which never
     reached the traversal.

Joern itself is not the bottleneck: flatgraph steps (`collectAll`, `getNeighbors`) are < 10 % of
samples, GC pauses 1–4.5 s per 120 s run, and loading even a 490 MB CPG takes under 5 s.

## 4. The CPG build

`prof_build.py`, one build at a time, `-Xmx32g`, JFR on (`results/build/`):

| repo | lang | delombok | source files | wall (s) | CPG MB | peak RSS (GB) | peak procs (java) | killed |
|---|---|---|---:|---:|---:|---:|---:|---|
| CI-CMG_geosamples-ingest | java | off | 253 | 20 | 2.0 | 2.5 | 7 (2) | - |
| JeanExtreme002_Dokku-API | python | off | 85 | 10 | 2.0 | 1.1 | 7 (2) | - |
| camunda_camunda | java | off | 15507 | 130 | 91.7 | 16.4 | 7 (2) | - |
| cibseven_cibseven | java | off | 8807 | 71 | 38.3 | 15.5 | 7 (2) | - |
| langflow-ai_langflow | python | off | 4037 | 143 | 200.3 | 24.2 | 7 (2) | - |
| shangyankeji_super-dev | python | off | 313 | 50 | 42.0 | 10.2 | 7 (2) | - |
| smart-cloud_smart-cloud | java | off | 713 | 20 | 2.1 | 1.8 | 7 (2) | - |
| smart-cloud_smart-cloud | java | types-only | 713 | 106 | 2.1 | 10.8 | 40 (38) | - |

Breakdown from Joern's own per-pass log lines:

| Repo | Wall | Logged passes | `ReachingDefPass` | Largest other passes |
|---|---:|---:|---:|---|
| CI-CMG (Java, 253 files) | 20.5 s | 3.7 s | 1.9 s (9 %) | CfgCreation 0.3, CfgDominator 0.3 |
| cibseven (Java, 8,807) | 71.0 s | 14.2 s | 6.0 s (8 %) | DynamicCallLinker 2.2, CfgDominator 1.3 |
| camunda (Java, 15,507) | 129.6 s | 22.5 s | 9.3 s (7 %) | DynamicCallLinker 3.1, CfgDominator 2.0 |
| Dokku-API (Python, 85) | 10.3 s | 4.2 s | 1.4 s (14 %) | PythonTypeRecovery 0.8 |
| super-dev (Python, 313) | 50.1 s | 37.4 s | 21.2 s (42 %) | PythonTypeRecovery 6.2, NaiveCallLinker 2.1 |
| langflow (Python, 4,037) | 142.7 s | 109.1 s | 23.1 s (16 %) | PythonTypeRecovery 47.2, NaiveCallLinker 12.1 |

- **Java builds are frontend-bound.** JavaParser plus symbol solving is everything outside the
  logged passes: 80–85 % of wall time.
- **Python builds are pass-bound,** dominated by `PythonTypeRecovery` and `ReachingDefPass`.
- **Delombok is the build's real bottleneck, as agreed earlier.**
  - On a 713-file Lombok repository, `types-only` took 5.4× the time (106 s vs 20 s) and 6× the
    memory (10.8 vs 1.8 GB), running 38 concurrent JVMs instead of 2, for a graph of the same size.
  - Every delombok JVM sizes its heap at 75 % of RAM unless capped. A guard watched the cgroup's
    task count during this build.
- **`ReachingDefPass` is pure overhead for the traversal,** which never reads data-flow edges (no
  reaching-def use anywhere in walk.sc, java.sc or python.sc).
  - `joern-parse --max-num-def 0` drops it: 1.9 M → 372 `REACHING_DEF` edges on cibseven.
  - Build time falls 27 % on super-dev (50.1 → 36.6 s) and 8 % on cibseven.
  - Traversals on the rebuilt Python CPGs are identical (3/3 drafts).
- **Java CPG builds are not reproducible.**
  - Two `joern-parse` runs on cibseven with identical settings, minutes apart, gave METHOD 53,284 vs
    53,304 and CALL edges 278,489 vs 278,398, and the stored CPG differs again (53,134 / 278,782).
  - The METHOD count varies with the set of stub methods for unresolved callees, i.e. call
    resolution itself varies between runs. cibseven's traversal output differs on each rebuild.
  - This is the frontend (javasrc2cpg), independent of `--max-num-def`.
  - Consequence: published orders must be reproduced from the shipped CPGs, never from a rebuild,
    as the README already prescribes. Why javasrc2cpg varies (parallel symbol resolution is the
    likely suspect) is worth a separate look.

Client changes made on this branch (`joern_local.py`, `config.py`):
- builds pass `--max-num-def 0` unless `JOERN_PARSE_DATAFLOW=1`;
- `JOERN_PARSE_XMX` caps every JVM a build starts, delombok's children included, through
  `JAVA_TOOL_OPTIONS`;
- `JOERN_DELOMBOK_MODE` selects the delombok mode (default unchanged, `types-only`; `no-delombok`
  is what built the last 14 CPGs).

## 5. Optimising the rule (output kept identical)

Each change below was checked by running the full job set and diffing against the published orders.

| Version | Change | Why it is exact |
|---|---|---|
| `walk_opt.sc` | Memoise `targetsOf`, `providersOf`, `fieldsOf`, `methodsPassedTo`, `entryEdges`, CFG successors, `brief` | Pure functions of the graph |
| | Build `methodsOfType` from each method's dotted prefixes | Both profiles define `declares` as `member.startsWith(owner + ".")`; same per-type order |
| | Undo log instead of `enteredBodies.clone()` | Restores the same set |
| | Memoise traversed bodies, keyed `(method, start, sources, |via|, dispatched, framework)` and replayed only when every entered-set lookup they made (present or absent) answers the same | `via`, `path` and `enteredBy` only flow into edges (stored relative to the caller); `keepMostRoles` reads only `via`'s length |
| | Skip bodies that can reach no role-carrying node (lazy Tarjan liveness) | A dead body emits nothing, and its entered keys are only ever tested by dead traversals |
| `opt/python.sc` | Memoise `toGraphFile`, and `chain` when `seen` is empty | Pure; inner `chain` calls depend on the cycle cut, so they are not cached |
| `walk_opt3.sc` | In `keepMostRoles`, do not traverse an alternative that provably emits nothing. Decided per (method, dispatched) state, with the second-override gate applied, without entering bodies already in the entered set. | Such an alternative cannot win (rank needs a target) and is rolled back without a trace. The entered set only grows while it runs. |

Results (all one JVM at a time; raw outputs in `results/opt-full/` and `results/opt3-timeouts/`):

| Run | Drafts | Identical to published | Different | Former timeouts that now derive | Still unbounded |
|---|---:|---:|---:|---:|---|
| `opt-full` (walk_opt.sc + opt/python.sc, 180 s budget) | 379 | **360 / 360** | 0 | 9 / 19 | 6 Camunda forks, cognee, CIRISAgent, cadelphi; litellm hit a `StackOverflowError` |
| `opt3-timeouts` (walk_opt3.sc, 300 s budget) | 19 | – | – | **17 / 19** | CIRISAgent (session_based); litellm (`StackOverflowError` at traversal depth 367) |

- **Speed:** the median traversal is 0.38 s and the p90 1.9 s.
  - On the 19 former timeouts the median is 8 s; the slowest is cadelphi at 277 s on the JVM.
  - factcast: from 930 k bodies entered in 120 s without finishing, to 12.6 k bodies and 1.0 s.
- **Fixed cost is now everything:** the 308 launches of `opt-full` took 101 min of wall time, and
  the traversals summed to 7 min.
- **Cross-check with the Rust port:** every former timeout that both engines derive is identical
  (15 Java, 8 Python; 0 different).
- **The two drafts left are not an implementation problem.**
  - Their alternatives nest 67–69 deep, and the cut skips over a million of them, yet the search
    stays unbounded in both engines.
  - Separately, litellm overflows the JVM thread stack before the rule's own `MAX_DEPTH = 512` guard.
    Running the traversal on a thread with a large stack would fix that crash without changing
    semantics.
  - Making them finish needs a decision about what `keepMostRoles` should mean (e.g. a bound on
    nested alternatives, or alternatives that do not undo each other's entered bodies). That would
    change the rule, so it is left to the authors.

## 6. The native engine

Repository `/workspace/joern-rust` (`docs/PLAN.md`, `docs/WALK.md`, `docs/cpg-store-spike.md`):

- **`crates/cpg-store`** reads `joern-parse`'s flatgraph files directly.
  - Opening reads only the header (~2–7 ms); columns are zstd-decoded on first touch.
  - Node ids and iteration order equal Joern's.
  - All 392 corpus CPGs decode, and every edge's OUT and IN copies agree (2.4 G edges).
- **`crates/secpat-walk`** ports walk_opt3.sc, java.sc and opt/python.sc, and returns the same JSON.
  - CLI: `run`, `batch`, `serve`, with `--liveness off|methods|sources|full|entered` (all give the
    same output; `entered` = walk_opt3's cut, the default) and `--gpu`.
  - The package uses it with `JOERN_ENGINE=rust` (`rust_walk.py`); a missing CPG is still built
    with `joern-parse`.

**Parity, all 379 drafts:**

| Reference | Identical | Different |
|---|---:|---:|
| Published orders | 360 / 360 | 0 |
| JVM `opt-full`, anchoredRoles included | 369 / 369 | 0 |
| JVM `opt3-timeouts` | 17 / 17 | 0 |

377 of 379 finish; the same two drafts as on the JVM do not (CIRISAgent, litellm).

**Cost per query:**

| Engine | Per query | All 379 drafts |
|---|---|---|
| `joern --script`, walk_opt (today's client) | ~11 s fixed + traversal | 101 min (308 JVMs), 9 still timing out |
| Warm Joern worker (`warm_worker.py`) | after a 10–12 s start: 0.1–0.6 s; 20 drafts in 41 s vs 9.4 min | – |
| `secpat-walk` (Rust) | CPG open 2 ms; traversal median 10 ms (Java) / 76 ms (Python), p90 0.24 / 1.1 s | ~2.3 min of traversal (26 s Java, 112 s Python) |
| `detect()` with `JOERN_ENGINE=rust` | 0.02–0.05 s on small repos (vs ~11–14 s) | – |

**Memory** (peak RSS of the whole process tree, sampled every 50 ms; same drafts, each run alone;
Joern runs `walk_opt3.sc` with `-Xmx48g`, close to the client's default of a quarter of the
container's 180 GiB; results in `results/mem-*`):

| Draft (CPG size) | `secpat-walk` | `joern --script` | Joern heap after GC, max |
|---|---:|---:|---:|
| ton-blockchain-mcp (541 MB) | 3.1 GB | 15.2 GB | 10.6 GB |
| kailash-py, 2 drafts (494 MB) | 3.6 GB | 33.3 GB | 9.7 GB |
| litellm (490 MB), never finishes | 15.3 GB at the 300 s limit | 31.1 GB when it died (StackOverflowError) | 8.5 GB |

- Peak RSS per CPG in Rust: median 13 MB (Java) / 48 MB (Python).
- Of the Rust peaks, the decoded graph columns are 2.1–2.3 GB; the rest is the traversal. The JVM holds
  the whole graph in its heap (6.6 GB for litellm after `importCpg`) and lets garbage build up to
  ~28 GB between collections, so its process grows to its 32–34 GB committed heap.
- litellm is not comparable: its traversal never ends, and Rust gets ~1,000× further before the limit
  (16.6 M bodies traversed and 526 k memo summaries, against 14.6 k bodies when the JVM's stack
  overflowed). Its memo of traversed bodies grows by up to ~120 MB/s.
- An earlier version of this report said the 490–541 MB Python CPGs peak at 13.5–13.7 GB in Rust.
  That was a measurement artefact. In the all-drafts batch, kailash-py and ton-blockchain-mcp ran
  after litellm in the same process, and the allocator kept litellm's ~15 GB after its traversal was
  aborted, so their per-CPG peaks included it. Reproduced: ton-blockchain-mcp alone 3.1 GB, after
  litellm in one process 16.0 GB. To do: give memory back after each draft (`malloc_trim`, or an
  allocator that returns it) so a long-lived `serve` process does not keep it, and cap the traversal
  memo so an unbounded draft cannot grow without limit.

**Bugs found in the Scala while porting,** reproduced faithfully, not fixed (they change outputs;
for the authors to decide):
- `fieldIn` keeps climbing after an ambiguous level.
- The entered key ignores `dispatched`, so results depend on visit order.
- `Entry.reachesFrom(..).toMap` drops a duplicate role row.
- The key `sources.mkString(" ")` collides for role names with spaces.
- python.sc:
  - `importedIn.toMap`: the last import wins;
  - `assignments` takes the first ARGUMENT, not `argumentIndex == 1`;
  - `signature` counts parentheses inside strings;
  - a latent `NoSuchElementException` on `__newInstance` identifiers;
  - `guarded` caching depends on question order.

## 7. GPU

Both GPU paths were built and measured.
- **Rust-CUDA** (`rustc_codegen_nvvm`) runs on the V100, but it is not a good foundation:
  - it needs a pinned nightly, git-only dependencies and LLVM 7;
  - it is 12–29 % slower than nvcc;
  - it has no future on sm_70.
- **The candle pattern** (CUDA C compiled to PTX by nvcc in `build.rs`, launched through `cudarc`) is
  what `crates/reach` uses: cargo feature `cuda`, runtime `--gpu on|off|auto`, CPU fallback with a
  reason. Details: `joern-rust-spikes/gpu/FINDINGS.md`, `crates/reach/README.md`.
- **Only bulk work fits the GPU.** The traversal is an order-sensitive depth-first search, which belongs
  on the CPU. Multi-source reachability on large graphs is the one thing that fits: 19–115× faster
  at 1M–5M nodes with 64+ source sets.
- **At SecPat's sizes the GPU loses.** Graphs have ≤ 300 k methods and repositories ≤ 6 drafts.
  Wired into the engine (batched liveness over a repository's drafts), outputs were identical with
  the GPU on or off (181/181), but the run took 101.5 s with it vs 28.6 s without. Building the
  method graph cost 71 s, the reach 0.55 s, of which 220 ms is the CUDA context. `auto` therefore
  stays on the CPU. The switch remains for corpus-scale analyses.

## 8. Recommendations

1. **Adopt walk_opt3.sc + opt/python.sc as walk.sc / python.sc.**
   - Identical output on all 360 published sequences.
   - 17 of the 19 drafts that had no sequence now get one, and agree across both engines.
   - Seconds instead of > 870 s.
   - The new sequences (17 drafts) are additions to the dataset; nothing published changes.
2. **Use the native engine (`JOERN_ENGINE=rust`) wherever the traversal runs repeatedly:** the annotation
   portal after every edit, Level 3 scoring (both sides per record), batch backfills.
   - It removes the ~11 s per query.
   - Keep `joern-parse` for building.
   - If a JVM must stay, the warm worker is the cheap middle ground (one start, then sub-second
     queries).
3. **Builds:**
   - keep delombok off for new builds (`JOERN_DELOMBOK_MODE=no-delombok`);
   - cap the heap (`JOERN_PARSE_XMX`);
   - skip reaching definitions (now the default);
   - never rebuild a Java CPG behind a published number: javasrc2cpg is not deterministic.
4. **Rule questions left to the authors:**
   - the two remaining drafts (alternatives nested 67–69 deep);
   - the Scala bugs listed in §6, each of which changes outputs if fixed.
5. **GPU:** keep it off for this workload; the switch is there for corpus-scale reachability.

## 9. Reproduce

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
