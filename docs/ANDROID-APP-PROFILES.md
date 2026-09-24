# Android 앱 프로필 연결

이 문서는 fixture·숫자 판정·제한된 수정 정책을 포함하는 v1 경로다.
일반 이슈 세션의 자동 로그에는 별도의 [Views 관찰 프로필](ANDROID-APP-OBSERVATIONS.md)을 사용한다.

앱별 패키지·activity·fixture·UI ID·oracle·빌드·수정 지점을 JSON 프로필로 고정한다.
`--app-profile`을 명시한 경우에만 이 계약을 사용한다. 기존 샘플 명령은 그대로 유지된다.

이번 구현은 일반 앱을 연결하기 위한 기반이다. 실제 대상 앱의 소스 경로와 버그는 아직 제공되지 않았다.
검증에는 저장소 샘플의 패키지·UI ID·fixture·함수명을 바꾼 합성 앱과 전용 Android 에뮬레이터를 사용했다.
실제 앱이나 물리 휴대폰에서 이 새 프로필 경로를 검증했다고 해석하지 않는다.

## 프로필과 SDK

[기본 샘플 프로필](../profiles/android-sample.json)을 출발점으로 사용한다.
서버와 CLI가 선택한 프로필은 불변 객체로 검증되며 녹화·HTTP 요청에서 새 정책을 선택할 수 없다.

| 필드 | 의미 |
|---|---|
| `id`, `package`, `activity` | 사례 ID와 실행 대상. activity는 상대·전체 클래스명을 지원한다. |
| `fixture`, `startState` | 재실행 시 앱이 준비해야 하는 상태. 모든 text/numeric 관찰 ID의 초기 값을 포함한다. |
| `targets` | 허용 tap/text/numeric/scroll/back/report ID. 텍스트 값은 빈 값·`QA`·`Test`, 숫자는 1–9자리다. |
| `oracle` | 같은 numeric ID의 서로 다른 버그 값과 정상 값. 설명은 조건에서 생성한다. |
| `build` | 한 앱 모듈의 assemble task, APK 상대 경로, unit-test task와 결과 경로. |
| `edit` | 같은 앱 모듈 `src/main/java` 또는 `src/main/kotlin` 아래 제품 파일과 0인자 숫자식 함수. |
| `sourceInputs` | 선택 사항. 사용자가 공개 입력으로 지정한 파일의 정확한 상대 경로 목록. 빌드 설정·소스·리소스를 함께 고정한다. |

프로필은 임의 shell, reset script, 편집 정규식, 자유로운 개인정보 값을 받지 않는다.
`edit.kind`는 `kotlin_numeric_expression_v1`이며 함수의 숫자 표현식 외 구조 변경을 차단한다.

지원하는 Kotlin Views Activity에는 [빌드 계측 명령](BUILD-INSTRUMENTATION.md)으로 앱 소스에 hook을
직접 넣지 않고 로그를 수집할 수 있다. [소스 삽입 방식](AUTO-INSTRUMENTATION.md)은 `--mode source` 옵션이다.
생성한 프로필은 `captureMode: debug_receiver`, `targets.report: null`, 소스 위치 목록과 계측 종류를 고정한다.
두 경로 모두 앱의 Report 버튼이 필요하지 않다.

기존 수동 연결 경로에서는 앱이 `ReproRecorder`를 통합하고 의미 이벤트를 직접 기록한다.
선택한 profile의 ID에 맞게 `safeTextTargets`, `tapTargets`, `scrollTargets`, `backTarget`, `reportTarget`을 설정한다.
Report 역할의 버튼은 `freezeAndExport()`를 호출해야 한다. SDK가 초기 metadata 뒤에 발급하는 실행 UUID를
Live 시작·reset 시 고정하고, 다른 실행의 기록이나 미완성·유실·비허용 텍스트 기록을 거절한다.

시작 시 고정된 intent extras `repro_mode`, `fixture_id`, `fixture_version`을 전달한다.
프로필 경로는 앱 전체 데이터에 `pm clear`를 실행하지 않는다. 앱의 fixture 구현이 force-stop 후 재실행에서
정확한 초기 상태를 제공해야 하며, 각 재현은 그 상태를 다시 확인한다.
접근성 resource ID로 조회할 수 없는 화면과 임의 앱의 내부 상태 복원은 이번 지원 범위가 아니다.

## 고정 빌드와 실행

기기를 한 대만 선택할 수 있는 환경에서는 다음과 같이 실행한다. 모든 새 산출물은 새로운 디렉터리에 둔다.

```bash
python3 -m reproof build --source PATH_TO_PUBLIC_APP_SOURCE \
  --app-profile PATH_TO_APP_PROFILE.json --output artifacts/new-profile-build

python3 -m reproof live-serve --android auto \
  --android-helper PATH_TO_MATCHING_LIVE_HELPER.apk \
  --android-app artifacts/new-profile-build/original.apk \
  --app-profile artifacts/new-profile-build/app-profile.json \
  --repair-source PATH_TO_PUBLIC_APP_SOURCE \
  --repair-build artifacts/new-profile-build --repair-agent claude \
  --output artifacts/new-profile-console
```

브라우저의 Reset & record → 허용된 QA 입력 → Stop → Analyze & repair recording → Open repaired app 흐름은 같다.
명시적 프로필 세션에서는 다른 앱으로 전환되면 좌표 입력을 거절하고 새 frame 게시를 멈춘다.
Reset으로 설정한 앱으로 복귀할 수 있다. 이 검사는 이미지의 모든 민감정보를 자동 마스킹하는 기능은 아니다.

단독 CLI의 `record`, `validate`, `replay`, `repair`, `tools`에도 같은 `--app-profile`을 전달한다.
프로필 bundle은 같은 프로필을 외부에서 명시하지 않으면 로드할 수 없다.
프로필 `repair`는 고정 driver의 `--driver-apk`와 `--driver-sha256`도 필요하다.
실제 앱 소스·QA를 Claude로 보내는 범위는 대상 앱 연결 시 별도 확인해야 한다.

## 빌드 입력과 검증 경계

프로필 빌드는 원본 앱과 플랫폼 드라이버를 각각 고정한 입력 복사본에서 빌드한다.
`sourceInputs`를 생략하면 기존 `.kt`, `.kts`, `.java`, `.xml` 정책을 사용한다.
명시하면 목록에 있는 파일만 복사하며 이미지·JSON assets·version catalog·공개 properties·ProGuard·
AIDL·C/C++ 입력도 같은 원본·후보 해시에 포함한다. 루트 `settings.gradle.kts`, 선택한 모듈의
`build.gradle.kts`와 편집 대상 제품 파일을 포함해야 한다. 의존성·컴파일러를 자동으로 설치하지 않는다.

```json
{
  "sourceInputs": [
    "settings.gradle.kts",
    "build.gradle.kts",
    "app/build.gradle.kts",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/java/example/MainActivity.kt",
    "app/src/main/java/example/Stock.kt",
    "app/src/main/res/drawable/logo.png",
    "gradle/libs.versions.toml",
    "public-build.properties",
    "buildSrc/build.gradle.kts",
    "buildSrc/src/main/java/PublicBuild.java"
  ]
}
```

위 목록은 필드 예시이며 실제 앱의 모든 공개 입력을 열거해야 한다. 목록은 준비본의 생성 파일까지
최대 900개, 파일당 8 MiB·합계 60 MiB다. 연결 파일·하드링크·특수 파일·대소문자 충돌·숨김 경로·
알려진 인증파일과 `local.properties`는 거절한다. 목록에 없는 파일은 읽거나 복사하지 않는다.
`build`, `.gradle`, `.kotlin`은 새 빌드의 출력이며 원본 캐시는 복사하지 않는다. 빌드·회귀 검사 중
선택한 리소스가 바뀌거나 새 입력이 추가되면 후보 검증을 거절한다. 공개 파일이라는 지정이
내용의 자동 비밀정보 검사를 뜻하지는 않는다.

`sourceInputs`를 쓰는 자동 계측은 `--mode build`를 사용한다. Kotlin 구문 분석으로 기존
`pluginManagement`와 `plugins` 블록에 `reproof-build-logic`의 별도 플러그인을 연결한다.
목록에 포함한 기존 `buildSrc`와 그 밖의 제품 입력은 바이트를 보존한다. 원본의 두 Gradle
연결 파일도 원래 디렉터리에서는 변경하지 않는다. 루트 설정과 선택한 앱 모듈은 Kotlin DSL을
요구하며, 포함된 공개 빌드 로직의 Groovy 파일은 명시적으로 복사할 수 있다.

빌드는 호스트 프로세스에서 실행한다. 작업 복사본은 OS/컨테이너 빌드 샌드박스를 제공하지 않는다.
임의 앱의 build logic과 넓은 AI 코드 수정은 별도 검토·격리가 필요하다.

schema v2 receipt는 원본 APK, 별도 플랫폼 runner proof, canonical 프로필과 그 digest를 고정한다.
Live identity, bundle, scenario, 각 replay, 후보 identity에 같은 프로필을 연결한다.
helper와 driver가 실제 사용한 native 계약의 digest도 직접 응답하며, 호스트는 그 응답을 검사하고 실행 증거에 남긴다.

원본 3/3 → 제한된 실제 AI 제안 → 보호된 회귀 검사 → 수정본 3/3 기준을 유지한다.
회귀 결과는 선택한 앱 모듈의 해당 task 출력이어야 하며, 실행 전 존재한 보고서·부모 경로 symlink·과대 XML을 거절한다.
후보는 보호 파일·APK·설치·fixture·기기·원본 driver·프로필 증거가 일치하고 cleanup이 확인된 뒤에 공개한다.

## 합성 연결 검증 재준비

```bash
python3 scripts/prepare-app-profile-fixture.py --output artifacts/new-inventory-fixture
python3 -m reproof build --source artifacts/new-inventory-fixture/source \
  --app-profile artifacts/new-inventory-fixture/app-profile.json \
  --output artifacts/new-inventory-build
```

이 fixture는 `io.reproof.inventory`, `inventory_empty`, `label`/`quantity`/`commit`/`export_capture`,
`unitsPerItem()`을 사용한다. 실제 사용자 앱과 구분하기 위한 합성 검증물이다.
helper는 같은 플랫폼 native 소스로 빌드한다. 공유 기기에 다른 작업이 있으면 전용 기기나 에뮬레이터를 사용한다.
helper 빌드 명령은 [Android Live 실행 문서](ANDROID-LIVE-REPAIR.md)의 `:live:assembleDebug`를 참고한다.

## 실행 증거

2026-09-11에 전용 API 36 Android 에뮬레이터에서 전체 흐름을 완료했다.
Raw Live 입력 5개에서 실제 SDK 이벤트 `replace(label, QA)`, `tap(commit)` 2개를 수집했다.
실제 Claude 첫 제안으로 `unitsPerItem()` 증가식을 `1`로 수정했다.

| 항목 | 결과 |
|---|---|
| 원본 | 3/3, quantity `2` |
| 보호된 회귀 검사 | 1 통과, 실패·오류·skip 0 |
| 수정본 | 3/3, quantity `1` |
| 프로필·native 계약 | 6개 실행의 실제 driver echo와 일치 |
| 후보 Live 재개 | 새 세션에서 `QA` + Add → quantity `1` |
| 대상 앱 범위 | Home 이후 입력 거절·새 frame 중지·Reset 복귀 통과 |
| 종료 | 세션·브라우저·서버·helper·fixture·직접 만든 에뮬레이터 종료, 임시 AVD 삭제 |

- [전체 검증 JSON](../artifacts/android-profile-validation.json)
- [실제 수정 보고서](../artifacts/android-profile-isolated-console/repairs/cd565b8a37d040e3ab33805405052ab5/repair/report.html)
- [실제 Claude 패치](../artifacts/android-profile-isolated-console/repairs/cd565b8a37d040e3ab33805405052ab5/repair/attempt-1/patch.diff)
- [브라우저 원본](../artifacts/android-profile-before.png) · [브라우저 후보](../artifacts/android-profile-after.png)
- [후보·cleanup 증거](../artifacts/android-profile-final/result.json)
- [대상 앱 이탈 검사](../artifacts/android-profile-focus-qa/result.json)
- [현재 후보 증거 검사](../artifacts/android-profile-current-gates.json)

Python 테스트 217개가 통과했다. 변경하지 않은 웹 코드의 15개 통과 결과는 재사용했다.
처음 연결된 공유 에뮬레이터에서 다른 앱이 앞으로 나와 해당 시도를 종료했으며, 그 시도에는 모델 요청이 없었다.
최종 성공 근거는 위 전용 에뮬레이터 경로다. 다른 작업의 에뮬레이터를 종료하거나 데이터를 지우지 않았다.
