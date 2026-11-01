# iOS 간편 모드 (`reproof app`)

회사 iOS 앱 저장소에서 설정을 거의 하지 않고 시작하는 모드다. 도구가 자기 전용 Simulator
(`reproof-<이름>-<소스 경로 해시 6자>`)와 작업 사본을 따로 쓰므로, 개발자의 소스와 기존 Simulator는 바뀌지 않는다.

## 현재 범위

이번 단계에서 되는 것: `init`(설정 만들기), `doctor`(환경 점검), `build`(Simulator용 빌드),
`inspect`(앱 실행 후 화면 구조 저장, 좌표의 요소 확인).

**녹화·재현·AI 수정은 다음 단계에서 제공한다.** 지금은 호출하지 않으며 Claude도 실행하지 않는다.

## 준비물

- macOS, Xcode(명령줄 도구 포함), Python 3.11 이상
- iOS Simulator 런타임 (없으면 `xcodebuild -downloadPlatform iOS`)
- 앱이 XcodeGen·Tuist로 프로젝트를 생성한다면 해당 도구 (`brew install xcodegen` 등). Tuist는 도구가 설치하지 않는다.
  Tuist 지원은 미검증(이 Mac에 Tuist 없음; `tuist install` 선행 필요 여부 미확인)이다.
- 작업 공간 여유 공간 30 GB 이상

## 설치

배포받은 wheel은 [INSTALLATION.md](INSTALLATION.md)의 절차로 설치한다. checkout에서 쓰려면:

```sh
cd reproof            # 이 저장소
python3 -m pip install -e .
reproof app --help
```

## 시작하기: 명령 네 개

모든 명령은 앱 저장소 폴더에서 실행한다. `reproof.toml`은 현재 폴더부터 위로 올라가며 찾는다.

### 1. `reproof app init`

```sh
cd ~/Desktop/회사\ 앱
reproof app init
```

`reproof.toml`을 만든다. 프로젝트 파일, scheme, 앱 번들 ID는 자동으로 찾는다. 후보가 여러 개일
때만 번호로 묻고, 테스트 scheme은 추측값을 보여 주며 Enter로 수락하면 된다. Simulator는
`iPhone 16`, 런타임은 `latest`로 두고 묻지 않는다. 이름은 폴더 이름에서 만든다.

출력 예(이 Mac, Xcode 27.0. 경로는 예시):

```
저장했습니다: /Users/me/Desktop/회사 앱/reproof.toml
  프로젝트: MixedShop.xcodeproj  scheme: MixedShop  테스트 scheme: MixedShopTests
  앱 번들 ID: io.reproof.example.mixedshop  Simulator: iPhone 16 (latest)
질문 1개
환경 점검:
✓ xcode: Xcode 27.0
...
다음 단계: reproof app build
```

질문은 테스트 scheme 추정값을 Enter로 수락하는 것까지 센다(`MixedShop` 예제는 1개). `--non-interactive`로 실행하면 0개다.

옵션: `--path DIR`(다른 폴더), `--non-interactive`(질문 없이; 모호하면 후보를 보여 주고 멈춤),
`--force`(기존 `reproof.toml` 덮어쓰기). XcodeGen·Tuist 프로젝트는 임시 사본에서 생성해 확인하고
바로 지운다. Tuist 경로는 미검증(이 Mac에 Tuist 없음; `tuist install` 선행 필요 여부 미확인)이다.
일반 프로젝트도 임시 사본에서 scheme을 조회하므로, `init`이 저장소에 남기는 파일은 `reproof.toml` 하나뿐이다.
`reproof.toml`이 심볼릭 링크이면(`config_symlink`) 쓰지 않는다.

### 2. `reproof app doctor`

```sh
reproof app doctor [--config FILE]
```

항목마다 한 줄로 보여 준다. 실패하면 바로 아래 `해결:`에 할 일이 나온다.

출력 예(이 Mac, Xcode 27.0·iOS 27.0 런타임. 디스크 값은 실행할 때마다 다르다):

```
✓ xcode: Xcode 27.0
✓ ios_runtime: iOS 27.0
✓ git: git 사용 가능
✓ disk: 남은 공간 63.3 GB
ⓘ claude: claude 사용 가능 (AI 수정에 필요)
```

런타임이 없을 때의 형식:

```
✗ ios_runtime: 사용 가능한 iOS Simulator 런타임이 없습니다.
  해결: `xcodebuild -downloadPlatform iOS`로 iOS 런타임을 설치한 뒤 다시 실행하세요.
```

점검 항목: `xcode`, `ios_runtime`, `git`, `generator`(생성기를 쓸 때만), `disk`(30 GB),
`claude`. 종료 코드는 모두 통과하면 0, 하나라도 실패하면 2다. `claude`는 안내용이라 결과에 영향을 주지 않는다.

### 3. `reproof app build`

```sh
reproof app build [--config FILE] [--workspace DIR]
```

소스를 작업 사본으로 복사해 Debug로 Simulator용 빌드를 한다. 서명은 인증서가 필요 없는 ad-hoc
서명(Xcode의 "Sign to Run Locally")만 하고 Release는 만들지 않는다. ad-hoc 서명을 하는 이유는 App Group 같은
entitlement가 있어야 Simulator에서도 앱이 정상 실행되기 때문이며, 키체인의 개발자 인증서·프로비저닝 프로필은 쓰지
않는다. 소스와 설정이 같으면 이전 빌드를 재사용한다. 끝나면 `빌드 ID: ...`를 보여 준다.
산출물과 로그는 `~/.reproof/projects/<이름>/builds/<빌드 ID>/`(`logs/` 포함)에 있다.
`REPROOF_HOME` 환경 변수나 `--workspace`로 위치를 바꿀 수 있다.

### 4. `reproof app inspect`

```sh
reproof app inspect [--point X,Y] [--output FILE]
```

빌드(재사용) → 전용 Simulator 초기화 → 앱 설치·실행 → 3초 대기 → 화면 계층을 JSON으로 저장한다.
기본 저장 위치는 `<빌드 폴더>/inspect-<YYYYmmdd-HHMMSS>.json`이다. 화면의 모든 글자가 들어 있으므로 소유자만 읽는 0600으로
쓰고, 저장 위치가 심볼릭 링크면 쓰지 않는다(`output_symlink`).
`--point 195,420`을 주면 그 좌표의 요소와, 재생 때 다시 찾을 수 있는 정도를 출력한다.

```
요소: 종류=button 라벨=담기 identifier=(없음)
근거: {"identifier": "", "elementType": "button", ...}
재탐색: strength=weak matched_by=type-label-anchor
```

`strength`가 `weak`이면 accessibility identifier 없이 종류·라벨로 찾은 것이다. 앱에 identifier를
달면 `strong`이 된다. 좌표는 저장된 JSON의 `frame`에서 확인한다.

참고: `reproof-<이름>-<해시>` Simulator는 `inspect` 후에도 켜 둔다(다음 실행이 빨라진다). 끄려면
`xcrun simctl shutdown <udid>`. 사용자가 만든 다른 Simulator는 건드리지 않는다. `<해시>`는 저장소 폴더 실경로의 sha256 앞
6자라, 이름이 같은 다른 저장소(클론·다른 작업 공간)가 같은 기기를 초기화하지 않는다. 기기 준비부터 세션 끝까지
사용자 전역 잠금(`<임시 폴더>/reproof-<uid>/sim-<기기 이름>.lock`)을 쥐므로, 같은 기기를 쓰는 다른 실행은 `simulator_busy`로
멈춘다. 이전 버전이 만든 `reproof-<이름>` 기기는 더 쓰지 않으며 지우지 않고 남겨 둔다.

## 설정 파일을 찾는 규칙

`--config`를 주지 않으면 현재 폴더에서 위로 올라가며 `reproof.toml`을 찾되, `.git`이 있는 폴더나 홈 폴더에서 멈춘다.
찾은 파일은 항상 `설정 파일: <경로>`로 출력한다. 현재 사용자 소유가 아닌 파일은 `config_untrusted`로 거절한다.

## `reproof.toml` 키

| 위치 | 키 | 설명 |
|---|---|---|
| 최상위 | `schema` | 설정 형식 버전(현재 1) |
| 최상위 | `name` | 소문자·숫자·`-` 이름(40자 이하). Simulator 이름 `reproof-<name>-<경로 해시>`에 쓴다 |
| `[project]` | `container` | `.xcworkspace` 또는 `.xcodeproj`의 저장소 기준 상대 경로(빈 값·절대 경로·`..` 불가) |
| `[project]` | `generator`, `generator_spec` | `none`/`xcodegen`/`tuist`, 스펙 파일의 저장소 기준 상대 경로(xcodegen은 `.yml`·`.yaml`, tuist는 `.swift`; 빈 값·절대 경로·`..` 불가). Tuist는 미검증(이 Mac에 Tuist 없음; `tuist install` 선행 필요 여부 미확인) |
| `[project]` | `scheme`, `app_target`, `bundle_id` | 빌드할 scheme, 앱 타깃, 번들 ID(비울 수 없음) |
| `[project]` | `test_scheme` | 단위 테스트 scheme(선택) |
| `[simulator]` | `device_type`, `runtime` | 기기 이름(`iPhone 16`), 런타임(`latest` 또는 `iOS 18.0`) |
| `[simulator]` | `locale`, `language`, `timezone` | 기본 `ko_KR`, `ko`, `Asia/Seoul` (스크린샷을 같게 하려고 매번 명시) |
| `[launch]` | `arguments` | 앱 실행 인자 목록 |
| `[launch.environment]` | `KEY = "값"` | 앱 실행 환경 변수 |
| `[build]` | `timeout_seconds` | 패키지 해석·빌드 제한 시간(기본 3600) |
| `[build]` | `skip_package_validation` | 기본 `false`. `true`면 Swift 매크로·패키지 플러그인 신뢰 검증을 건너뛴다(`-skipMacroValidation -skipPackagePluginValidation`). 매크로·플러그인은 빌드 중 코드를 실행하므로, 기본은 Xcode에서 한 번 신뢰(Trust & Enable)한 것만 쓴다. 회사가 검토한 패키지만 쓸 때만 켠다 |
| `[ai]` | `claude_consent` | AI 수정 동의(기본 false, 다음 단계에서 사용) |
| `[source]` | `include` | 비밀 가능 이름이어도 작업 사본에 넣을 파일의 저장소 기준 상대 경로 목록(선택; 빈 값·절대 경로·`..`·NUL 불가). 아래 "작업 사본에서 빼는 파일" 참고 |

### 비밀 값은 `env:`로

토큰 같은 값은 파일에 쓰지 말고 환경 변수를 참조한다. 참조할 수 있는 변수는 이름이 `REPROOF_APP_`으로 시작하는 것뿐이다
(AI가 만든 후보 코드가 앱 안에서 돌기 때문에, 호스트의 `AWS_*` 같은 비밀이 이름 지정 없이 앱에 넘어가지 않게 하려는 규칙).

```toml
[launch.environment]
API_TOKEN = "env:REPROOF_APP_API_TOKEN"
```

```sh
export REPROOF_APP_API_TOKEN=...   # 실행하는 셸에서만 설정
reproof app inspect
```

없으면 `env_missing` 오류가 어떤 변수를 설정해야 하는지 알려 준다.

### 작업 사본에서 빼는 파일

작업 사본은 작업 공간(`~/.reproof`)에 남고 다음 단계에서 AI가 읽을 수 있으므로, 비밀이 들어 있을 수 있는 이름의 파일은
**열지도 않고** 빼며 파일마다 한 번 `제외함(비밀 가능 파일): <경로>`를 출력한다. 이름 규칙(어느 폴더든, 대소문자 무시):
`.env`, `.env.*`, `.git-credentials`, `.netrc`, `.npmrc`, `*.p8`, `*.p12`, `*.pfx`, `*.pem`, `*.key`, `*.keystore`, `*.jks`,
`*.mobileprovision`, `*.provisionprofile`, `id_rsa*`, `id_ed25519*`, `*.cer`. 뺀 목록은 빌드 식별(`identity.json`의
`excludedPaths`)에 들어가고, 이런 파일의 변경은 `dirty.patch`에도 넣지 않는다.

빌드에 꼭 필요한 파일(예: 개발용 설정 `Config/Dev.env`)은 경로를 지정해 넣는다. 지정한 파일은 작업 사본에 복사되지만
`dirty.patch`에는 여전히 들어가지 않는다(내용은 작업 사본 digest로 식별에 반영된다).

```toml
[source]
include = ["Config/Dev.env"]
```

## 비공개 SPM 패키지

도구는 인증 값을 읽거나 저장하지 않고, 개발자 Mac의 기존 git 인증(SSH 에이전트, 자격 증명 도우미)을 그대로
쓴다. 빌드 전에 터미널에서 같은 주소가 되는지 확인한다.

```sh
git ls-remote git@github.com:회사/비공개-패키지.git     # 목록이 나오면 인증은 정상
```

여기서 실패하면 `ssh-add`로 키를 올리거나 `gh auth login`·자격 증명 도우미를 설정한다. 도구는
`GIT_TERMINAL_PROMPT=0`으로 실행해 인증 입력을 기다리며 멈추지 않고 실패로 알린다. 프록시·사내 CA·git SSH 변수
(`http_proxy`·`https_proxy`·`no_proxy`와 대문자형, `GIT_SSH_COMMAND`, `SSL_CERT_FILE`, `GIT_SSL_CAINFO`)는 설정돼 있으면
값을 읽지 않고 그대로 넘긴다.

가리는 범위: **오류 안내문**에서만 URL의 사용자 정보(`user:token@`)와 쿼리의 토큰(`access_token=` 등)·제어 문자를
지우고, `마지막 출력:` 줄의 토큰·서명·`Authorization` 값을 `***`로 바꾼다. 인증 실패 안내의 `git ls-remote` 명령은 주소를
따옴표로 감싸며, 주소에 셸 특수 문자나 가린 값이 있으면 명령을 만들지 않고 주소만 보여 준다. **로그 파일**(`logs/*.log`, 소유자만 읽는 0600)은 xcodebuild·git 출력을 그대로
담으므로 URL의 자격 증명이나 토큰이 남을 수 있다. 로그를 다른 사람에게 보내기 전에 내용을 확인한다.

## 툴이 손대는 전역 위치

작업 사본·빌드는 작업 공간에서 하지만, 다음 위치에도 흔적이 남는다.

- **작업 공간 `~/.reproof`**(또는 `REPROOF_HOME`·`--workspace`): 빌드(`projects/<이름>/builds/<빌드 ID>/`), 패키지 캐시
  (`cache/spm`), 실행기(`runner`), crash 로그(`logs`). 새 작업 공간 폴더와 도구 폴더(`projects`·`builds`·`cache`·`runner`·
  `logs`·`tmp`)는 소유자만 쓰는 0700이다. 도구 폴더가 링크·파일이거나 다른 사용자 것이면 쓰지 않고 `workspace_untrusted`로
  멈춘다(다른 디스크를 쓰려면 하위 폴더 링크 대신 `REPROOF_HOME`·`--workspace`로 작업 공간 자체를 옮긴다). 이미 있던 작업
  공간 폴더 자체는 내 소유여야 하고, 그룹·다른 사용자 쓰기가 열려 있으면 0700으로 좁힌다(그 밖의 권한은 바꾸지 않는다). 완성 빌드는 재사용 전에 `.app`의 digest·bundle id·경로가 기록과 같은지 다시 확인하고, 다르면 다시 빌드한다. 빌드는 크다(Wikipedia 앱은 빌드 하나에 약 3.8 GB). 소스·설정·Xcode가
  바뀌면 새 빌드 ID 폴더가 생기고 옛 폴더는 자동으로 지우지 않는다. 지울 때는 그 프로젝트의 `build`·`inspect`가 돌고 있지
  않을 때 `rm -rf ~/.reproof/projects/<이름>/builds/<빌드 ID>`처럼 폴더 단위로 지운다(`.partial`로 끝나는 폴더는 진행 중이거나
  끊긴 빌드이고, 다음 실행이 어차피 다시 만든다). 작업 사본은 원본의 읽기 전용 권한을 그대로 가져오므로 `Permission denied`가 나면
  `chmod -R u+w <폴더>` 뒤에 지운다. `~/.reproof` 전체를 지워도 되지만, 다음 실행이 패키지를 다시 받고 실행기를 다시 빌드한다.
- **SwiftPM 전역 캐시**: `~/Library/Caches/org.swift.swiftpm`(저장소 캐시), `~/Library/org.swift.swiftpm`(패키지 지문 등).
  xcodebuild가 패키지를 받을 때 쓰며 Xcode·다른 프로젝트와 함께 쓰는 위치다. 지우면 다른 프로젝트도 패키지를 다시 받는다.
- **CoreSimulator 기기**: 이름이 `reproof-<이름>-<해시>`인 Simulator(이전 버전은 `reproof-<이름>`). `inspect` 뒤에도 켜 둔다. 지우려면
  `xcrun simctl delete <udid>`(이름이 `reproof-`로 시작하는 기기만). 다른 Simulator는 도구가 만들거나 지우지 않는다.
- **Xcode DerivedData**: `~/Library/Developer/Xcode/DerivedData`에 `<scheme>-<해시>` 이름의 작은 로그 폴더(수 KB~수백 KB)가
  있을 수 있다. 확인된 원인은 이전 버전의 패키지 해석이었다(지금은 빌드 폴더의 `DerivedData`를 쓴다). 다른 xcodebuild 호출이
  남기는지는 확인하지 않았다. 지워도 된다.

## 자주 나는 오류

| 메시지(요약) | 원인 | 해결 |
|---|---|---|
| `config_exists` — reproof.toml이 이미 있음 | 이미 `init`을 했다 | 그대로 `build`, 새로 만들려면 `init --force` |
| `init_needs_choice` — 후보가 여러 개 | `--non-interactive`인데 scheme 등이 모호 | 옵션 없이 실행해 고르거나 `reproof.toml`을 직접 작성 |
| `app_target_missing` — 앱 scheme 없음 | scheme이 Shared가 아니거나 앱 타깃을 빌드하지 않음 | Xcode에서 앱 scheme을 Shared로 두고 커밋 |
| `generator_missing` | XcodeGen/Tuist 미설치 | `brew install xcodegen` 등으로 설치 |
| `ios_runtime_missing` | iOS Simulator 런타임 없음 | `xcodebuild -downloadPlatform iOS` |
| `config_missing` | 상위 폴더에도 reproof.toml 없음 | 앱 폴더에서 `init` 또는 `--config` 지정 |
| `<도구> 실행이 실패했습니다(종료 코드 N). 마지막 출력: …` | 빌드·도구 오류 | `마지막 출력:` 줄과 안내된 `로그:` 파일 끝부분을 확인(패키지 인증, 서명 설정, 누락된 파일 등) |
| `<도구> 실행이 시간 제한 N초를 넘겼습니다` | 시간 초과 | 빌드·패키지 단계면 `reproof.toml`의 `[build] timeout_seconds`를 늘리고, 아니면 네트워크·Simulator 상태 확인 |
| `package_validation_required` | 매크로·패키지 플러그인을 아직 신뢰하지 않음 | Xcode에서 프로젝트를 열어 Trust & Enable, 또는 `[build] skip_package_validation = true` |
| `point_invalid` | `--point` 형식 오류 | `--point 195,420`처럼 숫자 두 개 |
| `runner_start_failed` | 실행기 세션이 시작되지 않음 | `inspect-session.log`를 확인하고, Simulator가 부팅됐는지 `xcrun simctl list devices`로 확인 |
| `project_busy` / `runner_busy` / `simulator_busy` / `package_cache_busy` | 다른 터미널에서 같은 프로젝트(또는 실행기 준비, 같은 Simulator, 같은 Swift 패키지 캐시)가 실행 중 | 그 실행이 끝난 뒤 다시 실행(잠금은 실행이 끝나면 자동으로 풀림) |
| `workspace_untrusted` | 작업 공간 폴더나 도구 폴더가 링크·파일이거나 다른 사용자 것 | 안내된 경로를 본인 소유 폴더로 바꾸거나 `REPROOF_HOME`으로 다른 위치 지정 |
| `lock_directory_untrusted` | 임시 폴더의 `reproof-<uid>` 잠금 폴더가 링크이거나 다른 사용자 것 | 그 경로를 확인해 지우거나 `TMPDIR`을 본인 폴더로 바꿈 |
| `file_operation_failed` | 읽을 수 없는 파일, 디스크 부족, 읽기 전용 폴더 | 안내된 파일의 권한(`ls -l`)이나 디스크 여유 공간 확인 |
| `내부 오류입니다. 이 로그를 첨부해 알려 주세요` | 예상하지 못한 오류 | 안내된 `crash-*.log`(작업 공간 `logs/`)를 첨부해 알림 |

`Package.resolved`가 없는 프로젝트는 버전 고정 없이 해석하므로 실행마다 버전이 달라질 수 있다는 경고가
나온다. 저장소에 `Package.resolved`를 커밋하면 같은 빌드가 재현된다. git 저장소가 아니거나 커밋이
없어도 빌드는 되며, 이때 빌드 식별에는 "소스 버전 미확인"이 표시된다.
