# 제품 문맥

## 목적

`reolink-tracker`는 Reolink RTSP 카메라 피드를 낮은 지연 시간의 OSC 사람 위치 메시지로 변환하는 도구입니다. TouchDesigner, Max, Unity 같은 인터랙티브 미디어 시스템에서 센서 입력으로 쓰기 위해 만들었습니다.

현재 설치 대상은 복도형 projection 환경이며, 각 카메라의 감지 결과를 공유 projection UV 좌표계로 변환합니다.

## 사용자

- 현장에서 Reolink 카메라를 캘리브레이션하는 설치 운영자
- TouchDesigner/Max/Unity에서 OSC를 받는 크리에이티브 테크놀로지스트
- 트래킹, 캘리브레이션, 카메라 간 동작을 확장하는 개발자

## MVP

- Reolink sub-stream RTSP 피드를 낮은 지연 시간으로 읽기
- Ultralytics YOLO + BoT-SORT 또는 ByteTrack으로 사람 감지와 트래킹 수행
- 카메라별 image detection을 공유 projection UV 좌표로 변환
- track 위치, active ID, count를 안정적인 OSC 메시지로 송신
- 카메라와 region을 검증할 수 있는 operator preview 제공

## v1에서 하지 않는 것

- 시각 결과물 렌더링
- 카메라 간 identity fusion
- stereo/depth reconstruction
- cloud service 운영
- analytics 데이터 저장

## 성공 기준

- 실제 Reolink 카메라를 대상으로 로컬 실행 가능
- TouchDesigner가 유용한 지연 시간 안에서 사람별 OSC 업데이트 수신
- 코드 변경 없이 카메라 region 캘리브레이션 가능
- private camera credential은 로컬에만 유지
