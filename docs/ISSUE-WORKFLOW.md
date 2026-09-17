# 공유 QA 기록·재현 (G7)

공유 coordinator에서 프로젝트·빌드·기기를 선택하고, 준비 완료 후 입력과 영상을 기록한다. 원본은 보존하고 재현 조건을 별도 revision으로 작성한다. 승인한 명세는 원본 빌드에서 고정된 세 번의 재현을 실행한다. `reproduced`는 원본 결함 재현 결과다. 코드 수정은 같은 화면의 [수정 제안·보호 검증](PROJECT-REPAIR.md)으로 이어진다. `verified` 판정에는 별도로 자격을 확인한 실행 환경과 독립 검사가 필요하다.

## QA 화면

1. 개인 credential로 로그인한다. 프로젝트에 operator 권한이 있어야 녹화·입력·재현할 수 있고, maintainer 권한이 있어야 명세 작성·승인·가져오기를 할 수 있다. 역할은 합산된다.
2. 프로젝트, application, build, 호환 기기와 준비 항목을 선택한다. **Prepare & record**는 실제 fixture 준비와 기기의 첫 프레임을 기다린다. 준비를 생략하려면 unknown starting conditions를 명시적으로 선택한다.
3. 현재 프레임을 탭하거나 지원되는 입력을 전송한다. 텍스트 입력은 등록된 변수 참조를 사용한다. 변수 값은 원본이나 패키지에 저장하지 않는다.
4. **Stop recording**으로 입력을 닫는다. 종료 응답 이후에도 영상·기기·fixture 정리가 진행될 수 있다. 실패나 불확실성은 원본·상태·정리 영수증에 남는다.
5. 영상과 액션을 확인한 뒤 재현 조건을 작성한다. 좌표 입력에는 크기·방향·geometry version이 고정된다. 같은 이미지까지 요구할지 별도로 선택할 수 있다. 애니메이션 때문에 이미지가 달라지는 경우 크기·방향만 검사하도록 **새 명세에 명시해서 승인**할 수 있다. 원본 프레임 해시는 그대로 보존된다.
6. defect와 expected 조건을 각각 작성한다. 관측 ID, property, 비교값, coverage와 시간 범위는 등록된 앱 관측 계약에 맞춰야 한다. snapshot의 최대 timestamp uncertainty는 승인된 시점 허용 오차로도 적용되며, 이를 벗어나면 `unknown`이다. 샘플 관측은 연속 관측을 대신하지 못한다.
7. 새 revision을 저장하고 화면에 표시된 정확한 JSON·SHA-256을 검토해 승인한다. 편집 중이거나 저장된 revision이 바뀌면 이전 승인을 사용할 수 없다. 가져온 패키지는 로컬 프로젝트 바인딩 확인이 추가로 필요하다.
8. **Replay · 3 original attempts**로 실행한다. 입력, 관측, predicate, cleanup을 각각 확인한다. 동일 campaign을 다시 요청해도 세 번의 예산을 초기화하지 않는다.
9. 등록된 수정 provider가 있으면 **Generate proposal**로 패치를 검토한다. **Generate & verify**는 새 후보를 생성하고 보호된 빌드·서명·독립 검사·같은 명세의 후보 재생까지 실행한다. 제안만 생성한 작업은 검증 성공이 아니다. 버튼의 실행 조건, 취소·보존과 CLI는 [G9 운영 문서](PROJECT-REPAIR.md)를 따른다.

일반 QA 작업은 JSON 설정 파일을 편집하지 않는다. 등록·정책·외부 fixture 구현은 운영자가 구성한다. 지원하지 않는 입력과 locator는 서버에서 거절된다. 현재 원격 provider에는 semantic locator RPC가 없으므로 공유 원격 재현은 좌표 입력을 사용한다. 일반 iOS 앱의 semantic/app-log adapter 제한은 [워커 가이드](WORKER-RUNTIME.md)를 따른다.

## 신뢰된 운영 구성

[프로젝트 권한·호스트 등록](SHARED-COORDINATOR.md)과 [워크로드 프로필·워커 연결](WORKER-RUNTIME.md)을 먼저 구성한다. 기존 credential/서명 파일이나 실제 기기를 테스트 예시가 자동으로 읽거나 채택하지 않는다.

`live-serve`에 다음 옵션을 추가한다.

```sh
python3 -m reproloop live-serve \
  --shared-config /etc/reproloop/shared-coordinator-v2.json \
  --issue-config /etc/reproloop/issue-runtime.json \
  --video-helper /opt/reproloop/ReproVideo \
  --media-validator /opt/reproloop/media-validator \
  --output /var/lib/reproloop/live-output \
  --authority-mode shared-v2 \
  --workers-stdin
```

워커 연결 정보는 보호된 stdin으로 전달한다. 정확한 worker 옵션·전달 형식은 워커 가이드를 따른다. coordinator의 authority는 shared state root 옆 `host-authority-v1`에 별도로 생성한다. `--issue-config`는 `--shared-config`를 요구한다. 영상 helper를 생략하면 실제 MP4 기능이 제공되지 않는다. 이미지/MP4가 포함된 패키지 가져오기·내보내기에는 고정된 media validator 실행 파일이 필요하다.

설치된 Xcode/Swift로 helper를 빌드한다. 현재 패키지 MP4 decoder는 macOS 14 이상을 요구한다.

```sh
xcrun swift build --package-path native/macos-video -c release
xcrun swiftc native/macos-media-validator/main.swift -o /new/output/media-validator
```

`issue-runtime.json`은 최대 1 MiB의 비밀이 아닌 신뢰된 로컬 JSON이다. [예시 프로젝트](examples/issue-project.json)와 [예시 runtime](examples/issue-runtime.json)은 합성 계약의 구조 예시이며 회사 빌드나 기기를 등록했다는 증거가 아니다. 실제 project digest, 빌드 provenance와 loopback 서비스 주소로 구성해야 한다.

runtime schema version 1:

| 필드 | 내용 |
| --- | --- |
| `kind` | `reproloop-issue-runtime` |
| `projects[].projectId`, `projectDigest` | 로컬 등록 프로젝트와 정확히 일치 |
| `runtimePolicy` | 등록된 실행 환경 계약. imported JSON은 이 정책을 공급하지 못함 |
| `validationRecipeIds` | 프로젝트에 등록된 regression recipe ID |
| `fixtures[]` | application/fixture/endpoint ID, 고정 loopback base URL, check/cleanup recipe, 비밀 없는 payload |
| `variables[]` | `variableId`와 `environment` 이름. 값이나 dotenv 경로를 받지 않음 |
| `observations[]` | observation ID, provider incarnation, 고정 loopback base URL, 제공 가능한 coverage |
| `repair` (프로젝트별 선택) | 공개 소스 manifest·원본 artifact·고정 build recipe·제안 provider와 전송 정책. 보호 실행 객체는 로컬 코드에서 별도 등록 |
| `repairStorageBytes` (최상위 선택) | 수정 작업의 데이터 예약 예산. 기본 256 MiB와 별도 journal 8 MiB를 전체 저장 예산에 포함 |

변수 예: `{"variableId":"account_name","environment":"REPRO_QA_ACCOUNT_NAME"}`. 선언된 프로젝트 variable과 resolver의 타입이 맞아야 한다. 값은 입력 실행 시점에만 해석한다. 회사 비밀·정책은 별도로 승인·구성해야 한다.

fixture 서비스는 [준비·정리 계약](PREPARED-RECORDING.md)의 `/operations`, `/status`를 구현해야 한다. observation 서비스의 `POST /observations`에는 다음 요청이 전달된다.

```json
{
  "schemaVersion": 1,
  "requestId": "observation_unique_id",
  "projectDigest": "<등록한 SHA-256>",
  "observationId": "screen",
  "applicationId": "ios_app",
  "sessionId": "session_id",
  "anchorMs": 1789190000000,
  "requirement": "<해당 실행에 바인딩된 coverage 객체>"
}
```

응답의 정확한 필드는 `schemaVersion`, 동일 `requestId`, `envelope`, `values`, `absentProperties`다. envelope는 G4 observation 계약을 만족하고, 값은 실제 관측 시각에 연결되어야 한다. 요청의 시각·범위를 그대로 복사해 관측한 것처럼 만들지 않는다. partial·truncation·error·unknown은 그대로 전달한다. 연결/응답은 20초와 2 MiB로 제한하며 redirect·proxy·외부 주소를 사용하지 않는다. 코드·모듈·실행 명령을 runtime JSON에서 로드하지 않는다.

## HTTP와 CLI

모든 이슈 요청은 G5 개인 인증·현재 membership·프로젝트/리소스 바인딩을 검사한다. 브라우저는 로그인에서만 개인 credential을 사용하고 이후 HttpOnly cookie와 CSRF로 요청한다. credential은 URL·패키지·로그에 넣지 않는다.

브라우저는 보이는 동안 기기·프로젝트 목록을 5초 간격으로 갱신한다. 종료 직후 워커의 해제 보고가 늦게 도착해도 가용 상태를 다시 반영하며, 작성 중인 명세와 기기 선택은 보존한다. 실행 허용 여부는 각 서버 요청에서 다시 검사한다.

| HTTP | 역할 |
| --- | --- |
| `GET /api/release/projects` | 권한에 맞는 application/build/device/preparation/observation 목록 |
| `GET /api/release/issues` | 읽을 수 있는 이슈 목록 |
| `POST /api/release/issues` | 준비·녹화 시작; `202`는 준비 완료가 아님 |
| `GET /api/release/issues/{id}` | 원본, lifecycle, 명세/canonical/digest, 승인, 영상, campaign |
| `POST .../{id}/input` | 소유 controller/epoch/sequence에 연결된 typed input |
| `POST .../{id}/stop`, `/cancel` | 입력 종료와 비동기 정리 |
| `POST .../{id}/specifications` | `baseRevision`을 확인하고 새 revision 작성 |
| `POST .../{id}/approve` | 정확한 revision/digest 및 imported binding 승인 |
| `POST .../{id}/replay` | 지정 기기에서 동일 명세의 고정 예산 재현 |
| `POST .../{id}/export` | 현재 명세와 원본에 연결된 package publication |
| `GET .../{id}/media/{digest}` | 승인된 원본 또는 패키지의 immutable media |
| `GET /api/release/projects/{project}/packages/{package}/export` | 패키지 ZIP 다운로드 |
| `POST /api/release/projects/{project}/import` | bounded ZIP 업로드; SHA-256 헤더 필수 |

binary import는 `Content-Type: application/zip`, `X-Repro-Content-SHA256`, 정확한 Content-Length를 요구한다. media는 single Range, `206`/`416`, Content-Range/Length와 immutable ETag를 사용한다. 프로젝트 membership과 만료를 각 요청에서 검사한다.

```sh
python3 -m reproloop live-issues projects --server https://coordinator.example.internal:9443
python3 -m reproloop live-issues start --project checkout --application ios_app \
  --build original --device mac-worker-01--phone --preparation seed_account \
  --client-id qa_console --wait --server https://coordinator.example.internal:9443
python3 -m reproloop live-issues stop ISSUE_ID --wait --server https://coordinator.example.internal:9443
python3 -m reproloop live-issues export ISSUE_ID --output /new/output/issue.zip \
  --server https://coordinator.example.internal:9443
python3 -m reproloop live-issues import --project checkout --file /path/issue.zip \
  --server https://coordinator.example.internal:9443
```

각 명령은 TTY에서 credential을 숨겨 입력받는다. 자동화는 `--credential-stdin`으로 하나의 개인 credential 문자열을 전달한다. credential을 명령행 인자로 받지 않는다. `save`/`input --file`은 bounded JSON 요청 파일을 사용한다. `approve`에는 `--revision`, `--specification-digest`와 가져온 경우 `--bind-imported`가 필요하다. `replay`에도 `--device`, `--specification-digest`가 필요하다. `--wait` 시간 초과는 서버 작업을 자동 취소하지 않는다. 내보내기는 기존 출력 파일을 덮어쓰지 않는다.

## 영상과 패키지의 의미

영상은 G3의 실제 H.264 MP4와 `application/vnd.reproloop.video-manifest+json`을 사용한다. 표시 시각, source age, timestamp uncertainty, sample interval과 decoder seek 차이는 서로 다른 값이다. 원격 프레임의 native acquisition 시각이 매핑되지 않았으면 해당 시각·source age는 unknown으로 남고 원본은 incomplete로 보존된다. 이 상태에서도 실제 입력·준비·독립 관측이 조건을 충족하면 원본 결함 재현은 별도로 판정할 수 있다.

공백 위치로 이동하면 이전 영상을 지우고 그 위치에 프레임이 없음을 표시한다. 늦은 fetch/decode 응답은 다른 선택 화면을 복원하지 못한다. 화면을 바꾸면 기존 요청·객체 URL·listener를 정리한다. 권한 회수는 다음 요청을 거절하고 플레이어를 중지한다. 이미 다운로드한 바이트를 원격으로 회수하는 기능은 아니다.

ZIP은 최대 64 MiB이고 객체/JSON/개수/확장 크기도 별도로 제한한다. traversal, 절대 경로, 중복·대소문자 충돌, symlink/special entry, 미등록 객체, checksum/size 불일치, 알 수 없는 schema와 실제 decoding 실패를 거절한다. 파일 확장자나 헤더만 보고 MP4를 승인하지 않는다. 전체 검증과 최종 권한·retention 재검사 후에만 publication이 보인다. 실패한 staging은 실제 삭제 전까지 저장 비용을 유지한다. raw/파생 데이터와 다른 프로젝트의 같은 digest는 별도 권한을 갖는다.

## 재실행 가능한 검증

```sh
python3 scripts/release-check.py --goal G7 --effects filesystem,process,loopback,native-compile,browser
python3 scripts/qa-record-replay.py --environment synthetic-local \
  --output-new artifacts/qa-delivery/NEW_G7_RUN --browser
```

effect 목록은 검사 선택 선언이며 OS sandbox가 아니다. 실제 테스트는 새로 만든 임시 서비스·credential·프로세스와 설치된 offline 도구만 사용한다. 의존성이나 브라우저를 다운로드하지 않는다.

gate는 별도 상태ful backend, coordinator와 실제 worker CLI 프로세스 두 개를 시작한다. 고의로 한 탭에 2 증가하는 합성 앱을 녹화하고, coordinator **프로세스**를 재시작하고, ZIP 왕복 후 두 번째 워커에서 변경 없는 명세를 세 번 재현한다. 추가로 브라우저에서 새 녹화·조건 작성·승인·세 번 재현, MP4 재생·회전·action/gap seek·늦은 응답·권한 회수를 실행한다. 실제 값·predicate·정리 영수증·MP4·스크린샷·실행 전후 소스 해시를 보존한다. 정상 gate가 끝나면 소유 프로세스와 임시 credential을 정리한다.

G9 연결 후 현재 G7 검사 61개도 `artifacts/qa-delivery/g9-regression-g7-r1.json`에서 통과했다. 현재 전체 검사 대조는 `g9-parent-covered-tests.json`을 따른다. 고유 검사 1,026개 중 1,023개는 통과 근거가 있고, 고정 Android 의존성 검사 3개는 차단됐다. helper의 최신 offline 시도는 Gradle 8.14.5를 실행했지만 AGP 8.13.2를 해석하지 못했다. 이 결과를 전체 회귀 검사 통과로 해석하지 않는다.

`--environment /explicit/native-descriptor.json`은 명시된 native 환경의 필수 정보·소유권 선언을 검사한다. 실제 두 Mac·실기기·회사 앱·접근 credential·fixture 검증이 없으면 `blocked-unqualified`이며 이를 합성 환경으로 대신 통과시키지 않는다. G7의 로컬 통과는 회사 QA·두 Mac 실기기·G8 VM·G9 보호 수정 acceptance가 아니다.
