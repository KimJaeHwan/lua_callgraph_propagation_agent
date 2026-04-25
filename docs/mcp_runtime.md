# MCP Runtime

`lua_callgraph_propagation_agent`는 FastMCP 기반 stdio MCP 서버를 제공한다.  
analyst 또는 상위 LLM agent가 파이프라인을 제어하고 deferred case를 해소할 수 있는 표준화된 인터페이스다.

중요한 점: 여기 문서만 중요한 게 아니라, 실제 MCP 서버의 `instructions`와 각 tool의
`description`도 LLM에게 직접 노출된다. 그래서 툴 설명은 단순 주석이 아니라
"이 tool을 언제 쓰고, 언제 쓰면 안 되는지"를 알려주는 실행 힌트 역할을 한다.

처음 쓰는 사람이라면 이 문서 전에 아래 두 문서를 먼저 보는 편이 더 쉽다.

- [mcp_quickstart_guide.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_quickstart_guide.md)
- [mcp_tool_reference.md](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/docs/mcp_tool_reference.md)

## 실행

```bash
python scripts/20_run_mcp_server.py
```

서버 구현 위치:

- `scripts/20_run_mcp_server.py` — entrypoint
- `src/lua_callgraph_propagation_agent/mcp_server.py` — 툴 구현

---

## 툴 목록

### 실행 툴

#### `extract_query_features`
단일 바이너리에서 Ghidra feature를 추출해 런타임 workspace에 저장한다.  
MCP에서는 `10_run_name_mapping_pipeline.py`를 감싸는 tool을 일부러 노출하지 않고,
이 tool처럼 단계가 분리된 실행만 제공한다.

```
binary: str          — 대상 .so / ELF 절대 경로
lua_version: str     — 예: 'Lua_547', 'Lua_536'
architecture: str    — 'x86_64' 또는 'aarch64'
session_name: str    — 결과 저장 세션 ID
opt_level: str       — 'O0', 'O2' 등 (기본 'O2')
strip_mode: str      — 'stripped' 또는 'nostrip' (기본 'nostrip')
```

출력: `data/runtime/query_features/<session_name>/extract_manifest.json`

#### `bulk_query_retrieval`
feature manifest 또는 feature JSON에서 모든 함수에 대해 hybrid retrieval top-k를 생성한다.

```
index: str                — retrieval index 디렉터리 경로
                            예: data/inputs/retrieval_indexes/Lua_547/x86_64/runtime
output_json: str          — retrieval_result.json 저장 경로
extract_manifest: str     — extract_manifest.json 경로 (extract_query_features 출력)
query_json: str           — 또는 raw feature JSON 직접 지정
candidate_pool: int       — 후보 풀 크기 (기본 200)
topk: int                 — 반환할 top-k 수 (기본 50)
scoring_mode: str         — 'bonus_v2' 권장
```

#### `select_seed_anchors`
retrieval 결과에서 propagation 시작점이 될 초기 seed anchor를 고른다.  
`13_select_seed_anchors.py`를 감싼 MCP tool이다.

```
retrieval_json: str       — retrieval_result.json 경로
output_json: str          — seed_anchors.json 저장 경로
min_top1_score: float     — top1 최소 점수 (기본 0.92)
min_margin: float         — top1-top2 최소 margin (기본 0.05)
query_json: str           — optional, visible-name anchor 검출용 query feature JSON/manifest
reference_db: str         — optional, visible-name 검증용 SQLite DB
```

#### `build_runtime_suite`
retrieval 결과 + seed anchors + reference DB를 propagation 입력으로 조립한다.  
`14_build_runtime_propagation_suite.py`를 감싼 MCP tool이다.

```
retrieval_json: str            — retrieval_result.json 경로
anchor_json: str               — seed_anchors.json 경로
output_json: str               — runtime_propagation_suite.json 경로
propagation_output_json: str   — 이후 propagation이 쓸 출력 경로
lua_version: str               — reference_db 생략 시 DB 경로 해상도용
reference_db: str              — optional, 명시적 SQLite DB 경로
embedding_project_root: str    — 보통 project root
```

---

### 결과 조회 툴

#### `read_final_report`
최종 보고서 summary(accepted/deferred/conflict 수)와 각 bucket 상위 5개 preview를 반환한다.  
명시적 단계 실행 또는 `run_downstream` 완료 후 빠른 결과 확인용.

```
report_json: str  — final_mapping_report.json 경로
```

#### `read_mapping_record`
`case_id`로 단일 매핑 레코드를 조회한다.  
`case_id` 형식: `<function_name>@<hex_address>` (예: `sub_401234@00401234`)  
retrieval score, graph evidence, status reasons 포함 전체 레코드를 반환한다.

#### `read_propagation_summary`
propagation 중간 결과를 요약한다.  
accepted/deferred/conflict 수 + deferred·conflict 전체 케이스 목록(predicted, reasons 포함)을 반환한다.  
대용량 final_mapping_report.json을 읽지 않고 진행 상황을 빠르게 파악할 때 사용한다.

```
config_path: str  — data/configs/runtime_xxx.json 경로
```

#### `list_deferred_cases`
최종 보고서에서 deferred/conflict 케이스만 추출해 triage용으로 반환한다.  
각 케이스의 case_id, query_func, predicted_function_name, status_reasons를 포함한다.  
IDA에서 어떤 함수를 먼저 decompile할지 결정할 때 사용한다.

```
report_json: str  — final_mapping_report.json 경로
```

---

### Anchor 관리 툴

#### `register_force_anchor`
IDA decompile 분석으로 확정된 단일 매핑을 force anchor로 등록하고,  
`build_suite → propagation → deferred_analysis → final_report`를 자동 재실행한다.

```
config_path: str     — data/configs/runtime_xxx.json 경로
query_func: str      — stripped 함수명 (예: 'sub_401234', 'FUN_00401234')
reference_func: str  — 확정된 Lua 함수명 (예: 'luaD_precall')
reason: str          — 판단 근거 요약 (예: 'decompile shows lua_State arg + luaG_runerror callee')
```

이미 등록된 `query_func`는 에러를 반환한다 (중복 방지).

#### `batch_register_force_anchors`
여러 확정 매핑을 한 번에 등록한 뒤 downstream을 **1회만** 실행한다.  
`register_force_anchor`를 N번 호출하는 것보다 훨씬 효율적이다.  
중복 항목은 조용히 무시(skip)된다.

```
config_path: str
anchors: list[{query_func, reference_func, reason}]
```

예시:
```json
{
  "config_path": "data/configs/runtime_artale_libengine.json",
  "anchors": [
    {"query_func": "sub_1234", "reference_func": "luaD_precall", "reason": "lua_State first arg"},
    {"query_func": "sub_5678", "reference_func": "luaV_execute", "reason": "main dispatch loop"}
  ]
}
```

#### `run_downstream`
`seed_anchors.json`을 변경하지 않고 **build_suite → propagation → deferred_analysis → final_report**만 재실행한다.  
seed_anchors.json을 직접 편집한 후 propagation을 다시 돌릴 때 사용한다.  
retrieval과 seed_selection 단계를 건드리지 않으므로 force anchor가 덮어써지지 않는다.

```
config_path: str
```

---

## 전형적인 워크플로우

### 신규 바이너리 전체 분석

```
1. extract_query_features
   → Ghidra feature 추출. 완전히 종료될 때까지 기다린다.

2. `bulk_query_retrieval`
3. `select_seed_anchors`
4. `build_runtime_suite`
5. `04/05/15` 스크립트 또는 `run_downstream` 성격의 후속 실행

6. read_final_report
   → accepted/deferred/conflict 수 확인

7. list_deferred_cases  또는  read_propagation_summary
   → 미해소 케이스 목록 확인

5. (IDA Pro에서 deferred 케이스 decompile 분석)
   → 호출 패턴, 상수, 문자열 참조로 함수 확정

8. batch_register_force_anchors
   → 확정 매핑 일괄 등록 + downstream 자동 재실행

9. read_final_report
   → 최종 결과 확인
```

### force anchor 등록 후 재실행만 필요한 경우

```
1. (seed_anchors.json 직접 편집 또는 batch_register_force_anchors)
2. run_downstream  → propagation만 재실행 (retrieval 재실행 없음)
3. read_final_report
```

---

## 설계 원칙

- **복잡한 reasoning은 deterministic script가 담당한다.** MCP는 그 스크립트들을 표준 인터페이스로 노출할 뿐이다.
- **extraction과 analysis는 반드시 분리한다.** Ghidra JVM + embedding 메모리 겹침 방지.
- **MCP는 10번 통합 파이프라인 entrypoint를 노출하지 않는다.** 실전 binary 분석에서 오해를 줄이기 위한 의도적 제한이다.
- **force anchor는 후속 재실행에도 살아남는다.** `13_select_seed_anchors.py`가 `AUTO_SOURCES`(retrieval_high_confidence, name_visible) 이외의 anchor를 보존하므로 retrieval/seed-selection 이후 단계만 다시 돌려도 force anchor는 유지된다.
- **downstream만 재실행 가능.** retrieval 결과가 변하지 않은 상황에서 anchor 수정 후 propagation만 빠르게 재실행할 수 있다.
