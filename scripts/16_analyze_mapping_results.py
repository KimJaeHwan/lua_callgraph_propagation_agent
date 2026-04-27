#!/usr/bin/env python3
"""
Analyze propagation/final mapping results.

Provides three sub-commands:
  summary   -- accepted/deferred/conflict counts + round log
  dist      -- reference function name distribution (1:1 vs duplicates)
  trusted   -- export high-confidence mappings (mapping_count <= N, no FUN_/sub_ names)

Examples
--------
  python scripts/16_analyze_mapping_results.py summary  --result-dir data/runtime/results/artale_libengine_lua536
  python scripts/16_analyze_mapping_results.py dist     --result-dir data/runtime/results/artale_libengine_lua536
  python scripts/16_analyze_mapping_results.py trusted  --result-dir data/runtime/results/artale_libengine_lua536 --max-count 3 --output-json data/runtime/results/artale_libengine_lua536/trusted_mappings.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_query_json(result_dir: Path) -> Path | None:
    """Locate the most recent query feature JSON referenced in the final report."""
    report_path = result_dir / "final_mapping_report.json"
    if not report_path.exists():
        return None
    report = load_json(report_path)
    # grab query_file from first accepted entry
    for entry in report.get("accepted", []):
        qf = entry.get("query_file")
        if qf:
            return Path(qf)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# sub-commands
# ──────────────────────────────────────────────────────────────────────────────

def cmd_summary(args: argparse.Namespace) -> None:
    result_dir = Path(args.result_dir)
    prop_path = result_dir / "propagation_result.json"
    d = load_json(prop_path)
    s = d.get("summary", {})

    print("=== Propagation Summary ===")
    print(f"  accepted : {s.get('accepted')}")
    print(f"  deferred : {s.get('deferred')}")
    print(f"  conflict : {s.get('conflict')}")
    print()
    print("Round log:")
    for r in d.get("round_log", []):
        print(" ", r)

    results = d.get("results", [])
    lua_names = [
        r for r in results
        if r.get("status") == "accepted"
        and r.get("predicted_function_name")
        and not r["predicted_function_name"].startswith("FUN_")
        and not r["predicted_function_name"].startswith("sub_")
    ]
    print(f"\nAccepted with real Lua names: {len(lua_names)}")
    for r in lua_names[:15]:
        print(f"  {r['query_func']} -> {r['predicted_function_name']}")


def cmd_dist(args: argparse.Namespace) -> None:
    result_dir = Path(args.result_dir)
    prop_path = result_dir / "propagation_result.json"
    d = load_json(prop_path)
    results = d.get("results", [])

    accepted = [r for r in results if r.get("status") == "accepted"]

    name_to_queries: dict[str, list[str]] = {}
    for r in accepted:
        ref_name = r.get("predicted_function_name", "")
        query = r.get("query_func", "")
        name_to_queries.setdefault(ref_name, []).append(query)

    count_dist = Counter(len(v) for v in name_to_queries.values())
    print("=== Reference function → query function count distribution ===")
    for n in sorted(count_dist):
        if n == 1:
            label = "<- high confidence"
        elif n <= 3:
            label = "<- caution"
        else:
            label = "<- suspicious"
        print(f"  {n:3d} queries -> {count_dist[n]:5d} ref functions  {label}")

    print(f"\nTotal unique reference names : {len(name_to_queries)}")
    print(f"Total accepted query funcs   : {len(accepted)}")

    unique = {k: v[0] for k, v in name_to_queries.items() if len(v) == 1}
    print(f"\n[High confidence] 1:1 mappings: {len(unique)}")
    for ref, q in list(unique.items())[:15]:
        print(f"  {q} -> {ref}")

    high_dup = {k: v for k, v in name_to_queries.items() if len(v) >= 5}
    print(f"\n[Suspicious] >= 5 queries mapped to same ref: {len(high_dup)}")
    for ref, queries in sorted(high_dup.items(), key=lambda x: -len(x[1]))[:20]:
        print(f"  '{ref}' <- {len(queries)} funcs: {queries[:5]}")


def cmd_trusted(args: argparse.Namespace) -> None:
    result_dir = Path(args.result_dir)
    prop_path = result_dir / "propagation_result.json"
    d = load_json(prop_path)
    results = d.get("results", [])

    # build addr map from query feature JSON
    addr_map: dict[str, str] = {}
    query_json_path = find_query_json(result_dir)
    if query_json_path and query_json_path.exists():
        funcs = load_json(query_json_path)
        if isinstance(funcs, list):
            addr_map = {f["function_name"]: f.get("entry_point", "") for f in funcs}

    name_to_queries: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r.get("status") == "accepted":
            name_to_queries[r["predicted_function_name"]].append(r)

    max_count = args.max_count
    exclude_prefixes = tuple(args.exclude_prefixes.split(","))

    trusted = []
    for ref_name, rows in name_to_queries.items():
        if len(rows) <= max_count and not ref_name.startswith(exclude_prefixes):
            for r in rows:
                query_func = r["query_func"]
                entry_point = addr_map.get(query_func, "")
                top = (r.get("top_candidates") or [{}])[0]
                trusted.append({
                    "query_func": query_func,
                    "entry_point": entry_point,
                    "predicted_name": ref_name,
                    "mapping_count": len(rows),
                    "final_score": top.get("final_score", 0),
                    "propagation_round": r.get("propagation_round", -1),
                })

    trusted.sort(key=lambda x: (-int(x["mapping_count"] == 1), -x["final_score"]))

    print(f"Trusted mappings (mapping_count <= {max_count}, real Lua names): {len(trusted)}")
    unique = [t for t in trusted if t["mapping_count"] == 1]
    print(f"  1:1 unique: {len(unique)}")
    print()
    for t in unique[:25]:
        print(f"  0x{t['entry_point']}  {t['query_func']:40s} -> {t['predicted_name']}  (score={t['final_score']:.4f})")

    if args.output_json:
        out = Path(args.output_json)
        out.write_text(json.dumps(trusted, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved: {out}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze Lua name-mapping pipeline results."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- summary --
    p_sum = sub.add_parser("summary", help="Show accepted/deferred/conflict counts and round log.")
    p_sum.add_argument("--result-dir", required=True, help="Path to results directory")

    # -- dist --
    p_dist = sub.add_parser("dist", help="Show mapping count distribution.")
    p_dist.add_argument("--result-dir", required=True, help="Path to results directory")

    # -- trusted --
    p_trust = sub.add_parser("trusted", help="Export high-confidence trusted mappings.")
    p_trust.add_argument("--result-dir", required=True, help="Path to results directory")
    p_trust.add_argument("--max-count", type=int, default=3,
                         help="Max mapping_count to consider trusted (default: 3)")
    p_trust.add_argument("--exclude-prefixes", default="FUN_,sub_",
                         help="Comma-separated name prefixes to exclude (default: FUN_,sub_)")
    p_trust.add_argument("--output-json", default=None,
                         help="If set, save trusted list to this JSON file")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "summary":
        cmd_summary(args)
    elif args.command == "dist":
        cmd_dist(args)
    elif args.command == "trusted":
        cmd_trusted(args)


if __name__ == "__main__":
    main()
