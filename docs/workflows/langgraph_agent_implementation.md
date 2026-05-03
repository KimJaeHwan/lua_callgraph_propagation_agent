# LangGraph Agent Implementation

This package now includes an implementation scaffold for the LangGraph + Local LLM + Lua MCP + IDA MCP design.

## Package location

- `src/lua_callgraph_propagation_agent/langgraph_agent/state.py` — state models and schemas
- `src/lua_callgraph_propagation_agent/langgraph_agent/clients.py` — Lua MCP / IDA MCP sync adapters
- `src/lua_callgraph_propagation_agent/langgraph_agent/lmstudio.py` — LM Studio OpenAI-compatible JSON adapter
- `src/lua_callgraph_propagation_agent/langgraph_agent/confirmed.py` — `query_func` to `entry_point` confirmed-map conversion
- `src/lua_callgraph_propagation_agent/langgraph_agent/reasoner.py` — Local LLM structured verification contract and deterministic fallback
- `src/lua_callgraph_propagation_agent/langgraph_agent/nodes.py` — LangGraph node implementations
- `src/lua_callgraph_propagation_agent/langgraph_agent/graph.py` — graph wiring and router policy
- `scripts/22_run_local_llm_agent.py` — manual orchestrator using the same node/routing policy without requiring `langgraph` at runtime

## Install optional agent dependencies

```bash
pip install -e .[agent]
```

The base package does not require LangGraph. `build_graph()` imports LangGraph lazily and raises a clear error if the optional dependency is missing.

## Adapter contract

Both MCP clients accept a synchronous session object with this shape:

```python
class Session:
    def call_tool(self, name: str, args: dict) -> dict:
        ...
```

or a callable:

```python
def call_tool(name: str, args: dict) -> dict:
    ...
```

This means the implementation can be connected to:

- FastMCP client wrapper
- LangChain MCP tool wrapper
- test fake session
- local direct function dispatcher

## Minimal wiring sketch

```python
from lua_callgraph_propagation_agent.langgraph_agent import (
    AgentStateModel,
    IdaMcpClient,
    LangGraphAgentNodes,
    LocalLlmReasoner,
    LuaMcpClient,
    build_graph,
)

lua_client = LuaMcpClient(lua_session)
ida_client = IdaMcpClient(ida_session)
reasoner = LocalLlmReasoner(model=local_json_llm)
nodes = LangGraphAgentNodes(lua=lua_client, ida=ida_client, reasoner=reasoner)
graph = build_graph(nodes)

initial_state = AgentStateModel(
    config_path="data/configs/runtime_recommended_binary.json",
).to_dict()

result = graph.invoke(initial_state)
print(result["final_summary"])
```

## Local LLM contract

The model object passed to `LocalLlmReasoner` must expose:

```python
class LocalJsonModel:
    def invoke_json(self, prompt: str, schema: dict) -> dict:
        ...
```

The reasoner validates and coerces the result into `VerificationDecision`. If no model is supplied, it uses a conservative deterministic fallback that only accepts candidates when there is enough score + graph/IDA evidence and no contradiction.

## Safety defaults

- `allow_auto_rename=False` by default.
- Force anchors are registered only from accepted `VerificationDecision` objects.
- `ConfirmedMapBuilder` converts verified `query_func -> reference_func` decisions to the `entry_point_hex -> reference_func` map required by `patch_features_with_confirmed`.
- Tool failures are stored in `AgentState.tool_failures` and route to finalization once the configured failure threshold is reached.

## Implementation caveats

- IDA MCP tool names vary by plugin. Use `IdaMcpClient(..., decompile_tool=..., rename_tool=...)` to adapt names.
- For the current project workflow there is also a concrete adapter: `CodexIdaMcpClient`.
- `bulk_query_retrieval` on patched features requires `sentence-transformers` and can be expensive.
- The current workflow consumes the verification queue one item at a time and can continue to the next item on rejection.
