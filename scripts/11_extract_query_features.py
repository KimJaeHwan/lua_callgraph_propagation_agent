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
import os
import shutil
import subprocess
import sys
from pathlib import Path


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
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    return parser.parse_args()


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def detect_java_home() -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/libexec/java_home"],
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


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

    # Match the successful lua_extract_feature_ghidra flow more closely:
    # keep Ghidra state inside the runtime workspace instead of letting
    # pyghidra write under ~/Library/ghidra where sandboxed execution may fail.
    ghidra_user_home = work_dir / ".ghidra_user_home"
    ghidra_user_home.mkdir(parents=True, exist_ok=True)
    ghidra_tmp = work_dir / ".ghidra_tmp"
    ghidra_tmp.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(ghidra_user_home)
    env["TMPDIR"] = str(ghidra_tmp)
    env["TMP"] = str(ghidra_tmp)
    env["TEMP"] = str(ghidra_tmp)
    ghidra_install_dir = "/opt/homebrew/Cellar/ghidra/12.0.4/libexec"
    env.setdefault("GHIDRA_HOME", ghidra_install_dir)
    env.setdefault("GHIDRA_INSTALL_DIR", ghidra_install_dir)
    env["JAVA_TOOL_OPTIONS"] = f"-Duser.home={ghidra_user_home}"
    java_home = detect_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home

    cmd = [args.python_bin, str(args.extractor_script)]
    if args.list_only:
        cmd.append("--list-only")

    completed = subprocess.run(
        cmd,
        cwd=work_dir,
        text=True,
        capture_output=True,
        env=env,
    )

    print(completed.stdout.rstrip())
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        raise SystemExit(completed.returncode)

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
