# MCP Tool Reference

`lua_callgraph_propagation_agent` MCP tool reference (v0.6.0 기준).

각 tool이 "무엇을 하는지", "언제 써야 하는지", "입력은 무엇인지"를 빠르게 확인하기 위한 레퍼런스다.

> MCP는 `scripts/10_run_name_mapping_pipeline.py`를 감싸는 tool을 **의도적으로 노출하지 않는다**.  
> extraction과 analysis를 한 프로세스에 묶으면 Ghidra JVM + embedding 메모리가 겹쳐 OOM이 발생할 수 있기 때문이다.

---

## 분석 준비 계열

### `extract_query_features`

바이너리 하나에서 Ghidra feature를 추출한다.

| 파라미터 | 설명 |
|---------|------|
| `binary` | 대상 .so / ELF 절대 경로 |
| `lua_version` | 예: `Lua_547`, `Lua_536` |
| `architecture` | `x86_64` 또는 `aarch64` |
| `session_name` | 결과 저장 세션 ID |
| `opt_level` | 기본 `O2` |
| `strip_mode` | `stripped` 또는 `nostrip` (기본 `nostrip`) |

출력: `data/runtime/query_features/<session_name>/extract_manifest.json`

---

### `detect_lua_scope`

문자열 신호(Lua 런타임 에러 메시지, 메타메서드 이름 등) BFS로 Lua VM 함수를 탐지한다.  
게임 엔진처럼 Lua VM + 게임 코드가 혼합된 바이너리에서 **반드시 먼저 실행**해야 한다.  
`bulk_query_retrieval`과 `select_seed_anchors`에 `scope_json`으로 전달하면 게임 코드 오염을 차단한다.

| 파라미터 | 설명 |
|---------|------|
| `query_json` | 추출된 feature JSON |
| `output_json` | `lua_scope.json` 저장 경로 |
| `bfs_depth` | BFS 홉 수 (기본 4) |
| `max_pcode_instructions` | 이 이상 크면 게임 코드로 간주해 제외 (기본 8000) |
| `min_string_signals` | Lua 시그널 최소 개수 (기본 1) |

출력: 함수별 `{confidence: high/medium/low, reason, hop}` 맵

---

### `bulk_query_retrieval`

모든 query 함수에 대해 hybrid retrieval top-k를 생성한다.  
`scope_json`을 주면 Lua 스코프 함수만 encoding → 16k → ~800개로 줄어 **20배 빠르고 오염 없음**.

| 파라미터 | 설명 |
|---------|------|
| `index` | retrieval index 디렉터리 |
| `output_json` | `retrieval_result.json` 저장 경로 |
| `extract_manifest` / `query_json` | 둘 중 하나 필수 |
| `candidate_pool` | 후보 풀 크기 (기본 200) |
| `topk` | 반환 top-k 수 (기본 50) |
| `scoring_mode` | `bonus_v2` 권장 |
| `scope_json` | `detect_lua_scope` 출력. 혼합 바이너리에 강력 권장 |
| `scope_min_confidence` | `low` / `medium` / `high` (기본 `low`) |

> **주의**: `sentence-transformers` 미설치 시 즉시 crash.  
> `pip install sentence-transformers` 후 실행할 것.

---

### `targeted_retrieval`

확정 앵커의 callgraph 이웃만 후보로 검색한다. **embedding 모델 불필요**.  
vote_score = 해당 후보에 동의하는 확정 이웃 수 / 전체 확정 이웃 수 (0.0~1.0).  
Round 1 propagation 이후 anchors가 쌓인 다음 실행하면 효과적이다.

| 파라미터 | 설명 |
|---------|------|
| `query_json` | patched feature JSON (실제 이름 반영된 것 권장) |
| `anchors_json` | `seed_anchors.json` 또는 `propagation_result.json` |
| `reference_db` | reference callgraph SQLite |
| `output_json` | `targeted_retrieval.json` 저장 경로 |
| `topk` | 케이스당 최대 후보 수 (기본 10) |
| `min_vote_score` | 결과 포함 최소 점수 (기본 0.0 = 전부) |
| `min_voters` | 최소 확정 이웃 수 (기본 1) |
| `lua_version` | reference DB edges 필터 (예: `Lua_536`) |

---

### `select_seed_anchors`

retrieval 결과 + targeted 결과에서 propagation seed를 선택한다.  
dedup-first(같은 ref 이름 중복 제거) + scope gate + 노이즈 차단이 내장되어 있다.

| 파라미터 | 설명 |
|---------|------|
| `retrieval_json` | `retrieval_result.json` 경로 |
| `output_json` | `seed_anchors.json` 저장 경로 |
| `min_top1_score` | retrieval 최소 점수 (기본 0.92) |
| `min_margin` | top1-top2 최소 gap (기본 0.05) |
| `dedup_max_per_ref` | ref 이름 중복 허용 최대치 (기본 1 = 엄격) |
| `scope_json` | Lua 스코프 필터 (`detect_lua_scope` 출력) |
| `scope_min_confidence` | 스코프 최소 신뢰도 (기본 `low`) |
| `targeted_json` | `targeted_retrieval` 출력 (선택) |
| `targeted_min_score` | targeted 최소 vote_score (기본 0.75) |
| `targeted_min_margin` | targeted 최소 margin (기본 0.15) |
| `query_json` / `reference_db` | visible-name anchor 탐지용 (선택) |

---

### `build_runtime_suite`

retrieval + seed anchor + reference DB를 propagation 입력으로 조립한다.

| 파라미터 | 설명 |
|---------|------|
| `retrieval_json` | `retrieval_result.json` |
| `anchor_json` | `seed_anchors.json` |
| `output_json` | suite JSON 저장 경로 |
| `propagation_output_json` | propagation 출력 경로 |
| `lua_version` | reference DB 경로 자동 해석용 |
| `reference_db` | 명시적 SQLite 경로 (선택) |

---

## 결과 조회 계열

### `read_final_report`

accepted / deferred / conflict 수 + 각 bucket 상위 5개 preview.  
모든 downstream 재실행 후 기본 확인 도구.

```
report_json: str
```

---

### `read_propagation_summary`

propagation 결과 중간 요약. deferred/conflict 전체 목록(predicted, reasons) 포함.  
대용량 final_report를 열지 않고 현재 상태를 빠르게 파악할 때 사용.

```
config_path: str
```

---

### `list_deferred_cases`

deferred + conflict 케이스 triage 목록.  
IDA에서 어떤 함수를 먼저 decompile할지 정할 때 사용.

```
report_json: str
```

---

### `read_mapping_record`

`case_id` 하나를 깊게 조회. retrieval score, graph evidence, status reasons 전부 포함.

```
report_json: str
case_id: str      # 형식: FUN_xxx@주소
```

---

### `show_candidate_context`

한 케이스의 모든 정보를 한 번에 묶어 반환.

포함 내용:
- final mapping record
- deferred/conflict triage payload + top candidates
- 현재 등록된 seed/force anchor
- query feature 요약 (strings, callees, callers 등)

force anchor를 넣기 전 마지막 검증 도구.

```
config_path: str
case_id: str
```

---

### `get_mapping_distribution`

accepted mapping에서 reference 이름별 query 함수 집계 히스토그램.  
`suspicious_threshold` 이상 매핑된 이름 = 노이즈 후보.

```
config_path: str
suspicious_threshold: int  # 기본 5
```

반환: `count_distribution`, `suspicious_names` 목록, `high_confidence_1to1_count`

---

### `export_trusted_mappings`

accepted 매핑 중 mapping_count <= max_count 인 것만 추출.  
`max_count=1`이면 1:1 매핑만 (최고 신뢰도).  
IDA rename 작업 대상 리스트로 사용한다.

```
config_path: str
max_count: int         # 기본 1
exclude_prefixes: str  # 기본 "FUN_,sub_"
output_json: str       # 선택, 파일로 저장
```

---

## Anchor / 노이즈 관리 계열

### `register_force_anchor`

단일 확정 매핑 등록 + downstream 자동 재실행.  
IDA decompile로 하나를 확정했을 때 사용.

```
config_path: str
query_func: str      # 예: FUN_004a7141
reference_func: str  # 예: luaopen_base
reason: str          # 판단 근거
```

---

### `batch_register_force_anchors`

여러 확정 매핑을 한 번에 등록. downstream은 **1회만** 실행.  
한 IDA 세션에서 여러 개를 확정했을 때 이걸 쓴다. `register_force_anchor` 반복보다 훨씬 효율적.

```json
{
  "config_path": "...",
  "anchors": [
    {"query_func": "FUN_004a1fae", "reference_func": "luaV_execute", "reason": "main dispatch loop"},
    {"query_func": "FUN_490306",   "reference_func": "luaD_call",    "reason": "lua_State + precall callee"}
  ]
}
```

---

### `remove_force_anchor`

잘못 넣은 force anchor 제거.  
`source="force_anchor"`만 삭제. 자동 seed(retrieval_high_confidence 등)는 건드리지 않는다.

```
config_path: str
query_func: str
rerun_downstream: bool  # 기본 true
```

---

### `run_downstream`

`seed_anchors.json`을 건드리지 않고 build_suite → propagation → deferred_analysis → final_report만 재실행.  
seed를 직접 편집했거나 noise blacklist를 바꾼 뒤 빠르게 재검증할 때 사용.

```
config_path: str
```

---

### `update_noise_blacklist`

suite JSON의 `noise_blacklist`에 이름 추가/제거.  
노이즈 이름이 전파 후보로 올라오는 것을 차단한다.

```
suite_json: str
add: list[str]     # 추가할 이름들
remove: list[str]  # 제거할 이름들
```

---

### `patch_features_with_confirmed`

feature JSON의 callee/caller 목록에서 `FUN_xxx` 이름을 실제 Lua 이름으로 교체.  
patched JSON으로 `bulk_query_retrieval`을 다시 돌리면 callgraph 신호가 크게 강화된다.

```
query_json: str
confirmed_map: dict[str, str]  # {entry_point_hex: real_name}
```

---

## 추천 실행 순서

### Round 1 (초기 분석)

```
1. extract_query_features
2. detect_lua_scope         → lua_scope.json 생성
3. bulk_query_retrieval     → --scope-json 적용 (16k → ~800 encoding)
4. select_seed_anchors      → --scope-json, --dedup-max-per-ref 1
5. build_runtime_suite
6. run_downstream           → propagation
7. get_mapping_distribution → 노이즈 명 탐지
8. update_noise_blacklist + run_downstream
9. export_trusted_mappings  → IDA rename 대상 추출
10. (IDA 확인 후) batch_register_force_anchors
```

### Round N (반복)

```
1. patch_features_with_confirmed
2. targeted_retrieval        → vote 기반 추가 탐색
3. select_seed_anchors       → --targeted-json 포함
4. run_downstream
5. accepted 증가 없으면 수렴 종료
```

### 빠른 재검증

```
1. run_downstream
2. read_final_report
```

---

## 같이 보면 좋은 문서

- [../guides/mcp_quickstart_guide.md](../guides/mcp_quickstart_guide.md)
- [mcp_runtime.md](mcp_runtime.md)
- [../architecture/langgraph_agent_plan.md](../architecture/langgraph_agent_plan.md)
