"""The traversal view: reading traces, the page and its exports, the server, and detect()'s traces.

No engine here: traces are written by hand, and a stand-in ``secpat-walk`` (a Python script) writes
a trace and a result when asked.
"""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import urllib.error
import urllib.request

import pytest

from secpat_interaction_order import config, detect, view

EVENTS = [
    {"ev": "header", "engine": "rust", "repo": "demo", "roles": [{"role": "A"}, {"role": "B"}]},
    {"ev": "start", "entryRole": "A", "entryRule": "behaviour", "roles": [{"role": "A", "nodes": [1]}]},
    {"ev": "enter", "method": 1, "fullName": "p.C.a:void()", "file": "C.java", "line": 3, "depth": 0, "roles": ["A"]},
    {"ev": "step", "node": 10, "label": "CALL", "line": 4, "code": "b()"},
    {"ev": "resolve", "site": 11, "line": 4, "name": "b", "code": "b()", "gated": False,
     "hops": [{"how": "direct", "targets": [{"id": 2, "fullName": "p.C.b:void()"}]}]},
    {"ev": "alt_skip", "site": 11, "k": 0, "target": 3, "fullName": "x"},
    {"ev": "hit", "node": 2, "site": 11, "line": 4, "roles": ["B"], "sources": ["A"], "pairs": 1},
    {"ev": "emit", "from": "A", "to": "B", "node": 2, "site": 11, "file": "C.java", "line": 4, "mechanism": "call"},
    {"ev": "exit", "method": 1, "depth": 0, "edges": 1},
    {"ev": "end", "ok": True, "interactions": 1, "dropped": 0},
]


def _trace(folder, name="20261118-000000__demo.trace.jsonl", events=EVENTS, tail=""):
    path = folder / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + tail)
    return path


def test_read_leaves_out_steps_slims_resolve_and_stops_at_a_partial_line(tmp_path):
    path = _trace(tmp_path, tail='{"ev": "resu')
    part = view.read(path)
    kinds = [e["ev"] for e in part["events"]]
    assert "step" not in kinds and "alt_skip" not in kinds
    assert part["dropped"] == {"step": 1, "alt_skip": 1}
    assert part["done"] is True and part["more"] is False
    resolve = next(e for e in part["events"] if e["ev"] == "resolve")
    assert resolve == {"ev": "resolve", "site": 11, "line": 4, "name": "b",
                       "targets": [{"id": 2, "fullName": "p.C.b:void()"}]}
    # the partial line is not consumed: the next read picks it up once it is complete
    with path.open("a") as out:
        out.write('lt", "ok": true}\n')
    rest = view.read(path, part["offset"])
    assert [e["ev"] for e in rest["events"]] == ["result"]


def test_read_in_batches(tmp_path):
    path = _trace(tmp_path)
    first = view.read(path, 0, limit=3)
    assert first["more"] is True and len(first["events"]) == 3
    second = view.read(path, first["offset"], limit=100)
    assert len(first["events"]) + len(second["events"]) == len(EVENTS) - 2


def test_traces_lists_trace_files_newest_first(tmp_path):
    old = _trace(tmp_path, "a.trace.jsonl")
    new = _trace(tmp_path, "b.trace.jsonl")
    (tmp_path / "b.trace.jsonl.graph.json").write_text("{}")
    import os
    os.utime(old, (1, 1))
    assert [t["name"] for t in view.traces(tmp_path)] == [new.name, old.name]


def test_export_is_one_offline_file_with_the_trace_inside(tmp_path):
    events = EVENTS[:2] + [{"ev": "enter", "method": 1, "fullName": "</script><b>x", "depth": 0}]
    path = _trace(tmp_path, events=events)
    out = view.export(path, tmp_path / "out" / "demo.html")
    html = out.read_text()
    assert html.count('<script id="trace-data" type="application/json">') == 1
    assert "</script><b>x" not in html and "<\\/script><b>x" in html
    assert "http://" not in html and "https://" not in html
    data = html.split('<script id="trace-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data.replace("<\\/", "</"))["events"][2]["fullName"] == "</script><b>x"


def test_finish_appends_the_result(tmp_path, monkeypatch):
    path = _trace(tmp_path)
    row = {"from": "A", "to": "B", "file": "C.java", "line": 4, "mechanism": "call"}
    view.finish(path, {"entryRole": "A", "entryRule": "behaviour", "interactions": [row]}, "pat")
    last = json.loads(path.read_text().splitlines()[-1])
    assert last == {"ev": "result", "ok": True, "label": "pat", "entryRole": "A", "entryRule": "behaviour",
                    "interactions": [{"rank": 1, **row}]}
    view.finish(path, None, "pat")
    assert json.loads(path.read_text().splitlines()[-1]) == {"ev": "result", "ok": False, "label": "pat"}


@pytest.fixture
def server(tmp_path):
    folder = tmp_path / "traces"
    folder.mkdir()
    _trace(folder, "20261118-000001__demo.trace.jsonl")
    _trace(folder, "20261118-000002__other.trace.jsonl")
    (tmp_path / "secret.trace.jsonl").write_text("{}\n")
    srv = view.make_server(folder, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, folder
    srv.shutdown()
    srv.server_close()


def _get(srv, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{path}", timeout=5) as answer:
        return answer.status, dict(answer.headers), answer.read()


def test_server_serves_the_page_the_list_and_the_traces(server):
    srv, folder = server
    assert json.loads(_get(srv, "/api/ping")[2]) == {"app": view.APP}
    status, headers, body = _get(srv, "/")
    assert status == 200 and b"Traversal view" in body and b'id="trace-data"' not in body
    listed = json.loads(_get(srv, "/api/traces")[2])
    assert listed["follow"] is True and len(listed["traces"]) == 2
    part = json.loads(_get(srv, "/api/trace?name=20261118-000001__demo.trace.jsonl&offset=0")[2])
    assert part["done"] and part["events"][0]["ev"] == "header"
    status, headers, body = _get(srv, "/api/export?name=20261118-000001__demo.trace.jsonl")
    assert "attachment" in headers["Content-Disposition"] and b'id="trace-data"' in body


@pytest.mark.parametrize("name", ["../secret.trace.jsonl", "secret.trace.jsonl", "..%2Fsecret.trace.jsonl",
                                  "20261118-000001__demo.trace.jsonl.graph.json", ""])
def test_server_serves_only_trace_files_in_its_folder(server, name):
    srv, _ = server
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(srv, f"/api/trace?name={name}")
    assert err.value.code == 404


def test_server_for_one_file_lists_only_that_file(tmp_path):
    path = _trace(tmp_path, "one.trace.jsonl")
    _trace(tmp_path, "two.trace.jsonl")
    srv = view.make_server(path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        listed = json.loads(_get(srv, "/api/traces")[2])
        assert listed["follow"] is False and [t["name"] for t in listed["traces"]] == ["one.trace.jsonl"]
        with pytest.raises(urllib.error.HTTPError):
            _get(srv, "/api/trace?name=two.trace.jsonl")
    finally:
        srv.shutdown()
        srv.server_close()


def test_announce_starts_a_viewer_once_and_prints_its_address(tmp_path, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(view, "_announced", False)
    monkeypatch.setattr(view, "_ping", lambda port: "none")
    monkeypatch.setattr(view.subprocess, "Popen", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(config, "SECPAT_VIEW_DIR", tmp_path)
    monkeypatch.setattr(config, "SECPAT_VIEW_PORT", 8123)
    view.announce(tmp_path / "x.trace.jsonl")
    view.announce(tmp_path / "y.trace.jsonl")
    assert len(started) == 1 and started[0][-4:] == ["--port", "8123", "--idle-exit", "1800"]
    assert "traversal view: http://127.0.0.1:8123/" in capsys.readouterr().err


def test_announce_does_not_start_a_second_viewer_or_take_another_programs_port(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(view.subprocess, "Popen", lambda *a, **k: pytest.fail("started a viewer"))
    monkeypatch.setattr(config, "SECPAT_VIEW_DIR", tmp_path)
    for state, said in (("viewer", "traversal view: http://"), ("other", "taken by another program")):
        monkeypatch.setattr(view, "_announced", False)
        monkeypatch.setattr(view, "_ping", lambda port, s=state: s)
        view.announce(tmp_path / "x.trace.jsonl")
        assert said in capsys.readouterr().err


# ── detect() with SECPAT_VIEW=nodes, on a stand-in secpat-walk ────────────────

FAKE_WALK = textwrap.dedent("""
    import json, sys
    args = sys.argv[1:]
    assert args[0] == "run", args
    opt = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
    result = {"schemaVersion": 2, "lang": "JAVASRC", "entryRole": opt["--entry"], "entryRule": "behaviour",
              "anchoredRoles": [{"role": "A", "kind": "METHOD"}],
              "interactions": [{"from": "A", "to": "B", "file": "A.java", "line": 3, "via": [],
                                "mechanism": "call", "path": [], "parent": None}]}
    open(opt["--output"], "w").write(json.dumps(result))
    if "--trace" in opt:
        open(opt["--trace"], "w").write(json.dumps({"ev": "header", "engine": "rust"}) + "\\n"
                                        + json.dumps({"ev": "end", "ok": True}) + "\\n")
""")


@pytest.fixture
def fake_walk(tmp_path, monkeypatch):
    walk = tmp_path / "secpat-walk"
    walk.write_text(f"#!{sys.executable}\n{FAKE_WALK}")
    walk.chmod(0o755)
    store = tmp_path / "cpg"
    store.mkdir()
    (store / "demo__JAVASRC.cpg").write_bytes(b"stand-in")
    checkout = tmp_path / "demo"
    checkout.mkdir()
    monkeypatch.setattr(config, "SECPAT_WALK_BIN", str(walk))
    monkeypatch.setattr(config, "JOERN_CPG_ROOT", store)
    monkeypatch.setattr(config, "JOERN_SERVICE_URL", "")
    monkeypatch.setattr(config, "JOERN_ENGINE", "rust")
    monkeypatch.setattr(config, "SECPAT_PROGRESS", "off")
    monkeypatch.setattr(config, "SECPAT_VIEW_DIR", tmp_path / "traces")
    monkeypatch.setattr(view, "announce", lambda trace: None)
    return checkout


MAPPING = {"A": {"entity": "a", "file": "A.java", "lines": "1"}, "B": {"entity": "b", "file": "A.java", "lines": "2"}}


@pytest.mark.parametrize("serve", [False, True])
def test_detect_saves_a_trace_with_the_result_when_asked(fake_walk, tmp_path, monkeypatch, serve):
    monkeypatch.setattr(config, "SECPAT_VIEW", "nodes")
    monkeypatch.setattr(config, "SECPAT_WALK_SERVE", serve)  # a traced query runs one-shot regardless
    result = detect("demo", "java", MAPPING, entry="A", repo_path=fake_walk, label="pat")
    assert result.available and result.tokens == ["A → B"]
    (trace,) = (tmp_path / "traces").glob("*.trace.jsonl")
    assert "__demo__pat__" in trace.name
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [e["ev"] for e in lines] == ["header", "end", "result"]
    assert lines[-1]["interactions"][0] == {
        "rank": 1, "from": "A", "to": "B", "file": "A.java", "line": 3, "mechanism": "call"}


def test_detect_saves_nothing_by_default(fake_walk, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECPAT_VIEW", "off")
    assert detect("demo", "java", MAPPING, entry="A", repo_path=fake_walk).available
    assert not (tmp_path / "traces").exists()


def test_detect_on_joern_warns_once_and_saves_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "SECPAT_VIEW", "nodes")
    monkeypatch.setattr(config, "JOERN_ENGINE", "joern")
    monkeypatch.setattr(config, "JOERN_SERVICE_URL", "")
    monkeypatch.setattr(config, "SECPAT_VIEW_DIR", tmp_path / "traces")
    monkeypatch.setattr(view, "_warned_engine", False)
    monkeypatch.setattr("secpat_interaction_order.joern_local.query", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        detect("demo", "java", MAPPING, entry="A", repo_path=tmp_path)
        detect("demo", "java", MAPPING, entry="A", repo_path=tmp_path)
    assert sum("SECPAT_VIEW=nodes" in r.message for r in caplog.records) == 1
    assert not (tmp_path / "traces").exists()
