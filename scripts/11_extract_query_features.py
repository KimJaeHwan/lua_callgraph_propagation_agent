#!/usr/bin/env python3
"""
Prepare and run single-binary query feature extraction from this repository.

Why this wrapper exists:
  - We want the operational entrypoint and runtime workspace to live here.
  - The vendored PyGhidra extractor still expects a workspace with binaries/,
    outputs/, processed_binaries/, and ghidra_projects/ under the current cwd.
  - This wrapper creates that workspace under data/runtime and invokes the
    vendored extractor there so the user can stay inside one repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from platform_runtime import (
    detect_java_home,
    ensure_clean_dir,
    normalize_stdio_utf8,
    prepare_isolated_env,
)


normalize_stdio_utf8()


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "runtime" / "query_features"
DEFAULT_WORK_ROOT = PROJECT_ROOT / "data" / "runtime" / "extractor_workspace"
DEFAULT_EXTRACTOR = (
    PROJECT_ROOT
    / "src"
    / "lua_callgraph_propagation_agent"
    / "vendor"
    / "pyghidra_feature_extractor.py"
).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract query features for one target binary from this repository."
    )
    parser.add_argument("--binary", required=True, help="target .so or binary path")
    parser.add_argument("--lua-version", required=True, help="e.g. Lua_547")
    parser.add_argument("--architecture", required=True, choices=["arm64", "aarch64", "x86_64"])
    parser.add_argument("--opt-level", default="O2", help="e.g. O0/O2/O3")
    parser.add_argument("--strip-mode", default="nostrip", choices=["nostrip", "stripped"])
    parser.add_argument("--session-name", required=True, help="runtime session id")
    parser.add_argument("--extractor-script", type=Path, default=DEFAULT_EXTRACTOR)
    parser.add_argument("--python-bin", default=sys.executable, help="python used to run the vendored extractor")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument(
        "--ghidra-home",
        default=None,
        help="Ghidra install directory (overrides GHIDRA_INSTALL_DIR env var and auto-detection)",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    binary = Path(args.binary).resolve()
    if not binary.exists():
        raise SystemExit(f"Binary not found: {binary}")

    arch = "arm64" if args.architecture == "aarch64" else args.architecture
    work_dir = args.work_root.resolve() / args.session_name
    binaries_dir = work_dir / "binaries" / args.lua_version / arch / args.opt_level / args.strip_mode
    outputs_dir = work_dir / "outputs"

    ensure_clean_dir(work_dir)
    binaries_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, binaries_dir / binary.name)

    try:
        env, ghidra_user_home, _ghidra_tmp = prepare_isolated_env(
            work_dir=work_dir,
            ghidra_home_override=args.ghidra_home,
        )
    except RuntimeError as exc:
        raise SystemExit(f"[ERROR] {exc}") from exc

    ghidra_install_dir = env["GHIDRA_INSTALL_DIR"]
    print(f"[INFO] GHIDRA_INSTALL_DIR: {ghidra_install_dir}")

    java_home = detect_java_home()
    if java_home:
        print(f"[INFO] JAVA_HOME: {java_home}")

    cmd = [
        args.python_bin,
        str(args.extractor_script),
        "--lua-version",
        args.lua_version,
        "--architecture",
        arch,
        "--opt-level",
        args.opt_level,
        "--strip-mode",
        args.strip_mode,
    ]
    if args.list_only:
        cmd.append("--list-only")

    # stdout + stderr 합쳐서 실시간 스트리밍 — tqdm(stderr)도 즉시 보임
    proc = subprocess.Popen(
        cmd,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)

    if args.list_only:
        return

    json_files = sorted(outputs_dir.rglob("*.json"))
    if not json_files:
        raise SystemExit("No feature JSON produced by extractor.")

    output_root = args.output_root.resolve() / args.session_name
    ensure_clean_dir(output_root)
    copied = []
    for json_file in json_files:
        rel = json_file.relative_to(outputs_dir)
        dst = output_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(json_file, dst)
        copied.append(dst)

    manifest = {
        "session_name": args.session_name,
        "binary": str(binary),
        "lua_version": args.lua_version,
        "architecture": arch,
        "opt_level": args.opt_level,
        "strip_mode": args.strip_mode,
        "feature_files": [str(path) for path in copied],
        "workspace": str(work_dir),
    }
    manifest_path = output_root / "extract_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"[OK] feature manifest: {manifest_path}")
    print(f"[OK] copied feature files: {len(copied)}")


if __name__ == "__main__":
    main()
