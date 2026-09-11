# Day 12 Kibana Dashboard 명세

대상 버전은 Kibana 8.15.3입니다. Work에서는 Saved Object 내부 JSON을 만들지 않습니다.
PC에서 아래 패널을 구성하고 실제 Kibana가 내보낸 NDJSON을 최종 결과물로 사용합니다.

## Data views

setup_elk.py가 아래 세 뷰를 설치합니다. ID는 인덱스 이름과 같습니다.
같은 인덱스와 시간 필드를 사용하는 기존 뷰가 있으면 재사용합니다.
서로 다른 접두사의 뷰는 표시 이름이 같을 수 있으므로 **Index pattern**도 확인합니다.

| 표시 이름 | 인덱스 | 시간 필드 |
|---|---|---|
| Darkweb Events | PREFIX-events | last_seen |
| Darkweb Change History | PREFIX-history | changed_at |
| Darkweb Alert Delivery | PREFIX-alerts | created_at |

## 패널 5개

| 패널 제목 | Data view | 구성 | 해석 |
|---|---|---|---|
| 탐지 사건 시간 추이 — 최근 관찰 기준 | Events | Lens 선/막대, X last_seen 날짜 히스토그램, Y 문서 수 | 현재 사건 문서의 최근 관찰 시각 분포 |
| source별 사건 분포 | Events | Lens 가로 막대, source 상위 값 10개, 문서 수 | 출처별 현재 사건 수 |
| 위험도별 알림 분포 | Alert Delivery | Lens 막대/도넛, risk_level 상위 값 5개, 문서 수 | 사건 수가 아닌 알림 기록 수 |
| 알림 상태 분포 | Alert Delivery | Lens 막대/도넛, status 상위 값 10개, 문서 수 | SENT, SUPPRESSED, FAILED 등 저장된 상태 |
| 최근 변경 이벤트 | Change History | Discover 저장 검색, changed_at 내림차순, source/document_id/event_key/changes 열 | 사건 메타데이터의 변경 기록 |

위험도는 alerts.risk_level만 사용합니다. Events에 위험도 runtime field나 별도 분류 스크립트를 만들지 않습니다.
첫 번째 패널은 모든 관찰 횟수의 시계열이나 최초 발견 추이가 아닙니다.
사건이 다시 관찰되면 같은 문서의 last_seen이 바뀌므로 과거 버킷의 수가 달라질 수 있습니다.

## PC 작성 순서

1. http://127.0.0.1:5601/ 에서 Analytics → Discover를 엽니다.
2. 데이터 뷰의 Index pattern이 현재 ELK_INDEX_PREFIX와 일치하는지 확인합니다.
3. 시간 범위를 최근 7일 또는 테스트 시각을 포함하는 절대 범위로 지정합니다.
4. Advanced Settings의 dateFormat:tz를 UTC로 맞추면 Mongo/검증 도구 시각과 비교하기 쉽습니다.
5. Dashboard → Create dashboard → Create visualization에서 위 Lens 패널 4개를 구성합니다.
6. 각 패널에 표의 제목을 붙이고 저장합니다.
7. Discover에서 Change History 뷰를 골라 필요한 열을 추가하고 changed_at 내림차순으로 정렬합니다.
8. 검색을 저장한 뒤 대시보드의 Add from library로 추가합니다.
9. 대시보드를 **Day12 Darkweb Pipeline — 사용한 접두사**라는 이름으로 저장합니다.

E2E 합성 알림은 INFO/SUPPRESSED입니다. HIGH나 SENT가 없는 것이 정상입니다.
데이터가 보이지 않으면 인덱스 접두사와 전역 시간 범위를 먼저 확인합니다.

## Export

Stack Management → Saved Objects에서 방금 만든 대시보드만 선택하고 Export합니다.
관련 시각화·검색·Data views가 포함되는 옵션을 유지합니다.
파일명은 darkweb_day12_dashboard.ndjson으로 저장하고 Kibana 8.15.3에서 생성한 파일임을 기록합니다.
테스트 접두사를 사용한 뷰도 export에 포함되므로 운영 접두사로 자동 변환되지는 않습니다.
Export는 검색 결과 CSV와 다릅니다.
이 절차와 관련 객체 포함 방식은 [Elastic Saved objects 문서](https://www.elastic.co/docs/explore-analyze/find-and-organize/saved-objects#import-and-export)에 근거합니다.

Work 결과물에는 임의로 만든 NDJSON이 없습니다. PC export 후 생성된 파일이 최종 대시보드 결과물입니다.
