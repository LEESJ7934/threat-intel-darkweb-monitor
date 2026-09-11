# Project Story — Portfolio & Interview Notes

이 문서는 저장소 기능을 과장하지 않고 **문제 → 판단 → 구현 → 검증 → 결과** 순서로 설명하기 위한 발표/포트폴리오용 요약입니다.

## 1. 한 문장 소개

여러 다크웹 유출 게시글을 단순 저장하던 초기 프로젝트를, **사건 단위 identity·변경 이력·알림·분석·운영 통제까지 연결하는 위협 인텔리전스 모니터링 파이프라인**으로 개선했습니다.

## 2. 처음 상태에서 확인한 문제

초기 구조는 source별 crawler가 HTML을 읽어 MongoDB에 저장하고 Django에서 결과를 조회하는 데 초점이 있었습니다. 실제 운영 관점에서는 다음 문제가 남았습니다.

- 같은 사건을 다시 수집했을 때 중복과 재관찰을 구분하기 어려움
- 피해 규모나 설명이 바뀌어도 현재 상태만 남아 변경 근거가 사라짐
- 새 사건과 단순 날짜 변경을 같은 수준으로 알릴 수 있어 알림 피로가 생김
- crawler availability와 parser correctness가 한 덩어리로 취급됨
- Mongo 내부 delivery/private field까지 분석 표면에 노출될 위험이 있음
- 대시보드 접근통제, 보유기간, 감사, source lifecycle 같은 운영 통제가 부족함

## 3. 개선 흐름

```text
Crawler refactor
→ stable event identity / dedupe / history
→ NEW / UPDATED alert + retry / dedupe
→ analyst dashboard
→ MongoDB → Monstache → Elasticsearch / Kibana
→ access / retention / audit / source lifecycle governance
```

각 단계는 앞 단계의 schema와 동작을 최대한 보존하고, 새 기능 때문에 기존 기능이 깨지지 않는지 regression을 반복 확인했습니다.

## 4. 주요 설계 판단

### 기존 `_id`를 버리지 않고 `event_key`를 추가

기존 parser `_id`는 이미 저장된 문서와 fixture의 호환성에 사용되고 있었습니다. 이를 즉시 교체하지 않고 장기 사건 식별용 `event_key`를 별도로 두어 **레거시 호환성과 안정적인 사건 추적을 분리**했습니다.

### 현재 상태와 변경 이력을 분리

`leaked_data`에는 현재 상태를, `leak_history`에는 field-level before/after를 기록했습니다. 동일 변경 재처리 시 history가 중복되지 않도록 deterministic ID를 사용했습니다.

### 모든 변경을 알리지 않음

`NEW`와 `UPDATED`를 구분하고, 날짜만 변하는 등 비본질적 변경은 `SUPPRESSED`할 수 있게 했습니다. 알림에는 deterministic identity, lease, retry/attempt count를 두어 재시작 후 중복 전송을 줄였습니다.

### MongoDB와 분석 표면을 분리

MongoDB 운영에 필요한 내부 상태를 Django/Elasticsearch에 전부 노출하지 않았습니다. Monstache projection에서 allowlist를 사용하고 `message`, `claim_token`, `resume_token` 같은 alert 내부 필드는 Elasticsearch에서 제외했습니다.

### 접근 불가 source를 삭제하지 않고 lifecycle로 관리

parser와 fixture는 재현 자료로 남기되 scheduler는 `ACTIVE` source만 실행합니다. 현재 Bitlock만 ACTIVE이며 다른 source는 UNAVAILABLE 상태로 자동 실행에서 제외합니다. 이 상태는 위협 행위자의 실제 활동 여부를 의미하지 않습니다.

### retention은 자동 삭제보다 안전장치를 우선

운영 DB에 destructive test를 하지 않고 read-only checker와 DRY RUN을 기본으로 했습니다. 실제 삭제에는 `--apply`와 DB 이름 이중 확인이 필요하고, invalid/missing/future timestamp는 자동 삭제하지 않습니다.

## 5. 실제 통합 과정에서 발견한 문제

자동 테스트가 통과한 뒤에도 실제 환경에서 다음 문제를 발견했습니다.

### Elasticsearch date mapping 표현 차이

Elasticsearch 8.15.3이 기본 date format을 mapping 응답에서 생략해 strict comparator가 충돌로 오판했습니다. 생략 가능한 기본값만 호환 처리하고 나머지 mapping 검사는 유지했습니다.

### Monstache BSON datetime 호환

실제 Monstache/Otto 런타임에서 BSON datetime이 JavaScript native `Date`와 다른 Go-backed 형태로 전달돼 timestamp가 사라졌습니다. 여러 time 표현을 안전하게 ISO 문자열로 변환하도록 보완했습니다.

### Kibana Data View 이름 충돌

여러 테스트 index prefix가 공존할 때 같은 Data View display name이 충돌했습니다. prefix를 display name에 포함해 테스트 환경을 분리했습니다.

### 실제 브라우저 CSRF origin 오류

Django unit test는 통과했지만 실제 로그인 POST에서 template의 `no-referrer` 정책 때문에 Origin 검사가 실패했습니다. project 설정과 일치하도록 `same-origin`으로 수정하고 regression assertion을 추가한 뒤 로그인/로그아웃을 다시 검증했습니다.

이 경험 때문에 **mock/unit test 통과와 실제 서비스 통합 성공을 분리해서 판단**했습니다.

## 6. 검증 결과

최종 Day13 기준 확인한 대표 결과입니다.

- root unittest: 266 PASS
- Django `mongoDbConnect`: 77 PASS
- governance 전용 unittest: 72 PASS
- Django system check: 0 issues
- Node projection reference test: 14 PASS (Node 사용 가능 환경)
- secret/runtime/diff 검사: PASS
- MongoDB → Monstache → Elasticsearch initial sync: PASS
- 실행 중 change stream 반영: PASS
- Monstache 중단 중 변경 후 resume: PASS
- Kibana Data View 3개 / Dashboard 5개 패널 / Saved Objects export: PASS
- 실제 Mongo governance read-only check / retention DRY RUN: PASS
- 실제 Django 로그인 → 조회 → 로그아웃 → 미인증 재차단: PASS

## 7. 이력서용 요약

다음 문구는 구현한 범위를 넘어서는 제품/인증 수준을 주장하지 않는 버전입니다.

- 다크웹 유출 메타데이터 수집 프로젝트를 사건 단위 `event_key` 기반 dedupe·변경 이력 구조로 개선하고, 동일 사건 재관찰과 material change를 구분
- `NEW/UPDATED` 위험도 알림에 deterministic alert ID, retry/lease, 전송 이력을 적용하고 Telegram 실환경 E2E로 중복 방지와 재시작 복구 검증
- MongoDB 데이터를 Monstache로 Elasticsearch/Kibana에 실시간 투영하고 direct-read·change-stream·resume을 실제 Docker 환경에서 검증
- Django 인증·감사로그, metadata 최소화/redaction, retention DRY RUN, source lifecycle을 추가해 보안·개인정보/GRC 관점의 운영 통제 보완

## 8. 포트폴리오용 설명

초기 프로젝트는 여러 다크웹 사이트의 유출 게시글을 수집해 MongoDB에 저장하는 구조였지만, 동일 사건의 반복 수집과 정보 변경을 구분하기 어려웠습니다. 이를 개선하기 위해 기존 parser ID는 호환성을 위해 유지하면서 `event_key`를 별도로 도입하고, 현재 상태와 field-level 변경 이력을 분리했습니다. 이후 새 사건과 중요 변경을 `NEW/UPDATED`로 구분해 Telegram으로 알리고 deterministic alert ID와 retry/lease를 적용했습니다. 분석 단계에서는 Django 조회 화면과 MongoDB→Monstache→Elasticsearch/Kibana 파이프라인을 구성했으며, 최초 동기화·실시간 변경·중단 후 resume을 실제 환경에서 검증했습니다. 마지막으로 접근통제, 감사로그, metadata 최소화, retention DRY RUN, source lifecycle을 추가해 단순 수집기를 운영형 보안 모니터링 프로젝트로 확장했습니다.

## 9. 면접 60초 설명

> 기존 프로젝트는 다크웹 게시글을 수집해서 MongoDB에 저장하는 데 초점이 있었습니다. 그런데 같은 사건을 반복 수집했을 때 중복인지 업데이트인지 구분하기 어렵고, 변경 이력이나 알림 근거도 남지 않는 문제가 있었습니다. 그래서 기존 ID 호환성은 유지하면서 사건 단위 event_key를 추가하고, 현재 상태와 변경 이력을 분리했습니다. 이후 NEW와 UPDATED 알림, retry와 중복 방지, Django 분석 화면, Elasticsearch/Kibana 실시간 분석까지 연결했습니다. 특히 실제 통합 과정에서 Elasticsearch mapping, Monstache BSON datetime, Kibana Data View 충돌, Django 브라우저 CSRF 문제를 발견해 수정했고, 마지막에는 접근통제·감사·보유기간·source lifecycle까지 추가했습니다. 기능을 많이 넣는 것보다 각 단계가 재시작과 실제 환경에서도 유지되는지를 검증하는 데 초점을 뒀습니다.

## 10. 예상 꼬리질문 핵심 답변

**Q. 왜 기존 `_id`를 바로 바꾸지 않았나요?**
기존 DB와 fixture 호환성이 깨질 수 있어 parser ID는 그대로 두고 장기 사건 식별 책임을 `event_key`로 분리했습니다.

**Q. 왜 MongoDB collection을 current/history로 나눴나요?**
현재 상태 조회와 변경 근거 보존의 목적이 다르기 때문입니다. 현재값을 덮어쓰기만 하면 무엇이 언제 바뀌었는지 설명할 수 없습니다.

**Q. 왜 모든 변경을 Telegram으로 보내지 않았나요?**
날짜만 바뀌는 변화까지 모두 보내면 알림 피로가 생기므로 material change와 non-material change를 분리했습니다.

**Q. Monstache를 왜 썼나요?**
MongoDB의 운영 데이터를 유지하면서 Elasticsearch를 분석용 read model로 분리하고 change stream 기반 실시간 투영과 resume을 구현하기 위해 사용했습니다.

**Q. Work/자동 테스트가 통과했는데 왜 PC E2E가 필요했나요?**
실제 서비스 버전과 런타임 표현 차이는 mock에서 드러나지 않았습니다. BSON datetime과 CSRF origin 문제처럼 실제 Monstache/브라우저에서만 발견된 오류가 있었습니다.

**Q. retention을 왜 자동 TTL로 바로 처리하지 않았나요?**
삭제는 복구 비용이 크기 때문에 먼저 대상 수를 read-only로 확인하고 DRY RUN과 명시적 승인 후 삭제하도록 안전장치를 우선했습니다.

**Q. UNAVAILABLE source의 crawler를 왜 삭제하지 않았나요?**
현재 live availability와 parser 구현의 재현성은 별개라서 fixture와 parser는 보존하고 scheduler 자동 실행만 lifecycle 상태로 제어했습니다.

## 11. 설명할 때 지킬 경계

이 프로젝트를 설명할 때 다음 표현은 피합니다.

- "실제 다크웹 전체를 실시간 탐지한다"
- "ISMS-P/PIPA를 준수하는 시스템이다"
- "모든 개인정보나 credential을 탐지한다"
- "UNAVAILABLE source의 위협 조직이 활동을 종료했다"
- "법정 보유기간에 맞춰 자동 삭제한다"

대신 **프로젝트 범위에서 구현한 운영 통제, 실제로 확인한 E2E, 아직 남은 한계**를 같이 설명합니다.
