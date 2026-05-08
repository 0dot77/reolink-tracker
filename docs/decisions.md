# 결정 기록

## 2026-05-01: 런타임 설정은 로컬에만 둔다

`config.yaml`에는 실제 RTSP URL과 credential이 들어가므로 git에서 제외합니다. 저장소에는 대신 `config.example.yaml`만 추적합니다.

결과: 새 장비에서는 `config.example.yaml`을 `config.yaml`로 복사한 뒤 로컬 카메라 정보를 채웁니다.

## 2026-05-01: GitHub를 프로젝트 기억으로 사용한다

이 저장소는 코드뿐 아니라 AI가 참고할 수 있는 프로젝트 문맥도 함께 보관합니다. 제품 의도, 기술 제약, AI 규칙, Issue, PR, 결정 기록은 채팅 기록이 아니라 레포 안에 남깁니다.

## 2026-05-09: raw detection을 바로 actor로 쓰지 않는다

야간 청계천 현장 테스트에서 가방, 경계부, 작은 흔들림 검출이 사람 actor로 승격되는 문제가 확인되었습니다.
YOLO raw box는 confidence/크기/비율 필터와 짧은 confirm window를 통과한 뒤에만 OSC/fusion 이벤트로 넘깁니다.
fused person 좌표도 `fusion.position_alpha`로 EMA smoothing 하여 인터랙션 포인트가 덜 튀도록 합니다.

## 2026-05-09: interaction zone은 projection UV 위에 둔다

TouchDesigner 인스턴싱용 인터랙션 영역은 카메라 image region이 아니라 projection별 UV rectangle로 저장합니다.
기존 카메라 `regions`는 calibration/dispatch용으로 유지하고, `projections[].interaction_zones`에서 zone-local 좌표, dwell, held presence를 별도 OSC stream으로 내보냅니다.
기존 `/person/<gid>` payload 순서는 유지하며, 추가 zone 주소만 additive로 붙입니다.
