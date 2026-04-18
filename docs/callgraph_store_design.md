# Call Graph Store Design

이 문서는 `lua_callgraph_propagation_agent`에서 call graph를 어떻게 저장하고 조회할지 정리한다.

결론부터 말하면, 실제 reference call graph store는 JSON이 아니라 SQLite 기반 edge-list로 관리한다. JSON은 작은 fixture와 디버깅용 입력 포맷으로 유지하고, propagation scoring에서 반복 조회되는 vanilla reference graph는 SQLite로 변환해 사용한다.

## 1. 왜 Embedding이 아닌 DB인가

Call graph는 함수 의미를 자연어처럼 embedding해서 검색할 대상이 아니라, `src -> dst` 호출 관계를 정확히 조회하고 비교해야 하는 구조화 데이터다.

`lua_function_embedding`의 역할은 단일 함수 feature 기반으로 후보 top-k를 만드는 것이다. 반면 call graph propagation은 후보가 주변 caller/callee 문맥과 일치하는지 검증하고, 이미 확정된 anchor mapping을 주변 함수로 전파하는 단계다.

```text
FAISS / embedding index
  - 함수 후보 검색
  - symbolic/numeric/semantic feature 기반 top-k 생성

SQLite call graph store
  - vanilla reference graph 저장
  - query graph 저장 또는 임시 로딩
  - caller/callee edge 조회
  - architecture/optimization별 reference 비교

Propagation scorer
  - retrieval_prior를 입력으로 받음
  - SQLite에서 reference neighborhood 조회
  - query graph와 anchor mapping을 비교
  - graph_score와 status를 계산
```

## 2. 왜 SQLite인가

Call graph를 트리로 저장하려고 하면 설계가 복잡해진다. 하지만 call graph는 트리라기보다 방향 그래프다. 한 함수가 여러 함수를 호출할 수 있고, 한 함수가 여러 caller에게 호출될 수 있으며, 재귀와 순환도 가능하다.

SQLite에서는 이를 nested tree가 아니라 edge-list로 저장한다. 이 방식은 단순하고, 조회가 빠르며, 파일 하나로 관리된다. Python 기본 라이브러리 `sqlite3`만으로 사용할 수 있어 추가 인프라가 필요 없다.

SQLite를 선택하는 이유:

- `src -> dst`, `dst <- src` 조회가 핵심이라 edge-list가 잘 맞는다.
- architecture, optimization, strip mode별 필터링이 쉽다.
- O0 primary reference와 O1/O2/O3/Os auxiliary reference를 한 파일에 함께 둘 수 있다.
- JSON 전체를 매번 로드하지 않아도 된다.
- graph DB까지 도입할 만큼 데이터 규모나 질의가 복잡하지 않다.

## 3. 저장 정책

Reference call graph는 다음 정책으로 관리한다.

```text
Primary reference:
  - architecture별 O0 nostrip vanilla graph
  - 함수 경계와 호출 관계가 가장 덜 뭉개진 기준 그래프로 사용

Auxiliary tolerance reference:
  - architecture별 O1/O2/O3/Os nostrip vanilla graph
  - inline, 최적화, helper split/merge로 인한 edge 변화를 보정하는 참고 그래프

Query graph:
  - 분석 대상 바이너리에서 추출한 feature JSON으로 생성
  - 작은 실행에서는 JSON으로 로드해도 되고, 반복 평가에서는 SQLite에 함께 적재할 수 있음
```

중요한 점은 모든 최적화 버전을 embedding 후보 데이터셋에 섞지 않는 것이다. 동일 함수 변형이 top-k를 과점할 수 있기 때문이다. 최적화별 call graph는 retrieval 후보 생성이 아니라 graph scoring tolerance에 사용한다.

## 4. SQLite Schema

초기 스키마는 단순하게 유지한다.

```sql
CREATE TABLE functions (
  function_id TEXT PRIMARY KEY,
  function_name TEXT NOT NULL,
  graph_role TEXT NOT NULL,
  lua_version TEXT,
  architecture TEXT,
  opt_level TEXT,
  strip_mode TEXT,
  entry_point TEXT,
  source_json TEXT
);

CREATE TABLE edges (
  edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  src_name TEXT NOT NULL,
  dst_name TEXT NOT NULL,
  edge_type TEXT NOT NULL DEFAULT 'calls',
  graph_role TEXT NOT NULL,
  lua_version TEXT,
  architecture TEXT,
  opt_level TEXT,
  strip_mode TEXT,
  source_json TEXT
);

CREATE UNIQUE INDEX idx_edges_unique
ON edges(src_id, dst_id, edge_type, graph_role, lua_version, architecture, opt_level, strip_mode);

CREATE INDEX idx_edges_src ON edges(src_id);
CREATE INDEX idx_edges_dst ON edges(dst_id);
CREATE INDEX idx_edges_src_name_env ON edges(src_name, lua_version, architecture, opt_level, strip_mode);
CREATE INDEX idx_edges_dst_name_env ON edges(dst_name, lua_version, architecture, opt_level, strip_mode);
CREATE INDEX idx_functions_name_env ON functions(function_name, lua_version, architecture, opt_level, strip_mode);
```

`graph_role`은 `reference` 또는 `query`를 사용한다. 초기 reference DB는 `reference` 중심으로 만들고, query graph는 실행 시 JSON에서 임시 로드해도 된다.

## 5. Function ID 규칙

SQLite 내부에서는 reference의 최적화별 변형을 구분할 수 있도록 환경 정보를 포함한 stable id를 사용한다.

```text
ref::<lua_version>::<architecture>::<opt_level>::<strip_mode>::<function_name>
query::<entry_point_or_function_name>
```

예:

```text
ref::Lua_547::x86_64::O0::nostrip::luaV_execute
ref::Lua_547::x86_64::O3::nostrip::luaV_execute
query::00119970
```

사용자에게 노출되는 mapping 결과에서는 필요하면 단순한 `ref::Lua_547::luaV_execute` 형태를 함께 제공할 수 있다. 그러나 DB에서는 O0/O1/O2/O3/Os reference를 동시에 저장해야 하므로 opt level을 포함한다.

## 6. 대표 조회

특정 함수의 callee 조회:

```sql
SELECT dst_name
FROM edges
WHERE graph_role = 'reference'
  AND lua_version = 'Lua_547'
  AND architecture = 'x86_64'
  AND opt_level = 'O0'
  AND strip_mode = 'nostrip'
  AND src_name = 'luaV_execute';
```

특정 함수의 caller 조회:

```sql
SELECT src_name
FROM edges
WHERE graph_role = 'reference'
  AND lua_version = 'Lua_547'
  AND architecture = 'x86_64'
  AND opt_level = 'O0'
  AND strip_mode = 'nostrip'
  AND dst_name = 'luaD_precall';
```

특정 edge가 최적화별로 관찰되는지 확인:

```sql
SELECT DISTINCT opt_level
FROM edges
WHERE graph_role = 'reference'
  AND lua_version = 'Lua_547'
  AND architecture = 'x86_64'
  AND strip_mode = 'nostrip'
  AND src_name = 'luaV_execute'
  AND dst_name = 'luaD_precall';
```

이 질의는 O0에 없는 edge라도 O2/O3에 존재하는지 확인하는 tolerance signal로 사용할 수 있다.

## 7. Propagation Scoring에서의 사용

Propagation scorer는 retrieval 결과를 다음처럼 해석한다.

```text
retrieval_prior:
  - lua_function_embedding에서 받은 점수
  - 이 프로젝트에서 재정의하지 않음

graph_score:
  - DB에서 조회한 caller/callee evidence 기반 보정 점수
  - O0 primary reference 일치 시 강한 bonus
  - O1/O2/O3/Os auxiliary reference에서만 일치하면 약한 bonus
  - missing edge는 최적화 가능성을 고려해 강한 penalty로 처리하지 않음
  - extra edge는 즉시 reject하지 않고 custom_suspected signal로 저장
```

예:

```text
query::00119970 retrieval candidates:
  1. llex          retrieval_prior=0.711
  2. luaV_execute  retrieval_prior=0.709

anchor mapping:
  query::0011A010 -> luaD_precall
  query::0011B020 -> luaT_trybinTM

query edges:
  query::00119970 -> query::0011A010
  query::00119970 -> query::0011B020

reference O0 edges:
  luaV_execute -> luaD_precall
  luaV_execute -> luaT_trybinTM
```

이 경우 retrieval 점수는 `llex`가 근소하게 앞서지만, call graph evidence는 `luaV_execute`를 지지한다. Scorer는 `luaV_execute`에 graph bonus를 부여해 최종 후보를 재랭킹할 수 있다.

## 8. 산출물 위치

초기 구현에서 사용할 위치:

```text
data/inputs/callgraphs/reference_callgraph.sqlite
data/inputs/callgraphs/query_callgraph.sqlite        # optional
data/eval/fixtures/*.json                            # small fixture only
```

대량 생성된 SQLite DB는 재생성 가능한 산출물이므로 기본적으로 Git에 올리지 않는다. 작은 fixture JSON은 스키마와 테스트 의도를 보여주기 위해 Git에 포함한다.

## 9. 생성 명령

바닐라 feature JSON에서 reference DB를 생성한다.

```bash
python3 scripts/01_build_reference_callgraph_db.py --replace
```

기본 입력:

```text
../lua_extract_feature_ghidra/outputs_vanilla
```

기본 출력:

```text
data/inputs/callgraphs/reference_callgraph.sqlite
```

대상 feature JSON만 확인하려면 다음 명령을 사용한다.

```bash
python3 scripts/01_build_reference_callgraph_db.py --list-only
```

## 10. Scoring MVP 실행

생성된 `reference_callgraph.sqlite`를 사용해 retrieval 후보를 call graph evidence로 재랭킹한다.

```bash
python3 scripts/02_score_with_callgraph.py \
  --expected query::00119970=luaV_execute \
  --output-json data/eval/fixtures/result_callgraph_minimal.json
```

Minimal fixture의 의도는 retrieval 점수만으로는 `llex`가 근소하게 앞서지만, anchor callee evidence로 `luaV_execute`를 승격시키는 것이다.

```text
retrieval top-1:
  llex

propagation top-1:
  luaV_execute

graph evidence:
  luaV_execute -> luaD_precall
  luaV_execute -> luaT_trybinTM
```

## 11. 실제 Retrieval 결과 평가

`lua_function_embedding`의 `result_dir_index.json`를 입력으로 받아 실제 8개 평가 케이스에 callgraph score correction을 적용한다.

```bash
python3 scripts/03_eval_hybrid_callgraph_cases.py \
  --suite data/eval/cases/hybrid_callgraph_lua547_eval.json
```

현재 평가는 query feature에 남아 있는 caller/callee 이름 중 vanilla reference DB에 존재하는 이름을 temporary anchor로 사용한다. 이 정책은 통합 파이프라인 검증용이며, 완전 블라인드 평가는 아니다.

결과:

```text
retrieval_top1_accuracy   = 0.75
propagation_top1_accuracy = 0.875
improved                  = 1
regressed                 = 0
```

이 결과는 callgraph score가 retrieval 후보를 생성하는 것이 아니라, retrieval 후보 안에서 재랭킹한다는 점을 보여준다. expected function이 후보 목록에 없는 케이스는 이 단계만으로 복구할 수 없다.
