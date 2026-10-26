# 일반 UIKit 앱의 자동 관찰

일반 앱은 `uikit-observation-v2` 프로필로 준비한다. 앱의 SDK 호출이나 Report 버튼
없이 지정한 버튼의 호출 시작·반환·예외 여부, 지정한 화면의 등장·퇴장, 앱 생명주기를
수집한다. 실행 가능한 행동과 시작 조건은 [공유 이슈 흐름](ISSUE-WORKFLOW.md)에
기록한다. 관찰 프로필 자체가 fixture나 재현 판정 기준을 만들지는 않는다.

## 공개 입력과 앱 선택

아래는 형식 예시다. `sourceInputs`에는 빌드에 필요한 공개 파일을 빠짐없이 지정한다.
목록의 제품 파일과 계측기가 만든 고정 파일만 복사한다. 이미지·asset catalog JSON·지역화 문자열·
스토리보드·XIB·xcconfig도 지정할 수 있다. credential, 개인 설정, 빌드 산출물,
symlink와 hardlink 입력은 거절한다.

```json
{
  "schemaVersion": 2,
  "kind": "uikit-observation-v2",
  "applicationId": "com.example.inventory",
  "project": "Inventory.xcodeproj",
  "target": "Inventory",
  "build": {
    "scheme": "Inventory",
    "product": "Inventory",
    "infoPlist": "App/Info.plist",
    "debugConfiguration": "Debug"
  },
  "sourceInputs": [
    "App/Inventory.swift",
    "App/Info.plist",
    "Inventory.xcodeproj/project.pbxproj",
    "Inventory.xcodeproj/xcshareddata/xcschemes/Inventory.xcscheme"
  ],
  "tapTargets": ["inventory.save", "inventory.close"],
  "screenTargets": {"inventory.editor": "editor", "inventory.saved": "saved"}
}
```

`tapTargets`와 `screenTargets`의 키는 실제 `accessibilityIdentifier`다. 버튼은 현재
단일 target/action의 `touchUpInside` UIControl을 지원한다. 입력 내용·화면 문구·
클래스 이름·예외 메시지는 자동 관찰에 넣지 않는다. UIControl 이외의 제스처나
SwiftUI, 앱 내부의 비동기 함수·HTTP·DB 호출을 수집한다고 표시하지 않는다.

현재 프로젝트는 명시적인 Info.plist를 사용해야 하고, 선택한 앱 타깃의 각 빌드
설정에 `PRODUCT_BUNDLE_IDENTIFIER`가 프로필 값으로 선언돼 있어야 한다.
프로필의 파일 경로는 입력 루트 기준이고, Xcode의 `INFOPLIST_FILE`은 선택한 프로젝트
루트 기준의 상대 경로여야 한다. 하위 폴더의 `.xcodeproj`도 지원한다.
외부 패키지, 사용자 스크립트와 생성 입력의 자동 발견·다운로드는 제공하지
않는다. 입력은 최대 900개, 개별 8 MiB, 합계 60 MiB다.

## 준비와 빌드

새 출력의 부모 디렉터리는 먼저 만든다. 아래 명령은 신뢰하는 로컬 공개 소스를
사용하는 명시적인 빌드 경로다. 원격 QA나 AI 후보의 보호 실행은 별도 실행 계약을 따른다.

```sh
reproof ios-instrument --source APP_SOURCE --profile uikit-profile.json \
  --output WORK/prepared
reproof ios-app-build --source WORK/prepared/source --output WORK/debug
reproof ios-app-build --source WORK/prepared/source --configuration Release \
  --output WORK/release
```

준비 과정은 별도 복사본의 선택한 프로젝트 연결만 변경한다. 기존 제품 코드·
Info.plist·리소스 바이트는 보존하고, 선택한 Debug 설정에 런타임과 프로필을
추가한다. 전용 `REPRO_OBSERVATIONS` 조건을 사용해 제품의 기존 `DEBUG` 분기를
바꾸지 않는다. 다른 모든 설정은 런타임 소스를 제외하고 기존 Info.plist를 사용한다.
준비 영수증은 바이너리 리소스도 해시에 포함한다.

`ios-app-build`는 명시한 파일의 고정 사본에서 빌드하며 자동 패키지 해석·업데이트와
서명을 끈다. 출력의 앱 ID·버전·전체 파일 해시를 확인한 뒤 `receipt.json`을 만든다.
`--sdk device`는 서명 없는 기기용 빌드다. 설치나 물리 iPhone 검증의 완료를 뜻하지 않는다.
원본 비교 빌드는 `--source APP_SOURCE --profile uikit-profile.json`으로 실행한다.

## 공유 서비스에서 실행

공유 프로젝트의 앱·빌드·기기 할당을 등록하고, [일반 iOS 런타임 프로필](WORKER-RUNTIME.md)을
같은 프로젝트 digest와 선택한 앱 artifact에 연결한다. 자동 관찰을 사용할 프로필에는
`observations`의 `logs`와 `logAdapter: {"id":"repro-app-log","version":1}`을 선언한다.
서비스는 앱에 일치하는 v2 관찰 프로필이 실제로 포함돼 있는지 확인한다.

```sh
reproof live-serve --shared-config shared-coordinator-v2.json \
  --simulator SIMULATOR_ID --products LIVE_HELPER_PRODUCTS \
  --ios-app SELECTED_APP --ios-profile ios-runtime-profile.json \
  --output WORK/live
```

helper는 현재 배포 소스로 다시 빌드한다. 일반 앱용 실행은 `observe` 모드와 새 실행 ID,
내장 프로필 digest만 전달한다. 샘플 초기화 변수는 전달하지 않는다. 재실행 시 실행 ID를
교체하고 새 로그 marker를 확인하며, 종료는 같은 기기 권한으로 완료해야 한다.

Simulator 앱은 승인된 바이트의 별도 고정 사본으로 설치하고 전체 파일을 다시 검사한다.
같은 크기의 오래된 실행 파일을 덮어쓸 때 이전 코드가 남는 문제도 이 경로로 검증했다.
설치된 앱의 전체 파일이 선택한 빌드와 다르면 세션을 거절한다. Simulator의 Debug →
Release 덮어쓰기에서 이전 Debug 라이브러리가 남는 경우도 여기에 해당한다. 이 경로가
앱 데이터를 자동으로 삭제하지는 않는다. 깨끗한 전용 QA 기기나 명시적으로 승인된
초기화·provisioning 계약이 필요하며 운영 복구 통합은 제품 계획의 별도 단계다.

관찰은 실행당 2,000개 이벤트·1 MiB·30분으로 제한한다. 누락이나 저장 실패는
`lostEvents`/`truncated`에 표시한다. 공유 기록의 보존·권한 정책이 추가로 적용된다.
기존 `uikit-runtime-v1` 샘플의 replay/fixture/capture 계약은 그대로 유지하며,
v2 관찰 프로필로 샘플 capture를 채택할 수 없다.

## 제한된 로컬 워크스페이스 선택

기존 `.xcodeproj` 형식은 그대로 유지한다. 여러 로컬 프로젝트를 묶는 빌드에는
프로필 최상위에 `"workspace": "Inventory.xcworkspace"`를 추가할 수 있다.
`project`와 `target`은 여전히 **계측할 단 하나의 프로젝트와 앱 타깃**이다.
빌드 컨테이너만 `xcodebuild -workspace … -scheme …`으로 선택하며, 다른 프로젝트는
계측하지 않는다. 서로 다른 프로젝트의 동명 타깃은 scheme의 프로젝트 경로와
BlueprintIdentifier로 구분한다.

이 초기 경로는 다음의 명시적 입력만 지원한다.

- `sourceInputs`에 워크스페이스의 `contents.xcworkspacedata`, 모든 멤버 프로젝트의
  `project.pbxproj`, 그리고 `WORKSPACE/xcshareddata/xcschemes/SCHEME.xcscheme`를 선언한다.
- workspace의 `group:`/`container:` 참조는 입력 루트 안의 `.xcodeproj`에만 연결한다.
  절대 경로, `..`, 외부/누락/중복 프로젝트, 중첩 workspace는 거절한다.
- 정확히 하나의 공유 scheme이 선택한 프로젝트·타깃을 실행용 빌드 대상으로 지정해야
  한다. 같은 이름의 다른 공유 scheme, 외부 프로젝트를 가리키는 실행/테스트 참조,
  scheme의 사전/사후 스크립트·외부 실행 파일·test plan·환경 변수·사용자 작업 폴더는
  지원하지 않는다. 이 경로는 고정된 구조 태그만 받는다.
- SPM 로컬/원격 패키지, 암묵적 `PBXProject.projectReferences` 그래프는 이 초기
  workspace 경로에서 거절한다. 기존 project 경로의 지원 범위는 변경하지 않는다.

목록에 선언한 정규 파일만 기존 동결 경계를 통해 복사하며, 파일 수·용량·경로·링크
제한은 완화하지 않는다. workspace/scheme/project 구조는 출력 생성과 빌드 전에
검증한다. 준비 결과는 선택한 프로젝트 외의 입력 바이트를 보존한다.
이는 신뢰하는 로컬 빌드 입력 선택 기능이지 임의 Xcode 프로젝트 코드를 실행하는
샌드박스가 아니며, 회사 규모 앱·SPM 전체 지원이나 동작 재현을 뜻하지 않는다.
합성 입력과 가짜 빌드 명령으로 경계·명령 선택을 검사했으며, 실제 Xcode workspace
컴파일·Simulator 실행은 별도 검증이 필요하다.
