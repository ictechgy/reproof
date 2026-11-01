# iOS 상용 준비 spike 결과

설계 문서(`2026-11-01-ios-commercial-readiness-design.md`) 7장의 spike 결과를 모은다. 모든 수치는 실제 실행에서
측정했고, 추정이 섞인 곳은 "추정"으로 표시한다.

## S3 — Wikipedia 빌드

### 판정

**된다.** 고정 SHA의 Wikipedia iOS에서 `reproof app init` → `build` → `inspect`가 끝까지 성공했다
(첫 화면 계층 저장, `--point`로 고른 버튼을 `strength=strong`으로 다시 찾음).

다만 그대로는 되지 않았고, 다음 세 가지를 거쳤다.

| # | 막힌 곳 | 분류 | 대응 |
|---|---|---|---|
| 1 | `Configurations/OpenSourceDebug.xcconfig` 없음(빌드 설정 단계 오류) | 검증 앱 설정 | 파일을 손으로 만듦 |
| 2 | iOS 27.1 SDK 전용 API 사용(컴파일 오류) | 검증 앱 설정(환경: Xcode 27.1 필요) | iOS 27.0에서 동작이 같은 작은 patch(2곳) |
| 3 | 앱이 실행 직후 종료(App Group 컨테이너 없음) | **툴 일반 문제** | 서명 끄기 → ad-hoc 서명으로 수정(`b83476e`) |

1·2는 Wikipedia와 이 Mac(Xcode 27.0)의 조합 문제다. 회사 개발자의 기존 checkout에는 1의 파일이 이미 있고
(작업 사본은 `.gitignore`와 상관없이 소스 폴더를 복사한다), Xcode 버전도 회사 CI와 맞출 것이므로 회사 앱에서는
보통 생기지 않는다(추정). 3은 App Group·위젯을 쓰는 회사 앱이면 대부분 겪을 문제라 툴을 고쳤다.

### 환경

- Mac: Apple M4 Pro, 12코어, 메모리 24 GB, macOS 26.7.2
- Xcode 27.0 (27A266a), iOS Simulator 런타임 iOS 27.0, Python 3.14.7, git 2.54.0
- 설치하지 않은 도구: SwiftLint, ClangFormat, Tuist, Ruby gem(승인 범위 밖). Wikipedia의 `scripts/setup`도 실행하지 않았다.
- 작업 공간: 기본값 `~/.reproof`(`REPROOF_HOME` 미설정)

### 고정 SHA와 받기

- 저장소: `https://github.com/wikimedia/wikipedia-ios.git`
- 커밋: `09fa0f380510aedc4af515729cb6331b1a4acfcb` (2026-10-30T23:13:32Z 기준 `main` HEAD).
  2026-11-01 `git ls-remote ... refs/heads/main`도 같은 값이었다.
- pin 파일: `examples/simple-ios/wikipedia/pin.json`

```
$ python3 scripts/simple-fetch-app.py --pin examples/simple-ios/wikipedia/pin.json --output ~/.reproof/sources/wikipedia-ios
Cloning into '/Users/jinhongan/.reproof/sources/wikipedia-ios'...
HEAD is now at 09fa0f3805 Merge pull request #6335 from wikimedia/T440007/discover-setup
받았습니다: /Users/jinhongan/.reproof/sources/wikipedia-ios
  HEAD: 09fa0f380510aedc4af515729cb6331b1a4acfcb (pin과 같음)
```

17초, 파일 4,716개(`.git` 제외), 폴더 226 MB(`.git` 포함). 컨테이너는 `Wikipedia.xcodeproj` 하나,
`Package.resolved`는 `Wikipedia.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/`에 있다(원격 패키지 6개:
CocoaLumberjack, HCaptcha, rive-ios, RxSwift, SDWebImage, swift-log. 로컬 패키지: WMFComponents, WMFData,
WMFLocalizations, WMFTestKitchen).

### 측정 요약

| 항목 | 값 | 목표 |
|---|---|---|
| init 질문 수 | 2개 (앱 scheme 고르기 1, 테스트 scheme 확인 1) | — |
| init에서 사람이 직접 입력한 항목 | 2개 (`9`, `Wikipedia`) | 3개 이하 ✓ |
| init 밖에서 사람이 한 앱 준비 | 2건 (xcconfig 파일 만들기, Xcode 27.0용 patch) — 검증 앱 설정 | — |
| init 소요(빌드 제외) | 104.5초 (대화형, 답은 stdin으로 바로 줌) / 비대화형 시도 136.2초(선택 필요로 종료) | 10분 이내 ✓ |
| 패키지 해석 | 첫 build 24.9초, 이후 3.6–7.1초(작업 공간 캐시 재사용) | — |
| 앱 빌드(xcodebuild) | 성공 빌드 70.3초(서명 끔), 79.3초(ad-hoc 서명, 최종) | — |
| 빌드 성공 여부 | 성공 (검증 앱 설정 2건 + 툴 수정 1건 후) | — |
| inspect | 60.8초(빌드 재사용 포함), 첫 실행은 Simulator 생성·부팅·초기화·설치 약 34초 + 실행기 빌드 약 12초 | — |
| 첫 화면 interactive 요소 | 2개, 그중 identifier 있는 것 2개 (전체 요소 21개) | — |
| 설정부터 첫 inspect 성공까지 실제 경과 | 약 23분(15:04 받기 → 15:27 inspect 성공, 진단·툴 수정 포함) | 2시간 time-box ✓ |

참고:
- 빌드마다 새 DerivedData에서 처음부터 컴파일한다. `generic/platform=iOS Simulator`라 arm64와 x86_64를 모두
  만든다(Swift 컴파일 작업 1,467개 × 2, C/Objective-C 518개). 실행 파일: `x86_64 arm64`.
- 원격 패키지 6개는 이 Mac의 전역 SwiftPM 캐시(`~/Library/Caches/org.swift.swiftpm`)에서 가져왔다("(cached)").
  전역 캐시는 init의 `xcodebuild -list`가 먼저 채웠으므로, 첫 build의 24.9초는 완전히 빈 상태의 시간이 아니다.
  init 안에서 `-list`가 패키지를 해석하는 시간은 같은 소스 사본으로 따로 재서 약 58초였다.
- 빌드 로그 9.0 MB, 경고 450개. 출력 상한(64 MiB)까지 약 7배 여유가 있다.

### 실행 기록

모든 명령은 `~/.reproof/sources/wikipedia-ios`에서 `PYTHONPATH=<reproof checkout> python3 -m reproof app ...`으로
실행했다(`reproof` 명령은 설치하지 않음). 시각은 줄 앞 `[경과 시각]`이며 긴 로그는 줄였다.

#### init (비대화형) — 선택이 필요해 멈춤

```
$ reproof app init --non-interactive          # 136.2초, 종료 코드 2
문제: 앱 scheme 후보가 여러 개라 자동으로 고를 수 없습니다: Alpha, ContinueReadingWidget, Experimental,
Local Page Content Service & Announcements, NotificationServiceExtension, Performance Testing, RTL,
WidgetsExtension, Wikipedia, Wikipedia Stickers, WikipediaUITests
해결: `--non-interactive` 없이 실행해 직접 고르거나, 앞 후보 중 하나로 reproof.toml을 직접 작성하세요.
건너뜀: Update Languages (scheme 'Update Languages' 에서 앱(application) 타깃을 찾지 못했습니다.)
... (WMF, WMFComponents, WMFData, WMFDataMocks, WMFDataTestSupport, WMFLocalizations,
     WMFNativeLocalizations, WMFTestKitchen, Update Localizations — 모두 10줄)
```

scheme 21개 중 앱 타깃을 빌드하는 11개가 후보가 됐다. 후보별 앱 타깃을 따로 확인하니 9개가 `Wikipedia`
타깃이고(위젯·알림·스티커 확장 scheme은 호스트 앱을 함께 빌드하고, `WikipediaUITests`는 테스트 호스트로 빌드한다),
`Alpha`, `Experimental`은 별도 타깃이었다. Debug에서는 셋 다 번들 ID가 `org.wikimedia.wikipedia`다.

#### init (대화형) — 답 2개

```
$ printf '9\nWikipedia\n' | reproof app init  # 104.5초, 종료 코드 0
건너뜀: ... (위와 같은 10줄)
앱 scheme를 고르세요.
  1. Alpha
  ...
  9. Wikipedia
  ...
  11. WikipediaUITests
번호 입력 (1-11): 테스트 scheme은 'Performance Testing'(으)로 추정했습니다. Enter=수락, 다른 이름 입력, -=사용 안 함:
저장했습니다: /Users/jinhongan/.reproof/sources/wikipedia-ios/reproof.toml
  프로젝트: Wikipedia.xcodeproj  scheme: Wikipedia  테스트 scheme: Wikipedia
  앱 번들 ID: org.wikimedia.wikipedia  Simulator: iPhone 16 (latest)
질문 2개
환경 점검:
✓ xcode: Xcode 27.0
✓ ios_runtime: iOS 27.0
✓ git: git 사용 가능
✓ disk: 남은 공간 105.8 GB
ⓘ claude: claude 사용 가능 (AI 수정에 필요)
다음 단계: reproof app build
```

테스트 scheme 추정값 `Performance Testing`은 틀렸다. Wikipedia의 단위 테스트(`WikipediaUnitTests`)는 앱 scheme
`Wikipedia`의 테스트 동작에 들어 있어 `Wikipedia`를 직접 입력했다. Enter로 수락해도 답은 2개로 같지만, 그러면 틀린 값이 저장된다.
`doctor`의 `claude` 항목은 PATH에서 찾기만 하고 실행하지 않는다.

#### build 1 — 실패: 생성 xcconfig 없음

```
[   0.1s 15:09:32] 빌드 식별 중
[   0.1s 15:09:32] 작업 사본 만드는 중
[   1.2s 15:09:33] 패키지 받는 중
[  26.1s 15:09:58] 앱 빌드 중
[  33.5s 15:10:06] 문제: xcodebuild 실행이 실패했습니다.
                   해결: 로그를 확인해 원인을 해결한 뒤 다시 실행하세요.
                   로그: ~/.reproof/projects/wikipedia-ios/builds/6ce97a492db35eca.partial/logs/build.log
```

로그의 원인 줄:

```
.../source/Wikipedia.xcodeproj:1:1: error: Unable to open base configuration reference file
'.../source/Configurations/OpenSourceDebug.xcconfig'.
```

#### build 2 — 실패: Xcode 27.1 SDK 필요

```
[   4.9s 15:11:40] 앱 빌드 중
[  23.7s 15:11:58] 문제: xcodebuild 실행이 실패했습니다.
```

```
.../WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift:384:36:
error: value of type 'UITraitCollection' has no member 'verticalBarEdge'
...WMFNavigationBarConfiguring.swift:407:70: error: type 'UITraitCollection' has no member 'systemTraitsAffectingVerticalBarEdge'
```

#### build 3 — 성공(서명 끔)

```
[   1.3s 15:13:09] 패키지 받는 중
[   4.9s 15:13:13] 앱 빌드 중
[  75.2s 15:14:23] 빌드 완료: ~/.reproof/projects/wikipedia-ios/builds/9edc8552bee13db5/DerivedData/Build/Products/Debug-iphonesimulator/Wikipedia.app
[  75.2s 15:14:23] 빌드 ID: 9edc8552bee13db5
```

#### inspect 1 — 실패: 앱이 실행 직후 종료

```
[   1.9s 15:15:17] 빌드 완료: .../Wikipedia.app            # 재사용
[  89.7s 15:16:44] 문제: 실행기(127.0.0.1:63506)에 'launch' 명령을 보내지 못했습니다: timed out
                   해결: 실행기 세션이 살아 있는지 확인하고, 세션 로그를 본 뒤 다시 실행하세요.
```

세션 로그는 `Launch org.wikimedia.wikipedia` → `Setting up automation session`에서 멈췄다.
`~/Library/Logs/DiagnosticReports/Wikipedia-2026-11-01-151617.ips`(SIGABRT):
`-[NSURL initFileURLWithPath:isDirectory:]` ← `-[MWKDataStore setupLegacyDataStore]` ← `-[MWKDataStore initWithContainerURL:]`.
`WidgetsExtension-*.ips` 5개(EXC_BREAKPOINT)도 `SharedContainerCache.cacheDataFileURL.getter`에서 같은 원인으로 반복 종료했다.

#### build 4 — 성공(ad-hoc 서명, 최종)

툴 수정 뒤, 같은 빌드 ID의 완성 빌드가 재사용되지 않도록 `builds/9edc8552bee13db5`를 지우고 다시 빌드했다
(아래 "툴 발견" 2).

```
[   1.7s 15:24:28] 패키지 받는 중
[   8.8s 15:24:35] 앱 빌드 중
[  88.1s 15:25:55] 빌드 완료: .../builds/9edc8552bee13db5/DerivedData/Build/Products/Debug-iphonesimulator/Wikipedia.app
[  88.1s 15:25:55] 빌드 ID: 9edc8552bee13db5
```

`codesign -dv`: `flags=0x2(adhoc)`, 실행 파일에 `__TEXT,__entitlements`(App Group
`group.org.wikimedia.wikipedia` 포함)가 들어갔다. 이전 빌드는 `flags=0x20002(adhoc,linker-signed)`이고 이 영역이 없었다.

#### inspect 2·3 — 성공

```
$ reproof app inspect                          # 60.8초
[   2.1s 15:26:04] 빌드 완료: .../Wikipedia.app
[  60.2s 15:27:02] 화면 계층 저장: ~/.reproof/projects/wikipedia-ios/builds/9edc8552bee13db5/inspect-20261101-152702.json

$ reproof app inspect --point 355,788          # 60.5초
[  59.9s 15:28:21] 화면 계층 저장: .../inspect-20261101-152821.json
요소: 종류=button 라벨=다음 identifier=App Onboarding Next Button
근거: {"identifier": "App Onboarding Next Button", "elementType": "button", "label": "다음", "anchorIdentifier": "", "labelOrdinal": 0, "positionOrdinal": 0, "labelCount": 1, "positionCount": 1}
재탐색: strength=strong matched_by=identifier
```

이후 Wikipedia·WidgetsExtension 충돌 보고서는 새로 생기지 않았다.

### 첫 화면 계층

첫 실행 온보딩 소개 화면(한국어). 요소 21개 중 `INTERACTIVE_TYPES`(`reproof/simple/hierarchy.py`)에 드는 것은
`button` 2개이고 둘 다 identifier가 있다.

| 종류 | identifier | 라벨 | frame |
|---|---|---|---|
| button | `App Onboarding Learn More Link` | 위키백과에 대해 더 알아보기 | (32, 260.3, 177.7, 19.3) |
| button | `App Onboarding Next Button` | 다음 | (333, 766, 44, 44) |

그 밖에 `App Onboarding Intro View`(other), 이미지 2개, staticText 2개, 스크롤 막대 2개 등이 있다. 온보딩은
identifier가 잘 달린 화면이라, 앱 전체의 identifier 비율(설계 문서: 약 91개 파일)을 대표하지는 않는다.

### 검증 앱 설정 (Wikipedia 전용, 툴 기본값은 바꾸지 않음)

1. **`Configurations/OpenSourceDebug.xcconfig`**: 프로젝트 Debug 구성의 base configuration인데 `.gitignore`
   82번 줄에 있어 저장소에 없다. Wikipedia의 `scripts/setup_bundle_id`가 만들고, Xcode Cloud는
   `ci_scripts/ci_pre_xcodebuild.sh`에서 `scripts/setup_bundle_id ci`를 실행한다. 이번에는 스크립트를 실행하지 않고
   `ci` 모드와 같은 내용을 원본 사본(`~/.reproof/sources/wikipedia-ios`)에 직접 썼다.

   ```
   // This file was automatically generated, do not edit directly.

   DEVELOPMENT_TEAM=AKK7J2GV64

   SIGNING_DISAMBIGUATOR=
   ```

   `.gitignore` 대상이라 빌드 식별의 "작업 중 변경"에는 나오지 않지만, 작업 사본에는 복사되므로 소스 지문(build ID)에는 반영된다.

2. **Xcode 27.1 SDK 필요**: 커밋 `cbbfddf749`(2026-10-23, "Use the native large title when the system shows a
   vertical bar")가 iOS 27.1 SDK에만 있는 `UITraitCollection.verticalBarEdge`와
   `systemTraitsAffectingVerticalBarEdge`를 타입으로 쓴다. Wikipedia의 GitHub Actions도 `XCODE_VERSION: "27.1"`을 쓴다.
   같은 저장소의 `UIBarButtonItem+SwiftUI.swift`는 "CI builds with an older Xcode" 때문에 KVC로 우회하는데 이 파일은 그렇지
   않다. 이 Mac은 Xcode 27.0이고 Xcode 설치는 승인 범위 밖이라, 원본 사본에 아래 patch를 적용했다. iOS 27.1 미만
   런타임에서는 원래 코드도 `false`를 돌려주거나 바로 `return`하므로, iOS 27.0 Simulator에서 동작은 같다.
   빌드 식별에는 `commit 09fa0f3 + 작업 중 변경`으로 남는다(`dirty.patch`, `reproof.toml`은 추적 안 됨으로 기록).

   ```diff
   --- a/WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift
   +++ b/WMFComponents/Sources/WMFComponents/Extensions/Navigation Bar/WMFNavigationBarConfiguring.swift
   @@ -380,9 +380,7 @@
        private var hasVerticalBar: Bool {
   -        if #available(iOS 27.1, *) {
   -            return traitCollection.verticalBarEdge != .unspecified
   -        }
   +        // reproof 검증 앱 설정: Xcode 27.0 SDK에는 verticalBarEdge가 없다. iOS 27.1 미만에서는 원래도 false다.
            return false
        }
   @@ -402,12 +400,8 @@
        private func observeVerticalBarEdgeChanges(title: String) {
   -        guard #available(iOS 27.1, *) else { return }
   -        guard WMFVerticalBarEdgeObservers.registrations.object(forKey: self) == nil else { return }
   -        let registration = registerForTraitChanges(UITraitCollection.systemTraitsAffectingVerticalBarEdge) { (viewController: Self, _) in
   -            viewController.applyCustomLeadingLargeTitle(title)
   -        }
   -        WMFVerticalBarEdgeObservers.registrations.setObject(registration, forKey: self)
   +        // reproof 검증 앱 설정: Xcode 27.0 SDK에는 systemTraitsAffectingVerticalBarEdge가 없다. iOS 27.1 미만에서는 원래도 바로 return한다.
   +        return
        }
   ```

   다음 작업(심은 버그 등)을 위한 선택지: Xcode 27.1 설치를 승인받거나, 이 patch를 검증 앱 patch로 함께 관리하거나,
   `cbbfddf749` 이전 커밋으로 pin을 옮긴다.

### 툴 수정 (일반 문제)

- **Simulator 빌드 서명**(`b83476e fix: Simulator 빌드를 서명 끄기 대신 ad-hoc 서명으로 바꿔 entitlement 유지`):
  `CODE_SIGNING_ALLOWED=NO`면 entitlement가 실행 파일에 들어가지 않아 App Group 컨테이너가 `nil`이 되고, 이를 가정한
  앱·확장이 시작하자마자 죽는다. `simulator_build_argv`가 `CODE_SIGN_IDENTITY=-`(Sign to Run Locally)를 쓰게 바꿨다.
  키체인 인증서와 프로비저닝 프로필은 쓰지 않는다(빌드 로그: `Signing Identity: "Sign to Run Locally"`,
  `TeamIdentifier=not set`). 자동 서명·`DEVELOPMENT_TEAM`이 있는 Wikipedia에서도 오류 없이 빌드됐다.
  포터블 테스트 `test_build_argv_ad_hoc_signs_so_simulator_entitlements_are_embedded`를 먼저 실패시킨 뒤 고쳤고,
  MixedShop native 테스트(`test_simple_native_build`, `test_simple_native_cli`)도 같은 설정으로 통과했다.
  실행기(`ios/SimpleRunner`) 빌드는 entitlement가 없어 그대로 둔다.

### 툴 발견 (이번에는 고치지 않음, 후속 판단 필요)

1. **init이 전역 DerivedData에 큰 폴더를 남긴다.** init의 `xcodebuild -list`가 패키지를 해석하면서
   `~/Library/Developer/Xcode/DerivedData/Wikipedia-<해시>/SourcePackages`(1.1 GB)를 만든다. 경로가 매번 다른 임시
   사본 기준이라 Xcode가 다시 쓰지 않는 고아 폴더가 init 한 번마다 생긴다(이번 spike에서 2개, 2.2 GB).
   build의 `-resolvePackageDependencies`도 빌드 ID마다 8 KB짜리 `Logs` 폴더를 남긴다. 확인한 사실:
   `-list`는 `-derivedDataPath`를 받지 않는다(`The flag -scheme, -testProductsPath, or -xctestrun is required when
   specifying -derivedDataPath.`). `-list`에 `-clonedSourcePackagesDirPath <폴더>`를 주면 패키지는 그 폴더로 가고
   전역에는 8 KB `Logs`만 남는다. `-showBuildSettings`에 `-derivedDataPath`와 `-clonedSourcePackagesDirPath`를 함께
   주면 전역에 아무것도 남지 않는다(이미 해석된 패키지 폴더를 줬을 때 2초). 권고: discovery 호출에 init 임시 폴더(또는 공유 SPM 캐시)를
   `-clonedSourcePackagesDirPath`로, `-showBuildSettings`·`-resolvePackageDependencies`에는 `-derivedDataPath`도 준다.
2. **빌드 ID에 툴의 빌드 방법이 들어가지 않는다.** 서명 방식을 고친 뒤에도 빌드 ID가 `9edc8552bee13db5`로 같아,
   그대로 두면 App Group이 빠진 이전 앱을 재사용했을 것이다(이번에는 손으로 지움). 권고: 빌드 argv 규칙 버전(또는
   툴 버전)을 설정 지문이나 빌드 식별에 넣는다.
3. **앱 scheme 후보가 많다.** 11개 후보가 실제로는 앱 타깃 3개였다(9개가 `Wikipedia`). 권고: (타깃, 번들 ID)가
   같은 후보를 묶고, 컨테이너·타깃 이름과 같은 scheme(`Wikipedia`)을 기본값으로 보여 Enter로 수락하게 한다.
4. **테스트 scheme 추정이 틀렸다.** `"Test" in name` 규칙이 `Performance Testing`, `WMFDataTestSupport`,
   `WMFTestKitchen`을 후보로 잡고 정렬상 첫 값을 골랐다. 앱 scheme 자신이 테스트 동작을 가진 경우(Wikipedia)를
   우선하고, `Tests`로 끝나는 이름을 우선하는 것을 권고한다.
5. **init 출력의 "건너뜀" 10줄**은 앱 타깃이 없는 패키지·스크립트 scheme이라 정상인데 오류처럼 보인다. 한 줄 요약을 권고한다.
6. **실패한 빌드 폴더가 쌓인다.** 소스가 바뀌어 빌드 ID가 달라지면 이전 `*.partial`은 지워지지 않는다
   (이번에 166 MB, 983 MB가 남음).
7. **빌드 실패 안내가 원인을 보여 주지 않는다.** 두 실패 모두 원인은 로그의 `error:` 줄이었는데 화면에는
   "xcodebuild 실행이 실패했습니다"만 나온다. 첫 몇 개의 서로 다른 `error:` 줄을 함께 보여 주는 것을 권고한다.
   특히 `Unable to open base configuration reference file '<경로>'`는 "저장소에 없는(보통 설정 스크립트가 만드는)
   xcconfig가 필요하다"는 안내로 바꿀 수 있다.
8. **앱이 실행 직후 죽으면 안내가 엉뚱하다.** inspect 1은 `'launch' 명령을 보내지 못했습니다: timed out`으로만
   끝났다. 앱 충돌 보고서(`DiagnosticReports/<앱 이름>-*.ips`)나 앱 상태를 확인해 "앱이 실행 직후 종료됐습니다"로
   알리는 것을 권고한다.
9. **진행 표시가 없는 구간**: `build.log`는 xcodebuild가 끝나야 써지고, inspect의 Simulator 준비·실행기 빌드(첫 실행 약 46초)
   동안 출력이 없다. Wikipedia는 빌드가 80초 안팎이라 문제가 작지만, 더 큰 앱에서는 멈춘 것처럼 보일 수 있다.
10. **두 아키텍처 빌드**: generic Simulator destination이라 arm64·x86_64를 모두 컴파일한다. Apple Silicon에서 arm64만
    빌드하면 시간이 줄 것으로 추정하지만 측정하지 않았다(측정 뒤 결정).

### 경고 (첫 줄만)

- `warning: SwiftLint not installed, run "scripts/brew_install" script to install` — SwiftLint 단계는 경고만 내고 지나간다.
- `warning: tasks in 'Copy Headers' are delayed by unsandboxed script phases; set ENABLE_USER_SCRIPT_SANDBOXING=YES to enable sandboxing (in target 'WMF' from project 'Wikipedia')`
- `note: Run script build phase 'SwiftLint' will be run during every build because the option to run the script phase "Based on dependency analysis" is unchecked.` (`Update Localizations`, `Run Script`도 같음)
- 컴파일 경고 상위: `cannot find protocol definition for 'WMFBackgroundFetcher'`(86), `'weak' ownership of capture 'self' differs ...`(54), `'WMFAuthButton' is deprecated ...`(28). 전체 450개, 빌드에는 영향 없음.
- Run Script 단계(`Update Localizations`, `Copy UI Test Network Fixtures` 등)는 추가 도구 없이 통과했다.

### 매크로·플러그인 검증 실패 감지 (Task 7 이월 항목)

실제 "must be enabled" 문구는 **관찰하지 못했다**. 빌드·패키지·세션 로그 7개에서 `must be enabled`는 0건이다.
Wikipedia는 `Package.resolved`가 있어 버전 고정 빌드(`-skipMacroValidation -skipPackagePluginValidation`)로 돌았고,
원격 패키지 6개와 로컬 패키지 4개의 `Package.swift`에 `.macro(`, `.plugin(`, `CompilerPluginSupport`가 없다.
따라서 `reproof/simple/build.py`의 `is_validation_failure`는 실제 문구로 아직 검증되지 않았다. 검증하려면 고정되지 않은
매크로 패키지가 필요한데, 대부분 SwiftSyntax를 받아야 해서 승인 범위 밖이다.

### 이 spike가 남긴 것

저장소에 커밋한 것: `examples/simple-ios/wikipedia/pin.json`, `scripts/simple-fetch-app.py`와 테스트, 이 문서, 툴 수정.
Wikipedia 소스와 빌드 산출물은 커밋하지 않았다.

이 Mac에 남은 것(정리 여부는 사용자 판단):

- `~/.reproof/sources/wikipedia-ios` — 받은 소스 + `reproof.toml` + 위 xcconfig·patch
- `~/.reproof/projects/wikipedia-ios/builds/` — 완성 빌드 3.8 GB, 실패한 partial 2개(1.1 GB)
- `~/.reproof/cache/spm/` 1.1 GB, `~/.reproof/runner/` 155 MB
- `~/Library/Developer/Xcode/DerivedData/Wikipedia-*` 6개(init이 만든 1.1 GB짜리 2개, 8 KB짜리 4개 — 1개는 위 1번 확인 실험)
- `~/Library/Caches/org.swift.swiftpm/repositories`의 Wikipedia 의존성 6개(Xcode가 평소 쓰는 공용 캐시)
- `~/Library/Logs/DiagnosticReports/`의 `Wikipedia-2026-11-01-151617.ips`, `WidgetsExtension-2026-11-01-1516*.ips` 5개
- Simulator `reproof-wikipedia-ios`(켜 둠, 문서의 기본 동작). 다른 Simulator는 건드리지 않았다.
- inspect 1 실패 때 남은 세션 폴더(토큰이 들어 있을 수 있음)는 빌드 폴더 `9edc8552bee13db5`와 함께 지웠다.
