"""SQLite-backed Lua function signature catalog built from vanilla sources."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SIGNATURE_DB_PATH = REPO_ROOT / "data" / "inputs" / "ida_types" / "lua_function_signatures.sqlite"
DEFAULT_VANILLA_SOURCE_ROOT = REPO_ROOT.parent / "lua_custom_engine_generator" / "lua_source_vanilla"

VANILLA_SRC_DIR = {
    "Lua_524": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.2.4" / "src",
    "Lua_536": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.3.6" / "src",
    "Lua_547": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.4.7" / "src",
}

CLANG_BIN = "clang"


@dataclass(slots=True)
class SignatureRecord:
    lua_version: str
    function_name: str
    return_type: str
    parameters: str
    source_file: str
    origin_kind: str
    is_static: bool
    precedence: int

    @property
    def signature(self) -> str:
        return f"{self.return_type} {self.function_name}({self.parameters});"


def resolve_signature_db_path(configured_path: str | None = None) -> Path:
    if configured_path:
        candidate = Path(configured_path)
        if candidate.is_absolute():
            return candidate
        cwd_resolved = (Path.cwd() / candidate).resolve()
        if cwd_resolved.exists():
            return cwd_resolved
        return (REPO_ROOT / candidate).resolve()
    return DEFAULT_SIGNATURE_DB_PATH


def resolve_vanilla_source_root(configured_root: str | None = None) -> Path:
    if configured_root:
        candidate = Path(configured_root)
        if candidate.is_absolute():
            return candidate
        cwd_resolved = (Path.cwd() / candidate).resolve()
        if cwd_resolved.exists():
            return cwd_resolved
        return (REPO_ROOT / candidate).resolve()
    return DEFAULT_VANILLA_SOURCE_ROOT


def resolve_vanilla_src_dir(lua_version: str, configured_root: str | None = None) -> Path:
    if not configured_root:
        return VANILLA_SRC_DIR.get(lua_version, DEFAULT_VANILLA_SOURCE_ROOT / lua_version / "src")
    return resolve_vanilla_source_root(configured_root) / _vanilla_dirname(lua_version) / "src"


def ensure_signature_db(
    configured_db_path: str | None = None,
    vanilla_source_root: str | None = None,
) -> Path:
    db_path = resolve_signature_db_path(configured_db_path)
    if db_path.exists():
        return db_path
    build_signature_db(db_path, vanilla_source_root=vanilla_source_root)
    return db_path


def lookup_signature_record(
    lua_version: str,
    function_name: str,
    *,
    configured_db_path: str | None = None,
    vanilla_source_root: str | None = None,
) -> SignatureRecord | None:
    db_path = ensure_signature_db(configured_db_path, vanilla_source_root)
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            """
            SELECT lua_version, function_name, return_type, parameters, source_file,
                   origin_kind, is_static, precedence
            FROM function_signatures
            WHERE lua_version = ? AND function_name = ?
            ORDER BY precedence ASC, is_static ASC, source_file ASC
            LIMIT 1
            """,
            (lua_version, function_name),
        ).fetchone()
        if not row:
            return None
        return SignatureRecord(
            lua_version=row[0],
            function_name=row[1],
            return_type=row[2],
            parameters=row[3],
            source_file=row[4],
            origin_kind=row[5],
            is_static=bool(row[6]),
            precedence=int(row[7]),
        )
    finally:
        con.close()


def build_signature_db(
    output_db: str | Path,
    *,
    vanilla_source_root: str | None = None,
    replace: bool = True,
) -> Path:
    db_path = Path(output_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if replace and db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        _create_schema(con)
        source_root = resolve_vanilla_source_root(vanilla_source_root)
        _insert_metadata(con, source_root)
        for lua_version in ("Lua_524", "Lua_536", "Lua_547"):
            src_dir = resolve_vanilla_src_dir(lua_version, str(source_root))
            if not src_dir.exists():
                continue
            rows = extract_signatures_for_version(lua_version, src_dir)
            con.executemany(
                """
                INSERT INTO function_signatures (
                    lua_version, function_name, return_type, parameters, source_file,
                    origin_kind, is_static, precedence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.lua_version,
                        row.function_name,
                        row.return_type,
                        row.parameters,
                        row.source_file,
                        row.origin_kind,
                        int(row.is_static),
                        row.precedence,
                    )
                    for row in rows
                ],
            )
        con.commit()
    finally:
        con.close()
    return db_path


def extract_signatures_for_version(lua_version: str, src_dir: Path) -> list[SignatureRecord]:
    records: dict[tuple[str, str, str], SignatureRecord] = {}
    for path in sorted(src_dir.glob("*.c")):
        for record in _extract_file_records(lua_version, src_dir, path):
            key = (record.function_name, record.source_file, record.origin_kind)
            existing = records.get(key)
            if existing is None:
                records[key] = record
                continue
            if record.precedence < existing.precedence:
                records[key] = record
                continue
            if len(record.parameters) > len(existing.parameters):
                records[key] = record
    return sorted(records.values(), key=lambda row: (row.lua_version, row.function_name, row.precedence, row.source_file))


def _extract_file_records(lua_version: str, src_dir: Path, path: Path) -> Iterable[SignatureRecord]:
    ast = _dump_ast_json(src_dir, path)
    for node in _walk_ast(ast):
        if node.get("kind") != "FunctionDecl":
            continue
        name = str(node.get("name") or "")
        if not name.startswith("lua"):
            continue
        return_type, _ = _split_function_type(str((node.get("type") or {}).get("qualType") or ""))
        if not return_type:
            continue
        parameters = _build_parameter_list(node)
        is_definition = any(child.get("kind") == "CompoundStmt" for child in node.get("inner", []) if isinstance(child, dict))
        if not is_definition:
            continue
        origin_kind = "definition"
        precedence = 10
        source_rel = str(path.relative_to(src_dir))
        yield SignatureRecord(
            lua_version=lua_version,
            function_name=name,
            return_type=return_type,
            parameters=parameters,
            source_file=source_rel,
            origin_kind=origin_kind,
            is_static=str(node.get("storageClass") or "") == "static",
            precedence=precedence,
        )


def _dump_ast_json(src_dir: Path, path: Path) -> dict[str, Any]:
    lang = "c-header" if path.suffix == ".h" else "c"
    cmd = [
        CLANG_BIN,
        "-I",
        str(src_dir),
        "-fsyntax-only",
        "-x",
        lang,
        "-Xclang",
        "-ast-dump=json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"clang_ast_dump_failed:{path}:{result.stderr.strip()}")
    return json.loads(result.stdout)


def _walk_ast(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        inner = current.get("inner") or []
        for child in reversed(inner):
            if isinstance(child, dict):
                stack.append(child)


def _split_function_type(qual_type: str) -> tuple[str, str]:
    if not qual_type or "(" not in qual_type or ")" not in qual_type:
        return "", ""
    open_index = qual_type.find("(")
    close_index = qual_type.rfind(")")
    return qual_type[:open_index].strip(), qual_type[open_index + 1:close_index].strip()


def _build_parameter_list(node: dict[str, Any]) -> str:
    qual_type = str((node.get("type") or {}).get("qualType") or "")
    _, raw_params = _split_function_type(qual_type)
    params: list[str] = []
    for child in node.get("inner", []):
        if not isinstance(child, dict) or child.get("kind") != "ParmVarDecl":
            continue
        type_text = str((child.get("type") or {}).get("qualType") or "").strip()
        name_text = str(child.get("name") or "").strip()
        if name_text:
            params.append(f"{type_text} {name_text}")
        elif type_text:
            params.append(type_text)
    if "..." in raw_params:
        params.append("...")
    if not params:
        if raw_params == "void":
            return "void"
        return raw_params or "void"
    return ", ".join(params)


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE function_signatures (
            lua_version TEXT NOT NULL,
            function_name TEXT NOT NULL,
            return_type TEXT NOT NULL,
            parameters TEXT NOT NULL,
            source_file TEXT NOT NULL,
            origin_kind TEXT NOT NULL,
            is_static INTEGER NOT NULL,
            precedence INTEGER NOT NULL,
            PRIMARY KEY (lua_version, function_name, source_file, origin_kind)
        );

        CREATE INDEX idx_function_signatures_lookup
            ON function_signatures (lua_version, function_name, precedence, is_static);
        """
    )


def _insert_metadata(con: sqlite3.Connection, source_root: Path) -> None:
    items = [
        ("source_root", str(source_root)),
        ("builder", "clang_ast_signature_db"),
        ("storage_model", "sqlite_signature_catalog"),
    ]
    con.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", items)


def _vanilla_dirname(lua_version: str) -> str:
    return {
        "Lua_524": "lua-5.2.4",
        "Lua_536": "lua-5.3.6",
        "Lua_547": "lua-5.4.7",
    }.get(lua_version, lua_version)
