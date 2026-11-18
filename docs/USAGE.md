# Using secpat-interaction-order: the details

The [README](../README.md) shows the command `secpat-interaction-order`, on one case and on the
whole dataset. This page has the rest: how the output maps to the paper, what each module does,
where Joern runs and its settings, the native engine and the faster builds, the progress display,
the Python API, the Joern script on its own and the profiling runners.

## The output and the paper

This package is the interaction sequence recovery of the paper *A Hybrid Approach
for Automated Security Pattern Recovery* (Section III-B2, Interaction Sequence
Recovery). The paper's Appendix C (Using Code Property Graphs for Interaction
Recovery) describes the traversal rules and the edges the traversal adds, and
[`JOERN.md`](../src/secpat_interaction_order/JOERN.md) works through one example. The
appendix is at <https://doi.org/10.5281/zenodo.23664769>, and its Appendix G lists
the other artifacts. The traversal derived the groundtruth interaction sequences of the
paper's dataset (in version 0.1.0; 0.2.0 removes repeated work without changing its
rules or, on every order it was compared with, its output: see
[Changelog.md](../Changelog.md)), and IRC
([secpat_evaluation_metrics](https://github.com/anonymousspottedtoad/secpat_evaluation_metrics))
traverses both sides with it. It has rules for Java and Python, the two languages of
the paper.

In the output, an interaction of the paper is one entry of `interactions` (`from`,
`to`, the call site and its `path`), the entry role is `entryRole`, and the label
of an edge in Appendix C is its `mechanism`.

Two rules of the traversal are not described in Appendix C:

- **`co-located`.** When one code node carries several roles (one entity mapped to
  more than one role), the traversal records the interactions the pattern prescribes
  among those roles, labelled `co-located`. 20 of the 280 groundtruth interactions
  are `co-located`.
- **The entry role.** Appendix C starts the traversal at the sender of the pattern's
  first action whose role is mapped to code (`entryRule: behaviour`). The traversal uses
  that role only if no other mapped role reaches it. Otherwise it starts at the
  role, among those no other role reaches, that reaches the most roles
  (`entryRule: code-root`), and with no such role it has no entry. 3 of the 100
  groundtruth records start at a `code-root`.

## What is in here

| Module | Does |
| ------ | ---- |
| `cli.py` | the command `secpat-interaction-order` (`run`, `dataset`); also `python -m secpat_interaction_order` |
| `detector.py` | `detect()` -> `DetectionResult`; sends the mapping, reads the order back |
| `joern_service.py` | the HTTP client — POSTs `walk.sc` + a profile to a query service |
| `joern_local.py` | the local runner — `joern-parse` / `joern --script` on this machine |
| `rust_walk.py` | the native engine runner (`JOERN_ENGINE=rust`, see below) |
| `progress.py` | the progress display (`SECPAT_PROGRESS`, see below) |
| `view.py`, `view.html` | the traversal view (`SECPAT_VIEW`, see below): saves traces and whole-CPG maps, serves and exports the page |
| `joern/walk.sc` | the traversal: locates roles, steps the CFG, returns interactions |
| `joern/python.sc`, `joern/java.sc` | `LangSpec` profiles — what differs between frontends |
| `config.py` | `JOERN_*` env vars, read at import time |
| `JOERN.md` | source -> CPG -> traversal, with a worked example |

The traversal itself runs in Joern (or, opt-in, in the native engine described below);
nothing is traversed in Python. The package has no dependencies; the optional `progress`
extra adds tqdm for the progress bar.

## Where Joern runs

**A query service** — set `JOERN_SERVICE_URL` (and `JOERN_SERVICE_TOKEN`). The service
holds the checkouts and a CPG per repo; `detect()` sends the repo's name. The service
is not one of the paper's artifacts, so to reproduce the paper, use local Joern.

**Local Joern** — leave `JOERN_SERVICE_URL` unset, and pass `detect(..., repo_path=<checkout>)`:

| Setting | Default | |
| ------- | ------- | - |
| `JOERN_BIN` / `JOERN_PARSE_BIN` | `joern` / `joern-parse` on PATH | the two Joern binaries |
| `JOERN_CPG_ROOT` | `~/.cache/secpat/joern_cpg` | the CPG store, `<repo>__<FRONTEND>.cpg` |
| `JOERN_TIMEOUT_SECONDS` | 900 | one query |
| `JOERN_BUILD_TIMEOUT_SECONDS` | 3600 | one CPG build |

A CPG already in the store is used as is — the file naming is the service's, so a
copy of a service's CPG store works unchanged. A missing one is built with
`joern-parse` (Java with `--delombok-mode types-only`, as the service builds it)
and kept. Two things to get right:

- **A prebuilt CPG must match its checkout's content, not its location.** Joern
  records file names relative to the folder it parsed, so a CPG and a copy of its
  checkout can move together anywhere. A checkout edited after the build is not
  detected — delete the CPG to have it rebuilt.
- **The graph depends on the Joern install.** Use Joern 4.0.627 and a full JDK: the
  paper's dataset ships CPGs built with them (JDK 25), and secpatrec's README
  installs both. A JRE-only install resolves fewer Java types, so its orders are not
  comparable.

## Faster engines and builds

Profiling (`profiling/PROFILING.md`) found that a local `joern --script` query
spends ~11 s starting Joern for a traversal that mostly takes well under a second. Two options, both
returning the same orders:

| Setting | Default | |
| ------- | ------- | - |
| `JOERN_ENGINE` | `joern` | `rust` runs the traversal on `secpat-walk` (repository joern-rust), which reads the same CPGs |
| `SECPAT_WALK_BIN` / `SECPAT_WALK_SERVE` | `secpat-walk` / off | the native binary; keep one `serve` process per Python process |
| `JOERN_DELOMBOK_MODE` | `types-only` | `no-delombok` builds a Lombok repo in a fifth of the time and memory |
| `JOERN_PARSE_DATAFLOW` | off | builds skip reaching definitions (`--max-num-def 0`), which the traversal never reads |
| `JOERN_PARSE_XMX` | unset | heap cap for every JVM a build starts, delombok's children included |

## Progress

A traversal can take from a fraction of a second to minutes. While `detect()` runs, it can show
which repository and draft it is on and how much work the traversal has done:

```
camunda_camunda-bpm-platform / limit_request_rate [00:16], joern traverse | CPG 1.39M nodes 8.09M edges 51.9k methods | bodies=2.12k replayed=481 methods=149 calls=8.39k cfg=39.9k interactions=0 alts=475 depth=23/48
```

| Setting | Default | |
| ------- | ------- | - |
| `SECPAT_PROGRESS` | `auto` | `auto`: a progress line when stderr is a terminal, else nothing. `on` (or `bar`): the same, and when stderr is not a terminal a log line (logger `secpat_interaction_order.progress`, INFO) at most every 10 s. `lines`: the engine's raw progress lines, passed through to stderr. `off`: nothing |

The counters come from the traversal itself, in Joern (`walk.sc`) or in the native engine, which
both write one `SECPAT_PROGRESS {json}` line to stderr at most twice a second when run with
`SECPAT_PROGRESS=lines`: the CPG's node, edge and method counts, then the bodies the traversal
entered (`bodies`; `replayed` of them from its memo), distinct methods entered, call sites
resolved, CFG nodes stepped, interactions emitted so far, the body depth (current / maximum)
and `keepMostRoles` alternatives traversed. Before the traversal starts, the line says what
runs instead (building the CPG, starting Joern). A query service cannot stream them, so against
one the line shows only how long the query has been waiting. The lines never change the result.

The line is drawn with tqdm when it is installed
(`pip install "secpat-interaction-order[progress] @ git+..."`), else by the package itself.
`detect(..., label=...)` names the draft (the default is the entry role and the number of roles).
The profiling runners (`profiling/prof_walk.py`, `profiling/warm_worker.py`) add an outer bar
over their drafts with `[i/N]`, the current repository and an ETA.

## The traversal view

`SECPAT_VIEW=nodes` makes every query on the native engine save its traversal, and a local web
page replays it. The README's [Watch a traversal](../README.md#watch-a-traversal) shows a run.

**What is saved.** Each query writes one file in `SECPAT_VIEW_DIR` (default
`~/.cache/secpat/view`), named `<time>__<repository>__<draft>__<n>.trace.jsonl`: the native
engine's event log (`secpat-walk run --trace`), then one `result` line that `detect()` appends
with the order the query returned. The engine also writes `<file>.graph.json`, the CPG subgraph
the events move over; the page does not need it. The events, one JSON object per line:

| `ev` | what happened |
| --- | --- |
| `header`, `start` | the query (repository, CPG size, roles, steps); where each role was found, the entry role |
| `enter`, `exit` | the traversal starts / finishes a method body (`depth`, the roles it runs as) |
| `skip`, `replay` | a body not traversed (`dead`: it can reach no further role; `entered`: done before) or replayed from the memo |
| `resolve` | what a call runs |
| `hit` | control reached a node a role owns |
| `emit` | an interaction recorded (`from`, `to`, the call site, `mechanism`) |
| `kmr`, `alt_begin`, `alt_end`, `alt_rollback`, `alt_keep`, `kmr_end` | a call with several possible targets: each is traversed, and only the one reaching the most roles is kept |
| `step` | one CFG node stepped (most of a large trace; the page leaves these out) |
| `end`, `result` | the counters; the order the query returned |

A trace costs little: on the largest traversal of the 379 drafts (camunda-bpm-platform, 147,555
events, 27 MB) the traversal took 2.91 s with the trace against 2.54 s without, and on the
dataset's records the difference is within the noise. The interaction order is the same with
and without it: all 100 records of the dataset give the stored order with `SECPAT_VIEW=nodes`.

**The viewer.** When a query saves a trace and nothing answers on `SECPAT_VIEW_PORT`, `detect()`
starts `secpat-interaction-order view SECPAT_VIEW_DIR --idle-exit 1800` in the background (its
output goes to `viewer.log` in that folder) and prints the address once. That viewer stops after
30 minutes with no page open and no new trace. To run one yourself:

```bash
secpat-interaction-order view                       # follows SECPAT_VIEW_DIR
secpat-interaction-order view TRACE.trace.jsonl     # one saved trace
secpat-interaction-order view TRACE.trace.jsonl --export traversal.html
```

It listens on 127.0.0.1 only (`--host` changes that) and serves only the trace files in its
folder. The page:

- **The drawing.** Each method the traversal reaches is one node, placed by the body that first
  reached it: a tree from left to right, or a circle when there are more than 70 leaves (the
  layout menu switches). Filled: a method body entered; dashed: reached but not entered; diamond: a
  library call. A role's node has a ring in the role's colour. The current call stack is yellow.
  When the traversal records an interaction, the path from the source role's body to the target
  lights up in the source role's colour and stays lit. Scroll to zoom, drag to pan, double-click to
  fit, hover a node for its full name, file and line.
- **The side panel.** The roles (where each was found, and whether the traversal has reached it),
  the interactions as recorded, and the interaction order the query returned. An interaction
  recorded inside an alternative that was rolled back is counted, and listed on request.
- **The timeline.** Play, pause, step (arrow keys), jump to the start or the end, and the speed. A
  step is one event the page draws; at 1× a whole traversal plays in about 30 seconds.
- **Export HTML.** The page and the trace in one file, with no outside links, that replays
  offline; the same as `view --export`.

**The whole CPG.** With `--full-view-cpg` (on `run`, `dataset` or `view`) or
`SECPAT_VIEW_FULL_CPG=1`, the page draws the traversal on a map of the whole CPG (the layout menu's
"whole CPG"; the address `?layout=cpg` opens on it too):

- **The map.** Every method of the repository is a dot, in a box for its file, inside boxes for
  its folders (a treemap: a box's area is its number of methods; a folder holding only one folder
  is one box, `src/main/java/org`). The library methods the repository calls sit in a strip on the
  right, by package and class. Dots not reached are grey; zoomed in far enough, each dot shows its
  method's name.
- **The traversal on it.** A method the traversal enters lights up at its place; a call site or
  a field it reaches (a library role's call, a role on a field) sits next to the method it was
  reached from; the calls the traversal followed are drawn as arcs, and the interactions light up
  as in the call tree. Hover any method, reached or not, for its name, file and line, and its calls
  as the CPG records them: blue to the methods it calls, yellow from its callers.
- **The file.** `secpat-walk methods` writes the map, once per CPG, as
  `cpg/<CPG name>__<nodes>-<edges>.json` in the traces folder: every method except the
  `<operator>` ones (file, lines, library or not) and the CPG's calls between them. A library
  method counts only when a call has it as its one target: Python's CPG links an untyped call to
  every library method of that name (2.3 million calls on ctrl-gaurav_effGen otherwise). A run
  with the flag makes the map after each query when it is missing (0.3 s on camunda-bpm-platform:
  45,501 methods, 110,079 calls, 10 MB). All 100 records of the dataset give the stored order with
  the flag, in 23 s against 16 s without (93 maps, 92 MB, the largest 12.9 MB).
- **The viewer.** It makes a missing map when a page asks for one, if the trace's CPG and
  `secpat-walk` are on this machine (one at a time), and sends maps gzipped (about 8 to 1).
  `view --full-view-cpg` opens pages on the map; with `--export`, the map goes into the file.
  The page's Export button includes it when the page shows the map. A viewer started before this
  version has no map; the run says so, and stopping it (`pkill -f "[s]ecpat_interaction_order view"`)
  lets the next run start a new one.

The map needs a `secpat-walk` with the `methods` command (joern-rust, branch `traversal-view`).

Only the native engine writes traces for now. On Joern, `SECPAT_VIEW=nodes` logs one warning and
saves nothing.

## The Python API and the script, by hand

The command in the README calls `detect()`, the package's entry point. `joern/walk.sc` is a plain
Joern script, which also runs without Python. The commands below run from a checkout of this
repository: `PYTHONPATH=src` stands in for an install (the package has no dependencies). They use
the record of the README's [Check one case](../README.md#check-one-case) and the paper's dataset:

```bash
DATASET=/path/to/dataset                          # the paper's dataset, see the README
export JOERN_CPG_ROOT=$DATASET/codebase/cpg       # <repo>__<FRONTEND>.cpg
```

### `detect()` from a shell

With `JOERN_SERVICE_URL` unset, this runs on local Joern (`joern` on `PATH`, or `JOERN_BIN`):

```bash
DATASET=$DATASET PYTHONPATH=src python3 - <<'EOF'
import os
from pathlib import Path
from secpat_interaction_order import detect

security = "src/main/java/io/github/ertansidar/security"
mappings = {
    "Enforcer":      {"entity": "doFilterInternal",  "file": f"{security}/filter/BaseJwtAuthFilter.java", "lines": "27"},
    "Verifier":      {"entity": "validateToken",     "file": f"{security}/jwt/BaseJwtService.java", "lines": "41"},
    "Key manager":   {"entity": "getSignKey",        "file": f"{security}/jwt/BaseJwtService.java", "lines": "71"},
    "Cryptographer": {"entity": "parseSignedClaims", "file": f"{security}/jwt/BaseJwtService.java", "lines": "46"},
}
behaviour = [
    {"step": "2", "from": "Enforcer", "to": "Verifier"},
    {"step": "3", "from": "Verifier", "to": "Key manager"},
    {"step": "4", "from": "Verifier", "to": "Cryptographer"},
]
result = detect(
    "ErtanSidar_turkcell-common", "java", mappings, behaviour=behaviour,
    repo_path=Path(os.environ["DATASET"], "codebase/repos/java/ErtanSidar_turkcell-common"),
)
print(result.reason or f"entry {result.entry_role} ({result.entry_rule})")
for i in result.interactions:
    print(f"  {i.token:26} {i.mechanism:5} {i.file}:{i.line}")
EOF
```

```text
entry Enforcer (behaviour)
  Enforcer → Key manager     call  src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:54
  Enforcer → Verifier        call  src/main/java/io/github/ertansidar/security/filter/BaseJwtAuthFilter.java:45
  Verifier → Key manager     call  src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:44
  Verifier → Cryptographer   call  src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:42
```

These are the four interactions of the record's ground truth. On local Joern this takes about
11 s, almost all of it Joern starting up. The arguments:

- the repository's name, which picks the CPG `$JOERN_CPG_ROOT/<repo>__JAVASRC.cpg`. A missing one
  is built with `joern-parse` first, which takes minutes (see [Where Joern runs](#where-joern-runs));
- its language, `java` or `python`;
- the mapping, `{role: {"entity", "file", "lines"}}`, with `file` relative to the checkout;
- `behaviour`, the pattern's steps (`step`, `from`, `to`; other keys are ignored). The entry role is
  read off it unless `entry=` names one;
- `repo_path`, the checkout the CPG was built from. A local engine needs it; a query service ignores it.

Failures do not raise: `result.reason` says what went wrong, and the details are logged at
WARNING (add `import logging; logging.basicConfig()` to see them).

The same script on the other engines:

| run it with | the traversal runs on | one query here |
| --- | --- | --- |
| nothing set | local Joern: one `joern --script` per call | 11-14 s |
| `JOERN_ENGINE=rust SECPAT_WALK_BIN=/path/to/joern-rust/target/release/secpat-walk` | the native engine, one `secpat-walk run` per call (add `SECPAT_WALK_SERVE=1` for one `serve` process per Python process) | ~0.1 s for the whole script |
| `JOERN_SERVICE_URL=https://… JOERN_SERVICE_TOKEN=…` | the query service, which resolves the repository by name in its own checkouts and needs its CPG prebuilt | the service's time |

All three give the same result. The engine settings are described in
[Faster engines and builds](#faster-engines-and-builds), and building `secpat-walk` in the
joern-rust README. For a live view of what a query is doing, set `SECPAT_PROGRESS` (see
[Progress](#progress)).

Any of the 379 drafts in `profiling/jobs/jobs.jsonl` (paths of the machine the profiling ran on)
replays the same way. A job's `roles` is the list `detect()` sent, so keying it by role gives the
mapping back:

```bash
PYTHONPATH=src python3 - python/authentication_pwd/fdev31_wirikiki <<'EOF'
import json, sys
from pathlib import Path
from secpat_interaction_order import detect

job = next(j for j in map(json.loads, open("profiling/jobs/jobs.jsonl")) if j["id"] == sys.argv[1])
mappings = {row["role"]: row for row in json.loads(job["roles"])}
result = detect(job["repo"], job["language"], mappings, behaviour=json.loads(job["behaviour"]),
                entry=job["entry"], repo_path=Path(job["input"]), label=job["pattern"])
print(result.reason or "\n".join(result.tokens))
EOF
```

### The Joern script alone

Without Python, `walk.sc` runs on Joern directly: `walk.sc` and the language's profile joined into
one script (as `detector._script` joins them), then `joern --script` with the query as `--param`s.
That is what `joern_local.py` runs for one query.

```bash
D=/path/to/dataset                                              # the paper's dataset
J=/path/to/secpat_interaction_recovery/src/secpat_interaction_order/joern   # a clone of this repository
# or, where the package is installed (secpatrec installs it):
# J=$(python3 -c 'import secpat_interaction_order as s; print(s.__path__[0])')/joern

cd "$(mktemp -d)"                       # Joern leaves a workspace/ folder here
cat $J/walk.sc $J/java.sc > query.sc    # python.sc for a Python repository
joern --script query.sc \
  --param cpgPath=$D/codebase/cpg/ErtanSidar_turkcell-common__JAVASRC.cpg \
  --param inputPath=$D/codebase/repos/java/ErtanSidar_turkcell-common \
  --param language=JAVASRC \
  --param 'roles=[
    {"role": "Enforcer",      "entity": "doFilterInternal",  "file": "src/main/java/io/github/ertansidar/security/filter/BaseJwtAuthFilter.java", "lines": "27"},
    {"role": "Verifier",      "entity": "validateToken",     "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "41"},
    {"role": "Key manager",   "entity": "getSignKey",        "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "71"},
    {"role": "Cryptographer", "entity": "parseSignedClaims", "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "46"}]' \
  --param 'behaviour=[
    {"step": "2", "from": "Enforcer", "to": "Verifier"},
    {"step": "3", "from": "Verifier", "to": "Key manager"},
    {"step": "4", "from": "Verifier", "to": "Cryptographer"}]' \
  --param entry=Enforcer \
  --param outputPath=$PWD/result.json
```

It takes about 11 s. Among Joern's own messages it prints:

```text
derived 4 interaction(s) from 4 role(s), entry=Enforcer
```

`result.json` holds the interactions in order, the same four as `interaction_order` in the
ground-truth file:

```bash
grep -o '"interaction":"[^"]*"' result.json
```

```text
"interaction":"Enforcer → Key manager"
"interaction":"Enforcer → Verifier"
"interaction":"Verifier → Key manager"
"interaction":"Verifier → Cryptographer"
```

The parameters are `exec`'s, at the top of `walk.sc`:

| `--param` | |
| --- | --- |
| `cpgPath` | the CPG, loaded with `importCpg` (Joern works on a copy in `workspace/`) |
| `inputPath` | the checkout the CPG was built from; file names in the result are relative to it |
| `outputPath` | where the result JSON goes |
| `language` | `JAVASRC` with `java.sc`, `PYTHONSRC` with `python.sc` |
| `roles` | the mapping as a JSON array of `{"role", "entity", "file", "lines"}` |
| `behaviour` | the pattern's steps as a JSON array |
| `entry` | the entry role (`detect()` passes the one it read off `behaviour`) |

`joern_local.py` runs it in a temporary folder, without a heap cap (`JAVA_TOOL_OPTIONS` is only
set for builds, through `JOERN_PARSE_XMX`). The script's progress lines are described under
[Progress](#progress).

## The batch runners in `profiling/`

They replay the SecPat drafts of `profiling/jobs/jobs.jsonl`, whose paths (`cpg`, `input`,
`golden`) are this machine's. Each JVM they start runs alone; run one runner at a time.
`profiling/PROFILING.md` §1 and §9 have the background and the full-corpus commands.

| script | for | reads | writes |
| --- | --- | --- | --- |
| `extract_jobs.py` | capture the exact `(roles, behaviour, entry)` the 2026-11-09 run sent for each draft; Joern is replaced by a recorder, so no JVM runs | `/workspace/claude-recovery-result/MANIFEST.csv`, through `/workspace/cpg-backfill/scripts/derive_orders.py` (run with that project's venv) | `profiling/jobs/jobs.jsonl` |
| `prof_walk.py` | profile the traversal: one instrumented JVM per repository, with phase timers, counters, a watchdog at `--budget` seconds and optionally JFR | `jobs.jsonl` (`--jobs`), filtered by `--ids`, `--repos`, `--min-bytes`, `--max-bytes` | `profiling/results/NAME/`: per draft `<id>.result.json` and `<id>.prof.json`, per JVM its stdout and `.meta.json` (and `.jfr` with `--jfr`), and `summary.csv` |
| `warm_worker.py` | the warm-JVM baseline: one `joern --script` per language that compiles the traversal once and takes queries over a pipe | `jobs.jsonl`, filtered by `--ids`, `--repos`, `--max-bytes`, `--limit` | `profiling/results/NAME/`: `<id>.result.json`, `timings.json`, `worker_<language>.log` |
| `compare_golden.py` | compare a run's outputs with the published orders, or with another run | a run name under `profiling/results/`, or any folder of `<id with / replaced by __>.result.json` files (such as `secpat-walk batch` output); `--against golden` (default) or another run | a summary on stdout; exit status 1 if any draft differs; `--list-diff FILE` |

`--run NAME` names the output folder. Both JVM runners take `--xmx` (default `48g`) and
`--joern-dir DIR` or `--walk FILE` to run another traversal than the package's. Both use
`JOERN_BIN` when it is set, else `/opt/joern/joern-cli/joern`.

```bash
/workspace/cpg-backfill/venv/bin/python -I profiling/extract_jobs.py
```

```text
379 job(s) over 306 repo(s); 123 draft(s) never reach Joern -> /…/profiling/jobs/jobs.jsonl
```

Two drafts on one repository, in one JVM, then checked against the published orders:

```bash
python3 profiling/prof_walk.py --run check --repos gvfbossa_bws-base-api --budget 120 --xmx 24g
python3 profiling/compare_golden.py check
```

```text
03:10:04 2 job(s) over 1 repo(s) -> /…/profiling/results/check
03:10:04 [1/1] gvfbossa_bws-base-api: 2 job(s), CPG 0.2 MB
03:10:17     L0: main@12.0s loaded@12.5s exit@13.0s  session_based_access_control=done, verifiable_token_based_authentication=done
03:10:17 done
check vs golden: identical 2, different 0, no output on one side 377, previously timed out (now derived) 0
```

The warm worker on two drafts:

```bash
python3 profiling/warm_worker.py --run warm-check --budget 120 --xmx 24g \
  --ids java/verifiable_token_based_authentication/wjs1995_sb-jwt-auth-starter java/verifiable_token_based_authentication/gvfbossa_bws-base-api
```

```text
03:10:33 java worker ready in 10.3s
03:10:34   done     load=  0.16s walk=   0.192s java/verifiable_token_based_authentication/wjs1995_sb-jwt-auth-starter
03:10:34   done     load=  0.14s walk=   0.067s java/verifiable_token_based_authentication/gvfbossa_bws-base-api
03:10:34 all done in 11s
```

`compare_golden.py` also takes the native engine's output folder. joern-rust's full run gives
360 identical to the published orders, 17 former timeouts now derived, 2 without output and none
different:

```bash
python3 profiling/compare_golden.py /workspace/joern-rust/parity/out/full-entered
python3 profiling/compare_golden.py /workspace/joern-rust/parity/out/full-entered --against opt-full
```

```text
/workspace/joern-rust/parity/out/full-entered vs golden: identical 360, different 0, no output on one side 2, previously timed out (now derived) 17
  NEW  java/limit_request_rate/factcast_factcast: 2 interaction(s)
  ...
/workspace/joern-rust/parity/out/full-entered vs opt-full: identical 369, different 0, no output on one side 10, previously timed out (now derived) 0
```

`--show N` caps the `DIFF` lines; the `NEW` lines (drafts that have a result where the published
run timed out) are always listed.

The runners always use Joern. To run many drafts on the native engine, use `secpat-walk batch`
on `jobs.jsonl` (see the joern-rust README) and compare its folder the same way.

## Tests

`tests/` covers the command, the progress display and the traversal view, without Joern (`pip install -e ".[dev]"`,
then `pytest`). The checks against the paper's dataset are in the [README](../README.md):
`secpat-interaction-order dataset` on one record and on all 100, and secpatrec's
`secpat interaction` for the scores.
