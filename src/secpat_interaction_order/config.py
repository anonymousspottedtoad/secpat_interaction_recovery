"""Environment configuration for running the walk on Joern.

Read once, at import time — set them before importing this package. Everything
else the detector needs is passed in as an argument.

**Two ways to run the walk.** With ``JOERN_SERVICE_URL`` set, the walk runs on a
Joern query service that holds the checkouts and a CPG per repo; this package
POSTs ``joern/walk.sc`` to it (:mod:`.joern_service`). With it unset, the walk runs
on a local Joern install instead (:mod:`.joern_local`): ``JOERN_BIN`` /
``JOERN_PARSE_BIN`` name the binaries and ``JOERN_CPG_ROOT`` holds the CPGs.
"""

import os
from pathlib import Path

# Where the query service lives, e.g. "https://joern.example.internal". Unset, the
# walk runs on the local Joern install configured below instead.
JOERN_SERVICE_URL = os.getenv("JOERN_SERVICE_URL", "").strip().rstrip("/")

# Shared secret for that service. Sent as `Authorization: Bearer <token>`.
JOERN_SERVICE_TOKEN = os.getenv("JOERN_SERVICE_TOKEN", "").strip()

# Wall-clock budget for one query, in seconds: sent to the service, which bounds
# Joern's own run there, or the ceiling on a local `joern --script` run.
JOERN_TIMEOUT_SECONDS = int(os.getenv("JOERN_TIMEOUT_SECONDS", "900"))

# Client-side ceiling for one service call. Defaults to the budget above plus two
# minutes, so the service's own timeout expires first and the caller gets Joern's
# diagnostics rather than a bare timeout.
#
# This is a *query* budget. Against the service the CPG is already built — that
# is a separate call an operator makes ahead of time — so a query loads a
# finished graph and walks it, which is seconds. Nothing here waits out a build.
JOERN_SERVICE_TIMEOUT_SECONDS = int(
    os.getenv("JOERN_SERVICE_TIMEOUT_SECONDS", "0")
) or (JOERN_TIMEOUT_SECONDS + 120)

# ── local Joern (used when JOERN_SERVICE_URL is unset) ───────────────────────

# The two Joern binaries: `joern-parse` builds a CPG from source, `joern` runs a
# script against one. Names on PATH, or full paths.
JOERN_BIN = os.getenv("JOERN_BIN", "joern")
JOERN_PARSE_BIN = os.getenv("JOERN_PARSE_BIN", "joern-parse")

# Where CPGs are kept, one `<repo>__<FRONTEND>.cpg` per (repo, frontend) — the
# service's own naming, so a copy of its store can be used as-is. A CPG already
# here is used without rebuilding; one that is missing is built and kept.
JOERN_CPG_ROOT = Path(
    os.getenv("JOERN_CPG_ROOT", "") or Path.home() / ".cache" / "secpat" / "joern_cpg"
).expanduser()

# Wall-clock ceiling for one local CPG build, in seconds. A build is the slow,
# memory-bound step — minutes on a large repo.
JOERN_BUILD_TIMEOUT_SECONDS = int(os.getenv("JOERN_BUILD_TIMEOUT_SECONDS", "3600"))
