# 기술 문맥

## 런타임

- 기대 런타임은 Python 3.12입니다.
- PyTorch/Ultralytics 휠은 Python 3.14에서 안정적이라고 가정하지 않습니다.
- 구조는 의도적으로 single-process이며, 카메라당 RTSP reader thread 하나를 둡니다.

## 주요 파일

- `tracker.py`: 설정 로딩, RTSP grabber, YOLO 트래킹, OSC 송신, main loop
- `region.py`: projection/region 데이터 모델, homography 생성, UV 검증, 작은 self-test
- `viewer.py`: OpenCV operator preview와 projection UV canvas
- `config.example.yaml`: 공유 가능한 설정 예시
- `config.yaml`: 실제 카메라 URL이 들어가는 로컬 전용 런타임 설정

## 아키텍처 메모

- `OPENCV_FFMPEG_CAPTURE_OPTIONS`는 반드시 `cv2` import 전에 설정해야 합니다.
- `CamWorker`는 각자 dedicated `YOLO` instance를 가집니다. tracker state가 model instance 단위이기 때문입니다.
- primary OSC output은 raw image-space 좌표가 아니라 공유 projection UV 좌표입니다.
- `detection_filter`는 YOLO raw box를 OSC/fusion 이벤트로 넘기기 전에 confidence,
  bbox 크기/비율, 짧은 confirm window를 적용합니다. 현장 영상에서 가방이나 경계부
  1프레임 오검출이 actor로 승격되는 것을 줄이는 레이어입니다.
- `fusion.position_alpha`는 fused person 좌표를 EMA로 부드럽게 합니다. OSC schema는
  유지하고 `(u, v)` 값만 완만하게 움직입니다.
- 같은 projection을 공유하는 카메라들의 `dispatch_uv` slice는 겹치지 않아야 합니다. 겹치면 count가 부풀 수 있습니다.
- cross-camera fusion은 `fresh`와 `held` 상태를 구분합니다. `held` gid는 중앙 hand-off나 짧은 detection drop 중 마지막 좌표로 active 목록에 남겨 TouchDesigner 슬롯이 깜박이지 않게 합니다.
- `interaction_zones`는 projection별 UV rectangle입니다. fused person이 zone 안에 있으면 zone-local 좌표와 dwell/presence를 별도 OSC stream으로 내보내며, 카메라별 calibration `regions`와 섞지 않습니다.
- `/person/<gid>/lost`는 gid가 마지막으로 속한 projection에만 송신합니다. cross-projection broadcast cleanup은 더 이상 하지 않습니다.
- `tracker.py --show`는 operator preview이면서 검증 dashboard입니다. fused gid, trail, velocity, held 상태, 카메라 health를 함께 확인합니다.
- viewer는 `Tab`으로 `regions` / `lan` 페이지를 전환합니다. `lan` 페이지는 macOS의 `networksetup`, `route`, `ifconfig`, `arp` 출력만 읽어서 현재 Mac에 연결된 물리 LAN/IPv4 대역과 `config.yaml`의 RTSP target 라우팅을 보여주며 새 dependency를 요구하지 않습니다.

## 검증

가벼운 확인:

```bash
python -m py_compile tracker.py region.py viewer.py
```

실행 확인은 실제 카메라와 로컬 `config.yaml`이 필요합니다.

```bash
python tracker.py
python tracker.py --show
```
