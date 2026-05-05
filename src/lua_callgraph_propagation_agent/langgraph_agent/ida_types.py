"""Versioned IDA type-pack helpers for Lua analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .signature_db import lookup_signature_record

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_IDA_TYPE_ROOT = REPO_ROOT / "data" / "inputs" / "ida_types"
DEFAULT_VANILLA_SOURCE_ROOT = REPO_ROOT.parent / "lua_custom_engine_generator" / "lua_source_vanilla"
DEFAULT_TYPE_MODE = "vanilla_headers"

VERSION_DIR = {
    "Lua_524": "Lua_524",
    "Lua_536": "Lua_536",
    "Lua_547": "Lua_547",
}

VANILLA_SRC_DIR = {
    "Lua_524": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.2.4" / "src",
    "Lua_536": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.3.6" / "src",
    "Lua_547": DEFAULT_VANILLA_SOURCE_ROOT / "lua-5.4.7" / "src",
}

VANILLA_HEADER_SEQUENCE = (
    "lua.h",
    "llimits.h",
    "lmem.h",
    "lzio.h",
    "lobject.h",
    "ltm.h",
    "lstate.h",
    "ldo.h",
    "lvm.h",
    "lundump.h",
)

_C_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_DIRECTIVE_RE = re.compile(r"^\s*#\s*(\w+)(.*)$")
_LOCAL_INCLUDE_RE = re.compile(r'^\s*"([^"]+)"\s*$')
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_DEFINED_PAREN_RE = re.compile(r"defined\s*\(\s*([A-Za-z_]\w*)\s*\)")
_DEFINED_BARE_RE = re.compile(r"defined\s+([A-Za-z_]\w*)")

_TYPE_PACK_PRELUDE = """/* Generated from vanilla Lua headers for IDA type injection. */
typedef unsigned long long size_t;
typedef long long ptrdiff_t;
typedef char *va_list;
typedef int sig_atomic_t;
typedef long long intptr_t;
typedef unsigned long long uintptr_t;
"""

_PREDEFINED_MACROS = {
    "__GNUC__": "4",
    "__GNUC_MINOR__": "2",
    "__ELF__": "1",
    "__STDC_VERSION__": "199901L",
    "INTPTR_MAX": "9223372036854775807LL",
    "LLONG_MAX": "9223372036854775807LL",
    "LLONG_MIN": "(-9223372036854775807LL - 1)",
    "ULLONG_MAX": "18446744073709551615ULL",
}

@dataclass(slots=True)
class _ConditionalFrame:
    parent_active: bool
    branch_taken: bool
    active: bool


def resolve_ida_type_root(configured_root: str | None = None) -> Path:
    if configured_root:
        candidate = Path(configured_root)
        if candidate.is_absolute():
            return candidate
        cwd_resolved = (Path.cwd() / candidate).resolve()
        if cwd_resolved.exists():
            return cwd_resolved
        return (REPO_ROOT / candidate).resolve()
    return DEFAULT_IDA_TYPE_ROOT


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


def resolve_type_pack_path(lua_version: str, configured_root: str | None = None) -> Path:
    version_dir = VERSION_DIR.get(lua_version, lua_version)
    return resolve_ida_type_root(configured_root) / version_dir / "lua_core_min.h"


def available_type_pack(
    lua_version: str,
    configured_root: str | None = None,
    *,
    mode: str = DEFAULT_TYPE_MODE,
    vanilla_source_root: str | None = None,
) -> bool:
    if _use_vanilla_headers(mode):
        src_dir = resolve_vanilla_src_dir(lua_version, vanilla_source_root)
        return src_dir.exists() and all((src_dir / header).exists() for header in VANILLA_HEADER_SEQUENCE)
    return resolve_type_pack_path(lua_version, configured_root).exists()


def load_type_pack(
    lua_version: str,
    configured_root: str | None = None,
    *,
    mode: str = DEFAULT_TYPE_MODE,
    vanilla_source_root: str | None = None,
) -> str:
    if _use_vanilla_headers(mode):
        src_dir = resolve_vanilla_src_dir(lua_version, vanilla_source_root)
        return _LuaHeaderPreprocessor(src_dir).render(VANILLA_HEADER_SEQUENCE)
    path = resolve_type_pack_path(lua_version, configured_root)
    return path.read_text(encoding="utf-8")


def load_type_declarations(
    lua_version: str,
    configured_root: str | None = None,
    *,
    mode: str = DEFAULT_TYPE_MODE,
    vanilla_source_root: str | None = None,
) -> list[str]:
    pack = load_type_pack(
        lua_version,
        configured_root,
        mode=mode,
        vanilla_source_root=vanilla_source_root,
    )
    return _split_type_declarations(pack)


def build_function_signature(
    lua_version: str,
    current_name: str,
    candidate_name: str,
    *,
    configured_db_path: str | None = None,
    vanilla_source_root: str | None = None,
) -> str | None:
    if not current_name or not candidate_name:
        return None
    record = lookup_signature_record(
        lua_version,
        candidate_name,
        configured_db_path=configured_db_path,
        vanilla_source_root=vanilla_source_root,
    )
    if record is None:
        return None
    return f"{record.return_type} {current_name}({record.parameters});"


def _use_vanilla_headers(mode: str | None) -> bool:
    normalized = (mode or DEFAULT_TYPE_MODE).strip().lower()
    return normalized in {"vanilla", "vanilla_headers", "original", "source"}


def _vanilla_dirname(lua_version: str) -> str:
    return {
        "Lua_524": "lua-5.2.4",
        "Lua_536": "lua-5.3.6",
        "Lua_547": "lua-5.4.7",
    }.get(lua_version, lua_version)


class _LuaHeaderPreprocessor:
    def __init__(self, src_dir: Path):
        self.src_dir = src_dir
        self.macros: dict[str, str] = dict(_PREDEFINED_MACROS)
        self.included_files: set[Path] = set()

    def render(self, roots: tuple[str, ...]) -> str:
        sections = [
            _TYPE_PACK_PRELUDE.strip(),
            f"/* Source root: {self.src_dir} */",
        ]
        for root in roots:
            rendered = self._process_file(self.src_dir / root)
            if rendered.strip():
                sections.append(f"/* BEGIN {root} */\n{rendered}\n/* END {root} */")
        return _cleanup_generated_pack("\n\n".join(section for section in sections if section.strip()))

    def _process_file(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved in self.included_files:
            return ""
        self.included_files.add(resolved)
        raw_text = _strip_c_comments(path.read_text(encoding="utf-8"))
        frames: list[_ConditionalFrame] = []
        output: list[str] = []
        for line in _logical_lines(raw_text):
            directive = _DIRECTIVE_RE.match(line)
            if directive:
                self._handle_directive(directive.group(1), directive.group(2).strip(), frames, output, path)
                continue
            if _current_active(frames):
                expanded = self._expand_object_macros(line)
                if expanded.strip():
                    output.append(expanded.rstrip())
        return "\n".join(output)

    def _handle_directive(
        self,
        name: str,
        payload: str,
        frames: list[_ConditionalFrame],
        output: list[str],
        current_path: Path,
    ) -> None:
        directive = name.lower()
        if directive == "include":
            if not _current_active(frames):
                return
            include_match = _LOCAL_INCLUDE_RE.match(payload)
            if not include_match:
                return
            include_path = (current_path.parent / include_match.group(1)).resolve()
            if include_path.exists():
                rendered = self._process_file(include_path)
                if rendered.strip():
                    output.append(rendered)
            return
        if directive == "define":
            if _current_active(frames):
                self._register_macro(payload)
            return
        if directive == "undef":
            if _current_active(frames):
                macro_name = payload.split(None, 1)[0] if payload else ""
                self.macros.pop(macro_name, None)
            return
        if directive == "ifdef":
            parent_active = _current_active(frames)
            condition = payload in self.macros
            frames.append(_ConditionalFrame(parent_active, parent_active and condition, parent_active and condition))
            return
        if directive == "ifndef":
            parent_active = _current_active(frames)
            condition = payload not in self.macros
            frames.append(_ConditionalFrame(parent_active, parent_active and condition, parent_active and condition))
            return
        if directive == "if":
            parent_active = _current_active(frames)
            condition = self._eval_condition(payload)
            frames.append(_ConditionalFrame(parent_active, parent_active and condition, parent_active and condition))
            return
        if directive == "elif":
            if not frames:
                return
            frame = frames[-1]
            if not frame.parent_active or frame.branch_taken:
                frame.active = False
                return
            condition = self._eval_condition(payload)
            frame.active = frame.parent_active and condition
            frame.branch_taken = frame.active
            return
        if directive == "else":
            if not frames:
                return
            frame = frames[-1]
            frame.active = frame.parent_active and not frame.branch_taken
            frame.branch_taken = True
            return
        if directive == "endif":
            if frames:
                frames.pop()

    def _register_macro(self, payload: str) -> None:
        if not payload:
            return
        parts = payload.split(None, 1)
        name = parts[0]
        if "(" in name:
            return
        value = parts[1] if len(parts) > 1 else "1"
        self.macros[name] = value.strip()

    def _eval_condition(self, expr: str) -> bool:
        if not expr:
            return False
        prepared = _DEFINED_PAREN_RE.sub(lambda m: "1" if m.group(1) in self.macros else "0", expr)
        prepared = _DEFINED_BARE_RE.sub(lambda m: "1" if m.group(1) in self.macros else "0", prepared)
        prepared = self._expand_expression_macros(prepared)
        prepared = prepared.replace("&&", " and ").replace("||", " or ")
        prepared = re.sub(r"(?<![=!<>])!(?!=)", " not ", prepared)
        prepared = re.sub(r"(?<=\d)[uUlL]+", "", prepared)
        try:
            return bool(eval(prepared, {"__builtins__": {}}, {}))
        except Exception:
            return False

    def _expand_expression_macros(self, expr: str) -> str:
        expanded = expr
        for _ in range(8):
            previous = expanded

            def replace_token(match: re.Match[str]) -> str:
                token = match.group(0)
                if token in {"and", "or", "not"}:
                    return token
                if token in self.macros:
                    value = self.macros[token].strip()
                    if _is_expression_like(value):
                        return f"({value})"
                    return value
                return "0"

            expanded = _IDENT_RE.sub(replace_token, expanded)
            if expanded == previous:
                break
        return expanded

    def _expand_object_macros(self, line: str) -> str:
        expanded = line
        for _ in range(8):
            previous = expanded
            for name, value in sorted(self.macros.items(), key=lambda item: len(item[0]), reverse=True):
                if not name or "(" in name:
                    continue
                expanded = re.sub(rf"\b{re.escape(name)}\b", lambda _m, repl=value: repl, expanded)
            if expanded == previous:
                break
        return expanded


def _logical_lines(text: str) -> list[str]:
    physical = text.splitlines()
    logical: list[str] = []
    current = ""
    for line in physical:
        piece = line.rstrip()
        if current:
            current += piece
        else:
            current = piece
        if current.endswith("\\"):
            current = current[:-1]
            continue
        logical.append(current)
        current = ""
    if current:
        logical.append(current)
    return logical


def _strip_c_comments(text: str) -> str:
    return _C_COMMENT_RE.sub("", text)


def _current_active(frames: list[_ConditionalFrame]) -> bool:
    return frames[-1].active if frames else True


def _is_expression_like(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_ ()<>=!&|+\-*/%.?:]+", text.strip()))


def _cleanup_generated_pack(text: str) -> str:
    lines: list[str] = []
    previous_blank = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def _split_type_declarations(pack: str) -> list[str]:
    cleaned_lines: list[str] = []
    for raw_line in pack.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("/*"):
            continue
        cleaned_lines.append(raw_line)
    text = "\n".join(cleaned_lines)
    decls: list[str] = []
    current: list[str] = []
    brace_depth = 0
    paren_depth = 0
    for ch in text:
        current.append(ch)
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == ";" and brace_depth == 0 and paren_depth == 0:
            decl = "".join(current).strip()
            current = []
            if _keep_type_declaration(decl):
                decls.append(_normalize_decl_whitespace(decl))
    tail = "".join(current).strip()
    if tail and _keep_type_declaration(tail):
        decls.append(_normalize_decl_whitespace(tail))
    return decls


def _keep_type_declaration(decl: str) -> bool:
    stripped = decl.strip()
    if not stripped:
        return False
    if stripped.startswith("extern "):
        return False
    if stripped.startswith("typedef "):
        return True
    if stripped.startswith("struct "):
        return True
    if stripped.startswith("union "):
        return True
    if stripped.startswith("enum "):
        return True
    return False


def _normalize_decl_whitespace(decl: str) -> str:
    normalized_lines = [line.rstrip() for line in decl.splitlines()]
    return "\n".join(normalized_lines).strip()
