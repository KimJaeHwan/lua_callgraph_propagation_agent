# Call Graph Propagation Agent 설계 초안

## 1. 목적

이 Agent의 목적은 `lua_function_embedding`의 retrieval top-k 결과를 call graph 문맥으로 보정해 최종 함수 매핑을 더 안정적으로 결정하는 것이다.

Retrieval은 단일 함수 feature 기반 후보 생성기로 사용한다. 최종 판단은 caller/callee 일관성, 주변 함수의 high-confidence mapping, graph conflict 여부를 함께 본다.

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

## 6. Propagation Strategy

1. Retrieval top-1과 top-k margin이 충분한 후보를 anchor로 잡는다.
2. Anchor의 caller/callee 주변 후보에 graph bonus를 부여한다.
3. Query edge가 reference edge와 동시에 맞으면 mutual evidence로 강화한다.
4. 이미 매핑된 이웃이 많은 후보는 confidence를 올린다.
5. 같은 reference function에 여러 query function이 몰리면 conflict로 표시한다.
6. Conflict가 큰 후보는 `deferred` 상태로 두고 후속 Agent 판단 대상으로 넘긴다.

## 7. 초기 평가 기준

- Retrieval-only 대비 unique top-k/top-1 개선 여부.
- `luaV_execute`, `luaL_checktype` 같은 hard negative case 개선 여부.
- Cross-architecture case에서 false positive 감소 여부.
- Mapping conflict 수 감소 여부.
- Deferred case가 근거와 함께 잘 남는지 여부.

## 8. 현재 상태

이 문서는 프로젝트 scaffold용 설계 초안이다. 아직 propagation engine 구현 전이며, 다음 단계에서 입력 스키마와 최소 fixture를 먼저 고정한다.
