# Validation Strategy & Evidence

이 프로젝트는 unit test 숫자만으로 완료 판정하지 않습니다. 검증을 **offline logic → service integration → browser/operator flow** 세 층으로 나눴습니다.

## 1. Offline regression

최종 Day13 기준 실제 PC에서 다음을 다시 확인했습니다.

| 검증 | 결과 |
| --- | ---: |
| root `unittest discover` | 266 PASS |
| Django `mongoDbConnect` | 77 PASS |
| Django `manage.py check` | 0 issues |
| governance 전용 unittest | 72 PASS |
| secret checker | PASS |
| runtime config checker | PASS |
| `git diff --check` | PASS |

JavaScript Monstache projection reference test는 Node 사용 가능 환경에서 14개가 PASS했습니다. Node는 Python 앱 실행 자체의 필수 dependency는 아닙니다.

## 2. crawler / storage 검증

Offline HTML fixture를 사용해 실제 onion site에 접속하지 않고 다음을 검증합니다.

- source별 selector와 주요 metadata 추출
- 누락/blank/whitespace 처리
- legacy parser ID 호환
- UTC timestamp
- raw HTML 미저장
- import 시 WebDriver/Mongo/network side effect 없음
- browser/DB 오류 시 자원 정리
- stable event identity
- 동일 관찰 dedupe
- 변경 관찰 history 생성
- legacy schema 호환

live source availability와 parser correctness는 같은 문제가 아니므로 분리해서 판단합니다.

## 3. Telegram alert E2E

실제 PC에서 합성 사건을 사용해 NEW와 UPDATED 알림을 검증했습니다.

확인 항목:

- NEW HIGH 전송
- material change 후 UPDATED HIGH 전송
- 날짜만 바뀐 non-material change는 SUPPRESSED
- restart 후 같은 alert가 중복 전송되지 않음
- alert delivery state가 MongoDB에 남음

실제 피해자의 유출 원문 데이터나 credential을 테스트 fixture로 사용하지 않았습니다.

## 4. Django analyst dashboard E2E

실제 MongoDB 테스트 데이터를 사용해 다음을 확인했습니다.

- 목록 카드/차트와 사건 수
- search + risk filter 동기화
- 사건 상세 current metadata
- observation count
- field-level change history
- NEW/UPDATED/SUPPRESSED alert history

Day13 접근통제 적용 후에는 실제 브라우저에서 다음 흐름을 검증했습니다.

```text
미인증 / 요청
→ login redirect
→ 로그인 성공
→ dashboard 200
→ 로그아웃
→ 다시 / 요청
→ login redirect
```

### 실제 브라우저에서 발견한 CSRF 문제

자동 Django 테스트는 통과했지만 실제 브라우저 로그인에서 CSRF origin 오류가 발생했습니다.

원인은 template의 `Referrer-Policy: no-referrer`와 Django의 POST Origin 검사 조합이었습니다. template meta policy를 project setting과 일치하는 `same-origin`으로 수정하고 regression assertion을 추가한 뒤 실제 로그인/로그아웃을 다시 확인했습니다.

이 사례는 mock/unit test만으로 통합 완료를 판정하지 않는 이유를 보여줍니다.

## 5. ELK E2E — 세 단계로 이해하기

Day12에서 사용한 `revision 1/2/3`은 Git revision이 아니라 **같은 합성 사건의 상태 변화 번호**였습니다. 의미는 아래 세 질문입니다.

### 상태 1 — 처음 켰을 때 기존 데이터를 가져오는가

```text
MongoDB에 기존 event/history/alert 존재
→ Monstache 시작
→ Elasticsearch에 동일 document 반영
```

검증 목적: `direct-read`.

### 상태 2 — 켜진 상태에서 변경을 실시간으로 따라가는가

```text
Monstache 실행 중
→ MongoDB 사건 metadata 변경
→ history/alert 추가
→ Elasticsearch count/content 자동 갱신
```

검증 목적: `change-stream`.

### 상태 3 — 중간에 꺼졌다 켜져도 변경을 놓치지 않는가

```text
Monstache stop
→ MongoDB 사건 변경
→ Monstache start
→ 중단 중 변경까지 Elasticsearch가 따라잡음
```

검증 목적: `resume`.

최종 확인 결과는 events 1개, history 3개, alerts 3개의 상태까지 동기화됐고 private alert field는 Elasticsearch에 노출되지 않았습니다.

## 6. ELK 실제 환경에서 발견한 세 문제

### Elasticsearch date mapping serialization

ES 8.15.3이 기본 date format을 mapping 응답에서 생략할 수 있어 strict comparator가 충돌로 판단했습니다. "ES가 생략 가능한 기본값"만 호환 처리하고 다른 mapping ownership 검사는 유지했습니다.

### Monstache BSON datetime

JavaScript projection의 `iso()`가 실제 Monstache/Otto 런타임에서 BSON datetime의 Go-backed 표현을 처리하지 못해 timestamp가 사라졌습니다. Go-like time method와 BSON 형태를 안전하게 처리하도록 보완한 뒤 실제 E2E로 확인했습니다.

### Kibana Data View 이름 충돌

여러 테스트 index prefix가 공존할 때 동일 display name이 충돌했습니다. Data View 이름에 prefix를 포함해 isolation했습니다.

세 문제 모두 offline mock만으로는 드러나지 않았고 실제 Elasticsearch/Kibana/Monstache 통합 과정에서 발견했습니다.

## 7. Kibana 결과물

실제 Kibana에서 다음 5개 패널을 구성했습니다.

1. 탐지 사건 시간 추이 — 최근 관찰 기준
2. source별 사건 분포
3. 위험도별 알림 분포
4. 알림 상태 분포
5. 최근 변경 이벤트

Dashboard와 관련 Lens/Saved Search/Data View를 함께 export했으며 `elk/darkweb_day12_dashboard.ndjson`에 저장했습니다.

## 8. Governance 실제 PC 검증

운영 DB에는 destructive test를 하지 않았습니다.

실제로 수행한 검증:

- `check_governance.py`: MongoDB read-only retention/금지 field/source registry 검사
- `apply_retention.py`: DRY RUN, `deleted=0`
- 인증/세션용 Django migration
- 실제 로그인/로그아웃/미인증 재차단
- audit event 확인

실제 audit 흐름 예:

```text
login_success      authenticated=True
 dashboard_access  authenticated=True  status=200
logout             authenticated=True
 dashboard_access  authenticated=False status=302
```

로그에는 username/password/DB URI/token/raw query/description를 넣지 않는 정책을 유지합니다.

## 9. 테스트가 보장하지 않는 것

PASS 숫자가 다음을 의미하지는 않습니다.

- 모든 onion source가 현재 online이라는 보장
- 사이트 selector가 앞으로도 변하지 않는다는 보장
- 모든 credential/PII를 검출한다는 보장
- Telegram/Atlas/ELK 외부 서비스 장애가 없다는 보장
- 법적/규제 준수 판정
- 실제 피해 시스템에 대한 취약점 또는 침해 사실 확정

따라서 기능 테스트와 운영 상태 판단을 분리하고, source lifecycle과 read-only governance checker를 별도로 둡니다.
