# Architecture & Design Decisions

이 문서는 구현된 코드에서 중요한 설계 판단과 trade-off를 설명합니다. 기능 목록보다 **왜 이렇게 나눴는지**에 초점을 둡니다.

## 1. crawler / parser / storage 분리

초기 크롤러는 browser 제어, HTML parsing, MongoDB 저장 책임이 한 파일에 섞이기 쉬웠습니다. 현재는 다음 경계를 둡니다.

```text
browser/rendering
  → pure parse_html(html)
  → normalized LeakRecord-like document
  → storage.save_record(s)
```

이 구조의 목적은 live onion 접근 없이도 fixture만으로 parser를 반복 검증하고, import 시 browser/network/DB side effect가 발생하지 않게 만드는 것입니다.

`crawling/common.py`는 browser, config, Mongo lifecycle 같은 공통 실행부를 담당하고, 각 source module은 selector와 source-specific parser에 집중합니다.

## 2. legacy parser ID와 event identity를 분리

기존 문서는 source별 MD5 기반 parser `_id`를 이미 사용하고 있었습니다. 이것을 한 번에 교체하면 기존 MongoDB 데이터와 호환성이 깨질 수 있어 `_id`는 유지했습니다.

대신 사건 단위 추적을 위한 `event_key`를 별도로 계산합니다.

우선순위:

1. `source + canonical company_url`
2. URL을 사용할 수 없으면 `source + normalized company_name`
3. 둘 다 없으면 `source + legacy _id`

그 결과 parser의 역사적 ID와 장기 사건 identity의 책임이 분리됩니다.

### 왜 company URL을 그대로 쓰지 않는가

`http/https` 차이와 root slash는 같은 대상을 불필요하게 분리할 수 있어 canonicalization합니다. 반대로 path case, port, query 등 사건을 구분할 가능성이 있는 정보는 보존합니다. credential이 포함된 URL은 identity로 사용하지 않습니다.

## 3. 현재 상태와 변경 이력을 분리

`leaked_data`는 사건의 현재 상태를, `leak_history`는 변경 사실을 기록합니다.

```text
same fingerprint
→ last_seen / observation_count 갱신

changed fingerprint
→ deterministic history 기록
→ current state 갱신
```

history ID도 deterministic하게 만들어 중간 실패 후 같은 변경을 다시 처리하더라도 같은 이력이 반복 생성되지 않도록 했습니다.

MongoDB 두 collection 사이에 cross-collection transaction을 강제하지 않습니다. 대신 history를 먼저 기록하고 deterministic ID로 재시도를 안전하게 만드는 방식을 선택했습니다.

## 4. NEW와 UPDATED 알림을 분리

새 사건과 기존 사건의 중요한 변경은 분석가에게 의미가 다릅니다.

- `NEW`: 처음 발견된 사건
- `UPDATED`: 이미 존재하는 사건의 material change
- 비본질적 변경: 정책에 따라 `SUPPRESSED`

알림은 `alert_log`에서 상태를 추적하고 deterministic alert identity, lease, retry count를 사용합니다. 프로세스가 재시작돼도 같은 알림을 무한 중복 전송하지 않는 것이 목표입니다.

위험도는 수집된 metadata를 기반으로 한 프로젝트 내부 분류이며 실제 사고의 법적/사업적 심각도를 확정하는 판정이 아닙니다.

## 5. MongoDB와 분석 표면을 분리

MongoDB에는 운영을 위해 필요한 내부 필드가 존재할 수 있지만 Django/Elasticsearch에 모두 노출할 이유는 없습니다.

따라서 두 단계의 최소화를 적용합니다.

1. crawler storage: 허용된 metadata 중심 저장
2. Monstache projection: 분석용 allowlist만 Elasticsearch로 전달

특히 alert delivery에 필요한 `message`, `claim_token`, `resume_token` 같은 값은 분석 index에서 제외합니다.

## 6. Elasticsearch 동기화는 direct read + change stream + resume

Monstache는 세 collection을 각각 events/history/alerts index에 연결합니다.

- `direct-read`: 처음 시작할 때 기존 MongoDB 데이터를 적재
- `change-stream`: 실행 중 새 변경을 실시간 반영
- `resume`: Monstache가 중단된 동안의 변경을 재시작 후 이어서 처리

이 세 동작을 각각 실제 Docker 환경에서 검증했습니다. 단순히 "컨테이너가 켜졌다"가 아니라 데이터 개수와 document content/timestamp까지 확인했습니다.

## 7. source lifecycle을 scheduler와 분리

크롤러 파일이 존재한다는 사실과 실제 운영에서 자동 실행해야 한다는 사실은 다릅니다.

`governance/sources.py`는 다음 상태를 지원합니다.

```text
ACTIVE / PAUSED / UNAVAILABLE / RETIRED
```

현재 `ACTIVE`는 Bitlock 하나입니다. 다른 parser/fixture는 삭제하지 않고 offline 재현 자료로 유지합니다. scheduler는 registry를 조회해 ACTIVE module만 실행합니다.

이 상태는 "해당 위협 행위자가 실제로 활동 중/종료됨"을 의미하지 않고 **이 프로젝트가 해당 source를 현재 자동 수집 대상으로 취급하는지**만 나타냅니다.

## 8. 접근통제는 조회 로직 바깥에 둠

Django 목록/상세의 Mongo query 로직을 다시 작성하지 않고 view 바깥의 auth gate로 접근을 제한했습니다.

```text
unauthenticated request
→ Mongo query 전에 login redirect

authenticated request
→ 기존 read-only dashboard query
```

목표는 Day11의 조회/필터/상세 기능을 최대한 그대로 유지하면서 Day13에 인증과 감사 통제를 추가하는 것이었습니다.

운영 모드(`DJANGO_DEBUG=False`)에서는 인증 비활성화와 wildcard host를 거부합니다.

## 9. audit는 원문보다 행위 증거를 기록

감사 로그에는 사건 description, raw query, 전체 company URL, 비밀번호, token, DB URI를 남기지 않습니다.

대신 다음 범주를 기록합니다.

- UTC timestamp
- event/category/result
- authenticated 여부
- HTTP status
- 안전한 identifier hash
- retention count

`logs/audit.jsonl`은 rotation을 사용하며, 이 구조는 단일 프로세스 로컬 프로젝트 기준입니다. 중앙 로그 서버나 조직 수준 SIEM을 대체하지 않습니다.

## 10. retention은 자동 삭제보다 안전한 실행을 우선

기본 정책은 다음과 같습니다.

- event: 365일
- history: 365일
- alert: 180일

하지만 숫자는 프로젝트 기본값일 뿐 법정 의무 기간이 아닙니다.

실행 설계:

```text
read-only checker
→ expired/missing/invalid/future count
→ DRY RUN
→ 명시적 --apply
→ configured DB / requested DB / confirmation DB 일치 확인
→ collection별 delete_many
```

invalid/missing/future timestamp는 자동 삭제하지 않습니다. event 삭제가 history/alert를 cascade 삭제하지도 않습니다.

## 11. 데이터 최소화의 한계

credential redaction은 다음과 같은 명백한 secret 형태를 보수적으로 가리는 기능입니다.

- credential이 포함된 Mongo URI
- Telegram bot token 형태
- URL embedded credential
- password/token/api_key/secret assignment
- private key block

이는 완전한 PII/DLP 탐지기가 아닙니다. 과도한 자신감을 피하기 위해 README와 governance 문서에 이 한계를 명시합니다.

## 12. destructive action을 줄인 이유

이 프로젝트의 검증 원칙은 "운영 데이터에 위험한 작업을 해서 성공을 증명하지 않는다"입니다.

- 실제 유출 DB retention delete E2E 금지
- 합성 DB에서만 삭제 시나리오 허용
- DB/collection drop 금지
- governance retention tool이 Elasticsearch DELETE API를 직접 호출하지 않음
- CAPTCHA bypass / credential reuse / 피해 시스템 접근 금지

기능 구현보다 **안전한 검증 경계**도 프로젝트의 설계 결과로 간주합니다.
