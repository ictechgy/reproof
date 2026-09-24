# 인증된 로컬 독립 관찰자

2026-09-14 · D4 진행 중

`UnixAndroidValidationObserver`는 설치된 후보의 실제 Android 어댑터·retained scope·
operation/source/artifact 바인딩을 확인하고, 운영자가 등록한 호스트 서비스에 검사를
요청한다. 서비스는 후보 밖에서 해당 recipe를 독립적으로 관찰해야 하며 자신의 검사
작업 종료·정리를 확인한 뒤 응답해야 한다. 이 프로토콜의 v1은 `external-observation`
전용이며 `trusted-runner`를 지원한다고 표시하지 않는다.

## 입력과 인증키

`validation.observers`는 `{path, sha256}` JSON 참조다. 본문은 `schemaVersion: 1`,
`kind: "unix-validation-observers-v1"`, `observers` 배열이며 각 항목은 정확히
`sourceId`, `providerId`, `socketPath`, `authenticationReferenceId`를 갖는다.
source ID 집합은 validation plan과 같아야 한다. 파일 읽기는 socket에 연결하지 않는다.

`load_android_validation_inputs(reference, plan=..., mobile_inputs=...)`가 읽고,
`AndroidValidationInputs.bind(adapter, secret_registry)`가 고정 관찰자를 등록한다.
인증키는 JSON·환경변수·파일에서 자동 검색하지 않는다. 운영 코드가
`ValidationSecretRegistry.register(ref, project_digest=..., provider_id=..., secret=...)`로
명시적 32~64바이트 키를 별도 등록한다. 프로젝트/provider가 다른 키를 빌리지 않는다.
공유 registry는 하나의 서비스 composition이 소유하며 종료 시 사용 중 사본까지 기다린다.

socket은 현재 사용자 소유의 `0600` Unix 소켓, 부모는 `0700` 디렉터리여야 한다.
모든 경로 구성 요소의 symlink를 거절하고 Darwin `getpeereid`로 연결 상대 UID를
확인한다. socket/부모 identity를 요청 전후에 대조한다. macOS 경로 한도에 맞게
UTF-8 경로는 104바이트 미만이어야 한다. 실제 서비스는 운영자가 별도로 제공한다.

## wire 형식

연결당 요청/응답 각 한 프레임을 사용한다. 앞의 4바이트는 big-endian unsigned 길이,
뒤는 최대 65,536바이트 UTF-8 JSON이다. envelope 필드는 `message`, `mac` 두 개다.
canonical JSON은 키 정렬·공백 없는 구분자·`ensure_ascii=False`·NaN 금지이며 끝에
newline을 추가하지 않는다.

MAC은 HMAC-SHA-256의 소문자 hex다.

```text
request:  HMAC(key, b"reproof-validation-v1/request\0"  + canonical(message))
response: HMAC(key, b"reproof-validation-v1/response\0" + canonical(message))
```

요청 message의 필드:

- `schemaVersion: 1`, `providerId`, `sourceId`
- `context`: 기존 `ValidationContext.public()` 전체
- `exchangeNonce`: 매 교환 새로 생성한 32바이트의 hex
- `target`: `applicationId`, `deviceId`, `scopeDigest`

응답 message의 필드:

- `schemaVersion: 1`, 요청과 같은 `providerId`, `sourceId`, `exchangeNonce`, `target`
- `contextDigest`: 요청 `context`의 기존 `contracts.digest()` 값
- `outcome`: `pass`, `fail`, `unknown` 중 하나
- `evidenceDigest`: 독립적으로 얻은 증거의 digest
- `terminationConfirmed`, `cleanupConfirmed`: boolean

요청에는 코드·로그·키·변수 값·파일 경로를 넣지 않는다. 응답 MAC과 모든 바인딩을
대조하며 알 수 없는 필드도 거절한다. 읽기·쓰기 전체에 절대 deadline과 취소를 적용해
천천히 흘리는 응답이 제한을 연장하지 못하게 한다. 응답 이후에도 같은 설치 context와
동일한 live scope인지 확인한다.

## 판정과 소유권

연결 실패·잘못된 MAC/nonce/context·설치 교체는 `ValidationObservation`을 만들지 않는다.
`TrustedValidationAuthority`는 이를 quarantine으로 처리한다. 인증된 응답이라도 종료나
정리가 불확실하면 quarantine을 유지한다. socket의 존재나 성공 응답은 VM/mobile
qualification을 발급하지 않는다. 원본 복원과 native 정리는 별도의 고정 어댑터가 수행한다.

실제 회사 관찰자는 위 독립성·바인딩·정리 계약을 구현해야 한다. 현재 수용 검사는
자체 서비스가 도구 대역의 실제 설치 상태 파일을 읽으며, 실제 Unix 소유권·인증·
변조 거절·deadline·취소·서비스 종료를 확인한 범위다.
