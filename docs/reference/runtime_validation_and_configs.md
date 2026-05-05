# Runtime Validation And Config Guide

이 문서는 세 가지를 정리한다.

1. 현재 runtime 추천 config의 구조
2. 사용자가 직접 만지는 필드와 자동 유도되는 필드의 구분
3. fresh clone 이후 무엇을 먼저 확인해야 하는지

## 1. 현재 추천 config 정책

공식적으로 유지하는 실행 입력 config는 두 개다.

- [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
  - 가장 빠른 시작점
  - 이미 추출된 query feature JSON이 있을 때 사용
- [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)
  - 새 바이너리에서 extraction까지 포함해서 진행할 때 사용

둘 다 지금은 **최소 입력 중심 형식**으로 정리되어 있다.

대표 구조:

```json
{
  "session_name": "my_run",
  "user_input": {
    "binary": "data/runtime/input/target.so",
    "lua_version": "Lua_536",
    "architecture": "aarch64",
    "opt_level": "O2",
    "strip_mode": "stripped",
    "query_feature_json": "..."
  },
  "runtime": {
    "results_root": "data/runtime/results",
    "query_features_root": "data/runtime/query_features"
  },
  "tooling": {
    "vanilla_lua_source_root": "../lua_custom_engine_generator/lua_source_vanilla",
    "ida_signature_db": "data/inputs/ida_types/lua_function_signatures.sqlite"
  },
  "analysis": { ... },
  "graph_config": { ... },
  "managed_paths": { ... }
}
```

## 2. 무엇을 직접 수정하나

실무적으로는 아래만 자주 만지면 된다.

### `session_name`

- 결과가 저장될 실행 세션 이름
- 최종 결과는 보통 `data/runtime/results/<session_name>/` 아래에 모인다

### `user_input`

- `binary`
  - binary 입력 경로일 때만 필요
- `lua_version`
  - `Lua_547`, `Lua_536`, `Lua_524`
- `architecture`
  - `x86_64`, `aarch64`
- `opt_level`
  - `O0`, `O2` 등
- `strip_mode`
  - `nostrip`, `stripped`
- `query_feature_json`
  - pre-extracted 입력일 때 가장 중요
  - binary extraction을 이미 한 뒤 재실행할 때도 이 값만 있으면 편하다
- `feature_namespace`
  - 보통 건드릴 필요 없다
  - extractor가 쓰는 query feature 디렉터리 이름을 고정하고 싶을 때만 사용

### `tooling`

- `vanilla_lua_source_root`
  - 버전별 바닐라 Lua 헤더/소스 루트
  - IDA 타입 주입과 시그니처 DB 재생성에 필요
- `ida_signature_db`
  - SQLite 시그니처 DB

### `graph_config`

- accept / rename / targeted propagation 임계값
- 여기만 바꾸면 보수적 / 공격적 모드를 조절할 수 있다

## 3. 보통 안 건드려도 되는 것

### `runtime`

대부분 기본값 그대로 두면 된다.

- `results_root`
- `query_features_root`
- `extractor_work_root`

### `managed_paths`

설명용 블록이다.

- 실제로는 loader가 자동 계산한다
- retrieval index, reference DB, final report path, manual force anchor path 등을 사람이 직접 적지 않아도 된다

자동 유도되는 대표 경로:

```text
data/inputs/retrieval_indexes/<lua_version>/<architecture>/runtime
data/inputs/callgraphs/<lua_version>/reference_callgraph.sqlite
data/runtime/results/<session_name>/manual_force_anchors.json
data/runtime/results/<session_name>/final_mapping_report.json
data/runtime/query_features/<feature_namespace>/extract_manifest.json
```

## 4. runtime 결과 config도 같은 원칙

예를 들어 현재 실사용 config인
[runtime_config.json](../../data/runtime/results/libengine_lua536_aarch64_agent_rerun/runtime_config.json)
도 이제는 같은 식으로 나뉜다.

- 자주 바꿀 것:
  - `session_name`
  - `user_input`
  - `graph_config`
- 보통 유지:
  - `runtime`
  - `tooling`
  - `managed_paths`

즉 더 이상 `paths` 안에 결과 파일 위치를 전부 손으로 적을 필요가 없다.

## 5. fresh clone 이후 체크리스트

처음 clone 했을 때는 아래를 확인하면 된다.

### 필수 코드/환경

- Python 환경 설치
- 프로젝트 editable install
  - `pip install -e .`
- Local LLM을 쓸 경우
  - LM Studio endpoint 준비
- IDA evidence / rename을 쓸 경우
  - IDA MCP endpoint 준비

### 필수 데이터 자산

- reference DB
  - `data/inputs/callgraphs/<Lua_version>/reference_callgraph.sqlite`
- retrieval index
  - `data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime/`
- IDA function signature DB
  - `data/inputs/ida_types/lua_function_signatures.sqlite`
- 바닐라 Lua source tree
  - `../lua_custom_engine_generator/lua_source_vanilla`
  - 또는 config의 `tooling.vanilla_lua_source_root`가 가리키는 위치

### binary extraction까지 할 경우 추가

- Ghidra / pyghidra 환경
- extractor가 접근 가능한 target binary

### pre-extracted만 쓸 경우

- `user_input.query_feature_json`이 실제 존재하는지 확인

## 6. 환경 자산이 없을 때

자산이 비어 있으면 보통 아래 스크립트나 release 자산이 필요하다.

- reference DB 재생성
  - `scripts/setup/01_build_reference_callgraph_db.py`
- Lua signature DB 재생성
  - `scripts/setup/02_build_lua_signature_db.py`
- 샘플 runtime asset 복사
  - `scripts/setup/21_prepare_runtime_assets.py`

대용량 자산인 retrieval index와 callgraph DB는 Git tracked가 아니라 release / 로컬 준비 자산으로 취급한다.

## 7. 운영 관점 한 줄 정리

- 실행용 진입점은 `20`, `22`
- config에서 주로 보는 곳은 `user_input`, `tooling`, `graph_config`
- 나머지 경로는 loader가 최대한 자동으로 채운다
