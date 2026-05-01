# AGENTS.md

이 저장소는 인터랙티브 설치 작업을 위한 Reolink RTSP 카메라 트래킹 Python 도구입니다.

## 작업 규칙

- `config.yaml`, 모델 weight, virtualenv, 캐시, OMX runtime state는 git에 넣지 않습니다.
- 공유 가능한 설정 구조는 `config.example.yaml`에 둡니다.
- 실제 RTSP URL, 비밀번호, 사설 장비 IP, 프로젝트 전용 credential은 커밋하지 않습니다.
- TouchDesigner/수신기 쪽을 같이 바꾸지 않는 한 primary OSC 스키마는 유지합니다.
- 변경은 작게 유지하고, 최소한 `python -m py_compile tracker.py region.py viewer.py`로 검증합니다.

## 프로젝트 문맥

- 제품 문맥: `docs/product.md`
- 기술 문맥: `docs/tech.md`
- AI 코딩 규칙: `docs/ai-rules.md`
- 결정과 번복 기록: `docs/decisions.md`

사소하지 않은 변경 전에는 위 파일들을 먼저 읽습니다.
