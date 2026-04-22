#!/usr/bin/env python3
"""
Ghidra PyGhidra Lua Feature Extractor - FINAL (HighFunction + Listing Hybrid)
- HighFunction: struct_offsets, read/write, compare, strings
- Listing: histogram, callgraph, bb, constants
"""

#!/usr/bin/env python3

import os
import sys
import json
import shutil
import time
from pathlib import Path
from datetime import datetime
from multiprocessing import Pool, cpu_count
from collections import Counter, defaultdict
import itertools

# ====================== 설정 ======================
BASE_DIR = Path.cwd().absolute()
BINARIES_DIR = BASE_DIR / "binaries"
OUTPUT_BASE = BASE_DIR / "outputs"
PROJECT_BASE = BASE_DIR / "extractor" / "ghidra_projects"
PROCESSED_DIR = BASE_DIR / "processed_binaries"

PROCESSED_DIR.mkdir(exist_ok=True)

WORKERS = 8



# BATCH_SIZE = 1800

# ====================== Helper ======================
def get_binary_info(binary_path: Path):
    if not binary_path.is_file() or binary_path.name.startswith('.'):
        return None, None, None
    parts = binary_path.parts
    try:
        lua_version = next(p for p in parts if p.startswith("Lua_"))
        arch_dir = next(p for p in parts if p in ("arm64", "aarch64", "x86_64"))
        arch = "arm64" if arch_dir in ("arm64", "aarch64") else "x86_64"
        opt_level = next((p for p in parts if p.startswith("O")), "O0")
        return lua_version, arch, opt_level
    except:
        return None, None, None

# ====================== Pointer Trace ======================
def trace_ptr(varnode, depth=0, visited=None, memo=None):
    from ghidra.program.model.pcode import PcodeOp
    if varnode is None:
        return None

    if visited is None:
        visited = set()

    if memo is None:
        memo = {}

    # 🔥 이미 계산된 경우 (성능 핵심)
    if varnode in memo:
        return memo[varnode]

    # 🔥 depth 제한
    if depth > 20:
        return None

    # 🔥 cycle 방지
    if varnode in visited:
        return None

    visited.add(varnode)

    # constant
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

    # ADD / PTRSUB
    if opcode in (PcodeOp.INT_ADD, PcodeOp.PTRSUB):
        offset = 0
        for inp in def_op.getInputs():
            o = trace_ptr(inp, depth + 1, visited, memo)
            if o is not None:
                offset += o
        result = offset

    # MULT
    elif opcode == PcodeOp.INT_MULT:
        vals = []
        for inp in def_op.getInputs():
            o = trace_ptr(inp, depth + 1, visited, memo)
            if o is not None:
                vals.append(o)
        if len(vals) == 2:
            result = vals[0] * vals[1]

    else:
        # fallback
        for inp in def_op.getInputs():
            o = trace_ptr(inp, depth + 1, visited, memo)
            if o is not None:
                result = o
                break

    memo[varnode] = result
    return result
    
# 문자열 추출
# -----------------------------
def extract_strings(program, high_func):
    listing = program.getListing()
    addr_factory = program.getAddressFactory()
    strings = set()

    for op in high_func.getPcodeOps():
        for inp in op.getInputs():
            if inp.isConstant():
                try:
                    addr = addr_factory.getDefaultAddressSpace().getAddress(inp.getOffset())
                    data = listing.getDataAt(addr)

                    if data and data.hasStringValue():
                        s = str(data.getValue()).lower()

                        # 필터링
                        if len(s) >= 4 and not s.isdigit():
                            strings.add(s)

                except:
                    pass

    return list(strings)

# ====================== Feature Extraction ======================
def extract_features_inside_program(currentProgram, lua_version, arch):
    from ghidra.program.model.pcode import PcodeOp
    from ghidra.program.model.block import BasicBlockModel
    from ghidra.util.task import ConsoleTaskMonitor
    from ghidra.app.decompiler import DecompInterface
    
    results = []

    fm = currentProgram.getFunctionManager()
    listing = currentProgram.getListing()
    ref_mgr = currentProgram.getReferenceManager()

    # 🔥 Decompiler
    iface = DecompInterface()
    iface.openProgram(currentProgram)
    monitor = ConsoleTaskMonitor()

    def get_pcode_hist(function):
        hist = Counter()
        total = 0
        for instr in listing.getInstructions(function.getBody(), True):
            for p in instr.getPcode():
                if p:
                    op = PcodeOp.getMnemonic(p.getOpcode())
                    hist[op] += 1
                    total += 1
        ratio = {k: round(v/total, 4) for k,v in hist.items()} if total else {}
        return dict(hist), ratio, total

    def get_callees(function):
        callees = set()
        for instr in listing.getInstructions(function.getBody(), True):
            for p in instr.getPcode():
                if p.getOpcode() == PcodeOp.CALL:
                    vn = p.getInput(0)
                    if vn.isAddress():
                        addr = vn.getOffset()
                        f = fm.getFunctionAt(currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(addr))
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

    for func in fm.getFunctions(True):
        if func.isExternal() or func.isThunk():
            continue

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

                # LOAD / STORE
                if opcode in (PcodeOp.LOAD, PcodeOp.STORE):
                    ptr = op.getInput(1)
                    offset = trace_ptr(ptr, 0, set(), {})

                    if offset is not None:
                        offset = int(offset)
                        offsets.append(offset)

                        if opcode == PcodeOp.LOAD:
                            read_count[offset] += 1
                        else:
                            write_count[offset] += 1

                # compare
                if opcode in (
                    PcodeOp.INT_EQUAL,
                    PcodeOp.INT_NOTEQUAL,
                    PcodeOp.INT_LESS,
                    PcodeOp.INT_LESSEQUAL
                ):
                    const_val = None
                    ptr_node = None

                    for inp in op.getInputs():
                        if inp.isConstant():
                            const_val = inp.getOffset()
                        else:
                            ptr_node = inp

                    if const_val is not None and ptr_node is not None:
                        offset = trace_ptr(ptr_node, 0, set(), {})
                        if offset is not None:
                            compare_map[int(offset)].add(int(const_val))

                # strings
                for inp in op.getInputs():
                    if inp.isConstant():
                        try:
                            addr = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(inp.getOffset())
                            data = listing.getDataAt(addr)
                            if data and data.hasStringValue():
                                s = str(data.getValue()).lower()
                                if len(s) >= 4:
                                    strings.add(s)
                        except:
                            pass

        unique_offsets = list(set(offsets))

        hist, ratio, pcount = get_pcode_hist(func)

        features = {
            "function_name": func.getName(),
            "entry_point": str(func.getEntryPoint()),
            "basic_block_count": get_bb(func),

            # 기존
            "pcode_opcode_histogram": hist,
            "pcode_opcode_ratio": ratio,
            "pcode_instruction_count": pcount,
            "callees": get_callees(func),
            "callers": get_callers(func),

            # 🔥 구조
            "struct_offsets": unique_offsets,
            "read_write": {
                str(off): {
                    "read": read_count[off],
                    "write": write_count[off]
                } for off in unique_offsets
            },
            "compare": {
                str(off): list(vals)
                for off, vals in compare_map.items()
            },
            "co_occurrence": list(itertools.combinations(unique_offsets, 2)),

            # 🔥 의미
            "strings": list(strings),

            "lua_version": lua_version,
            "architecture": arch
        }

        results.append(features)
    iface.dispose()

    return results



# ====================== Worker ======================
def process_binary(binary_path_str):
    try:
        import pyghidra
        pyghidra.start()

        from ghidra.program.model.pcode import PcodeOp
        from ghidra.program.model.block import BasicBlockModel
        from ghidra.util.task import ConsoleTaskMonitor
        from ghidra.app.decompiler import DecompInterface

        binary = Path(binary_path_str)

        lua_version, arch, opt_level = get_binary_info(binary)
        if not lua_version:
            return f"[SKIP] invalid path: {binary.name}"

        relative = binary.relative_to(BINARIES_DIR)
        parent_dir = relative.parent  # Lua_547/x86_64/O2/nostrip

        output_dir = OUTPUT_BASE / parent_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        output_pattern = f"{arch}_{opt_level}_nostrip_{binary.stem}_*.json"

        # ✅ 이미 처리됨
        if list(output_dir.glob(output_pattern)):
            try:
                relative = binary.relative_to(BINARIES_DIR)
                dest = PROCESSED_DIR / relative

                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(binary), str(dest))
            except:
                pass
            return f"[SKIP] {binary.name}"

        project_loc = PROJECT_BASE / f"{binary.stem}_{os.getpid()}"
        project_name = f"Proj_{binary.stem}"

        # 기존 프로젝트 제거
        if project_loc.exists():
            shutil.rmtree(project_loc, ignore_errors=True)

        with pyghidra.open_program(
            str(binary.absolute()),
            project_location=str(project_loc),
            project_name=project_name,
            analyze=True
        ) as flat_api:

            currentProgram = flat_api.getCurrentProgram()

            time.sleep(3)

            results = extract_features_inside_program(currentProgram, lua_version, arch)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = output_dir / f"{arch}_{opt_level}_nostrip_{binary.stem}_{timestamp}.json"

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

        # 프로젝트 삭제
        if project_loc.exists():
            shutil.rmtree(project_loc, ignore_errors=True)

        # binary 이동
        try:
            relative = binary.relative_to(BINARIES_DIR)
            dest = PROCESSED_DIR / relative

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(binary), str(dest))
        except Exception as e:
            return f"[WARN] move failed: {e}"

        return f"[OK] {binary.name} → {len(results)} funcs"

    except Exception as e:
        return f"[ERROR] {binary_path_str} - {e}"

# ====================== Main ======================
def main():
    print(f"[{datetime.now()}] Multiprocessing start (workers={WORKERS})")

    binaries = []

    for lua_dir in sorted(BINARIES_DIR.glob("Lua_*")):
        for arch_dir in sorted(lua_dir.glob("*")):
            for opt_dir in sorted(arch_dir.glob("O*")):
                for status_dir in sorted(opt_dir.glob("*")):
                    if status_dir.name != "nostrip":
                        continue
                    for binary in sorted(status_dir.glob("*")):
                        if binary.is_file() and not binary.name.startswith('.'):
                            binaries.append(str(binary))

    print(f"Total binaries: {len(binaries)}")

    if len(binaries) == 0:
        print("[DONE] No binaries left. Exiting.")
        sys.exit(10)  # 🔥 특별한 종료 코드

    with Pool(WORKERS) as pool:
        for result in pool.imap_unordered(process_binary, binaries):
            print(result)

    print(f"\n[{datetime.now()}] All done.")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.set_start_method("spawn", force=True)
    main()
