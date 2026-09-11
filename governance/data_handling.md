# Day 13 프로젝트 내부 데이터 처리·운영 정책

이 문서는 보안 설계 예제와 프로젝트 운영 통제를 설명합니다. 대외 개인정보처리방침,
ISMS-P 인증 충족 선언, 법률상 적정성 판단 또는 인증심사 대응 완성 문서가 아닙니다.
실제 운영 조직은 처리 근거·계약·권한·보유기간·법적 보존 필요성을 별도로 검토해야 합니다.

## 목적과 수집 범위

유출 게시물의 **회사 관련 메타데이터 변화**를 관찰하고 담당자의 검토를 지원합니다.
게시자의 주장은 확인된 침해 사실과 구별합니다.

기준 목록은 [crawling/models.py](../crawling/models.py)의 `METADATA_FIELDS`입니다.
`governance.policy`는 이 객체를 그대로 재사용합니다.
회사명, 회사 URL, 국가, 게시자가 기재한 유출 내용의 개요·용량·게시/갱신일,
설명, 출처 URL, source 및 기존 identity/관찰 시각만 저장 계층에 전달합니다.

유출 원문 파일·첨부파일, 계정 자격증명, 비밀번호, 쿠키/세션, Telegram 토큰,
MongoDB URI, CAPTCHA 인증정보는 수집·저장 대상이 아닙니다.
렌더링된 HTML은 기존 parser 실행 중 메모리에서만 사용하며 MongoDB에 전체 저장하지 않습니다.
이 통제는 별도 피해 시스템 접근이나 추가 자료 다운로드를 허용하지 않습니다.

## 저장 전 정규화와 길이 제한

| 필드 | 최대 문자 수 | 초과 처리 |
|---|---:|---|
| company_name | 512 | 해당 저장 요청 거부 |
| company_url | 2048 | 해당 저장 요청 거부 |
| country | 128 | 자르기 |
| data_contents | 2000 | 자르기 |
| data_size | 128 | 자르기 |
| publication_date | 128 | 자르기 |
| description | 8000 | 자르기 |
| source_url | 2048 | 자르기 |
| source | 128 | 해당 저장 요청 거부 |

Python 문자 수 기준입니다. parser가 기존 방식으로 `_id`를 만든 **후** storage에서
적용합니다. 잘린 텍스트로 parser ID를 다시 만들지 않습니다.
회사명·회사 URL·source는 단순 절단으로 서로 다른 사건이 합쳐지는 것을 막기 위해
상한을 넘으면 거부합니다. 정상 identity의 Day 9 event_key 계산 방식은 유지됩니다.
과도한 identity가 있으면 해당 crawl 저장 작업이 실패할 수 있으며 자동 우회/재작성하지 않습니다.

일반 메타데이터에는 공백 정규화, 자격증명 가림, 길이 제한, 끝 공백 제거를 적용합니다.
가림은 절단보다 먼저 수행하여 토큰 일부가 절단 경계에 남지 않게 합니다.
잘린 description의 범위 바깥에서만 내용이 바뀌면 change history에 반영되지 않습니다.
source_url은 기존과 같이 identity에 포함하지 않습니다.

`governance/policy.py`는 Mongo URI, Telegram 토큰 형태, URL 내 자격증명,
password/passwd/token/api_key/secret 할당문, private-key 블록을 보수적으로 가립니다.
일반 회사명, 정상 URL, 'password policy' 같은 설명을 그대로 유지하는 테스트가 있습니다.
자격증명이 섞인 회사명은 identity로 사용하지 않습니다.
저장·Telegram 텍스트·Django 출력에서 Python 함수를 공유하고 Monstache도 해당 패턴을 가립니다.
위험도 분류 조건과 Day 10 delivery/dedupe 절차는 변경하지 않습니다.

이 기능은 모든 개인정보·비밀정보의 자동 탐지나 완전 삭제를 보장하지 않습니다.
인코딩/변형/문맥에 따른 누락과 오탐이 가능합니다. 기존 DB 전체를 일괄 정제하지 않으며,
재관찰 때 허용 필드만 보강합니다. 예전 문서에 남은 금지 필드는 checker로 확인 후 수동 검토합니다.
기존 history/alert snapshot의 값을 일괄 덮어쓰지 않습니다.

## 데이터 흐름과 저장 위치

| 저장소/표면 | 내용과 용도 | 통제 |
|---|---|---|
| Mongo leaked_data | 사건별 현재 메타데이터와 identity/관찰 시각 | 기존 _id/event_key·dedupe 유지, 저장 전 정책 적용 |
| Mongo leak_history | 메타데이터 before/after 변경 이력 | 기존 deterministic history ID 유지 |
| Mongo alert_log | 알림 판정·전송 결과 및 내부 snapshot | message/claim_token 내부 사용만 허용 |
| Mongo alert_state | Day 10 resume 상태 | 이번 보유기간 처리에서 제외 |
| Mongo monstache DB | Day 12 resume/direct-read 상태 | 이번 처리에서 제외 |
| Django SQLite | Django 계정·해시된 비밀번호·세션 | threat data를 이 DB로 이관하지 않음 |
| logs/audit.jsonl | 제한된 구조의 운영 감사 이벤트 | 회전 파일, Git 제외, 운영체제 접근권한 |
| Elasticsearch/Kibana | Day 12의 events/history/alerts 투영 | strict mapping·필드 allowlist, 내부 snapshot 제외 |
| Telegram | 기존의 길이 제한된 메타데이터 요약 | 공유 redaction, 기존 전달 임계값·중복 방지 유지 |

alert_log의 `message`는 이미 예약된 전송 내용을 재시도하는 내부 snapshot이며 새 수집 필드가 아닙니다.
`claim_token`도 내부 전송 소유권 확인용으로 유지합니다. 두 필드는 Django·ELK 분석 모델에
복사하지 않습니다. `resume_token`은 alert_state용으로, 위 세 threat collection에서는 경고 대상입니다.
Telegram에 과거 이미 전송된 메시지의 회수나 삭제는 Day 13 기능이 아닙니다.

## 접근통제와 HTTPS 구분

`DjangoProject/manage.py`가 운영·개발 공통의 정식 진입점입니다.
기본 `DASHBOARD_REQUIRE_AUTH=True`일 때 목록과 상세 GET/HEAD는 로그인 후 접근합니다.
미인증 요청은 Mongo 조회 전에 로그인으로 이동합니다. 검색어·문서 ID는 로그인 redirect에 복사하지 않습니다.
목록과 상세의 POST 등 쓰기 메서드는 계속 거부하며 응답은 no-store로 표시합니다.

Django 기본 LoginView/LogoutView, 세션, CSRF를 사용합니다. 로그아웃은 CSRF가 필요한 POST입니다.
등록 기능·새 인증 시스템·새 역할 체계는 추가하지 않습니다. 기본적으로 유효한 Django 계정은
동일한 분석 화면을 읽을 수 있으므로 계정 발급 자체를 운영자가 관리해야 합니다.
세부 RBAC, MFA, 로그인 시도 제한, 외부 SSO는 이번 구현에 포함하지 않습니다.

오래된 `webapp` 복제본은 공개 조회 경로가 별도로 남아 있어 settings 초기화에서
명확한 안내와 함께 실행을 거부합니다. 이 경로를 운영하지 마세요. 정식 Day 11 query/parser/UI는 유지됩니다.

`DJANGO_DEBUG=False`이면 50자 이상이며 예시값이 아닌 SECRET_KEY, 비어 있지 않고
와일드카드가 없는 ALLOWED_HOSTS, AUTH=True가 필수입니다. 잘못된 boolean도 거부합니다.
키의 길이 검사가 난수성/비밀성을 보장하지는 않습니다. 운영자는 로컬에서 무작위 키를 생성해 관리합니다.

기본 HTTP 개발 환경에서는 `DJANGO_SECURE_COOKIES=False`입니다. 실제 브라우저 접속이
HTTPS인 환경에서는 True로 설정해 session/CSRF cookie의 Secure 속성을 활성화합니다.
HTTPOnly session cookie, nosniff, DENY frame, same-origin referrer 헤더는 명시적으로 적용합니다.
기존 HTML의 no-referrer 메타 태그는 더 제한적인 페이지 정책으로 유지합니다.

이 설정만으로 HTTPS 서버가 생기지는 않습니다. 운영자는 신뢰할 프록시와 TLS 종료 위치,
HTTPS redirect·Secure cookie 동작을 확인한 후에 HSTS를 단계적으로 적용해야 합니다.
HSTS includeSubDomains/preload를 검증 없이 켜거나 임의의 forwarded header를 신뢰하지 않습니다.
Day 12 ES/Kibana의 loopback 바인딩과 기존 구성은 유지하며 Django 로그인으로 보호된다고 주장하지 않습니다.

## 보유기간과 삭제

아래 기간은 **프로젝트 기본값**이며 법정 보유기간이 아닙니다.

| collection | 기본 기간 | 기준 시각 | 설정 |
|---|---:|---|---|
| leaked_data | 365일 | last_seen, 없거나 null일 때만 scraped_time | GOV_EVENT_RETENTION_DAYS |
| leak_history | 365일 | changed_at | GOV_HISTORY_RETENTION_DAYS |
| alert_log | 180일 | updated_at, 없거나 null일 때만 created_at | GOV_ALERT_RETENTION_DAYS |

설정은 1~7자리 양의 정수입니다. 시각 계산은 aware UTC를 사용합니다.
기준 시각이 `now - days`보다 **엄격히 이전**이어야 대상입니다. 경계와 같으면 보존합니다.
매번 최근 관찰이 갱신되는 사건은 last_seen 기준으로 계속 보존될 수 있습니다.

Mongo의 scalar BSON date만 자동 판단합니다. 문자열/배열/숫자/잘못된 자료형은
날짜로 강제 변환하지 않습니다. 각 정책이 참조하는 primary/fallback 필드 중 어느 하나라도
잘못된 자료형 또는 미래 시각이면 자동 삭제에서 제외합니다. 선택된 시각이 누락된 문서도 보존합니다.
first_seen 등 정책이 참조하지 않는 모든 시각의 품질 검사까지 수행하는 것은 아닙니다.
보조 진단 missing/invalid/future count는 중복될 수 있으며 합산해 전체 문서 수로 사용하지 않습니다.
기존 PyMongo의 naive 읽기 방식은 변경하지 않고, 현재 client는 BSON date를 aware UTC로 읽습니다.

`check_governance.py`와 `apply_retention.py`의 기본 동작은 읽기입니다.
검사 스크립트는 실제 문서 본문이나 원문 ID를 출력하지 않습니다.
삭제는 `--apply --database NAME --confirm-database NAME`이 모두 명시되고,
두 값·DB_NAME·실제 연결 DB가 정확히 일치해야 가능합니다.
admin/local/config/monstache DB 및 allowlist 밖 collection은 거부합니다.

세 collection 모두의 사전 count가 성공한 후 각 date predicate로 `delete_many`합니다.
삭제 직전에 최근 관찰 시각으로 갱신된 문서는 다시 평가된 조건에 따라 살아남습니다.
사전 count와 deleted_count는 동시 쓰기 때문에 다를 수 있습니다.
Mongo transaction은 추가하지 않았으므로 중간 실패 시 앞 collection 삭제는 이미 완료되었을 수 있습니다.
감사 기록과 read-only checker로 결과를 확인한 뒤 재시도 여부를 결정합니다.

이벤트를 삭제해도 history/alert를 cascade 삭제하지 않습니다. 이들은 독립적인 감사·전달 이력이며
각자의 정책으로 만료됩니다. 원본 이벤트가 없는 이력이 잠시 남을 수 있습니다.
TTL 인덱스, scheduler 자동 삭제, DB/collection drop, ES DELETE 호출은 없습니다.
Mongo의 개별 문서 삭제가 ES로 전파되는 것은 기존 Monstache 동작이며 별도로 PC에서 관찰해야 합니다.
백업·기존 export·Telegram 메시지의 삭제나 법적 보존 예외 처리는 자동화하지 않았습니다.

## 감사 이벤트

목록/상세 접근 결과, 로그인 성공/실패, 로그아웃, retention 실행 시작·collection별 삭제 건수·결과를
`governance/audit.py`가 JSON으로 기록합니다.
허용 정보는 UTC 시각, 정해진 event/category/result, 인증 여부, HTTP 상태,
user PK/document ID의 domain-separated SHA-256 해시, collection 이름과 건수입니다.
실패한 로그인의 username/credentials는 기록하지 않습니다. 사용자명, IP, query,
company_url, description, exception 메시지, 환경변수 값은 audit API의 필드가 아닙니다.
해시는 익명화가 아니라 상관관계를 위한 가명 표기이며 후보 값을 아는 사람은 비교할 수 있습니다.
HTTP 200 접근 기록은 Mongo 데이터의 정확성이나 가용성을 입증하는 기록이 아닙니다.

처음 이벤트가 발생할 때 `logs/`를 생성합니다. 파일은 1 MiB마다 회전하며 이전 5개를 보관합니다.
POSIX에서 디렉터리 0700/파일 0600을 적용하도록 시도하고, Windows에서는 운영자 ACL을 확인합니다.
초기 파일 생성 또는 쓰기 실패는 원문 예외를 표시하지 않고 stderr로 안전한 JSON을 보냅니다.
fallback이 발생하면 운영자가 stderr 수집과 저장 공간을 점검해야 합니다.
다중 프로세스가 하나의 회전 파일을 공유하는 운영 구성은 보장하지 않습니다. 단일 프로세스로 사용하거나
운영자가 중앙 로그 수집/프로세스별 파일 구성을 별도로 마련해야 합니다.
변조 방지, 감사 전용 서버, 시간 기반 audit 보유기간 자동 삭제는 구현하지 않습니다.

## Source 상태와 검토

`governance/sources.py`의 Bitlock만 ACTIVE이며 나머지 세 source는 UNAVAILABLE입니다.
ACTIVE/PAUSED/UNAVAILABLE/RETIRED는 이 프로젝트의 수집 운영 상태입니다.
위협 행위자의 실제 활동 여부를 단정하지 않습니다.
name/module/status/reason/review_note를 수동 검토·변경하고 scheduler를 재시작합니다.
registry에 onion URL을 중복 저장하지 않으며 import/build 시 live probing하지 않습니다.
기존 네 parser와 synthetic fixture는 모두 유지됩니다.

## 점검 범위와 테스트 데이터

Mongo checker는 세 threat collection의 금지된 **최상위 필드명**을 대소문자 무시로 점검합니다.
값이 null이어도 필드가 있으면 감지합니다. 중첩된 모든 값의 DLP 검사는 아닙니다.
alert_log message/claim_token 예외는 내부 전달 호환에만 적용합니다.
만료 건수가 0보다 커도 read-only 점검 자체는 PASS일 수 있습니다. 이는 삭제 승인이나
모든 보유기간 준수 선언이 아닙니다. 미래/누락/invalid 값은 별도 수동 검토 항목입니다.

`--check-elk`는 Day 12 mapping/소유 DB 검사를 재사용하고, runtime field로 _source의
최상위 키만 서버에서 검사해 count를 받습니다. 문서 body는 반환하지 않습니다.
읽기 검색(POST /_search)이며 mapping/index를 변경하지 않습니다.
실제 ES가 꺼졌거나 검색/스크립트 권한·시간 제한이 맞지 않으면 명확한 FAIL을 반환합니다.
Work의 fake 테스트는 실제 Mongo 표현식/ES Painless 실행을 대체하지 않습니다.

테스트에는 Example Company, 예약 도메인, 합성 시각·토큰 형태만 사용합니다.
실제 피해자 개인정보·유출 원문·실제 자격증명을 fixture/로그/보고서에 넣지 않습니다.
Work는 실제 Atlas/Telegram/Tor/ELK/Docker를 실행하지 않습니다.
사용자 PC에서는 먼저 읽기 점검만 실행하고, 삭제 동작 실습은 합성 DB에서만 선택적으로 수행합니다.

## 구현에 사용한 공식 문서

Django 내장 인증과 POST 로그아웃은 [Django 5.2 인증 문서](https://docs.djangoproject.com/en/5.2/topics/auth/default/)를,
이벤트 수신은 [인증 signal 문서](https://docs.djangoproject.com/en/5.2/ref/contrib/auth/#topics-auth-signals)를 참고합니다.
Mongo scalar 형식/누락 판단은 [aggregation $type 문서](https://www.mongodb.com/docs/manual/reference/operator/aggregation/type/)에 근거합니다.
ES의 _source 읽기와 emit은 [Painless runtime field context 문서](https://www.elastic.co/docs/reference/scripting-languages/painless/painless-runtime-fields-context)에 근거하며,
사용자 PC의 고정된 Day 12 버전에서 실제 검색 검증을 남겨 둡니다.
