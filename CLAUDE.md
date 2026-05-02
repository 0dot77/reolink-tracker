# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 참고할 지침입니다.

## 이 프로젝트는 무엇인가

Reolink 카메라 2대의 RTSP 스트림을 받아 YOLO26 사람 감지 + BoT-SORT 트래킹을 수행하고 OSC로 내보내는 파이프라인입니다. 기본값은 YOLO26 + BoT-SORT이며, ByteTrack도 선택할 수 있습니다. TouchDesigner / Max / Unity 인터랙티브 시스템의 센서 입력 역할을 하며, 이 저장소는 시각 결과물을 렌더링하지 않고 track coordinate만 OSC로 송신합니다.

프로젝트 외부 문맥: 이 도구가 연결되는 설치 작업은 2026 청계천 미디어아트 전시(봄랩, 청계천 박물관)입니다. 참고 아이디어는 WHAT MUSEUM의 ALTEMY + risa kagami, *Inter-Embodiment*입니다. Obsidian vault의 `~/Documents/taeyang/01_Projects/봄랩/청계천 박물관/` 아래에 기획 노트가 있으며, `청계천 5월 컨텐츠 기술 정리 1차.md`는 카메라 zoning 접근을 다룹니다.

## 데이터 모델: 공유 projection UV

감지는 카메라별로 수행하지만, TouchDesigner로 내보내는 좌표는 `projection_id`로 식별되는 **공유 projection coordinate space**입니다. 카메라 image frame은 더 이상 wire format이 아닙니다.

`config.yaml`의 각 카메라는 하나 이상의 `regions[]`를 가집니다.

- `image_points`: 카메라 프레임에서 region을 둘러싸는 네 점입니다. 클릭/입력 순서는 아래 설명처럼 projection-UV 순서입니다.
- `projection_id`: 이 region이 속한 공유 projection입니다.
- `projection_uv`: 네 `image_points`가 커버하는 projection UV slice입니다. 예: 복도 왼쪽 55%는 `[0.0, 0.0, 0.55, 1.0]`.
- `dispatch_uv`: 이 카메라가 OSC 송신 권한을 갖는 UV slice입니다. 생략하면 `projection_uv`와 같습니다. foot pixel이 `dispatch_uv` 밖으로 project되면 감지/표시는 할 수 있지만 OSC로 송신하지 않습니다.

같은 실제 바닥 projection을 보는 카메라들은 같은 `projection_id`를 씁니다. 한 사람이 동시에 여러 OSC stream으로 나오지 않도록 같은 projection 안의 `dispatch_uv`는 겹치지 않아야 합니다. v1에서는 cross-camera ID fusion을 하지 않습니다.

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

첫 실행 시 `yolo26n.pt`가 작업 디렉터리에 다운로드됩니다. 모델 파일(`*.pt`, `runs/`)은 gitignore 대상입니다.

테스트, lint config, build step은 아직 없습니다. 기본 검증은 다음입니다.

```bash
python -m py_compile tracker.py region.py viewer.py
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

Primary channel은 cross-camera fusion이 만든 person-keyed 주소입니다. raw per-cam 채널은 호환/디버깅을 위해 같이 나갑니다.

Person-keyed (`osc.person_level: true`, 기본 ON):

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/person/<gid>` | `u, v, vx, vy, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | 활성 person마다 매 프레임 |
| `/proj/<projection_id>/person/<gid>/lost` | 없음 | hand-off window가 만료될 때 한 번 |
| `/proj/<projection_id>/persons` | `[gid, ...]` | 활성 gid 목록, 매 프레임 |
| `/proj/<projection_id>/persons/count` | int | 매 프레임 |

Raw per-cam (`osc.raw_per_cam: true`, 기본 ON):

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/cam/<cam>/track/<id>` | `u, v, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | foot이 `image_points` polygon 안이고 `dispatch_uv` 안이며 `min_bbox_height_px`를 통과할 때 매 프레임 |
| `/proj/<projection_id>/cam/<cam>/track/<id>/lost` | 없음 | track이 끝나거나 dispatch 밖으로 나갈 때 한 번 |
| `/proj/<projection_id>/cam/<cam>/count` | int | 카메라별 count, 매 프레임 |
| `/proj/<projection_id>/cam/<cam>/active` | id 목록 | active id가 있을 때 매 프레임 |

`(u=0, v=0)`은 projection 좌상단, `(u=1, v=1)`은 우하단입니다. `gid`는 1부터 단조 증가하며 lost 후 재사용되지 않습니다.

Legacy image-space 메시지(`<cam_prefix>/track/<id>`와 `cx, cy, w, h, conf`)는 `osc.legacy_image_space: true`일 때만 송신합니다. 기본값은 off입니다.

## Config 주의점

- `config.yaml`의 RTSP URL 안 비밀번호는 URL encoding이 필요합니다. `! -> %21`, `# -> %23`, `@ -> %40`, `: -> %3A`. 실제 credential은 로컬에만 둡니다.
- `_main` 대신 `_sub` stream을 씁니다. 많은 Reolink 모델은 main이 H.265이고 sub가 H.264라 OpenCV bundled FFmpeg에서 main decode가 실패할 수 있습니다.
- username은 device friendly name이 아니라 `admin`입니다.
- macOS 기본 device는 `mps`, 그 외는 `cpu`입니다. ultralytics에서 `MPS not implemented`가 나면 ultralytics를 업그레이드하거나 config에서 `device: cpu`로 바꿉니다.
- 기본 tracker는 `botsort.yaml`입니다. throughput을 더 원하면 `bytetrack.yaml`을 쓰되 smoothness는 낮아질 수 있습니다.

## 확장할 때

- **세 번째 카메라 추가**: `cameras:` 항목을 추가하고 unique `name`, `osc_prefix`, `regions[]`를 넣습니다. 같은 projection을 보면 기존 `projection_id`를 재사용하고, 같은 projection 안에서 `dispatch_uv`가 겹치지 않게 잡습니다. grabber/worker pair는 자동 생성되므로 다른 코드 변경은 필요 없습니다.
- **Cross-camera fusion**: v2로 미루지 말고 `fusion.py`의 `PersonTracker`를 갱신합니다. UV 거리 + 시간 윈도우 기반의 단순 매칭이 v1이며, 다음 단계 후보는 (a) 진행 방향(velocity) prior로 매칭 안정화, (b) appearance ReID 임베딩으로 ambiguity 해소, (c) 다중 hypothesis tracking입니다. `CamWorker.step` 내부에 카메라 간 공유 state를 넣지 말고 fusion 모듈에 머무르게 합니다.
- **Stereo depth / 3D 위치**: v2 후보. 두 카메라가 같은 사람을 동시에 본다면 UV 차이를 이용한 disparity로 v 좌표를 보강할 수 있습니다.
- **OSC 스키마 변경**: 수신기 TouchDesigner patch가 이 저장소 밖에 있으므로 주소나 argument 순서 변경 전 조율이 필요합니다.
- `step()` 안의 `r.plot() if True else frame` 형태는 annotated frame을 항상 반환합니다. dead branch는 향후 raw frame path를 위한 scaffolding입니다.
