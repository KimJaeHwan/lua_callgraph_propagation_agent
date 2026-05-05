# MCP Quickstart Guide

`lua_callgraph_propagation_agent` MCP는 "Lua 함수 이름 매핑 파이프라인을 단계별로 조작하는 도구 모음"이다.

처음 붙이면 가장 헷갈리는 점은 이거다.

- 어떤 tool을 언제 써야 하는지
- full pipeline과 downstream rerun의 차이
- force anchor를 언제 넣어야 하는지
- binary 입력과 pre-extracted 입력이 어떻게 다른지

이 문서는 그 흐름을 빠르게 익히기 위한 입문 가이드다.

## 먼저 확인할 것: sentence_transformers 설치 여부

`bulk_query_retrieval` (embedding 검색) 은 `sentence-transformers` 라이브러리가 **반드시** 필요하다.

```bash
# 설치 확인
pip show sentence-transformers

# 없으면 설치 (2~3 GB, 5~15분)
pip install sentence-transformers
```

### 설치 안 된 상태에서 쓰면 어떻게 되나

`12_run_bulk_query_retrieval.py`가 즉시 crash 한다.  
나머지 스크립트(propagation, targeted_retrieval, 결과 조회 등)는 모두 정상 동작한다.

manual force anchor나 patched feature를 반영한 뒤에는, 기존 retrieval 결과 재사용보다
실제 `bulk_query_retrieval`을 다시 태우는 쪽이 더 안전하다.

## 한 줄 이해

이 MCP는 크게 4가지를 한다.

1. 입력 준비
2. 분석 실행
3. 결과 읽기
4. analyst 개입 후 재실행

즉 "자동 분석 + 사람이 확정한 anchor 반영 + 재전파"를 반복하기 위한 인터페이스라고 보면 된다.

## 가장 먼저 이해할 개념

### 1. binary 분석과 pre-extracted 분석은 다르다

binary 입력은 Ghidra가 필요하다.

- 먼저 feature extraction
- 그다음 retrieval/propagation

이 둘을 한 프로세스에서 같이 돌리면 메모리 문제가 나기 쉬워서 분리 실행하는 게 원칙이다.

pre-extracted 입력은 이미 feature JSON이 있으므로 extraction이 필요 없다.

### 2. force anchor는 "사람이 확정한 정답"이다

retrieval과 propagation이 자동으로 잡아준 후보가 애매할 때,
IDA/Ghidra decompile을 보고 사람이 확정한 함수 매핑을 `force_anchor`로 등록한다.

그다음 propagation을 다시 돌리면 주변 함수들까지 더 잘 풀릴 수 있다.

### 3. downstream rerun은 retrieval를 다시 하지 않는다

`run_downstream`은 이 단계만 다시 돌린다.

- build_suite
- propagation
- deferred_analysis
- final_report

즉 seed anchor를 수정했을 때 빠르게 재실행하는 용도다.

## 어떤 tool부터 써야 하나

### A. 새 바이너리 처음 분석할 때 (게임 엔진 같은 혼합 바이너리 권장 순서)

```
1. extract_query_features     → Ghidra feature 추출
2. detect_lua_scope           → Lua VM 함수 스코프 탐지 (~800개)
                                  출력: lua_scope.json
3. bulk_query_retrieval       → --scope-json 적용 (16k → ~800개만 encoding)
                                  출력: retrieval_result.json
4. select_seed_anchors        → --scope-json + --dedup-max-per-ref 1
                                  출력: seed_anchors.json
5. build_runtime_suite        → propagation 입력 조립
6. run_downstream             → propagation Round 1
7. get_mapping_distribution   → 노이즈 명 탐지 (5개 이상이면 blacklist 후보)
8. update_noise_blacklist     → 노이즈 제거
   + run_downstream
9. export_trusted_mappings    → 1:1 신뢰 매핑 추출
10. (IDA에서 확인 후 rename)
11. batch_register_force_anchors → 확정 매핑 등록
```

### B. Round N 반복 (targeted retrieval 포함)

앵커가 쌓인 후 구조 기반 검색으로 추가 탐색:

```
1. patch_features_with_confirmed  → callee/caller에 실제 이름 반영
2. targeted_retrieval             → 확정 앵커 이웃 기반 검색 (embedding 불필요)
                                     출력: targeted_retrieval.json
3. select_seed_anchors            → --targeted-json 포함
4. run_downstream                 → propagation Round N
5. accepted 증가 없으면 수렴 → 종료
```

### C. 이미 feature가 있는 상태에서 분석할 때

```
1. read_final_report
2. list_deferred_cases
3. show_candidate_context (개별 케이스 상세)
4. IDA 확인 후 batch_register_force_anchors
```

### D. seed를 직접 수정한 뒤 재실행만 할 때

```
1. run_downstream
2. read_final_report
```

## tool을 고를 때 빠른 판단표

### "지금 뭘 하고 싶은지" 기준

- 바이너리에서 feature만 뽑고 싶다
  - `extract_query_features`

- retrieval 결과에서 자동 초기 anchor만 만들고 싶다
  - `select_seed_anchors`

- propagation 입력 JSON까지 만들고 싶다
  - `build_runtime_suite`

- deferred/conflict만 보고 싶다
  - `list_deferred_cases`

- 특정 case 하나를 자세히 보고 싶다
  - `read_mapping_record`

- 현재 accepted/deferred/conflict 수만 빨리 보고 싶다
  - `read_final_report`
  - 또는 `read_propagation_summary`

- 내가 확인한 정답을 anchor로 반영하고 싶다
  - 하나면 `register_force_anchor`
  - 여러 개면 `batch_register_force_anchors`

- anchor 수정 후 retrieval는 건드리지 않고 propagation만 다시 돌리고 싶다
  - `run_downstream`

## 가장 흔한 실수

### 1. MCP에서 10번 파이프라인 tool이 있을 거라고 기대하는 것

지금 MCP는 일부러 그런 tool을 노출하지 않는다.

이유:

- extraction과 analysis가 한 프로세스에 묶이기 쉽고
- Ghidra JVM과 embedding 모델 메모리가 겹칠 수 있다

그래서 MCP에서는:

- `extract_query_features`
- 결과 조회 / anchor 관리 / `run_downstream`

이 조합으로 analyst loop를 돌리는 쪽을 기준으로 본다.

### 2. deferred를 보지 않고 바로 force anchor를 넣는 것

score가 높아 보여도 오탐일 수 있다.

안전한 흐름:

1. `list_deferred_cases`
2. `read_mapping_record`
3. IDA/Ghidra 확인
4. 그다음 force anchor

### 3. anchor를 여러 개 넣으면서 `register_force_anchor`를 반복 호출하는 것

가능은 하지만 비효율적이다.

여러 개를 한 번에 정리했으면:

- `batch_register_force_anchors`

가 더 좋다.

## 실전 사고 흐름

이 MCP를 잘 쓰려면 "정답을 바로 맞히려는" 느낌보다,
"불확실한 영역을 점점 줄이는" 흐름으로 보는 게 좋다.

보통은 이렇게 생각하면 된다.

1. 자동 파이프라인으로 큰 지도를 깐다
2. deferred/conflict를 본다
3. 허브 함수나 라이브러리 open 함수부터 확정한다
4. force anchor를 넣는다
5. propagation을 다시 돌린다
6. 결과가 좋아졌는지 본다
7. 다시 다음 허브로 간다

## 추천 시작 세트

처음 MCP를 붙인 사람이 가장 먼저 익혀야 할 tool 조합:

1. `extract_query_features`
2. `bulk_query_retrieval`
3. `select_seed_anchors`
4. `build_runtime_suite`
5. `read_final_report`
6. `show_candidate_context`
7. `batch_register_force_anchors`
8. `run_downstream`

이 6개만 익혀도 analyst loop는 거의 돌릴 수 있다.

## 같이 보면 좋은 문서

- [../mcp/mcp_tool_reference.md](../mcp/mcp_tool_reference.md)
- [../mcp/mcp_runtime.md](../mcp/mcp_runtime.md)
- [../mcp/mcp_feature_review.md](../mcp/mcp_feature_review.md)
