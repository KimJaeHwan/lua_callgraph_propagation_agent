# macOS MPS Setup

`lua_callgraph_propagation_agent`를 Apple Silicon Mac에서 빠르게 돌리기 위한 전용 가이드다.

목표:

- Python 3.11 기반 가상환경 생성
- `sentence-transformers` / `torch` / `faiss-cpu` 설치
- PyTorch MPS 활성화 확인
- retrieval 성능 가속 준비

## 왜 별도 환경이 필요한가

현재 기본 개발 venv는 Python 3.14 기반일 수 있다. 이 경우 PyTorch가:

- `torch.backends.mps.is_built() == True`
- `torch.backends.mps.is_available() == False`

상태가 될 수 있다.

Apple Silicon + MPS는 상대적으로 Python 3.11 환경이 더 안정적이어서, 맥 전용 retrieval 환경을 별도 venv로 두는 편이 실무적으로 안전하다.

다만 Python 3.11로 바꿔도 끝이 아니다. 실제로는:

- `torch.backends.mps.is_built()`
- `torch.backends.mps.is_available()`
- `torch.ones(1, device="mps")`

세 단계가 모두 통과해야 진짜 usable한 환경이다.

## 권장 경로

프로젝트 루트 기준:

- venv: `../lua_llm_mps`
- requirements: [../../requirements-macos-mps.txt](../../requirements-macos-mps.txt)

## 빠른 시작

```bash
cd lua_callgraph_propagation_agent
python3 scripts/setup/00_setup_macos_mps_environment.py
```

기본 동작:

1. `python3.11` 탐지
2. `../lua_llm_mps` venv 생성
3. requirements 설치
4. MPS runtime 확인
5. `faiss` 설치 여부 확인

## 수동 설치

```bash
cd lua_callgraph_propagation_agent
/opt/homebrew/bin/python3.11 -m venv ../lua_llm_mps
../lua_llm_mps/bin/python -m pip install --upgrade pip
../lua_llm_mps/bin/python -m pip install -r requirements-macos-mps.txt
```

## 확인 명령

```bash
../lua_llm_mps/bin/python - <<'PY'
import torch
print("torch", torch.__version__)
print("mps_built", torch.backends.mps.is_built())
print("mps_available", torch.backends.mps.is_available())
PY
```

기대값:

- `mps_built True`
- `mps_available True`
- `torch.ones(..., device="mps")` 성공

## 현재 확인된 제한

이 저장소에서 2026-04-24 기준으로 실제 확인한 결과, Apple Silicon Mac Studio(M4 Max)에서도 다음 조합에서는 MPS가 열리지 않았다.

- macOS `26.3.1`
- Python `3.11.15`
- torch `2.11.0`

증상은 이렇다.

- `mps_built == True`
- `mps_available == False`
- 실제 tensor 할당 시:
  - `RuntimeError: The MPS backend is supported on MacOS 14.0+`

즉 하드웨어 문제라기보다, 현재 PyTorch가 `macOS 26.x`를 제대로 처리하지 못하는 호환성 문제로 보는 게 맞다.

이 경우에는:

- setup은 성공해도 MPS runtime 검증에서 실패해야 정상
- retrieval는 CPU fallback으로 실행된다
- 속도 개선은 FAISS / symbolic 최적화 쪽이 우선 효력을 낸다

실무적으로는 다음 중 하나가 필요하다.

- macOS 26.x를 정식 지원하는 newer PyTorch wheel 사용
- PyTorch upstream fix 반영 대기
- 당분간 맥에서는 CPU+FAISS, 윈도우에서는 CUDA 경로 사용

## 주의

- MPS는 embedding 인코딩 단계 가속에 가장 직접적이다.
- retrieval 전체 속도는 MPS만으로 끝나지 않고, FAISS / symbolic 최적화와 함께 볼 때 가장 효과가 크다.
- Windows는 MPS 대신 CUDA 경로를 사용하므로, 이 문서는 macOS 전용이다.

## 관련 파일

- [../../scripts/setup/00_setup_macos_mps_environment.py](../../scripts/setup/00_setup_macos_mps_environment.py)
- [../../requirements-macos-mps.txt](../../requirements-macos-mps.txt)
- [../architecture/retrieval_performance_plan.md](../architecture/retrieval_performance_plan.md)
