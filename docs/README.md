# Documentation Index

`docs/`는 역할별 폴더로 정리되어 있다.

## guides

- [guides/mcp_quickstart_guide.md](guides/mcp_quickstart_guide.md): MCP를 처음 사용하는 사람용 시작 문서
- [guides/extraction_runtime_environment.md](guides/extraction_runtime_environment.md): Ghidra / pyghidra 환경 설정
- [guides/macos_mps_setup.md](guides/macos_mps_setup.md): Apple Silicon MPS 설정
- [guides/release_assets.md](guides/release_assets.md): 릴리스 자산과 배포 메모

## workflows

- [workflows/runtime_pipeline_overview_ko.md](workflows/runtime_pipeline_overview_ko.md): 전체 런타임 흐름 설명
- [workflows/runtime_pipeline_flow.mmd](workflows/runtime_pipeline_flow.mmd): 기본 파이프라인 다이어그램
- [workflows/mcp_ida_analysis_loop.mmd](workflows/mcp_ida_analysis_loop.mmd): MCP + IDA Pro MCP 분석 루프 다이어그램
- [workflows/langgraph_local_llm_ida_automation.mmd](workflows/langgraph_local_llm_ida_automation.mmd): LangGraph + Local LLM + Lua MCP + IDA MCP 자동화 호출 다이어그램
- [workflows/langgraph_agent_object_design.mmd](workflows/langgraph_agent_object_design.mmd): LangGraph AgentState, MCP client, reasoner 객체 설계 다이어그램
- [workflows/langgraph_local_llm_implementation_notes.md](workflows/langgraph_local_llm_implementation_notes.md): LangGraph 실구현 호출 규칙, 상태 갱신, Local LLM 판단 schema
- [workflows/langgraph_agent_implementation.md](workflows/langgraph_agent_implementation.md): 구현 모듈 위치, MCP adapter contract, 최소 wiring 예시
- [workflows/local_llm_runtime_final_design_ko.md](workflows/local_llm_runtime_final_design_ko.md): LM Studio + Lua MCP + IDA MCP 최종 운영 설계와 실행 예시

## mcp

- [mcp/mcp_runtime.md](mcp/mcp_runtime.md): MCP 런타임 구조와 analyst loop
- [mcp/mcp_tool_reference.md](mcp/mcp_tool_reference.md): MCP tool 레퍼런스
- [mcp/mcp_feature_review.md](mcp/mcp_feature_review.md): 구현 현황과 개선 이력

## architecture

- [architecture/callgraph_propagation_agent_design.md](architecture/callgraph_propagation_agent_design.md): propagation 설계 배경
- [architecture/callgraph_store_design.md](architecture/callgraph_store_design.md): callgraph 저장 구조 설계
- [architecture/langgraph_agent_plan.md](architecture/langgraph_agent_plan.md): LangGraph agent 설계
- [architecture/retrieval_performance_plan.md](architecture/retrieval_performance_plan.md): retrieval 성능 계획

## reference

- [reference/input_schema.md](reference/input_schema.md): 입력/출력 JSON 스키마
- [reference/config_field_reference.md](reference/config_field_reference.md): config 필드 정리
- [reference/runtime_validation_and_configs.md](reference/runtime_validation_and_configs.md): 검증 이력과 추천 config

## portfolio

- [portfolio/portfolio_case_study.md](portfolio/portfolio_case_study.md): 포트폴리오 / 면접용 소개 문서
