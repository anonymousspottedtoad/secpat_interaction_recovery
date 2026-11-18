"""The command line: argument handling, the dataset's role rule, and the comparison with the dataset.

No Joern here: ``detect`` is replaced by a stand-in that records what it was asked and
answers with a fixed order.
"""

from __future__ import annotations

import json

import pytest

from secpat_interaction_order import cli
from secpat_interaction_order.detector import DetectionResult, Interaction

PATTERN = {
    "roles": [
        {"name": "Subject", "required": False},
        {"name": "Enforcer", "required": True},
        {"name": "Verifier"},
        {"name": "Store", "required": None},
    ],
    "behaviour": [
        {"step": "1", "from": "Subject", "to": "Enforcer"},
        {"step": "2", "from": "Enforcer", "to": "Verifier"},
        {"step": "3", "from": "Verifier", "to": "Store"},
    ],
}

ROWS = [
    {"patternRole": "Subject", "status": "present", "entity": "", "file": "", "lines": ""},
    {"patternRole": "Enforcer", "status": "present", "entity": "check", "file": "a.py", "lines": "3"},
    {"patternRole": "Enforcer", "status": "present", "entity": "other", "file": "b.py", "lines": "9"},
    {"patternRole": "Verifier", "status": "present", "entity": "verify", "file": "a.py", "lines": "10"},
    {"patternRole": "Store", "status": "external", "entity": "get", "file": "a.py", "lines": "12"},
]

RESULT = DetectionResult(
    interactions=[
        Interaction(source="Enforcer", target="Verifier", file="a.py", line=5),
        Interaction(source="Verifier", target="Store", file="a.py", line=12, parent="Enforcer → Verifier"),
    ],
    entry_role="Enforcer",
    entry_rule="behaviour",
    anchored_roles={"Enforcer": "METHOD", "Verifier": "METHOD", "Store": "external"},
)


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    """The commands set the CPG folder and the engine in ``config``; undo that after each test."""
    for name in ("JOERN_CPG_ROOT", "JOERN_ENGINE"):
        monkeypatch.setattr(cli.config, name, getattr(cli.config, name))


@pytest.fixture
def fake_detect(monkeypatch):
    calls = []

    def detect(repo, language, mappings, **kwargs):
        calls.append({"repo": repo, "language": language, "mappings": mappings, **kwargs})
        return RESULT

    monkeypatch.setattr(cli, "detect", detect)
    return calls


def test_scored_drops_only_roles_marked_false_and_their_steps():
    walked, behaviour = cli.scored(PATTERN, cli._index(ROWS))
    assert list(walked) == ["Enforcer", "Verifier", "Store"]
    assert [s["step"] for s in behaviour] == ["2", "3"]


def test_scored_without_a_pattern_keeps_everything():
    mappings = cli._index(ROWS)
    assert cli.scored(None, mappings) == (mappings, [])


def test_index_reads_role_or_pattern_role_and_keeps_the_first_row():
    indexed = cli._index(ROWS + [{"role": "Hasher", "entity": "h"}])
    assert indexed["Enforcer"]["entity"] == "check"
    assert list(indexed) == ["Subject", "Enforcer", "Verifier", "Store", "Hasher"]


def _dataset(tmp_path, stored):
    root = tmp_path / "dataset"
    (root / "codebase" / "repos" / "python" / "owner_repo").mkdir(parents=True)
    (root / "security_patterns" / "pat").mkdir(parents=True)
    (root / "security_patterns" / "pat" / "04_pattern.json").write_text(json.dumps(PATTERN))
    (root / "ground_truth" / "python").mkdir(parents=True)
    truth = {"role_mappings": ROWS, "interaction_order": stored}
    (root / "ground_truth" / "python" / "pat__owner_repo.json").write_text(json.dumps(truth))
    runs = {"runs": [{"pattern": "pat", "repo": "owner_repo", "language": "python"}]}
    (root / "manifest_python_full.json").write_text(json.dumps(runs))
    return root


def _stored():
    """What the dataset stores for ROWS: the envelope of RESULT."""
    return cli._envelope(RESULT, "owner_repo", "python", cli._index(ROWS))


def test_dataset_reports_a_match_and_traverses_the_scored_roles(tmp_path, fake_detect, capsys):
    root = _dataset(tmp_path, _stored())
    code = cli.main(["dataset", "--dataset", str(root), "--output", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0
    assert "[1/1] pat__owner_repo: 2 interaction(s), same as the dataset" in out
    assert "1 of 1 record(s) give the same interaction order as the dataset" in out

    (call,) = fake_detect
    assert call["repo"] == "owner_repo" and call["language"] == "python"
    assert list(call["mappings"]) == ["Enforcer", "Verifier", "Store"]
    assert [s["step"] for s in call["behaviour"]] == ["2", "3"]
    written = json.loads((tmp_path / "out" / "python" / "pat__owner_repo.json").read_text())
    assert written == _stored()
    assert written["skippedRoles"] == ["Subject"]


def test_dataset_reports_a_difference_and_exits_1(tmp_path, fake_detect, capsys):
    stored = _stored()
    stored["interactions"] = stored["interactions"][:1]
    root = _dataset(tmp_path, stored)
    assert cli.main(["dataset", "--dataset", str(root)]) == 1
    out = capsys.readouterr().out
    assert "DIFFERENT from the dataset" in out
    assert "dataset: Enforcer → Verifier\n" in out
    assert "derived: Enforcer → Verifier, Verifier → Store" in out


def test_dataset_with_fewer_than_two_grounded_roles_does_not_traverse(tmp_path, fake_detect, capsys):
    stored = _stored()
    root = _dataset(tmp_path, stored)
    truth_file = root / "ground_truth" / "python" / "pat__owner_repo.json"
    truth = json.loads(truth_file.read_text())
    truth["role_mappings"] = [dict(r, status="missing") for r in ROWS[:4]] + ROWS[4:]
    truth_file.write_text(json.dumps(truth))
    assert cli.main(["dataset", "--dataset", str(root)]) == 1
    assert fake_detect == []
    assert "0 interaction(s)" in capsys.readouterr().out


def test_dataset_filters_by_repo_and_pattern(tmp_path, fake_detect):
    root = _dataset(tmp_path, _stored())
    with pytest.raises(SystemExit, match="no record .* for repo 'other'"):
        cli.main(["dataset", "--dataset", str(root), "--repo", "other"])
    assert cli.main(["dataset", "--dataset", str(root), "--repo", "owner_repo", "--pattern", "pat"]) == 0


def test_run_passes_the_roles_as_given_and_prints_the_order(tmp_path, fake_detect, capsys):
    codebase = tmp_path / "owner_repo"
    codebase.mkdir()
    roles = [{"role": "Enforcer", "entity": "check", "file": "a.py", "lines": "3"},
             {"role": "Verifier", "entity": "verify", "file": "a.py", "lines": "10"}]
    (tmp_path / "roles.json").write_text(json.dumps(roles))
    code = cli.main([
        "run", "--codebase", str(codebase), "--language", "PYTHONSRC",
        "--roles", f"@{tmp_path / 'roles.json'}",
        "--behaviour", '[{"step": "1", "from": "Enforcer", "to": "Verifier"}]',
        "--cpg-dir", str(tmp_path / "cpg"), "--output", str(tmp_path / "result.json"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("derived 2 interaction(s) from 2 role(s), entry=Enforcer")
    assert "  1. Enforcer → Verifier  a.py:5  call\n  2. Verifier → Store     a.py:12  call\n" in out
    (call,) = fake_detect
    assert call["language"] == "python" and call["repo_path"] == codebase
    assert list(call["mappings"]) == ["Enforcer", "Verifier"]
    assert cli.config.JOERN_CPG_ROOT == (tmp_path / "cpg").resolve()
    assert json.loads((tmp_path / "result.json").read_text())["entryRole"] == "Enforcer"


def test_run_rejects_an_unknown_language(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--codebase", ".", "--language", "kotlin", "--roles", "[]"])
    assert "use java or python" in capsys.readouterr().err


def test_run_reports_a_failed_traversal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect", lambda *a, **k: DetectionResult(reason="Joern failed."))
    code = cli.main(["run", "--codebase", str(tmp_path), "--language", "java",
                     "--roles", '[{"role": "A", "entity": "a", "file": "A.java"}]'])
    assert code == 1
    assert capsys.readouterr().out == "no interaction order: Joern failed.\n"
