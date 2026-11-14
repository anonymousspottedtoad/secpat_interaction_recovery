#!/usr/bin/env python3
"""Compare a run's raw walk outputs with the published orders (or with another run).

    python3 profiling/compare_golden.py RUN [--against golden|RUN2] [--show 5]

RUN (and RUN2) are run names under profiling/results/, or paths to any folder holding
`<id with / replaced by __>.result.json` files (e.g. a Rust engine's outputs).

Two outputs agree when entryRole, entryRule, anchoredRoles and the interaction list
(rank, from, to, file, line, via, mechanism, path, parent) are identical. The golden
side is the secpatrec envelope in /workspace/cpg-backfill/orders, whose `interactions`
and `anchoredRoles` carry walk.sc's own fields.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def norm_interactions(rows):
    out = []
    for r in rows or []:
        out.append({k: r.get(k) for k in ("rank", "from", "to", "file", "line", "via", "mechanism", "path")}
                   | {"parent": r.get("parent") or ""})
    return out


def norm_anchored(rows):
    keys = ("role", "kind", "bodies", "by", "file", "line")
    return [{k: r.get(k) for k in keys if k in r} for r in rows or []]


def load_raw(path: Path) -> dict | None:
    if not path.is_file():
        return None
    d = json.loads(path.read_text())
    return {"entryRole": d.get("entryRole", ""), "entryRule": d.get("entryRule", ""),
            "anchoredRoles": norm_anchored(d.get("anchoredRoles")),
            "interactions": norm_interactions(d.get("interactions"))}


def load_golden(job: dict) -> dict | None:
    p = Path(job["golden"])
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    if not d.get("available") and not d.get("interactions"):
        reason = d.get("reason", "")
        if reason.startswith("TIMEOUT"):
            return {"timeout": True}
    return {"entryRole": d.get("entryRole", ""), "entryRule": d.get("entryRule", ""),
            "anchoredRoles": norm_anchored(d.get("anchoredRoles")),
            "interactions": norm_interactions(d.get("interactions"))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--against", default="golden")
    ap.add_argument("--show", type=int, default=5)
    ap.add_argument("--list-diff", default="", help="write the differing job ids to this file")
    a = ap.parse_args()
    jobs = {json.loads(l)["id"]: json.loads(l) for l in (HERE / "jobs" / "jobs.jsonl").open()}
    def folder(name: str) -> Path:
        p = Path(name)
        return p if p.is_dir() and ("/" in name or name.startswith(".")) else HERE / "results" / name
    run_dir = folder(a.run)
    same, diff, missing, golden_timeout, new_from_timeout = [], [], [], [], []
    for jid, job in jobs.items():
        name = jid.replace("/", "__") + ".result.json"
        mine = load_raw(run_dir / name)
        if mine is None:
            missing.append(jid)
            continue
        other = load_golden(job) if a.against == "golden" else load_raw(folder(a.against) / name)
        if other is None:
            missing.append(jid)
            continue
        if other.get("timeout"):
            new_from_timeout.append((jid, len(mine["interactions"])))
            continue
        # anchoredRoles in the golden envelope may hold extra skipped rows: compare the walk's own.
        if a.against == "golden":
            mine_cmp = {k: v for k, v in mine.items() if k != "anchoredRoles"}
            other_cmp = {k: v for k, v in other.items() if k != "anchoredRoles"}
        else:
            mine_cmp, other_cmp = mine, other
        (same if mine_cmp == other_cmp else diff).append(jid)
    if a.list_diff:
        Path(a.list_diff).write_text("\n".join(diff) + ("\n" if diff else ""))
    print(f"{a.run} vs {a.against}: identical {len(same)}, different {len(diff)}, "
          f"no output on one side {len(missing)}, previously timed out (now derived) {len(new_from_timeout)}")
    for jid in diff[:a.show]:
        print("  DIFF", jid)
    for jid, n in new_from_timeout:
        print(f"  NEW  {jid}: {n} interaction(s)")
    return 0 if not diff else 1


if __name__ == "__main__":
    sys.exit(main())
