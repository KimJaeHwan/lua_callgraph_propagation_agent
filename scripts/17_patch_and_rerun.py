#!/usr/bin/env python3
"""
17_patch_and_rerun.py

Three-step preparation before re-running propagation:

1. PATCH FEATURES  — replace confirmed FUN_xxx names in callee/caller lists
   with real Lua names so hybrid retrieval gets callgraph bonus matches.

2. PATCH ANCHORS   — inject confirmed functions as force-anchors (confidence=1.0)
   into seed_anchors.json.

3. DEDUP RETRIEVAL — for each reference name that appears as top-1 for multiple
   query functions, keep only the highest-scoring one; demote the rest so
   propagation must resolve them via callgraph evidence instead of a weak
   retrieval guess.

Usage:
  python scripts/17_patch_and_rerun.py \\
      --result-dir  data/runtime/results/artale_libengine_lua536 \\
      --query-json  data/runtime/query_features/artale_libengine_lua536/Lua_536/x86_64/O2/stripped/libengine_20260427_102236.json \\
      --confirmed   scripts/confirmed_mappings.json

confirmed_mappings.json format:
  { "0x4a7141": "luaopen_base", "0x48dc9e": "lua_setfield", ... }
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── hard-coded confirmed mappings (entry_point hex -> real name) ──────────────
CONFIRMED: dict[str, str] = {
    # Round 1 — verified via IDA MCP (20 core Lua VM functions)
    "4a7141":  "luaopen_base",
    "48dc9e":  "lua_setfield",
    "48d2d7":  "lua_pushlstring",
    "48ce08":  "lua_pushvalue",
    "48d52c":  "lua_pushcclosure",
    "4a6dc4":  "luaL_setfuncs",
    "48d91e":  "lua_rawgeti",
    "4a7206":  "luaB_type",
    "48ca03":  "lua_index2value",
    "49c23e":  "luaS_newlstr",
    "4a1604":  "luaV_settable",
    "4a5a32":  "luaL_checkany",
    "49fb92":  "luaH_get",
    "49c3cb":  "createstrobj",
    "4a008c":  "luaH_getint",
    "48cbe8":  "lua_settop",
    "49556b":  "luaM_realloc_",
    "4a0049":  "luaH_getstr",
    "4a52bb":  "luaL_argerror",
    "492b27":  "luaC_step",
    # Round 2 — luaopen_* standard library entry points
    "4a4430":  "luaopen_utf8",
    "4a01fe":  "luaopen_table",
    "492d7a":  "luaopen_io",
    "4942f9":  "luaopen_math",
    "4955f0":  "luaopen_package",
    "4aa689":  "luaopen_debug",
    "4aa006":  "luaopen_coroutine",
    "49c472":  "luaopen_string",
    "4972fb":  "luaopen_os",
    "4a8529":  "luaopen_bit32",
    # Round 3 — lua_execute call chain (lua_pcall → luaD_call → ccall → luaV_execute)
    "4a1fae":  "luaV_execute",
    "490306":  "luaD_call",
    "490387":  "lua_resume",
    "490497":  "luaD_poscall",
    "48e192":  "lua_pcallk",
    "490725":  "luaD_pcall",
    "48fa23":  "luaD_rawrunprotected",
    "4a789b":  "luaB_pcall",
    "48fe6a":  "ccall",
    # Round 4 — from trusted_mappings batch (157 newly renamed in IDA)
    # lua_xxx public API
    "48d352":  "lua_pushstring",
    "48d097":  "lua_tolstring",
    "48cb5a":  "lua_xmove",
    "48e73f":  "lua_concat",
    "48cc66":  "lua_rotate",
    "48d3d7":  "lua_pushvfstring",
    "5440d0":  "lua_seti",
    "4cc0b0":  "lua_setmetatable",
    "4ed9f0":  "lua_getmetatable",
    "5eaef0":  "lua_setupvalue",
    "4efd00":  "lua_isstring",
    "5fb030":  "lua_copy",
    "592240":  "lua_pushnil",
    "5fadc0":  "lua_upvalueid",
    "578d00":  "lua_gettop",
    "de094":   "lua_type",
    "49baca":  "lua_newstate",
    "49b8b4":  "lua_newthread",
    # luaL_xxx
    "4a5901":  "luaL_checklstring",
    "4a5442":  "luaL_where",
    "4a5862":  "luaL_checkoption",
    "4a5cf9":  "luaL_addvalue",
    "4a6a04":  "luaL_len",
    "3333e4":  "luaL_pushresult",
    "4a5932":  "luaL_optlstring",
    "4a6ff4":  "luaL_gsub",
    "475064":  "luaL_loadfilex",
    "469baa":  "luaL_getmetafield",
    "602750":  "luaL_execresult",
    # luaV_xxx (VM operations)
    "4a1efe":  "luaV_div",
    "4a1422":  "luaV_tointeger",
    "481d8c":  "luaV_finishOp",
    "481fe8":  "luaV_shiftl",
    # luaT_xxx (tag methods)
    "4a12b5":  "luaT_trybinTM",
    "4c5500":  "luaT_callbinTM",
    # luaG_xxx (runtime errors)
    "48f883":  "luaG_tointerror",
    "48f671":  "luaG_runerror",
    "48f824":  "luaG_concaterror",
    "48f7c4":  "luaG_errormsg",
    # luaK_xxx (code generation)
    "4a8f20":  "luaK_patchclose",
    "4a914f":  "luaK_setreturns",
    "4aba26":  "luaK_patchtohere",
    "4ca010":  "luaK_getlabel",
    "610cb0":  "luaK_codeABC",
    # luaH_xxx (hash table)
    "49fb54":  "luaH_set",
    "49faf9":  "luaH_setint",
    "1b34fc":  "luaH_newkey",
    # luaD_xxx (execution / stack)
    "48fbbf":  "luaD_growstack",
    "58fdc0":  "luaD_reallocstack",
    # luaC_xxx (GC)
    "491b6a":  "luaC_runtilstate",
    "52afa0":  "luaC_barrier_",
    # luaM_xxx (memory)
    "4955e1":  "luaM_toobig",
    # luaO_xxx (object utilities)
    "4a8db9":  "luaO_int2fb",
    "48ae23":  "luaO_fb2int",
    "4ed410":  "luaO_pushvfstring",
    "1ab3f0":  "luaO_utf8esc",
    # luaF_xxx (function objects / closures)
    "620860":  "luaF_freeproto",
    "4d8800":  "luaF_close",
    # luaX_xxx (lexer)
    "48dda0":  "luaX_newstring",
    # luaB_xxx (base library)
    "4ab7f3":  "luaB_coresume",
    "e6265":   "luaB_cocreate",
    # String library internals
    "49d4f2":  "str_rep",
    "49c6ad":  "str_char",
    "49d714":  "str_sub",
    "49dd78":  "str_packsize",
    "494e17":  "str_pack",
    # IO library
    "493163":  "f_setvbuf",
    "493d1b":  "io_read",
    "493f86":  "io_write",
    "49402f":  "io_tmpfile",
    "46cf6b":  "io_readline",
    "494264":  "opencheck",
    "1fe9a2":  "aux_close",
    "1797a3":  "g_write",
    # OS library
    "497b47":  "os_rename",
    "497db0":  "os_tmpname",
    # Math library
    "494f8e":  "math_randomseed",
    # Package library
    "495fb7":  "searcher_preload",
    "496016":  "ll_searchpath",
    "4789e9":  "searcher_Croot",
    "1b13ec":  "searchpath",
    "5da680":  "ll_require",
    "4961b0":  "findfile.constprop.0",
    # UTF-8 library
    "4a4cda":  "pushutfchar",
    "4a4488":  "byteoffset",
    "4a464b":  "codepoint",
    # Bit32 library
    "4a8896":  "b_lshift",
    "46dedd":  "b_test",
    "1ee539":  "b_rot",
    # Table library
    "4a0259":  "tconcat",
    "539720":  "iter_aux",
    "4a04c1":  "unpack",
    # Compiler / parser internals
    "499888":  "new_localvar",
    "4a8aba":  "fieldargs",
    "49977d":  "cond",
    "4983a6":  "newlabelentry",
    "4ac72b":  "inclinenumber",
    "4acc54":  "check_next2",
    "49b752":  "singlevaraux",
    "55ae90":  "block_follow",
    "5634d0":  "explist",
    "4acd8d":  "checkliteral",
    "12a9d1":  "subexpr",
    "5a25a0":  "open_func",
    "5b3610":  "freeexps",
    "4b7f5d":  "test_then_block",
    "4c0eab":  "ipairsaux",
    "5633d0":  "getthread",
    # GC internals
    "4929d2":  "traverseephemeron",
    "491a0b":  "GCTM",
    "3355ca":  "convergeephemerons",
    "4537e0":  "atomic",
    "61e1f0":  "restartcollection",
    "5da2e0":  "gctm",
    "1b9aa6":  "traverseproto",
    # Loader / lexer / parser (GCC-optimised variants)
    "49b64a":  "LoadUpvalues",
    "5c9840":  "f_parser",
    "611940":  "f_luaopen",
    "559a60":  "getF",
    "601690":  "read_number",
    "55f540":  "read_long_string",
    "6036c0":  "l_checkmode",
    "48f22e":  "getobjname.part.0",
    "5a84c0":  "LoadBlock.constprop.1",
    "4b15b5":  "freestack.part.0",
    "544270":  "findlocal.constprop.0",
    # Debug library
    "49e227":  "getdetails",
    "4a6acc":  "db_getinfo",
    "575470":  "findpcall",
    "640a70":  "pushglobalfuncname",
    # State / error handling
    "48f9c2":  "seterrorobj",
    "49b116":  "recover",
    "4a7108":  "panic",
    "49b9de":  "stack_init",
    "49e2ee":  "unpackint",
    "49960f":  "errorlimit",
    "49f734":  "mainposition",
    "5e6c20":  "l_alloc",
    "49f083":  "start_capture",
    "49f0ce":  "classend",
    "4ab936":  "checkupval",
    "4ab990":  "treatstackoption",
    "58c190":  "l_message",
    "478224":  "msghandler",
    "159566":  "funcinfo",
    "4a1885":  "l_strcmp",
    "490fd1":  "checkmode",
    "49e655":  "getnumlimit",
    # Round 5 — new 1:1 high-confidence from r5 trusted_mappings
    "48d432":  "lua_pushfstring",
    "49550c":  "luaM_growaux_",
    "4a7666":  "luaB_load",
    "4a8987":  "b_rrot",
    "4d8580":  "l_str2dloc",
    "5daa00":  "read_numeral",
    "5103a0":  "swapextra",
    "4aa158":  "luaB_costatus",
    "566130":  "str_find_aux",
    "4cf450":  "l_str2d",
    "4d84b0":  "reallymarkobject",
    "523d90":  "luaY_parser",
}


def load_json(p: Path) -> dict | list:
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [saved] {p.name}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — patch query feature JSON
# ─────────────────────────────────────────────────────────────────────────────

def patch_features(query_json_path: Path, confirmed: dict[str, str]) -> Path:
    """
    Replace FUN_xxx names in callees/callers with confirmed real names.
    Saves a new file with suffix _patched.json and returns the path.
    """
    # Build: ghidra_func_name -> real_name  (from entry_point -> real_name)
    funcs = load_json(query_json_path)
    if not isinstance(funcs, list):
        funcs = funcs.get("functions", [])

    # entry_point (hex str) -> function_name (ghidra)
    ep_to_ghidra: dict[str, str] = {f["entry_point"].lower(): f["function_name"]
                                     for f in funcs}
    # ghidra_name -> real_name
    ghidra_to_real: dict[str, str] = {}
    for ep, real in confirmed.items():
        ep_norm = ep.lower().lstrip("0") or "0"
        # match with possible leading zeros
        for k, v in ep_to_ghidra.items():
            k_norm = k.lower().lstrip("0") or "0"
            if k_norm == ep_norm:
                ghidra_to_real[v] = real
                break

    print(f"\n[STEP 1] Patching features — {len(ghidra_to_real)} names resolved")
    for g, r in ghidra_to_real.items():
        print(f"  {g} -> {r}")

    patched = []
    replaced_total = 0
    for f in funcs:
        fc = copy.deepcopy(f)
        # patch callee list
        new_callees = []
        for c in fc.get("callees", []):
            if c in ghidra_to_real:
                new_callees.append(ghidra_to_real[c])
                replaced_total += 1
            else:
                new_callees.append(c)
        fc["callees"] = new_callees
        # patch caller list
        new_callers = []
        for c in fc.get("callers", []):
            if c in ghidra_to_real:
                new_callers.append(ghidra_to_real[c])
                replaced_total += 1
            else:
                new_callers.append(c)
        fc["callers"] = new_callers
        patched.append(fc)

    print(f"  total callee/caller references replaced: {replaced_total}")

    out_path = query_json_path.parent / (query_json_path.stem + "_patched.json")
    save_json(out_path, patched)
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — inject force anchors into seed_anchors.json
# ─────────────────────────────────────────────────────────────────────────────

def patch_anchors(seed_anchors_path: Path, query_json_path: Path,
                  confirmed: dict[str, str]) -> None:
    anchors = load_json(seed_anchors_path)

    # ── Purge noisy anchors written by previous propagation runs ────────────
    # Only keep manually confirmed force anchors; strip retrieval-auto entries
    # whose reference name is in NOISE_REFERENCE_NAMES.
    before = len(anchors.get("mappings", []))
    anchors["mappings"] = [
        m for m in anchors.get("mappings", [])
        if m.get("source") == "force_anchor_manual"
        or m.get("reference_function_name") not in NOISE_REFERENCE_NAMES
    ]
    purged = before - len(anchors["mappings"])
    if purged:
        print(f"  [purge] removed {purged} noisy anchors from seed_anchors.json")

    # build entry_point -> ghidra name from (potentially patched) feature file
    funcs = load_json(query_json_path)
    if not isinstance(funcs, list):
        funcs = funcs.get("functions", [])
    ep_to_ghidra = {f["entry_point"].lower(): f["function_name"] for f in funcs}

    existing_queries = {m["query_function_name"] for m in anchors.get("mappings", [])}

    added = 0
    for ep, real_name in confirmed.items():
        ep_norm = ep.lower().lstrip("0") or "0"
        ghidra_name = None
        for k, v in ep_to_ghidra.items():
            if k.lower().lstrip("0") or "0" == ep_norm:
                ghidra_name = v
                break
            # fallback: loose match
            if k.lower().rstrip("0").rstrip("0") == ep.lower().rstrip("0"):
                ghidra_name = v
                break

        # better match: compare stripped zeros
        for k, v in ep_to_ghidra.items():
            if int(k, 16) == int(ep, 16):
                ghidra_name = v
                break

        if not ghidra_name:
            print(f"  [WARN] cannot find ghidra name for ep={ep}")
            continue

        if ghidra_name in existing_queries:
            # update existing entry
            for m in anchors["mappings"]:
                if m["query_function_name"] == ghidra_name:
                    m["reference_function_name"] = real_name
                    m["confidence"] = 1.0
                    m["source"] = "force_anchor_manual"
                    m["status"] = "accepted"
                    m["evidence"] = [f"manually_verified_ida_mcp: {real_name}"]
            continue

        anchors["mappings"].append({
            "query_function_name": ghidra_name,
            "reference_function_name": real_name,
            "confidence": 1.0,
            "source": "force_anchor_manual",
            "status": "accepted",
            "evidence": [f"manually_verified_ida_mcp: {real_name}"],
        })
        added += 1

    print(f"\n[STEP 2] Force anchors — added {added} new, updated existing")
    save_json(seed_anchors_path, anchors)
    print(f"  total anchors now: {len(anchors['mappings'])}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — deduplicate + de-noise retrieval results
# ─────────────────────────────────────────────────────────────────────────────

# Reference names that are known noise — too generic / game-specific / ambiguous.
# Any retrieval top-1 hit with one of these names is demoted so propagation
# cannot spread false positives further.
NOISE_REFERENCE_NAMES: set[str] = {
    # ── original noise (generic / game-specific) ─────────────────────────────
    "custom_decrypt_block",  # game-specific, 907 false matches
    "lua_version",           # tiny getter, 577 false matches
    "DumpString",            # weak signal, 211 false matches
    "luaD_throw",            # too generic, 91 false matches
    "luaZ_fill",             # 80 false matches
    "getS",                  # very short, highly ambiguous
    "LoadFunction",          # 75 false matches
    "_start",                # binary entry point, not a Lua function
    "close_state",           # 30 false matches
    "boxgc",                 # 30 false matches
    "luaZ_read",             # 28 false matches
    "lua_isyieldable",       # 28 false matches
    "constructor",           # too generic (Lua parser artifact)
    "assignment",            # too generic (Lua parser artifact)
    "luaE_extendCI",         # 19 false matches
    "luaC_upvalbarrier_",    # 19 false matches
    "luaU_dump",             # 18 — weak signal
    # ── Round 4 newly discovered noise (dist analysis ≥5 false matches) ──────
    "match",                 # regex match — 74 false matches (too generic)
    "luaD_rawrunprotected",  # 17 false matches (real one is force anchor 48fa23)
    "luaD_poscall",          # 26 false matches (real one is force anchor 490497)
    "str_gsub",              # 40 false matches
    "resume",                # 16 false matches
    "gmatch",                # 15 false matches
    "lua_toboolean",         # 15 false matches (tiny function, too ambiguous)
    "statement",             # 14 false matches (parser internal, too generic)
    "luaL_newstate",         # 14 false matches
    "LoadString",            # 13 false matches
    "lua_getinfo",           # 13 false matches
    "g_read",                # 13 false matches
    "math_abs",              # 13 false matches
    "io_pclose",             # 13 false matches
    "llex",                  # 12 false matches
    "body",                  # 11 false matches (parser internal)
    "os_exit",               # 11 false matches
    "separatetobefnz",       # 11 false matches
    "lua_topointer",         # 10 false matches
    "luaS_hashlongstr",      # 10 false matches
    # ── Targeted retrieval noise (structural false positives) ─────────────────
    "__stack_chk_fail",      # GCC stack canary, not a Lua function — multi-hit
}


def inject_targeted_anchors(
    seed_anchors_path: Path,
    targeted_json_path: Path,
    min_vote_score: float = 0.75,
    min_margin: float = 0.15,
) -> int:
    """
    Read targeted_retrieval.json (from 12c) and inject high-confidence cases
    into seed_anchors.json as 'targeted_retrieval' source entries.

    Filters applied:
      • vote_score >= min_vote_score  (structural confidence)
      • margin     >= min_margin      (top-1 vs top-2 gap)
      • ref_name not in NOISE_REFERENCE_NAMES
      • dedup: at most one query func per reference name
      • skip if query func already present in seed_anchors
    """
    if not targeted_json_path.exists():
        print(f"  [WARN] targeted_retrieval.json not found: {targeted_json_path}")
        return 0

    targeted = load_json(targeted_json_path)
    anchors  = load_json(seed_anchors_path)

    existing_queries: set[str]       = {m["query_function_name"] for m in anchors.get("mappings", [])}
    existing_refs:    dict[str, int] = defaultdict(int)
    for m in anchors.get("mappings", []):
        existing_refs[m["reference_function_name"]] += 1

    added = 0
    for case in targeted.get("cases", []):
        preview = case.get("unique_topk_preview", [])
        if not preview:
            continue

        top1   = preview[0]
        score1 = float(top1.get("score_total", 0.0))
        score2 = float(preview[1].get("score_total", 0.0)) if len(preview) > 1 else 0.0
        margin = score1 - score2

        qfunc    = case.get("query_func", "")
        ref_name = top1.get("function_name", "")

        if not qfunc or not ref_name:
            continue
        if score1 < min_vote_score or margin < min_margin:
            continue
        if qfunc in existing_queries:
            continue
        if ref_name in NOISE_REFERENCE_NAMES:
            continue
        if existing_refs[ref_name] >= 1:
            continue  # dedup: keep only one query per reference name

        anchors["mappings"].append({
            "query_function_name":     qfunc,
            "reference_function_name": ref_name,
            "confidence":              round(score1, 4),
            "source":                  "targeted_retrieval",
            "status":                  "accepted",
            "evidence": [
                f"vote_score={score1:.4f}",
                f"margin={margin:.4f}",
                f"voter_count={case.get('voter_count', 0)}",
            ],
        })
        existing_queries.add(qfunc)
        existing_refs[ref_name] += 1
        added += 1

    print(f"\n[STEP 2b] Targeted anchors — injected {added} new entries")
    if added:
        save_json(seed_anchors_path, anchors)
    return added


def dedup_retrieval(retrieval_path: Path) -> None:
    """
    Two passes:
    1. NOISE REMOVAL — clear top-1 for any known-noisy reference name.
    2. DEDUP         — for each reference name that appears as top-1 for
                       multiple query functions, keep only the highest-scoring
                       one; demote the rest.
    """
    data = load_json(retrieval_path)
    cases = data.get("cases", [])

    # Pass 1 — noise removal
    noise_cleared = 0
    for c in cases:
        top1 = c.get("unique_top1_function")
        if top1 and top1 in NOISE_REFERENCE_NAMES:
            c["unique_top1_function"] = None
            c["unique_top1_hit"] = None
            noise_cleared += 1

    print(f"\n[STEP 3a] Noise removal — cleared {noise_cleared} noisy top-1 entries")

    # Pass 2 — dedup
    name_to_cases: dict[str, list] = defaultdict(list)
    for c in cases:
        top1 = c.get("unique_top1_function")
        if top1 and not top1.startswith("FUN_") and not top1.startswith("sub_"):
            score = 0.0
            for cand in c.get("unique_topk_preview", []):
                if cand.get("function_name") == top1:
                    score = cand.get("score_total", 0.0)
                    break
            name_to_cases[top1].append((score, c))

    demoted = 0
    for name, entries in name_to_cases.items():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda x: -x[0])
        for score, c in entries[1:]:
            c["unique_top1_function"] = None
            c["unique_top1_hit"] = None
            demoted += 1

    print(f"[STEP 3b] Dedup — demoted {demoted} duplicate top-1 entries")
    save_json(retrieval_path, data)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — re-run retrieval + propagation
# ─────────────────────────────────────────────────────────────────────────────

def run(label: str, cmd: list[str]) -> None:
    import time
    print(f"\n{'='*60}\n[{label}] running...")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    elapsed = time.time() - t0
    status = "OK" if r.returncode == 0 else f"FAILED (rc={r.returncode})"
    print(f"[{label}] {status} ({elapsed:.1f}s)")
    if r.returncode != 0:
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--result-dir",    type=Path, required=True)
    p.add_argument("--query-json",    type=Path, required=True)
    p.add_argument("--index",         type=Path,
                   default=PROJECT_ROOT / "data/inputs/retrieval_indexes/Lua_536/x86_64/runtime")
    p.add_argument("--reference-db",  type=Path,
                   default=PROJECT_ROOT / "data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite",
                   help="Reference callgraph SQLite for targeted retrieval (12c).")
    p.add_argument("--lua-version",   type=str, default="Lua_536",
                   help="Lua version string used to filter reference DB edges (default Lua_536).")
    p.add_argument("--skip-retrieval", action="store_true",
                   help="Skip embedding retrieval (uses existing retrieval_result.json).")
    p.add_argument("--skip-targeted", action="store_true",
                   help="Skip targeted retrieval step (12c_targeted_retrieval.py).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result_dir    = args.result_dir.resolve()
    query_json    = args.query_json.resolve()
    retrieval_json    = result_dir / "retrieval_result.json"
    targeted_json     = result_dir / "targeted_retrieval.json"
    seed_anchors  = result_dir / "seed_anchors.json"
    suite_json    = result_dir / "suite.json"
    prop_json     = result_dir / "propagation_result.json"
    deferred_json = result_dir / "deferred_analysis.json"
    final_json    = result_dir / "final_mapping_report.json"

    PYTHON = sys.executable

    # ── 1. patch features ────────────────────────────────────────────────────
    patched_query = patch_features(query_json, CONFIRMED)

    # ── 2. patch anchors (force anchors from CONFIRMED dict) ─────────────────
    patch_anchors(seed_anchors, query_json, CONFIRMED)

    # ── 3. re-retrieval with patched features ────────────────────────────────
    if not args.skip_retrieval:
        run("12_retrieval_patched", [
            PYTHON, "scripts/12_run_bulk_query_retrieval.py",
            "--query-json",  str(patched_query),
            "--index",       str(args.index),
            "--output-json", str(retrieval_json),
        ])
    else:
        print("\n[SKIP] retrieval step (--skip-retrieval)")

    # ── 3b. targeted retrieval (callgraph neighbor voting, no embeddings) ────
    if not args.skip_targeted:
        run("12c_targeted", [
            PYTHON, "scripts/12c_targeted_retrieval.py",
            "--query-json",   str(patched_query),
            "--anchors-json", str(seed_anchors),
            "--reference-db", str(args.reference_db),
            "--output-json",  str(targeted_json),
            "--lua-version",  args.lua_version,
        ])
        # ── 2b. inject targeted anchors into seed_anchors.json ───────────────
        inject_targeted_anchors(seed_anchors, targeted_json)
    else:
        print("\n[SKIP] targeted retrieval step (--skip-targeted)")

    # ── 4. dedup retrieval ───────────────────────────────────────────────────
    dedup_retrieval(retrieval_json)

    # ── 5. rebuild suite with updated anchor ─────────────────────────────────
    suite = load_json(suite_json)
    # suite already points to correct paths; just ensure lua_version_override
    suite.setdefault("scoring", {})["lua_version_override"] = "Lua_536"
    save_json(suite_json, suite)

    # ── 6. propagation ───────────────────────────────────────────────────────
    run("04_propagation", [
        PYTHON, "scripts/04_propagate_from_anchors.py",
        "--suite", str(suite_json),
        "--iterative",
    ])

    # ── 7. deferred ──────────────────────────────────────────────────────────
    run("05_deferred", [
        PYTHON, "scripts/05_build_deferred_analysis.py",
        "--input-json",  str(prop_json),
        "--output-json", str(deferred_json),
    ])

    # ── 8. final report ──────────────────────────────────────────────────────
    run("15_final_report", [
        PYTHON, "scripts/15_export_final_mapping_report.py",
        "--propagation-json", str(prop_json),
        "--deferred-json",    str(deferred_json),
        "--output-json",      str(final_json),
        "--session-name",     "artale_libengine_lua536",
    ])

    # ── summary ──────────────────────────────────────────────────────────────
    report = load_json(final_json)
    s = report.get("summary", {})
    print("\n" + "="*60)
    print("Pipeline complete!")
    print(f"  accepted : {s.get('accepted')}")
    print(f"  deferred : {s.get('deferred')}")
    print(f"  conflict : {s.get('conflict')}")


if __name__ == "__main__":
    main()
