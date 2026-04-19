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

## 현재 구현된 파이프라인

```text
lua_function_embedding retrieval result
  + query feature/callgraph
  + vanilla reference_callgraph.sqlite
  -> callgraph reranking
  -> graph-based candidate expansion
  -> seed-anchor propagation
  -> accepted / deferred / conflict classification
  -> deferred feature summary
  -> optional Local LLM analyst review
```

## Local LLM Analyst Layer

Local LLM은 최종 판정자가 아니라 애매한 함수에 대한 analyst assistant로 사용한다. 모든 함수에 LLM을 적용하지 않고, retrieval과 graph score만으로 판단이 어려운 `deferred`/`conflict` 함수에 한해 feature와 graph evidence를 검토하게 한다.

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

출력은 최종 mapping을 바로 덮어쓰는 값이 아니라 advisory evidence로 저장한다.

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

LM Studio / OpenAI-compatible local server 예시:

```bash
python3 scripts/06_run_local_llm_analyst.py \
  --provider openai-compatible \
  --base-url http://localhost:1234/v1 \
  --model qwen/qwen3.6-35b-a3b \
  --input-json data/eval/results/representative/deferred_analysis_lua547.json \
  --output-json data/eval/results/representative/llm_analysis_lua547_temp0.json \
  --temperature 0 \
  --timeout 180
```

`temperature 0` 결과는 보수적인 analyst review에 더 적합했다. `luaL_checktype`처럼 후보군 안에 정답이 있어도 graph evidence가 동점이면 `remain_deferred`로 두는 것이 현재 Agent 철학에 맞다.

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

## Reference Call Graph DB 생성

바닐라 Lua feature JSON에서 SQLite edge-list 기반 reference call graph를 생성한다.

```bash
python3 scripts/01_build_reference_callgraph_db.py --replace
```

기본 입력은 `../lua_extract_feature_ghidra/outputs_vanilla`이고, 기본 출력은 `data/inputs/callgraphs/reference_callgraph.sqlite`다. 실제 DB 파일은 재생성 가능한 산출물이므로 Git에는 포함하지 않는다.

대상 feature 파일만 확인하려면 다음처럼 실행한다.

```bash
python3 scripts/01_build_reference_callgraph_db.py --list-only
```

## Call Graph Scoring MVP

Retrieval top-k 후보를 query call graph anchor와 SQLite reference graph로 재랭킹한다.

```bash
python3 scripts/02_score_with_callgraph.py \
  --expected query::00119970=luaV_execute \
  --output-json data/eval/fixtures/result_callgraph_minimal.json
```

현재 minimal fixture에서는 retrieval-only가 `llex`를 top-1로 선택하지만, call graph evidence를 적용하면 `luaV_execute`가 top-1로 올라온다.

```text
retrieval_top1_accuracy   = 0.0
propagation_top1_accuracy = 1.0
improved                  = 1
regressed                 = 0
```

## Hybrid Retrieval + Call Graph 평가

`lua_function_embedding`의 실제 retrieval 평가 결과를 받아 callgraph score correction을 적용한다.

```bash
python3 scripts/03_eval_hybrid_callgraph_cases.py \
  --suite data/eval/cases/hybrid_callgraph_lua547_eval.json
```

이 평가는 `lua_function_embedding/data/eval/result_dir_index.json`의 `unique_topk_preview`를 retrieval 후보로 사용한다. Query feature에 남아 있는 caller/callee 이름 중 vanilla reference DB에 존재하는 이름을 임시 anchor로 사용한다.

현재 8개 Lua 5.4.7 평가 케이스 기준 결과:

```text
retrieval_top1_accuracy   = 0.75
propagation_top1_accuracy = 0.875
improved                  = 1
regressed                 = 0
```

`arm_to_x86_luaV_execute`는 retrieval-only에서 `llex`가 top-1이었지만, callgraph evidence 적용 후 `luaV_execute`로 재랭킹된다. `arm_to_x86_luaL_checktype`은 expected function이 retrieval 후보 목록에 없어서 callgraph 재랭킹만으로는 복구되지 않는다.

## Anchor Propagation / Deferred Analysis

High-confidence seed anchor를 사용해 주변 mapping을 전파한다.

```bash
python3 scripts/04_propagate_from_anchors.py \
  --suite data/eval/cases/anchor_propagation_lua547_eval.json
```

대표 결과:

```text
num_cases     = 9
accepted      = 7
deferred      = 2
conflict      = 0
top1_accuracy = 0.888889
top5_accuracy = 1.0
```

Deferred case는 feature summary와 LLM 입력 payload로 변환한다.

```bash
python3 scripts/05_build_deferred_analysis.py \
  --input-json data/eval/results/anchor_propagation_lua547_summary.json \
  --embedding-root ../lua_function_embedding \
  --output-json data/eval/results/representative/deferred_analysis_lua547.json
```

결과는 `data/eval/results/representative/deferred_analysis_lua547.json`에 compact representative output으로 남긴다.

## 실제 SO 대상 Name Mapping 흐름

내일 실제 Lua embedded `.so`를 분석할 때는 다음 순서로 진행한다.

```text
1. lua_extract_feature_ghidra
   -> SO에서 query feature JSON 추출

2. lua_function_embedding
   -> query feature로 architecture별 retrieval index 검색
   -> unique top-k 결과 생성

3. lua_callgraph_propagation_agent
   -> reference_callgraph.sqlite 준비
   -> retrieval 후보 callgraph reranking
   -> candidate expansion
   -> seed-anchor propagation
   -> accepted/deferred/conflict 분류

4. deferred analysis
   -> feature summary와 graph evidence 정리

5. optional Local LLM analyst
   -> deferred/custom-suspected case만 설명 및 우선순위화
```

자세한 최종 정리는 [docs/final_project_summary.md](docs/final_project_summary.md)를 참고한다.

## Git 관리 방침

Git에 포함하는 항목:

- Agent 설계 문서.
- 실행 스크립트와 핵심 모듈.
- 작은 fixture 또는 평가 case.
- compact summary / representative result.
- 디렉터리 유지용 `.gitkeep`.

Git에서 제외하는 항목:

- 대량 retrieval 결과.
- 대량 call graph dump.
- generated mapping 결과.
- root-level full trace result JSON.
- local DB, model/cache, binary artifact.

## 다음 작업 후보

- 실제 Lua embedded `.so`를 대상으로 feature extraction부터 name mapping까지 end-to-end 실행한다.
- accepted mapping을 누적 seed anchor로 사용하는 iterative propagation loop를 다듬는다.
- custom-suspected function을 분리하고 Local LLM analyst review를 적용한다.
- final mapping exporter를 추가해 accepted/deferred/conflict 결과를 하나의 보고서로 묶는다.
