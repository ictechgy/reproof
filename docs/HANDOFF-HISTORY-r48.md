# Historical handoff — through r48

_Archived: 2026-09-15 05:32 KST by Codex_

이 문서는 이전 HANDOFF.md의 상세 이력이다. **현재 상태·다음 작업·승인 범위는
[프로젝트 HANDOFF.md](../HANDOFF.md)를 우선한다.** 본문의 “현재/최신/다음”은 각 기록 당시를
뜻하며, 옛 완료 수·차단 판단·권한 설명을 현재 상태나 새 승인으로 사용하지 않는다.
본문의 코드 경로는 프로젝트 루트 기준이고, Markdown 링크는 이 위치에 맞게 조정했다.

Original HANDOFF.md SHA-256: `3351f5e1d8cf82b2bdb6e830bfad125e7355e2d1bffc9f45a0c3050df0bab131`

---

# Handoff

_Last updated: 2026-09-14 KST by Codex_

## Active Delivery — 2026-09-13

사용자가 남은 작업의 우선순위를 재검토한 뒤 계속 진행을 요청했다. 현재는
[일반 앱 제품 경로 완성 계획](../docs/PRODUCT-DELIVERY-PLAN.md)을 실행 중이다.
아래 G9 검사는 해당 시점의 소프트웨어 근거이며 독립적으로 할 수 있는 제품
구현이 모두 끝났다는 뜻이 아니다. 새 설치 경로 → 일반 앱 계측 → 진단 증거
연결 → 한 Mac 제품 흐름 → 보호 실행기/복구 → 실제 회사·두 Mac 검증을 이어간다.

최신 구현/검증 checkpoint는 **r48** (`d4-protected-adapters-r1/foundation-progress-r48.json`)이다.
`IOSMobileOperationStore.native_owner()`와 원래 producer/기기 잠금의 descriptor export를 추가했다.
모든 역할의 앱을 준비해야 진입하며, 실제 발급된 같은 iPhone 권한과 현재 grant를 확인한다.
native 진입 기록 이후에는 준비 파일 복구로 비용이나 기기를 해제할 수 없다.
`native/ios-device-guardian/main.c`와 고정 조회 조합도 구현했다. `query.nativeGuardian`의 경로·해시를
조회 정의에 포함하며 `open_client(native_owner=...)`가 같은 owner의 잠금을 native 자식에 전달한다.
부모 SIGKILL에는 guardian이 직접 자식을 수거하며, guardian SIGKILL에는 자체 SDK 대역이
끝날 때까지 원래 잠금을 유지하는 실제 OS 검사가 통과했다. 실제 CoreDevice의 fd 유지·daemon
격리는 아직 검증하지 않았고, guardian은 details/apps/processes만 실행한다.

관련 **148개 검사**와 538개 입력의 불변성을 `ios-native-gate-r1.json`에 기록했다.
clang 정적 분석 진단은 0개다. **D4 package r15**에는 모듈 155개·공개 리소스 110개가 들어 있다.
wheel SHA-256은 `636e0bfba2e5f4d360267df7b11ae05a3a8f3e2d64e53f4bca1092840e46966d`다.
별도 설치의 native guardian 컴파일·조회·수거·준비 전용 복구 거절을 확인했다. 설치 runtime은
checkout/test import를 차단했고, 부모의 IPA fixture 생성과 SDK 프로토콜 대역 사용은 명시했다.
첫 설치 harness의 argv 개수 오류 로그는 보존했고 harness만 수정했다. 실제 Apple/기기는 사용하지 않았다.
r14 wheel/증거는 보존하고 build-input/installation 사본 19,050,496바이트만 정리했다.
최신 r15 설치본과 복구된 오프라인 Gradle 의존성은 유지한다. `parent-final-verification-r48.json`과
`next-work-after-r48.json`을 읽고 고정 iOS 설치·XCTest·재생·sanitation 및 native 복구 구현을 이어간다.

사용자가 2026-09-14에 **“남은 개발부터 ㄱㄱ”**라고 요청해 독립적으로 가능한 구현을
재개했다. r47에서 실제 환경 미제공을 전체 개발의 blocker로 적용한 판단은 너무 넓었다.
목표 도구의 기존 `blocked` 표시는 그때의 상태이며 개발 완료나 재개 금지의 근거가 아니다.
실제 환경 질문은 대기 상태로 두고, iOS 실행 소유권·고정 어댑터 구현과 로컬 검증을 진행한다.
이전 r44에서는 누적 r39–r43 소스를 **D4 package r14**에 포함했다. 전체 실행과 캐시 복원 후 실패 검사 재실행을
합쳐 고유 1,883개가 통과했고, 별도 설치본의 Android/iOS 준비·복구 검사도 통과했다.
r13 wheel과 증거는 보존했으며 r13의 build-input/installation 사본만 정리했다.
간헐적 SDK 멈춤의 원인, 실제 mobile qualification과 일반 보호 실행 CLI는 남아 있다.
재개 시 r48에서 바뀌지 않은 아래 r44 구현 근거를 재사용한다. 실제 기기·격리 환경의 수용 검사 대기와
계속 가능한 개발을 구분한다. 기존 환경 질문은 반복하지 않는다.

첫 설치 단계는 완료했다. `artifacts/product-delivery/d0-package-r1/acceptance.json`에
표준 wheel의 100개 공개 리소스, 별도 가상 환경의 실제 CLI/HTTP/브라우저,
리소스 내보내기와 Swift 컴파일, 관련 검사 38개 통과를 기록했다.
일반 UIKit의 프로필·공개 빌드 입력·자동 관찰은 구현했다. `ios-instrument --profile`,
`ios-app-build`, `live-serve --ios-profile`을 사용한다. 실제 Simulator에서 일반 앱의
원본/계측본 Debug·Release 네 서비스 경로, 자동 클릭·화면 로그, 실행 ID 교체와
정상 종료를 확인했다. `docs/IOS-APP-OBSERVATIONS.md`와
`artifacts/product-delivery/d1-uikit-r1`을 따른다.
`d1-uikit-r1/acceptance.json`은 고유 검사 98개, 실제 네 서비스 경로, 설치본 서비스,
실패 모드와 세 전용 Simulator의 종료·삭제를 연결한다. 두 실패 설치의 격리 기록은
보존했다. `d1-package-r1`의 현재 wheel은 Python 캐시 변조 거절까지 반영한다.
설치된 서비스에서 회사 앱을 수정·검증하는 전체 흐름의 완료 근거는 아직 아니다.

Android v1은 `sourceInputs`로 공개 리소스·기존 buildSrc를 보존하고 별도 포함 플러그인을
연결하도록 확장했다. `d1-android-r1/acceptance.json`에 관련 검사 77개, 실제 원본/계측본
Debug·Release 빌드, 동일 제품 클래스와 Release DEX를 기록했다. `d1-android-package-r1`의
새 wheel을 별도 환경에 설치해 CLI 준비·실제 보호 빌드도 확인했다. 현재 캐시에 고정
AGP 8.13.2·ASM 9.8 파일이 있어 기존 의존성 차단 검사 3개를 다운로드 없이 통과했다.
별도의 Views v2 관찰 프로필도 구현했다. `android-instrument --observation-profile`,
`android-app-build`, `live-serve --android-profile`을 사용한다. APK의 관찰 설정·실행 ID를
일반 공유 세션에 연결하며 fixture·숫자식·수정 권한을 요구하지 않는다.
`d1-android-views-r1/acceptance.json`과 `docs/ANDROID-APP-OBSERVATIONS.md`가 근거다.
고유 검사 145개, 원본/계측본 Debug·Release 빌드, 같은 제품 클래스·Release DEX,
실제 에뮬레이터의 버튼·화면 로그·실행 ID 교체·종료 후 보존을 확인했다.

실제 일반 앱 연결에서 helper의 동작 목록 JSON 배열, 관찰 응답의 앱 식별 필드,
첫 프레임을 기다리는 준비 교착, 첫 화면 이전 성공 응답을 수정했다. 일반 `launch`는
로그 설정과 관계없이 앱 데이터를 보존하며 프로세스를 다시 시작하고 첫 화면을
준비한다. APK는 등록 때부터 링크 없는 파일 경계로 검사하고 설치 전 고정 사본,
설치 후 선택한 해시로 검증한다. D1 Views wheel은 `d1-android-views-package-r2`의
`0248831e9fc3f4c9095134b8e86f6c89b58f98f11bf8cd50ab22b8ed89f3e33e`이며,
새 설치의 실제 준비·빌드·공유 서비스 실행을 확인했다. 전체 제품 완료는 아니다.

D2 진단 연결도 구현·검증했다. `docs/PROJECT-DIAGNOSTICS.md`와
`artifacts/product-delivery/d2-diagnostics-r1/acceptance.json`을 따른다. 원본의 별도
앱 로그 MIME 참조에서 정책이 허용한 필드만 파생하며 원본 녹화·실행·프로필·소스·빌드에
바인딩한다. 외부 AI는 별도 v2 전송 승인이 필요하고 실제 전송 필드를
`live-issues repair-diagnostics`로 검토할 수 있다. 소프트웨어 검사 173개가 통과했다.
삭제·만료·취소와 수신 저장소의 패키지 경로를 검증했으며 archive 잠금을
AI 응답 동안 잡아두지 않는다. 등록된 세션의 로그는 보존 관리 밖에 별도 파일을
남기지 않고 만료된 메모리 사본도 정리한다.

D2 단계의 wheel은 `d2-diagnostics-package-r1`의
`a18d5532122e25eb8f2c15b051a15a927b260bf881ac8bb99d8bb90c4c111dc9`다.
새 설치의 진단 모듈·CLI·리소스 확인을 마쳤다. D2 이슈·제안 검사는 명시적인 앱·AI
대역을 사용했으며 실제 회사 앱·AI·기기 검증은 아니다.

D3의 한 Mac 일반 이슈 경로를 완료했다. `docs/ISSUE-RECORDING.md`와
`artifacts/product-delivery/d3-issue-flow-r1/acceptance.json`을 먼저 읽는다.
현재 wheel은 `d3-issue-flow-package-r3`의
`a0045ded132512ae5c0128db33863604d2a890408320b7e6d5e4c56914929e68`이다.
코드 107개·공개 리소스 101개를 별도 설치에서 검사했고, 일반 CLI의 실제 Android
녹화 476개 프레임·자동 로그·시작 조건·같은 원본 3회 재현·로컬 제안·진단과 패키지
내보내기가 통과했다. 최종 실행은 test 모듈이나 서버 override를 사용하지 않았다.

일반 영상은 v2 임시 프레임 저장을 기본으로 하며 영속 MP4/손실 증거 뒤에 원본
프레임 바이트를 정리한다. 600초 자동 종료, 네이티브 단조 시각 매핑, 제한된 버퍼,
설정·취득·저장·삭제 도중 중단 복구와 원본 만료 후 정리 증거 보존을 연결했다.
종료는 새 입력을 즉시 막고 이미 받은 프레임 저장을 최대 1초 기다린다. iOS의
새 XCTest 시작은 원래 권한을 재검사하며 최대 90초 기다린다.

별도 실제 10분 Android 녹화는 600,000ms·8,743개 프레임·60개 구간·손실 0개와
모든 프레임의 독립 디코딩을 확인했다. 그 실행의 후속 응답 처리 오류는 보존했고,
10분 원본의 replay까지 완료한 것으로 표시하지 않는다. 실제 iOS Simulator에서는
네이티브 시각·자동 클릭 로그·영상 26개 프레임·정리를 확인했다. 준비 조건 없는
관찰 프로필의 원본은 명시적으로 불완전하며 전체 공유 이슈/수정 수용 검사는 아니다.
앱 로그 시계는 여전히 `app-elapsed-unmapped`다.

설치 UI의 영상·행동 시점 이동·3회 재현 결과·패치·로그아웃 정리를 1440px와 390px에서
확인했다. UI 증거는 테스트용 오류 추적 wrapper가 있는 r2 실행이며, 같은 웹 자산을
가진 최종 r3 일반 CLI 실행과 구분한다. UI 대기 시간 초과도 실패 산출물로 보존했다.
Python 고유 1,259개 ID의 통과 근거는 `parent-covered-tests.json`에 있다. 전체 1,253개
실행 중 두 실패는 검사 실행 경로와 오래된 테스트 예산을 고친 개별 검사로 해결했고,
추가 iOS 시작 검사 6개와 영향 범위를 확인했다. 실패한 전체 실행을 통과로 바꾸지 않는다.

D3의 전용 AVD 네 개는 종료됐고 임시 정의는 보존했다. 전용 iOS Simulator ID 아홉 개의
부재, 테스트 서버·브라우저 종료와 credential 정리를 확인했다. 근거는 D3의
`final-cleanup.json`과 `d3-ios-native-runtime-r1/parent-owned-cleanup.json`이다.
Android 재생 중 확인하지 못한 동작을 포함한 기존 quarantine은 유지했다.

현재 [D4 실행기·복구 계획](../docs/PROTECTED-ADAPTERS-PLAN.md)을 진행 중이다.
서비스 소유 조합, 고정 Android JVM 서명·독립 검사와 영속 소유자/복구, retained
기기 scope와 작업 저널을 연결했다. 서명은 일반 `android-signing build-tools/status/recover`
CLI로 구성·복구할 수 있다. 고정 Android 어댑터의 실제 전용 AVD 원본 대조 후보
3회 재생·원본 복원 근거도 보존했지만 mobile isolation qualification은 발급하지 않았다.

현재 개발 wheel은 `d4-foundation-package-r9/dist/reproof-0.1.0-py3-none-any.whl`이며
SHA-256은 `2d05c0ca3e23dd7b55a972e386d4db050eacdcb422c9c7b51ee77bd5719064fd`다.
코드 139개·공개 리소스 107개를 별도 설치에서 대조했다. r9는 실제 SDK ADB로 scoped
기기 조회·shell v2·APK 전송·helper 직접 연결과 OS 차단을 확인했다. upstream server는
소유한 protocol 대역이며 실제 기기/서버 인증 환경 qualification은 아니다.
r8은 mobile 정의·실제 Unix
인증 관찰·원본 복원과 인증키 정리를 명시적 Android 도구 대역으로 확인했다.
r7 설치본에서는 파일에서 읽은 정의로
실제 iOS 서명·독립 검사·부모 종료·키 없는 CLI 복구를 확인했다. 보호 설정 검사 CLI는
변경되지 않은 입력의 r6 설치 근거를 재사용한다.
앱/IPA provisioning, ad-hoc·자체 인증서 두 architecture와 실제 native 서명·복구는
변경되지 않은 입력의 r5 근거를 재사용한다. 이전 r1 설치본의 실제 Android
서명 도구 빌드·자체 키 서명·독립 검사·부모 종료 후 새 CLI 복구가 통과했고,
134,742,016바이트 예약을 정리 뒤 해제했다. 원본 D3 wheel은 보존한다.

r10 기준 고유 Python 검사 1,514개가 전체 실행에서 통과했다. `tests`를 정식 패키지로 선언하고
fixture import를 통일했으며, 테스트용 VM 작업도 실제 machine lease 계약을 따르게 했다.
원격 polling의 프레임 순번 누락은 손실로 명시해 이후 영상이 멈추지 않게 했고,
반복 재생은 정리 뒤 최신 inventory 보고를 최대 15초 기다린다. 실제 worker 두 개·
coordinator 재시작·같은 원본 3회 재현·패키지·브라우저와 실제 AVFoundation 인코딩/
독립 디코딩도 통과했다. 이는 합성 앱/worker 환경이며 실제 회사·두 Mac 검증은 아니다.
이전 실패 로그는 유지한다. `full-suite-test-ids-r4.json`과 `foundation-progress-r10.json`은
위의 전체 실행·설치 근거다. 이후 native 소유자 변경은 아래 r11을 따른다.

iOS는 제한된 `.app`/IPA 파서와 같은 artifact의 main·extension profile 캡처,
명시적 발급자/anchor에 대한 고정 CMS 검증·entitlement 정책, 안전한 앱 사본 준비와
모든 code object/architecture의 고정 `codesign` 검사기를 연결했다. 별도 설치에서
자체 UIKit 앱 사본의 앱/IPA provisioning과 ad-hoc x86_64·arm64 서명 검사를 실행했다.
원본을 보존하고 임시 재료를 정리했으며 test 모듈이나 checkout 모듈을 사용하지 않았다.
실제 Apple profile·인증서와 회사 앱 수용은 아직 없다.
`docs/IOS-ARTIFACTS.md`, `docs/ANDROID-MOBILE-OPERATIONS.md`,
`docs/ANDROID-SIGNING-RECOVERY.md`가 API와 제한을 설명한다.

사용자의 `승인`으로 Apple 공식 Security 저장소와 AOSP ADB 공개 소스 조회를 진행했다.
범위는 공개 소스이며 코드·로그·키 전송 권한이 아니다. `official-source-r1/findings.md`와
각 source-records JSON을 따른다. 사용자 HOME·기본 인증 파일·Keychain 설정 변경이나
사용자 키 읽기는 승인되지 않았다.

`memory-app-signing-r8`은 명시적 인증서 체인과 고정 로컬 서명 함수를 받는 Apple SPI로
자체 UIKit 앱의 두 architecture를 메모리 키로 서명하고 production inspector에서
확인했다. 서명은 아직 artifact prototype이다. 제품의 `native/ios-code-verifier/main.c`와
`IOSCodeSignatureTools.verifier`·`verifier_sha256`을 추가했으며 인증서 모드에 필수다.
공개 Security API의 네트워크 금지·만료·엄격 검증을 고정하고 로컬 trustd IPC만 허용한다.
Keychain 인증서 검색은 Apple 구현에서 비활성화하며, entitlement 조회의 호환 alias는
검증된 blob을 파싱해 제외한다. 다른 인증서·팀·entitlement와 코드/Info.plist 변조를
거절했고 임시 키를 정리했다. 전체 회귀와 새 설치본 근거를 r10에 연결했다.
검사기 변경은 위의 r2 wheel에 포함되며 서명 생성 자체는 artifact prototype이다.
자체 AIA 시험은 온라인 허용 대조도 요청을 만들지 못해 네트워크 qualification 근거가 아니다.

목표가 `resume 쭉 해줘 그럼`으로 다시 활성화됐다. 뒤이어
[iOS 네이티브 서명 소유자](../docs/IOS-NATIVE-SIGNING-OWNER.md)를 구현했다.
`native/ios-signing-owner`는 producer·phase 잠금을 직접 보유하고 부모 EOF에 종료하며,
결과가 나와도 ACK/실제 종료까지 잠금을 유지한다. 키는 읽기 FD와 비밀번호 pipe로만
받아 메모리에서 사용한다. 실제 부모 `os._exit`, 시작 전 EOF, 정지·조기 ACK·실패와
자체 앱 서명/독립 검사 등 신규 8개 및 관련 기존 28개가 통과했다. 전체 회귀를
다시 실행한 것은 아니다. `foundation-progress-r12.json`과 `next-work-after-r12.json`이
현재 재개 지점이다. 공개 리소스는 106개이며 새 소유자는 기존 r2 wheel 이후 변경이다.
영속 iOS 저널·예산·복구·서비스 조합은 다음 작업으로 남아 있다.
부분 서명 뒤 실패한 경우 완료한 객체 수를 보존하고 실패 결과와 잠금을 유지하는
경로까지 실제 검사했다. 현재 관련 로그는 `ios-artifacts/native-owner-related-r3.log`다.

그 뒤 [iOS 서명 입력·준비·복구](../docs/IOS-SIGNING-RECOVERY.md)를 구현했다.
`ios_signing_inputs.py`는 인증서/권한/profile 정의와 명시적 키 재료를 고정한다.
`ios_signing_operation.py`는 RunStore 예약 뒤 IPA를 준비하고, 기존 root의 정확한
구성·원래 inode·producer/owner 잠금을 확인해 부분 사본을 정리한다. iOS 정리
capability도 RunStore가 정확한 클래스와 살아 있는 잠금을 확인해 한 번만 소비한다.
준비 중 실제 부모 종료, native 소유자가 살아 있을 때 복구 거절, 입력/앱 교체,
늦은 staging callback과 최종 상태 기록 실패를 검증했다. IPA 임시 쓰기는 원래 FD에
묶었으며 caller workspace는 저널에서 정리할 수 있도록 보존한다.
현재 재개 지점은 `foundation-progress-r13.json` / `next-work-after-r13.json`이다.
이 단계는 native 서명 실행·CMS 검증·독립 검사까지 연결한 운영 경로가 아니다.
현재 예약은 준비 단계의 상한이며 native 출력/검사 용량과 프로세스 소유권을 더 연결해야 한다.
기존 r2 wheel도 이 최신 변경을 포함하지 않는다.

이후 [iOS 실행 경로](../docs/IOS-SIGNING-EXECUTION.md)를 연결했다. 고정 검증 도구를
감시하는 `native/ios-process-guardian`, `ios_native_process.py`,
`ios_signing_execution.py`와 구체적인 signer/inspector callback을 사용한다.
프로필 검증 뒤 정확한 준비 앱 digest를 확인해야 키를 열며, 별도 nonce의 독립
검사는 signed IPA를 다시 준비한다. native 결과/프로세스 그룹을 수집하고 사본을
정리한 후에만 supervisor 증거를 만든다. 실제 부모 종료와 새 Python 프로세스의
복구, 실제 암호 연산이 포함된 서비스 factory 검사도 통과했다. VM은 그 검사에서
명시적인 대역이다. 앱의 자체 리소스 서명 규칙은 키 사용 전과 native 단계에서 거절한다.
현재 단계는 r14로 검증 근거를 수집 중이며, 공개 CLI·실제 환경 수용은 남아 있다.
`full-suite-parent-r5.log`의 실패 둘은 전송 완료/서버 pin 해제 사이의 검사 경합과
검사 중 파일 수정에 따른 G7 source-stability 실패였다. 전송 검사는 실제 handler
종료를 기다리게 했고, 소스를 고정한 전체 검사를 다시 수행한다. 실패 로그는 보존한다.
`full-suite-parent-r6.log`에서는 G9 브라우저 작업이 `build_unavailable`로 막혔다.
실제 실패의 상태·이유 코드만 보존해 재현했고, UI availability 조회가 실행용 lease를
잡아 작업 시작과 충돌함을 확인했다. UI 관찰은 canonical authority/ledger를 읽기만
하게 바꾸고 실제 실행의 lease는 유지했다. VM·서명·기기의 동시 관찰 검사와 브라우저
5회 반복이 통과했다. 진단 로그는 `ios-artifacts/availability-lease-*`,
`availability-all-scopes-r1.log`, `g9-browser-repeat-r3.log`에 있다.
최종 `full-suite-parent-r7.log`에서 1,558개가 전체 통과했고,
`full-suite-test-ids-r7.json`으로 고유 ID를 대조했다. 새 r4 wheel의 설치본에서 테스트
모듈이나 checkout import 없이 고정 callback의 실제 CMS·서명·별도 nonce의 독립
검사·정리·예약 해제까지 확인했다. native 호출은 6개였고 자체 키와 임시 사본을
정리했다. 현재 코드·검증 근거는 `foundation-progress-r14.json`이며 재개 순서는
`next-work-after-r14.json` / `ios-cli-next-r1.md`다. 공개 iOS CLI, 실제 보호 환경과
회사·두 Mac 수용은 남아 있으므로 전체 목표를 완료로 표시하지 않는다.

이후 `ios-signing build-tools/status/recover`와 키·프로필·도구를 다시 읽지 않는
복구 전용 구성을 연결했다. 고정 리소스를 등록된 clang·SDK로 오프라인 빌드하고
manifest/source/binary 해시를 검증한다. macOS clang의 하드링크 거절, 잘못된 저널
버전 타입과 누락된 출력 부모의 오류 분류를 수정했다. 실제 빌드·변조 거절·취소·
시간 제한에서 자식 프로세스와 임시 사본을 정리하며 기존 출력은 보존한다.
복구 전용 객체는 새 준비·서명·검사를 거절하고, 마지막 ledger 기록 실패도 재시도한다.

실제 native 부모 종료 뒤 새 CLI가 원래 저널을 복구했다. 자체 키·profile·도구의
읽기를 OS에서 차단한 조건도 통과했다. 처음에는 공유 테스트 키를 이름 변경해
그 뒤 검사가 정상적으로 키 변경을 거절했다. 해당 검사는 파일을 바꾸지 않는
OS 차단 방식으로 격리했으며 제품의 키 변경 감지 규칙은 유지했다. 최초 구문 오류와
실패 로그도 보존한다. `ios-artifacts/ios-cli-native-green-r3.log`의 native 10개,
`ios-cli-recovery-final-r1.log`의 복구 7개와 도구 CLI 검사가 근거다.

새 r5 설치본은 checkout/test import 없이 공개 CLI 빌드·실제 서명·독립 검사와
부모 종료·키 읽기 차단·새 CLI 복구·반복 복구·예약 0을 확인했다.
`d4-foundation-package-r5/ios-signing-cli/result.json`에 근거를 보존했다.
이 추가 작업의 전체 회귀와 현재 소스 근거는 `foundation-progress-r15.json`,
다음 작업은 `next-work-after-r15.json`에 기록한다. D4/D5 전체 완료는 아니다.

이후 [보호 서비스 공개 설정 검사](../docs/PROTECTED-SERVICE-CONFIGURATION.md)를 추가했다.
`repair_configuration.py`는 고정 route·정책·계획과 공개 파일 참조, 서로 겹치지 않는
작업 root를 검증한다. `originalBuildId`를 명시하여 같은 앱의 다른 등록 빌드를 원본으로
채택하지 않는다. `validate_runtime()`는 실제 서비스 등록·runner·정책·기기 배정과
선택 원본을 확인한다. 이 결과는 실행/qualification 권한이 아니다.
`protected-service check-config --config ... --issue-config ...`는 두 공개 설정을
검사하며 참조된 VM·도구·키·관찰자 파일을 열지 않는다.

신규 검사는 `tests/test_protected_service_configuration.py`에 둔다. 최종 대조에서
기존 `tests/test_repair_configuration.py`와 파일명이 겹친 것을 발견해, 보존 사본에서
r15와 바이트 단위로 같은 원본을 복원했다. SHA는 `protected-config-test-restoration-r1.json`에
기록했다. 신규 11개와 복원된 기존 검사·서비스 조합·프로토콜·Android/iOS CLI를 포함한
재검증은 `protected-configuration-related-r2.log`를 따른다. r6 설치본의 별도 CLI는
test/checkout import 없이 정상·불일치·인자 비노출·참조 미접근을 확인했다.
이번에는 전체 검사를 다시 실행하지 않았으며 r15의 전체 1,574개 근거와 구분한다.
초기 테스트 구문/API 호출 오류와 등록된 다른 빌드가 원본으로 허용된 실패,
설치 검사에서 `/var` 별칭 경로를 전달한 실패는 보존했다. 경로 검사를 약화하지 않고
검사 입력의 실제 경로를 사용했다. 현재 근거는 `foundation-progress-r16.json`이다.
다음은 `next-work-after-r16.json`을 따른다. 실제 참조 로딩·고정 실행기 초기화와
`live-serve` 시작 조합, 고정 독립 관찰자와 mobile qualification은 아직 남아 있다.

이후 [빌드·서명 입력 로딩](../docs/PROTECTED-SIGNING-INPUTS.md)을 구현했다.
`protected_tool_inputs.py`는 preflight 이후 실제 VM 번들과 Android/iOS 서명 도구
manifest를 읽고 환경·recipe·출력·정리 정책을 대조한다. `protected_signing_inputs.py`는
해시·크기가 고정된 인증서/profile 참조를 기존 Android/iOS 입력 타입으로 변환한다.
개인 키·비밀번호를 읽지 않으며 실제 암호 검증은 고정 native 실행 경로가 수행한다.
`protected_build_signing_inputs.py`는 둘을 묶고 서비스 전체 profile 보존을 64 MiB로
제한한다. 파일 읽기 뒤에도 실제 원본·기기·등록을 다시 확인하며 저널/실행기를 만들지 않는다.

신규 17개와 기존 입력·서비스·실제 native 경로를 포함한 92개 검사가
`protected-inputs-related-r1.log`에서 통과했다. 실제 Android JVM 도구와 iOS 도구를
읽었고, 파일에서 로딩한 iOS 정의로 실제 자체 앱 서명과 독립 검사를 수행했다.
r7 새 설치본도 test/checkout import 없이 로딩한 정의로 native 호출 6개·서명·검사·
정리·예약 해제, 부모 종료와 읽기 차단 아래 새 CLI 복구를 통과했다.
`d4-foundation-package-r7/ios-loaded-signing-inputs/result.json`이 근거다.
VM 입력 시험은 합성 파일이며 실제 VM boot 근거가 아니다. 전체 검사는 재실행하지
않았고 기존 전체 1,574개 및 r16 근거와 이번 관련 검사를 구분한다.
현재 근거는 `foundation-progress-r17.json`, 다음 작업은 `next-work-after-r17.json`이다.
다음은 Android mobile 입력 본문과 고정 독립 관찰자, 실제 초기화·`live-serve` 조합이다.
`protected-mobile-validation-inputs-next-r1.md`를 따른다. D4/D5 전체 목표는 미완료다.

이후 [Android mobile 입력](../docs/PROTECTED-MOBILE-INPUTS.md)과
[로컬 독립 관찰자](../docs/LOCAL-VALIDATION-PROTOCOL.md)를 구현했다.
`protected_mobile_inputs.py`는 실제 runtime의 profile·원본 APK·helper·도구·fixture
payload digest를 대조하며, 원격 기기를 로컬로 채택하지 않는다. 입력 단계는 ADB를
실행하지 않고 native qualification도 발급하지 않는다.

`protected_validation.py`는 소유한 Unix socket/peer UID, 프로젝트/provider별 별도 인증키,
방향이 분리된 HMAC과 새 교환 nonce를 사용한다. 실제 설치 context와 동일한 scope를
응답 뒤에도 확인한다. 연결 실패·잘못된 서명/바인딩·불명확한 정리는 quarantine이다.
`protected_validation_inputs.py`가 고정 관찰자를 등록하며 mobile factory는 내부에서
만든 실제 어댑터에 연결하고 인증키 소유권/종료를 관리한다. v1은 운영자가 제공하는
독립 읽기 전용 서비스의 `external-observation`이며 `trusted-runner`는 지원하지 않는다.

`full-suite-parent-r9.log`에서 고유 1,619개가 전체 통과했고, 실행 당시 코드·테스트·문서
462개가 변하지 않았음을 확인했다. `full-suite-test-ids-r9.json`이 근거다. 이 handoff
갱신은 검사 후 문서 기록 변경이다. 별도 r8 설치에서 test/checkout import 없이 mobile
정의·실제 Unix/HMAC 관찰·원본 복원·인증키 사본 종료를 확인했다. Android transport와
APK는 명시적 대역이며 실제 기기/VM qualification이 아니다. VM/signature 대역을 쓴
별도 factory 검사는 관찰 뒤 후보 3회 재생·정리·예약 해제까지 통과했다.

현재 근거는 `foundation-progress-r18.json`, 다음 작업은 `next-work-after-r18.json`이다.
`android-guardian-next-r1.md`에 따라 격리 ADB client 연결과 native 소유권/복구를 이어간다.
기본 ADB 인증·실제 mobile qualification·전체 `live-serve`·회사·두 Mac 수용은 남아 있다.

이후 [명시적인 ADB 연결](../docs/SCOPED-ADB-TRANSPORT.md)을 구현했다.
`adb_endpoint.py`는 선택한 사용자 소유 Unix endpoint 앞에서 서버 version 41과
transport를 검사하며 daemon 제어·다른 기기 선택을 전달하지 않는다. 실제 SDK client는
파일·network·fork·Mach 제한 아래 실행한다. dyld에는 시스템 cache 상위 디렉터리의
명시적 읽기만 추가했다. 사용자 파일 허용을 넓히거나 HOME을 바꾸지 않았다.

`PinnedAdbDevice`와 `AndroidLiveProvider`는 선택 endpoint를 사용할 수 있다. helper는
ADB의 `tcp:8766` service로 직접 연결하여 host 포트 forward를 만들지 않는다.
endpoint digest는 실제 operation intent에 바인딩되고, endpoint 없는 구성으로
해당 작업을 복구하는 시도는 거절한다. 정상/실패/늦은 gateway 정리를 검사했다.

`full-suite-parent-r10.log`는 생성자를 우회하는 기존 테스트 대역에 새 선택 필드가
없어 9개가 실패했다. `adb_endpoint=None`과 setup 실패 시 정리를 명시한 뒤,
`full-suite-parent-r11.log`에서 고유 1,633개가 전체 통과했다. 실행 당시 입력 465개도
고정돼 있었다. 실패·dyld 진단·실제 OS positive/negative control은 보존한다.
endpoint 저널 시험은 unique synthetic serial을 사용한다. 생성자 자체가 아니라
operation intent/recovery에서 구성 바인딩을 검사한다.

r9 설치본은 test/checkout import 없이 실제 캐시 ADB 36.0.0과 소유한 server 대역으로
같은 경로를 확인했다. OS probe는 작업 밖 읽기/쓰기·다른 TCP·추가 spawn을 차단했고,
원래 SDK 프로세스·gateway·임시 사본 종료를 확인했다. 현재 근거는
`foundation-progress-r19.json`, 다음은 `next-work-after-r19.json`이다.
`android-native-owner-next-r2.md`에 따라 원래 producer/기기 잠금을 유지하는 native
guardian과 실제 device/backend 복구를 연결한다. Python 부모 종료나 client 종료만으로
기기 정리를 주장하지 않는다. 전체 D4/D5와 실제 회사·VM·기기·두 Mac 수용은 미완료다.

이후 원래 producer/기기 잠금 FD 전달과 Android C guardian의 독립 구현을 추가했다.
`Lease`와 Android phase는 부모 FD를 닫을 때 명시적으로 잠금을 풀지 않는다.
`borrow_native_descriptors`는 실제 phase·기기 generation에 묶인 원래 FD만 전달하며,
현재 capability는 같은 스레드에서만 유효하다. guardian은 부모 EOF 시 SDK 자식을
종료·회수하고, 정상 결과는 부모 ACK까지 잠금을 유지한다. 실제 Python 부모 종료,
SIGSTOP, 실제 캐시 SDK가 실행 중인 guardian을 SIGKILL한 뒤 잠금 유지를 확인했다.

r20 checkpoint는 `foundation-progress-r20.json`, 당시 다음 작업은 `next-work-after-r20.json`이다.
`android-native-guardian-green-r3.log`의 6개와 FD 전달 4개를 포함한 관련 회귀검사
75개가 `android-native-ownership-regressions-r1.log`에서 통과했다. r19 전체 1,633개는
그 당시 변경 이전 결과다. 새 C 소스 두 개는 배포 리소스 목록에 추가했고, r20 당시의
보존 wheel r9에는 이 구현이 포함되지 않았다.
임시 설치본의 리소스 무결성·native 소스 export·비공개 파일 제외 검사 3개도
`android-native-distribution-r1.log`에서 통과했다. 공개 리소스는 109개다.

이후 `android_native_calls.py`와 `android_native_process.py`를 구현해 실제
`PinnedAdbDevice`의 SDK 명령과 instrumentation을 guardian에 연결했다. 공개 입력의
선택 `nativeGuardian`은 명시적 endpoint와 영속 operation store를 요구하고,
definition digest를 intent에 바인딩한다. 어댑터가 실제 phase에서 발급한 dispatcher만
callback 스레드에 원래 FD 사용을 허용한다. 직접 접근·복사·종료된 phase는 거절한다.

endpoint가 있는 새 작업은 native 공간 17,522,688바이트를 추가 예약한다. 일반 명령과
instrumentation의 고정 slot은 원래 guardian·gateway 수집 후 ACK와 회수를 확인한
뒤 재사용한다. 요청과 기기 generation이 바인딩되고, 중단·부분 쓰기는
`native-call-unresolved`로 남는다. 미완료 slot이 있으면 APK 사본을 삭제하지 않는다.
guardian 결과의 `deviceCleanupConfirmed`는 항상 false이며 종료 의도 JSON은
기기/backend 정리나 비용 해제 권한이 아니다.

관련 검사 84개 통과 후 추가한 종료 경합 검사에서, SDK가 회수됐지만 취소 표시가
빠지는 문제를 재현했다. 최종 관측 시점에 취소를 재검사해 실제 SDK 검사 9개가
통과했다. 별도 probe에서 helper 요청이 완료된 phase 뒤에도 전달되는 누락을
재현했다. helper에도 dispatcher의 phase·취소 검사를 적용했고 진행 중인 helper가
있으면 phase 완료를 거절한다. 새 native/helper 11개와 fixture 7개가 통과했다.

`full-suite-parent-r12.log`는 1,661개 중 기존 fixture 만료 검사 1개가 실패했다.
첫 응답 전에도 2ms 보존 기간이 지나갈 수 있었다. 초기 완료를 먼저 확인한 뒤
coordinator 시간을 실제 보존 기한 뒤로 이동하는 테스트로 바꿨으며 fixture 런타임은
수정하지 않았다. r12 실행 중 입력 473개는 고정돼 있었다. 실패·helper probe와 중간
wheel r10은 보존하고 최종 소스의 전체 검사·설치를 다시 진행한다.
r21 checkpoint의 최종 검사·배포 근거는
`foundation-progress-r21.json`, 다음은 `next-work-after-r21.json`을 따른다.
최신 소스와 r19/r20 당시의 검사·wheel 결과를 혼동하지 않는다.

현재 r22에서는 `DeviceAuthority.borrow_native_recovery_lease()`와
`AndroidOperationStore.native_recovery()`를 추가했다. 이전 소유권 snapshot·같은
프로젝트의 살아 있는 grant를 확인하며, 일반 명령 권한과 비용 해제를 열지 않고 원래
잠금 FD만 전달한다. 재시작한 호스트에서도 quarantine은 유지하고, 빌린 기기 FD가
있는 동안 reconciliation으로 generation을 바꾸지 않는다. 복구 context의 종료도
명시적인 `LOCK_UN` 없이 close만 수행해 자식이 보유한 원래 잠금을 유지한다.

`android-recovery-scope-regressions-r2.log`에서 고유 103개가 통과했다. 복구 전용 신규
검사 9개는 stale/copy/만료/다른 프로젝트/close와 실제 자식의 잠금 유지를 확인한다.
테스트용 ADB 서버가 POST 본문을 읽기 전에 응답·종료하던 문제도 분할 전송으로
재현해 고쳤다. r1의 실패와 별도 재확인 결과는 보존한다. 테스트 대역 변경이며
이번 작업에서 운영 `adb_endpoint.py`는 바꾸지 않았다.

r22 근거는 `foundation-progress-r22.json`, 당시 다음은 `next-work-after-r22.json`이다.
최신 보존 wheel r11과 전체 1,663개 결과는 r21이며 이번 복구 토큰 변경을 포함하지
않는다. 다음은 `PinnedAdbDevice.apk_identity()`의 고정 APK 검사기다. 현재 `_run()`의
해당 분기는 `_ProcessOwner.run(..., pass_fds=())`이고 guardian 밖에 있다. 이를
원래 잠금과 고정 native 감시에 연결하기 전에는 복구 잠금 획득을 모든 호스트 도구의
종료 증거로 쓰지 않는다. 실제 기기/backend 복구·예약 해제·CLI는 여전히 후속 작업이다.

현재 r23은 그 APK 검사기 경로도 guardian에 연결했다. `android_inspector.py`가
정확한 `dump badging`·등록된 staging APK·파일 해시·읽기 전용 sandbox를 구성한다.
SDK 보조 `lib64/libc++.dylib`가 있으면 구성 생성 시 측정·고정하고 operation/input
digest에도 바인딩한다. guardian의 기존 SDK 입력 18개 필드는 유지하고 inspector의
25개 필드를 별도로 엄격히 검사한다. 기존 guardian 바이너리는 재빌드·재등록해야 하며
실패 시 기존 일반 프로세스 소유자로 우회하지 않는다.

`android-inspector-regressions-r1.log`의 고유 102개가 통과했다. 실제 AAPT 검사와
OS 대조 검사, 검사기 명령/입력 변조 거절, 기존 SDK/helper·복구 경로를 포함한다.
`actual-aapt-owner-r1/result.json`은 실제 AAPT가 APK를 연 상태에서 SIGSTOP 후
guardian을 SIGKILL해도 두 원래 잠금을 보유함을 확인했다. `parent-exit-result.json`은
실제 Python 부모 종료 후 guardian과 AAPT가 회수되고 잠금이 풀렸음을 확인한다.
큰 소유 manifest를 사용했으며 실기기에 설치하지 않았다. 마지막 기록은
`foundation-progress-r23.json`, 다음은 `next-work-after-r23.json`을 따른다.
전체 1,663개 검사는 r21의 기록이며 현재 변경 위에서 다시 실행한 것으로 읽지 않는다.

사용자가 진행을 계속하면서 지워도 되는 빌드 산출물을 정리하도록 승인했다.
`artifacts/product-delivery/cleanup-r1/result.json`에 따라 검증한 캐시·이전 패키지
사본 744개 디렉터리, 49,262개 파일, 할당 용량 10,863,194,112바이트를 제거했다.
남긴 파일 55,845개의 메타데이터와 소스·wheel 해시는 일치했다. 최신 r12 설치본의
리소스 109개도 정리 후 다시 확인했다. APK·앱 Products·로그·영상·오프라인 Gradle
의존성·최신 r12는 보존했다. 이전 r1–r11의 `build-input`/`installation`은 wheel과
해시가 일치함을 확인하고 삭제했으므로, 과거 경로의 부재를 실패나 미완료로 재해석하지 않는다.
필요한 과거 소스는 보존한 wheel과 `source-inputs.json`을 기준으로 복원한다.

r24는 `nativeToolOwnershipVersion: 2`도 원래 operation과 입력 snapshot에 바인딩한다.
과거 SDK 전용/일반 APK 검사기 실행을 현재 전체 소유권 계약으로 재해석하지 않으며,
표시 없는 과거 작업은 격리와 예약을 유지한다. 버전 표시는 cleanup 권한이 아니다.
`android-recovery-provenance-green-r1.log`에서 42개가 통과했다. 현재 근거는
`foundation-progress-r24.json`, 다음은 `next-work-after-r24.json`이다. 이 소스 변경은
보존한 r12 wheel 이후이며, 실제 기기/fixture 복구와 예약 해제는 계속 구현해야 한다.

현재 r25에서는 `android_recovery.py`의 고정 기기 복구 실행기를 연결했다. 원래 잠금과
계약 버전 2, 준비된 staging을 요구한다. `recovery.json`을 먼저 기록하고 bounded host
scratch를 회수한 뒤 helper/앱 종료, 프로세스 부재, 원본 identity·설치·설치 hash,
데이터 초기화·최종 프로세스 부재를 9단계로 검사한다. 일회성 recovery dispatch는
그 단계의 정확한 SDK 명령/원본 검사에만 쓸 수 있다. 실패·취소·다시 생긴 프로세스·
경로 traversal은 거절하며 32회 제한 초과는 마지막 유효 기록을 보존한다.

`android-device-recovery-regressions-r2.log`에서 고유 89개가 통과했다. 실제 SDK와 C
guardian을 사용했지만 기기 프로토콜 서버와 APK identity 도구는 명시적인 자체 대역이다.
기기 자체 수용이나 환경 qualification을 주장하지 않는다. `device-restored` 뒤에도
기기 quarantine과 RunStore 예약은 유지한다. fixture 정리·새 helper reconciliation·
최종 cleanup capability·staging 삭제 중단 처리·예약 해제·CLI는 아직 남아 있다.

fixture 준비 요청 전에 allocation ID/generation을 이슈 파일에 기록하도록 바꿨다.
재시작 로더가 `issue_` 접두사만 읽어 `mobile_...` 기록을 잃던 문제도 고쳤다.
후속 fixture 복구는 이 원래 연계를 사용해야 한다. reserve와 이슈 기록 사이에 중단된
미전송 로컬 할당의 처리와 과거 연계가 없는 작업은 별도 검증이 필요하다.
현재 근거는 `foundation-progress-r25.json`, 다음은 `next-work-after-r25.json`이다.
새 패키지 사본은 만들지 않았으며 최신 보존 r12 wheel은 이 실행기 이전이다.

현재 r26은 `recover_android_resources()`로 기기 복구와 원래 fixture 정리를 연결했다.
context/replay 번호에서 원래 mobile 이슈 ID를 구하고 프로젝트·기기·앱·빌드·할당 세대를
검증한다. `FixtureCoordinator.recover_cleanup()`은 원래 준비 요청과 payload, 소유자·
기기·계획을 대조하고 격리된 할당만 재조정·정리한다. 형식 2의 `allocation_history`에
재할당 전의 결합 정보를 보존하여 이미 정리된 과거 할당을 확인할 때 새 세대를 변경하지 않는다.
형식 1에서 원래 미완료 작업을 유지한 채 이전하며, 새 형식을 오래된 설치본에서 열면 거절한다.

`android-fixture-recovery-regressions-r2.log`의 77개와 `fixture-history-boundaries-r1.log`의
11개, 총 고유 88개가 통과했다. 원격 cleanup 실패·다른 이슈 기기·다른 과거 소유자/기기·
세대 재사용·형식 이전을 검사했다. 초기 테스트 setup 오류로 남은 프로세스는 해당 실행의
로그를 실제로 열고 있는 PID만 확인해 종료했고, 테스트 setup 이전에 cleanup을 등록하도록 고쳤다.
이는 테스트 대역 정리이며 실제 기기 검증이나 최종 권한 해제가 아니다.
첫 회귀 실행에서 이전 형식 생성용 테스트 SQLite 연결의 미수집 경고를 발견했고,
명시적인 close를 추가한 r2 재실행에는 ResourceWarning이 없었다.

현재 근거는 `foundation-progress-r26.json`, 다음은 `next-work-after-r26.json`이다.
준비 요청이 전송되기 전의 로컬 할당과 이슈 기록 사이 중단, 참조 없는 과거 할당은 보수적으로
남아 있다. 이 연결을 보완한 뒤 새 helper handshake/reconciliation과 살아 있는 최종 cleanup
capability를 만들고, staging·native slot·RunStore 예약을 해제한다. 새 패키지는 만들지 않았다.

현재 r27은 reserve/이슈 참조 기록 사이의 중단을 연결했다. 예약 ID는 이슈·fixture에서
결정하고 최초 이슈 의향에 기록한다. `fixtureReservationVersion: 1`도 원래 operation과
입력 snapshot에 바인딩하므로 이슈 JSON의 표시만 추가해 과거 작업을 미전송으로
재해석하지 않는다. 저장되지 않은 참조는 계획·소유자·기기·예약 ID로 찾고, 원격 operation이
없는 로컬 할당만 해제한다. 아직 없었던 예약은 generation 0 봉인 이력으로 남겨 늦은
reserve를 막는다. 현재/과거 ID 재사용과 다른 소유자의 봉인 채택을 거절하며 이력 수는 제한한다.

`fixture-reservation-recovery-regressions-r1.log`, `fixture-reservation-contract-green-r1.log`,
`fixture-reservation-reopen-r1.log`의 현재 관련 검사를 대조했다. 새 코드·문서·검증 집계는
`foundation-progress-r27.json`, 다음 작업은 `next-work-after-r27.json`을 따른다.
원격 fixture 전송 없는 해제, 봉인 뒤 재개, 오래된 연계 거절, 작업 계약·입력 바인딩을 확인했다.
기기 quarantine과 RunStore 예약은 아직 유지한다. 다음은 새 helper handshake와
DeviceAuthority reconciliation을 실제 관측에 연결하고 살아 있는 최종 cleanup capability로
staging/native slot/예약을 해제하는 것이다. 새 빌드·설치 사본은 만들지 않았다.

현재 r28은 `android_recovery_helper.py`로 기기/fixture 복구 뒤 새 helper 관측·회수를
연결했다. helper APK 설치/hash 확인 → 새 host/helper/provider 식별자와 임의 인증 token의
설정 전송 → 원래 FD를 보유한 native instrumentation → 인증된 status/빈 입력 상태 확인 →
helper 종료·데이터 초기화·프로세스 부재를 검사한다. input digest와 slot도 recovery dispatch에
바인딩한다. 별도 Python worker 권한을 풀지 않고, native owner가 살아 있는 동안 같은 owner
스레드에서 고정 helper probe를 수행한다. token은 공개 기록에 넣지 않는다.

`android-recovery-helper-regressions-r1.log`의 고유 88개가 통과했다. 실제 SDK/C guardian과
자체 동시 연결 protocol server를 사용했고 실기기는 아니다. 잘못된 helper·활성 포인터·
미지원 clock·설정 본문 변조·취소를 검사했다. 테스트 서버가 SDK stdin 종료 프레임을 읽기 전에
닫히던 문제를 재현하고 해당 fixture의 framing을 수정했다. 실패 기록도 보존한다.
r28 근거는 `foundation-progress-r28.json`과 당시의 `next-work-after-r28.json`이다.
새 helper 검증/회수는 연결했지만 DeviceAuthority.reconcile 및 최종 cleanup capability는
아직 적용하지 않았다. 특히 원래 uncertain 작업의 실행 여부를 추측해 succeeded/rejected로
바꾸지 말고, 기존 receipt를 보존하는 복구 처리 의미부터 명시해야 한다. 최신 wheel은 r12다.

r29는 원래 실행 결과가 불확실한 작업에 `recovered` disposition/status를 추가했다.
복원 결과는 reconciliation disposition에 저장하고 원래 operation의 result digest·provider·
receipt를 보존한다. queued 작업은 계속 `not-dispatched`만 허용하며 provider가 `recovered`를
보고할 수 없다. 늦은 성공/거절/unknown 응답은 이력으로만 남고 새 세대를 바꾸지 않는다.
HostAuthority 저널 format/reader/writer는 2이며 알려진 v1만 배타적 트랜잭션으로 이전한다.
기존 작업·receipt·reconciliation·watermark와 FK를 보존하고, 이전 중 프로세스 종료와
실패 시 v1 복원을 검사했다. 추가 인덱스 등 다른 스키마는 제거하지 않고 거절한다.

`authority-recovery-regressions-r3.log` 105개와 필수 `authority-recovery-g1a-r1.log` 49개,
중복 제외 117개가 현재 코드에서 통과했다. 최신 통과 실행에 ResourceWarning은 없다.
`authority-recovery-regressions-r2.log`에서 예약 생성 전 복구 사례가 한 번 실패했고,
`authority-recovery-diagnosis-r1.log`의 단독 3회와 r3 묶음에서는 재현되지 않았다.
원인은 미확정이며 테스트 실패에 복구 기록·소요 시간을 추가했다. 고쳤다고 표현하지 않는다.
r29 근거는 `foundation-progress-r29.json`과 당시의 `next-work-after-r29.json`이다.
Android 관측에서 실제 reconciliation으로 전환하는 경로, 그동안의 원래 잠금 유지,
staging 삭제·RunStore 예약 해제 capability와 중단 복구·CLI는 아직 남아 있다.
실기기/회사/두 Mac acceptance 및 최신 변경의 wheel 설치 검증도 남아 있다. r12 wheel과
10.1 GiB 정리 결과는 보존했고 새 빌드·설치 사본은 만들지 않았다.

r30은 `AndroidOperationStore.finalize_recovery()`를 연결했다. 고정 기기/fixture/helper
복구 관측을 직접 실행하고, 원래 producer/기기 open-file description을 계속 보유하면서
새 권한 세대를 `recovery-cleanup-pending`으로 전환한다. 이 상태는 일반 dispatch, 추가
reconciliation, revoke/clock 변경이나 handle close로 풀리지 않는다. 정확한 staged APK를
삭제한 뒤 살아 있는 일회성 `AndroidCleanupCapability`를 RunStore가 소비한다. 예약을 0으로
만들고 작업을 failed/cancelled로 종결한 뒤 기기 저널과 lease를 해제한다.

`android_recovery_finalization.py`의 finalization.json은 원래 native binding 및 실제 authority
reconciliation ID/fingerprint와 연결된 진행 기록이다. 저장된 JSON을 실행 권한으로 받지 않는다.
권한 commit 뒤 private 기록 갱신 실패, 일부 APK 삭제, 예약 commit 뒤 중단을 재개하며 새
HostAuthority에서도 기기 명령을 반복하지 않는다. 이미 다음 세대가 취득했으면 그 소유자를
건드리지 않는다. 미확정 작업/대기 작업과 늦은 응답, 잠금 유지, 토큰 복제·재사용 거절을 검사했다.
native recovery descriptor 발급 전에 원래 provider 응답도 원자적으로 fence한다. 경합 응답이
복구 도중 owned 상태를 되살리는 결함을 테스트로 재현한 뒤 수정했다.

저널 format/reader/writer는 3이다. 알려진 v1/v2만 이전하고 v2의 recovered 이력도 보존한다.
새 정리 보류 의미를 모르는 예전 writer의 재진입을 막는다. 테이블 구조는 v2와 같다.
등록된 `D4_ANDROID_RECOVERY` 소프트웨어 검증 134개와 G1A 53개, 중복 제외 157개가 통과했다.
근거는 `android-finalization-d4_android_recovery-r3.log`, `android-finalization-g1a-r2.log`다.
실제 cached SDK/C guardian과 자체 protocol server를 썼다. 새 HostAuthority 재시작과 I/O 중단
주입을 검사했으며 실제 Android finalizer 프로세스를 죽이는 실기기 검증으로 표현하지 않는다.

r1 묶음의 한 native 복구 실패는 원인이 미확정이다. r2의 여섯 실패는 주입한 I/O 오류를
감싸는 기존 recovery scope의 오류 코드에 대한 테스트 기대값을 바로잡았고 r3은 통과했다.
실패 로그를 보존한다. r30 근거는 `foundation-progress-r30.json`과 당시 `next-work-after-r30.json`이다.
다음은 이 finalization 진입 전에 기존 정상 cleanup이 이미 staged APK를 지운 작업의 복구다.
그 경우 현재 helper/device 복구의 prepared-stage 요구에 걸린다. 기기에서 원본/helper 설치
바인딩을 새로 확인하거나 원래 APK를 다시 쓸 수 있는 명시적 소유권 경로를 구현해야 하며,
과거 cleanup JSON만으로 해제하지 않는다. 이후 운영 CLI, 실제 qualification 및 남은 D4/D5를
이어간다. 전체/실기기/회사/두 Mac 완료나 최신 wheel 설치 검증을 선언하지 않는다. r12와
10.1 GiB 정리 근거를 보존했고 이번에도 새 package build/installation 사본은 만들지 않았다.

r31은 기존 정상 cleanup이 staged APK의 일부/전부를 삭제한 작업의 복구를 연결했다.
유효한 원래 discard 기록·inode·내용 연계가 있는 경우 `installed-original` 모드에서 기기의
원본 패키지 경로/해시와 helper 해시를 새로 확인한다. 일치하면 원본 데이터 초기화 → 새
helper 인증/빈 입력 확인·종료 → r30 finalization으로 이어간다. 이 경로는 APK 재설치를
했다고 기록하지 않는다. recovery.json v2는 `staged-apks`의 9단계와 `installed-original`의
7단계를 구분하고, 기존 v1 기록도 읽는다. 모드에 필요한 단계가 빠진 완료 기록은 거절한다.

원본/helper가 없거나 해시가 다르면 일반 정리로 승인하지 않고 격리와 예약을 유지한다.
discard 의향 없이 파일만 사라진 경우도 거절한다. 정상 cleanup의 실제 `discard_staged()`로
전체/부분 삭제 상태를 만든 후 SDK/native guardian과 자체 프로토콜 서버로 검증했다.
`android-discarded-recovery-gate-r1.log`의 등록된 D4 142개가 통과했고 변경되지 않은 G1A 53개를
재사용하면 고유 165개다. 새 실행에는 ResourceWarning/skip이 없다. 실제 물리 기기 근거가 아니다.

간헐 실패의 위치는 좁혔지만 원인은 미확정이다. `android-discarded-recovery-diagnosis-r1.log`는
새 helper status가 확인된 뒤 중첩 stop-helper SDK 호출이 약 14초 동안 반환되지 않고 제한
시간에 끊긴 상태(returnCode 75, interrupted)를 보존한다. `android-stop-diagnosis-r1.log` 6회,
r2 8회, r3 18회 관측에서는 재현되지 않았고 OS stack sample은 수집되지 않았다. 타이밍을 바꾼
실험 통과를 해결 근거로 쓰지 않는다. 마지막 필수 142개 묶음은 별도 OS 샘플 관측 프로세스 없이 통과했다.

r31 근거는 `foundation-progress-r31.json`과 당시 `next-work-after-r31.json`이다. 당시 다음은 APK가
이미 삭제됐고 설치된 원본/helper도 없거나 다른 경우의 **명시적으로 바인딩된 복구용 APK
준비·설치 경로**다. 원래 config의 immutable APK는 남아 있을 수 있으므로 그 파일에서 보호된
복구 사본을 만들고 검사·설치·정리할 소유권/예산/중단 계약을 구현한다. 원래 intent의 inode를
새 inode로 몰래 바꾸거나, 없는 설치본을 정상으로 간주하지 않는다. 이어서 운영 CLI와 서비스
시작 조합을 연결한다. 관련 진입점은 `protected_mobile_inputs.py`의 `load_protected_mobile_inputs`,
`repair_configuration.py`의 runtime binding과 기존 `repair_signing_cli.py`다. 전체 D4/D5·실기기·
회사·두 Mac 검증과 새 wheel 설치 검증은 남아 있다. r12 wheel과 10.1 GiB 정리 근거를 보존했다.

r32는 설치된 원본/helper까지 없거나 유효한 hash가 다른 경우에, 원래 설정의 APK로
복구 사본을 준비·검사·설치·삭제하는 경로를 연결했다. `android_recovery_materials.py`가
기존 staging의 사라진 original/helper 슬롯만 다시 만들며 남은 원래 파일은 재사용한다.
원래 intent·inode 기록과 RunStore 예약을 바꾸지 않고, 별도 `recovery-materials.json`에
새 파일 identity와 원래 파일 목록 digest, preparing/prepared/discarding/discarded를 바인딩한다.
payload는 원래 세 APK 예약 상한을 넘지 않는다. 새 파일은 identity commit 전에는 비어 있어야
하며 등록된 부분 사본만 같은 inode에 다시 복사한다. 등록 전 비어 있지 않은 파일은 거절한다.

원본/helper 누락·불일치를 실제로 관측한 경우에만 한 번 자동 재시도한다. 사본 경로의
recovery.json v3 `recovery-apks`는 준비 기록 digest를 포함한다. 검사기와 설치 직전에는
선택 APK의 새로운 identity·hash를 검증하며, 일반 native 검사 권한으로 사본을 선택할 수 없다.
SDK/C guardian의 경로·ABI는 그대로이고 원래 producer/기기 FD를 계속 보유한다. 설치 후에도
누락·불일치가 남으면 격리·예약을 유지한다. helper 진입 시 descriptor 검증도 파일 읽기보다
먼저 수행한다. 완료 시 새 사본까지 삭제한 뒤 r30의 live cleanup capability로 예약을 해제한다.

소스 변경·사본 변조·검사 직후 설치 전 변조, descriptor 복제, 부분/빈 파일 commit 중단,
helper만 없는 경우, 원래 일부 파일 재사용, 사본 삭제 중단 후 기기 명령 없는 재개를 검사했다.
`android-recovery-material-gate-r1.log`의 등록된 D4/검사기 160개가 통과했다. 변경되지 않은
G1A 53개를 재사용하면 고유 183개다. 이번 gate에는 ResourceWarning/skip이 없으며 실제
cached SDK·C guardian과 자체 protocol server를 썼다. 실기기/전체 제품 완료 근거는 아니다.

r32 근거는 `foundation-progress-r32.json`과 당시 `next-work-after-r32.json`이다. 당시 다음은 실제
프로세스 종료를 이용해 metadata commit 경계를 검사하는 것이다. `_write_new_at()`는 처음
JSON에 직접 쓰고 `_replace_at()`는 `.record-*` 임시 파일을 만든다. 지금의 I/O 예외 테스트는
finally가 실행되므로 SIGKILL/os._exit로 남는 부분 JSON/임시 파일 경계를 검증하지 못한다.
원래 잠금과 committed 기록을 보존하면서 해당 임시 파일만 처리하고, 미완성 JSON을 정리
권한으로 승격하지 않는 복구를 구현·검증한다. 관련 진입점은 `repair_android_operation.py`의
두 writer와 `_recovery_files`, `android_recovery_materials.py`다. 그 뒤 운영 CLI·서비스 시작,
실제 qualification·나머지 D4/D5를 이어간다. r31의 간헐 stop-helper 대기는 원인 미확정 이력으로
유지하며 이번 gate에서 재현되지 않았다. 새 package/build/installation 사본은 만들지 않았다.

r33은 실제 metadata writer 프로세스 종료 경계를 재현하고 보완했다. `_write_new_at()`는
부분 JSON을 최종 이름에 쓰지 않고, fsync한 임시 파일을 atomic no-replace link로 공개한다.
공개 직후 죽어 남은 내부 2-link 기록은 같은 폴더의 단 하나 `.record-*` alias인 경우에만
완성된 metadata로 읽는다. 일반 APK/input 파일의 hardlink 거절은 유지한다.

`finalize_recovery()`만 `_recovery_files(retire_metadata=True)`를 사용한다. 원래 요청·구성·
RunStore/producer 바인딩과 잠금을 먼저 확인한 뒤 작업 폴더·phases·원래 native-calls의 제한된
writer scratch를 지운다. 내용을 정리 권한으로 채택하지 않고 committed target은 보존한다.
범위 밖 링크·symlink·큰 파일·틀린 요청·활성 producer는 거절한다. 공개 read-only recovery는
scratch를 지우지 않는다. 구버전이 이미 최종 이름에 쓴 불완전 JSON은 추정 복구하지 않고
보존하며, parser 오류는 일관된 AndroidOperationError로 처리한다.

별도 writer의 `os._exit`와 SIGKILL, 공개 전/후·교체 전 종료, committed state 보존, 2-link 정규화,
native slot metadata 보존과 전체 finalization을 검사했다. `android-metadata-crash-gate-r1.log`의
등록된 D4 175개가 통과했고 변경되지 않은 G1A 53개를 재사용하면 고유 198개다. ResourceWarning/
skip은 없다. 이는 실제 파일 writer 종료와 자체 SDK/protocol 환경 검증이며 물리 기기나 전체
Android finalizer 서비스 프로세스 종료 qualification으로 확대해서 표현하지 않는다.

r33 근거는 `foundation-progress-r33.json`과 당시 `next-work-after-r33.json`이다. 당시 다음은 운영
Android recovery CLI와 서비스 시작 조합이다. `protected_mobile_inputs.py`의
`load_protected_mobile_inputs`, `repair_configuration.py`의 runtime binding, 기존
`repair_signing_cli.py`와 `live/issue_configuration.py`를 활용한다. JSON status/설정만으로
DeviceAuthority·새 parent grant·최종 cleanup 권한을 복원하지 않는다. 그 뒤 실제 qualification,
남은 iOS/VM·회사·두 Mac 수용 검사와 새 wheel 설치 검증을 이어간다. r12 wheel과 10.1 GiB 정리
근거를 보존했고 새 package/build/installation 사본을 만들지 않았다. 간헐 stop-helper 대기
관측은 원인 미확정 이력으로 유지하며 이번 gate에서는 재현되지 않았다.

r34는 인증된 `ProtectedRecoveryService`와 HTTP/CLI를 연결했다. `live-serve`의
`--protected-recovery-config`는 shared/issue 설정을 함께 요구하고 `compose_recovery_workflow()`로
실제 runtime·등록·고정 mobile 입력을 연결한다. 이 시작 모드는 보호 repair 실행기 조합을
명시적으로 보류한다. 기존 journal만 create=False로 열고 다른 설정/요청의 작업은 거절한다.

`protected-service android-profiles`, `android-operations`, `android-status`, `android-recover`,
`recovery-job`, `recovery-cancel`이 `/api/protected-recovery`를 사용한다. 자격 증명은 prompt 또는
bounded stdin이며 출력하지 않는다. 서비스가 현재 읽기 또는 device.operate+fixture.execute
권한을 확인하고 실제 host의 새 project grant·DeviceAuthority를 사용한다. 실행 중 credential,
브라우저 세션·membership·assignment 취소를 재검사한다. JSON이 live 권한을 대신하지 않는다.

작업은 비동기이며 동시 4개·기기당 1개, 프로세스 내 조회 128개로 제한한다. 동일 request ID는
동일 요청에만 재사용하고 실행 중인 작업은 조회 이력에서 제거하지 않는다. 살아 있는 retained owner/세션을
빼앗지 않고, 종료된 원래 scope만 실제 정리 완료 후 Lab에서 제거한다. 등록된 보호 기기는
일반 device recover 경로로 우회할 수 없다. 자세한 명령은 `docs/PROTECTED-SERVICE-CONFIGURATION.md`다.

`protected-recovery-service-gate-r1.log`의 86개와 `protected-recovery-android-gate-r1.log`의
175개가 통과했다. CLI를 실제 subprocess로 실행해 인증된 실제 HTTP 서비스와 cached SDK/C
guardian의 복구까지 연결했다. viewer/outsider, forged principal, 취소/권한 회수, 원래 요청
불일치, 반복 요청, 활성 owner 거절, 실제 runtime 시작 조합을 검사했다. ResourceWarning/skip은 없다.

r34 근거는 `foundation-progress-r34.json`과 당시 `next-work-after-r34.json`이다. 당시 다음은 **실제
live-serve 프로세스 시작 및 종료/재시작 수용 검사**다. 지금은 실제 CLI client + 직접 구성한
HTTP server와 실제 시작 조합 함수까지 검증했고, 완전한 server CLI 프로세스는 아직 검사하지
않았다. 원래 authority·issue runtime·mobile journal 경로를 유지한 자체 endpoint/프로필로
시작해 metadata/SDK 작업 중 종료 후 새 프로세스가 복구하도록 증거를 만든다. 필요 시
기존 fixture의 authority/runtime 경로를 CLI의 표준 경로에 맞추되 production의 권한 검사를
test 편의로 풀지 않는다. 그 뒤 전체 보호 build/signing/validation 서비스 조합, 실제 native
qualification 및 남은 iOS/VM·회사·두 Mac 수용을 이어간다. 최신 설치본 검증은 아직 r12이며
현재 source는 포함하지 않는다. 10.1 GiB 정리 근거를 보존했고 새 package/installation 사본은
만들지 않았다. 간헐 stop-helper 대기는 원인 미확정 이력으로 보존한다.

r35는 완전한 `live-serve` CLI 프로세스를 실제로 시작하고 강제 종료·재시작했다.
복구 모드가 일반 `android_live_device()`를 거쳐 기본 ADB를 탐색할 수 있던 경로를 보완했다.
`recovery_device_descriptors()`가 고정 profile ID/정의/앱 메타데이터만으로 기기를 등록하며,
복구 모드에는 별도 Android/Simulator/iPhone/worker 선택 옵션을 섞을 수 없다. `_recoveryOnly`
기기는 일반 세션/녹화 admission을 거절하고, 공개 capabilities에 recoveryOnly 및 빈 actions를
표시한다. 의도적인 admission 제한이 실제 복구 완료 기기를 uncertain으로 바꾸지 않도록
presence/inventory 검사와 구분했다. 어떤 production 권한 검사도 fixture 편의로 풀지 않았다.

자체 endpoint와 실제 SDK/C guardian을 사용하는 fixture의 authority·lease·issue runtime 경로와
저장 용량 설정을 CLI의 표준 위치에 맞췄다. 원래 준비된 fixture도 재시작한 runtime이 정리했다.
별도 CLI server가 기본 ADB를 호출하지 않는지 검사한 뒤 실제 인증 요청으로 복구했다. SDK
작업 중, APK 삭제 뒤 예약 해제 전, RunStore 예약 0 commit 뒤 기기 해제 전에 SIGKILL을 보냈다.
원래 producer/device 잠금이 유지되는 것과 native 수집 후 다시 획득 가능한 것을 확인했다.
뒤의 두 재시작은 SDK/기기 명령을 반복하지 않고 정리를 마무리했다. credential/serial/ResourceWarning이
server 출력에 없는 것도 검사했다. 이는 자체 프로토콜 환경의 전체 프로세스 수용이며 실기기
qualification을 대신하지 않는다.

`protected-recovery-process-service-gate-r1.log` 91개, `protected-recovery-process-android-gate-r1.log`
175개, `protected-recovery-process-g1a-r1.log` 53개가 현재 코드에서 통과했고 고유 289개다.
이번 gate에는 skip/ResourceWarning이 없다. 다음 문서와 source hash는 `foundation-progress-r35.json`,
다음 작업은 `next-work-after-r35.json`에 기록했다. 문서의 참조 digest는 암호 서명이 아닌 고정
hash이며 그 표현만 gate 후 바로잡았다.

다음은 현재 복구 전용 시작 모드와 별개로 **보호 build/signing/mobile/validation 실행기 전체를
연결하는 서비스 조합**이다. 기존 `protected_build_signing_inputs.py`, `protected_mobile_inputs.py`,
`protected_validation_inputs.py`, `repair_composition.py`의 고정 입력/팩토리를 조합하고 현재
qualification capability를 요구한다. 설정/과거 status만으로 qualified 실행기를 만들지 않는다.
이후 실제 native qualification, 남은 iOS/VM·회사·두 Mac 수용과 새 wheel 설치 검증을 이어간다.
최신 보존 설치본은 r12이며 현재 source를 포함하지 않는다. 새 package/installation 사본을 만들지
않았고 10.1 GiB 정리 근거를 보존했다. r31의 간헐 stop-helper 대기는 원인 미확정 이력으로 유지한다.

r36은 `protected_service.py`에 고정 입력 전체를 묶는 `load_protected_service_inputs()`,
기존 runtime에 연결하는 `compose_android_protected_service()`, 새 runtime을 만드는
`compose_android_protected_workflow()`를 구현했다. 같은 composition authority의 현재 mobile
qualification, 명시적 signing material resolver와 validation secret registry가 있어야 owner를
만든다. VM qualification → unsigned APK 구조 검사 → JVM signing owner/독립 inspector →
Android mobile/외부 observer → 실제 이슈 repair jobs로 연결한다. 복구 전용 inventory는 거절한다.
입력·관찰자·권한을 중간과 연결 직전에 다시 확인하며 실패/인터럽트 때 이미 만든 owner를 수집한다.

`QualificationAuthority.authorize()`가 같은 issuer를 가진 qualification 복사본/변경된 만료시각을
받아들이는 결함을 재현했다. 이제 `require_qualification()`은 registry의 정확한 발급 객체,
범위·현재 시각·폐기를 확인한다. 서명 resolver는 키 내용을 열지 않고 등록 여부를 확인하고,
observer 입력은 해당 owner의 등록된 secret reference를 확인한다. owner가 생성된 뒤의 중단도
정리하며, 불완전한 close에는 호출자가 보유한 원래 composition으로 재시도할 수 있다.

`protected-service-assembly-gate-r3.log`의 등록된 `D4_SERVICE_ASSEMBLY` 114개가 통과했다.
qualification 복사/다른 authority/폐기 거절, 입력·observer 변경, 생성 중 실패·인터럽트,
기존/새 runtime 연결과 기존 VM·JVM 서명·기기·관찰자 회귀를 포함한다. 새 assembly 테스트의
VM/mobile qualification과 build-input loader는 명시적 대역이며 signing material은 등록용
placeholder다. 실제 고정 팩토리와 runtime은 사용했지만 이 테스트로 실제 VM/mobile 환경
qualification이나 private-key 서명 실행을 주장하지 않는다. 개별 실제 JVM 서명 검사는 gate에
포함했다. 첫 gate 로그 r1은 등록 전 잘못된 테스트 파일명을 사용한 harness 오류이며 보존한다.

r36 근거는 `foundation-progress-r36.json`과 당시 `next-work-after-r36.json`이다. 당시 다음은 일반
실행 CLI를 위해 필요한 **고정 mobile qualification runner**와 명시적 secret 등록 시작 경로다.
`execution/backend.py`의 mobile-device 필수 5개(device-boundary, network-boundary, backend-scope,
process-termination, state-cleanup)를 실제로 측정하는 trusted 경로가 필요하다. 저장된 pass JSON이나
새 인증 nonce만으로 해당 환경을 측정했다고 간주하지 않는다. 실제 환경이 없어 실행하지 못하는
경우에는 이를 분리해서 기록하고 가능한 구현을 진행한다. 현재 전체 체인 연결은 신뢰된 로컬
Python API이며, `live-serve --protected-recovery-config`는 계속 복구 전용이다. 원래 회사·실기기·
두 Mac/iOS/VM 수용과 새 wheel 설치 검증도 남아 있다. 최신 보존 package는 r12이며 현재 source는
포함하지 않는다. 새 package/installation 사본을 만들지 않았고 10.1 GiB 정리 근거를 보존했다.

r37에서는 실제 mobile qualification에 기기 자체의 네트워크·앱 데이터 경계를 측정할 환경이
필요함을 확인했다. host Seatbelt/gateway 측정이나 live 인증 응답을 5개 전체 probe의 통과로
대체하지 않았다. 사용자에게 전용 Android emulator/준비된 실기기 격리 환경/추후 환경 제공 중
우선 환경을 async 질문했으며 아직 답변은 없다. 질문은 권한 승인으로 간주하지 않는다.

독립적으로 진행 가능한 `protected_service_materials.py`를 구현했다. 고정 configuration digest와
profile/observer 참조 전체를 확인한 명시적 256 KiB binary stream 또는 bytearray만 받는다.
키 경로·alias·Base64 password와 observer key를 별도의 private-material 채널로 등록하고 환경/
credential 파일을 탐색하지 않는다. 공개 artifact 경로의 key-file 거절 규칙은 약화하지 않았다.
키 내용은 열지 않고 resolver의 파일 metadata 바인딩만 수행한다. 등록 중 실패/인터럽트에는
만든 registry를 닫고 가변 입력/디코딩 버퍼를 지운다. Python의 불변 임시 값 전체 소거는 주장하지 않는다.

`compose_service_from_material_stream()`은 실제 현재 qualification·비어 있는 시작 owner를
확인하기 전에는 비밀 stream을 소비하지 않는다. 입력 중 권한 폐기/구성 변경을 다시 검사하고
등록한 재료를 r36의 고정 실행기 체인으로 연결한다. 이 역시 신뢰된 로컬 API이며 일반 실행
CLI나 mobile qualification runner 자체는 아니다. 공개 결과에는 digest/개수/authority none만 있다.

`protected-service-material-gate-r1.log`의 D4 조합·native signing 관련 133개가 통과했다.
명시적 입력/registry, 실패 후 정리, buffer 보존/소거, 프로필·참조·중복·경로·인코딩 거절,
unqualified/closed owner의 stream 미소비와 읽는 중 qualification 폐기를 포함한다. 새 조합
테스트의 VM/mobile qualification·build-input loading은 이전과 같은 명시적 대역이다.
실제 mobile qualification은 구현/발급하지 않았으며 실환경 검증으로 확대하지 않는다.

r37 근거는 `foundation-progress-r37.json`, 당시 다음 작업은 `next-work-after-r37.json`이다. 환경 답변이
오면 qualification의 실제 제어/측정 계약에 반영한다. 독립적인 다음 작업은 r12 이후 누적된
변경의 전체 검사와 새 wheel 설치 검증이다. 현재 배포물은 오래된 source이므로 표준 빌드/설치
경로를 확인하고 새 결과를 만든 뒤 검증한다. 필요하면 이미 승인된 범위의 이전 build-input/
installation 사본을 원본 wheel/hash 근거와 비교해 정리하되 최신 설치본·실행 증거는 보존한다.
회사·실기기·두 Mac/iOS/VM의 실제 수용과 정상 실행 CLI는 여전히 남아 있다. 목표는 active다.

r38에서 누적 source 전체를 검사하고 새 wheel을 설치·검증했다. 최초 전체 실행 r14는
1,826개 중 실패 2개·오류 1개였다. 프로세스 그룹 종료 중 macOS `PermissionError`가
다시 cleanup 오류로 번지던 경로를 수정했다. 신호 거절 뒤에도 원래 자식의 수거와 빈 그룹을
모두 확인해야 정리 완료로 인정하며, 살아 있거나 상태가 불명확한 소유자는 유지한다.
새 회귀 검사 2개와 기존 출력 초과 검사가 통과했다. 누락된 고정
`error_prone_annotations:2.28.0` 원본 JAR는 기존 승인 범위의 Maven Central에서 체크섬을
확인해 복원하고 Gradle로 캐시를 등록했다. 실제 오프라인 buildSrc 검사도 통과했다.

`full-suite-parent-r15.log`에서 **1,828개**가 629.893초에 통과했다. 입력 499개를 고정했고
실행 중 변경·skip·ResourceWarning이 없다. Python 3.14.7에서 실행했으며 고정 보조 도구
PATH를 유지했다. 첫/중첩 SDK stop 명령의 지연을 감시했지만 이번에는 sample이 발생하지
않았다. r14의 첫 stop-helper 15초 멈춤과 이전 중첩 stop 멈춤은 **원인 미확정**이다.
통과한 재실행을 SDK 수정 근거로 삼지 않는다. r4 진단 harness는 inspector의 None command를
처리하지 못한 오류였고, 이를 수정한 r5에서 위 전체 검사를 실행했다. 이전 실패 로그는 보존했다.

새 `artifacts/product-delivery/d4-foundation-package-r13/acceptance.json`은 149개 모듈·109개
공개 리소스와 현재 source의 일치를 기록한다. wheel SHA-256은
`f67e7c946df2d76e784d94a73ec23c6add0d2ea3c1e0ae2901591b24ddb77956`이다.
Python 3.11의 별도 설치본으로 guardian 컴파일, 실제 cached SDK/AAPT와 OS 경계,
설치된 CLI client/server의 정상 시작 및 세 SIGKILL 복구 경로(실행 중·APK 폐기 후·예산 반환 후)를
확인했다. 서버는 checkout 밖에서 실행하고 tests import를 차단했다. fixture 생성에는
repository tests를 사용하며 upstream 기기는 소유한 프로토콜 대역이다. 실제 기기/VM/mobile
qualification을 검증했다는 뜻은 아니다. `docs/INSTALLATION.md`도 새 배포물로 갱신했다.

기존 승인에 따라 r12의 `build-input`과 `installation`만 정리했다. 이전 wheel·입력 해시와
사본을 비교하고 열린 프로세스가 없음을 확인한 뒤 379개 디렉터리·926개 파일/링크,
19,177,472 allocated bytes(18.3 MiB)를 제거했다. `cleanup-r2/result.json`과 정리 후 새 설치본
검사를 보존했다. r12 wheel·기존 증거와 최신 r13 설치본은 보존했다. r1의 10.1 GiB 정리는
별도 기존 실적이다. 현재 근거는 `foundation-progress-r38.json`, 다음 작업은
`next-work-after-r38.json`이다. 환경 질문에는 아직 답이 없고 D4/D5/전체 목표는 미완료다.

r39에서는 iOS 기기 실행 조합의 남은 경로를 확인하다 고정 서명 factory의 중단 정리
누락을 재현했다. `configure_ios_signing_owner()`와 `configure_android_signing_owner()`는
일반 예외만 처리하여 `KeyboardInterrupt`/`SystemExit`가 검사기 생성 또는 supervisor의
ready 검사에서 발생하면 이미 만든 signer·작업 store(또는 inspector)를 열린 채 남겼다.
`signing-factory-interruption-red-r1.log`에서 두 검사·8개 하위 경우가 모두 이 이유로 실패했다.
두 factory가 중단 예외에서도 부분 소유자를 닫고 원래 중단을 재전달하도록 수정했다.
조합에 채택되지 않은 resolver는 호출자 소유로 보존한다. 고정 callback·서명 정책과
정상 실행 경로는 유지했다. 중단은 Python 예외로 주입했으며 실제 OS 신호 시험으로 확대하지 않는다.

`D4_SERVICE_ASSEMBLY`에 기존 iOS native pipeline 모듈을 포함했다.
`signing-factory-lifecycle-gate-r1.log`의 **147개**가 통과했고 입력 500개는 실행 중
변경되지 않았다. ResourceWarning/skip이 없으며 실제 자체 CMS·iOS 서명·독립 검사,
부모 프로세스 종료 후 복구와 JVM 서명 조합도 포함했다. VM/mobile qualification 부분은
명시적 대역이다. 전체 검사는 r38 근거를 유지하며 새 전체 실행을 주장하지 않는다.
이번 수정은 source에만 있고 r13 wheel은 r38 기준이다. 새 설치/빌드 사본을 만들지 않았다.

iOS의 signed IPA 준비·서명·복구는 이미 있지만, `ProtectedRepairComposition`에는
Android mobile factory만 있고 `load_protected_service_inputs()`도 Android만 받는다.
기존 `IosPhysicalDevice`의 전역 devicectl 호출을 보호 실행기로 바로 채택할 수 없다.
선택 기기의 고정 transport/tool 경계·전체 작업 소유권·정리 및 실제 환경 qualification을
연결해야 한다. 다음에는 이 실제 공백을 진행하며 서명/패키지 검사를 무작정 반복하지 않는다.
현재 근거는 `foundation-progress-r39.json`, 다음은 `next-work-after-r39.json`이다.

r40은 `ios_device_tools.py`의 `IOSDeviceTools`/`PinnedDeviceCtlClient`를 구현했다.
명시적 Mach-O 경로·hash와 CoreDevice UUID·UDID·bundle·private 작업 root를 묶고,
매번 선택 기기의 details를 확인한 뒤 고정 앱 필터/프로세스 조회를 수행한다.
`select_iphone(query_client=...)`는 실패 시 전역 검색으로 우회하지 않는다.
기존 기본 선택 경로는 유지하며, 설치·XCTest·재생을 새 클라이언트로 바꾼 것은 아니다.

실제 캐시된 Xcode `usr/bin/devicectl`은 셸 래퍼이며 CoreDevice 버전 불일치 시
`xcodebuild -runFirstLaunch`를 실행할 수 있었다. 이 래퍼는 거절하고 명시적으로 선택한
실제 CoreDevice Mach-O를 읽어 hash/형식을 확인했다. `ios-device-tools-cached-binding-r1.json`은
이 공개 SDK 파일 확인만 증명한다. 실제 devicectl/서비스/기기/개인 pairing 파일을 호출하지 않았다.
framework 의존성 전체와 CoreDevice daemon도 고정/격리했다고 주장하지 않는다.

조회 자식은 기존 `_ProcessOwner`의 깨끗한 환경·취소·기한·수거를 사용한다. JSON 256 KiB,
각 stdout/stderr 64 KiB 읽기 한도와 결과 파일 감시를 적용했다. 디스크 강제 quota는 아니다.
미수거 프로세스나 예상 밖 파일/링크는 작업 사본과 재시도할 owner를 보존한다. 공개 관찰은
digest와 host collection 정보만 포함하고 deviceCleanupConfirmed/실행 권한은 발급하지 않는다.
`docs/IOS-DEVICE-QUERIES.md`에 조회 API와 현재 경계를 기록했다.

`D4_IOS_DEVICE_QUERIES`의 `ios-device-query-gate-r1.log` **41개**가 통과했다.
입력 503개는 실행 중 불변이며 skip/ResourceWarning이 없다. 새 13개 검사는 자체 Mach-O
프로토콜 대역(본문을 고정한 Python payload 실행)으로 argv·환경·응답·취소·정리 실패를 확인한다.
기존 iPhone/worker profile/자동 관찰 회귀도 포함한다. 실제 iPhone·native device lease 상속·
기기 OS 경계/네트워크/backend qualification은 이 gate의 범위가 아니다.
새 build-input/installation 사본을 만들지 않았고 최신 패키지는 여전히 r13이다.
현재 근거는 `foundation-progress-r40.json`, 다음은 `next-work-after-r40.json`이다.

r41은 `ios_mobile_operation.py`의 `IOSMobileDefinition`/`IOSMobileOperationStore`를 구현했다.
기존 canonical repair-scope/RunStore 예약에 후보와 baseline IPA, 원래 context·기기 scope·
profile/query digest, root/file inode를 묶었다. 파일을 쓰기 전에 압축·확장·추출 사본 예산을
예약한다. 준비 callback은 원래 producer descriptor를 복제해 admission 종료 뒤에도 파일
잠금을 유지한다. live intent digest가 IPA+저널 동시 변조를 막고, baseline의 후보 역할 덮어쓰기도
거절한다. `.app` 파싱·bundle/digest 검사와 단순 파일 준비만 수행하며 서명/기기 권한은 없다.

중단·실패 시 파일과 예산을 보존한다. run hold 때문에 일반 `finish(stopped=True)`로 파일이
남은 예약을 반환하지 못한다. fresh `create=False`의 status는 엄격한 스키마의 관찰이며
복구/정리 권한이 아니다. 원래 producer 잠금·guard·artifact를 확인하고 예산을 반환하는
전용 복구는 다음 단계다. `docs/IOS-MOBILE-OPERATIONS.md`에 현재 계약과 미완성 범위를 기록했다.

`ios-mobile-preparation-gate-r3.log`의 `D4_IOS_MOBILE_PREPARATION` **62개**가 통과했다.
새 15개와 기존 IPA/parser/RunStore/mobile 회귀를 포함하며 입력 506개는 불변, skip/ResourceWarning은
없다. 추출 후 실제 별도 Python이 `os._exit(73)`으로 종료된 뒤 파일·예산이 남고 fresh owner가
관찰하는 경로, admission 종료 후에도 callback의 원래 kernel 잠금이 남는 경로를 확인했다.
기기 명령·native 기기 lease 상속·sanitation·실제 qualification은 수행하지 않았다.
초기 임시 추출 폴더를 public `.app` parser에 넘긴 오류는 기존 내부 capability parser로
수정했다. 변조 red-r1은 비정규 JSON에 막힌 부적절한 재현이었고, 올바른 canonical JSON과
유효한 변경 IPA의 red-r2에서 두 결함을 재현한 뒤 수정했다. 모든 이전 로그는 보존했다.

실제 등록 runtime과 이 선언을 연결하는 iOS 입력 로더/adapter도 아직 없으며
`ProtectedMobileSupervisor`의 Android 전용 영속 조합은 유지했다. 새 배포 사본은 만들지 않았다.
현재 근거는 `foundation-progress-r41.json`, 다음은 `next-work-after-r41.json`이다.

r42는 `ios_mobile_recovery.py`와 `RunStore.finish_ios_preparation_recovery()`를 연결했다.
canonical scope·RunStore 잠금·원래 producer 잠금 아래에서 context/정의/inode를 재확인하고,
원본 IPA의 경로 목록과 남은 앱·추출 사본을 대조한다. 각 IPA는 그 역할의 나머지 payload를
지운 뒤 마지막에 지운다. 이상 파일/링크·변경 baseline·위조된 완료 JSON은 정리 권한이 아니다.
현재 프로세스/스레드/원래 owner/registry/열린 잠금에 묶인 일회용 capability가 run hold를
지우고 예약을 0으로 바꾼다. 결과는 failed/cancelled이며 기기 소유권·정리 권한을 발급하지 않는다.

실제 별도 프로세스가 삭제 중, 예약 commit 직전, commit 직후에 `os._exit`로 사라진 뒤
새 owner가 복구를 마치는 경로가 통과했다. 기존 preparation 중단 사본, 늦은 callback의
kernel 잠금 거절, capability 복사/다른 스레드/다른 store/재사용 거절도 확인했다.
완전한 원래 intent가 없거나 손상된 작업은 계속 거절한다. 원래 기기 실행은 이 형식의 범위가 아니다.

기존 ZIP metadata 검사를 추출·복구가 공유하도록 분리했다. 명시되지 않은 상위 디렉터리도
추출 전 개수·대소문자·파일 충돌 검사에 포함한다. 원본/helper manifest digest와 후보의 context
digest 연결을 journal reader에서도 재검사한다. `ios-mobile-recovery-gate-r1.log`의
`D4_IOS_MOBILE_PREPARATION` **88개**가 통과했고 입력 508개는 불변이며 skip/ResourceWarning이 없다.
기존 실제 자체 CMS/iOS 서명·독립 검사·부모 종료 복구도 포함한다. 새 recovery 13개와 implicit
ZIP parent 검사 2개가 추가됐다. 실제 기기·VM/Apple 회사 환경의 qualification은 아니다.

초기 helper 분리에서 임시 경로 변수를 잘못 옮긴 오류와 `App.app`에 Android ID 전용 디렉터리
helper를 사용한 오류를 수정하고 실패 로그를 보존했다. 원래 Android helper 규칙은 완화하지 않았다.
기능 API와 범위는 `docs/IOS-MOBILE-OPERATIONS.md`를 따른다. 새 배포 사본을 만들지 않았고
현재 근거는 `foundation-progress-r42.json`, 다음은 `next-work-after-r42.json`이다.

r43은 `ios_mobile_inputs.py`와 공통 mobile loader의 iOS 분기를 연결했다. 실제 등록된
project/application/build·기기 할당·profile/identity·UDID·fixture 참조를 대조하고, pinned IPA
내용을 메모리에서 읽어 기존 tree_manifest 의미의 앱 해시/크기와 bundle/version/build를 검사한다.
ZIP metadata를 객체 할당 전에 확인하고 읽기 중 source 변경도 거절한다. 로딩 시 디스크 추출·
도구 실행·기기 접속·새 작업 root 생성은 없다. 기존 `.DS_Store` 제외 의미와 별도의 전체 IPA
SHA-256을 구분한다. helper는 host/runner 쌍의 역할·서로 다른 bundle·총 64 MiB 한도를 적용한다.

`IOSDeviceQueryDefinition`은 작업 폴더가 아직 없어도 만들 수 있는 실행 없는 선언이다.
실제 client는 기존 private 폴더를 요구하며 정의 digest는 같아야 한다. 조회 root와 다른
profile의 journal/owner/tool/input 경로 중첩은 거절한다. `PreparedMobileInputs.open_ios_preparation()`은
현재 입력·등록을 재검사한 뒤 지정된 RunStore/owner root로 준비 저장소를 연다. 정의·baseline을
실제 준비·복구·예약 반환까지 연결한 검사도 통과했다. 전체 보호 서비스는 여전히 Android만 받는다.

`D4_IOS_MOBILE_INPUTS`의 `ios-mobile-inputs-gate-r1.log` **85개**가 통과했다. 새 iOS 입력 9개와
query 선언 1개, 기존 Android 입력/서비스 조합/실제 CLI 복구 회귀를 포함한다. 입력 510개는
불변이며 skip/ResourceWarning이 없다. 등록 저장소는 실제 구현이지만 테스트의 물리 기기
descriptor와 grant provider는 명시적인 metadata 대역이고 grant를 발급하지 않았다.
실제 CoreDevice Mach-O는 파일 hash만 확인했으며 실행하지 않았다. 실제 기기/격리 qualification은 아니다.

초기 fixture의 APPL package type 누락/잘못된 build 값을 수정했고 실패 로그는 보존했다.
진단을 stdin으로 실행해 multiprocessing spawn이 실패한 한 번의 harness 오류도 실제 기능
오류로 취급하지 않았다. 이후 guarded 실제 파일에서 원인을 확인했다. 새 패키지를 만들지 않았다.
현재 근거는 `foundation-progress-r43.json`, 다음은 `next-work-after-r43.json`이다.

r44는 누적 변경의 전체 검사·새 설치·정리를 수행했다. `full-suite-parent-r16.log`에서
1,883개를 실행했으며 초기 결과는 실패 12개·오류 10개였다. 모두 없어져 있던 고정 Kotlin/
AGP/ASM cache와 그에 필요한 Gradle metadata/의존성 문제였다. 기존 승인 범위의 Google Maven/
Maven Central/Plugin Portal에서 정확한 버전을 복원했다. JDK 자체는 17.0.20으로 정상임을 확인했다.
광범위 dependency graph만으로 빠지는 별도 버전과 plugin marker, AAPT2까지 실제 Gradle
8.14.5/9.6.1과 helper compile로 복원한 뒤 실제 offline helper 검사도 통과했다.
처음 cache 복원용 Gradle 설정의 variant 불명확 오류는 java runtime 설정으로 수정했다.

`full-suite-r16-cache-recheck-r1/r2/r3.log`는 각각 22개 중 20개, 2개 중 1개, 마지막 1개의
통과를 기록한다. 모든 source 입력 510개는 불변이었다. `full-suite-combined-r16.json`이 각
검사의 최종 결과를 대조한 고유 **1,883개 통과** 근거다. 최초 전체 실행이 한 번에 통과했다고
주장하지 않는다. skip/ResourceWarning이 없고 stop observer의 stall sample도 없었다.
간헐적 SDK stop-helper 멈춤의 원인은 여전히 미확정이다.

`artifacts/product-delivery/d4-foundation-package-r14/acceptance.json`이 최신 package 근거다.
wheel은 1,113,080 bytes, Python 모듈 153개·리소스 109개이며 SHA-256은
`b56c7aa393c3db868b4ebf36557f15a8f077ef7b9aa5c4b2595b4d6344324c49`다.
Python 3.11 설치본의 native guardian/ADB/AAPT와 Android CLI server 복구 4개가 통과했다.
새 iOS API는 별도 설치 프로세스 10개로 정상 준비·복구, 추출 중 종료, 예약 commit 전/후 종료의
4개 시나리오를 통과했다. runtime은 checkout 밖에서 실행하고 tests import를 차단했다.
fixture 생성에는 repository tests를 썼으며 기기/grant provider는 metadata 대역이다.
실제 CoreDevice·기기·VM·qualification을 검증한 것은 아니다. 초기 설치 harness의 잘못된
Lab.close 호출은 close_all로 수정했고 실패 로그를 보존했다.

기존 승인에 따라 r13 build-input/installation의 wheel/입력 hash와 열린 프로세스 부재를
확인한 뒤 376개 디렉터리·889개 파일/링크, 18,612,224 allocated bytes(17.8 MiB)를 정리했다.
`cleanup-r3/result.json`과 정리 후 r14 installation-check를 보존했다. 이전 wheel·증거와
최신 설치본 및 복원한 Gradle dependencies는 유지했다. 이번에는 제품 source를 수정하지 않았다.
현재 근거는 `foundation-progress-r44.json`, 다음은 `next-work-after-r44.json`이다.

다음은 실제 mobile qualification runner와 보호 서비스의 공개 구성,
기기 작업의 실제 복구·CLI, iOS 실제 앱 서명과 보호 실행 예산·전송·복구 연결이다.
기기 저널의 read-only inspection이나 설정/과거 JSON을 정리·qualification 권한으로
쓰지 않는다. 공식 ADB 소스도 기본 사용자 키 로딩 실패 시 vendor key 로딩을 건너뛴다.
캐시된 ADB는 기본 `.android`를 막은 전용 서버 실험에서 실패했으며
소유한 서버와 생성 키는 정리했다. 실제 회사 앱·VM image/toolchain·서명 재료·기기/
backend 격리·AI 전송 정책·두 Mac 입력은 아직 제공되지 않았다. D4와 D5 및 전체 목표는
미완료이며 새 wheel도 개발 checkpoint다.

G9의 일반 제품 파일 수정 제안과 보호된 검증 조합을 구현했다. G7 이슈 화면과 HTTP/CLI에서 제안·검증 요청·진행·취소·패치 검토를 사용할 수 있다. 원본의 같은 명세 3회 재현을 확인하고, 별도 후보의 빌드 → 고정 서명·독립 signature 검사 → 후보 밖의 회귀 검사 → 같은 명세의 후보 3회 재생 → 종료·정리를 연결한다. 기본 JSON 구성은 제안만 활성화한다. 보호 검증은 운영자가 로컬 코드에 등록한 실제 qualification과 실행기를 요구한다. [G9 운영 문서](../docs/PROJECT-REPAIR.md)를 먼저 읽는다.

G9 정식 검사 92개는 `artifacts/qa-delivery/g9-release-parent-r3.json`에서 통과했다. `g9-parent-covered-tests.json`은 그 시점의 고유 검사 1,026개를 대조한다. 998개를 실행했고 변경되지 않은 G8a 25개는 이전 통과 근거를 재사용했다. 당시 차단된 고정 Android 검사 3개는 9월 13일 `d1-android-r1/cached-dependencies.json`과 `helper-compile.json`에서 모두 통과했다. 원래 차단 보고서는 보존하며 현재 전체 검사를 한 번에 재실행했다는 뜻은 아니다. G0–G7·G8b 결과는 `g9-regression-*-r1.json`, 빠진 기존 검사 146개는 `g9-parent-remaining-regressions.json`에 있다. 추가 다운로드·빌드 버전 변경은 하지 않았다.

G9 브라우저 증거는 `g9-protected-browser-r4/result.json`과 같은 폴더의 데스크톱·390px 화면이다. 실제 UI에서 제안/보호 검증, 후보 3회 결과, 패치 검토, 프로젝트 전환 중 늦은 응답, credential 회수 후 소스 제거를 확인했다. VM·기기·서명은 명시적인 소프트웨어 대역이다. 최상위 `actualVM`, `actualMobile`, `actualAI`, `companyAcceptance`, `verified`는 모두 false이며 내부 `jobVerified`를 실제 회사 앱 검증으로 읽지 않는다.

G8b/G9 실제 환경 gate는 각각 `g8b-environment-r1/result.json`, `g9-environment-r1/result.json`에 `environment-not-supplied`를 기록했다. 회사 앱·QA·fixture, 소유 VM 이미지/오프라인 toolchain, 실제 signer/inspector와 기기 격리 provider, 독립 관찰, AI 전송 정책, 두 Mac 입력이 남아 있다. 특히 모바일 provider는 설치부터 마지막 sanitation까지 배타적 소유권을 유지해야 한다. 기존 Lab의 개별 replay 예약만으로 이 조건을 충족하지 않는다. 실제 `.app` provisioning 처리 및 signing/mobile quarantine의 운영 복구 CLI도 별도 통합 항목이다. 전체 제품 acceptance는 미완료다.

핵심 소스는 `reproof/project_repair.py`, `repair_{execution,signing,mobile,verification,journal}.py`, `validation.py`, `live/project_repair_jobs.py`다. G4는 로컬 candidate capability를 임시로 발급하며 원본 project/specification은 변경하지 않는다. 위조된 결과나 정리 불명확 상태로 `verified`를 발급하지 않는다. 서명·기기 scope의 영속 quarantine은 새 디렉터리로 우회할 수 없다. 반복 SQLite 읽기 경쟁으로 발생하던 명세·원본 recording·campaign 누락도 회귀 검사 후 잠금으로 수정했다.

최종 변경 검토와 채택 근거는 `artifacts/qa-delivery/g9-parent-review.md`, `g9-acceptance.json`에 있다. 공개 소스 사본/해시는 `g9-parent-source.tar.gz`/`g9-parent-source.json`으로 고정한다. 이전 실패 시도도 보존하며 독립 worker 승인이나 실제 환경 완료를 만들어내지 않는다.

사용자가 “계획부터 짜고 구현까지 쭉 밀어줘”로 계획·구현·검증을 승인했다. [최종 실행 계획](../docs/IMPLEMENTATION-DELIVERY-PLAN.md)의 앞부분 목표별 보완 사항이 아래 이전 상태보다 우선한다. 3회의 독립 계획 검토는 ITERATE로 종료됐고 메인 에이전트가 필수 지적을 직접 실행 기준에 반영했다. 합의 승인으로 표현하지 않는다.

## Delivery History — before G9

아래 완료 수와 남은 작업은 각 단계 당시의 기록이다. 현재 상태는 위 Active Delivery를 따른다.

G7 기능 검사 61개가 통과했다. 최신 운영 API/CLI는 [공유 이슈 워크플로](../docs/ISSUE-WORKFLOW.md)에 있다. `artifacts/qa-delivery/g7-release-parent-r4.json`과 `g7-release-gate/20260912T074806Z-c5769014/result.json`은 실제 backend/coordinator/worker CLI 두 프로세스, coordinator 프로세스 재시작, ZIP 왕복, 두 번째 워커의 변경 없는 명세 3회 재현을 기록한다. `browser.json`의 12개 검사는 새 UI 녹화·조건 작성·승인·3회 재현, 실제 H.264 재생·회전·공백 이동·늦은 fetch/decode·권한 회수를 포함한다. 별도 브라우저 검사는 예약/해제 후 목록 자동 갱신과 미저장 명세 보존을 확인한다. 원격 native 촬영 시각은 unknown이며 원본은 incomplete로 보존된다.

`g7-parent-covered-tests.json`은 G7 시점의 서로 다른 878개 검사를 모두 대조해 875개 통과와 환경 차단 3개를 기록한다. 기존 Gradle 8.14.5·AGP 8.13.2·ASM 9.8 캐시가 현재 없어 Android helper/bytecode 컴파일 재검증이 차단됐다. 로컬 설치본 9.2.0의 offline 시도도 고정 AGP 부재로 실패했다. 배포본·고정 의존성을 작업 전용 폴더에 복구하는 네트워크 승인은 질문했고 아직 답변 전이다. 생성된 Kotlin classpath만 설치된 2.2.10 의존성으로 복구해 Kotlin 경계·runtime 검사와 iOS helper/Swift 실행 4개를 통과했다. 프로젝트 빌드 버전과 전역 설정은 바꾸지 않았다. G7 전체 acceptance는 이 3개 검사를 복구하기 전까지 보류한다. G8b의 실제 VM 미제공 상태를 유지하면서 구현 가능한 실행 프로토콜과 G9의 제안·보호 검증 거부 경로를 이어간다. 전체 목표는 미완료다.

G0의 project/evidence/scenario/observation/execution 계약을 구현·검증했다. 새 계약 검사는 49개, gate 통합 검사는 1개가 통과했고, 잘못된 중첩 입력 416개가 모두 안전하게 거절됐다. 근거는 `artifacts/qa-delivery/g0-release-final.json`, `g0-gate-integration-final.log`, `g0-malformed-wire-probes.json`, `g0-final-source.json`이다. 중간 구현에서 전체 Python 327개도 통과했으며 이후 변경은 새 계약과 해당 검사/문서로 한정했다.

다음 순서는 이슈 UI/package → 자격 확인된 보호 수정이다. 격리 VM 전제조건이 없어도 기록·재현 및 수정 제안/거부 경로는 계속 구현한다. [계약 API와 신뢰 경계](../docs/RELEASE-CONTRACTS.md)를 먼저 읽는다. 명세의 관찰 시간창은 각 replay의 oracle 시작점에 대한 상대 시간이고, 후보 승인은 빌드 이름과 manifest digest를 모두 고정한다. 문법 검사와 관찰 coverage 통과는 재현·수정 검증을 뜻하지 않는다.

G8a의 [실행 경로·준비 검사](../docs/REPAIR-EXECUTION.md)도 완료했다. `g8a-release-final.json`에 24개 검사 통과, `g8a-doctor-host.json`에 현재 host의 실제 Swift compile/run 결과가 있다. 이름이 같은 정책의 내용 변경과 동시 등록 충돌, 다른 프로젝트/실행 종류의 권한 사용, 잘못된 파일 입력을 거절한다. G8a 당시의 기본 backend는 모든 실행을 거절했다. 현재 G8b 구현은 위 문단을 따르며 실제 환경 자격은 아직 없다. `g8a-parent-review.md`와 `g8a-parent-source.json`이 최종 근거다.

G1a의 [영속 호스트 권한·시계·기존 잠금 공유 코어](../docs/HOST-AUTHORITY.md)는 37개 검사로 검증했다. 실제 프로세스 간 잠금, 재시작 격리, 갱신·만료·큐 순서·할당/종료 경쟁 조건을 포함한다. 시계 역행이나 부팅 식별자 변경은 이전 매핑을 영구 무효화하며, 부팅 식별자를 얻지 못할 때 추정값으로 권한을 발급하지 않는다. `g1a-release-final.json`, `g1a-parent-review.md`, `g1a-parent-source.json`이 근거다. G1b에서 Live/worker/네이티브 권한 연결 및 명시적 legacy 호환 경로를 구현했다. G1b 당시 전체 457개 unittest 범위를 검증했고 실제 loopback worker, Android live/driver 오프라인 컴파일, iOS 서명 없는 빌드, Swift/Kotlin 경계 실행 검사가 통과했다. 최종 근거는 `artifacts/qa-delivery/g1b-parent-review.md`, `g1-release-final.json`, `g1b-parent-remaining-final.json`, `g1b-parent-ios-final.json`이다. 호스트 종료만으로 살아 있는 helper를 해제할 수 없으며, 정확한 정리 작업의 terminal receipt 이후 `confirm_native_cleanup(permit)`이 필요하다. G2의 불변 기록·영구 저장·수집 정책도 실제 Lab에 연결했다. `artifacts/qa-delivery/g2-parent-review.md`와 `g2-acceptance.json`을 읽는다. G2 필수 98개 + 기존 연동·HTTP·권한 회귀 214개 = 서로 다른 312개 검사가 통과했다. 프로젝트 등록은 영속 비용을 먼저 예약하고, 같은 revision 재등록/재시작은 이중 계산하지 않는다. 기록은 저널 8 MiB와 종료 원본 4 MiB를 선예약하며, 입력 256 KiB 한도는 UTF-8 바이트로 계산한다. 객체별 8 KiB 메타데이터와 최대 8개 핀, 영속 삭제 표식 수를 제한한다. 네이티브 소스는 바뀌지 않아 G1b 빌드 근거를 재사용한다. G3의 실제 AVFoundation MP4 생성·복구·보관도 검증했다. 정식 G3 검사 80개(실제 인코딩·독립 디코딩 포함), G2 98개, 기존 회귀 214개가 통과했으며 중복을 제외하면 355개다. `artifacts/qa-delivery/g3-parent-review.md`, `g3-release-parent-r1.json`, `g3-parent-covered-tests.json`과 `g3-native-gate/20260911T185228Z-45747/result.json`을 읽는다. 영상 2개의 24장 프레임·색상·회전·불규칙 PTS를 확인했고 오류 입력 21개 및 고장 사례 7개가 통과했다. 정지 화면 중복 핀, 늦은 인코더의 원본 보존, 삭제/복구 실패의 용량 유지, 공백 탐색과 원본 시각 연결을 검증했다. G3 API는 `docs/AVFOUNDATION-VIDEO.md`에 있다.

G4의 prepared recording·fixture·승인된 시나리오·원본 반복 판정도 구현·검증했다. `artifacts/qa-delivery/g4-parent-review.md`와 `docs/PREPARED-RECORDING.md`를 읽는다. G4 정식 81개와 G0/G1/G2/G3 및 남은 기존 회귀는 총 574회, 서로 다른 527개 검사가 통과했다. native 빌드·경계 실행과 실제 AVFoundation MP4 생성·독립 디코딩도 다시 통과했다. 원본은 불변이며, 실행 전 시도 번호를 영속 저장하고 프로세스 중단 시 그 시도를 unknown/quarantined로 보존한다. 같은 결과를 여러 시도에 사용할 수 없다. 실제 승인 내용·전체 후보 manifest digest·준비 plan/payload·관측값의 시간 구간을 검사한다. 취소/기한 초과는 성공으로 바뀌지 않고, 응답 없는 입력·종료는 기기와 fixture 재사용을 보류한다. G4는 `verified`를 발급하지 않는다. 다음은 G6의 일반 앱 worker와 artifact 전송이다.

G5의 공유 coordinator·프로젝트 권한·호스트 등록도 구현·검증했다. `docs/SHARED-COORDINATOR.md`와 `artifacts/qa-delivery/g5-parent-review.md`를 읽는다. G5 필수 142개와 G0–G4/남은 기존 회귀는 총 702회, 서로 다른 587개가 통과했고 이전 G4의 527개 검사 ID가 모두 포함됐다. 실제 enrolled worker CLI 프로세스에서 허용된 grant 발급, 다른 프로젝트 거절, 호스트 취소 후 거절, 요청 본문 대기 중 취소를 확인했다. 브라우저 로그아웃·만료는 다음 프레임·작업 입력과 종료 레코드에도 적용된다. 과거 원본은 당시 프로젝트/policy로 계속 읽을 수 있고, 새 실행은 현재 revision을 요구한다. 오래된 세션/작업의 종료·취소와 확정된 정리는 유지한다. 회귀 검사는 artifacts 동적 로딩을 제거하고 tests에 포함했다. 네이티브 빌드·경계 실행 및 실제 MP4 인코딩·독립 디코딩도 다시 통과했다. 두 Mac TLS/실기기/회사 QA는 이 결과에 포함하지 않는다. G6 전에는 remote prepared recording을 거절하며, shared native CLI는 현재 한 프로젝트의 제한된 수명 grant만 구성한다.

새 기준선: `artifacts/qa-delivery/baseline-python-tests.log`에서 Python 294 tests가 33.100s에 통과했다. `before-source.tar.gz`/`before-source.json`에 공개 소스 277개 사본/해시가 있다. `host-preflight.json`은 실제 Swift 컴파일·실행으로 AVFoundation과 Virtualization 지원을 확인했다. 게스트 image/offline toolchain은 제공되지 않았고 containment qualification은 false이다.

실제 회사 앱/QA/fixture/두 번째 Mac/회사 AI 전송 정책은 질문했으나 답변 전이다. 이 환경들을 지어내지 않으며 로컬 합성 테스트와 실제 회사 acceptance를 구분한다. 원래 승인된 샘플·합성 QA 외 소스/로그는 외부에 보내지 않는다. 현재 단계는 기기를 생성·조작하지 않았다.

G6의 일반 앱 프로필·물리 워커 예약·영속 아티팩트 전송을 구현하고 검증했다. [워커 운영 가이드](../docs/WORKER-RUNTIME.md)와 `artifacts/qa-delivery/g6-parent-final-covered-tests.json`을 읽는다. G6는 82개, G0–G5와 기존 누락 회귀를 포함한 채택 검사는 784회·서로 다른 669개이며 이전 587개 ID를 모두 포함한다. 통과 후 바뀌지 않은 검사 묶음은 원래 실행 근거를 명시해 재사용했다. 같은 Mac의 실제 worker CLI 두 프로세스로 등록·프로필·예약/해제·업로드/다운로드를 검증했고, 다른 프로세스의 동일 물리 ID 등록도 실제 coordinator에서 거절됐다. 하드웨어 어댑터는 합성이다. 일반 iPhone은 현재 픽셀·입력을 지원하며 일반 로그/접근성 어댑터는 거절한다. 실제 설치 후 bundle/version 조회와 launch 확인 경로가 있지만 실기기 호환성은 미검증이다. Android/iOS helper 컴파일과 G3 MP4 생성/독립 디코딩은 통과했다. G5 당시의 한 프로젝트 grant·원격 prepared recording 거절 제한은 해소됐다.

G6 인벤토리 v2는 G1의 실제 소유권 저널·세대와 별도 hold를 사용한다. 격리/누락/재시작 상태 문자열로 해제할 수 없고 오래된 보고는 배정에서 제외한다. 전송은 `artifact-v2`의 별도 EvidenceStore를 사용하되 recording DiskBudget을 공유한다. 만료/삭제와 활성 pin, publication/cleanup 경쟁, allocation 중 프로세스 종료, 메타데이터 예산을 검사했다. 이전 전송·인벤토리 namespace는 자동 전환하지 않는다. 보조 CLI worker는 사용량 제한으로 종료했으며 메인 에이전트가 저장된 변경을 인계해 수정·검증했다. 그 worker의 실패 기록을 성공으로 바꾸지 않는다. G7, G8b, G9 및 최종 리뷰/브라우저 QA는 아직 남아 있다.

## Latest Product Goal — 2026-09-11 clarification

사용자는 회사 QA의 난재현 이슈를 **영상 + 실행 가능한 행동 + 시작 상태/환경**으로 남기고, 원격 실기기에서 재현 → AI 수정 → 같은 원본 기록으로 검증하는 루프를 원한다. 여러 Mac에 연결된 Android/iPhone을 빌리러 다니지 않도록 실기기 팜도 필요하다. 로그를 함수마다 직접 작성하는 부담을 없애려는 것이며, 관찰 로그 자체가 실행 기록인 것은 아니다.

우선 읽을 문서는 [제품 기준·Toss 사례·현재 간격·다음 검증](../docs/QA-REPLAY-DEVICE-FARM.md)이다. `PLAN.md`의 실행 순서도 실제 두 호스트/실기기 → 회사 QA 재현 패키지 → 보호된 AI 수정 검증으로 바꿨다. 실제 앱 스택은 사용자에게 질문했고 아직 제공되지 않았다. 회사 앱·서버·계정·두 Mac 접근을 임의로 가정하지 않는다.

당시 전체 제품은 미완료였고 durable video는 없었으며 `frame-references-only`이며, 실제 앱/백엔드 상태 복원과 서로 다른 Mac의 물리 iPhone worker/전체 artifact 전송은 구현·검증되지 않았다. 샘플의 3+3/숫자 oracle 통과를 회사 QA 전체 완료로 표현하지 않는다.

검증용 Live 서버·브라우저·세션 3개를 종료하고 전용 Android AVD/iOS Simulator를 소유권 확인 후 삭제했다. 다른 기기는 보존했다. `artifacts/app-logs-cleanup.json`에 결과가 있다.

이번 자동 관찰 로그 작업은 클릭·화면·Activity/앱/Scene 생명주기, 별도 app-log snapshot, Live 조회/다운로드와 `--app-logs`를 추가했다. Android의 패널·별도 Activity·재생성·background/복귀·run 회전·입력 비기록·다운로드를 전용 AVD에서 확인했다. iOS는 같은 흐름을 Simulator에서 확인했고, 이후 앱 외부 UIKit controller 로그를 제외한 현재 빌드는 compile 및 startup snapshot까지 확인했다. 마지막 filter 변경 후 전체 조작 흐름 재실행은 남아 있다. 이 자료도 실제 회사 앱의 실행 가능한 재현 패키지로 표현하지 않는다.

검사: 전체 Python 291개 통과 후 controller filter와 Android close ownership을 보완했다. Android cleanup 관련 25개 검사도 통과했다. 새 기록 계약은 `docs/APP-LOG-CONTRACT.md`, 자료는 `artifacts/app-logs-*`다. 이전 절의 iOS 자동 계측 결과는 이전 단계의 이력이다.

## Previous Completed Milestone / iOS Automatic Instrumentation

- Self-hosted 모바일 테스트 플랫폼: 브라우저 조작 → 녹화 → 실제 AI 수정 → 검증 → 후보 앱 재개.
- 최신 요청은 **iOS도 앱에 로그 호출을 직접 심지 않고 수집하도록 구현**하는 것이다. `ios-instrument`가 별도 복사본의 Debug 빌드에 UIKit 런타임을 연결한다. 기존 제품 Swift/Objective-C 파일·Info.plist와 원본 프로젝트는 보존한다.
- SDK 호출·Report 버튼이 없는 저장소 소유 합성 앱으로 counter/duplicate-submit/reset/화면 왕복의 원본 대비 동작, 자동 SDK 진단, Release 제외, 실행 정보 불일치/background 거절을 실제 Simulator에서 검증했다.
- 실제 Claude 첫 제안 → 원본 3/3 → 보호 회귀 1개 → 수정본 3/3 → 새 Live count `1`까지 완료했다. 실제 요청의 prompt hash를 재구성해 자동 진단 포함도 확인했다. 최종 작업은 `artifacts/ios-auto-live-final-console/repairs/9e0e8c52bf294646ab7a0d7a9f1699b2`다.
- 검증용 Live 세션 2개·브라우저·서버를 종료하고 기기/lease를 반납했다. 이번 작업의 전용 Simulator도 종료·삭제했으며 다른 Simulator는 보존했다. `ios-auto-simulator.json`은 재실행할 기기가 아닌 종료 기록이다.
- 현재 지원은 고정된 ReproSample UIKit 프로필이다. 일반 앱, SwiftUI, 비동기 함수/네트워크/DB 내부 로깅, 이번 자동 계측의 물리 iPhone 검증까지 완료했다고 표현하지 않는다.
- 이 디렉터리는 Git 저장소가 아니다. branch/commit/PR 및 프로젝트 AGENTS.md 없음. 사용자 제공 전역 지침을 따른다.

## First Read / Commands

- [iOS UIKit 자동 수집](../docs/IOS-AUTO-INSTRUMENTATION.md): 명령, 동작, 증거, 지원 경계.
- [Android 기본 빌드 계측](../docs/BUILD-INSTRUMENTATION.md), [소스 삽입 옵션](../docs/AUTO-INSTRUMENTATION.md), [Android 앱 프로필](../docs/ANDROID-APP-PROFILES.md).

```bash
python3 scripts/prepare-ios-instrumentation-fixture.py --output artifacts/NEW_IOS_PLAIN
python3 -m reproof ios-instrument --source artifacts/NEW_IOS_PLAIN/source --output artifacts/NEW_IOS_PREPARED
python3 -m reproof ios-build --source artifacts/NEW_IOS_PREPARED/source --output artifacts/NEW_IOS_BUILD --simulator "$REPRO_SIMULATOR_ID"
python3 -m reproof ios-record --build artifacts/NEW_IOS_BUILD --case counter --output artifacts/NEW_IOS_RECORD --simulator "$REPRO_SIMULATOR_ID"
```

첫 명령은 검증용 합성 앱 생성이다. 실제 계측이 기존 SDK 호출을 삭제하는 것은 아니다. 출력은 항상 새 경로를 사용한다. Live helper는 현재 `live-ios` 소스로 빌드하고, 동일 preparation의 source/build를 전달한다.

## Key Files / Contracts

- `reproof/ios_instrumentation.py`: 고정 profile, 별도 준비 복사본, PBX 프로젝트 연결, Debug/Release 설정, provenance 및 capture 진단 validator.
- `reproof/ios_instrumentation_templates/{RLAutomaticRecorder.swift,RLAutoBootstrap.m}`: 앱 시작 후 자동 bootstrap, 실제 UIApplication `sendAction`의 저장된 IMP 호출, 입력 완료/탭/뒤로 이동 전후 숫자·화면 수집. 제품 callback은 같은 인자로 한 번 호출하고 BOOL·같은 Objective-C 예외를 보존한다. collector 오류는 제품 동작에 전달하지 않는다.
- `reproof/{ios_build,ios_storage}.py`: preparation/source/embedded profile/build/product 연결. bundle 진단을 scenario digest에 포함한다. Release는 런타임 소스와 profile 제외.
- `reproof/{ios_runner,ios_device}.py`: 실행 UUID·profile·build·fixture·session·endSequence를 고정해 컨테이너 자료를 수집한다. 처음 검증한 marker 전체를 수집 후에도 비교한다.
- `ios/ReplayTests/ReproReplayTests.swift`, `live-ios/Tests/LiveControlTests.swift`: 준비 완료 후 입력하고 Darwin notification으로 자동 수집을 확정한다. 기존 Report 버튼 경로는 수동 SDK에 유지한다.
- `reproof/live/{providers,iphone,cli,repair_jobs}.py`: Simulator/물리 기기의 자동 수집 연결, 실제 AI 진단 전달, 후보의 보호 소스·프로필·반복 검증 확인.
- `reproof/live/model.py`: `close_session(..., reserve_for_repair=True)`는 provider 종료 동안 lab.lock을 놓고 기기를 busy로 유지한다. 종료 확인 후 repairing으로 원자적으로 전환하므로 native HTTP bridge가 끝나면서도 기기 재할당 틈이 생기지 않는다.
- `tests/test_ios_auto_*.py`, `tests/test_ios_instrumentation.py`: 컴파일·실제 Objective-C dispatch/예외 harness, 준비/빌드/수집의 거절 경로. `tests/test_live_repair.py`는 다른 스레드의 마지막 bridge 요청과 기기 예약/취소를 검증한다.

수집은 Debug + record + 일치하는 UUID/고정 profile/fixture/build에서만 시작한다. 빈 값/QA/Test와 9자리 이하 숫자만 기록하며 임의 인자·예외 메시지를 수집하지 않는다. 한 active key window와 지정한 UIControl의 단일 target/action/touchUpInside를 요구한다. 최대 500 events/actions, 20MiB, 10분이다. background·지원하지 않는 상태는 완전한 capture로 내보내지 않는다.

준비 복사본은 OS 샌드박스가 아니다. 지원 suffix의 공개 iOS 입력만 복사하며 assets/storyboard/외부 패키지 등 모든 빌드 입력을 지원하지 않는다. 프로젝트 pbxproj는 plist로 다시 직렬화한다. UI ID/fixture/oracle/회귀 테스트 없이 임의 업무 의미를 추론하지 않는다.

## iOS Verification / Artifacts

- `artifacts/ios-auto-validation.json`: 현재 코드·원본 보존·프로필·실제 수집·AI·반복 검증·Release·정리의 종합 증거.
- `artifacts/ios-auto-live-final/{result.json,frame.jpg,prompt-proof.json}`: 현재 후보 source/build/profile/설치 hash 연결, 새 세션 count1, 실제 요청 진단 포함.
- `artifacts/ios-auto-live-final-console/repairs/9e0e8c52bf294646ab7a0d7a9f1699b2/repair/report.html`: 실제 수정 보고서, 한 줄 CounterLogic 2→1 변경.
- `artifacts/ios-auto-live-final-before.png`, `ios-auto-live-final-after.png`: 실제 브라우저 2→1.

- 전체 Python 270 tests passed (28.618s), 마지막 handoff 잠금 보완 후 관련 Live 132 tests passed (17.292s). 변경되지 않은 나머지 결과는 재사용한다. 웹 코드는 변경하지 않아 이전 Node 15 tests 결과를 재사용했다.
- `artifacts/ios-auto-plain-fixture/source`, `ios-auto-plain-build`: 수동 SDK가 없는 원본과 Debug 빌드.
- `artifacts/ios-auto-prepared/{source,auto-profile.json,patch.diff,instrumentation.json}`: 제품 입력 보존 및 자동 연결. prepared source digest `c722366a675ba9ff55fd33c3b4a21f87cd72b9fb7443a14e3bd499b3addea96b`.
- `artifacts/ios-auto-built`: 현재 런타임의 실제 Debug 앱·원본 replay/logic 테스트. profile digest `9945d4fbf9ded0d23476bc1c0247b595404c7863b83f6d5629946a0cae8e2d83`.
- `artifacts/ios-auto-counter-record/bundle`: 앱의 Report UI 없이 실제 capture 2 events, 전후 진단 0→2.
- `artifacts/ios-auto-behavior-qa/result.json`: 네 흐름 원본/계측본 일치. 각 bundle에서 중복 0→1→2, reset 0→1→1, navigation main→details→main을 확인한다.
- `artifacts/ios-auto-release-{original,automatic}`와 `ios-auto-release-isolation.json`: 실제 Release 빌드, Info 및 Mach-O 자동 profile/bootstrap/collector 부재. Debug에서 동일 검색의 positive control 통과.
- `artifacts/ios-auto-safety-qa.json`: 잘못된 profile/fixture·replay mode는 SDK 무기록 및 marker 거절, 양쪽 Release는 record env에서도 무기록, background 후 export 거절.
- `artifacts/ios-auto-live-verified/repairs/31ecdabbeb144f12bf3c461de9d99b13`은 자동 수집 성공 후 handoff 잠금 충돌로 중단된 이전 진단 자료다. 실제 Claude 요청 전 실패했으며 최종 성공 근거로 쓰지 않는다.
- 전용 Simulator는 `artifacts/ios-auto-simulator.json`의 name/public hash/owned로 식별한다. raw UUID를 출력하지 않는다. 설치된 iOS27.0 beta와 Xcode27 beta를 사용했고 전역 Xcode 선택을 바꾸지 않았다.

## Previous Android / Physical Work

Android 기본 `instrument`는 build 모드다. 기존 제품 Kotlin/Java/XML/manifest를 보존하고 별도 플러그인으로 컴파일된 Activity 클래스의 지원 지점에 hook을 넣는다. `--mode source`는 이전 삽입 옵션이다. 실제 Claude→원본3/3→회귀1→수정본3/3→새 Live count1, Release runtime/hook/no-op 부재까지 완료했다.

- 최종 Android 종합: `artifacts/bytecode-validation.json`; 빌드 `bytecode-final-prepared`/`bytecode-final-built`; Live `bytecode-live-final`. 해당 전용 AVD와 세션은 종료·삭제 완료.
- source 옵션: `artifacts/instrumentation-validation.json`; 일반 프로필 합성 앱: `android-profile-validation.json`.
- 이전 물리 기기 검증: `android-live-repair-validation.json`, `iphone-repair-cases/{index.html,validated-summary.json}`, `iphone-live-repair-final`. iPhone의 이전 증거는 수동 SDK 경로이며 이번 자동 계측의 실기기 증거가 아니다.

## Authorization / Avoid / Next

- 기존 승인: 저장소 샘플 소스·합성 QA의 실제 Claude 요청, 고정 샘플의 Xcode 서명/프로파일/Keychain 및 필요 시 Apple provisioning, 고정 Android 의존성의 Google Maven/Maven Central/Gradle Plugin Portal 다운로드. 새 비공개 앱 소스/QA나 새로운 네트워크 대상은 별도 판단한다.
- 비밀·쿠키·raw serial/UDID 출력 금지. `.env`, auth, 키스토어 등은 사용자 지침을 따른다. 글로벌 도구/모델 설정을 바꾸지 않는다.
- 전용 검증 기기만 사용·종료·삭제하며 name/public hash/owned를 먼저 확인한다. 다른 작업의 Simulator/AVD를 건드리지 않는다.
- 다음 기능 확장은 실제 대상 앱의 빌드 입력·UI ID·fixture·profile·편집/회귀 계약부터 확인한다. Compose/SwiftUI, async/network/DB, packaging, 다중 사용자/외부 worker/재시작 복구/Appium 전체 호환은 별도 범위다.
