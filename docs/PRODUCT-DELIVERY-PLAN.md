# 일반 앱 제품 경로 완성 계획

2026-09-15 · 실기기 전 소프트웨어 검증 완료

최종 목표는 회사 QA가 영상·실행 가능한 행동·자동 관찰·시작 조건을 남기고,
다른 개발자가 원격 Android/iPhone에서 같은 문제를 재현하며, AI 수정 후 같은
원본과 보호된 판정 기준으로 검증하는 것이다. 여러 Mac의 기기 공유도 포함한다.
로컬 구성 요소의 테스트 통과나 새 합성 앱으로 전체 완료를 대신하지 않는다.

[제품 공백 재점검](../artifacts/qa-delivery/product-gap-audit-r1.md)에 따라
외부 환경을 기다리기 전에 가능한 구현을 아래 순서로 진행한다. 기존
[G0–G9 계약](IMPLEMENTATION-DELIVERY-PLAN.md)과 원본·권한·격리·정리 기준은 유지한다.

| 단계 | 구현할 결과 | 완료 근거 | 상태 |
| --- | --- | --- | --- |
| 설치·시작 | 배포물에 UI·계측 템플릿·고정 도구 소스를 포함하고 설치 상태를 진단 | 개발 checkout/test 모듈을 사용할 수 없는 새 설치의 실제 CLI·HTTP·리소스 검사 | 완료 · D0 근거 아래 |
| 일반 앱 계측 | 명시적인 UIKit/Views 앱 프로필과 공개 빌드 입력, 기존 빌드 로직 보존 | 샘플 ID가 없는 앱의 준비·빌드·자동 수집, 원본/계측본 동작·Release 비교 | UIKit·Views 관찰 검증 완료 |
| 진단 증거 연결 | 수집한 진단의 허용 필드만 별도 파생 증거로 만들어 일반 수정에 연결 | 원본/run/source 바인딩, 전송 허용·거절, 비밀 값 제외·보존/만료 검사 | 완료 · D2 소프트웨어 근거 아래 |
| 한 Mac 제품 흐름 | 등록된 앱으로 시작·기록·조건 승인·원본 재현·수정 제안을 일반 서비스 경로에서 실행 | 설치된 서비스/CLI/브라우저의 이슈·영상·로그·제안 증거, test fixture import 없는 실행 | 완료 · D3 범위 아래 |
| 보호 실행과 복구 | 고정 서명/검사·기기 실행기·서비스 조합·provisioning·정리 실패 복구 | 실행 경계의 실패·취소·소유권·독립 검사와 실제 환경 qualification | 소프트웨어 검증 완료 · 실제 qualification 대기 |
| 실제 회사 수용 검사 | 승인된 AI, 회사 QA와 fixture, 서로 다른 두 Mac의 실기기 공유 | 원본 3회 결함·후보 3회 정상·회사 회귀 검사 및 단절/재시작/정리 증거 | 입력 필요 |

설치 단계는 표준 wheel과 가상 환경에서 검증한다. 이 Mac의 Python 3.11에
setuptools 80.9.0이 설치되어 있어 새 다운로드 없이 빌드를 실행할 수 있다.
런타임은 Python 표준 라이브러리를 유지한다. 기기·컴파일 도구·VM·계정이 없는
상태를 준비 완료로 표시하지 않는다.

설치 단계의 [D0 검증 기록](../artifacts/product-delivery/d0-package-r1/acceptance.json)은
wheel의 100개 공개 리소스, 새 설치의 CLI/HTTP와 실제 브라우저 시작·종료,
내보낸 Swift 영상 도구 소스의 컴파일, 관련 검사 38개 통과를 묶는다.
wheel 빌드 캐시 변조·누락·개인 파일 제외·sdist 재빌드도 검사했다.
[설치 안내](INSTALLATION.md)의 명령으로 시작할 수 있다.

UIKit은 [v2 관찰 프로필](IOS-APP-OBSERVATIONS.md)의 명시적 앱·프로젝트·타깃·UI ID와
공개 빌드 입력을 연결했다. [D1 UIKit 검증](../artifacts/product-delivery/d1-uikit-r1/acceptance.json)에 실제 Simulator의
원본/계측본 Debug·Release 네 서비스 실행, 자동 버튼·화면 로그, 실행 ID 교체,
실패 모드·Release 제외·정상 종료 근거가 있다. 설치 전 고정 사본과 설치 후 전체
해시 검사도 적용했다. UIKit 구현이 회사 앱·물리 iPhone·전체 제품 수용 검사를
대신하지는 않는다.

UIKit 단계의 [D1 wheel](../artifacts/product-delivery/d1-package-r1/repro_loop-0.1.0-py3-none-any.whl)은
새 가상 환경에서 일반 UIKit 준비·빌드·공유 서비스 실행까지 확인했다. D0 이후
Python 코드 캐시 검사도 보완했으며, UIKit 관련 고유 검사 98개가 통과했다.

Android는 [명시적 공개 입력](ANDROID-APP-PROFILES.md#빌드-입력과-검증-경계)과 기존 `buildSrc`를
보존하는 별도 포함 플러그인을 구현했다. [검증 기록](../artifacts/product-delivery/d1-android-r1/acceptance.json)은
관련 검사 77개, 고정 의존성 복구 검사 3개, 실제 Gradle 플러그인 공존, 원본/계측본 Debug·Release
빌드와 같은 제품 클래스·Release DEX를 묶는다. 현재 로컬 캐시에서 고정 의존성을 사용할 수 있어
다운로드 없이 복구했다. 이 Android v1 경로에는 fixture·숫자식 oracle·수정 제약이 남아 있으며,
일반 런타임에는 아래의 별도 Views 관찰 프로필을 사용한다.

이 단계의 [Android 공개 입력 wheel](../artifacts/product-delivery/d1-android-package-r1/repro_loop-0.1.0-py3-none-any.whl)은
이 변경까지 포함한다. [새 설치 검증](../artifacts/product-delivery/d1-android-package-r1/acceptance.json)에서
일반 CLI의 준비와 실제 Android 보호 빌드를 확인했다. 물리 기기·회사 QA 검증은 아니다.

별도 [Views 관찰 프로필](ANDROID-APP-OBSERVATIONS.md)은 fixture·숫자식·수정 정책 없이
일반 Android 세션의 자동 로그에 연결된다. [D1 Views 검증](../artifacts/product-delivery/d1-android-views-r1/acceptance.json)에
고유 검사 145개, 실제 원본/계측본 Debug·Release 빌드, 동일 제품 클래스·Release DEX,
공유 서비스의 버튼·화면 로그·실행 ID 교체·종료 후 보존을 묶는다. 원본과 계측본의
시작·버튼 조작·재시작 화면도 비교했다. 이 과정에서 드러난 helper의 JSON 배열,
일반 관찰 응답, 첫 실행의 준비 교착과 첫 화면 경합을 수정했다.

D1 [Views wheel](../artifacts/product-delivery/d1-android-views-package-r2/repro_loop-0.1.0-py3-none-any.whl)은
일반 관찰 준비·실제 빌드·공유 세션을 개발 폴더 밖의 새 설치에서 실행한다.
[설치 검증](../artifacts/product-delivery/d1-android-views-package-r2/acceptance.json)에 근거를 기록한다.
자동 관찰 로그는 진단 증거이며, 실행 가능한 행동 기록·시작 조건·보호 검증은
다음 단계에서 이어 연결한다. 회사 앱·AI·물리 기기·두 Mac 수용 검사는 남아 있다.

[D2 진단 연결](PROJECT-DIAGNOSTICS.md)은 명시적으로 수집한 앱 로그의 허용 필드를
일반 수정 제안에 연결한다. [D2 검증 기록](../artifacts/product-delivery/d2-diagnostics-r1/acceptance.json)에
관련 검사 173개, 별도 수신 저장소의 이슈 패키지 재현, 전송 필드 제한·원본 바인딩,
삭제·만료·권한 검사, 실제 HTTP·CLI 진단 내보내기를 묶는다. 앱·AI·미디어 일부는
명시적인 테스트 대역이며 실제 회사 수용 근거로 사용하지 않는다.

[D2 wheel](../artifacts/product-delivery/d2-diagnostics-package-r1/repro_loop-0.1.0-py3-none-any.whl)의
[새 설치 검사](../artifacts/product-delivery/d2-diagnostics-package-r1/acceptance.json)는
진단 모듈·CLI와 리소스 구성을 확인한 단계의 이력이다.

현재 [D3 wheel](../artifacts/product-delivery/d3-issue-flow-package-r3/repro_loop-0.1.0-py3-none-any.whl)은
[새 설치 검증](../artifacts/product-delivery/d3-issue-flow-package-r3/acceptance.json)에서 일반 Android 앱의
기록·조건·같은 원본 3회 재현·로컬 제안과 내보내기를 통과했다.
[D3 기록](../artifacts/product-delivery/d3-issue-flow-r1/acceptance.json)은 별도 실제 10분 자동 종료와
8,743개 영상 프레임의 독립 디코딩, 실제 iOS Simulator의 시각·로그·영상,
설치 UI와 고유 Python 검사 1,259개 범위의 근거를 묶는다.
세부 범위·실패 보존·보존기간·기본 한도는 [이슈 기록 안내](ISSUE-RECORDING.md)를 따른다.
앱 로그의 시계는 여전히 `app-elapsed-unmapped`다.

[D4 실행기·복구 계획](PROTECTED-ADAPTERS-PLAN.md)의 Android/iOS 고정 운영 어댑터와
서비스 조합을 구현했다. iOS의 명시적 초기화·3회 보호 재생·native 복구와 인증 CLI는
[보호 서비스](IOS-PROTECTED-SERVICE.md)에 정리했다. [r51 검증](../artifacts/product-delivery/d4-ios-service-r1/acceptance.json)은 594개 관련 검사와
[r18 설치본](../artifacts/product-delivery/d4-foundation-package-r18/acceptance.json)을 묶는다.
실제 VM·서명·기기 경계의 qualification은 해당 환경에서
측정해야 하며, 설정 파일이나 소프트웨어 대역의 결과로 대신하지 않는다.

일반 앱 지원은 먼저 명시적으로 구성된 UIKit/Views 경로를 완성한다. 실제 앱
스택을 받기 전에 Compose/SwiftUI나 모든 내부 함수/네트워크/DB 계측을 지원한다고
표시하지 않는다. 앱·fixture·관찰 계약은 자동으로 추정해 승인하지 않는다.

실제 회사 소스/QA의 AI 전송, 서명 자격증명 읽기, 새 네트워크 대상, 사용자 기기
운영은 기존 승인 범위를 확인한다. 현재 미제공인 회사 앱·fixture·AI 정책·VM
이미지/오프라인 toolchain·두 Mac은 남은 전체 목표의 입력이며 완료 기준에서
제외하지 않는다. 이전 Android 고정 의존성 검사 3개는 위 오프라인 실행에서 모두 통과했다.

변경 단계마다 실패를 재현하는 검사, 영향을 받는 기존 검사, 실제 실행 증거와
남은 제한을 기록한다. 이전 통과 결과와 실패 산출물은 보존하고, 변경되지 않은
검사는 재사용한다.
