# secpat-interaction-order

Derives the order in which a role mapping's roles interact, by walking the repo's
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
Recovery) describes the traversal rules and the edges the walk adds, and
[`JOERN.md`](src/secpat_interaction_order/JOERN.md) works through one example. The
appendix is at <https://doi.org/10.5281/zenodo.23664769>, and its Appendix G lists
the other artifacts. The same walk derived the groundtruth interaction sequences of
the paper's dataset, and IRC
([secpat_evaluation_metrics](https://github.com/anonymousspottedtoad/secpat_evaluation_metrics))
walks both sides with it. It has rules for Java and Python, the two languages of
the paper.

In the output, an interaction of the paper is one entry of `interactions` (`from`,
`to`, the call site and its `path`), the entry role is `entryRole`, and the label
of an edge in Appendix C is its `mechanism`.

Two rules of the walk are not described in Appendix C:

- **`co-located`.** When one code node carries several roles (one entity mapped to
  more than one role), the walk records the interactions the pattern prescribes
  among those roles, labelled `co-located`. 20 of the 280 groundtruth interactions
  are `co-located`.
- **The entry role.** Appendix C starts the walk at the sender of the pattern's
  first action whose role is mapped to code (`entryRule: behaviour`). The walk uses
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
| `joern/walk.sc` | the walk: locates roles, steps the CFG, returns interactions |
| `joern/python.sc`, `joern/java.sc` | `LangSpec` profiles — what differs between frontends |
| `config.py` | `JOERN_*` env vars, read at import time |
| `JOERN.md` | source -> CPG -> walk, with a worked example |

The walk itself runs in Joern; nothing is walked in Python. The package has no
dependencies.

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

## Install

```bash
pip install "secpat-interaction-order @ git+https://github.com/anonymousspottedtoad/secpat_interaction_recovery.git@v0.1.0"
```

`secpat-scoring` pins it by name, so a consumer installing scoring from git lists this
URL as well (pip will not resolve it from an index).

## Checking an install

The repository has no test suite. To check an install against known numbers, run
secpatrec's Experiment 2 on the `pipeline` experiment: it walks the paper's
recoveries again with this package, and its scores equal those stored in the
dataset's `evaluation.json` files.

## License

MIT — see [LICENSE](LICENSE).
