# iOS UIKit 자동 로그 수집

이 문서는 기존 `uikit-runtime-v1` 샘플의 실행 가능한 capture와 판정 계약이다.
일반 UIKit 앱의 설정·공개 빌드 입력·자동 관찰은 [v2 관찰 가이드](IOS-APP-OBSERVATIONS.md)를 따른다.

앱 함수에 `Recorder` 호출이나 Report 버튼을 넣지 않고 Debug 빌드에서 입력·버튼 실행·앞뒤 숫자 상태를 수집한다. `ios-instrument`가 별도 복사본의 Xcode 설정에 런타임을 연결한다. 원본 프로젝트와 기존 제품 Swift/Objective-C 파일·Info.plist는 그대로 둔다.

현재는 기존 ReproSample UIKit 어댑터의 세 사례(counter, duplicate-submit, reset)를 지원한다. 임의 iOS 앱, SwiftUI, 모든 내부 함수·네트워크·DB 로그의 자동 수집 기능으로 해석하지 않는다. 새 앱에는 해당 앱의 UI ID·fixture·관찰/편집 계약과 빌드 입력 지원을 먼저 추가해야 한다.

## 실행

SDK 호출이 없는 저장소 소유 합성 앱을 만드는 아래 명령은 검증용이다. 실제 계측 명령이 앱에서 SDK 호출을 삭제하지는 않는다.

```bash
python3 scripts/prepare-ios-instrumentation-fixture.py --output artifacts/NEW_IOS_PLAIN
python3 -m reproof ios-instrument \
  --source artifacts/NEW_IOS_PLAIN/source --output artifacts/NEW_IOS_PREPARED
python3 -m reproof ios-build \
  --source artifacts/NEW_IOS_PREPARED/source --output artifacts/NEW_IOS_BUILD \
  --simulator "$REPRO_SIMULATOR_ID"
python3 -m reproof ios-record \
  --build artifacts/NEW_IOS_BUILD --case counter --output artifacts/NEW_IOS_RECORD \
  --simulator "$REPRO_SIMULATOR_ID"
```

`REPRO_SIMULATOR_ID`는 사용할 수 있는 부팅된 Simulator의 UUID다. 출력 경로는 매번 새 경로를 사용한다. `ios-record --manual`은 준비 완료 후 직접 앱을 조작하고 터미널에서 Enter로 수집을 끝낸다. 앱 안에 Report 버튼이 없어도 된다. 물리 iPhone의 수동 조작은 기존 Live 경로를 사용한다.

기존 수동 SDK 통합 프로젝트는 기존 기록 경로를 유지한다. 이미 `Recorder.shared`를 사용하는 소스나 재계측된 프로젝트는 `ios-instrument`가 거절한다.

## 수집과 증거

- Debug에만 Objective-C bootstrap, Swift collector, 고정된 프로필을 추가한다. 런타임이 실제 UIApplication 클래스의 `sendAction` 실행을 관찰한다.
- 한 callback을 같은 sender로 한 번 실행하고 반환값·같은 Objective-C 예외를 보존한다. collector 실패가 제품 예외나 반환값을 덮어쓰지 않게 한다. 실행 시간·스택·런타임 메서드 identity가 원본과 같다는 보장은 아니다.
- 프로필의 텍스트·숫자·버튼·화면 ID만 관찰한다. 합성 입력은 빈 문자열/QA/Test, 숫자는 9자리 이하로 제한한다. 키 입력 중간값 대신 완료된 입력을 기록한다.
- 각 tap/navigation 이벤트에 전후 숫자·화면과 `returned`/`threw`를 연결한다. 임의 selector·클래스·인자·예외 메시지는 로그에 넣지 않는다.
- 호스트가 매 실행의 UUID를 정하고 준비 완료를 기다린다. 내보내기는 로컬 Darwin notification과 앱 컨테이너의 원자적 파일 저장으로 진행한다. notification 자체를 인증 수단으로 취급하지 않는다.
- run/session/build/profile/fixture/endSequence가 모두 맞는 파일만 수집한다. 진단은 해당 세션 디렉터리에서만 읽고, 앞뒤 marker 확인으로 다른 세션 자료의 혼합을 거절한다.
- bundle에 `diagnostics.json`을 포함하고 그 내용을 scenario digest 및 실제 AI 요청에 연결한다. 수정 후보도 같은 보호 런타임·프로필·테스트 산출물 검사를 통과해야 한다.
- 한 active key window, 지정한 UIControl의 단일 target/action/touchUpInside 형태를 요구한다. 지원 범위 밖의 상태나 background 전환, 수집 한도 초과를 완전한 기록으로 내보내지 않는다.

최대 500개 이벤트/액션, 20MiB 수집 데이터, 10분 세션으로 제한한다. 비동기 작업 완료·scroll의 자동 의미화·프로세스 충돌 후 영속 로그 회수는 지원하지 않는다.

## 원본 보존과 Release

준비 복사본의 `Reproof.xcodeproj/project.pbxproj`와 새 `ReproofInstrumentation/`만 계측으로 변경한다. Xcode 프로젝트 파일은 plist로 다시 직렬화하므로 diff가 클 수 있다. 기존 지원 제품 입력의 바이트 일치는 preparation receipt로 확인한다. 복사본은 OS 샌드박스가 아니다.

현재 입력 복사는 기존 iOS 어댑터가 허용한 Swift/Objective-C/header/plist/Xcode project/scheme/yml 등에 한정된다. assets/storyboard/외부 패키지를 포함한 모든 앱 빌드 입력을 복사한다고 보장하지 않는다.

```bash
python3 -m reproof ios-build \
  --source artifacts/NEW_IOS_PREPARED/source --configuration Release \
  --output artifacts/NEW_IOS_RELEASE --simulator "$REPRO_SIMULATOR_ID"
```

Release에는 세 런타임 소스를 빌드에서 제외하고 원래 Info.plist를 사용한다. 자동 수집 profile·bootstrap·collector·no-op API가 포함되지 않는다. 실제 Mach-O의 심볼/문자열과 Info.plist로 검증한다.

## Live 연결

현재 `live-ios` 소스로 `ReproLive` scheme을 `build-for-testing`한 Simulator용 helper가 필요하다.

```bash
python3 -m reproof live-serve \
  --simulator "$REPRO_SIMULATOR_ID" --products "$REPRO_LIVE_PRODUCTS" \
  --repair-source artifacts/NEW_IOS_PREPARED/source \
  --repair-build artifacts/NEW_IOS_BUILD --repair-agent claude \
  --output artifacts/NEW_IOS_LIVE
```

브라우저에서 Open → Reset & record → QA 입력/Add → Stop → Analyze & repair를 사용한다. 이 마지막 동작은 설정한 샘플 소스와 합성 QA를 실제 Claude에 전송한다. 새 앱의 비공개 소스/QA는 승인된 전송 범위인지 먼저 확인한다.

## 검증 자료

실제 Claude 첫 제안으로 원본 3/3 재현, 보호 로직 회귀 1개 통과, 수정본 3/3 정상 동작, 새 Live 세션 count `1`까지 확인했다. 실제 요청의 prompt hash를 재구성해 자동 진단 포함도 검증했으며 추가 모델 요청은 하지 않았다.

전체 Python 270개 검사가 통과했다. 이후 Live 인계의 잠금 충돌을 회귀 테스트로 재현하고 수정한 뒤 관련 132개 검사를 다시 통과했다. 수집 도중 marker 전체 identity가 교체되는 경우도 Simulator/물리 reader 양쪽에서 거절한다.

- [종합 검증](../artifacts/ios-auto-validation.json), [새 Live 후보 결과](../artifacts/ios-auto-live-final/result.json), [실제 요청 진단 포함](../artifacts/ios-auto-live-final/prompt-proof.json)
- [실제 AI 수정 보고서](../artifacts/ios-auto-live-final-console/repairs/9e0e8c52bf294646ab7a0d7a9f1699b2/repair/report.html)
- [Live 수정 전 화면](../artifacts/ios-auto-live-final-before.png), [수정 후 화면](../artifacts/ios-auto-live-final-after.png)
- [기록 거절 및 Release 무기록](../artifacts/ios-auto-safety-qa.json)

- [원본/계측본 동작 비교](../artifacts/ios-auto-behavior-qa/result.json)
- [첫 실제 자동 수집](../artifacts/ios-auto-counter-record/bundle/capture.json), [전후 진단](../artifacts/ios-auto-counter-record/bundle/diagnostics.json)
- [Release 제외 검사](../artifacts/ios-auto-release-isolation.json)

검증용 세션·브라우저·서버·lease와 전용 Simulator는 모두 정리했다. [정리 결과](../artifacts/ios-auto-live-final/cleanup.json)를 남겼다.

검증 기기는 이 작업만을 위해 만든 iPhone 17 Pro Simulator, 설치된 iOS 27.0 beta runtime이다. 이번 자동 계측의 물리 iPhone 동작은 별도로 검증하지 않았다. 기존 수동 SDK의 실기기 검증과 구분한다.
