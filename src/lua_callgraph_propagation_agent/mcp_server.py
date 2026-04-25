from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# config_loader는 scripts/ 에 있으므로 경로 추가 후 import
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from config_loader import load_config as _cl_load_config, resolve_paths as _cl_resolve_paths, deferred_top_candidates as _cl_deferred_top_candidates  # noqa: E402


def _default_runtime_paths(config: dict) -> dict[str, Any]:
    """Delegate to config_loader.resolve_paths() — single source of truth."""
    return _cl_resolve_paths(config)

mcp = FastMCP(
    name="lua-callgraph-propagation-agent",
    instructions=(
        "Use this MCP to drive the deterministic Lua runtime name-mapping workflow with "
        "phase-separated execution only. Do not rely on scripts/10_run_name_mapping_pipeline.py "
        "from MCP, because extraction and analysis must stay split to avoid Ghidra JVM and "
        "embedding-model memory overlap. Preferred analyst loop: "
        "(1) extract features with extract_query_features when starting from a binary; "
        "(2) run retrieval and seed/propagation steps explicitly or inspect existing results; "
        "(3) inspect results with read_final_report, list_deferred_cases, "
        "read_mapping_record, read_propagation_summary, or show_candidate_context; "
        "(4) after manual decompile validation, register_force_anchor or "
        "batch_register_force_anchors; "
        "(5) if anchors were edited manually, call run_downstream. "
        "Prefer batch_register_force_anchors over repeated single-anchor calls when several "
        "mappings were confirmed in one reverse-engineering session."
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


@mcp.tool(
    description=(
        "Extract Ghidra/pyghidra features from one target binary into the runtime workspace. "
        "Runs scripts/11_extract_query_features.py as a subprocess so Ghidra JVM is fully "
        "isolated. This MCP intentionally does not expose scripts/10_run_name_mapping_pipeline.py. "
        "binary: absolute path to the .so or ELF binary. "
        "lua_version: e.g. 'Lua_547', 'Lua_536'. "
        "architecture: 'x86_64' or 'aarch64'. "
        "opt_level: compiler optimisation level, e.g. 'O0', 'O2'. "
        "strip_mode: 'stripped' for production binaries with no debug symbols, "
        "'nostrip' for debug/test builds. "
        "Output manifest is written to data/runtime/query_features/<session_name>/extract_manifest.json. "
        "Use this when the caller wants direct parameter control instead of config-driven execution."
    )
)
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


@mcp.tool(
    description=(
        "Run hybrid (embedding + graph) retrieval for every function in an extracted feature set "
        "against a versioned reference index. Produces a ranked top-k list per function. "
        "Provide either extract_manifest (path to extract_manifest.json from extract_query_features) "
        "or query_json (path to a raw feature JSON list). "
        "index: path to the retrieval index directory, e.g. "
        "'data/inputs/retrieval_indexes/Lua_547/x86_64/runtime'. "
        "scoring_mode: 'bonus_v2' is recommended. "
        "output_json: where to write retrieval_result.json. "
        "Use this for retrieval-only experiments or benchmarking without running full propagation."
    )
)
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



@mcp.tool(
    description=(
        "List all deferred and conflict cases from the final mapping report for analyst triage. "
        "Deferred cases had no confident top-1 prediction; conflict cases had competing predictions "
        "that the propagation graph could not resolve. "
        "Use this to decide which functions to inspect in IDA/Ghidra and resolve via "
        "register_force_anchor or batch_register_force_anchors. "
        "report_json: path to final_mapping_report.json. "
        "Good first triage tool when the user asks 'what should we inspect next?'."
    )
)
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


@mcp.tool(
    description=(
        "Read one final mapping report and return its summary (accepted/deferred/conflict counts) "
        "plus a small preview (up to 5 entries) of each bucket. "
        "Use this for a quick sanity check after explicit runtime steps or run_downstream completes. "
        "report_json: path to final_mapping_report.json. "
        "This is the default 'status check' tool after any downstream rerun."
    )
)
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


@mcp.tool(
    description=(
        "Read one mapping record from the final mapping report by case_id for deep inspection. "
        "case_id format is '<function_name>@<hex_address>', e.g. 'sub_401234@00401234'. "
        "Returns the full record including retrieval scores, graph evidence, and status reasons. "
        "Useful for reverse-validating an accepted mapping or understanding why a case was deferred. "
        "Prefer show_candidate_context when you want the mapping record plus triage payload and "
        "query-feature summary in one call."
    )
)
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


def _find_record_by_case_id(report_data: dict[str, Any], case_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for key in ("mapping_records", "accepted", "deferred", "conflicts"):
        rows = report_data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("case_id") == case_id:
                return key, row
    return None, None


def _load_query_feature_summary(query_file: Path, query_func: str) -> dict[str, Any] | None:
    if not query_file.exists():
        return None
    try:
        data = _load_json(query_file)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for row in data:
        if row.get("function_name") != query_func:
            continue
        compare_value = row.get("compare", [])
        if isinstance(compare_value, list):
            compare_value = compare_value[:10]
        read_write_value = row.get("read_write", [])
        if isinstance(read_write_value, list):
            read_write_value = read_write_value[:10]
        return {
            "function_name": row.get("function_name"),
            "entry_point": row.get("entry_point"),
            "architecture": row.get("architecture"),
            "lua_version": row.get("lua_version"),
            "basic_block_count": row.get("basic_block_count"),
            "pcode_instruction_count": row.get("pcode_instruction_count"),
            "strings": row.get("strings", [])[:10],
            "callees": row.get("callees", [])[:10],
            "callers": row.get("callers", [])[:10],
            "struct_offsets": row.get("struct_offsets", [])[:10],
            "compare": compare_value,
            "read_write": read_write_value,
        }
    return None


def _find_deferred_case(deferred_data: dict[str, Any], case_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for key in ("deferred_cases", "conflict_cases"):
        rows = deferred_data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("case_id") == case_id:
                return key, row
    return None, None


def _deferred_top_candidates(config: dict) -> str:
    """Delegate to config_loader.deferred_top_candidates()."""
    return str(_cl_deferred_top_candidates(config))


def _run_downstream_steps(
    *,
    config: dict,
    paths: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """build_suite → propagation → deferred_analysis → final_report 를 순서대로 실행.

    Returns (all_ok, steps) 튜플.  retrieval / seed_selection 은 건드리지 않으므로
    force anchor 를 직접 편집한 뒤 호출해도 anchor 가 덮어써지지 않는다.
    """
    steps: list[dict[str, Any]] = []

    def run(name: str, cmd: list[str]) -> bool:
        result = _run_command(cmd)
        result["step"] = name
        steps.append(result)
        return result["ok"]

    anchor_path      = _resolve_path(paths["seed_anchor_json"])
    suite_json       = _resolve_path(paths["runtime_suite_json"])
    propagation_json = _resolve_path(paths["propagation_output_json"])
    deferred_json    = _resolve_path(paths["deferred_output_json"])
    final_json       = _resolve_path(paths["final_report_json"])
    reference_db     = _resolve_path(paths["reference_db"])
    embedding_root   = _resolve_path(paths.get("embedding_project_root", "."))
    retrieval_json   = _resolve_path(paths["retrieval_output_json"])
    session_name     = paths["session_name"]
    top_candidates   = _deferred_top_candidates(config)

    if not run("build_runtime_suite", [
        sys.executable, "scripts/14_build_runtime_propagation_suite.py",
        "--retrieval-json", str(retrieval_json),
        "--anchor-json", str(anchor_path),
        "--reference-db", str(reference_db),
        "--output-json", str(suite_json),
        "--embedding-project-root", str(embedding_root),
        "--propagation-output-json", str(propagation_json),
    ]):
        return False, steps

    if not run("propagation", [
        sys.executable, "scripts/04_propagate_from_anchors.py",
        "--suite", str(suite_json),
        "--output-json", str(propagation_json),
        "--iterative",
    ]):
        return False, steps

    if not run("deferred_analysis", [
        sys.executable, "scripts/05_build_deferred_analysis.py",
        "--input-json", str(propagation_json),
        "--embedding-root", str(embedding_root),
        "--output-json", str(deferred_json),
        "--top-candidates", top_candidates,
    ]):
        return False, steps

    if not run("final_report", [
        sys.executable, "scripts/15_export_final_mapping_report.py",
        "--propagation-json", str(propagation_json),
        "--deferred-json", str(deferred_json),
        "--output-json", str(final_json),
        "--session-name", session_name,
    ]):
        return False, steps

    return True, steps


@mcp.tool(
    description=(
        "Force-register a manually confirmed mapping as a seed anchor after decompile analysis, "
        "then re-run propagation and regenerate the final report. "
        "Use this when decompiled code analysis reveals a confident answer for a deferred case. "
        "query_func is the stripped function name (e.g. sub_401234), "
        "reference_func is the confirmed Lua function name (e.g. luaD_precall), "
        "reason should summarize the decompile evidence used to make this decision. "
        "To register multiple anchors at once use batch_register_force_anchors instead. "
        "Only use this after manual validation; do not use it for speculative guesses."
    )
)
def register_force_anchor(
    config_path: str,
    query_func: str,
    reference_func: str,
    reason: str,
) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    anchor_path = _resolve_path(paths["seed_anchor_json"])
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    anchor_data = _load_json(anchor_path)

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

    ok, steps = _run_downstream_steps(config=config, paths=paths)
    if not ok:
        return {"ok": False, "registered_anchor": f"{query_func} → {reference_func}", "steps": steps}

    updated_report = _load_json(_resolve_path(paths["final_report_json"]))
    return {
        "ok": True,
        "registered_anchor": f"{query_func} → {reference_func}",
        "reason": reason,
        "updated_summary": updated_report.get("summary", {}),
        "report_json": str(_resolve_path(paths["final_report_json"])),
        "steps": [{"step": s["step"], "ok": s["ok"], "returncode": s["returncode"]} for s in steps],
    }


@mcp.tool(
    description=(
        "Register multiple manually confirmed force anchors at once, then re-run "
        "build_suite → propagation → deferred_analysis → final_report exactly ONCE. "
        "Much more efficient than calling register_force_anchor N times when you have "
        "several deferred/conflict cases resolved in one IDA analysis session. "
        "anchors is a list of {query_func, reference_func, reason} dicts. "
        "Duplicates (query_func already registered) are silently skipped. "
        "This is the preferred anchor-registration tool for batch analyst workflows."
    )
)
def batch_register_force_anchors(
    config_path: str,
    anchors: list[dict[str, str]],
) -> dict[str, Any]:
    """anchors 형식: [{"query_func": "sub_401234", "reference_func": "luaD_precall", "reason": "..."}]"""
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    anchor_path = _resolve_path(paths["seed_anchor_json"])
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    anchor_data = _load_json(anchor_path)
    existing = {m["query_function_name"] for m in anchor_data.get("mappings", [])}

    registered: list[str] = []
    skipped: list[str] = []

    for entry in anchors:
        qf  = entry.get("query_func", "").strip()
        rf  = entry.get("reference_func", "").strip()
        rsn = entry.get("reason", "batch_force_anchor").strip()
        if not qf or not rf:
            continue
        if qf in existing:
            skipped.append(qf)
            continue
        anchor_data.setdefault("mappings", []).append({
            "query_function_name": qf,
            "reference_function_name": rf,
            "confidence": 1.0,
            "source": "force_anchor",
            "status": "accepted",
            "evidence": [f"force_registered_by_llm_decompile_analysis: {rsn}"],
        })
        existing.add(qf)
        registered.append(f"{qf} → {rf}")

    if not registered:
        return {"ok": True, "registered": [], "skipped": skipped, "note": "nothing new to register"}

    _save_json(anchor_path, anchor_data)

    ok, steps = _run_downstream_steps(config=config, paths=paths)
    if not ok:
        return {"ok": False, "registered": registered, "skipped": skipped, "steps": steps}

    updated_report = _load_json(_resolve_path(paths["final_report_json"]))
    return {
        "ok": True,
        "registered": registered,
        "skipped": skipped,
        "updated_summary": updated_report.get("summary", {}),
        "report_json": str(_resolve_path(paths["final_report_json"])),
        "steps": [{"step": s["step"], "ok": s["ok"], "returncode": s["returncode"]} for s in steps],
    }


@mcp.tool(
    description=(
        "Remove one or more force anchors for a query function from seed_anchors.json. "
        "Only anchors with source='force_anchor' are removed; retrieval_high_confidence seeds are preserved. "
        "If rerun_downstream is true, re-run build_suite → propagation → deferred_analysis → final_report "
        "after removal so the report stays in sync. "
        "Use this to undo a bad manual anchor without touching automatic seeds."
    )
)
def remove_force_anchor(
    config_path: str,
    query_func: str,
    rerun_downstream: bool = True,
) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    anchor_path = _resolve_path(paths["seed_anchor_json"])
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    anchor_data = _load_json(anchor_path)
    mappings = anchor_data.get("mappings", [])
    removed = [
        m for m in mappings
        if m.get("query_function_name") == query_func and m.get("source") == "force_anchor"
    ]
    if not removed:
        return {
            "ok": False,
            "error": f"no force_anchor found for query_func: {query_func}",
            "seed_anchor_json": str(anchor_path),
        }

    anchor_data["mappings"] = [
        m for m in mappings
        if not (m.get("query_function_name") == query_func and m.get("source") == "force_anchor")
    ]
    _save_json(anchor_path, anchor_data)

    if not rerun_downstream:
        return {
            "ok": True,
            "removed_count": len(removed),
            "removed": removed,
            "rerun_downstream": False,
            "seed_anchor_json": str(anchor_path),
        }

    ok, steps = _run_downstream_steps(config=config, paths=paths)
    if not ok:
        return {
            "ok": False,
            "removed_count": len(removed),
            "removed": removed,
            "steps": steps,
        }

    updated_report = _load_json(_resolve_path(paths["final_report_json"]))
    return {
        "ok": True,
        "removed_count": len(removed),
        "removed": removed,
        "rerun_downstream": True,
        "updated_summary": updated_report.get("summary", {}),
        "report_json": str(_resolve_path(paths["final_report_json"])),
        "steps": [{"step": s["step"], "ok": s["ok"], "returncode": s["returncode"]} for s in steps],
    }


@mcp.tool(
    description=(
        "Show one analyst-friendly context bundle for a case_id. "
        "Combines the final mapping record, deferred/conflict triage payload, current seed anchor "
        "status, and a compact query feature summary. "
        "Use this before deciding whether to register or remove a force anchor. "
        "This is the best single-call context tool for one deferred or conflict case."
    )
)
def show_candidate_context(config_path: str, case_id: str) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    report_path = _resolve_path(paths["final_report_json"])
    deferred_path = _resolve_path(paths["deferred_output_json"])
    anchor_path = _resolve_path(paths["seed_anchor_json"])

    if not report_path.exists():
        return {"ok": False, "error": f"final_report_json not found: {report_path}"}
    if not deferred_path.exists():
        return {"ok": False, "error": f"deferred_output_json not found: {deferred_path}"}
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    report_data = _load_json(report_path)
    report_section, mapping_record = _find_record_by_case_id(report_data, case_id)
    if not mapping_record:
        return {"ok": False, "error": f"case_id not found: {case_id}", "report_json": str(report_path)}

    deferred_data = _load_json(deferred_path)
    triage_section, triage_case = _find_deferred_case(deferred_data, case_id)

    anchor_data = _load_json(anchor_path)
    query_func = mapping_record.get("query_func")
    matching_anchors = [
        m for m in anchor_data.get("mappings", [])
        if m.get("query_function_name") == query_func
    ]

    query_file = _resolve_path(mapping_record.get("query_file", ""))
    query_summary = _load_query_feature_summary(query_file, query_func) if query_file else None

    compact_candidates = []
    if triage_case:
        for cand in triage_case.get("top_candidates", [])[:5]:
            compact_candidates.append({
                "reference_function_name": cand.get("reference_function_name"),
                "candidate_source": cand.get("candidate_source"),
                "final_score": cand.get("final_score"),
                "retrieval_prior": cand.get("retrieval_prior"),
                "graph_score": cand.get("graph_score"),
                "graph_breakdown": cand.get("graph_breakdown"),
            })

    return {
        "ok": True,
        "case_id": case_id,
        "query_func": query_func,
        "report_json": str(report_path),
        "deferred_json": str(deferred_path),
        "seed_anchor_json": str(anchor_path),
        "mapping_record_source": report_section,
        "mapping_record": mapping_record,
        "triage_case_source": triage_section,
        "triage_case": {
            "review_category": triage_case.get("review_category"),
            "current_top_prediction": triage_case.get("current_top_prediction"),
            "recommended_action": triage_case.get("recommended_action"),
            "score_margin_top1_top2": triage_case.get("score_margin_top1_top2"),
            "anchor_counts": triage_case.get("anchor_counts"),
            "anchors": triage_case.get("anchors"),
            "status_reasons": triage_case.get("status_reasons"),
            "top_candidates": compact_candidates,
        } if triage_case else None,
        "registered_anchors_for_query": matching_anchors,
        "query_feature_summary": query_summary,
    }


@mcp.tool(
    description=(
        "Re-run only the downstream steps (build_suite → propagation → deferred_analysis → final_report) "
        "without touching retrieval or seed_selection. "
        "Use this after manually editing seed_anchors.json, or after batch_register_force_anchors "
        "if you want a fresh run without re-registering anchors. "
        "Critically: does NOT overwrite seed_anchors.json, so force anchors are preserved."
        " This is the standard rerun tool after anchor edits."
    )
)
def run_downstream(config_path: str) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    anchor_path = _resolve_path(paths["seed_anchor_json"])
    if not anchor_path.exists():
        return {"ok": False, "error": f"seed_anchor_json not found: {anchor_path}"}

    ok, steps = _run_downstream_steps(config=config, paths=paths)
    if not ok:
        return {"ok": False, "steps": steps}

    updated_report = _load_json(_resolve_path(paths["final_report_json"]))
    return {
        "ok": True,
        "updated_summary": updated_report.get("summary", {}),
        "report_json": str(_resolve_path(paths["final_report_json"])),
        "steps": [{"step": s["step"], "ok": s["ok"], "returncode": s["returncode"]} for s in steps],
    }


@mcp.tool(
    description=(
        "Read a quick summary of the propagation result: accepted/deferred/conflict counts "
        "plus the full deferred and conflict case lists with their top predictions and reasons. "
        "Use this to check pipeline progress without reading the large final_mapping_report.json."
        " Prefer this when you want propagation-centric status rather than the full final report."
    )
)
def read_propagation_summary(config_path: str) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    propagation_path = _resolve_path(paths["propagation_output_json"])
    if not propagation_path.exists():
        return {"ok": False, "error": f"propagation_output_json not found: {propagation_path}"}

    data = _load_json(propagation_path)
    results = data.get("results", [])

    accepted  = [r for r in results if r.get("status") == "accepted"]
    deferred  = [r for r in results if r.get("status") == "deferred"]
    conflicts = [r for r in results if r.get("status") == "conflict"]

    def compact(r: dict) -> dict:
        return {
            "case_id":   r.get("case_id"),
            "query_func": r.get("query_func"),
            "predicted": r.get("predicted_function_name"),
            "reasons":   r.get("status_reasons", []),
            "round":     r.get("propagation_round"),
        }

    return {
        "ok": True,
        "propagation_json": str(propagation_path),
        "summary": data.get("summary", {}),
        "round_log": data.get("round_log", []),
        "deferred": [compact(r) for r in deferred],
        "conflicts": [compact(r) for r in conflicts],
        "accepted_count": len(accepted),
    }


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


__all__ = ["main", "mcp"]
