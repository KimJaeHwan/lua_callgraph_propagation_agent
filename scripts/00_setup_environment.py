#!/usr/bin/env python3
"""
Environment setup script for lua_callgraph_propagation_agent.

What this does:
  1. Check Python version (>= 3.11)
  2. Install Python dependencies (pip install -e .)
  3. Download the BGE embedding model to HuggingFace cache
  4. Download runtime assets (SQLite DB + retrieval index) from GitHub Release
  5. Run a dry-run pipeline to verify everything is wired up

Usage:
  python scripts/00_setup_environment.py

Options:
  --release-tag       GitHub release tag to download assets from (default: v0.1.0)
  --lua-version       Lua version for assets (default: Lua_547)
  --architecture      Architecture for retrieval index (default: x86_64)
  --skip-deps         Skip pip install
  --skip-model        Skip embedding model download
  --skip-assets       Skip runtime asset download
  --skip-verify       Skip dry-run verification
  --venv              Path to venv to install into (default: current interpreter)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from platform_runtime import normalize_stdio_utf8, resolve_venv_python

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GITHUB_REPO = "KimJaeHwan/lua_callgraph_propagation_agent"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

normalize_stdio_utf8()


def run(cmd: list[str], desc: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n[SETUP] {desc}")
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        if check:
            raise SystemExit(f"[FAIL] {desc}")
    return result


def check_python_version() -> None:
    print("\n[SETUP] Checking Python version...")
    major, minor = sys.version_info[:2]
    print(f"  Python {major}.{minor}")
    if (major, minor) < (3, 11):
        raise SystemExit(f"[FAIL] Python >= 3.11 required, got {major}.{minor}")
    print("  OK")


def install_dependencies(python: str) -> None:
    run(
        [python, "-m", "pip", "install", "--upgrade", "pip"],
        "Upgrading pip",
    )
    run(
        [python, "-m", "pip", "install", "-e", str(PROJECT_ROOT)],
        "Installing project dependencies (pip install -e .)",
    )
    # faiss is not in pyproject.toml (GPU vs CPU variant choice)
    result = subprocess.run(
        [python, "-c", "import faiss"],
        capture_output=True,
    )
    if result.returncode != 0:
        run(
            [python, "-m", "pip", "install", "faiss-cpu"],
            "Installing faiss-cpu",
        )


def download_embedding_model(python: str) -> None:
    print(f"\n[SETUP] Downloading embedding model: {EMBEDDING_MODEL}")
    script = (
        "from sentence_transformers import SentenceTransformer; "
        f"SentenceTransformer('{EMBEDDING_MODEL}'); "
        "print('  Model ready')"
    )
    run([python, "-c", script], f"Pulling {EMBEDDING_MODEL} into HuggingFace cache")


def _get_release_asset_url(tag: str, lua_version: str, architecture: str) -> str:
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    print(f"\n[SETUP] Fetching release info: {tag}")
    with urllib.request.urlopen(api_url) as resp:  # noqa: S310
        release = json.loads(resp.read())

    asset_prefix = f"lua_callgraph_runtime_assets_{lua_version}_{architecture}"
    for asset in release.get("assets", []):
        name = asset["name"]
        if name.startswith(asset_prefix) and name.endswith(".tar.gz"):
            return asset["browser_download_url"]

    names = [a["name"] for a in release.get("assets", [])]
    raise SystemExit(
        f"[FAIL] No matching asset for {asset_prefix} in release {tag}.\n"
        f"  Available: {names}"
    )


def download_runtime_assets(tag: str, lua_version: str, architecture: str) -> None:
    url = _get_release_asset_url(tag, lua_version, architecture)
    print(f"  Downloading: {url}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "assets.tar.gz"

        # stream download with progress
        with urllib.request.urlopen(url) as resp, open(archive, "wb") as f:  # noqa: S310
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 1024 * 256
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                f.write(data)
                downloaded += len(data)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r  {pct:3d}%  {downloaded // (1024*1024)}MB / {total // (1024*1024)}MB", end="", flush=True)
        print()

        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        print("  Extracting...")
        with tarfile.open(archive, "r:gz") as tf:
            members = [
                m for m in tf.getmembers()
                if not Path(m.name).name.startswith("._")
            ]
            tf.extractall(extract_dir, members=members)

        # find top-level dir inside archive
        tops = list(extract_dir.iterdir())
        asset_root = tops[0] if len(tops) == 1 and tops[0].is_dir() else extract_dir

        # copy retrieval index
        src_index = asset_root / "retrieval_indexes" / lua_version / architecture / "runtime"
        dst_index = PROJECT_ROOT / "data" / "inputs" / "retrieval_indexes" / lua_version / architecture / "runtime"
        if src_index.exists():
            dst_index.mkdir(parents=True, exist_ok=True)
            for f in src_index.iterdir():
                shutil.copy2(f, dst_index / f.name)
            print(f"  Retrieval index → {dst_index}")
        else:
            print(f"  [WARN] retrieval index not found in archive: {src_index}")

        # copy reference DB
        src_db = asset_root / "callgraphs" / lua_version / "reference_callgraph.sqlite"
        dst_db_dir = PROJECT_ROOT / "data" / "inputs" / "callgraphs" / lua_version
        if src_db.exists():
            dst_db_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_db, dst_db_dir / "reference_callgraph.sqlite")
            print(f"  Reference DB    → {dst_db_dir / 'reference_callgraph.sqlite'}")
        else:
            print(f"  [WARN] reference DB not found in archive: {src_db}")


def verify_setup(python: str) -> None:
    default_config = PROJECT_ROOT / "data" / "configs" / "runtime_recommended_preextracted.json"
    run(
        [
            python,
            str(PROJECT_ROOT / "scripts" / "10_run_name_mapping_pipeline.py"),
            "--config", str(default_config),
            "--dry-run",
        ],
        "Dry-run pipeline verification",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up lua_callgraph_propagation_agent environment.")
    parser.add_argument("--release-tag", default="v0.1.0")
    parser.add_argument("--lua-version", default="Lua_547")
    parser.add_argument("--architecture", default="x86_64")
    parser.add_argument("--skip-deps", action="store_true")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-assets", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--venv", type=Path, default=None, help="Path to venv root (e.g. /path/to/.venv)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    python = resolve_venv_python(args.venv)

    print(f"[SETUP] Python: {python}")
    print(f"[SETUP] Project root: {PROJECT_ROOT}")

    check_python_version()

    if not args.skip_deps:
        install_dependencies(python)

    if not args.skip_model:
        download_embedding_model(python)

    if not args.skip_assets:
        download_runtime_assets(args.release_tag, args.lua_version, args.architecture)

    if not args.skip_verify:
        verify_setup(python)

    print("\n[SETUP] All done. Ready to run the pipeline.")
    print(f"\n  Quick start:")
    print(f"  {python} scripts/10_run_name_mapping_pipeline.py \\")
    print(f"    --config data/configs/runtime_recommended_preextracted.json")


if __name__ == "__main__":
    main()
