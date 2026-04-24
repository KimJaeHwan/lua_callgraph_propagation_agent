#!/usr/bin/env python3
"""
Unified config loader for the lua_callgraph_propagation_agent pipeline.

Supports two config formats:

  New format  — session_name + extraction{} / analysis{} at top level.
  Legacy format — paths{} + steps{} nested dicts.

All scripts and mcp_server.py should import from here so path resolution
logic lives in exactly one place.

Usage (from any script):
    from config_loader import load_config, resolve_paths, is_new_format
    config = load_config("data/configs/runtime_recommended_binary.json")
    paths  = resolve_paths(config)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> dict:
    """Load and return a pipeline config JSON."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_new_format(config: dict) -> bool:
    """Return True when config uses the new session_name + extraction/analysis layout."""
    return "session_name" in config and ("extraction" in config or "analysis" in config)


def resolve_paths(config: dict) -> dict[str, Any]:
    """Return the full set of runtime paths for *config*.

    Works with both new-format and legacy-format configs.  The returned dict
    always contains the same keys regardless of format, so callers don't need
    to branch on format themselves.
    """
    if is_new_format(config):
        return _paths_new(config)
    return _paths_legacy(config)


def deferred_top_candidates(config: dict) -> int:
    """Return the top_candidates setting for the deferred-analysis step."""
    v = (
        config.get("analysis", {}).get("deferred_analysis", {}).get("top_candidates")
        or config.get("steps", {}).get("deferred_analysis", {}).get("top_candidates")
        or 5
    )
    return int(v)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_COMMON_KEYS = (
    "session_name",
    "lua_version",
    "architecture",
    "arch_norm",
    "extract_manifest_json",
    "query_feature_json",
    "retrieval_index",
    "retrieval_output_json",
    "seed_anchor_json",
    "runtime_suite_json",
    "reference_db",
    "embedding_project_root",
    "propagation_output_json",
    "deferred_output_json",
    "final_report_json",
    "extractor_work_root",
    "query_feature_output_root",
    "extractor_script",
    "retrieval_script",
)


def _build_defaults(session: str, lua_version: str, architecture: str) -> dict[str, Any]:
    arch_norm   = "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"
    result_root = f"data/runtime/results/{session}"
    query_root  = f"data/runtime/query_features/{session}"
    return {
        "session_name":              session,
        "lua_version":               lua_version,
        "architecture":              architecture,
        "arch_norm":                 arch_norm,
        "extract_manifest_json":     f"{query_root}/extract_manifest.json",
        "query_feature_json":        "",
        "retrieval_index":           f"data/inputs/retrieval_indexes/{lua_version}/{arch_norm}/runtime",
        "retrieval_output_json":     f"{result_root}/retrieval_result.json",
        "seed_anchor_json":          f"{result_root}/seed_anchors.json",
        "runtime_suite_json":        f"{result_root}/runtime_propagation_suite.json",
        "reference_db":              f"data/inputs/callgraphs/{lua_version}/reference_callgraph.sqlite",
        "embedding_project_root":    ".",
        "propagation_output_json":   f"{result_root}/propagation_result.json",
        "deferred_output_json":      f"{result_root}/deferred_analysis.json",
        "final_report_json":         f"{result_root}/final_mapping_report.json",
        "extractor_work_root":       "data/runtime/extractor_workspace",
        "query_feature_output_root": "data/runtime/query_features",
        "extractor_script":          "src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py",
        "retrieval_script":          "src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py",
    }


def _paths_new(config: dict) -> dict[str, Any]:
    """Resolve paths from new-format config (session_name at top level)."""
    session      = config.get("session_name", "runtime_session")
    analysis     = config.get("analysis", {})
    extraction   = config.get("extraction", {})
    lua_version  = analysis.get("lua_version") or extraction.get("lua_version") or "Lua_547"
    architecture = analysis.get("architecture") or extraction.get("architecture") or "x86_64"
    return _build_defaults(session, lua_version, architecture)


def _paths_legacy(config: dict) -> dict[str, Any]:
    """Resolve paths from legacy-format config (paths{} sub-dict)."""
    p            = config.get("paths", config)
    session      = p.get("session_name", "runtime_session")
    lua_version  = p.get("target_lua_version") or "Lua_547"
    architecture = p.get("target_architecture") or "x86_64"
    defaults     = _build_defaults(session, lua_version, architecture)
    # legacy format allows arbitrary path overrides inside paths{}
    merged = defaults.copy()
    for key, value in p.items():
        if value not in (None, ""):
            merged[key] = value
    return merged
