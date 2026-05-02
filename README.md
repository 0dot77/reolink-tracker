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
- `1`-`9`: focus camera 선택
- `d`: focus camera에 region 그리기 시작
- 마우스 왼쪽 클릭 4회: projection 기준 top-left -> top-right -> bottom-right -> bottom-left 순서로 image point 입력
- `Backspace`: region 그리기 중 마지막 점 취소
- `x`: focus camera의 마지막 region 삭제
- `e`: focus camera region의 `projection_uv` / `dispatch_uv` slice 편집 모드 시작 / 다음 region으로 순환 / 마지막 region 이후엔 종료
- `t`: 편집 대상 토글 — `projection_uv` ↔ `dispatch_uv`
- `g`: 편집할 모서리 순환 — `u0 → v0 → u1 → v1`
- `[` / `]`: 선택한 모서리 -0.01 / +0.01 nudge
- `,` / `.`: 선택한 모서리 -0.05 / +0.05 nudge
- `r`: 편집 중인 slice 초기화 (`projection`은 `[0,0,1,1]`로, `dispatch`는 `projection_uv`와 같게)
- `w`: 현재 region 편집 내용을 로컬 `config.yaml`에 저장

## AI / GitHub 작업 방식

이 레포는 AI 코딩 도구가 채팅 기록에만 의존하지 않도록 프로젝트 맥락을 버전 관리되는 Markdown 파일에 저장합니다.

- `docs/product.md`: 제품 목적, 사용자, MVP, 비목표
- `docs/tech.md`: 아키텍처와 검증 방법
- `docs/ai-rules.md`: AI 에이전트가 따라야 할 안정적인 규칙
- `docs/decisions.md`: 중요한 결정과 번복 기록

혼자 작업하더라도 GitHub Issue를 작은 작업 단위로 쓰고, Pull Request에서 변경 diff를 확인하는 흐름을 권장합니다. PR 화면은 AI가 만든 변경을 `main`에 넣기 전에 검토하는 장소입니다.

## OSC 스키마

좌표는 `projection_id`로 식별되는 **공유 projection UV 공간**으로 송신됩니다. 같은 실제 바닥 프로젝션을 보는 카메라 2대는 같은 `projection_id`를 공유하며, 각 카메라가 내보내는 `(u, v)` 값은 서로 비교할 수 있습니다.

| 주소 | 인자 | 송신 시점 |
|---|---|---|
| `/proj/<projection_id>/cam/<cam_name>/track/<id>` | `u, v, conf` (`pixel_size`가 있으면 `u_px, v_px` 추가) | 트랙의 발 위치가 카메라 region polygon 안이고 `dispatch_uv` slice 안일 때 매 프레임 |
| `/proj/<projection_id>/cam/<cam_name>/track/<id>/lost` | 없음 | ID가 사라지거나 dispatch 영역을 벗어났을 때 한 번 |
| `/proj/<projection_id>/count` | int | 카메라 전체 합산 count, 매 프레임 |
| `/proj/<projection_id>/cam/<cam_name>/count` | int | 카메라별 count, 매 프레임 |
| `/proj/<projection_id>/cam/<cam_name>/active` | id 목록 | 활성 ID가 있을 때 매 프레임 |

`(u=0, v=0)`은 projection의 좌상단, `(u=1, v=1)`은 우하단입니다. `id`는 카메라별로 트랙이 유지되는 동안 안정적입니다. v1에서는 카메라 간 ID fusion을 하지 않으므로 서로 다른 카메라의 ID는 독립적입니다.

`osc.legacy_image_space: true`를 `config.yaml`에 설정하면 예전 image-space 메시지(`<cam_prefix>/track/<id>`와 `cx, cy, w, h, conf`)도 함께 송신합니다. 기본값은 꺼져 있습니다.

## TouchDesigner 수신

- **OSC In DAT** 또는 숫자 스트림용 **OSC In CHOP**를 추가합니다.
- Network Port는 `config.yaml`의 포트와 맞춥니다. 기본값은 `7000`입니다.
- 주소 패턴은 `/proj/corridor/cam/*/track/*`처럼 wildcard를 써서 특정 projection의 모든 카메라와 트랙을 받을 수 있습니다.

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
