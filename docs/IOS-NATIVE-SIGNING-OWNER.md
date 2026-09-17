# iOS 네이티브 서명 소유자

현재 `native/ios-signing-owner/main.c`와 `ownership.h`는 고정 서명 프로세스의 파일
잠금·부모 종료·메모리 키 수명을 구현한다. [준비 단계의 영속 저널과 복구](IOS-SIGNING-RECOVERY.md)는
추가했으며, [native 실행·서비스 factory](IOS-SIGNING-EXECUTION.md)도 연결했다.
이 소스만으로 보호 실행 qualification이나 복구 비용 해제 권한을 발급하지 않는다.

## 네이티브 계약

프로세스는 아래 9개 descriptor 번호만 인자로 받는다. 경로·비밀번호·명령을 인자로
받거나 candidate hook을 실행하지 않는다.

1. 비공개 요청 plist: 단일 링크, 소유자 권한 `0600`, 최대 2 MiB
2. 원래 작업 디렉터리: 소유자 권한 `0700`
3. `producer.lock`
4. `owner.lock`
5. 부모가 write end를 보유하는 liveness pipe의 read end
6. 승인된 PKCS#12의 읽기 descriptor: 최대 8 MiB
7. 비밀번호 pipe 또는 비공개 파일의 읽기 descriptor: 최대 512바이트
8. 미리 만든 빈 `start.json`의 쓰기 descriptor
9. 미리 만든 빈 `termination.json`의 쓰기 descriptor

필드가 늘어나거나 descriptor 역할·원래 inode가 다르면 시작을 거절한다. 부모가
잡은 두 flock의 동일 open-file description을 자식이 이어받는다. 부모는 전달한
사본을 `close`해야 하며, `LOCK_UN`으로 자식의 잠금을 풀면 안 된다. 요청 파싱 전부터
부모 종료를 감시하고 core dump를 막는다. 상속 descriptor는 이후 exec에서 닫힌다.

요청은 다음 필드로 한정한다. native 요청의 `schemaVersion`은 **문자열 `"1"`**이다.
CFPropertyList가 값이 같은 정수·실수를 정규화하기 때문에 native 경계에서는 문자열을
사용한다. 공개 실행 계약과 JSON 감사 기록의 정수 버전은 바꾸지 않는다.

```text
schemaVersion, mode, operationId, requestDigest, contextDigest,
scopeDigest, definitionDigest, workPath, appRelativePath,
certificateSha256, teamId, certificateChain, codeObjects
```

`appRelativePath`는 `App.app`으로 고정한다. `codeObjects`의 각 행은 `bundlePath`,
`bundleId`, `entitlements`만 가지며 마지막 값은 plist 바이트다. native consumer는
bundle ID와 작업 안의 실제 경로를 확인한다. 최대 512개 코드 객체, 객체당 32개
architecture 서명 callback, 인증서 최대 8개·각 128 KiB, entitlement plist당 256 KiB다.
전체 요청도 2 MiB 안에 들어야 한다. PKCS#12는 메모리 전용으로 import하고, leaf
인증서 SHA-256을 전달한 체인 및 승인된 값과 대조한다. RSA 2048–8192비트의 고정
SHA-256 서명만 사용하며 timestamp 서비스는 요청하지 않는다.

Apple의 `SecCodeSignerRemote` SPI에 제공하는 block은 동일 프로세스의 고정 코드다.
외부 서명 서비스를 호출하지 않는다. API 출처는 [공식 소스 검토](../artifacts/product-delivery/d4-protected-adapters-r1/official-source-r1/findings.md)를
따른다. 현재 캐시된 SDK와 macOS에서 검증했으며 다른 OS 환경의 호환성은 별도 수용 조건이다.

## 종료와 복구 연결

성공·실패 때 키의 CF 참조와 입력 descriptor를 닫고 종료 의향을 fsync한다. 결과를
출력한 뒤에도 잠금은 유지한다. 부모가 완성된 결과를 받고 `0x01`을 보내면 종료한다.
결과 이전 ACK는 실패이며, ACK 대기는 최대 약 5초다. 부모 pipe의 EOF는 프로세스를
종료한다. 감시에는 단조 시각 기준 900초의 상한도 있다. `liveness-probe`는 키를 받지
않고 서명 성공 결과를 만들지 않는다.

`termination.json`은 종료 의향이다. PID나 그 JSON만으로 종료·정리·예산 해제를
판정하지 않는다. 후속 Python 작업 소유자는 실제 자식 종료와 원래 두 잠금의 재획득,
원래 디렉터리/파일 식별, 부분 앱 정리를 확인하고 살아 있는 일회성 복구 capability를
발급해야 한다. 준비·native 실행과 RunStore 복구를 연결했으며 공개 운영 CLI는 남아 있다.
여러 객체 중 뒤의 서명이 실패하면 앞에서 서명한 사본은 남을 수 있다. 이 경우
결과는 실패로 유지하고 이미 완료한 객체 수를 보존하며, 사본을 재사용하지 않는다.

## 확인한 범위

[관련 36개 검사](../artifacts/product-delivery/d4-protected-adapters-r1/ios-artifacts/native-owner-related-r3.log)가
통과했다. 신규 native 검사 8개는 부모의 실제 `os._exit`, 시작 전 EOF, 중지된 프로세스,
조기 ACK, 바뀐 inode·잘못된 요청, 실패 결과의 잠금 유지와 실제 앱 서명을 다룬다.
실제 서명은 자체 UIKit 앱·자체 키로 수행하고 독립 검사기에서 두 architecture와
entitlement를 확인했다. 서명 자식은 네트워크·Mach lookup·fork를 막고 필요한 파일만
허용한 sandbox에서 실행했다. 원본 앱과 사용자 Keychain은 변경하지 않았다.
첫 객체 서명 뒤 다음 요청이 실패하는 경우도 실제 사본과 결과를 대조했다.

앱/프로필/정책 바인딩, 예약, 영속 저널, 부분 사본 정리와 별도 검증 프로세스의
소유권을 연결했다. 다음은 공개 구성·복구 CLI와 실제 환경 수용이다. 과거 D4 r2
wheel에는 이 실행 경로가 없으며 새 개발 배포물의 검증 기록을 확인해야 한다.
