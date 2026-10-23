"""Run the walk on a local Joern install — used when ``JOERN_SERVICE_URL`` is unset.

It does here what the query service does on its host, in the same two steps:

    joern-parse <checkout> -o <JOERN_CPG_ROOT>/<repo>__<FRONTEND>.cpg     once per repo
    joern --script <walk + profile> --param cpgPath=… --param inputPath=…  every query

**A CPG already in the store is used as is.** That is what makes a prebuilt graph
— the service's ``cpg_store/`` copied over, say — usable without rebuilding: the
store uses the service's own file naming. A missing one is built, with the
service's flags (``--delombok-mode types-only`` for JAVASRC, so a Lombok repo's
lines are the repository's), and kept.

**A prebuilt CPG travels with its checkout.** ``joern-parse`` records file names
relative to the folder it parsed (only ``metaData.root`` keeps that folder, and the
walk does not read it), so a CPG built in one place works against a copy of the same
checkout anywhere — ``inputPath`` only has to hold the same source, which the walk
reads lines from. What it cannot catch is a checkout whose *content* changed after
the build: nothing here checks, as the service does not, because telling a stale
graph apart costs a walk of the whole repo on every query. Delete the CPG to rebuild.

**The graph depends on the Joern install.** The same release on a full JDK builds
the same graph the service does; a JRE-only install resolves fewer Java types and
links fewer calls, so its orders are not comparable with the service's.

Like :mod:`.joern_service`, every failure returns ``None`` and is logged at
WARNING with Joern's own output; the caller turns it into an unavailable order.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config
from .joern_service import frontend_for

logger = logging.getLogger(__name__)

#: Frontend -> arguments joern-parse passes to it verbatim — the service's own.
#: JAVASRC's default delombok mode analyses the *delomboked* source, whose
#: pretty-printer drops blank lines and comments, so every line in the graph would
#: drift from the checkout; ``types-only`` resolves Lombok's types but analyses the
#: original files.
_FRONTEND_ARGS = {"JAVASRC": ["--delombok-mode", "types-only"]}

#: How much of Joern's stdout/stderr is logged on a failure.
_DETAIL_TAIL = 2000


def cpg_path(repo: str, frontend: str) -> Path:
    """Where the CPG for ``repo`` under ``frontend`` lives in the store."""
    return config.JOERN_CPG_ROOT / f"{repo}__{frontend or 'auto'}.cpg"


def query(
    repo: str,
    language: str,
    script: str,
    repo_path: Path,
    params: dict[str, str] | None = None,
) -> dict | None:
    """Run ``script`` against ``repo``'s CPG with local Joern; its JSON result, or ``None``.

    ``params`` are passed to the script's ``@main`` as ``--param k=v``, beside the
    ``cpgPath`` / ``inputPath`` / ``outputPath`` / ``language`` this supplies itself.
    The CPG is built first if the store has none.
    """
    repo_path = Path(repo_path).absolute()
    if not repo_path.is_dir():
        logger.warning("joern: no checkout for %s at %s", repo, repo_path)
        return None
    frontend = frontend_for(language)
    cpg = _ensure_cpg(repo, frontend, repo_path)
    if cpg is None:
        return None

    workdir = Path(tempfile.mkdtemp(prefix="secpat-joern-"))
    try:
        script_path = workdir / "query.sc"
        script_path.write_text(script, encoding="utf-8")
        output = workdir / "result.json"
        command = [
            config.JOERN_BIN, "--script", str(script_path),
            "--param", f"cpgPath={cpg}",
            "--param", f"inputPath={repo_path}",
            "--param", f"outputPath={output}",
        ]
        if frontend:
            command += ["--param", f"language={frontend}"]
        for key, value in (params or {}).items():
            command += ["--param", f"{key}={value}"]

        logger.info("joern: querying %s (%s) with local Joern, CPG %s", repo, frontend or "auto", cpg)
        started = time.monotonic()
        completed = _run(command, workdir, config.JOERN_TIMEOUT_SECONDS, repo, "query")
        if completed is None:
            return None
        if completed.returncode != 0 or not output.is_file():
            logger.warning(
                "joern: the query for %s %s. stdout: %s\nstderr: %s",
                repo,
                f"exited {completed.returncode}" if completed.returncode else "wrote no result",
                completed.stdout[-_DETAIL_TAIL:], completed.stderr[-_DETAIL_TAIL:],
            )
            return None
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("joern: the query for %s wrote an unreadable result: %s", repo, e)
            return None
        logger.info("joern: %s answered in %.1fs", repo, time.monotonic() - started)
        return result if isinstance(result, dict) else None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _ensure_cpg(repo: str, frontend: str, repo_path: Path) -> Path | None:
    """The stored CPG for ``repo``, building it first if there is none.

    A lock file per CPG keeps two processes of one parallel batch from building the
    same graph at once; the second waits and then finds it built. The build writes
    to a temporary name and renames on success, so a killed build never leaves a
    half-written file that a later query would load as a finished graph.
    """
    final = cpg_path(repo, frontend)
    if final.is_file():
        return final
    config.JOERN_CPG_ROOT.mkdir(parents=True, exist_ok=True)
    with open(final.with_suffix(".cpg.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if final.is_file():
            return final
        partial = final.with_suffix(".cpg.partial")
        partial.unlink(missing_ok=True)
        command = [config.JOERN_PARSE_BIN, str(repo_path), "-o", str(partial)]
        if frontend:
            command += ["--language", frontend]
        if frontend in _FRONTEND_ARGS:
            command += ["--frontend-args", *_FRONTEND_ARGS[frontend]]

        logger.info("joern: building the CPG for %s — %s", repo, " ".join(command))
        started = time.monotonic()
        workdir = Path(tempfile.mkdtemp(prefix="secpat-joern-parse-"))
        try:
            completed = _run(command, workdir, config.JOERN_BUILD_TIMEOUT_SECONDS, repo, "build")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if completed is None or completed.returncode != 0 or not partial.is_file():
            partial.unlink(missing_ok=True)
            if completed is not None:
                logger.warning(
                    "joern: joern-parse exited %s for %s. stdout: %s\nstderr: %s",
                    completed.returncode, repo,
                    completed.stdout[-_DETAIL_TAIL:], completed.stderr[-_DETAIL_TAIL:],
                )
            return None
        partial.replace(final)
        logger.info(
            "joern: built %s in %.0fs (%.1f MB)",
            final.name, time.monotonic() - started, final.stat().st_size / 1048576,
        )
        return final


def _run(
    command: list[str], cwd: Path, timeout: int, repo: str, what: str
) -> subprocess.CompletedProcess | None:
    """Run one Joern binary; ``None`` (logged) when it is missing or times out."""
    try:
        return subprocess.run(
            command, cwd=str(cwd), env=dict(os.environ),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        logger.warning(
            "joern: %s is not installed or not on PATH, and JOERN_SERVICE_URL is unset — "
            "install Joern, set JOERN_BIN / JOERN_PARSE_BIN, or point JOERN_SERVICE_URL "
            "at a query service.",
            command[0],
        )
        return None
    except subprocess.TimeoutExpired:
        limit = "JOERN_BUILD_TIMEOUT_SECONDS" if what == "build" else "JOERN_TIMEOUT_SECONDS"
        logger.warning("joern: the %s for %s ran past %s=%ds.", what, repo, limit, timeout)
        return None
