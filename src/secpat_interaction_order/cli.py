"""``secpat-interaction-order``: the interaction order from the command line.

Three commands. ``run`` and ``dataset`` call :func:`detect`; ``view`` shows traversals:

``run``
    One role mapping in one repository, given on the command line.

``dataset``
    Records of the paper's dataset. For each, derives the order of its ground-truth
    role mapping and compares it with the ``interaction_order`` stored beside it.

``view``
    The traversal view (``view.py``): a local web page that replays the traces saved
    with ``SECPAT_VIEW=nodes``, or writes one of them as a standalone HTML file.

Before the traversal, ``dataset`` drops the roles the pattern marks ``required:
false`` (``Subject``, ``System``) and the steps that involve them. That is the rule
of secpat-scoring's ``scored_mappings``, which produced the stored orders through
secpatrec's ``detect_action_sequence``. It is repeated here so this package needs
nothing else. ``run`` traverses the roles exactly as given.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import config
from .detector import DetectionResult, detect

#: Statuses that count as "found in code", as in secpat-scoring's ``is_detected``.
_DETECTED = frozenset({"present", "external"})

#: The parts of an ``interaction_order`` that ``dataset`` compares: what the traversal
#: derived. ``reason`` and ``skippedRoles`` describe the input, not the result.
_COMPARED = ("entryRole", "entryRule", "interactions", "anchoredRoles")

_LANGUAGES = {"java": "java", "javasrc": "java", "python": "python", "pythonsrc": "python"}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if getattr(args, "engine", None):
        config.JOERN_ENGINE = args.engine
    if getattr(args, "full_view_cpg", False):
        config.SECPAT_VIEW_FULL_CPG = True
        config.SECPAT_VIEW = "nodes"
    return args.command(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secpat-interaction-order",
        description="Derive the order in which a security pattern's roles interact in a codebase.",
    )
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    run = commands.add_parser(
        "run",
        help="one role mapping in one repository",
        description="Derive the interaction order of one role mapping in one repository.",
    )
    run.add_argument("--codebase", required=True, type=Path, help="the repository's folder")
    run.add_argument("--language", required=True, type=_language, help="java or python")
    run.add_argument(
        "--roles", required=True,
        help='the role mapping, a JSON list of {"role", "entity", "file", "lines"} '
             "(or @FILE to read it from a file)",
    )
    run.add_argument(
        "--behaviour", default="",
        help='the pattern\'s steps, a JSON list of {"step", "from", "to"} (or @FILE); '
             "they choose the entry role and order roles found on the same code",
    )
    run.add_argument("--entry", default="", help="the role to start from (default: read off --behaviour)")
    run.add_argument(
        "--cpg-dir", type=Path,
        help="the folder with the repository's CPG, named <folder name>__JAVASRC.cpg or __PYTHONSRC.cpg; "
             "when it is missing, joern-parse builds it there "
             f"(default: {config.JOERN_CPG_ROOT}, or JOERN_CPG_ROOT)",
    )
    run.add_argument("--output", type=Path, help="write the result, as JSON, to this file")
    _engine_option(run)
    _full_view_option(run)
    run.set_defaults(command=_run)

    dataset = commands.add_parser(
        "dataset",
        help="re-derive the dataset's ground-truth interaction orders",
        description="For each record of the paper's dataset, derive the interaction order of its "
                    "ground-truth role mapping and compare it with the one stored in the dataset.",
    )
    dataset.add_argument("--dataset", required=True, type=Path, help="the dataset's folder")
    dataset.add_argument("--repo", default="", help="only the records of this repository")
    dataset.add_argument("--pattern", default="", help="only the records of this pattern")
    dataset.add_argument("--output", type=Path, help="write each derived order to this folder")
    _engine_option(dataset)
    _full_view_option(dataset)
    dataset.set_defaults(command=_dataset)

    view_cmd = commands.add_parser(
        "view",
        help="replay saved traversals in a web page",
        description="Serve the traversal view: a local web page that replays traversal traces "
                    "(saved with SECPAT_VIEW=nodes on the native engine). Given a folder, it follows "
                    "the newest trace as new ones arrive.",
    )
    view_cmd.add_argument(
        "trace", nargs="?", type=Path,
        help=f"a trace file, or a folder of them (default: {config.SECPAT_VIEW_DIR}, or SECPAT_VIEW_DIR)",
    )
    view_cmd.add_argument("--port", type=int, default=config.SECPAT_VIEW_PORT,
                          help=f"the port (default: {config.SECPAT_VIEW_PORT}, or SECPAT_VIEW_PORT)")
    view_cmd.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: 127.0.0.1)")
    view_cmd.add_argument("--export", type=Path, metavar="OUT.html",
                          help="write the trace as one HTML file that replays offline, and exit")
    view_cmd.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                          help="stop after this long with no page open and no new trace (default: never)")
    view_cmd.add_argument(
        "--full-view-cpg", action="store_true", default=config.SECPAT_VIEW_FULL_CPG,
        help="open on a map of the whole CPG, the traversal's methods lit up on it (with --export: put "
             "the map in the file); made with secpat-walk when missing (default: off, or SECPAT_VIEW_FULL_CPG)",
    )
    view_cmd.set_defaults(command=_view)
    return parser


def _engine_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine", choices=("joern", "rust"),
        help="joern (local Joern) or rust (secpat-walk, from SECPAT_WALK_BIN or PATH); "
             f"(default: {config.JOERN_ENGINE}, or JOERN_ENGINE)",
    )


def _full_view_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full-view-cpg", action="store_true",
        help="save each traversal for the traversal view and show it on a map of the whole CPG "
             "(native engine; the same as SECPAT_VIEW_FULL_CPG=1)",
    )


def _language(value: str) -> str:
    language = _LANGUAGES.get(value.strip().lower())
    if language is None:
        raise argparse.ArgumentTypeError(f"{value!r}: use java or python")
    return language


# --- run --------------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    codebase = args.codebase.resolve()
    if not codebase.is_dir():
        sys.exit(f"secpat-interaction-order: --codebase {args.codebase} is not a folder")
    if args.cpg_dir:
        config.JOERN_CPG_ROOT = args.cpg_dir.resolve()
    mappings = _index(_json_list(args.roles, "--roles"))
    if not mappings:
        sys.exit('secpat-interaction-order: --roles names no role (each row needs "role")')
    behaviour = _json_list(args.behaviour, "--behaviour") if args.behaviour else []

    started = time.monotonic()
    result = detect(
        codebase.name, args.language, mappings,
        behaviour=behaviour, entry=args.entry, repo_path=codebase,
    )
    order = _envelope(result, codebase.name, args.language, mappings)
    _report(order, len(mappings), time.monotonic() - started)
    if args.output:
        _write(args.output, order)
    return 0 if result.available else 1


def _json_list(value: str, flag: str) -> list[dict]:
    """A JSON list given inline, or read from a file as ``@FILE``."""
    try:
        text = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
        rows = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"secpat-interaction-order: {flag}: {exc}")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        sys.exit(f"secpat-interaction-order: {flag} must be a JSON list of objects")
    return rows


def _report(order: dict, roles: int, seconds: float) -> None:
    interactions = order["interactions"]
    if not order["available"]:
        print(f"no interaction order: {order['reason']}")
        return
    print(
        f"derived {len(interactions)} interaction(s) from {roles} role(s), "
        f"entry={order['entryRole'] or 'none'} ({seconds:.1f} s)"
    )
    width = max((len(i["interaction"]) for i in interactions), default=0)
    for i in interactions:
        print(f"  {i['rank']}. {i['interaction']:<{width}}  {i['file']}:{i['line']}  {i['mechanism']}")
    if not interactions and order["reason"]:
        print(order["reason"])


# --- dataset ----------------------------------------------------------------------------


def _dataset(args: argparse.Namespace) -> int:
    root = args.dataset.resolve()
    records = _records(root, args.repo, args.pattern)
    if not records:
        sys.exit(
            f"secpat-interaction-order: no record in {root}/manifest_*.json"
            + (f" for repo {args.repo!r}" if args.repo else "")
            + (f" and pattern {args.pattern!r}" if args.pattern else "")
        )
    config.JOERN_CPG_ROOT = root / "codebase" / "cpg"

    same = 0
    started = time.monotonic()
    for n, record in enumerate(records, 1):
        language, repo, pattern_name = record["language"], record["repo"], record["pattern"]
        slug = f"{pattern_name}__{repo}"
        truth = _load(root / "ground_truth" / language / f"{slug}.json")
        pattern = _load(root / "security_patterns" / pattern_name / "04_pattern.json")
        order = derive_record(
            root / "codebase" / "repos" / language / repo, language,
            truth.get("role_mappings") or [], pattern, label=pattern_name,
        )
        stored = truth.get("interaction_order") or {}
        matches = all(order.get(key) == stored.get(key) for key in _COMPARED)
        same += matches
        count = len(order["interactions"])
        verdict = "same as the dataset" if matches else "DIFFERENT from the dataset"
        print(f"[{n}/{len(records)}] {slug}: {count} interaction(s), {verdict}", flush=True)
        if not matches:
            print(f"    dataset: {_tokens(stored)}\n    derived: {_tokens(order)}", flush=True)
            if order["reason"]:
                print(f"    {order['reason']}", flush=True)
        if args.output:
            _write(args.output / language / f"{slug}.json", order)

    print(
        f"{same} of {len(records)} record(s) give the same interaction order as the dataset "
        f"({_duration(time.monotonic() - started)})."
    )
    return 0 if same == len(records) else 1


def _records(root: Path, repo: str, pattern: str) -> list[dict]:
    """The records of the dataset's manifests, Java's before Python's."""
    records: list[dict] = []
    for manifest in sorted(root.glob("manifest_*.json")):
        data = _load(manifest)
        records += data["runs"] if isinstance(data, dict) else data
    return [
        r for r in records
        if (not repo or r["repo"] == repo) and (not pattern or r["pattern"] == pattern)
    ]


def derive_record(
    codebase: Path, language: str, rows: list[dict], pattern: dict | None, *, label: str = ""
) -> dict:
    """The ``interaction_order`` of one dataset record: its role rows, in its repository.

    Drops the roles the pattern marks ``required: false`` and the pattern's steps that
    involve them, then traverses, as secpatrec's ``detect_action_sequence`` does. Fewer
    than two roles found in code give no interactions, without starting the traversal.
    """
    codebase = codebase.resolve()
    mappings = _index(rows)
    walked, behaviour = scored(pattern, mappings)
    grounded = [role for role, row in walked.items() if _is_grounded(row)]
    if len(grounded) < 2:
        result = DetectionResult(reason=f"{len(grounded)} role(s) found in code; an interaction needs two.")
        return _envelope(result, codebase.name, language, mappings)
    result = detect(
        codebase.name, language, walked, behaviour=behaviour, repo_path=codebase, label=label
    )
    return _envelope(result, codebase.name, language, mappings)


def scored(pattern: dict | None, mappings: dict[str, dict]) -> tuple[dict[str, dict], list[dict]]:
    """``mappings`` and the pattern's steps without the roles marked ``required: false``.

    Only an explicit ``false`` drops a role. A step is dropped when either end is a dropped
    role. The same as secpat-scoring's ``scored_mappings``.
    """
    optional = {
        name
        for role in (pattern or {}).get("roles") or []
        if role.get("required") is False and (name := (role.get("name") or "").strip())
    }
    walked = {role: row for role, row in mappings.items() if role not in optional}
    behaviour = [
        step for step in (pattern or {}).get("behaviour") or []
        if (step.get("from") or "").strip() not in optional
        and (step.get("to") or "").strip() not in optional
    ]
    return walked, behaviour


# --- view -------------------------------------------------------------------------------


def _view(args: argparse.Namespace) -> int:
    from . import view

    target = (args.trace or config.SECPAT_VIEW_DIR).expanduser()
    if args.export:
        if not target.is_file():
            sys.exit(f"secpat-interaction-order: --export needs one trace file, not {target}")
        out = view.export(target, args.export, full_cpg=args.full_view_cpg)
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
        return 0
    if not target.exists():
        if args.trace is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            sys.exit(f"secpat-interaction-order: {target} does not exist")
    try:
        view.serve(target, host=args.host, port=args.port, idle_exit=args.idle_exit, full_cpg=args.full_view_cpg)
    except OSError as exc:
        sys.exit(f"secpat-interaction-order: cannot listen on {args.host}:{args.port}: {exc}")
    return 0


# --- shared -----------------------------------------------------------------------------


def _index(rows: list[dict]) -> dict[str, dict]:
    """Role rows by role name (``role``, or the dataset's ``patternRole``); the first row wins."""
    indexed: dict[str, dict] = {}
    for row in rows:
        role = (row.get("role") or row.get("patternRole") or "").strip()
        if role and role not in indexed:
            indexed[role] = row
    return indexed


def _is_grounded(row: dict) -> bool:
    """Whether a role was found in code and says where: a file, and an entity or lines."""
    status = (row.get("status") or "present").strip().lower()
    return (
        status in _DETECTED
        and bool((row.get("file") or "").strip())
        and bool((row.get("entity") or "").strip() or str(row.get("lines") or "").strip())
    )


def _envelope(result: DetectionResult, repo: str, language: str, mappings: dict[str, dict]) -> dict:
    """A result in the shape of the dataset's ``interaction_order``."""
    anchored = dict(result.anchored_roles)
    return {
        "available": result.available,
        "language": language,
        "repo": repo,
        "reason": result.reason,
        "entryRole": result.entry_role,
        "entryRule": result.entry_rule,
        "interactions": [
            {"rank": rank, **interaction.to_dict()}
            for rank, interaction in enumerate(result.interactions, start=1)
        ],
        "anchoredRoles": [{"role": role, "kind": kind} for role, kind in sorted(anchored.items())],
        "skippedRoles": sorted(set(mappings) - set(anchored)),
    }


def _duration(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min"


def _tokens(order: dict) -> str:
    return ", ".join(i.get("interaction", "") for i in order.get("interactions") or []) or "(none)"


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"secpat-interaction-order: {exc}")


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
