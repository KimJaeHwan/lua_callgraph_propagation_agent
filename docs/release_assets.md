# Release Assets Guide

이 문서는 `lua_callgraph_propagation_agent`를 GitHub에 올린 뒤, 실제 실행에 필요한 자산이 무엇인지 정리한다.

## 1. GitHub Clone 직후 가능한 것

레포만 clone한 상태에서도 아래는 바로 가능하다.

- config/디렉터리 구조 확인
- pipeline `--dry-run`
- tracked fixture JSON 확인
- vendored extractor / retrieval 코드 검토
- FastMCP 서버 코드 확인

현재 기본 샘플 config:

- pre-extracted:
  [runtime_recommended_preextracted.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/configs/runtime_recommended_preextracted.json)
- binary:
  [runtime_recommended_binary.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/configs/runtime_recommended_binary.json)

이 두 config는 now-existing tracked sample input을 가리키도록 맞춰져 있다.

## 2. GitHub Clone 직후 바로 없는 것

다음 자산은 기본적으로 Git에 올리지 않는다.

- retrieval index:
  `data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime/`
- reference callgraph DB:
  `data/inputs/callgraphs/<Lua_version>/reference_callgraph.sqlite`
- full reference feature set:
  `data/inputs/reference_features/<Lua_version>/...`

즉, 레포만 clone하면 구조와 config는 보이지만 실제 retrieval/propagation 실행에 필요한 대형 runtime asset은 비어 있다.

## 3. Runtime Release에 포함할 것

GitHub Release나 별도 배포 패키지에는 아래를 같이 올리는 것이 좋다.

- `data/inputs/retrieval_indexes/Lua_547/x86_64/runtime/`
- `data/inputs/callgraphs/Lua_547/reference_callgraph.sqlite`
- 필요하면 `data/inputs/reference_features/Lua_547/`
- 샘플 binary 또는 테스트용 pre-extracted query JSON

권장 패키지 예:

```text
lua_callgraph_runtime_assets/
  callgraphs/
    Lua_547/
      reference_callgraph.sqlite
  retrieval_indexes/
    Lua_547/
      x86_64/
        runtime/
```

## 4. 코드 외 환경 준비물

실제 binary extraction까지 하려면 다음도 필요하다.

- Python runtime
- `sentence-transformers`, `torch`, `faiss`, `fastmcp`
- Java
- Ghidra / pyghidra
- embedding model cache
  - 현재 기준 `BAAI/bge-small-en-v1.5`

pre-extracted query만 넣는 경우에는 Ghidra 쪽 준비는 생략할 수 있다.

## 5. 추천 실행 순서

가장 쉬운 검증:

```bash
../lua_llm/bin/python scripts/10_run_name_mapping_pipeline.py \
  --config data/configs/runtime_recommended_preextracted.json \
  --dry-run
```

runtime asset까지 준비된 뒤의 기본 실행:

```bash
../lua_llm/bin/python scripts/10_run_name_mapping_pipeline.py \
  --config data/configs/runtime_recommended_preextracted.json \
  --stop-on-error
```

binary extraction까지 포함한 실행:

```bash
../lua_llm/bin/python scripts/10_run_name_mapping_pipeline.py \
  --config data/configs/runtime_recommended_binary.json \
  --stop-on-error
```

## 6. 현재 상태 요약

현재 레포는 다음을 만족한다.

- versioned runtime path 구조
- tracked sample config
- tracked sample query/binary fixture
- dry-run 가능한 상태

다만 end-to-end 실실행은 retrieval index / reference DB / model / Ghidra 환경이 준비되어야 한다.
