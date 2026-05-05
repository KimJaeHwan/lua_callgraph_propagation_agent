"""Config loading helpers shared by the LangGraph agent layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]

_CONFIG_META_KEYS = {
    "__config_path",
    "__config_dir",
    "__project_root",
}

_PATH_KEYS = {
    "target_binary",
    "extractor_script",
    "extractor_work_root",
    "query_feature_output_root",
    "extract_manifest_json",
    "query_feature_json",
    "retrieval_script",
    "retrieval_index",
    "retrieval_output_json",
    "seed_anchor_json",
    "runtime_suite_json",
    "reference_feature_root",
    "reference_db",
    "ida_signature_db",
    "vanilla_lua_source_root",
    "manual_force_anchors_json",
    "embedding_project_root",
    "propagation_suite",
    "propagation_output_json",
    "deferred_output_json",
    "final_report_json",
}


def load_config(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path).resolve()
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    data["__config_path"] = str(cfg_path)
    data["__config_dir"] = str(cfg_path.parent)
    data["__project_root"] = str(PROJECT_ROOT.resolve())
    return data


def resolve_target_lua_version(config: dict[str, Any]) -> str:
    paths = resolve_paths(config)
    return str(
        paths.get("target_lua_version")
        or paths.get("lua_version")
        or _deep_get(config, "user_input", "lua_version")
        or _deep_get(config, "target", "lua_version")
        or config.get("analysis", {}).get("lua_version")
        or config.get("extraction", {}).get("lua_version")
        or "Lua_547"
    )


def resolve_target_architecture(config: dict[str, Any]) -> str:
    paths = resolve_paths(config)
    return str(
        paths.get("target_architecture")
        or paths.get("architecture")
        or _deep_get(config, "user_input", "architecture")
        or _deep_get(config, "target", "architecture")
        or config.get("analysis", {}).get("architecture")
        or config.get("extraction", {}).get("architecture")
        or "x86_64"
    )


def resolve_paths(config: dict[str, Any]) -> dict[str, Any]:
    if _is_legacy_format(config):
        paths = _paths_legacy(config)
    else:
        paths = _paths_modern(config)
    return _normalize_paths(paths, config)


def _is_legacy_format(config: dict[str, Any]) -> bool:
    return "paths" in config


def _config_project_root(config: dict[str, Any]) -> Path:
    raw = config.get("__project_root")
    if raw:
        return Path(str(raw))
    return PROJECT_ROOT.resolve()


def _deep_get(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _target_block(config: dict[str, Any]) -> dict[str, Any]:
    target = config.get("user_input") or config.get("target") or {}
    extraction = config.get("extraction") or {}
    analysis = config.get("analysis") or {}
    legacy_paths = config.get("paths") or {}
    return {
        "binary": (
            target.get("binary")
            or extraction.get("binary")
            or legacy_paths.get("target_binary")
            or ""
        ),
        "lua_version": (
            target.get("lua_version")
            or config.get("lua_version")
            or analysis.get("lua_version")
            or extraction.get("lua_version")
            or legacy_paths.get("target_lua_version")
            or "Lua_547"
        ),
        "architecture": (
            target.get("architecture")
            or config.get("architecture")
            or analysis.get("architecture")
            or extraction.get("architecture")
            or legacy_paths.get("target_architecture")
            or "x86_64"
        ),
        "opt_level": (
            target.get("opt_level")
            or extraction.get("opt_level")
            or legacy_paths.get("target_opt_level")
            or "O2"
        ),
        "strip_mode": (
            target.get("strip_mode")
            or extraction.get("strip_mode")
            or legacy_paths.get("target_strip_mode")
            or "stripped"
        ),
        "feature_namespace": (
            target.get("feature_namespace")
            or _deep_get(config, "runtime", "feature_namespace")
            or _deep_get(config, "inputs", "feature_namespace")
            or legacy_paths.get("feature_namespace")
            or ""
        ),
    }


def _session_name(config: dict[str, Any], target: dict[str, Any]) -> str:
    explicit = config.get("session_name")
    if explicit:
        return str(explicit)
    binary = str(target.get("binary") or "")
    stem = Path(binary).stem if binary else "runtime"
    version = str(target.get("lua_version") or "Lua_547").replace("_", "").lower()
    arch = _normalize_arch(str(target.get("architecture") or "x86_64"))
    return f"{stem}_{version}_{arch}_run"


def _normalize_arch(architecture: str) -> str:
    return "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"


def _feature_namespace(session: str, target: dict[str, Any]) -> str:
    explicit = str(target.get("feature_namespace") or "").strip()
    if explicit:
        return explicit
    binary = str(target.get("binary") or "")
    stem = Path(binary).stem if binary else session
    version = str(target.get("lua_version") or "Lua_547").replace("_", "").lower()
    arch = _normalize_arch(str(target.get("architecture") or "x86_64"))
    return f"{stem}_{version}_{arch}"


def _build_defaults(
    *,
    session: str,
    target: dict[str, Any],
    results_root: str,
    query_features_root: str,
    extractor_work_root: str,
) -> dict[str, Any]:
    lua_version = str(target.get("lua_version") or "Lua_547")
    architecture = str(target.get("architecture") or "x86_64")
    arch_norm = _normalize_arch(architecture)
    feature_namespace = _feature_namespace(session, target)
    result_root = f"{results_root.rstrip('/')}/{session}"
    feature_root = f"{query_features_root.rstrip('/')}/{feature_namespace}"
    return {
        "session_name": session,
        "lua_version": lua_version,
        "architecture": architecture,
        "arch_norm": arch_norm,
        "target_binary": str(target.get("binary") or ""),
        "target_lua_version": lua_version,
        "target_architecture": architecture,
        "target_opt_level": str(target.get("opt_level") or "O2"),
        "target_strip_mode": str(target.get("strip_mode") or "stripped"),
        "feature_namespace": feature_namespace,
        "extractor_script": "src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py",
        "extractor_work_root": extractor_work_root,
        "query_feature_output_root": query_features_root,
        "extract_manifest_json": f"{feature_root}/extract_manifest.json",
        "query_feature_json": "",
        "retrieval_script": "src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py",
        "retrieval_index": f"data/inputs/retrieval_indexes/{lua_version}/{arch_norm}/runtime",
        "retrieval_output_json": f"{result_root}/retrieval_result.json",
        "seed_anchor_json": f"{result_root}/seed_anchors.json",
        "runtime_suite_json": f"{result_root}/runtime_propagation_suite.json",
        "reference_feature_root": "data/inputs/reference_features",
        "reference_db": f"data/inputs/callgraphs/{lua_version}/reference_callgraph.sqlite",
        "ida_signature_db": "data/inputs/ida_types/lua_function_signatures.sqlite",
        "vanilla_lua_source_root": "../lua_custom_engine_generator/lua_source_vanilla",
        "manual_force_anchors_json": f"{result_root}/manual_force_anchors.json",
        "embedding_project_root": ".",
        "propagation_suite": "",
        "propagation_output_json": f"{result_root}/propagation_result.json",
        "deferred_output_json": f"{result_root}/deferred_analysis.json",
        "final_report_json": f"{result_root}/final_mapping_report.json",
    }


def _paths_modern(config: dict[str, Any]) -> dict[str, Any]:
    target = _target_block(config)
    session = _session_name(config, target)
    runtime = config.get("runtime") or {}
    user_input = config.get("user_input") or config.get("target") or {}
    inputs = config.get("inputs") or {}
    analysis = config.get("analysis") or {}
    tooling = config.get("tooling") or {}
    managed = config.get("managed_paths") or {}
    defaults = _build_defaults(
        session=session,
        target=target,
        results_root=str(runtime.get("results_root") or "data/runtime/results"),
        query_features_root=str(runtime.get("query_features_root") or "data/runtime/query_features"),
        extractor_work_root=str(runtime.get("extractor_work_root") or "data/runtime/extractor_workspace"),
    )
    overrides = {
        "query_feature_json": (
            user_input.get("query_feature_json")
            or user_input.get("query_json")
            or inputs.get("query_feature_json")
            or analysis.get("query_json")
            or managed.get("query_feature_json")
            or ""
        ),
        "extract_manifest_json": managed.get("extract_manifest_json") or "",
        "retrieval_index": managed.get("retrieval_index") or "",
        "reference_db": managed.get("reference_db") or "",
        "manual_force_anchors_json": managed.get("manual_force_anchors_json") or "",
        "ida_signature_db": tooling.get("ida_signature_db") or managed.get("ida_signature_db") or "",
        "vanilla_lua_source_root": (
            tooling.get("vanilla_lua_source_root")
            or managed.get("vanilla_lua_source_root")
            or ""
        ),
    }
    merged = defaults.copy()
    for key, value in overrides.items():
        if value not in (None, ""):
            merged[key] = value
    if not merged.get("query_feature_json"):
        manifest = merged.get("extract_manifest_json")
        auto_query = _query_json_from_manifest(manifest, config)
        if auto_query:
            merged["query_feature_json"] = auto_query
    return merged


def _paths_legacy(config: dict[str, Any]) -> dict[str, Any]:
    paths = config.get("paths", config)
    session = str(paths.get("session_name") or "runtime_session")
    target = {
        "binary": str(paths.get("target_binary") or ""),
        "lua_version": str(paths.get("target_lua_version") or "Lua_547"),
        "architecture": str(paths.get("target_architecture") or "x86_64"),
        "opt_level": str(paths.get("target_opt_level") or "O2"),
        "strip_mode": str(paths.get("target_strip_mode") or "stripped"),
        "feature_namespace": str(paths.get("feature_namespace") or ""),
    }
    defaults = _build_defaults(
        session=session,
        target=target,
        results_root="data/runtime/results",
        query_features_root=str(paths.get("query_feature_output_root") or "data/runtime/query_features"),
        extractor_work_root=str(paths.get("extractor_work_root") or "data/runtime/extractor_workspace"),
    )
    merged = defaults.copy()
    for key, value in paths.items():
        if value not in (None, ""):
            merged[key] = value
    if not merged.get("query_feature_json"):
        manifest = merged.get("extract_manifest_json")
        auto_query = _query_json_from_manifest(manifest, config)
        if auto_query:
            merged["query_feature_json"] = auto_query
    return merged


def _query_json_from_manifest(manifest: str | Path | None, config: dict[str, Any]) -> str:
    if not manifest:
        return ""
    manifest_path = _to_abs_path(str(manifest), _config_project_root(config))
    if not manifest_path.exists():
        return ""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    feature_files = data.get("feature_files") or []
    if not feature_files:
        return ""
    return str(_to_abs_path(str(feature_files[0]), _config_project_root(config)))


def _normalize_paths(paths: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    project_root = _config_project_root(config)
    normalized = {}
    for key, value in paths.items():
        if key in _CONFIG_META_KEYS:
            continue
        if key in _PATH_KEYS and isinstance(value, str) and value:
            normalized[key] = str(_to_abs_path(value, project_root))
        else:
            normalized[key] = value
    return normalized


def _to_abs_path(raw: str, project_root: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()
