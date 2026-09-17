# 프로젝트 수정 제안·보호 검증 (G9)

2026-09-13

G7에서 승인하고 원본 결함을 재현한 이슈에 수정 작업을 연결한다. 제품 파일의
일반 텍스트 변경을 지원하며 숫자 표현식에 한정하지 않는다. 원본 소스·빌드,
재현 명세, fixture, 빌드 recipe와 독립 검증 기준은 후보가 변경할 수 없다.

수정 제안, 검증 실행, 실제 회사 환경 수용 검사는 별개다. 기본 JSON 구성은
제안 기능을 제공한다. 보호 검증은 운영자가 로컬 코드에서 등록한 빌드·서명·독립
검사·기기 실행기를 모두 요구한다. 현재 회사 앱과 fixture, 실제 VM, 서명·기기
격리 정책, 회사 AI 전송 정책, 두 Mac 환경은 제공되지 않았다. 이 문서의 합성
검사는 실제 회사 배포나 실기기 검증 결과가 아니다.

## QA 화면과 CLI

1. [공유 QA 화면](ISSUE-WORKFLOW.md)에서 기록을 선택하고 명세를 승인한다.
   같은 revision의 원본 재현이 고정된 모든 시도에서 성공해야 한다. 기본은 3회다.
2. **Generate proposal**은 등록된 공개 소스의 별도 후보와 패치를 만든다.
   `proposal-ready`는 검증되지 않은 제안이다. 작업 공간의 원본 파일을 덮어쓰지 않는다.
3. **Generate & verify**는 새 제안을 생성하고 보호된 검증까지 실행한다.
   이 버튼은 현재 프로젝트·기기 권한과 실행 환경이 허용할 때 활성화된다.
   이전에 선택한 제안을 재사용하는 버튼은 아니다.
4. 완료된 작업에서 원본 재현 횟수, 후보의 각 시도, 정리 상태와 패치를 확인한다.
   실패한 시도도 작업 이력에 남으며 유리한 결과로 교체하지 않는다.
5. 취소하면 새 실행을 중단하고 실행 중인 후보를 정리한다. 종료·정리 증거가
   불명확하면 `quarantined`로 남긴다. 취소 이후 도착한 성공 응답은 검증을 되살리지 못한다.

동일한 API를 CLI에서 사용할 수 있다. 아래 대문자 항목은 운영 환경의 값이다.
credential은 기존 `--credential-stdin` 방식으로 전달하며 명령 인자나 파일에 넣지 않는다.

```sh
python3 -m reproloop live-issues repair-propose ISSUE_ID \
  --specification-digest SPEC_SHA256 --request-id UNIQUE_REQUEST \
  --server COORDINATOR_URL --credential-stdin --wait

python3 -m reproloop live-issues repair-verify ISSUE_ID \
  --specification-digest SPEC_SHA256 --request-id ANOTHER_REQUEST \
  --server COORDINATOR_URL --credential-stdin --wait

python3 -m reproloop live-issues repairs ISSUE_ID \
  --server COORDINATOR_URL --credential-stdin
python3 -m reproloop live-issues repair-show REPAIR_ID \
  --server COORDINATOR_URL --credential-stdin
python3 -m reproloop live-issues repair-cancel REPAIR_ID \
  --server COORDINATOR_URL --credential-stdin
python3 -m reproloop live-issues repair-patch REPAIR_ID --output NEW_DIRECTORY \
  --server COORDINATOR_URL --credential-stdin
```

`repair-patch`는 새 디렉터리에 `proposal.json`과 `change.diff`를 쓴다. 기존 경로를
덮어쓰지 않으며 소스 내용을 표준 출력에 내보내지 않는다. `--wait`의 클라이언트
시간 초과는 서버 작업을 취소하지 않는다. 취소에는 `repair-cancel`을 사용한다.
같은 request ID는 동일 작업을 반환한다. 모드·명세·설정이 다른 재사용은 거절한다.

| HTTP | 권한과 결과 |
| --- | --- |
| `GET /api/release/issues/{issue}/repairs` | 이슈 읽기 권한; 작업 목록과 현재 실행 가능 여부 |
| `POST /api/release/issues/{issue}/repairs` | maintainer; 정확히 `requestId`, `specificationDigest`, `mode` |
| `GET /api/release/repairs/{id}` | 작업·이슈 읽기 권한; 소스 없는 상태와 증거 메타데이터 |
| `GET /api/release/repairs/{id}/proposal` | maintainer; 만료되지 않은 제안 또는 검증된 패치 |
| `GET /api/release/repairs/{id}/diagnostics` | maintainer; 전송 정책이 선택한 파생 진단 자료 |
| `POST /api/release/repairs/{id}/cancel` | 작업 소유자 또는 maintainer; 빈 객체 |

`mode`는 `propose` 또는 `verify`다. verify는 replay·fixture 실행 권한과 등록된
검증 기기의 사용 권한도 요구한다. credential, 브라우저 세션, membership, 기기
배정, 프로젝트 revision과 원본 보존 기간을 실제 실행 경계에서 다시 확인한다.
업로드한 capability, 명령, 모듈, 실행 결과 또는 `verified` 필드는 받지 않는다.

## 공개 소스와 제안 구성

`--issue-config`의 프로젝트 항목에 선택적인 `repair` 구성을 추가한다.
최상위 `repairStorageBytes`로 작업 데이터 예약 크기를 지정할 수 있다.

```json
{
  "sourceRoot": "/ABSOLUTE/PUBLIC_SOURCE",
  "sourcePaths": ["src/Checkout.swift", "tests/CheckoutTests.swift", "build/recipe.json", "checks/ui.json", "fixtures/account.json"],
  "protectedPaths": ["tests/CheckoutTests.swift"],
  "originalArtifactRoot": "/ABSOLUTE/ORIGINAL_BUILD",
  "originalArtifactPaths": ["original.bin"],
  "artifactIdentity": "file-sha256",
  "buildRecipeId": "build_app",
  "agent": {"kind": "local-patch", "patchFile": "/ABSOLUTE/OWNED_PATCH.json"}
}
```

이는 구조 예시다. 각 경로와 recipe는 등록된 프로젝트의 실제 선언과 일치해야 한다.
프로젝트의 모든 fixture·recipe 파일과 보호할 테스트 harness를 소스 manifest에
포함한다. `editablePaths`만 수정 가능하다. 원본 build의 `sourceDigest`는
`BlobSet` manifest digest와, `artifactDigest`는 선택한 artifact identity와 일치해야 한다.
`file-sha256`은 단일 파일 내용의 SHA-256, `tree-sha256`은 경로별 내용 해시 맵의 digest다.
디렉터리 이름만 지정해 재귀적으로 소스나 비밀 파일을 수집하지 않는다.

local patch 파일의 형식은 다음과 같다. 각 기존 문자열은 원본 파일에서 정확히
한 번 등장해야 한다. 겹치는 변경, no-op, 미선언 경로, 원본 변경은 거절한다.

```json
{"edits":[{"path":"src/Checkout.swift","old":"if !ready","new":"if ready"}]}
```

`local-patch`는 결정적인 테스트 어댑터이며 실제 AI가 아니다. Claude를 사용하려면
`agent`에 `kind: "claude"`, 운영자가 선택한 `model`, 선택적 절대 `executable` 경로를
등록하고 `transferPolicy`를 제공해야 한다. v1 정책의 정확한 필드는 `schemaVersion: 1`,
`projectDigest`, `providerId: "claude"`, `approvedTransfer: true`, `sourcePaths`,
`specificationFields`다. 프로젝트 자체의 `evidencePolicy.aiEligible`도 true여야 한다.
실제 회사 전송 승인이 없는 상태에서 이 값을 활성화하지 않는다.

기본 전송 대상은 정책이 선택한 편집 가능 제품 파일과 작성된 명세의 `actions`,
`assertions`, `waits`다. [별도 진단 정책과 전송 정책 v2](PROJECT-DIAGNOSTICS.md)를
등록하면 원본 앱 로그의 허용 필드도 파생 자료로 연결할 수 있다.
해석된 변수 값, fixture payload, 원본 녹화, 영상,
보호 테스트·빌드 규칙은 제안 패킷에서 제외한다. 패킷은 512 KiB, 응답은 256 KiB로
제한한다. 어댑터가 사용할 수 없으면 해당 작업은 끝내며 인증·쿼터를 반복 시도하지 않는다.

## 보호된 검증 실행기

[G8 실행 계약](REPAIR-EXECUTION.md)의 실제 환경 qualification은 JSON 승인 플래그로
생성되지 않는다. 운영자가 실제 검사 결과를 측정한 동일 `QualificationAuthority`와
그 인스턴스의 capability를 소유해야 한다. 로컬 구성은 다음 객체를 조합한다.

| 객체 | 책임 |
| --- | --- |
| `ProtectedBuildSupervisor` | 구체적인 `MacOSVirtualizationBackend`, 고정 recipe, 측정된 source/artifact와 네이티브 종료·정리 확인 |
| `TrustedSigningSupervisor` | 고정 호스트 signer와 별도 signature inspector; 승인된 identity/entitlements 정책 및 서명 전후 artifact 연결 |
| `TrustedValidationAuthority` | 후보 밖의 고정 runner·관찰 어댑터; 정확한 recipe, nonce, source/artifact와 종료·정리 증거 |
| `TrustedMobileAdapter` | 설치 전부터 최종 정리까지 네이티브 배타적 소유권, 기기·네트워크·계정·백엔드 범위 유지 |
| `ProtectedMobileSupervisor` | 별도 mobile qualification, 독립 검사, 동일 G4 명세의 후보 재현, capability 회수와 최종 sanitation |
| `ProtectedRepairExecutor` | 위 빌드·서명·모바일 단계를 하나의 고정 정의로 연결 |

완성한 executor를 `ProjectRepairConfiguration(..., executor=executor)`로
`ProjectRepairJobs`에 전달한다. 해당 프로젝트의 G7 `ScenarioRunner`와 runtime policy가
정확히 일치해야 한다. 기본 JSON loader는 이 객체나 임의 Python 모듈·callback을
만들지 않는다. 회사별 고정 signer/inspector, 네이티브 기기 provider와 실제 독립
관찰 구현은 운영자가 준비해야 하며 이번에 실제 환경 구현·수용 검사를 마친 것은 아니다.

기기 어댑터의 replay는 실제 G4 실행기가 발급하고 정리한 결과 객체를 그대로 반환한다.
JSON, 다른 실행의 결과, `dataclasses.replace`로 복제한 결과는 권한이 없다.
새 candidate build ID는 측정된 빌드와 서명을 검사한 후에만 임시 등록하며,
원본 프로젝트나 승인된 명세를 다시 작성하지 않는다. 모든 종료 경로에서 이를 회수한다.

공용 `Lab`의 개별 replay 예약만으로는 설치부터 마지막 정리까지의 배타적 소유권이
성립하지 않는다. native provider가 이 전체 구간을 소유해야 한다. mobile qualification은
Wi-Fi·cellular·VPN·백그라운드 실행·계정·keychain/shared storage·백엔드 부작용과
정리 범위를 실제로 통제하는지 별도로 측정해야 한다. 제공하지 못하는 통제는 승인하지 않는다.

서명·기기 scope는 안정적인 물리 식별자와 정규 상태 저장소에 연결한다. timeout,
미확인 callback 종료 또는 sanitation 실패는 그 scope를 재사용하지 못하게 한다.
새 디렉터리나 재시작으로 우회할 수 없다. mobile/signing 상태에 VM 종료 기록 복구를
사용하지 않는다. 이 두 scope의 운영 복구 CLI는 아직 제공하지 않으며 provider별
독립 정리 증거와 절차가 필요하다.

원시 iPhone `.app` 안의 `embedded.mobileprovision`은 기본 공개 파일 전송 manifest에서
거절된다. 승인된 provisioning 처리와 artifact 형식을 별도 구성하지 않고 실제 iPhone
전송을 지원한다고 가정하지 않는다. G9의 현재 조합은 mobile 경로이며 desktop 후보
실행은 별도의 `desktop-guest` 통합이 필요하다.

## 저장과 판정

작업별 소스/제안 데이터는 사전 66 MiB를 예약한다. 서비스 기본 데이터 예산은
256 MiB이고 별도 8 MiB journal allowance를 G2 전체 예산에 함께 청구한다.
동시 작업은 기본 2개, 최대 4개다. 전체 G2 예산에는 녹화·최종화 여유도 남겨야 한다.
작업 ID/요청 이력은 최대 128개를 유지하며 자동으로 지워 재실행하지 않는다.

활성 작업이 만료되면 우선 취소한다. 종료된 작업의 소스·제안은 선언된 파일만
삭제하고, 파일·디렉터리 동기화까지 확인한 후 데이터 예약을 해제한다. 알 수 없는 파일이나
정리 실패는 용량을 계속 청구한다. 원본 삭제/만료도 파생 작업의 보존에 전파한다.
브라우저는 프로젝트 변경, 권한 회수, 만료 시 검토 중인 소스를 지운다.

한 작업의 제안 예산은 1개이며 원본/후보 반복 예산과 validation recipe는 제안 전에
고정된다. 성공에는 원본 재현, 원본 무결성, 보호 빌드, 독립 서명 검사, 모든 독립
회귀 검사, 동일 명세의 모든 후보 시도, 기기·fixture·실행 종료 및 정리가 필요하다.
공개 `result`에는 패치 digest, build/signing provenance, 원본 campaign digest와 후보
시도별 결과/녹화 digest가 남는다. 후보가 만든 보고서에는 판정 권한을 주지 않는다.

## 검증 명령과 실제 환경 제한

```sh
python3 scripts/release-check.py --goal G9 --effects filesystem,process,loopback,browser
python3 scripts/qa-protected-repair.py --environment synthetic-local \
  --output-new artifacts/NEW_PROPOSAL_QA --browser
python3 scripts/qa-protected-repair.py --environment synthetic-protected \
  --output-new artifacts/NEW_PROTECTED_SOFTWARE_QA --browser
python3 scripts/qa-protected-repair.py --output-new artifacts/NEW_ACTUAL_ENVIRONMENT_CHECK
```

`synthetic-protected`는 실제 G4 저장소·loopback fixture·시나리오 실행기와 명시적인
VM/기기 대역을 연결한다. 결과의 `jobVerified`는 소프트웨어 제어 흐름 검사이고,
최상위 `verified`, `actualVM`, `actualMobile`, `actualAI`, `companyAcceptance`는 false다.
환경 없는 마지막 명령은 `blocked-unqualified`와 비정상 종료 코드를 반환한다.
환경 이름이나 업로드 JSON으로 실제 권한을 생성하지 않는다.

정식 G9 소프트웨어 검사 92개는 `artifacts/qa-delivery/g9-release-parent-r3.json`에서
모두 통과했다. 기존 기능을 포함한 현재 고유 검사 1,026개 중 998개를 새로 실행해
통과했고, 입력이 바뀌지 않은 G8a 25개는 기존 통과 근거를 재사용했다. 나머지
Android 의존성 검사 3개는 차단됐다. 대조 자료는 `g9-parent-covered-tests.json`이다.
환경을 지정하지 않은 실제 환경 gate는 `g9-environment-r1/result.json`에
`environment-not-supplied`, `verified: false`를 기록했다.

현재 실제 브라우저 기록은 `artifacts/qa-delivery/g9-protected-browser-r4/result.json`에 있다.
제안·검증 버튼, 3회 후보 결과, 패치 검토, 390px 화면, 늦은 응답의 프로젝트 경계,
credential 회수 후 소스 제거를 확인했다. 회사 앱과 실제 AI·VM·실기기 검증은 여전히 남아 있다.
