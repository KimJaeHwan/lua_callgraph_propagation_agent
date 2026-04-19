# Lua Callgraph Propagation Agent 최종 요약

## 목적

`lua_callgraph_propagation_agent`는 `lua_function_embedding`이 만든 함수 후보를 call graph 문맥으로 재검증하고, 확실한 함수 이름 매핑을 주변 함수로 전파하기 위한 리버싱 분석 보조 Agent다.

이 프로젝트는 취약점 자동 판정기가 아니다. 목표는 Lua embedded engine 또는 Lua 기반 application binary에서 기능 함수를 빠르게 식별하고, logical vulnerability 분석자가 봐야 할 함수를 줄이는 것이다.

## 전체 파이프라인

```text
lua_custom_engine_generator
  -> vanilla/custom Lua binary 생성

lua_extract_feature_ghidra
  -> 함수별 static feature 추출
  -> callers/callees, strings, pcode histogram, struct offset, constants

lua_function_embedding
  -> symbolic/numeric/semantic hybrid retrieval
  -> unique top-k 후보 생성

lua_callgraph_propagation_agent
  -> vanilla reference callgraph SQLite DB 생성
  -> retrieval 후보 callgraph reranking
  -> graph-expanded candidate generation
  -> high-confidence anchor propagation
  -> accepted/deferred/conflict 분류
  -> deferred case feature summary 생성
  -> optional Local LLM analyst review
```

## 구현된 주요 단계

### 1. Reference Callgraph Store

`scripts/01_build_reference_callgraph_db.py`는 vanilla Lua feature JSON에서 SQLite edge-list 기반 reference call graph DB를 생성한다.

Reference graph는 embedding 대상이 아니라 정확한 `src -> dst` 관계 조회 대상이다. 따라서 FAISS 같은 vector index가 아니라 SQLite로 관리한다.

### 2. Callgraph Scoring

`scripts/02_score_with_callgraph.py`와 `scripts/03_eval_hybrid_callgraph_cases.py`는 retrieval 후보에 caller/callee consistency score를 더한다.

Scoring 철학:

- `retrieval_prior`는 `lua_function_embedding` 결과를 그대로 사용한다.
- graph score는 기존 score를 덮어쓰지 않고 보정한다.
- O0 edge는 primary evidence다.
- O1/O2/O3/Os edge는 optimization-tolerant auxiliary evidence다.
- missing edge는 inline/custom call 가능성 때문에 약한 penalty로만 다룬다.

### 3. Candidate Expansion

Retrieval top-k에 정답 후보가 없으면 reranking만으로는 복구할 수 없다.

`03_eval_hybrid_callgraph_cases.py`는 visible caller/callee anchor 주변의 reference function을 graph-expanded candidate로 추가할 수 있다.

실험 결과:

```text
40-case expanded eval
retrieval_top1_accuracy      0.95
propagation_top1_accuracy    0.975
expanded_candidate_recall    1.0
propagation_top5_accuracy    1.0
```

`luaL_checktype`은 retrieval 후보에는 없었지만, `sort -> luaL_checktype` 관계를 통해 후보군에는 복구되었다. 다만 `sort` 하나만으로는 `lua_type`, `lua_settop`, `luaL_len`, `luaL_checktype` 등을 완전히 구분하지 못해 top1 확정에는 실패했다.

### 4. Anchor Propagation

`scripts/04_propagate_from_anchors.py`는 seed anchor를 입력으로 받아 주변 함수 mapping을 전파한다.

출력 상태:

- `accepted`: retrieval과 graph evidence가 충분하고 margin이 있음.
- `deferred`: 후보는 있으나 graph evidence 또는 margin이 부족함.
- `conflict`: 같은 scope에서 중복 mapping 충돌이 있음.

대표 결과:

```text
num_cases: 9
accepted: 7
deferred: 2
conflict: 0
top1_accuracy: 0.888889
top5_accuracy: 1.0
```

중요한 점은 `luaU_undump`처럼 retrieval top1이 맞아 보여도 graph anchor evidence가 없으면 자동 확정하지 않는다는 것이다.

### 5. Deferred Analysis

`scripts/05_build_deferred_analysis.py`는 deferred/conflict case를 사람이 읽기 쉬운 analyst task로 변환한다.

포함 정보:

- deferred reason
- graph anchor summary
- top candidate score
- query feature summary
- candidate role hint
- LLM 입력용 `llm_payload`
- label leakage warning

대표 deferred case:

- `luaL_checktype`: small API wrapper처럼 보이나 tied candidate가 많아 `ambiguous_candidate_tie`.
- `luaU_undump`: string-rich loader-like function이나 graph anchor가 없어 `needs_more_graph_anchors`.

### 6. Local LLM Analyst

`scripts/06_run_local_llm_analyst.py`는 optional adapter다.

LLM은 최종 판정자가 아니라 analyst다. LLM 출력은 mapping을 직접 덮어쓰지 않고 advisory evidence로만 저장한다.

LM Studio 테스트:

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

`temperature 0` 결과:

```text
luaL_checktype -> remain_deferred
luaU_undump    -> remain_deferred
```

이 결과는 프로젝트 철학과 맞다. LLM은 애매한 후보를 무리하게 확정하기보다, 왜 추가 anchor 또는 수동 분석이 필요한지 설명하는 쪽에서 가장 유용하다.

## Git 결과 관리 정책

Full trace JSON은 후보 전체와 evidence를 모두 포함해 커질 수 있으므로 Git에 올리지 않는다.

Git에 남기는 항목:

- `data/eval/results/summaries/*.json`
- `data/eval/results/representative/*.json`
- 작은 fixture/case/anchor JSON
- 스크립트와 문서

로컬에만 두는 항목:

- root-level `data/eval/results/*.json`
- SQLite DB
- 대량 retrieval 결과
- generated full trace

## 내일 실제 SO 분석 플로우

실제 Lua embedded `.so`가 들어오면 다음 순서로 진행한다.

```text
1. lua_extract_feature_ghidra
   -> SO에서 query feature JSON 추출

2. lua_function_embedding
   -> query feature를 기존 architecture별 index로 검색
   -> unique top-k retrieval 결과 생성

3. lua_callgraph_propagation_agent
   -> reference_callgraph.sqlite 준비
   -> retrieval 결과를 callgraph로 reranking
   -> candidate expansion
   -> high-confidence accepted mapping을 seed anchor로 전파
   -> accepted/deferred/conflict 결과 생성

4. deferred analysis
   -> feature summary + graph evidence + retrieval evidence 정리

5. optional Local LLM analyst
   -> deferred/custom-suspected case만 설명/우선순위화

6. final mapping export
   -> accepted mapping
   -> deferred reason
   -> conflict/custom suspected list
```

## 현재 결론

이 프로젝트는 reverse-engineering analysis assistant MVP로 닫을 수 있는 수준까지 왔다.

핵심 성과는 다음과 같다.

- 단일 함수 feature retrieval의 한계를 call graph로 보정했다.
- 후보에 없던 함수를 graph neighborhood에서 복구하는 candidate expansion을 구현했다.
- 확실한 함수 mapping을 seed anchor로 사용해 주변 함수 mapping을 전파했다.
- 애매한 case를 자동 확정하지 않고 deferred로 분리했다.
- deferred case를 feature summary와 함께 LLM analyst payload로 변환했다.
- local LLM을 실제로 연결해 advisory review까지 검증했다.

다음 단계는 기능 추가가 아니라 실제 바이너리 대상으로 end-to-end name mapping을 수행하며 운영 흐름을 다듬는 것이다.
