# 프로젝트 보안 운영 통제 매트릭스

구현·증거·한계를 연결한 프로젝트용 설계 예제입니다.
ISMS-P 인증 충족표, PIPA 준수 판정 또는 실제 인증심사 대응 완성 문서가 아닙니다.
Evidence는 코드와 테스트의 위치를 의미하며 실제 운영 통제의 지속적 수행까지 입증하지 않습니다.

| Control | Risk | Implementation | Evidence | Limitation |
|---|---|---|---|---|
| Secret management | 환경변수/자격증명 노출 | env 분리, 알려진 패턴 검사, 공유 credential redaction | [.gitignore](../.gitignore), [check_secrets.py](../scripts/check_secrets.py), [policy.py](policy.py) | 모든 secret/PII를 탐지하지 않음; 실제 키 회전은 담당자 업무 |
| Least data collection | 원문·과도한 metadata 저장 | 기존 allowlist 재사용, storage 길이 제한, 알려진 secret 가림 | [models.py](../crawling/models.py), [storage.py](../crawling/storage.py), [test_governance.py](../tests/test_governance.py) | 기존 전체 DB 정제 없음; 긴 identity 저장은 거부됨 |
| Access control | 비인가 분석 화면 조회 | auth 기본 True, 운영 모드 강제, POST 로그아웃·CSRF, 구형 진입점 차단 | [settings.py](../DjangoProject/DjangoProject/settings.py), [django_controls.py](django_controls.py), [tests_governance.py](../DjangoProject/mongoDbConnect/tests_governance.py), [구형 설정](../webapp/DjangoProject/settings.py) | 상세 RBAC/MFA/외부 SSO 미구현; 계정 발급 통제 필요 |
| Retention | 목적 없이 장기 보유하거나 잘못 삭제 | 365/365/180일 기본값, UTC scalar date, invalid/future 제외, read-only checker | [policy.py](policy.py), [retention.py](retention.py), [check_governance.py](../scripts/check_governance.py) | 운영 기본값이며 법정 기간이 아님; legal hold·백업 만료 별도 |
| Explicit deletion approval | DB 오지정·대량 삭제 사고 | dry-run 기본, --apply 및 두 DB 이름·연결 DB 확인, 세 collection date query만 허용 | [apply_retention.py](../scripts/apply_retention.py), [test_governance.py](../tests/test_governance.py) | 권한 있는 운영자의 잘못된 기간 설정은 별도 검토 필요; 다중 collection transaction 없음 |
| Audit logging | 추적성 부족·로그 자체의 노출 | 고정 JSON 필드, PK/문서 ID 해시, 1 MiB 회전/백업 5개, 안전한 console fallback | [audit.py](audit.py), [django_controls.py](django_controls.py), [tests_governance.py](../DjangoProject/mongoDbConnect/tests_governance.py) | 변조 방지·중앙 수집 미구현; 다중 프로세스 운영 설계 별도 |
| Source lifecycle | 불안정 source 반복 실행·수집 범위 확대 | Bitlock ACTIVE, 나머지 UNAVAILABLE, 상태별 자동 실행 필터 | [sources.py](sources.py), [scheduler.py](../scheduler/scheduler.py), [test_scheduler.py](../tests/test_scheduler.py) | 상태 수동 검토/프로세스 재시작 필요; 실제 threat actor 상태 단정 불가 |
| Alert dedupe | 반복 Telegram 발송·전달 일관성 문제 | 기존 deterministic 예약·claim/retry 흐름 유지 | [service.py](../alert/service.py), [test_alerts.py](../tests/test_alerts.py) | 외부 Telegram 전달의 모든 실패를 제거하지 않음; 기존 한계 유지 |
| Change history | metadata 변경 근거 소실 | 기존 event_key, history 선기록, deterministic history upsert 유지 | [storage.py](../crawling/storage.py), [test_storage.py](../tests/test_storage.py) | Mongo cross-collection transaction 없음; 보존 기간 뒤에는 history가 없을 수 있음 |
| ELK field minimization | snapshot·토큰이 분석 표면으로 복사 | 기존 strict mapping/projection, 추가 key redaction, 선택적 _source 키 검사 | [project_document.js](../monstache/project_document.js), [mappings.py](../elk/mappings.py), [elk_check.py](elk_check.py), [JS 테스트](../tests/test_elk_projection.js) | 실제 ES runtime query·전파는 PC 검증 필요; arbitrary nested PII 탐지 아님 |
| Backup/destructive protection | 복구 불능·resume 상태 손상 | 기존 토큰·볼륨 유지, drop/ES DELETE/자동 cascade 없음, 실행 전 범위·백업 검토 절차 | [retention.py](retention.py), [Day12 운영 절차](../elk/operations.md), [data_handling.md](data_handling.md) | 자동 백업/복원 검증을 추가하지 않음; export/Telegram 과거 메시지 삭제 안 함 |
| Incident response | 미확인 게시물을 사실로 단정·과도한 대응 | 메타데이터만으로 탐지~사후 검토 9단계, 담당자 판단과 최소 공유 | [incident_response.md](incident_response.md) | 조직별 연락망·신고 의무·법적 판단을 대신하지 않음 |
| Testing | 보안 통제 우회·기존 기능 회귀 | fake Mongo, 인증용 in-memory SQLite, 신규/기존 Python·JS 테스트 | [test_governance.py](../tests/test_governance.py), [Django 테스트](../DjangoProject/mongoDbConnect/tests_governance.py), [기존 Day12 테스트](../tests/test_elk.py) | Atlas/Tor/Telegram/ELK/Docker 실제 E2E는 Work에서 수행하지 않음 |
