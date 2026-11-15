# secpat-interaction-order

Derives the order in which a role mapping's roles interact, by traversing the repo's
Code Property Graph with Joern — a Joern query service, or a local Joern install.
Installed by:

- **`secpat-scoring`** — Level 3 compares two orders derived here (it imports the
  result types; the caller binds `detect` to a repo and passes it in).
- **`secpat-recovery`** — stage 3 of a recovery run writes the order into
  `recovery.json`.

## The paper

This package is the interaction sequence recovery of the paper *A Hybrid Approach
for Automated Security Pattern Recovery* (Section III-B2, Interaction Sequence
Recovery). The paper's Appendix C (Using Code Property Graphs for Interaction
Recovery) describes the traversal rules and the edges the traversal adds, and
[`JOERN.md`](src/secpat_interaction_order/JOERN.md) works through one example. The
appendix is at <https://doi.org/10.5281/zenodo.23664769>, and its Appendix G lists
the other artifacts. The traversal derived the groundtruth interaction sequences of the
paper's dataset (in version 0.1.0; 0.2.0 removes repeated work without changing its
rules or, on every order it was compared with, its output: see
[Changelog.md](Changelog.md)), and IRC
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
| `detector.py` | `detect()` -> `DetectionResult`; sends the mapping, reads the order back |
| `joern_service.py` | the HTTP client — POSTs `walk.sc` + a profile to a query service |
| `joern_local.py` | the local runner — `joern-parse` / `joern --script` on this machine |
| `rust_walk.py` | the native engine runner (`JOERN_ENGINE=rust`, see below) |
| `progress.py` | the progress display (`SECPAT_PROGRESS`, see below) |
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

## Install

```bash
pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git@v0.1.0"
```

`secpat-scoring` pins it by name, so a consumer installing scoring from git lists this
URL as well (pip will not resolve it from an index).

## Running from the command line

The package has no command of its own. `detect()` is the entry point, and `joern/walk.sc` is a
plain Joern script. The commands below run from a checkout of this repository: `PYTHONPATH=src`
stands in for an install (the package has no dependencies). They use this machine's corpus, a
CPG store and the checkouts it was built from. Point the variables at your own copies:

```bash
export JOERN_CPG_ROOT=/workspace/mining-sec-arcs-tactics/build/volumes/joern_cpg   # <repo>__<FRONTEND>.cpg
JAVA_REPOS=/workspace/mining-sec-arcs-tactics/build/volumes/data_java/cloned_repos
```

### `detect()` from a shell

One draft, on a 160 kB Java CPG. With `JOERN_SERVICE_URL` unset, this runs on local Joern (`joern`
on `PATH`, or `JOERN_BIN`):

```bash
PYTHONPATH=src python3 - <<'EOF'
from pathlib import Path
from secpat_interaction_order import detect

java = "src/main/java/cloud/xiaoweiyun/auth"
mappings = {
    "Enforcer":      {"entity": "doFilterInternal", "file": f"{java}/filters/JwtAuthenticationFilter.java", "lines": "34"},
    "Verifier":      {"entity": "validateToken",    "file": f"{java}/JwtUtil.java", "lines": "63"},
    "Key manager":   {"entity": "secret",           "file": f"{java}/JwtUtil.java", "lines": "15"},
    "Cryptographer": {"entity": "parseClaimsJws",   "file": f"{java}/JwtUtil.java", "lines": "73"},
}
behaviour = [
    {"step": "2", "from": "Enforcer", "to": "Verifier"},
    {"step": "3", "from": "Verifier", "to": "Key manager"},
    {"step": "4", "from": "Verifier", "to": "Cryptographer"},
]
result = detect(
    "wjs1995_sb-jwt-auth-starter", "java", mappings, behaviour=behaviour,
    repo_path=Path("/workspace/mining-sec-arcs-tactics/build/volumes/data_java/cloned_repos/wjs1995_sb-jwt-auth-starter"),
)
print(result.reason or f"entry {result.entry_role} ({result.entry_rule})")
for i in result.interactions:
    print(f"  {i.token:32} {i.mechanism:5} {i.file}:{i.line}")
EOF
```

```text
entry Enforcer (behaviour)
  Enforcer → Key manager           data  src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java:73
  Enforcer → Cryptographer         call  src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java:73
  Enforcer → Verifier              call  src/main/java/cloud/xiaoweiyun/auth/filters/JwtAuthenticationFilter.java:69
  Verifier → Key manager           data  src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java:73
  Verifier → Cryptographer         call  src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java:73
```

This takes 11-14 s here, almost all of it Joern starting up. The arguments:

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
| `JOERN_ENGINE=rust SECPAT_WALK_BIN=/workspace/joern-rust/target/release/secpat-walk` | the native engine, one `secpat-walk run` per call (add `SECPAT_WALK_SERVE=1` for one `serve` process per Python process) | ~0.1 s for the whole script |
| `JOERN_SERVICE_URL=https://… JOERN_SERVICE_TOKEN=…` | the query service, which resolves the repository by name in its own checkouts and needs its CPG prebuilt | the service's time |

All three give the same result. The engine settings are described in
[Faster engines and builds](#faster-engines-and-builds), and building `secpat-walk` in the
joern-rust README. For a live view of what a query is doing, set `SECPAT_PROGRESS` (see
[Progress](#progress)).

Any of the 379 drafts in `profiling/jobs/jobs.jsonl` replays the same way. A job's `roles` is the
list `detect()` sent, so keying it by role gives the mapping back:

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

### The Scala traversal, directly

What `joern_local.py` runs for one query: `walk.sc` and the language's profile joined into one
script (as `detector._script` joins them), then `joern --script` with the query as `--param`s.
Run it in an empty folder, because Joern leaves its project folder (`workspace/`) in the current
directory:

```bash
J=$PWD/src/secpat_interaction_order/joern
cd "$(mktemp -d)"
{ cat $J/walk.sc; echo; cat $J/java.sc; } > query.sc
JAVA_TOOL_OPTIONS=-Xmx24g joern --script query.sc \
  --param cpgPath=$JOERN_CPG_ROOT/wjs1995_sb-jwt-auth-starter__JAVASRC.cpg \
  --param inputPath=$JAVA_REPOS/wjs1995_sb-jwt-auth-starter \
  --param outputPath=$PWD/result.json \
  --param language=JAVASRC \
  --param 'roles=[{"role": "Enforcer", "entity": "doFilterInternal", "file": "src/main/java/cloud/xiaoweiyun/auth/filters/JwtAuthenticationFilter.java", "lines": "34"}, {"role": "Verifier", "entity": "validateToken", "file": "src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java", "lines": "63"}, {"role": "Key manager", "entity": "secret", "file": "src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java", "lines": "15"}, {"role": "Cryptographer", "entity": "parseClaimsJws", "file": "src/main/java/cloud/xiaoweiyun/auth/JwtUtil.java", "lines": "73"}]' \
  --param 'behaviour=[{"step": "2", "from": "Enforcer", "to": "Verifier"}, {"step": "3", "from": "Verifier", "to": "Key manager"}, {"step": "4", "from": "Verifier", "to": "Cryptographer"}]' \
  --param entry=Enforcer
```

Among Joern's own lines, the script prints
`derived 5 interaction(s) from 4 role(s), entry=Enforcer`, and `result.json` holds the result
that `detect()` reads (11-17 s here). The parameters are `exec`'s, at the top of `walk.sc`:

| `--param` | |
| --- | --- |
| `cpgPath` | the CPG, loaded with `importCpg` (Joern works on a copy in `workspace/`) |
| `inputPath` | the checkout the CPG was built from; file names in the result are relative to it |
| `outputPath` | where the result JSON goes |
| `language` | `JAVASRC` with `java.sc`, `PYTHONSRC` with `python.sc` |
| `roles` | the mapping as a JSON array of `{"role", "entity", "file", "lines"}` |
| `behaviour` | the pattern's steps as a JSON array |
| `entry` | the entry role (`detect()` passes the one it read off `behaviour`) |

`joern_local.py` builds exactly this command. It runs in a temporary folder, without the heap cap
(`JAVA_TOOL_OPTIONS` is only set for builds, through `JOERN_PARSE_XMX`). The script's progress
lines are described under [Progress](#progress).

### The batch runners in `profiling/`

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

## Checking an install

`tests/` covers the progress display only (`pip install -e ".[dev]"`, then `pytest`). To check
an install against known numbers, run secpatrec's Experiment 2 on the `pipeline` experiment:
it traverses the paper's recoveries again with this package, and its scores equal those stored
in the dataset's `evaluation.json` files.

## Changelog

See [Changelog.md](Changelog.md).

## License

MIT — see [LICENSE](LICENSE).
