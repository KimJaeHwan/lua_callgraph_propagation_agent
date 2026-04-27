#!/usr/bin/env python3
"""
Full pipeline runner for artale_libengine_lua536 (image-base-fixed features).
Runs: 12 retrieval → 13 seed anchors → 14 suite → 04 propagation → 05 deferred → 15 final report
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PYTHON = sys.executable  # use same Python that launched this script
RESULTS = ROOT / "data" / "runtime" / "results" / "artale_libengine_lua536"
QUERY_JSON = ROOT / "data" / "runtime" / "query_features" / "artale_libengine_lua536" / "Lua_536" / "x86_64" / "O2" / "stripped" / "libengine_20260427_102236.json"
INDEX = ROOT / "data" / "inputs" / "retrieval_indexes" / "Lua_536" / "x86_64" / "runtime"
REF_DB = ROOT / "data" / "inputs" / "callgraphs" / "Lua_536" / "reference_callgraph.sqlite"

RETRIEVAL_JSON = RESULTS / "retrieval_result.json"
SEED_JSON = RESULTS / "seed_anchors.json"
SUITE_JSON = RESULTS / "suite.json"
PROP_JSON = RESULTS / "propagation_result.json"
DEFERRED_JSON = RESULTS / "deferred_analysis.json"
FINAL_JSON = RESULTS / "final_mapping_report.json"


def run(label, args, timeout=1200):
    print(f"\n{'='*60}")
    print(f"[{label}] Starting...")
    t0 = time.time()
    result = subprocess.run(
        [PYTHON] + args,
        capture_output=False,
        cwd=str(ROOT),
    )
    elapsed = time.time() - t0
    status = "OK" if result.returncode == 0 else f"FAILED (rc={result.returncode})"
    print(f"[{label}] {status} — {elapsed:.1f}s")
    if result.returncode != 0:
        print("ERROR — stopping pipeline.")
        sys.exit(1)


# ── 12: retrieval ────────────────────────────────────────────
run("12_retrieval", [
    "scripts/12_run_bulk_query_retrieval.py",
    "--query-json", str(QUERY_JSON),
    "--index", str(INDEX),
    "--output-json", str(RETRIEVAL_JSON),
])

# ── 13: seed anchors ─────────────────────────────────────────
run("13_seed_anchors", [
    "scripts/13_select_seed_anchors.py",
    "--retrieval-json", str(RETRIEVAL_JSON),
    "--output-json", str(SEED_JSON),
    "--query-json", str(QUERY_JSON),
    "--reference-db", str(REF_DB),
    "--min-top1-score", "0.92",
    "--min-margin", "0.05",
])

# ── 14: build suite ──────────────────────────────────────────
run("14_suite", [
    "scripts/14_build_runtime_propagation_suite.py",
    "--retrieval-json", str(RETRIEVAL_JSON),
    "--anchor-json", str(SEED_JSON),
    "--output-json", str(SUITE_JSON),
    "--lua-version", "Lua_536",
    "--reference-db", str(REF_DB),
    "--propagation-output-json", str(PROP_JSON),
])

# Patch lua_version_override into suite.json
import json
suite = json.loads(SUITE_JSON.read_text(encoding="utf-8"))
suite.setdefault("scoring", {})["lua_version_override"] = "Lua_536"
SUITE_JSON.write_text(json.dumps(suite, indent=2, ensure_ascii=False), encoding="utf-8")
print("[patch] lua_version_override = Lua_536 added to suite.json")

# ── 04: propagation ──────────────────────────────────────────
run("04_propagation", [
    "scripts/04_propagate_from_anchors.py",
    "--suite-json", str(SUITE_JSON),
], timeout=3600)

# ── 05: deferred ─────────────────────────────────────────────
run("05_deferred", [
    "scripts/05_build_deferred_analysis.py",
    "--propagation-json", str(PROP_JSON),
    "--retrieval-json", str(RETRIEVAL_JSON),
    "--output-json", str(DEFERRED_JSON),
])

# ── 15: final report ─────────────────────────────────────────
run("15_final_report", [
    "scripts/15_export_final_mapping_report.py",
    "--propagation-json", str(PROP_JSON),
    "--deferred-json", str(DEFERRED_JSON),
    "--output-json", str(FINAL_JSON),
    "--session-name", "artale_libengine_lua536",
])

print("\n" + "="*60)
print("Pipeline complete!")

# Quick summary
report = json.loads(FINAL_JSON.read_text(encoding="utf-8"))
s = report.get("summary", {})
print(f"  accepted : {s.get('accepted')}")
print(f"  deferred : {s.get('deferred')}")
print(f"  conflict : {s.get('conflict')}")
