"""What a query shows while it runs: which repository and draft, and how much the traversal does.

The traversal runs in another process: ``joern --script``, or the native engine ``secpat-walk``.
With ``SECPAT_PROGRESS=lines`` in its environment, either engine writes progress lines to stderr,
at most two a second, and a last one with ``"done": true``. A key the engine cannot provide is
left out::

    SECPAT_PROGRESS {"engine":"joern","repo":"factcast_factcast","draft":null,"phase":"traverse",
      "elapsed_s":0.68,"cpg":{"nodes":139502,"edges":735095,"methods":6088},"bodies":2,
      "replayed":0,"distinct_methods":2,"calls":40,"cfg_steps":167,"interactions":2,"depth":1,
      "max_depth":1,"alternatives":0,"done":false}

:func:`parse_line` reads one line. A :class:`Display` shows one draft's lines, and a :class:`Batch`
is the outer bar of a runner that loops over many drafts. ``SECPAT_PROGRESS`` (:mod:`.config`)
says how:

* ``auto`` (the default): a display when stderr is a terminal, otherwise nothing;
* ``on`` or ``bar``: a display; when stderr is not a terminal, a log line at most every 10 s;
* ``lines``: the engine's own lines, passed through to stderr as they arrive;
* ``off``: nothing; every engine runs exactly as it does without this module.

The display is a tqdm bar when tqdm is installed (the ``progress`` extra), otherwise a line of
this module's own, redrawn in place. Nothing else here needs more than the standard library.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from . import config

logger = logging.getLogger(__name__)

#: How a progress line starts.
PREFIX = "SECPAT_PROGRESS "

#: The counters a line may carry, in display order, with the name each is shown under.
COUNTERS = (
    ("bodies", "bodies"),
    ("replayed", "replayed"),
    ("distinct_methods", "methods"),
    ("calls", "calls"),
    ("cfg_steps", "cfg"),
    ("interactions", "interactions"),
    ("alternatives", "alts"),
)

#: Seconds between two log lines when stderr is not a terminal, and between two redraws on one.
LOG_EVERY = 10.0
REDRAW_EVERY = 0.5


def parse_line(line: str) -> dict | None:
    """The object a progress line carries; ``None`` for any other line."""
    if not line.startswith(PREFIX):
        return None
    try:
        info = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def mode(setting: str | None = None, stream=None) -> str:
    """How to show progress: ``off``, ``bar`` (a live line), ``log`` or ``lines``.

    ``setting`` defaults to ``SECPAT_PROGRESS``; whether ``stream`` (stderr) is a terminal
    decides ``auto``, and between ``bar`` and ``log`` for ``on``. An unknown value is ``auto``.
    """
    s = (config.SECPAT_PROGRESS if setting is None else setting).strip().lower() or "auto"
    if s == "lines":
        return "lines"
    if s in ("off", "0", "false", "no", "none"):
        return "off"
    tty = _isatty(stream if stream is not None else sys.stderr)
    if s in ("on", "bar", "1", "true", "yes"):
        return "bar" if tty else "log"
    return "bar" if tty else "off"


def human(n) -> str:
    """A count in at most three digits: 1234 -> ``1.23k``, 1386997 -> ``1.39M``."""
    try:
        x = float(n)
    except (TypeError, ValueError):
        return str(n)
    units = ("", "k", "M", "G", "T")
    i = 0
    while abs(x) >= 999.5 and i < len(units) - 1:
        x /= 1000
        i += 1
    return f"{x:.3g}{units[i]}" if i else f"{x:.0f}"


def describe(info: dict) -> str:
    """A progress object's graph size and counters, on one line."""
    parts = []
    cpg = info.get("cpg")
    if isinstance(cpg, dict) and cpg:
        parts.append("CPG " + " ".join(f"{human(cpg[k])} {k}" for k in ("nodes", "edges", "methods") if k in cpg))
    counts = [f"{short}={human(info[k])}" for k, short in COUNTERS if k in info]
    if "depth" in info or "max_depth" in info:
        counts.append(f"depth={info.get('depth', '?')}/{info.get('max_depth', '?')}")
    if counts:
        parts.append(" ".join(counts))
    return " | ".join(parts)


def clock(seconds: float) -> str:
    """``mm:ss``, or ``h:mm:ss`` past an hour."""
    s = int(max(0, seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class Display:
    """One draft's progress: what the engine last reported, shown as :func:`mode` says.

    Hand it the engine's stderr line by line (:meth:`feed`): a line that is not a progress line is
    the caller's to keep. Before the engine reports, :meth:`phase` says what this side is doing
    ("starting joern"). Close it on every exit path; it is a context manager.
    """

    def __init__(self, repo: str, draft: str = "", engine: str = "", *, how: str | None = None, stream=None):
        self.repo, self.draft, self.engine = repo, draft, engine
        self.stream = stream if stream is not None else sys.stderr
        self.how = mode(stream=self.stream) if how is None else how
        self.info: dict = {}
        self.status = "starting"
        self.started = time.monotonic()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._logged_at = self.started
        self._logged = False
        self._bar = _tqdm_bar(self) if self.how == "bar" else None
        self._unlog = _log_above(self.stream, self._bar is not None) if self.how == "bar" else None
        if self.how in ("bar", "log"):
            threading.Thread(target=self._heartbeat, name="secpat-progress", daemon=True).start()

    # ── what the caller asks ──────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """Whether to ask the engine for progress lines, and stream its stderr to read them."""
        return self.how != "off"

    @property
    def title(self) -> str:
        return f"{self.repo} / {self.draft}" if self.draft else self.repo

    def env(self, base: dict | None = None) -> dict:
        """An engine's environment: ``base`` (default ``os.environ``) asking for progress lines."""
        env = dict(os.environ if base is None else base, SECPAT_PROGRESS="lines")
        if self.draft:
            env["SECPAT_PROGRESS_DRAFT"] = self.draft
        return env

    def phase(self, text: str) -> None:
        """What this side is doing until the engine reports ("building the CPG", ...)."""
        with self._lock:
            self.status = text
            self._draw()

    def update(self, info: dict) -> None:
        """One progress object. Keys it leaves out keep their last value."""
        with self._lock:
            self.info.update(info)
            self.status = str(info.get("phase") or self.status)
            self._draw()

    def feed(self, line: str) -> bool:
        """One line of the engine's stderr; True when it was a progress line, consumed here."""
        info = parse_line(line)
        if info is None:
            return False
        if self.how == "lines":
            with self._lock:
                self.stream.write(line if line.endswith("\n") else line + "\n")
                self.stream.flush()
        elif self.how != "off":
            self.update(info)
        return True

    def summary(self) -> str:
        """Engine, phase and counters."""
        engine = self.info.get("engine") or self.engine
        head = f"{engine} {self.status}" if engine else self.status
        counts = describe(self.info)
        return f"{head} | {counts}" if counts else head

    def text(self) -> str:
        """Everything on one line: what runs, for how long, and how far it got."""
        return f"{self.title}  {time.monotonic() - self.started:.1f}s  {self.summary()}"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            if self.how == "log" and self._logged:
                logger.info("progress: %s", self.text())
            elif self._bar is not None:
                self._bar.set_postfix_str(self.summary(), refresh=False)
                self._bar.close()
            elif self.how == "bar":
                _LINE.finish(self, keep=_line_batch() is None)
            if self._unlog is not None:
                self._unlog()

    def __enter__(self) -> Display:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── drawing ───────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        if self._closed or self.how != "bar":
            return
        if self._bar is not None:
            self._bar.set_postfix_str(self.summary(), refresh=False)
            self._bar.refresh()
        else:
            batch = _line_batch()
            _LINE.draw(self, (batch.prefix() + " " if batch is not None else "") + self.text())

    def _heartbeat(self) -> None:
        """Redraw while the engine is silent (Joern's start-up, a remote service), or log."""
        while not self._stop.wait(REDRAW_EVERY):
            with self._lock:
                if self._closed:
                    return
                if self.how == "bar":
                    self._draw()
                elif time.monotonic() - self._logged_at >= LOG_EVERY:
                    self._logged_at = time.monotonic()
                    self._logged = True
                    logger.info("progress: %s", self.text())


class Batch:
    """The outer bar of a runner looping over many drafts: [i/N], the current one, the ETA.

    ``log`` (default: this module's logger at INFO) gets a line at most every 10 s when stderr is
    not a terminal and ``SECPAT_PROGRESS=on``. :meth:`write` prints a message above the bar.
    """

    def __init__(self, total: int, desc: str = "", *, how: str | None = None, stream=None, log=None):
        global _batch
        self.total, self.desc, self.done, self.label = total, desc, 0, ""
        self.stream = stream if stream is not None else sys.stderr
        self.how = mode(stream=self.stream) if how is None else how
        self.log = log or logger.info
        self.started = time.monotonic()
        self._logged_at = 0.0
        self._bar = None
        self._unlog = None
        if self.how == "bar":
            tqdm = _tqdm()
            if tqdm is not None:
                self._bar = tqdm(total=total, desc=desc, unit="draft", file=self.stream, dynamic_ncols=True)
            _batch = self
            self._show()
            self._unlog = _log_above(self.stream, tqdm is not None)

    def eta(self) -> str:
        if not self.done:
            return "?"
        return clock((time.monotonic() - self.started) / self.done * (self.total - self.done))

    def prefix(self) -> str:
        """``[i/N ETA mm:ss]``, i counting the draft that runs now."""
        return f"[{min(self.done + 1, self.total)}/{self.total} ETA {self.eta()}]"

    def current(self, label: str) -> None:
        """``label`` (a repository, a draft) runs now."""
        self.label = label
        if self._bar is not None:
            self._bar.set_postfix_str(label)
        elif self.how == "log" and time.monotonic() - self._logged_at >= LOG_EVERY:
            self._logged_at = time.monotonic()
            self.log(f"{self.desc} {self.prefix()} {label}".strip())
        else:
            self._show()

    def advance(self, n: int = 1) -> None:
        self.done += n
        if self._bar is not None:
            self._bar.update(n)
        else:
            self._show()

    def write(self, message: str) -> None:
        """Print ``message`` on stdout without breaking the bar."""
        if self._bar is not None:
            self._bar.write(message, file=sys.stdout)
        elif _line_batch() is self:
            _LINE.around(lambda: print(message, flush=True), self._text())
        else:
            print(message, flush=True)

    def close(self) -> None:
        global _batch
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        elif _line_batch() is self:
            _LINE.finish(self, keep=True, text=self._text())
        if _batch is self:
            _batch = None
        if self._unlog is not None:
            self._unlog()
            self._unlog = None

    def __enter__(self) -> Batch:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _text(self) -> str:
        return f"{self.desc} {self.prefix()} {self.label}  {clock(time.monotonic() - self.started)}".strip()

    def _show(self) -> None:
        if _line_batch() is self:
            _LINE.draw(self, self._text())


def write(message: str) -> None:
    """Print ``message`` on stdout above the batch's bar, if a batch shows one."""
    if _batch is not None:
        _batch.write(message)
    else:
        print(message, flush=True)


def run(command: list[str], *, timeout: float | None, display: Display, cwd=None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run(command, capture_output=True, text=True, timeout=timeout)``, streamed.

    The child is asked for progress lines (:meth:`Display.env`). Each goes to ``display`` as it
    arrives and is left out of the returned ``stderr``, which keeps every other line. Past
    ``timeout`` the child is killed and ``subprocess.TimeoutExpired`` raised, as ``subprocess.run``
    does; a missing binary raises ``FileNotFoundError`` from here, as there.
    """
    proc = subprocess.Popen(command, cwd=cwd, env=display.env(env), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1)
    out: list[str] = []
    err: list[str] = []

    def pump_out() -> None:
        out.append(proc.stdout.read())

    def pump_err() -> None:
        for line in proc.stderr:
            if not display.feed(line):
                err.append(line)

    readers = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
    for t in readers:
        t.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        proc.wait(timeout=timeout)
        # Like communicate(): the output is complete when the pipes close, within the same budget.
        for t in readers:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            raise subprocess.TimeoutExpired(command, timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    proc.stdout.close()
    proc.stderr.close()
    return subprocess.CompletedProcess(command, proc.returncode, "".join(out), "".join(err))


# ── the terminal line ─────────────────────────────────────────────────────────

class _Line:
    """The one terminal line the fallback renderer redraws with ``\\r``, shared by a batch and the
    draft it runs, so the two never fight over it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.width = 0
        self.owner = None
        self.last = ""
        self.spin = 0

    def draw(self, owner, text: str, final: bool = False) -> None:
        with self.lock:
            self.owner = owner
            self.spin = (self.spin + 1) % 4
            cols = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
            self.last = text
            text = (text if final else "|/-\\"[self.spin] + " " + text)[:cols]
            stream = getattr(owner, "stream", sys.stderr)
            stream.write("\r" + text + " " * max(0, self.width - len(text)))
            stream.flush()
            self.width = len(text)

    def clear(self, stream) -> None:
        with self.lock:
            if self.width:
                stream.write("\r" + " " * self.width + "\r")
                self.width = 0

    def redraw(self) -> None:
        with self.lock:
            if self.owner is not None and not self.width:
                self.draw(self.owner, self.last)

    def finish(self, owner, keep: bool, text: str | None = None) -> None:
        """``owner`` is done: leave its last line (``keep``), or clear it for the batch to redraw."""
        with self.lock:
            stream = getattr(owner, "stream", sys.stderr)
            if keep:
                self.draw(owner, text if text is not None else owner.text(), final=True)
                stream.write("\n")
                self.width = 0
            else:
                stream.write("\r" + " " * self.width + "\r")
                self.width = 0
                if _line_batch() is not None:
                    _line_batch()._show()
            stream.flush()
            self.owner = None

    def around(self, action, redraw: str) -> None:
        """Clear the line, run ``action`` (a print), and redraw the batch's line."""
        with self.lock:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()
            self.width = 0
            action()
            if _line_batch() is not None:
                self.draw(_batch, redraw)


_LINE = _Line()

#: The batch showing a bar, if any.
_batch: Batch | None = None


def _line_batch() -> Batch | None:
    """The batch drawing the fallback line, if any: a draft's line then starts with its ``[i/N]``."""
    return _batch if _batch is not None and _batch.how == "bar" and _batch._bar is None else None


class _Above:
    """A logging handler's stream while the line is drawn on it: a record clears the line, and
    the line comes back below the record."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text: str) -> None:
        with _LINE.lock:
            _LINE.clear(self.stream)
            self.stream.write(text)

    def flush(self) -> None:
        with _LINE.lock:
            self.stream.flush()
            _LINE.redraw()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _log_above(stream, with_tqdm: bool):
    """Keep log records off the display's line while it is shown; returns the undo.

    The root logger's handlers writing to the display's stream are pointed through tqdm
    (``logging_redirect_tqdm``) or through :class:`_Above`.
    """
    if with_tqdm:
        from tqdm.contrib.logging import logging_redirect_tqdm

        redirect = logging_redirect_tqdm()
        redirect.__enter__()
        return lambda: redirect.__exit__(None, None, None)
    swapped = [(h, h.setStream(_Above(stream))) for h in logging.root.handlers
               if type(h) is logging.StreamHandler and h.stream is stream]

    def undo() -> None:
        for h, old in swapped:
            h.setStream(old)
    return undo


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _tqdm():
    try:
        from tqdm import tqdm
    except ImportError:
        return None
    return tqdm


def _tqdm_bar(display: Display):
    """A tqdm bar for ``display``: unknown total, the counters as postfix; ``None`` without tqdm."""
    tqdm = _tqdm()
    if tqdm is None:
        return None
    # leave=None keeps the last line of a bar of its own, and clears one nested under a batch's bar.
    return tqdm(total=None, desc=display.title, file=display.stream, leave=None, dynamic_ncols=True,
                bar_format="{desc} [{elapsed}]{postfix}")
