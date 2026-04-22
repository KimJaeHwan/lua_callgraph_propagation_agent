# Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent`는 Lua 함수 name mapping을 실제로 수행하는 단일 레포 런타임이다.  
이 레포는 다음 단계를 한 곳에서 연결한다.

- query binary 또는 pre-extracted feature 입력
- hybrid retrieval top-k 생성
- callgraph 기반 seed anchor 선택
- propagation / conflict / deferred 분류
- deferred 분석 payload 생성
- optional local LLM analyst 연결
- FastMCP 서버로 tool interface 제공

연구 단계에서 쓰였던 sibling repository는 여전히 데이터 생성 이력으로 남아 있지만, 실제 운용 경로는 이 레포 안에 복사된 runtime asset을 기준으로 정리한다.

## 현재 런타임 구성

- vendored extractor: [src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py)
- vendored retrieval engine: [src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py)
- pipeline entrypoint: [scripts/10_run_name_mapping_pipeline.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/scripts/10_run_name_mapping_pipeline.py)
- FastMCP server: [scripts/20_run_mcp_server.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/scripts/20_run_mcp_server.py)
- runtime asset bootstrap: [scripts/21_prepare_runtime_assets.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/scripts/21_prepare_runtime_assets.py)

핵심 입력 위치:

- reference features: `data/inputs/reference_features/`
- runtime retrieval index 기본값: `data/inputs/retrieval_indexes/lua547_x86_runtime`
- sample binaries: `data/runtime/input/`
- optional pre-extracted query feature: `data/inputs/query_features/`

## Quick Start

처음 한 번 runtime asset을 준비한다.

```bash
../lua_llm/bin/python scripts/21_prepare_runtime_assets.py --force
```

이 단계가 끝나면 다음이 이 레포 안에 준비된다.

- Lua 5.4.7 vanilla reference feature 세트
- sample binary
- sample query feature JSON

기본 실행은 이 레포 안에 복사된 full retrieval index를 사용한다.

- 기본 index: `data/inputs/retrieval_indexes/lua547_x86_runtime`
- 즉, 파이프라인 실행 시 sibling repository 경로를 직접 참조하지 않아도 된다.

## 검증된 실행 경로

현재 이 레포에서 끝까지 검증된 경로는 pre-extracted query feature 기준이다.

```bash
../lua_llm/bin/python scripts/10_run_name_mapping_pipeline.py \
  --config data/configs/runtime_lua547_x86_demo_preextracted.json \
  --stop-on-error
```

이 실행은 실제로 완료되었고, 결과는 다음 위치에 생성된다.

- retrieval: `data/runtime/results/lua547_x86_demo_preextracted/retrieval_result.json`
- propagation: `data/runtime/results/lua547_x86_demo_preextracted/propagation_result.json`
- deferred analysis: `data/runtime/results/lua547_x86_demo_preextracted/deferred_analysis.json`
- final report: `data/runtime/results/lua547_x86_demo_preextracted/final_mapping_report.json`

샘플 실행 결과 요약:

- total cases: `1095`
- accepted: `1006`
- deferred: `77`
- conflict: `12`

## Binary Extraction 경로

binary에서 바로 feature를 뽑는 설정도 포함되어 있다.

```bash
../lua_llm/bin/python scripts/10_run_name_mapping_pipeline.py \
  --config data/configs/runtime_lua547_x86_demo.json \
  --stop-on-error
```

현재는 runtime wrapper가 `pyghidra` / `Ghidra` 환경을 더 보수적으로 맞추도록 보정되어, 실제 processed binary 대상 extraction smoke test와 MCP 경유 extraction 생성까지 확인했다.

정리 문서:

- [docs/extraction_runtime_environment.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/extraction_runtime_environment.md)

다만 대규모 real-binary case는 extraction 이후 retrieval / propagation이 오래 걸릴 수 있으므로, 현재는 pre-extracted config가 가장 빠른 검증 경로다.

## FastMCP

이 레포는 FastMCP 기반 stdio 서버를 제공한다.

실행:

```bash
../lua_llm/bin/python scripts/20_run_mcp_server.py
```

현재 주요 tool:

- `pipeline_dry_run`
- `pipeline_run`
- `extract_query_features`
- `bulk_query_retrieval`
- `run_local_llm_analyst`
- `read_final_report`
- `read_mapping_record`

FastMCP 클라이언트 기준으로 `bulk_query_retrieval`, `read_final_report`, `read_mapping_record`, 그리고 실제 binary extraction을 포함한 `pipeline_run` 경로까지 점검했다.

## Local LLM

Local LLM은 기본 경로가 아니라 optional analyst layer다.

- deterministic retrieval + graph scoring만으로 확정 가능한 함수는 자동 accept
- 애매한 함수만 deferred/conflict로 분리
- 그 뒤에만 `scripts/06_run_local_llm_analyst.py`를 붙인다

즉, 이 프로젝트의 기본 철학은 “LLM이 최종 판정자가 아니라, 애매한 함수만 도와주는 reviewer”에 가깝다.

## Retrieval Index 정책

기본 정책은 full index를 내부 복사본으로 유지하는 것이다.

- slim 실험에서는 index 크기를 크게 줄일 수 있었지만 정확도가 유의미하게 떨어졌다.
- 그래서 runtime은 slim이 아니라 full index를 채택했다.
- 다만 실제 운용 편의성을 위해 full index 자체를 이 레포 안으로 복사해서 사용한다.
- slim 정책과 실험 결과는 `lua_function_embedding` 쪽 문서로 남겨 두었다.

## 남겨둔 문서

- [docs/input_schema.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/input_schema.md)
- [docs/callgraph_propagation_agent_design.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/callgraph_propagation_agent_design.md)
- [docs/callgraph_store_design.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/callgraph_store_design.md)
- [docs/mcp_runtime.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_runtime.md)
- [docs/extraction_runtime_environment.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/extraction_runtime_environment.md)
