# MCP Runtime

`lua_callgraph_propagation_agent`는 단일 레포 런타임을 목표로 하며, FastMCP 기반 MCP 인터페이스를 제공한다.

기본 retrieval index는 실험용 slim index가 아니라 이 레포 안에 복사된 full index를 사용한다. slim index는 정확도 저하가 확인되어 실험 결과로만 남겨 두었다.

## 목적

- 여러 개의 numbered script를 MCP 툴로 노출한다.
- 내일 실제 `.so`가 들어왔을 때 CLI와 MCP 둘 다 같은 실행 경로를 사용하게 만든다.
- 사람 또는 상위 agent가 deferred case를 조회하고 force anchor를 주입할 수 있게 만든다.

## 실행

```bash
../lua_llm/bin/python scripts/20_run_mcp_server.py
```

서버 구현 위치:

- `scripts/20_run_mcp_server.py`
- `src/lua_callgraph_propagation_agent/mcp_server.py`

## 현재 제공 툴

- `pipeline_dry_run`
  - config JSON을 받아 전체 파이프라인 명령을 미리 해석한다.
- `pipeline_run`
  - config JSON을 받아 통합 파이프라인을 실행한다.
- `extract_query_features`
  - 대상 바이너리에서 query feature를 추출한다.
- `bulk_query_retrieval`
  - 추출된 feature JSON 또는 manifest를 기반으로 retrieval top-k를 생성한다.
- `list_deferred_cases`
  - deferred/conflict case를 analyst가 보기 좋게 요약한다.
- `register_force_anchor`
  - analyst가 확정한 anchor를 입력해 propagation을 다시 돌릴 수 있게 한다.
- `read_final_report`
  - 최종 report의 summary와 일부 preview를 읽는다.
- `read_mapping_record`
  - 특정 `case_id`에 대한 저장된 mapping record를 조회한다.

## 현재 범위

이번 단계의 MCP는 FastMCP 기반 “runtime orchestration layer”에 가깝다.

- 복잡한 reasoning은 기존 deterministic script가 담당한다.
- MCP는 그 스크립트들을 표준화된 tool interface로 묶는다.
- reference DB 재생성이나 runtime asset bootstrap 같은 maintenance 작업은 MCP의 기본 분석 인터페이스에서 제외했다.
- 즉, 지금은 새로운 알고리즘을 넣은 것이 아니라, 실제 운용 가능한 제어면을 만든 것이다.

## 매핑 결과 저장

Propagation으로 확정되거나 보류된 결과는 최종적으로 `final_mapping_report.json`에 저장한다.

- `accepted`, `deferred`, `conflicts`
- `mapping_records`

특히 `mapping_records`는 역검증을 위해 남겨두는 ledger 역할을 한다.

- query 함수 식별 정보
- 최종 예측 함수명
- status / status reasons
- top candidate의 retrieval prior / graph score / final score
- anchor summary
- graph evidence
- optional LLM analysis

즉, 나중에 “왜 이 함수를 이렇게 매핑했는가?”를 다시 추적할 때 `mapping_records`를 기준으로 보면 된다.

## 다음 확장 후보

- `select_seed_anchors`, `build_runtime_suite`, `export_final_report`도 개별 툴로 노출
- `read_deferred_case` 같은 조회형 툴 추가
- 최종적으로는 실제 analyst agent가 MCP 툴만 사용해 end-to-end name mapping을 수행하도록 확장
