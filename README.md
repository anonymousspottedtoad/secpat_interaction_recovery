# secpat-interaction-order

Derives the order in which a role mapping's roles interact, by walking the repo's
Code Property Graph with Joern — a Joern query service, or a local Joern install.
Installed by:

- **`secpat-scoring`** — Level 3 compares two orders derived here (it imports the
  result types; the caller binds `detect` to a repo and passes it in).
- **`secpat-recovery`** — stage 3 of a recovery run writes the order into
  `recovery.json`.

## What is in here

| Module | Does |
| ------ | ---- |
| `detector.py` | `detect()` -> `DetectionResult`; sends the mapping, reads the order back |
| `joern_service.py` | the HTTP client — POSTs `walk.sc` + a profile to a query service |
| `joern_local.py` | the local runner — `joern-parse` / `joern --script` on this machine |
| `joern/walk.sc` | the walk: locates roles, steps the CFG, returns interactions |
| `joern/python.sc`, `joern/java.sc` | `LangSpec` profiles — what differs between frontends |
| `config.py` | `JOERN_*` env vars, read at import time |
| `JOERN.md` | source -> CPG -> walk, with a worked example |

The walk itself runs in Joern; nothing is walked in Python. The package has no
dependencies.

## Where Joern runs

**A query service** — set `JOERN_SERVICE_URL` (and `JOERN_SERVICE_TOKEN`). The service
holds the checkouts and a CPG per repo; `detect()` sends the repo's name.

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
- **The graph depends on the Joern install.** Use the Joern release and a full JDK
  matching the graphs you compare against; a JRE-only install resolves fewer Java
  types, so its orders are not comparable.

## Install

```bash
pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git@v0.1.0"
```

`secpat-scoring` pins it by name, so a consumer installing scoring from git lists this
URL as well (pip will not resolve it from an index).

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The service is never started: `joern_service.query` is the seam the tests replace.
