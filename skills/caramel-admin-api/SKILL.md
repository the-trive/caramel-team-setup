---
name: caramel-admin-api
description: 카라멜 어드민 REST API를 browse 클릭 없이 직접 호출. 세차권 지급·예약 취소/수정·구독 취소·포인트·주소·차량·환불·빙의 등 CS 어드민 고객상세(/admin/users/[id]) 전 기능. Use when 어드민에서 고객 데이터를 조회/변경(세차권 지급, 예약 취소, 포인트 지급 등)해야 하거나 "어드민 API", "콜콘솔 백엔드", "예약 취소 API" 등이 나올 때.
---

# caramel-admin-api 스킬

카라멜 어드민 REST API(zero-api, base: `https://api-prod.thetrive.com`)를 browse 없이 직접 호출하는 Bash 헬퍼 스킬.

## 헬퍼 사용법

```bash
HELPER=~/.claude/skills/caramel-admin-api/caramel-admin-api.sh

# 고객 상세 조회
bash $HELPER GET /v1/admin/users/225046

# 포인트 지급
bash $HELPER POST /v1/admin/users/225046/points '{"point":1000,"expiredDate":"2026-12-31","reason":"CS 보상"}'

# 예약 일괄취소 (세차권 반환)
bash $HELPER POST /v1/admin/users/225046/reservations/bulk-cancel '{"reservationIds":[12345,12346],"ticketAction":"GIVE_BACK"}'

# 구독 취소
bash $HELPER POST /v1/admin/users/225046/subscriptions/789/cancel '{"cashRefundAmount":0,"pointRefundAmount":0,"postprocess":{"actionType":"NONE","reason":[]}}'
```

- 응답 JSON: stdout (pretty-print)
- HTTP 상태코드: stderr `[HTTP 200]`
- 쓰기 요청: stderr `[WRITE] POST /v1/...` 경고 출력 후 실행

## API 목록

### 읽기 (GET)

| 엔드포인트 | 설명 |
|---|---|
| GET /v1/admin/users/{userId} | 고객 상세 |
| GET /v1/admin/users/{userId}/add-options | 예약폼 옵션(주소·디테일러) |
| GET /v1/admin/users/{userId}/subscriptions/{subscriptionId}/cancel/plan | 구독 취소계획 |
| GET /v1/admin/cars/{carId}/service-history | 차량 서비스이력 |
| GET /v1/admin/addresses/search?query= | 주소검색 |
| GET /v1/vehicles/brands | 차량 브랜드 (pretend token 필요) |
| GET /v1/vehicles/models?brandId= | 차량 모델 (pretend token 필요) |
| GET /v1/commerce/products?type=&displayTier= | 상품목록 |

### 쓰기 (POST/PATCH/DELETE)

| 엔드포인트 | 설명 |
|---|---|
| POST /v1/admin/users/{userId}/tickets | 세차권/옵션 지급 `{productIds[]}` |
| PATCH /v1/admin/users/{userId}/tickets/{ticketKind}/{ticketId} | 티켓 수정 `{usedYn,paidYn,deletedYn,postpaidYn,reservationId,endedAt 전필드 필수}` |
| POST /v1/admin/users/{userId}/entitlement-packages | 반얀 패키지 지급 `{packageKey}` |
| POST /v1/admin/users/{userId}/reservations | 예약 생성 `{addressId,carId,contact,detailerId,keyDirectHandoverYn,reservationDatetime,userServiceId}` ⚠️`keyDirectHandoverYn`은 **boolean**(`0` 넣으면 "Invalid request body" 400). `contact`만 optional. `userServiceId`는 적용 차량(`applicable_car_id`)이 `carId`와 맞는 세차권이어야 함 |
| PATCH /v1/admin/users/{userId}/reservations/{reservationId} | 예약 수정/재배정/완료 `{status,reservationDatetime,detailerId,addressId,carId,postpaidYn}` ⚠️재배정 시 → **"예약 재배정 가드" 섹션 필수** |
| POST /v1/admin/users/{userId}/reservations/bulk-cancel | 예약 일괄취소 `{reservationIds[], ticketAction: GIVE_BACK\|DELETE}` |
| POST /v1/admin/users/{userId}/subscriptions | 구독 추가 `{productId,representCarId}` |
| PATCH /v1/admin/users/{userId}/subscriptions/{subscriptionId} | 구독 수정 `{status,endedAt,pausedAt,stoppedAt,representCarId}` |
| POST /v1/admin/users/{userId}/subscriptions/{subscriptionId}/cancel | 구독 취소 `{cashRefundAmount,pointRefundAmount,postprocess{actionType,reason[]}}` |
| POST /v1/admin/users/{userId}/points | 포인트 지급 `{point,expiredDate,reason}` |
| PATCH /v1/admin/users/{userId}/points/{pointId} | 포인트 수정 `{point,leftPoint,expiredDate,reason}` |
| DELETE /v1/admin/users/{userId}/points/{pointId} | 포인트 삭제 |
| POST /v1/admin/users/{userId}/addresses | 주소 추가 `{address,dong,sido,sigungu 필수+옵션}` |
| PATCH /v1/admin/users/{userId}/addresses/{addressId} | 주소 수정 |
| DELETE /v1/admin/users/{userId}/addresses/{addressId} | 주소 삭제 |
| POST /v1/admin/users/{userId}/cars | 차량 추가 `{brandId,modelId,plateNumber}` |
| PATCH /v1/admin/users/{userId}/cars/{carId} | 차량 수정 |
| POST /v1/admin/users/{userId}/payments/{paymentId}/refund | 결제 환불 `{cashRefundAmount,pointRefundAmount,reasonMemo}` |
| PATCH /v1/admin/users/{userId} | 기본정보 수정 `{name,phone,note,adminYn}` |
| POST /v1/admin/users/{userId}/pretend-token | 빙의 토큰 발급 |

## 안전 규칙 — 스크립트가 3층으로 강제한다

`mysql-write.sh`와 같은 방식이다. 규칙을 문서에 적어두고 지키길 바라는 게 아니라, 스크립트가 거절한다.

| 층 | 대상 | 통과 조건 |
|---|---|---|
| `read` | GET·HEAD | 없음 |
| `reversible` | 세차권·옵션권·패키지·포인트·주소·차량 지급/수정, 예약 취소, 구독 | `CARAMEL_ADMIN_WRITE_APPROVED=1` |
| `outbound` | 예약 생성(`/reservations`·`/concierge-reservation`·`create-candidates/confirm`), 재배정·시각/주소 변경, 환불 | 위 + 대상이 `.test-accounts` 의 테스트 계정, 아니면 `CARAMEL_ADMIN_OUTBOUND_APPROVED=1` |

```bash
# 되돌릴 수 있는 것
CARAMEL_ADMIN_WRITE_APPROVED=1 bash $HELPER POST /v1/admin/users/225046/points '{"point":1000,"expiredDate":"2026-12-31","reason":"CS 보상"}'

# 실고객 예약 생성 — 대상과 body를 사용자에게 확인받은 뒤에만
CARAMEL_ADMIN_WRITE_APPROVED=1 CARAMEL_ADMIN_OUTBOUND_APPROVED=1 bash $HELPER POST /v1/admin/users/225046/reservations '{...}'
```

- `outbound`가 따로 있는 이유: 이 층만 **실제 디테일러 슬롯을 점유하고 고객에게 알림이 나간다.** 테스트 계정 대상 예약과 실고객 예약은 위험도가 다른데 명령만 보면 똑같아서, 계정 화이트리스트로 가른다.
- **테스트 계정 목록** = `.test-accounts` (한 줄에 userId 하나). 실고객 id를 넣지 말 것 — 이 목록이 유일한 안전장치다.
- 모든 쓰기는 `.audit.log`에 시각·환경·층·경로·대상 userId로 남는다.
- **prod 대상**: base URL이 `api-prod.thetrive.com`이므로 모든 호출이 운영 서버에 즉시 반영됨
- **raw SQL 대신 이 API**: DB 직접 write 금지, 반드시 이 헬퍼 경유

### E2E 실증 절차 (코드 수정이 실제로 먹는지 확인할 때)

테스트 계정에 상품을 지급해 한 사이클 돌리고 되돌린다. 실고객 데이터를 건드리지 않는다.

```bash
H=~/.claude/skills/caramel-admin-api/caramel-admin-api.sh
U=111175   # .test-accounts 에 있는 계정

CARAMEL_ADMIN_WRITE_APPROVED=1 bash $H POST /v1/admin/users/$U/entitlement-packages '{"packageKey":"PREMIUM_WASH_PACKAGE_1"}'
bash $H GET "/v1/admin/users/$U/reservation-create-candidates/available-slots?addressId=<addr>&carId=<car>&userServiceId=<us>&fromDate=<D>&toDate=<D+3>"
CARAMEL_ADMIN_WRITE_APPROVED=1 bash $H POST /v1/admin/users/$U/concierge-reservation '{"addressId":...,"carId":...,"pricingMode":"USER_SERVICE","serviceGroupId":1,"productId":null,"reservations":[{"detailerId":...,"reservationDatetime":"...Z","serviceIndex":0}]}'
# ← 여기서 DB로 확인
CARAMEL_ADMIN_WRITE_APPROVED=1 bash $H POST /v1/admin/users/$U/reservations/bulk-cancel '{"reservationIds":[<r>],"ticketAction":"DELETE"}'
```

- `PREMIUM_WASH_PACKAGE_1` = 세차권 1 + 프왁 1 + 살균 1. 패키지 묶음 동작을 보는 가장 작은 단위다.
- 보유 세차권은 서버가 `serviceGroupId` 안에서 **만료 임박순**으로 고른다. 테스트 계정에 다른 세차권이 있으면 방금 지급한 게 먼저 뽑히는지 `ended_at`으로 확인할 것.
- 끝나면 `bulk-cancel`(`ticketAction=DELETE`)로 예약과 세차권·붙은 옵션권을 함께 지운 뒤, `POST /v1/admin/users/{u}/entitlement-packages/cancel '{"instanceIds":[<inst>]}'`로 패키지 인스턴스를 CANCELLED로 닫는다(2026-09-17 실측). ⚠️`GIVE_BACK`은 패키지 항목을 재발급한다(옛 item → REPLACED, 새 세차권·옵션권 생성) — 테스트 정리에 쓰면 티켓이 다시 생긴다.

## 예약 재배정 가드 (필수 — 담당 디테일러 변경 시)

재배정 API(이 PATCH, sales-admin `PUT /careplus/reservations-admin/{id}/schedule`)는 **대상 디테일러의 근무시간·휴무·현직/퇴사를 전혀 검증하지 않는다** (겹침 체크도 같은 디테일러 동일시각만). 검증은 고객 슬롯조회 경로에만 있음 → **API 성공 ≠ 실제 가용.** 바뀔 디테일러를 정하기 전 아래를 **직접** 확인한다.

**대상 디테일러 적격 필터 (전부 만족해야 배정 가능)**
1. **현직**: `detailer_supply_sheet.status='현직'` (퇴사/하차/삭제/교육중 제외). ⚠️`detailer.retired_yn` 미신뢰 — 실제 퇴사자도 0(주진우147). `booking_yn=0`이 실질 비활성. supply_sheet 조인=phone `REPLACE(phone,'-','') COLLATE utf8mb4_general_ci`, `status IS NULL`=로스터 누락(퇴사 아님, 확인 필요).
2. **필드 디테일러**: `supply_sheet.region <> '오토랩'` (오토랩=고정샵, 출장 안 돎).
3. **예약수령**: `detailer.booking_yn=1`.
4. **휴무 아님**: 대상 시각이 `detailer_holiday(from~to, 둘 다 UTC)` 안에 없음(부분휴무=교육 등도 포함).
5. **겹침 없음**: 그 시각 ±(소요+이동)에 CONFIRMED 예약 없음.
6. **더미 제외**: 132 맹주원·125 노준호·168 손정민.
- ⚠️ **반얀 파견 예외**: `detailer_work_schedule.type LIKE 'BANYAN%'` 디테일러는 정상근무 차단용 **종일휴무**가 걸려도 그날 배정된 **반얀 예약(장충단로 60)은 본인 담당** → 4번(휴무)에서 제외.

**이 PATCH의 null-덮어쓰기 함정**: `carId`/`reservationDatetime`/`status`를 안 실으면 그 값이 null로 덮어써지고 `reservation_car` 재생성(차량연결 소멸). **재배정 시 현재값을 그대로 실어 보낼 것** — `carId`=reservation_car MAX, `reservationDatetime`=기존 UTC ISO, `status`='CONFIRMED', `postpaidYn`=number(0/1, boolean 넣으면 400). 상세: 메모리 `reference_reservation_reassign_api`.

## 주의사항

- **차량 브랜드/모델 조회** (`/v1/vehicles/brands`, `/v1/vehicles/models`): 고객 인증(pretend token) 필요 — 어드민 토큰만으로는 빈 응답 또는 403 가능
- **bulk-cancel ticketAction 값**: `GIVE_BACK`(세차권 반환) / `DELETE`(세차권 삭제 — 환불 없음). 기본은 `GIVE_BACK` 사용
- **토큰 캐시**: `.token` 파일에 20분간 캐시, 401/403 응답 시 자동 재로그인·재시도
- **토큰 값은 stdout 비노출**: 헬퍼가 토큰을 화면에 출력하지 않음. 보고 시도도 금지
