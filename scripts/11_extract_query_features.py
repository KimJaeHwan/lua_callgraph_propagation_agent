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
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 터미널이 cp949일 때 UTF-8 출력이 깨지는 것을 방지
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


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


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def detect_java_home() -> str | None:
    """Return JAVA_HOME by OS-specific discovery, or None if not found."""
    # 1. Already set in environment
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]

    if sys.platform == "darwin":
        # macOS: /usr/libexec/java_home -v 21 (or without version)
        for args_candidate in (["/usr/libexec/java_home", "-v", "21"],
                               ["/usr/libexec/java_home"]):
            try:
                result = subprocess.run(
                    args_candidate, text=True, capture_output=True, check=True
                )
                value = result.stdout.strip()
                if value:
                    return value
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

    elif sys.platform == "win32":
        # Windows: search common JDK install roots
        search_roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(r"C:\Program Files\Eclipse Adoptium"),
            Path(r"C:\Program Files\Microsoft"),
            Path(r"C:\Program Files\Java"),
        ]
        for root in search_roots:
            if not root.exists():
                continue
            # prefer JDK 21, then any JDK
            candidates = sorted(root.glob("jdk-21*"), reverse=True) + \
                         sorted(root.glob("jdk*"), reverse=True)
            for jdk_dir in candidates:
                java_exe = jdk_dir / "bin" / "java.exe"
                if java_exe.exists():
                    return str(jdk_dir)

    else:
        # Linux: use update-alternatives or JAVA_HOME convention
        try:
            result = subprocess.run(
                ["update-java-alternatives", "-l"],
                text=True, capture_output=True, check=True,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        # fallback: resolve java from PATH
        java_bin = shutil.which("java")
        if java_bin:
            # /usr/bin/java -> /etc/alternatives/java -> /usr/lib/jvm/...
            real = Path(java_bin).resolve()
            return str(real.parents[1])

    return None


def detect_ghidra_install_dir() -> str | None:
    """Search for a Ghidra installation directory by OS convention."""
    if sys.platform == "darwin":
        # Homebrew versioned paths
        homebrew_cellar = Path("/opt/homebrew/Cellar/ghidra")
        if homebrew_cellar.exists():
            versions = sorted(homebrew_cellar.iterdir(), reverse=True)
            for v in versions:
                candidate = v / "libexec"
                if (candidate / "ghidraRun").exists():
                    return str(candidate)
        # Non-Homebrew: /Applications or arbitrary
        for candidate in [Path("/Applications/ghidra"), Path.home() / "ghidra"]:
            if candidate.exists():
                return str(candidate)

    elif sys.platform == "win32":
        search_roots = [
            Path(r"C:\ghidra"),
            Path.home() / "ghidra",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        ]
        for root in search_roots:
            if not root.exists():
                continue
            candidates = sorted(root.glob("ghidra_*"), reverse=True)
            for d in candidates:
                # Only accept installations that include the PyGhidra module
                pyghidra_jar = d / "Ghidra" / "Features" / "PyGhidra" / "lib" / "PyGhidra.jar"
                if (d / "ghidraRun.bat").exists() and pyghidra_jar.exists():
                    return str(d)

    else:
        for candidate in [Path("/opt/ghidra"), Path.home() / "ghidra"]:
            candidates = sorted(candidate.parent.glob("ghidra_*"), reverse=True) \
                if candidate == Path("/opt/ghidra") else [candidate]
            for d in candidates:
                if (d / "ghidraRun").exists():
                    return str(d)

    return None


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

    # ── Temp / home isolation (cross-platform) ──────────────────────────────
    if sys.platform == "win32":
        env["USERPROFILE"] = str(ghidra_user_home)
        env["APPDATA"] = str(ghidra_user_home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(ghidra_user_home / "AppData" / "Local")
    else:
        env["HOME"] = str(ghidra_user_home)
        env["TMPDIR"] = str(ghidra_tmp)
    env["TMP"] = str(ghidra_tmp)
    env["TEMP"] = str(ghidra_tmp)

    # ── Ghidra install dir ───────────────────────────────────────────────────
    ghidra_install_dir = (
        args.ghidra_home                        # 1. CLI arg
        or env.get("GHIDRA_INSTALL_DIR")        # 2. env var already set
        or env.get("GHIDRA_HOME")               # 3. alt env var
        or detect_ghidra_install_dir()          # 4. OS-specific search
    )
    if not ghidra_install_dir:
        raise SystemExit(
            "[ERROR] Ghidra installation not found.\n"
            "  Set GHIDRA_INSTALL_DIR environment variable or pass --ghidra-home."
        )
    print(f"[INFO] GHIDRA_INSTALL_DIR: {ghidra_install_dir}")
    env["GHIDRA_HOME"] = ghidra_install_dir
    env["GHIDRA_INSTALL_DIR"] = ghidra_install_dir

    # ── Java home ────────────────────────────────────────────────────────────
    java_home = detect_java_home()
    if java_home:
        print(f"[INFO] JAVA_HOME: {java_home}")
        env["JAVA_HOME"] = java_home
    env["JAVA_TOOL_OPTIONS"] = f"-Duser.home={ghidra_user_home}"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

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

    # stdout을 실시간 스트리밍 — tqdm 진행바가 즉시 보임
    proc = subprocess.Popen(
        cmd,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        for line in proc.stderr:
            print(line, end="", flush=True, file=sys.stderr)
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
