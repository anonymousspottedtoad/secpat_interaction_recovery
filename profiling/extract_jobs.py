#!/usr/bin/env python3
"""Capture the exact walk parameters of every SecPat draft into one JSONL job file.

Each of the 502 drafts in /workspace/claude-recovery-result/MANIFEST.csv goes through
the same code path the 2026-11-09 batch run used (cpg-backfill/scripts/derive_orders.py
-> secpatrec stage 3 -> secpat_interaction_order.detect). The Joern backend is replaced
by a capture that records the (roles, behaviour, entry) parameters detect would have
posted, so the jobs replay exactly what the published orders were derived from.

Run with the cpg-backfill venv (it has secpat_scoring / secpatrec's deps):

    /workspace/cpg-backfill/venv/bin/python -I profiling/extract_jobs.py

Writes profiling/jobs/jobs.jsonl, one line per draft that reaches Joern:
    {id, language, pattern, repo, frontend, cpg, cpg_bytes, input, roles, behaviour, entry,
     golden}  -- golden is the published order file of that draft.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
BASE = Path("/workspace/cpg-backfill")
spec = importlib.util.spec_from_file_location("derive_orders", BASE / "scripts/derive_orders.py")
do = importlib.util.module_from_spec(spec)
sys.modules["derive_orders"] = do
spec.loader.exec_module(do)
from secpat_interaction_order import joern_service  # noqa: E402

GOLDEN = BASE / "orders"


def main() -> int:
    rows = list(csv.DictReader((do.RESULTS / "MANIFEST.csv").open()))
    scratch = Path(tempfile.mkdtemp(prefix="extract-jobs-"))
    captured: list[dict] = []

    def capture(repo, language, script, params=None):
        captured.append(dict(params or {}))
        return None

    joern_service.query = capture
    do.out_path = lambda row: scratch / row["language"] / row["pattern"] / f"{row['repo']}.json"
    jobs, skipped = [], 0
    for r in rows:
        captured.clear()
        do.run_one(r)
        if not captured:          # fewer than two grounded roles, or no CPG: never reaches Joern
            skipped += 1
            continue
        assert len(captured) == 1, (r["repo"], r["pattern"], len(captured))
        p = captured[0]
        lang, repo = r["language"], r["repo"]
        cpg = do.STORE / f"{repo}__{do.FE[lang]}.cpg"
        jobs.append({
            "id": f"{lang}/{r['pattern']}/{repo}",
            "language": lang,
            "pattern": r["pattern"],
            "repo": repo,
            "frontend": do.FE[lang],
            "cpg": str(cpg),
            "cpg_bytes": cpg.stat().st_size,
            "input": str(do.ROOTS[lang] / repo),
            "roles": p["roles"],
            "behaviour": p["behaviour"],
            "entry": p["entry"],
            "golden": str(GOLDEN / lang / r["pattern"] / f"{repo}.json"),
        })
    out = HERE / "jobs" / "jobs.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for j in sorted(jobs, key=lambda j: (j["cpg_bytes"], j["id"])):
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    print(f"{len(jobs)} job(s) over {len({j['repo'] for j in jobs})} repo(s); "
          f"{skipped} draft(s) never reach Joern -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
