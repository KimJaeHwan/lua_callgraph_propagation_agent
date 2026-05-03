# Local LLM Runtime Final Design

이 문서는 `lua_callgraph_propagation_agent`를 **LM Studio 기반 local LLM + Lua MCP + IDA MCP** 조합으로 바로 운영하기 위한 최종 설계 메모다.  
목표는 다음 세 가지다.

1. Lua 쪽 deterministic pipeline은 기존 스크립트/MCP를 그대로 사용한다.
2. local LLM은 판단과 triage만 맡는다.
3. LangGraph optional dependency가 없어도 동일한 노드/라우팅 정책으로 바로 실행할 수 있어야 한다.

---

## 1. 최종 구조

### 역할 분리

- **Lua MCP**
  - feature extraction
  - scope detection
  - retrieval
  - seed selection
  - propagation / deferred / final report
  - force anchor / noise blacklist / patched feature 관리

- **IDA MCP**
  - caller / callee / decompile / string evidence 수집
  - 선택적으로 rename 적용

- **Local LLM**
  - `show_candidate_context` + IDA evidence를 읽고
  - `VerificationDecision` JSON만 반환
  - speculative mapping 금지

### 핵심 원칙

- extraction과 analysis는 분리한다.
- accepted를 늘리는 것보다 잘못된 force anchor를 막는 것을 우선한다.
- Lua MCP tool을 직접 호출하는 loop를 표준 경로로 삼는다.
- local LLM은 free-form 설명 대신 JSON schema만 반환한다.

---

## 2. 실제 실행 경로

### 실행기

- [scripts/22_run_local_llm_agent.py](../../scripts/22_run_local_llm_agent.py)

이 스크립트는 두 모드를 지원한다.

- **deterministic fallback**
  - `--lmstudio-model` 없이 실행
  - score / graph / IDA evidence만으로 보수적으로 판단

- **LM Studio local LLM mode**
  - `--lmstudio-model <model-id>`
  - OpenAI-compatible `/v1/chat/completions` 사용

### MCP 연결 방식

- **Lua MCP**: in-process direct dispatch
  - [mcp_server.py](../../src/lua_callgraph_propagation_agent/mcp_server.py)의 tool 함수를 직접 호출
  - 별도 stdio 서버 프로세스 불필요

- **IDA MCP**: HTTP MCP client
  - `--ida-url http://127.0.0.1:13337/mcp`
  - [clients.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/clients.py)의 `CodexIdaMcpClient` 사용

즉 local LLM runner는:

- Lua 쪽은 repo 내부 함수로 즉시 연결
- IDA 쪽은 외부 MCP endpoint로 연결

구조로 고정된다.

---

## 3. 현재 기준 표준 워크플로우

### 초기 분석

1. `extract_query_features` 또는 pre-extracted `query_json`
2. `detect_lua_scope`
3. `bulk_query_retrieval`
4. `select_seed_anchors`
5. `build_runtime_suite`
6. `run_downstream`
7. `read_final_report` + `read_propagation_summary`

### 반복 루프

1. `get_mapping_distribution`
2. `update_noise_blacklist` 필요 시 적용
3. `export_trusted_mappings`
4. `list_deferred_cases`
5. `show_candidate_context`
6. IDA evidence 수집
7. local LLM `VerificationDecision`
8. `batch_register_force_anchors`
9. `patch_features_with_confirmed`
10. `targeted_retrieval`
11. `select_seed_anchors(targeted_json 포함)`
12. `run_downstream`

---

## 4. 이번 보완 사항

### LangGraph state / routing 보완

- `current_top_prediction`을 deferred 후보 선택에서 읽도록 수정
- `show_candidate_context`를 deferred 기본 입력으로 승격
- `case_id`의 `@entry_point` suffix에서 entry point 정규화 지원
- `read_propagation_summary`를 metrics 단계에 다시 연결
- `new_confirmed_count` 기준으로 fresh retrieval 분기 수정
- verification queue를 소모형으로 바꿔 다음 candidate로 넘어갈 수 있게 수정

관련 파일:

- [state.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/state.py)
- [reasoner.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/reasoner.py)
- [nodes.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/nodes.py)
- [graph.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/graph.py)

### local LLM 연결 보완

- [lmstudio.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/lmstudio.py)
  - LM Studio OpenAI-compatible adapter 추가
  - JSON object 강제

### IDA adapter 보완

- [clients.py](../../src/lua_callgraph_propagation_agent/langgraph_agent/clients.py)
  - `CodexIdaMcpClient` 추가
  - 현재 사용하는 IDA tool profile을 high-level evidence API로 매핑

---

## 5. 바로 실행 예시

### deterministic / no-IDA

```bash
cd lua_callgraph_propagation_agent

../lua_llm/bin/python scripts/22_run_local_llm_agent.py \
  --config data/runtime/results/libengine_lua536_aarch64_analysis/runtime_config.json \
  --no-ida
```

### LM Studio + IDA MCP

```bash
cd lua_callgraph_propagation_agent

../lua_llm/bin/python scripts/22_run_local_llm_agent.py \
  --config data/runtime/results/libengine_lua536_aarch64_analysis/runtime_config.json \
  --lmstudio-model qwen/qwen3.6-35b-a3b \
  --lmstudio-base-url http://127.0.0.1:1234/v1 \
  --ida-url http://127.0.0.1:13337/mcp
```

선택 옵션:

- `--allow-auto-rename`
- `--max-rounds 8`
- `--trusted-min-score 0.94`
- `--decompile-min-score 0.88`
- `--write-summary-json /tmp/langgraph_run_summary.json`

---

## 6. 왜 이 설계가 local LLM에 맞는가

- 도메인이 좁다.
- 입력 구조가 정형화되어 있다.
- deterministic tool 결과가 강하다.
- local LLM은 “최종 계산”이 아니라 “보수적 triage”만 맡는다.

즉 이 프로젝트는 범용 agent보다:

- **analyst copilot**
- **structured verifier**
- **force-anchor gatekeeper**

역할에 더 잘 맞는다.

---

## 7. 현재 남은 한계

### 1. containing-function normalization

mixed binary에서 `case_id` 주소가 실제 함수 시작이 아닌 경우가 있다.  
현재는 `@entry_point` suffix와 context bundle을 우선 사용하지만, 완전한 IDA-side containing-function lookup 단계는 아직 별도 노드로 분리되지 않았다.

### 2. IDA MCP tool profile 차이

`CodexIdaMcpClient`는 현재 프로젝트에서 쓰는 profile 기준으로 맞춰져 있다.  
다른 IDA MCP 서버를 쓰면 tool name / payload key mapping을 조정해야 한다.

### 3. per-round 다건 verification 최적화

지금은 verification queue를 한 건씩 처리하면서 안전하게 rerun하는 흐름이다.  
batch verification은 이후 최적화 포인트다.

---

## 8. 추천 운영 전략

### 초기엔

- `--no-ida` 또는 deterministic fallback으로 루프가 도는지 확인
- 그다음 IDA MCP 연결
- 마지막에 LM Studio 판단까지 붙인다

### 실전 분석에선

- `show_candidate_context` 기반 후보만 검토
- custom / crypto / game code 오염은 early reject
- 확실한 함수만 batch force anchor
- accepted 변화량이 작아지면 stop

---

## 9. 한 줄 결론

지금 설계는 **Lua deterministic pipeline + IDA evidence + LM Studio local LLM reviewer** 구조로 바로 투입 가능한 상태에 가깝다.  
LangGraph optional dependency가 없어도 같은 노드 정책으로 실행할 수 있고, 나중에 LangGraph 본체로 바꿔도 동일한 workflow를 유지할 수 있다.
