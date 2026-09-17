# D4 고정 실행기와 운영 복구

2026-09-15 · D4 실환경 수용 대기

현재 G9의 감독자·검증 계약에 서비스 소유 조합, 고정 Android 서명기, 유지되는 기기
예약을 연결하고 있다. [제품 경로](PRODUCT-DELIVERY-PLAN.md)의 D4는 진행 중이다.
이전 D3 wheel을 보존하고, 현재 개발 변경은 별도
[D4 개발 wheel](../artifacts/product-delivery/d4-foundation-package-r4/acceptance.json)로
빌드·설치 검증했다. 전체 제품 및 실제 보호 환경 수용의 완료판은 아니다.

## r51 현재 구현 범위

[고정 iOS 보호 서비스](IOS-PROTECTED-SERVICE.md)에 명시적 앱 초기화, 후보 3회 재생,
원본 복원, native 재시작 복구·fixture 정리·예약 반환과 인증 복구 CLI를 연결했다.
초기화·helper 종료는 자체 Simulator에서, 감독자·재시작/실패 경로는 실제 저장소·
권한 객체와 명시적 SDK/HTTP 대역으로 검증한다. 아래 초기 개발 이력의 미구현 표시는
이 후속 iOS 범위에 대해서는 이 문단과 링크된 API 문서가 우선한다.

실제 mobile 격리 probe는 운영 기기·네트워크/backend 환경의 고정 조작·관측 입력이
필요하다. JSON qualification이나 helper handshake를 실제 격리 측정으로 취급하지 않는다.
정상 실행 service factory는 살아 있는 qualification을 받으며,
`live-serve --protected-recovery-config`는 Android/iOS의 복구 전용 진입점이다.

## 구현된 기반

- `ProtectedRepairComposition`은 같은 프로세스의 qualification 권한을 유지한다.
  `qualify_build()`가 고정 VM probe를 실행하며, 서비스 종료는 먼저 권한을 회수하고
  probe와 이슈 작업의 정리를 기다린다. 종료가 불명확하면 소유 정보를 남겨 재시도한다.
- `compose_issue_workflow(..., defer_repairs=True)`로 실제 runtime을 만든 뒤 고정 로컬
  executor를 등록하고 `compose_issue_repairs()`로 연결한다. `protectedProfileId`는
  이미 등록한 프로필만 선택한다. 서비스·runner·프로젝트·기기 배정·원본 앱 식별이
  다르면 작업을 등록하지 않는다. CLI에서 이 조합을 만드는 경로는 아직 없다.
- `configure_android_signing_owner()`는 같은 권한의 살아 있는 build 감독자에 고정
  JVM 서명·독립 검사기와 영속 `SigningOperationStore`를 연결한다. 실제 캐시된 SDK와
  자체 임시 키로 단일 APK·패키지·인증서·서명 방식·permission 집합을 검사했다.
  서명 재료는 상속된 descriptor로만 전달하고, 서비스는 늦은 콜백이 끝나기 전까지
  재료와 소유 정보를 보존한다. 같은 인증서의 다른 참조 ID가 이전 저널을 우회할 수 없다.
- 공개 native 소스에서 서명 소유자를 오프라인으로 만드는 `android-signing build-tools`,
  기존 작업을 읽는 `android-signing status`, 실제 정리 뒤 실패/취소로 마치는
  `android-signing recover`를 일반 CLI에 연결했다. 새 설정이 빈 대체 저널을 만들거나
  JSON 상태만으로 비용을 해제하지 않는다. 별도 설치본의 실제 도구 빌드·서명·중단
  복구도 확인했다.
- Lab의 retained scope는 개별 이슈 세션이 끝나도 canonical 기기 예약을 유지한다.
  설치·복원과 세션이 같은 authority sequence를 사용한다. 동시 세션·다른 앱·만료된
  권한은 fixture 변경 전에 거절한다. 고정 Android 어댑터는 일반 앱 프로필과 실제
  service/runner/registration을 검사한다. 전용 AVD에서 변경하지 않은 원본을 대조 후보로
  설치하고 같은 결함을 세 번 관찰한 뒤, 원본 APK·앱 데이터·fixture·프로세스 정리와
  서비스 종료 후 기기 반환까지 확인했다. 이 실행은 mobile isolation qualification이 아니다.
- `configure_android_mobile()`은 같은 서비스의 고정 어댑터와 영속
  `AndroidOperationStore`를 조합한다. 실제 세 APK 크기와 메타데이터를 예약하고,
  native generation·설치·각 재생·정리 phase를 묶는다. 설치 전 취소도 사본을 정리하며
  마지막 저널 기록까지 끝나야 예약을 해제한다. 실제 OS 기기 복구는 아직 남아 있다.
  [기기 작업 저널](ANDROID-MOBILE-OPERATIONS.md)에 API와 대역 검증 범위를 기록했다.
- 실행 저널은 VM·서명·기기 도메인과 물리 scope를 고정한다. VM 복구는 같은 스레드와
  RunStore가 보유한 machine lease를 요구한다. VM 종료 파일로 서명·기기 quarantine을
  해제할 수 없다. 서명 복구 API/CLI는 원래 inode의 producer/phase 잠금과 정리를
  확인한 일회성 capability만 받는다. 기기 작업 저널의 읽기 전용 복구 검사는 구현했으며,
  실제 기기 종료·sanitation과 예약 해제 및 운영 CLI 연결은 아직 남아 있다.
- 설치·재생의 확인된 실패는 `MobileFailureObservation`으로 구분한다. 이 경우도 마지막
  기기·fixture 정리까지 통과해야 비용을 해제하며, 후보 검증 성공은 발급하지 않는다.
- iOS `.app`/`.ipa`의 [제한된 산출물 파서](IOS-ARTIFACTS.md)는 코드·서명을 실행하지
  않고 앱/컨테이너 digest와 명시적인 코드 객체 목록을 만든다. ZIP의 실제 파일 목록을
  사전 제한하고 비공개 사본만 검사한다. 디코딩한 provisioning profile의 순수 정책
  검사와 명시적 발급자/anchor에 대한 고정 CMS 검증기도 추가했다. 자체 실제 인증서로
  내용 변조·다른 root·만료·취소와 정책 연결을 검사했다. 실제 Apple 입력 등록,
  앱 서명·실기기 전송·복구는 별도 통합이다. 같은 `.app`/IPA 파싱에서 모든 앱/extension의
  profile을 캡처하고 고정 정책과 실행 context에 묶는 경로도 연결했다. 자체 UIKit
  빌드 사본과 test 모듈 없는 API로 앱/IPA profile 검증·원본 보존을 확인했다.
- iOS 산출물을 같은 digest의 새 `.app`으로 준비하는 파일 primitive와 고정 코드 서명
  검사기를 연결했다. 모든 code object/architecture의 identifier·entitlement와 서명을
  대조한다. 자체 앱의 ad-hoc 두 architecture 검사와 변조·취소·정리를 확인했다.
  승인받은 Apple 소스를 검토한 뒤 메모리 키의 실제 앱 서명 prototype과 고정 offline
  네이티브 검사기를 연결했다. 자체 인증서의 두 architecture·정확한 entitlement·변조
  거절이 통과했다. 후속 영속 실행·복구 조합은 아래와 같으며 실제 Apple 재료 수용은 남아 있다.

iOS의 [영속 준비·복구](IOS-SIGNING-RECOVERY.md)와
[실제 서명 실행·독립 검사](IOS-SIGNING-EXECUTION.md)도 연결했다. 키 사용 전의 정확한
프로필 검증, 부모 종료 시 native 자식 수집, 새 nonce로 하는 독립 검사, signed IPA
전송과 한 번만 소비하는 복구 권한을 사용한다. `configure_ios_signing_owner()`는
같은 authority의 살아 있는 build supervisor를 요구한다. 실제 부모 종료·새 프로세스
복구 및 서비스 factory의 고정 callback을 검사했다. VM 부분의 대역과 실제 native
암호 연산을 구분한다. 공개 `ios-signing build-tools/status/recover`와 복구 전용
구성도 연결했다. 실제 native 부모 종료 뒤 키·profile·도구 읽기를 OS에서 막은
새 CLI가 원래 저널을 정리하는 경로를 검사했다. 공개 보호 서비스 시작 구성과
실제 Apple/회사/VM/기기 수용은 남아 있다.

[보호 서비스 공개 설정 검사](PROTECTED-SERVICE-CONFIGURATION.md)는
`protected-service check-config`와 실제 issue runtime의 metadata 검사를 제공한다.
원본 빌드 ID를 명시하며 다른 등록 빌드를 원본으로 채택하지 않는다. 참조 내용의
검사 뒤 [VM·서명 입력 로더](PROTECTED-SIGNING-INPUTS.md)로 실제 번들·도구·서명
정의를 읽을 수 있다. mobile/독립 관찰자 입력·실제 실행기 초기화와 `live-serve`
시작 조합은 다음 구현 단계다.

[r14 검증 기록](../artifacts/product-delivery/d4-protected-adapters-r1/foundation-progress-r14.json)은
CLI 추가 전의 전체 검사와 해당 로그·소스 hash를 연결한다. 명령의 잘못된 테스트 모듈명과
새 machine lease 계약을 적용하지 않았던 테스트 실패도 원본 로그에 남겼다. 실제
VM qualification, 보호된 후보의 기기 검증, 회사 서명·환경 수용을 뜻하지 않는다.

별도 JVM 서명 소유자의 [중단·복구 API/CLI](ANDROID-SIGNING-RECOVERY.md)는 종료 의향
파일과 실제 프로세스 종료를 구분한다. 실제 서명·독립 검사 뒤 부모 Python을 종료하고,
새 CLI 프로세스에서 잠금·정리를 확인해 남은 비용을 해제하는 실행도 통과했다.

Android의 다섯 mobile probe를 실제 측정하는 production runner는 아직 없다. 기기
예약·helper handshake만으로 네트워크와 backend 격리를 증명할 수 없으며, 등록된
격리 환경을 조작하고 관측하는 고정 실행기를 서비스 구성에 연결해야 한다.

## 구현 순서

1. 고정 운영 어댑터와 신뢰된 Python 조합 경계를 만든다. 하나의 살아 있는
   `QualificationAuthority`에서 VM bundle·실행 계획·정책·경로를 고정하고 실제
   qualification을 측정한다. 명령·모듈·callback·credential·`qualified` 값을
   받아 실행하는 JSON 플러그인은 만들지 않는다.
2. 공유 이슈 조합에 프로젝트 digest별의 정확한 로컬 executor를 전달한다. 같은
   runner·프로젝트·앱·빌드/회귀 recipe·기기·runtime policy를 기존 계약으로 검사하고,
   서비스 종료는 자신이 구성한 자원만 닫는다. 설정 로딩만으로 실행 권한을 발급하지 않는다.
3. 단일 APK의 Android 서명부터 연결한다. 고정 `apksigner`와 별도 검사기를 사용하고,
   입력·출력 SHA-256, 인증서·패키지 식별, 출력 한도와 프로세스 종료를 확인한다.
   서명 재료는 운영자가 주입한 불투명 참조로만 받으며 이슈 JSON이나 로그에 넣지 않는다.
4. Android 설치 전부터 같은 원본의 후보 3회 재생과 마지막 정리까지 canonical 기기
   lease를 유지한다. G4의 입력·fixture·독립 관찰을 재사용하되 개별 replay 사이에
   기기를 반환하지 않는다. 기기·네트워크·backend·프로세스 종료·상태 정리의
   실제 측정이 없는 어댑터에는 qualification을 주지 않는다.
5. iOS는 signed app/IPA의 제한된 전송 형식과 provisioning·entitlement 검사를 먼저
   정한다. 전역 Keychain/profile 검색을 재사용하지 않고 명시적 참조를 받는다.
   설치 후 bundle/version 확인만으로 읽을 수 없는 전체 artifact digest를 주장하지 않는다.
6. 서명·기기 작업의 영속 저널과 `status`/`recover` 운영 경로를 추가한다. 복구는 원래
   작업·요청·scope·프로세스·기기·정리 경로를 확인하고 실제 종료·sanitation을 증명한다.
   불명확한 상태는 quarantine과 비용을 유지한다. 복구는 작업을 다시 실행하거나
   후보 검증을 발급하지 않는다. VM 전용 `RunStore.reconcile()`로 서명·기기를 해제하지 않는다.

## 완료 근거

새 설치에서 test 모듈 없이 조합되는 경로, 잘못된 설정·도구·artifact·프로젝트·기기
거절, 취소·시간 초과·동시 소유권, 서명/설치/재생/정리 도중 실제 프로세스 종료를
검사한다. 기존 원본·명세·소스 바인딩과 독립 회귀 판정은 유지한다.

운영 어댑터의 구현·실패 경로 검사는 외부 환경 없이 진행할 수 있다. 실제 보호
검증은 소유 VM 이미지·오프라인 toolchain, 승인된 서명 재료와 기기·네트워크/backend
격리 입력을 받아 같은 서비스 프로세스에서 측정해야 한다. 회사 QA·외부 AI 전송·
두 Mac의 물리 기기는 D5 수용 조건으로 계속 남긴다.
