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
`hold_handoff_u_edges`로 projection별 값을 직접 지정할 수 있습니다.

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
