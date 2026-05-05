#!/usr/bin/env python3
"""Apply accepted final-report names into IDA as a manual plateau-stage step.

This script is intentionally more permissive than the online LLM verification
path, but still keeps a few safety rails:

- dry-run by default
- skips duplicate predicted names unless explicitly allowed
- skips functions whose current IDA name is already non-internal unless
  --overwrite-noninternal is passed

Typical use:

  python scripts/23_apply_accepted_ida_renames.py \
      --config data/runtime/results/<session>/runtime_config.json \
      --ida-url http://127.0.0.1:13337/mcp

Then review the dry-run output and rerun with:

  --apply
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Any
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lua_callgraph_propagation_agent.langgraph_agent import CodexIdaMcpClient
from lua_callgraph_propagation_agent.langgraph_agent.config import load_config, resolve_paths, resolve_target_lua_version
from lua_callgraph_propagation_agent.langgraph_agent.ida_types import available_type_pack, build_function_signature, load_type_declarations


INTERNAL_PREFIXES = ("sub_", "FUN_", "nullsub_", "j_", "off_", "loc_", "unk_")


class HttpMcpSession:
    """Simple synchronous JSON-RPC wrapper for FastMCP HTTP servers."""

    def __init__(self, url: str, *, timeout_seconds: float = 60.0):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._initialized = False
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}

    def _post_notification(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds):
            return

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._post_json(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "accepted-rename-sync", "version": "0.1.0"},
                },
            }
        )
        self._post_notification(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": None}
        )
        self._initialized = True

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        result = self._post_json(payload)
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        rpc_result = result.get("result") or {}
        if isinstance(rpc_result.get("structuredContent"), dict):
            payload = dict(rpc_result["structuredContent"])
        elif isinstance(rpc_result, dict):
            payload = dict(rpc_result)
        else:
            payload = {"content": rpc_result}
        payload.setdefault("ok", True)
        return payload


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_existing_path(value: str) -> Path:
    candidate = Path(value)
    search_roots = [
        Path.cwd(),
        PROJECT_ROOT,
        PROJECT_ROOT.parent,
    ]
    if candidate.is_absolute():
        return candidate
    for root in search_roots:
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return (Path.cwd() / candidate).resolve()


def _build_query_entry_map(query_json: Path) -> dict[str, str]:
    data = _load_json(query_json)
    rows = data if isinstance(data, list) else (data.get("functions") or data.get("query_functions") or [])
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fn = str(row.get("function_name") or "").strip()
        ep = str(row.get("entry_point") or "").strip()
        if fn and ep:
            result[fn] = ep
    return result


def _accepted_rows(final_report: Path) -> list[dict[str, Any]]:
    data = _load_json(final_report)
    return [row for row in data.get("accepted", []) if isinstance(row, dict)]


def _entry_point_hex(row: dict[str, Any], query_map: dict[str, str]) -> str:
    case_id = str(row.get("case_id") or "")
    if "@" in case_id:
        return case_id.rsplit("@", 1)[-1].lower().removeprefix("0x")
    query_func = str(row.get("query_func") or "")
    return str(query_map.get(query_func) or "").lower().removeprefix("0x")


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part for part in (s.strip() for s in value.split(",")) if part)


def _is_internal_name(name: str) -> bool:
    return name.startswith(INTERNAL_PREFIXES)


def _graph_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("graph_config") or config.get("agent", {}).get("graph_config") or {})


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename accepted final-report mappings in IDA")
    parser.add_argument("--config", required=True, help="runtime config JSON")
    parser.add_argument("--ida-url", default="http://127.0.0.1:13337/mcp", help="IDA MCP HTTP URL")
    parser.add_argument("--apply", action="store_true", help="actually apply renames")
    parser.add_argument(
        "--only-prefixes",
        default="",
        help="comma-separated predicted name prefixes to include (example: luaD_,luaZ_,luaV_finish,luaopen_)",
    )
    parser.add_argument(
        "--exclude-prefixes",
        default="",
        help="comma-separated predicted name prefixes to exclude",
    )
    parser.add_argument(
        "--overwrite-noninternal",
        action="store_true",
        help="allow renaming functions that already have a non-internal IDA name",
    )
    parser.add_argument(
        "--allow-duplicate-names",
        action="store_true",
        help="allow renaming multiple functions to the same predicted name",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    paths = resolve_paths(config)
    final_report = _resolve_existing_path(str(paths["final_report_json"]))
    query_json = _resolve_existing_path(str(paths["query_feature_json"]))
    if not final_report.exists():
        raise SystemExit(f"final report missing: {final_report}")
    if not query_json.exists():
        raise SystemExit(f"query feature json missing: {query_json}")

    accepted = _accepted_rows(final_report)
    query_map = _build_query_entry_map(query_json)
    include_prefixes = _split_csv(args.only_prefixes)
    exclude_prefixes = _split_csv(args.exclude_prefixes)

    prepared: list[dict[str, Any]] = []
    duplicate_counts = Counter(
        str(row.get("predicted_function_name") or "").strip()
        for row in accepted
        if str(row.get("predicted_function_name") or "").strip()
    )

    skipped = Counter()
    for row in accepted:
        predicted = str(row.get("predicted_function_name") or "").strip()
        if not predicted:
            skipped["missing_name"] += 1
            continue
        if include_prefixes and not predicted.startswith(include_prefixes):
            skipped["prefix_filtered"] += 1
            continue
        if exclude_prefixes and predicted.startswith(exclude_prefixes):
            skipped["prefix_excluded"] += 1
            continue
        if not args.allow_duplicate_names and duplicate_counts[predicted] > 1:
            skipped["duplicate_predicted_name"] += 1
            continue
        entry = _entry_point_hex(row, query_map)
        if not entry:
            skipped["missing_entry_point"] += 1
            continue
        prepared.append(
            {
                "entry_point": entry,
                "new_name": predicted,
                "query_func": row.get("query_func"),
                "status_reasons": row.get("status_reasons") or [],
                "propagation_round": row.get("propagation_round"),
            }
        )

    session = HttpMcpSession(args.ida_url)
    ida = CodexIdaMcpClient(session)
    lua_version = resolve_target_lua_version(config)
    graph_cfg = _graph_cfg(config)
    type_mode = str(graph_cfg.get("ida_type_injection_mode") or "vanilla_headers")
    enable_type_injection = bool(graph_cfg.get("enable_ida_type_injection", True))
    ida_type_root = str(paths.get("ida_type_root") or "")
    ida_signature_db = str(paths.get("ida_signature_db") or "")
    vanilla_source_root = str(paths.get("vanilla_lua_source_root") or "")
    type_declared = False

    rename_plan: list[dict[str, Any]] = []
    for item in prepared:
        opened = ida.open_function(item["entry_point"])
        if not opened.ok:
            skipped["ida_lookup_failed"] += 1
            continue
        fn_info = opened.result.get("function") or {}
        current_name = str(fn_info.get("name") or "")
        resolved_addr = str(fn_info.get("addr") or "").lower().removeprefix("0x")
        if resolved_addr and resolved_addr != item["entry_point"].lower().removeprefix("0x"):
            skipped["ida_boundary_mismatch"] += 1
            continue
        if current_name and not _is_internal_name(current_name) and not args.overwrite_noninternal:
            skipped["already_named"] += 1
            continue
        rename_plan.append({**item, "current_name": current_name})

    print(f"accepted rows            : {len(accepted)}")
    print(f"prepared rename targets  : {len(prepared)}")
    print(f"eligible rename targets  : {len(rename_plan)}")
    if skipped:
        print("skipped:")
        for key, value in sorted(skipped.items()):
            print(f"  {key}: {value}")

    for item in rename_plan[:50]:
        print(
            f"{item['entry_point']:>10}  "
            f"{item['current_name'] or '-':<24} -> {item['new_name']}  "
            f"[round={item['propagation_round']} reason={','.join(item['status_reasons'])}]"
        )
    if len(rename_plan) > 50:
        print(f"... {len(rename_plan) - 50} more")

    if not args.apply:
        print("\ndry-run only. Re-run with --apply to rename in IDA.")
        return 0

    renamed = 0
    failed = 0
    type_failed = 0
    for item in rename_plan:
        result = ida.rename_function(item["entry_point"], item["new_name"])
        if result.ok:
            renamed += 1
            if enable_type_injection:
                if not type_declared and available_type_pack(
                    lua_version,
                    ida_type_root,
                    mode=type_mode,
                    vanilla_source_root=vanilla_source_root,
                ):
                    declare = ida.declare_types(
                        load_type_declarations(
                            lua_version,
                            ida_type_root,
                            mode=type_mode,
                            vanilla_source_root=vanilla_source_root,
                        )
                    )
                    if declare.ok:
                        type_declared = True
                    else:
                        type_failed += 1
                        print(
                            f"type_declare_failed {lua_version}: {declare.error}",
                            file=sys.stderr,
                        )
                signature = build_function_signature(
                    lua_version,
                    item["new_name"],
                    item["new_name"],
                    configured_db_path=ida_signature_db,
                    vanilla_source_root=vanilla_source_root,
                )
                if signature:
                    set_type = ida.set_function_signature(item["entry_point"], signature)
                    if not set_type.ok:
                        type_failed += 1
                        print(
                            f"type_apply_failed {item['entry_point']} {item['new_name']}: {set_type.error}",
                            file=sys.stderr,
                        )
        else:
            failed += 1
            print(
                f"rename_failed {item['entry_point']} {item['current_name']} -> {item['new_name']}: "
                f"{result.error}",
                file=sys.stderr,
            )

    print(f"\nrenamed: {renamed}")
    print(f"failed : {failed}")
    print(f"type_failed: {type_failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
