# Fresh Clone Checklist

이 문서는 **처음 clone 한 직후** 이 프로젝트가 바로 실행 가능한지 확인할 때 보는 짧은 체크리스트다.

## 1. 코드 / Python 환경

- repo clone 완료
- 가상환경 준비
- 프로젝트 설치

```bash
pip install -e .
```

## 2. 꼭 있어야 하는 데이터 자산

### reference DB

아래 중 대상 Lua version에 맞는 파일이 있어야 한다.

```text
data/inputs/callgraphs/Lua_547/reference_callgraph.sqlite
data/inputs/callgraphs/Lua_536/reference_callgraph.sqlite
data/inputs/callgraphs/Lua_524/reference_callgraph.sqlite
```

### retrieval index

대상 Lua version / architecture 조합에 맞는 runtime index가 있어야 한다.

```text
data/inputs/retrieval_indexes/<Lua_version>/<architecture>/runtime/
```

예:

```text
data/inputs/retrieval_indexes/Lua_536/aarch64/runtime/
```

### Lua 함수 시그니처 DB

```text
data/inputs/ida_types/lua_function_signatures.sqlite
```

### 바닐라 Lua 소스

기본 기대 경로:

```text
../lua_custom_engine_generator/lua_source_vanilla
```

다른 위치에 있다면 config의 `tooling.vanilla_lua_source_root`를 바꾸면 된다.

## 3. 실행 모드별 추가 확인

### pre-extracted만 사용할 때

- `user_input.query_feature_json`이 실제로 존재하는지

### binary extraction까지 할 때

- Ghidra / pyghidra 환경이 준비되어 있는지
- extractor가 target binary에 접근 가능한지

## 4. Local LLM / IDA까지 쓸 때

### Local LLM runner (`22`)

- LM Studio endpoint 준비
- 모델 이름 확인

### IDA evidence / rename

- IDA MCP endpoint 준비
- IDA가 target IDB를 연 상태인지 확인

## 5. 자산이 없으면 어떻게 하나

### release / 로컬 자산에서 가져오기

대용량 자산은 Git tracked가 아닐 수 있다.

- retrieval index
- reference callgraph DB

이 둘은 release 자산이나 로컬 준비 자산에서 채워야 한다.

### 생성 스크립트

- reference DB 재생성
  - `scripts/setup/01_build_reference_callgraph_db.py`
- 시그니처 DB 재생성
  - `scripts/setup/02_build_lua_signature_db.py`
- 샘플 runtime asset 복사
  - `scripts/setup/21_prepare_runtime_assets.py`

## 6. 운영용으로 기억할 것

- 서버: `scripts/20_run_mcp_server.py`
- 에이전트: `scripts/22_run_local_llm_agent.py`
- setup/생성: `scripts/setup/`

즉 fresh clone 후에는 먼저 **데이터 자산이 있는지**, 그다음 **LM Studio / IDA MCP / Ghidra 중 필요한 것만 준비되었는지** 보면 된다.
