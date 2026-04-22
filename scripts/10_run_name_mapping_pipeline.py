#!/usr/bin/env python3
"""
Unified runtime entrypoint for the lua_callgraph_propagation_agent pipeline.

Goal:
  - Stop thinking in terms of isolated experiment scripts.
  - Centralize runtime paths and step toggles in one JSON config.
  - Execute the currently implemented deterministic + optional LLM stages
    from this repository as one pipeline.

Typical commands:

  # Show the exact commands that would run.
  python3 scripts/10_run_name_mapping_pipeline.py \
    --config data/configs/name_mapping_pipeline.example.json \
    --dry-run

  # Execute all enabled internal steps.
  python3 scripts/10_run_name_mapping_pipeline.py \
    --config data/configs/name_mapping_pipeline.example.json

Notes:
  - This runner centralizes the workflow inside lua_callgraph_propagation_agent.
  - Extraction and retrieval code used by the runtime is vendored into this
    repository so the operational path does not require sibling repositories.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "configs" / "name_mapping_pipeline.example.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the consolidated name-mapping pipeline from one config."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="pipeline config JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print resolved commands without executing them",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop immediately if a step fails",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


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


def build_step_commands(config: dict) -> list[dict]:
    python = sys.executable
    paths = config.get("paths", {})
    steps = config.get("steps", {})

    commands: list[dict] = []

    build_db = steps.get("build_reference_db", {})
    if build_db.get("enabled", False):
        cmd = [
            python,
            "scripts/01_build_reference_callgraph_db.py",
            "--input-root",
            str(resolve_path(paths["reference_feature_root"])),
            "--output-db",
            str(resolve_path(paths["reference_db"])),
        ]
        if build_db.get("replace", False):
            cmd.append("--replace")
        commands.append(
            {
                "name": "build_reference_db",
                "description": "Build or refresh vanilla reference callgraph SQLite DB",
                "cmd": cmd,
            }
        )

    extract = steps.get("extract_query_features", {})
    if extract.get("enabled", False):
        cmd = [
            python,
            "scripts/11_extract_query_features.py",
            "--binary",
            str(resolve_path(paths["target_binary"])),
            "--lua-version",
            paths["target_lua_version"],
            "--architecture",
            paths["target_architecture"],
            "--opt-level",
            paths.get("target_opt_level", "O2"),
            "--strip-mode",
            paths.get("target_strip_mode", "nostrip"),
            "--session-name",
            paths["session_name"],
            "--output-root",
            str(resolve_path(paths["query_feature_output_root"])),
            "--work-root",
            str(resolve_path(paths["extractor_work_root"])),
            "--extractor-script",
            str(resolve_path(paths["extractor_script"])),
            "--python-bin",
            paths.get("extractor_python", python),
        ]
        commands.append(
            {
                "name": "extract_query_features",
                "description": "Extract query features from one target binary into this repo runtime workspace",
                "cmd": cmd,
            }
        )

    retrieval = steps.get("bulk_retrieval", {})
    if retrieval.get("enabled", False):
        cmd = [
            python,
            "scripts/12_run_bulk_query_retrieval.py",
            "--index",
            str(resolve_path(paths["retrieval_index"])),
            "--output-json",
            str(resolve_path(paths["retrieval_output_json"])),
            "--retrieval-script",
            str(resolve_path(paths["retrieval_script"])),
            "--candidate-pool",
            str(retrieval.get("candidate_pool", 200)),
            "--topk",
            str(retrieval.get("topk", 50)),
            "--scoring-mode",
            retrieval.get("scoring_mode", "bonus_v2"),
            "--mode",
            retrieval.get("mode", "runtime_query"),
        ]
        if paths.get("extract_manifest_json"):
            cmd.extend(["--extract-manifest", str(resolve_path(paths["extract_manifest_json"]))])
        elif paths.get("query_feature_json"):
            cmd.extend(["--query-json", str(resolve_path(paths["query_feature_json"]))])
        commands.append(
            {
                "name": "bulk_retrieval",
                "description": "Generate retrieval candidates for every function in the extracted query feature JSON",
                "cmd": cmd,
            }
        )

    auto_anchor = steps.get("select_seed_anchors", {})
    if auto_anchor.get("enabled", False):
        cmd = [
            python,
            "scripts/13_select_seed_anchors.py",
            "--retrieval-json",
            str(resolve_path(paths["retrieval_output_json"])),
            "--output-json",
            str(resolve_path(paths["seed_anchor_json"])),
            "--min-top1-score",
            str(auto_anchor.get("min_top1_score", 0.92)),
            "--min-margin",
            str(auto_anchor.get("min_margin", 0.05)),
        ]
        commands.append(
            {
                "name": "select_seed_anchors",
                "description": "Select deterministic seed anchors from high-confidence retrieval results",
                "cmd": cmd,
            }
        )

    build_suite = steps.get("build_runtime_suite", {})
    if build_suite.get("enabled", False):
        cmd = [
            python,
            "scripts/14_build_runtime_propagation_suite.py",
            "--retrieval-json",
            str(resolve_path(paths["retrieval_output_json"])),
            "--anchor-json",
            str(resolve_path(paths["seed_anchor_json"])),
            "--reference-db",
            str(resolve_path(paths["reference_db"])),
            "--output-json",
            str(resolve_path(paths["runtime_suite_json"])),
            "--embedding-project-root",
            str(resolve_path(paths["embedding_project_root"])),
            "--propagation-output-json",
            str(resolve_path(paths["propagation_output_json"])),
        ]
        commands.append(
            {
                "name": "build_runtime_suite",
                "description": "Generate a propagation suite JSON for the current runtime session",
                "cmd": cmd,
            }
        )

    propagation = steps.get("propagation", {})
    if propagation.get("enabled", False):
        cmd = [
            python,
            "scripts/04_propagate_from_anchors.py",
            "--suite",
            str(resolve_path(paths["propagation_suite"] if paths.get("propagation_suite") else paths["runtime_suite_json"])),
            "--output-json",
            str(resolve_path(paths["propagation_output_json"])),
        ]
        commands.append(
            {
                "name": "propagation",
                "description": "Run anchor-based propagation and accepted/deferred/conflict classification",
                "cmd": cmd,
            }
        )

    deferred = steps.get("deferred_analysis", {})
    if deferred.get("enabled", False):
        cmd = [
            python,
            "scripts/05_build_deferred_analysis.py",
            "--input-json",
            str(resolve_path(paths["propagation_output_json"])),
            "--embedding-root",
            str(resolve_path(paths["embedding_project_root"])),
            "--output-json",
            str(resolve_path(paths["deferred_output_json"])),
        ]
        top_candidates = deferred.get("top_candidates")
        if top_candidates is not None:
            cmd.extend(["--top-candidates", str(top_candidates)])
        commands.append(
            {
                "name": "deferred_analysis",
                "description": "Build compact deferred/conflict analysis payload",
                "cmd": cmd,
            }
        )

    llm = steps.get("llm_analyst", {})
    if llm.get("enabled", False):
        cmd = [
            python,
            "scripts/06_run_local_llm_analyst.py",
            "--provider",
            llm.get("provider", "dry-run"),
            "--input-json",
            str(resolve_path(paths["deferred_output_json"])),
            "--output-json",
            str(resolve_path(paths["llm_output_json"])),
        ]
        if llm.get("provider") != "dry-run":
            if llm.get("base_url"):
                cmd.extend(["--base-url", llm["base_url"]])
            if llm.get("model"):
                cmd.extend(["--model", llm["model"]])
            if llm.get("temperature") is not None:
                cmd.extend(["--temperature", str(llm["temperature"])])
            if llm.get("timeout") is not None:
                cmd.extend(["--timeout", str(llm["timeout"])])
            if llm.get("response_format_json", False):
                cmd.append("--response-format-json")
        else:
            cmd.append("--dry-run")

        if llm.get("max_cases") is not None:
            cmd.extend(["--max-cases", str(llm["max_cases"])])

        commands.append(
            {
                "name": "llm_analyst",
                "description": "Run optional Local LLM analyst over deferred/conflict payloads",
                "cmd": cmd,
            }
        )

    final_report = steps.get("final_report", {})
    if final_report.get("enabled", False):
        cmd = [
            python,
            "scripts/15_export_final_mapping_report.py",
            "--propagation-json",
            str(resolve_path(paths["propagation_output_json"])),
            "--deferred-json",
            str(resolve_path(paths["deferred_output_json"])),
            "--output-json",
            str(resolve_path(paths["final_report_json"])),
            "--session-name",
            paths["session_name"],
        ]
        llm_output = paths.get("llm_output_json")
        if llm_output:
            cmd.extend(["--llm-json", str(resolve_path(llm_output))])
        commands.append(
            {
                "name": "final_report",
                "description": "Export one compact final report from runtime outputs",
                "cmd": cmd,
            }
        )

    for extra in config.get("external_steps", []):
        if not extra.get("enabled", False):
            continue
        commands.append(
            {
                "name": extra.get("name", "external_step"),
                "description": extra.get("description", "External upstream/downstream step"),
                "cmd": normalize_command(extra.get("command", [])),
            }
        )

    return commands


def run_step(step: dict, *, dry_run: bool) -> dict:
    print(f"\n[STEP] {step['name']}")
    print(step["description"])
    print("Command:")
    print("  " + " ".join(step["cmd"]))

    if dry_run:
        return {"name": step["name"], "status": "dry_run"}

    completed = subprocess.run(
        step["cmd"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    print(completed.stdout.rstrip())
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return {
            "name": step["name"],
            "status": "error",
            "returncode": completed.returncode,
        }
    return {"name": step["name"], "status": "completed"}


def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))
    commands = build_step_commands(config)

    if not commands:
        raise SystemExit("No enabled steps found in config.")

    print(f"[PIPELINE] {config.get('pipeline_name', 'unnamed_pipeline')}")
    print(config.get("description", ""))

    results = []
    for step in commands:
        result = run_step(step, dry_run=args.dry_run)
        results.append(result)
        if result["status"] == "error" and args.stop_on_error:
            break

    summary = {
        "num_steps": len(results),
        "completed": sum(1 for item in results if item["status"] == "completed"),
        "dry_run": sum(1 for item in results if item["status"] == "dry_run"),
        "errors": sum(1 for item in results if item["status"] == "error"),
    }

    print("\n[SUMMARY]")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
