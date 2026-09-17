# 일반 수정에 자동 로그 연결

2026-09-13

일반 UIKit/Views 앱에서 수집한 자동 관찰을 [프로젝트 수정](PROJECT-REPAIR.md)에
선택적으로 연결한다. 원본 녹화, 앱 실행과 프로필, 원본 소스·빌드의 해시를 고정하고
허용한 이벤트 필드만 별도의 `diagnostics.json`으로 만든다. 기본 설정은 진단을
전송하지 않는다. 회사 앱의 실제 AI 전송 정책과 전체 수용 검사는 아직 필요하다.

## 수집과 출처

등록된 공유 세션의 `app-logs` 조회는 관측 스냅샷을 원본 EvidenceStore에 저장한다.
참조 MIME은 `application/vnd.reproloop.app-log+json`이다. 일반 텍스트·접근성·영상
객체는 진단 추출을 위해 열지 않는다. 이전의 `application/json` 관측을 자동으로
앱 로그라고 추정하거나 원본 참조를 수정하지 않는다. 이 MIME이 들어 있는 패키지는
발신·수신 서비스 모두 이 형식을 지원하는 버전을 사용해야 한다.

선택한 로그는 본문 크기·해시, 전체 스키마, 앱 bundle·platform·관찰 profile digest,
실행 marker를 검사한다. 같은 세션의 후속 스냅샷은 앞선 이벤트를 바꿀 수 없고,
손실·잘림 표시도 없앨 수 없다. 중복 스냅샷의 이벤트는 한 번만 전달한다.
원본에 연결되지 않은 파일이나 임의 파일 경로를 수정 요청으로 받지 않는다.

진단의 `runDigest`는 원본 앱 로그 marker 전체의 해시다. 실제 실행 UUID, 세션 UUID,
절대 시작 시각, 화면 텍스트, 인자·변수 값, 오류 메시지와 component ID는 전달하지
않는다. `target`은 정책에 나열한 공개 별칭만 남긴다. 수집기가 만든 익명 화면 ID는
`null`로 표시한다. 허용되지 않은 원문 필드가 섞인 앱 로그는 거절한다.

`elapsedMs`는 앱 로그의 시계다. 영상 시각과 연결되지 않았으므로 `clock`을
`app-elapsed-unmapped`로 명시한다. 클릭 관찰은 callback 진입·복귀·예외의 진단이며,
명세의 재생 동작이나 결함·정상 판정으로 사용하지 않는다.

공유 이슈는 종료 장벽 전에 허용된 자동 앱 로그의 마지막 스냅샷을 수집한다.
로그 패널을 열지 않고 종료해도 같은 경로를 사용하며, 600초 자동 종료에도 적용한다.
장벽 이후 새 로그를 원본에 넣지 않는다. 실제 설치 서비스의 근거는
[이슈 기록 검증](ISSUE-RECORDING.md#검증-범위)을 따른다.

## 진단 정책

`--issue-config`의 프로젝트 `repair` 항목에 `diagnosticPolicy`를 추가한다.
다음 해시·앱 ID·별칭은 설명용이며 등록한 프로젝트와 계측 프로필의 실제 값으로
바꾼다. 프로젝트의 `evidencePolicy.logs`도 허용되어 있어야 한다.

```json
{
  "schemaVersion": 1,
  "projectDigest": "1111111111111111111111111111111111111111111111111111111111111111",
  "applicationId": "ios_app",
  "profileDigest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "clickTargets": ["commit"],
  "screenTargets": ["inventory"],
  "eventFields": ["seq", "elapsedMs", "type", "name", "target"],
  "maxEvents": 500
}
```

`eventFields`의 허용 값은 `seq`, `elapsedMs`, `type`, `name`, `component`, `target`이다.
`seq`, `type`, `name`은 필수다. `maxEvents`는 모든 실행을 합쳐 1–2,000개다.
전달하지 못한 이벤트 수는 실행별 `omittedEvents`에 남으며 원본의 `truncated`,
`lostEvents`와 구별한다. 원본 로그 참조는 최대 64개·합계 8 MiB, 스냅샷 하나는
최대 1 MiB, 실행은 16개, 파생 자료는 384 KiB로 제한한다.

## 별도 AI 전송 승인

기존 `transferPolicy` v1은 제품 소스와 작성된 명세 필드만 허용한다. 진단을 외부
AI에 보내려면 v2의 `diagnostics`에 진단 정책 전체의 digest와 전달할 이벤트 필드를
지정한다. `eventFields`는 진단 정책의 부분집합이어야 한다. 프로젝트·제공자·소스
경로·`approvedTransfer`·`evidencePolicy.aiEligible` 검사도 그대로 적용한다.
로컬 patch 어댑터는 AI가 아니며 `transferPolicy`를 사용하지 않는다.

```json
{
  "schemaVersion": 2,
  "projectDigest": "1111111111111111111111111111111111111111111111111111111111111111",
  "providerId": "claude",
  "approvedTransfer": true,
  "sourcePaths": ["src/Inventory.swift"],
  "specificationFields": ["actions", "assertions", "waits"],
  "diagnostics": {
    "policyDigest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "eventFields": ["seq", "type", "name"]
  }
}
```

진단 출처의 해시, 실행 구분, 시계의 미연결 상태, 손실·생략 수는 고정된 출처
메타데이터로 함께 전달한다. 전체 제안 패킷은 512 KiB 한도를 유지한다. 소스만
승인한 v1 정책으로 진단을 보내거나, 정책이 선택한 진단을 조용히 생략하지 않는다.
일치하는 원본 로그가 없으면 `diagnostics_unavailable`로 거절한다.

## 검토와 보존

`GET /api/release/repairs/{id}/diagnostics`는 maintainer에게 실제 제안 패킷에 들어간
진단 자료를 반환한다. CLI는 내용 대신 저장 위치와 digest만 출력하며 기존 디렉터리를
덮어쓰지 않는다.

```sh
python3 -m reproloop live-issues repair-diagnostics REPAIR_ID \
  --server COORDINATOR_URL --credential-stdin --output NEW_DIRECTORY
```

작업 plan에는 `proposalPacketDigest`, `diagnosticEvidenceDigest`,
`diagnosticPolicyDigest`, `diagnosticSourceDigests`가 남는다. 일반 작업 상태 응답은
원문 진단 이벤트를 포함하지 않는다. 제안은 계속 별도 후보이며 원본 소스를 바꾸지
않는다. 진단을 추가해도 보호된 원본 재현·후보 검증 기준은 바뀌지 않는다.

파생 파일은 기존 수정 작업의 디스크 예약과 보존 관리에 포함한다. 기한은 원본,
선택한 로그, 파생 자료 정책 중 가장 이른 시각이다. 사용 중인 로컬 로그는 pin으로
보호하되 만료나 권한 회수 후 도착한 AI 결과는 채택하지 않는다. 가져온 이슈는
원본 archive에 고정하고 별도 로컬 바인딩과 원본 재현을 다시 요구한다. archive의
읽기 잠금은 진단 바이트를 확보한 뒤 해제하므로 AI 응답 동안 다른 작업을 막지 않는다.

원본 로그 또는 패키지가 제거되거나 만료되면 진단 조회를 거절하고 파생 파일도
만료 처리한다. 등록된 공유 세션은 보존 관리 밖에 별도의 로그 파일을 남기지
않으며, 종료한 세션의 메모리 사본도 정기 정리에서 제거한다.
