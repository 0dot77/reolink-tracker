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

## 2026-05-09: 현장 런처 앱은 repo 내부 `app/` 하위 프로젝트로 둔다

초기 Tauri 런처는 `/Users/taeyang/Developer/tools/reolink-tracker-app` sibling 프로젝트로 만들었지만,
engine 파일과 운영 문맥이 갈라져서 현장 작업 중 어떤 tracker 버전을 앱이 복사하는지 확인하기 어려웠습니다.

결정: Tauri/Vite/Rust 런처를 이 저장소의 `app/` 하위 프로젝트로 편입합니다. Python tracker는 계속 source of truth로 유지하고,
앱은 repo root의 `tracker.py`, `fusion.py`, `region.py`, `viewer.py`, `requirements.txt`, `config.example.yaml`을
macOS app data runtime으로 복사해 실행합니다.

결과: 앱 소스와 tracker engine 변경을 같은 PR/diff에서 검토할 수 있습니다. 실제 `config.yaml`은 저장소가 아니라
앱 data runtime에 유지하므로 RTSP credential과 현장 private IP는 여전히 git에 들어가지 않습니다.

## 2026-05-09: 계단 착석자는 별도 relaxed presence polygon으로 잡는다

계단/착석자는 bbox가 세로형 사람 형태로 잡히지 않을 수 있지만, 계단은 바닥 projection
평면과 다르므로 UV homography 기준점에 섞으면 좌표가 왜곡됩니다.

결정: 기존 `image_points`는 바닥/projection UV 변환용 4점으로 유지하고, 같은 region에
`relaxed_presence_points`를 추가해 그 polygon 안에서만 confidence/bbox 비율 기준을 완화합니다.
OSC schema는 유지하고, 위치는 기존 homography에서 나온 `u`를 우선 쓰며 `v`만 projection
범위 안으로 clamp합니다.

보강: 계단 영역을 쓰지 않는 현장에서는 `relaxed_presence_enabled: false`로 relaxed 설정을
보존한 채 runtime에서 무시합니다. 이 경우 `stair_relaxed` actor와 relaxed hold는 생성되지
않고, 다시 켜면 기존 계단 polygon을 재사용할 수 있습니다.

## 2026-05-10: 중앙 사각지대는 cam2로 보강한다

긴 복도 양끝의 `cam0`/`cam1`만으로 중앙 hand-off 구간을 보정하려 하면 멀리 있는 사람의
bbox가 작고 foot point가 흔들려 바닥 homography를 무리하게 넓히게 됩니다.

결정: 도면 중앙 위치의 `cam2`를 같은 `projection_id`에 추가하고, dispatch 담당 구간을
`cam0 -> cam2 -> cam1` 세 slice로 나눕니다. `projection_uv`는 hand-off 후보를 위해
살짝 겹치게 둘 수 있지만, `dispatch_uv`는 positive-area overlap 없이 맞닿게 둡니다.

결과: OSC schema와 fusion 모델은 유지하면서 중앙부 actor 안정성을 카메라 배치로 해결합니다.
앱 에디터와 시뮬레이션은 카메라 수를 2대로 가정하지 않고 configured cameras 전체를 다룹니다.

보강: `hold_boundary_margin_uv`는 projection 외곽 ghost 방지용으로 유지하되,
`hold_handoff_margin_uv`를 추가해 `dispatch_uv` 내부 u 경계 근처에서만 중앙 held를 허용합니다.
기본 경계 목록은 configured camera regions의 dispatch slice에서 자동 산출하며, 필요하면
`hold_handoff_u_edges`로 projection별 값을 직접 지정할 수 있습니다. 이 보강은 2026-05-11에
중앙 경계 ghost가 이상하게 보이는 현장 판단으로 되돌렸습니다.

## 2026-05-10: 착석자와 보행자는 별도 source zone metadata로 분리한다

TouchDesigner에서 같은 `v`/`y` remap을 쓰면 계단 착석자와 바닥 보행자의 출력 lane이
같이 움직입니다. 좌표 payload에 문자열을 끼워 넣으면 기존 TD 패치와 CHOP 변환이 깨질 수
있으므로, primary `/uv`, `/xy`, `/person/<gid>` payload는 유지합니다.

결정: heartbeat마다 `/proj/<projection_id>/person_zones`를 추가해
`[gid, zone_code, ...]`를 보냅니다. `zone_code`는 `0=floor`, `1=body_catch`,
`2=stair_relaxed`입니다. person-level 디버그 스트림에는
`/proj/<projection_id>/person/<gid>/source_zone`을 추가합니다.

결과: TD 수신기는 `gid` 기준으로 좌표와 source zone을 병합하고, `stair_relaxed`만 별도
`ty` 또는 `tz` offset/lane으로 remap할 수 있습니다.

## 2026-05-10: 계단 착석자는 별도 relaxed UV warp를 적용한다

계단 착석자는 바닥과 다른 평면에 있으므로 바닥 `image_points` homography의 `u`만 쓰면
좌우 카메라에서 사다리꼴 오차가 남습니다. 단순 `tx` offset은 전체 lane을 평행이동할 뿐,
카메라별 perspective 차이를 보정하지 못합니다.

결정: `relaxed_presence_points`는 계속 계단/착석자 detection mask로 쓰되,
선택 필드 `relaxed_presence_uv`가 있으면 그 4점을 계단 전용 projection UV rect로 다시
투영합니다. `relaxed_presence_v`가 있으면 warp 결과의 v 대신 고정 v를 사용합니다.

결과: primary OSC payload와 TD 수신 스키마는 유지하면서, 계단 actor의 `u` 위치를 카메라별
사다리꼴에 맞게 보정할 수 있습니다.

## 2026-05-11: Output Warp는 최종 projection 보정으로 저장한다

현장 projection 면과 계산된 공유 UV가 마지막에 조금 어긋나는 문제는 카메라별 Floor UV나
Stair relaxed를 다시 넓히면 hand-off/fusion 기준까지 흔들립니다.

결정: `projections[].output_warp_points`를 projection-level 4점 보정으로 저장하고,
cross-camera fusion 이후 OSC 송신과 interaction zone 평가 직전에만 적용합니다.

결과: TouchDesigner primary OSC 주소와 argument 순서는 그대로 유지하면서, 운영자는 앱
Projection Workbench의 Output Warp에서 설치 projection에 맞게 최종 actor 위치를 이동할 수 있습니다.

## 2026-05-11: OSC 좌표 지연은 카메라별 추론 worker와 telemetry로 줄인다

3대 카메라 구성에서 한 카메라의 YOLO 추론이 늦으면 main loop의 fusion/OSC 송신 기회도
함께 밀려 TouchDesigner 좌표가 사람 움직임보다 늦게 따라오는 문제가 생길 수 있습니다.

결정: `FrameGrabber`는 계속 최신 frame만 유지하고, `processing.parallel=true`에서는
카메라별 `CameraProcessor`가 YOLO tracking을 수행한 뒤 main loop로 결과를 넘깁니다.
OSC socket과 `PersonTracker`는 main loop가 단독 소유하여 schema와 gid/lost 의미를 유지합니다.

결과: 느린 카메라가 전체 OSC heartbeat를 막지 않으며, `fps_tick`에 frame age,
YOLO/track timing, dropped result, heartbeat, main-loop processing time을 남겨 이후
YOLO export나 confidence/confirm 튜닝이 실제 병목에 근거해 이뤄지게 합니다.

## 2026-05-11: 내부 dispatch 경계 held를 제거한다

cam0 -> cam2 -> cam1 경계에서 detection이 끊긴 actor를 중앙에 held로 남기면 실제 사람
움직임보다 ghost처럼 보이는 문제가 있습니다.

결정: projection 외곽 `hold_boundary_margin_uv`는 유지하되, 내부 `dispatch_uv` u 경계의
held band는 제거합니다. 경계 hand-off는 live overlap/projection-only observation과
fresh duplicate suppression으로 처리하고, 중앙에서 사라진 actor는 즉시 lost 처리합니다.

## 2026-05-11: cam2 정면 보강은 body-catch 기반 ROI crop으로 먼저 테스트한다

정면 cam2에서는 사람이 화면을 가로질러 걸을 때 옆면 실루엣으로 작게 보이고, 전체 프레임을
축소해 YOLO에 넣으면 bbox confidence와 track 안정성이 떨어질 수 있습니다. Duo 2 PoE처럼
광학 줌이 없는 넓은 화각 카메라에서는 앱의 디지털 줌보다 추론 입력 자체를 줄이는 편이
실제 검출 픽셀 수를 더 직접적으로 늘립니다.

결정: 카메라별 `body_catch_inference_crop` 옵션을 추가해, 해당 카메라의
`body_catch_points`가 있는 region에서 `body_catch_points`와 floor `image_points`의 합집합을
YOLO 입력 crop으로 사용합니다. bbox는 전체 프레임 좌표로 되돌려 기존 body-catch,
homography, fusion, OSC schema를 그대로 사용합니다.

## 2026-05-11: cam2는 보행자 tracking source에서 제외할 수 있게 한다

현장 테스트에서 정면 cam2는 보행자가 화면을 가로질러 지나갈 때 옆면 실루엣이 작게 잡히고,
중앙 crop도 유효한 보행자 픽셀을 잘라내는 경우가 있었습니다. cam2가 불안정한 detection을
만들면 cam0/cam1 hand-off보다 오히려 중앙 actor가 끊기거나 ghost처럼 보일 수 있습니다.

결정: 카메라별 `tracking_enabled` 옵션을 추가합니다. `false`인 카메라는 RTSP preview와
Calibration UI 대상으로는 남기지만 YOLO worker, fusion source, OSC actor, dispatch overlap
검증에서는 제외합니다. Projection Workbench와 Calibration 화면에서 이 값을 저장할 수 있게
하고, disabled region은 dimmed overlay로만 표시합니다.

결과: cam2는 현장 구도 확인/캘리브레이션용으로 유지하면서 보행자 actor는 cam0/cam1의
`projection_uv` overlap과 `dispatch_uv` 분담 위주로 구성할 수 있습니다.

## 2026-05-12: 야간은 Reolink ColorX + spotlight로 운용한다

야간 사이트에서 Reolink가 IR B&W로 전환되면 RGB로 학습된 YOLO의 측면 보행자 검출이
급격히 무너집니다. 같은 시간대에 ColorX/Color + 보조 spotlight 조합으로 컬러 채널을
유지하면 정면/후면 검출은 물론 cam2의 옆모습 sighting도 살아납니다.

결정: 야간 모드를 Reolink 웹 UI에서 ColorX/Color + spotlight로 고정합니다. Auto
day/night 전환에 맡기지 않습니다. 노출/게인/day-night threshold는 야간 시간대에서 한 번
튜닝합니다.

결과: 코드 변경 없이 야간 mAP이 보강됩니다. 운용 표준이므로 `docs/cam2-auxiliary-direction.md`의
설치 가정 절에도 명시합니다.

## 2026-05-12: 저조도 보강은 LAB L 채널 CLAHE로 한다

Zero-DCE, EnlightenGAN 같은 deep low-light enhancement는 720p 기준 40-200 ms를 잡아
30 FPS OSC 파이프라인에 끼울 수 없습니다. 반면 LAB colorspace의 L 채널에 적용하는
`cv2.createCLAHE`는 같은 해상도에서 ~3-5 ms로, fps 영향이 사실상 없습니다.

결정: `CamWorker.step()`에서 YOLO 추론 직전에 LAB L-채널 CLAHE를 적용합니다. config 키는
`preprocessing.clahe.{enabled, clip_limit, tile_grid}`로 두고 기본 ON, `clip_limit: 2.0`,
`tile_grid: [8, 8]`로 시작합니다. auxiliary 카메라에도 동일하게 적용합니다.

결과: 야간 측면/후방 검출이 보강되며, 코드 surface는 한 카메라 worker 안에 갇혀 있어
fusion이나 OSC schema에 영향이 없습니다. 이 키는 다음 PR에서 도입합니다.

## 2026-05-12: cam2는 auxiliary confirmer로 재활성화한다

2026-05-11에 `tracking_enabled: false`로 cam2를 보행자 tracking source에서 제외했지만,
중앙 hand-off 구간을 보강할 수 있는 유일한 카메라이기도 합니다. primary detector로
되돌리면 같은 옆모습/야간 문제로 다시 무너집니다.

결정: 카메라별 `role: primary | auxiliary` 필드를 추가합니다. cam2는
`role: auxiliary`, `tracking_enabled: true`, `dispatch_uv: []`로 운용합니다. auxiliary
카메라는 YOLO 추론은 하되 `PersonTracker`에서 새 gid를 만들지 않고 sighting buffer로만
기여합니다. raw per-cam OSC 송신도 하지 않습니다. `fusion.aux_match_uv_radius`,
`fusion.aux_match_time_window_s`로 stitch 조건을 둡니다.

결과: cam0 ↔ cam1 hand-off 안정성이 목표이며 OSC schema와 기존 gid/lost 의미는 유지됩니다.
2026-05-11 결정과 의미가 다른 축이므로 함께 보존합니다. 자세한 spec은
`docs/cam2-auxiliary-direction.md`. 이 필드와 fusion 키는 다음 PR에서 도입합니다.

## 2026-05-12: 측면 검출 보강용 사이트 footage fine-tune을 도입한다

CLAHE와 ColorX, auxiliary sighting을 모두 적용해도 cam2의 옆모습 보행자가 sighting
조차 만들지 못하는 경우가 남을 수 있습니다. 일반 COCO 학습 weight로는 사이트 특유의
조명과 각도를 따라가지 못합니다.

결정: 사이트 야간 footage 200-500 frame을 캡쳐해 pseudo-label(confidence > 0.75 자동
채택 + 수동 보정) 방식으로 라벨링하고, MPS에서 10 epoch fine-tune합니다. 산출 weight는
`models/site/best.pt`에 두고 `.gitignore`에 `models/`를 추가합니다. `model_path` config
키로 가리키며 없으면 stock `yolo26n.pt`로 fallback합니다.

결과: 검증 후 hold-out night clip에서 측면 missed-rate가 baseline 대비 줄어들면 이 결정에
수치를 덧붙입니다. 캡쳐 스크립트와 학습 절차는 다음 PR에서 추가합니다.
