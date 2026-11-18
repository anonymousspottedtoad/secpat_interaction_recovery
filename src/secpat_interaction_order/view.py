"""The traversal view: a local web page that replays a query's traversal, step by step.

With ``SECPAT_VIEW=nodes``, :func:`detect` saves each query's trace in ``SECPAT_VIEW_DIR``: the
native engine's event log (``secpat-walk --trace``), plus one ``result`` line with the order the
query returned. :func:`serve` serves ``view.html`` and the traces; the page draws the methods the
traversal enters as a call tree, marks the role nodes it reaches, lights up the path of every
interaction it records, and plays all of it back with a timeline. :func:`export` writes the page
and one trace as a single HTML file that replays offline.

Standard library only. The viewer reads traces; nothing here takes part in a traversal, and a
trace is written only when ``SECPAT_VIEW=nodes``.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

SUFFIX = ".trace.jsonl"
PAGE = Path(__file__).with_name("view.html")

#: Answered on ``/api/ping``, so a viewer can be told apart from whatever else holds the port.
APP = "secpat-traversal-view"

#: Events the page does not draw. ``step`` (one CFG node) is most of a large trace (79,401 of
#: 147,555 on camunda-bpm-platform); ``alt_skip`` and ``passed`` are bookkeeping. Counted, not sent.
DROPPED = frozenset({"step", "alt_skip", "passed"})

#: At most this many events per ``/api/trace`` answer; the page asks again for the rest.
BATCH = 20000

_announced = False
_warned_engine = False
_counter = 0


# --- writing traces (detect() side) ----------------------------------------------------


def new_trace(repo: str, label: str = "") -> Path:
    """A fresh trace path in ``SECPAT_VIEW_DIR``: time, repository and label, so it sorts by time."""
    global _counter
    _counter += 1
    folder = Path(config.SECPAT_VIEW_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "__".join(part for part in (stamp, _slug(repo), _slug(label), str(_counter)) if part)
    trace = folder / f"{name}{SUFFIX}"
    announce(trace)
    return trace


def finish(trace: Path, result: dict | None, label: str = "") -> None:
    """Append the query's outcome to its trace: whether it answered, and the order it returned."""
    if not trace.is_file():
        logger.warning("traversal view: the native engine wrote no trace at %s", trace)
        return
    line = {"ev": "result", "ok": isinstance(result, dict), "label": label}
    if isinstance(result, dict):
        line.update(
            entryRole=result.get("entryRole") or "",
            entryRule=result.get("entryRule") or "",
            interactions=[
                {
                    "rank": rank,
                    "from": row.get("from") or "",
                    "to": row.get("to") or "",
                    "file": row.get("file") or "",
                    "line": row.get("line"),
                    "mechanism": row.get("mechanism") or "call",
                }
                for rank, row in enumerate(result.get("interactions") or [], start=1)
            ],
        )
    with trace.open("a", encoding="utf-8") as out:
        out.write(json.dumps(line, ensure_ascii=False) + "\n")


def announce(trace: Path) -> None:
    """Start a viewer on ``SECPAT_VIEW_PORT`` if none answers there, and print its address once."""
    global _announced
    if _announced:
        return
    _announced = True
    port, folder = config.SECPAT_VIEW_PORT, Path(config.SECPAT_VIEW_DIR)
    state = _ping(port)
    if state == "other":
        print(f"traversal view: port {port} is taken by another program; set SECPAT_VIEW_PORT, then run "
              f"`secpat-interaction-order view {folder}`. Traces: {folder}", file=sys.stderr)
        return
    if state == "none":
        log = (folder / "viewer.log").open("a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-m", "secpat_interaction_order", "view", str(folder),
             "--port", str(port), "--idle-exit", "1800"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    print(f"traversal view: http://127.0.0.1:{port}/  (traces in {folder})", file=sys.stderr)


def warn_engine(engine: str) -> None:
    """Once: the view needs the native engine's trace, and this query runs elsewhere."""
    global _warned_engine
    if not _warned_engine:
        _warned_engine = True
        logger.warning(
            "SECPAT_VIEW=nodes: the traversal view reads the native engine's trace, and these queries "
            "run on %s. Set JOERN_ENGINE=rust to save traces.", engine,
        )


def _ping(port: int) -> str:
    """``viewer`` if our viewer answers on ``port``, ``other`` if something else holds it, else ``none``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return "none"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as answer:
            return "viewer" if json.load(answer).get("app") == APP else "other"
    except (OSError, ValueError):
        return "other"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-")[:80]


# --- reading traces ---------------------------------------------------------------------


def read(path: Path, offset: int = 0, limit: int = BATCH) -> dict:
    """Events of ``path`` from byte ``offset``: complete lines only, without :data:`DROPPED`.

    Returns ``events``, the ``offset`` to ask from next, ``dropped`` (events left out, by kind),
    ``done`` (the trace has its ``end`` line) and ``more`` (stopped at ``limit``).
    """
    events: list[dict] = []
    dropped: Counter = Counter()
    done = more = False
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):  # the engine is still writing this line
                handle.seek(start)
                break
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("ev")
            if kind in ("end", "result"):
                done = done or kind == "end"
            if kind in DROPPED:
                dropped[kind] += 1
                continue
            events.append(_slim(event))
            if len(events) >= limit:
                more = True
                break
        offset = handle.tell()
    return {"events": events, "offset": offset, "dropped": dict(dropped), "done": done, "more": more}


def read_all(path: Path) -> dict:
    """Every event of ``path`` the page draws, and the counts of the ones it does not."""
    events: list[dict] = []
    dropped: Counter = Counter()
    offset = 0
    while True:
        part = read(path, offset, limit=10**9)
        events += part["events"]
        dropped.update(part["dropped"])
        if part["offset"] == offset or not part["more"]:
            return {"events": events, "dropped": dict(dropped), "done": part["done"]}
        offset = part["offset"]


def _slim(event: dict) -> dict:
    """A ``resolve`` keeps what the page uses: where the call is, and what it runs."""
    if event.get("ev") != "resolve":
        return event
    targets = [
        {"id": t.get("id"), "fullName": t.get("fullName")}
        for hop in event.get("hops") or []
        for t in hop.get("targets") or []
    ]
    return {"ev": "resolve", "site": event.get("site"), "line": event.get("line"),
            "name": event.get("name"), "targets": targets}


def traces(folder: Path) -> list[dict]:
    """The traces in ``folder``, newest first."""
    found = []
    for path in folder.glob(f"*{SUFFIX}"):
        stat = path.stat()
        found.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return sorted(found, key=lambda t: (t["mtime"], t["name"]), reverse=True)


# --- the page ---------------------------------------------------------------------------


def page(embedded: dict | None = None) -> str:
    """``view.html``, with one trace inside it when ``embedded`` is given (an export)."""
    html = PAGE.read_text(encoding="utf-8")
    if embedded is None:
        return html
    data = json.dumps(embedded, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tag = f'<script id="trace-data" type="application/json">{data}</script>'
    return html.replace("<!--TRACE-DATA-->", tag, 1)


def export(trace: Path, out: Path) -> Path:
    """One HTML file that replays ``trace`` offline: the page, with the trace inside."""
    data = read_all(trace)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page({"name": trace.name, **data}), encoding="utf-8")
    return out


# --- the server -------------------------------------------------------------------------


def serve(target: Path, host: str = "127.0.0.1", port: int = 8765, idle_exit: float = 0) -> None:
    """Serve the page for ``target`` until interrupted (see :func:`make_server`).

    ``idle_exit`` > 0 stops the server after that many seconds with no request and no new trace.
    """
    server = make_server(target, host, port)
    target = target.resolve()
    folder = target if target.is_dir() else target.parent
    if idle_exit > 0:
        threading.Thread(target=_idle_watch, args=(server, folder, server.activity, idle_exit), daemon=True).start()
    print(f"traversal view: http://{host}:{server.server_port}/  "
          f"({'following ' + str(folder) if target.is_dir() else target})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def make_server(target: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """A server for ``target``: one trace file, or a folder whose newest trace the page follows.

    Only trace files directly in that folder are served (only ``target`` itself when it is a file).
    """
    target = target.resolve()
    folder = target if target.is_dir() else target.parent
    only = None if target.is_dir() else target.name
    activity = {"at": time.monotonic()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet: the page polls twice a second
            pass

        def do_GET(self):
            activity["at"] = time.monotonic()
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if url.path in ("/", "/index.html"):
                return self._send(200, page().encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/ping":
                return self._json({"app": APP})
            if url.path == "/api/traces":
                listed = traces(folder)
                if only is not None:
                    listed = [t for t in listed if t["name"] == only]
                return self._json({"folder": str(folder), "follow": only is None, "traces": listed})
            path = self._trace(query.get("name", ""))
            if path is None:
                return self._send(404, b"no such trace", "text/plain")
            if url.path == "/api/trace":
                try:
                    offset = max(0, int(query.get("offset", "0")))
                except ValueError:
                    offset = 0
                return self._json(read(path, offset))
            if url.path == "/api/export":
                body = page({"name": path.name, **read_all(path)}).encode("utf-8")
                filename = path.name.removesuffix(SUFFIX) + ".html"
                return self._send(200, body, "text/html; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{filename}"'})
            return self._send(404, b"not found", "text/plain")

        def _trace(self, name: str) -> Path | None:
            """Only a trace file directly in the served folder (and only ``target`` when it is one)."""
            if not name.endswith(SUFFIX) or "/" in name or "\\" in name or name.startswith("."):
                return None
            if only is not None and name != only:
                return None
            path = folder / name
            return path if path.is_file() else None

        def _json(self, data: dict):
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

        def _send(self, status: int, body: bytes, kind: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.activity = activity
    return server


def _idle_watch(server: ThreadingHTTPServer, folder: Path, activity: dict, idle: float) -> None:
    newest = 0.0
    while True:
        time.sleep(min(15.0, idle))
        listed = traces(folder)
        if listed and listed[0]["mtime"] > newest:
            newest = listed[0]["mtime"]
            activity["at"] = time.monotonic()
        if time.monotonic() - activity["at"] > idle:
            server.shutdown()
            return
