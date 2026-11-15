"""The progress display: parsing engine lines, choosing a mode, and showing them without a terminal.

No Joern here: a stand-in `joern` (a Python script) writes progress lines and a result, so the
streamed run can be compared with the captured one it replaces.
"""

from __future__ import annotations

import http.server
import io
import json
import logging
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from secpat_interaction_order import config, detect, joern_local, joern_service, progress

LINE = (
    'SECPAT_PROGRESS {"engine":"joern","repo":"factcast_factcast","draft":null,"phase":"traverse",'
    '"elapsed_s":0.68,"cpg":{"nodes":139502,"edges":735095,"methods":6088},"bodies":2,"replayed":0,'
    '"distinct_methods":2,"calls":40,"cfg_steps":167,"interactions":2,"depth":1,"max_depth":1,'
    '"alternatives":0,"done":false}\n'
)


class Stream(io.StringIO):
    def __init__(self, tty: bool = False):
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_line_reads_a_progress_line():
    info = progress.parse_line(LINE)
    assert info["engine"] == "joern"
    assert info["draft"] is None
    assert info["cpg"] == {"nodes": 139502, "edges": 735095, "methods": 6088}
    assert info["done"] is False


@pytest.mark.parametrize("line", [
    "",
    "Picked up JAVA_TOOL_OPTIONS: -Xmx8g\n",
    "SECPAT_PROGRESS\n",                       # no object
    "SECPAT_PROGRESS {not json}\n",
    'SECPAT_PROGRESS [1, 2]\n',                # not an object
    ' SECPAT_PROGRESS {"phase":"load"}\n',     # the prefix starts the line
    'BATCH-PROGRESS {"phase":"load"}\n',
])
def test_parse_line_rejects_other_lines(line):
    assert progress.parse_line(line) is None


def test_parse_line_accepts_a_partial_object():
    line = 'SECPAT_PROGRESS {"engine":"rust","phase":"load"}'
    assert progress.parse_line(line) == {"engine": "rust", "phase": "load"}


# ── modes ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("setting, tty, expected", [
    ("auto", True, "bar"), ("auto", False, "off"), ("", False, "off"), ("bogus", True, "bar"),
    ("on", True, "bar"), ("on", False, "log"), ("bar", False, "log"), ("1", True, "bar"),
    ("lines", True, "lines"), ("lines", False, "lines"), ("LINES", False, "lines"),
    ("off", True, "off"), ("0", True, "off"), ("none", False, "off"),
])
def test_mode(setting, tty, expected):
    assert progress.mode(setting, Stream(tty)) == expected


def test_mode_reads_config(monkeypatch):
    monkeypatch.setattr(config, "SECPAT_PROGRESS", "on")
    assert progress.mode(stream=Stream(False)) == "log"


# ── formatting ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, text", [
    (0, "0"), (999, "999"), (1234, "1.23k"), (999_999, "1M"), (1_386_997, "1.39M"), (8_085_045, "8.09M"),
    ("x", "x"),
])
def test_human(n, text):
    assert progress.human(n) == text


def test_describe():
    assert progress.describe(progress.parse_line(LINE)) == (
        "CPG 140k nodes 735k edges 6.09k methods | bodies=2 replayed=0 methods=2 calls=40 cfg=167 "
        "interactions=2 alts=0 depth=1/1")
    assert progress.describe({"phase": "load"}) == ""


# ── the display ───────────────────────────────────────────────────────────────

def test_display_feed_keeps_other_lines_for_the_caller():
    with progress.Display("factcast_factcast", "Enforcer, 4 roles", "joern", how="log", stream=Stream()) as d:
        assert d.feed(LINE) is True
        assert d.feed("Picked up JAVA_TOOL_OPTIONS: -Xmx8g\n") is False
        assert d.feed('SECPAT_PROGRESS {"phase":"done","interactions":3,"done":true}\n') is True
    # A key a later line leaves out keeps its last value.
    assert d.info["cpg"]["nodes"] == 139502
    assert d.info["interactions"] == 3
    assert d.status == "done"


def test_display_off_does_nothing():
    stream = Stream(tty=True)
    with progress.Display("repo", how="off", stream=stream) as d:
        assert not d.active
        assert d.feed(LINE) is True
        d.phase("starting joern")
    assert stream.getvalue() == ""


def test_display_lines_passes_lines_through():
    stream = Stream()
    with progress.Display("repo", how="lines", stream=stream) as d:
        assert d.active
        d.feed(LINE)
        d.feed("other\n")
    assert stream.getvalue() == LINE


def test_display_env_asks_for_lines():
    d = progress.Display("repo", "Enforcer, 4 roles", how="off")
    env = d.env({"PATH": "/bin", "SECPAT_PROGRESS": "auto"})
    assert env == {"PATH": "/bin", "SECPAT_PROGRESS": "lines", "SECPAT_PROGRESS_DRAFT": "Enforcer, 4 roles"}


def test_log_fallback_is_quiet_for_a_short_run(caplog):
    caplog.set_level(logging.INFO, logger="secpat_interaction_order.progress")
    with progress.Display("repo", how="log", stream=Stream()) as d:
        d.feed(LINE)
    assert caplog.records == []


def test_log_fallback_logs_periodically_and_at_the_end(monkeypatch, caplog):
    monkeypatch.setattr(progress, "LOG_EVERY", 0.05)
    monkeypatch.setattr(progress, "REDRAW_EVERY", 0.01)
    caplog.set_level(logging.INFO, logger="secpat_interaction_order.progress")
    stream = Stream()
    with progress.Display("factcast_factcast", "Enforcer, 4 roles", "joern", how="log", stream=stream) as d:
        d.phase("starting joern")
        time.sleep(0.1)
        d.feed(LINE)
        time.sleep(0.1)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) >= 3
    assert "factcast_factcast / Enforcer, 4 roles" in messages[0] and "joern starting joern" in messages[0]
    assert "joern traverse | CPG 140k nodes 735k edges 6.09k methods | bodies=2" in messages[-1]
    assert stream.getvalue() == ""        # the log, not the stream


def test_line_fallback_draws_and_leaves_its_last_line(monkeypatch):
    monkeypatch.setattr(progress, "_tqdm", lambda: None)
    stream = Stream(tty=True)
    with progress.Display("factcast_factcast", "Enforcer, 4 roles", "joern", how="bar", stream=stream) as d:
        d.feed(LINE)
    out = stream.getvalue()
    assert out.startswith("\r") and out.endswith("\n")
    last = out.rstrip("\n").split("\r")[-1]
    assert last.startswith("factcast_factcast / Enforcer, 4 roles")
    assert "joern traverse | CPG 140k nodes" in last


def test_tqdm_bar_shows_the_counters():
    pytest.importorskip("tqdm")
    stream = Stream(tty=True)
    with progress.Display("factcast_factcast", "Enforcer, 4 roles", "joern", how="bar", stream=stream) as d:
        d.feed(LINE)
    last = stream.getvalue().rstrip("\n").split("\r")[-1]
    assert last.startswith("factcast_factcast / Enforcer, 4 roles [00:00], joern traverse | CPG 140k nodes")


def test_batch_logs_its_position(caplog):
    caplog.set_level(logging.INFO, logger="secpat_interaction_order.progress")
    with progress.Batch(3, "drafts", how="log", stream=Stream()) as b:
        b.current("repo-a")
        b.advance()
        b.current("repo-b")      # within 10 s of the last line: not logged
    assert [r.getMessage() for r in caplog.records] == ["drafts [1/3 ETA ?] repo-a"]


# ── streaming a child ─────────────────────────────────────────────────────────

CHILD = textwrap.dedent("""
    import os, sys, time
    print("out-1")
    print("err-1", file=sys.stderr)
    if os.environ.get("SECPAT_PROGRESS") == "lines":
        print('SECPAT_PROGRESS {"phase":"traverse","bodies":5,"draft":"%s"}'
              % os.environ.get("SECPAT_PROGRESS_DRAFT", ""), file=sys.stderr, flush=True)
    print("err-2", file=sys.stderr)
    print("out-2")
    sys.exit(3)
""")


def test_run_streams_progress_and_captures_the_rest():
    d = progress.Display("repo", "draft-x", how="log", stream=Stream())
    with d:
        done = progress.run([sys.executable, "-c", CHILD], timeout=60, display=d)
    plain = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout, done.stderr) == (plain.returncode, plain.stdout, plain.stderr)
    assert done.returncode == 3 and done.stderr == "err-1\nerr-2\n"
    assert d.info == {"phase": "traverse", "bodies": 5, "draft": "draft-x"}


def test_run_times_out_like_subprocess_run():
    d = progress.Display("repo", how="log", stream=Stream())
    started = time.monotonic()
    with d, pytest.raises(subprocess.TimeoutExpired):
        progress.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5, display=d)
    assert time.monotonic() - started < 10


def test_run_missing_binary_raises_like_subprocess_run():
    with pytest.raises(FileNotFoundError):
        progress.run(["/nonexistent/joern"], timeout=5, display=progress.Display("repo", how="log"))


# ── the local Joern runner, with a stand-in joern ─────────────────────────────

FAKE_JOERN = textwrap.dedent("""
    import json, os, sys
    params = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    print("Picked up JAVA_TOOL_OPTIONS: -Xmx8g", file=sys.stderr)
    if os.environ.get("SECPAT_PROGRESS") == "lines":
        print('SECPAT_PROGRESS {"engine":"joern","phase":"load","bodies":0,"done":false}', file=sys.stderr)
        print('SECPAT_PROGRESS {"engine":"joern","phase":"done","bodies":7,"interactions":1,"done":true}',
              file=sys.stderr)
    if params.get("entry") == "fail":
        print("boom", file=sys.stderr)
        sys.exit(1)
    result = {"schemaVersion": 2, "lang": "JAVASRC", "entryRole": params.get("entry", ""),
              "entryRule": "behaviour", "anchoredRoles": [{"role": "A", "kind": "METHOD"}],
              "interactions": [{"from": "A", "to": "B", "file": "A.java", "line": 3, "via": [],
                                "mechanism": "call", "path": [], "parent": None}]}
    open(params["outputPath"], "w").write(json.dumps(result))
    print("derived 1 interaction(s)")
""")


@pytest.fixture
def fake_joern(tmp_path, monkeypatch):
    joern = tmp_path / "joern"
    joern.write_text(f"#!{sys.executable}\n{FAKE_JOERN}")
    joern.chmod(0o755)
    store = tmp_path / "cpg"
    store.mkdir()
    (store / "demo__JAVASRC.cpg").write_bytes(b"stand-in")
    checkout = tmp_path / "demo"
    checkout.mkdir()
    monkeypatch.setattr(config, "JOERN_BIN", str(joern))
    monkeypatch.setattr(config, "JOERN_CPG_ROOT", store)
    monkeypatch.setattr(config, "JOERN_SERVICE_URL", "")
    monkeypatch.setattr(config, "JOERN_ENGINE", "joern")
    return checkout


def test_local_query_returns_the_same_result_with_and_without_progress(fake_joern):
    params = {"roles": "[]", "behaviour": "[]", "entry": "A"}
    plain = joern_local.query("demo", "java", "// script", fake_joern, params=params)
    d = progress.Display("demo", how="log", stream=Stream())
    with d:
        streamed = joern_local.query("demo", "java", "// script", fake_joern, params=params, display=d)
    assert plain == streamed and plain["entryRole"] == "A"
    assert d.info["bodies"] == 7 and d.status == "done"


def test_local_query_failure_logs_the_same_detail(fake_joern, caplog):
    params = {"roles": "[]", "behaviour": "[]", "entry": "fail"}
    caplog.set_level(logging.WARNING, logger="secpat_interaction_order.joern_local")
    assert joern_local.query("demo", "java", "// script", fake_joern, params=params) is None
    with progress.Display("demo", how="log", stream=Stream()) as d:
        assert joern_local.query("demo", "java", "// script", fake_joern, params=params, display=d) is None
    plain, streamed = [r.getMessage() for r in caplog.records]
    assert plain == streamed
    assert "exited 1" in plain and "boom" in plain and "SECPAT_PROGRESS" not in plain


def test_detect_gives_the_same_result_with_progress_on_and_off(fake_joern, monkeypatch):
    mappings = {"A": {"entity": "a", "file": "A.java", "lines": "3"},
                "B": {"entity": "b", "file": "B.java", "lines": "9"}}
    behaviour = [{"step": "1", "from": "A", "to": "B"}]
    results = {}
    for setting in ("off", "on", "lines"):
        monkeypatch.setattr(config, "SECPAT_PROGRESS", setting)
        results[setting] = detect("demo", "java", mappings, behaviour=behaviour, repo_path=fake_joern)
    assert results["off"] == results["on"] == results["lines"]
    assert results["off"].tokens == ["A → B"] and results["off"].entry_role == "A"


# ── the query service: nothing streams, the display waits ─────────────────────

def test_service_query_shows_the_wait(monkeypatch, caplog):
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(0.3)
            body = json.dumps({"graph": {"schemaVersion": 2, "interactions": []}, "elapsedSeconds": 0.3}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "JOERN_SERVICE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(progress, "LOG_EVERY", 0.05)
    monkeypatch.setattr(progress, "REDRAW_EVERY", 0.01)
    caplog.set_level(logging.INFO, logger="secpat_interaction_order.progress")
    try:
        with progress.Display("demo", "A, 2 roles", "service", how="log", stream=Stream()) as d:
            result = joern_service.query("demo", "java", "// script", params={}, display=d)
    finally:
        server.shutdown()
    assert result == {"schemaVersion": 2, "interactions": []}
    assert any("demo / A, 2 roles" in r.getMessage() and "service waiting for the query service" in r.getMessage()
               for r in caplog.records)
