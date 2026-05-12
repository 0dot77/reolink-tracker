# Cam2 Auxiliary Confirmer 방향

## 목적

청계천 설치 구간은 `cam0`이 projection 왼쪽에서 오른쪽을, `cam1`이 오른쪽에서 왼쪽을
바라보는 2카메라 구조다. 중앙 hand-off 구간을 보강하려고 `cam2`를 같은
`projection_id`에서 primary detector로 시도했지만, 두 차례 뒤집혔다.

- 2026-05-11 `body_catch_inference_crop`: cam2가 옆모습 보행자를 작게 잡는 문제를 ROI
  crop으로 줄이려 했으나, 중앙 crop이 유효 픽셀을 잘라내는 경우가 있었다.
- 2026-05-11 `tracking_enabled: false`: cam2의 detection이 cam0/cam1 hand-off보다 오히려
  중앙 actor를 끊거나 ghost로 만들어, 보행자 tracking source에서 제외했다.

근본 원인은 세 가지다. 첫째, cam2는 사람의 옆모습을 본다. YOLO는 정면/후면에 비해
옆모습 silhouette의 anchor/aspect-ratio 분포에서 안정성이 떨어진다. 둘째, 야간 조도가
낮으면 Reolink가 IR B&W로 전환하면서 RGB로 학습된 YOLO가 더 무너진다. 셋째, 이전
baseline이 `_sub` stream + `yolo26n.pt` + `imgsz: 640`이라 중앙의 작은 옆모습 사람은
모델 입력에서 이미 픽셀 수가 부족하다.

새 방향은 cam2를 primary detector로 되돌리지 않는다. cam2는 **auxiliary confirmer**로만
쓴다. YOLO 추론은 돌리되, 새 `gid`도 raw per-cam OSC도 만들지 않고, primary 카메라가
hand-off 윈도우에서 사람을 놓칠 때 "방금 cam2가 같은 projection 근처에서 봤다"는 sighting
buffer만 fusion에 흘려보낸다.

이 방향은 현재 코드에 `role: auxiliary`와 `fusion.aux_match_*`로 반영되어 있다. 단,
스폿라이트를 쓸 수 없는 현재 현장 조건에서는 먼저 `imgsz: 1280` + `yolo26s.pt` 조합을
기준 baseline으로 잡고, cam2 auxiliary가 그 baseline 대비 중앙 hand-off를 실제로 줄이는지
비교한다.

## 역할 분리

기존 2026-05-11 결정의 `tracking_enabled: false`와 헷갈리지 않도록 두 축을 분리한다.

- `tracking_enabled: false` — YOLO 모델 자체를 로드하지 않는다. RTSP preview와
  calibration UI 대상으로만 남는다. 현장 카메라 구도 확인용.
- `role: auxiliary` + `tracking_enabled: true` — YOLO 추론은 한다. 그러나 `PersonTracker`에서
  새 gid를 만들지 않고, raw per-cam OSC 송신도 하지 않는다. fusion sighting buffer로만
  기여한다.
- `role: primary` (기본값) — 기존 동작. 새 gid 생성, raw per-cam OSC 송신.

cam2의 운영 상태는 다음과 같이 단계적으로 갈 수 있다.

1. `tracking_enabled: false` — calibration only.
2. `role: auxiliary`, `tracking_enabled: true` (현재 권장) — sighting만.
3. `role: primary` (옛 방향) — 다시 채택하지 않는다.

## 설치 가정

- `cam2`는 도면 중앙 위치. 중앙 hand-off 구간을 가능한 한 사선으로 바라봐서 정면 옆모습
  보행자가 카메라 화면을 가로지르는 비율을 줄인다.
- 스폿라이트는 현재 현장에서 쓸 수 없다. 야간 모드는 가능하면 Auto B&W 전환을 피하고
  Color/RGB 상태를 유지하되, 실제 저조도 RGB frame에서 raw YOLO detection이 살아나는지를
  먼저 확인한다. 자세한 단기 실험 순서는 `docs/cam2-low-light-research.md`를 따른다.
- 모든 카메라는 같은 `projection_id`를 공유한다.

## Config 예시

```yaml
cameras:
  - name: cam2
    role: auxiliary           # 새 필드, 기본은 primary
    tracking_enabled: true    # YOLO 추론은 한다
    regions:
      - projection_id: floor
        image_points: [...]
        projection_uv: [0.30, 0.0, 0.70, 1.0]
        dispatch_uv: []       # 빈 배열 = primary OSC 송신 안 함

fusion:
  aux_match_uv_radius: 0.08
  aux_match_time_window_s: 0.5
```

`fusion.aux_match_uv_radius`는 primary에서 lost로 가려는 gid의 마지막 UV와 auxiliary
sighting의 UV가 이 거리 안에 있을 때만 stitch한다. `fusion.aux_match_time_window_s`는
sighting buffer에서 같은 시간 윈도우 안의 항목만 후보로 본다.

## OSC contract 영향

변경 없음.

- cam2는 person-level `/proj/<projection_id>/person/<gid>` stream에 sighting으로만 기여한다.
  TouchDesigner 수신 측에서 보이는 것은 더 안정적인 hand-off 결과뿐이다.
- cam2는 raw per-cam 주소(`/proj/<projection_id>/cam/cam2/track/<id>` 등)를 송신하지
  않는다. 이는 `osc.raw_per_cam: true`이더라도 auxiliary 카메라에는 적용되지 않는다.
- `/persons/count`, `/persons`, `/person/<gid>/lost`는 기존 의미 그대로 유지된다.

## 현장 검증 절차

비교 기준은 예전 `640+n` baseline이 아니라, 먼저 실행한 `_sub + imgsz 1280 + yolo26s.pt`
2카메라 baseline이다.

1. 앱에서 cam2를 `role: auxiliary`, `tracking_enabled: true`, `dispatch_uv: []`로 설정한다.
2. Projection Workbench에서 dispatch overlap 경고가 없는지 확인한다 (auxiliary는 dispatch가
   비어 있으므로 자연히 통과).
3. 한 사람이 `cam0 -> cam2 시야 -> cam1` 방향으로 천천히 걸으며 같은 `gid`가 유지되는지
   본다. 반대 방향도 확인.
4. `$sim`으로 20초 projection usage 영상을 만들고 다음 지표를 baseline(2카메라)과 비교한다.
   - `spawned_gids`
   - `lost_count`
   - `handoff_count`
5. TouchDesigner 수신 측에서 중앙 구간 통과 시 `/persons/count`가 0으로 떨어지거나 2로
   부풀지 않는지 확인한다.

## 성공 기준

- 같은 사람이 중앙을 통과할 때 `gid`가 끊기는 빈도가 baseline 대비 낮아진다.
- `spawned_gids`와 `lost_count`가 baseline보다 낮아진다.
- cam2 추가로 인한 `/persons/count` 부풀음이 발생하지 않는다.
- cam2의 raw per-cam 주소가 OSC에 등장하지 않는다.

## 남은 리스크

- cam2의 옆모습 검출이 sighting조차 만들지 못할 만큼 약하면 이 방향만으로는 hand-off
  보강 효과가 작다. 그 경우 사이트 footage YOLO fine-tune (`decisions.md` 2026-05-12 entry
  참고)이 다음 단계다.
- 야간 IR B&W로 무심코 전환되면 cam2 sighting 자체가 무너질 수 있다. 스폿라이트 없이
  Color/RGB를 유지해야 하므로 noise, motion blur, projector/background pattern 영향을
  raw detection 단계에서 따로 봐야 한다.
- auxiliary가 잘못된 sighting을 만들면 primary lost가 부당하게 미뤄질 수 있다.
  `aux_match_uv_radius`, `aux_match_time_window_s`는 보수적으로 시작한다 (위 예시값 참고).
