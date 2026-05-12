# Field Mac Transfer

같은 현장에서 다른 Mac으로 옮겨 실행할 때는 GitHub 코드와 현장 runtime 설정을 분리해서
옮깁니다. 코드는 GitHub에서 받으면 되고, 실제 카메라 credential과 calibration은 private
runtime config 파일로만 전달합니다.

## 복사할 파일

필수:

```text
~/Library/Application Support/com.taeyang.reolink-tracker/runtime/config.yaml
```

이 파일에는 다음 현장값이 들어 있습니다.

- RTSP 카메라 URL과 비밀번호
- cam0/cam1/cam2 calibration points
- projection/dispatch/body-catch/relaxed 영역
- cam2 `role: auxiliary`, cam2 전용 `conf`, CLAHE 설정
- OSC 대상 host/port

선택:

```text
~/Library/Application Support/com.taeyang.reolink-tracker/runtime/yolo26s.pt
```

현장 인터넷이 불안정하면 모델 파일도 같이 복사합니다. 모델 파일이 없으면 첫 실행 때
Ultralytics가 다운로드합니다.

## 새 Mac에서 실행 순서

1. GitHub에서 최신 `main`을 다운로드하거나 clone합니다.
2. 앱을 한 번 실행하고 `Setup`을 눌러 runtime 폴더와 Python venv를 만듭니다.
3. 기존 Mac의 `runtime/config.yaml`을 새 Mac의 같은 경로에 덮어씁니다.
4. 인터넷이 불안정하면 `yolo26s.pt`도 같은 runtime 폴더에 복사합니다.
5. 앱을 다시 열고 Projection/Calibration 화면에서 cam0/cam1은 primary, cam2는 auxiliary로
   보이는지 확인합니다.
6. `Start` 또는 `Show Preview`로 실행합니다.
7. 무인 운영이 필요하면 `Showtime` 화면에서 `Open app at login`과
   `Start tracker on launch`를 켭니다.

## 주의

- `config.yaml`은 GitHub에 올리지 않습니다. 실제 RTSP credential과 장비 IP가 들어갑니다.
- 다른 Mac이 같은 현장 네트워크에 있어도 DHCP 때문에 카메라 IP가 바뀔 수 있습니다. 영상이 안
  뜨면 먼저 `config.yaml`의 RTSP host를 확인합니다.
- 앱 Setup은 기존 config를 덮어쓰지 않아야 하지만, 안전하게 하려면 Setup을 먼저 끝낸 뒤
  `config.yaml`을 복사합니다.
- repo root의 `config.yaml`이 아니라 앱 runtime의 `config.yaml`이 현장 앱 실행 기준입니다.
- `Open app at login`은 새 Mac의 사용자 계정 아래
  `~/Library/LaunchAgents/com.taeyang.reolink-tracker.autostart.plist`를 만들거나 삭제합니다.
- `Start tracker on launch`는 앱 runtime의 `operator-settings.json`에 저장됩니다. Setup과
  config가 준비된 상태에서만 앱 시작 직후 tracker를 자동으로 시작합니다.
