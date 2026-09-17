# iOS 고정 서명 실행과 독립 검사

2026-09-14 · D4 진행 중

`IOSSigningOperationStore.sign()`과 `inspect()`는 준비 저널에 실제 CMS 검증,
메모리 키 서명, 독립 코드 검사와 정리를 연결한다. `IOSSigningOwnerSigner`와
`IOSSigningOwnerInspector`는 이 경로를 기존 `TrustedSigningSupervisor`에 전달하는
서로 다른 고정 callback이다. 운영 코드는
`ProtectedRepairComposition.configure_ios_signing_owner()`로 같은 authority의
살아 있는 build supervisor와 조합할 수 있다.

실행기 구성 도중 `KeyboardInterrupt`나 `SystemExit`가 발생하면 부분적으로 만든
서명기·검사기·작업 저널을 닫은 뒤 원래 중단을 전달한다. 아직 조합에 넘기지 않은
서명 재료는 호출자 소유로 유지한다. Android의 고정 JVM 서명 조합도 같은 규칙을 따른다.

## 실행 순서

1. 일반 앱 IPA를 준비하고, 정책에 등록한 profile만 앱/extension에 넣는다.
   허용한 profile 외 코드·리소스가 바뀌면 거절한다.
2. 명시적 CMS issuer/anchor와 선택 기기·prefix·entitlement 정책으로 profile을
   검증한다. 같은 준비 앱 digest를 다시 확인한 뒤에만 키를 연다.
3. 별도 native 소유자가 상속된 PKCS#12 descriptor와 비밀번호 pipe를 사용해
   서명한다. 메모리 전용 import와 명시적 인증서 체인을 사용한다.
4. 변경된 앱을 제한된 IPA로 내보내고, native 종료와 작업 사본 정리를 확인한다.
5. supervisor가 새 nonce를 발급하면 독립 검사 callback이 그 문맥으로 signed IPA를
   다시 준비한다. profile을 다시 확인하고 모든 코드 객체·architecture의 서명,
   인증서·team·entitlement를 별도 native 프로세스에서 검사한다.
6. 검사 프로세스의 종료와 사본 정리 후에만 서명된 빌드 증거를 반환한다. 이것은
   후보 앱의 모바일 회귀 검증이나 전체 이슈의 `verified` 결과가 아니다.

## 검증 프로세스의 소유권

`native/ios-process-guardian/main.c`는 고정 Python 검증 어댑터가 만든 private 명령
배열을 실행한다. 시작 프로그램은 `sandbox-exec`로 제한하며, 이 명령 배열은 이슈·
후보·AI 또는 공개 JSON 플러그인에서 선택하지 않는다. 검증 자식은 fork를 금지한다.
서명 키는 이 guardian에 전달하지 않는다.

guardian은 producer/owner 잠금을 유지하고 stdout·stderr를 제한해 별도 private
결과 파일에 담는다. 부모가 사라지면 자신이 소유한 자식을 종료하고 `waitpid`로
수집한 뒤 종료한다. 자식 PID의 신호와 수집은 같은 native mutex로 직렬화하며
수집한 PID를 다시 신호 대상으로 쓰지 않는다. guardian 자체가 강제 종료되는 경우를
위해 자식도 원래 잠금 descriptor를 이어받는다. 현재 고정 OpenSSL의 descriptor
유지는 실제 FIFO 대조 시험으로 확인했다.

Python 소유자는 원래 제어 파일, 문맥·요청·정의 digest, 실제 프로세스 종료와
프로세스 그룹의 부재를 확인한다. 종료 의향 JSON만으로 비용을 해제하지 않는다.
일찍 반환한 callback, 종료하지 않은 자식 또는 불명확한 정리는 quarantine과
예약을 유지한다. 로그·결과에 키·비밀번호·profile 본문을 넣지 않는다.

## 용량과 전송

기존 준비 예약 안에서 고정 서명 형식의 CMS/인증서, XML·DER entitlement, 페이지
hash와 리소스 기록의 보수적인 크기 상한을 계산한다. 같은 OS 형식의 상한을 앱
크기 제한이나 현재 예약이 수용하지 못하면 키를 쓰기 전에 거절한다. native 결과의
실제 크기도 다시 확인한다. 다른 OS/toolchain의 수용 검증을 대신하는 수치는 아니다.
앱이 `CFBundleResourceSpecification`으로 자체 리소스 서명 규칙을 선택하는 경로는
Python의 키 사용 전 검사와 native 서명기에서 거절한다. 기본 OS 리소스 형식으로
고정한 용량·검증 경계를 후보가 바꾸지 못하게 한다.

IPA는 최대 64 MiB의 제한 writer로 만든다. 원래 파일 inode·hash를 대조하며 파일을
읽고, 고정된 ZIP metadata와 `Payload/App.app` 경로를 쓴다. 전체 확장 앱 상한은
512 MiB다. 이 전송 경로는 IPA의 기존 symlink 거절 정책을 유지한다. 원래 IPA와
작업 사본을 유지하는 시점, native 임시 사본, 검사기 사본을 예약 안에 포함한다.

## 구성과 검증 범위

`IOSSigningOwnerTools`에 native signer·verifier 외에 guardian 경로와 SHA-256을
등록하면 native 실행용 저널 구조를 사용한다. guardian 없는 기존 구조는 준비/
복구용으로 남으며 실제 서명을 시작하지 않는다. `IOSSigningProvisioning`은 명시적
CMS 도구·trust·prefix·선택 기기를 고정하며 supervisor의 정의 digest에도 연결한다.

자체 UIKit 앱·자체 인증서/profile의 실제 서명과 독립 검사, 잘못된 비밀번호,
변조된 signed IPA, 다른 provisioning 구성, 키 사용 직전 앱 변경, 실제 부모 종료와
새 프로세스의 복구를 검사했다. 서비스 조합 검사는 VM 부분에만 명시적인 대역을
사용했다. 실제 Apple 인증서/profile·회사 앱·보호 VM·실기기 수용 검사는 아니다.

공개 CLI의 iOS 도구 빌드·status/recover는 [운영·복구 문서](IOS-SIGNING-RECOVERY.md)를
따른다. 복구 구성은 키·profile·도구를 읽지 않으며 서명 실행 권한을 만들지 않는다.
전체 보호 서비스의 공개 시작 구성과 실제 환경 수용은 다음 단계다.
[D4 r4 개발 wheel](../artifacts/product-delivery/d4-foundation-package-r4/acceptance.json)의
새 설치에서 테스트 모듈 없이 실제 CMS·서명·독립 검사·정리를 확인했다. 과거 r2
wheel에는 이 실행 경로가 없다.
