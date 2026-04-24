#!/usr/bin/env python3
"""
Ghidra PyGhidra Lua Feature Extractor (single-process)
- HighFunction: struct_offsets, read/write, compare, strings
- Listing: histogram, callgraph, bb, constants

Single Ghidra project per run. pyghidra.start() is called once in main(),
and all Ghidra JVM imports are done immediately after start().
"""

import argparse
import itertools
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from tqdm import tqdm


# ====================== 경로 설정 ======================
BASE_DIR = Path.cwd().absolute()
BINARIES_DIR = BASE_DIR / "binaries"
OUTPUT_BASE = BASE_DIR / "outputs"
PROJECT_BASE = BASE_DIR / "ghidra_projects"
PROCESSED_DIR = BASE_DIR / "processed_binaries"

PROCESSED_DIR.mkdir(exist_ok=True)


# ====================== CLI ======================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract Lua binary features (single-process, single Ghidra project)."
    )
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--lua-version", default=None)
    parser.add_argument("--architecture", default=None, choices=["arm64", "aarch64", "x86_64"])
    parser.add_argument("--opt-level", default=None)
    parser.add_argument("--strip-mode", default=None, choices=["nostrip", "stripped"])
    return parser.parse_args()


# ====================== 경로 메타데이터 추출 ======================
def get_binary_info(binary_path: Path):
    if not binary_path.is_file() or binary_path.name.startswith("."):
        return None, None, None, None
    parts = binary_path.parts
    try:
        lua_version = next(p for p in parts if p.startswith("Lua_"))
        arch_dir = next(p for p in parts if p in ("arm64", "aarch64", "x86_64"))
        arch = "arm64" if arch_dir in ("arm64", "aarch64") else "x86_64"
        opt_level = next((p for p in parts if p.startswith("O")), "O0")
        strip_mode = next((p for p in parts if p in ("nostrip", "stripped")), None)
        return lua_version, arch, opt_level, strip_mode
    except Exception:
        return None, None, None, None


# ====================== 포인터 오프셋 추적 ======================
def trace_ptr(PcodeOp, varnode, depth=0, visited=None, memo=None):
    """Pcode 연산을 재귀적으로 따라가 상수 오프셋을 계산한다."""
    if varnode is None:
        return None
    if visited is None:
        visited = set()
    if memo is None:
        memo = {}

    if varnode in memo:
        return memo[varnode]
    if depth > 20 or varnode in visited:
        return None

    visited.add(varnode)

    if varnode.isConstant():
        val = varnode.getOffset()
        memo[varnode] = val
        return val

    def_op = varnode.getDef()
    if def_op is None:
        memo[varnode] = None
        return None

    opcode = def_op.getOpcode()
    result = None

    if opcode in (PcodeOp.INT_ADD, PcodeOp.PTRSUB):
        offset = 0
        for inp in def_op.getInputs():
            o = trace_ptr(PcodeOp, inp, depth + 1, visited, memo)
            if o is not None:
                offset += o
        result = offset

    elif opcode == PcodeOp.INT_MULT:
        vals = []
        for inp in def_op.getInputs():
            o = trace_ptr(PcodeOp, inp, depth + 1, visited, memo)
            if o is not None:
                vals.append(o)
        if len(vals) == 2:
            result = vals[0] * vals[1]

    else:
        for inp in def_op.getInputs():
            o = trace_ptr(PcodeOp, inp, depth + 1, visited, memo)
            if o is not None:
                result = o
                break

    memo[varnode] = result
    return result


# ====================== Feature 추출 ======================
def extract_features(
    currentProgram,
    PcodeOp,
    BasicBlockModel,
    ConsoleTaskMonitor,
    DecompInterface,
    lua_version,
    arch,
):
    results = []

    fm = currentProgram.getFunctionManager()
    listing = currentProgram.getListing()
    ref_mgr = currentProgram.getReferenceManager()
    addr_factory = currentProgram.getAddressFactory()
    monitor = ConsoleTaskMonitor()

    iface = DecompInterface()
    iface.openProgram(currentProgram)

    def get_pcode_hist(function):
        hist = Counter()
        total = 0
        for instr in listing.getInstructions(function.getBody(), True):
            for p in instr.getPcode():
                if p:
                    hist[PcodeOp.getMnemonic(p.getOpcode())] += 1
                    total += 1
        ratio = {k: round(v / total, 4) for k, v in hist.items()} if total else {}
        return dict(hist), ratio, total

    def get_callees(function):
        callees = set()
        for instr in listing.getInstructions(function.getBody(), True):
            for p in instr.getPcode():
                if p.getOpcode() == PcodeOp.CALL:
                    vn = p.getInput(0)
                    if vn.isAddress():
                        f = fm.getFunctionAt(
                            addr_factory.getDefaultAddressSpace().getAddress(vn.getOffset())
                        )
                        if f:
                            callees.add(f.getName())
        return list(callees)

    def get_callers(function):
        callers = set()
        for ref in ref_mgr.getReferencesTo(function.getEntryPoint()):
            if ref.getReferenceType().isCall():
                f = fm.getFunctionContaining(ref.getFromAddress())
                if f:
                    callers.add(f.getName())
        return list(callers)

    def get_bb(function):
        bb_model = BasicBlockModel(currentProgram)
        blocks = list(bb_model.getCodeBlocksContaining(function.getBody(), monitor))
        return len(blocks)

    all_funcs = [f for f in fm.getFunctions(True) if not f.isExternal() and not f.isThunk()]
    print(f"[INFO] extracting features from {len(all_funcs)} functions...")

    for func in tqdm(all_funcs, desc="  decompile", unit="func", ncols=72, file=sys.stdout, mininterval=2.0):
        result = iface.decompileFunction(func, 60, monitor)
        high_func = result.getHighFunction()

        offsets = []
        read_count = defaultdict(int)
        write_count = defaultdict(int)
        compare_map = defaultdict(set)
        strings = set()

        if high_func:
            for op in high_func.getPcodeOps():
                opcode = op.getOpcode()

                # LOAD / STORE → struct offset 추적
                if opcode in (PcodeOp.LOAD, PcodeOp.STORE):
                    offset = trace_ptr(PcodeOp, op.getInput(1), 0, set(), {})
                    if offset is not None:
                        offset = int(offset)
                        offsets.append(offset)
                        if opcode == PcodeOp.LOAD:
                            read_count[offset] += 1
                        else:
                            write_count[offset] += 1

                # 비교 연산 → compare_map
                if opcode in (
                    PcodeOp.INT_EQUAL,
                    PcodeOp.INT_NOTEQUAL,
                    PcodeOp.INT_LESS,
                    PcodeOp.INT_LESSEQUAL,
                ):
                    const_val = None
                    ptr_node = None
                    for inp in op.getInputs():
                        if inp.isConstant():
                            const_val = inp.getOffset()
                        else:
                            ptr_node = inp
                    if const_val is not None and ptr_node is not None:
                        offset = trace_ptr(PcodeOp, ptr_node, 0, set(), {})
                        if offset is not None:
                            compare_map[int(offset)].add(int(const_val))

                # 상수 → 문자열 참조
                for inp in op.getInputs():
                    if inp.isConstant():
                        try:
                            addr = addr_factory.getDefaultAddressSpace().getAddress(inp.getOffset())
                            data = listing.getDataAt(addr)
                            if data and data.hasStringValue():
                                s = str(data.getValue()).lower()
                                if len(s) >= 4:
                                    strings.add(s)
                        except Exception:
                            pass

        unique_offsets = list(set(offsets))
        hist, ratio, pcount = get_pcode_hist(func)

        features = {
            "function_name": func.getName(),
            "entry_point": str(func.getEntryPoint()),
            "basic_block_count": get_bb(func),
            "pcode_opcode_histogram": hist,
            "pcode_opcode_ratio": ratio,
            "pcode_instruction_count": pcount,
            "callees": get_callees(func),
            "callers": get_callers(func),
            "struct_offsets": unique_offsets,
            "read_write": {
                str(off): {"read": read_count[off], "write": write_count[off]}
                for off in unique_offsets
            },
            "compare": {str(off): list(vals) for off, vals in compare_map.items()},
            "co_occurrence": list(itertools.combinations(unique_offsets, 2)),
            "strings": list(strings),
            "lua_version": lua_version,
            "architecture": arch,
        }
        results.append(features)

    iface.dispose()
    return results


# ====================== 바이너리 이동 ======================
def _move_to_processed(binary: Path, relative: Path) -> None:
    try:
        dest = PROCESSED_DIR / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(binary), str(dest))
    except Exception as e:
        print(f"[WARN] move failed: {binary.name} — {e}")


# ====================== Main ======================
def main():
    args = parse_args()

    forced_meta = None
    if any([args.lua_version, args.architecture, args.opt_level, args.strip_mode]):
        forced_meta = {
            "lua_version": args.lua_version,
            "architecture": args.architecture,
            "opt_level": args.opt_level,
            "strip_mode": args.strip_mode,
        }

    # 처리 대상 바이너리 수집
    binaries: list[Path] = []
    for lua_dir in sorted(BINARIES_DIR.glob("Lua_*")):
        for arch_dir in sorted(lua_dir.glob("*")):
            for opt_dir in sorted(arch_dir.glob("O*")):
                for status_dir in sorted(opt_dir.glob("*")):
                    if status_dir.name not in {"nostrip", "stripped"}:
                        continue
                    for binary in sorted(status_dir.glob("*")):
                        if binary.is_file() and not binary.name.startswith("."):
                            binaries.append(binary)

    print(f"[INFO] Total binaries: {len(binaries)}")

    if args.list_only:
        for b in binaries:
            print(b)
        return

    if not binaries:
        print("[DONE] No binaries found. Exiting.")
        sys.exit(10)

    # ── Ghidra JVM 시작 (프로세스당 1회) ──────────────────────────────
    import pyghidra
    pyghidra.start()

    from ghidra.program.model.pcode import PcodeOp
    from ghidra.program.model.block import BasicBlockModel
    from ghidra.util.task import ConsoleTaskMonitor
    from ghidra.app.decompiler import DecompInterface

    print(f"[{datetime.now()}] Ghidra ready — processing {len(binaries)} binary/binaries...")

    for binary in binaries:
        lua_version, arch, opt_level, detected_strip_mode = get_binary_info(binary)
        if forced_meta:
            lua_version = forced_meta.get("lua_version") or lua_version
            forced_arch = forced_meta.get("architecture")
            if forced_arch == "aarch64":
                forced_arch = "arm64"
            arch = forced_arch or arch
            opt_level = forced_meta.get("opt_level") or opt_level
            strip_mode = forced_meta.get("strip_mode") or detected_strip_mode
        else:
            strip_mode = detected_strip_mode

        if not lua_version:
            print(f"[SKIP] cannot determine metadata: {binary.name}")
            continue

        relative = binary.relative_to(BINARIES_DIR)
        parent_dir = relative.parent
        if not strip_mode:
            strip_mode = parent_dir.name

        output_dir = OUTPUT_BASE / parent_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        # 이미 처리된 바이너리 스킵
        if list(output_dir.glob(f"{binary.stem}_*.json")):
            print(f"[SKIP] already processed: {binary.name}")
            _move_to_processed(binary, relative)
            continue

        project_loc = PROJECT_BASE / binary.stem
        project_name = f"Proj_{binary.stem}"

        # 이전 실행에서 남은 프로젝트 정리
        if project_loc.exists():
            shutil.rmtree(project_loc, ignore_errors=True)

        print(f"[INFO] opening {binary.name} in Ghidra (auto-analysis may take a few minutes)...")
        try:
            with pyghidra.open_program(
                str(binary.absolute()),
                project_location=str(project_loc),
                project_name=project_name,
                analyze=True,
            ) as flat_api:
                currentProgram = flat_api.getCurrentProgram()
                print(f"[INFO] Ghidra analysis done — starting feature extraction...")

                results = extract_features(
                    currentProgram,
                    PcodeOp,
                    BasicBlockModel,
                    ConsoleTaskMonitor,
                    DecompInterface,
                    lua_version,
                    arch,
                )

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = output_dir / f"{binary.stem}_{timestamp}.json"
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=2, ensure_ascii=False)

            print(f"[OK] {binary.name} → {len(results)} funcs → {output_path.name}")

        except Exception as e:
            print(f"[ERROR] {binary.name} — {e}")

        finally:
            if project_loc.exists():
                shutil.rmtree(project_loc, ignore_errors=True)

        _move_to_processed(binary, relative)

    print(f"\n[{datetime.now()}] All done.")


if __name__ == "__main__":
    main()
