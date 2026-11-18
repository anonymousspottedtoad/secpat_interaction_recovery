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

# ── engine ────────────────────────────────────────────────────────────────────

# Which engine runs the walk when JOERN_SERVICE_URL is unset: "joern" (`joern --script`, the
# default) or "rust" (`secpat-walk` from the joern-rust repository, which reads the same CPGs
# and returns the same result without Joern's ~10 s start-up per query).
JOERN_ENGINE = os.getenv("JOERN_ENGINE", "joern").strip().lower() or "joern"

# The native engine's binary, on PATH or a full path.
SECPAT_WALK_BIN = os.getenv("SECPAT_WALK_BIN", "secpat-walk")

# With the native engine: keep one `secpat-walk serve` process per Python process, so
# consecutive queries on one repository read its CPG once.
SECPAT_WALK_SERVE = os.getenv("SECPAT_WALK_SERVE", "").strip().lower() in ("1", "true", "yes")

# ── progress display ──────────────────────────────────────────────────────────

# What a query shows while it runs (`progress.py`): "auto" (a display when stderr is a terminal),
# "on" / "bar" (a display; when stderr is not a terminal, a log line at most every ~10 s), "lines"
# (the engine's own SECPAT_PROGRESS lines, passed through to stderr), or "off".
SECPAT_PROGRESS = os.getenv("SECPAT_PROGRESS", "auto").strip().lower() or "auto"

# ── traversal view ───────────────────────────────────────────────────────────

# "nodes": every query on the native engine saves its traversal's trace (the methods it enters,
# the role nodes it reaches, the interactions it records) for the traversal view (`view.py`), and
# a viewer is started on SECPAT_VIEW_PORT if none is running. "off" (the default) saves nothing.
SECPAT_VIEW = os.getenv("SECPAT_VIEW", "off").strip().lower() or "off"

# Where the traces go, one file per query.
SECPAT_VIEW_DIR = Path(os.getenv("SECPAT_VIEW_DIR", "") or Path.home() / ".cache" / "secpat" / "view")

# The viewer's port, on 127.0.0.1.
SECPAT_VIEW_PORT = int(os.getenv("SECPAT_VIEW_PORT", "8765") or "8765")

# On (1, true, yes, on): the view shows each traversal on a map of the whole CPG (every method of
# the repository, in its folders and files, and the library methods it calls), lighting up the
# methods the traversal reaches. The map is saved once per CPG, next to the traces. Implies
# SECPAT_VIEW=nodes; the same as --full-view-cpg.
SECPAT_VIEW_FULL_CPG = os.getenv("SECPAT_VIEW_FULL_CPG", "").strip().lower() in ("1", "true", "yes", "on")
if SECPAT_VIEW_FULL_CPG:
    SECPAT_VIEW = "nodes"

# ── CPG builds (local joern-parse) ────────────────────────────────────────────

# Delombok mode for JAVASRC builds. "types-only" is what the query service used; "no-delombok"
# skips it. Profiling (2026-11-14, a 713-file Lombok repo): types-only took 106 s, 10.8 GB and 38
# concurrent JVMs against 20 s, 1.8 GB and 2 JVMs without -- the cause of the 2026-11-09 freezes.
JOERN_DELOMBOK_MODE = os.getenv("JOERN_DELOMBOK_MODE", "types-only").strip() or "types-only"

# joern-parse computes reaching definitions (the data-flow overlay) by default. The walk never
# reads them, and they cost 6-42 % of a build; unset, builds pass `--max-num-def 0`, which skips
# them. Set JOERN_PARSE_DATAFLOW=1 to build the full graph.
JOERN_PARSE_DATAFLOW = os.getenv("JOERN_PARSE_DATAFLOW", "").strip().lower() in ("1", "true", "yes")

# Heap cap for every JVM a build starts, e.g. "32g". Passed through JAVA_TOOL_OPTIONS, so delombok's
# child JVMs inherit it too; without it each JVM sizes itself at 75 % of the machine's memory.
JOERN_PARSE_XMX = os.getenv("JOERN_PARSE_XMX", "").strip()
