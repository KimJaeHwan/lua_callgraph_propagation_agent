#!/usr/bin/env python3
"""
Run an optional Local LLM analyst over deferred mapping cases.

This script is intentionally separate from deterministic propagation. The core
pipeline should work without an LLM; this adapter only consumes compact
deferred-analysis payloads and writes analyst suggestions.

Supported modes:

  1. Dry run / prompt export:

     python3 scripts/06_run_local_llm_analyst.py \
       --input-json data/eval/results/representative/deferred_analysis_lua547.json \
       --output-json data/eval/results/representative/llm_analysis_lua547_dryrun.json \
       --dry-run

  2. Ollama local API:

     python3 scripts/06_run_local_llm_analyst.py \
       --input-json data/eval/results/representative/deferred_analysis_lua547.json \
       --output-json data/eval/results/representative/llm_analysis_lua547.json \
       --provider ollama \
       --model qwen2.5-coder:7b

  3. OpenAI-compatible local API, such as LM Studio:

     python3 scripts/06_run_local_llm_analyst.py \
       --provider openai-compatible \
       --base-url http://localhost:1234/v1 \
       --model qwen/qwen3.6-35b-a3b

The LLM output is advisory evidence only. It must not directly overwrite the
accepted/deferred/conflict status from deterministic scoring.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("data/eval/results/representative/deferred_analysis_lua547.json")
DEFAULT_OUTPUT = Path("data/eval/results/representative/llm_analysis_lua547_dryrun.json")


SYSTEM_PROMPT = """\
You are a reverse-engineering analyst helping with Lua function mapping.

Rules:
- Treat query_func and expected_function as debug/eval labels, not identity evidence.
- Use only feature summary, retrieval scores, candidate roles, and callgraph evidence.
- Do not claim a vulnerability exists.
- Prefer conservative recommendations. If evidence is weak, recommend remain_deferred.
- Return strict JSON matching the requested answer format.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local LLM analyst on deferred callgraph propagation cases."
    )
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--provider",
        choices=["dry-run", "ollama", "openai-compatible"],
        default="dry-run",
        help="LLM provider. Use dry-run to export prompts without calling a model.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force prompt export without calling a model",
    )
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--response-format-json",
        action="store_true",
        help="send OpenAI response_format=json_object. Disabled by default for LM Studio compatibility.",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="limit number of deferred/conflict cases for quick tests",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def post_json(url: str, payload: dict, *, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def trim_payload(payload: dict) -> dict:
    """Keep prompts compact and stable across local model context windows."""
    result = dict(payload)
    query_summary = dict(result.get("query_feature_summary") or {})
    query_summary["top_pcode_opcodes"] = query_summary.get("top_pcode_opcodes", [])[:8]
    query_summary["strings"] = query_summary.get("strings", [])[:12]
    result["query_feature_summary"] = query_summary
    result["candidate_summaries"] = result.get("candidate_summaries", [])[:5]
    return result


def build_user_prompt(case: dict) -> str:
    payload = trim_payload(case.get("llm_payload") or {})
    compact_case = {
        "case_id": case.get("case_id"),
        "review_category": case.get("review_category"),
        "recommended_action_from_rules": case.get("recommended_action"),
        "score_margin_top1_top2": case.get("score_margin_top1_top2"),
        "top_tied_candidates": case.get("top_tied_candidates", []),
        "llm_payload": payload,
    }
    return textwrap.dedent(
        f"""\
        Analyze this deferred Lua function mapping case.

        Return strict JSON with this shape:
        {{
          "classification": "prefer_candidate | remain_deferred | need_more_anchors | possible_custom",
          "recommended_candidate": "string or null",
          "confidence": 0.0,
          "reasoning_summary": ["short evidence bullet"],
          "risks": ["short risk bullet"]
        }}

        Case:
        {json.dumps(compact_case, indent=2, ensure_ascii=False)}
        """
    )


def dry_run_result(case: dict, prompt: str) -> dict:
    return {
        "case_id": case.get("case_id"),
        "provider": "dry-run",
        "model": None,
        "status": "prompt_exported",
        "prompt": prompt,
        "analysis": None,
    }


def call_ollama(case: dict, prompt: str, *, model: str, base_url: str | None, timeout: float) -> dict:
    url = (base_url or "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": SYSTEM_PROMPT + "\n\n" + prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    response = post_json(url, payload, timeout=timeout)
    text = response.get("response", "")
    return {
        "case_id": case.get("case_id"),
        "provider": "ollama",
        "model": model,
        "status": "completed",
        "raw_response": text,
        "analysis": parse_model_json(text),
    }


def call_openai_compatible(
    case: dict,
    prompt: str,
    *,
    model: str,
    base_url: str | None,
    temperature: float,
    timeout: float,
    response_format_json: bool,
) -> dict:
    url = (base_url or "http://localhost:1234/v1").rstrip("/") + "/chat/completions"
    api_key = os.environ.get("OPENAI_API_KEY", "local-not-needed")
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    text = result["choices"][0]["message"]["content"]
    return {
        "case_id": case.get("case_id"),
        "provider": "openai-compatible",
        "model": model,
        "status": "completed",
        "raw_response": text,
        "analysis": parse_model_json(text),
    }


def parse_model_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def run_case(case: dict, args: argparse.Namespace) -> dict:
    prompt = build_user_prompt(case)
    if args.dry_run or args.provider == "dry-run":
        return dry_run_result(case, prompt)
    try:
        if args.provider == "ollama":
            return call_ollama(
                case,
                prompt,
                model=args.model,
                base_url=args.base_url,
                timeout=args.timeout,
            )
        return call_openai_compatible(
            case,
            prompt,
            model=args.model,
            base_url=args.base_url,
            temperature=args.temperature,
            timeout=args.timeout,
            response_format_json=args.response_format_json,
        )
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        return {
            "case_id": case.get("case_id"),
            "provider": args.provider,
            "model": args.model,
            "status": "error",
            "error": str(exc),
            "prompt": prompt,
            "analysis": None,
        }


def main() -> None:
    args = parse_args()
    source = load_json(args.input_json)
    cases = source.get("deferred_cases", []) + source.get("conflict_cases", [])
    if args.max_cases is not None:
        cases = cases[: args.max_cases]

    results = [run_case(case, args) for case in cases]
    output = {
        "schema_version": "0.1",
        "description": (
            "Local LLM analyst output for deferred/conflict Lua mapping cases. "
            "These are advisory suggestions only."
        ),
        "input_json": str(args.input_json),
        "provider": "dry-run" if args.dry_run else args.provider,
        "model": None if args.dry_run or args.provider == "dry-run" else args.model,
        "summary": {
            "num_cases": len(results),
            "completed": sum(1 for item in results if item["status"] == "completed"),
            "prompt_exported": sum(1 for item in results if item["status"] == "prompt_exported"),
            "errors": sum(1 for item in results if item["status"] == "error"),
        },
        "results": results,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[OK] wrote LLM analyst output: {args.output_json}")
    print(json.dumps(output["summary"], indent=2, ensure_ascii=False))
    for item in results:
        print(f"{item['case_id']}: {item['status']} provider={item['provider']}")


if __name__ == "__main__":
    main()
