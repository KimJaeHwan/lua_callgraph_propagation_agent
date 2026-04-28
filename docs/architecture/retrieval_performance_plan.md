# Retrieval 성능 개선 정리

## 목적

대형 stripped 바이너리에서도 retrieval 단계가 실무적으로 쓸 만한 시간 안에 끝나도록 줄이는 것이 목표다.

이번 문서는 계획 초안이 아니라, 실제 측정과 반영 결과를 기준으로 정리한 현재 상태 문서다.

## 측정 대상

- 대상 바이너리: `libengine.so`
- query feature 수: `16,154`
- 비교 index: `Lua_547/x86_64/runtime`
- scoring mode: `bonus_v2`
- candidate pool: `100`
- topk: `20`

주의:

- 이 측정은 `Lua_536/aarch64` 레퍼런스 자산이 아직 없어서, 정확도 평가용이 아니라 retrieval 성능 비교용으로 수행했다.
- 동일 query / 동일 index / 동일 옵션으로만 속도를 비교했다.

## 측정 결과

### 1. 원본 baseline

환경:

- Windows에서 관측한 retrieval loop: 약 `43분`
- Mac에서 같은 규모를 임시 index로 재현한 baseline 총 시간: `855.18초` (`14분 15초`)

Mac baseline 세부:

| 단계 | 시간 |
|------|------|
| query encoding | 약 `1분 47초` |
| retrieval loop | 약 `12분 06초` |
| 합계 | `855.18초` |

### 2. 1차 최적화 후

반영한 것:

- `sklearn cosine_similarity` 제거 후 `numpy matmul` 사용
- symbolic token set 재계산 제거
- `SymbolicProfile` 사전 계산
- device 자동 감지 (`cuda -> mps -> cpu`)

결과:

| 단계 | 시간 |
|------|------|
| query encoding | 약 `1분 48초` |
| retrieval loop | 약 `1분 58초` |
| 합계 | `251.13초` |

효과:

- 전체 약 `3.4배` 개선
- retrieval loop만 보면 약 `6.1배` 개선

### 3. FAISS 적용 후

반영한 것:

- `semantic.faiss`가 없으면 `semantic.npy`로부터 자동 생성
- 이후 semantic shortlist는 FAISS `IndexFlatIP` 경로 사용

결과:

| 단계 | 시간 |
|------|------|
| query encoding | 약 `1분 51초` |
| retrieval loop | 약 `1분 20초` |
| 합계 | `216.51초` |

효과:

- baseline 대비 전체 약 `3.95배` 개선
- 1차 최적화 대비 추가 약 `1.16배` 개선

### 4. macOS MPS venv 재검증

별도 Python 3.11 venv를 만들어 다시 측정했다.

결과:

| 단계 | 시간 |
|------|------|
| 합계 | `228.82초` |

중요:

- 이번 실행 로그에서 embedding model은 실제로 `device=cpu`로 올라갔다.
- 즉 이 값은 `MPS 가속 결과`가 아니라 `새 venv에서의 CPU+FAISS 재측정`이다.

## 현재까지 실제로 효과가 검증된 항목

### A. symbolic scoring 최적화

기존 문제:

- candidate마다 `set(query_tokens)`, `set(candidate_tokens)`를 반복 생성
- overlap 계산이 순수 Python 루프에 과도하게 의존

개선:

- `FunctionRecord`에 `symbolic_profile` 저장
- offsets / compares / strings / callees 집합을 1회만 준비
- scoring 시 재사용

효과:

- retrieval loop의 주 병목 제거

### B. semantic / numeric similarity 계산 단순화

기존:

- `sklearn.metrics.pairwise.cosine_similarity`

개선:

- 정규화된 행렬 기준 `numpy matmul`

효과:

- Python/Sklearn 오버헤드 감소
- Windows / macOS 공통 적용 가능

### C. FAISS semantic shortlist

기존:

- `semantic.npy` 전체 스캔

개선:

- `semantic.faiss` 자동 생성 후 `faiss_index.search(...)`

효과:

- semantic 후보 검색 추가 가속
- index 규모가 커질수록 이점 증가

## 순위 변화 영향

### 1차 최적화 vs baseline

- top1 동일: `16104 / 16154`
- 변경: `50`

이 중:

- `34`개는 top1 score가 완전히 같은 tie
- 나머지도 대부분 매우 작은 floating-point 차이

즉 큰 의미의 알고리즘 변화보다는 계산 순서/수치 차이에 가까웠다.

### FAISS 적용 후 vs 1차 최적화

- top1 동일: `15934 / 16154`
- 변경: `220`

이 중:

- `193`개는 score가 완전히 같은 tie
- 나머지도 모두 `1e-6` 이하 수준의 미세한 차이

즉 FAISS로 인해 top1 이름이 달라진 케이스는 있었지만, 대부분 동점 정렬 순서 차이로 보는 게 맞다.

## MPS 현황

코드에는 이미 MPS 자동 감지가 들어가 있다.

하지만 2026-04-24 기준 이 환경에서는:

- macOS `26.3.1`
- Python `3.11.15`
- torch `2.11.0`

조합에서

- `torch.backends.mps.is_built() == True`
- `torch.backends.mps.is_available() == False`

상태였다.

실제 tensor 생성도 실패했다.

- `torch.ones(1, device="mps")`
- `RuntimeError: The MPS backend is supported on MacOS 14.0+`

이건 하드웨어 이슈라기보다, 현재 PyTorch가 `macOS 26.x` 버전 체계를 제대로 처리하지 못하는 호환성 문제로 보는 것이 자연스럽다.

결론:

- MPS 경로는 코드상 준비되어 있음
- 하지만 현재 이 맥 환경에서는 실제 usable 상태가 아님
- 당분간 Mac에서는 `CPU + FAISS + symbolic 최적화`가 현실적인 주 경로

자세한 설정은 [../guides/macos_mps_setup.md](../guides/macos_mps_setup.md)를 참고한다.

## Windows 지원 관점

이번 개선은 Windows도 고려해서 넣었다.

유지한 원칙:

- OS 전용 multiprocessing 전제에 의존하지 않음
- CUDA가 없어도 CPU fallback 가능
- FAISS는 `faiss-cpu`로 동작 가능
- symbolic 최적화는 순수 Python 자료구조/NumPy 기반

즉 현재 반영된 최적화는 Windows/macOS 공통으로 안전하게 가져갈 수 있다.

## 지금 우선순위

### 이미 반영 완료

- symbolic scoring 최적화
- normalized dot similarity
- FAISS auto-build / auto-load
- CUDA/MPS/CPU device auto-detect

### 다음 우선순위

1. 실제 `Lua_536/aarch64` retrieval index 준비
2. 같은 최적화 코드로 Windows 재실측
3. `candidate_pool` 확대가 정확도에 주는 영향 검증
4. macOS 26.x를 제대로 지원하는 PyTorch가 나오면 MPS 재검증

## 최종 판단

현재 속도는 이미 충분히 실용적이다.

- baseline `14분 15초`
- 현재 `3분 37초`

즉 retrieval은 이제 “병목 때문에 못 쓰는 상태”는 아니다.

남은 일은:

- 정확도 향상을 위해 candidate 전략을 더 다듬는 것
- Lua 5.3 / aarch64 같은 실제 target용 레퍼런스 자산을 채우는 것
- 필요할 때만 추가 가속(MPS/CUDA)을 붙이는 것
