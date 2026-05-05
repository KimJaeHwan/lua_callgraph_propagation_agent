#!/usr/bin/env python3
"""
Create a macOS Apple Silicon retrieval environment with Python 3.11 and verify MPS.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from platform_runtime import normalize_stdio_utf8


normalize_stdio_utf8()


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = PROJECT_ROOT / "requirements-macos-mps.txt"
DEFAULT_VENV = PROJECT_ROOT.parent / "lua_llm_mps"
PYTHON_CANDIDATES = [
    "/opt/homebrew/bin/python3.11",
    "/usr/local/bin/python3.11",
    "python3.11",
]


def run(cmd: list[str], desc: str, cwd: Path | None = None) -> None:
    print(f"\n[SETUP] {desc}")
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"[FAIL] {desc}")


def verify_mps_runtime(python_bin: Path) -> None:
    check = textwrap.dedent(
        """
        import platform
        import subprocess
        import torch

        print("torch", torch.__version__)
        print("platform", platform.platform())
        print("sw_vers")
        print(subprocess.check_output(["sw_vers"], text=True).strip())
        print("mps_built", torch.backends.mps.is_built())
        print("mps_available", torch.backends.mps.is_available())

        if not torch.backends.mps.is_built():
            raise SystemExit("[FAIL] PyTorch was installed without MPS support.")

        if not torch.backends.mps.is_available():
            raise SystemExit(
                "[FAIL] PyTorch MPS runtime is unavailable on this macOS/PyTorch combination."
            )

        tensor = torch.ones(1, device="mps")
        print("mps_tensor_device", tensor.device)
        """
    ).strip()
    run(
        [str(python_bin), "-c", check],
        "Verifying PyTorch MPS runtime",
    )


def resolve_python311() -> str:
    for candidate in PYTHON_CANDIDATES:
        exe = shutil.which(candidate) if "/" not in candidate else candidate
        if not exe:
            continue
        result = subprocess.run(
            [exe, "--version"],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            continue
        version_text = (result.stdout or result.stderr).strip()
        if "Python 3.11" in version_text:
            return exe
    raise SystemExit("[FAIL] Python 3.11 not found. Install python3.11 first.")


def verify_platform() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("[FAIL] This setup script is for macOS only.")
    if platform.machine() != "arm64":
        raise SystemExit("[FAIL] This setup script expects Apple Silicon (arm64).")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up macOS MPS environment for retrieval.")
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--recreate", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    verify_platform()

    python311 = resolve_python311()
    venv_dir = args.venv.resolve()
    requirements = args.requirements.resolve()

    if not requirements.exists():
        raise SystemExit(f"[FAIL] requirements file not found: {requirements}")

    print(f"[SETUP] python3.11: {python311}")
    print(f"[SETUP] venv: {venv_dir}")

    if args.recreate and venv_dir.exists():
        shutil.rmtree(venv_dir)

    if not venv_dir.exists():
        run([python311, "-m", "venv", str(venv_dir)], "Creating Python 3.11 venv")

    python_bin = venv_dir / "bin" / "python"
    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], "Upgrading pip")
    run(
        [str(python_bin), "-m", "pip", "install", "-r", str(requirements)],
        "Installing macOS MPS requirements",
        cwd=PROJECT_ROOT,
    )
    verify_mps_runtime(python_bin)

    print("\n[SETUP] Done.")
    print(f"  Venv: {venv_dir}")
    print(f"  Python: {python_bin}")


if __name__ == "__main__":
    main()
