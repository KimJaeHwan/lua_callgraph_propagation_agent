from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_runtime_paths(paths: dict) -> dict[str, Any]:
    """Mirror of default_runtime_paths() in scripts/10_run_name_mapping_pipeline.py.
    Fills in conventional output paths from session_name / lua_version / architecture
    so that configs only need to specify the fields that differ from convention.
    """
    session = paths.get("session_name", "runtime_session")
    lua_version = paths.get("target_lua_version") or "Lua_547"
    architecture = paths.get("target_architecture") or "x86_64"
    normalized_arch = "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"
    result_root = f"data/runtime/results/{session}"
    query_root = f"data/runtime/query_features/{session}"

    defaults: dict[str, Any] = {
        "session_name": session,
        "target_lua_version": lua_version,
        "target_architecture": architecture,
        "extract_manifest_json": f"{query_root}/extract_manifest.json",
        "query_feature_json": "",
        "retrieval_index": f"data/inputs/retrieval_indexes/{lua_version}/{normalized_arch}/runtime",
        "retrieval_output_json": f"{result_root}/retrieval_result.json",
        "seed_anchor_json": f"{result_root}/seed_anchors.json",
        "runtime_suite_json": f"{result_root}/runtime_propagation_suite.json",
        "reference_db": f"data/inputs/callgraphs/{lua_version}/reference_callgraph.sqlite",
        "embedding_project_root": ".",
        "propagation_output_json": f"{result_root}/propagation_result.json",
        "deferred_output_json": f"{result_root}/deferred_analysis.json",
        "final_report_json": f"{result_root}/final_mapping_report.json",
    }

    merged = defaults.copy()
    for key, value in paths.items():
        if value not in (None, ""):
            merged[key] = value
    return merged

mcp = FastMCP(
    name="lua-callgraph-propagation-agent",
    instructions=(
        "Run the deterministic Lua name-mapping runtime pipeline for analysis targets, "
        "inspect final mapping reports, and register force anchors when decompile analysis "
        "reveals a confident mapping for deferred cases."
    ),
    version="0.4.0",
)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "ok": completed.returncode == 0,
    }


@mcp.tool(description="Resolve and preview the full name-mapping pipeline from one config JSON.")
def pipeline_dry_run(config_path: str) -> dict[str, Any]:
    return _run_command(
        [
            sys.executable,
            "scripts/10_run_name_mapping_pipeline.py",
            "--config",
            str(_resolve_path(config_path)),
            "--dry-run",
        ]
    )


@mcp.tool(description="Run the full deterministic name-mapping pipeline from one config JSON.")
def pipeline_run(config_path: str, stop_on_error: bool = False) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/10_run_name_mapping_pipeline.py",
        "--config",
        str(_resolve_path(config_path)),
    ]
    if stop_on_error:
        command.append("--stop-on-error")
    return _run_command(command)


@mcp.tool(description="Extract query features for one target binary into the runtime workspace.")
def extract_query_features(
    binary: str,
    lua_version: str,
    architecture: str,
    session_name: str,
    opt_level: str = "O2",
    strip_mode: str = "nostrip",
) -> dict[str, Any]:
    return _run_command(
        [
            sys.executable,
            "scripts/11_extract_query_features.py",
            "--binary",
            str(_resolve_path(binary)),
            "--lua-version",
            lua_version,
            "--architecture",
            architecture,
            "--session-name",
            session_name,
            "--opt-level",
            opt_level,
            "--strip-mode",
            strip_mode,
        ]
    )


@mcp.tool(description="Run hybrid retrieval for all functions from one extracted feature manifest or JSON.")
def bulk_query_retrieval(
    index: str,
    output_json: str,
    extract_manifest: str | None = None,
    query_json: str | None = None,
    candidate_pool: int = 200,
    topk: int = 50,
    scoring_mode: str = "bonus_v2",
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/12_run_bulk_query_retrieval.py",
        "--index",
        str(_resolve_path(index)),
        "--output-json",
        str(_resolve_path(output_json)),
        "--candidate-pool",
        str(candidate_pool),
        "--topk",
        str(topk),
        "--scoring-mode",
        scoring_mode,
    ]
    if extract_manifest:
        command.extend(["--extract-manifest", str(_resolve_path(extract_manifest))])
    if query_json:
        command.extend(["--query-json", str(_resolve_path(query_json))])
    return _run_command(command)



@mcp.tool(description="List all deferred and conflict cases from the final mapping report for triage.")
def list_deferred_cases(report_json: str) -> dict[str, Any]:
    path = _resolve_path(report_json)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "report_json": str(path),
        "summary": data.get("summary"),
        "deferred": [
            {
                "case_id": row.get("case_id"),
                "query_func": row.get("query_func"),
                "predicted_function_name": row.get("predicted_function_name"),
                "status_reasons": row.get("status_reasons"),
                "propagation_round": row.get("propagation_round"),
            }
            for row in data.get("deferred", [])
        ],
        "conflicts": [
            {
                "case_id": row.get("case_id"),
                "query_func": row.get("query_func"),
                "predicted_function_name": row.get("predicted_function_name"),
                "status_reasons": row.get("status_reasons"),
            }
            for row in data.get("conflicts", [])
        ],
    }


@mcp.tool(description="Read one final mapping report and return its summary plus a small accepted/deferred/conflict preview.")
def read_final_report(report_json: str) -> dict[str, Any]:
    path = _resolve_path(report_json)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "report_json": str(path),
        "summary": data.get("summary"),
        "accepted_preview": data.get("accepted", [])[:5],
        "deferred_preview": data.get("deferred", [])[:5],
        "conflicts_preview": data.get("conflicts", [])[:5],
    }


@mcp.tool(description="Read one mapping record from the final mapping report by case_id for reverse validation and follow-up analysis.")
def read_mapping_record(report_json: str, case_id: str) -> dict[str, Any]:
    path = _resolve_path(report_json)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for key in ("mapping_records", "accepted", "deferred", "conflicts"):
        rows = data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("case_id") == case_id:
                return {
                    "report_json": str(path),
                    "source_section": key,
                    "record": row,
                }

    return {
        "report_json": str(path),
        "source_section": None,
        "record": None,
        "error": f"case_id not found: {case_id}",
    }


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@mcp.tool(
    description=(
        "Force-register a manually confirmed mapping as a seed anchor after decompile analysis, "
        "then re-run propagation and regenerate the final report. "
        "Use this when decompiled code analysis reveals a confident answer for a deferred case. "
        "query_func is the stripped function name (e.g. sub_401234), "
        "reference_func is the confirmed Lua function name (e.g. luaD_precall), "
        "reason should summarize the decompile evidence used to make this decision."
    )
)
def register_force_anchor(
    config_path: str,
    query_func: str,
    reference_func: str,
    reason: str,
) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config.get("paths", {}))

    anchor_path = _resolve_path(paths["seed_anchor_json"])
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    anchor_data = _load_json(anchor_path)

    # 중복 등록 방지
    for mapping in anchor_data.get("mappings", []):
        if mapping.get("query_function_name") == query_func:
            return {
                "ok": False,
                "error": f"already registered: {query_func} → {mapping.get('reference_function_name')}",
            }

    anchor_data.setdefault("mappings", []).append({
        "query_function_name": query_func,
        "reference_function_name": reference_func,
        "confidence": 1.0,
        "source": "force_anchor",
        "status": "accepted",
        "evidence": [f"force_registered_by_llm_decompile_analysis: {reason}"],
    })
    _save_json(anchor_path, anchor_data)

    # build_runtime_suite → propagation (iterative) → deferred_analysis → final_report 재실행
    steps: list[dict[str, Any]] = []

    def run(name: str, cmd: list[str]) -> bool:
        result = _run_command(cmd)
        result["step"] = name
        steps.append(result)
        return result["ok"]

    suite_json = _resolve_path(paths["runtime_suite_json"])
    propagation_json = _resolve_path(paths["propagation_output_json"])
    deferred_json = _resolve_path(paths["deferred_output_json"])
    final_json = _resolve_path(paths["final_report_json"])
    reference_db = _resolve_path(paths["reference_db"])
    embedding_root = _resolve_path(paths.get("embedding_project_root", "."))
    retrieval_json = _resolve_path(paths["retrieval_output_json"])
    session_name = paths["session_name"]
    deferred_top_candidates = str(config.get("steps", {}).get("deferred_analysis", {}).get("top_candidates", 5))

    if not run("build_runtime_suite", [
        sys.executable, "scripts/14_build_runtime_propagation_suite.py",
        "--retrieval-json", str(retrieval_json),
        "--anchor-json", str(anchor_path),
        "--reference-db", str(reference_db),
        "--output-json", str(suite_json),
        "--embedding-project-root", str(embedding_root),
        "--propagation-output-json", str(propagation_json),
    ]):
        return {"ok": False, "registered_anchor": f"{query_func} → {reference_func}", "steps": steps}

    if not run("propagation", [
        sys.executable, "scripts/04_propagate_from_anchors.py",
        "--suite", str(suite_json),
        "--output-json", str(propagation_json),
        "--iterative",
    ]):
        return {"ok": False, "registered_anchor": f"{query_func} → {reference_func}", "steps": steps}

    if not run("deferred_analysis", [
        sys.executable, "scripts/05_build_deferred_analysis.py",
        "--input-json", str(propagation_json),
        "--embedding-root", str(embedding_root),
        "--output-json", str(deferred_json),
        "--top-candidates", deferred_top_candidates,
    ]):
        return {"ok": False, "registered_anchor": f"{query_func} → {reference_func}", "steps": steps}

    if not run("final_report", [
        sys.executable, "scripts/15_export_final_mapping_report.py",
        "--propagation-json", str(propagation_json),
        "--deferred-json", str(deferred_json),
        "--output-json", str(final_json),
        "--session-name", session_name,
    ]):
        return {"ok": False, "registered_anchor": f"{query_func} → {reference_func}", "steps": steps}

    # 재실행 후 결과 요약
    updated_report = _load_json(final_json)
    summary = updated_report.get("summary", {})

    return {
        "ok": True,
        "registered_anchor": f"{query_func} → {reference_func}",
        "reason": reason,
        "updated_summary": summary,
        "report_json": str(final_json),
        "steps": [{"step": s["step"], "ok": s["ok"], "returncode": s["returncode"]} for s in steps],
    }


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


__all__ = ["main", "mcp"]
