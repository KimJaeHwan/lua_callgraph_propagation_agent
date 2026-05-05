#!/usr/bin/env python3
"""Build a SQLite catalog of Lua function signatures from vanilla sources."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lua_callgraph_propagation_agent.langgraph_agent.signature_db import (  # noqa: E402
    build_signature_db,
    extract_signatures_for_version,
    resolve_signature_db_path,
    resolve_vanilla_source_root,
    resolve_vanilla_src_dir,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-db",
        type=Path,
        default=PROJECT_ROOT / "data/inputs/ida_types/lua_function_signatures.sqlite",
        help="SQLite DB output path.",
    )
    parser.add_argument(
        "--vanilla-root",
        type=Path,
        default=(PROJECT_ROOT.parent / "lua_custom_engine_generator" / "lua_source_vanilla"),
        help="Versioned vanilla Lua source root.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing DB if present. Without this flag, keep an existing DB as-is.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Show discovered counts per version without writing a DB.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = ("Lua_524", "Lua_536", "Lua_547")
    source_root = resolve_vanilla_source_root(str(args.vanilla_root))
    if args.list_only:
        for lua_version in versions:
            src_dir = resolve_vanilla_src_dir(lua_version, str(source_root))
            rows = extract_signatures_for_version(lua_version, src_dir)
            print(f"{lua_version}: {len(rows)} signatures from {src_dir}")
        return 0

    db_path = resolve_signature_db_path(str(args.output_db))
    if db_path.exists() and not args.replace:
        print(f"exists: {db_path}")
        print("use --replace to rebuild")
        return 0
    build_signature_db(db_path, vanilla_source_root=str(source_root), replace=True)
    print(f"built: {db_path}")
    con = sqlite3.connect(str(db_path))
    try:
        for lua_version in versions:
            count = con.execute(
                "SELECT COUNT(*) FROM function_signatures WHERE lua_version = ?",
                (lua_version,),
            ).fetchone()[0]
            print(f"  {lua_version}: {count}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
