# Live 운영·녹화 라이브러리·자동화 큐

2026-09-09. 기기 연결 없이 진행 가능한 공통 운영 코드와 로컬 통합 검증을 추가했다. 서버는 계속 단일 사용자·loopback 범위이며, 여러 기기를 같은 프로세스에서 운영한다. 원격 worker 팜이나 다중 사용자 권한 시스템을 완성했다는 의미는 아니다.

## 실행과 콘솔

```bash
python3 -m reproof live-serve --demo --demo-count 2 \
  --job-concurrency 2 --output artifacts/live-operations
```

서버가 출력하는 주소를 연다. `--simulator`와 `--android`는 여러 번 지정할 수 있다. 중복 기기 ID는 거절한다. 같은 저장 디렉터리를 두 Live 서버가 동시에 운영할 수 없도록 OS lease를 적용한다.

콘솔에 다음 기능을 추가했다.

- Recording library: 저장된 녹화 선택, JSON 가져오기, JSON/Python 내보내기, 속도를 바꾼 복사본 생성.
- Use in Live: 같은 기기의 현재 수동 세션에 녹화를 불러와 재생. 현재 녹화 중이거나 다른 controller가 제어 중이면 거절.
- Automation jobs: 녹화·변수·반복 횟수·시간 제한을 선택해 제출, 상태 관찰과 취소.
- Live sessions: 현재 세션 목록과 attach. 다른 controller의 세션은 관찰 상태로 붙고 명시적으로 조작권을 인계.

수동 세션이 기기를 점유하면 자동화 작업은 기다린다. 작업 제출이 사용 중인 세션을 임의로 닫지 않는다. 기기가 반납되면 작업이 자신의 세션을 만들어 실행하고 정리한다. 독립 기기는 병렬로 시작할 수 있으며, 같은 기기는 제출 순서대로 처리한다.

## 작업 계약

상태는 `queued → starting → running → cleaning → succeeded/failed/cancelled`다. 서버 재시작 시 이전 비종료 작업은 `interrupted`로 바꾸고 자동 재실행하지 않는다. 완료 이력과 반복별 주입 영수증은 보존한다.

- 기본 병렬 작업 수 2, 대기/실행 중 작업 합계 기본 한도 100.
- 반복 1–10회, 작업 제한 시간 1–3600초. 제한 시간에는 기기를 기다린 시간도 포함된다.
- timeout/cancel은 새 입력을 중단하는 요청이다. 이미 주입 중인 동작은 provider acknowledgment와 정리가 끝날 때까지 기다리며, 실시간 강제 중단을 보장하지 않는다.
- 같은 owner의 동일 `requestId` 재전송은 같은 작업을 반환한다. 다른 요청 내용으로 같은 ID를 쓰면 거절한다.
- 변수 값은 메모리에서만 사용한다. 요청 비교용 HMAC도 메모리에만 보관한다. 재시작 후 같은 requestId는 안전하게 재검증할 수 없어 거절하며, 기존 작업은 조회하고 새 실행에는 새 requestId를 쓴다.
- 제출 때의 녹화 digest를 고정하고 기기 점유 전·매 재생 전에 다시 확인한다. 변경된 녹화를 자동 실행하지 않는다.
- 작업 성공은 모든 반복의 영수증과 정상 정리가 확인됐다는 뜻이다. 원래 버그나 수정의 oracle 판정은 별도다.

자동화 작업은 생성한 세션만 정리한다. 사용자가 작업 세션의 조작권을 가져가면 이전 controller로 실행을 계속하지 않는다. 이 큐의 세션은 작업 소유이므로 작업 종료 시 반납한다. 종료 후에도 Live 세션을 유지하려면 기존 Python replay의 `--session` attach 방식을 사용한다.

## 녹화 가져오기와 복사

외부 JSON은 schemaVersion, digest, 이벤트 수/순서, frame geometry, gesture 범위, 변수 참조와 텍스트 원문 비포함을 검증한 뒤 저장한다. 동일 ID·동일 digest는 중복 저장하지 않고, 같은 ID의 다른 내용은 거절한다. JSON digest는 내용 무결성 검사이며 발행자의 신원을 인증하는 전자서명이 아니다.

속도는 0.25–4배이며 최초 입력까지의 대기 시간도 같은 비율로 조절한다. 복사본에 새 ID와 원본 ID/digest/변환 정보를 남긴다. 원본은 수정하지 않는다. 이벤트 삭제·일부 선택은 CLI/API에서 가능하지만, 시작 상태와 동작 의존성이 달라질 수 있어 복사본을 `replayable=false`로 둔다. 이 편집본을 다시 복사해도 자동으로 재생 가능 상태로 올리지 않는다. 변환 후 기록 시간은 최대 10분이다.

## CLI

```bash
python3 -m reproof live-recordings list
python3 -m reproof live-recordings import /path/to/recording.json
python3 -m reproof live-recordings derive <RECORDING_ID> --speed 2
python3 -m reproof live-recordings export <RECORDING_ID> \
  --format python --output /path/to/replay.py

python3 -m reproof live-jobs submit <RECORDING_ID> --repeats 3 --wait
python3 -m reproof live-jobs list
python3 -m reproof live-jobs show <JOB_ID>
python3 -m reproof live-jobs wait <JOB_ID> --wait-timeout 300
python3 -m reproof live-jobs cancel <JOB_ID>
```

각 leaf 명령에 `--server http://127.0.0.1:PORT`를 지정할 수 있다. export는 기존 파일을 덮어쓰지 않는다. 변수는 대화형 터미널에서 echo 없이 입력하며 CI에서는 `--variables-stdin`으로 JSON 객체를 전달한다. 실제 비밀값을 명령행 인자나 로그에 적지 않는다.

`submit --wait`나 `wait`에서 클라이언트만 중단되면 서버 작업은 계속된다. 명시적 `cancel`로 중단을 요청한다. 작업 실패/중단 이력은 종료 코드로 구분한다.

## 로컬 에이전트 도구

```bash
python3 -m reproof live-tools
```

stdin/stdout JSON-lines 인터페이스다. 실제 모델 호출이나 MCP/WebDriver 호환 서버는 아니다.

```json
{"id":"fleet","tool":"devices.list","arguments":{}}
{"id":"sessions","tool":"sessions.list","arguments":{}}
{"id":"history","tool":"recordings.list","arguments":{}}
{"id":"jobs","tool":"jobs.list","arguments":{}}
```

허용 도구는 devices.list, sessions.list/create/get/close/heartbeat, control.claim, frame.observe, input.send, recordings.list/get/import/derive/start/stop, replay.start/cancel, jobs.list/get/submit/cancel이다. 입력 주입과 재생은 브라우저와 같은 epoch/sequence/geometry 검사 및 세션 점유 규칙을 사용한다.

요청 한 줄은 최대 1 MiB이며 잘못된 JSON·중복 키·비유한 수·알 수 없는 필드/도구·경로 탈출을 거절한다. 잘못된 요청이나 로컬 연결 실패 후에도 다음 요청을 처리한다. 임의 shell·URL·파일 읽기 기능은 없다. frame.observe는 요청한 화면을 반환하므로 해당 응답을 외부 모델에 전달할지는 호출 측 정책으로 결정한다.

## 세션 수명과 재시작

기본 idle timeout은 120초, 전체 세션 수명은 900초다. `--idle-timeout`과 `--max-session-seconds`로 설정한다. 브라우저는 heartbeat를 보내며 유효한 owner의 세션 상태 조회와 입력도 idle 시간을 갱신한다. 만료 뒤 도착한 heartbeat나 입력은 세션을 되살리지 않는다. 전체 수명은 heartbeat로 늘어나지 않는다. 재생은 대기 구간에도 idle lease를 유지하지만 전체 수명 제한은 적용된다.

기기 점유/반납을 `device-state.json`에 기록한다. 정상 정리가 확인되지 않은 채 서버가 종료됐으면 다음 시작에서 해당 기기를 격리한다. 다른 기기만 등록해서 실행해도 이전 기기의 미정리 표식은 보존한다. 현재 재시작 후 orphan recovery API를 검증한 provider는 합성 Demo뿐이다. 실제 Android/iOS의 자동 복구 계약은 후속 검증 대상으로 두고 임의로 격리를 풀지 않는다.

세션 배정·조작권 변경·종료는 `session-logs/<id>.json`, 녹화는 `recordings`, 작업은 `jobs`, 재생은 `replays`에 보관한다. 세션 로그에 입력 payload를 보관하지 않는다. API는 `/api/health`, `/api/sessions/:id/events`, `/api/jobs/:id/report`도 제공한다.

## 검증 결과

이번 단계는 합성 provider와 로컬 HTTP 서버로 검증했다. 실제 Android/iOS 원격 조작의 이전 검증은 [기존 Live QA](LIVE-QA.md)에 있고, 이번에 실기기/Simulator를 새로 부팅해 검증한 것으로 표시하지 않는다.

- 전체 Python 테스트 128개 통과, JavaScript 구문 검사 통과.
- 브라우저 JSON import → 변수 보존 → 사용 중인 기기에 작업 제출 → 수동 반납 → 2/2 실행·정리 확인.
- 가져온 녹화를 현재 Live 세션에 불러와 2개 입력 재생 확인.
- 녹화 3개와 완료 작업을 서버 재시작 뒤 복원, 기기 2대 available 확인.
- 동시 중복 제출, 같은 시각의 FIFO, 느린 기기 시작 중 다른 기기의 실행, 시작 중/실행 중 취소, 대기 timeout, digest 변경, cleanup 실패·격리를 테스트.
- 모바일 390×844와 데스크톱 1600×1100 확인. 긴 작업 이력 때문에 기기 종료 버튼이 화면 아래로 밀리던 레이아웃을 수정.

증거: [통합 검증 JSON](../artifacts/operations-validation.json), [재시작 복원](../artifacts/operations-restart.json), [데스크톱](../artifacts/operations-final-desktop.png), [모바일](../artifacts/operations-mobile.png).

P2의 단일 host 운영 기반과 P4/P5의 자체 자동화 도구를 진행했다. 연속 입력 helper·고FPS 미디어, 원격 worker·역할별 권한, WebDriver/Appium 호환, 실제 화면 AI와 기존 repair 엔진의 세션 통합은 다음 단계다.


실제 Android continuous-pointer/stream과 별도 worker 프로세스 검증을 [기기 Live 문서](DEVICE-LIVE.md)에 추가했다. 기존 합성 QA와 새 실기기 QA를 구분한다.
