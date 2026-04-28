# Portfolio Case Study

## Project

Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent` is an analysis runtime for recovering embedded Lua function names from stripped native binaries. It combines static feature extraction, hybrid retrieval, deterministic seed-anchor selection, callgraph-based propagation, and analyst-guided rerun workflows.

This document is written for portfolio and interview use rather than internal pipeline operation.

## One-Line Summary

Built an end-to-end reverse-engineering pipeline that recovers embedded Lua interpreter function names from stripped binaries using retrieval, callgraph propagation, and analyst-in-the-loop reranking.

## Problem

Reverse engineering stripped native binaries is difficult because symbol names are removed.

This becomes harder when the target binary contains:

- an embedded Lua interpreter mixed with non-Lua application code
- incomplete or noisy caller/callee information
- many semantically similar runtime functions
- a need for iterative analyst confirmation rather than one-shot prediction

The practical goal of this project was not only to rank likely matches, but to build a workflow that lets an analyst confirm a subset of mappings, feed those confirmations back into the system, and improve subsequent rounds of recovery.

## What I Built

The project is organized as a deterministic analysis pipeline:

1. Extract function-level static features from a target binary using Ghidra and pyghidra.
2. Run hybrid retrieval over a reference index using structural and semantic signals.
3. Select reliable initial seed anchors from visible names and high-confidence retrieval results.
4. Expand and rerank candidates using accepted caller/callee anchors in a reference callgraph database.
5. Classify mappings into accepted, deferred, or conflict.
6. Let an analyst confirm mappings, patch feature context with confirmed names, and rerun downstream steps.

The system also exposes a FastMCP interface so the workflow can be driven by an analyst or a higher-level agent without collapsing the logic into one opaque script.

## Core Technical Contributions

### 1. Hybrid Retrieval for Stripped Binary Functions

Implemented a retrieval stage that combines multiple signals instead of relying on a single embedding score.

- semantic text built from extracted function features
- symbolic tokens such as strings, compare values, offsets, callees, and callers
- numeric and structural metadata

This helps produce a top-k candidate list even when symbol names are unavailable.

### 2. Deterministic Seed Anchor Selection

Built a seed-selection stage that chooses only high-confidence mappings as starting anchors.

- exact visible-name matches against reference functions
- retrieval-based anchors gated by top-1 score and score margin
- preservation of manually confirmed anchors across reruns

This made propagation safer by preventing weak early guesses from dominating later rounds.

### 3. Callgraph-Based Propagation

Designed a propagation step that uses already accepted caller/callee neighbors as structural evidence.

- anchored neighbors are projected onto reference function names
- candidates are expanded from reference callgraph edges
- each candidate receives graph-based bonuses and penalties
- low-margin or low-evidence cases are deferred instead of forced

This makes the system more conservative and interpretable than a pure one-shot ranking approach.

### 4. Analyst-in-the-Loop Patch-and-Rerun Workflow

One of the most practical parts of the project is the iterative analysis loop.

- trusted mappings can be exported for manual verification
- confirmed mappings can be registered as force anchors
- feature JSON caller/callee names can be patched with confirmed names
- targeted retrieval and downstream propagation can be rerun with stronger structural context

This turned the project from a static scoring script into a usable reverse-engineering workflow.

### 5. MCP Tooling for Operational Use

Wrapped the runtime in a FastMCP server so the pipeline could be driven as a tool-based workflow.

- extraction and analysis stay phase-separated to avoid memory issues
- downstream reruns are exposed as explicit tools
- deferred and conflict cases can be triaged through structured outputs

This is useful to highlight if applying for platform, developer tools, AI tooling, or security engineering roles.

## Technical Challenges

### Mixed-Code Binaries
## 프로젝트

Lua Callgraph Propagation Agent

`lua_callgraph_propagation_agent`는 stripped native binary에 내장된 Lua 인터프리터 함수명을 복원하기 위한 분석 런타임이다. 이 프로젝트는 static feature extraction, hybrid retrieval, deterministic seed-anchor selection, callgraph 기반 propagation, analyst-guided rerun workflow를 하나의 파이프라인으로 결합한다.

이 문서는 내부 운영 문서가 아니라 포트폴리오와 면접 설명에 바로 사용할 수 있도록 정리한 문서다.

Many target binaries contain both Lua VM code and unrelated application code. A naive retrieval pass over all functions introduces heavy noise. I addressed this with a Lua-scope detection stage that narrows retrieval to likely Lua-related functions.
## 한 줄 소개

Stripped binary에 포함된 Lua 인터프리터 함수명을 복원하기 위해 retrieval, callgraph propagation, analyst-in-the-loop reranking을 결합한 end-to-end reverse-engineering 파이프라인을 설계하고 구현했다.

### Noisy Caller/Callee Names
## 문제 정의

Stripped native binary는 심볼 이름이 제거되어 있기 때문에 역공학 난도가 높다.

이 문제는 다음 조건이 겹치면 더 어려워진다.

- Lua 인터프리터 코드와 일반 애플리케이션 코드가 같은 바이너리에 섞여 있음
- caller/callee 이름이 불완전하거나 노이즈가 많음
- 서로 매우 비슷한 runtime 함수가 많음
- one-shot 예측보다 반복적인 analyst 확인과 재실행이 더 현실적임

이 프로젝트의 목표는 단순히 후보를 점수순으로 나열하는 것이 아니었다. 일부 매핑을 사람이 확인하고, 그 결과를 다시 시스템에 반영해 다음 라운드 복원을 더 정확하게 만드는 분석 워크플로우를 구축하는 것이 핵심이었다.

Caller/callee names can be partially stripped, auto-generated, or simply wrong. This creates bias in both retrieval and propagation. To manage that risk, the pipeline treats propagation as evidence accumulation rather than direct feature rewriting by default, and only injects confirmed names through an explicit patch-and-rerun step.
## 내가 만든 것

프로젝트는 다음과 같은 결정론적 분석 파이프라인으로 구성된다.

1. Ghidra와 pyghidra로 바이너리에서 함수 단위 static feature를 추출한다.
2. 구조적 신호와 의미적 신호를 결합한 hybrid retrieval로 top-k 후보를 생성한다.
3. visible name과 high-confidence retrieval 결과에서 초기 seed anchor를 선택한다.
4. accepted caller/callee anchor를 이용해 reference callgraph DB에서 후보를 확장하고 재정렬한다.
5. 각 매핑을 accepted, deferred, conflict로 분류한다.
6. analyst가 일부 결과를 확정하면 feature context를 patch하고 downstream 단계를 다시 실행한다.

또한 FastMCP 인터페이스를 제공해, 이 워크플로우를 하나의 불투명한 스크립트가 아니라 도구 기반 분석 시스템으로 운영할 수 있게 했다.

### Precision vs. Coverage Tradeoff
## 핵심 기술 기여

### 1. Stripped Binary 함수용 Hybrid Retrieval
### 1. Stripped Binary 함수용 Hybrid Retrieval

단일 embedding score에만 의존하지 않고 여러 신호를 결합하는 retrieval 단계를 구현했다.

- 추출된 함수 feature로부터 semantic text 구성
- 문자열, compare 값, offset, callee, caller 같은 symbolic token 사용
- numeric 및 structural metadata 반영

이 덕분에 심볼 이름이 없는 상태에서도 의미 있는 top-k 후보군을 만들 수 있었다.


### 2. Deterministic Seed Anchor Selection

초기 propagation 시작점으로 사용할 매핑을 보수적으로 고르는 seed-selection 단계를 설계했다.

- reference 함수와 정확히 일치하는 visible-name anchor 선택
- top-1 score와 top-2 margin을 함께 보는 retrieval anchor 선택
- 수동으로 확정한 anchor를 rerun 이후에도 보존

이 단계는 약한 초기 추정이 뒤 propagation 전체를 오염시키지 않도록 만드는 안전장치 역할을 했다.


### 3. Callgraph 기반 Propagation

이미 accepted된 caller/callee neighbor를 구조적 근거로 사용하는 propagation 단계를 설계했다.

- anchored neighbor를 reference function name으로 투영
- reference callgraph edge에서 후보를 추가 확장
- graph evidence에 따라 bonus와 penalty 부여
- margin이 낮거나 근거가 약한 경우는 강제로 확정하지 않고 deferred 처리

이 방식은 one-shot ranking보다 더 보수적이고 해석 가능성이 높다.


### 4. Analyst-in-the-Loop Patch-and-Rerun Workflow

이 프로젝트에서 가장 실전적인 부분은 반복 분석 루프다.

- trusted mapping을 export해서 수동 검토 가능
- confirmed mapping을 force anchor로 등록 가능
- feature JSON의 caller/callee 이름을 확정 이름으로 patch 가능
- stronger structural context로 targeted retrieval과 downstream propagation 재실행 가능

이 덕분에 프로젝트는 단순한 점수 계산 스크립트가 아니라 실제 역공학 workflow에 가까운 시스템이 되었다.


### 5. 운영용 MCP Tooling

FastMCP 서버 위에 런타임을 감싸서 파이프라인을 도구 기반 워크플로우로 노출했다.

- extraction과 analysis를 분리해 메모리 충돌 회피
- downstream rerun을 명시적 도구로 제공
- deferred/conflict 케이스를 구조화된 출력으로 triage 가능

이 부분은 platform, developer tools, AI tooling, security engineering 포지션에서 특히 강한 포트폴리오 포인트가 된다.


## 기술적으로 어려웠던 점

### Mixed-Code Binary 문제
### Mixed-Code Binary 문제

많은 타깃 바이너리에는 Lua VM 코드와 무관한 애플리케이션 코드가 함께 들어 있다. 모든 함수에 대해 retrieval을 돌리면 노이즈가 심해진다. 이를 줄이기 위해 likely Lua-related function만 남기는 Lua-scope detection 단계를 도입했다.

- turn an ambiguous reverse-engineering problem into a staged system
### Noisy Caller/Callee Names

caller/callee 이름은 strip되거나, Ghidra 자동 생성 이름일 수 있고, 일부는 잘못된 이름일 수도 있다. 이 노이즈는 retrieval과 propagation 모두에 bias를 만든다. 이를 완화하기 위해 기본 propagation은 원본 feature를 직접 rewrite하지 않고 evidence accumulation 방식으로 동작하게 했고, confirmed name은 명시적인 patch-and-rerun 단계에서만 반영하도록 했다.

- design conservative decision policies for noisy inputs
### Precision과 Coverage의 균형

자동 accept를 너무 많이 하면 propagation drift가 발생한다. 반대로 너무 보수적으로 가면 deferred가 지나치게 많아진다. 그래서 이 파이프라인은 accepted, deferred, conflict 상태를 분리하고, 단일 최고점 후보를 무조건 확정하지 않고 margin 기반 정책을 사용한다.

- think in terms of analyst workflow, not only algorithm output
### Runtime 제약

Ghidra extraction과 embedding retrieval은 메모리 특성이 다르다. 대형 바이너리에서는 한 프로세스 안에서 모든 단계를 묶어 돌리기보다, extraction과 analysis를 운영적으로 분리하는 편이 안정적이었다.

## Resume Bullet Options
## 왜 의미 있는 프로젝트인가

이 프로젝트는 단순히 모델을 호출한 사례가 아니다.

이 프로젝트를 통해 다음 역량을 보여줄 수 있다.

- 모호한 reverse-engineering 문제를 단계적 시스템으로 분해하는 능력
- static analysis, graph reasoning, retrieval, human feedback를 결합하는 능력
- noisy input에 대해 보수적인 decision policy를 설계하는 능력
- 연구형 프로토타입을 실제로 쓸 수 있는 도구로 만드는 능력
- 알고리즘 결과만이 아니라 analyst workflow까지 고려하는 사고방식

### Option A
## 이력서 Bullet 예시

### Option A
### 짧은 버전

- Stripped native binary에 내장된 Lua 인터프리터 함수명을 복원하기 위한 end-to-end reverse-engineering 파이프라인을 설계 및 구현
- Ghidra 기반 feature extraction, hybrid retrieval, callgraph propagation을 결합해 Lua runtime 함수 후보를 구조적으로 재정렬하는 시스템 개발
- FastMCP 기반 인터페이스를 구축해 retrieval, propagation, deferred triage, downstream rerun workflow를 도구 형태로 운영 가능하게 구성

### 중간 버전
### 중간 버전

- Stripped ELF binary에 포함된 embedded Lua interpreter 함수명을 복원하기 위해 static feature extraction, hybrid retrieval, deterministic seed-anchor selection, callgraph-based propagation을 결합한 분석 파이프라인을 개발함
- Mixed-code binary에서 발생하는 retrieval contamination 문제를 줄이기 위해 Lua-scope detection과 analyst-in-the-loop patch-and-rerun workflow를 설계하여 정확도와 운영성을 동시에 개선함
- Deferred/conflict case를 강제 확정하지 않고 구조화된 triage 대상으로 분리해 conservative decision policy를 적용하고, confirmed mapping을 후속 라운드 추론에 재반영하는 반복 분석 루프를 구현함

### 강하게 어필하는 버전
### 강하게 어필하는 버전

- Reverse engineering, static analysis, graph reasoning을 결합해 stripped production binary에서 embedded Lua VM 함수명을 복원하는 분석 런타임을 설계·구현함
- Noisy caller/callee context와 mixed application code 환경에서 retrieval-only 접근의 한계를 해결하기 위해 seed-anchor gating, callgraph propagation, targeted rerun 전략을 도입함
- Analyst confirmation을 force anchor와 feature patch로 재활용하는 iterative workflow를 구축해 단발성 예측기가 아닌 실전형 분석 도구로 발전시킴

### 면접 설명용 버전
### 면접 설명용 버전

### 짧은 설명
### 짧은 설명

Stripped binary 안의 Lua 인터프리터 함수명을 복원하는 파이프라인을 만들었습니다. 단순 retrieval만으로는 noisy caller/callee 정보와 mixed-code binary 문제 때문에 안정적인 결과가 나오지 않아, seed-anchor selection, callgraph-based propagation, analyst feedback loop를 함께 설계했습니다. 핵심은 점수만 높이는 게 아니라 신뢰 가능한 분석 workflow를 만드는 것이었습니다.

### 긴 설명
### 긴 설명

처음에는 함수 매칭 문제로 시작했지만, 실제로는 시스템 설계 문제에 가까웠습니다. Ghidra extraction과 retrieval은 런타임 특성이 달라 분리 실행이 필요했고, mixed binary에서는 Lua VM 코드만 우선 scope로 좁혀야 contamination을 줄일 수 있었습니다. 이후 top-1 retrieval 결과를 그대로 믿지 않고 deterministic seed-anchor policy를 만들었고, accepted mapping을 reference callgraph의 structural anchor로 사용해 주변 함수를 재정렬했습니다. 자동 확신이 부족한 경우는 억지로 맞췄다고 하지 않고 deferred로 남겼고, analyst가 일부를 확정하면 feature를 patch해서 다음 라운드 추론에 반영하도록 했습니다. 이 과정을 통해 noisy analysis domain에서 보수적인 자동화를 설계하는 경험을 쌓았습니다.

### Option C
## 면접 데모 흐름 추천

면접이나 포트폴리오 발표에서 이 프로젝트를 설명할 때는 다음 순서가 좋다.

1. 문제 제시: stripped binary에서는 함수 이름이 사라진다.
2. retrieval만으로 왜 부족한지 설명한다.
3. seed-anchor selection과 confidence gating의 필요성을 설명한다.
4. accepted caller/callee neighbor를 이용한 propagation을 보여준다.
5. deferred/conflict를 강제 정답 대신 triage 대상으로 다루는 이유를 설명한다.
6. confirmed mapping을 다음 라운드에 patch-and-rerun으로 반영하는 흐름을 보여준다.
7. 마지막으로 MCP tooling이 analyst workflow를 어떻게 개선하는지 설명한다.

Implemented a FastMCP-based tool interface for a binary analysis runtime, exposing deterministic retrieval, propagation, deferred-case review, and downstream rerun workflows for iterative reverse engineering.
## 직무별 포지셔닝

### Reverse Engineering / Security Research Engineering
### Reverse Engineering / Security Research Engineering

Stripped binary, static analysis, callgraph, triage workflow, analyst feedback를 강조하는 것이 좋다.


### Applied ML for Code / Binary Analysis

Hybrid retrieval, structural reranking, weak-signal integration, evaluation tradeoff를 강조하는 것이 좋다.


### Developer Tools / AI Tooling

Deterministic pipeline design, MCP tooling, structured output, iterative human-in-the-loop workflow를 강조하는 것이 좋다.


### Backend / Platform Engineering

Pipeline decomposition, failure isolation, data flow across stages, reproducible rerun 설계를 강조하는 것이 좋다.


## 솔직하게 말할 수 있는 한계

포트폴리오에서는 한계를 숨기기보다, tradeoff를 이해하고 있었다는 점을 보여주는 것이 오히려 좋다.

- 여러 heuristic threshold와 scoring rule을 사용한다.
- 범용 binary name recovery보다는 특정 문제 도메인에 맞춘 시스템이다.
- 정확도는 reference coverage, feature quality, callgraph cleanliness에 영향을 받는다.
- 가장 강한 workflow는 완전 자동이 아니라 iterative analyst-assisted 방식이다.

이 한계들은 precision과 operability를 우선한 설계 선택으로 설명할 수 있다.


## 최종 포지셔닝 문장

이 프로젝트는 stripped binary라는 어려운 환경에서, static analysis, retrieval, graph reasoning, iterative human feedback를 결합해 embedded Lua interpreter 함수명을 복원하는 실전형 reverse-engineering 및 analysis-tooling 시스템으로 소개하는 것이 가장 적절하다.

The project started as a function matching problem, but in practice it became a systems problem. I had to separate feature extraction from retrieval because of runtime constraints, restrict retrieval scope to likely Lua functions to reduce contamination, and then avoid over-trusting top-1 retrieval results by introducing deterministic anchor policies. After that I used accepted mappings as structural anchors in a reference callgraph to rerank nearby functions. When automatic confidence was not strong enough, the system deferred the case instead of forcing a guess. I also built a patch-and-rerun workflow so confirmed mappings could improve later rounds. That taught me a lot about building conservative automation for noisy analysis domains.

## Suggested Demo Flow

If demonstrating this project during an interview or portfolio walkthrough, a good order is:

1. Show the problem: stripped binary with missing names.
2. Explain why retrieval alone is insufficient.
3. Show seed-anchor selection and why confidence gating matters.
4. Show propagation using accepted caller/callee neighbors.
5. Show deferred/conflict outputs instead of forced predictions.
6. Show how confirmed mappings are patched back into later rounds.
7. End with the MCP tooling layer and why it improves analyst workflow.

## Recommended Positioning By Role

### Reverse Engineering / Security Research Engineering

Emphasize stripped binaries, static analysis, callgraphs, triage workflow, and analyst feedback.

### Applied ML for Code / Binary Analysis

Emphasize hybrid retrieval, structural reranking, weak-signal integration, and evaluation tradeoffs.

### Developer Tools / AI Tooling

Emphasize deterministic pipeline design, MCP tooling, structured outputs, and iterative human-in-the-loop workflows.

### Backend / Platform Engineering

Emphasize pipeline decomposition, failure isolation, data flow across stages, and reproducible reruns.

## Honest Limitations

For portfolio use, it is stronger to state the limitations clearly.

- The pipeline uses several heuristic thresholds and scoring rules.
- It is designed for a specific problem domain rather than universal binary name recovery.
- Accuracy depends on reference coverage, feature quality, and the cleanliness of callgraph context.
- The strongest workflow is iterative and analyst-assisted rather than fully automatic.

These are acceptable limitations if explained as deliberate tradeoffs for precision and operability.

## Final Positioning Statement

This project is best presented as a practical reverse-engineering and analysis-tooling system that combines static analysis, retrieval, graph reasoning, and iterative human feedback to solve a difficult real-world naming problem in stripped binaries.