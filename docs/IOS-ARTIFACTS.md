# iOS 산출물 입력 경계

2026-09-13 · D4 개발 파서. 서명·기기 전송 통합은 진행 중이다.

`parse_ios_artifact()`는 명시적으로 선택한 `.app` 디렉터리 또는 `.ipa` 파일을
읽어 `IosBundleCapability`를 발급한다. 앱 코드, `codesign`, CMS/Keychain 도구를
실행하지 않는다. 이 capability는 현재 프로세스가 입력을 구조적으로 검사했다는
뜻이며 Apple 서명·provisioning·실기기 설치 유효성을 증명하지 않는다.

앱은 최대 512 MiB·100,000개 항목·512개 코드 객체로 제한한다. 일반 실행 `BlobSet`의
64 MiB 한도는 바꾸지 않는다. Info.plist와 Mach-O 헤더/load command를 제한된 크기로
읽고, 정확한 bundle ID/version/build·실행 파일·framework/extension 목록을 만든다.
링크 없는 파일 경계와 소유권을 검사하며, `.app`의 제한된 versioned-framework
symlink만 처리한다. 링크 순환·경로 탈출·case 충돌·특수 파일·위험한 Unix 모드는 거절한다.

IPA는 `Payload` 아래 앱 하나만 받는다. 먼저 크기가 제한된 비공개 사본을 만들고,
실제 central-directory 레코드 수와 끝 위치를 ZIP 객체 생성 전에 검사한다.
EOCD의 거짓 개수, directory 위치의 틈, 다중 디스크·ZIP symlink·과도한 압축 비율은
거절한다. 고정 ZIP64 end record는 지원하며 확장된 end-record 형태는 거절한다.
검사 중 원본을 교체하거나 수정하면 최종 capability를 발급하지 않는다.

IPA 임시 저장에는 압축 사본과 추출본 각각 입력 한도만큼, 기본 최대 합계 1 GiB가
필요할 수 있다. 호출 뒤 임시 저장은 정리한다. 향후 전송·서명 조합에서 이 용량을
영속 예산에 예약해야 하며 현재 파서는 해당 운영 조합을 대신하지 않는다.

공개 manifest는 `appDigest`와 `containerDigest`를 구분한다. 같은 앱의 `.app`과 IPA는
앱 내용 digest가 같아도 컨테이너 digest는 다르다. 빈 디렉터리·실행 모드와 앱/extension
루트의 embedded profile도 앱 내용 계산에 포함한다. embedded profile 경로·본문·
개인 정보는 공개 manifest에서 제외한다. 현재 검사는 자체 생성한 dummy profile을
사용했으며 사용자 provisioning profile을 읽지 않았다.

capability는 직렬화한 dict, dataclass 복제본 또는 다른 프로세스의 객체로 대체할 수
없다. 이 검사는 변경 가능한 원본 파일을 영구히 고정하지 않으므로, 후속 소비자는
실제 사용하는 사본의 내용과 앞서 확인한 digest를 다시 묶어야 한다. 특히 물리 iPhone의
설치된 bundle/version 조회만으로 읽지 못한 전체 앱 내용 digest를 주장하지 않는다.

[관련 검사 45개](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/parent-r5-green-final.log)가
통과했다. 파일 형식·바인딩·입력 한도와 기존 iOS 저장/관찰 회귀 근거이며, 실제 iOS
서명·provisioning 유효성이나 보호된 후보 재생 수용 검사는 아니다.

별도 `ios_provisioning_policy.assess_decoded_profile()`은 이미 디코딩한 profile의
제한된 공개 필드를 검사한다. 고정 profile projection digest·인증서 SHA-256·팀과
application identifier prefix·bundle ID·선택 기기·평가 시각·entitlement를 대조한다.
prefix와 팀 ID를 구분하고 wildcard의 점 경계, 만료, development/distribution 기기
조건과 값의 정확한 타입을 검사한다. 관련 정책·기존 신뢰 경계
[25개 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/provisioning-policy-parent-r2.log)가
통과했다.

이 순수 함수는 CMS 서명이나 Apple 인증서 체인을 검증하지 않고 Keychain·실제
profile 파일에도 접근하지 않는다. 임의 dict가 정책 조건을 만족하더라도 Apple이
발급한 profile이나 서명/설치 권한이 되지 않는다.

## 고정 CMS 검증기

`ios_provisioning_cms.IOSCmsVerifier`는 고정 hash의 OpenSSL과 `sandbox-exec`로
CMS 서명과 명시적으로 등록한 인증서 체인을 검증한다. `IOSCmsTrust`에는 profile
발급자의 정확한 DER 인증서와 신뢰 anchor를 전달한다. 이 값은 앱 개발자의 서명
인증서와 구분하며, 모듈이 Apple 발급자나 루트 값을 자동으로 선택하지 않는다.

입력은 최대 4 MiB의 DER SignedData이며, 내장 data 내용과 SHA-256/384/512 digest를
사전 검사한다. BER indefinite length, detached/encrypted 내용과 약한 digest는
거절한다. profile과 인증서는 상속한 descriptor로 전달하고, 출력 파일의 원래 inode와
내용 digest를 검사한다. 명시한 signer 외 인증서, 다른 root, 만료된 체인, 내용 변조는
거절한다. Native stderr나 decoded profile 본문은 공개 결과에 넣지 않는다.

자식 프로세스는 네트워크와 Mach lookup을 거절하고, 필요한 시스템 runtime과 작업
입력만 읽을 수 있다. 파일 metadata 조회는 허용하며 Keychain 내용은 읽지 않는다.
입력 밖에 둔 자체 인증서 파일의 읽기 거절도 실제 OS에서 확인했다. macOS의 현재
dyld가 필요한 OS Cryptex 경로는 허용 목록에 포함한다. 검사 중 시간 초과와
KeyboardInterrupt는 소유한 native 프로세스를 수집하며, 정리가 불명확하면 작업
파일과 소유 정보를 유지한다.

반환한 `VerifiedCmsProfile`은 해당 verifier의 살아 있는 객체여야 한다.
`assess_profile()`은 그 내용과 평가 시각을 위의 순수 provisioning 정책 검사에
연결한다. 보관하는 profile 내용은 합계 64 MiB로 제한하며 `release_profile()`과
`close()`가 capability를 무효화한다. 이 객체는 앱 코드의 서명·설치·기기 격리 권한이
아니다. 실제 Apple profile/발급자 등록과 고정 앱 서명기·전송·복구 조합은 남아 있다.

[관련 32개 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/cms-verifier-green-r10.log)와
[test 모듈 없는 실제 API 실행](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/native-cms-trust-r2/result.json)을
확인했다. 자체 생성한 인증서와 profile만 사용했고 개인키·임시 작업을 정리했다.
실제 Apple 발급 profile이나 회사 앱의 수용 근거로 표시하지 않는다.

## 앱·extension과 profile의 연결

`ios_artifact_provisioning.parse_provisioned_ios_artifact()`는 같은 파싱 과정에서
앱과 각 extension의 bundle ID·CMS 바이트를 비공개로 캡처한다. IPA도 검사한 임시
사본 안에서 캡처하고 추출 디렉터리를 정리한다. 본래 `parse_ios_artifact()`의 공개
manifest에는 profile 본문이나 경로를 추가하지 않는다. 캡처는 profile마다 4 MiB,
합계 64 MiB이며 한도를 넘으면 profile 바이트를 보관하기 전에 거절한다. 없는
profile은 해당 bundle에서 명시적으로 누락 상태를 유지한다.

`IOSArtifactProvisioningVerifier`는 고정 발급자/anchor, 앱 서명 인증서 SHA-256,
팀·prefix·선택 기기와 bundle별 profile/entitlement 정책을 소유한다. 캡처된 모든
앱·extension의 경로와 bundle ID가 정책과 정확히 같아야 검증한다. 일부 profile만
통과한 결과는 발급하지 않는다. 정책 입력의 나중 변경, 다른 앱 산출물·실행 context,
복제한 결과는 기존 결과의 권한을 바꾸지 못한다. 결과 해제·수거·종료는 소유한
decoded profile 참조와 capability를 정리한다.

관련 [56개 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/artifact-cms-green-r2.log)가
통과했다. 별도 [실제 API 실행](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/native-cms-app-r1/result.json)은
앞서 빌드한 자체 UIKit Simulator 앱의 사본에 자체 CMS profile을 넣고 `.app`과 IPA의
같은 앱 digest·provisioning 정책·원본 코드/리소스 보존·정리를 확인했다. test 모듈은
사용하지 않았다. 이 실행은 실제 Apple profile 검증, 앱 코드의 서명 또는 설치를
수행하지 않았다. 후속 서명·설치 소비자는 실제 사용할 앱 사본도 같은 artifact
digest에 고정해야 한다.

## Native 소비자를 위한 사본과 코드 서명 검사

`ios_artifact_staging.stage_ios_artifact()`는 선택한 parser capability와 내용이 같은
새 `.app` 디렉터리를 만든다. 파일은 원래 inode·크기·hash를 확인하며 descriptor로
복사하고, 실행 비트·framework 링크·profile을 보존한다. 원본과 겹치는 출력, 기존
디렉터리와 부모 경로의 symlink는 거절한다. 완성한 사본의 앱 digest를 다시 대조한
뒤에만 새 capability를 반환한다. IPA 입력의 container digest는 원래 입력에 남고,
새 사본은 `.app`의 container digest를 갖는다.

이 함수는 파일 준비용이며 실행 예약이나 비용 해제 권한이 아니다. 실패한 부분
출력은 호출자가 소유하고 정리해야 한다. 보호 실행은 호출 전에 실제 저장 용량을
예약해야 한다. IPA는 압축 사본·추출본·최종 사본 각각 최대 512 MiB가 동시에 필요할
수 있어 논리 바이트 기준 최대 1.5 GiB와 filesystem 여유 공간을 고려한다.

`ios_code_signature.IOSCodeSignatureInspector`는 이 사본에 고정 네이티브 검사기를 적용한다.
모든 code object와 universal Mach-O의 각 architecture에서 엄격한 서명 검증,
CodeDirectory identifier와 정확한 entitlement 집합을 검사한다. `identity` 모드는
embedded leaf 인증서 SHA-256과 team을 요구하고, `adhoc-simulator` 모드는 실제
Simulator 플랫폼 표시가 있는 artifact로 제한한다. 이 검사는 provisioning이나
실기기 설치·격리 권한을 대신하지 않는다.

`adhoc-simulator`는 고정 `codesign`을 사용하며 Keychain 내용, 네트워크와 Mach lookup을
거절한다. `identity`는 `native/ios-code-verifier/main.c`로 만든 실행 파일과 SHA-256을
`IOSCodeSignatureTools.verifier`·`verifier_sha256`에 추가로 요구한다. 누락된 구성은
작업 디렉터리를 만들기 전에 거절한다. 공개 Security API로 모든 architecture·nested
code·resource를 검증하고, 인증서 만료 검사와 `kSecCSNoNetworkAccess`를 고정한다.

인증서 모드에는 로컬 `com.apple.trustd.agent` IPC만 허용한다. 검증 프로세스의
네트워크와 Keychain 파일 내용 접근은 계속 차단한다. 검토한 Apple 구현은 CMS 검색
목록을 비우고 해당 SecTrust의 Keychain 인증서 검색을 끈다. 인증서와 entitlement
조회도 이미 검증한 같은 객체에서 수행하며, CFDictionary에 추가되는 호환용 alias
대신 검증된 entitlement blob을 읽어 정확한 정책과 대조한다. 인증서 본문은
임시 파일이나 공개 결과에 남기지 않고 SHA-256만 반환한다.

취소·시간 초과·종료 중
늦은 작업은 새 proof를 만들지 못한다. 정리는 열린 원래 디렉터리 안에서 수행하며,
경로가 다른 디렉터리로 교체됐으면 그 내용을 삭제하거나 정리 완료를 보고하지 않는다.
큰 앱의 준비 용량을 보호 signing/mobile 실행 저널에 연결하는 작업은 남아 있다.

현재 [70개 관련 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/staging-code-inspector-green-r3.log)와
[test 모듈 없는 실제 검사기 실행](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/code-inspector-api-r3/result.json)이
통과했다. 자체 UIKit 앱의 ad-hoc 서명과 x86_64·arm64 검사, 서명된 페이지·리소스
변조 거절, 원본 보존과 정리를 확인했다. 이 자료는 앞선 ad-hoc 검사 근거다.

별도 자체 PKCS#12 실험에서는 SDK의 `kSecImportToMemoryOnly`를 사용해 Keychain에
저장하지 않고 private key 연산을 수행했고, 별도 OpenSSL로 서명을 확인했다.
[메모리 identity 실험](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/memory-identity-r3/result.json)은
앱 코드 서명을 수행한 결과가 아니다.

승인받은 Apple 공개 소스에서 명시적 인증서 체인과 서명 함수를 받는
`SecCodeSignerRemote` SPI를 확인했다. 고정된 로컬 함수가 메모리 키로 서명하므로
이 실험에는 외부 서명 서비스가 없다. [실제 앱 서명과 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/memory-app-signing-r8/result.json)는
자체 UIKit 앱·자체 PKCS#12로 두 architecture의 서명 생성, production inspector,
다른 인증서·팀·entitlement 거절, 코드·Info.plist 변조 거절과 원본/임시 키 정리를
확인했다. 서명 실험은 현재 artifact prototype이며 영속 서명 소유자·복구 API는 아니다.
실제 Apple 인증서/profile, 회사 앱, 기기 설치·보호 qualification은 이 결과에 포함되지 않는다.

공개 검사기 소스는 `reproloop export-resources --output-new <새 절대 경로>`로
설치본에서도 꺼낼 수 있다. 등록된 macOS SDK의 clang으로 다음 소스만 컴파일하고,
생성한 실행 파일의 승인된 SHA-256을 tools 구성에 고정한다.

```sh
/usr/bin/xcrun --sdk macosx clang -Wall -Wextra -Werror \
  /absolute/export/native/ios-code-verifier/main.c \
  -framework Security -framework CoreFoundation \
  -o /absolute/new-code-verifier
```

[공식 소스 검토](../artifacts/product-delivery/d4-protected-adapters-r1/official-source-r1/findings.md)에
API 출처와 한계를 기록했다. 자체 AIA HTTP 시험은 온라인 허용 대조에서도 요청을
만들지 못했으므로 네트워크 격리의 측정 근거로 채택하지 않았다. 이 검사기는 온라인
폐기 상태의 최신성이나 mobile/backend 격리를 증명하지 않는다.
