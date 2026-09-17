# 보호 빌드·서명 입력 로딩

2026-09-14 · D4 진행 중

공개 설정과 실제 issue runtime의 preflight 이후
`load_protected_build_signing_inputs(configuration, issue_configuration, runtime_bundle)`가
VM 번들·고정 서명 도구·서명 정의를 읽는다. RunStore·VM·키·기기 실행기는 만들지 않는다.
mobile 입력과 독립 관찰자 초기화는 아직 별도 남은 단계다.

## VM과 도구

`load_protected_tool_inputs()`는 `GuestBundle.load()`와 기존 Android/iOS 도구 manifest
로더를 사용한다. 실제 환경 digest·recipe·artifact/cleanup 정책을 대조한다.
출력은 Android의 `candidate.apk` 또는 iOS의 `candidate.ipa` 하나여야 한다.
원본·프로젝트·정책·기기 배정을 파일 읽기 전후에 확인한다.

준비 결과의 `profile(id)`는 기존 `GuestBundle`과 고정 도구 타입을 제공한다.
`verify()`는 원래 선언과 현재 파일을 다시 대조한다. `public()`에는 경로·명령을
넣지 않으며 `executionAuthority: "none"`을 명시한다. 입력 해시 검사는 VM boot나
격리 qualification을 대신하지 않는다.

## 서명 정의 공통 규칙

`load_signing_definition(reference, policy_document=..., application=...)`는 공개 설정의
`signing.definition` 참조를 읽는다. 최대 2 MiB JSON이며 SHA-256은 공백을 포함한
원시 파일 바이트 기준이다. 중복 키·알 수 없는 필드·다른 앱·정책을 거절한다.

서명 정의는 키 경로·비밀번호를 받지 않는다. 인증서/profile 참조는 정확히
`{ "path": "/absolute/input.der", "sha256": "…", "bytes": 1234 }` 형태다.
심볼릭 링크·하드링크·안전하지 않은 쓰기 권한·크기/해시/읽기 중 변경을 거절한다.
인증서는 DER 형식의 `.der` 또는 `.cer`, profile은 `.cms`, `.mobileprovision`,
`.provisionprofile`을 사용한다. PKCS#12·개인 키 파일 역할은 이 로더에 없다.

## Android 정의

최상위는 `schemaVersion: 1`, `kind: "android-signing-definition-v1"`, `identity`다.
`identity`의 필수 필드는 아래와 같다.

- `referenceId`, `applicationId`, `packageName`, `certificateSha256`
- `signatureSchemes`: 기존 계약의 정렬된 배열이며 `v2` 필수
- `permissions`: 정렬된 고유 permission 배열

`AndroidSigningIdentity`로 변환하고 package·identity 참조와 정책의
`entitlementsDigest`를 기존 `signing_configuration_digest` 계약으로 검사한다.

## iOS 정의

최상위 필드는 다음 표와 같으며 추가 필드를 받지 않는다.

| 필드 | 내용 |
| --- | --- |
| `schemaVersion`, `kind` | `1`, `ios-signing-definition-v1` |
| `identity` | `referenceId`, `applicationId`, `teamId`, `certificateChain` 참조 배열 |
| `provisioningReferenceId` | 기존 서명 정책이 선택한 profile 참조 ID |
| `bundlePolicies` | 코드 경로별 `bundleId`, `entitlements`; 기존 `IOSSigningDefinition` 계약 |
| `profiles` | 앱/extension 경로별 `cms` 파일 참조와 `profileDigest` |
| `provisioning` | 아래의 고정 CMS 입력 |

`provisioning`은 다음 필드를 갖는다.

- `tools`: `opensslPath`, `opensslSha256`, `sandboxSha256`
- `trust`: `referenceId`, CMS 발급자 인증서 참조 `signer`, anchor 참조 배열 `anchors`
- `applicationIdentifierPrefix`, `selectedDevice`

CMS 발급자와 앱 서명 인증서는 별도 역할이다. 발급자나 Apple anchor를 자동 선택하지
않는다. `profileDigest`는 기존 `decoded_profile_digest()`의 projection digest다.
인증서는 각각 128 KiB, profile은 각각 4 MiB, 한 서비스 준비 결과가 유지하는 전체
profile은 64 MiB 이하다. 선언된 합계를 확인한 뒤 인증서/profile 바이트를 읽는다.

결과는 불변 입력을 담은 `IOSSigningDefinition`과 `IOSSigningProvisioning`이다.
raw profile·entitlement·기기 식별자·입력 경로는 공개 요약에 넣지 않는다.
실제 인증서/CMS의 암호학적 검증은 고정 실행 경로에서 다시 수행하며,
profile 확인과 준비 앱 digest 대조가 끝나야 키를 연다.

## 검증 범위

실제 고정 Android/iOS 도구 로딩, 변조·잘못된 recipe·원본 변경·한도 초과를 검사했다.
자체 UIKit 앱에서는 파일에서 읽은 정의로 실제 서명·새 nonce의 독립 검사·정리와
예약 해제까지 확인했다. VM 입력 시험은 합성 바이트이며 boot 근거가 아니다.
회사/Apple 입력·보호 mobile 환경·물리 기기·두 Mac 수용과 전체 `live-serve`
시작 조합은 남아 있다.
