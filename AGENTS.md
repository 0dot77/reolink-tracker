# AGENTS.md

이 저장소는 인터랙티브 설치 작업을 위한 Reolink RTSP 카메라 트래킹 Python 도구입니다.

## 작업 규칙

- `config.yaml`, 모델 weight, virtualenv, 캐시, OMX runtime state는 git에 넣지 않습니다.
- 공유 가능한 설정 구조는 `config.example.yaml`에 둡니다.
- 실제 RTSP URL, 비밀번호, 사설 장비 IP, 프로젝트 전용 credential은 커밋하지 않습니다.
- TouchDesigner/수신기 쪽을 같이 바꾸지 않는 한 primary OSC 스키마는 유지합니다.
- OSC 스키마의 가장 가까운 source-of-truth는 `tracker.py` 상단 docstring입니다.
- 변경은 작게 유지하고, 최소한 `python -m py_compile tracker.py region.py viewer.py fusion.py`로 검증합니다.
- 앱 변경은 `cd app && npm run build`, `cargo check --manifest-path src-tauri/Cargo.toml`,
  필요 시 `cargo test --manifest-path src-tauri/Cargo.toml`까지 확인합니다.

## 프로젝트 문맥

- 제품 문맥: `docs/product.md`
- 기술 문맥: `docs/tech.md`
- AI 코딩 규칙: `docs/ai-rules.md`
- 결정과 번복 기록: `docs/decisions.md`

사소하지 않은 변경 전에는 위 파일들을 먼저 읽습니다.

## 현재 작업 메모: 청계천 RGB 트래킹 보정

2026-05-12 기준 최근 작업은 청계천 광교 다리 아래 긴 복도형 projection에서
RGB Reolink 영상을 안정적인 OSC actor로 바꾸기 위한 보정입니다. 초기 2카메라
hand-off 보정에서 시작해, 현재는 `cam0`/`cam1`을 primary tracking source로 유지하고
`cam2`는 auxiliary confirmer로만 재활성화할 수 있게 구현되어 있습니다.
맥락이 이어지지 않을 때는 아래를 먼저 확인합니다.

- 원본 테스트 영상:
  - `/Users/taeyang/Desktop/VomReo01-01-154552-154621.mp4`
  - `/Users/taeyang/Desktop/VomReo01-01-211808-211942.mp4`
- 시뮬레이션 결과 폴더: `~/Desktop/vom-reolink-videosim`
- repo-local simulation skill:
  `.codex/skills/sim/SKILL.md`
  - 실행: `./.venv/bin/python .codex/skills/sim/scripts/live_projection_usage.py --config config.yaml`
  - `config.yaml` 요청은 앱 런타임 config가 있으면 그 파일로 resolve됩니다.
  - 출력 형식은 `~/Desktop/vom-reolink-videosim/live-YYYYMMDD-HHMMSS-20s-usage/`
    아래의 `cam0-cam1-20s-projection-usage.mp4`, `summary.json`,
    `preview-frame-*.jpg`입니다.
- Obsidian 현장 기록:
  `/Users/taeyang/Documents/taeyang/01_Projects/봄랩/청계천 박물관/Reolink-Tracker-Logs.md`
- 진행 중인 cam2 보강 spec:
  `docs/cam2-auxiliary-direction.md`
- cam2 저조도/옆모습 리서치:
  `docs/cam2-low-light-research.md`
- TouchDesigner receiver 참고:
  `touchdesigner/person_table_receiver.py`

현장 영상에서 확인한 문제:

- 카메라 1대가 약 40m 구간을 한 번에 보면서 원거리 사람 픽셀 크기가 작아짐.
- 야간 조명, 바닥 반사, 180도 렌즈 왜곡 때문에 사람 박스가 흔들림.
- 가방, 어두운 옷, 앉은 사람 일부, ROI 경계부가 사람처럼 잡히는 경우가 있음.
- raw YOLO track id가 짧게 갈라져 실제 사람 수보다 actor가 많이 생길 수 있음.
- 중앙 hand-off 구간에서 actor가 끊기거나, 반대로 중앙 ghost actor가 남을 수 있음.
- `cam2` 정면 보강은 사람이 옆모습으로 작게 잡히는 구도와 야간 IR/B&W 전환 때문에
  primary detector로 쓰면 오히려 count/gid가 불안정할 수 있음.

현재 구현/운용 계약:

- raw detection을 그대로 OSC/fusion actor로 쓰지 않습니다.
- `tracker.py`는 기본 설정 파일로 `config.yaml`을 요청받아도 Tauri 앱 런타임 config
  `/Users/taeyang/Library/Application Support/com.taeyang.reolink-tracker/runtime/config.yaml`
  가 있으면 그것을 우선 사용합니다. 앱에서 저장한 영역이 tracker, Preview, `$sim`에
  항상 반영되어야 합니다.
- Tauri Preview/Start는 tracker 실행 전에 엔진 파일을 앱 런타임 디렉터리로 동기화합니다.
- Tauri dev 앱을 띄우거나 앞으로 가져올 때 앱 이름을 추측하지 않습니다.
  `reolink-tracker-app` 또는 `Reolink Tracker` 이름으로 `osascript tell application ...`
  activate를 시도하면 macOS 등록 이름과 달라 실패할 수 있습니다. 활성화가 필요하면
  우선 bundle id `com.taeyang.reolink-tracker` 기준으로 시도하고, 그래도 실패하면
  Computer Use/list_apps나 창 상태 확인으로 실제 실행 여부를 확인합니다. 실패한 앱 이름으로
  반복 activate하지 않습니다.
- primary OSC 주소와 기존 좌표 argument 순서는 유지합니다. `/proj/<projection_id>/person_zones`
  metadata는 additive stream이며, `zone_code`는 `0=floor`, `1=body_catch`,
  `2=stair_relaxed`입니다.
- `tracker.py`에는 `detection_filter`가 추가되어 confidence, bbox 크기, bbox 비율,
  짧은 confirm window를 통과한 detection만 이벤트로 승격합니다.
- 낮은 confidence 완화는 bbox 크기/비율 필터와 confirm window를 통과한 detection에만
  적용되어야 합니다.
- `body_catch_points`는 foot point가 바닥면 밖으로 살짝 빠진 bbox를 구제하는 보조
  polygon입니다. 위치 계산은 계속 `image_points` 바닥 homography를 사용합니다.
- `relaxed_presence_points`는 계단/착석자 전용 relaxed 영역입니다. 필요하면
  `relaxed_presence_uv`로 stair plane을 별도 투영하고, `relaxed_presence_v`로 projection
  row를 고정합니다. `relaxed_presence_enabled: false`이면 저장값은 보존하지만 runtime에서는
  `stair_relaxed` actor를 만들지 않습니다.
- `projections[].output_warp_points`는 cross-camera fusion 이후 OSC 송신과
  `interaction_zones` 평가 직전에만 적용하는 projection-level 4점 보정입니다. Floor UV,
  Camera Fit, fusion 상태에 피드백하지 않습니다.
- `processing.parallel=true`에서는 카메라별 YOLO/BoT-SORT 처리를 worker가 수행하고,
  main loop만 fusion/OSC를 소유합니다. `fps_tick` telemetry로 RTSP frame age,
  YOLO/track timing, dropped result, heartbeat, main-loop processing time을 봅니다.
- cross-camera identity/fusion state는 `fusion.py`의 `PersonTracker`에 머물러야 합니다.
  `CamWorker.step()` 안에 카메라 간 공유 state를 넣지 않습니다.
- RTSP low-latency 설정인 `OPENCV_FFMPEG_CAPTURE_OPTIONS`는 `cv2` import 전에 설정해야 합니다.
  이 순서를 바꾸면 옵션이 조용히 적용되지 않습니다.
- 카메라별 `tracking_enabled: false`는 RTSP preview와 calibration frame은 유지하되
  YOLO 모델 로드, fusion source, OSC actor, dispatch overlap 검증에서 제외합니다.
- 카메라별 `role: primary | auxiliary`를 지원합니다. `role: auxiliary`는 YOLO 추론과
  projection sighting은 만들지만 새 `gid`, raw per-cam OSC, dispatch ownership을 만들지
  않습니다. cam2는 `tracking_enabled: true`, `role: auxiliary`로 두고 cam0/cam1 primary
  actor가 중앙에서 끊기려 할 때 `fusion.aux_match_uv_radius`,
  `fusion.aux_match_time_window_s`, `fusion.aux_position_alpha`로만 보강합니다.
- `body_catch_inference_crop`은 cam2 같은 보강 카메라에서 테스트하는 소프트웨어 ROI zoom입니다.
  중앙 miss의 1차 대응은 crop이 아니라 cam0/cam1의 `projection_uv` overlap과
  `dispatch_uv` 분담 조정입니다.
- `fusion.py`에는 `position_alpha` 기반 fused person 좌표 smoothing,
  `max_update_jump_uv` 순간이동 차단, `overlap_duplicate_radius_uv` fresh duplicate 흡수,
  `miss_buffer_frames` 결손 완충, `reuse_lost_gids` 작은 gid 재사용, stair relaxed actor용
  긴 pending hold(`fusion.relaxed_hold_s`)가 추가되었습니다.
- `fusion.hold_boundary_margin_uv`는 projection edge 근처에서만 `held`를 유지하게 하는
  옵션입니다. 내부 `dispatch_uv` 경계에서는 held band를 만들지 않습니다. 중앙에서
  끊긴 actor는 ghost로 잡아두지 말고 `/lost` 처리하며, hand-off는 live overlap과
  fresh duplicate suppression으로 stitch합니다.
- `config.example.yaml`에는 현장 기본값을 추가했습니다. 실제 `config.yaml`은 로컬
  credential이 들어가므로 임의로 수정하지 않습니다.

캘리브레이션 기준:

- Tauri Calibration UI는 `Floor UV`, `Body catch`, `Stair relaxed` 도구를 분리합니다.
  선택 중인 polygon은 선명하게, 나머지는 흐리게 보여야 작업 대상이 헷갈리지 않습니다.
- 작업 순서는 `Floor UV` -> `Body catch` -> `Stair relaxed` -> `Camera Fit`
  (`projection_uv`/`dispatch_uv`) -> `Output Warp`입니다.
- `image_points`는 정확한 바닥 homography/UV 면입니다. 멀리 있는 사람 bbox를 잡기 위해
  바닥면을 무리하게 넓히지 말고, 필요한 경우 `body_catch_points`를 넓힙니다.
- `projection_uv`는 관찰/hand-off 여유 구간이고, `dispatch_uv`는 실제 gid/OSC actor
  ownership 구간입니다. 같은 projection 안의 enabled camera `dispatch_uv`는 겹치면 안 됩니다.
- `dispatch_uv`는 항상 `projection_uv` 안에 있어야 합니다. `tracking_enabled: false`인
  카메라는 dispatch overlap 판단에서 제외됩니다.
- relaxed/stair path도 OSC payload는 기존처럼 `u, v, conf`를 유지합니다. 위치 보정은
  tracker 내부에서 하고 TouchDesigner primary receiver schema를 바꾸지 않습니다.
- cam1은 오른쪽에서 왼쪽을 바라보는 카메라이므로 같은 polygon 좌표라도 homography
  대응 순서가 중요합니다. 최근 로컬 `config.yaml`의 cam1 순서는
  `[(936,166), (1017,575), (307,535), (827,166)]`입니다. 화면상 바닥 polygon은 같지만
  이 순서가 projection 우측 구간의 u/v 방향을 맞춥니다. 이전 순서
  `[(936,166), (827,166), (307,535), (1017,575)]`로 되돌리면 v가 어긋납니다.

2026-05-12 방향/주의:

- 현장에서는 스폿라이트를 켤 수 없으므로, 야간 운용은 저조도 RGB/Color 상태에서
  소프트웨어와 모델 쪽 mitigation을 먼저 검증합니다. Auto가 IR B&W로 바뀌면 RGB YOLO
  입력 도메인이 바뀌므로, 가능한 경우 Color 고정 상태와 실제 Auto 상태를 구분해 기록합니다.
- 현재 1차 실험 후보는 `_sub` stream 유지, `imgsz: 1280`, `model: yolo26s.pt`, cam2
  `role: auxiliary`입니다. FPS가 15 아래로 떨어지거나 OSC/ID latency가 악화되면
  `imgsz: 960`으로 내립니다. `_main` stream은 H.265/decode/bandwidth 리스크 때문에 첫
  실험에서 제외합니다.
- 2026-05-12 replay 검증에서
  `~/Desktop/vom-reolink-videosim/live-20260511-204813-20s-usage`의 기존 20초 클립을 재처리했습니다.
  current runtime 후보(`cam2 conf: 0.08`, `body_catch_min_confidence: 0.08`,
  `auxiliary_confirm_hits: 1`)는 cam2 auxiliary event 2개, cam2-only CLAHE까지 켠 후보는
  cam2 auxiliary event 4개였습니다. 그러나 `auxiliary_center_event_samples`는 계속 0입니다.
  따라서 이 클립에서는 auxiliary matching을 넓히기보다 cam2 입력/검출 자체를 먼저 보강해야 합니다.
- `preprocessing.clahe.*`는 구현되어 있으며 현재 cam2 전용으로 켭니다. 사이트 footage
  fine-tune(`models/site/best.pt`, 별도 `model_path` config key)은 결정 기록의 다음 단계입니다.
  현재 코드에서 모델 선택은 top-level `model` 또는 `--model` 경로를 사용합니다.

검증 상태:

- `./.venv/bin/python -m py_compile tracker.py region.py viewer.py fusion.py .codex/skills/sim/scripts/live_projection_usage.py` 통과.
- `./.venv/bin/python region.py` self-test 통과.
- `./.venv/bin/python fusion.py` self-test 통과.
- `npm run build` 통과.
- `cargo check --manifest-path app/src-tauri/Cargo.toml` 통과.
- `cargo test --manifest-path app/src-tauri/Cargo.toml` 통과.
- 20초 라이브 projection usage simulation은 1280x600 레이아웃을 기준으로 봅니다:
  상단 cam0/cam1, 하단 24x8 projection usage grid, 결과 summary의
  `fresh_samples`, `left_samples`, `right_samples`, `center_overlap_samples`,
  `handoff_count`, `teleport_reject_count`, `lost_count`를 비교합니다.
  summary에는 요청 config(`requested_config`)와 실제 사용 config(`runtime_config`)가 같이 남습니다.

다음 작업 후보:

- 앱에서 Preview/Start를 다시 눌러 런타임 엔진 동기화와 앱 저장 config 적용 여부를 확인합니다.
- `$sim` 또는 위 script로 20초 라이브 사용률 영상을 만든 뒤, cam1 사람들이 projection
  우측 구간에서 실제 깊이 방향과 같은 v 위치로 찍히는지 확인합니다.
- cam2 auxiliary event가 0인 클립에서는 `aux_match_uv_radius`를 먼저 넓히지 않습니다.
  raw YOLO가 cam2 사람 bbox를 내는지, `detection_filter`/region/min bbox에서 탈락하는지,
  또는 저조도 RGB/IR B&W 입력 자체가 문제인지부터 분리합니다.
- cam0/cam1 primary에서만 중앙 hand-off가 끊기는 경우 `projection_uv` overlap,
  `dispatch_uv` 분담, `detection_filter`, `miss_buffer_frames`,
  `overlap_duplicate_radius_uv`를 확인합니다.
- 계단 착석자가 짧게 사라졌다 다시 잡히는 경우 `relaxed_hold_s`와 `relaxed_presence_v`를
  먼저 조정합니다. 바닥 보행자의 v가 높게 뜨면 `projection_uv` v range와 `dispatch_uv`
  sub-range를 앱 Mapping 패널에서 조정합니다.
- cam1 v가 다시 어긋나면 floor 좌표를 넓히기 전에 먼저 image point 순서가
  오른쪽-시점 순서로 유지되는지 확인합니다.
- cam2를 다시 조정할 때도 primary detector로 바로 되돌리지 않습니다.
  `docs/cam2-low-light-research.md`의 저조도 RGB/raw-detection A/B와
  `docs/cam2-auxiliary-direction.md`의 auxiliary contract를 유지합니다.
- 현재 worktree에는 이 작업 외에도 기존 dirty 파일/미추적 파일이 있을 수 있습니다.
  특히 작업 범위 밖 변경은 되돌리지 말고, 필요하면 먼저 `git status --short`로 확인합니다.
