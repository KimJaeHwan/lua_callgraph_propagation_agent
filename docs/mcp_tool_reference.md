# MCP Tool Reference

`lua_callgraph_propagation_agent` MCP tool reference.

이 문서는 각 tool이 "무엇을 하는지", "언제 써야 하는지", "입력은 무엇인지", "주의할 점은 무엇인지"를 빠르게 확인하기 위한 레퍼런스다.

## 실행 계열

중요:

- MCP는 `scripts/10_run_name_mapping_pipeline.py`를 감싸는 tool을 노출하지 않는다.
- binary target에서 extraction과 analysis를 하나의 프로세스로 묶지 않기 위한 의도적 제한이다.

### `extract_query_features`

용도:

- config 없이 직접 바이너리를 넣어 feature 추출

언제 쓰나:

- 빠른 실험
- 단일 바이너리 extraction만 필요할 때

입력:

- `binary`
- `lua_version`
- `architecture`
- `session_name`
- `opt_level`
- `strip_mode`

### `bulk_query_retrieval`

용도:

- query feature set 전체에 대해 top-k retrieval 생성

언제 쓰나:

- extraction 이후 retrieval만 따로 돌리고 싶을 때
- index 바꿔가며 실험할 때

입력:

- `index`
- `output_json`
- `extract_manifest` 또는 `query_json`
- `candidate_pool`
- `topk`
- `scoring_mode`

주의:

- `extract_manifest`와 `query_json` 중 하나는 필요

## 결과 조회 계열

### `read_final_report`

용도:

- accepted / deferred / conflict 수와 preview 확인

언제 쓰나:

- 분석 끝난 직후
- 재전파 이후 결과 비교할 때

입력:

- `report_json`

### `read_mapping_record`

용도:

- `case_id` 하나를 깊게 보기

언제 쓰나:

- 특정 deferred/conflict 케이스를 조사할 때
- 왜 accepted됐는지 역검증할 때

입력:

- `report_json`
- `case_id`

참고:

- `case_id` 형식은 보통 `<function>@<address>`

### `read_propagation_summary`

용도:

- propagation 결과의 빠른 중간 요약 보기

언제 쓰나:

- final report까지 안 열고도 현재 상태를 빨리 보고 싶을 때

입력:

- `config_path`

반환:

- summary
- round log
- deferred 목록
- conflict 목록

### `list_deferred_cases`

용도:

- triage용 deferred/conflict 리스트 보기

언제 쓰나:

- IDA에서 뭘 먼저 볼지 정할 때

입력:

- `report_json`

반환:

- `case_id`
- `query_func`
- `predicted_function_name`
- `status_reasons`

## Anchor 관리 계열

### `register_force_anchor`

용도:

- 한 개의 manually confirmed mapping을 force anchor로 등록
- 그 후 downstream 재실행

언제 쓰나:

- IDA/Ghidra에서 함수 하나를 확실히 판별했을 때

입력:

- `config_path`
- `query_func`
- `reference_func`
- `reason`

주의:

- 이미 등록된 `query_func`는 중복 방지로 에러 반환

### `batch_register_force_anchors`

용도:

- 여러 force anchor를 한 번에 등록
- downstream을 한 번만 재실행

언제 쓰나:

- 한 IDA 분석 세션에서 여러 개를 한 번에 확정했을 때

입력:

- `config_path`
- `anchors`

`anchors` 형식:

```json
[
  {
    "query_func": "FUN_005c8394",
    "reference_func": "luaopen_package",
    "reason": "confirmed from package/searchers/_LOADED registration flow"
  }
]
```

### `remove_force_anchor`

용도:

- 잘못 넣은 `force_anchor` 제거

언제 쓰나:

- analyst가 나중에 판단을 번복했을 때
- 실험 anchor를 되돌리고 싶을 때

입력:

- `config_path`
- `query_func`
- `rerun_downstream`

주의:

- `source=\"force_anchor\"`만 제거
- 자동 seed는 지우지 않음

### `run_downstream`

용도:

- retrieval / seed selection은 건드리지 않고
- build_suite → propagation → deferred_analysis → final_report만 다시 실행

언제 쓰나:

- seed anchor 수정 후 빠르게 재검증할 때

입력:

- `config_path`

## Analyst 지원 계열

### `show_candidate_context`

용도:

- 한 케이스의 문맥을 한 번에 보기

포함:

- final mapping record
- deferred/conflict triage payload
- 현재 등록된 seed/force anchor
- query feature 요약

언제 쓰나:

- force anchor를 넣기 전 마지막 확인
- 한 케이스를 여러 파일 열지 않고 한 번에 보고 싶을 때

입력:

- `config_path`
- `case_id`

## 추천 사용 순서

### 가장 기본 루프

1. `extract_query_features` 또는 기존 결과 준비
2. `read_final_report`
3. `list_deferred_cases`
4. `read_mapping_record`
5. `show_candidate_context`
6. IDA/Ghidra 확인
7. `batch_register_force_anchors`
8. `read_final_report`

### 수정/되돌리기 루프

1. `remove_force_anchor`
2. `run_downstream`
3. `read_final_report`

## 같이 보면 좋은 문서

- [mcp_quickstart_guide.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_quickstart_guide.md)
- [mcp_runtime.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_runtime.md)
- [mcp_feature_review.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_feature_review.md)
