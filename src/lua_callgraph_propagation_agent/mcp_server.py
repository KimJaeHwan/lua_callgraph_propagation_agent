from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP


PROJECT_ROOT = Path(__file__).resolve().parents[2]

mcp = FastMCP(
    name="lua-callgraph-propagation-agent",
    instructions=(
        "Run the deterministic Lua name-mapping runtime pipeline for analysis targets, "
        "inspect final mapping reports, and optionally invoke the deferred local LLM analyst."
    ),
    version="0.3.0",
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


@mcp.tool(description="Run the optional local LLM analyst over deferred cases.")
def run_local_llm_analyst(
    provider: str,
    input_json: str,
    output_json: str,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
    max_cases: int | None = None,
    response_format_json: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/06_run_local_llm_analyst.py",
        "--provider",
        provider,
        "--input-json",
        str(_resolve_path(input_json)),
        "--output-json",
        str(_resolve_path(output_json)),
    ]
    if dry_run:
        command.append("--dry-run")
    if base_url:
        command.extend(["--base-url", base_url])
    if model:
        command.extend(["--model", model])
    if temperature is not None:
        command.extend(["--temperature", str(temperature)])
    if timeout is not None:
        command.extend(["--timeout", str(timeout)])
    if max_cases is not None:
        command.extend(["--max-cases", str(max_cases)])
    if response_format_json:
        command.append("--response-format-json")
    return _run_command(command)


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


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


__all__ = ["main", "mcp"]
