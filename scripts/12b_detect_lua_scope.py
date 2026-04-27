#!/usr/bin/env python3
"""
12b_detect_lua_scope.py

Automatically identifies which functions in a stripped binary are likely part
of the embedded Lua VM, so that 13_select_seed_anchors.py can gate seed
selection to only that subset — preventing game-code functions from polluting
seed anchors.

Algorithm
---------
1. STRING SIGNALS
   Scan every function's `strings` feature for Lua-specific runtime patterns
   ("attempt to ", "stack overflow", "Lua 5.", "__index", ...).
   These functions are high-confidence Lua scope seeds.

2. CALLEE / CALLER BFS
   Expand outward from string-signal functions through the callgraph (both
   callee and caller directions) up to --bfs-depth hops.
   All reached functions join the "likely Lua" scope.

3. SIZE FILTER (optional)
   Functions larger than --max-pcode-instructions are skipped even if reached
   by BFS (very large functions are almost always game logic, not Lua VM).

Output JSON
-----------
{
  "scope_version": "0.1",
  "total_functions": 16769,
  "lua_scope_count": 850,
  "string_signal_count": 45,
  "bfs_hop_counts": {1: 200, 2: 400, ...},
  "functions": {
    "FUN_004a7141": {"confidence": "high",   "reason": "lua_string_signal"},
    "FUN_004a1fae": {"confidence": "medium", "reason": "bfs_hop_1"},
    ...
  }
}

Usage
-----
  python scripts/12b_detect_lua_scope.py \\
      --query-json  data/runtime/query_features/.../libengine.json \\
      --output-json data/runtime/results/artale_libengine_lua536/lua_scope.json \\
      --bfs-depth 4 \\
      --max-pcode-instructions 8000
"""

from __future__ import annotations

import argparse
import json
from collections import deque, defaultdict
from pathlib import Path

# ── Lua runtime string signatures ─────────────────────────────────────────────
# These patterns appear in strings embedded in Lua VM functions (error messages,
# metamethod names, library version strings, etc.).  A function that directly
# references one of these is almost certainly inside the Lua VM.
LUA_STRING_SIGNALS: list[str] = [
    # Runtime error messages
    "attempt to ",
    "stack overflow",
    "not enough memory",
    "bad argument",
    "value expected",
    "table index is",
    "perform arithmetic",
    "perform bitwise",
    "perform comparison",
    "get length of",
    "concatenate",
    "call a ",
    "global '",
    "upvalue '",
    "field '",
    "method '",
    # VM / state markers
    "Lua 5.",
    "LUA_",
    "_ENV",
    "_VERSION",
    # Metamethod names
    "__index",
    "__newindex",
    "__call",
    "__add",
    "__sub",
    "__mul",
    "__div",
    "__mod",
    "__pow",
    "__unm",
    "__len",
    "__concat",
    "__eq",
    "__lt",
    "__le",
    "__gc",
    "__tostring",
    "__metatable",
    # Standard library markers
    "stdin",
    "stdout",
    "stderr",
    "cannot open",
    "cannot close",
    # Coroutine / threading
    "cannot resume",
    "cannot yield",
    "thread",
    # Parser / lexer
    "<eof>",
    "<string>",
    "<name>",
    "unexpected symbol",
    "'=' expected",
    "'(' expected",
    "'end' expected",
    "chunk",
]

# Compiled lower-case set for fast substring check
_LUA_SIGS_LOWER: list[str] = [s.lower() for s in LUA_STRING_SIGNALS]


def _has_lua_string(strings: list[str]) -> bool:
    """Return True if any string in the list matches a Lua signature."""
    for s in strings:
        sl = s.lower()
        if any(sig in sl for sig in _LUA_SIGS_LOWER):
            return True
    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Detect likely Lua VM function scope in a stripped binary."
    )
    p.add_argument("--query-json", type=Path, required=True,
                   help="Query feature JSON produced by the Ghidra extractor.")
    p.add_argument("--output-json", type=Path, required=True,
                   help="Where to write the scope JSON.")
    p.add_argument("--bfs-depth", type=int, default=4,
                   help="How many callgraph hops to expand from string-signal functions. "
                        "Default 4 is conservative; increase for very modular Lua builds.")
    p.add_argument("--max-pcode-instructions", type=int, default=8000,
                   help="Functions larger than this are excluded even if reached by BFS. "
                        "Very large functions are almost always game code. Default 8000.")
    p.add_argument("--min-string-signals", type=int, default=1,
                   help="Minimum number of Lua string signals to classify a function as "
                        "a high-confidence seed (default 1).")
    return p.parse_args()


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    args = parse_args()

    print(f"[12b] Loading query features: {args.query_json}")
    raw = load_json(args.query_json)
    funcs: list[dict] = raw if isinstance(raw, list) else raw.get("functions", [])
    print(f"[12b] Total functions in binary: {len(funcs)}")

    # Index by function_name for fast lookup
    name_to_func: dict[str, dict] = {f["function_name"]: f for f in funcs}

    # ── Step 1: String signal detection ───────────────────────────────────────
    string_seeds: set[str] = set()
    for f in funcs:
        pcode = f.get("pcode_instruction_count", 0) or 0
        if pcode > args.max_pcode_instructions:
            continue
        strings = f.get("strings", [])
        sig_count = sum(
            1 for s in strings
            if any(sig in s.lower() for sig in _LUA_SIGS_LOWER)
        )
        if sig_count >= args.min_string_signals:
            string_seeds.add(f["function_name"])

    print(f"[12b] String-signal seeds: {len(string_seeds)}")

    # ── Step 2: BFS callgraph expansion ───────────────────────────────────────
    # Build adjacency: callee → set of callers, caller → set of callees
    # (bidirectional BFS)
    callees_of: dict[str, list[str]] = defaultdict(list)
    callers_of: dict[str, list[str]] = defaultdict(list)
    for f in funcs:
        fname = f["function_name"]
        for ce in f.get("callees", []):
            if ce in name_to_func:
                callees_of[fname].append(ce)
                callers_of[ce].append(fname)

    scope: dict[str, dict] = {}

    # Seed functions: high confidence
    for name in string_seeds:
        f = name_to_func[name]
        scope[name] = {
            "confidence": "high",
            "reason": "lua_string_signal",
            "hop": 0,
        }

    # BFS
    frontier: deque[tuple[str, int]] = deque(
        (name, 0) for name in string_seeds
    )
    hop_counts: dict[int, int] = defaultdict(int)

    while frontier:
        current, depth = frontier.popleft()
        if depth >= args.bfs_depth:
            continue

        neighbors = callees_of[current] + callers_of[current]
        for neighbor in neighbors:
            if neighbor in scope:
                continue
            f = name_to_func.get(neighbor)
            if f is None:
                continue
            pcode = f.get("pcode_instruction_count", 0) or 0
            if pcode > args.max_pcode_instructions:
                continue

            hop = depth + 1
            confidence = "medium" if hop <= 2 else "low"
            scope[neighbor] = {
                "confidence": confidence,
                "reason": f"bfs_hop_{hop}",
                "hop": hop,
            }
            hop_counts[hop] += 1
            frontier.append((neighbor, hop))

    print(f"[12b] Lua scope size after BFS: {len(scope)} functions")
    for h in sorted(hop_counts):
        print(f"       hop {h}: +{hop_counts[h]}")

    # ── Step 3: Build output ───────────────────────────────────────────────────
    output = {
        "scope_version": "0.1",
        "query_json": str(args.query_json),
        "total_functions": len(funcs),
        "lua_scope_count": len(scope),
        "string_signal_count": len(string_seeds),
        "bfs_depth": args.bfs_depth,
        "max_pcode_instructions": args.max_pcode_instructions,
        "bfs_hop_counts": {str(k): v for k, v in sorted(hop_counts.items())},
        "functions": scope,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[12b] Saved scope: {args.output_json}")
    coverage = len(scope) / len(funcs) * 100 if funcs else 0
    print(f"[12b] Coverage: {coverage:.1f}% of binary functions flagged as likely Lua")


if __name__ == "__main__":
    main()
