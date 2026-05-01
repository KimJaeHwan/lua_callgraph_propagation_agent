from __future__ import annotations

import subprocess
import sys
import json
import sqlite3
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
        "embedding-model memory overlap. "
        "RECOMMENDED ANALYST LOOP for a stripped production binary (e.g. game engine): "
        "(1) extract_query_features — Ghidra feature extraction; "
        "(2) bulk_query_retrieval — full embedding+graph retrieval (wide net); "
        "(3) detect_lua_scope — identify Lua VM functions via string-signal BFS "
        "    (blocks game-code from polluting seed selection); "
        "(4) select_seed_anchors with scope_json + dedup_max_per_ref=1 — "
        "    clean 1:1 seeds only; "
        "(5) build_runtime_suite → run_downstream — propagation round 1; "
        "(6) get_mapping_distribution — detect noisy many:1 reference names; "
        "(7) update_noise_blacklist + run_downstream — strip noise, re-propagate; "
        "(8) export_trusted_mappings → rename in IDA → batch_register_force_anchors; "
        "--- TARGETED RETRIEVAL (round N+1, requires confirmed anchors) --- "
        "(9) patch_features_with_confirmed — inject real names into callee/caller lists; "
        "(10) targeted_retrieval with patched features + seed_anchors as anchors_json — "
        "     vote-based structural matching, no embeddings needed; "
        "(11) select_seed_anchors again with targeted_json from step 10 — "
        "     adds 'targeted_high_confidence' anchors at lower score threshold (0.75); "
        "(12) run_downstream — propagation round 2 with denser anchor set; "
        "(13) repeat from step 8 until convergence (0 new accepted per round). "
        "KEY INSIGHT: targeted_retrieval uses callgraph structure (which functions call "
        "which) to constrain candidates. 199 confirmed anchors × ~8 neighbors each = "
        "~1600 structurally-constrained target functions, dramatically higher precision "
        "than full retrieval. Works without sentence_transformers."
    ),
    version="0.6.0",
)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    # Allow callers to pass either project-relative paths like "data/..." or
    # repo-root-relative paths like "lua_callgraph_propagation_agent/data/...".
    if path.parts and path.parts[0] == PROJECT_ROOT.name:
        path = Path(*path.parts[1:])
    return (PROJECT_ROOT / path).resolve()


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
        "scope_json: HIGHLY RECOMMENDED for mixed binaries (game + Lua VM). "
        "Pass lua_scope.json from detect_lua_scope() to restrict embedding encoding "
        "to only Lua-scope functions (~800 vs 16000). Effect: 20x faster encoding, "
        "zero game-code contamination in retrieval_result.json. "
        "scope_min_confidence: 'low' includes BFS-reached functions (default); "
        "'medium' limits to within 2 hops; 'high' = string-signal seeds only."
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
    scope_json: str | None = None,
    scope_min_confidence: str = "low",
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/12_run_bulk_query_retrieval.py",
        "--index",          str(_resolve_path(index)),
        "--output-json",    str(_resolve_path(output_json)),
        "--candidate-pool", str(candidate_pool),
        "--topk",           str(topk),
        "--scoring-mode",   scoring_mode,
        "--scope-min-confidence", scope_min_confidence,
    ]
    if extract_manifest:
        command.extend(["--extract-manifest", str(_resolve_path(extract_manifest))])
    if query_json:
        command.extend(["--query-json", str(_resolve_path(query_json))])
    if scope_json:
        command.extend(["--scope-json", str(_resolve_path(scope_json))])
    return _run_command(command)


@mcp.tool(
    description=(
        "Targeted retrieval using callgraph neighbor constraints (no embedding model required). "
        "For each unconfirmed query function that shares a callgraph edge with a confirmed anchor, "
        "restricts the candidate pool to the reference callgraph neighbors of that anchor and "
        "scores candidates by vote consensus, then applies a small reference-string bonus only as "
        "a tie-breaker between structurally similar candidates. "
        "vote_score = confirmed_neighbors_that_agree / total_confirmed_neighbors. "
        "1.0 = unanimous consensus; 0.75 = 3 out of 4 neighbors agree. "
        "Run AFTER a round of propagation has produced confirmed anchors (seed_anchors.json "
        "or propagation_result.json), and BEFORE the next seed selection step. "
        "query_json: patched feature JSON (use patch_features_with_confirmed first for best results). "
        "anchors_json: seed_anchors.json or propagation_result.json — source of confirmed mappings. "
        "reference_db: reference callgraph SQLite (edges table with src_name/dst_name). "
        "output_json: where to write targeted_retrieval.json. "
        "lua_version: filter reference DB by version (e.g. 'Lua_536') for cleaner neighbor sets. "
        "min_vote_score: include only cases with vote_score >= this (default 0.0 = all, "
        "use 0.75 for high-confidence only). "
        "Passes output to select_seed_anchors via --targeted-json for dedup/scope gate filtering."
    )
)
def targeted_retrieval(
    query_json: str,
    anchors_json: str,
    reference_db: str,
    output_json: str,
    topk: int = 10,
    min_vote_score: float = 0.0,
    min_voters: int = 1,
    lua_version: str | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/12c_targeted_retrieval.py",
        "--query-json",     str(_resolve_path(query_json)),
        "--anchors-json",   str(_resolve_path(anchors_json)),
        "--reference-db",   str(_resolve_path(reference_db)),
        "--output-json",    str(_resolve_path(output_json)),
        "--topk",           str(topk),
        "--min-vote-score", str(min_vote_score),
        "--min-voters",     str(min_voters),
    ]
    if lua_version:
        command.extend(["--lua-version", lua_version])
    return _run_command(command)


@mcp.tool(
    description=(
        "Automatically detect which functions in a stripped binary are part of the embedded Lua VM, "
        "using Lua-specific string signal detection followed by bidirectional BFS callgraph expansion. "
        "This wraps scripts/12b_detect_lua_scope.py and produces a lua_scope.json file. "
        "Run this BEFORE select_seed_anchors so the scope gate can filter out game-code false positives. "
        "query_json: path to the extracted feature JSON (or extract_manifest). "
        "output_json: where to write lua_scope.json. "
        "bfs_depth: how many callgraph hops to expand from string-signal seeds (default 4). "
        "max_pcode_instructions: exclude functions larger than this (default 8000 — very large = game code). "
        "Returns the subprocess output including string_signal_count and lua_scope_count. "
        "Typical result: ~600-1000 functions flagged as Lua scope in a 16k-function binary."
    )
)
def detect_lua_scope(
    query_json: str,
    output_json: str,
    bfs_depth: int = 4,
    max_pcode_instructions: int = 8000,
    min_string_signals: int = 1,
) -> dict[str, Any]:
    return _run_command([
        sys.executable,
        "scripts/12b_detect_lua_scope.py",
        "--query-json",        str(_resolve_path(query_json)),
        "--output-json",       str(_resolve_path(output_json)),
        "--bfs-depth",         str(bfs_depth),
        "--max-pcode-instructions", str(max_pcode_instructions),
        "--min-string-signals", str(min_string_signals),
    ])


@mcp.tool(
    description=(
        "Select initial seed anchors from retrieval_result.json using deterministic confidence rules. "
        "This wraps scripts/13_select_seed_anchors.py and produces seed_anchors.json. "
        "Use this after bulk_query_retrieval (and optionally detect_lua_scope) and before build_runtime_suite. "
        "retrieval_json: path to retrieval_result.json. "
        "output_json: where to write seed_anchors.json. "
        "query_json: optional query feature JSON or extract_manifest for visible-name anchor detection. "
        "reference_db: optional reference callgraph DB used to validate visible names. "
        "min_top1_score and min_margin control retrieval_high_confidence anchor selection. "
        "--- NEW anti-noise parameters --- "
        "scope_json: path to lua_scope.json from detect_lua_scope. When provided, only functions "
        "inside the detected Lua scope can become retrieval_high_confidence seeds, blocking game-code "
        "false positives from poisoning the seed set. Highly recommended for stripped production binaries. "
        "scope_min_confidence: minimum scope confidence to pass the gate — 'low' (default), 'medium', or 'high'. "
        "dedup_max_per_ref: reject reference names that attract more than this many query candidates. "
        "Default 1 = only allow 1:1 unambiguous mappings. Prevents noisy names (e.g. 'match', 'resume') "
        "from becoming seeds when they match dozens of functions. "
        "--- Targeted retrieval integration --- "
        "targeted_json: path to targeted_retrieval.json from targeted_retrieval(). When provided, "
        "adds a third anchor source 'targeted_high_confidence' processed with its own lower thresholds. "
        "targeted_min_score: minimum vote_score for targeted anchors (default 0.75). "
        "targeted_min_margin: minimum top1-top2 gap for targeted anchors (default 0.15)."
    )
)
def select_seed_anchors(
    retrieval_json: str,
    output_json: str,
    min_top1_score: float = 0.92,
    min_margin: float = 0.05,
    query_json: str | None = None,
    reference_db: str | None = None,
    scope_json: str | None = None,
    scope_min_confidence: str = "low",
    dedup_max_per_ref: int = 1,
    targeted_json: str | None = None,
    targeted_min_score: float = 0.75,
    targeted_min_margin: float = 0.15,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/13_select_seed_anchors.py",
        "--retrieval-json",       str(_resolve_path(retrieval_json)),
        "--output-json",          str(_resolve_path(output_json)),
        "--min-top1-score",       str(min_top1_score),
        "--min-margin",           str(min_margin),
        "--dedup-max-per-ref",    str(dedup_max_per_ref),
        "--scope-min-confidence", scope_min_confidence,
        "--targeted-min-score",   str(targeted_min_score),
        "--targeted-min-margin",  str(targeted_min_margin),
    ]
    if query_json:
        command.extend(["--query-json",    str(_resolve_path(query_json))])
    if reference_db:
        command.extend(["--reference-db",  str(_resolve_path(reference_db))])
    if scope_json:
        command.extend(["--scope-json",    str(_resolve_path(scope_json))])
    if targeted_json:
        command.extend(["--targeted-json", str(_resolve_path(targeted_json))])
    return _run_command(command)


@mcp.tool(
    description=(
        "Build runtime_propagation_suite.json from retrieval results, seed anchors, and a reference DB. "
        "This wraps scripts/14_build_runtime_propagation_suite.py. "
        "Use this after select_seed_anchors and before propagation or run_downstream-style reruns. "
        "retrieval_json: path to retrieval_result.json. "
        "anchor_json: path to seed_anchors.json. "
        "output_json: where to write runtime_propagation_suite.json. "
        "propagation_output_json: target path that propagation should later write to. "
        "reference_db: optional explicit SQLite DB path; if omitted, lua_version is used to resolve it. "
        "embedding_project_root should usually stay as the project root."
    )
)
def build_runtime_suite(
    retrieval_json: str,
    anchor_json: str,
    output_json: str,
    propagation_output_json: str,
    lua_version: str = "Lua_547",
    reference_db: str | None = None,
    embedding_project_root: str = ".",
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/14_build_runtime_propagation_suite.py",
        "--retrieval-json",
        str(_resolve_path(retrieval_json)),
        "--anchor-json",
        str(_resolve_path(anchor_json)),
        "--output-json",
        str(_resolve_path(output_json)),
        "--lua-version",
        lua_version,
        "--embedding-project-root",
        str(_resolve_path(embedding_project_root)),
        "--propagation-output-json",
        str(_resolve_path(propagation_output_json)),
    ]
    if reference_db:
        command.extend(["--reference-db", str(_resolve_path(reference_db))])
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


def _load_reference_string_samples(
    reference_db: Path,
    lua_version: str | None,
    architecture: str | None,
    function_names: list[str],
    limit_per_function: int = 8,
) -> dict[str, list[str]]:
    if not reference_db.exists() or not function_names:
        return {}

    unique_names = [name for name in dict.fromkeys(function_names) if name]
    if not unique_names:
        return {}

    conn = sqlite3.connect(str(reference_db))
    try:
        out: dict[str, list[str]] = {}
        for function_name in unique_names:
            clauses = ["function_name = ?"]
            params: list[Any] = [function_name]
            if lua_version:
                clauses.append("lua_version = ?")
                params.append(lua_version)
            if architecture:
                norm_arch = "aarch64" if architecture in {"arm64", "aarch64"} else architecture
                clauses.append("architecture = ?")
                params.append(norm_arch)
            params.append(limit_per_function)
            rows = conn.execute(
                "SELECT string_value FROM function_strings "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY LENGTH(string_value) DESC, string_value ASC "
                "LIMIT ?",
                params,
            ).fetchall()
            out[function_name] = [row[0] for row in rows]
        return out
    finally:
        conn.close()


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
        "status, a compact query feature summary, and reference-side string samples for the top candidates. "
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
    reference_db = _resolve_path(paths["reference_db"])

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
    candidate_names: list[str] = []
    if triage_case:
        for cand in triage_case.get("top_candidates", [])[:5]:
            ref_name = cand.get("reference_function_name") or cand.get("function_name")
            candidate_names.append(ref_name)
            compact_candidates.append({
                "reference_function_name": ref_name,
                "candidate_source": cand.get("candidate_source") or cand.get("source"),
                "final_score": cand.get("final_score"),
                "retrieval_prior": cand.get("retrieval_prior"),
                "graph_score": cand.get("graph_score"),
                "graph_breakdown": cand.get("graph_breakdown"),
            })

    ref_string_samples = _load_reference_string_samples(
        reference_db=reference_db,
        lua_version=(query_summary or {}).get("lua_version") or mapping_record.get("lua_version"),
        architecture=(query_summary or {}).get("architecture") or mapping_record.get("architecture"),
        function_names=candidate_names,
        limit_per_function=8,
    )

    if ref_string_samples:
        for cand in compact_candidates:
            cand["reference_strings"] = ref_string_samples.get(
                cand.get("reference_function_name", ""),
                [],
            )

    return {
        "ok": True,
        "case_id": case_id,
        "query_func": query_func,
        "report_json": str(report_path),
        "deferred_json": str(deferred_path),
        "seed_anchor_json": str(anchor_path),
        "reference_db": str(reference_db),
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


@mcp.tool(
    description=(
        "Analyse the propagation result and return a histogram of how many query functions "
        "each reference name is mapped to. "
        "High-count reference names (>= suspicious_threshold) are strong noise candidates: "
        "they likely match a common code pattern rather than a unique function. "
        "Use this after run_downstream or batch_register_force_anchors to decide which names "
        "to add to the noise blacklist before the next propagation round. "
        "Returns: count_distribution (bucket → count), suspicious_names (list of "
        "{reference_name, query_count, example_queries}), total_accepted, total_unique_ref_names."
    )
)
def get_mapping_distribution(
    config_path: str,
    suspicious_threshold: int = 5,
) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    propagation_path = _resolve_path(paths["propagation_output_json"])
    if not propagation_path.exists():
        return {"ok": False, "error": f"propagation_output_json not found: {propagation_path}"}

    data = _load_json(propagation_path)
    results = data.get("results", [])
    accepted = [r for r in results if r.get("status") == "accepted"]

    from collections import defaultdict, Counter
    name_to_queries: dict[str, list[str]] = defaultdict(list)
    for r in accepted:
        ref_name = r.get("predicted_function_name", "")
        query = r.get("query_func", "")
        if ref_name and not ref_name.startswith(("FUN_", "sub_")):
            name_to_queries[ref_name].append(query)

    count_dist = Counter(len(v) for v in name_to_queries.values())
    suspicious = [
        {
            "reference_name": name,
            "query_count": len(queries),
            "example_queries": queries[:5],
        }
        for name, queries in sorted(name_to_queries.items(), key=lambda x: -len(x[1]))
        if len(queries) >= suspicious_threshold
    ]

    return {
        "ok": True,
        "propagation_json": str(propagation_path),
        "summary": data.get("summary", {}),
        "count_distribution": {str(k): v for k, v in sorted(count_dist.items())},
        "total_accepted": len(accepted),
        "total_unique_ref_names": len(name_to_queries),
        "high_confidence_1to1_count": count_dist.get(1, 0),
        "suspicious_names": suspicious,
        "suspicious_threshold": suspicious_threshold,
    }


@mcp.tool(
    description=(
        "Export high-confidence accepted mappings filtered by mapping_count. "
        "mapping_count=1 means exactly ONE query function matched this reference name (1:1, highest confidence). "
        "max_count=1 returns only 1:1 mappings; max_count=2 includes 2:1 caution entries too. "
        "exclude_prefixes filters out unwanted reference names (default: FUN_,sub_). "
        "Returns sorted list of {query_func, entry_point, predicted_name, mapping_count, final_score, propagation_round}. "
        "Use this to decide which functions to rename in IDA and register as force anchors for the next round. "
        "Optionally saves results to output_json."
    )
)
def export_trusted_mappings(
    config_path: str,
    max_count: int = 1,
    exclude_prefixes: str = "FUN_,sub_",
    output_json: str | None = None,
) -> dict[str, Any]:
    config = _load_json(_resolve_path(config_path))
    paths = _default_runtime_paths(config)

    propagation_path = _resolve_path(paths["propagation_output_json"])
    if not propagation_path.exists():
        return {"ok": False, "error": f"propagation_output_json not found: {propagation_path}"}

    data = _load_json(propagation_path)
    results = data.get("results", [])

    # build entry_point lookup from query feature JSON
    addr_map: dict[str, str] = {}
    query_file_path = _resolve_path(paths.get("query_json", ""))
    if query_file_path.exists():
        try:
            funcs = _load_json(query_file_path)
            if isinstance(funcs, list):
                addr_map = {f["function_name"]: f.get("entry_point", "") for f in funcs}
        except Exception:
            pass

    from collections import defaultdict
    excl = tuple(x for x in exclude_prefixes.split(",") if x)
    name_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r.get("status") == "accepted":
            name_to_rows[r["predicted_function_name"]].append(r)

    trusted: list[dict] = []
    for ref_name, rows in name_to_rows.items():
        if len(rows) > max_count:
            continue
        if ref_name.startswith(excl):
            continue
        for r in rows:
            qf = r["query_func"]
            top = (r.get("top_candidates") or [{}])[0]
            trusted.append({
                "query_func": qf,
                "entry_point": addr_map.get(qf, ""),
                "predicted_name": ref_name,
                "mapping_count": len(rows),
                "final_score": top.get("final_score", 0),
                "propagation_round": r.get("propagation_round", -1),
            })

    trusted.sort(key=lambda x: (-int(x["mapping_count"] == 1), -x["final_score"]))

    result = {
        "ok": True,
        "propagation_json": str(propagation_path),
        "max_count": max_count,
        "total_trusted": len(trusted),
        "unique_1to1": sum(1 for t in trusted if t["mapping_count"] == 1),
        "mappings": trusted,
    }

    if output_json:
        out_path = _resolve_path(output_json)
        _save_json(out_path, trusted)
        result["saved_to"] = str(out_path)

    return result


@mcp.tool(
    description=(
        "Add or remove reference function names from the noise_blacklist in a suite JSON file. "
        "The noise_blacklist prevents noisy reference names from being proposed as candidates "
        "during propagation (applied at both retrieval-candidate filtering AND callgraph expansion). "
        "add: list of names to add to the blacklist. "
        "remove: list of names to remove from the blacklist. "
        "Both operations are idempotent — adding an already-listed name or removing a missing name "
        "is a no-op. "
        "Call run_downstream after this to re-propagate with the updated policy."
    )
)
def update_noise_blacklist(
    suite_json: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    suite_path = _resolve_path(suite_json)
    if not suite_path.exists():
        return {"ok": False, "error": f"suite_json not found: {suite_path}"}

    data = _load_json(suite_path)
    policy = data.setdefault("classification_policy", {})
    current: list[str] = policy.get("noise_blacklist", [])
    current_set = set(current)

    actually_added: list[str] = []
    actually_removed: list[str] = []

    for name in (add or []):
        if name and name not in current_set:
            current_set.add(name)
            actually_added.append(name)

    for name in (remove or []):
        if name and name in current_set:
            current_set.discard(name)
            actually_removed.append(name)

    policy["noise_blacklist"] = sorted(current_set)
    _save_json(suite_path, data)

    return {
        "ok": True,
        "suite_json": str(suite_path),
        "blacklist_size": len(policy["noise_blacklist"]),
        "added": actually_added,
        "removed": actually_removed,
        "current_blacklist": policy["noise_blacklist"],
    }


@mcp.tool(
    description=(
        "Patch a query feature JSON so that known real Lua function names appear in callee/caller "
        "neighbour lists, giving hybrid retrieval a stronger callgraph signal on the next run. "
        "confirmed_map: dict mapping hex entry_point strings to confirmed real names, "
        "e.g. {'4a7141': 'luaopen_base', '48dc9e': 'lua_setfield'}. "
        "For each function in the feature JSON whose entry_point appears in confirmed_map, "
        "every occurrence of that function's Ghidra name (FUN_xxx / sub_xxx) in ALL other "
        "functions' callee/caller lists is replaced with the real name. "
        "Saves the patched file as <stem>_patched.json next to the original. "
        "Returns the path to the patched file and how many callee/caller references were replaced. "
        "Run bulk_query_retrieval again with the patched file to improve retrieval quality."
    )
)
def patch_features_with_confirmed(
    query_json: str,
    confirmed_map: dict[str, str],
) -> dict[str, Any]:
    query_path = _resolve_path(query_json)
    if not query_path.exists():
        return {"ok": False, "error": f"query_json not found: {query_path}"}

    funcs: list[dict] = _load_json(query_path)
    if not isinstance(funcs, list):
        funcs = funcs.get("functions", [])

    # build: ghidra_function_name -> real_name  (keyed by entry_point hex)
    ghidra_to_real: dict[str, str] = {}
    for f in funcs:
        ep_raw = f.get("entry_point", "").lower().lstrip("0") or "0"
        for ep_key, real_name in confirmed_map.items():
            ep_norm = ep_key.lower().lstrip("0") or "0"
            if ep_raw == ep_norm:
                ghidra_to_real[f["function_name"]] = real_name
                break

    replaced_total = 0
    patched: list[dict] = []
    for fc in funcs:
        import copy
        fc = copy.deepcopy(fc)
        new_callees = []
        for c in fc.get("callees", []):
            if c in ghidra_to_real:
                new_callees.append(ghidra_to_real[c])
                replaced_total += 1
            else:
                new_callees.append(c)
        fc["callees"] = new_callees

        new_callers = []
        for c in fc.get("callers", []):
            if c in ghidra_to_real:
                new_callers.append(ghidra_to_real[c])
                replaced_total += 1
            else:
                new_callers.append(c)
        fc["callers"] = new_callers
        patched.append(fc)

    out_path = query_path.parent / (query_path.stem + "_patched.json")
    _save_json(out_path, patched)

    return {
        "ok": True,
        "original_query_json": str(query_path),
        "patched_query_json": str(out_path),
        "confirmed_names_count": len(ghidra_to_real),
        "callee_caller_references_replaced": replaced_total,
        "note": "Run bulk_query_retrieval with patched_query_json to improve retrieval candidates.",
    }


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


__all__ = ["main", "mcp"]
