# Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent`는 `lua_function_embedding`의 retrieval top-k 결과를 입력으로 받아, call graph 문맥을 이용해 함수 이름/역할 매핑을 전파하고 역검증하는 Agent 개발 프로젝트입니다.

이 프로젝트는 단일 함수 feature만으로 애매한 후보를 결정하려는 단계가 아니라, caller/callee 관계를 이용해 retrieval 후보를 재랭킹하고 high-confidence mapping을 주변 함수로 propagation/ref-backpropagation하는 단계를 담당합니다.

이 프로젝트에서 Agent는 LLM이 단독으로 취약점을 판정하는 시스템이 아니다. Retrieval 후보, call graph evidence, local LLM 분석 요약을 함께 사용해 분석자가 봐야 할 함수 후보를 줄이고, Lua core 함수와 custom application/binding logic의 경계를 더 빠르게 파악하기 위한 분석 보조 루프다.

## 관련 서브프로젝트

이 프로젝트는 Lua Mapper 전체 흐름의 마지막 decision layer에 가깝습니다. 앞 단계의 서브프로젝트들은 각각 데이터를 만들고, feature를 추출하고, retrieval 후보를 생성하는 역할을 담당합니다.

| Repository | 역할 |
| --- | --- |
| [`lua_custom_engine_generator`](https://github.com/KimJaeHwan/lua_custom_engine_generator) | 커스텀 Lua 엔진/바이너리 생성 단계. 다양한 Lua 버전, 아키텍처, 최적화 옵션 조합을 만들어 후속 분석 입력을 준비한다. |
| [`lua_extract_feature_ghidra`](https://github.com/KimJaeHwan/lua_extract_feature_ghidra) | Ghidra/PyGhidra 기반 feature extraction 단계. 바이너리 함수별 opcode, call, struct offset, compare, string 등 정적 feature를 추출한다. |
| [`lua_function_embedding`](https://github.com/KimJaeHwan/lua_function_embedding) | 함수 retrieval baseline 단계. 추출된 feature를 symbolic/numeric/semantic 표현으로 바꾸고, hybrid embedding 검색으로 top-k 후보를 만든다. |
| [`lua_callgraph_propagation_agent`](https://github.com/KimJaeHwan/lua_callgraph_propagation_agent) | 최종 graph reasoning 단계. retrieval 후보를 call graph 문맥으로 재검증하고 propagation/ref-backpropagation을 통해 최종 함수 매핑을 결정한다. |

전체 흐름은 다음과 같이 본다.

```text
lua_custom_engine_generator
  -> lua_extract_feature_ghidra
  -> lua_function_embedding
  -> lua_callgraph_propagation_agent
```

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
  + reference_callgraph.sqlite
  -> normalize graph ids
  -> initialize candidate beliefs
  -> propagate confidence from anchors
  -> backpropagate evidence from mapped neighbors
  -> local LLM analyst review for ambiguous/custom-suspected cases
  -> resolve conflicts
  -> export final_mapping.json
```

## Local LLM Analyst Layer

Local LLM은 최종 판정자가 아니라 애매한 함수에 대한 analyst assistant로 사용한다. 모든 함수에 LLM을 적용하지 않고, retrieval과 graph score만으로 판단이 어려운 함수에 한해 feature와 코드 문맥을 요약하게 한다.

대상 예시:

- Retrieval top-k score gap이 작아 후보가 애매한 함수.
- Retrieval confidence가 낮지만 call graph상 중요한 위치에 있는 함수.
- Lua core 함수와 custom binding/application logic 사이에서 판단이 필요한 함수.
- Graph conflict가 발생해 자동 확정하기 어려운 함수.

입력으로 줄 수 있는 정보:

- Function-level extracted feature.
- Retrieval top-k 후보와 score breakdown.
- Query/reference call graph neighborhood.
- 이미 확정된 anchor mapping.
- 필요한 경우 decompiled code, assembly, pcode snippet.

출력은 최종 mapping을 바로 덮어쓰는 값이 아니라 evidence로 저장한다.

```json
{
  "classification": "custom_application_logic",
  "confidence": 0.78,
  "reasoning_summary": [
    "calls Lua C API helper functions",
    "contains application-specific strings such as user_id and policy",
    "calls custom_auth_check, which is not present in vanilla Lua reference graph"
  ],
  "recommended_action": "prioritize manual review as possible authorization logic"
}
```

이 방식은 취약점 자동 분석이 아니라 리버싱 기반 logical vulnerability 분석에서 기능 함수 식별과 분석 우선순위화를 돕는 AX 도구를 목표로 한다.

## 디렉터리

- `docs/`: Agent 설계, scoring policy, propagation rule 문서.
- `scripts/`: CLI 실험 스크립트와 evaluation runner.
- `src/lua_callgraph_propagation_agent/`: 향후 패키지화할 핵심 모듈.
- `data/inputs/retrieval_results/`: `lua_function_embedding`에서 생성한 top-k retrieval 결과 입력.
- `data/inputs/callgraphs/`: query/reference call graph 입력.
- `data/inputs/callgraphs/reference_callgraph.sqlite`: vanilla reference graph를 edge-list로 저장하는 실제 조회용 DB.
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

- [docs/development_plan.md](docs/development_plan.md)에 정리된 순서대로 deterministic graph propagation baseline을 먼저 구현한다.
- 입력 스키마 정의: retrieval result, query call graph, reference call graph.
- Call graph 저장 설계: SQLite edge-list 기반 reference graph store.
- 후보 belief 모델 정의: semantic/numeric/symbolic score와 graph score 결합 방식.
- propagation rule 설계: caller, callee, mutual edge, path neighborhood.
- local LLM analyst 입력/출력 스키마 정의.
- conflict resolver 설계: one-to-one mapping, family-level mapping, ambiguous candidate 보류.
- eval runner 작성: retrieval-only 대비 propagation 후 top-k 개선 여부 측정.
