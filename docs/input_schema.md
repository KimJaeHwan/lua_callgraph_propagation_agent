# Input Schema

이 문서는 `lua_callgraph_propagation_agent`가 읽고 쓰는 JSON 입력/출력 스키마를 고정하기 위한 문서다.

현재 목표는 구현 전 단계에서 데이터 계약을 명확히 하는 것이다. 이후 `graph_loader.py`, `candidate_store.py`, `propagation_engine.py`, `llm_analyst.py`는 이 문서를 기준으로 구현한다.

주의: 이 문서의 JSON graph는 작은 fixture와 query 입력 교환 포맷을 위한 것이다. 대량 vanilla reference call graph는 SQLite edge-list로 저장한다. 저장 설계는 [callgraph_store_design.md](callgraph_store_design.md)를 따른다.

## 1. 공통 원칙

### 1.1 Schema Version

모든 JSON에는 `schema_version`을 둔다.

```json
{
  "schema_version": "0.1"
}
```

초기 개발 중에는 `0.1`을 사용한다.

### 1.2 Function ID

Function ID는 간단한 형태로 유지한다.

```text
query::<entry_point_or_function_name>
ref::<lua_version>::<function_name>
```

예:

```text
query::00119970
query::sub_401230
ref::Lua_547::luaV_execute
ref::Lua_547::luaD_precall
```

`binary_id`, `architecture`, `source_json` 같은 정보는 function id에 넣지 않고 `metadata`에 둔다.

예:

```json
{
  "function_id": "query::00119970",
  "function_name": "sub_401230",
  "metadata": {
    "architecture": "x86_64",
    "source_json": "data/eval/tmp/masked_x86_query.json"
  }
}
```

### 1.3 Edge Type

초기 버전에서는 `calls` edge만 사용한다.

```json
{
  "src": "query::00119970",
  "dst": "query::0011A010",
  "edge_type": "calls"
}
```

나중에 필요하면 `references`, `indirect_calls`, `data_refs` 같은 edge type을 추가한다.

### 1.4 Score Range

`score_total`과 `score_breakdown`은 이전 단계인 `lua_function_embedding`에서 계산된 retrieval score를 그대로 전달받는다. 이 프로젝트는 retrieval scoring 방식을 다시 정의하지 않는다.

`lua_callgraph_propagation_agent`는 해당 retrieval score를 `retrieval_prior`로 사용한다. Propagation 단계에서 새로 계산하는 값은 call graph 기반 evidence score이며, 이는 기존 retrieval score를 대체하는 것이 아니라 보정하는 역할을 한다.

외부에 노출되는 score와 confidence는 기본적으로 `0.0 ~ 1.0` 범위로 둔다.

예외적으로 penalty를 포함한 internal score가 음수가 될 수 있으면, 최종 출력 전 normalize하거나 별도 필드에 저장한다.

### 1.5 Scoring Policy

Propagation 단계에서 scoring이 추가되는 이유는 call graph가 함수 역할 판단에 중요한 문맥 정보를 제공하기 때문이다.

그러나 call graph는 완전한 정답 신호가 아니다. 최적화에 의한 inline, dead code 제거, indirect call 처리 차이, 커스텀 function call 추가 때문에 query graph와 reference graph는 완전히 일치하지 않을 수 있다.

따라서 scoring 정책은 다음을 따른다.

- `lua_function_embedding`의 retrieval score는 변경하지 않고 `retrieval_prior`로 사용한다.
- `graph_score`는 caller/callee anchor 일치, reference neighborhood 일치, mutual edge evidence를 반영하는 보정 점수다.
- `graph_score`는 애매한 retrieval 후보를 재랭킹하거나 `accepted`, `deferred`, `conflict` 상태를 정하는 데 사용한다.
- 최적화로 인한 missing edge는 강한 penalty로 바로 처리하지 않는다.
- Reference graph에 없는 extra edge가 있다고 후보를 즉시 reject하지 않는다.
- Reference graph에 없는 extra edge는 `custom_suspected` 또는 Local LLM analyst 대상 signal로 저장할 수 있다.
- 명확한 one-to-one 충돌이나 graph inconsistency가 있을 때만 제한적으로 penalty를 적용한다.

예시:

```text
query::00119970 후보:
  llex          retrieval_prior = 0.711
  luaV_execute  retrieval_prior = 0.709

query graph evidence:
  query::00119970 -> query::0011A010
  query::0011A010 -> ref::Lua_547::luaD_precall 로 이미 매핑됨

reference graph evidence:
  ref::Lua_547::luaV_execute -> ref::Lua_547::luaD_precall
```

이 경우 retrieval 점수만 보면 `llex`가 근소하게 앞서지만, graph evidence는 `luaV_execute` 쪽을 지지한다. Propagation 단계에서는 `luaV_execute`에 graph bonus를 부여해 최종 후보를 재랭킹할 수 있다.

Custom call이 추가된 경우는 다음처럼 처리한다.

```text
query candidate:
  matched_core_callees = ["luaD_precall", "luaT_trybinTM"]
  unknown_extra_callees = ["custom_auth_check"]

policy:
  keep core candidate
  add positive evidence for matched_core_callees
  mark custom_suspected for unknown_extra_callees
  optionally send to Local LLM analyst layer
```

즉, 이 프로젝트의 scoring 철학은 다음과 같다.

```text
final_score = retrieval_prior + graph_bonus - graph_penalty
```

단, `graph_bonus`와 `graph_penalty`는 conservative하게 적용한다. Graph evidence는 retrieval 결과를 무조건 덮어쓰는 정답이 아니라, 리버싱 분석자가 확인해야 할 후보를 더 잘 정렬하기 위한 보조 신호다.

### 1.6 Retrieval 후보 기본 정책

`retrieval_topk.json`의 `candidates`는 기본적으로 function name 기준 unique top-k를 사용한다.

이 프로젝트의 embedding 데이터는 동일 함수가 여러 binary/optimization variant에서 반복될 가능성이 높다. 따라서 raw top-k를 그대로 쓰면 같은 함수명이 후보 목록을 과점할 수 있다.

정책:

- `candidates`: unique function-name top-k.
- `raw_candidates`: optional debug field.
- propagation baseline은 `candidates`만으로 동작해야 한다.

### 1.7 LLM Context

LLM 입력은 optional이다.

기본 propagation/evaluation은 LLM 없이 동작해야 한다. Local LLM은 아래와 같은 case에서만 analyst layer로 사용한다.

- `status = deferred`
- `conflict_count > 0`
- `top1_margin < threshold`
- `custom_suspected = true`

LLM 출력은 final mapping을 바로 덮어쓰지 않고 `llm_evidence`로 저장한다.

## 2. `retrieval_topk.json`

`lua_function_embedding`에서 생성한 retrieval 결과를 Agent 입력용으로 정리한 파일이다.

예시:

```json
{
  "schema_version": "0.1",
  "source": "lua_function_embedding",
  "retrieval_mode": "jaccard",
  "candidate_policy": "unique_function_name_topk",
  "queries": [
    {
      "query_function_id": "query::00119970",
      "query_function_name": "sub_401230",
      "metadata": {
        "architecture": "x86_64",
        "source_json": "data/eval/tmp/masked_x86_query.json"
      },
      "candidates": [
        {
          "candidate_function_id": "ref::Lua_547::llex",
          "candidate_function_name": "llex",
          "rank": 1,
          "score_total": 0.711,
          "score_breakdown": {
            "symbolic": 0.28,
            "numeric": 0.75,
            "semantic": 0.8
          }
        },
        {
          "candidate_function_id": "ref::Lua_547::luaV_execute",
          "candidate_function_name": "luaV_execute",
          "rank": 2,
          "score_total": 0.709,
          "score_breakdown": {
            "symbolic": 0.31,
            "numeric": 0.72,
            "semantic": 0.81
          }
        }
      ],
      "raw_candidates": []
    }
  ]
}
```

필수 필드:

- `schema_version`
- `source`
- `candidate_policy`
- `queries`
- `query_function_id`
- `candidates`
- `candidate_function_id`
- `candidate_function_name`
- `rank`
- `score_total`

선택 필드:

- `retrieval_mode`
- `query_function_name`
- `metadata`
- `score_breakdown`
- `raw_candidates`

## 3. `query_callgraph.json`

분석 대상 바이너리의 call graph다.

예시:

```json
{
  "schema_version": "0.1",
  "graph_id": "query_x86_64_masked",
  "graph_type": "query",
  "nodes": [
    {
      "function_id": "query::00119970",
      "function_name": "sub_401230",
      "entry_point": "00119970",
      "metadata": {
        "architecture": "x86_64",
        "source_json": "data/eval/tmp/masked_x86_query.json"
      }
    },
    {
      "function_id": "query::0011A010",
      "function_name": "sub_405000",
      "entry_point": "0011A010",
      "metadata": {
        "architecture": "x86_64"
      }
    }
  ],
  "edges": [
    {
      "src": "query::00119970",
      "dst": "query::0011A010",
      "edge_type": "calls"
    }
  ]
}
```

필수 필드:

- `schema_version`
- `graph_id`
- `graph_type`
- `nodes`
- `edges`
- `function_id`
- `src`
- `dst`
- `edge_type`

선택 필드:

- `function_name`
- `entry_point`
- `metadata`

## 4. `reference_callgraph.json`

기준 Lua 함수 집합의 call graph다.

이 포맷은 fixture와 디버깅용으로 유지한다. 실제 propagation에서 반복 조회할 vanilla reference graph는 `reference_callgraph.sqlite`로 변환해 사용한다.

예시:

```json
{
  "schema_version": "0.1",
  "graph_id": "lua_547_reference",
  "graph_type": "reference",
  "nodes": [
    {
      "function_id": "ref::Lua_547::luaV_execute",
      "function_name": "luaV_execute",
      "metadata": {
        "lua_version": "Lua_547",
        "source_file": "lvm.c"
      }
    },
    {
      "function_id": "ref::Lua_547::luaD_precall",
      "function_name": "luaD_precall",
      "metadata": {
        "lua_version": "Lua_547",
        "source_file": "ldo.c"
      }
    }
  ],
  "edges": [
    {
      "src": "ref::Lua_547::luaV_execute",
      "dst": "ref::Lua_547::luaD_precall",
      "edge_type": "calls"
    }
  ]
}
```

필수/선택 필드는 `query_callgraph.json`과 동일하다.

## 5. `anchor_mapping.json`

이미 확정된 high-confidence mapping을 저장한다.

예시:

```json
{
  "schema_version": "0.1",
  "mappings": [
    {
      "query_function_id": "query::0011A010",
      "reference_function_id": "ref::Lua_547::luaD_precall",
      "reference_function_name": "luaD_precall",
      "confidence": 0.96,
      "source": "retrieval_high_confidence",
      "status": "accepted"
    },
    {
      "query_function_id": "query::0011B020",
      "reference_function_id": "ref::Lua_547::luaT_trybinTM",
      "reference_function_name": "luaT_trybinTM",
      "confidence": 0.94,
      "source": "manual_seed",
      "status": "accepted"
    }
  ]
}
```

필수 필드:

- `schema_version`
- `mappings`
- `query_function_id`
- `reference_function_id`
- `confidence`
- `source`
- `status`

선택 필드:

- `reference_function_name`
- `evidence`
- `metadata`

## 6. Optional `llm_context`

`llm_context`는 query 단위 또는 mapping candidate 단위에 붙을 수 있다.

예시:

```json
{
  "llm_context": {
    "enabled": true,
    "decompiled_code": "/* optional decompiled code snippet */",
    "pcode_snippet": "/* optional pcode snippet */",
    "assembly_snippet": "/* optional assembly snippet */",
    "notes": [
      "custom function suspected due to application-specific strings"
    ]
  }
}
```

정책:

- `llm_context`는 없어도 된다.
- 너무 긴 code snippet은 넣지 않는다.
- symbol name이나 known label이 label leakage를 만들 수 있으면 masking한다.
- Local LLM 결과는 `llm_evidence`로만 저장한다.

## 7. `mapping_result.json`

Agent 실행 결과다.

예시:

```json
{
  "schema_version": "0.1",
  "run_id": "propagation_001",
  "inputs": {
    "retrieval": "data/inputs/retrieval_results/retrieval_topk.json",
    "query_callgraph": "data/inputs/callgraphs/query_callgraph.json",
    "reference_callgraph": "data/inputs/callgraphs/reference_callgraph.json",
    "anchor_mapping": "data/inputs/anchor_mapping.json"
  },
  "results": [
    {
      "query_function_id": "query::00119970",
      "final_candidate": {
        "reference_function_id": "ref::Lua_547::luaV_execute",
        "reference_function_name": "luaV_execute"
      },
      "status": "accepted",
      "confidence": 0.84,
      "scores": {
        "retrieval_prior": 0.709,
        "graph_score": 0.18,
        "final_score": 0.889
      },
      "evidence": [
        "retrieval_top2_candidate",
        "callee_anchor_match:luaD_precall",
        "reference_neighborhood_matches:luaV_execute"
      ],
      "conflicts": [],
      "llm_evidence": null
    }
  ]
}
```

상태값:

- `accepted`: 자동 확정 가능한 mapping.
- `deferred`: 근거 부족 또는 충돌로 수동 검토가 필요한 mapping.
- `conflict`: one-to-one 충돌 또는 graph inconsistency가 큰 mapping.
- `rejected`: 후보에서 제외된 mapping.

## 8. Validation Rules

초기 validation rule:

- 모든 JSON은 `schema_version`을 가져야 한다.
- `function_id`는 graph 안에서 unique해야 한다.
- 모든 edge의 `src`, `dst`는 `nodes`에 존재해야 한다.
- `retrieval_topk.json`의 `candidates`는 unique function-name 기준이어야 한다.
- `score_total`과 `confidence`는 기본적으로 `0.0 ~ 1.0` 범위여야 한다.
- `anchor_mapping.json`의 `status`는 `accepted`만 baseline anchor로 사용한다.
- `llm_context`가 없어도 propagation은 동작해야 한다.

## 9. Minimal Fixture Plan

다음 단계에서 작은 fixture를 만든다.

```text
data/eval/fixtures/
  retrieval_topk_minimal.json
  query_callgraph_minimal.json
  reference_callgraph_minimal.json
  anchor_mapping_minimal.json
```

이 fixture의 목표는 `llex`와 `luaV_execute`처럼 retrieval 점수는 비슷하지만 call graph evidence로 재랭킹 가능한 toy case를 만드는 것이다.
