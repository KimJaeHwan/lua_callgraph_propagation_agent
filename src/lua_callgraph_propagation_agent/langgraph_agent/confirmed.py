"""Helpers for converting verified decisions into force anchors and patch maps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import VerificationDecision, normalize_entry_point


class ConfirmedMapBuilder:
    """Build both MCP force-anchor payloads and feature-patch confirmed maps.

    The Lua MCP uses two shapes:
    - ``batch_register_force_anchors``: query function name -> reference name
    - ``patch_features_with_confirmed``: entry_point hex -> reference name

    This helper keeps that conversion explicit and testable.
    """

    def __init__(self, query_json: str | Path):
        self.query_json = Path(query_json)
        self._query_index: dict[str, dict[str, Any]] | None = None

    def load_query_index(self) -> dict[str, dict[str, Any]]:
        if self._query_index is not None:
            return self._query_index
        if not self.query_json.exists():
            self._query_index = {}
            return self._query_index

        data = json.loads(self.query_json.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rows = data.get("functions") or data.get("results") or []
        else:
            rows = data
        self._query_index = {
            str(row.get("function_name")): row
            for row in rows
            if isinstance(row, dict) and row.get("function_name")
        }
        return self._query_index

    def query_func_to_entry_point(self, query_func: str) -> str:
        row = self.load_query_index().get(query_func, {})
        return normalize_entry_point(row.get("entry_point")) if row else ""

    def build(self, decisions: list[VerificationDecision]) -> dict[str, str]:
        confirmed: dict[str, str] = {}
        for decision in decisions:
            if not decision.accepted or not decision.candidate_name:
                continue
            entry_point = normalize_entry_point(decision.entry_point) if decision.entry_point else ""
            if not entry_point or entry_point == "0":
                entry_point = self.query_func_to_entry_point(decision.query_func)
            if entry_point and entry_point != "0":
                existing = confirmed.get(entry_point)
                if existing and existing != decision.candidate_name:
                    continue
                confirmed[entry_point] = decision.candidate_name
        return confirmed

    def to_force_anchors(self, decisions: list[VerificationDecision]) -> list[dict[str, str]]:
        anchors = []
        seen: set[str] = set()
        for decision in decisions:
            if not decision.accepted or not decision.query_func or not decision.candidate_name:
                continue
            if decision.query_func in seen:
                continue
            anchors.append(decision.to_force_anchor())
            seen.add(decision.query_func)
        return anchors
