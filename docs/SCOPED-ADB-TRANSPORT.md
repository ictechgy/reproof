# 명시적인 ADB endpoint와 클라이언트 경계

2026-09-14 · D4 진행 중

`AdbEndpoint`는 운영자가 이미 준비한 사용자 소유 Unix socket을 선택한다.
프로토콜 버전은 41이며 `0600` socket·`0700` 부모·동일 UID peer와 원래 inode를
확인한다. 이 검사는 서버의 인증 환경이나 실제 기기 격리 qualification을 발급하지 않는다.
서버 시작·사용자 기본 키 읽기·HOME 변경은 이 경로에 없다.

## SDK 클라이언트와 gateway

ADB의 [클라이언트 소스](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/client/adb_client.cpp)는
서버 버전 불일치에서 daemon 종료와 재시작을 시도한다. 따라서 별도 endpoint 선택만으로
충분하다고 보지 않는다. 검토한 소스와 캐시된 SDK 바이너리의 실제 동작을 구분하여 검사했다.

`AdbGateway`가 SDK와 upstream 사이의 smart-socket 요청을 검사한다.
버전 조회 결과가 정확히 일치해야 요청을 전달하고, 실패 문구도 고정한다.
서버 종료/시작·다른 기기 선택·임의 host 제어는 전달하지 않는다.
`devices` 결과는 선택 serial로 제한하고, device service도 해당 transport 선택 후에만 연다.
데이터 릴레이는 작은 버퍼·전체 바이트 한도·종료 수집을 사용한다.

`ScopedAdbClient`는 고정 ADB 해시와 `sandbox-exec` 해시를 확인한다. SDK에는
명시적인 `-L localfilesystem:...`과 `-s`를 전달한다.
클라이언트의 파일 접근은 시스템 로딩에 필요한 경로·고정 실행 파일·작업 폴더로
제한하며, network는 gateway socket 하나만 허용한다. fork와 Mach lookup을 거절한다.
기본 서버 fallback을 시도해도 새 서버 프로세스나 사용자 인증 파일 접근을 허용하지 않는다.

## helper 연결과 종료

scoped mode의 `PinnedAdbDevice`는 조회·shell·APK 설치와 instrumentation을 이 경계에서 실행한다.
helper HTTP는 선택된 ADB transport의 `tcp:8766`을 직접 열어 전달한다.
`adb forward`로 호스트 수신 포트를 만들지 않는다. helper 읽기에는 절대 deadline과
취소 시 socket shutdown을 적용한다. 정상 종료와 재시도 가능한 gateway 수집을 구분한다.

호스트 SDK 프로세스가 끝났다는 사실은 기기 프로세스·fixture 정리의 증거가 아니다.

## native guardian 연결 상태

`native/android-process-guardian`은 원래 작업 producer와 기기 lease의 파일 디스크립터를
유지하며 고정 SDK 자식을 감시한다. Python 부모의 liveness pipe가 닫히면 SDK 자식을
종료·회수한 뒤 잠금을 놓는다. 정상 결과에서도 부모 ACK까지 잠금을 유지하므로,
호출자는 gateway 수집을 먼저 마친 뒤 ACK해야 한다. 결과의 `hostClientStopped`와
`deviceCleanupConfirmed`는 별도 필드이며 후자는 항상 false다.

`Lease.borrow_descriptor`, `DeviceAuthority.borrow_native_lease`,
`AndroidOperationStore.borrow_native_descriptors`는 원래 열려 있는 잠금을 전달한다.
새로 연 같은 이름의 파일이나 저장한 JSON으로 권한을 만들지 않는다. 부모 scope가
닫혀도 상속된 마지막 FD가 닫힐 때까지 잠금이 유지되도록 close만 수행한다.

`nativeGuardian`이 설정된 `AndroidTrustedMobileAdapter`는 실제 phase에서 원래 FD를
빌리고 명시적인 dispatcher를 발급한다. `PinnedAdbDevice`의 조회·shell·설치와
instrumentation은 이 dispatcher를 통해 guardian을 호출한다. 재현 callback 스레드는
그 경로에서만 빌린 FD를 사용할 수 있고, 직접 접근이나 종료된 phase의 재사용은 거절한다.
APK 검사기도 같은 guardian이 원래 잠금을 유지하며 실행한다. `dump badging`과 작업에
등록된 `candidate.apk`·`original.apk`·`helper.apk`만 허용한다. 검사기·선택 APK·SDK의
`lib64/libc++.dylib`가 있으면 그 파일까지 해시를 고정한다. SDK 보조 라이브러리의
측정된 digest는 operation 구성과 입력 snapshot에도 포함한다. 검사기는 네트워크·
파일 쓰기·추가 fork 없이 선택한 APK와 실행에 필요한 파일만 읽는다.

helper HTTP도 같은 dispatcher에서
phase 권한·취소를 검사한 뒤 직접 ADB service로 연결한다. 진행 중인 helper 요청은
phase 완료를 막고, phase를 닫으면 해당 요청을 취소·수집한다.

명시적인 endpoint가 있는 새 작업은 native 명령·결과 공간 17,522,688바이트를 별도로
예약한다. 일반 명령과 instrumentation에 각각 하나의 고정 slot을 사용한다. 정상 SDK
결과·원래 guardian의 회수·gateway 종료를 확인한 뒤에만 해당 slot을 정리하고 다음
명령에 재사용한다. 요청·명령·입력 hash와 기기 generation을 저장하며 기록 크기는
누적 명령 수에 따라 늘어나지 않는다.

취소·부모 종료·부분 쓰기는 `native-call-unresolved`로 남고 사본 삭제와 비용 해제를
막는다. 호스트의 회수 결과만으로 실제 기기/backend 정리를 승인하지 않는다.
실제 복구 실행기와 보호된 전체 `live-serve` 초기화는 후속 작업이다.

## 구성

`android-mobile-definition-v1`에 선택적으로 다음 필드를 추가한다.

```json
{
  "adbEndpoint": {
    "socketPath": "/absolute/private/adb.sock",
    "serverVersion": 41,
    "sandboxSha256": "등록한 sandbox-exec의 SHA-256"
  },
  "nativeGuardian": {
    "path": "/absolute/public-tools/android-process-guardian",
    "sha256": "검토하고 등록한 guardian 바이너리의 SHA-256"
  }
}
```

값을 로딩하면 `AndroidMobileAdapterConfig.adb_endpoint`가 된다. endpoint digest는
새 작업의 원래 저널 구성에 포함된다. 해당 작업을 endpoint 없는 구성으로 복구할 수 없다.
`nativeGuardian`은 endpoint와 영속 operation store를 요구한다. 경로·해시는
`AndroidGuardianTools`로 고정하고 guardian definition digest도 저널에 포함한다.
입력 로딩은 guardian을 실행하지 않는다. 바이너리 등록은 환경 qualification이 아니다.
APK 검사 지원에는 현재 배포 리소스의 guardian을 다시 빌드하고 해당 바이너리 해시를
등록해야 한다. 이전 SDK 전용 guardian으로 APK 검사를 실행하면 거절되며 우회 실행하지 않는다.
현재 런타임은 `nativeToolOwnershipVersion: 2`를 operation 구성과 입력 snapshot에
바인딩한다. 이 표시는 SDK·APK 검사기·helper의 현재 소유권 계약을 구분하며, 과거
표시 없는 작업을 같은 계약으로 재해석하지 않는다. 버전 표시 자체가 기기 정리 증거는 아니다.
기존 endpoint 없는 경로는 호환성을 위해 남으며 보호 환경 qualification으로 간주하지 않는다.

## 현재 근거

소유한 protocol server와 실제 캐시 ADB로 기기 목록·shell v2·APK 스트리밍·helper HTTP·
instrumentation 수집을 확인했다. 버전 불일치·서버 부재·제어 요청·다른 기기를 거절했다.
OS 대조 probe에서 작업 밖 파일 읽기/쓰기, 다른 TCP 접속, 추가 process spawn을 차단했다.
probe 파일·endpoint는 자체 자료이며 사용자 키를 읽지 않았다.

원래 FD 전달, 실제 Python 부모 종료, guardian SIGSTOP, 실제 캐시 SDK 실행 중
guardian SIGKILL 뒤의 잠금 유지를 검사했다. 실제 phase의 SDK 명령·instrumentation·
helper 호출, 늦은 접근 거절과 진행 중 종료도 검사했다. 종료 통지가 취소 표시보다
먼저 도착하는 경합과 helper의 phase 검사 누락을 재현·수정했다.
실제 AAPT가 APK를 연 상태에서 guardian을 SIGKILL해도 원래 두 잠금이 유지됐고,
Python 부모가 종료되는 경우에는 guardian과 AAPT 회수 뒤에만 잠금이 풀렸다.
현재 검사·패키지 범위는 `foundation-progress-r23.json`을 따른다.

실제 인증 서버 환경·기기/backend 격리·전체 `live-serve` 및
회사·두 Mac 수용은 미완료다. 이 구현이나 입력 JSON을 mobile qualification으로
승격하지 않는다.
