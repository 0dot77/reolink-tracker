# reolink-tracker

Reolink 카메라 2대의 RTSP 스트림을 받아 YOLO26 사람 감지 + BoT-SORT 트래킹을 수행하고 OSC로 좌표를 내보내는 도구입니다. TouchDesigner, Max, Unity 같은 인터랙티브 설치 작업에서 낮은 지연 시간으로 사람 위치를 쓰기 위해 만들었습니다. 더 빠른 대안으로 ByteTrack도 선택할 수 있습니다.

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

`--show` 미리보기 키:

- `q` 또는 `Esc`: 종료 (slice 편집 중에는 편집 모드만 빠져나옵니다)
- `h`: HUD 표시 토글
- `u`: projection UV canvas 표시 토글
- `p`: 좌측 region 목록 패널 토글
- `1`-`9`: focus camera 선택
- `[` / `]`: 편집 모드에서는 선택한 모서리 ±0.01 nudge, 그 외에는 focus camera 안의 focused region 순환
- `d`: focus camera에 region 그리기 시작
- 마우스 왼쪽 클릭 4회: projection 기준 top-left -> top-right -> bottom-right -> bottom-left 순서로 image point 입력
- `Backspace`: region 그리기 중 마지막 점 취소
- `x`: focus camera의 마지막 region 삭제
- `e`: focus camera region의 `projection_uv` / `dispatch_uv` slice 편집 모드 시작 / 다음 region으로 순환 / 마지막 region 이후엔 종료
- `t`: 편집 대상 토글 — `projection_uv` ↔ `dispatch_uv`
- `g`: 편집할 모서리 순환 — `u0 → v0 → u1 → v1`
- `,` / `.`: 선택한 모서리 -0.05 / +0.05 nudge (편집 모드 한정)
- `r`: 편집 중인 slice 초기화 (`projection`은 `[0,0,1,1]`로, `dispatch`는 `projection_uv`와 같게)
- `w`: 현재 region 편집 내용을 로컬 `config.yaml`에 저장 (status bar의 `[unsaved]`가 `[saved]`로 바뀜)

좌측 패널은 카메라별 region 목록과 각 region의 `dispatch_uv`를 보여줍니다. `>`는 focus camera, `*`는 focused region을 가리키고, 상단에 `[unsaved]`/`[saved]` 와 `overlap: N` 카운터가 같이 떠서 저장 누락이나 dispatch 충돌을 빠르게 알 수 있게 합니다. 충돌이 있으면 UV canvas 패널 좌하단에도 `cam0:near_half <-> cam1:far_half` 같은 줄이 빨간색으로 떠서 어떤 쌍이 겹치는지 알려줍니다.

## AI / GitHub 작업 방식

이 레포는 AI 코딩 도구가 채팅 기록에만 의존하지 않도록 프로젝트 맥락을 버전 관리되는 Markdown 파일에 저장합니다.

- `docs/product.md`: 제품 목적, 사용자, MVP, 비목표
- `docs/tech.md`: 아키텍처와 검증 방법
- `docs/ai-rules.md`: AI 에이전트가 따라야 할 안정적인 규칙
- `docs/decisions.md`: 중요한 결정과 번복 기록

혼자 작업하더라도 GitHub Issue를 작은 작업 단위로 쓰고, Pull Request에서 변경 diff를 확인하는 흐름을 권장합니다. PR 화면은 AI가 만든 변경을 `main`에 넣기 전에 검토하는 장소입니다.

## OSC 스키마

좌표는 `projection_id`로 식별되는 **공유 projection UV 공간**으로 송신됩니다. 같은 실제 바닥 프로젝션을 보는 카메라 2대는 같은 `projection_id`를 공유하며, 각 카메라가 내보내는 `(u, v)` 값은 서로 비교할 수 있습니다.

### Person-keyed (권장, 기본 ON)

`gid`는 cross-camera fusion이 만드는 global person ID입니다. 한 사람이 cam0 dispatch 슬라이스에서 cam1 dispatch 슬라이스로 넘어가도 같은 `gid`가 유지되도록 fusion 레이어가 hand-off를 stitch 합니다 (UV 거리 + 시간 윈도우 기반).

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/person/<gid>` | `u, v, vx, vy, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | 활성 person마다 매 프레임 |
| `/proj/<projection_id>/person/<gid>/lost` | 없음 | hand-off 윈도우(기본 0.4 s) 안에 다른 카메라가 받지 못하면 한 번 |
| `/proj/<projection_id>/persons` | `[gid, ...]` | 활성 gid 목록, 매 프레임 |
| `/proj/<projection_id>/persons/count` | int | 매 프레임 |

`(vx, vy)`는 fusion이 EMA로 산출한 UV 단위/초 속도입니다. 정지 상태에서는 0에 수렴.

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

`/persons/count`로 인원 수를 그대로 받고, `/persons` 배열로 현재 살아있는 gid 목록을 동기화합니다. gid는 재사용되지 않으므로 슬롯 할당은 단순 modulo (`slot = gid % N`) 또는 first-free-slot 정책으로 충분합니다.

### Raw per-cam wildcard (디버깅용)

```
/proj/corridor/cam/*/track/*
```

각 카메라가 보고 있는 raw 트랙을 보고 싶을 때 씁니다. cross-camera 경계에서 ID가 끊기는 게 정상입니다.

## 캘리브레이션 / region 설정

`viewer.py`는 `--show`에서 focus camera의 region을 4점 클릭으로 추가하고 `w`로 로컬 `config.yaml`에 저장할 수 있습니다. 새로 그린 region은 기본적으로 첫 projection의 전체 UV `[0.0, 0.0, 1.0, 1.0]`로 만들어집니다. 여러 카메라가 한 projection을 나눠 담당하는 설치에서는 viewer 안에서 직접 slice를 조정할 수 있습니다. `e`로 편집 모드를 시작해 region을 선택하고 `t`로 `projection_uv` / `dispatch_uv` 대상을 전환, `g`로 모서리(`u0` `v0` `u1` `v1`)를 고른 뒤 `[` `]`(±0.01) 또는 `,` `.`(±0.05)로 값을 움직입니다. UV canvas에서는 `projection_uv`가 점선 외곽 + 옅은 채움으로, `dispatch_uv`가 진한 채움 + 실선으로 구분되어 보이고, 편집 중인 slice에는 강조선이 추가됩니다. `dispatch_uv`가 `projection_uv`의 부분집합이 아니게 되는 변경은 status 메시지로 거부되고 `w`로 저장되는 값에는 반영되지 않습니다. `config.example.yaml`에는 공유 가능한 예시 구조만 두고, 실제 RTSP URL과 비밀번호가 들어가는 `config.yaml`은 로컬에만 유지합니다.

region의 4개 `image_points`는 **projection-UV 방향** 기준으로 입력합니다.

```text
top-left -> top-right -> bottom-right -> bottom-left
```

이는 카메라 이미지에서 보이는 순서가 아니라 projection/world 기준 순서입니다. 그래서 서로 마주 보는 복도 카메라 2대가 거울처럼 반대로 보이더라도 같은 공유 UV 프레임으로 매핑할 수 있습니다.

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

## 카메라 2대 캘리브레이션 절차

복도 양쪽 끝에 카메라 2대를 두고 같은 바닥 projection을 분담하게 만드는 표준 순서입니다. 두 카메라가 같은 `projection_id`를 공유하고, `projection_uv`는 겹쳐도 되지만 `dispatch_uv`는 절대 겹치면 안 됩니다(같은 사람이 두 OSC stream으로 동시에 나갑니다).

1. 먼저 `config.yaml`에 두 카메라가 모두 같은 `projection_id`를 쓰도록 항목을 만들어 둡니다 (`projections:` 아래에 `corridor` 같은 id를 한 번만 정의하고, 두 카메라 모두 그 id를 region에 씁니다).
2. `python tracker.py --show`로 viewer를 띄우고 `1`로 cam0을 focus 합니다. 좌측 패널 상단이 `[saved]`인지 확인하고, `d` -> 카메라 화면에서 projection 기준으로 top-left -> top-right -> bottom-right -> bottom-left 순서로 4점을 클릭합니다. 이 카메라는 가까운 절반을 담당할 것이므로 4점은 카메라가 실제로 잘 보이는 가까운 영역만 둘러쌉니다.
3. `w`로 일단 저장합니다. 새 region은 `projection_uv = [0.0, 0.0, 1.0, 1.0]`, `dispatch_uv`도 동일하게 만들어집니다. `[saved]`가 떠야 디스크에 반영된 상태입니다.
4. 에디터에서 `config.yaml`의 cam0 region을 열어 분담 범위를 줄입니다. 예를 들어 cam0이 복도 왼쪽 절반이면 `projection_uv: [0.0, 0.0, 0.55, 1.0]`, `dispatch_uv: [0.0, 0.0, 0.50, 1.0]`로 바꿉니다. `projection_uv`는 살짝 더 넓게(0.55) 두고 `dispatch_uv`는 안전 마진(0.50)을 두면 카메라 끝부분 떨림이 송신에 영향을 덜 줍니다.
5. cam1도 같은 방식으로 진행합니다. viewer에서 `2`로 focus를 옮긴 뒤 `d`로 region을 그립니다. 이 카메라는 마주 보는 방향이라 같은 바닥인데도 영상이 좌우 반전돼 보이지만, 4점 클릭 순서는 여전히 **projection-UV 기준** top-left -> top-right -> bottom-right -> bottom-left 입니다. cam0과 동일한 `projection_id`를 쓰도록 `config.yaml`을 정리합니다.
6. cam1의 `projection_uv` / `dispatch_uv`는 cam0과 거울처럼 반대 슬라이스를 잡습니다. 예시 분할: cam0 `dispatch_uv [0.0, 0.0, 0.50, 1.0]`, cam1 `dispatch_uv [0.50, 0.0, 1.0, 1.0]`. 두 `dispatch_uv`의 u 경계가 정확히 같은 값이면 `dispatches_overlap`은 통과합니다(접하기만 하고 겹치지 않음).
7. 저장 후 viewer를 다시 시작하면 좌측 패널에 두 카메라의 region 목록이 보이고, 상단 `overlap` 카운터가 `0`이어야 합니다. UV canvas 패널 좌하단에 빨간 경고가 뜨면 어떤 쌍의 `dispatch_uv`가 겹치는지 알려주므로 그 부분만 다시 분할 비율을 조정합니다.
8. 마지막으로 사람이 한 명 복도 끝에서 끝까지 천천히 걸어가게 하고, TouchDesigner OSC In CHOP에서 `/proj/corridor/cam/cam0/track/*`와 `/proj/corridor/cam/cam1/track/*`가 서로 끊김 없이 이어지는지 확인합니다. 같은 사람이 양쪽에서 동시에 송신되는 구간이 있으면 `dispatch_uv`가 아직 겹치는 것이므로 4번~6번 단계로 돌아갑니다.

## 카메라 2대 네트워크 구성

도구는 `config.yaml`에 적힌 RTSP URL만 사용합니다. 카메라 IP는 고정된 프로젝트 값이 아니라 현재 공간의 네트워크에서 받은 값입니다. 장소를 옮기거나 직결/공유기 구성을 바꾸면 Reolink 앱, 공유기 DHCP 목록, 또는 `arp`로 IP를 다시 확인한 뒤 `config.yaml`을 갱신합니다.

아래 중 하나를 선택하면 됩니다.

1. **두 카메라를 라우터에 연결**: 추천 방식입니다. 두 카메라를 iPTIME LAN 포트에 꽂으면 DHCP로 `172.30.1.x` 대역 IP를 받고, Mac은 Wi-Fi나 LAN으로 접속합니다.
2. **두 카메라를 스위치로 직접 연결**: Mac의 USB-Ethernet 어댑터에 작은 unmanaged 5-port switch를 연결합니다. Mac에서 DHCP를 제공해야 하므로 System Settings -> General -> Sharing -> Internet Sharing을 사용합니다. 이 경우 카메라는 보통 `192.168.2.x` 같은 주소를 받습니다.
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
