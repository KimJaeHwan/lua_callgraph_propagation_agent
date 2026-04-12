# Call Graph Propagation Agent 설계 초안

## 1. 목적

이 Agent의 목적은 `lua_function_embedding`의 retrieval top-k 결과를 call graph 문맥으로 보정해 최종 함수 매핑을 더 안정적으로 결정하는 것이다.

Retrieval은 단일 함수 feature 기반 후보 생성기로 사용한다. 최종 판단은 caller/callee 일관성, 주변 함수의 high-confidence mapping, graph conflict 여부를 함께 본다.

이 Agent는 취약점 자동 판정기가 아니다. 목표는 리버싱 기반 logical vulnerability 분석에서 기능 함수 식별과 분석 우선순위화를 보조하는 것이다. Local LLM은 필요한 경우 feature와 코드 문맥을 요약하는 analyst layer로만 사용하고, 최종 확정은 retrieval score, graph score, conflict policy를 함께 고려한다.

## 2. 입력

초기 입력은 세 가지로 잡는다.

```text
retrieval_topk.json
query_callgraph.json
reference_callgraph.json
```

예상 입력 정보:

- Query function id.
- Retrieval candidate list.
- Candidate score breakdown.
- Query function caller/callee edges.
- Reference function caller/callee edges.
- 이미 확정된 high-confidence anchor mapping.
- 선택 입력: decompiled code, assembly, pcode snippet.

## 3. 출력

Agent 출력은 최종 mapping과 판단 근거를 함께 저장한다.

```json
{
  "query_function_id": "query::00123456",
  "predicted_function_name": "luaV_execute",
  "confidence": 0.87,
  "status": "accepted",
  "evidence": [
    "retrieval_top3_candidate",
    "callee_consistency:luaD_precall",
    "caller_consistency:f_parser"
  ],
  "conflicts": []
}
```

## 4. Core Components

- Graph loader: call graph JSON을 읽고 node/edge index를 만든다.
- Candidate store: retrieval top-k 후보와 초기 점수를 보관한다.
- Anchor selector: high-confidence mapping을 anchor로 선택한다.
- Propagation engine: anchor 주변의 caller/callee evidence를 전파한다.
- Ref-backpropagation engine: 이미 매핑된 이웃에서 ambiguous node로 증거를 역전파한다.
- Conflict resolver: one-to-one 충돌, family-level 충돌, graph inconsistency를 감지한다.
- Local LLM analyst: ambiguous/custom-suspected 함수에 대해 feature와 코드 문맥을 요약하고 custom 여부 판단 근거를 제안한다.
- Mapping exporter: 최종 mapping과 intermediate trace를 JSON으로 저장한다.

## 5. Scoring Sketch

초기 아이디어:

```text
final_score =
  retrieval_prior
  + caller_consistency_bonus
  + callee_consistency_bonus
  + mutual_edge_bonus
  - graph_conflict_penalty
```

중요한 점은 graph score가 retrieval score를 무조건 덮어쓰지 않는 것이다. Graph evidence는 애매한 후보를 재정렬하고 conflict를 감지하는 보정 신호로 시작한다.

`retrieval_prior`는 `lua_function_embedding`에서 계산된 score를 그대로 사용한다. 이 Agent는 retrieval scoring 자체를 다시 정의하지 않는다.

Graph score는 최적화에 의한 inline 변화와 커스텀 function call 추가 가능성을 고려해 conservative하게 적용한다. Missing edge는 강한 penalty로 바로 처리하지 않고, reference graph에 없는 extra edge도 즉시 reject하지 않는다. Extra edge는 `custom_suspected` signal 또는 Local LLM analyst layer의 입력 근거로 사용할 수 있다.

## 6. Propagation Strategy

1. Retrieval top-1과 top-k margin이 충분한 후보를 anchor로 잡는다.
2. Anchor의 caller/callee 주변 후보에 graph bonus를 부여한다.
3. Query edge가 reference edge와 동시에 맞으면 mutual evidence로 강화한다.
4. 이미 매핑된 이웃이 많은 후보는 confidence를 올린다.
5. 같은 reference function에 여러 query function이 몰리면 conflict로 표시한다.
6. Conflict가 큰 후보는 `deferred` 상태로 두고 후속 Agent 판단 대상으로 넘긴다.

## 7. Local LLM Analyst Layer

Local LLM은 모든 함수에 호출하지 않는다. Retrieval과 graph evidence만으로 충분히 판단 가능한 함수는 deterministic rule로 처리하고, 애매한 함수만 LLM analyst layer로 넘긴다.

LLM 호출 대상:

- Retrieval top-k score gap이 작아 후보가 애매한 함수.
- Retrieval confidence는 낮지만 call graph상 중심성이 높은 함수.
- Lua core 함수인지 custom binding/application logic인지 판단이 필요한 함수.
- 같은 후보에 여러 query function이 몰리는 one-to-one conflict case.
- Graph evidence가 부족해 `deferred` 상태가 된 함수.

LLM 입력 예시:

```text
Task:
Determine whether query function sub_401230 is likely a Lua core function,
stdlib helper, or custom application/binding logic.

Query features:
- strings: ["permission denied", "user_id", "policy"]
- callees: ["luaL_checklstring", "lua_pushboolean", "custom_auth_check"]
- pcode pattern: CALL-heavy, moderate branches

Retrieval candidates:
1. luaB_pcall score=0.62
2. luaL_argerror score=0.59
3. luaL_checktype score=0.57

Graph evidence:
- called by sub_400900, mapped to custom_request_handler
- calls custom_auth_check, not present in vanilla Lua reference graph
- weak match to Lua core callgraph neighborhood
```

LLM 출력 예시:

```json
{
  "classification": "custom_application_logic",
  "confidence": 0.78,
  "reasoning_summary": [
    "Function calls Lua C API style helpers but also calls custom_auth_check.",
    "Strings user_id and policy suggest application-level authorization logic.",
    "Retrieval candidates are weak and generic Lua API helpers.",
    "Callgraph neighborhood does not match vanilla Lua core functions."
  ],
  "recommended_action": "Prioritize manual review as possible authorization policy logic."
}
```

중요한 정책:

- LLM 출력은 `llm_evidence`로 저장하고 final mapping을 즉시 덮어쓰지 않는다.
- Symbol name이나 known label이 남아 있으면 label leakage를 방지한다.
- LLM이 확신하더라도 graph conflict가 크면 `accepted`가 아니라 `deferred`로 둔다.
- LLM은 취약점 존재를 판정하지 않고, 분석해야 할 함수 후보와 근거를 정리한다.

## 8. 초기 평가 기준

- Retrieval-only 대비 unique top-k/top-1 개선 여부.
- `luaV_execute`, `luaL_checktype` 같은 hard negative case 개선 여부.
- Cross-architecture case에서 false positive 감소 여부.
- Mapping conflict 수 감소 여부.
- Deferred case가 근거와 함께 잘 남는지 여부.
- Local LLM analyst가 custom/core 구분 근거를 일관되게 생성하는지 여부.

## 9. 현재 상태

이 문서는 프로젝트 scaffold용 설계 초안이다. 아직 propagation engine 구현 전이며, 다음 단계에서 입력 스키마와 최소 fixture를 먼저 고정한다.
