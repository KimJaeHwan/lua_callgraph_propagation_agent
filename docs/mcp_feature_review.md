# MCP Feature Review

Current `lua_callgraph_propagation_agent` MCP coverage after the libengine analysis loop.

## Already Implemented

- Direct runtime steps
  - `extract_query_features`
  - `bulk_query_retrieval`
  - `select_seed_anchors`
  - `build_runtime_suite`
- Report / triage helpers
  - `list_deferred_cases`
  - `read_final_report`
  - `read_mapping_record`
  - `read_propagation_summary`
- Anchor workflow
  - `register_force_anchor`
  - `batch_register_force_anchors`
  - `run_downstream`

## Intentionally Not Exposed

- `scripts/10_run_name_mapping_pipeline.py` wrapper tools
  - removed from MCP on purpose
  - reason: binary analysis must keep extraction and analysis in separate processes
  - this avoids encouraging one-call execution that can recreate Ghidra JVM / embedding memory overlap

## Added In This Round

- `remove_force_anchor`
  - Removes only `source="force_anchor"` mappings for one query function.
  - Can optionally re-run downstream steps.
- `show_candidate_context`
  - Bundles one analyst-facing case context from:
    - `final_mapping_report.json`
    - `deferred_analysis.json`
    - `seed_anchors.json`
    - query feature JSON summary

## Why These Two Matter

- `remove_force_anchor`
  - Makes anchor experimentation reversible.
  - Useful when a decompile-based decision later turns out to be wrong.
- `show_candidate_context`
  - Reduces context switching during analyst review.
  - Puts mapping status, deferred rationale, top candidates, current anchors, and query features in one place.

## Highest-Priority Next Additions

- `query address -> containing function`
  - Needed for Ghidra/IDA function-boundary mismatch handling.
- `library table decoder`
  - Automatically decode `luaL_Reg`-style registration tables around `luaopen_*`.
- `conflict diff`
  - Show the competing query functions and evidence for one conflicting reference target.
- `accepted-neighbor explorer`
  - Summarize already-accepted caller/callee anchors around one deferred case.
