#!/usr/bin/env python3
"""
Build a SQLite reference call graph DB from vanilla Lua feature JSON files.

Input:
  ../lua_extract_feature_ghidra/outputs_vanilla/

Output:
  data/inputs/callgraphs/reference_callgraph.sqlite

Typical commands from the lua_callgraph_propagation_agent project root:

  # Build or replace the DB from the default vanilla feature directory.
  python3 scripts/01_build_reference_callgraph_db.py --replace

  # Show target JSON files without creating a DB.
  python3 scripts/01_build_reference_callgraph_db.py --list-only

  # Build with explicit paths.
  python3 scripts/01_build_reference_callgraph_db.py \
    --input-root ../lua_extract_feature_ghidra/outputs_vanilla \
    --output-db data/inputs/callgraphs/reference_callgraph.sqlite \
    --replace

Design notes:
  - The call graph is stored as an edge-list, not as a tree.
  - O0 is treated later as the primary reference graph.
  - O1/O2/O3/Os are loaded into the same DB as auxiliary tolerance graphs.
  - Callees that are not present as internal functions in the same feature JSON
    are preserved as external/unresolved nodes instead of being dropped.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_ROOT = (PROJECT_ROOT / ".." / "lua_extract_feature_ghidra" / "outputs_vanilla").resolve()
DEFAULT_OUTPUT_DB = PROJECT_ROOT / "data" / "inputs" / "callgraphs" / "reference_callgraph.sqlite"
SCHEMA_VERSION = "0.1"


@dataclass(frozen=True)
class FeatureFile:
    path: Path
    lua_version: str
    architecture: str
    opt_level: str
    strip_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build reference_callgraph.sqlite from vanilla Lua feature JSON files."
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help="root directory containing vanilla feature JSON files",
    )
    parser.add_argument(
        "--output-db",
        type=Path,
        default=DEFAULT_OUTPUT_DB,
        help="SQLite DB output path",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace output DB if it already exists",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="list detected feature JSON files without creating a DB",
    )
    return parser.parse_args()


def discover_feature_files(input_root: Path) -> list[FeatureFile]:
    files: list[FeatureFile] = []
    for path in sorted(input_root.rglob("*.json")):
        try:
            rel = path.relative_to(input_root)
        except ValueError:
            continue

        if len(rel.parts) < 5:
            continue

        lua_version, architecture, opt_level, strip_mode = rel.parts[:4]
        if not lua_version.startswith("Lua_"):
            continue
        if architecture not in {"aarch64", "arm64", "x86_64"}:
            continue
        if not opt_level.startswith("O"):
            continue
        if strip_mode not in {"nostrip", "stripped"}:
            continue

        normalized_arch = "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"
        files.append(
            FeatureFile(
                path=path,
                lua_version=lua_version,
                architecture=normalized_arch,
                opt_level=opt_level,
                strip_mode=strip_mode,
            )
        )

    return files


def ref_function_id(meta: FeatureFile, function_name: str) -> str:
    return (
        f"ref::{meta.lua_version}::{meta.architecture}::"
        f"{meta.opt_level}::{meta.strip_mode}::{function_name}"
    )


def external_function_id(meta: FeatureFile, function_name: str) -> str:
    return (
        f"external::{meta.lua_version}::{meta.architecture}::"
        f"{meta.opt_level}::{meta.strip_mode}::{function_name}"
    )


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;

        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE functions (
          function_id TEXT PRIMARY KEY,
          function_name TEXT NOT NULL,
          graph_role TEXT NOT NULL,
          lua_version TEXT,
          architecture TEXT,
          opt_level TEXT,
          strip_mode TEXT,
          entry_point TEXT,
          source_json TEXT
        );

        CREATE TABLE edges (
          edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
          src_id TEXT NOT NULL,
          dst_id TEXT NOT NULL,
          src_name TEXT NOT NULL,
          dst_name TEXT NOT NULL,
          edge_type TEXT NOT NULL DEFAULT 'calls',
          graph_role TEXT NOT NULL,
          lua_version TEXT,
          architecture TEXT,
          opt_level TEXT,
          strip_mode TEXT,
          source_json TEXT
        );

        CREATE UNIQUE INDEX idx_edges_unique
        ON edges(src_id, dst_id, edge_type, graph_role, lua_version, architecture, opt_level, strip_mode);

        CREATE INDEX idx_edges_src ON edges(src_id);
        CREATE INDEX idx_edges_dst ON edges(dst_id);
        CREATE INDEX idx_edges_src_name_env ON edges(src_name, lua_version, architecture, opt_level, strip_mode);
        CREATE INDEX idx_edges_dst_name_env ON edges(dst_name, lua_version, architecture, opt_level, strip_mode);
        CREATE INDEX idx_functions_name_env ON functions(function_name, lua_version, architecture, opt_level, strip_mode);
        """
    )


def insert_metadata(conn: sqlite3.Connection, input_root: Path) -> None:
    rows = [
        ("schema_version", SCHEMA_VERSION),
        ("source", "lua_extract_feature_ghidra.outputs_vanilla"),
        ("input_root", str(input_root)),
        ("graph_role", "reference"),
        ("storage_model", "sqlite_edge_list"),
    ]
    conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", rows)


def load_feature_json(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"feature JSON must be a list: {path}")
    return data


def insert_feature_file(conn: sqlite3.Connection, meta: FeatureFile, input_root: Path) -> tuple[int, int, int]:
    rows = load_feature_json(meta.path)
    source_json = str(meta.path.relative_to(input_root))
    internal_names = {row.get("function_name") for row in rows if row.get("function_name")}

    function_count = 0
    edge_count = 0
    unresolved_count = 0

    for row in rows:
        function_name = row.get("function_name")
        if not function_name:
            continue

        function_id = ref_function_id(meta, function_name)
        conn.execute(
            """
            INSERT OR IGNORE INTO functions(
              function_id, function_name, graph_role, lua_version, architecture,
              opt_level, strip_mode, entry_point, source_json
            )
            VALUES (?, ?, 'reference', ?, ?, ?, ?, ?, ?)
            """,
            (
                function_id,
                function_name,
                meta.lua_version,
                meta.architecture,
                meta.opt_level,
                meta.strip_mode,
                row.get("entry_point"),
                source_json,
            ),
        )
        function_count += 1

    for row in rows:
        src_name = row.get("function_name")
        if not src_name:
            continue

        src_id = ref_function_id(meta, src_name)
        callees = row.get("callees") or []
        for dst_name in sorted({name for name in callees if name}):
            if dst_name in internal_names:
                dst_id = ref_function_id(meta, dst_name)
            else:
                dst_id = external_function_id(meta, dst_name)
                unresolved_count += 1
                conn.execute(
                    """
                    INSERT OR IGNORE INTO functions(
                      function_id, function_name, graph_role, lua_version, architecture,
                      opt_level, strip_mode, entry_point, source_json
                    )
                    VALUES (?, ?, 'reference_external', ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        dst_id,
                        dst_name,
                        meta.lua_version,
                        meta.architecture,
                        meta.opt_level,
                        meta.strip_mode,
                        source_json,
                    ),
                )

            conn.execute(
                """
                INSERT OR IGNORE INTO edges(
                  src_id, dst_id, src_name, dst_name, edge_type, graph_role,
                  lua_version, architecture, opt_level, strip_mode, source_json
                )
                VALUES (?, ?, ?, ?, 'calls', 'reference', ?, ?, ?, ?, ?)
                """,
                (
                    src_id,
                    dst_id,
                    src_name,
                    dst_name,
                    meta.lua_version,
                    meta.architecture,
                    meta.opt_level,
                    meta.strip_mode,
                    source_json,
                ),
            )
            edge_count += 1

    return function_count, edge_count, unresolved_count


def build_db(input_root: Path, output_db: Path, replace: bool) -> None:
    files = discover_feature_files(input_root)
    if not files:
        print(f"[ERROR] no vanilla feature JSON files found: {input_root}")
        sys.exit(2)

    if output_db.exists():
        if not replace:
            print(f"[ERROR] output DB already exists: {output_db}")
            print("        Use --replace to rebuild it.")
            sys.exit(3)
        output_db.unlink()

    output_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(output_db)

    total_functions = 0
    total_edges = 0
    total_unresolved = 0
    env_counter: Counter[tuple[str, str, str, str]] = Counter()

    try:
        create_schema(conn)
        insert_metadata(conn, input_root)

        for feature_file in files:
            function_count, edge_count, unresolved_count = insert_feature_file(
                conn,
                feature_file,
                input_root,
            )
            total_functions += function_count
            total_edges += edge_count
            total_unresolved += unresolved_count
            env_counter[
                (
                    feature_file.lua_version,
                    feature_file.architecture,
                    feature_file.opt_level,
                    feature_file.strip_mode,
                )
            ] += function_count

        conn.commit()
    finally:
        conn.close()

    print(f"[OK] wrote DB: {output_db}")
    print(f"Feature files : {len(files)}")
    print(f"Functions     : {total_functions}")
    print(f"Edges         : {total_edges}")
    print(f"External edges: {total_unresolved}")
    print("By environment:")
    for (lua_version, arch, opt, strip), count in sorted(env_counter.items()):
        print(f"  {lua_version:7} {arch:7} {opt:2} {strip:7} functions={count}")


def list_files(input_root: Path) -> None:
    files = discover_feature_files(input_root)
    print(f"Input root: {input_root}")
    print(f"Feature files: {len(files)}")
    for feature_file in files:
        print(feature_file.path.relative_to(input_root))


def main() -> None:
    args = parse_args()
    input_root = args.input_root.resolve()
    output_db = args.output_db.resolve()

    if not input_root.exists():
        print(f"[ERROR] input root does not exist: {input_root}")
        sys.exit(2)

    if args.list_only:
        list_files(input_root)
        return

    build_db(input_root, output_db, args.replace)


if __name__ == "__main__":
    main()
