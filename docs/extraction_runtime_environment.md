# Extraction Runtime Environment

이 문서는 `lua_callgraph_propagation_agent`에서 실제 binary를 입력으로 받을 때, `pyghidra` / `Ghidra` 실행 환경을 어떻게 맞췄는지 정리한 문서다.

핵심 목표는 다음과 같다.

- `lua_extract_feature_ghidra`에서 되던 extraction 경로를
- `lua_callgraph_propagation_agent`의 runtime wrapper에서도 최대한 동일하게 재현한다.

## 문제 배경

초기 상태에서는 다음 현상이 있었다.

- `scripts/11_extract_query_features.py`를 직접 CLI로 실행하면 일부 환경에서 성공
- 그러나 `MCP -> pipeline_run -> extract_query_features` 경로에서는 extraction 단계가 실패

즉, extractor 알고리즘 자체보다는 **실행 환경 차이**가 문제였다.

## 확인된 실패 유형

실제 확인한 오류는 다음과 같았다.

### 1. Ghidra LaunchSupport / java_home.save

```text
Command 'java -cp ".../LaunchSupport.jar" LaunchSupport ".../ghidra" -jdk_home -save' returned non-zero exit status 1
```

원인을 추적해보면 Ghidra가 다음 경로에 설정을 쓰려고 했다.

```text
~/Library/ghidra/ghidra_12.0.4_PUBLIC/java_home.save
```

샌드박스/실행 맥락에 따라 이 경로에 쓰기 실패가 발생했다.

### 2. GHIDRA_INSTALL_DIR 미설정

일부 시도에서는 다음 오류도 확인됐다.

```text
Please set the GHIDRA_INSTALL_DIR environment variable
```

즉, `pyghidra.start()`가 Ghidra 설치 경로를 안정적으로 찾지 못하는 경우가 있었다.

### 3. Ghidra file cache / tmp 경로 쓰기 실패

로그에서 다음과 같은 오류가 확인됐다.

```text
/var/tmp/test2000-ghidra/fscache2/.lastmaint (Operation not permitted)
```

이는 Ghidra가 사용자 홈 외에도 임시 캐시 경로에 쓰려다가 막히는 경우였다.

## 적용한 환경 보정

대상 스크립트:

- [11_extract_query_features.py](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/scripts/11_extract_query_features.py)

runtime wrapper는 extractor subprocess를 실행하기 전에 다음 환경을 명시적으로 설정한다.

### 1. Ghidra user home을 runtime workspace 내부로 고정

```text
HOME=<work_dir>/.ghidra_user_home
JAVA_TOOL_OPTIONS=-Duser.home=<work_dir>/.ghidra_user_home
```

중요한 점은 `HOME`만 바꾸는 것으로는 부족했다는 것이다.

실제 테스트에서 `pyghidra.start()`는 Java 쪽 `user.home` 기준으로 `~/Library/ghidra/...`를 해석했다.  
따라서 `JAVA_TOOL_OPTIONS=-Duser.home=...`가 핵심 보정이다.

### 2. Ghidra tmp/cache 경로를 runtime workspace 내부로 고정

```text
TMPDIR=<work_dir>/.ghidra_tmp
TMP=<work_dir>/.ghidra_tmp
TEMP=<work_dir>/.ghidra_tmp
```

이 설정으로 `/var/tmp/test2000-ghidra/...` 대신 runtime workspace 안쪽 임시 경로를 사용하게 유도한다.

### 3. Ghidra / Java 설치 경로를 명시

```text
GHIDRA_HOME=/opt/homebrew/Cellar/ghidra/12.0.4/libexec
GHIDRA_INSTALL_DIR=/opt/homebrew/Cellar/ghidra/12.0.4/libexec
JAVA_HOME=$(/usr/libexec/java_home)
```

이 부분은 `pyghidra.start()`가 설치 위치를 추론하다 흔들리지 않도록 고정해주는 역할이다.

## 검증 결과

### 1. 직접 CLI extraction smoke test

실행 명령:

```bash
cd lua_callgraph_propagation_agent
../lua_llm/bin/python scripts/11_extract_query_features.py \
  --binary ../lua_extract_feature_ghidra/processed_binaries/Lua_547/x86_64/O0/nostrip/lua_lua_547_0000 \
  --lua-version Lua_547 \
  --architecture x86_64 \
  --opt-level O0 \
  --strip-mode nostrip \
  --session-name smoke_fix_0000 \
  --force
```

결과:

- `1095 funcs`
- manifest 생성
- feature JSON 생성

생성 파일:

- [extract_manifest.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/query_features/smoke_fix_0000/extract_manifest.json)
- [x86_64_O0_nostrip_lua_lua_547_0000_20260423_072238.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/query_features/smoke_fix_0000/Lua_547/x86_64/O0/nostrip/x86_64_O0_nostrip_lua_lua_547_0000_20260423_072238.json)

즉, extractor wrapper는 이제 `lua_callgraph_propagation_agent` 내부에서도 독립적으로 동작한다.

### 2. MCP -> pipeline_run -> 실제 binary

실제 binary config:

- [runtime_lua547_x86_processed_binary_0000.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/configs/runtime_lua547_x86_processed_binary_0000.json)

대상 binary:

- [lua_lua_547_0000](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_extract_feature_ghidra/processed_binaries/Lua_547/x86_64/O0/nostrip/lua_lua_547_0000)

환경 보정 이후에는 `MCP -> pipeline_run` 경로에서도 extraction 결과가 실제로 생성되는 것을 확인했다.

생성 파일:

- [extract_manifest.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/query_features/lua547_x86_processed_binary_0000/extract_manifest.json)
- [x86_64_O0_nostrip_lua_lua_547_0000_20260423_072802.json](/Users/test2000/Desktop/01_project/01_AI_Project/03_Lua_Mapper/lua_callgraph_propagation_agent/data/runtime/query_features/lua547_x86_processed_binary_0000/Lua_547/x86_64/O0/nostrip/x86_64_O0_nostrip_lua_lua_547_0000_20260423_072802.json)

즉, 이전의 “MCP pipeline에서는 extraction이 안 된다” 상태는 해소됐다.

## 현재 해석

현재 상태를 정리하면 다음과 같다.

- extractor 로직 문제는 아니다
- 핵심 원인은 `pyghidra` / `Ghidra`의 user home, install dir, tmp/cache 경로였다
- wrapper에서 해당 환경을 명시적으로 고정하면서 extraction 안정성이 개선됐다

남은 과제는 대규모 real-binary case에서 extraction 다음 단계인 retrieval / propagation의 긴 실행 시간을 어떻게 더 잘 관측하고, 실패 시 외부 종료코드와 요약을 더 정확히 반영할지 정리하는 것이다.

## 추천 운영 원칙

- extraction은 항상 `scripts/11_extract_query_features.py` wrapper를 통해 실행한다
- 직접 `vendor/pyghidra_feature_extractor.py`를 runtime에서 호출하지 않는다
- Ghidra 관련 환경은 wrapper가 책임지고 설정한다
- real-binary end-to-end 검증 시에는 먼저 extraction smoke test로 환경을 확인한 뒤 전체 `pipeline_run`으로 넘어간다
