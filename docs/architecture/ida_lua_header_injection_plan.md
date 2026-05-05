# IDA Lua Header Injection Plan

이 문서는 LangGraph + IDA MCP 경로에서 Lua 바닐라 헤더를 활용해
IDA decompile 품질을 높이고, 그 결과 Local LLM의 함수 판별 정확도를
개선하기 위한 **계획 문서**다.

현재 문서는 구현이 아니라 설계와 주의사항만 다룬다.

## 1. 목표

- IDA decompile 출력에서 `int`, `__int64`, `a1`, `a2` 같은 정보 손실을 줄인다.
- `lua_State *`, `TValue *`, `CallInfo *`, `ZIO *`, `Proto *` 같은
  Lua 내부 타입을 IDA에 주입해 pseudocode 가독성을 높인다.
- `collect_ida_evidence` 단계에서 더 깨끗한 decompile을 확보해
  `llm_verify_candidate`의 semantic 판단 품질을 높인다.
- 이 기능은 retrieval / propagation / sqlite schema와는 별개로,
  **IDA evidence 품질 향상 레이어**로 취급한다.

## 2. 기본 원칙

### 2.1 바닐라 헤더를 우선 사용한다

새로운 독자 헤더를 설계하는 것이 기본 전략이 아니다.

우선순위:

1. **바닐라 Lua 소스의 원본 헤더를 그대로 사용 시도**
2. IDA가 파싱하지 못하는 경우에만
   **원본 헤더에서 필요한 선언만 추린 IDA-friendly adapter 헤더**를 만든다

즉, adapter 헤더가 필요해도 그것은 “새 타입 정의”가 아니라
**원본 Lua 헤더의 얇은 호환본**이어야 한다.

### 2.2 버전별로 분리한다

Lua 5.2 / 5.3 / 5.4 계열은 내부 struct layout, 함수 signature, helper 정의가
조금씩 다를 수 있다.

따라서 헤더 팩은 반드시 Lua version별로 분리한다.

예시:

```text
data/inputs/lua_source_vanilla/
  lua-5.2.4/src/
  lua-5.3.6/src/
  lua-5.4.7/src/
```

### 2.3 release asset로 함께 배포한다

`callgraphs/*.sqlite`와 `retrieval_indexes/**`처럼, 이 헤더 팩도
실행 품질에 영향을 주는 런타임 자산이다.

다만 헤더는 대용량 자산이 아니므로, 후보는 두 가지다.

- **Git tracked asset로 repo에 포함**
- 또는 release asset bundle 안에 포함

현재 방향으로는 **repo tracked asset + release 문서 명시**가 가장 단순하다.

## 3. 왜 필요한가

현재 LangGraph 경로에서 LLM은 IDA decompile을 강하게 참고한다.

타입 정보가 없으면:

- 첫 번째 인자가 `lua_State *L`인지 보이지 않음
- `CallInfo`, `TValue`, `Proto`, `ZIO` 계열 field 접근 의미가 사라짐
- `luaD_*`, `luaV_*`, `luaZ_*`, `luaU_*` 함수의 semantic 구분이 어려워짐

타입 정보가 들어가면:

- 함수 인자의 역할이 드러남
- field offset이 이름으로 보일 수 있음
- pseudocode가 안정적으로 정리됨
- LLM이 candidate name과 decompile semantics를 더 잘 매칭할 수 있음

## 4. 적용 범위

초기 단계에서는 전체 Lua 헤더를 한 번에 넣기보다,
핵심 타입과 핵심 함수 prototype부터 적용한다.

### 4.1 우선 주입할 타입

- `lua_State`
- `TValue`
- `CallInfo`
- `Proto`
- `TString`
- `ZIO`
- `GCObject`

### 4.2 우선 주입할 함수 prototype

- `lua_callk`
- `lua_pcallk`
- `lua_resume`
- `luaD_call`
- `luaD_precall`
- `luaD_pcall`
- `luaV_execute`
- `luaU_undump`
- `luaZ_read`
- `luaZ_fill`

## 5. LangGraph 삽입 위치

가장 자연스러운 위치는 `collect_ida_evidence` 직전, 또는 그 내부 전처리 단계다.

권장 흐름:

1. candidate address resolve
2. target Lua version resolve
3. 해당 version의 헤더 팩을 IDA local type library에 선언
4. 현재 candidate name에 대응하는 known prototype이 있으면 함수 type 적용
5. 그 뒤 `decompile` / `analyze_function`
6. 정제된 evidence를 `llm_verify_candidate`에 전달

즉 개념적으로는 아래와 같다.

```text
plan_ida_verification
  -> prepare_ida_types
  -> collect_ida_evidence
  -> llm_verify_candidate
```

초기 구현은 새 노드를 늘리기보다,
`collect_ida_evidence` 안의 전처리 단계로 넣는 편이 부담이 적다.

## 6. 상태/설정 계획

이 기능을 넣을 때는 `runtime_config.json` 또는 resolved config에서
아래 정도의 필드가 있으면 관리가 편하다.

```json
{
  "graph_config": {
    "enable_ida_type_injection": true,
    "ida_type_injection_mode": "vanilla_headers"
  }
}
```

의미:

- `enable_ida_type_injection`
  - 타입 주입 기능 on/off
- `ida_type_injection_mode`
  - 기본값은 `vanilla_headers`
  - 필요하면 fallback으로 adapter/minimal 모드를 둘 수 있음

## 7. IDA 적용 전략

IDA MCP 도구 기준으로는 다음 흐름을 계획한다.

1. `declare_type`
   - 버전별 헤더 팩 선언
2. `type_inspect`
   - 필요한 타입이 실제로 등록됐는지 확인
3. `set_type` 또는 `type_apply_batch`
   - candidate 함수에 prototype 적용
4. `decompile` 또는 `analyze_function`
   - 타입 적용 후 pseudocode 재수집

중요한 점:

- 모든 candidate마다 헤더 전체를 다시 선언하면 비효율적이다.
- **세션당 1회 선언 + 함수별 prototype 적용** 구조가 바람직하다.

## 8. 원본 헤더를 그대로 못 쓸 수 있는 이유

바닐라 Lua 헤더가 있어도 그대로 IDA에 넣으면 파싱 실패가 날 수 있다.

대표 이유:

- include dependency가 깊다
- 구현용 매크로가 많다
- 플랫폼 typedef / compiler attribute가 섞여 있다
- IDA type parser가 일반 C compiler만큼 유연하지 않다

그래서 fallback으로는 아래를 허용한다.

- 원본 헤더에서 필요한 typedef / struct / prototype만 추린
  **IDA adapter 헤더** 생성

단, 이 adapter는 항상 원본 헤더를 근거로 유지해야 한다.

현재 구현 방향은:

1. 버전별 원본 헤더를 재귀적으로 읽는다
2. include / conditional macro를 IDA parser 친화적으로 정리한다
3. 세션당 1회 `declare_type`로 주입한다
4. candidate 함수별로 versioned signature catalog에서 prototype을 조회해 `set_type` 한다

현재 구현 방향은:

1. 버전별 원본 헤더를 재귀적으로 읽는다
2. include / conditional macro를 IDA parser 친화적으로 정리한다
3. 세션당 1회 `declare_type`로 주입한다
4. 바닐라 소스에서 추출한 SQLite signature catalog를 조회한다
5. candidate 함수별로 catalog prototype을 `set_type` 한다

### 8.1 signature catalog

함수 prototype은 더 이상 `_VERSION_SIGNATURES` 같은 하드코딩 dict에 오래 의존하지 않는다.

대신 아래 자산을 사용한다.

- `data/inputs/ida_types/lua_function_signatures.sqlite`

이 DB는 버전별 바닐라 소스에서 Lua 함수 signature를 추출해 만든다.
핵심 목적은:

- 버전별 차이 관리
- IDA `set_type`용 prototype 재사용
- release 자산으로 함께 배포 가능한 런타임 카탈로그 확보

## 9. 기대 효과

- `luaD_*`, `luaV_*`, `luaZ_*`, `luaU_*` 계열 함수 구분 개선
- caller/callee evidence 해석 품질 향상
- rename confidence 상승
- accepted/deferred 중 애매한 케이스 해소

특히 아래 계열에서 체감 효과가 클 가능성이 높다.

- `luaD_call`, `luaD_precall`, `luaD_pcall`
- `luaV_execute`, `luaV_finish*`
- `luaU_undump`
- `luaZ_read`, `luaZ_fill`

## 10. 주의사항

### 10.1 버전 mismatch를 절대 허용하지 않는다

`Lua_536` 바이너리에 `Lua_547` 헤더를 넣으면
오히려 decompile이 더 misleading해질 수 있다.

이 기능은 반드시 현재 runtime config에서 resolve한
`target_lua_version`을 기준으로 동작해야 한다.

### 10.2 sqlite / retrieval과 혼동하지 않는다

이 기능은:

- `reference_callgraph.sqlite`
- retrieval index
- `lua_scope` / propagation schema

를 바꾸는 기능이 아니다.

즉 기존 sqlite 구조 변경 이력과는 별개로 문서화해야 하며,
release note에서도 “IDA type pack 추가”는 별도 항목으로 다뤄야 한다.

### 10.3 자동 rename을 바로 공격적으로 풀지 않는다

타입 정보가 좋아졌다고 해서 곧바로 rename threshold를 크게 낮추는 것은 위험하다.

권장 순서:

1. 타입 주입
2. decompile 품질 향상 확인
3. accepted / confirmed 변화 관찰
4. 필요할 때만 rename policy 완화 검토

## 11. 구현 전 체크리스트

- 바닐라 Lua 5.2 / 5.3 / 5.4 소스 위치 확인
- 각 버전에서 우선 적용할 헤더 파일 목록 선정
- IDA가 그대로 파싱 가능한지 소규모 smoke test
- 필요 시 adapter 헤더 최소 세트 작성
- LangGraph `collect_ida_evidence` 진입 전 hook 위치 설계
- release 문서에 header pack 포함 여부 반영

## 12. 현재 결론

이 기능은 해볼 가치가 충분하다.

다만 구현 방향은 명확해야 한다.

- 바닐라 헤더 우선
- 버전별 분리
- `collect_ida_evidence` 앞단 적용
- 세션당 1회 타입 선언
- 필요 시에만 adapter 헤더
- release와 문서에 자산으로 반영

즉, 이 기능은 “새로운 Lua 타입 시스템을 만드는 일”이 아니라
**기존 바닐라 Lua 타입 정보를 IDA와 LangGraph 경로에서 안정적으로 재사용하는 일**로 정의한다.
