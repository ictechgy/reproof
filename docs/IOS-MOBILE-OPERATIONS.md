# iOS 작업의 영속 IPA 준비

`IOSMobileOperationStore`는 후보 IPA와 원본·선택된 helper IPA를 하나의 작업·요청·기기 scope에
묶어 보관하고 `.app`으로 준비한다. 이 단계는 파일 준비이며 서명 검증·설치·재생 권한을 발급하지 않는다.

`IOSMobileDefinition`은 project/application/runtime policy, 공개 기기 ID와 UDID의 canonical scope,
원본 profile digest, 명시적 조회 도구 구성 digest, 원본/helper `BlobSet` digest를 고정한다.
helper를 지정할 때는 host/runner 두 역할과 각각의 bundle ID를 함께 지정한다.
이 선언은 [서비스 입력 로더](PROTECTED-MOBILE-INPUTS.md)에서 실제 등록된 runtime과 대조한다.

`admit(context, candidate, baselines)`는 기존 `RunStore.repair_scope_lease()`로 같은 기기를
하나의 canonical 로컬 저널에 고정한다. 기존 불확실한 작업을 새 journal/owner root로 우회할 수 없다.
이는 실제 기기 lease나 native 프로세스의 잠금 상속을 대신하지 않는다.

IPA를 쓰기 전에 원본 요청과 후보 SHA-256·baseline digest를 확인하고 디스크 용량을 예약한다.
예약은 모든 압축 IPA, 보존할 확장 앱, 추출 중인 앱·snapshot과 metadata를 보수적으로 포함한다.
각 IPA는 최대 64 MiB, 각 확장 앱은 최대 512 MiB의 기존 제한을 적용한다. 모든 입력·추출 사본은
작업 저널 아래에 둔다. 원래 디렉터리·파일 inode를 기록하고 symlink·다른 작업/요청을 거절한다.
원본/helper 입력이 후보 역할을 덮어쓰는 구성도 받지 않는다.

`prepare(operation, role, ...)`는 취소·기한·살아 있는 원래 작업을 확인하고 컨테이너를 다시
해시 검사한다. bounded IPA parser로 확인한 앱의 bundle ID와 app digest가 맞아야 준비 결과를
반환한다. 살아 있는 작업은 발급 당시 intent digest도 기억하므로 IPA와 저장된 intent를 함께
고쳐 입력을 바꿀 수 없다. JSON만으로 원래 작업 capability를 복원하지 않는다.
준비 callback은 원래 producer 잠금 descriptor를 복제해 보유한다. admission 문맥이 먼저
끝나더라도 실제 callback이 반환할 때까지 이 파일 잠금이 유지된다. 이는 기기 도구 자식에
기기 lease를 상속한 증거와는 별개다.

프로세스 중단·파싱 실패·미완료 종료에는 파일과 예약을 보존한다. `RunStore`의 일반
`finish(stopped=True)`도 준비 파일이 남은 예약을 해제하지 못하도록 별도 hold를 둔다.
`close()`는 진행 중인 준비 callback이 끝날 때까지 기한 안에서 기다리며, 파일이나 예약을
해제하지 않는다. `False`이면 아직 callback/작업이 살아 있다.

새 프로세스는 같은 정의와 `create=False`로 저장된 상태를 확인할 수 있다. `status()`는
엄격한 스키마의 저널 상태를 보고하며 파일의 현재 유효성이나 기기 정리를 새로 증명하지 않는다.
실행 권한은 `none`, `deviceCleanupConfirmed`는 `false`다. 공개 상태에 UDID나 임의의
저널 필드를 넣지 않는다.

## 준비 파일의 복구·예약 반환

`preparation_recovery(operation_id, request_digest, cancellation=..., deadline_monotonic=...)`는
원래 canonical scope, RunStore 작업 잠금과 producer 잠금을 획득한 뒤 준비 파일을 정리한다.
늦게 끝나는 callback의 descriptor가 남아 있으면 시작하지 못한다. 원래 request·context·정의·
baseline digest·디렉터리/파일 inode를 다시 확인하며, 완전한 원래 intent가 없거나 손상됐으면 거절한다.

복구는 원본 IPA를 마지막까지 유지한다. 같은 bounded ZIP metadata 검사로 남은 파일의 경로·
종류·상한을 대조하고, 다른 파일·symlink·hardlink는 보존한 채 거절한다. IPA에 별도로 쓰이지 않은
상위 디렉터리도 추출 전 파일 수 한도와 대소문자/파일 충돌 검사에 포함한다. 복구 중 새 앱 사본을
추출하지 않는다. archive가 이미 없으면 대응 앱·추출 사본이 모두 사라졌음을 직접 확인해야 한다.

context manager가 반환하는 capability는 현재 프로세스·스레드·원래 owner와 보유 중인 잠금에
묶인다. `RunStore.finish_ios_preparation_recovery(capability, authority=owner)`가 이를 한 번만
소비해 run hold를 지우고 예약을 0으로 만든다. 최종 상태는 `failed` 또는 원래 작업이 취소된
경우 `cancelled`다. 저장된 `discarded`/`completed` JSON, 복사한 capability, 다른 저장소나
종료된 context는 정리 권한이 아니다. 파일만 정리하고 capability를 소비하지 않으면 예약은 유지된다.

파일 삭제 도중, run hold 삭제 후 예약 commit 전, commit 후에 프로세스가 사라져도 새 owner가
남은 파일과 원래 저널을 다시 확인해 이어간다. `recovery.json`은 진행 기록이며 단독으로 권한을
주지 않는다. `status()`의 role 상태는 과거 준비 기록이므로 현재 파일 존재를 뜻하지 않는다.
이 경로는 기기 명령이 없는 preparation-only 형식만 지원한다. 기기 sanitation이나 native 기기
소유권을 해제하지 않는다.

`D4_IOS_MOBILE_PREPARATION`은 자체 inert IPA의 예약·준비·변조·취소·다른 root 우회 거절과
추출 후 실제 자식 프로세스가 즉시 종료된 뒤의 파일/예약 보존을 검사한다. OS 기기 명령은 실행하지 않는다.
고정 설치·XCTest와 재생 연결 API는 아래와 [고정 실행 연결](IOS-FIXED-RUNTIME.md)을 따른다.
sanitation, 기기 실행 이후의 복구, 보호 서비스 조합과 실제 환경 qualification은 남아 있다.
`ProtectedMobileSupervisor`의 기존 Android 전용 영속 adapter 검사는 그대로 유지한다.

## 원래 기기 소유권과 네이티브 조회

모든 역할의 앱을 준비한 뒤 `native_owner(operation, device_authority)`로 원래 기기 소유권을
연결한다. 살아 있는 `HostAuthority`가 실제로 발급한 동일 iPhone의 `DeviceAuthority`만 받는다.
현재 grant·generation·host/helper incarnation과 원래 작업을 검사하고, `native.json`과
`state.json.nativeBindingDigest`를 저장한 뒤에만 잠금 descriptor를 빌려준다.
공개 상태의 `nativeOwnership`은 과거 바인딩 기록이며 현재 프로세스가 살아 있다는 증거는 아니다.

`owner.borrow_descriptors()`는 원래 producer·기기 잠금의 열린 파일 description을 복제한다.
다른 inode, 같은 inode를 새로 연 descriptor, 복사한 capability, 다른 스레드·프로세스와 끝난
context의 재사용은 거절한다. 서비스 종료는 살아 있는 owner·export·조회 프로세스의 수거를 기다린다.
이 API는 새 기기 명령의 dispatch permit이나 sanitation 권한을 발급하지 않는다.

등록된 `IOSDeviceQueryDefinition`에 고정 native guardian을 포함하면
`definition.open_client(native_owner=owner)`가 원래 잠금을 상속하는 조회 클라이언트를 만든다.
[조회 도구 문서](IOS-DEVICE-QUERIES.md)의 부모/guardian 종료 경계를 따른다.

native 소유권에 진입한 작업은 파일 준비 전용 복구로 되돌릴 수 없다. 바인딩 기록 저장 중이나
descriptor export 후 프로세스가 종료돼도 원본·후보·예약을 보존한다. `native.json`이나 state의
바인딩 중 하나만 남아도 준비 파일 복구를 거절한다. 이후의 실제 기기 복구·정리 경로는 아직 남아 있다.
이 보수적인 경계는 조회만 실행한 작업에도 적용한다.

## 고정 설치와 원본 재설치 명령

`definition.open_installer(native_owner=owner)`는 같은 고정 조회 정의와 원래 native owner를
사용한다. `installer.payload('install-candidate')` 또는 `payload('restore-original')`로
현재 context·바인딩·앱/IPA digest·bundle·기기 scope에 묶인 명령 내용을 얻는다.
호출자는 이 payload digest로 실제 `DeviceAuthority`의 dispatch permit을 발급받은 뒤
`installer.run(kind, permit=permit, cancellation=event, deadline_monotonic=deadline)`을 호출한다.
이 API가 qualification이나 permit을 발급하지는 않는다. 보호 서비스에서 qualification을
검사해 이 API를 연결하는 작업은 남아 있다.

명령은 `device install app --device <고정 UUID> <원래 역할의 App.app> --json-output <고정 작업 결과>`다.
`install-candidate`는 `candidate/App.app`, `restore-original`은 `original/App.app`만 선택한다.
임의 경로·역할·argv를 받지 않는다. helper 설치·제거·XCTest는 이 API에 포함되지 않는다.
두 명령은 각각 한 작업에서 한 번만 시도할 수 있다. 실패한 시도의 반복 실행은 별도 복구 구현을 기다린다.

새 native 바인딩은 schema 2이며 모든 준비 앱 digest를 바인딩 자체에 포함한다.
과거 schema 1 기록은 상태 조회용으로 읽을 수 있지만 설치 owner로 사용할 수 없다.
호출 시 원래 IPA와 준비 앱을 다시 확인하고 고정 details 조회로 실제 선택 기기를 대조한다.
permit의 payload·generation·incarnation·기한을 원래 authority 저널과도 대조한다.

작업 아래 `command-install-candidate-work/` 또는 `command-restore-original-work/`를 독점 생성하고
`intent.json`과 `state.json`을 저장한다. `attempted`는 시도 기록, `dispatching`은 명령을
허용하기 직전에 기록한 상태, `tool-succeeded`는 정상 수거와 응답 검사를 마친 상태다.
디렉터리만 생성된 중단도 재시도를 막는다. 살아 있는 owner는 시도한 명령을 기억하므로
저널 디렉터리를 옮겨 같은 permit을 재사용할 수 없다.

native guardian은 원래 잠금·고정 도구·역할 경로를 확인한 후 pipe로 준비 완료를 알리고 기다린다.
Python이 앱을 다시 검사하고 `dispatching`을 영속화한 뒤 현재 permit·취소 상태를 재확인해야
SDK 자식 생성이 허용된다. guardian은 같은 Mac의 `mach_continuous_time` 기한을 검사한다.
Python이 일시 정지되거나 Mac이 잠들어도 기한이 연장되지 않는다. 부모 pipe EOF나 기한 만료에는
직접 자식을 종료·수거한다. guardian만 강제 종료되면 자식이 원래 잠금을 유지하는 경계는 조회와 같다.

반환되는 관찰은 CoreDevice 성공 응답과 선택 bundle의 `installedApplications` 확인만 뜻한다.
호출자가 별도로 명령 receipt를 확정해야 다음 authority 작업을 발급할 수 있다.
설치된 바이너리의 원본 일치·서명·앱 데이터 초기화·daemon 종료·sanitation을 증명하지 않는다.
따라서 `installedArtifactVerified`와 `deviceCleanupConfirmed`는 `false`이며 파일과 예약을 보존한다.
원본 재설치 성공도 기기 복구 완료가 아니다. 결과 파일은 크기를 제한해 원래 작업 아래에 보존한다.

`tests.test_ios_mobile_install`은 자체 Mach-O SDK 대역으로 고정 경로·저널 순서·권한 변조·
기한 만료·취소·재시도 거절·잘못된 기기/응답과 실제 OS 프로세스 종료를 검증한다.
실제 CoreDevice 응답 호환성, 설치된 artifact 증명, 기기/service 격리와 회사 앱 수용은 별도다.

## 고정 XCTest와 실행 앱 식별

`IOSXCTestTools`는 실제 Xcode 실행 파일, developer root, guardian, Xcode가 생성한
`IOSXCTestTemplate`의 경로·해시를 고정한다. `IOSMobileDefinition.xctest_definition_digest`를
지정하면 original/candidate 각각 최대 세 회의 실행을 위해 추가로 384 MiB를 예약한다.
helper host와 runner IPA가 모두 필요하며, runner의 직접 `PlugIns/ReproLiveTests.xctest`를 검사한다.

`IOSXCTestRunner.prepare(...)`는 원래 준비 앱 경로로 생성 템플릿을 재배치하고 필요한 환경만
추가한다. 생성된 진단·attachment·target metadata는 보존한다. 다른 테스트 선택, skip,
자동 재시도와 병렬 실행은 허용하지 않는다. `start(launch, permit=...)`는 실제 발급된 동일
payload/provider permit을 요구하며 dispatch operation ID와 fingerprint도 저널에 남긴다.

Xcode는 `/dev/fd`와 그 링크를 `.xctestrun` 입력으로 받지 않는 것으로 실제 SDK 검사에서
확인했다. guardian은 읽기 전용의 이름 있는 설정 사본을 만들고 실행 직전 해시와 출력 경로를
다시 확인한다. 이 검사는 동일 UID의 다른 프로그램에 대한 파일 시스템 격리를 증명하지 않는다.
helper 파일의 쓰기 격리와 SDK/daemon 격리는 실제 환경 qualification의 별도 조건이다.

guardian은 SDK를 별도 프로세스 그룹으로 실행한다. 부모 종료·기한 만료·일반 취소에는
원래 자식 PID를 수거하기 전에 그 그룹을 종료하고, 그룹이 사라진 뒤 잠금을 놓는다.
Darwin의 zombie-only 그룹에서 발생하는 `EPERM`을 정리 완료로 해석하지 않는다.
guardian 자체가 강제 종료된 경우에는 기존의 보수적인 native 복구 경계를 유지한다.
Python은 별도 completion pipe에서 guardian의 수거 완료 바이트를 받아야 호스트 종료를
확인한다. SDK에는 이 pipe의 쓰기 권한을 넘기지 않는다. guardian의 PID 종료나 pipe EOF만
관측하면 SDK가 나중에 종료되더라도 현재 owner는 불확실한 상태를 유지한다.
조기 취소는 먼저 liveness pipe로 철회를 전달해 초기화 중인 guardian도 종료 기록을 남길 수 있게 한다.

세션은 실행 중과 프로세스 종료 후 출력 크기를 검사한다. 64 MiB/8,192개 한도를 넘으면
자식 수거 후 생성 출력만 정리하고 입력·작업 기록·제한 초과 요약을 보존한다.
정리 기한이 끝나거나 경로가 바뀌면 `close()`가 false를 반환하며 후속 정리를 재시도할 수 있다.
이 관측 한도는 CoreDevice daemon이나 다른 프로세스에 대한 파일 시스템 quota가 아니다.

`IOSNativeCallbackCoordinator`는 동일한 native owner를 `invoke_fixed`의 서로 다른 작업
스레드에서 순서대로 사용할 수 있게 한다. 직접적인 다른 스레드 접근은 계속 거절하며,
진행 중인 export/자식, 복사된 capability, 순서가 틀린 phase는 소유권을 넘겨받을 수 없다.
저장된 phase JSON은 권한을 재구성하지 않는다.
콜백이 살아 있는 자식이나 export를 남기거나 종료 기록 저장에 실패하면 native owner 자체를
차단한다. 원래 생성 스레드로 돌아왔다는 이유로 다른 조회·명령을 시작할 수 없다.

설치 후에는 [설치 identity 관찰](IOS-INSTALLED-IDENTITY.md)로 bundle/version/build를 대조한다.
새 SDK는 앱 자신의 bundle ID·embedded build ID·실행 UUID·profile digest를
`runtime-identity.json`에 기록한다. 이 파일은 고정 `appDataContainer` 경로로만 읽고,
실제로 발급된 XCTest launch 및 준비된 앱과 대조한다. helper의 설정 echo와 구분한다.
증거 등급은 [iOS 설계](IOS-DESIGN.md)의 `ios-install-receipt-runtime-id`이며 직접 설치본 SHA가 아니다.

준비 단계의 운영 조회·복구는 [ios-mobile CLI](IOS-MOBILE-CLI.md)를 따른다. 이 CLI는
native-bound 작업을 파일 준비 복구로 해제하지 않는다. 앱별 초기화 계약과 실제 환경의
기기 복구·qualification이 없는 상태에서 sanitation이나 전체 보호 실행 완료를 표시하지 않는다.
