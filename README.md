# Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent`는 stripped ELF 바이너리에 임베딩된 Lua 함수 이름을 자동으로 복원하는 단일 레포 런타임이다.

다음 단계를 한 곳에서 연결한다.

- query binary 또는 pre-extracted feature 입력
- Ghidra/pyghidra 기반 feature 추출 (callgraph, 상수, 문자열 참조)
- hybrid retrieval top-k 생성 (embedding + graph 결합 scoring)
- callgraph 기반 seed anchor 선택 (name_visible + retrieval_high_confidence)
- iterative propagation / conflict / deferred 분류
- deferred 분석 payload 생성
- FastMCP 서버로 analyst 개입용 tool interface 제공

## 설치

```bash
pip install -e .
```

`pyproject.toml`에 `sentence-transformers`가 의존성으로 등록되어 있어 위 명령어 한 번으로 설치된다.  
단, PyTorch를 함께 끌어오기 때문에 **설치 용량 2~3 GB, 소요 시간 5~15분** 정도를 예상한다.

### sentence_transformers 미설치 시 동작

| 단계 | 설치 여부 | 동작 |
|------|-----------|------|
| `12_run_bulk_query_retrieval.py` | **필수** | 없으면 즉시 crash |
| 나머지 모든 스크립트 (13~17, 04, 05, 15) | 불필요 | 정상 동작 |
| `12c_targeted_retrieval.py` | 불필요 | 정상 동작 (embedding 없이 구조 기반 검색) |

> **주의**: `sentence-transformers`가 설치되지 않은 상태에서 `17_patch_and_rerun.py`를 실행하면  
> retrieval 단계가 crash 한다. 이때는 `--skip-retrieval` 플래그로 우회한다.  
> 단, `--skip-retrieval`을 쓰면 기존 `retrieval_result.json`을 재사용하므로  
> 확정된 함수 이름(patched feature)이 retrieval에 반영되지 않는다.  
> **가능하면 설치 후 최소 1회 fresh retrieval을 돌리는 것을 강력히 권장한다.**

### 권장 전체 파이프라인 (설치 후)

```bash
# 1. Lua 스코프 탐지 (embedding 불필요)
python scripts/12b_detect_lua_scope.py \
  --query-json data/runtime/query_features/.../libengine_patched.json \
  --output-json data/runtime/results/<session>/lua_scope.json

# 2. 스코프 필터 적용 retrieval (핵심: 16k → ~800개만 encoding)
python scripts/12_run_bulk_query_retrieval.py \
  --query-json data/runtime/query_features/.../libengine_patched.json \
  --index data/inputs/retrieval_indexes/Lua_536/x86_64/runtime \
  --scope-json data/runtime/results/<session>/lua_scope.json \
  --output-json data/runtime/results/<session>/retrieval_result.json

# 3. seed 선택 (dedup-first + scope gate + targeted)
python scripts/13_select_seed_anchors.py \
  --retrieval-json .../retrieval_result.json \
  --output-json .../seed_anchors.json \
  --scope-json .../lua_scope.json \
  --targeted-json .../targeted_retrieval.json

# 또는 17_patch_and_rerun.py 한 번에:
python scripts/17_patch_and_rerun.py \
  --result-dir data/runtime/results/<session> \
  --query-json .../libengine_patched.json \
  --lua-version Lua_536
  # (scope JSON이 result-dir에 있으면 자동 적용)
```

## 런타임 구성

| 역할 | 경로 |
|------|------|
| Ghidra feature extractor | `src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py` |
| hybrid retrieval engine | `src/lua_callgraph_propagation_agent/vendor/hybrid_retrieval_embedding.py` |
| 파이프라인 참조용 진입점 | `scripts/10_run_name_mapping_pipeline.py` (**직접 실행 비권장** — 메모리 이슈 참고) |
| FastMCP 서버 | `scripts/20_run_mcp_server.py` |
| config 경로 통합 | `scripts/config_loader.py` |

핵심 입력 위치:

- reference callgraph DB: `data/inputs/callgraphs/<Lua_version>/reference_callgraph.sqlite`
- versioned retrieval index: `data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime`
- 실행 결과: `data/runtime/results/<session_name>/`

## Quick Start

### 1. pre-extracted feature로 분석 (가장 빠른 경로)

```bash
python scripts/12_run_bulk_query_retrieval.py \
  --extract-manifest data/runtime/query_features/<session>/extract_manifest.json \
  --index data/inputs/retrieval_indexes/Lua_547/x86_64/runtime \
  --output-json data/runtime/results/<session>/retrieval_result.json

python scripts/13_select_seed_anchors.py \
  --retrieval-json data/runtime/results/<session>/retrieval_result.json \
  --output-json data/runtime/results/<session>/seed_anchors.json \
  --query-json data/runtime/query_features/<session>/extract_manifest.json \
  --reference-db data/inputs/callgraphs/Lua_547/reference_callgraph.sqlite

# ... 이하 14, 04, 15 스크립트 순서대로
```

### 2. 새 바이너리 분석 전체 경로

**Ghidra JVM과 embedding 모델은 메모리가 겹치므로 반드시 분리 실행한다.**

```bash
# Step 1: feature 추출 (Ghidra 프로세스 단독 실행)
python scripts/11_extract_query_features.py \
  --binary /path/to/target.so \
  --lua-version Lua_547 \
  --architecture x86_64 \
  --strip-mode stripped \
  --session-name my_session \
  --output-root data/runtime/query_features \
  --work-root data/runtime/extractor_workspace \
  --extractor-script src/lua_callgraph_propagation_agent/vendor/pyghidra_feature_extractor.py \
  --ghidra-home /path/to/ghidra

# Step 2: 분석 (Ghidra 완전 종료 후)
python scripts/12_run_bulk_query_retrieval.py ...
python scripts/13_select_seed_anchors.py ...
python scripts/14_build_runtime_propagation_suite.py ...
python scripts/04_propagate_from_anchors.py --iterative ...
python scripts/15_export_final_mapping_report.py ...
```

## 메모리 이슈 주의

`scripts/10_run_name_mapping_pipeline.py`는 extraction + analysis를 하나의 프로세스에서 순서대로 실행하는 참조용 스크립트다. Ghidra JVM과 embedding 모델이 메모리를 동시에 점유하면 OOM이 발생할 수 있으므로, **실제 binary 분석에서는 10번 스크립트를 직접 사용하지 않는다.**

같은 이유로 MCP 서버도 `10_run_name_mapping_pipeline.py`를 감싸는 tool을 노출하지 않는다.  
MCP에서는 extraction과 downstream 단계를 직접 호출하는 방식만 지원한다.

## FastMCP 서버

```bash
python scripts/20_run_mcp_server.py
```

### 제공 툴 목록 (v0.6.0 기준)

#### 실행 / 분석

| 툴 | 설명 |
|----|------|
| `extract_query_features` | 단일 바이너리 Ghidra feature 추출 |
| `detect_lua_scope` | 문자열 신호 BFS로 Lua VM 함수 스코프 자동 탐지 |
| `bulk_query_retrieval` | feature → retrieval top-k 생성 (`--scope-json`으로 Lua 함수만 검색 가능) |
| `targeted_retrieval` | 확정 앵커 이웃 기반 구조적 검색 (embedding 불필요) |
| `select_seed_anchors` | retrieval + targeted 결과에서 seed anchor 선택 (dedup-first + scope gate) |
| `build_runtime_suite` | retrieval + seed + reference DB를 propagation 입력으로 조립 |
| `run_downstream` | build_suite → propagation → deferred_analysis → final_report 재실행 |

#### 결과 조회

| 툴 | 설명 |
|----|------|
| `read_final_report` | 최종 보고서 summary + preview |
| `read_mapping_record` | case_id 기준 단일 매핑 레코드 조회 |
| `read_propagation_summary` | propagation 중간 결과 요약 |
| `list_deferred_cases` | deferred + conflict 케이스 triage 목록 |
| `show_candidate_context` | 단일 케이스 전체 컨텍스트 번들 (매핑+triage+feature 요약) |
| `get_mapping_distribution` | 다:1 노이즈 명 탐지용 히스토그램 |
| `export_trusted_mappings` | 신뢰도 높은 1:1 매핑 추출 (IDA 리네임용) |

#### anchor / 노이즈 관리

| 툴 | 설명 |
|----|------|
| `register_force_anchor` | 단일 확정 매핑 등록 + propagation 재실행 |
| `batch_register_force_anchors` | 여러 확정 매핑 일괄 등록 (downstream 1회) |
| `remove_force_anchor` | force anchor 취소 |
| `update_noise_blacklist` | suite.json noise_blacklist 추가/제거 |
| `patch_features_with_confirmed` | feature JSON callee/caller에 실제 이름 반영 |

### 권장 분석 루프 (혼합 바이너리 기준)

```
Round 1 (초기):
  1. extract_query_features   → feature 추출
  2. detect_lua_scope         → Lua 스코프 탐지 (~800개)
  3. bulk_query_retrieval     → scope 필터 적용 retrieval (800개만 encoding)
  4. select_seed_anchors      → dedup-first + scope gate
  5. run_downstream           → propagation Round 1
  6. get_mapping_distribution → 노이즈 명 탐지
  7. update_noise_blacklist + run_downstream → 노이즈 제거

Round N (반복):
  8. export_trusted_mappings  → 신뢰 매핑 추출
  9. (IDA에서 확인 후 rename)
 10. patch_features_with_confirmed → callee/caller에 실제 이름 반영
 11. targeted_retrieval       → 구조 기반 추가 탐색 (embedding 불필요)
 12. select_seed_anchors      → targeted_json 포함해서 재선택
 13. run_downstream           → propagation Round N
 14. accepted 증가 없으면 수렴 → 종료
```

## Retrieval Index 정책

기본 정책은 full index를 내부 복사본으로 유지하는 것이다.

- slim 실험에서 index 크기를 줄일 수 있었지만 정확도가 유의미하게 떨어졌다.
- runtime은 full index를 채택한다.
- 현재 제공 버전: `Lua_547` (x86_64, aarch64), stub: `Lua_536`, `Lua_524`
- 새 버전 추가 시 동일 규칙으로 `data/inputs/retrieval_indexes/<version>/` 디렉터리를 추가하면 된다.

## 관련 문서

| 문서 | 설명 |
|------|------|
| [docs/mcp_quickstart_guide.md](docs/mcp_quickstart_guide.md) | 처음 쓰는 사람용 입문 가이드. sentence_transformers 설치 확인부터 시작 |
| [docs/mcp_tool_reference.md](docs/mcp_tool_reference.md) | 19개 툴 전체 레퍼런스 (v0.6.0) |
| [docs/mcp_runtime.md](docs/mcp_runtime.md) | MCP 서버 구조, 워크플로우, 설계 원칙 |
| [docs/mcp_feature_review.md](docs/mcp_feature_review.md) | 구현 현황, 실전 이슈 해결 이력, 향후 방향 |
| [docs/langgraph_agent_plan.md](docs/langgraph_agent_plan.md) | Local LLM 자율 실행용 LangGraph 에이전트 설계 |
| [docs/input_schema.md](docs/input_schema.md) | feature JSON 스키마 |
| [docs/config_field_reference.md](docs/config_field_reference.md) | config 필드 레퍼런스 |
| [docs/extraction_runtime_environment.md](docs/extraction_runtime_environment.md) | Ghidra / pyghidra 환경 설정 |
| [docs/macos_mps_setup.md](docs/macos_mps_setup.md) | Apple Silicon MPS 전용 환경 구성 |
| [docs/callgraph_propagation_agent_design.md](docs/callgraph_propagation_agent_design.md) | 초기 설계 문서 (구현 과정에서 일부 발전됨) |
| [docs/runtime_validation_and_configs.md](docs/runtime_validation_and_configs.md) | 검증 이력 |
