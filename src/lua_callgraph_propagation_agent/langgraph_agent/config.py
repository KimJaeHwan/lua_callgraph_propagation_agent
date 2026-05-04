"""Config loading helpers for the LangGraph agent layer.

This mirrors the runtime path keys used by scripts/config_loader.py while keeping
package imports resolvable for type checkers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_target_lua_version(config: dict[str, Any]) -> str:
    paths = resolve_paths(config)
    return str(
        paths.get("target_lua_version")
        or paths.get("lua_version")
        or config.get("analysis", {}).get("lua_version")
        or config.get("extraction", {}).get("lua_version")
        or "Lua_547"
    )


def resolve_target_architecture(config: dict[str, Any]) -> str:
    paths = resolve_paths(config)
    return str(
        paths.get("target_architecture")
        or paths.get("architecture")
        or config.get("analysis", {}).get("architecture")
        or config.get("extraction", {}).get("architecture")
        or "x86_64"
    )


def resolve_paths(config: dict[str, Any]) -> dict[str, Any]:
    if "session_name" in config and ("extraction" in config or "analysis" in config):
        return _paths_new(config)
    return _paths_legacy(config)


def _build_defaults(session: str, lua_version: str, architecture: str) -> dict[str, Any]:
    arch_norm = "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"
    result_root = f"data/runtime/results/{session}"
    query_root = f"data/runtime/query_features/{session}"
    return {
        "session_name": session,
        "lua_version": lua_version,
        "architecture": architecture,
        "arch_norm": arch_norm,
        "extract_manifest_json": f"{query_root}/extract_manifest.json",
        "query_feature_json": "",
        "retrieval_index": f"data/inputs/retrieval_indexes/{lua_version}/{arch_norm}/runtime",
        "retrieval_output_json": f"{result_root}/retrieval_result.json",
        "seed_anchor_json": f"{result_root}/seed_anchors.json",
        "runtime_suite_json": f"{result_root}/runtime_propagation_suite.json",
        "reference_db": f"data/inputs/callgraphs/{lua_version}/reference_callgraph.sqlite",
        "embedding_project_root": ".",
        "propagation_output_json": f"{result_root}/propagation_result.json",
        "deferred_output_json": f"{result_root}/deferred_analysis.json",
        "final_report_json": f"{result_root}/final_mapping_report.json",
    }


def _paths_new(config: dict[str, Any]) -> dict[str, Any]:
    session = config.get("session_name", "runtime_session")
    analysis = config.get("analysis", {})
    extraction = config.get("extraction", {})
    lua_version = analysis.get("lua_version") or extraction.get("lua_version") or "Lua_547"
    architecture = analysis.get("architecture") or extraction.get("architecture") or "x86_64"
    return _build_defaults(session, lua_version, architecture)


def _paths_legacy(config: dict[str, Any]) -> dict[str, Any]:
    paths = config.get("paths", config)
    session = paths.get("session_name", "runtime_session")
    lua_version = paths.get("target_lua_version") or "Lua_547"
    architecture = paths.get("target_architecture") or "x86_64"
    merged = _build_defaults(session, lua_version, architecture)
    for key, value in paths.items():
        if value not in (None, ""):
            merged[key] = value
    return merged
