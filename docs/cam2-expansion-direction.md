# Cam2 추가 설치 방향

## 목적

현재 청계천 설치 구간은 `cam0`이 projection 왼쪽에서 오른쪽을 보고, `cam1`이
projection 오른쪽에서 왼쪽을 보는 2카메라 구조다. 두 카메라가 약 40m 이상의 긴
구간을 끝에서 바라보기 때문에 중앙 hand-off 구간에서 사람이 작게 잡히고, bbox의
foot point가 바닥 homography 또는 `projection_uv` 밖으로 튀는 경우가 있다.

추가 방향은 중앙부를 억지로 `cam0`/`cam1`의 floor polygon에 포함시키는 것이 아니라,
중앙 사각지대와 hand-off 구간을 직접 보는 `cam2`를 추가해 projection UV 담당 구간을
세 카메라로 나누는 것이다.

## 설치 가정

- `cam0`: projection 왼쪽에서 오른쪽을 바라보며 좌측 구간을 담당한다.
- `cam1`: projection 오른쪽에서 왼쪽을 바라보며 우측 구간을 담당한다.
- `cam2`: 도면상 앞쪽 중앙 위치에 추가 설치해 중앙 hand-off/사각지대 구간을 담당한다.
- 모든 카메라는 같은 `projection_id`를 공유한다.
- OSC schema는 변경하지 않는다. 기존 `/proj/<projection_id>/active`,
  `/proj/<projection_id>/xy`, `/person/<gid>` 흐름을 유지한다.

## Cam2 역할

`cam2`는 전체 45m 구간을 다시 넓게 보려는 카메라가 아니다. 중앙 20% 안팎의 구간을
안정적으로 보는 보강 카메라로 둔다. 목표는 다음과 같다.

- 중앙부에서 `cam0`/`cam1`의 bbox foot point가 region 밖으로 떨어지는 문제를 줄인다.
- 중앙 hand-off 구간에서 `gid`가 끊기거나 새로 spawn되는 빈도를 줄인다.
- 양끝 카메라의 `image_points`를 무리하게 넓히지 않고 정확한 바닥 homography를 유지한다.
- `dispatch_uv`는 한 카메라만 담당하게 해서 count 중복을 막는다.

## 초기 UV 분담안

`projection_uv`는 hand-off 후보를 위해 서로 조금 겹치게 둔다. `dispatch_uv`는 실제 OSC
송신 담당 구간이므로 positive-area overlap이 없어야 한다.

```yaml
cam0:
  projection_uv: [0.00, 0.00, 0.48, 1.00]
  dispatch_uv:   [0.00, 0.00, 0.40, 1.00]

cam2:
  projection_uv: [0.32, 0.00, 0.68, 1.00]
  dispatch_uv:   [0.40, 0.00, 0.60, 1.00]

cam1:
  projection_uv: [0.52, 0.00, 1.00, 1.00]
  dispatch_uv:   [0.60, 0.00, 1.00, 1.00]
```

이 값은 시작점이다. 실제 현장에서는 `$sim`의 24x8 projection usage grid와 Preview의
trail을 보고 중앙 actor가 실제 바닥 위치와 맞는지 확인하면서 조정한다.

## 캘리브레이션 원칙

- `image_points`는 화면상 시계방향이 아니라 projection/world UV 기준
  `top-left -> top-right -> bottom-right -> bottom-left` 순서로 입력한다.
- `cam2`도 기존 카메라와 마찬가지로 실제 바닥면 foot point가 보이는 4점을 기준으로 잡는다.
- 사람을 더 많이 잡기 위해 floor `image_points`를 무리하게 넓히지 않는다.
- bbox 몸통은 보이지만 발점이 바닥면 밖으로 빠지는 경우에만 `body_catch_points`를 보조로 쓴다.
- 계단/착석자처럼 바닥 homography와 다른 평면은 `relaxed_presence_points`로 분리한다.

## 물리 설치 체크

- `cam2`는 중앙 바닥의 발 위치가 가려지지 않는 높이와 각도에 둔다.
- 완전 정면보다 약간 사선으로 두는 편이 사람끼리 겹치는 occlusion을 줄인다.
- 너무 낮게 설치하면 몸통이 발점을 가려 같은 문제가 반복될 수 있다.
- 중앙 보강 카메라이므로 먼 양끝까지 욕심내지 않는다.
- 야간 반사와 바닥 조명 때문에 bbox 하단이 흔들릴 수 있으므로 Preview에서 raw bbox와
  accepted actor 색상을 함께 본다.

## 검증 절차

1. 앱에서 `cam2`를 추가하고 같은 `projection_id`로 설정한다.
2. 세 카메라의 `dispatch_uv`가 겹치지 않는지 Preview의 overlap 경고를 확인한다.
3. 한 사람이 `cam0 -> cam2 -> cam1` 방향으로 천천히 걸으며 같은 `gid`가 유지되는지 본다.
4. 반대 방향 `cam1 -> cam2 -> cam0`도 확인한다.
5. `$sim`으로 20초 projection usage 영상을 만들고 다음 지표를 비교한다.
   - `center_overlap_samples`
   - `used_cells`
   - `spawned_gids`
   - `handoff_count`
   - `teleport_reject_count`
   - `lost_count`
6. 중앙부 preview frame에서 actor가 하단 edge에만 번쩍이지 않고 실제 중앙 band를 따라
   이어지는지 확인한다.

## 성공 기준

- 중앙부에서 회색 raw bbox가 actor로 승격되지 못하는 사례가 줄어든다.
- `center_overlap_samples`가 유지되거나 증가한다.
- `spawned_gids`와 `lost_count`가 2카메라 기준보다 낮아진다.
- `teleport_reject_count`가 0 또는 낮은 수준을 유지한다.
- 같은 사람이 중앙을 통과할 때 `/persons/count`가 0으로 떨어지거나 2로 부풀지 않는다.

## 남은 리스크

- `cam2` 위치에서 발점이 가려지면 카메라를 추가해도 같은 homography 문제가 반복된다.
- 중앙 카메라가 너무 넓은 구간을 dispatch하면 기존 `cam0`/`cam1`과 count 중복이 생길 수 있다.
- 카메라 3대 구성에서는 fusion tuning이 더 민감해질 수 있으므로 `dispatch_uv`를 먼저
  명확히 나눈 뒤 `miss_buffer_frames`, `hand_off_window_s`, `match_uv_radius`를 조정한다.
