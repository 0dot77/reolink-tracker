# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 참고할 지침입니다.

## 이 프로젝트는 무엇인가

Reolink 카메라 2-3대의 RTSP 스트림을 받아 YOLO26 사람 감지 + BoT-SORT 트래킹을 수행하고 OSC로 내보내는 파이프라인입니다. 기본값은 YOLO26 + BoT-SORT이며, ByteTrack도 선택할 수 있습니다. TouchDesigner / Max / Unity 인터랙티브 시스템의 센서 입력 역할을 하며, 이 저장소는 시각 결과물을 렌더링하지 않고 track coordinate만 OSC로 송신합니다.

이 파일은 high-level 오리엔테이션입니다. 상세한 기술 contract는 `docs/tech.md`, 결정 기록은 `docs/decisions.md`, cam2 auxiliary hand-off contract는 `docs/cam2-auxiliary-direction.md`를 참고합니다.

프로젝트 외부 문맥: 이 도구가 연결되는 설치 작업은 2026 청계천 미디어아트 전시(봄랩, 청계천 박물관)입니다. 참고 아이디어는 WHAT MUSEUM의 ALTEMY + risa kagami, *Inter-Embodiment*입니다. Obsidian vault의 `~/Documents/taeyang/01_Projects/봄랩/청계천 박물관/` 아래에 기획 노트가 있으며, `청계천 5월 컨텐츠 기술 정리 1차.md`는 카메라 zoning 접근을 다룹니다.

## 데이터 모델: 공유 projection UV

감지는 카메라별로 수행하지만, TouchDesigner로 내보내는 좌표는 `projection_id`로 식별되는 **공유 projection coordinate space**입니다. 카메라 image frame은 더 이상 wire format이 아닙니다.

`config.yaml`의 각 카메라는 하나 이상의 `regions[]`를 가집니다.

- `image_points`: 카메라 프레임에서 region을 둘러싸는 네 점입니다. 클릭/입력 순서는 아래 설명처럼 projection-UV 순서입니다.
- `projection_id`: 이 region이 속한 공유 projection입니다.
- `projection_uv`: 네 `image_points`가 커버하는 projection UV slice입니다. 예: 복도 왼쪽 55%는 `[0.0, 0.0, 0.55, 1.0]`.
- `dispatch_uv`: 이 카메라가 OSC 송신 권한을 갖는 UV slice입니다. 생략하면 `projection_uv`와 같습니다. foot pixel이 `dispatch_uv` 밖으로 project되면 감지/표시는 할 수 있지만 OSC로 송신하지 않습니다.

같은 실제 바닥 projection을 보는 카메라들은 같은 `projection_id`를 씁니다. 한 사람이 동시에 여러 OSC stream으로 나오지 않도록 같은 projection 안의 `dispatch_uv`는 겹치지 않아야 합니다. cross-camera ID fusion은 `fusion.py`의 `PersonTracker`가 UV 거리 + 시간 윈도우 기반으로 수행합니다.

카메라별 옵션:

- `tracking_enabled: false`이면 YOLO 모델 자체를 로드하지 않고 preview/calibration 대상으로만 남습니다 (cam2를 잠시 빼둘 때 사용).
- `role: auxiliary`이면 YOLO 추론은 하되 새 `gid`나 raw per-cam OSC를 만들지 않고, primary
  actor가 중앙에서 끊기려 할 때 sighting으로만 fusion에 기여합니다.
- `body_catch_points`/`body_catch_inference_crop`은 발점이 floor 밖으로 빠지는 보행자를 살리는 보조 polygon과 ROI crop입니다.
- `relaxed_presence_points`(별칭 `stair_catch_points`)는 계단/착석자 detection mask로, 결과 actor는 `source_zone=stair_relaxed`로 표시됩니다.

region의 4점 순서는 카메라 이미지 기준이 아니라 projection-UV 기준입니다.

```text
top-left -> top-right -> bottom-right -> bottom-left
```

이 방식 덕분에 서로 마주 보는 복도 카메라 2대가 mirrored view를 가져도 같은 UV frame으로 매핑할 수 있습니다.

## 설치와 실행

```bash
# Python 3.12 권장. PyTorch 휠은 Python 3.14에서 안정적이지 않습니다.
uv venv -p python3.12
source .venv/bin/activate
uv pip install -r requirements.txt

python tracker.py
python tracker.py --show
python tracker.py --config foo.yaml
python tracker.py --model yolov8s.pt --device cpu
```

첫 실행 시 설정된 YOLO weight가 작업 디렉터리에 다운로드됩니다. 현장 기본 후보는
`yolo26s.pt`이며, 더 가벼운 테스트가 필요할 때만 `yolo26n.pt`로 내립니다.
모델 파일(`*.pt`, `runs/`)은 gitignore 대상입니다.

현장 런처는 `app/` 하위 프로젝트(Tauri + Vite + Rust)입니다. macOS 릴리즈는 GitHub Actions가 자동 빌드하고, 앱이 자체 runtime venv에 engine 파일을 복사해 실행합니다. 자세한 흐름은 `docs/tech.md` "현장 런처 앱" 절 참고.

기본 검증은 다음입니다.

```bash
python -m py_compile tracker.py region.py viewer.py fusion.py
cd app && npm run build && cargo check --manifest-path src-tauri/Cargo.toml
```

실제 검증은 live camera와 TouchDesigner OSC 수신으로 합니다.

## 아키텍처

Single-process, camera당 thread 하나입니다.

1. **`FrameGrabber(threading.Thread)`**: 카메라별 RTSP stream을 가능한 한 빠르게 drain하고 최신 frame만 lock 아래 보관합니다. 오래된 frame은 버립니다. read failure가 나면 1초에서 10초까지 exponential backoff로 재연결합니다. detection은 decode를 막지 않고 stale frame queue도 만들지 않습니다.

2. **`CamWorker`**: 카메라별 worker입니다. ByteTrack/BoT-SORT state가 YOLO model instance에 묶이므로 각 카메라가 자기 `YOLO` instance를 가집니다. track ID는 카메라 안에서는 유지되지만 카메라 간에는 독립적입니다. `current_ids`와 `last_ids` diff로 `lost` event를 냅니다.

3. **`main()` loop**: 각 grabber의 `(frame, idx)`를 polling하고, `idx`가 변하지 않으면 skip합니다. 새 frame이 있으면 `worker.step()`을 호출해 `(overlays, regions, person_events, lost_sources)`를 받고, 모든 카메라의 events/lost_sources를 모아 `PersonTracker.update()`로 fusion합니다. 결과 person 리스트와 `drain_lost_gids()`를 `_emit_person_osc()`가 송신합니다. raw per-cam OSC는 `CamWorker.step()` 안에서 직접 송신되며 `osc.raw_per_cam: false`로 끌 수 있습니다. 2초마다 camera별 fps와 OSC rate를 출력합니다. SIGINT/SIGTERM은 clean shutdown을 위해 `stop_flag`를 세웁니다.

4. **`PersonTracker` (fusion.py)**: pure-Python 상태 머신. 입력은 매 프레임 `PersonEvent` 리스트와 `(cam_name, track_id)` lost_sources. 같은 `(cam, tid)` source가 들어오면 기존 gid 유지, 새 source는 같은 projection 안에서 최근 잃은 gid와 UV 거리/시간 윈도우로 매칭해 stitch, 매칭 실패면 새 gid 할당. hand-off window 안에 매칭이 없으면 `drain_lost_gids()`로 한 번 lost 통보. EMA로 `(vx, vy)` 산출. cross-projection 매칭은 의도적으로 차단됩니다.

## RTSP latency contract

`OPENCV_FFMPEG_CAPTURE_OPTIONS`는 `tracker.py` 상단에서 `cv2` import 전에 설정합니다. 이 위치를 아래로 옮기면 low-latency flag가 조용히 적용되지 않습니다.

현재 조합은 `rtsp_transport=tcp`, `nobuffer`, `low_delay`, `max_delay=500ms`, `reorder_queue_size=0`, `CAP_PROP_BUFFERSIZE=1`이며 sub stream 기준 약 200-400 ms glass-to-OSC를 목표로 합니다. 더 낮추려면 Reolink Client에서 keyframe interval을 1초로 낮춥니다.

## OSC 스키마

기본 primary channel은 `osc.td_minimal: true`의 TouchDesigner 최소 스트림입니다. `td_minimal: true`이면 raw per-cam과 zone-level은 자동 off되어 채널 노이즈가 줄어듭니다. 더 상세한 디버그 채널은 `td_minimal: false`로 두고 개별 토글로 켭니다. 정식 source-of-truth는 `tracker.py` 최상단 docstring입니다.

TD-minimal (`osc.td_minimal: true`, 기본 ON):

| 주소 | 인자 |
|---|---|
| `/proj/<projection_id>/active` | `[gid, gid, ...]` |
| `/proj/<projection_id>/person_zones` | `[gid, zone_code, gid, zone_code, ...]` (0=floor, 1=body_catch, 2=stair_relaxed) |
| `/proj/<projection_id>/xy` | `[gid, x, y, gid, x, y, ...]` |
| `/proj/<projection_id>/uv` | `[gid, u, v, gid, u, v, ...]` |
| `/proj/<projection_id>/persons/count` | int |

Person-keyed 디버그 (`osc.td_minimal: false` + `osc.person_level: true`):

| 주소 | 인자 |
|---|---|
| `/proj/<projection_id>/person/<gid>` | `u, v, vx, vy, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) |
| `/proj/<projection_id>/person/<gid>/source_zone` | `zone_code, zone_name` |
| `/proj/<projection_id>/person/<gid>/lost` | 없음 (hand-off window 만료) |
| `/proj/<projection_id>/persons` | `[gid, ...]` |
| `/proj/<projection_id>/persons/count` | int |

Interaction zone (`projection.interaction_zones`가 설정되어 있고 `td_minimal: false`):

| 주소 | 인자 |
|---|---|
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>` | `u, v, zone_u, zone_v, vx, vy, dwell_s, presence, state_code` |
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>/enter` | `zone_u, zone_v` |
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>/leave` | `reason_code, dwell_s` |
| `/proj/<projection_id>/zone/<zone_id>/count` | int |

Raw per-cam (`osc.raw_per_cam: true`, `td_minimal: true`이면 자동 off):

| 주소 | 인자 |
|---|---|
| `/proj/<projection_id>/cam/<cam>/track/<id>` | `u, v, conf` (+ optional `u_px, v_px`) |
| `/proj/<projection_id>/cam/<cam>/track/<id>/lost` | 없음 |
| `/proj/<projection_id>/cam/<cam>/count` | int |
| `/proj/<projection_id>/cam/<cam>/active` | `[id, ...]` |

`(u=0, v=0)`은 projection 좌상단, `(u=1, v=1)`은 우하단입니다. `gid`는 1부터 단조 증가하며 기본적으로 lost 후 재사용되지 않지만 `fusion.reuse_lost_gids: true`이면 작은 번호부터 재활용됩니다 (TouchDesigner OSC 채널/테이블이 무한히 커지지 않게).

Legacy image-space 메시지(`<cam_prefix>/track/<id>`와 `cx, cy, w, h, conf`)는 `osc.legacy_image_space: true`일 때만 송신합니다. 기본값은 off입니다.

`projections[].output_warp_points`가 있으면 cross-camera fusion 이후, OSC 송신과 interaction zone 평가 직전에 4점 bilinear 보정이 적용됩니다. fusion/캘리브레이션 state에는 피드백되지 않습니다.

## Config 주의점

- `config.yaml`의 RTSP URL 안 비밀번호는 URL encoding이 필요합니다. `! -> %21`, `# -> %23`, `@ -> %40`, `: -> %3A`. 실제 credential은 로컬에만 둡니다.
- `_main` 대신 `_sub` stream을 씁니다. 많은 Reolink 모델은 main이 H.265이고 sub가 H.264라 OpenCV bundled FFmpeg에서 main decode가 실패할 수 있습니다.
- username은 device friendly name이 아니라 `admin`입니다.
- macOS 기본 device는 `mps`, 그 외는 `cpu`입니다. ultralytics에서 `MPS not implemented`가 나면 ultralytics를 업그레이드하거나 config에서 `device: cpu`로 바꿉니다.
- 기본 tracker는 `botsort.yaml`입니다. throughput을 더 원하면 `bytetrack.yaml`을 쓰되 smoothness는 낮아질 수 있습니다.

## 확장할 때

- **세 번째 카메라 추가**: `cameras:` 항목을 추가하고 unique `name`, `osc_prefix`, `regions[]`를 넣습니다. 같은 projection을 보면 기존 `projection_id`를 재사용합니다. primary 카메라끼리는 같은 projection 안에서 `dispatch_uv`가 겹치지 않게 잡습니다. cam2처럼 중앙 보강만 할 카메라는 `tracking_enabled: true`, `role: auxiliary`로 두고 새 actor ownership을 주지 않습니다.
- **Cross-camera fusion 보강**: 이미 `PersonTracker`가 UV 거리 + 시간 윈도우 + velocity prior로 stitching하고, auxiliary 카메라 sighting buffer도 지원합니다. 다음 단계 후보는 (a) cam2 raw detection recall 개선, (b) appearance ReID 임베딩 (OSNet 등), (c) 다중 hypothesis tracking입니다. `CamWorker.step` 내부에 카메라 간 공유 state를 넣지 말고 fusion 모듈에 머무르게 합니다.
- **야간/저조도 보강**: 현재 현장에서는 스폿라이트를 켤 수 없으므로 `_sub` stream 유지,
  `imgsz: 1280`, `model: yolo26s.pt`, cam2 auxiliary를 기준 후보로 본다. FPS가 15 아래로 떨어지면
  `imgsz: 960`으로 내린다. 2026-05-12 replay에서는 cam2 accepted auxiliary event가
  0 -> 2 -> 4로 늘었지만 center auxiliary event는 여전히 0이었다. 다음 보강은 auxiliary radius보다
  Color/RGB 고정/ROI/fine-tune 쪽을 우선 검토한다. 자세한 순서는
  `docs/cam2-low-light-research.md`와 `docs/cam2-auxiliary-direction.md`를 따른다.
- **Stereo depth / 3D 위치**: v2 후보. 두 카메라가 같은 사람을 동시에 본다면 UV 차이를 이용한 disparity로 v 좌표를 보강할 수 있습니다.
- **OSC 스키마 변경**: 수신기 TouchDesigner patch가 이 저장소 밖에 있으므로 주소나 argument 순서 변경 전 조율이 필요합니다.
- `step()` 안의 `r.plot() if True else frame` 형태는 annotated frame을 항상 반환합니다. dead branch는 향후 raw frame path를 위한 scaffolding입니다.
