# Config Field Reference

이 문서는 **현재 권장 config 형식**만 빠르게 설명한다.

대표 예시:

- [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
- [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)

## 1. 최상위 구조

보통 아래 블록만 보면 된다.

```json
{
  "session_name": "...",
  "user_input": { ... },
  "runtime": { ... },
  "analysis": { ... },
  "graph_config": { ... },
  "managed_paths": { ... }
}
```

의미:

- `session_name`
  - 결과 세션 이름
- `user_input`
  - 사용자가 직접 바꾸는 핵심 입력
- `runtime`
  - 결과/중간산출물 루트 같은 실행 루트 설정
- `analysis`
  - retrieval / seed / propagation 단계 설정
- `graph_config`
  - accept / deferred / rename 임계값
- `managed_paths`
  - 자동 유도되는 경로 설명용

## 2. `user_input`

### 공통

- `lua_version`
  - `Lua_547`, `Lua_536`, `Lua_524`
- `architecture`
  - `x86_64`, `aarch64`

### binary 입력일 때

- `binary`
  - 분석할 바이너리 경로
- `opt_level`
  - `O0`, `O2` 등
- `strip_mode`
  - `nostrip`, `stripped`

### pre-extracted 입력일 때

- `query_feature_json`
  - 이미 추출된 feature JSON

### 선택적

- `feature_namespace`
  - query feature 디렉터리 이름을 강제로 고정하고 싶을 때만 사용
  - 비워두면 binary 이름 + Lua version + arch에서 자동 유도

## 3. `runtime`

대부분 기본값 유지 권장.

- `results_root`
  - 결과 루트
- `query_features_root`
  - extractor 결과 루트
- `extractor_work_root`
  - Ghidra/pyghidra 작업 루트

## 4. `tooling`

고급 override 블록이다.

보통은 config에 아예 안 적어도 된다.

- 기본 바닐라 Lua 소스 루트
  - `data/inputs/lua_source_vanilla`
- 기본 시그니처 DB
  - `data/inputs/ida_types/lua_function_signatures.sqlite`

## 5. `analysis`

### `analysis.query_json`

- pre-extracted 입력일 때 `user_input.query_feature_json`과 같은 값으로 두는 편이 가장 단순하다

### `analysis.retrieval`

- `candidate_pool`
- `topk`
- `scoring_mode`

### `analysis.seed_anchors`

- `min_top1_score`
- `min_margin`

### `analysis.propagation`

- `iterative`

### `analysis.deferred_analysis`

- `top_candidates`

## 6. `graph_config`

여기가 accept/rename 성향을 가장 크게 바꾼다.

대표적으로 자주 보는 값:

- `targeted_min_score`
- `trusted_min_score`
- `decompile_min_score`
- `rename_min_score`
- `rename_relaxed_min_score`
- `safe_auto_rename_prefixes`
- `enable_ida_type_injection`

## 7. `managed_paths`

보통 설명용이다.

실제 실행 때는 loader가 아래를 자동 유도한다.

- retrieval index
- reference DB
- extract manifest path
- result directory
- final report path
- manual force anchor path

즉 일반적으로는 여기 값을 직접 적지 않아도 된다.

## 8. 자동 유도 규칙

예를 들어:

- `lua_version = Lua_536`
- `architecture = aarch64`
- `session_name = libengine_run`

이면 대략 아래가 자동으로 잡힌다.

```text
data/inputs/retrieval_indexes/Lua_536/aarch64/runtime
data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite
data/runtime/results/libengine_run/final_mapping_report.json
data/runtime/results/libengine_run/manual_force_anchors.json
```

## 9. legacy config

예전 `paths{}` / `steps{}` 형식도 loader가 여전히 읽는다.  
다만 새로 만들거나 수정할 때는 **현재 권장 형식**을 쓰는 편이 훨씬 단순하다.
