# LangGraph Agent Plan — Lua Callgraph Propagation

> **Purpose**: 이 문서는 로컬 LLM(Qwen, DeepSeek, Llama 등)이 LangGraph를 통해  
> Lua 바이너리 함수명 복원 파이프라인을 자율적으로 반복 실행할 수 있도록  
> 노드·엣지·상태·도구 매핑을 정의한다.

---

## 전체 워크플로 개요

```
START
  │
  ▼
[init_state]  ── 기존 결과 있으면 스킵 ──▶ [run_propagation]
  │ (신규 바이너리)                               │
  ▼                                              ▼
[run_retrieval]                         [analyze_distribution]
  │                                              │
  ▼                                      noise ≥ threshold?
[run_propagation]                               │
                                     YES ◀──────┤────▶ NO
                                      │                │
                              [update_noise]    [export_trusted]
                                      │                │
                              [run_propagation] [filter_new_mappings]
                                      │                │
                                      └──────▶ [verify_and_apply]
                                                       │
                                              [patch_features]
                                                       │
                                              [run_propagation]
                                                       │
                                            accepted 증가?
                                       YES ◀────────────┤────▶ NO
                                        │                      │
                              (loop back to                [analyze_deferred]
                               analyze_distribution)           │
                                                      유망한 케이스?
                                                   YES ◀────────┤────▶ NO
                                                    │                  │
                                          [decompile_verify]         [END]
                                                    │
                                          확인되면 force anchor 등록
                                                    │
                                          [run_propagation] ──▶ (loop)
```

---

## LangGraph 상태 스키마

```python
from typing import TypedDict, Optional

class AgentState(TypedDict):
    # 파이프라인 설정
    config_path: str          # 런타임 config JSON 경로
    suite_json: str           # suite.json 경로 (noise_blacklist 포함)
    query_json: str           # 원본 query feature JSON 경로
    patched_query_json: str   # 패치된 query JSON 경로 (없으면 query_json 사용)

    # 진행 상태
    phase: str                # 현재 단계 이름
    propagation_round: int    # 반복 횟수
    last_accepted: int        # 직전 accepted 수
    current_accepted: int     # 현재 accepted 수
    convergence_count: int    # accepted 증가 없이 반복된 횟수

    # 누적 확인 앵커
    confirmed_map: dict       # {entry_point_hex: real_name} (총 누적)
    noise_blacklist: list     # 현재 noise blacklist

    # 마지막 실행 결과 캐시
    last_distribution: dict   # get_mapping_distribution 결과
    last_trusted: list        # export_trusted_mappings 결과
    pending_deferred: list    # 검토할 deferred 케이스 목록

    # 완료 여부
    done: bool
    summary: str              # 최종 요약 메시지
```

---

## 노드 정의

### 1. `init_state`
**역할**: 기존 결과 파일 존재 여부 확인. 결과가 없으면 extraction/retrieval이 필요함을 표시.

**사용 MCP 도구**: `read_final_report` (존재 확인용)

**판단 기준**:
- `final_mapping_report.json` 없음 → `run_retrieval` 노드로
- 있음 → `run_propagation` 노드로 (기존 결과로 재시작)

```python
def init_state(state: AgentState) -> AgentState:
    result = mcp.read_final_report(report_json=...)
    if not result["ok"]:
        state["phase"] = "needs_retrieval"
    else:
        state["current_accepted"] = result["summary"]["accepted"]
        state["phase"] = "resume"
    return state
```

---

### 2. `run_retrieval`
**역할**: Ghidra feature extraction → embedding retrieval → seed anchor 선택.  
sentence_transformers가 설치된 환경에서만 실행 가능.

**사용 MCP 도구**:
- `extract_query_features` (바이너리 → feature JSON)
- `bulk_query_retrieval` (feature JSON → retrieval_result.json)
- `select_seed_anchors` (retrieval → seed_anchors.json)

**주의**: Ghidra JVM과 embedding 모델이 메모리를 공유하면 안 됨.  
반드시 extract → retrieval 순서로 별도 subprocess로 실행.

---

### 3. `run_propagation`
**역할**: 현재 seed_anchors.json을 기반으로 callgraph 전파 실행.

**사용 MCP 도구**: `run_downstream`

**출력**: accepted/deferred/conflict 수 업데이트

```python
def run_propagation(state: AgentState) -> AgentState:
    result = mcp.run_downstream(config_path=state["config_path"])
    state["last_accepted"] = state["current_accepted"]
    state["current_accepted"] = result["updated_summary"]["accepted"]
    state["propagation_round"] += 1
    return state
```

---

### 4. `analyze_distribution`
**역할**: 어떤 reference name이 너무 많은 함수에 매핑됐는지 파악.  
노이즈 후보 목록을 생성.

**사용 MCP 도구**: `get_mapping_distribution`

**판단 기준**:
- `suspicious_names`이 비어있음 → `export_trusted` 노드로
- 있음 → `update_noise` 노드로

```python
def analyze_distribution(state: AgentState) -> AgentState:
    result = mcp.get_mapping_distribution(
        config_path=state["config_path"],
        suspicious_threshold=5,
    )
    state["last_distribution"] = result
    return state

def route_after_distribution(state: AgentState) -> str:
    if state["last_distribution"]["suspicious_names"]:
        return "update_noise"
    return "export_trusted"
```

---

### 5. `update_noise`
**역할**: 과다 매핑된 reference name들을 noise_blacklist에 추가.  
**강도 기준**: query_count ≥ 10인 이름만 자동 추가. 5~9는 검토 후 수동 판단.

**사용 MCP 도구**: `update_noise_blacklist`

```python
def update_noise(state: AgentState) -> AgentState:
    suspicious = state["last_distribution"]["suspicious_names"]
    # 자동 블랙리스트: count >= 10인 것만
    auto_blacklist = [s["reference_name"] for s in suspicious
                      if s["query_count"] >= 10]
    if auto_blacklist:
        result = mcp.update_noise_blacklist(
            suite_json=state["suite_json"],
            add=auto_blacklist,
        )
        state["noise_blacklist"] = result["current_blacklist"]
    return state
```

---

### 6. `export_trusted`
**역할**: 1:1 고신뢰 매핑 추출. 이미 확인된 것들은 필터링.

**사용 MCP 도구**: `export_trusted_mappings`

```python
def export_trusted(state: AgentState) -> AgentState:
    result = mcp.export_trusted_mappings(
        config_path=state["config_path"],
        max_count=1,
        exclude_prefixes="FUN_,sub_",
    )
    all_trusted = result["mappings"]
    # 이미 confirmed_map에 있는 entry_point 제외
    confirmed_eps = set(state["confirmed_map"].keys())
    new_trusted = [
        t for t in all_trusted
        if t["entry_point"] not in confirmed_eps
        and t["final_score"] >= 0.85   # 최소 신뢰도
    ]
    state["last_trusted"] = new_trusted
    return state
```

---

### 7. `filter_new_mappings`
**역할**: 새 trusted 매핑이 있는지 확인. 없으면 deferred 분석으로.

```python
def route_after_export(state: AgentState) -> str:
    if state["last_trusted"]:
        return "verify_and_apply"
    return "analyze_deferred"
```

---

### 8. `verify_and_apply`
**역할**: 새 trusted 매핑을 IDA에 적용하고 force anchor로 등록.

점수 기반 자동 처리 전략:
- `final_score >= 1.0`: 즉시 IDA rename + force anchor 등록
- `0.85 <= score < 1.0`: IDA decompile로 확인 후 적용
- 의심스러운 이름(query_func가 FUN_이 아닌 경우): 스킵

**사용 MCP 도구** (우리 파이프라인):
- `batch_register_force_anchors` — 확인된 것들 일괄 등록

**사용 IDA MCP 도구**:
- `mcp__ida-pro-mcp__rename_function` — IDA에서 함수명 변경
- `mcp__ida-pro-mcp__decompile_function` — 중간 신뢰도 케이스 검증용
- `mcp__ida-pro-mcp__get_callees` — 함수 확인용

```python
def verify_and_apply(state: AgentState) -> AgentState:
    anchors_to_register = []
    new_confirmed = {}

    for mapping in state["last_trusted"]:
        ep = mapping["entry_point"]
        name = mapping["predicted_name"]
        score = mapping["final_score"]
        addr = f"0x{ep}"

        if score >= 1.0:
            # 고신뢰: 즉시 적용
            ida_mcp.rename_function(function_address=addr, new_name=name)
            anchors_to_register.append({
                "query_func": mapping["query_func"],
                "reference_func": name,
                "reason": f"auto_trusted score={score:.4f}",
            })
            new_confirmed[ep] = name

        elif score >= 0.85:
            # 중간 신뢰: decompile 확인
            decompile = ida_mcp.decompile_function(function_address=addr)
            # LLM이 decompile 출력과 reference name을 비교해 판단
            if llm_verify(decompile["code"], name):
                ida_mcp.rename_function(function_address=addr, new_name=name)
                anchors_to_register.append({...})
                new_confirmed[ep] = name

    if anchors_to_register:
        mcp.batch_register_force_anchors(
            config_path=state["config_path"],
            anchors=anchors_to_register,
        )
        state["confirmed_map"].update(new_confirmed)

    return state
```

---

### 9. `patch_features`
**역할**: 누적된 confirmed_map을 query feature JSON에 적용.  
callee/caller 목록의 FUN_xxx를 실제 이름으로 교체.

**사용 MCP 도구**: `patch_features_with_confirmed`

```python
def patch_features(state: AgentState) -> AgentState:
    if not state["confirmed_map"]:
        return state
    result = mcp.patch_features_with_confirmed(
        query_json=state["query_json"],
        confirmed_map=state["confirmed_map"],
    )
    state["patched_query_json"] = result["patched_query_json"]
    return state
```

---

### 10. `check_convergence`
**역할**: accepted 수가 유의미하게 증가했는지 확인.

**수렴 기준**:
- accepted 증가량 < 5이면 `convergence_count += 1`
- `convergence_count >= 3`이면 수렴으로 판단 → `analyze_deferred`로
- 아니면 `analyze_distribution`으로 루프

```python
def check_convergence(state: AgentState) -> str:
    delta = state["current_accepted"] - state["last_accepted"]
    if delta < 5:
        state["convergence_count"] += 1
    else:
        state["convergence_count"] = 0

    if state["convergence_count"] >= 3:
        return "analyze_deferred"
    if state["propagation_round"] >= 20:
        return "end"
    return "analyze_distribution"
```

---

### 11. `analyze_deferred`
**역할**: Deferred 케이스 중 IDA 분석으로 식별 가능한 후보를 선별.  
특히 다음 케이스를 우선 처리:
- `top_candidates`가 있지만 score_margin이 낮은 것 (아슬아슬하게 탈락)
- callgraph 이웃에 이미 알려진 함수가 많은 것

**사용 MCP 도구**:
- `read_propagation_summary` — deferred 목록 가져오기
- `show_candidate_context` — 각 case의 후보 + 컨텍스트

**사용 IDA MCP 도구**:
- `mcp__ida-pro-mcp__get_callers` — 이 함수를 누가 호출하는지
- `mcp__ida-pro-mcp__get_callees` — 이 함수가 뭘 호출하는지
- `mcp__ida-pro-mcp__decompile_function` — 함수 본문

```python
def analyze_deferred(state: AgentState) -> AgentState:
    summary = mcp.read_propagation_summary(config_path=state["config_path"])
    deferred = summary["deferred"]

    # 후보가 있는 deferred 케이스만 선별
    candidates = [d for d in deferred if d.get("predicted")]
    # 최대 10개만 검토
    state["pending_deferred"] = candidates[:10]
    return state

def route_after_deferred(state: AgentState) -> str:
    if state["pending_deferred"]:
        return "decompile_verify"
    return "end"
```

---

### 12. `decompile_verify`
**역할**: 각 deferred 케이스를 IDA decompile로 확인.  
LLM이 decompile 출력을 보고 top candidate 이름이 맞는지 판단.

**판단 기준**:
- 함수 크기, 호출하는 함수 목록, 비교하는 상수값 등
- 이미 확인된 Lua 함수들과의 callgraph 관계

**사용 IDA MCP 도구**:
- `mcp__ida-pro-mcp__decompile_function`
- `mcp__ida-pro-mcp__get_callers`
- `mcp__ida-pro-mcp__get_callees`
- `mcp__ida-pro-mcp__list_strings_filter`

성공 시 → `batch_register_force_anchors` → `run_propagation` → loop  
실패 시 (확인 불가) → 다음 케이스 시도 → 모두 실패 시 `end`

---

### 13. `end`
**역할**: 최종 결과 요약 출력. 수행된 분석 정리.

```python
def end(state: AgentState) -> AgentState:
    state["done"] = True
    state["summary"] = (
        f"완료: {state['propagation_round']}라운드 반복\n"
        f"  accepted: {state['current_accepted']}\n"
        f"  확인된 force anchor: {len(state['confirmed_map'])}개\n"
        f"  noise blacklist: {len(state['noise_blacklist'])}개\n"
    )
    return state
```

---

## LangGraph 그래프 구성 코드 스케치

```python
from langgraph.graph import StateGraph, END

builder = StateGraph(AgentState)

# 노드 등록
builder.add_node("init_state",          init_state)
builder.add_node("run_retrieval",        run_retrieval)
builder.add_node("run_propagation",      run_propagation)
builder.add_node("analyze_distribution", analyze_distribution)
builder.add_node("update_noise",         update_noise)
builder.add_node("export_trusted",       export_trusted)
builder.add_node("verify_and_apply",     verify_and_apply)
builder.add_node("patch_features",       patch_features)
builder.add_node("analyze_deferred",     analyze_deferred)
builder.add_node("decompile_verify",     decompile_verify)
builder.add_node("end",                  end)

# 엣지
builder.set_entry_point("init_state")

builder.add_conditional_edges("init_state", lambda s: (
    "run_retrieval" if s["phase"] == "needs_retrieval" else "run_propagation"
))

builder.add_edge("run_retrieval",        "run_propagation")
builder.add_edge("run_propagation",      "analyze_distribution")

builder.add_conditional_edges("analyze_distribution",
    route_after_distribution,
    {"update_noise": "update_noise", "export_trusted": "export_trusted"}
)

builder.add_edge("update_noise",         "run_propagation")

builder.add_conditional_edges("export_trusted",
    route_after_export,
    {"verify_and_apply": "verify_and_apply", "analyze_deferred": "analyze_deferred"}
)

builder.add_edge("verify_and_apply",     "patch_features")
builder.add_edge("patch_features",       "run_propagation")

builder.add_conditional_edges("run_propagation",
    check_convergence,
    {
        "analyze_distribution": "analyze_distribution",
        "analyze_deferred":     "analyze_deferred",
        "end":                  "end",
    }
)

builder.add_conditional_edges("analyze_deferred",
    route_after_deferred,
    {"decompile_verify": "decompile_verify", "end": "end"}
)

builder.add_edge("decompile_verify",     "run_propagation")
builder.add_edge("end",                  END)

graph = builder.compile()
```

---

## 실행 진입점 예시

```python
initial_state = AgentState(
    config_path="data/configs/runtime_recommended_preextracted.json",
    suite_json="data/runtime/results/artale_libengine_lua536/suite.json",
    query_json="data/runtime/query_features/.../libengine_20260427_102236.json",
    patched_query_json="",
    phase="start",
    propagation_round=0,
    last_accepted=0,
    current_accepted=0,
    convergence_count=0,
    confirmed_map={},
    noise_blacklist=[],
    last_distribution={},
    last_trusted=[],
    pending_deferred=[],
    done=False,
    summary="",
)

result = graph.invoke(initial_state)
print(result["summary"])
```

---

## 도구 우선순위 및 판단 가이드

### 노이즈 판단 기준

| query_count | 판단 | 조치 |
|------------|------|------|
| ≥ 20 | 확실한 노이즈 | 즉시 blacklist 추가 |
| 10~19 | 거의 확실한 노이즈 | blacklist 추가 |
| 5~9 | 의심스러움 | IDA로 몇 개 확인 후 결정 |
| 2~4 | 주의 (caution) | 그대로 두거나 IDA 확인 |
| 1 | 고신뢰 | 그대로 accept |

### 자동 force anchor 적용 기준

| final_score | 조치 |
|------------|------|
| ≥ 1.0 | 즉시 IDA rename + force anchor |
| 0.92~0.99 | IDA callee/caller 확인 후 적용 |
| 0.85~0.91 | IDA decompile 확인 후 적용 |
| < 0.85 | 스킵 (다음 라운드에서 자연스럽게 해결되길 기대) |

### 수렴 후 deferred 분석 전략

1. **callgraph 이웃 확인**: 이미 알려진 함수들 사이에 있는 deferred 함수 우선
2. **크기 기반 힌트**: 함수 크기로 특정 함수 추정 (luaV_execute는 수천 byte)
3. **문자열 기반 힌트**: 에러 메시지, 라이브러리 이름 등으로 추정
4. **호출 패턴**: lua_pcallk를 여러 번 호출하면 게임 코드 로직

---

## 현재 파이프라인 세션 상태 참고 (artale_libengine_lua536)

이 세션에서 수동으로 확인한 내용:

- **총 force anchor**: 199개 (Rounds 1~5)
- **accepted 함수**: ~515개 (Lua VM + 표준 라이브러리 대부분)
- **noise blacklist**: 37개 항목
- **수렴 원인**: `--skip-retrieval` 상태. 재retrieval 시 accepted가 크게 증가 예상
- **미확인 중요 함수**: `luaD_precall` (2:1 caution, 0x4e64e0 또는 0x5962a0)

---

## 의존성

```
# LangGraph 실행 환경
pip install langgraph langchain-core

# 파이프라인 도구
pip install fastmcp sentence-transformers numpy scikit-learn tqdm

# IDA MCP는 별도 IDA Pro + ida-pro-mcp 플러그인 필요
```

---

*이 문서는 `scripts/22_run_local_llm_agent.py` 와 `src/.../mcp_server.py`의 현재 운영 흐름과 함께 유지되어야 한다.*
