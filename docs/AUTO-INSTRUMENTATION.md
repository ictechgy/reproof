# Android 소스 삽입 계측 옵션

기본 `instrument` 명령은 [소스 수정 없는 빌드 계측](BUILD-INSTRUMENTATION.md)을 사용한다.
이 문서는 `--mode source`로 선택하는 기존 소스 삽입 방식을 설명한다.

`instrument --mode source`가 앱 프로필에 지정한 Kotlin Activity의 클릭 핸들러를 찾아 계측 코드를 삽입한다.
원본 디렉터리는 보존하고 새 소스 복사본, 변경 diff, 계측 위치 목록, 새 프로필을 생성한다.
SDK 호출과 녹화 내보내기 버튼을 앱 개발자가 직접 추가하지 않아도 Live 녹화를 재현·수정 엔진에 연결할 수 있다.

현재 지원 범위는 **명시적 프로필을 사용하는 Kotlin Android Views Activity**다.
앱의 업무 의미나 필요한 모든 로그를 AI가 자동 판단하는 기능은 아니다. 실행 대상, 버튼·입력·숫자 상태 ID,
초기 fixture, 정상 조건, 빌드와 수정 범위는 [앱 프로필](ANDROID-APP-PROFILES.md)에 지정해야 한다.

## 실행

저장소 checkout에서 JDK 17과 로컬 Gradle 캐시의 Kotlin compiler 2.2.10을 사용한다.
`tools/kotlin-instrumenter/build.sh`는 캐시의 JAR로 PSI 도구를 컴파일하며 다운로드하지 않는다.
현재 일반 wheel 설치에는 Java 도구와 템플릿을 패키징하지 않으므로 checkout에서 실행한다.

```bash
python3 -m reproof instrument --mode source --source PATH_TO_PUBLIC_APP_SOURCE \
  --app-profile PATH_TO_APP_PROFILE.json --output artifacts/new-instrumented-app

python3 -m reproof build --source artifacts/new-instrumented-app/source \
  --app-profile artifacts/new-instrumented-app/app-profile.json \
  --output artifacts/new-instrumented-build
```

출력 디렉터리는 원본 밖의 새 경로여야 한다. 이미 계측한 소스를 다시 입력하거나 기존 런타임 파일을
덮어쓰려 하면 거절한다. 프로필은 구체적인 `Debug` variant를 지정해야 한다.
공개 소스 복사 정책은 `.kt`, `.kts`, `.java`, `.xml`이다. 실제 앱에 필요한 다른 빌드 입력 지원은 별도 작업이다.

| 산출물 | 용도 |
|---|---|
| `source/` | 계측된 작업 복사본. 원본 제품 파일을 덮어쓰지 않는다. |
| `patch.diff` | 바뀐 부분만 포함하는 검토용 diff |
| `instrumentation.json` | 원본·결과 파일 hash, 삽입 위치, profile digest와 patch hash |
| `app-profile.json` | `captureMode: debug_receiver`와 계측 위치를 고정한 새 프로필 |

`instrument` 결과의 `behaviorVerified: false`는 소스 변환만 완료했다는 의미다.
동작 보존은 원본·계측본 빌드와 실제 실행으로 별도 확인해야 한다. 빌드 receipt가 변환 receipt와 같은
소스·프로필을 고정하며, 이후 AI 수정에서는 계측 코드와 테스트를 보호한다.

## 삽입과 수집 범위

Kotlin PSI로 지정한 Activity와 `setOnClickListener` lambda를 분석한다.
`Button(...).apply { id = R.id.add; setOnClickListener { ... } }`, typed `findViewById`와
지원하는 지역 변수 연결을 처리한다. 모호한 ID 연결, 중복 대상, 지원하지 않는 수신 객체나 lifecycle 형태,
기존 수동 계측은 추측해서 고치지 않고 명령을 실패시킨다.

핸들러 앞뒤에 다음 역할의 호출을 넣는다.

1. 허용된 텍스트 변경을 SDK 의미 이벤트로 기록한다.
2. 버튼 ID·소스 위치와 지정한 숫자 상태의 호출 전 값을 기록한다.
3. 원래 lambda 본문을 실행한다. labeled return은 그대로 유지하며 예외는 같은 객체를 다시 던진다.
4. 정상 반환·예외 여부와 호출 직후 숫자 상태를 기록한다.

`onCreate`에 시작 hook, `onDestroy`에 종료 hook을 추가한다. 기록은 debuggable 앱에서
`repro_mode=record`와 일치하는 fixture ID/version으로 실행했을 때만 활성화된다.
디버그 source set에 recorder·설정·내보내기 receiver를 넣고 release에는 아무것도 기록하지 않는 API를 넣는다.
release에서도 main 소스의 호출과 제어 흐름 wrapper는 남는다. 원본과 bytecode나 실행 시간이 같다고 주장하지 않는다.

텍스트 허용 값은 빈 값·`QA`·`Test`, 숫자는 최대 9자리다. 임의 입력 인자·예외 메시지·네트워크 본문·DB 값은
수집하지 않는다. 비허용 입력이나 불완전한 상태를 만나면 앱 업무 동작을 계속 실행하되 캡처의 내보내기를 거절한다.

내보내기는 debug manifest의 고정 receiver를 ADB shell에서 호출한다.
receiver는 `android.permission.DUMP`로 보호되며 release APK에는 포함되지 않는다.
호스트는 broadcast 응답만으로 완료를 판단하지 않고 SDK의 현재 실행 UUID, 최종 sequence, profile digest와
완성된 진단 파일을 확인한다. 진단 파일의 각 클릭은 SDK tap 이벤트 및 프로필의 소스 위치와 1:1로 일치해야 한다.

예를 들어 검증 앱에서는 아래 상태를 실제로 수집했다.

```json
{
  "eventId": "e2",
  "target": "add",
  "siteId": "s26229907e30db8ef",
  "before": {"count": "0"},
  "after": {"count": "2"},
  "outcome": "returned"
}
```

이 진단은 bundle digest와 scenario digest에 연결되고 AI 입력·수정 보고서에 포함된다.
Live의 Reset & record → QA 입력 → Stop → Analyze & repair recording 흐름을 그대로 사용한다.
`--android-app`, `--app-profile`, `--repair-source`, `--repair-build`에는 같은 계측 산출물의 경로를 전달한다.

## 실제 실행 검증

2026-09-11, SDK 호출이나 Report 버튼이 없는 합성 앱 `io.reproof.plain`에 Add·예외 테스트 버튼
두 곳을 자동 계측했다. 설치된 API 36 이미지로 만든 전용 Android 에뮬레이터에서 검증했다.

| 검사 | 확인한 결과 |
|---|---|
| 원본·계측본 debug/release 빌드 | 모두 오프라인 빌드 통과 |
| 일반 클릭 | 양쪽 모두 QA + Add → count `2` |
| labeled return | 양쪽 모두 Test + Add → count `0` |
| 예외 핸들러 | 양쪽 모두 예외 후 앱을 관찰할 수 없음; tap ACK 성공을 요구하지 않음 |
| 자동 진단 | `0 → 2`, `0 → 0`을 각각 SDK 이벤트와 연결 |
| 허용하지 않은 입력·잘못된 fixture | 내보내기 거절, 비허용 입력은 SDK 파일에 없음 |
| release 격리 | recorder/config/receiver 없음, record intent로 실행해도 기존 SDK 파일 불변 |
| 실제 Claude 수정 | 첫 제안으로 제품 함수 증가량 `2 → 1`, 계측 코드는 보호 |
| 기존 재현·수정 기준 | 원본 3/3·보호 회귀 검사 1개·수정본 3/3 통과 |
| 후보 Live 재개 | 새 세션에서 QA + Add → count `1` |
| 정리 | 세션·브라우저·서버·helper·fixture·전용 에뮬레이터 종료, ADB forward/lease 해제, 임시 AVD 삭제 |

실제 요청의 prompt digest를 같은 제품 소스와 진단이 포함된 bundle로 재구성해 일치를 확인했다.
이 확인에서는 모델을 추가 호출하지 않았다. 화면 캡처는 로컬에 보관하고 모델에는 허용한 합성 QA·제품 소스·진단을 전달했다.

- [자동 생성된 patch](../artifacts/instrumentation-prepared/patch.diff)
- [동작 비교와 자동 캡처](../artifacts/instrumentation-behavior-final-qa/result.json)
- [입력·fixture 거절 검증](../artifacts/instrumentation-safety-qa.json)
- [release APK 구성 검사](../artifacts/instrumentation-built/release-isolation.json)
- [전체 검증 JSON](../artifacts/instrumentation-validation.json)
- [실제 AI 수정 보고서](../artifacts/instrumentation-live-console/repairs/f4c351e51de2449c85e660354b58f456/repair/report.html)
- [실제 자동 계측 진단](../artifacts/instrumentation-live-console/repairs/f4c351e51de2449c85e660354b58f456/bundle/diagnostics.json)
- [실제 요청에 진단 포함 확인](../artifacts/instrumentation-live-final/prompt-proof.json)
- [브라우저 원본 2](../artifacts/instrumentation-live-before.png) · [브라우저 후보 1](../artifacts/instrumentation-live-after.png)
- [후보·종료 증거](../artifacts/instrumentation-live-final/result.json)

이 소스 삽입 구현을 검증할 당시 전체 Python 검사 240개가 통과했다. 변경하지 않은 웹 코드의 기존 Node 검사 15개 결과는 재사용했다.
첫 예외 QA는 앱 종료 시 tap 명령도 실패할 수 있다는 조건을 검사 도구가 처리하지 못해 실패했다.
후속 실행에서는 원본·계측본 모두의 종료 상태를 따로 확인했으며 실패한 시도는 성공 근거에 포함하지 않는다.

## 남은 범위

실제 사용자 앱 소스는 아직 제공되지 않았다. Compose·Fragment·복잡한 listener 연결, iOS 자동 계측,
비동기 처리 완료 뒤 상태, 네트워크·DB 계측, 프로세스 충돌 뒤 진단의 영속 보존은 지원하지 않는다.
예외 경로의 제어 흐름은 보존하지만 프로세스가 죽은 뒤 로그를 항상 회수할 수 있는 crash reporter는 아니다.
새 앱은 빌드 입력·fixture·화면 구조를 확인하고 그 앱의 동작 비교를 다시 수행해야 한다.
실제 앱의 소스나 데이터를 외부 모델에 전송하는 범위도 연결 시 별도로 정한다.
