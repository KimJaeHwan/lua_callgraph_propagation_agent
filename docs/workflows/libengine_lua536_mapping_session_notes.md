# libengine Lua 5.3.6 Mapping Session Notes

This note captures the current manual mapping progress for `libengine.so` so the
same reasoning can later be reproduced with a local LLM + LangGraph workflow.

## Session Target

- Binary: `libengine.so`
- Expected Lua family: `Lua_536`
- Architecture: `aarch64`
- Goal:
  - map at least 200 Lua-related functions, or
  - make the flow from file/string input to `luaV_execute` operationally clear

## Current Confirmed Anchors

### Base library and core openers

- `0x4da44c` -> `luaopen_base`
- `0x4cf584` -> `luaopen_string`
- `0x4d357c` -> `luaopen_table`
- `0x4d7180` -> `luaopen_utf8`
- `0x4dd6f4` -> `luaopen_coroutine`
- `0x4c8384` -> `luaopen_package`
- `0x4c5990` -> `luaopen_io`
- `0x4ca1ac` -> `luaopen_os`
- `0x4db8d4` -> `luaopen_bit32`

### Base library functions

- `0x4da520` -> `luaB_type`
- `0x4da58c` -> `luaB_assert`
- `0x4da768` -> `luaB_dofile`
- `0x4da7f0` -> `luaB_error`
- `0x4da8ec` -> `luaB_ipairs`
- `0x4da93c` -> `luaB_loadfile`
- `0x4da9c0` -> `luaB_load`
- `0x4daaf8` -> `luaB_next`
- `0x4dabec` -> `luaB_pairs`
- `0x4dac14` -> `luaB_pcall`
- `0x4dacb8` -> `luaB_print`
- `0x4dae14` -> `luaB_rawequal`
- `0x4dae58` -> `luaB_rawlen`
- `0x4daf04` -> `luaB_rawget`
- `0x4daf48` -> `luaB_rawset`
- `0x4daf98` -> `luaB_select`
- `0x4db094` -> `luaB_setmetatable`
- `0x4db3ec` -> `luaB_tostring`
- `0x4db418` -> `luaB_xpcall`

### Load / parse / execution chain

- `0x4db718` -> `load_aux`
- `0x4db620` -> `generic_reader`
- `0x4d9b68` -> `getS`
- `0x4db4cc` -> `finishpcall`
- `0x4c0cdc` -> `lua_load`
- `0x4d8f94` -> `luaL_loadfilex`
- `0x4c0ac0` -> `lua_callk`
- `0x4c0b60` -> `lua_pcallk`
- `0x4d891c` -> `luaL_checkany`
- `0x4c2994` -> `luaD_precall`
- `0x4c2558` -> `luaD_rawrunprotected`
- `0x4c32b4` -> `runprotected_wrapper`
- `0x4c3340` -> `f_parser`
- `0x4d5518` -> `luaV_execute`

### Coroutine helpers

- `0x4ddd90` -> `getco`
- `0x4ddb0c` -> `auxresume`
- `0x4dda50` -> `luaB_auxwrap`

### Internal wrappers worth preserving

- `0x4c32b4` -> `runprotected_wrapper`
  - used by `lua_pcallk`
  - used by `lua_load`
  - restores execution state after `luaD_rawrunprotected`
- `0x4c0cc8` -> `pcall_trampoline` (behavioral description only for now)
  - very small helper that forwards into `sub_4C2E3C`
  - used by `runprotected_wrapper` inside the non-continuation path of
    `lua_pcallk`

## Flow Confidence Summary

### Strongly supported flow

1. `luaB_dofile`
   - calls `luaL_loadfilex`
   - on success transitions into `lua_callk`
2. `luaB_loadfile`
   - calls `luaL_loadfilex`
   - returns through `load_aux`
3. `luaB_load`
   - uses `generic_reader` / `getS`
   - invokes `lua_load`
   - returns through `load_aux`
4. `lua_load`
   - dispatches parsing through `f_parser`
   - runs parser under `luaD_rawrunprotected`
5. `lua_callk`
   - transitions into internal call engine
6. `luaD_precall`
   - is reached before `luaV_execute`
7. `luaV_execute`
   - confirmed VM execution body

### Confirmed by reference callgraph plus IDA evidence

- `luaB_dofile -> lua_callk`
- `luaB_load -> lua_load`
- `luaB_load -> load_aux`
- `luaB_loadfile -> load_aux`
- `luaB_pcall -> lua_pcallk`
- `luaB_pcall -> finishpcall`
- `luaB_xpcall -> lua_pcallk`
- `lua_callk -> luaD_call`
- `lua_callk -> luaD_callnoyield`
- `luaD_call -> luaD_precall`
- `luaD_call -> luaV_execute`
- `lua_load -> luaD_protectedparser`
- `luaD_protectedparser -> luaD_rawrunprotected`
- `luaB_coresume -> auxresume`
- `luaB_auxwrap -> auxresume`
- `getco -> auxresume`
- `auxresume -> luaD_rawrunprotected`
- `lua_pcallk -> runprotected_wrapper -> pcall_trampoline -> sub_4C2E3C`
- `lua_load -> runprotected_wrapper -> luaD_rawrunprotected`

## Ambiguous / Deferred Nodes

### `0x4c2e3c`

Current status:

- not renamed yet
- has both:
  - `luaD_precall -> luaV_execute` style behavior
  - coroutine resume error strings like:
    - `cannot resume dead coroutine`
    - `cannot resume non-suspended coroutine`

Interpretation:

- likely shared internal call/resume engine
- `auxresume` also reaches protected execution through `luaD_rawrunprotected`,
  which strengthens the possibility that this region is tied to coroutine
  resume behavior rather than plain non-yielding call dispatch
- possible candidates:
  - `luaD_call`
  - `luaD_callnoyield`
  - `lua_resume`
  - merged helper serving more than one of the above

Decision:

- keep unnamed for now
- preserve comment in IDA rather than forcing a wrong rename

## Why These Renames Are Reliable

The most reliable anchors were accepted only when at least one of these held:

1. registration-table evidence
   - `luaopen_base` and other `luaopen_*` functions directly register names
2. distinctive string evidence
   - `_VERSION`, `_G`, `charpattern`, `stdin`, `stdout`, etc.
3. reference callgraph agreement
   - reference DB edges line up with observed caller/callee behavior
4. decompiler role match
   - function body behavior matches Lua 5.3 implementation role

If none of these were strong enough, the function was left unnamed.

## LangGraph Translation Notes

The manual workflow used here should become a graph of small, auditable steps.

### Suggested nodes

1. `collect_candidate_context`
   - gather retrieval result
   - gather targeted retrieval result
   - gather reference strings
   - gather IDA decompile / xrefs / callers / callees

2. `decode_registration_table`
   - if candidate is referenced by a `luaopen_*` table, prefer table-derived names

3. `verify_string_anchor`
   - compare query strings with reference-side `function_strings`
   - use only as support, not as sole authority

4. `verify_callgraph_relation`
   - compare candidate against known reference edges
   - important for `load`, `pcall`, `call`, `vm` chain

5. `separate_wrapper_from_engine`
   - distinguish small forwarding wrappers from the shared execution engine
   - examples:
     - `runprotected_wrapper`
     - `pcall_trampoline`
     - `auxresume`

6. `classify_confidence`
   - `confirmed`
   - `needs_ida_review`
   - `defer`

7. `apply_rename_or_comment`
   - rename only for `confirmed`
   - write cautionary comment for ambiguous internal helpers

8. `sync_force_anchors`
   - register safe anchors back into runtime pipeline
   - rerun downstream propagation

### Key policy for the agent

- do not force-renamed internal dispatcher functions on retrieval score alone
- prefer registration-table and execution-flow evidence
- preserve ambiguity explicitly when a helper appears shared or merged

## Immediate Next Step

Focus next on splitting the `0x4c2e3c` region:

- distinguish `luaD_call`
- distinguish `luaD_callnoyield`
- distinguish `lua_resume`
- keep `luaV_execute` and `luaD_precall` as fixed anchors while doing so

This is the highest-value next step for both:

- human-readable execution flow
- future LangGraph automation
