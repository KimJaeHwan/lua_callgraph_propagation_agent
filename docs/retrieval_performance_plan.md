# Retrieval 성능 및 정확도 개선 계획

## 현황 (baseline)

대상 바이너리: `libengine.so` (stripped, x86_64, ~8.5MB)  
총 함수 수: 16,769개  
측정 환경: Windows, CPU only (Intel/AMD)

| 단계 | 소요 시간 | 비고 |
|------|-----------|------|
| Batch 인코딩 (embedding) | ~6분 | bge-small-en-v1.5, CPU |
| Retrieval 루프 | ~43분 | 16,769 × 100 candidates, Python 루프 |
| **합계** | **~49분** | |

---

## 병목 분석

### 1. Batch 인코딩
`SentenceTransformer`가 device 지정 없이 CPU로만 실행됨.  
GPU(CUDA/MPS)를 지원하는 라이브러리임에도 활용 못하는 상태.

### 2. Retrieval 루프 (주요 병목)
`_score_candidates` 내부의 symbolic bonus 계산이 순수 Python set 연산:

```
for i in candidate_ids (100개):
    set(query_tokens) & set(candidate_tokens)  # Python loop
```

16,769 함수 × 100 candidates = **167만 번 Python 반복**  
Python 인터프리터 오버헤드 + GIL 점유로 병렬화도 불가.

### 3. candidate pool 크기 제약
정확도를 높이려면 pool을 늘려야 하는데,  
현재는 pool 크기에 비례해 Python 루프도 늘어나므로 늘리기 어려운 구조.

---

## 개선 항목

### Priority 1 — Symbolic bonus 벡터화

**방법**: token 존재 여부를 sparse matrix(함수 × token)로 표현 후 행렬곱으로 overlap 계산

```
현재: for i in candidates: set(q) & set(c)  → 167만 Python 루프
개선: query_sparse @ reference_sparse.T     → C/BLAS 행렬곱 1번
```

**효과**:
- Retrieval 43분 → **2~3분** 예상
- candidate pool 크기가 속도에 거의 영향을 주지 않게 됨
- Mac/Windows 동일하게 적용 가능 (numpy/scipy 공통)

**연관 효과**: Priority 2 (candidate pool 확대)의 전제조건

---

### Priority 2 — Candidate pool 확대 (정확도 향상)

**방법**: Priority 1 완료 후 `--candidate-pool` 100 → 500~1000으로 확대

**현재 딜레마**:
```
pool 크기  │  정확도  │  속도 (현재)
─────────────────────────────────
100        │  낮음    │  43분
500        │  높음    │  ~3.5시간  ← 현실적으로 불가
```

**벡터화 후**:
```
pool 크기  │  정확도  │  속도 (개선 후)
────────────────────────────────────
100        │  낮음    │  ~2분
500        │  높음    │  ~2분      ← pool 크기가 속도에 무관
1000       │  더 높음 │  ~2~3분
```

pool이 커질수록 진짜 정답이 후보에 포함될 확률이 높아지므로 직접적인 정확도 향상.

---

### Priority 3 — GPU 가속 (embedding)

**방법**: `load_embedding_model`에 device 자동 감지 추가

```python
# 감지 우선순위
# 1. CUDA  (Windows/Linux GPU)
# 2. MPS   (Mac M-series)
# 3. CPU   (fallback)
```

**효과**:
- Batch 인코딩 6분 → **30~40초** 예상 (10~15배)
- Mac/Windows 크로스플랫폼 지원

---

### Priority 4 — 멀티프로세싱 (Mac 우선)

**방법**: Retrieval 루프를 `ProcessPoolExecutor`로 함수 단위 병렬 처리

**Mac (fork)**:
- index를 메모리 복사 없이 worker 간 공유 가능
- 16 P-cores (M4 Max) 활용 시 추가 8~12배 가속 가능
- 코드 변경 상대적으로 단순

**Windows (spawn)**:
- worker마다 index pickle 직렬화 비용 발생
- `multiprocessing.shared_memory`로 numpy 행렬 공유 필요
- 코드 변경량 큼, 별도 구현 필요

---

## 개선 후 예상 성능

### Windows (Priority 1~3 적용)

| 단계 | 현재 | 개선 후 |
|------|------|---------|
| 인코딩 | 6분 | ~35초 |
| Retrieval | 43분 | ~2~3분 |
| **합계** | **~49분** | **~3~4분** |

### Mac M4 Max (Priority 1~4 전체 적용)

| 단계 | Windows 현재 | M4 Max 최적화 |
|------|-------------|--------------|
| 인코딩 | 6분 | ~15초 |
| Retrieval | 43분 | ~30초 |
| **합계** | **~49분** | **~1분 이내** |

약 **15~20배** 차이 예상.

---

## 정확도 향상 요약

| 개선 항목 | 정확도 영향 | 속도 영향 |
|-----------|------------|---------|
| Symbolic 벡터화 | 중립 (동일 알고리즘) | 대폭 향상 |
| Pool 확대 (100→500) | **직접 향상** | 벡터화 후 중립 |
| GPU 가속 | 중립 | 인코딩 향상 |
| 멀티프로세싱 | 중립 | 루프 향상 |

핵심은 **벡터화가 pool 확대의 전제조건**이라는 점.  
벡터화 없이 pool만 늘리면 속도가 선형으로 나빠지므로 반드시 같이 진행해야 한다.
