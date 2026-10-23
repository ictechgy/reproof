# 설치와 로컬 시작

Python 3.11 이상과 pip가 필요하다. 설치된 Reproof Python 런타임의 외부
의존성은 없다. wheel 빌드에는 별도의 setuptools가 필요하다. 현재 portable CI는
Ubuntu의 Python 3.11, 3.12, 3.13에서 검사하며, 이것이 모든 OS와 네이티브
모바일 도구의 지원을 의미하지는 않는다.

wheel은 웹 UI, 자동 계측 템플릿, Android SDK/드라이버, iOS helper와 고정
네이티브 도구의 공개 소스를 포함한다. 기기용 바이너리, 서명 자격증명, VM 이미지,
회사 설정이나 과거 수용 검사 산출물은 포함하지 않는다.

회사 도입은 [회사 파일럿 준비 절차](COMPANY-PILOT.md)를 먼저 따른다. 아래 절차는
설치와 합성 콘솔을 확인할 뿐, 회사 앱 검증이나 enterprise 배포 완료를 선언하지 않는다.

## 정확한 배포물 선택

배포 담당자에게 선택한 소스 commit SHA, 해당 소스에서 만든 wheel의 원래 파일명과
SHA-256, 검사 결과를 받는다. 신뢰하는 전달 경로의 해시와 받은 바이트를 비교한다.
`reproof-qa`는 배포 패키지명이고 `reproof`는 CLI와 Python import 이름이다.
패키지명이나 버전 문자열만 같다고 checkout의 최신 기능이 포함됐다고 가정하지 않는다.

이 문서는 특정 PyPI 버전의 현재 게시 여부나 최신 소스와의 일치를 보증하지 않는다.
`HANDOFF.md`의 r14/r18 wheel 및 실기기 결과는 과거 실행 기록이며,
공개 checkout에 없는 `artifacts/` 경로를 설치 다운로드로 사용하지 않는다.

## checkout 밖의 새 작업 폴더에 설치

`RELEASE_WHEEL`을 검증한 wheel의 절대 경로로 설정하고, checkout 밖의 새 작업
폴더에서 실행한다. 파일명은 배포된 원래 wheel 이름을 유지한다.

```sh
: "${RELEASE_WHEEL:?Set RELEASE_WHEEL to the verified wheel's absolute path}"
python3 -m venv .venv
.venv/bin/python -I -m pip --isolated --disable-pip-version-check install \
  --no-index --no-deps --no-cache-dir "$RELEASE_WHEEL"
.venv/bin/python -I -c 'import reproof; print(reproof.__file__)'
.venv/bin/reproof installation-check
```

import 경로가 새 `.venv` 안인지 확인한다. `installation-check`가 종료 코드 0,
`status: "ready"`, `distribution: "installed"`, `actualVM: false`,
`actualMobile: false`를 출력해야 한다. `checkout` 결과는 설치된 배포물 검사로
대체할 수 없다.

이 검사는 선언된 공개 리소스의 존재, 형식, 바이트 해시를 확인한다. 기기나 인증
파일을 읽지 않고 네트워크에도 접속하지 않는다. `ready`는 컴파일 도구, VM, 실제
기기, 계정 권한이나 수정 검증의 준비 완료가 아니다. 해시는 unsigned manifest와의
일치 검사이며 배포자의 진위를 증명하는 서명이 아니다. 손상되거나 빠진 리소스는
`resource-integrity-failed`와 종료 코드 2로 알린다. 실패하면 원인을 해결하기 전에
실제 환경으로 진행하지 않는다.

## 선택 사항: 합성 로컬 콘솔

```sh
.venv/bin/reproof live-serve --demo --port 0 --output runtime/demo-r1
```

이 단계는 프로세스, 로컬 상태 파일과 loopback listener를 만든다. 출력한
`http://127.0.0.1:...` 주소를 로컬 브라우저에서 열고, 확인 후 Ctrl-C로 종료한다.
`--demo`는 명시적인 합성 기기이며 실제 기기를 연결하지 않는다. 재검사에는 새 출력
경로를 사용한다. 이 콘솔을 공유 서비스나 실제 앱 수용 검사로 취급하지 않는다.

공유 서비스를 시작하지 않고 공개 설정을 검토하려면
[읽기 전용 preflight](SHARED-PREFLIGHT.md)의 `live-check-config`를 사용한다.
실제 앱/기기 작업은 별도 승인과 [공유 QA 가이드](ISSUE-WORKFLOW.md),
[워커 가이드](WORKER-RUNTIME.md)의 환경 준비가 필요하다.

## 공개 소스 내보내기

설치 폴더를 빌드 작업 공간으로 사용하지 않는다. 다음 명령은 소스만 복사하며
컴파일하거나 기기에 접근하지 않는다.

```sh
.venv/bin/reproof export-resources --output-new native-sources
```

출력 경로는 존재하지 않아야 하며 부모는 실제 디렉터리여야 한다. symlink가 포함된
출력 경로는 거절하며 기존 출력은 덮어쓰지 않는다. `resource-manifest.json`에
선택된 파일과 해시를 기록한다. 이것은 실행 qualification이나 네이티브 빌드 성공
증거가 아니다. 컴파일, 서명, VM 구성과 실제 실행은 별도 승인 후
[보호 실행 가이드](REPAIR-EXECUTION.md)를 따른다.

설치된 Kotlin 분석기는 선언된 소스를 별도 임시 작업 공간에서 컴파일한다.
JDK 17과 고정 Kotlin 의존성의 로컬 캐시가 별도로 필요하며 자동 다운로드하지 않는다.
이 분석기는 아래 portable PACKAGE 검사에 포함되지 않는다.

## 소스에서 wheel 빌드와 portable 검증

깨끗한 선택 소스와 준비된 Python/pip/setuptools 환경을 사용한다. 빌드 요구사항은
`pyproject.toml`에 있으며, CI는 빌드 도구로 `setuptools==84.0.0`을 준비한다.
아래 명령 자체는 빌드 의존성을 다운로드하지 않는다. 없는 도구를 자동 설치하는 대신
사전에 승인된 도구 준비 절차를 따른다. `dist-r1`은 이 빌드에만 쓰는 새 경로다.

```sh
python3 -m pip --isolated --disable-pip-version-check wheel \
  --no-index --no-deps --no-build-isolation --wheel-dir dist-r1 .
python3 scripts/release-check.py --goal PACKAGE --effects filesystem,process,loopback
```

PACKAGE는 현재 interpreter로 임시 소스 사본에서 wheel을 만들고 새 가상 환경에
오프라인 설치한다. checkout 밖 CLI, 리소스 무결성, 합성 loopback 콘솔, 공개 소스
내보내기/덮어쓰기 거절, private 상태 제외, stale build cache 거절, sdist 재빌드와
설치된 읽기 전용 preflight/locator 모듈을 검사한다. Kotlin 분석/계측 3개 검사는
제외한다. 이 임시 wheel 검사는 별도로 전달할 `dist-r1` wheel의 해시/설치 확인을
대체하지 않는다.

`distribution-resources.json`이 공개 리소스를 명시한다. 빌드/기기 산출물이나
credential 파일을 재귀적으로 모으지 않는다. 선언하지 않은 캐시 자산/모듈은 거절하고,
파일 시각이 더 최신인 캐시가 현재 소스 바이트를 대체하지 못하게 한다.

나머지 명시적 portable gate와 증거 기록 방법은 [파일럿 준비 절차](COMPANY-PILOT.md),
검사의 범위는 [release 계약](RELEASE-CONTRACTS.md)을 따른다.
전체 test discovery나 `tests.test_distribution` 전체는 네이티브 도구가 필요한 검사를
포함하므로 portable 설치 검사로 실행하지 않는다.
