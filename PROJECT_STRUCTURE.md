# 프로젝트 디렉토리 구조

이 문서는 `lua_callgraph_propagation_agent` 프로젝트의 전체 구조와 각 폴더의 역할을 빠르게 파악하기 위한 참고 문서입니다.

## 요약 트리

```text
lua_callgraph_propagation_agent/
├── .gitignore
├── README.md
├── PROJECT_STRUCTURE.md
├── docs/
│   ├── callgraph_propagation_agent_design.md
│   ├── callgraph_store_design.md
│   ├── development_plan.md
│   └── input_schema.md
├── scripts/
│   ├── .gitkeep
│   ├── 01_build_reference_callgraph_db.py
│   ├── 02_score_with_callgraph.py
│   └── 03_eval_hybrid_callgraph_cases.py
├── src/
│   └── lua_callgraph_propagation_agent/
│       └── __init__.py
├── data/
│   ├── inputs/
│   │   ├── retrieval_results/
│   │   │   └── .gitkeep
│   │   └── callgraphs/
│   │       └── .gitkeep
│   ├── outputs/
│   │   └── mappings/
│   │       └── .gitkeep
│   ├── eval/
│   │   ├── .gitkeep
│   │   ├── cases/
│   │   │   └── hybrid_callgraph_lua547_eval.json
│   │   ├── fixtures/
│   │       ├── anchor_mapping_minimal.json
│   │       ├── query_callgraph_minimal.json
│   │       ├── reference_callgraph_minimal.json
│   │       ├── retrieval_topk_minimal.json
│   │       └── result_callgraph_minimal.json
│   │   └── results/
│   │       └── hybrid_callgraph_lua547_result.json
│   └── tmp/
│       └── .gitkeep
└── tests/
    └── .gitkeep
```

## 디렉터리 역할

- `docs/`
  - call graph propagation/ref-backpropagation Agent의 설계와 평가 기준을 정리한다.
- `scripts/`
  - CLI 실험 스크립트, 변환 스크립트, evaluation runner를 둔다.
- `src/lua_callgraph_propagation_agent/`
  - graph loader, candidate model, propagation engine, conflict resolver 같은 핵심 로직을 모듈화한다.
- `data/inputs/retrieval_results/`
  - `lua_function_embedding`에서 생성한 retrieval top-k JSON을 입력으로 둔다.
- `data/inputs/callgraphs/`
  - query binary와 reference Lua 함수 집합의 call graph 입력을 둔다.
  - 실제 vanilla reference graph는 SQLite edge-list 형태의 `reference_callgraph.sqlite`로 생성해 사용한다.
- `data/outputs/mappings/`
  - Agent가 생성한 final mapping 또는 intermediate mapping 결과를 저장한다.
- `data/eval/`
  - propagation 평가 case, expected mapping, result summary를 둔다.
  - `cases/`에는 실제 retrieval 결과와 callgraph 보정 평가를 연결하는 suite 파일을 둔다.
  - `fixtures/`에는 입력 스키마와 graph propagation baseline을 검증하기 위한 작은 toy case를 둔다.
  - `results/`에는 작은 평가 결과 JSON을 둔다.
- `data/tmp/`
  - 중간 변환 파일이나 임시 실험 결과를 둔다.
- `tests/`
  - 작은 fixture 기반 단위 테스트와 regression test를 둔다.

## Git 포함 원칙

저장소에는 다음을 남긴다.

- Agent 설계 문서.
- 실행/분석 로직을 담은 스크립트와 모듈.
- 작은 fixture 또는 평가 case.
- 디렉터리 구조 유지용 `.gitkeep`.

다음은 제외한다.

- 대량 retrieval 결과.
- 대량 call graph dump.
- generated mapping 결과.
- local DB, model/cache, binary artifact.
