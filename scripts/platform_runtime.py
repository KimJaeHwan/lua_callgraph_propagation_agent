#!/usr/bin/env python3
"""
OS-dependent runtime helpers shared by extraction/setup scripts.

Keep platform-specific logic here so the main pipeline scripts can stay focused
on Lua analysis flow rather than macOS/Windows/Linux environment differences.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path


def normalize_stdio_utf8() -> None:
    """Force UTF-8 text wrappers when the current terminal encoding is legacy."""
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def resolve_venv_python(venv_root: Path | None) -> str:
    """Return the interpreter path for a venv on the current platform."""
    if venv_root is None:
        return sys.executable
    if sys.platform == "win32":
        return str(venv_root / "Scripts" / "python.exe")
    return str(venv_root / "bin" / "python")


def safe_rmtree(path: Path) -> None:
    """Remove a directory with Windows-friendly fallbacks for locked files."""
    if not path.exists():
        return

    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
            capture_output=True,
        )
        if result.returncode == 0 and not path.exists():
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    else:
        shutil.rmtree(path)


def ensure_clean_dir(path: Path) -> None:
    safe_rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def detect_java_home() -> str | None:
    """Return JAVA_HOME by OS-specific discovery, or None if not found."""
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]

    if sys.platform == "darwin":
        for args_candidate in (["/usr/libexec/java_home", "-v", "21"], ["/usr/libexec/java_home"]):
            try:
                result = subprocess.run(
                    args_candidate,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                value = result.stdout.strip()
                if value:
                    return value
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

    elif sys.platform == "win32":
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
            candidates = sorted(root.glob("jdk-21*"), reverse=True) + sorted(root.glob("jdk*"), reverse=True)
            for jdk_dir in candidates:
                if (jdk_dir / "bin" / "java.exe").exists():
                    return str(jdk_dir)

    else:
        try:
            result = subprocess.run(
                ["update-java-alternatives", "-l"],
                text=True,
                capture_output=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        java_bin = shutil.which("java")
        if java_bin:
            real = Path(java_bin).resolve()
            return str(real.parents[1])

    return None


def detect_ghidra_install_dir() -> str | None:
    """Search for a Ghidra installation directory by OS convention."""
    if sys.platform == "darwin":
        homebrew_cellar = Path("/opt/homebrew/Cellar/ghidra")
        if homebrew_cellar.exists():
            for version_dir in sorted(homebrew_cellar.iterdir(), reverse=True):
                candidate = version_dir / "libexec"
                if (candidate / "ghidraRun").exists():
                    return str(candidate)
        for candidate in (Path("/Applications/ghidra"), Path.home() / "ghidra"):
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
            for ghidra_dir in sorted(root.glob("ghidra_*"), reverse=True):
                pyghidra_jar = ghidra_dir / "Ghidra" / "Features" / "PyGhidra" / "lib" / "PyGhidra.jar"
                if (ghidra_dir / "ghidraRun.bat").exists() and pyghidra_jar.exists():
                    return str(ghidra_dir)

    else:
        for candidate in (Path("/opt/ghidra"), Path.home() / "ghidra"):
            candidates = (
                sorted(candidate.parent.glob("ghidra_*"), reverse=True)
                if candidate == Path("/opt/ghidra")
                else [candidate]
            )
            for ghidra_dir in candidates:
                if (ghidra_dir / "ghidraRun").exists():
                    return str(ghidra_dir)

    return None


def prepare_isolated_env(
    *,
    work_dir: Path,
    ghidra_home_override: str | None = None,
) -> tuple[dict[str, str], Path, Path]:
    """Prepare isolated HOME/TMP + Java/Ghidra env for extraction subprocesses."""
    ghidra_user_home = work_dir / ".ghidra_user_home"
    ghidra_user_home.mkdir(parents=True, exist_ok=True)
    ghidra_tmp = work_dir / ".ghidra_tmp"
    ghidra_tmp.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()

    if sys.platform == "win32":
        env["USERPROFILE"] = str(ghidra_user_home)
        env["APPDATA"] = str(ghidra_user_home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(ghidra_user_home / "AppData" / "Local")
    else:
        env["HOME"] = str(ghidra_user_home)
        env["TMPDIR"] = str(ghidra_tmp)
    env["TMP"] = str(ghidra_tmp)
    env["TEMP"] = str(ghidra_tmp)

    ghidra_install_dir = (
        ghidra_home_override
        or env.get("GHIDRA_INSTALL_DIR")
        or env.get("GHIDRA_HOME")
        or detect_ghidra_install_dir()
    )
    if not ghidra_install_dir:
        raise RuntimeError(
            "Ghidra installation not found. Set GHIDRA_INSTALL_DIR or pass --ghidra-home."
        )
    env["GHIDRA_HOME"] = ghidra_install_dir
    env["GHIDRA_INSTALL_DIR"] = ghidra_install_dir

    java_home = detect_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home

    env["JAVA_TOOL_OPTIONS"] = f"-Duser.home={ghidra_user_home}"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env, ghidra_user_home, ghidra_tmp
