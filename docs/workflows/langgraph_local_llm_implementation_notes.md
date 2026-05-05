# LangGraph + Local LLM Implementation Notes

이 문서는 `langgraph_local_llm_ida_automation.mmd`와 `langgraph_agent_object_design.mmd`를 실제 구현으로 옮길 때 필요한 호출 규칙, 상태 갱신 규칙, Local LLM 판단 규칙을 정리한다.

## 1. 설계 목표

- Lua MCP와 IDA MCP를 LangGraph 노드로 분리한다.
- Local LLM은 추론과 판단만 담당하고, 파일 변경과 분석 실행은 MCP tool을 통해서만 수행한다.
- 틀린 force anchor가 전체 propagation을 오염시키지 않도록 보수적으로 동작한다.
- 실패한 tool call, 반복되는 noise, accepted 증가 없음 같은 상황에서 무한 루프를 방지한다.

## 2. 핵심 상태

`AgentState`는 최소한 아래 값을 유지해야 한다.

| 필드 | 목적 |
|---|---|
| `config_path` | runtime config 위치 |
| `paths` | `query_json`, `seed_anchor_json`, `suite_json`, `final_report_json` 등 resolved path |
| `graph_config` | LangGraph 실행 정책 |
| `round_index` | downstream 반복 횟수 |
| `last_accepted`, `current_accepted`, `delta_accepted` | 수렴 판단 |
| `convergence_count` | accepted 증가가 부족한 연속 횟수 |
| `confirmed_map` | `entry_point_hex -> reference_func` |
| `pending_trusted` | IDA 검증 후보 trusted mappings |
| `pending_deferred` | IDA 검증 후보 deferred cases |
| `noise_blacklist` | 현재 blacklist |
| `tool_failures` | tool 실패 누적 |

## 3. GraphConfig 권장값

현재 구현에서는 seed / targeted / deferred / rename 관련 임계값을 `GraphConfig` 한군데에서 관리한다.  
실행 시에는 runtime config의 `graph_config` 블록으로 override 할 수 있고, CLI는 실행 제어용 값만 덮어쓴다.

| 필드 | 권장값 | 설명 |
|---|---:|---|
| `max_rounds` | 20 | 전체 자동화 최대 반복 |
| `convergence_patience` | 3 | accepted 증가 부족 허용 횟수 |
| `min_delta_accepted` | 5 | 이보다 적게 늘면 수렴 후보 |
| `suspicious_threshold` | 5 | many-to-one 의심 기준 |
| `auto_blacklist_threshold` | 10 | 자동 blacklist 추가 기준 |
| `seed_min_top1_score` | 0.92 | 일반 retrieval seed 최소 점수 |
| `seed_min_margin` | 0.05 | 일반 retrieval seed 최소 margin |
| `seed_dedup_max_per_ref` | 1 | 같은 reference 이름에 허용할 최대 query 수 |
| `targeted_min_score` | 0.74 | targeted retrieval anchor 최소 점수 |
| `targeted_min_margin` | 0.15 | targeted retrieval anchor 최소 margin |
| `trusted_min_score` | 0.92 | trusted 자동 검증 queue 최소 점수 |
| `decompile_min_score` | 0.85 | deferred decompile 검토 최소 점수 |
| `deferred_min_score_relaxation` | 0.05 | deferred 검토 시 score 완화 폭 |
| `deferred_no_graph_min_score` | 0.90 | graph 근거가 약할 때 필요한 최소 점수 |
| `max_ida_cases_per_round` | 10 | round당 IDA 검토 상한 |
| `fresh_retrieval_anchor_delta` | 20 | 새 anchor가 이 이상이면 fresh retrieval 고려 |
| `allow_auto_rename` | false 기본 | 보고/검증 모드에서는 rename 비활성 권장 |
| `allow_fresh_retrieval` | true | patched feature 기반 embedding retrieval 허용 |
| `rename_min_score` | 0.92 | 일반 auto rename 최소 점수 |
| `rename_relaxed_min_score` | 0.89~0.90 | 안전 prefix 함수용 완화 rename 점수 |
| `enable_ida_type_injection` | true | IDA evidence 수집 전 Lua 타입 팩 자동 주입 |
| `ida_type_injection_mode` | `vanilla_headers` | 버전별 바닐라 Lua 원본 헤더를 읽어 IDA용 선언으로 주입 |
| `safe_auto_rename_prefixes` | `luaD_`, `luaZ_`, `luaV_finish`, `luaopen_` | 완화 rename 허용 prefix |

## 4. 정확한 호출 순서

### 4.1 초기 binary mode

1. `extract_query_features`
2. `detect_lua_scope`
3. `bulk_query_retrieval` with `scope_json`
4. `select_seed_anchors` with `scope_json`
5. `build_runtime_suite`
6. `run_downstream`
7. `update_metrics`

### 4.2 pre-extracted mode

1. `detect_lua_scope`
2. `bulk_query_retrieval` with `query_json` and `scope_json`
3. `select_seed_anchors`
4. `build_runtime_suite`
5. `run_downstream`
6. `update_metrics`

### 4.3 반복 round

1. `get_mapping_distribution`
2. 새 blacklist 후보가 있으면 `update_noise_blacklist` 후 `run_downstream`
3. 없으면 `export_trusted_mappings`
4. trusted가 없으면 deferred case 선별
5. IDA evidence 수집
6. Local LLM structured verification
7. 확정된 것만 `batch_register_force_anchors`
8. `patch_features_with_confirmed`
9. 필요하면 patched feature로 `bulk_query_retrieval`
10. `targeted_retrieval`
11. `select_seed_anchors` with `targeted_json`
12. `run_downstream`
13. `update_metrics`

## 5. `confirmed_map` 생성 규칙

`patch_features_with_confirmed`는 `entry_point_hex -> real_name` 형태를 요구한다.  
반면 `batch_register_force_anchors`는 `query_func -> reference_func` 형태를 요구한다.

따라서 `build_confirmed_map` 노드는 반드시 필요하다.

```text
VerificationDecision(query_func, reference_func)
  -> query_json에서 query_func 검색
  -> entry_point 추출
  -> entry_point hex normalize
  -> confirmed_map[entry_point_hex] = reference_func
```

주의:

- `entry_point`가 없으면 `confirmed_map`에 넣지 않는다.
- `batch_register_force_anchors`에는 넣을 수 있지만 feature patch 효과는 없다.
- 이미 다른 이름으로 확인된 entry_point면 충돌로 처리한다.

## 6. Local LLM 판단 출력 schema

Local LLM은 free-form 설명만 반환하면 안 된다. 아래 JSON 형태를 강제한다.

```json
{
  "case_id": "FUN_...@...",
  "query_func": "FUN_...",
  "entry_point": "4a7141",
  "candidate_name": "luaD_precall",
  "confidence": 0.94,
  "accepted": true,
  "rename_in_ida": false,
  "reason": "caller/callee anchors match luaD_precall and decompile shows Lua call preparation",
  "evidence": [
    "callee anchor luaD_callnoyield",
    "uses lua_State-like first argument",
    "top candidate margin is high"
  ],
  "contradictions": []
}
```

`accepted=true` 조건:

- candidate 의미와 decompile 내용이 모순되지 않는다.
- caller/callee 또는 문자열/상수 evidence가 최소 2개 이상 있다.
- candidate가 noise blacklist에 없다.
- many-to-one suspicious name이 아니다.

## 7. IDA rename 정책

기본값은 `allow_auto_rename=false`가 안전하다.

자동 rename을 허용하려면 모두 만족해야 한다.

1. `VerificationDecision.accepted == true`
2. `VerificationDecision.confidence >= 0.92`
3. `mapping_count == 1`
4. candidate가 blacklist에 없음
5. IDA의 현재 이름이 `FUN_`, `sub_` 계열이거나 명시적 overwrite 허용 상태
6. decompile/caller/callee evidence 중 하나 이상 확보

## 8. Noise loop guard

`update_noise` 노드는 새로 추가할 blacklist가 있을 때만 `run_downstream`으로 간다.

- `actually_added == []`이면 `export_trusted`로 라우팅한다.
- 같은 suspicious set이 2회 반복되면 noise loop를 종료한다.
- round당 noise update는 1회만 수행한다.

## 9. Fresh retrieval 분기

`patch_features_with_confirmed` 이후에는 두 경로가 있다.

### Fast loop

- `targeted_retrieval`만 수행
- embedding 불필요
- 반복 속도 빠름

### Precision loop

- patched feature로 `bulk_query_retrieval` 재실행
- 새 confirmed anchor가 충분히 많거나 기존 retrieval이 stale할 때 수행
- `sentence-transformers` 설치와 시간이 필요함

권장 분기:

```text
if allow_fresh_retrieval and new_confirmed_count >= fresh_retrieval_anchor_delta:
    fresh_bulk_retrieval
else:
    targeted_retrieval
```

## 10. Tool failure 정책

모든 MCP call은 `ToolResult`로 감싼다.

- `ok=false`이고 retryable이면 최대 1~2회 retry
- IDA MCP 실패 시 해당 candidate는 skip하고 다음 candidate로 이동
- Lua MCP 핵심 단계 실패 시 graph를 중단하고 `final_summary`에 실패 원인 기록
- 같은 tool이 연속 3회 실패하면 종료

## 11. 수렴 조건

다음 중 하나면 종료한다.

- `round_index >= max_rounds`
- `convergence_count >= convergence_patience`
- trusted queue와 deferred queue가 모두 비어 있음
- 새 blacklist도 없고 새 force anchor도 없음
- 핵심 MCP tool 반복 실패

## 12. 평가

이 고도화 버전은 초기 다이어그램보다 실구현 가능성이 높다.

- `confirmed_map` 변환 문제를 별도 노드로 분리했다.
- `update_metrics`가 수렴 판단을 명확히 담당한다.
- noise loop 무한 반복 방지 조건이 생겼다.
- IDA 검증을 evidence 수집, LLM 판단, rename 적용, force anchor 등록으로 분리했다.
- fast targeted loop와 precision fresh retrieval loop를 모두 지원한다.
- Local LLM 출력이 structured schema로 고정되어 자동화 안정성이 높다.

남은 구현 리스크:

- IDA MCP tool 이름과 실제 인자명은 사용하는 플러그인에 맞춰 adapter가 필요하다.
- Local LLM의 decompile 해석 품질은 모델 크기와 prompt 품질에 의존한다.
- fresh retrieval은 sentence-transformers와 index 상태에 따라 비용이 크다.

## 13. 현재 코드 반영 상태

현재 구현 기준으로는 아래 보완이 이미 들어가 있다.

- deferred triage는 `show_candidate_context`를 기본 컨텍스트 번들로 사용한다.
- legacy runtime config에서는 `analysis/extraction.lua_version`이 없어도 `paths.target_lua_version`을 읽어 `build_suite` / `targeted_retrieval`에 전달한다.
- `graph_config`는 runtime config에서 직접 관리 가능하며, threshold 실험은 이 블록만 수정하는 것을 권장한다.
- `current_top_prediction` 및 `score_margin_top1_top2`를 Local LLM 입력에 반영한다.
- `case_id`의 `@entry_point` suffix에서 entry point를 정규화한다.
- `read_propagation_summary`를 metrics 단계에 다시 연결한다.
- fresh retrieval 분기는 전체 confirmed 수가 아니라 `new_confirmed_count`로 판단한다.
- LangGraph optional dependency가 없어도 동일한 노드 정책으로 실행 가능한 runner가 있다.
  - [scripts/22_run_local_llm_agent.py](../../scripts/22_run_local_llm_agent.py)
- `manual_force_anchors.json` 이 더 최신이면 runner는 자동으로 `build_suite` 부터 resume 하며,
  이 경로에서 manual force anchor seed 반영과 IDA rename/type 반영을 함께 처리한다.

LM Studio를 실제로 붙일 때는 아래 조합을 표준으로 본다.

- Lua MCP: in-process direct dispatch
- IDA MCP: streamable HTTP (`http://127.0.0.1:13337/mcp`)
- Local LLM: LM Studio OpenAI-compatible `/v1/chat/completions`
