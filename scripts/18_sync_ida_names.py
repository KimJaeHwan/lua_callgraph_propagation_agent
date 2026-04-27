#!/usr/bin/env python3
"""
18_sync_ida_names.py

Auto-sync: reads IDA-renamed functions (via MCP list_functions or a names dump)
and cross-references them with the query feature JSON entry_points to produce
new CONFIRMED-dict entries that can be pasted into 17_patch_and_rerun.py.

Two modes
---------
A) --ida-names-json   : JSON file exported from IDA (array of {name, address})
B) --scan-confirmed   : just prints the current CONFIRMED dict + counts

Usage
-----
  # Export names from IDA first (run in IDA Python console):
  #   import json, idautils, idc
  #   funcs = [{"name": idc.get_func_name(ea), "address": hex(ea)}
  #            for ea in idautils.Functions()
  #            if not idc.get_func_name(ea).startswith(("sub_", "FUN_", "nullsub_", "j_"))]
  #   open("ida_names.json","w").write(json.dumps(funcs, indent=2))

  python scripts/18_sync_ida_names.py \\
      --query-json  data/runtime/query_features/artale_libengine_lua536/Lua_536/x86_64/O2/stripped/libengine_20260427_102236.json \\
      --ida-names-json  ida_names.json \\
      --exclude-prefixes  lua_api_,luaL_,luaB_,luaV_,luaD_,luaH_,luaS_,luaC_,luaM_,luaO_,luaG_,luaF_,luaT_,luaopen_,create,lua_,sub_,FUN_,nullsub_ \\
      --output-confirmed  scripts/confirmed_auto.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Functions already in CONFIRMED — skip them in output
ALREADY_CONFIRMED: set[str] = {
    # round 1
    "luaopen_base", "lua_setfield", "lua_pushlstring", "lua_pushvalue",
    "lua_pushcclosure", "luaL_setfuncs", "lua_rawgeti", "luaB_type",
    "lua_index2value", "luaS_newlstr", "luaV_settable", "luaL_checkany",
    "luaH_get", "createstrobj", "luaH_getint", "lua_settop",
    "luaM_realloc_", "luaH_getstr", "luaL_argerror", "luaC_step",
    # round 2
    "luaopen_utf8", "luaopen_table", "luaopen_io", "luaopen_math",
    "luaopen_package", "luaopen_debug", "luaopen_coroutine",
    "luaopen_string", "luaopen_os", "luaopen_bit32",
    # round 3
    "luaV_execute", "luaD_call", "lua_resume", "luaD_poscall",
    "lua_pcallk", "luaD_pcall", "luaD_rawrunprotected",
    "luaB_pcall", "ccall",
    # round 4 — trusted_mappings batch
    "lua_pushstring", "lua_tolstring", "lua_xmove", "lua_concat",
    "lua_rotate", "lua_pushvfstring", "lua_seti", "lua_setmetatable",
    "lua_getmetatable", "lua_setupvalue", "lua_isstring", "lua_copy",
    "lua_pushnil", "lua_upvalueid", "lua_gettop", "lua_type",
    "lua_newstate", "lua_newthread",
    "luaL_checklstring", "luaL_where", "luaL_checkoption", "luaL_addvalue",
    "luaL_len", "luaL_pushresult", "luaL_optlstring", "luaL_gsub",
    "luaL_loadfilex", "luaL_getmetafield", "luaL_execresult",
    "luaV_div", "luaV_tointeger", "luaV_finishOp", "luaV_shiftl",
    "luaT_trybinTM", "luaT_callbinTM",
    "luaG_tointerror", "luaG_runerror", "luaG_concaterror", "luaG_errormsg",
    "luaK_patchclose", "luaK_setreturns", "luaK_patchtohere",
    "luaK_getlabel", "luaK_codeABC",
    "luaH_set", "luaH_setint", "luaH_newkey",
    "luaD_growstack", "luaD_reallocstack",
    "luaC_runtilstate", "luaC_barrier_",
    "luaM_toobig",
    "luaO_int2fb", "luaO_fb2int", "luaO_pushvfstring", "luaO_utf8esc",
    "luaF_freeproto", "luaF_close",
    "luaX_newstring",
    "luaB_coresume", "luaB_cocreate",
    "str_rep", "str_char", "str_sub", "str_packsize", "str_pack",
    "f_setvbuf", "io_read", "io_write", "io_tmpfile", "io_readline",
    "opencheck", "aux_close", "g_write",
    "os_rename", "os_tmpname", "math_randomseed",
    "searcher_preload", "ll_searchpath", "searcher_Croot", "searchpath",
    "ll_require", "pushutfchar", "byteoffset", "codepoint",
    "b_lshift", "b_test", "b_rot", "tconcat", "iter_aux", "unpack",
    "new_localvar", "fieldargs", "cond", "newlabelentry", "inclinenumber",
    "check_next2", "singlevaraux", "block_follow", "explist", "checkliteral",
    "subexpr", "open_func", "freeexps", "test_then_block", "ipairsaux",
    "getthread", "traverseephemeron", "GCTM", "convergeephemerons",
    "atomic", "restartcollection", "gctm", "traverseproto",
    "LoadUpvalues", "f_parser", "f_luaopen", "getF", "read_number",
    "read_long_string", "l_checkmode",
    "getdetails", "db_getinfo", "findpcall", "pushglobalfuncname",
    "seterrorobj", "recover", "panic", "stack_init", "unpackint",
    "errorlimit", "mainposition", "l_alloc", "start_capture", "classend",
    "checkupval", "treatstackoption", "l_message", "msghandler", "funcinfo",
    "l_strcmp", "checkmode", "getnumlimit",
}


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def build_ep_map(query_json_path: Path) -> dict[int, str]:
    """entry_point (int) -> ghidra function_name"""
    funcs = load_json(query_json_path)
    if not isinstance(funcs, list):
        funcs = funcs.get("functions", [])
    result = {}
    for f in funcs:
        try:
            ep = int(f["entry_point"], 16)
            result[ep] = f["function_name"]
        except (KeyError, ValueError):
            pass
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Sync IDA renamed functions to CONFIRMED dict")
    p.add_argument("--query-json", type=Path, required=True,
                   help="Path to query features JSON (Ghidra output)")
    p.add_argument("--ida-names-json", type=Path, default=None,
                   help="JSON array of {name, address} exported from IDA")
    p.add_argument("--exclude-prefixes", default="",
                   help="Comma-separated name prefixes to EXCLUDE from output")
    p.add_argument("--output-confirmed", type=Path, default=None,
                   help="Save new confirmed mappings to this JSON file")
    p.add_argument("--patch-script", action="store_true",
                   help="Print Python snippet to paste into 17_patch_and_rerun.py")
    args = p.parse_args()

    exclude = tuple(x for x in args.exclude_prefixes.split(",") if x)
    ep_map = build_ep_map(args.query_json)

    if not args.ida_names_json:
        print(f"Already confirmed: {len(ALREADY_CONFIRMED)} functions")
        print("Pass --ida-names-json to sync new names from IDA.")
        return

    ida_names = load_json(args.ida_names_json)  # [{name, address}, ...]

    new_confirmed: dict[str, str] = {}  # ep_hex -> real_name

    for entry in ida_names:
        name = entry.get("name", "")
        addr_raw = entry.get("address", "0x0")
        if not name:
            continue
        # skip already confirmed
        if name in ALREADY_CONFIRMED:
            continue
        # skip excluded prefixes
        if name.startswith(exclude):
            continue
        # skip internal IDA names
        if name.startswith(("sub_", "FUN_", "nullsub_", "j_", "off_", "loc_", "unk_")):
            continue

        try:
            ep_int = int(addr_raw, 16)
        except ValueError:
            continue

        # match against feature JSON
        if ep_int not in ep_map:
            continue

        ep_hex = format(ep_int, "x")
        new_confirmed[ep_hex] = name

    print(f"\nNew confirmed mappings found: {len(new_confirmed)}")
    for ep, name in sorted(new_confirmed.items()):
        ghidra_name = ep_map.get(int(ep, 16), "?")
        print(f"  {ep}  ({ghidra_name}) -> {name}")

    if args.output_confirmed and new_confirmed:
        out = args.output_confirmed
        out.write_text(json.dumps(new_confirmed, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved: {out}")

    if args.patch_script and new_confirmed:
        print("\n# ── Paste into scripts/17_patch_and_rerun.py CONFIRMED dict ──")
        print("    # Round N — auto-synced from IDA")
        for ep, name in sorted(new_confirmed.items()):
            print(f'    "{ep}":  "{name}",')


if __name__ == "__main__":
    main()
