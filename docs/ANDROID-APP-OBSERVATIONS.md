# 일반 Android Views 앱의 자동 관찰

`views-observation-v2`는 Kotlin Activity의 명시적인 버튼·화면 ID를 관찰한다.
fixture, 숫자 판정식, AI 수정 권한은 이 프로필에 넣지 않는다. 시작 조건과
판정·수정 정책은 [일반 이슈 흐름](ISSUE-WORKFLOW.md)의 별도 계약으로 등록한다.
기존 v1 기록·수정 프로필은 [Android 앱 프로필](ANDROID-APP-PROFILES.md)을 따른다.

## 공개 입력과 준비

관리자가 앱 식별자, Activity, Debug 빌드, 공개 파일을 선택한다. 예:

```json
{
  "schemaVersion": 2,
  "kind": "views-observation-v2",
  "package": "com.example.inventory",
  "activity": ".MainActivity",
  "build": {
    "task": ":app:assembleDebug",
    "apk": "app/build/outputs/apk/debug/app-debug.apk"
  },
  "sourceInputs": [
    "settings.gradle.kts",
    "build.gradle.kts",
    "app/build.gradle.kts",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/java/com/example/inventory/MainActivity.kt",
    "app/src/main/res/values/ids.xml"
  ],
  "tapTargets": ["save"],
  "screenTargets": {"inventory_root": "inventory"}
}
```

실제 빌드가 쓰는 리소스·버전 카탈로그·공개 Gradle 설정·기존 `buildSrc`도
`sourceInputs`에 포함한다. 파일 내용의 공개 여부는 관리자가 판단한다.
경로 검사만으로 소스에 들어 있는 모든 비밀 값을 분류하지 않는다.
선택되지 않은 파일, 기존 빌드 출력, 사용자 캐시를 복사하지 않는다. 링크·중복·
충돌 경로와 인증 파일 경로는 거절한다. 원본 파일은 최대 900개, 파일당 8 MiB,
합계 60 MiB이며, 준비 후 생성 파일도 900개 한도에 포함된다.

```sh
reproof android-instrument \
  --source /path/to/public-android-source \
  --observation-profile /path/to/views-profile.json \
  --output /path/to/new-prepared-directory

reproof android-app-build \
  --source /path/to/new-prepared-directory/source \
  --observation-profile /path/to/new-prepared-directory/app-profile.json \
  --output /path/to/new-build-directory \
  --gradle /path/to/approved-offline-gradle \
  --java-home /path/to/jdk17 \
  --sdk-home /path/to/android-sdk
```

준비는 Kotlin PSI로 선택한 Activity의 클릭 등록 지점을 확인하고, 별도 포함
Gradle 플러그인을 연결한다. 제품 Kotlin·Java 본문, 리소스, 기존 `buildSrc`를
보존하며 `settings.gradle.kts`와 선택한 모듈의 플러그인 연결을 변경한다.
실제 Debug 빌드에서 ASM이 선택한 클래스의 콜백 등록과 생명주기에 훅을 넣는다.
앱 소스에 SDK 호출을 추가하지 않는다. 준비 결과의 `patch.diff`와
`instrumentation.json`으로 변경과 원본 해시를 확인할 수 있다.

빌드는 새 작업 디렉터리의 고정 입력을 사용하고 `--offline`으로 실행한다.
결과는 `app.apk`, `receipt.json`, 공개 소스 사본이다. 계측본은 실제 변환
클래스·사이트 증거와 APK 내부 관찰 프로필을 다시 검사한다. 기본 Android 사용자
경로는 해당 빌드의 `android-user-home`이다. 연속 설치에 필요한 같은 Debug 서명은
승인된 빌드 도구 설정으로 제공한다. 이 로컬 빌드 명령 자체가 회사 서명이나
격리된 후보 실행 환경을 제공하지는 않는다.

## 일반 Live 세션

[런타임 등록 프로필](WORKER-RUNTIME.md)의 Android v2 계약에 선택한 APK의 해시·크기·
버전을 바인딩하고, `observations`의 `logs`와
`logAdapter: {"id":"repro-app-log","version":1}`을 선언한다.
등록한 프로젝트·앱·빌드와 일치하는 공유 서비스 구성을 사용한다.

```sh
reproof live-serve \
  --android AUTHORIZED_SERIAL \
  --android-helper /path/to/current-live-debug.apk \
  --android-app /path/to/new-build-directory/app.apk \
  --android-profile /path/to/runtime-profile.json \
  --shared-config /path/to/shared-config.json \
  --output /path/to/new-service-state
```

현재 helper는 관찰 시작 기능 버전 2를 광고해야 한다. 서비스는 APK 안의 관찰
프로필을 검증하고, 설치 전에 APK 사본을 고정하며, 설치 후 실제 APK 해시를
선택한 해시와 비교한다. 관찰 프로필과 런타임 등록 프로필은 서로 다른 계약이다.
자동 로그는 APK에 들어 있는 관찰 프로필의 해시에 바인딩한다.

일반 Android의 `launch`는 관찰 설정과 관계없이 앱 프로세스를 다시 시작한다.
앱 데이터는 지우지 않는다. 관찰을 켠 경우에는 새 실행 ID를 전달하며,
해당 실행의 시작 로그가 확인돼야 시작·재실행 성공을
반환한다. 이전 실행의 파일, 다른 앱·프로필, 준비되지 않은 APK를 현재 로그로
채택하지 않는다. 종료 후에도 저장한 로그는 세션 증거에서 조회할 수 있다.
재현할 입력은 Live 행동 기록으로 남기고, 이 앱 관찰 로그는 별도 진단 증거로
사용한다. 백엔드·앱의 초기 데이터는 승인한 시작 조건으로 따로 준비·검증한다.

## 수집 범위

- 설정한 버튼의 시작·반환·예외, Activity 생명주기, 설정한 화면의 등장·퇴장.
- 로그당 최대 2,000개 이벤트, 1 MiB, 30분. 누락·잘림 상태를 명시한다.
- UI 텍스트·입력 내용·원시 클래스명·임의 함수 인자·네트워크·DB 내용은 수집하지 않는다.
- Release에는 자동 관찰 런타임과 관찰 프로필을 포함하지 않는다.

현재 준비기는 한 Kotlin Activity의 명시적 Views 클릭 등록을 지원한다. 매핑이
모호하거나 지원하지 않는 생명주기 상속이면 준비·빌드를 거절한다. Compose,
모든 Activity의 자동 탐색, 임의 내부 함수 추적을 지원한다고 표시하지 않는다.
준비·빌드 성공만으로 제품 동작이나 회사 QA 수용 검사가 완료되는 것은 아니다.

전체 진행 범위와 남은 검증은 [제품 완성 계획](PRODUCT-DELIVERY-PLAN.md)에 기록한다.
