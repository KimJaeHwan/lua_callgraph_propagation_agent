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

### 제공 툴 목록

#### 실행 / 분석

| 툴 | 설명 |
|----|------|
| `extract_query_features` | 단일 바이너리 Ghidra feature 추출 |
| `bulk_query_retrieval` | feature manifest → retrieval top-k 생성 |
| `run_downstream` | build_suite → propagation → deferred_analysis → final_report 재실행 |

#### 결과 조회

| 툴 | 설명 |
|----|------|
| `read_final_report` | 최종 보고서 summary + preview |
| `read_mapping_record` | case_id 기준 단일 매핑 레코드 조회 |
| `read_propagation_summary` | propagation 중간 결과 요약 (deferred/conflict 전체 목록) |
| `list_deferred_cases` | deferred + conflict 케이스 triage 목록 |

#### anchor 관리

| 툴 | 설명 |
|----|------|
| `register_force_anchor` | IDA 분석으로 확정된 단일 매핑 등록 후 propagation 재실행 |
| `batch_register_force_anchors` | 여러 확정 매핑을 한 번에 등록 (downstream 1회만 실행) |
| `run_downstream` | seed_anchors.json을 건드리지 않고 build_suite → propagation → report만 재실행 |

### 전형적인 워크플로우

```
1. extract_query_features  → Ghidra로 feature 추출
2. 12/13/14/04/05/15 스크립트 또는 기존 결과 파일 준비
3. read_final_report       → 결과 확인 (accepted/deferred/conflict 수)
4. list_deferred_cases     → deferred/conflict 케이스 목록 확인
5. (IDA로 deferred 케이스 decompile 분석)
6. batch_register_force_anchors → 확정 매핑 일괄 등록 + propagation 재실행
7. read_final_report       → 최종 결과 재확인
```

## Retrieval Index 정책

기본 정책은 full index를 내부 복사본으로 유지하는 것이다.

- slim 실험에서 index 크기를 줄일 수 있었지만 정확도가 유의미하게 떨어졌다.
- runtime은 full index를 채택한다.
- 현재 제공 버전: `Lua_547` (x86_64, aarch64), stub: `Lua_536`, `Lua_524`
- 새 버전 추가 시 동일 규칙으로 `data/inputs/retrieval_indexes/<version>/` 디렉터리를 추가하면 된다.

## 관련 문서

- [docs/mcp_runtime.md](docs/mcp_runtime.md) — MCP 툴 상세 설명
- [docs/mcp_quickstart_guide.md](docs/mcp_quickstart_guide.md) — 처음 쓰는 사람용 MCP 입문 가이드
- [docs/mcp_tool_reference.md](docs/mcp_tool_reference.md) — tool별 기능/입력/주의점 레퍼런스
- [docs/input_schema.md](docs/input_schema.md) — feature JSON 스키마
- [docs/config_field_reference.md](docs/config_field_reference.md) — config 필드 레퍼런스
- [docs/extraction_runtime_environment.md](docs/extraction_runtime_environment.md) — Ghidra 환경 설정
- [docs/macos_mps_setup.md](docs/macos_mps_setup.md) — Apple Silicon MPS 전용 환경 구성
- [docs/retrieval_performance_plan.md](docs/retrieval_performance_plan.md) — retrieval 성능 개선 측정/정리
- [docs/callgraph_propagation_agent_design.md](docs/callgraph_propagation_agent_design.md) — 설계 문서
- [docs/runtime_validation_and_configs.md](docs/runtime_validation_and_configs.md) — 검증 이력
