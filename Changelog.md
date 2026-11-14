# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Numbers below come from
`profiling/PROFILING.md` and the run outputs under `profiling/results/`.

"Drafts" are the SecPat drafts of the 2026-11-09 batch run: 379 of its 502 drafts reach
Joern (`profiling/jobs/jobs.jsonl`); the others have fewer than two grounded roles. "Published
orders" are that run's outputs. 19 drafts timed out in it and have no order.

## [0.2.0] - unreleased (branch release-0.2.0)

The walk no longer repeats work. Its traversal rules are unchanged, and on the 360 drafts that
had an order its output is identical (see Output for which run shows what). 17 of the 19
drafts that timed out now finish.

### Added

- MIT license: `LICENSE`, `license = "MIT"` in `pyproject.toml`, and a License section in the
  README.
- CPG build options for local `joern-parse` (`config.py`, `joern_local.py`):
  - `JOERN_DELOMBOK_MODE`: delombok mode for Java builds. The default, `types-only`, is
    unchanged; `no-delombok` skips delombok. On a 713-file Lombok repository, `types-only` took
    106 s, 10.8 GB and 38 concurrent JVMs, against 20 s, 1.8 GB and 2 JVMs without it, for a
    graph of the same size.
  - `JOERN_PARSE_DATAFLOW`: set it to `1` to build the data-flow overlay (see Changed).
  - `JOERN_PARSE_XMX`: heap cap for every JVM a build starts, passed through
    `JAVA_TOOL_OPTIONS` so that delombok's child JVMs inherit it. Unset, each JVM sizes its own
    heap.
- `JOERN_ENGINE=rust` (experimental, opt-in; the default stays `joern`). It runs the walk on
  `secpat-walk`, a Rust port of the walk and both profiles from the separate joern-rust project.
  It reads the same CPG files. The binary is not published in this repository: point
  `SECPAT_WALK_BIN` at a build of it. `SECPAT_WALK_SERVE=1` keeps one `secpat-walk serve`
  process per Python process. A missing CPG is still built with `joern-parse`. This applies
  only to local runs (`JOERN_SERVICE_URL` unset). On small repositories `detect()` answered in
  0.02–0.05 s instead of ~11–14 s.
- `profiling/`, the profiling harness and its report:
  - `PROFILING.md`: the write-up.
  - `prof_walk.py`: an instrumented walk, one JVM at a time, with a watchdog and optional JFR.
  - `prof_build.py`, `startup_bench.py`, `jfr_analyze.py`, `warm_worker.py`,
    `report_tables.py`.
  - `extract_jobs.py` and `jobs/`: the 379 drafts' exact walk parameters.
  - `compare_golden.py`: compares raw walk outputs with the published orders or with another
    run.
  - The intermediate walks (`walk_opt.sc`, `walk_opt2.sc`, `walk_opt3.sc`, `opt*/`).
  - Measured results under `results/`. JFR recordings and JVM logs are not committed.

  The scripts use paths of the machine they ran on (`/workspace/...`, `/opt/joern`).

### Changed

- README: ties the package to the paper's Appendix C. It links the paper and the other
  artifacts, maps the output fields to the paper's terms, and documents the two walk rules that
  Appendix C does not describe (`co-located` interactions, the `code-root` entry role). It also
  pins Joern 4.0.627 and a full JDK, and says the query service is not one of the paper's
  artifacts. Later edits add the build and engine settings, say that version 0.1.0 derived the
  groundtruth sequences, and link this file.
- **The walk.** `joern/walk.sc` is now `profiling/walk_opt3.sc`, and `joern/python.sc` is now
  `profiling/opt/python.sc` (the same file as `profiling/opt3/python.sc`). Both are byte-for-byte
  copies. `joern/java.sc` is unchanged. The `@main def exec` entry point, its parameters, the
  output JSON and `SCHEMA_VERSION` (2) are unchanged. `Edge` gains two bookkeeping fields,
  `chain` and `ctxParent`, which are never rendered. The script is sent from this source tree,
  so the change applies to local Joern and to the query service alike.

  Why the 19 drafts timed out (PROFILING.md §3). Joern's own graph steps were under 10 % of the
  walk's samples. The time went to three things:
  - `keepMostRoles` snapshotted the entered-bodies set, ran each alternative, and restored the
    snapshot. Every alternative therefore re-walked what its siblings had walked, with
    alternatives nested 12–48 deep. factcast re-entered its 171 distinct bodies 930 k times, and
    the set clones copied 130 M elements.
  - Pure queries were recomputed on every visit. `targetsOf` ran 1,226 times per call site on
    factcast. On the Python drafts, 58–79 % of samples were in the profile's `Index.chain` →
    `toGraphFile`.
  - `methodsOfType` was O(types × methods). It took 15–25 s on the Camunda family and ran past
    the 120 s profiling budget on camunda_camunda, langflow and cadelphi.

  What changed, and why each change is exact (PROFILING.md §5):
  - **Memoised pure queries:** `targetsOf`, `providersOf` (each provider's return type is read
    once), `fieldsOf`, `methodsPassedTo`, `referencedMethodInRepo`, `isCallArgument`, the entry
    edges, CFG successors, a method's file, and `brief` of a call's code. These are pure
    functions of the graph.
  - **`methodsOfType` from each method's dotted name prefixes**, i.e. O(methods × dots). This is
    exact because both profiles define `declares(owner, member)` as
    `member.startsWith(owner + ".")`, and the per-type order is kept.
  - **An undo log instead of set clones:** `keepMostRoles` rolls an alternative back by
    truncating a log of the bodies it entered, which restores the same set.
  - **An exact memo of walked bodies.** A walked body's edges and entered keys are stored under
    (method, start node, roles, length of `via`, `dispatched`, `framework`). They are replayed
    only when every entered-set lookup the body made, whether the key was present or absent,
    answers the same. Edges of the body's own helper chain are stored relative to the caller
    and rebuilt with its `via`, `path` and `parent`. This is exact because `via`, `path` and
    `enteredBy` only flow into the edges, and `keepMostRoles` reads only `via`'s length.
  - **Dead bodies are skipped.** A body from which no role-carrying node can be reached is not
    walked (lazy liveness over Tarjan's SCCs). A dead body emits nothing, and its entered keys
    are only ever tested by dead walks.
  - **The exact `keepMostRoles` cut.** `keepMostRoles` does not walk an alternative that
    provably emits nothing. This is decided per (method, `dispatched`) state, with the
    second-override gate applied and without entering bodies already in the entered set. Such
    an alternative cannot win (the rank needs a target) and would be rolled back without a
    trace, and the entered set only grows while it runs. This cut was found while porting the
    walk to Rust; on the Camunda forks every hot alternative had rank 0.
  - **Python profile:** `Index.toGraphFile` is memoised, and so is `Index.chain` when `seen` is
    empty. Both are pure. The inner `chain` calls depend on the cycle cut, so they are not
    cached.
- **Builds skip the data-flow overlay by default.** Local builds now pass `--max-num-def 0`,
  which drops `ReachingDefPass`; `JOERN_PARSE_DATAFLOW=1` restores the old build. The evidence
  (PROFILING.md §4):
  - No reaching-definition data is used in `walk.sc`, `java.sc` or `python.sc`.
  - `REACHING_DEF` edges fell from 1.9 M to 372 on cibseven.
  - Builds were 27 % faster on super-dev (Python, 50.1 → 36.6 s) and 8 % faster on cibseven.
  - Walks on the rebuilt Python CPGs were identical (3/3 drafts).
  - This could not be checked on Java, because two Java builds differ anyway (see Known
    issues).

  CPGs already in the store are used as they are, so only new builds are affected.
- Version 0.1.0 → 0.2.0 in `pyproject.toml`.

### Removed

- README's Tests section, which named tests the repository does not have.

### Output

Evidence that the walk's output is unchanged:

| Run | Walk | Drafts | Result |
|---|---|---:|---|
| `results/opt-full` (JVM, 180 s budget) | `walk_opt.sc` + optimised `python.sc`: everything above except the `keepMostRoles` cut | 379 | identical to the published orders on **360 / 360**, 0 different; 9 of the 19 former timeouts finish |
| `results/opt3-timeouts` (JVM, 300 s budget) | the promoted walk | the 19 former timeouts | **17 / 19** finish; the 9 that `opt-full` also finished are identical to it |
| `results/warm-opt3` (JVM, warm worker) | the promoted walk | 20 | identical to the published orders on 16 / 16 |
| `secpat-walk`, a Rust port of the promoted walk (its cut is on by default) | – | 379 | identical to the published orders on **360 / 360**; to `opt-full`'s raw outputs, `anchoredRoles` included, on **369 / 369**; to `opt3-timeouts` on 17 / 17 |

On `opt-full`, the median walk took 0.38 s and the p90 1.9 s. On the former timeouts with the
promoted walk the median was 8 s, and the slowest was cadelphi at 277 s. factcast went from
930 k bodies entered in 120 s without finishing to 12.6 k bodies in 1.0 s.

Drafts that timed out in the published run and now produce a result (`compare_golden.py
opt3-timeouts`). Three of them finish with no interaction.

| Draft | Interactions |
|---|---:|
| java/limit_request_rate/camunda_camunda-bpm-platform | 4 |
| java/limit_request_rate/factcast_factcast | 2 |
| java/obscure_token_based_access_control/DSpace_DSpace | 2 |
| java/limit_request_rate/EximeeBPMS_eximeebpms | 1 |
| java/limit_request_rate/OrqueIO_OrqueIO | 1 |
| java/limit_request_rate/operaton_operaton | 1 |
| java/limit_request_rate/cadenzaflow_cadenzaflow-bpm-platform | 1 |
| java/limit_request_rate/cibseven_cibseven | 1 |
| java/limit_request_rate/camunda_camunda | 0 |
| python/authentication_pwd/BilgisayarKavramlari_cadelphi | 5 |
| python/limit_request_rate/terrene-foundation_kailash-py | 4 |
| python/verifiable_token_based_authentication/ThalesGroup_fred | 3 |
| python/session_based_access_control/TrueSelph_jvspatial | 2 |
| python/obscure_token_based_access_control/langflow-ai_langflow | 2 |
| python/obscure_token_based_access_control/terrene-foundation_kailash-py | 2 |
| python/obscure_token_based_access_control/topoteretes_cognee | 0 |
| python/limit_request_rate/agno-agi_agno | 0 |

Two drafts are still unbounded, on the JVM and in the Rust port:
python/session_based_access_control/CIRISAI_CIRISAgent and
python/verifiable_token_based_authentication/BerriAI_litellm. Their `keepMostRoles`
alternatives nest 67–69 deep, and the search stays unbounded even though the cut skips over a
million of them. litellm also overflows the JVM thread stack (`StackOverflowError` at walk depth
367) before the walk's `MAX_DEPTH = 512` guard. Making them finish would change what
`keepMostRoles` means, which is a rule decision left open.

A record whose walk timed out under 0.1.0 can now get an order. Scores computed from such
records can therefore change.

### Known issues

- Bugs in the walk found while porting it to Rust. The port reproduces them faithfully, and
  none is fixed here, because each fix changes outputs:
  - `fieldIn` keeps climbing after an ambiguous level.
  - The entered key ignores `dispatched`, so results depend on visit order.
  - `Entry.reachesFrom(..).toMap` drops a duplicate role row.
  - The key `sources.mkString(" ")` collides for role names with spaces.
  - In `python.sc`:
    - `importedIn.toMap` lets the last import win.
    - `assignments` takes the first ARGUMENT, not `argumentIndex == 1`.
    - `signature` counts parentheses inside strings.
    - There is a latent `NoSuchElementException` on `__newInstance` identifiers.
    - `guarded` caching depends on question order.
- javasrc2cpg builds are not deterministic. Two `joern-parse` runs on cibseven with identical
  settings gave 53,284 vs 53,304 METHOD nodes and 278,489 vs 278,398 CALL edges, and the walk's
  output on cibseven differs on each rebuild. Do not rebuild a Java CPG behind a published
  number; reproduce from the shipped CPGs.
- `JOERN.md` still calls the entered set `Walker.enteredBodies`. The promoted walk keeps it as
  `entered` plus an undo `log`, with the same meaning.
- `profiling/prof_walk.py` without `--joern-dir` now profiles the promoted walk. To rerun
  PROFILING.md §3's profile of the original walk, pass `--joern-dir` a folder holding
  v0.1.0's `walk.sc`, `python.sc` and `java.sc`.
- No `v0.2.0` tag exists yet, so the README's install line still pins `v0.1.0`. To install
  this branch, use
  `pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git@release-0.2.0"`.

## [0.1.0] - 2026-10-23

Initial release, the version released around the time the paper was submitted. It contains:
- `detect()`: derives a role mapping's interaction order.
- The walk, run in Joern: `joern/walk.sc`, with the `LangSpec` profiles `java.sc` and
  `python.sc`.
- Two backends: a Joern query service (`JOERN_SERVICE_URL`) or a local Joern install.
- `JOERN.md`: a worked example.

[0.2.0]: https://github.com/anonymousspottedtoad/secpat_interaction_recovery/compare/v0.1.0...release-0.2.0
[0.1.0]: https://github.com/anonymousspottedtoad/secpat_interaction_recovery/tree/v0.1.0
