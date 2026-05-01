# 결정 기록

## 2026-05-01: 런타임 설정은 로컬에만 둔다

`config.yaml`에는 실제 RTSP URL과 credential이 들어가므로 git에서 제외합니다. 저장소에는 대신 `config.example.yaml`만 추적합니다.

결과: 새 장비에서는 `config.example.yaml`을 `config.yaml`로 복사한 뒤 로컬 카메라 정보를 채웁니다.

## 2026-05-01: GitHub를 프로젝트 기억으로 사용한다

이 저장소는 코드뿐 아니라 AI가 참고할 수 있는 프로젝트 문맥도 함께 보관합니다. 제품 의도, 기술 제약, AI 규칙, Issue, PR, 결정 기록은 채팅 기록이 아니라 레포 안에 남깁니다.
