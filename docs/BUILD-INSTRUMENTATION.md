# 소스 수정 없는 테스트 빌드 계측

아래는 기존 v1 기록·수정 프로필의 빌드 계측이다. 같은 PSI·ASM 준비기를 사용하는
일반 앱 관찰 경로는 [Android Views 자동 관찰](ANDROID-APP-OBSERVATIONS.md)을 따른다.

기본 `instrument` 모드는 Kotlin Activity의 원래 함수 본문을 수정하지 않는다.
앱 프로필에 지정한 클릭 핸들러를 분석하고, 테스트 variant를 빌드할 때 컴파일된 클래스에 계측을 추가한다.
개발자가 각 파일에 SDK 호출·로그·내보내기 버튼을 작성할 필요가 없다.

```bash
python3 -m reproof instrument --source PATH_TO_PUBLIC_APP_SOURCE \
  --app-profile PATH_TO_APP_PROFILE.json --output artifacts/new-build-instrumentation

python3 -m reproof build --source artifacts/new-build-instrumentation/source \
  --app-profile artifacts/new-build-instrumentation/app-profile.json \
  --output artifacts/new-instrumented-build
```

`--mode build`가 기본값이다. 소스에 hook을 직접 넣는 기존 방식은 `--mode source`로 선택한다.
기존 수동 SDK 통합도 계속 사용할 수 있다. 새 출력 디렉터리를 사용하며 원본 프로젝트는 덮어쓰지 않는다.

## 자동으로 처리하는 일

1. 명시적 프로필의 Activity와 버튼 ID에 연결된 Kotlin 클릭 리스너를 찾는다.
2. 새 작업 복사본에 로컬 Gradle 플러그인과 수집 런타임을 준비한다.
3. 선택한 Debug variant의 컴파일 결과에서 리스너 등록을 감싸고 lifecycle hook을 추가한다.
4. 클릭 전후의 허용된 숫자 상태, 소스 위치, 정상 반환·예외 여부를 SDK 이벤트에 연결한다.
5. Live의 Analyze & repair가 실행 UUID와 변환 증거를 확인한 뒤 같은 기록을 AI 수정·검증에 사용한다.

사용자는 계측할 코드 줄이나 lambda 이름을 지정하지 않는다. 기존 앱 프로필의 Activity·버튼·관찰 ID를 이용한다.
업무상 정상 조건, 초기 fixture와 앱 빌드 정보는 여전히 [앱 프로필](ANDROID-APP-PROFILES.md)에 필요하다.
현재 모든 앱의 업무 의미나 필요한 내부 로그를 자동 추론하지는 않는다.

## 원본과 테스트 빌드

원본 디렉터리는 읽기 대상으로만 사용한다. 준비한 복사본에서도 기존 `.kt/.java/.xml` 제품 파일과 manifest의
바이트를 보존한다. 추가하는 것은 보호된 Gradle 플러그인, 별도 런타임 디렉터리와 빌드 설정 연결이다.
일반 소스 디렉터리에 hook이나 release용 no-op 코드를 추가하지 않는다.

| 산출물 | 의미 |
|---|---|
| 준비 단계 `source/` | 지원하는 공개 입력의 복사본과 빌드 계측 설정 |
| `patch.diff` | 추가한 빌드 설정·런타임의 검토용 diff. 원래 Activity 본문 변경 없음. |
| `instrumentation.json` | 입력·준비 결과·프로필·계측 위치·diff의 hash |
| 보호 빌드의 `bytecode-report.json` | 실제 변환한 클래스·위치·lifecycle과 클래스/JAR hash |
| `receipt.json` | 소스·변환 증거·원본 APK·별도 플랫폼 driver를 고정한 빌드 증거 |

준비 단계의 `behaviorVerified: false`는 아직 컴파일과 동작 검증을 하지 않았다는 뜻이다.
실제 빌드에서는 모든 지정 위치가 정확히 한 번 변환됐는지 확인한다. 누락·중복·이미 계측된 클래스는 실패한다.
호스트는 보고서뿐 아니라 변환된 JAR의 실제 클래스 hash도 확인하고 원본·후보 빌드 receipt에 연결한다.

## 동작과 수집 경계

바이트코드 변환은 클릭 lambda 본문을 재작성하지 않는다. 기존 리스너의 호출을 감싸므로 컴파일된 조기 반환과
원래 예외 객체를 유지한다. 앱이 등록한 callback에는 같은 View를 한 번 전달하고, 수집기 실패가 앱의 예외를
덮어쓰지 않도록 한다. record mode 밖에서는 가능한 경우 원래 리스너를 그대로 등록한다.
record mode에서 리스너의 객체 identity나 stack trace 전체가 원본과 같다는 보장은 하지 않는다.

수집은 debuggable + `repro_mode=record` + 일치하는 fixture에서만 활성화된다.
문자열은 빈 값·QA·Test, 숫자는 최대 9자리로 제한한다. 임의 인자·예외 메시지·네트워크/DB 본문은 수집하지 않는다.
기존 debug manifest의 복사본에 DUMP 권한으로 보호한 receiver를 병합하고 그 파일을 debug manifest로 사용한다.
원본 manifest는 수정하지 않는다. 고정 receiver를 통해 내보내므로 앱 UI에 Report 버튼이 필요하지 않다.

Release에는 변환을 등록하지 않고 런타임 소스도 포함하지 않는다. 소스 삽입 모드와 달리 release용 hook 호출이나
no-op 런타임도 필요하지 않다. 실제 앱의 release 격리는 해당 variant 빌드와 APK 검사로 확인해야 한다.

## 현재 지원 범위

Kotlin Views의 단일 Activity와 지원하는 `setOnClickListener` 형태를 처리한다. ASM 변환은 AGP 8.13.2와
현재 로컬 JDK 17·Gradle 환경을 기준으로 개발한다. 플러그인과 의존성은 고정 캐시로 오프라인 빌드한다.
템플릿과 Kotlin 분석기는 [wheel 설치](INSTALLATION.md)에도 포함된다. Android 컴파일 도구와
고정 의존성은 별도로 준비해야 한다.

이미 `buildSrc`가 있는 프로젝트는 [공개 입력 목록](ANDROID-APP-PROFILES.md#빌드-입력과-검증-경계)의
`sourceInputs`를 사용한다. 기존 빌드 로직을 복사하고 별도 `reproof-build-logic`을 포함한다.
목록을 생략한 기존 준비 경로는 계속 새 `buildSrc`를 요구한다.
사용자 정의 debug manifest 경로, 모호한 클릭 대상·동일 줄의 여러 계측 위치도 별도 처리가 필요하다.
`onDestroy`가 없는 Activity의 종료 hook은 직접 상속한 `android.app.Activity`에서만 합성한다.
사용자 정의 superclass에서 상속한 종료 메서드는 final·접근 수준을 분석하기 전까지 빌드 단계에서 거절한다.
기본 복사 정책은 `.kt/.kts/.java/.xml`이며 명시적인 `sourceInputs`는 공개 assets·이미지·AIDL·
C/C++·version catalog·properties 등을 포함한다. 임의 트리의 자동 복사나 누락된 도구 설치 기능은 아니다.

Compose·Fragment, async 완료 시점·네트워크·DB 내부 상태와 프로세스 충돌 뒤 로그 영속 회수는
후속 범위다. 작업 복사본은 OS/컨테이너 빌드 샌드박스를 제공하지 않는다. 실제 앱 연결 전 필요한 공개 빌드 입력,
fixture·프로필과 원본/계측본의 동작을 확인한다.
별도의 [UIKit 관찰 경로](IOS-APP-OBSERVATIONS.md)는 fixture와 숫자식 수정 프로필을 요구하지 않는다.
이 문서의 Android v1은 여전히 해당 fixture·oracle·수정 제약을 적용한다.

## 실행 검증

2026-09-11, SDK 호출과 Report 버튼이 없는 합성 앱 `io.reproof.plain`에서 검증했다.
설치된 API 36 이미지로 별도 에뮬레이터를 만들었으며 실제 사용자 앱이나 물리 휴대폰 검증으로 해석하지 않는다.

| 항목 | 결과 |
|---|---|
| 준비 복사본 | 기존 지원 제품 입력 13개 모두 원본과 바이트 동일 |
| 실제 컴파일 입력 | 계측 직전 Activity 클래스가 원본 Debug 클래스와 동일 |
| 변환 | 클릭 위치 2개 + 시작·종료, 클래스·JAR hash 검사 통과 |
| 일반·조기 반환 동작 | 원본/계측본 모두 QA → `2`, Test → `0` |
| 예외 | 양쪽 모두 예외 후 앱 종료; JVM에서는 같은 예외 객체 전달 확인 |
| 자동 로그 | SDK 이벤트와 연결된 `0→2`, `0→0` 상태 변화 수집 |
| 입력·fixture | 비허용 입력과 잘못된 fixture의 내보내기 거절, 비허용 값은 SDK 파일에 없음 |
| Release | recorder/config/receiver/hook/no-op API 없음, 원본 제품 클래스 2개의 hash 동일, record 실행에도 로그 파일 불변 |
| 실제 Claude | 첫 제안으로 제품 증가식만 `1`로 수정, 계측·빌드·테스트 보호 |
| 반복 검증 | 원본 3/3·보호 회귀 검사 1개·수정본 3/3 통과 |
| 후보 Live | 새 세션에서 QA + Add → `1`, 현재 후보 bytecode 증거 검사 통과 |
| 종료 | 세션·브라우저·서버·helper·fixture·전용 에뮬레이터 종료, forward/lease 해제, 임시 AVD 삭제 |

같은 제품 소스·SDK 진단으로 실제 Claude 요청을 재구성해 prompt hash 일치도 확인했다. 이 확인은 추가 모델 요청을 하지 않는다.
화면은 로컬에 보관하며 외부 모델에는 승인한 합성 QA·제품 소스·진단을 전달했다.

전체 Python 검사 **250개**가 통과했다. 최종 상속 경로 거절 보완 뒤 해당 JVM/AGP 검사 **2개**를 다시 실행해 통과했고,
최종 소스로 Debug·Release 빌드와 위 기기 검증을 수행했다. 웹 코드는 변경하지 않아 기존 Node 검사 15개의 통과 결과를 재사용했다.

record mode에서 프로세스를 강제 종료한 뒤 시작하는 시간을 각각 3회 관측했다. `am start -W`의 TotalTime 중앙값은
원본 637ms, 계측본 759ms였다. 단일 에뮬레이터에서 순서대로 수행한 소규모 관측이며 보장된 오버헤드나 성능 목표 달성치가 아니다.

- [전체 검증 JSON](../artifacts/bytecode-validation.json)
- [준비 단계 diff](../artifacts/bytecode-final-prepared/patch.diff)
- [실제 클래스 변환 보고서](../artifacts/bytecode-final-built/bytecode-report.json)
- [Release 격리](../artifacts/bytecode-final-built/release-isolation.json)
- [동작 비교·자동 로그](../artifacts/bytecode-behavior-qa/result.json)
- [입력·fixture 거절](../artifacts/bytecode-safety-qa.json)
- [실제 AI 수정 보고서](../artifacts/bytecode-live-console/repairs/522f8172889443709a794eff85f7f31c/repair/report.html)
- [실제 수집 진단](../artifacts/bytecode-live-console/repairs/522f8172889443709a794eff85f7f31c/bundle/diagnostics.json)
- [실제 요청 hash 확인](../artifacts/bytecode-live-final/prompt-proof.json)
- [원본 2](../artifacts/bytecode-live-before.png) · [후보 1](../artifacts/bytecode-live-after.png)
- [후보·종료 증거](../artifacts/bytecode-live-final/result.json)

`bytecode-prepared`/`bytecode-built`는 최종 manifest·상속 거절 보완 전 빌드 진단 자료다.
최종 성공 근거는 `bytecode-final-prepared`/`bytecode-final-built`와 위 QA 경로다.
