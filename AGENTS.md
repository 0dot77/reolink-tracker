# AGENTS.md

이 저장소는 인터랙티브 설치 작업을 위한 Reolink RTSP 카메라 트래킹 Python 도구입니다.

## 작업 규칙

- `config.yaml`, 모델 weight, virtualenv, 캐시, OMX runtime state는 git에 넣지 않습니다.
- 공유 가능한 설정 구조는 `config.example.yaml`에 둡니다.
- 실제 RTSP URL, 비밀번호, 사설 장비 IP, 프로젝트 전용 credential은 커밋하지 않습니다.
- TouchDesigner/수신기 쪽을 같이 바꾸지 않는 한 primary OSC 스키마는 유지합니다.
- 변경은 작게 유지하고, 최소한 `python -m py_compile tracker.py region.py viewer.py fusion.py`로 검증합니다.

## 프로젝트 문맥

- 제품 문맥: `docs/product.md`
- 기술 문맥: `docs/tech.md`
- AI 코딩 규칙: `docs/ai-rules.md`
- 결정과 번복 기록: `docs/decisions.md`

사소하지 않은 변경 전에는 위 파일들을 먼저 읽습니다.

## 현재 작업 메모: 청계천 RGB 트래킹 보정

2026-05-09 기준 최근 작업은 청계천 광교 다리 아래 Reolink 영상 테스트를 바탕으로
인터랙션 actor 안정화 로직을 추가하는 것입니다. 맥락이 이어지지 않을 때는 아래를
먼저 확인합니다.

- 원본 테스트 영상:
  - `/Users/taeyang/Desktop/VomReo01-01-154552-154621.mp4`
  - `/Users/taeyang/Desktop/VomReo01-01-211808-211942.mp4`
- 임시 시뮬레이션 결과 폴더: `/private/tmp/reolink-video-sim`
- repo-local simulation skill:
  `.codex/skills/sim/SKILL.md`
  - 실행: `./.venv/bin/python .codex/skills/sim/scripts/live_projection_usage.py --config config.yaml`
  - `config.yaml` 요청은 앱 런타임 config가 있으면 그 파일로 resolve됩니다.
  - 출력 형식은 `/private/tmp/reolink-video-sim/live-YYYYMMDD-HHMMSS-20s-usage/`
    아래의 `cam0-cam1-20s-projection-usage.mp4`, `summary.json`,
    `preview-frame-*.jpg`입니다.
- Obsidian 현장 기록:
  `/Users/taeyang/Documents/taeyang/01_Projects/봄랩/청계천 박물관/Reolink-Tracker-Logs.md`

현장 영상에서 확인한 문제:

- 카메라 1대가 약 40m 구간을 한 번에 보면서 원거리 사람 픽셀 크기가 작아짐.
- 야간 조명, 바닥 반사, 180도 렌즈 왜곡 때문에 사람 박스가 흔들림.
- 가방, 어두운 옷, 앉은 사람 일부, ROI 경계부가 사람처럼 잡히는 경우가 있음.
- raw YOLO track id가 짧게 갈라져 실제 사람 수보다 actor가 많이 생길 수 있음.

반영한 방향:

- raw detection을 그대로 OSC/fusion actor로 쓰지 않습니다.
- `tracker.py`는 기본 설정 파일로 `config.yaml`을 요청받아도 Tauri 앱 런타임 config
  `/Users/taeyang/Library/Application Support/com.taeyang.reolink-tracker/runtime/config.yaml`
  가 있으면 그것을 우선 사용합니다. 앱에서 저장한 영역이 tracker, Preview, `$sim`에
  항상 반영되어야 합니다.
- Tauri Preview/Start는 tracker 실행 전에 엔진 파일을 앱 런타임 디렉터리로 동기화합니다.
- `tracker.py`에는 `detection_filter`가 추가되어 confidence, bbox 크기, bbox 비율,
  짧은 confirm window를 통과한 detection만 이벤트로 승격합니다.
- `fusion.py`에는 `position_alpha` 기반 fused person 좌표 smoothing과 stair relaxed
  actor용 긴 pending hold(`fusion.relaxed_hold_s`)가 추가되었습니다.
- `fusion.hold_boundary_margin_uv`는 projection edge 근처에서만 `held`를 유지하게 하는
  옵션입니다. 현재 예시 기본값 `0.08`은 한쪽 영상 테스트에서 중앙 ghost actor를 줄이기
  위한 시작점일 뿐, 2카메라 현장 최종값은 아닙니다.
- 2카메라 연결 후에는 `projection_uv` overlap hand-off 구간에서 `gid`가 끊기는지 먼저
  확인합니다. 끊기면 `hold_boundary_margin_uv`를 `0.12`, `0.16` 순서로 올려 비교하고,
  ghost가 다시 눈에 띄면 낮춥니다. 중앙 miss가 계속 많으면 이 값보다 먼저
  `miss_buffer_frames`와 `detection_filter`를 조정합니다.
- primary OSC 주소와 argument 순서는 유지해야 합니다.
- `config.example.yaml`에는 현장 기본값을 추가했습니다. 실제 `config.yaml`은 로컬
  credential이 들어가므로 임의로 수정하지 않습니다.
- 2026-05-09 라이브 보정 기준:
  - Tauri Calibration UI는 `Floor UV`, `Body catch`, `Stair relaxed` 도구를 분리합니다.
    선택 중인 polygon은 선명하게, 나머지는 흐리게 보여야 작업 대상이 헷갈리지 않습니다.
  - `image_points`는 정확한 바닥 homography/UV 면입니다. 멀리 있는 사람 bbox를 잡기 위해
    바닥면을 무리하게 넓히지 말고, 필요한 경우 `body_catch_points`를 넓힙니다.
  - `body_catch_points`는 신체 bbox를 받기 위한 넓은 보조 영역입니다. foot point가 바닥면
    밖이어도 이 영역과 projection margin 조건을 통과하면 기존 floor path로 승격할 수 있습니다.
  - `relaxed_presence_points`는 계단/착석자 전용 relaxed 영역입니다. 이 polygon 안에서
    bbox가 잡히면 더 완화된 bbox 비율 조건을 적용하고, 필요하면 `relaxed_presence_v`로
    stair 전용 고정 v를 씁니다.
  - `projection_uv`는 TouchDesigner/프로젝션에서 실제로 쓰는 부분 영역을 나타냅니다.
    예를 들어 영상의 세로 1080px 중 절반만 쓰면 projection v 범위를 `0.0-0.5` 또는
    현장 의도에 맞는 sub-range로 잡습니다. `dispatch_uv`는 항상 `projection_uv` 안에 있어야 합니다.
  - relaxed/stair path도 OSC payload는 기존처럼 `u, v, conf`를 유지합니다. 위치 보정은
    tracker 내부에서 하고 TouchDesigner primary receiver schema를 바꾸지 않습니다.
  - cam1은 오른쪽에서 왼쪽을 바라보는 카메라이므로 같은 polygon 좌표라도 homography
    대응 순서가 중요합니다. 현재 로컬 `config.yaml`의 cam1 순서는
    `[(936,166), (1017,575), (307,535), (827,166)]`입니다. 화면상 바닥 polygon은 같지만
    이 순서가 projection 우측 구간의 u/v 방향을 맞춥니다. 이전 순서
    `[(936,166), (827,166), (307,535), (1017,575)]`로 되돌리면 v가 어긋납니다.
  - 2026-05-09 라이브 비교 결과 현재 기준값은 YOLO raw `conf: 0.16`, `imgsz: 1280`,
    `fusion.relaxed_hold_s: 3.0`입니다. `conf: 0.12`는 raw bbox는 늘었지만 noisy actor도
    늘었고, `conf: 0.14`는 cam0 stair 쪽에서 명확히 낫지 않아 0.16을 우선값으로 둡니다.
  - 낮은 confidence 완화는 bbox 크기/비율 필터와 confirm window를 통과한 detection에만
    적용되어야 합니다.

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
- 계단 착석자가 짧게 사라졌다 다시 잡히는 경우 `relaxed_hold_s`와 `relaxed_presence_v`를
  먼저 조정합니다. 바닥 보행자의 v가 높게 뜨면 `projection_uv` v range와 `dispatch_uv`
  sub-range를 앱 Mapping 패널에서 조정합니다.
- cam1 v가 다시 어긋나면 floor 좌표를 넓히기 전에 먼저 image point 순서가
  오른쪽-시점 순서로 유지되는지 확인합니다.
- 현재 worktree에는 이 작업 외에도 기존 dirty 파일/미추적 파일이 있을 수 있습니다.
  특히 작업 범위 밖 변경은 되돌리지 말고, 필요하면 먼저 `git status --short`로 확인합니다.
