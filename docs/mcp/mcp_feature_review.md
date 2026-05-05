# MCP Feature Review

`lua_callgraph_propagation_agent` MCP 구현 현황 (v0.6.0 기준).

## 구현 완료 (19개)

### 분석 준비

| 툴 | 추가 시점 | 비고 |
|----|---------|------|
| `extract_query_features` | 초기 | |
| `bulk_query_retrieval` | 초기 → v0.6 업데이트 | scope_json 파라미터 추가 |
| `select_seed_anchors` | 초기 → v0.5, v0.6 업데이트 | dedup-first, scope gate, targeted 통합 |
| `build_runtime_suite` | 초기 | |
| `detect_lua_scope` | v0.5 | 12b_detect_lua_scope.py 래핑 |
| `targeted_retrieval` | v0.6 | 12c_targeted_retrieval.py 래핑. embedding 불필요 |

### 결과 조회

| 툴 | 추가 시점 |
|----|---------|
| `read_final_report` | 초기 |
| `read_mapping_record` | 초기 |
| `read_propagation_summary` | 초기 |
| `list_deferred_cases` | 초기 |
| `show_candidate_context` | Round 3 |
| `get_mapping_distribution` | Round 4 |
| `export_trusted_mappings` | Round 4 |

### Anchor / 노이즈 관리

| 툴 | 추가 시점 |
|----|---------|
| `register_force_anchor` | 초기 |
| `batch_register_force_anchors` | 초기 |
| `run_downstream` | 초기 |
| `remove_force_anchor` | Round 3 |
| `update_noise_blacklist` | Round 4 |
| `patch_features_with_confirmed` | Round 4 |

---

## 의도적으로 노출하지 않는 것

- 단일 full-pipeline 래핑 툴
  - 이유: binary 분석 시 extraction과 analysis를 반드시 별도 프로세스로 실행해야 함
  - Ghidra JVM + embedding 모델이 한 프로세스에 겹치면 OOM 발생 가능

---

## 실전에서 발견된 핵심 이슈 (해결 완료)

### 1. 게임 코드 오염 (v0.5~0.6에서 해결)
- **문제**: 16,000개 게임 함수가 seed anchor를 오염 (`match×74`, `resume×40` 등)
- **해결**:
  - `detect_lua_scope` → Lua VM 함수만 골라냄
  - `bulk_query_retrieval --scope-json` → 800개만 embedding
  - `select_seed_anchors --dedup-max-per-ref 1` → 1:1 명확한 seed만

### 2. sentence_transformers 미설치로 retrieval 불가
- **문제**: `.venv` 재생성 후 `sentence-transformers`가 빠지면서 retrieval crash
- **해결**: `pyproject.toml`에 이미 등록됨. `pip install -e .`로 재설치
- **문서화**: README.md 설치 섹션, mcp_quickstart_guide.md 상단에 명시

### 3. patched feature가 retrieval에 반영 안 됨
- **문제**: `--skip-retrieval` 우회로 인해 199개 확정 이름이 retrieval에 미반영
- **해결**: `sentence-transformers` 설치 후 patched feature JSON + scope filter로 fresh retrieval 필요

---

## 향후 개선 방향

| 우선순위 | 항목 | 설명 |
|---------|------|------|
| 높음 | 멀티 라운드 자동 수렴 | export_trusted → anchor 추가 → 재실행을 수렴까지 자동 반복 |
| 높음 | 확정 쌍 파인튜닝 | 199개 confirmed pair로 embedding 모델 fine-tune → 낮은 threshold에서도 신뢰 가능 |
| 중간 | conflict diff | 충돌하는 query 함수들의 증거 비교 뷰 |
| 중간 | accepted-neighbor explorer | deferred 케이스 주변에 이미 accepted된 앵커 요약 |
| 낮음 | library table decoder | `luaL_Reg` 테이블 자동 디코딩 (`luaopen_*` 주변) |
