# iOS 서명 준비와 영속 복구

2026-09-14 · D4 진행 중

`IOSSigningOperationStore`는 IPA를 준비하는 작업의 용량·원래 디렉터리·파일 잠금을
영속화한다. [고정 서명 실행](IOS-SIGNING-EXECUTION.md)은 여기에 실제 CMS·서명·독립
검사 프로세스를 연결하며, 서비스 소유 factory와 supervisor callback을 제공한다.
`ios-signing build-tools/status/recover`는 도구 준비와 기존 작업의 관찰·복구를 제공한다.
실제 보호 환경과 회사 앱 수용은 남아 있다.

## 고정 도구 빌드

운영자가 확인한 컴파일러·macOS SDK의 절대 경로와 SHA-256을 전달한다.
`SDK_SETTINGS_SHA256`은 선택 SDK의 `SDKSettings.json` 해시다.
`NEW_TOOLS`는 기존의 소유자 전용 부모 디렉터리 안에 있는 새 출력 경로여야 한다.

```sh
reproloop ios-signing build-tools \
  --output-new "$NEW_TOOLS" \
  --clang "$CLANG" --clang-sha256 "$CLANG_SHA256" \
  --sdk-root "$SDK_ROOT" --sdk-settings-sha256 "$SDK_SETTINGS_SHA256" \
  --timeout-seconds 90
```

설치 리소스의 signer·guardian·verifier와 공통 header만 오프라인으로 빌드한다.
출력에는 실행 파일 세 개와 `tools-manifest.json`이 있으며, 표준 출력에는 manifest·
출력·소유자 정의 digest만 반환한다. `load_ios_signing_owner(path, manifest_digest)`는
소스·출력 파일·sandbox 해시를 대조한다. 기존 출력을 덮어쓰지 않는다.
시간 제한은 1~120초이며 SIGINT/SIGTERM은 취소를 요청한다. 자식 프로세스 그룹의
종료를 확인한 뒤 임시 디렉터리를 정리한다. 종료가 불명확하면 사본을 보존하고
`cleanupConfirmed: false`로 실패한다.

이 manifest는 명시한 컴파일러 실행 파일과 SDK 설정 파일을 고정한다. 컴파일러가
호출하는 전체 도구나 SDK의 모든 내용에 대한 검증·보호 환경 qualification은 아니다.

## 명시적 입력

`ios_signing_inputs.IOSSigningIdentity`는 키 참조·앱 ID·team과 명시적 인증서 체인을
고정한다. 같은 leaf 인증서는 참조 ID가 달라도 같은 canonical scope를 사용한다.
`IOSSigningDefinition`은 모든 코드 객체의 bundle ID·entitlement, 앱/extension의
정확한 profile 집합을 복사해 고정한다. `public()`에는 profile 본문과 entitlement
값을 넣지 않는다. 입력의 DER/크기 검사는 실제 인증서·CMS 검증을 대신하지 않는다.

`IOSSigningMaterialResolver`는 운영자가 등록한 PKCS#12와 UTF-8 비밀번호만 사용한다.
파일은 symlink가 없는 경로의 단일 링크·소유자 `0600` 파일이며 최대 8 MiB다.
열 때 원래 inode·크기·변경 시각을 다시 확인한다. 새 호출을 막는 `close()`와 열린
재료의 명시적 종료를 구분한다. 비밀번호는 최대 512바이트이고 native 전달에는
pipe를 사용한다. 공개 정책·오류·repr에 비밀번호나 키 경로를 포함하지 않는다.

## 준비 작업

생성자는 `RunStore`, 고정 `IOSSigningOwnerTools`, `IOSSigningDefinition`과 private
작업 root를 받는다. 기존 root는 `create=False`로 열 수 있다. 도구·정의·환경·RunStore
root·scope가 원래 구성과 달라지면 기존 작업을 채택하지 않는다.

`admit(context, request_digest)`는 원래 제어 파일과 디렉터리의 inode를 intent에
기록한 뒤 RunStore 예약을 얻는다. 아직 사본 바이트를 쓰지 않는다. `stage()`는
`candidate.ipa` 하나인 불변 BlobSet과 context의 unsigned digest를 대조하고, 예약
안에서 원본 IPA·압축 snapshot·추출 앱을 준비한다. native 실행 경로는 실제 준비한
트리와 등록된 서명 정책으로 추가 크기 상한을 계산해, 기존 예약 안에 들어올 때만
키를 사용한다. staging만 호출한 결과를 native 실행 승인으로 사용하지 않는다.

준비 예약은 압축/추출 상한 두 개, 전송 상한 두 개, 명시적 profile 바이트와 4 MiB
메타데이터 여유를 포함한다. 현재 앱 확장 상한은 512 MiB, BlobSet 전송 상한은
64 MiB다. native 경로의 입력별 상한과 실제 출력 크기 검사도 별도로 통과해야 한다.

추출물은 미리 기록한 `transfer` 디렉터리 안에 남긴다. snapshot·ZIP 읽기와 추출
쓰기는 원래 descriptor를 유지하며, 다른 디렉터리로 경로가 바뀌면 결과를 거절한다.
코드 객체 집합과 bundle ID를 정의와 대조하고, 추출된 앱은 기존 디렉터리를 덮어쓰지
않는 rename으로 `App.app`에 옮긴다. `stage()` 결과에는 서명·provisioning 승인이 없다.

## 복구

원래 실행 구성이 살아 있을 때 `operations.recovery_configuration()`을 내보내
운영자 소유의 `0600` JSON 파일로 보관한다. 앱 ID·원래 저널 경로·환경·scope·구성
digest와 용량 한도만 포함하며 키 경로·비밀번호·인증서·profile 원문은 포함하지 않는다.
원래 저널이 없는 경로를 자동으로 만들거나 다른 구성으로 재생성하지 않는다.

```sh
reproloop ios-signing status --config "$RECOVERY_REFERENCE" --operation "$OPERATION_ID"
reproloop ios-signing recover --config "$RECOVERY_REFERENCE" \
  --operation "$OPERATION_ID" --request-digest "$ORIGINAL_REQUEST_DIGEST"
```

`status`는 읽기만 한다. 두 명령 모두 도구 바이너리·키·profile을 다시 읽지 않는다.
`load_ios_signing_configuration(path).open_existing()`도 같은 복구 전용 객체를 연다.
그 객체는 새 admit·stage·sign·inspect를 거절하며 실행 가능한 서명기로 승격되지 않는다.

중단 뒤 `recovery(operation_id, request_digest)`는 canonical scope와 RunStore 잠금,
원래 producer·native owner 잠금을 모두 획득한다. 실제 native 프로세스가 해당
잠금을 보유하면 복구와 비용 해제를 거절한다. 상태 파일이나 PID를 종료 증거로
사용하지 않는다.

원래 입력 hash, 앱/임시 디렉터리 식별과 제어 파일을 검사하고, 열린 원래 디렉터리
안에서 부분 사본을 정리한다. 예상 밖의 파일, 사라지거나 교체된 앱, 입력 바인딩
변경은 예약을 유지한다. 정리 도중의 중단과 RunStore의 마지막 기록 실패는 재시도할
수 있다. audit용 intent/state와 빈 제어 파일은 남으며 namespace 수는 제한된다.

반환한 정리 capability는 같은 프로세스·스레드의 살아 있는 객체여야 한다.
`run_store.finish_signing_recovery(capability, authority=operations)`가 원래 잠금과
정리 상태를 다시 확인한 뒤 한 번 소비한다. 결과는 `failed` 또는 `cancelled`이며
서명 성공이나 수정 검증을 발급하지 않는다. VM 종료 JSON으로 서명 작업을 해제할 수 없다.
이미 끝난 작업에 대한 같은 복구 요청은 `already-terminal`을 반환한다.

## 검증 범위와 다음 작업

실제 Python 부모 종료 도중의 추출 사본 복구, 실제 native 소유자의 잠금, 취소,
늦은 callback의 종료 대기, 복제/만료 capability 거절과 마지막 ledger 기록 실패를
검사했다. 입력 계약에는 명시적인 합성 DER/profile 및 실행하지 않는 도구 대역도
사용했다. 이는 실제 Apple profile·회사 앱·보호 VM·기기 수용 검증이 아니다.

profile 주입·고정 CMS 검증, native 프로세스 소유권, signed IPA 전송과 보호
supervisor/서비스 factory와 공개 도구 빌드·상태·복구 CLI를 연결했다.
실제 회사·보호 환경 검증은 남아 있다. CLI는 D4 r5 개발 wheel의 대상이며,
r4 wheel에는 고정 native 실행 API까지 포함된다. 새 설치본의 검증 근거는
[r5 수용 기록](../artifacts/product-delivery/d4-foundation-package-r5/acceptance.json)에 둔다.
