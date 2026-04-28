# Runtime Validation And Config Guide

이 문서는 두 가지를 정리한다.

1. 외부 `stripped / nostrip` 바이너리 쌍으로 수행한 실제 검증 결과
2. `data/configs/` 아래 JSON 파일이 무엇인지에 대한 구분
3. Lua version/architecture별 입력 디렉터리 규칙

## 1. External Stripped/Nostrip Validation

검증 대상:

- stripped binary:
  `/Volumes/DO/lua_custom_engine_binaries/03_Lua_Mapper/lua_extract_feature_ghidra/binaries/Lua_547/x86_64/O0/stripped/lua_lua_547_0000`
- nostrip binary:
  `/Volumes/DO/lua_custom_engine_binaries/03_Lua_Mapper/lua_extract_feature_ghidra/binaries/Lua_547/x86_64/O0/nostrip/lua_lua_547_0000`

검증 방식:

- 동일한 binary index `0000`에 대해
- `nostrip`에서 함수 이름 정답표를 만든다
- 같은 `entry_point`의 `stripped` 함수 subset을 query로 사용한다
- stripped query를 pipeline에 태우고
- nostrip 정답 함수명과 비교한다

생성한 입력:

- query subset:
  `data/inputs/query_features/external_strip_0000_eval_subset.json`
- runtime config:
  `data/configs/runtime_external_strip_0000_eval_subset.json`
- expected mapping table:
  `data/runtime/results/external_strip_0000_eval_subset_expected.json`

검증 결과:

- total cases: `19`
- accepted: `10`
- deferred: `9`
- conflict: `0`
- overall top1 match: `18 / 19`
- overall top1 accuracy: `0.9474`
- accepted precision: `10 / 10 = 1.0`

해석:

- 자동 accept된 10개는 전부 정답이었다.
- deferred 9개 중 대부분은 top1 prediction 자체는 이미 정답이었다.
- 실제 오답은 `luaD_call -> luaD_callnoyield` 혼동 1건이었다.
- 즉 현재 policy는 비교적 보수적으로 accept를 주고 있으며, accept 결과의 precision은 높게 유지되고 있다.

대표 결과:

- `data/runtime/results/external_strip_0000_eval_subset/final_mapping_report.json`
- `data/runtime/results/external_strip_0000_eval_subset/propagation_result.json`
- `data/runtime/results/external_strip_0000_eval_subset/deferred_analysis.json`

## 2. Config Folder Meaning

`data/configs/` 아래 파일들은 기본적으로 **실행 입력 설정 파일**이다.

즉 이 폴더의 JSON은:

- 어떤 query를 쓸지
- 어떤 retrieval index를 쓸지
- 어떤 결과 파일로 저장할지
- 어떤 step을 켤지

를 정의하는 **pipeline input config**다.

산출물은 여기 저장되지 않는다.

실제 산출물 위치:

- `data/runtime/query_features/`
- `data/runtime/results/`

예를 들어:

- config:
  [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
- output:
  `data/runtime/results/recommended_preextracted_run/`

즉 `configs`는 "어떻게 실행할지"를 저장하는 곳이고,
`runtime/results`는 "실행 결과가 무엇이었는지"를 저장하는 곳이다.

추가로 현재 파이프라인은 비어 있는 경로를 자동으로 채우므로, config는 가능한 한 최소 입력만 담는 방향으로 정리하고 있다.

권장 최소 config:

- binary 입력용:
  [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)
- pre-extracted 입력용:
  [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)

## 3. Versioned Runtime Layout

reference DB와 retrieval index는 Lua version/architecture별로 분리해 둔다.

```text
data/inputs/callgraphs/<Lua_version>/reference_callgraph.sqlite
data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime/
```

예:

```text
data/inputs/callgraphs/Lua_547/reference_callgraph.sqlite
data/inputs/retrieval_indexes/Lua_547/x86_64/runtime/
data/inputs/retrieval_indexes/Lua_547/aarch64/runtime/
```

현재 확인 결과 `data/inputs/callgraphs/Lua_547/reference_callgraph.sqlite`에는 `Lua_547` 데이터만 들어 있다.  
아키텍처는 `aarch64`, `x86_64`, 최적화는 `O0/O1/O2/O3/Os`, strip mode는 현재 `nostrip`만 포함된다.

향후 Lua 5.3 / 5.2를 추가할 때는 같은 규칙으로 디렉터리만 확장하면 된다.

```text
data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite
data/inputs/callgraphs/Lua_524/reference_callgraph.sqlite
data/inputs/retrieval_indexes/Lua_536/x86_64/runtime/
data/inputs/retrieval_indexes/Lua_524/x86_64/runtime/
```

## 4. Current Config Policy

현재 Git에 포함되는 공식 config는 두 개만 유지한다.

- [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
  - 가장 빠르게 구조와 실행 흐름을 확인하는 기본 진입점
- [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)
  - 실제 binary extraction을 포함하는 기본 진입점

그 외 실험/평가/디버깅용 config는 로컬 재현용 자산으로 취급하고 Git에는 포함하지 않는다.

## 5. Practical Rule

실무적으로는 이렇게 생각하면 된다.

- `data/configs/*.json`
  - 실행에 필요한 입력 설정
- `data/inputs/query_features/*.json`
  - 실행에 넣을 query fixture 또는 pre-extracted feature
- `data/runtime/results/**`
  - 실행 후 생성된 산출물

즉 `configs`는 보존할 가치가 있는 "재현 가능한 실행 계획"이고,
`runtime/results`는 대체로 재생성 가능한 "실행 결과"다.
