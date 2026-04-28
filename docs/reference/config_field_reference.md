# Config Field Reference

이 문서는 `lua_callgraph_propagation_agent/data/configs/*.json`에서 사용하는 주요 필드가 무엇을 의미하는지 설명한다.

대표 예시는 아래 두 파일을 기준으로 본다.

- pre-extracted 기준:
  [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
- binary 기준:
  [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)

## 1. 최상위 구조

모든 config는 보통 아래 구조를 가진다.

```json
{
  "pipeline_name": "...",
  "description": "...",
  "paths": { ... },
  "steps": { ... },
  "external_steps": []
}
```

각 영역의 의미:

- `pipeline_name`
  - 이 실행 설정의 이름이다.
  - 로그, 디버깅, 결과 정리에 쓰인다.
- `description`
  - 사람이 읽는 설명이다.
  - 실행 동작에는 직접 영향이 없다.
- `paths`
  - 입력/출력 파일, reference DB, retrieval index 같은 경로와 실행 metadata를 넣는다.
- `steps`
  - 파이프라인 단계별 enable 여부와 옵션을 넣는다.
- `external_steps`
  - 현재 내부 파이프라인 밖의 추가 명령을 붙이고 싶을 때 쓰는 확장 슬롯이다.
  - 일반 사용에서는 보통 빈 배열이다.

## 2. `paths` 필드

### 공통 식별/입력 필드

- `session_name`
  - 이번 실행 세션 이름이다.
  - 보통 결과가 `data/runtime/results/<session_name>/` 아래에 저장된다.

- `target_binary`
  - 실제 분석할 바이너리 파일 경로다.
  - binary 입력 경로에서만 사용한다.
  - pre-extracted 경로에서는 비워도 된다.

- `query_feature_json`
  - 이미 추출된 query feature JSON 경로다.
  - pre-extracted 경로에서 핵심 입력이다.

- `extract_manifest_json`
  - extractor가 만든 manifest JSON 경로다.
  - binary 입력 경로에서 extraction 다음 단계가 이 파일을 참조한다.

### 대상 환경 metadata

- `target_lua_version`
  - 대상이 어떤 Lua version family 기준인지 나타낸다.
  - 예: `Lua_547`, 이후 `Lua_536`, `Lua_524`
  - reference DB와 retrieval index 기본 경로를 고를 때 중요하다.

- `target_architecture`
  - 대상 아키텍처다.
  - 예: `x86_64`, `aarch64`
  - versioned retrieval index 경로 선택에 사용된다.

- `target_opt_level`
  - 대상 바이너리의 최적화 레벨이다.
  - 예: `O0`, `O2`, `O3`
  - feature extraction metadata에 반영된다.

- `target_strip_mode`
  - 대상 바이너리가 `nostrip`인지 `stripped`인지 나타낸다.
  - extraction metadata에 반영된다.

### extractor 관련 필드

- `extractor_script`
  - feature extraction에 사용할 extractor 스크립트 경로다.

- `extractor_work_root`
  - extractor가 runtime workspace, Ghidra project, temp 파일 등을 둘 루트다.

- `query_feature_output_root`
  - 추출된 query feature JSON이 저장될 루트 디렉터리다.

### retrieval 관련 필드

- `retrieval_script`
  - hybrid retrieval 실행 스크립트 경로다.

- `retrieval_index`
  - semantic/numeric/symbolic 검색용 runtime index 디렉터리다.
  - 기본 규칙:
    `data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime`

- `retrieval_output_json`
  - retrieval 결과 저장 위치다.

### propagation 관련 필드

- `seed_anchor_json`
  - seed anchor 선택 결과 저장 위치다.

- `runtime_suite_json`
  - propagation 입력용 suite JSON 저장 위치다.

- `propagation_suite`
  - 기본적으로는 비워둔다.
  - 이미 만들어 둔 propagation input JSON을 직접 지정하고 싶을 때 사용한다.

- `propagation_output_json`
  - propagation 결과 저장 위치다.

### reference 데이터 관련 필드

- `reference_feature_root`
  - vanilla reference feature JSON 루트다.
  - reference DB 재생성 시 주로 사용한다.

- `reference_db`
  - propagation에서 참조할 SQLite callgraph DB 경로다.
  - 기본 규칙:
    `data/inputs/callgraphs/<Lua_version>/reference_callgraph.sqlite`

- `embedding_project_root`
  - 일부 스크립트가 상대 경로를 해석할 때 기준으로 쓰는 루트다.

### 최종 산출물 관련 필드

- `deferred_output_json`
  - deferred/conflict 분석 결과 저장 위치다.

- `final_report_json`
  - 최종 mapping report 저장 위치다.

## 3. `steps` 필드

각 step은 대체로 아래 형식이다.

```json
"step_name": {
  "enabled": true,
  "...option": value
}
```

핵심 공통 필드:

- `enabled`
  - 해당 단계를 실행할지 말지를 결정한다.

### `build_reference_db`

- 역할:
  - vanilla reference feature JSON들로 reference SQLite DB를 다시 만든다.

- 주요 필드:
  - `enabled`
  - `replace`

- 보통:
  - 운영 중에는 `false`
  - reference dataset이 바뀌었을 때만 `true`

### `extract_query_features`

- 역할:
  - 대상 바이너리에서 query feature JSON을 추출한다.

- 보통:
  - binary 입력이면 `enabled: true`
  - pre-extracted 입력이면 `enabled: false`

### `bulk_retrieval`

- 역할:
  - 각 query 함수에 대해 top-k 후보를 생성한다.

- 주요 필드:
  - `enabled`
  - `candidate_pool`
    - 후보 풀 크기
  - `topk`
    - 최종 저장할 후보 수
  - `scoring_mode`
    - retrieval 점수 계산 모드
  - `mode`
    - 현재 보통 `runtime_query`

### `select_seed_anchors`

- 역할:
  - retrieval 결과 중 확실한 함수만 seed anchor로 선택한다.

- 주요 필드:
  - `enabled`
  - `min_top1_score`
    - anchor로 삼기 위한 최소 top1 score
  - `min_margin`
    - 1위와 2위 점수 차이 최소값

### `build_runtime_suite`

- 역할:
  - propagation에 필요한 입력 JSON을 구성한다.

- 보통:
  - propagation을 돌릴 때 `true`

### `propagation`

- 역할:
  - callgraph 기반 보정과 iterative propagation을 수행한다.

- 보통:
  - `enabled: true`

### `deferred_analysis`

- 역할:
  - 자동 확정되지 않은 함수들을 analyst-friendly payload로 정리한다.

- 주요 필드:
  - `enabled`
  - `top_candidates`
    - deferred case에 몇 개 후보까지 남길지

### `final_report`

- 역할:
  - 최종 결과를 `accepted`, `deferred`, `conflict`, `mapping_records` 형태로 정리한다.

- 보통:
  - `enabled: true`

## 4. `external_steps`

- 현재 파이프라인 밖의 추가 명령을 붙이고 싶을 때 사용한다.
- 일반 사용에서는 보통 빈 배열 `[]`이다.
- 연구용 후처리나 외부 실험 도구를 연결할 때만 의미가 있다.

## 5. 실제로 최소한 바꿔야 하는 것

### pre-extracted 입력

보통 이 정도만 바꾸면 된다.

```json
"paths": {
  "session_name": "my_preextracted_run",
  "target_lua_version": "Lua_547",
  "target_architecture": "x86_64",
  "query_feature_json": "data/inputs/query_features/my_query.json"
}
```

### binary 입력

보통 이 정도만 바꾸면 된다.

```json
"paths": {
  "session_name": "my_binary_run",
  "target_binary": "data/runtime/input/my_target.so",
  "target_lua_version": "Lua_547",
  "target_architecture": "x86_64",
  "target_opt_level": "O0",
  "target_strip_mode": "stripped"
}
```

나머지 경로는 [10_run_name_mapping_pipeline.py](../../scripts/10_run_name_mapping_pipeline.py)에서 기본값으로 자동 채워준다.

## 6. 추천 읽는 순서

처음 보는 사람 기준으로는 아래 순서가 가장 이해가 빠르다.

1. [runtime_recommended_preextracted.json](../../data/configs/runtime_recommended_preextracted.json)
2. [runtime_recommended_binary.json](../../data/configs/runtime_recommended_binary.json)
3. [runtime_validation_and_configs.md](runtime_validation_and_configs.md)
4. [../guides/release_assets.md](../guides/release_assets.md)
