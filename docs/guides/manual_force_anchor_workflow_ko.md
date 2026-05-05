# Manual Force Anchor Workflow

이 문서는 **평소에는 `22_run_local_llm_agent.py` 하나만 쓰는 흐름**을 빠르게 기억하기 위한 운영 메모다.  
`24_apply_manual_force_anchors.py` 는 남아 있지만, 지금은 주로 디버그/수동 보조용이다.

## 핵심 원칙

- 처음 시작할 때는 `22_run_local_llm_agent.py` 를 쓴다.
- 중간에 진행이 둔화되거나, 사람이 확신한 함수가 생기면 runner를 중지한다.
- 그 다음 [manual_force_anchors.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/manual_force_anchors.json)에 수동 anchor를 추가한다.
- 그 뒤 **같은 `22` 명령을 다시 실행하면**
  - manual force anchor 반영
  - IDA rename/type 반영
  - 필요한 downstream 재계산
  - 자동 resume
  를 알아서 처리한다.

## 1. 처음 시작

```bash
cd /Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper

./lua_llm/bin/python lua_callgraph_propagation_agent/scripts/22_run_local_llm_agent.py \
  --config lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/runtime_config.json \
  --lmstudio-model qwen3.5-27b-claude-4.6-opus-distilled-mlx \
  --lmstudio-base-url http://127.0.0.1:1234/v1 \
  --ida-url http://127.0.0.1:13337/mcp \
  --max-rounds 5
```

## 2. 중간에 수동 확정 넣기

수동으로 확신한 함수는 [manual_force_anchors.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/manual_force_anchors.json)에 넣는다.

형식:

```json
{
  "anchors": [
    {
      "entry_point": "0x4da44c",
      "reference_func": "luaopen_base",
      "reason": "manual_verified"
    }
  ]
}
```

여러 개를 넣고 싶으면 `anchors` 배열에 계속 추가하면 된다.

```json
{
  "anchors": [
    {
      "entry_point": "0x4da44c",
      "reference_func": "luaopen_base",
      "reason": "manual_verified"
    },
    {
      "entry_point": "0x4c2e3c",
      "reference_func": "luaD_call",
      "reason": "manual_verified"
    },
    {
      "entry_point": "0x4d5518",
      "reference_func": "luaV_execute",
      "reason": "manual_verified"
    }
  ]
}
```

## 3. 수동 anchor 반영 후 다시 실행

평소에는 별도 스크립트를 기억할 필요 없이, **수정 후 같은 `22` 명령을 다시 실행**하면 된다.

```bash
cd /Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper

./lua_llm/bin/python lua_callgraph_propagation_agent/scripts/22_run_local_llm_agent.py \
  --config lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/runtime_config.json \
  --lmstudio-model qwen3.5-27b-claude-4.6-opus-distilled-mlx \
  --lmstudio-base-url http://127.0.0.1:1234/v1 \
  --ida-url http://127.0.0.1:13337/mcp \
  --max-rounds 5
```

이 명령은:

- 기존 중간 산출물을 보고 적절한 단계부터 자동 재개한다
- `manual_force_anchors.json` 이 더 최신이면 `build_suite` 부터 다시 시작한다
- manual force anchor를 `seed_anchors.json` 에 반영한다
- IDA rename/type 반영도 같이 시도한다
- 이후 downstream과 analyst loop를 이어서 진행한다

## 4. 그 다음 또 확신한 게 생기면

같은 방식으로 하면 된다.

1. `manual_force_anchors.json`에 새 항목 추가
2. 같은 `22` 명령 다시 실행

즉 지금 운영 기준으로는 **manual anchor가 생길 때마다 `22`를 다시 실행**하면 된다.

## 5. 언제 다시 22를 돌리나

일반 운영 흐름은 보통 이렇다.

1. `22`로 시작
2. plateau가 오면 중지
3. `manual_force_anchors.json` 수정
4. **같은 `22` 명령 다시 실행**

중요:

- 이제 `22`는 기존 출력물이 있으면 자동으로 resume 한다.
- 예를 들어 `final_mapping_report.json`, `seed_anchors.json` 등이 남아 있으면 처음부터 retrieval을 다시 하지 않고 적절한 단계부터 이어서 시작한다.
- `manual_force_anchors.json`이 더 최신이면 `build_suite`부터 다시 시작한다.

## 6. 기억할 최소 명령 하나

처음 시작과 재개가 모두 같은 명령이다.

```bash
./lua_llm/bin/python lua_callgraph_propagation_agent/scripts/22_run_local_llm_agent.py \
  --config lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/runtime_config.json \
  --lmstudio-model qwen3.5-27b-claude-4.6-opus-distilled-mlx \
  --lmstudio-base-url http://127.0.0.1:1234/v1 \
  --ida-url http://127.0.0.1:13337/mcp \
  --max-rounds 5
```

## 7. `24`는 언제 쓰나

대부분은 안 써도 된다. 아래처럼 **정말 수동으로 seed/downstream만 한 번 강제 반영하고 싶을 때**만 쓴다.

```bash
./lua_llm/bin/python lua_callgraph_propagation_agent/scripts/24_apply_manual_force_anchors.py \
  --config lua_callgraph_propagation_agent/data/runtime/results/libengine_lua536_aarch64_agent_rerun/runtime_config.json \
  --ida-url http://127.0.0.1:13337/mcp \
  --run-downstream
```

즉 정리하면:

- 평소 운영: `22`만 사용
- 수동 보조/디버그: 필요할 때만 `24`
