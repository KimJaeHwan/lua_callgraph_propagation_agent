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
reference_callgraph.sqlite
```

예상 입력 정보:

- Query function id.
- Retrieval candidate list.
- Candidate score breakdown.
- Query function caller/callee edges.
- SQLite edge-list로 저장된 vanilla reference caller/callee edges.
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

- Graph loader: query call graph JSON과 reference call graph SQLite를 읽고 node/edge index를 만든다.
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

Reference call graph는 embedding하지 않는다. Call graph는 의미 유사도 검색 대상이 아니라 `src -> dst` 관계를 정확히 조회하고 비교해야 하는 구조화 데이터이므로 SQLite edge-list로 관리한다. 자세한 저장 설계는 [callgraph_store_design.md](callgraph_store_design.md)를 따른다.

## 6. Propagation Strategy

1. Retrieval top-1과 top-k margin이 충분한 후보를 anchor로 잡는다.
2. Anchor의 caller/callee 주변 후보에 graph bonus를 부여한다.
3. Query edge가 reference edge와 동시에 맞으면 mutual evidence로 강화한다.
4. 이미 매핑된 이웃이 많은 후보는 confidence를 올린다.
5. 같은 reference function에 여러 query function이 몰리면 conflict로 표시한다.
6. Conflict가 큰 후보는 `deferred` 상태로 두고 후속 Agent 판단 대상으로 넘긴다.

## 7. Candidate Expansion Policy

Call graph는 retrieval 후보를 재정렬하는 데만 쓰지 않고, retrieval이 놓친 후보를 다시 후보군으로 끌어오는 데도 사용할 수 있다.

예를 들어 query 함수의 caller가 `sort`로 확인되고, vanilla reference graph에서 `sort -> luaL_checktype` edge가 존재한다면 `luaL_checktype`은 retrieval top-k에 없더라도 graph-expanded candidate가 될 수 있다.

정책은 다음과 같다.

- Retrieval top-k 후보는 항상 1차 후보군으로 유지한다.
- Query의 visible caller/callee anchor가 reference graph에 존재하면 anchor 주변의 reference 함수를 보조 후보로 추가한다.
- Callee anchor가 있는 경우 `candidate -> anchor` edge를 찾는다.
- Caller anchor가 있는 경우 `anchor -> candidate` edge를 찾는다.
- O0 edge는 primary evidence로 보고, O1/O2/O3/Os edge는 optimization-tolerant auxiliary evidence로 본다.
- Graph-expanded candidate는 identity proof가 아니라 candidate generation 결과로 취급한다.
- Anchor가 하나뿐이면 같은 caller/callee neighborhood 안의 여러 함수가 동점이 될 수 있으므로 LLM analyst 또는 추가 feature score가 필요할 수 있다.

현재 실험에서는 `luaL_checktype`이 retrieval 후보에 없던 cross-architecture case에서 graph expansion을 통해 후보군에는 복구되었지만, `sort` 하나의 caller anchor만으로는 `lua_type`, `lua_settop`, `luaL_len`, `luaL_checktype` 등을 완전히 구분하지 못했다.

따라서 Agent의 다음 방향은 다음처럼 잡는다.

```text
high-confidence anchor propagation
  -> graph-expanded candidate generation
  -> feature/retrieval score와 graph score 결합
  -> ambiguous neighborhood는 Local LLM analyst 또는 manual review로 deferred
```

## 8. Local LLM Analyst Layer

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

## 9. 초기 평가 기준

- Retrieval-only 대비 unique top-k/top-1 개선 여부.
- Retrieval 후보에 없던 정답이 graph expansion으로 후보군에 복구되는지 여부.
- `luaV_execute`, `luaL_checktype` 같은 hard negative case 개선 여부.
- Cross-architecture case에서 false positive 감소 여부.
- Mapping conflict 수 감소 여부.
- Deferred case가 근거와 함께 잘 남는지 여부.
- Local LLM analyst가 custom/core 구분 근거를 일관되게 생성하는지 여부.

## 10. 현재 상태

현재 입력 스키마와 최소 fixture를 고정했고, vanilla feature JSON에서 SQLite reference call graph를 생성하는 변환 스크립트를 구현했다.

Deterministic scorer는 retrieval 후보의 caller/callee edge consistency를 기준으로 보수적인 graph bonus를 부여한다. Expanded evaluation 40 cases 기준으로 retrieval top1 accuracy는 0.95, propagation top1 accuracy는 0.975였다.

추가로 candidate expansion 실험을 구현했다. Graph expansion을 켜면 expanded candidate recall은 0.975에서 1.0으로 올라갔고, top5 기준으로는 모든 case에서 정답 후보가 포함되었다. 다만 `luaL_checktype` cross-architecture case는 caller anchor가 하나뿐이라 top1까지는 회복하지 못했고, 이 케이스는 후보 생성 이후 feature score 또는 Local LLM analyst가 필요한 대표적인 deferred case로 본다.
