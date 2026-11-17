# secpat-interaction-order

Recovers the order in which the roles of a security pattern interact in a codebase. You give
it the place in the code of each role (the role mapping). It traverses the repository's code
property graph (CPG) with [Joern](https://joern.io) and returns the interactions in order. This
is the interaction sequence recovery of the paper *A Hybrid Approach for Automated Security
Pattern Recovery* (Section III-B2 and Appendix C), for Java and Python.

It runs on its own, as the command `secpat-interaction-order`:

- `secpat-interaction-order dataset` re-derives the interaction orders of the paper's dataset
  and checks them against the stored ones (one record, or all 100);
- `secpat-interaction-order run` derives the order of any role mapping in any Java or Python
  repository.

[secpatrec](https://github.com/anonymousspottedtoad/secpatrec), the paper's main artifact, uses
it as a library.

## What you need

- Python 3.11 or newer, and this package (it has no dependencies):

  ```bash
  pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git"
  ```

  secpatrec installs an older version, 0.1.0, which has no command. Installing this one over it
  is safe.
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
`ErtanSidar_turkcell-common`.

```bash
D=/path/to/dataset
secpat-interaction-order dataset --dataset $D \
    --repo ErtanSidar_turkcell-common --pattern verifiable_token_based_authentication
```

```text
[1/1] verifiable_token_based_authentication__ErtanSidar_turkcell-common: 4 interaction(s), same as the dataset
1 of 1 record(s) give the same interaction order as the dataset (13 s).
```

It reads the record's ground-truth role mapping
(`ground_truth/java/verifiable_token_based_authentication__ErtanSidar_turkcell-common.json`) and
the pattern's steps (`security_patterns/verifiable_token_based_authentication/04_pattern.json`).
Then it traverses the record's CPG on Joern and compares the result with the `interaction_order`
stored in the ground-truth file. It takes about 13 s, most of it Joern starting up. Any other
record works the same way: its repository and pattern are listed in `manifest_java_*.json` and
`manifest_python_*.json`.

### Your own role mapping

`run` takes the role mapping on the command line. The same record, with its four roles written
out:

```bash
secpat-interaction-order run \
  --codebase $D/codebase/repos/java/ErtanSidar_turkcell-common \
  --cpg-dir $D/codebase/cpg \
  --language java \
  --roles '[
    {"role": "Enforcer",      "entity": "doFilterInternal",  "file": "src/main/java/io/github/ertansidar/security/filter/BaseJwtAuthFilter.java", "lines": "27"},
    {"role": "Verifier",      "entity": "validateToken",     "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "41"},
    {"role": "Key manager",   "entity": "getSignKey",        "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "71"},
    {"role": "Cryptographer", "entity": "parseSignedClaims", "file": "src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java",    "lines": "46"}]' \
  --behaviour '[
    {"step": "2", "from": "Enforcer", "to": "Verifier"},
    {"step": "3", "from": "Verifier", "to": "Key manager"},
    {"step": "4", "from": "Verifier", "to": "Cryptographer"}]' \
  --output result.json
```

```text
derived 4 interaction(s) from 4 role(s), entry=Enforcer (12.6 s)
  1. Enforcer → Key manager    src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:54  call
  2. Enforcer → Verifier       src/main/java/io/github/ertansidar/security/filter/BaseJwtAuthFilter.java:45  call
  3. Verifier → Key manager    src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:44  call
  4. Verifier → Cryptographer  src/main/java/io/github/ertansidar/security/jwt/BaseJwtService.java:42  call
```

Each line is one interaction: the two roles, the call site, and how the first role reaches the
second (the edge label of Appendix C). `result.json` has the same, plus the calls in between.

| option | |
| --- | --- |
| `--codebase` | the repository's folder |
| `--language` | `java` or `python` |
| `--roles` | one entry per role: its name, the function or class (`entity`), the `file` (relative to `--codebase`) and the line. `@FILE` reads the list from a file |
| `--behaviour` | optional: the pattern's steps, which choose the role the traversal starts from ([the rule](docs/USAGE.md#the-output-and-the-paper)) |
| `--entry` | optional: the role to start from, instead |
| `--cpg-dir` | the folder with the repository's CPG, `<folder name>__JAVASRC.cpg` (or `__PYTHONSRC`). Without it, or when the file is not there, Joern's `joern-parse` builds the CPG first, in `~/.cache/secpat/joern_cpg` (seconds for a small repository, minutes for a large one) |
| `--output` | write the result as JSON |
| `--engine` | `joern` (the default) or `rust` |

## Reproduce the whole dataset

The same `dataset` command without `--repo` and `--pattern` checks all 100 records:

```bash
secpat-interaction-order dataset --dataset $D
```

```text
[1/100] authentication_pwd__Open-Core-Finance_corefinance: 4 interaction(s), same as the dataset
...
[100/100] session_based_access_control__kinde-oss_kinde-python-sdk: 3 interaction(s), same as the dataset
100 of 100 record(s) give the same interaction order as the dataset (23 min).
```

It takes about 23 minutes on Joern, or 18 s with `--engine rust`. A record that
differs is printed with both orders, and the command then exits with status 1. `--output DIR`
saves every derived order.

These ground-truth orders are the reference that the paper's Level 3 (IRC) scores the recovered
orders against. To compute the scores themselves, use secpatrec's `secpat interaction` (its
Experiment 2), which runs this package and secpatrec's scoring package. Run it from secpatrec's
folder, with `dataset/` inside:

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

This takes about an hour on Joern, or 2 minutes with the native engine. The scores in
`output/index.html` equal the ones stored in
`dataset/experiment_outputs/pipeline/gpt-6.1-sol/<language>/evaluation.json`.
[secpatrec's README](https://github.com/anonymousspottedtoad/secpatrec#what-reproduces-which-result)
maps them to the paper's tables and figures.

## Faster: the native engine

`secpat-walk` (repository joern-rust) runs the same traversal without Joern and gives the same
results. Build it as its README says and put it on `PATH` (or set `SECPAT_WALK_BIN` to it). Then
add `--engine rust` to any command above:

```bash
secpat-interaction-order dataset --dataset $D --engine rust
```

All 100 records take 18 s instead of about 23 minutes. For secpatrec's
`secpat interaction`, install this package in secpatrec's environment (the `pip install` line
above) and set `JOERN_ENGINE=rust`.

## Settings

Environment variables:

| Variable | Default | |
| --- | --- | --- |
| `JOERN_BIN` | `joern` on `PATH` | the Joern binary |
| `JOERN_TIMEOUT_SECONDS` | 900 | the time limit of one query |
| `JOERN_ENGINE` | `joern` | `rust` runs the traversal on `secpat-walk` (the same as `--engine rust`) |
| `SECPAT_WALK_BIN` | `secpat-walk` on `PATH` | the native engine's binary |
| `SECPAT_PROGRESS` | `auto` | `on` shows a progress line while a query runs; `off` hides it |

## More

- [docs/USAGE.md](docs/USAGE.md): the Python API (`detect()`), the Joern script on its own,
  every setting, the progress display and the profiling scripts.
- [JOERN.md](src/secpat_interaction_order/JOERN.md): how the traversal works, on a worked
  example, including the two rules that Appendix C does not describe.
- [profiling/PROFILING.md](profiling/PROFILING.md): where a query's time goes, and how version
  0.2.0 made the traversal faster without changing its output.
- [Changelog.md](Changelog.md)

## License

MIT, see [LICENSE](LICENSE).
