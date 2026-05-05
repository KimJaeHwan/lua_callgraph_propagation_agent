"""Helpers for persistent user-managed manual force anchors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ida_types import available_type_pack, build_function_signature, load_type_declarations


def load_manual_force_anchors(path: str | Path) -> list[dict[str, str]]:
    anchor_path = Path(path)
    if not str(anchor_path).strip() or str(anchor_path) == ".":
        return []
    if not anchor_path.exists():
        return []
    data = json.loads(anchor_path.read_text(encoding="utf-8"))
    rows = data.get("anchors") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "query_func": str(row.get("query_func") or "").strip(),
                "entry_point": _normalize_entry_point(row.get("entry_point") or ""),
                "reference_func": str(
                    row.get("reference_func")
                    or row.get("predicted_function_name")
                    or row.get("reference_function_name")
                    or ""
                ).strip(),
                "reason": str(row.get("reason") or "manual_verified").strip(),
            }
        )
    return result


def apply_manual_force_anchors(
    *,
    seed_anchor_json: str | Path,
    query_json: str | Path,
    manual_force_anchors_json: str | Path,
) -> dict[str, Any]:
    seed_path = Path(seed_anchor_json)
    query_path = Path(query_json)
    manual_path = Path(manual_force_anchors_json)
    if not str(manual_path).strip() or str(manual_path) == ".":
        return {
            "manual_force_anchors_json": str(manual_path),
            "registered": [],
            "skipped": [],
            "errors": [],
            "changed": False,
        }

    anchors = load_manual_force_anchors(manual_path)
    if not anchors:
        return {
            "manual_force_anchors_json": str(manual_path),
            "registered": [],
            "skipped": [],
            "errors": [],
            "changed": False,
        }
    if not seed_path.exists():
        return {
            "manual_force_anchors_json": str(manual_path),
            "registered": [],
            "skipped": [],
            "errors": [f"seed_anchor_json not found: {seed_path}"],
            "changed": False,
        }
    if not query_path.exists():
        return {
            "manual_force_anchors_json": str(manual_path),
            "registered": [],
            "skipped": [],
            "errors": [f"query_json not found: {query_path}"],
            "changed": False,
        }

    seed_data = _load_json(seed_path)
    entry_to_query = _build_entry_to_query_map(query_path)
    existing = {
        str(mapping.get("query_function_name") or ""): str(mapping.get("reference_function_name") or "")
        for mapping in seed_data.get("mappings", [])
        if isinstance(mapping, dict)
    }
    registered: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for anchor in anchors:
        reference_func = anchor["reference_func"]
        if not reference_func:
            errors.append(f"missing_reference_func:{anchor}")
            continue
        query_func = anchor["query_func"]
        if not query_func:
            query_func = entry_to_query.get(anchor["entry_point"], "")
        if not query_func:
            errors.append(
                f"unresolved_query_func:{anchor.get('entry_point') or '<missing_entry_point>'}->{reference_func}"
            )
            continue
        current_ref = existing.get(query_func, "")
        if current_ref:
            if current_ref == reference_func:
                skipped.append(f"{query_func} (already {reference_func})")
            else:
                errors.append(f"conflicting_anchor:{query_func}:{current_ref}!={reference_func}")
            continue
        seed_data.setdefault("mappings", []).append(
            {
                "query_function_name": query_func,
                "reference_function_name": reference_func,
                "confidence": 1.0,
                "source": "force_anchor",
                "status": "accepted",
                "evidence": [f"force_registered_by_manual_anchor: {anchor['reason']}"],
            }
        )
        existing[query_func] = reference_func
        registered.append(f"{query_func} → {reference_func}")

    changed = bool(registered)
    if changed:
        seed_path.write_text(json.dumps(seed_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "manual_force_anchors_json": str(manual_path),
        "registered": registered,
        "skipped": skipped,
        "errors": errors,
        "changed": changed,
    }


def apply_manual_force_anchor_ida_updates(
    *,
    ida: Any,
    manual_force_anchors_json: str | Path,
    lua_version: str,
    ida_type_root: str = "",
    ida_signature_db: str = "",
    vanilla_source_root: str = "",
    type_mode: str = "vanilla_headers",
    enable_type_injection: bool = True,
) -> dict[str, Any]:
    manual_path = Path(manual_force_anchors_json)
    anchors = load_manual_force_anchors(manual_path)
    if not anchors:
        return {
            "manual_force_anchors_json": str(manual_path),
            "renamed": [],
            "skipped": [],
            "errors": [],
        }

    renamed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    type_declared = False

    for anchor in anchors:
        entry_point = anchor["entry_point"]
        reference_func = anchor["reference_func"]
        if not entry_point or not reference_func:
            skipped.append(f"missing_fields:{anchor}")
            continue
        opened = ida.open_function(entry_point)
        if not opened.ok:
            errors.append(f"ida_lookup_failed:{entry_point}:{opened.error}")
            continue
        fn_info = opened.result.get("function") or {}
        current_name = str(fn_info.get("name") or "")
        resolved_addr = _normalize_entry_point(fn_info.get("addr") or "")
        if resolved_addr and resolved_addr != _normalize_entry_point(entry_point):
            errors.append(f"ida_boundary_mismatch:{entry_point}->{resolved_addr}")
            continue
        if current_name == reference_func:
            skipped.append(f"{entry_point} already {reference_func}")
        else:
            rename = ida.rename_function(entry_point, reference_func)
            if not rename.ok:
                errors.append(f"ida_rename_failed:{entry_point}:{reference_func}:{rename.error}")
                continue
            renamed.append(f"{entry_point} -> {reference_func}")
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
                if not declare.ok:
                    errors.append(f"ida_type_declare_failed:{lua_version}:{declare.error}")
                    continue
                type_declared = True
            signature = build_function_signature(
                lua_version,
                reference_func,
                reference_func,
                configured_db_path=ida_signature_db,
                vanilla_source_root=vanilla_source_root,
            )
            if signature:
                set_type = ida.set_function_signature(entry_point, signature)
                if not set_type.ok:
                    errors.append(f"ida_set_type_failed:{entry_point}:{reference_func}:{set_type.error}")
    return {
        "manual_force_anchors_json": str(manual_path),
        "renamed": renamed,
        "skipped": skipped,
        "errors": errors,
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_entry_to_query_map(query_json: Path) -> dict[str, str]:
    data = _load_json(query_json)
    rows = data if isinstance(data, list) else (data.get("functions") or data.get("query_functions") or [])
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        query_func = str(row.get("function_name") or "").strip()
        entry_point = _normalize_entry_point(row.get("entry_point") or "")
        if query_func and entry_point:
            result[entry_point] = query_func
    return result


def _normalize_entry_point(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("0x"):
        text = text[2:]
    return text
