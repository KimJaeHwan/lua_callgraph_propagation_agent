# Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent`는 `lua_function_embedding`의 retrieval top-k 결과를 입력으로 받아, call graph 문맥을 이용해 함수 이름/역할 매핑을 전파하고 역검증하는 Agent 개발 프로젝트입니다.

이 프로젝트는 단일 함수 feature만으로 애매한 후보를 결정하려는 단계가 아니라, caller/callee 관계를 이용해 retrieval 후보를 재랭킹하고 high-confidence mapping을 주변 함수로 propagation/ref-backpropagation하는 단계를 담당합니다.

## 배경

`lua_function_embedding`에서는 symbolic/numeric/semantic hybrid retrieval baseline을 만들었습니다.

하지만 다음과 같은 함수는 local feature만으로 충돌이 발생할 수 있습니다.

- `luaV_execute` vs `llex`
- `luaL_checktype` vs `callbinTM`
- architecture가 다른 query와 index 간 매칭
- 중복 함수가 많은 family

따라서 다음 단계에서는 call graph evidence를 사용합니다.

```text
Function feature extraction
  -> hybrid retrieval top-k
  -> call graph neighborhood collection
  -> propagation / ref-backpropagation agent
  -> final mapping decision
```

## 핵심 목표

- Retrieval 결과를 최종 정답이 아니라 후보 prior로 사용한다.
- Caller/callee consistency를 graph evidence로 추가한다.
- High-confidence mapping을 anchor로 고정한다.
- Anchor 주변으로 mapping confidence를 propagation한다.
- 이미 매핑된 caller/callee에서 ambiguous function으로 ref-backpropagation한다.
- Local similarity는 높지만 graph consistency가 낮은 후보를 conflict로 감지한다.

## 예정 파이프라인

```text
retrieval_topk.json
  + query_callgraph.json
  + reference_callgraph.json
  -> normalize graph ids
  -> initialize candidate beliefs
  -> propagate confidence from anchors
  -> backpropagate evidence from mapped neighbors
  -> resolve conflicts
  -> export final_mapping.json
```

## 디렉터리

- `docs/`: Agent 설계, scoring policy, propagation rule 문서.
- `scripts/`: CLI 실험 스크립트와 evaluation runner.
- `src/lua_callgraph_propagation_agent/`: 향후 패키지화할 핵심 모듈.
- `data/inputs/retrieval_results/`: `lua_function_embedding`에서 생성한 top-k retrieval 결과 입력.
- `data/inputs/callgraphs/`: query/reference call graph 입력.
- `data/outputs/mappings/`: Agent가 생성한 mapping 결과.
- `data/eval/`: propagation 평가 case와 결과.
- `data/tmp/`: 임시 변환 파일.
- `tests/`: 단위 테스트 및 작은 fixture 기반 검증.

## Git 관리 방침

Git에 포함하는 항목:

- Agent 설계 문서.
- 실행 스크립트와 핵심 모듈.
- 작은 fixture 또는 평가 case.
- 디렉터리 유지용 `.gitkeep`.

Git에서 제외하는 항목:

- 대량 retrieval 결과.
- 대량 call graph dump.
- generated mapping 결과.
- local DB, model/cache, binary artifact.

## 다음 작업 후보

- 입력 스키마 정의: retrieval result, query call graph, reference call graph.
- 후보 belief 모델 정의: semantic/numeric/symbolic score와 graph score 결합 방식.
- propagation rule 설계: caller, callee, mutual edge, path neighborhood.
- conflict resolver 설계: one-to-one mapping, family-level mapping, ambiguous candidate 보류.
- eval runner 작성: retrieval-only 대비 propagation 후 top-k 개선 여부 측정.
