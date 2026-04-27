# MCP Runtime

`lua_callgraph_propagation_agent`는 FastMCP 기반 stdio MCP 서버를 제공한다.  
analyst 또는 상위 LLM agent가 파이프라인을 제어하고 deferred case를 해소할 수 있는 표준화된 인터페이스다.

처음 쓰는 사람이라면 이 문서 전에 아래 두 문서를 먼저 보는 편이 더 쉽다.

- [mcp_quickstart_guide.md](mcp_quickstart_guide.md)
- [mcp_tool_reference.md](mcp_tool_reference.md)

## 실행

```bash
python scripts/20_run_mcp_server.py
```

서버 구현 위치:

- `scripts/20_run_mcp_server.py` — entrypoint
- `src/lua_callgraph_propagation_agent/mcp_server.py` — 툴 구현 (v0.6.0)

---

## 툴 목록 (v0.6.0 기준, 총 19개)

### 분석 준비 툴

| 툴 | 스크립트 | 설명 |
|----|---------|------|
| `extract_query_features` | `11_extract_query_features.py` | 바이너리 → Ghidra feature 추출 |
| `detect_lua_scope` | `12b_detect_lua_scope.py` | Lua VM 함수 스코프 자동 탐지 (문자열 신호 BFS) |
| `bulk_query_retrieval` | `12_run_bulk_query_retrieval.py` | hybrid retrieval top-k 생성. `scope_json`으로 게임코드 차단 가능 |
| `targeted_retrieval` | `12c_targeted_retrieval.py` | 확정 앵커 이웃 기반 구조적 검색. embedding 불필요 |
| `select_seed_anchors` | `13_select_seed_anchors.py` | seed anchor 선택. dedup-first + scope gate + targeted 통합 |
| `build_runtime_suite` | `14_build_runtime_propagation_suite.py` | propagation 입력 조립 |
| `run_downstream` | 04+05+15 | build_suite → propagation → deferred → report 재실행 |

### 결과 조회 툴

| 툴 | 설명 |
|----|------|
| `read_final_report` | accepted/deferred/conflict 수 + preview |
| `read_propagation_summary` | propagation 중간 결과 요약 |
| `list_deferred_cases` | triage용 deferred/conflict 목록 |
| `read_mapping_record` | case_id 단일 매핑 레코드 조회 |
| `show_candidate_context` | 케이스 전체 컨텍스트 번들 (매핑+triage+feature 요약) |
| `get_mapping_distribution` | ref 이름별 매핑 수 히스토그램. 노이즈 탐지용 |
| `export_trusted_mappings` | 1:1 신뢰 매핑 추출. IDA rename 작업 대상 리스트 |

### Anchor / 노이즈 관리 툴

| 툴 | 설명 |
|----|------|
| `register_force_anchor` | 단일 확정 매핑 등록 + downstream 재실행 |
| `batch_register_force_anchors` | 여러 확정 매핑 일괄 등록 (downstream 1회) |
| `remove_force_anchor` | force_anchor 소스 매핑 제거 |
| `update_noise_blacklist` | suite.json noise_blacklist 추가/제거 |
| `patch_features_with_confirmed` | feature JSON callee/caller에 실제 이름 반영 |

---

## 전형적인 워크플로우

### 혼합 바이너리 전체 분석 (게임 엔진 권장)

```
1. extract_query_features
   → Ghidra 완전히 종료될 때까지 기다린다 (메모리 분리 원칙)

2. detect_lua_scope
   → lua_scope.json 생성 (~800개 Lua 함수 탐지)

3. bulk_query_retrieval  (scope_json 적용)
   → 16,000개 중 ~800개만 encoding → 20배 빠르고 게임코드 오염 없음
   → sentence-transformers 필수 (pip install sentence-transformers)

4. select_seed_anchors  (scope_json + dedup_max_per_ref=1)
   → 1:1 명확한 seed만 선택

5. build_runtime_suite

6. run_downstream  → propagation Round 1

7. get_mapping_distribution
   → suspicious_threshold=5로 노이즈 명 탐지

8. update_noise_blacklist  → 노이즈 차단
   run_downstream

9. export_trusted_mappings  → IDA rename 대상 추출
   (IDA에서 확인 후 rename)
   batch_register_force_anchors  → 확정 매핑 등록

─── Round N 반복 ───────────────────────────────────────────

10. patch_features_with_confirmed
    → callee/caller에 실제 이름 반영

11. targeted_retrieval  (patched features + seed_anchors)
    → 구조 기반 추가 탐색, embedding 없이 동작

12. select_seed_anchors  (targeted_json 포함)

13. run_downstream  → propagation Round N

14. accepted 증가 없으면 수렴 → 종료
```

### force anchor 등록 후 재실행만 필요한 경우

```
1. batch_register_force_anchors  또는  seed_anchors.json 직접 편집
2. run_downstream
3. read_final_report
```

---

## 설계 원칙

- **복잡한 reasoning은 deterministic script가 담당한다.** MCP는 스크립트를 표준 인터페이스로 노출할 뿐이다.
- **extraction과 analysis는 반드시 분리한다.** Ghidra JVM + embedding 메모리 겹침 방지.
- **MCP는 10번 통합 파이프라인 entrypoint를 노출하지 않는다.** 실전 binary 분석에서 단계를 강제로 분리하기 위한 의도적 제한이다.
- **force anchor는 후속 재실행에도 살아남는다.** `13_select_seed_anchors.py`가 `AUTO_SOURCES` 이외 anchor를 보존하므로 retrieval/seed-selection 재실행 후에도 force anchor는 유지된다.
- **downstream만 재실행 가능.** retrieval 결과가 바뀌지 않은 상황에서 anchor 수정 후 propagation만 빠르게 재실행할 수 있다.
- **게임 코드 혼합 바이너리에는 scope gate 필수.** detect_lua_scope → bulk_query_retrieval(scope_json) → select_seed_anchors(scope_json) 체인이 없으면 게임 코드가 seed를 오염시킨다.
