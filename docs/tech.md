# 기술 문맥

## 런타임

- 기대 런타임은 Python 3.12입니다.
- PyTorch/Ultralytics 휠은 Python 3.14에서 안정적이라고 가정하지 않습니다.
- 구조는 의도적으로 single-process이며, 카메라당 RTSP reader thread와 선택적 YOLO
  processing worker thread 하나를 둡니다.

## 주요 파일

- `tracker.py`: 설정 로딩, RTSP grabber, YOLO 트래킹, OSC 송신, main loop
- `region.py`: projection/region 데이터 모델, homography 생성, UV 검증, 작은 self-test
- `viewer.py`: OpenCV operator preview와 projection UV canvas
- `config.example.yaml`: 공유 가능한 설정 예시
- `config.yaml`: 실제 카메라 URL이 들어가는 로컬 전용 런타임 설정
- `app/`: macOS 현장 런처용 Tauri/Vite/Rust 하위 프로젝트

## 아키텍처 메모

- `OPENCV_FFMPEG_CAPTURE_OPTIONS`는 반드시 `cv2` import 전에 설정해야 합니다.
- `CamWorker`는 각자 dedicated `YOLO` instance를 가집니다. tracker state가 model instance 단위이기 때문입니다.
- `processing.parallel=true`이면 카메라별 `CameraProcessor`가 YOLO/BoT-SORT 결과를
  main loop로 넘기고, main loop만 cross-camera fusion과 OSC 송신을 수행합니다. 오래된
  처리 결과보다 최신 좌표를 우선하기 위해 worker result queue는 최신 결과 위주로 교체됩니다.
- `fps_tick` telemetry는 `track_step_ms_*`, `yolo_speed_ms`, frame age/drop count,
  heartbeat count, main-loop `processing_ms`를 포함해 RTSP/YOLO/OSC 중 어느 구간이
  좌표 지연을 만드는지 구분하는 데 사용합니다.
- primary OSC output은 raw image-space 좌표가 아니라 공유 projection UV 좌표입니다.
- TouchDesigner lane 분리를 위해 primary 좌표 payload는 유지하고
  `/proj/<projection_id>/person_zones` metadata를 추가로 보냅니다.
  `zone_code`는 `0=floor`, `1=body_catch`, `2=stair_relaxed`입니다.
- `detection_filter`는 YOLO raw box를 OSC/fusion 이벤트로 넘기기 전에 confidence,
  bbox 크기/비율, 짧은 confirm window를 적용합니다. 현장 영상에서 가방이나 경계부
  1프레임 오검출이 actor로 승격되는 것을 줄이는 레이어입니다.
- `fusion.position_alpha`는 fused person 좌표를 EMA로 부드럽게 합니다. OSC schema는
  유지하고 `(u, v)` 값만 완만하게 움직입니다.
- `projections[].output_warp_points`는 cross-camera fusion 이후, OSC 송신과
  `interaction_zones` 평가 직전에 적용하는 projection-level 4점 bilinear 보정입니다.
  Floor UV/Camera Fit/fusion 상태에는 피드백하지 않고, TD는 기존 주소와 argument 순서로
  보정된 위치값을 받습니다.
- `fusion.max_update_jump_uv`가 0보다 크면 같은 `gid`의 다음 관측점이 지정 UV 거리보다
  멀리 튀는 경우 순간이동으로 보고 기존 gid를 lost 처리한 뒤 새 gid로 분리합니다.
  바닥 인터랙션에서 OSC actor가 프로젝션 면을 갑자기 가로지르는 것을 막기 위한
  안전장치입니다.
- camera region의 `body_catch_points`는 발점 투영용 homography를 대체하지 않습니다.
  bbox가 이 보조 polygon과 겹치고 foot UV가 projection rect 근처에 있으면 낮은
  confidence/경계 밖 foot을 살려 projection rect로 clamp합니다. OSC 위치 계산은 계속
  `image_points`의 바닥 homography를 사용합니다. 낮은 confidence 완화는 bbox
  크기/비율 필터와 confirm window를 통과한 detection에만 적용합니다. `too-small`
  rejection은 region의 `min_bbox_height_px`와 relaxed area floor를 통과한 경우에만
  body catch가 구제할 수 있어, 중앙 원거리 보행자처럼 작게 잡히는 bbox를 살립니다.
- 카메라별 `body_catch_inference_crop.enabled`를 켜면 해당 카메라의
  `body_catch_points`가 있는 region만 기준으로, `body_catch_points`와 `image_points`의
  합집합을 crop해 YOLO 입력으로 사용합니다. 이는 cam2 같은 정면 보강 카메라에서 옆모습
  보행자가 작게 잡히는 문제를 줄이기 위한 소프트웨어 ROI 줌입니다. YOLO bbox는 즉시 전체
  프레임 좌표로 되돌린 뒤 기존 body-catch, homography, fusion 경로를 타므로 primary OSC
  schema와 캘리브레이션 좌표계는 바뀌지 않습니다. 다만 중앙 crop이 실제 보행자 픽셀을
  자르는 현장에서는 이 옵션을 끄고 cam0/cam1 분담을 먼저 조정합니다.
- 카메라별 `tracking_enabled: false`는 해당 카메라를 preview/calibration-only로 둡니다.
  FrameGrabber와 캘리브레이션 스냅샷은 유지하지만 YOLO 모델 로드, fusion source, OSC actor,
  `dispatch_uv` overlap 검증에서는 제외합니다. Projection Workbench의 seam/dispatch 판단도
  enabled camera만 기준으로 보므로 cam2를 끄고 cam0/cam1 중심의 `projection_uv`/`dispatch_uv`
  구성을 잡을 수 있습니다.
- 카메라별 `role: primary | auxiliary`는 YOLO를 돌리는 카메라가 actor ownership을 갖는지
  분리합니다. 기본 `primary`는 기존처럼 새 `gid` 생성, raw per-cam OSC 송신, dispatch
  ownership을 수행합니다. `auxiliary`는 YOLO 추론과 projection sighting은 만들지만 새 `gid`
  또는 raw per-cam OSC를 만들지 않고, primary gid가 중앙에서 lost 되려 할 때
  `fusion.aux_match_uv_radius`, `fusion.aux_match_time_window_s`,
  `fusion.aux_position_alpha` 조건으로만 pending/held gid를 보강합니다.
- camera region의 `relaxed_presence_points`는 계단/착석자용 별도 image polygon입니다.
  `image_points`는 UV 변환용 기준 영역으로 유지하고, relaxed polygon은 그 안에서만
  가로로 넓거나 짧은 bbox를 완화해 actor 후보로 승격합니다. `relaxed_presence_uv`가 있으면
  relaxed polygon 4점을 계단 전용 projection UV rect로 매핑해 좌우 카메라의 사다리꼴 오차를
  보정합니다. 값이 없으면 기존 homography의 `u`를 씁니다. `relaxed_presence_v`가 있으면
  최종 projection v를 고정값으로 씁니다. 값이 없으면 `v`만 계단 전용 rect 또는 projection
  rect 안으로 clamp하며, dispatch 판정도 `u` 중심으로만 수행합니다.
  `relaxed_presence_enabled: false`이면 저장된 relaxed polygon/UV/fixed-v 값은 보존하지만
  runtime은 이 경로를 없는 것처럼 취급해 `stair_relaxed` actor와 relaxed hold를 만들지 않습니다.
  `stair_catch_points`는 같은 의미의 입력 alias입니다. 이 경로에서 생성된
  fused actor는 `source_zone=stair_relaxed`로 유지되어 TouchDesigner가 보행자와 다른
  y lane으로 remap할 수 있습니다.
- 저조도 보강의 현재 1차 실험은 `_sub` stream을 유지한 채 `imgsz: 1280`,
  `model: yolo26s.pt`, cam2 `role: auxiliary`를 함께 보는 것입니다. FPS가 15 아래로
  떨어지거나 OSC/ID latency가 나빠지면 `imgsz: 960`으로 내립니다. 자세한 순서는
  `docs/cam2-low-light-research.md`를 따릅니다.
- `preprocessing.clahe.*`는 YOLO 추론 직전 BGR을 LAB로 바꾸고 L 채널에
  `cv2.createCLAHE(clipLimit, tileGridSize)`를 적용한 뒤 다시 BGR로 되돌립니다.
  `cameras: [cam2]`처럼 특정 카메라에만 적용할 수 있어 cam0/cam1 primary baseline을
  유지한 채 중앙 보강 카메라만 저조도 대비를 올릴 수 있습니다.
- 사이트 fine-tuned weight는 아직 별도 `model_path` key로 구현되어 있지 않습니다. 현재 모델
  선택은 top-level `model` 또는 `--model`로 합니다. fine-tune 도입 시 권장 산출 위치는
  `models/site/best.pt`이며, 모델 weight는 git에 넣지 않습니다.
- 같은 projection을 공유하는 카메라들의 `dispatch_uv` slice는 겹치지 않아야 합니다. 겹치면 count가 부풀 수 있습니다.
- cross-camera fusion은 `fresh`와 `held` 상태를 구분합니다. `held` gid는 짧은 detection drop 중 마지막 좌표로 active 목록에 남겨 TouchDesigner 슬롯이 깜박이지 않게 합니다.
- `fusion.relaxed_hold_s`가 0보다 크면 계단/착석자 relaxed polygon에서 생성된 actor만 detection drop 이후 더 오래 held로 남습니다. 일반 바닥 보행자 hold 정책은 그대로 둡니다.
- `fusion.hold_boundary_margin_uv`가 0보다 크면 held gid는 projection 가장자리 근처에서만 active로 남습니다. cam0 -> cam2 -> cam1 같은 내부 dispatch 경계에서 track을 놓친 경우에는 ghost actor가 남지 않도록 즉시 `/lost` 처리합니다. hand-off는 겹치는 `projection_uv`의 live observation과 fresh duplicate suppression으로 stitch합니다.
- `fusion.reuse_lost_gids` 기본값은 `true`입니다. 완전히 lost된 gid는 작은 번호부터 재사용해서 TouchDesigner OSC 채널/테이블이 총 방문자 수만큼 계속 커지지 않게 합니다.
- `interaction_zones`는 projection별 UV rectangle입니다. fused person이 zone 안에 있으면 zone-local 좌표와 dwell/presence를 별도 OSC stream으로 내보내며, 카메라별 calibration `regions`와 섞지 않습니다.
- `/person/<gid>/lost`는 gid가 마지막으로 속한 projection에만 송신합니다. cross-projection broadcast cleanup은 더 이상 하지 않습니다.
- `tracker.py --show`는 operator preview이면서 검증 dashboard입니다. fused gid, trail, velocity, held 상태, 카메라 health를 함께 확인합니다.
- viewer는 `Tab`으로 `regions` / `lan` 페이지를 전환합니다. `lan` 페이지는 macOS의 `networksetup`, `route`, `ifconfig`, `arp` 출력만 읽어서 현재 Mac에 연결된 물리 LAN/IPv4 대역과 `config.yaml`의 RTSP target 라우팅을 보여주며 새 dependency를 요구하지 않습니다.

## 현장 런처 앱

`app/`은 Python tracker를 대체하지 않고 운영 UI와 runtime supervisor만 담당합니다.
앱의 Setup은 repo root의 engine 파일(`tracker.py`, `fusion.py`, `region.py`, `viewer.py`,
`requirements.txt`, `config.example.yaml`)을 macOS app data 아래 `runtime/engine/`으로 복사합니다.

앱 runtime의 실제 설정은 저장소 root가 아니라 app data의 `runtime/config.yaml`에 둡니다.
처음 Setup할 때 없으면 `runtime/engine/config.example.yaml`을 복사하고, 이후 Config 패널에서 읽고 저장합니다.
repo에서 직접 실행하는 `tracker.py`와 repo-local `$sim`도 기본 `config.yaml` 실행일 때는
앱 runtime config를 우선 resolve하므로, 앱에서 그린 캘리브레이션이 다음 CLI 검증에 바로 반영됩니다.
tracker 실행 형태는 아래와 같습니다.

```bash
<app-data>/runtime/.venv/bin/python \
  <app-data>/runtime/engine/tracker.py \
  --config <app-data>/runtime/config.yaml
```

Show Preview는 같은 명령에 `--show`만 추가합니다. primary OSC schema와 기존 Python CLI 동작은 앱 통합과
무관하게 유지해야 합니다.

Setup은 재시도 가능한 작업이어야 합니다. engine 파일 복사, Python 3.12/venv 준비, `requirements.txt`
설치, YOLO model warmup/download가 반복 실행되어도 기존 config를 덮어쓰지 않아야 합니다.

## 검증

가벼운 확인:

```bash
python -m py_compile tracker.py region.py viewer.py fusion.py
```

실행 확인은 실제 카메라와 로컬 `config.yaml`이 필요합니다.

```bash
python tracker.py
python tracker.py --show
```

앱 변경 확인:

```bash
cd app
npm run build
cargo check --manifest-path src-tauri/Cargo.toml
```
