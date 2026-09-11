# Day 12 ELK 운영 및 PC 검증

## 구성과 변경 이유

기존 설정은 darkweb.leaked_data와 leaked_data 인덱스를 고정 사용했습니다.
이력/알림 동기화, 명시적 매핑, Data views 설정 및 동기화 검증 도구가 없었습니다.
컬렉션/DB drop 전파와 확인 없는 resume 쓰기가 켜져 있었습니다.

| Mongo 컬렉션 | Elasticsearch 인덱스 | 분석 용도 |
|---|---|---|
| DB_NAME.leaked_data | ELK_INDEX_PREFIX-events | 현재 사건·관찰 정보 |
| DB_NAME.leak_history | ELK_INDEX_PREFIX-history | 메타데이터 변경 |
| DB_NAME.alert_log | ELK_INDEX_PREFIX-alerts | 위험도·알림 상태 |

Monstache가 세 컬렉션을 읽고 project_document.js로 허용 필드만 투영한 후 ES에 저장합니다.
Kibana는 세 인덱스를 Data views로 조회합니다. Mongo의 원문 문서를 변경하는 변환은 없습니다.
alert_state와 Monstache 자체 상태 컬렉션은 동기화 목록에 없습니다.

Go template의 환경변수 접근은 {{index . "DB_NAME"}} 형식이고 -tpl로 활성화합니다.
초기 direct read와 change stream을 함께 사용합니다. direct-read-stateful은 완료된 초기 읽기의 반복을 막습니다.
resume-strategy=1의 토큰과 초기 읽기 상태는 monstache DB에 저장되며,
resume-name에 DB_NAME과 접두사를 모두 넣어 테스트/운영 상태를 구분합니다.
정상 재시작은 완료된 전체 읽기를 건너뜁니다. 중간에 실패한 초기 읽기는 반복될 수 있습니다.
관련 옵션은 [Monstache 설정 문서](https://rwynn.github.io/monstache-site/config/)와
[6.7.7 구현](https://github.com/rwynn/monstache/blob/v6.7.7/monstache.go)에 대조했습니다.

resume-write-unsafe=false이며, resume는 Monstache 동기화의 재개 기능입니다.
Telegram exactly-once 보장과는 관계가 없습니다.
Atlas 계정에는 원본 컬렉션 읽기/change stream 권한과 별도 monstache 상태 DB 쓰기 권한이 필요합니다.
토큰이 보존 범위를 벗어나거나 상태 DB 접근이 실패하면 자동 초기화하지 않고 원인을 확인합니다.
인덱스를 수동으로 없앤 경우에도 상태가 남아 있으면 초기 읽기가 다시 돌지 않습니다.
복구가 필요하면 원인 확인 후 새 접두사로 별도 인덱스를 준비해 전체 읽기를 수행할 수 있습니다.

컬렉션/DB drop에 따른 인덱스 자동 삭제는 껐습니다. 일반 개별 문서 삭제의 동기화까지 끈 것은 아닙니다.
삭제 대상 검색은 현재 접두사의 세 인덱스로 한정합니다. 이 구성은 DB 백업을 대체하지 않습니다.
이전 leaked_data 인덱스를 포함해 기존 인덱스를 삭제하거나 마이그레이션하는 명령은 없습니다.

## 매핑과 필드 최소화

elk/mappings.py에서 세 composable template을 생성합니다.
인덱스 패턴은 와일드카드가 없는 정확한 이름입니다.
문자 분류값은 keyword, 회사명은 text + keyword, 설명/유출 내용은 text,
관찰/변경/알림 시각은 date, 횟수/schema_version은 integer입니다.
publication_date는 원문 게시일 표현을 유지하는 keyword입니다.
history.changes는 Day 9의 메타데이터 8개와 before/after만 허용합니다.

dynamic=strict와 JS allowlist를 함께 사용합니다.
Mongo _id는 ES 문서 ID로 유지하고 _source의 일반 필드로 복사하지 않습니다.
ObjectId 참조는 Monstache에서 hex 문자열로 전달되며 document_id는 keyword로 저장합니다.
이 동작은 [Monstache 매핑/미들웨어 문서](https://rwynn.github.io/monstache-site/advanced/)와 버전 소스를 확인했습니다.

alert_log의 claim_token, resume_token, message, raw_exception 등 비허용 필드는 ES로 보내지 않습니다.
허용된 문자열에도 알려진 Mongo URI, Telegram 토큰 모양, URL 자격증명, 비밀값 대입 패턴을 가립니다.
임의의 비밀값 전체를 탐지하는 DLP 기능은 아니므로 허용 메타데이터에 자격증명을 넣는 운영은 하지 않습니다.
Mongo 원문 및 Day 10 알림 처리 로직은 바뀌지 않습니다.
legacy 문서의 관찰 필드가 없으면 ES 투영에서만 scraped_time을 대체 시각으로 사용합니다.

템플릿 설치는 [Elasticsearch Index Template API](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-indices-put-index-template),
Data views 설치는 [Kibana v8 Data Views API](https://www.elastic.co/docs/api/doc/kibana/v8/operation/operation-createdataviewdefaultw)를 사용합니다.
setup 도구는 템플릿과 빈 인덱스, Data views를 만듭니다. 같은 설정으로 재실행해도 중복 뷰를 만들지 않습니다.
기존 인덱스의 타입/필드 또는 원본 DB 표시가 다르면 삭제·덮어쓰기 대신 conflict로 종료합니다.
필요하면 별도의 새 접두사를 선택하세요. 템플릿 변경만으로 기존 인덱스의 필드 타입이 바뀌지는 않습니다.

## 환경과 실행 순서

버전: Elasticsearch/Kibana 8.15.3, Monstache 6.7.7.
신규 필수 Python 패키지는 없습니다. Python 3.11 이상과 기존 requirements.txt를 사용합니다.
ES/Kibana URL은 자격증명·쿼리·경로 없는 HTTP(S) origin만 지원합니다.
기본 구성의 인증 비활성/localhost 접근 범위를 유지하며 인증 방식 확대는 이번 범위가 아닙니다.
ELK_INDEX_PREFIX는 소문자/숫자로 시작하는 최대 64자의 소문자, 숫자, -, _입니다.
설정이 아예 없으면 darkweb-monitor를 사용하지만 명시적 빈 값은 거부합니다.
DB_NAME은 이 도구에서 이식 가능한 영문/숫자/-/_ 이름 1~63자를 사용하며 내부 DB 이름은 거부합니다.
원본과 상태 DB를 분리하기 위한 Day 12 도구의 제한입니다.

ES는 이미지에 포함된 curl로 응답을 확인하고, Kibana는 번들 Node로 /api/status를 확인합니다.
ES의 curl 포함 여부는 [8.15.3 Dockerfile](https://github.com/elastic/elasticsearch/blob/v8.15.3/distribution/docker/src/docker/Dockerfile)에서 확인했습니다.
Monstache는 ES healthy 이후 시작합니다. /healthz는 프로세스 생존 응답이며 동기화 완료 보장이 아닙니다.
check_elk_pipeline.py가 매핑, Data views와 세 컬렉션의 건수까지 별도로 검사합니다.

일반 docker compose up -d는 ES/Kibana를 시작합니다.
Monstache는 sync profile에 있으므로 **setup 완료 후** 명시적으로 시작해야 합니다.
기존 Monstache 컨테이너가 실행 중이면 먼저 중단하세요. 새 profile 추가만으로 기존 컨테이너가 멈추지는 않습니다.

## PC E2E — Windows PowerShell

프로젝트 최상단에서 실행합니다. 오류가 나오면 다음 쓰기 단계 전에 오류를 해결합니다.
기존 .env의 DB_URI는 그대로 사용하고 비밀값을 출력하지 않습니다.
현재 가상환경의 Python을 사용하세요. py가 다른 인터프리터를 선택하면 python으로 바꿉니다.
Work에서는 아래 실제 서비스 명령을 실행하지 않았습니다.

### 1. 코드 및 환경 검사

~~~powershell
py -m unittest tests.test_elk -v
py -m unittest discover -v
py DjangoProject/manage.py test mongoDbConnect -v 2
py DjangoProject/manage.py check
py scripts/check_secrets.py
py scripts/check_runtime_config.py
git diff --check
~~~

Node가 설치되어 있으면 추가로 변환 스크립트를 검사할 수 있습니다.
~~~powershell
node --test tests/test_elk_projection.js
~~~

Node는 Work 변환 검사에 사용했으며 Python 도구 실행을 위한 신규 dependency는 아닙니다.

### 2. 테스트 DB/접두사 선택

아래 이름을 처음 사용할 때의 절차입니다.
이미 revision 2/3을 실행했다면 데이터를 초기화하지 말고 기존 단계를 이어가거나
새 이름(예: day12_elk_e2e_2, day12-e2e-2)을 사용하세요.
DB를 바꾸면 접두사도 새로 선택해야 합니다.

~~~powershell
$env:DB_NAME = "day12_elk_e2e"
$env:ELK_INDEX_PREFIX = "day12-e2e"
py scripts/check_runtime_config.py
docker version
docker compose config --quiet
docker compose --profile sync stop monstache
~~~

현재 터미널 환경변수는 .env보다 우선합니다. 실제 .env 파일을 덮어쓰지 않습니다.
Docker Desktop 엔진이 실행 중이어야 합니다. 테스트 중 crawler/scheduler/Telegram watcher는 실행하지 않습니다.
DB_URI 계정은 테스트 DB와 monstache 상태 DB에도 접근할 수 있어야 합니다.

### 3. ES/Kibana 준비 및 초기 데이터

~~~powershell
docker compose up -d elasticsearch kibana
docker compose ps
py scripts/setup_elk.py --wait 120
py scripts/day12_elk_e2e.py seed --database day12_elk_e2e
~~~

setup은 DAY12_ELK_SETUP: PASS가 정상입니다.
seed는 세 컬렉션에 각 1건의 합성 메타데이터를 준비합니다.
합성 알림은 SUPPRESSED이며 Telegram API를 호출하지 않습니다.
setup을 한 번 더 실행해도 Data views가 6개로 늘어나면 안 됩니다.

### 4. 초기 direct read 확인

~~~powershell
docker compose --profile sync up -d monstache
Invoke-RestMethod http://127.0.0.1:8080/healthz
py scripts/check_elk_pipeline.py --wait 120
py scripts/day12_elk_e2e.py verify --database day12_elk_e2e --revision 1 --wait 120
~~~

정상 건수: events 1/1, history 1/1, alerts 1/1 (Mongo/ES).
E2E 검사는 문서 3개의 ID/내용/시각과 알림 비공개 필드 제외를 확인합니다.
필요하면 http://127.0.0.1:8080/stats 에서 색인 통계를 확인할 수 있습니다.

### 5. 실시간 update/insert 확인

~~~powershell
py scripts/day12_elk_e2e.py update --database day12_elk_e2e --revision 2
py scripts/check_elk_pipeline.py --wait 120
py scripts/day12_elk_e2e.py verify --database day12_elk_e2e --revision 2 --wait 120
~~~

같은 사건의 규모는 10 GB에서 20 GB로 바뀝니다.
정상 건수: events 1/1, history 2/2, alerts 2/2.
사건 _id/first_seen은 유지되고 새 변경 이력과 합성 알림만 추가됩니다.
E2E 검사는 총 5개 문서의 내용을 확인합니다.

### 6. 중단 중 변경과 resume 확인

~~~powershell
docker compose --profile sync stop monstache
py scripts/day12_elk_e2e.py update --database day12_elk_e2e --revision 3
docker compose --profile sync start monstache
py scripts/check_elk_pipeline.py --wait 120
py scripts/day12_elk_e2e.py verify --database day12_elk_e2e --revision 3 --wait 120
~~~

규모는 30 GB, 정상 건수는 events 1/1, history 3/3, alerts 3/3입니다.
중단 중 기록한 revision 3이 반영되면 resume 경로의 실제 동작을 확인한 것입니다.
건수 비교만으로는 내용 동일성을 증명하지 못하므로 E2E 내용 검사도 함께 실행합니다.
검사 도구는 DELETE 요청이나 Mongo 쓰기를 하지 않습니다.
실시간 다른 writer가 동작하면 건수 일치가 일시적으로 지연될 수 있습니다.

### 7. Kibana 확인 및 Export

http://127.0.0.1:5601/ 에서 Data views의 인덱스 패턴이 day12-e2e-*인지 확인합니다.
전역 시간 범위에 테스트 시각을 포함하고 elk/kibana_dashboard_spec.md대로 패널 5개를 만듭니다.
실행 중인 Kibana의 Saved Objects Export로 NDJSON을 생성합니다.
Work에서 dashboard 생성/Export가 완료됐다고 판정하지 않습니다.

### 8. 테스트 종료와 운영 설정

~~~powershell
docker compose --profile sync stop monstache
~~~

테스트 데이터/인덱스/상태는 남겨둡니다. 자동 drop/delete/볼륨 삭제는 하지 않습니다.
새 터미널에서 기존 .env 기준 DB_NAME과 운영 접두사를 선택한 뒤 setup을 실행하고 Monstache를 시작합니다.
운영 prefix의 템플릿을 설치하지 않은 상태로 Monstache부터 시작하지 마세요.
새 prefix는 새로운 초기 읽기를 수행합니다. 기존 prefix 상태를 그대로 재사용하면 전체 읽기를 반복하지 않습니다.

## 오류 해석

| category | 의미 / 확인할 항목 |
|---|---|
| invalid_ELK_INDEX_PREFIX | 빈 값, 대문자, 특수문자, 길이 제한 |
| invalid_DB_NAME | 도구가 지원하는 namespace 또는 내부 DB 제한 |
| invalid_ELASTICSEARCH_URL / invalid_KIBANA_URL | HTTP(S) origin 형식 |
| unreachable / timeout / connection_error | 해당 서비스와 포트 응답 |
| not_ready | ES green/yellow 또는 Kibana available까지 준비되지 않음 |
| mapping_or_database_conflict | 기존 인덱스의 타입/허용 필드/원본 DB 불일치; 별도 prefix 필요 |
| data_view_conflict / data_view_time_field_conflict | 기존 뷰의 인덱스 또는 시간 필드 불일치 |
| database_error | Mongo 연결/권한/조회 실패; 원문 오류/URI는 도구가 출력하지 않음 |
| count_mismatch | 아직 동기화 중이거나 기존 인덱스/데이터에 차이가 있음 |
| private_field_leak | E2E가 비공개 알림 필드의 ES 유입을 발견함 |

HTTP 요청/응답은 제한된 크기와 timeout으로 처리하고 원문 오류 응답은 출력하지 않습니다.
--wait는 재시도 창이며 개별 진행 중인 HTTP/Mongo timeout까지 포함한 엄밀한 전체 실행시간 제한은 아닙니다.
