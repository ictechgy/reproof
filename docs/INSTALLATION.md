# 설치와 로컬 시작

Python 3.11 이상과 pip가 필요하다. Python 런타임의 외부 의존성은 없다.
wheel은 웹 UI, 자동 계측 템플릿, Android SDK/드라이버, iOS helper와 고정 네이티브
도구의 공개 소스를 포함한다. 기기용 바이너리·서명 자격증명·VM 이미지는 포함하지 않는다.

현재 [D4 r14 개발 배포물](../artifacts/product-delivery/d4-foundation-package-r14/dist/reproof-0.1.0-py3-none-any.whl)의
SHA-256은 `b56c7aa393c3db868b4ebf36557f15a8f077ef7b9aa5c4b2595b4d6344324c49`이다.
[설치 검증](../artifacts/product-delivery/d4-foundation-package-r14/acceptance.json)에는
153개 모듈·109개 공개 리소스의 해시, 설치된 ADB/검사기와 CLI 서버의 중단 후 복구를 기록했다.
설치된 iOS 입력 로더·준비·복구도 checkout/test 모듈을 쓰지 않는 별도 프로세스에서 검증했다.
기기와 grant provider는 명시적 metadata 대역이다. 전체 소스 검사 중 빠진 고정 Android 캐시는
복원한 뒤 실패한 검사만 재실행했으며, 합쳐서 고유 검사 1,883개의 통과 근거를 보존했다.
복구 검사는 소유한 프로토콜 서버를 사용했다. 실제 회사 앱·실기기·VM의 보호 실행 수용은
남아 있으며, 이전 검사에서 발생한 간헐적 SDK 멈춤도 원인이 확정되지 않았다.

이 개발본에는 Android 복구 전용 서버, iOS 파일 준비·복구·등록 입력, 명시적 서명·관찰 재료 등록 API가 포함된다.
[보호 서비스 구성](PROTECTED-SERVICE-CONFIGURATION.md)을 따른다. 실제 모바일 환경의
격리 검증기와 일반 보호 실행 CLI는 아직 완성되지 않았다.

## 새 작업 폴더에 설치

새 폴더에서 아래 명령을 실행한다. `RELEASE_WHEEL`은 제공받은 wheel의 경로다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps RELEASE_WHEEL
.venv/bin/reproof installation-check
.venv/bin/reproof live-serve --demo --output runtime/live
```

마지막 명령이 출력하는 loopback 주소를 브라우저에서 연다. `--demo`는 명시적인
합성 기기다. 실제 앱과 기기 연결은 [공유 QA 가이드](ISSUE-WORKFLOW.md)와
[워커 가이드](WORKER-RUNTIME.md)를 따른다.

`installation-check`는 배포 리소스의 존재·형식·해시를 확인한다. 기기나 인증 파일을
읽지 않고 네트워크에도 접속하지 않는다. `ready`는 설치 리소스가 준비됐다는 뜻이며
실제 기기·컴파일 도구·VM·수정 검증의 준비 완료를 뜻하지 않는다.
손상되거나 빠진 설치 파일은 `resource-integrity-failed`와 종료 코드 2로 알린다.

## 네이티브 도구 소스 내보내기

설치 폴더를 빌드 작업 공간으로 사용하지 않는다. 새 경로로 공개 소스를 내보낸다.

```sh
.venv/bin/reproof export-resources --output-new native-sources
xcrun swift build --package-path native-sources/native/macos-video \
  --scratch-path native-build/video -c release
```

출력 경로는 존재하지 않아야 하며 부모는 실제 디렉터리여야 한다. symlink가 포함된
출력 경로는 거절한다. 기존 출력은 덮어쓰지 않는다. `resource-manifest.json`에
선택된 파일·해시를 기록한다. 이는 도구 소스의 무결성 정보이며 실행 qualification이 아니다.
서명·VM 구성과 실제 실행은 [보호 실행 가이드](REPAIR-EXECUTION.md)를 따른다.

설치된 Kotlin 분석기는 선언된 두 도구 소스만 별도 임시 작업 공간으로 복사해
컴파일한다. 설치 폴더나 개발 checkout의 컴파일 결과를 사용하지 않는다.
JDK 17과 고정 Kotlin 의존성의 로컬 캐시는 별도로 필요하며 자동 다운로드하지 않는다.

## 배포물 빌드와 검증

checkout에서 setuptools wheel builder가 준비된 Python으로 빌드한다.
이번 검증에는 로컬 Python 3.11과 setuptools 80.9.0을 사용했다.

```sh
python3.11 -m pip wheel --no-index --no-deps --no-build-isolation \
  --wheel-dir dist .
python3 -m unittest tests.test_distribution -v
```

`distribution-resources.json`이 배포할 공개 리소스를 명시한다. 빌드·기기 산출물,
개인 설정이나 credential 파일을 재귀적으로 모으지 않는다. 빌드 캐시에 선언하지
않은 자산이 있으면 배포를 중단하며, 선언된 자산은 파일 시각과 관계없이 현재
소스 바이트로 다시 만든다.
Python 모듈도 같은 규칙을 적용한다. 목록에 없는 캐시 코드가 들어오면 거절하고,
파일 시각이 더 최신인 캐시가 현재 소스를 대체하지 못하게 한다.

설치 검사는 새 가상 환경의 실제 CLI/HTTP, 계측 분석기의 새 컴파일, 소스 배포물의
재빌드, 자산 변조와 오래된 빌드 캐시, 기존 출력 보존을 포함한다. 회사 앱과 실기기
검증은 [제품 경로 완성 계획](PRODUCT-DELIVERY-PLAN.md)에 별도로 남아 있다.
