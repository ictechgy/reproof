# Android 기기 작업의 영속 저널

2026-09-14 · D4 개발 경로. 실제 기기 격리 qualification과 운영 복구 CLI는 남아 있다.

`ProtectedRepairComposition.configure_android_mobile()`은 같은 서비스·프로젝트·원본
앱에 등록한 고정 Android 어댑터를 `AndroidOperationStore`와 연결한다. 호출자는 같은
`QualificationAuthority`가 발급한 살아 있는 mobile qualification, 고정 서명 감독자와
독립 검증기를 전달해야 한다. 저장한 `qualified` JSON이나 VM qualification으로 이
단계를 활성화할 수 없다. 현재 일반 CLI에서 이 전체 조합을 만드는 설정은 아직 없다.

## 설치부터 정리까지

작업은 canonical 기기 scope를 잡고 원본 요청·환경·RunStore 루트·프로젝트·수집 정책·
원본 앱 프로필·도구 hash·fixture 계획과 payload digest를 고정한다. payload 본문이나
서명 재료는 저널에 넣지 않는다. 원본과 helper는 링크 없는 descriptor로 열고 소유권·
파일 크기·hash를 검사한다.

세 APK의 빈 inode와 작업 의향을 먼저 기록한 뒤 실제 후보·원본·helper 크기의 합계에
메타데이터 512 KiB를 더한 용량을 예약한다. 명시적인 ADB endpoint가 있는 새 작업은
native 명령·결과용 17,522,688바이트도 더한다. 예약 후에만 비공개 staging에 바이트를
쓴다. 새 감독자 경로는 기존 callback API의 1바이트 예약이나 임시 디렉터리를 쓰지
않는다. 준비 도중 중단되면 부분 사본과 예약이 남는다.

설치 시 Lab의 retained scope를 획득한 직후 실제 소유권 generation·host/helper
incarnation을 기록한다. 이 기록을 끝내기 전에는 ADB나 APK 검사기를 실행하지 않는다.
설치·각 재생·마지막 정리는 원래 producer inode의 잠금과 프로세스 내 일회성 phase
capability로 묶는다. `MobileContext`의 내부 operation capability는 digest나 공개
문서에 포함하지 않으며, 복사한 capability를 받아들이지 않는다.

선택한 `nativeGuardian`이 있으면 실제 phase에서 원래 FD를 넘겨 SDK 명령과
instrumentation을 실행한다. 명시적으로 발급한 dispatcher만 callback 스레드에 그
권한을 연결하며 helper 요청에도 phase·취소 검사를 적용한다. 진행 중인 dispatch가
있으면 phase 완료를 거절한다. 명령·instrumentation slot은 각각 하나이며 정상 host/gateway 수집
뒤에 재사용한다. 부분 파일 작성·취소·종료 불명확 상태는 예약과 함께 보존한다.
[명시적인 ADB 경계](SCOPED-ADB-TRANSPORT.md)의 설정과 실행 범위를 따른다.

각 native phase 시작 전에는 staging의 세 APK를 다시 검사한다. phase의 `completed`는
해당 callback 결과가 기록됐다는 뜻이다. 단독으로 프로세스 종료·fixture 정리·기기
격리나 후보의 성공을 증명하지 않는다. 감독자는 원래 G4 결과와 독립 검증을 계속
요구한다.

정상 정리는 fixture 완료·도구 종료를 확인하고 원본 APK를 복원한 뒤 앱 데이터를
지우고 남은 앱 프로세스를 검사한다. 그다음 원래 inode와 내용이 일치하는 세 사본만
삭제하고, 기기 scope 반환과 마지막 phase 기록을 끝낸다. 이 과정이 모두 확인돼야
감독자가 예약 용량을 해제한다. scope 반환이나 마지막 저널 기록에 실패하면 실제
사본 삭제가 끝났더라도 quarantine과 예약을 유지한다.

설치 전 취소는 native binding을 만들어내지 않는다. 아직 native 소유권을 얻지 않은
정확한 operation의 사본만 정리한다. 소유권을 획득했지만 영속 binding을 기록하지
못했다면 native 동작을 실행하지 않으며 정리를 확인했다고 보고하지 않는다.

## 상태와 복구 경계

`AndroidOperationStore.status(operation_id)`는 현재 구성·원본 요청·inode·phase를
대조해 정적인 상태를 반환한다. `recovery(operation_id, request_digest)`는 canonical
scope, RunStore의 실행 잠금과 원래 producer 잠금을 확보한 동안
`AndroidRecoveryInspection`만 제공한다.

다른 요청·구성·루트·파일 교체, 살아 있는 producer는 거절한다. 의향만 남은 작업,
부분 staging, phase 파일과 요약 사이의 중단, 사본 삭제 중 중단을 구분한다.
native slot의 중단을 `native-call-unresolved`로 표시하고 교체된 디렉터리·초과 크기·
변조된 입력도 거절한다. 미완료 slot이 있으면 staging 사본 삭제를 진행하지 않는다.
이 검사는 기기를 조작하거나 파일을 지우지 않고, RunStore 예약을 해제하는 capability도
발급하지 않는다. 실제 기기 종료·fixture 복구·sanitation을 수행하는 고정 복구
실행기와 공개 `status/recover` CLI는 후속 통합이다.

`native_recovery()`는 별도의 복구 전용 잠금 범위다. 이전 generation·host/helper,
현재 quarantine snapshot, 같은 프로젝트의 살아 있는 parent grant를 대조하고 원래
producer·기기 잠금 FD를 빌린다. 새 호스트의 기기 권한은 reconciliation 전까지
quarantine으로 유지된다. 복사되거나 만료된 토큰, 다른 프로젝트, 닫힌 복구 범위는
거절하며 빌린 기기 FD가 남아 있는 동안 generation 변경도 거절한다.

이 범위를 닫아도 자식이 원래 FD를 보유하면 잠금은 유지된다. 복구 범위 자체는 기기
명령·sanitation·예약 해제를 승인하지 않는다. 고정 APK 검사기도 native 소유권에
연결했고 실제 AAPT의 부모 종료·잠금 유지를 검사했다.

`android_recovery.recover_android_device()`는 살아 있는 복구 잠금 범위에서 실행한다.
현재 계약 버전과 준비된 staging을 확인하고 `recovery.json`을 먼저 쓴 뒤, 원래 host
작업의 bounded scratch slot을 회수한다. 이때 예약 용량과 기기 quarantine은 유지한다.
일회성 내부 dispatch는 선택한 명령만 허용하며 일반 SDK 명령으로 재사용할 수 없다.

고정 순서는 helper/대상 앱 종료 → 프로세스 부재 확인 → 원본 APK identity 검사 →
원본 설치 → 설치 위치와 SHA-256 확인 → 앱 데이터 초기화 → 최종 프로세스 부재
확인이다. 각 단계 전 의향과 단계 후 결과 digest를 저장한다. 경로의 `..`, 남은
프로세스, 실패한 초기화, 취소, 불완전한 기록은 거절하며 최대 32회 시도 뒤에는 기존
기록을 덮어쓰지 않는다. 현재 실행기는 준비된 세 APK가 남아 있는 작업을 지원한다.

`device-restored`는 이 순서의 결과이며 ownership/fixture cleanup capability가 아니다.
`recover_android_resources()`는 기기 복구 뒤 원래 이슈의 fixture 할당을 검증·재조정하고
정리한다. 이슈 ID는 원래 context와 replay 번호에서 구하며 프로젝트·기기·앱·빌드와
할당 ID/세대, 준비 요청 ID·payload digest를 대조한다. 실패한 원격 정리는 quarantine으로
남고, 이미 완료된 과거 할당은 새 세대를 건드리지 않고 확인한다. fixture 저장소 형식 2는
재할당 전의 소유자·기기·계획 정보를 이력으로 보존하며 형식 1에서 기존 작업을 유지해 이전한다.

fixture 결과도 ownership release capability가 아니다.
`recover_android_helper()`는 이 결과 뒤 helper APK 설치/hash 확인, 새 식별자와 임의
인증 토큰을 가진 설정 전송, instrumentation 시작과 인증된 status 확인을 연결한다.
프로토콜·앱 프로필·host/helper/provider/native 식별자·지원 시계·빈 포인터 상태를 검사한다.
설정 본문과 command/instrumentation slot도 dispatch 권한에 바인딩한다. 확인 후 helper를
종료하고 데이터를 초기화하며 프로세스 부재와 host client 회수를 확인한다.

현재 이 결과는 새 helper 관측과 회수이며 최종 DeviceAuthority reconciliation이나
ownership release가 아니다. HostAuthority 저널은 원래 실행 결과가 불확실해도
복원이 확인된 경우를 `recovered`로 구분한다. 원래 작업의 결과 digest·provider·receipt는
보존하고 복원 결과는 reconciliation disposition에 기록한다. 늦은 응답은 새 세대를
변경하지 않는다.

`AndroidOperationStore.finalize_recovery()`는 위 관측을 직접 실행한 뒤 새 parent grant로
권한 세대를 전환하고 `recovery-cleanup-pending` 격리를 유지한다. 원래 producer/기기
descriptor를 계속 보유한 상태에서 정확한 staged APK만 삭제하고, 살아 있는 일회성
`AndroidCleanupCapability`를 `RunStore.finish_mobile_recovery()`가 소비한다. 원래 작업은
`failed` 또는 `cancelled`, 예약 용량은 0이 된 뒤 기기 저널과 lease를 해제한다.
저널 v3는 이 정리 보류 상태를 해제할 수 있는 구버전의 재진입을 막는다.

권한 저널 commit 뒤 private record 갱신 실패, 일부 APK 삭제, 예약 해제 commit 뒤
중단을 재개할 수 있다. 새 HostAuthority도 원래 reconciliation·구성·파일·잠금을 다시
검사하며 기기 명령을 반복하지 않는다. 다음 소유자가 이미 취득한 경우에는 완료된
원래 작업의 이력만 확인한다. `status()`의 `finalization-*`는 읽기 전용 진행 표시다.

기존 정상 cleanup의 유효한 discard 의향·원래 파일 연계가 있으면 APK의 일부 또는 전부가
삭제된 상태에서도 복구한다. 이 경우 `recoveryMode: installed-original`로 기록하고 현재
기기의 원본 패키지 경로·APK hash와 helper 설치 hash를 직접 확인한다. 확인된 원본의
데이터를 초기화하고 새 인증 설정·helper 검증·종료를 수행하며, 남은 정확한 파일을 정리한다.
원본이나 helper가 없거나 유효한 설치 hash가 다르면, 원래 설정이 고정한 APK에서 필요한
복구 사본을 준비해 한 번 더 복구한다. 기존 파일은 재사용하고 사라진 슬롯만 다시 만든다.
`recovery-materials.json`은 새 inode·준비 상태·원래 파일 목록 digest를 바인딩하며 원래
intent의 inode 기록을 바꾸지 않는다. 삭제로 확보한 원래 예약 공간 안에서 복사하고,
검사기와 설치 직전에 선택된 사본의 hash·inode를 확인한다. 복구 후 사본도 함께 삭제한다.
불완전한 등록된 사본은 다시 복사할 수 있지만, inode 등록 전에 생긴 비어 있지 않은 파일은
받아들이지 않는다. 입력 변조나 재설치 후에도 남은 누락·불일치는 격리와 예약을 유지한다.
discard 기록 없이 APK가 없어진 경우도 거절한다.

recovery.json의 v2 경로는 `staged-apks`와 `installed-original`이다. 복구 사본을 쓰는 v3
`recovery-apks` 경로는 준비된 사본 기록의 digest도 바인딩한다. 각 경로의 실제 단계만
완료 목록에 허용하고 기존 v1/v2 기록도 읽는다. 복사·삭제 단계의 I/O 중단 재개를 검사했다.

새 metadata는 완성된 임시 파일을 fsync한 뒤 원자적으로 공개하며 기존 기록을 덮어쓰지
않는다. writer가 공개 전 또는 직후 종료돼도 최종 이름에는 부분 JSON을 노출하지 않는다.
공개 직후 남은 내부 임시 링크는 완성된 기록으로 읽고, 원래 요청·구성·producer 잠금을
검사한 finalization 경로에서만 제거한다. 원래 작업 폴더·phases·native-calls의 제한된
`.record-*` 파일만 다루며 그 안의 미확정 상태를 채택하지 않는다. 읽기 전용 recovery
검사는 파일을 지우지 않는다. 실제 writer의 `os._exit`·SIGKILL과 복구 경로를 검사했다.
구버전이 최종 이름에 이미 남긴 부분 JSON은 의미를 추정해 고치지 않고 격리 상태로 보존한다.
실제 기기 qualification과 운영 CLI는 후속 작업이다. 이 API의 기기 검사는 실제 SDK와
native guardian을 사용한 자체 프로토콜 서버 검증이며 물리 기기 검증이 아니다.
새 작업은 `fixtureReservationVersion: 1`을 원래 operation과 입력 snapshot에 고정하고,
이슈 ID·fixture ID에서 결정한 예약 ID를 이슈의 초기 의향에 먼저 기록한다. 실제 할당 번호
저장 전에 중단돼도 같은 계획·소유자·기기와 예약 ID로 원래 할당을 찾을 수 있다.
원격 operation이 하나도 없는 로컬 할당만 전송 없이 해제한다. 아직 생성되지 않은 예약은
봉인 기록을 남겨 늦은 reserve가 시작되지 못하게 한다. 이력 개수도 제한한다.
계약과 원래 연계가 없는 과거 작업은 계속 불확실한 상태로 보존한다.
fixture 할당 번호는 원격 준비 전에 이슈 파일에 저장하며 `mobile_...`를 포함한 유효한
이슈 ID의 기록을 재시작 후 다시 읽는다.

`close()`는 새 admission을 막고 native liveness를 닫으며 staging·활성 작업·callback과
원래 native 프로세스·dispatch 스레드가 끝날 때까지 지정된
기한 안에서 기다린다. 작업 스레드가 다른 일을 계속한다는 이유로 종료를 기다리지
않는다. 서비스 조합은 먼저 작업을 취소·수집하고 고정 어댑터의 정리를 기다린다.

## 검증 근거와 남은 격리 작업

[관련 검사 58개](../artifacts/product-delivery/d4-protected-adapters-r1/android-mobile/operation-parent-combined-r4.log)가
통과했다. 실제 Lab·프로필·fixture·보호 감독자를 연결하고 설치 전 취소, 후보 3회
재생, 원본 복원, 구성 변경, 잠금 descriptor 수집과 중단 지점을 검사했다.
이 검사에서 VM·서명·기기 도구와 UI bridge는 명시적인 대역이다.

앞선 전용 AVD의 원본 대조 후보 실행은
[실제 원본 복원 근거](../artifacts/product-delivery/d4-protected-adapters-r1/android-mobile/actual-composition-r1/parent-verification.json)로
보존한다. 그 실행은 이 영속 operation 통합이나 mobile isolation qualification의
실제 수용 검사가 아니다.

호스트 OS의 IPv4/IPv6 TCP·UDP 차단과 허용한 한 inbound TCP listener의 통신은 별도로
측정했다. 현재 캐시된 ADB 36.0.0은 사용자 기본 `.android` 접근을 차단한 전용 서버
실험에서 초기화 또는 요청 처리에 실패했다. 기본 키를 읽지 못하면 vendor key를
정상적으로 사용할 수 있다고 가정하지 않는다. 사용자 HOME이나 기본 인증 파일을
변경하지 않았으며 실험의 전용 서버와 생성한 키는 정리했다.

실제 qualification에는 전용 ADB 인증, guest/host/fixture 통신 경계, 기본 서버와의
분리, 부모가 죽어도 emulator·ADB와 기기 lease를 보유하고 종료를 수집하는 native
소유자가 필요하다. 현재 저널의 generation이나 과거 결과 파일이 이를 대신하지 않는다.
