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
- 같은 projection을 공유하는 카메라들의 `dispatch_uv` slice는 겹치지 않아야 합니다. 겹치면 count가 부풀 수 있습니다.

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
