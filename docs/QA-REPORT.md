# 실패 경로 QA — 2026-09-09

범위는 기록·번들·반복 판정·패치 보호·중단 처리다. 비싼 기기/빌드 경계는 테스트 대역으로 분리했고, 파일 무결성·실제 subprocess 종료는 실제 로컬 동작으로 검사했다. 성공 경로는 별도 Simulator 실행으로 확인한다.

## 발견하고 수정한 문제

| 문제 | 실패 재현 | 수정 |
|---|---|---|
| iOS 원본 검증 중 취소 결과가 저장되지 않음 | baseline에서 KeyboardInterrupt가 발생하면 job이 prepared에 남음 | cancelled 결과와 보고서를 저장하고 종료. Android에도 같은 경계 적용 |
| 마지막 실행 뒤 iOS 원본 소스가 바뀌어도 성공 판정 | 6번째 실행 직후 보호된 원본 파일 변경 | 완료 직전 원본·작업 복사본·policy 재검사 |
| 마지막 실행 뒤 iOS frozen runner가 바뀌어도 성공 판정 | 마지막 실행 직후 번들의 test product 변경 | run 종료 및 job 완료 직전 번들·runner 무결성 검사 |
| 마지막 실행 뒤 Android 보호 소스가 바뀌어도 성공 판정 | 마지막 device run 이후 build 설정 변경 | 최종 원본·작업 소스·policy·번들·APK digest 검사 |
| 부모가 종료되면 SIGTERM을 무시한 자식이 남음 | 실제 subprocess에서 부모만 종료하고 자식이 계속 실행 | 부모 종료 여부와 무관하게 해당 process group의 남은 자식도 종료 |

각 수정은 수정 전 실패한 회귀 테스트가 수정 후 통과하는 것을 확인했다. 이전 정상 실행 증거는 덮어쓰지 않았다.

## 추가로 확인한 차단 경로

- 기록의 중복 이벤트 ID, freeze 경계 불일치, 잘림·유실·민감 입력.
- XCTest attachment 중복과 대상 Simulator identity 누락/위조.
- 문법상 허용돼도 결과가 틀린 패치(`return 3`)의 성공 판정 차단.
- fixture와 oracle의 사례 불일치, 등록되지 않은 사례.
- 다른 사례의 Swift 수정 규칙 사용, 새 실행 코드 삽입.
- 실제 테스트가 없는 NO-SOURCE 결과와 skip·실패 결과.
- 혼재된 반복 결과를 성공한 실행만 골라 통과시키지 않음.
- stale capture, finalized marker 누락, 마지막 sequence 불일치.

## 실행

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p test_adversarial.py -v
python3 -m unittest discover -s tests -p test_process_boundary.py -v
```

현재 전체 60개 테스트가 통과했다. 무제한 환경·보안 보증이나 일반 앱의 검증 완료를 뜻하지 않는다. 실제 기기별 차이·서명·범용 코드 빌드 격리는 별도 범위다.
