"""Run the walk on the native engine (`secpat-walk`, repository joern-rust) instead of Joern.

Used when ``JOERN_ENGINE=rust``. The engine reads the same CPG files ``joern-parse`` writes
(the CPG store of :mod:`.joern_local`; a missing one is still built with ``joern-parse``)
and runs a port of ``joern/walk.sc`` and its language profiles that returns the same JSON:
the 379 SecPat drafts give identical interactions through either engine. What it removes is
Joern's per-query start-up -- two JVMs and compiling the walk, about 10 s -- so a query costs
the walk itself plus reading the CPG columns it touches.

Two modes:

* one-shot (default): ``secpat-walk run ...`` per query;
* ``SECPAT_WALK_SERVE=1``: one ``secpat-walk serve`` process per Python process, fed one JSON
  line per query; it keeps the last CPG open, so consecutive queries on one repository (both
  sides of a Level 3 comparison, say) read it once.

Like the Joern backends, every failure returns ``None`` and is logged at WARNING.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config, joern_local
from .joern_service import frontend_for

logger = logging.getLogger(__name__)

_DETAIL_TAIL = 2000


def query(repo: str, language: str, repo_path: Path, params: dict[str, str]) -> dict | None:
    """The walk's JSON result for ``repo`` with ``params`` (roles, behaviour, entry), or ``None``."""
    repo_path = Path(repo_path).absolute()
    if not repo_path.is_dir():
        logger.warning("secpat-walk: no checkout for %s at %s", repo, repo_path)
        return None
    frontend = frontend_for(language)
    cpg = joern_local._ensure_cpg(repo, frontend, repo_path)
    if cpg is None:
        return None
    workdir = Path(tempfile.mkdtemp(prefix="secpat-walk-"))
    try:
        output = workdir / "result.json"
        started = time.monotonic()
        if config.SECPAT_WALK_SERVE:
            ok = _serve_query(repo, cpg, repo_path, frontend, params, output)
        else:
            ok = _run_query(repo, cpg, repo_path, frontend, params, output)
        if not ok:
            return None
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("secpat-walk: the query for %s wrote an unreadable result: %s", repo, e)
            return None
        logger.info("secpat-walk: %s answered in %.2fs", repo, time.monotonic() - started)
        return result if isinstance(result, dict) else None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_query(repo, cpg, repo_path, frontend, params, output) -> bool:
    command = [
        config.SECPAT_WALK_BIN, "run",
        "--cpg", str(cpg), "--input", str(repo_path), "--language", frontend,
        "--roles", params.get("roles", ""), "--behaviour", params.get("behaviour", ""),
        "--entry", params.get("entry", ""), "--output", str(output),
        "--timeout", str(config.JOERN_TIMEOUT_SECONDS),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                   timeout=config.JOERN_TIMEOUT_SECONDS + 60)
    except FileNotFoundError:
        logger.warning("secpat-walk: %s is not installed or not on PATH -- build joern-rust "
                       "(`cargo build --release -p secpat-walk`) or set SECPAT_WALK_BIN.",
                       config.SECPAT_WALK_BIN)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("secpat-walk: the query for %s ran past JOERN_TIMEOUT_SECONDS=%ds.",
                       repo, config.JOERN_TIMEOUT_SECONDS)
        return False
    if completed.returncode != 0 or not output.is_file():
        logger.warning("secpat-walk: the query for %s exited %s. stdout: %s\nstderr: %s", repo,
                       completed.returncode, completed.stdout[-_DETAIL_TAIL:], completed.stderr[-_DETAIL_TAIL:])
        return False
    return True


class _Server:
    """One `secpat-walk serve` process, answering one query at a time."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [config.SECPAT_WALK_BIN, "serve", "--timeout", str(config.JOERN_TIMEOUT_SECONDS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self.lock = threading.Lock()
        self.count = 0
        # One thread owns the pipe: select() on a buffered text stream misses lines that
        # readline() already pulled into the buffer.
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def ask(self, job: dict, timeout: float) -> tuple[bool, str]:
        with self.lock:
            self.count += 1
            job = dict(job, id=f"q{self.count}")
            self.proc.stdin.write(json.dumps(job) + "\n")
            self.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    line = self.lines.get(timeout=left)
                except queue.Empty:
                    break
                if line is None:
                    return False, "secpat-walk serve exited"
                parts = line.split(" ", 2)
                if len(parts) >= 2 and parts[1] == job["id"]:
                    return parts[0] == "DONE", line.strip()
            return False, "no answer in time"

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 -- best effort at exit
            self.proc.kill()


_server: _Server | None = None


def _serve_query(repo, cpg, repo_path, frontend, params, output) -> bool:
    global _server
    try:
        if _server is None or _server.proc.poll() is not None:
            _server = _Server()
            atexit.register(_server.close)
    except FileNotFoundError:
        logger.warning("secpat-walk: %s is not installed or not on PATH.", config.SECPAT_WALK_BIN)
        return False
    ok, detail = _server.ask({
        "cpgPath": str(cpg), "inputPath": str(repo_path), "language": frontend,
        "roles": params.get("roles", ""), "behaviour": params.get("behaviour", ""),
        "entry": params.get("entry", ""), "outputPath": str(output),
    }, config.JOERN_TIMEOUT_SECONDS + 60)
    if not ok:
        logger.warning("secpat-walk serve: the query for %s failed: %s", repo, detail)
        if _server.proc.poll() is None and "in time" in detail:
            _server.proc.kill()
        return False
    return output.is_file()


def available() -> bool:
    """Whether the engine binary can be found."""
    return shutil.which(config.SECPAT_WALK_BIN) is not None or os.path.isfile(config.SECPAT_WALK_BIN)
