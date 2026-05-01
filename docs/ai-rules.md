# AI 코딩 규칙

Codex, Claude Code, Cursor 또는 다른 AI 코딩 에이전트가 안정적으로 참고할 규칙입니다.

## 수정 전

- `README.md`, `docs/product.md`, `docs/tech.md`, 이 파일을 먼저 읽습니다.
- `git status`를 확인하고, 관련 없는 사용자 변경을 덮어쓰지 않습니다.
- `config.yaml`은 private local state로 취급합니다.

## 구현 선호

- 작고 되돌리기 쉬운 변경을 선호합니다.
- 현재의 `tracker.py` / `region.py` / `viewer.py` 경계를 먼저 재사용합니다.
- 작업이 명시적으로 필요로 하지 않는 한 새 dependency를 추가하지 않습니다.
- OSC 주소와 argument 순서는 schema migration 작업이 아닌 이상 backward-compatible하게 유지합니다.
- 카메라 credential과 설치 현장 private detail은 커밋하지 않습니다.

## 완료 전 검증

- `python -m py_compile tracker.py region.py viewer.py`를 실행합니다.
- region 수학을 바꿨다면 dependency가 설치된 환경에서 `python region.py`를 실행합니다.
- runtime behavior를 바꿨다면 live-camera validation이 아직 필요한지 명시합니다.

## 좋은 Issue 형태

각 GitHub Issue에는 다음을 포함합니다.

- 목표
- 현재 동작
- 원하는 동작
- 완료 기준
- 현장 설치 제약이 있다면 그 내용
