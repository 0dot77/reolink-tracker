# reolink-tracker

Reolink 카메라 여러 대의 RTSP 스트림을 받아 YOLO26 사람 감지 + BoT-SORT 트래킹을 수행하고 OSC로 좌표를 내보내는 도구입니다. TouchDesigner, Max, Unity 같은 인터랙티브 설치 작업에서 낮은 지연 시간으로 사람 위치를 쓰기 위해 만들었습니다. 더 빠른 대안으로 ByteTrack도 선택할 수 있습니다.

## 설치

PyTorch 휠은 Python 3.11-3.12에서 안정적입니다. Python 3.14는 쓰지 않는 편이 좋습니다.

```bash
cd /Users/taeyang/Developer/tools/reolink-tracker
uv venv -p python3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

`uv`가 없으면 `pip install -r requirements.txt`를 써도 됩니다.

공유용 예시 설정을 로컬 설정으로 복사합니다.

```bash
cp config.example.yaml config.yaml
```

그 다음 `config.yaml`에 실제 카메라 URL, 비밀번호, 모델, OSC 대상 주소를 입력합니다. 카메라 IP는 공간과 네트워크 구성마다 달라질 수 있으므로 현장에서 확인한 값을 넣습니다. `config.yaml`에는 RTSP 인증 정보가 들어갈 수 있으므로 git에 커밋하지 않습니다.

첫 실행 시 `yolo26n.pt`가 작업 폴더에 다운로드됩니다. YOLO26은 Ultralytics가 2026-01-14에 공개한 모델로, NMS 없는 end-to-end inference, CPU 기준 약 43% 속도 향상, 긴 복도 끝의 작은 사람 감지에 도움이 되는 small-target STAL head가 특징입니다.

## 실행

```bash
# 헤드리스 실행, OSC만 송신
python tracker.py

# 미리보기 창 표시, 종료는 q
python tracker.py --show
```

카메라, 모델, OSC 대상은 `config.yaml`에서 수정합니다. 수정 후에는 프로그램을 다시 시작합니다.

## 현장 런처 앱

<img src="app/src-tauri/icons/icon.png" alt="Reolink Tracker 앱 아이콘" width="96" align="right">

macOS 현장 점검용 Tauri 앱은 이 저장소의 `app/` 하위 프로젝트입니다. 앱은 Python tracker를
대체하지 않고 setup, config 편집, start/stop, preview 실행, 로그 확인, 네트워크 점검 UI만 제공합니다.

```bash
cd app
npm install
npm run tauri:dev
```

앱의 Setup은 repo root의 engine 파일을 macOS app data runtime으로 복사하고, 그 runtime 안에서
Python 3.12 venv와 dependencies, YOLO model을 준비합니다. 실제 카메라 credential이 들어가는
`config.yaml`은 저장소 root가 아니라 app data의 `runtime/config.yaml`에 저장됩니다.

앱이 tracker를 시작할 때의 기본 형태는 아래와 같습니다. Show Preview는 `--show`만 추가합니다.
repo에서 직접 `python tracker.py` 또는 `$sim`을 실행할 때도 앱 runtime config가 있으면
repo-local `config.yaml` 대신 앱에서 저장한 `runtime/config.yaml`을 우선 사용합니다.

```bash
<app-data>/runtime/.venv/bin/python \
  <app-data>/runtime/engine/tracker.py \
  --config <app-data>/runtime/config.yaml
```

### 앱 아이콘과 macOS 릴리스 빌드

GitHub Actions의 `release` workflow는 macOS runner에서 `npm run tauri:build`를 실행해
`Reolink Tracker.app`을 번들링합니다. Tauri macOS 번들러는 앱 아이콘으로 `.icns` 파일을
찾기 때문에 `app/src-tauri/icons/icon.icns`를 커밋 상태로 유지해야 합니다.
`tauri.conf.json`의 `bundle.icon`에는 공용 PNG인 `icons/icon.png`와 macOS용
`icons/icon.icns`가 함께 등록되어 있습니다.

앱 아이콘을 바꿀 때는 1024x1024 PNG를 기준으로 아이콘을 다시 생성한 뒤, 최소한
`app/src-tauri/icons/icon.png`, `app/src-tauri/icons/icon.icns`,
`app/src-tauri/tauri.conf.json`의 아이콘 목록을 함께 확인합니다. `.icns`가 빠지면
macOS 릴리스 빌드가 `Failed to create app icon: No matching IconType`에서 멈춥니다.

`--show` 미리보기 키:

- `q` 또는 `Esc`: 종료 (slice 편집 중에는 편집 모드만 빠져나옵니다)
- `h`: HUD 표시 토글
- `u`: projection UV canvas 표시 토글
- `p`: 좌측 region 목록 패널 토글
- `Tab`: 페이지 전환 (`regions` / `lan`)
- `1`-`9`: focus camera 선택
- `[` / `]`: 편집 모드에서는 선택한 모서리 ±0.01 nudge, 그 외에는 focus camera 안의 focused region 순환
- `d`: focus camera에 region 그리기 시작
- 마우스 왼쪽 클릭 4회: projection 기준 top-left -> top-right -> bottom-right -> bottom-left 순서로 image point 입력
- `s`: focus camera의 선택된 region 위에 계단/착석자 relaxed presence polygon 그리기 시작
- 마우스 왼쪽 클릭 4회: 계단/착석자 bbox를 완화할 이미지 polygon 입력
- `z`: projection UV canvas에 interaction zone 그리기 시작
- 마우스 왼쪽 클릭 2회: zone rectangle의 두 모서리 입력
- `Backspace`: region 그리기 중 마지막 점 취소
- `x`: focus camera의 마지막 region 삭제 (zone 편집 중이면 선택 zone 삭제)
- `e`: focus camera region의 `projection_uv` / `dispatch_uv` slice 편집 모드 시작 / 다음 region으로 순환 / 마지막 region 이후엔 종료
- `v`: interaction zone 편집 모드 시작 / 다음 zone으로 순환 / 마지막 zone 이후엔 종료
- `t`: 편집 대상 토글 — `projection_uv` ↔ `dispatch_uv`
- `g`: 편집할 모서리 순환 — `u0 → v0 → u1 → v1`
- `,` / `.`: 선택한 모서리 -0.05 / +0.05 nudge (편집 모드 한정)
- `r`: 편집 중인 slice 초기화 (`projection`은 `[0,0,1,1]`로, `dispatch`는 `projection_uv`와 같게)
- `w`: 현재 region 편집 내용을 로컬 `config.yaml`에 저장 (status bar의 `[unsaved]`가 `[saved]`로 바뀜)

기본 미리보기는 현장 검증용 dashboard입니다. 상단 status bar는 active/held/spawned/handoff/lost 상태를 요약하고, UV canvas에는 fused `gid`, trail, velocity 방향, fresh/held 상태가 표시됩니다. 우측 패널의 `regions` 탭은 카메라 상태와 active gid table을 보여줍니다. `>`는 focus camera, `*`는 focused region을 가리키고, 상단에 `[unsaved]`/`[saved]` 와 `overlap: N` 카운터가 같이 떠서 저장 누락이나 dispatch 충돌을 빠르게 알 수 있게 합니다. 충돌이 있으면 UV canvas 패널 좌하단에도 `cam0:near_half <-> cam1:far_half` 같은 줄이 빨간색으로 떠서 어떤 쌍이 겹치는지 알려줍니다.

`lan` 페이지는 현재 Mac의 물리 네트워크 서비스와 활성 IPv4 대역을 전체 화면으로 시각화합니다. 여러 USB/Thunderbolt Ethernet 어댑터, 프로젝터용 직결 LAN, 라우터 경유 LAN이 동시에 붙어 있을 때 `en*` 장치명, macOS 서비스명, IP/prefix, 기본 gateway를 한 화면에서 구분할 수 있습니다. `config.yaml`의 RTSP 카메라 host도 함께 표시해서 각 카메라 target이 어느 인터페이스로 라우팅되는지, 같은 subnet인지, ARP에 보였는지 빠르게 확인할 수 있습니다.

## 현장 캘리브레이션 순서

현장에서는 카메라별 입력 보정과 최종 projection 보정을 분리해서 잡습니다. 카메라 homography를
프로젝션 위치 보정용으로 억지로 늘리면 fusion과 hand-off 기준까지 같이 흔들리므로, 아래 순서로
작업합니다.

1. **Floor UV**: 실제 보행 바닥면의 4점을 먼저 찍습니다.
   `image_points`는 사람 발점이 공유 projection UV로 변환되는 기준입니다. 멀리 있는 사람 bbox를
   더 받으려고 바닥면을 넓히지 말고, 바닥의 실제 평면만 정확히 맞춥니다.
2. **Body catch**: Floor UV 주변에서 사람 bbox를 더 넓게 받기 위한 보조 polygon을 잡습니다.
   이 영역은 검출을 구제할 뿐 위치 계산은 계속 Floor UV homography를 사용합니다.
   cam2처럼 정면 보강 카메라에서 사람이 옆모습으로 작게 잡히는 경우에는 먼저
   `tracking_enabled: false`로 preview/calibration-only 상태를 만들고 cam0/cam1의
   `projection_uv`/`dispatch_uv` 분담을 조정합니다. `body_catch_inference_crop`은 cam2 tracking을
   다시 켜서 비교할 때만 쓰는 테스트 옵션입니다.
3. **Stair relaxed**: 계단, 착석자, 낮거나 넓게 잡히는 bbox 전용 mask를 잡습니다.
   필요하면 `relaxed_presence_uv`와 `relaxed_presence_v`로 stair plane의 별도 perspective와
   고정 projection row를 보정합니다.
4. **Camera Fit**: 카메라별 `projection_uv`와 `dispatch_uv`를 정리합니다.
   `projection_uv`는 관찰과 hand-off 여유 구간이고, `dispatch_uv`는 실제 gid/OSC actor를 만드는
   ownership 구간입니다. 여러 카메라의 `dispatch_uv`는 겹치지 않게 맞춥니다.
5. **Output Warp**: 마지막으로 설치 projection 위의 인터랙션 위치를 맞춥니다.
   `projections[].output_warp_points`는 fusion 이후 OSC 송신과 interaction zone 평가 직전에만
   적용되는 projection-level 4점 보정입니다. 앱 Projection Workbench의 `Output Warp` 모드에서
   핸들을 움직이면 같은 OSC 주소와 argument 순서로 보정된 위치가 나갑니다.

빠른 라이브 반응이 필요하면 `detection_filter.confirm_hits`, `confirm_window_s`,
`fusion.position_alpha`를 먼저 봅니다. `fusion.miss_buffer_frames`는 사람이 사라졌을 때 lost 처리까지
기다리는 프레임 수라서 첫 등장 지연을 줄이지 않습니다.

## AI / GitHub 작업 방식

이 레포는 AI 코딩 도구가 채팅 기록에만 의존하지 않도록 프로젝트 맥락을 버전 관리되는 Markdown 파일에 저장합니다.

- `docs/product.md`: 제품 목적, 사용자, MVP, 비목표
- `docs/tech.md`: 아키텍처와 검증 방법
- `docs/ai-rules.md`: AI 에이전트가 따라야 할 안정적인 규칙
- `docs/decisions.md`: 중요한 결정과 번복 기록

혼자 작업하더라도 GitHub Issue를 작은 작업 단위로 쓰고, Pull Request에서 변경 diff를 확인하는 흐름을 권장합니다. PR 화면은 AI가 만든 변경을 `main`에 넣기 전에 검토하는 장소입니다.

## OSC 스키마

좌표는 `projection_id`로 식별되는 **공유 projection UV 공간**으로 송신됩니다. 같은 실제 바닥 프로젝션을 보는 카메라들은 같은 `projection_id`를 공유하며, 각 카메라가 내보내는 `(u, v)` 값은 서로 비교할 수 있습니다.

### TouchDesigner minimal (권장, 기본 ON)

운영용 기본값은 `osc.td_minimal: true`입니다. projection마다 고정 주소를 보내고, `person_level: true`이면 per-person 디버그 주소도 함께 보냅니다.

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/active` | `[gid, ...]` | 활성 gid 목록, heartbeat마다 |
| `/proj/<projection_id>/person_zones` | `[gid, zone_code, gid, zone_code, ...]` | actor source zone, heartbeat마다 |
| `/proj/<projection_id>/xy` | `[gid, x, y, gid, x, y, ...]` | 활성 person 위치, heartbeat마다 |
| `/proj/<projection_id>/uv` | `[gid, u, v, gid, u, v, ...]` | 활성 person UV 위치, heartbeat마다 |
| `/proj/<projection_id>/persons/count` | int | heartbeat마다 |

`x/y`는 `projections[].pixel_size`가 있으면 projection pixel 좌표이고, 없으면 0..1 UV입니다.
`u/v`는 항상 0..1 정규화 좌표입니다. TD 패치가 variable-length `/xy`를 전부 unpack하지
못하거나 pixel/UV 단위가 헷갈릴 때는 `/uv` 또는 `person_level: true`의
`/proj/<projection_id>/person/<gid>` 스트림으로 확인합니다.

`/person_zones`는 좌표 payload를 바꾸지 않고 TD가 actor lane을 분기할 수 있게 하는
보조 metadata입니다. `zone_code`는 `0=floor`, `1=body_catch`, `2=stair_relaxed`입니다.
계단/착석자처럼 `relaxed_presence_points`에서 승격된 actor는 `stair_relaxed`로 나가며,
TD에서는 이 코드로 보행자와 착석자의 y lane을 따로 remap할 수 있습니다.

### Person-keyed debug (`osc.td_minimal: false`)

`gid`는 cross-camera fusion이 만드는 global person ID입니다. 한 사람이 cam0 dispatch 슬라이스에서 cam1 같은 다음 enabled dispatch 슬라이스로 넘어가도 같은 `gid`가 유지되도록 fusion 레이어가 hand-off를 stitch 합니다 (UV 거리 + 시간 윈도우 기반). 짧은 detection drop 중에는 허용된 edge/relaxed 조건에서만 gid가 `held` 상태로 남고, 내부 dispatch 경계에서 끊긴 중앙 actor는 바로 `/lost` 처리됩니다.

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/person/<gid>` | `u, v, vx, vy, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | 활성 person마다 매 프레임 |
| `/proj/<projection_id>/person/<gid>/source_zone` | `zone_code, zone_name` | 활성 person마다 매 프레임 |
| `/proj/<projection_id>/person/<gid>/lost` | 없음 | hand-off 윈도우(기본 0.4 s) 안에 다른 카메라가 받지 못하면 한 번 |
| `/proj/<projection_id>/persons` | `[gid, ...]` | 활성 gid 목록(fresh + held), 매 프레임 |
| `/proj/<projection_id>/persons/count` | int | 매 프레임 |

`(vx, vy)`는 fusion이 EMA로 산출한 UV 단위/초 속도입니다. 정지 상태에서는 0에 수렴.
`fusion.position_alpha`는 person 좌표 자체의 EMA 가중치입니다. 기본 예시값 `0.75`는
약간의 jitter를 허용하고 인터랙션 포인트가 더 빠르게 따라오도록 한 fast live 설정입니다.
`fusion.max_update_jump_uv`는 같은 `gid`의 다음 관측점이 지정 UV 거리보다 멀리 튈 때
순간이동으로 보고 새 `gid`로 분리합니다. 바닥 인터랙션에서 OSC actor가 projection 면을
갑자기 가로지르는 경우를 줄이기 위한 안전장치입니다.

카메라 region에 `body_catch_points`를 추가하면 bbox 전체가 이 보조 polygon과 겹칠 때
발점이 바닥 `image_points` 경계 밖으로 살짝 빠져도 actor 후보로 살릴 수 있습니다. 이
polygon은 검출 보조용이고, OSC 위치는 계속 `image_points` homography의 foot point로
계산됩니다. 낮은 confidence 완화는 `body_catch_min_confidence`까지만 적용되며, bbox
크기/비율 필터와 confirm window는 그대로 통과해야 합니다. 단, 중앙 원거리 보행자처럼
`too-small`로 떨어지는 bbox는 region의 `min_bbox_height_px`와 relaxed area floor를 통과하면
body catch가 actor 후보로 살릴 수 있습니다.

계단에 앉은 사람처럼 bbox가 세로형으로 잡히지 않는 구간은 같은 region 안에
`relaxed_presence_points`를 추가합니다. `image_points`는 계속 바닥/projection UV 변환용
4점이고, `relaxed_presence_points`는 bbox가 그 polygon과 겹칠 때만 완화된 bbox 비율
기준을 적용하는 별도 마스크입니다. 이 경로에서도 confirm window는 유지됩니다.
앱 Calibration UI에서 Stair relaxed 사용을 끄면 `relaxed_presence_enabled: false`가 저장되고,
기존 계단 polygon/UV/fixed-v 값은 남지만 tracker는 이 경로를 트래킹에 쓰지 않습니다.
`relaxed_presence_uv`를 설정하면 이 polygon 4점을 계단 전용 UV rect로 다시 투영해서
좌우 카메라의 사다리꼴 오차를 보정합니다. 값이 없으면 기존처럼 바닥 homography의 `u`를
사용합니다. `relaxed_presence_v`가 있으면 최종 `v`는 그 값으로 고정되고, 없으면 계단 전용
UV rect 또는 projection 범위 안으로 clamp됩니다. TouchDesigner OSC payload 순서는 바뀌지
않습니다. 설정 alias로 `stair_catch_points`도 읽을 수 있지만 저장할 때는
`relaxed_presence_points`로 정규화됩니다.

### Interaction zones

`projections[].interaction_zones`는 카메라 image region이 아니라 공유 projection UV canvas 위에 놓는 사각형 인터랙션 영역입니다. fused person 좌표가 zone 안에 들어오면 TD 인스턴싱용 zone-local stream이 추가로 송신됩니다.

`projections[].output_warp_points`는 cross-camera fusion이 끝난 뒤 OSC 송신과 interaction zone
판정 직전에 적용되는 최종 4점 보정입니다. 순서는 `top-left`, `top-right`,
`bottom-right`, `bottom-left`이며, TD 쪽 주소나 argument 순서를 바꾸지 않고 `/uv`, `/xy`,
`/person/<gid>` 위치값만 설치 projection에 맞게 이동합니다. 계단/착석자용
`relaxed_presence_uv`는 카메라별 stair plane 보정이고, `output_warp_points`는 projection 전체
출력 보정이므로 서로 다른 용도로 둡니다.

```yaml
projections:
  - id: corridor
    pixel_size: [9600, 1080]
    output_warp_points: [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    interaction_zones:
      - id: center
        uv_rect: [0.35, 0.15, 0.65, 0.85]
        release_after_s: 0.6
```

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>` | `u, v, zone_u, zone_v, vx, vy, dwell_s, presence, state_code` | zone 안의 person마다 heartbeat |
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>/enter` | `zone_u, zone_v` | zone 진입 시 한 번 |
| `/proj/<projection_id>/zone/<zone_id>/person/<gid>/leave` | `reason_code, dwell_s` | zone 이탈 또는 stale release 시 한 번 |
| `/proj/<projection_id>/zone/<zone_id>/count` | int | zone별 active count heartbeat |

`zone_u`, `zone_v`는 zone rectangle 안의 0..1 local 좌표입니다. `state_code`는 `1=fresh`, `0=held`이며, held 상태에서는 `presence`가 `release_after_s` 동안 1에서 0으로 감소한 뒤 stale leave가 한 번 나갑니다. `reason_code`는 `1=exited`, `2=stale`, `3=zone_removed`입니다.

`fusion.hold_boundary_margin_uv`가 0보다 크면 tracking을 놓친 gid는 projection 가장자리 근처에서만 `held`로 유지됩니다. 예시값 `0.08`은 UV 가장자리 8% 안쪽에서만 held를 허용하고, 영역 중앙에서 놓친 gid는 바로 `/lost` 처리해 화면 한가운데 ghost actor가 남지 않게 합니다. cam0 -> cam2 -> cam1 같은 내부 dispatch handoff는 중앙 held band를 만들지 않습니다. 같은 사람이 겹치는 구간에서 계속 관측되면 live overlap/projection-only observation과 fresh duplicate suppression으로 stitch하고, 중앙에서 실제로 끊긴 actor는 즉시 사라집니다.

### Detection filter / actor confirmation

`detection_filter`는 YOLO raw person box가 곧바로 actor가 되는 것을 막는 후처리입니다.
가방, 그림자, ROI 경계부, 한 프레임짜리 검출이 OSC identity를 만들지 않도록
confidence, bbox 크기, bbox 비율을 검사하고 `confirm_hits`만큼 반복 검출된 뒤에만
person/fusion 이벤트로 넘깁니다.
야간처럼 confidence가 낮게 나오는 현장에서는 상단 YOLO `conf`를 `0.22` 안팎으로 낮추고,
`detection_filter.min_confidence`로 actor 승격 기준을 잡는 편이 더 안정적입니다.

```yaml
detection_filter:
  enabled: true
  min_confidence: 0.28
  min_bbox_height_px: 42
  min_bbox_area_px: 900
  min_aspect_h_over_w: 1.15
  max_aspect_h_over_w: 5.8
  max_width_over_height: 1.05
  projection_inner_margin_uv: 0.0
  relaxed_min_confidence: 0.12
  relaxed_min_bbox_height_px: 24
  relaxed_min_bbox_area_px: 500
  relaxed_min_aspect_h_over_w: 0.45
  relaxed_max_aspect_h_over_w: 6.5
  relaxed_max_width_over_height: 2.4
  confirm_hits: 2
  confirm_window_s: 0.35
```

값을 강하게 잡을수록 오검출 actor는 줄지만, 멀리 있거나 어두운 사람의 진입 반응은
늦어질 수 있습니다. 실제 설치에서는 `--show` viewer에서 raw 트랙과 person stream을
보면서 `min_bbox_height_px`, `confirm_hits`, `confirm_window_s`를 먼저 조정합니다.
`fusion.miss_buffer_frames`는 사라짐/lost 반응을 바꾸는 값이고 첫 등장 지연은 줄이지 않습니다.

### Raw per-cam (호환/디버깅)

`osc.raw_per_cam: true` (기본값)이면 카메라별 트랙도 함께 나갑니다. `id`는 카메라 안에서만 안정적이라 cross-camera 인터랙션에는 person-keyed 채널을 권장합니다.

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/cam/<cam_name>/track/<id>` | `u, v, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | 발 위치가 region polygon 안이고 `dispatch_uv` 안일 때 매 프레임 |
| `/proj/<projection_id>/cam/<cam_name>/track/<id>/lost` | 없음 | ID가 사라지거나 dispatch 영역을 벗어났을 때 한 번 |
| `/proj/<projection_id>/cam/<cam_name>/count` | int | 카메라별 count, 매 프레임 |
| `/proj/<projection_id>/cam/<cam_name>/active` | id 목록 | 활성 ID가 있을 때 매 프레임 |

`(u=0, v=0)`은 projection의 좌상단, `(u=1, v=1)`은 우하단입니다.

`osc.legacy_image_space: true`를 `config.yaml`에 설정하면 예전 image-space 메시지(`<cam_prefix>/track/<id>`와 `cx, cy, w, h, conf`)도 함께 송신합니다. 기본값은 꺼져 있습니다.

## TouchDesigner 수신

- **OSC In DAT**(주소 라우팅) 또는 숫자 스트림용 **OSC In CHOP**를 추가합니다.
- Network Port는 `config.yaml`의 포트와 맞춥니다. 기본값은 `7000`입니다.

### 권장: person 스트림 패턴

```
OSC In DAT
  → onReceiveOSC: address가 /proj/corridor/person/*/lost면 active 테이블에서 gid 제거
                  /proj/corridor/person/<gid>면 [u, v, vx, vy, conf]를 테이블에 업서트
  → Table DAT (gid, u, v, vx, vy, conf)
  → DAT to CHOP, 또는 Lookup CHOP로 인터랙션 슬롯에 매핑
```

`/persons/count`로 인원 수를 그대로 받고, `/persons` 배열로 현재 살아있는 gid 목록을 동기화합니다. 기본값에서는 완전히 lost된 gid를 재사용하므로, TD 테이블/채널 수는 총 방문자 수가 아니라 동시 점유 피크에 가깝게 유지됩니다.

이 저장소의 `touchdesigner/person_table_receiver.py`는 기본 minimal `/uv` 수신기에
`/person_zones`를 병합합니다. 출력 `person_table`은 숫자 컬럼
`gid, tx, ty, tz, active, zone_code`를 쓰며, 기본값으로 `zone_code=2`
(`stair_relaxed`)만 `tz` offset을 받아 기존 XZ plane 인스턴싱에서 보행자 lane과
분리됩니다. 필요하면 파일 상단의 `STAIR_TZ_OFFSET`을 설치 TD 좌표계에 맞게 조정합니다.

### Raw per-cam wildcard (디버깅용)

```
/proj/corridor/cam/*/track/*
```

각 카메라가 보고 있는 raw 트랙을 보고 싶을 때 씁니다. cross-camera 경계에서 ID가 끊기는 게 정상입니다.

## 캘리브레이션 / region 설정

`viewer.py`는 `--show`에서 focus camera의 region을 4점 클릭으로 추가하고 `w`로 로컬 `config.yaml`에 저장할 수 있습니다. 새로 그린 region은 기본적으로 첫 projection의 전체 UV `[0.0, 0.0, 1.0, 1.0]`로 만들어집니다. 여러 카메라가 한 projection을 나눠 담당하는 설치에서는 viewer 안에서 직접 slice를 조정할 수 있습니다. `e`로 편집 모드를 시작해 region을 선택하고 `t`로 `projection_uv` / `dispatch_uv` 대상을 전환, `g`로 모서리(`u0` `v0` `u1` `v1`)를 고른 뒤 `[` `]`(±0.01) 또는 `,` `.`(±0.05)로 값을 움직입니다. UV canvas에서는 `projection_uv`가 점선 외곽 + 옅은 채움으로, `dispatch_uv`가 진한 채움 + 실선으로 구분되어 보이고, 편집 중인 slice에는 강조선이 추가됩니다. `dispatch_uv`가 `projection_uv`의 부분집합이 아니게 되는 변경은 status 메시지로 거부되고 `w`로 저장되는 값에는 반영되지 않습니다.

계단 착석자용 relaxed presence polygon은 먼저 `[` / `]`로 기준 floor region을 선택한 뒤
`s`를 눌러 같은 카메라 화면에 4점으로 따로 그립니다. 저장된 polygon은
`relaxed_presence_points`로 들어갑니다. `relaxed_presence_uv`를 설정하면 이 4점으로
계단 전용 homography를 만들고, 설정하지 않으면 기존 `image_points`의 `u` 위치를
사용합니다. 앱 캘리브레이션 화면에서는 계단 전용 `relaxed_presence_uv`와
`relaxed_presence_v`도 같이 저장할 수 있습니다.

Interaction zone은 `z`로 UV canvas에서 두 모서리를 클릭해 추가하고, `v`로 선택/순환한 뒤 region slice와 같은 `g`, `[`, `]`, `,`, `.` 키로 rectangle edge를 조정합니다. UV canvas에는 zone rectangle, zone id, 현재 zone 안의 active gid count가 함께 표시됩니다. `config.example.yaml`에는 공유 가능한 예시 구조만 두고, 실제 RTSP URL과 비밀번호가 들어가는 `config.yaml`은 로컬에만 유지합니다.

region의 4개 `image_points`는 **projection-UV 방향** 기준으로 입력합니다.

```text
top-left -> top-right -> bottom-right -> bottom-left
```

이는 카메라 이미지에서 보이는 순서가 아니라 projection/world 기준 순서입니다. 그래서 서로 마주 보거나 중앙에서 내려다보는 카메라들이 서로 다른 방향으로 보이더라도 같은 공유 UV 프레임으로 매핑할 수 있습니다.

예시:

```yaml
cameras:
  - name: cam0
    regions:
      - id: near_half
        projection_id: corridor
        image_points: [[120, 90], [760, 95], [790, 470], [100, 460]]
        projection_uv: [0.0, 0.0, 0.55, 1.0]
        dispatch_uv: [0.0, 0.0, 0.50, 1.0]
        min_bbox_height_px: 24
```

## 다중 카메라 캘리브레이션 절차

복도 양쪽 끝 카메라(cam0/cam1)를 주 tracking source로 잡고, 중앙 카메라(cam2)는 필요할 때만 preview/calibration-only 또는 보조 tracking source로 켜는 절차입니다. 모든 카메라가 같은 `projection_id`를 공유하고, `projection_uv`는 hand-off 후보를 위해 겹쳐도 되지만 enabled camera의 `dispatch_uv`는 절대 겹치면 안 됩니다(같은 사람이 두 OSC stream으로 동시에 나갑니다). `tracking_enabled: false`인 카메라는 headless 실행의 dispatch overlap 검증과 actor 생성에서 제외됩니다.

1. 먼저 `config.yaml`에 모든 카메라가 같은 `projection_id`를 쓰도록 항목을 만들어 둡니다 (`projections:` 아래에 `corridor` 같은 id를 한 번만 정의하고, 각 카메라가 그 id를 region에 씁니다).
2. `python tracker.py --show`로 viewer를 띄우고 `1`로 cam0을 focus 합니다. 좌측 패널 상단이 `[saved]`인지 확인하고, `d` -> 카메라 화면에서 projection 기준으로 top-left -> top-right -> bottom-right -> bottom-left 순서로 4점을 클릭합니다. 이 카메라는 가까운 절반을 담당할 것이므로 4점은 카메라가 실제로 잘 보이는 가까운 영역만 둘러쌉니다.
3. `w`로 일단 저장합니다. 새 region은 `projection_uv = [0.0, 0.0, 1.0, 1.0]`, `dispatch_uv`도 동일하게 만들어집니다. `[saved]`가 떠야 디스크에 반영된 상태입니다.
4. 에디터에서 `config.yaml`의 cam0 region을 열어 분담 범위를 줄입니다. 예를 들어 cam0이 복도 왼쪽 절반이면 `projection_uv: [0.0, 0.0, 0.55, 1.0]`, `dispatch_uv: [0.0, 0.0, 0.50, 1.0]`로 바꿉니다. `projection_uv`는 살짝 더 넓게(0.55) 두고 `dispatch_uv`는 안전 마진(0.50)을 두면 카메라 끝부분 떨림이 송신에 영향을 덜 줍니다.
5. cam1도 같은 방식으로 진행합니다. viewer에서 `2`로 focus를 옮긴 뒤 `d`로 region을 그립니다. 이 카메라는 마주 보는 방향이라 같은 바닥인데도 영상이 좌우 반전돼 보이지만, 4점 클릭 순서는 여전히 **projection-UV 기준** top-left -> top-right -> bottom-right -> bottom-left 입니다. cam0과 동일한 `projection_id`를 쓰도록 `config.yaml`을 정리합니다.
6. cam2가 정면/옆모습 문제로 불안정하면 `tracking_enabled: false`로 둡니다. 앱 Projection Workbench에서는 cam2 overlay가 흐리게 보이고, seam/dispatch 판단은 cam0/cam1 기준으로 계산됩니다. 시작점은 cam0 `dispatch_uv [0.00, 0.0, 0.50, 1.0]`, cam1 `dispatch_uv [0.50, 0.0, 1.00, 1.0]`입니다.
7. 저장 후 viewer를 다시 시작하면 좌측 패널에 모든 카메라의 region 목록이 보이고, enabled camera 기준 상단 `overlap` 카운터가 `0`이어야 합니다. UV canvas 패널 좌하단에 빨간 경고가 뜨면 어떤 쌍의 `dispatch_uv`가 겹치는지 알려주므로 그 부분만 다시 분할 비율을 조정합니다.
8. 마지막으로 사람이 한 명 복도 끝에서 끝까지 천천히 걸어가게 하고, `python tracker.py --show` dashboard에서 같은 `gid` 색상이 유지되는지 확인합니다. 중앙 경계에서 detection이 실제로 끊기면 held로 붙잡지 않고 `/lost`가 나가는 것이 정상입니다. 같은 사람이 여러 카메라에서 동시에 송신되는 구간이 있으면 `dispatch_uv`가 아직 겹치는 것이므로 4번~6번 단계로 돌아갑니다.

## 다중 카메라 네트워크 구성

도구는 `config.yaml`에 적힌 RTSP URL만 사용합니다. 카메라 IP는 고정된 프로젝트 값이 아니라 현재 공간의 네트워크에서 받은 값입니다. 장소를 옮기거나 직결/공유기 구성을 바꾸면 Reolink 앱, 공유기 DHCP 목록, 또는 `arp`로 IP를 다시 확인한 뒤 `config.yaml`을 갱신합니다.

아래 중 하나를 선택하면 됩니다.

1. **카메라를 라우터에 연결**: 추천 방식입니다. 카메라를 iPTIME LAN 포트에 꽂으면 DHCP로 `172.30.1.x` 대역 IP를 받고, Mac은 Wi-Fi나 LAN으로 접속합니다.
2. **카메라를 스위치로 직접 연결**: Mac의 USB-Ethernet 어댑터에 작은 unmanaged 5-port switch를 연결합니다. Mac에서 DHCP를 제공해야 하므로 System Settings -> General -> Sharing -> Internet Sharing을 사용합니다. 이 경우 카메라는 보통 `192.168.2.x` 같은 주소를 받습니다.
3. **하나는 직접 연결, 하나는 Wi-Fi 경유**: 동작은 가능하지만 경로가 섞여 설정이 복잡해집니다.

## 지연 시간 튜닝

포함된 FFmpeg 옵션(`nobuffer`, `low_delay`, `max_delay=500ms`, `reorder_queue=0`)과 `BUFFERSIZE=1` 조합은 sub stream 기준 약 200-400 ms glass-to-OSC 지연을 목표로 합니다. 더 낮추려면 Reolink Client -> Display -> Stream -> Frame Interval에서 keyframe interval을 1초로 낮춥니다.

## 자주 생기는 문제

- **검은 미리보기 / `read failed`**: URL을 `ffprobe`로 확인합니다.
  ```bash
  ffprobe -rtsp_transport tcp 'rtsp://admin:%21pass@1.2.3.4:554/h264Preview_01_sub'
  ```
- **401 Unauthorized**: username은 장치 별칭이 아니라 `admin`입니다.
- **`h264Preview_01_main`이 디코딩되지 않음**: 많은 Reolink 모델은 main이 H.265, sub가 H.264입니다. `_sub`를 사용합니다.
- **MPS `not implemented` warning**: ultralytics를 업그레이드하거나 `config.yaml`에서 `device: cpu`로 설정합니다.
- **Track ID가 자주 튐**: `bytetrack.yaml`은 더 빠르지만 덜 부드럽습니다. 기본값은 `botsort.yaml`입니다. 가림이 심하면 StrongSORT 같은 별도 대안을 검토합니다.

### `--show` 미리보기 창이 안 보일 때

`python tracker.py --show`를 실행했는데 카메라 처리 로그(`fps=...`)는 정상이지만 viewer 창이 보이지 않을 때 macOS에서는 아래 순서로 확인합니다.

1. 터미널 stdout에 `[viewer] window 'reolink-tracker' opened (use q or Esc to quit)` 라인이 나왔는지 확인합니다. 이 라인이 있으면 cv2 window는 이미 떠 있는 상태입니다.
2. Mission Control(F3) 또는 Dock의 Python/터미널 아이콘에서 `reolink-tracker` 창을 찾습니다. 다른 풀스크린 앱 뒤로 넘어갔을 수 있습니다.
3. `[viewer] failed to open cv2 window`나 `imshow failed` 같은 stderr 메시지가 있으면 cv2 GUI 빌드 문제입니다. `python -c "import cv2; print(cv2.getBuildInformation())" | grep -i gui`로 GUI 항목을 확인합니다. `opencv-python-headless`가 깔려 있으면 `pip uninstall opencv-python-headless && pip install opencv-python`으로 교체합니다.
4. SSH 세션이나 nohup 같은 터미널 분리 환경에서는 macOS 그래픽 세션과 연결되지 않을 수 있습니다. 로컬 Terminal.app 또는 iTerm 세션에서 직접 실행합니다.
5. `q`나 `Esc`를 눌러도 종료되지 않으면 cv2 창에 포커스가 가지 않은 상태입니다. 창을 한 번 클릭해 포커스를 준 뒤 다시 누르거나, 터미널에서 `Ctrl+C`로 SIGINT를 보냅니다.
