#!/usr/bin/env python3
"""
Unified runtime entrypoint for the lua_callgraph_propagation_agent pipeline.

Supports two config formats:

  [New] session_name + extraction{} + analysis{}
    - Run Ghidra extraction and retrieval/propagation in separate phases
      to avoid JVM / embedding model memory overlap.

  [Legacy] paths{} + steps{}
    - Original flat config; all steps run in one pipeline_run call.

Typical commands:

  # Ghidra feature extraction only (new format)
  python scripts/10_run_name_mapping_pipeline.py \\
    --config data/configs/runtime_recommended_binary.json \\
    --phase extraction

  # Analysis pipeline only (new format) — run after extraction finishes
  python scripts/10_run_name_mapping_pipeline.py \\
    --config data/configs/runtime_recommended_binary.json \\
    --phase analysis

  # Legacy: run all enabled steps
  python scripts/10_run_name_mapping_pipeline.py \\
    --config data/configs/runtime_recommended_preextracted.json

  # Dry-run preview
  python scripts/10_run_name_mapping_pipeline.py \\
    --config data/configs/runtime_recommended_binary.json \\
    --phase extraction --dry-run


!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
[Warning]해당 스크립트는 사용하지 마십시오 !!!!!!!!!!!!!!!!!!
ghidra feature extraction과 retrieval/propagation이 메모리 측면에서 충돌이 발생할 수 있어, 두 단계를 분리하여 실행하는 새로운 config 포맷을 도입했습니다. 새 포맷에서는 --phase 플래그로 extraction 또는 analysis 단계만 선택적으로 실행할 수 있습니다. 기존의 legacy 포맷도 여전히 지원하지만, 새 포맷으로의 전환을 권장드립니다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "configs" / "runtime_recommended_preextracted.json"

# ── Windows cp949 stdout fix ──────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except AttributeError:
        pass

# config_loader는 같은 scripts/ 폴더에 있으므로 경로 추가 후 import
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config, is_new_format as _new_format, resolve_paths as _cl_resolve_paths  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the name-mapping pipeline (or one phase) from a config."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--phase",
        choices=["extraction", "analysis"],
        default=None,
        help="Run only one phase (new config format). Omit for legacy full-pipeline mode.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


# ── New config format helpers — 경로 해석은 config_loader에 위임 ─────────────

def _computed_paths(config: dict) -> dict[str, Any]:
    """Derive all runtime paths — delegates to config_loader.resolve_paths()."""
    return _cl_resolve_paths(config)


def build_extraction_commands(config: dict) -> list[dict]:
    python = sys.executable
    p = _computed_paths(config)
    ext = config.get("extraction", {})

    binary = ext.get("binary", "")
    if not binary:
        raise SystemExit("[ERROR] extraction.binary is required in config.")

    cmd = [
        python, "scripts/11_extract_query_features.py",
        "--binary", str(resolve_path(binary)),
        "--lua-version", ext.get("lua_version", p["lua_version"]),
        "--architecture", ext.get("architecture", p["architecture"]),
        "--opt-level", ext.get("opt_level", "O2"),
        "--strip-mode", ext.get("strip_mode", "nostrip"),
        "--session-name", p["session_name"],
        "--output-root", str(resolve_path(p["query_feature_output_root"])),
        "--work-root", str(resolve_path(p["extractor_work_root"])),
        "--extractor-script", str(resolve_path(p["extractor_script"])),
        "--python-bin", python,
    ]
    if ext.get("ghidra_home"):
        cmd.extend(["--ghidra-home", ext["ghidra_home"]])

    return [{"name": "extract_query_features", "description": "Extract Ghidra features from target binary", "cmd": cmd}]


def build_analysis_commands(config: dict) -> list[dict]:
    python = sys.executable
    p = _computed_paths(config)
    ana = config.get("analysis", {})
    retrieval = ana.get("retrieval", {})
    anchors = ana.get("seed_anchors", {})
    prop = ana.get("propagation", {})
    deferred = ana.get("deferred_analysis", {})

    commands: list[dict] = []

    # query feature 소스 결정 (우선순위: query_json > extract_manifest > session 경로)
    query_json_path: str | None = ana.get("query_json")
    extract_manifest_path: str | None = ana.get("extract_manifest") or p["extract_manifest_json"]

    # bulk_retrieval
    retrieval_cmd = [
        python, "scripts/12_run_bulk_query_retrieval.py",
        "--index", str(resolve_path(p["retrieval_index"])),
        "--output-json", str(resolve_path(p["retrieval_output_json"])),
        "--retrieval-script", str(resolve_path(p["retrieval_script"])),
        "--candidate-pool", str(retrieval.get("candidate_pool", 100)),
        "--topk", str(retrieval.get("topk", 20)),
        "--scoring-mode", retrieval.get("scoring_mode", "bonus_v2"),
    ]
    if query_json_path:
        retrieval_cmd.extend(["--query-json", str(resolve_path(query_json_path))])
    else:
        retrieval_cmd.extend(["--extract-manifest", str(resolve_path(extract_manifest_path))])
    commands.append({
        "name": "bulk_retrieval",
        "description": "Batch retrieval for all query functions",
        "cmd": retrieval_cmd,
    })

    # select_seed_anchors
    anchor_query_arg = query_json_path or extract_manifest_path
    anchor_cmd = [
        python, "scripts/13_select_seed_anchors.py",
        "--retrieval-json", str(resolve_path(p["retrieval_output_json"])),
        "--output-json", str(resolve_path(p["seed_anchor_json"])),
        "--min-top1-score", str(anchors.get("min_top1_score", 0.92)),
        "--min-margin", str(anchors.get("min_margin", 0.05)),
        "--query-json", str(resolve_path(anchor_query_arg)),
        "--reference-db", str(resolve_path(p["reference_db"])),
    ]
    commands.append({"name": "select_seed_anchors", "description": "Select seed anchors", "cmd": anchor_cmd})

    # build_runtime_suite
    commands.append({
        "name": "build_runtime_suite",
        "description": "Build propagation suite",
        "cmd": [
            python, "scripts/14_build_runtime_propagation_suite.py",
            "--retrieval-json", str(resolve_path(p["retrieval_output_json"])),
            "--anchor-json", str(resolve_path(p["seed_anchor_json"])),
            "--reference-db", str(resolve_path(p["reference_db"])),
            "--output-json", str(resolve_path(p["runtime_suite_json"])),
            "--embedding-project-root", str(resolve_path(p["embedding_project_root"])),
            "--propagation-output-json", str(resolve_path(p["propagation_output_json"])),
        ],
    })

    # propagation
    prop_cmd = [
        python, "scripts/04_propagate_from_anchors.py",
        "--suite", str(resolve_path(p["runtime_suite_json"])),
        "--output-json", str(resolve_path(p["propagation_output_json"])),
    ]
    if prop.get("iterative", True):
        prop_cmd.append("--iterative")
    commands.append({"name": "propagation", "description": "Anchor-based propagation", "cmd": prop_cmd})

    # deferred_analysis
    deferred_cmd = [
        python, "scripts/05_build_deferred_analysis.py",
        "--input-json", str(resolve_path(p["propagation_output_json"])),
        "--embedding-root", str(resolve_path(p["embedding_project_root"])),
        "--output-json", str(resolve_path(p["deferred_output_json"])),
        "--top-candidates", str(deferred.get("top_candidates", 5)),
    ]
    commands.append({"name": "deferred_analysis", "description": "Build deferred analysis", "cmd": deferred_cmd})

    # final_report
    commands.append({
        "name": "final_report",
        "description": "Export final mapping report",
        "cmd": [
            python, "scripts/15_export_final_mapping_report.py",
            "--propagation-json", str(resolve_path(p["propagation_output_json"])),
            "--deferred-json", str(resolve_path(p["deferred_output_json"])),
            "--output-json", str(resolve_path(p["final_report_json"])),
            "--session-name", p["session_name"],
        ],
    })

    return commands


# ── Legacy config format helpers ───────────────────────────────────────────────

def default_runtime_paths(paths: dict) -> dict[str, Any]:
    session = paths.get("session_name", "runtime_session")
    lua_version = paths.get("target_lua_version") or "Lua_547"
    architecture = paths.get("target_architecture") or "x86_64"
    normalized_arch = "aarch64" if architecture in {"aarch64", "arm64"} else "x86_64"
    result_root = f"data/runtime/results/{session}"
    query_root = f"data/runtime/query_features/{session}"

    defaults: dict[str, Any] = {
        "session_name": session,
        "target_binary": "",
        "target_lua_version": lua_version,
        "target_architecture": architecture,
        "target_opt_level": "O0",
        "target_strip_mode": "nostrip",
        "extractor_script": "src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py",
        "extractor_work_root": "data/runtime/extractor_workspace",
        "query_feature_output_root": "data/runtime/query_features",
        "extract_manifest_json": f"{query_root}/extract_manifest.json",
        "query_feature_json": "",
        "retrieval_script": "src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py",
        "retrieval_index": f"data/inputs/retrieval_indexes/{lua_version}/{normalized_arch}/runtime",
        "retrieval_output_json": f"{result_root}/retrieval_result.json",
        "seed_anchor_json": f"{result_root}/seed_anchors.json",
        "runtime_suite_json": f"{result_root}/runtime_propagation_suite.json",
        "reference_feature_root": "data/inputs/reference_features",
        "reference_db": f"data/inputs/callgraphs/{lua_version}/reference_callgraph.sqlite",
        "embedding_project_root": ".",
        "propagation_suite": "",
        "propagation_output_json": f"{result_root}/propagation_result.json",
        "deferred_output_json": f"{result_root}/deferred_analysis.json",
        "final_report_json": f"{result_root}/final_mapping_report.json",
    }

    merged = defaults.copy()
    for key, value in paths.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def normalize_command(command: list[str]) -> list[str]:
    result: list[str] = []
    for token in command:
        if token == "{python}":
            result.append(sys.executable)
        elif token == "{project_root}":
            result.append(str(PROJECT_ROOT))
        else:
            result.append(token)
    return result


def build_legacy_commands(config: dict) -> list[dict]:
    python = sys.executable
    paths = default_runtime_paths(config.get("paths", {}))
    steps = config.get("steps", {})
    commands: list[dict] = []

    build_db = steps.get("build_reference_db", {})
    if build_db.get("enabled", False):
        cmd = [
            python, "scripts/01_build_reference_callgraph_db.py",
            "--input-root", str(resolve_path(paths["reference_feature_root"])),
            "--output-db", str(resolve_path(paths["reference_db"])),
        ]
        if build_db.get("replace", False):
            cmd.append("--replace")
        commands.append({"name": "build_reference_db", "description": "Build reference callgraph DB", "cmd": cmd})

    extract = steps.get("extract_query_features", {})
    if extract.get("enabled", False):
        cmd = [
            python, "scripts/11_extract_query_features.py",
            "--binary", str(resolve_path(paths["target_binary"])),
            "--lua-version", paths["target_lua_version"],
            "--architecture", paths["target_architecture"],
            "--opt-level", paths.get("target_opt_level", "O2"),
            "--strip-mode", paths.get("target_strip_mode", "nostrip"),
            "--session-name", paths["session_name"],
            "--output-root", str(resolve_path(paths["query_feature_output_root"])),
            "--work-root", str(resolve_path(paths["extractor_work_root"])),
            "--extractor-script", str(resolve_path(paths["extractor_script"])),
            "--python-bin", paths.get("extractor_python", python),
        ]
        if paths.get("ghidra_home"):
            cmd.extend(["--ghidra-home", paths["ghidra_home"]])
        commands.append({"name": "extract_query_features", "description": "Extract query features from binary", "cmd": cmd})

    retrieval = steps.get("bulk_retrieval", {})
    if retrieval.get("enabled", False):
        cmd = [
            python, "scripts/12_run_bulk_query_retrieval.py",
            "--index", str(resolve_path(paths["retrieval_index"])),
            "--output-json", str(resolve_path(paths["retrieval_output_json"])),
            "--retrieval-script", str(resolve_path(paths["retrieval_script"])),
            "--candidate-pool", str(retrieval.get("candidate_pool", 200)),
            "--topk", str(retrieval.get("topk", 50)),
            "--scoring-mode", retrieval.get("scoring_mode", "bonus_v2"),
            "--mode", retrieval.get("mode", "runtime_query"),
        ]
        if paths.get("query_feature_json"):
            cmd.extend(["--query-json", str(resolve_path(paths["query_feature_json"]))])
        elif paths.get("extract_manifest_json"):
            cmd.extend(["--extract-manifest", str(resolve_path(paths["extract_manifest_json"]))])
        commands.append({"name": "bulk_retrieval", "description": "Bulk retrieval for all query functions", "cmd": cmd})

    auto_anchor = steps.get("select_seed_anchors", {})
    if auto_anchor.get("enabled", False):
        cmd = [
            python, "scripts/13_select_seed_anchors.py",
            "--retrieval-json", str(resolve_path(paths["retrieval_output_json"])),
            "--output-json", str(resolve_path(paths["seed_anchor_json"])),
            "--min-top1-score", str(auto_anchor.get("min_top1_score", 0.92)),
            "--min-margin", str(auto_anchor.get("min_margin", 0.05)),
        ]
        query_feature_json = paths.get("query_feature_json") or paths.get("extract_manifest_json")
        if query_feature_json:
            cmd.extend(["--query-json", str(resolve_path(query_feature_json))])
        if paths.get("reference_db"):
            cmd.extend(["--reference-db", str(resolve_path(paths["reference_db"]))])
        commands.append({"name": "select_seed_anchors", "description": "Select seed anchors", "cmd": cmd})

    build_suite = steps.get("build_runtime_suite", {})
    if build_suite.get("enabled", False):
        commands.append({
            "name": "build_runtime_suite",
            "description": "Build propagation suite",
            "cmd": [
                python, "scripts/14_build_runtime_propagation_suite.py",
                "--retrieval-json", str(resolve_path(paths["retrieval_output_json"])),
                "--anchor-json", str(resolve_path(paths["seed_anchor_json"])),
                "--reference-db", str(resolve_path(paths["reference_db"])),
                "--output-json", str(resolve_path(paths["runtime_suite_json"])),
                "--embedding-project-root", str(resolve_path(paths["embedding_project_root"])),
                "--propagation-output-json", str(resolve_path(paths["propagation_output_json"])),
            ],
        })

    propagation = steps.get("propagation", {})
    if propagation.get("enabled", False):
        cmd = [
            python, "scripts/04_propagate_from_anchors.py",
            "--suite", str(resolve_path(paths.get("propagation_suite") or paths["runtime_suite_json"])),
            "--output-json", str(resolve_path(paths["propagation_output_json"])),
        ]
        if propagation.get("iterative", True):
            cmd.append("--iterative")
        commands.append({"name": "propagation", "description": "Anchor-based propagation", "cmd": cmd})

    deferred = steps.get("deferred_analysis", {})
    if deferred.get("enabled", False):
        cmd = [
            python, "scripts/05_build_deferred_analysis.py",
            "--input-json", str(resolve_path(paths["propagation_output_json"])),
            "--embedding-root", str(resolve_path(paths["embedding_project_root"])),
            "--output-json", str(resolve_path(paths["deferred_output_json"])),
        ]
        if deferred.get("top_candidates") is not None:
            cmd.extend(["--top-candidates", str(deferred["top_candidates"])])
        commands.append({"name": "deferred_analysis", "description": "Build deferred analysis", "cmd": cmd})

    final_report = steps.get("final_report", {})
    if final_report.get("enabled", False):
        commands.append({
            "name": "final_report",
            "description": "Export final mapping report",
            "cmd": [
                python, "scripts/15_export_final_mapping_report.py",
                "--propagation-json", str(resolve_path(paths["propagation_output_json"])),
                "--deferred-json", str(resolve_path(paths["deferred_output_json"])),
                "--output-json", str(resolve_path(paths["final_report_json"])),
                "--session-name", paths["session_name"],
            ],
        })

    for extra in config.get("external_steps", []):
        if not extra.get("enabled", False):
            continue
        commands.append({
            "name": extra.get("name", "external_step"),
            "description": extra.get("description", "External step"),
            "cmd": normalize_command(extra.get("command", [])),
        })

    return commands


# ── Step runner ────────────────────────────────────────────────────────────────

def run_step(step: dict, *, dry_run: bool) -> dict:
    print(f"\n[STEP] {step['name']}")
    print(step["description"])
    print("Command:")
    print("  " + " ".join(step["cmd"]))

    if dry_run:
        return {"name": step["name"], "status": "dry_run"}

    proc = subprocess.Popen(
        step["cmd"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        return {"name": step["name"], "status": "error", "returncode": proc.returncode}
    return {"name": step["name"], "status": "completed"}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))

    if _new_format(config):
        if args.phase == "extraction":
            commands = build_extraction_commands(config)
        elif args.phase == "analysis":
            commands = build_analysis_commands(config)
        else:
            # 새 포맷에서 phase 미지정 → 둘 다
            commands = build_extraction_commands(config) + build_analysis_commands(config)
    else:
        if args.phase:
            raise SystemExit(f"[ERROR] --phase requires the new config format (session_name + extraction/analysis sections).")
        commands = build_legacy_commands(config)

    if not commands:
        raise SystemExit("No steps to run.")

    print(f"[PIPELINE] {config.get('pipeline_name', 'unnamed')}")
    print(config.get("description", ""))
    if args.phase:
        print(f"[PHASE] {args.phase}")

    results = []
    for step in commands:
        result = run_step(step, dry_run=args.dry_run)
        results.append(result)
        if result["status"] == "error" and args.stop_on_error:
            break

    summary = {
        "num_steps": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "dry_run": sum(1 for r in results if r["status"] == "dry_run"),
        "errors": sum(1 for r in results if r["status"] == "error"),
    }
    print("\n[SUMMARY]")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
