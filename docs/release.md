# 릴리즈 자동화

`main`에 push되면 GitHub Actions의 `release` workflow가 마지막 `v*.*.*` 태그 이후 커밋을 읽고
다음 semver 버전을 정한 뒤 GitHub Release를 생성합니다.

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
