#!/usr/bin/env python3
"""
Prepare deployable runtime assets inside lua_callgraph_propagation_agent.

This script is a one-time bootstrap helper. It copies reference data from the
research repositories into this repository, including a full retrieval index
copy so the runtime can work without depending on sibling repositories.

Typical usage:

  ./lua_llm/bin/python scripts/21_prepare_runtime_assets.py --force
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent

DEFAULT_REFERENCE_ROOT = WORKSPACE_ROOT / "lua_extract_feature_ghidra" / "outputs_vanilla"
DEFAULT_SOURCE_INDEX = WORKSPACE_ROOT / "lua_function_embedding" / "data" / "indexes" / "lua_547_x86_64_bge"
DEFAULT_SAMPLE_BINARY = (
    WORKSPACE_ROOT
    / "lua_custom_engine_generator"
    / "binaries_vanilla"
    / "Lua_547"
    / "x86_64"
    / "O0"
    / "nostrip"
    / "lua_547_vanilla"
)
DEFAULT_SAMPLE_QUERY_JSON = (
    WORKSPACE_ROOT
    / "lua_extract_feature_ghidra"
    / "outputs"
    / "Lua_547"
    / "x86_64"
    / "O0"
    / "nostrip"
    / "x86_64_O0_nostrip_lua_lua_547_0000_20260405_100336.json"
)

DEFAULT_TARGET_REFERENCE_ROOT = PROJECT_ROOT / "data" / "inputs" / "reference_features"
DEFAULT_TARGET_INDEX_DIR = PROJECT_ROOT / "data" / "inputs" / "retrieval_indexes" / "Lua_547" / "x86_64" / "runtime"
DEFAULT_TARGET_BINARY = PROJECT_ROOT / "data" / "runtime" / "input" / "lua547_x86_64_O0_vanilla_demo"
DEFAULT_TARGET_QUERY_JSON = PROJECT_ROOT / "data" / "inputs" / "query_features" / "lua547_x86_custom_demo.json"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy reference assets and the full retrieval index into this repo."
    )
    parser.add_argument("--source-reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--source-index-dir", type=Path, default=DEFAULT_SOURCE_INDEX)
    parser.add_argument("--source-sample-binary", type=Path, default=DEFAULT_SAMPLE_BINARY)
    parser.add_argument("--source-sample-query-json", type=Path, default=DEFAULT_SAMPLE_QUERY_JSON)
    parser.add_argument("--target-reference-root", type=Path, default=DEFAULT_TARGET_REFERENCE_ROOT)
    parser.add_argument("--target-index-dir", type=Path, default=DEFAULT_TARGET_INDEX_DIR)
    parser.add_argument("--target-sample-binary", type=Path, default=DEFAULT_TARGET_BINARY)
    parser.add_argument("--target-sample-query-json", type=Path, default=DEFAULT_TARGET_QUERY_JSON)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def reset_dir(path: Path, *, force: bool) -> None:
    if path.exists():
        if not force:
            raise SystemExit(f"Path already exists: {path}. Use --force to replace it.")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_tree(src: Path, dst: Path, *, force: bool) -> None:
    if not src.exists():
        raise SystemExit(f"Source directory not found: {src}")
    if dst.exists():
        if not force:
            raise SystemExit(f"Target directory already exists: {dst}. Use --force to replace it.")
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def copy_file(src: Path, dst: Path, *, force: bool) -> None:
    if not src.exists():
        raise SystemExit(f"Source file not found: {src}")
    ensure_parent(dst)
    if dst.exists() and not force:
        raise SystemExit(f"Target file already exists: {dst}. Use --force to replace it.")
    shutil.copy2(src, dst)


def main() -> None:
    args = parse_args()

    target_reference_root = args.target_reference_root.resolve()
    target_index_dir = args.target_index_dir.resolve()
    target_sample_binary = args.target_sample_binary.resolve()
    target_sample_query_json = args.target_sample_query_json.resolve()

    copy_tree(args.source_reference_root.resolve(), target_reference_root, force=args.force)
    copy_file(args.source_sample_binary.resolve(), target_sample_binary, force=args.force)
    copy_file(args.source_sample_query_json.resolve(), target_sample_query_json, force=args.force)
    copy_tree(args.source_index_dir.resolve(), target_index_dir, force=args.force)

    summary = {
        "reference_feature_root": str(target_reference_root),
        "sample_binary": str(target_sample_binary),
        "sample_query_feature_json": str(target_sample_query_json),
        "runtime_retrieval_index": str(target_index_dir),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
