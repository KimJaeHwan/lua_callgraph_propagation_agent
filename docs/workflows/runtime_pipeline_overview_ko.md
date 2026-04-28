# Lua 함수 이름 매핑 런타임 개요

이 문서는 `lua_callgraph_propagation_agent`가 바이너리 하나를 입력받았을 때 함수 이름 매핑을 어떻게 진행하는지, 그리고 FastMCP + IDA Pro MCP를 포함한 analyst loop가 어떻게 돌아가는지를 정리한다.

핵심 관점은 세 가지다.

- 이 프로젝트는 처음부터 모든 함수에 이름을 붙이지 않는다.
- 먼저 retrieval로 후보를 넓게 모으고, seed anchor를 매우 보수적으로 고른 뒤, callgraph propagation으로 accepted를 확장한다.
- 이후 분석가가 IDA에서 일부 결과를 검증하고 rename하면, 그 사실이 다음 라운드의 retrieval과 propagation 품질을 높인다.

## 1. 입력 바이너리에서 최종 매핑까지

### 1-1. Feature extraction

입력은 보통 stripped `.so` 또는 ELF 바이너리다. 먼저 [scripts/11_extract_query_features.py](../scripts/11_extract_query_features.py)가 Ghidra/pyghidra 기반 extractor를 분리된 프로세스로 실행해서 함수별 feature를 JSON으로 만든다.

이 단계 산출물:

- `extract_manifest.json`
- 함수 feature JSON
- 함수별 callgraph, 문자열, 상수, pcode 크기 등의 정적 feature

이 단계는 이름을 붙이는 단계가 아니라, 이후 retrieval이 소비할 query 표현을 만드는 단계다.

### 1-2. Lua scope detection

[scripts/12b_detect_lua_scope.py](../scripts/12b_detect_lua_scope.py)는 mixed binary 안에서 Lua VM 쪽 함수만 우선 골라내기 위한 필터다.

동작:

- Lua 특유 문자열 신호 탐지
- 해당 함수들을 high-confidence seed로 사용
- caller/callee 방향 BFS 확장
- 너무 큰 함수는 제외

산출물은 `lua_scope.json` 이고, 이 파일은 이후 retrieval과 seed 선택에서 game code가 섞이는 문제를 줄이는 데 쓰인다.

### 1-3. Bulk retrieval

[scripts/12_run_bulk_query_retrieval.py](../scripts/12_run_bulk_query_retrieval.py)는 각 query 함수에 대해 reference Lua 함수 후보 top-k를 검색한다.

여기서 retrieval은 단순 embedding이 아니라 hybrid 방식이다.

- semantic similarity
- symbolic token overlap
- 수치 feature
- caller/callee 관련 정보

산출물은 `retrieval_result.json` 이다. 이 시점 결과는 최종 매핑이 아니라 함수별 후보집이다.

### 1-4. Seed anchor selection

[scripts/13_select_seed_anchors.py](../scripts/13_select_seed_anchors.py)는 retrieval 결과 중 일부만 고신뢰 anchor로 채택한다.

주요 source:

- `name_visible`
- `retrieval_high_confidence`
- 반복 라운드에서는 `targeted_high_confidence`

이 단계가 중요한 이유는 propagation 품질이 seed 품질에 크게 의존하기 때문이다. 그래서 아래 조건을 강하게 건다.

- top-1 점수 임계치
- top-1 vs top-2 margin
- dedup-first
- scope gate

산출물은 `seed_anchors.json` 이다.

### 1-5. Propagation suite build

[scripts/14_build_runtime_propagation_suite.py](../scripts/14_build_runtime_propagation_suite.py)는 retrieval 결과, seed anchor, reference DB, scoring 정책, classification 정책을 propagation용 suite JSON으로 조립한다.

이 단계는 실행 정책을 묶는 단계이며, 실제 함수명 판정은 아직 하지 않는다.

### 1-6. Propagation

실제 함수명 판정의 중심은 [scripts/04_propagate_from_anchors.py](../scripts/04_propagate_from_anchors.py) 이다.

각 query 함수에 대해 다음을 수행한다.

- accepted된 caller/callee 이웃을 anchor로 수집
- 이 anchor를 reference 함수 이름 쪽으로 투영
- retrieval 후보와 reference graph 이웃을 함께 평가
- `retrieval_prior + graph_score` 기반 점수 계산
- margin, tie, graph evidence를 기준으로 `accepted`, `deferred`, `conflict` 분류

iterative 모드에서는 이번 라운드에 새로 accepted된 함수가 다음 라운드의 anchor가 된다. 그래서 초기 seed로 닿지 않던 2-hop, 3-hop 함수까지 점점 확장된다.

### 1-7. Deferred analysis / final report

Propagation 결과는 후속 정리 단계를 거쳐 최종 리포트로 묶인다.

- `propagation_result.json`
- `deferred_analysis.json`
- `final_mapping_report.json`

[scripts/15_export_final_mapping_report.py](../scripts/15_export_final_mapping_report.py)는 최종 compact report를 만든다. 여기서 `accepted`는 새로 결정되는 것이 아니라 propagation 단계에서 이미 정해진 결과를 요약한 것이다.

## 2. 실제로 함수명이 결정되는 순간

함수명이 "진짜로 결정되는 순간"은 retrieval 단계가 아니라 propagation 분류 단계다.

정확히는 [scripts/04_propagate_from_anchors.py](../scripts/04_propagate_from_anchors.py) 안에서 후보 점수와 주변 anchor evidence를 합쳐서 status를 `accepted`로 올리는 시점이 핵심이다.

즉 순서는 다음과 같다.

1. retrieval은 후보를 제안한다.
2. seed selection은 출발점으로 쓸 만한 것만 남긴다.
3. propagation이 구조적 근거를 합쳐 최종 채택 여부를 판정한다.

그래서 `retrieval_result.json`만 보고는 최종 이름 매핑을 알 수 없고, 실제 채택은 `propagation_result.json` 또는 `final_mapping_report.json`을 봐야 한다.

## 3. 산출물 생성 순서

일반적으로 결과 디렉터리에는 아래 순서로 파일이 생긴다.

1. `extract_manifest.json`
2. `lua_scope.json`
3. `retrieval_result.json`
4. `seed_anchors.json`
5. `runtime_propagation_suite.json`
6. `propagation_result.json`
7. `deferred_analysis.json`
8. `final_mapping_report.json`

반복 라운드까지 들어가면 여기에 추가로 다음이 붙는다.

1. `trusted_mappings.json`
2. `targeted_retrieval.json`
3. patched feature JSON
4. 갱신된 `seed_anchors.json`
5. 갱신된 `propagation_result.json`

## 4. MCP 기준 analyst loop

[src/lua_callgraph_propagation_agent/mcp_server.py](../src/lua_callgraph_propagation_agent/mcp_server.py)는 이 전체 흐름을 tool 단위로 노출한다. README와 MCP instructions 기준 권장 루프는 아래처럼 읽으면 된다.

초기 라운드:

1. `extract_query_features`
2. `detect_lua_scope`
3. `bulk_query_retrieval`
4. `select_seed_anchors`
5. `build_runtime_suite`
6. `run_downstream`
7. `get_mapping_distribution`
8. `update_noise_blacklist`
9. `run_downstream`

반복 라운드:

1. `export_trusted_mappings`
2. IDA에서 검증 및 rename
3. `batch_register_force_anchors`
4. `patch_features_with_confirmed`
5. `targeted_retrieval`
6. `select_seed_anchors`
7. `run_downstream`
8. 새 accepted가 없을 때까지 반복

## 5. IDA Pro MCP를 포함한 해석 루프

실전에서는 final report만 보고 끝나지 않고, 애매하지만 유망한 함수나 trusted mapping 후보를 IDA에서 직접 검증하는 루프가 중요하다.

이 루프는 보통 다음처럼 동작한다.

1. MCP에서 `export_trusted_mappings` 또는 deferred 케이스를 추린다.
2. IDA Pro MCP로 대상 함수 주소를 연다.
3. `decompile`로 고수준 로직을 읽는다.
4. caller/callee, 문자열, 상수, switch, stack frame을 확인한다.
5. 함수 의미가 확인되면 `rename`으로 실제 Lua 이름을 부여한다.
6. 그 결과를 force anchor 또는 confirmed mapping으로 다시 파이프라인에 주입한다.

여기서 핵심은 IDA가 단순 시각화 도구가 아니라, propagation이 헷갈리는 경계 케이스를 사람이 해석해서 다음 라운드 성능을 끌어올리는 검증 장치라는 점이다.

특히 `decompile -> 의미 해석 -> rename -> anchor 주입`이 연결되는 순간, 다음 라운드부터는 해당 함수의 이름이 caller/callee feature에도 반영될 수 있고 targeted retrieval의 구조 제약도 더 강해진다.

## 6. 다이어그램

일반 런타임 파이프라인 다이어그램은 [runtime_pipeline_flow.mmd](runtime_pipeline_flow.mmd)에 있다.

MCP + IDA analyst loop 다이어그램은 [mcp_ida_analysis_loop.mmd](mcp_ida_analysis_loop.mmd)에 있다.