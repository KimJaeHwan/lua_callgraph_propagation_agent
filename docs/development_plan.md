# Development Plan

이 문서는 `lua_callgraph_propagation_agent` 개발 순서를 정리한 계획서다.

핵심 원칙은 Local LLM을 처음부터 붙이지 않는 것이다. 먼저 deterministic retrieval + graph propagation baseline을 만들고, 이후 ambiguous/custom-suspected case에만 Local LLM analyst layer를 추가한다.

## 1. 입력 스키마 고정

먼저 Agent가 받을 JSON 형태를 고정한다.

필요한 입력:

- `retrieval_topk.json`: `lua_function_embedding`에서 생성한 query 함수별 top-k 후보와 score breakdown.
- `query_callgraph.json`: 분석 대상 바이너리의 caller/callee graph.
- `reference_callgraph.json`: 기준 Lua 함수 집합의 caller/callee graph.
- `anchor_mapping.json`: 이미 확정된 high-confidence mapping.

산출물:

- `docs/input_schema.md`
- 작은 fixture JSON

현재 입력 스키마 결정사항은 `docs/input_schema.md`에 정리한다.

## 2. Graph Loader 구현

Call graph JSON을 내부 구조로 읽는 모듈을 만든다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/graph_loader.py
```

목표:

- `function_id -> callees` 조회.
- `function_id -> callers` 조회.
- function metadata 조회.
- 없는 node, 빈 edge, 중복 edge를 안전하게 처리.

이 단계에서는 scoring을 하지 않는다. graph를 안정적으로 읽고 이웃을 조회하는 기능만 만든다.

## 3. Retrieval Candidate Store 구현

`lua_function_embedding`의 retrieval 결과를 query별 후보 목록으로 변환한다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/candidate_store.py
```

목표:

- query function별 top-k 후보 로드.
- candidate score breakdown 보존.
- raw top-k와 unique function-name top-k를 구분.
- 중복 함수가 많은 경우 unique 후보를 우선 사용.

초기 구현에서는 unique function-name 기준 후보를 기본값으로 사용한다.

## 4. Baseline Mapping 초기화

Retrieval 점수만으로 초기 mapping belief를 만든다.

예시 정책:

```text
if top1_score >= 0.90 and top1_margin >= 0.05:
    status = anchor_candidate
else:
    status = ambiguous
```

산출물:

- query function별 initial candidate belief.
- anchor candidate 목록.
- ambiguous candidate 목록.

## 5. Graph Consistency Scorer 구현

후보 `query_func -> candidate_func`가 call graph 문맥에서 얼마나 일관적인지 점수화한다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/graph_scorer.py
```

초기 score sketch:

```text
graph_score =
  callee_anchor_match_count * 0.05
  + caller_anchor_match_count * 0.05
  + mutual_edge_match_count * 0.03
  - conflict_count * 0.10
```

이 단계도 Local LLM 없이 rule 기반으로 만든다. 그래야 이후 LLM layer가 실제로 도움을 주는지 비교할 수 있다.

## 6. Propagation Engine 구현

High-confidence mapping을 anchor로 삼고 주변 후보 점수를 갱신한다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/propagation_engine.py
```

초기 흐름:

```text
anchors 선택
  -> anchors 주변 query node 찾기
  -> reference graph에서 candidate 주변 관계 확인
  -> graph bonus 부여
  -> final_score 재계산
  -> accepted / deferred / conflict 상태 업데이트
```

초기 버전에서는 1-hop caller/callee만 사용한다. 2-hop 이상은 noise가 늘 수 있으므로 baseline 이후에 추가한다.

현재 MVP 구현:

- `scripts/04_propagate_from_anchors.py`
- 입력 suite: `data/eval/cases/anchor_propagation_lua547_eval.json`
- seed anchor: `data/eval/anchors/lua547_seed_anchors.json`
- 로컬 full trace 결과: `data/eval/results/anchor_propagation_lua547_summary.json`
- Git 추적 요약: `data/eval/results/summaries/callgraph_eval_summary.json`
- Git 추적 대표 결과: `data/eval/results/representative/anchor_propagation_lua547_compact.json`

현재 결과는 9개 대표 cross-architecture case 기준 `accepted=7`, `deferred=2`, `conflict=0`이다. `luaL_checktype`은 `sort` caller anchor를 통해 후보군에는 복구되지만, 같은 caller neighborhood 안의 `lua_type`, `lua_settop`, `luaL_len` 등과 graph evidence가 같아 `deferred`로 분류된다. `luaU_undump`는 retrieval top1은 맞지만 anchor evidence가 없어 보수적으로 `deferred` 처리된다.

이 결과는 Agent 정책상 중요하다. 단순히 top1이 맞는지만 보지 않고, 충분한 graph evidence와 score margin이 없으면 확정하지 않는 방향이 리버싱 보조 도구에 더 안전하다.

Eval 결과 관리 정책은 full trace와 tracked summary를 분리한다. Root-level `data/eval/results/*.json`은 로컬 디버깅 artifact로 보고 `.gitignore` 처리한다. Git에는 `summaries/`의 작은 지표 요약과 `representative/`의 대표 compact 결과만 남긴다.

## 7. Conflict Resolver 구현

같은 reference 함수에 여러 query 함수가 매핑되는 경우를 처리한다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/conflict_resolver.py
```

초기 정책:

- score margin이 충분하면 높은 쪽은 `accepted`, 낮은 쪽은 `deferred`.
- 둘 다 낮으면 둘 다 `deferred`.
- graph conflict가 크면 LLM 여부와 관계없이 `accepted`하지 않는다.
- family-level mapping이 허용되는 경우는 별도 정책으로 분리한다.

보안 분석 도구에서는 틀린 확정이 분석 시간을 더 낭비시킬 수 있으므로, 애매한 case는 보수적으로 `deferred` 처리한다.

## 8. CLI Runner 구현

End-to-end로 propagation을 실행하는 스크립트를 만든다.

예상 파일:

```text
scripts/01_run_propagation.py
```

예상 명령어:

```bash
python scripts/01_run_propagation.py \
  --retrieval data/inputs/retrieval_results/result_jaccard.json \
  --query-graph data/inputs/callgraphs/query_callgraph.json \
  --reference-graph data/inputs/callgraphs/reference_lua547_callgraph.json \
  --output data/outputs/mappings/mapping_result.json
```

## 9. Evaluation Runner 구현

Retrieval-only와 propagation-after 결과를 비교하는 평가 스크립트를 만든다.

예상 파일:

```text
scripts/02_eval_mapping.py
```

초기 지표:

- top-1 accuracy.
- top-k hit.
- accepted mapping accuracy.
- deferred count.
- conflict count.
- hard negative case 개선 여부.

우선 확인할 hard case:

- `luaV_execute`
- `llex`
- `luaL_checktype`
- `callbinTM`

## 10. Local LLM Analyst Layer 추가

Deterministic graph propagation baseline이 동작한 뒤 Local LLM analyst layer를 추가한다.

예상 파일:

```text
src/lua_callgraph_propagation_agent/llm_analyst.py
```

LLM 호출 대상:

- `status = deferred`
- `conflict_count > 0`
- `top1_margin < threshold`
- `custom_suspected = true`

LLM 출력은 final mapping을 바로 덮어쓰지 않고 evidence로 저장한다.

예상 출력:

```json
{
  "llm_classification": "custom_application_logic",
  "llm_confidence": 0.78,
  "llm_evidence": [
    "calls Lua C API helpers",
    "contains application-specific strings",
    "does not match vanilla Lua reference graph"
  ],
  "recommended_action": "manual_review"
}
```

중요 정책:

- LLM은 취약점 존재를 판정하지 않는다.
- LLM은 feature와 코드 문맥을 요약하는 analyst assistant로만 사용한다.
- Graph conflict가 큰 경우 LLM이 확신해도 `accepted`하지 않는다.
- Symbol name이나 known label이 남아 있는 경우 label leakage를 방지한다.

## 11. Report Generator 구현

사람이 읽기 좋은 분석 결과 report를 만든다.

예상 파일:

```text
scripts/03_generate_report.py
```

Report에 포함할 내용:

- accepted mappings.
- deferred mappings.
- graph conflicts.
- custom-suspected functions.
- hard negative cases.
- Local LLM analyst summaries.
- manual review priority list.

## 12. 권장 구현 순서 요약

처음 구현은 다음 순서로 진행한다.

```text
1. docs/input_schema.md 작성
2. 작은 fixture JSON 작성
3. graph_loader.py 구현
4. candidate_store.py 구현
5. graph_scorer.py 구현
6. propagation_engine.py 구현
7. conflict_resolver.py 구현
8. scripts/01_run_propagation.py 구현
9. scripts/02_eval_mapping.py 구현
10. llm_analyst.py 추가
11. scripts/03_generate_report.py 구현
```

가장 중요한 baseline은 `1~9`다. Local LLM은 그 이후에 붙인다.

## 13. 최종 목표

이 프로젝트의 최종 목표는 취약점 자동 판정기가 아니다.

목표는 리버싱 기반 logical vulnerability 분석에서 기능 함수 식별과 분석 우선순위화를 돕는 AX 도구다.

```text
retrieval candidate generator
  -> deterministic graph propagation
  -> conflict/deferred handling
  -> optional local LLM analyst evidence
  -> human-reviewable mapping report
```
