# secpat-interaction-order

Recovers the order in which the roles of a security pattern interact in a codebase. You give
it the place in the code of each role (the role mapping). It traverses the repository's code
property graph (CPG) with [Joern](https://joern.io) and returns the interactions in order. This
is the interaction sequence recovery of the paper *A Hybrid Approach for Automated Security
Pattern Recovery* (Section III-B2 and Appendix C), for Java and Python.

[secpatrec](https://github.com/anonymousspottedtoad/secpatrec), the paper's main artifact,
installs and runs it.

## What you need

- Python 3.11 or newer, and this package. secpatrec installs it. On its own:
  `pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git"`
  (it has no dependencies).
- Joern 4.0.627 with a full JDK. secpatrec's README installs both (its section "2. Joern 4.0.627").
- The paper's dataset, which includes a prebuilt CPG for every repository:

  ```bash
  pip install --upgrade huggingface_hub
  hf buckets sync hf://buckets/AnonymousSpottedToad/securityPatternsRecovery dataset
  ```

- Optional: `secpat-walk`, a native engine from the repository joern-rust. It gives the same
  results without starting Joern (see [Faster: the native engine](#faster-the-native-engine)).

## Check one case

One record of the dataset: verifiable token-based authentication in the Java repository
`ErtanSidar_turkcell-common`. The roles below are the four in its ground truth that point to code
(`dataset/ground_truth/java/verifiable_token_based_authentication__ErtanSidar_turkcell-common.json`),
and the steps are the pattern's steps between them (`04_pattern.json` in
`dataset/security_patterns/verifiable_token_based_authentication/`).

The traversal is the Joern script `walk.sc` followed by the language's rules (`java.sc` or
`python.sc`). Both are in `src/secpat_interaction_order/joern/`. Set the two paths, then run:

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

It takes about 11 s, most of it Joern starting up. Among Joern's own messages it prints:

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

Each entry also gives the call site (`file`, `line`), the calls in between (`path`) and the edge
label of Appendix C (`mechanism`).

### Any other record

secpatrec's `secpat interaction` takes the role mapping that a model recovered for a record
(stored in the dataset), derives its interaction order, and scores it. Pick the record with
`--repo` and `--pattern`. Run it from secpatrec's folder, with `dataset/` inside:

```bash
secpat interaction --models gpt-6.1-sol \
    --manifests dataset/manifest_java_full_20261005.json \
    --groundtruth-path dataset/ground_truth \
    --cpg-path dataset/codebase/cpg \
    --repos-path dataset/codebase/repos \
    --patterns-path dataset/security_patterns \
    --recoveries-path dataset/experiment_outputs \
    --repo ErtanSidar_turkcell-common --pattern verifiable_token_based_authentication
```

```text
[1/1] verifiable_token_based_authentication__ErtanSidar_turkcell-common: 4 interactions
...
    level1=1.000 level2=1.000 level3=1.000
```

This takes about 35 s (3 s with the native engine). Level 3 is the paper's IRC. For a Python
repository, use `manifest_python_full_20261005.json`. The derived order is in
`output/interaction/pipeline/gpt-6.1-sol/java/<pattern>__<repo>/recovery.json`.

## Reproduce the paper's results (whole dataset)

The same command without `--repo` and `--pattern`, on both manifests, covers all 100 records
(secpatrec's Experiment 2):

```bash
secpat interaction --models gpt-6.1-sol \
    --manifests dataset/manifest_java_full_20261005.json \
    --manifests dataset/manifest_python_full_20261005.json \
    --groundtruth-path dataset/ground_truth \
    --cpg-path dataset/codebase/cpg \
    --repos-path dataset/codebase/repos \
    --patterns-path dataset/security_patterns \
    --recoveries-path dataset/experiment_outputs
```

This takes about an hour on Joern (59 minutes here), or 2 minutes with the
[native engine](#faster-the-native-engine). The scores in `output/index.html` equal the ones
stored in `dataset/experiment_outputs/pipeline/gpt-6.1-sol/<language>/evaluation.json`.
`--experiments all` adds the baseline and the two ablations, and each `--models` adds a model.
[secpatrec's README](https://github.com/anonymousspottedtoad/secpatrec#what-reproduces-which-result)
maps the scores to the paper's tables and figures.

## Faster: the native engine

`secpat-walk` (repository joern-rust) runs the same traversal without Joern and writes the same
result file. Build it as its README says, then:

- **One case:** `secpat-walk run` takes the same values as the `joern --script` command above:
  `--cpg`, `--input`, `--language`, `--roles`, `--behaviour`, `--entry` and `--output`. It takes
  0.02 s.
- **Whole dataset:** secpatrec installs version 0.1.0 of this package, the version the paper
  used. The engine needs 0.2.0 or newer. In secpatrec's environment:

  ```bash
  pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git@main"
  export JOERN_ENGINE=rust SECPAT_WALK_BIN=/path/to/joern-rust/target/release/secpat-walk
  ```

  Then the `secpat interaction` commands above run all 100 records in about 2 minutes, with the
  same scores.

## Settings

Environment variables, read when the package is imported:

| Variable | Default | |
| --- | --- | --- |
| `JOERN_BIN` | `joern` on `PATH` | the Joern binary |
| `JOERN_TIMEOUT_SECONDS` | 900 | the time limit of one query |
| `JOERN_ENGINE` | `joern` | `rust` runs the traversal on `secpat-walk` |
| `SECPAT_PROGRESS` | `auto` | `on` shows a progress line while a query runs; `off` hides it |

## More

- [docs/USAGE.md](docs/USAGE.md): the Python API (`detect()`), every setting, the Joern query
  service, the progress display and the profiling scripts.
- [JOERN.md](src/secpat_interaction_order/JOERN.md): how the traversal works, on a worked
  example, including the two rules that Appendix C does not describe.
- [profiling/PROFILING.md](profiling/PROFILING.md): where a query's time goes, and how version
  0.2.0 made the traversal faster without changing its output.
- [Changelog.md](Changelog.md)

## License

MIT, see [LICENSE](LICENSE).
