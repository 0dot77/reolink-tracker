# 릴리즈 자동화

`main`에 push되면 GitHub Actions의 `release` workflow가 마지막 `v*.*.*` 태그 이후 커밋을 읽고
다음 semver 버전을 정한 뒤 GitHub Release를 생성합니다. 릴리즈가 필요하다고 판단되면 macOS runner에서
Tauri 앱을 빌드하고 `Reolink-Tracker-vX.Y.Z-macos.zip` asset을 함께 첨부합니다.

## 판단 규칙

커밋 본문에 아래 trailer가 있으면 가장 우선합니다.

```text
Release: major
Release: minor
Release: patch
Release: none
```

trailer가 없으면 workflow가 커밋 메시지를 보수적으로 분류합니다.

- `major`: `BREAKING CHANGE`, `!:`, 호환성 파괴 표현
- `minor`: `feat`, `add`, `implement`, `integrate`, `unify`, 앱/런타임 같은 새 기능 표현
- `patch`: `fix`, `prevent`, `docs`, `test`, `ci` 또는 그 밖의 일반 변경

여러 커밋이 한 번에 push되면 가장 큰 bump가 적용됩니다. 예를 들어 patch와 minor가 섞이면 minor,
minor와 major가 섞이면 major가 됩니다.

## 에이전트 운용 규칙

Codex가 릴리즈 판단을 명확히 남겨야 하는 커밋에는 Lore commit body에 `Release:` trailer를 추가합니다.
판단이 애매한 경우에는 호환성 파괴가 아니면 `minor`, 사용자 동작 변화가 작으면 `patch`를 선택합니다.

## 현장 설치

현장용 Mac에서는 최신 GitHub Release의 `Reolink-Tracker-vX.Y.Z-macos.zip`을 다운로드한 뒤 압축을 풀고
`Reolink Tracker.app`을 실행합니다. 첫 실행 후 앱의 `Setup` 버튼을 누르면 app data runtime에
Python 3.12 venv, engine snapshot, YOLO model, runtime `config.yaml`이 준비됩니다.
