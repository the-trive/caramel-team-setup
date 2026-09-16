# Caramel DB Schema Reference

> Prisma schema 기반 DB 구조 문서. 쿼리 작성 시 참고용.
> 실제 DB 테이블명은 `@@map` 이 있는 경우 해당 이름 사용 (예: `user` → `app_user`, `option` → `options`)

---

## ⚠️ 쿼리 작성 시 공통 주의사항

### MySQL 환경
- `sql_mode = ONLY_FULL_GROUP_BY` 활성화 → SELECT의 모든 비집계 컬럼은 GROUP BY에 명시 필요
  - CASE WHEN을 SELECT에 쓰면 GROUP BY에도 동일하게 명시해야 함
- `requested_at` 등 DateTime 컬럼은 **UTC 저장** → 시간대 분석 시 KST 변환 필요
  ```sql
  HOUR(CONVERT_TZ(requested_at, '+00:00', '+09:00'))
  ```

### 예약 완료 상태
- 완료된 예약 필터 조건: `status IN ('REPORT_SENT', 'WASHED')`
- `COMPLETED` 는 실제로 사용되지 않음

### 구독 여부 판단
- `reservation.subscription_id` 는 **신뢰 불가** (실제 데이터의 98% 이상이 NULL)
- 구독 여부는 반드시 **subscription 테이블 EXISTS 서브쿼리**로 판단할 것
  ```sql
  EXISTS (
      SELECT 1 FROM subscription s
      WHERE s.user_id = r.user_id
        AND s.status = 'ACTIVE'
        AND s.started_at <= r.washed_at
  )
  ```
- active 구독 기준: `status = 'ACTIVE'` 만 해당 (`CREATED` 제외)

### 매출 집계
- 집계 대상 status: `IN ('PAID', 'PARTIAL_CANCELED')`
- `PARTIAL_CANCELED`(부분취소)는 실매출 = `amount - IFNULL(cancel_amount, 0)` 로 계산
- `deleted_yn = 0` 필터 필수

### reservation_car 조인
- `confirmed_yn` 은 실제로 **대부분 0** → 차량 조인 시 `confirmed_yn = 1` 조건 사용 시 결과 없음
- 차량 정보 조인 시 `confirmed_yn` 조건 제거하고 사용할 것

### MySQL LIMIT + IN 서브쿼리 제약
- `WHERE id IN (SELECT ... LIMIT N)` 구문 **지원 안 함** → 에러 발생
- 해결: 서브쿼리 결과를 먼저 별도 쿼리로 추출 후 직접 IN 절에 삽입하거나, JOIN으로 대체

---

## 목차

- [Core: 사용자/차량](#core-사용자차량)
- [Subscription: 구독](#subscription-구독)
- [Reservation: 예약](#reservation-예약)
- [Payment: 결제](#payment-결제)
- [Service & Option: 서비스/옵션](#service--option-서비스옵션)
- [Report & Checkup: 리포트/점검](#report--checkup-리포트점검)
- [Review: 리뷰](#review-리뷰)
- [CRM: 고객 관리](#crm-고객-관리)
- [Detailer: 디테일러](#detailer-디테일러) *(detailer_supply_sheet 포함)*
- [Promotion & Coupon: 프로모션/쿠폰](#promotion--coupon-프로모션쿠폰)
- [Point: 포인트](#point-포인트)
- [Cart: 장바구니](#cart-장바구니)
- [Message & Notification: 메시지](#message--notification-메시지)
- [Experiment & Link: 실험/링크](#experiment--link-실험링크)
- [Referral: 친구 초대](#referral-친구-초대) *(신규 - 2026-03)*
- [View Log: 페이지 조회 로그](#view-log-페이지-조회-로그) *(신규 - 2026-03)*
- [Community: 커뮤니티](#community-커뮤니티)
- [Config & System: 설정/시스템](#config--system-설정시스템)
- [Region & Scheduling: 지역/스케줄링](#region--scheduling-지역스케줄링)
- [Ads & Analytics: 광고/분석](#ads--analytics-광고분석)
- [Zone & Slot: 슬롯 수요 분석](#zone--slot-슬롯-수요-분석) *(신규 - 2026-03-11)*
- [Views](#views) *(car_model_target, car_target, v_detailer_holiday_daily)*
- [실험/코호트 분석 쿼리 패턴](#실험코호트-분석-쿼리-패턴) *(신규 - 2026-03, 업데이트 - 2026-03-13)*
- [Zone 수요-공급 분석 쿼리 패턴](#zone-수요-공급-분석-쿼리-패턴) *(신규 - 2026-03-11)*
- [테이블 관계 요약](#테이블-관계-요약)

---

## Core: 사용자/차량

### user (실제 테이블: `app_user`)

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(50) | NO | - | 이름 |
| phone | VarChar(15) | YES | - | 전화번호 |
| note | Text | YES | - | 메모 |
| utm_source | Text | YES | - | UTM 소스 |
| gender | VarChar(10) | YES | - | 성별 |
| age_range | VarChar(10) | YES | - | 연령대 |
| birthday | VarChar(10) | YES | - | 생일 |
| dealer_id | Int | YES | - | FK → dealer |
| created_at | DateTime | NO | now() | 생성일 |
| modified_at | DateTime | NO | now() | 수정일 |
| marketing_updated_at | DateTime | NO | now() | 마케팅 동의 수정일 |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| manually_registered_yn | TinyInt | NO | 1 | 수동 등록 여부 |
| tnc_yn | TinyInt | YES | - | 이용약관 동의 |
| privacy_yn | TinyInt | YES | - | 개인정보 동의 |
| marketing_yn | TinyInt | YES | - | 마케팅 동의 |
| over_14_yn | TinyInt | YES | - | 14세 이상 동의 |
| test_yn | TinyInt | NO | 0 | 테스트 계정 여부 |
| admin_yn | Boolean | NO | false | 관리자 여부 |
| temp_yn | Boolean | YES | false | 임시 유저 여부 |
| referrer | Text | YES | - | 추천인 |
| promotion_group_id | Int | YES | - | FK → promotion_group |
| uuid | VarChar(50) | YES | - | UUID (unique) |
| salesforce_id | VarChar(18) | YES | - | Salesforce ID |
| metadata | Json | YES | - | 메타데이터 |
| referral_code | VarChar(30) | YES | - | 추천 코드 (unique) |
| use_points_auto_yn | TinyInt | NO | 1 | 포인트 자동 사용 |
| delete_reason | Text | YES | - | 탈퇴 사유 |
| deleted_at | DateTime | YES | - | 탈퇴일 |

**인덱스:** `dealer_id`, `promotion_group_id`, `phone`

---

### car

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime | NO | now() | 생성일 |
| modified_at | DateTime | NO | now() | 수정일 |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| plate_number | VarChar(10) | YES | - | 차량번호 |
| model_year | Int | YES | - | 연식 |
| brand | VarChar(15) | YES | - | 브랜드명 (레거시) |
| model | VarChar(100) | YES | - | 모델명 (레거시) |
| tire_comment | Text | YES | - | 타이어 코멘트 |
| nickname | VarChar(25) | YES | - | 차량 별명 |
| user_id | Int | NO | - | FK → user |
| color | VarChar(15) | YES | - | 차량 색상 |
| mileage | Int | YES | - | 주행거리 |
| vin | VarChar(100) | YES | - | 차대번호 |
| temp_yn | Boolean | YES | false | 임시 차량 |
| manufacture_at | DateTime | YES | - | 제조일 |
| next_car_inspection_at | DateTime | YES | - | 다음 검사일 |
| registered_at | DateTime | YES | - | 최초 등록일 |
| final_registered_at | DateTime | YES | - | 최종 등록일 |
| subscription_id | Int | YES | - | FK → subscription |
| register_image_url | VarChar(300) | YES | - | 등록증 이미지 URL |
| car_front_tire_info | VarChar(45) | YES | - | 전륜 타이어 정보 |
| car_rear_tire_info | VarChar(45) | YES | - | 후륜 타이어 정보 |
| salesforce_id | VarChar(18) | YES | - | Salesforce ID |
| last_wonbu_at | DateTime | YES | - | 마지막 원부 조회일 |
| uuid | VarChar(40) | YES | - | UUID (unique) |
| brand_id | Int | YES | - | FK → car_brand |
| model_id | Int | YES | - | FK → car_model |

**인덱스:** `user_id`, `subscription_id`, `brand_id`, `model_id`, `plate_number`

---

### car_detail_info

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime | NO | now() | 생성일 |
| modified_at | DateTime | NO | now() | 수정일 |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| car_id | Int | NO | - | FK → car |
| key | VarChar(255) | NO | - | 키 (예: CUSTOM_PROCESS) |
| value | VarChar(255) | NO | - | 값 (PPF 보호, 무광 코팅 등) |

---

### car_brand

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(15) | YES | - | 브랜드명 |
| image | Text | YES | - | 로고 이미지 |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| display_order | Int | YES | - | 표시 순서 |
| category | VarChar(100) | YES | - | 카테고리 |
| target_yn | Boolean | YES | false | ⚠️ DEPRECATED — 쓰지 말 것. 브랜드 근사라 제네시스 등 누락. 고가차 타겟은 `car_price`(launch_price>=6500) 또는 `car_model_target.is_target` 사용 |
| powertrain_warranty_months | Int | YES | - | 파워트레인 보증 (월) |
| powertrain_warranty_km | Int | YES | - | 파워트레인 보증 (km) |
| general_parts_warranty_months | Int | YES | - | 일반 부품 보증 (월) |
| general_parts_warranty_km | Int | YES | - | 일반 부품 보증 (km) |

**국산/수입 분류 기준 (2026-03 확인):**
- 국산: `현대`, `기아`, `제네시스`, `쉐보레`, `르노`, `KGM(쌍용)`
- 수입: 그 외 전부

```sql
CASE
    WHEN cb.name IN ('현대','기아','제네시스','쉐보레','르노','KGM(쌍용)')
    THEN '국산'
    ELSE '수입'
END AS car_type
```

---

### car_model

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| brand_id | Int | NO | - | FK → car_brand |
| name | VarChar(100) | NO | - | 모델명 |
| tier_id | Int | NO | - | FK → car_tier |
| display_order | Int | YES | - | 표시 순서 |

---

### car_tier

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| tier | Int | NO | - | 티어 등급 |
| extra_charge | Int | NO | - | 추가 요금 |

---

### car_mileage

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| car_id | Int | NO | - | FK → car |
| mileage | Int | NO | - | 주행거리 |
| record_date | Date | YES | - | 기록일 |
| type | VarChar(25) | YES | - | 기록 타입 |
| salesforce_id | VarChar(18) | YES | - | Salesforce ID |

---

### user_address

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| service_region_id | Int | YES | - | FK → service_region |
| name | VarChar(50) | YES | - | 주소 별명 |
| address | Text | YES | - | 도로명 주소 |
| jibun_address | Text | YES | - | 지번 주소 |
| detail_address | Text | YES | - | 상세 주소 |
| sido | VarChar(50) | YES | - | 시/도 |
| sigungu | VarChar(50) | YES | - | 시/군/구 |
| dong | VarChar(50) | YES | - | 동 |
| zonecode | VarChar(10) | YES | - | 우편번호 |
| parking_lot_type | VarChar(50) | YES | - | 주차장 유형 |
| parking_info_content | Text | YES | - | 주차 정보 |
| parking_location_content | Text | YES | - | 주차 위치 |
| entrance_required | Boolean | YES | - | 입차 등록 필요 여부 |
| entrance_when | VarChar(10) | YES | - | 입차 등록 시점 |
| latitude | Decimal(11,8) | YES | - | 위도 |
| longitude | Decimal(11,8) | YES | - | 경도 |
| building_name | VarChar(100) | YES | - | 건물명 |
| apartment_yn | Boolean | YES | false | 아파트 여부 |
| deleted_yn | Boolean | YES | false | 삭제 여부 |

---

### user_social_account

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| provider | VarChar(50) | NO | - | 소셜 제공자 (kakao 등) |
| social_id | VarChar(100) | NO | - | 소셜 ID |
| deleted_yn | Boolean | YES | false | 삭제 여부 |

---

### user_metadata

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| key | VarChar(50) | NO | - | 키 |
| value | Text | YES | - | 값 |

---

### user_attribution

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| source | VarChar(50) | NO | - | 유입 소스 |
| channel | VarChar(100) | YES | - | 채널 |
| campaign | VarChar(255) | YES | - | 캠페인 |
| ad_group | VarChar(255) | YES | - | 광고 그룹 |
| ad_creative | VarChar(255) | YES | - | 광고 소재 |
| term | VarChar(255) | YES | - | 검색어 |
| content | VarChar(255) | YES | - | 콘텐츠 |
| raw_data | Json | YES | - | 원본 데이터 |
| received_at | DateTime | YES | - | 수신일 |

**인덱스:** `user_id`, `channel`, `created_at`, `(source, channel, created_at)`

---

### user_onboarding_survey

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user (unique) |
| referral_source | VarChar(50) | NO | - | 유입 경로 |
| referral_source_detail | VarChar(255) | YES | - | 유입 경로 상세 |
| interest | VarChar(50) | NO | - | 관심사 |

---

### dealer

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(15) | NO | - | 딜러명 |
| phone | VarChar(20) | YES | - | 전화번호 |
| branch | VarChar(255) | YES | - | 지점 |

---

### user_device

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| platform | VarChar(255) | NO | - | ANDROID / IOS |
| device_id | VarChar(255) | NO | - | 디바이스 ID |
| device_unique_id | VarChar(255) | NO | - | 디바이스 고유 ID |
| notification_token | VarChar(255) | YES | - | 푸시 토큰 |
| os_version | VarChar(255) | YES | - | OS 버전 |
| app_version | VarChar(255) | YES | - | 앱 버전 |

---

## Subscription: 구독

### subscription

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| status | VarChar(25) | YES | - | 상태 (실제 값: `ACTIVE`, `CREATED`, `STOPPED`, `ENDED`, NULL. **active 구독 판단 기준: `ACTIVE` 만 해당**) |
| ended_at | DateTime | YES | - | 종료일 |
| user_id | Int | NO | - | FK → user |
| uuid | VarChar(50) | NO | - | UUID (unique) |
| referral_code_id | Int | YES | - | FK → referral_code |
| started_at | DateTime | NO | CURRENT_TIMESTAMP | 구독 시작일 (**코호트 분석의 기준 시점. created_at이 아닌 이 컬럼 사용**) |
| stopped_at | DateTime | YES | - | 중지일 |
| paused_at | DateTime | YES | - | 일시정지일 |
| product_id | Int | YES | - | FK → product |
| represent_car_id | Int | YES | - | FK → car (대표 차량) |
| period | Int | YES | - | 기간 |
| period_unit | VarChar(15) | YES | - | 기간 단위 (month, week) |
| price | Int | YES | - | 가격 |

**인덱스:** `user_id`, `referral_code_id`, `product_id`, `represent_car_id`

---

### subscription_service

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| subscription_id | Int | NO | - | FK → subscription |
| service_id | Int | NO | - | FK → service |
| total_times | Int | NO | - | 총 횟수 |
| left_times | Int | NO | - | 잔여 횟수 |
| expires_at | DateTime | YES | - | 만료일 |

---

## Reservation: 예약

### reservation

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime | NO | CURRENT_TIMESTAMP | 생성일 |
| modified_at | DateTime | NO | CURRENT_TIMESTAMP | 수정일 (on update CURRENT_TIMESTAMP — 어떤 컬럼이든 변경 시 자동 갱신) |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| subscription_id | Int | YES | - | FK → subscription (**주의: 실제 데이터 98% 이상 NULL. 구독 여부 판단에 사용 불가**) |
| reservation_datetime | DateTime | YES | - | 예약 일시 |
| contact | VarChar(15) | YES | - | 연락처 |
| terms | Int | YES | - | 회차 |
| note | Text | YES | - | 관리자 메모 |
| user_note | Text | YES | - | 고객 메모 |
| detailer_note | Text | YES | - | 디테일러 메모 |
| extra_care | Json | YES | - | 추가 케어 |
| location | Text | YES | - | 주소 |
| detailed_location | Text | YES | - | 상세 주소 |
| parking_info_content | Text | YES | - | 주차 정보 |
| requested_at | DateTime | YES | now() | 요청일 |
| status | VarChar(25) | YES | REQUESTED | 상태 (완료: `REPORT_SENT` 또는 `WASHED`. `COMPLETED` 미사용) |
| estimated_time | Int | YES | - | 예상 소요시간 |
| technician | VarChar(100) | YES | - | 기술자 |
| detailer_id | Int | YES | - | FK → detailer |
| washed_at | DateTime | YES | - | 세차 완료일 |
| canceled_at | DateTime | YES | - | 취소일 |
| cancel_reason | Text | YES | - | 취소 사유 |
| reserved_with_date | Boolean | NO | false | 날짜 지정 예약 |
| booked_online_yn | TinyInt | NO | 0 | 온라인 예약 여부 |
| latitude | Decimal(11,8) | YES | - | 위도 |
| longitude | Decimal(11,8) | YES | - | 경도 |
| key_direct_handover_yn | TinyInt | YES | - | 키 직접 전달 |
| salesforce_id | VarChar(18) | YES | - | Salesforce ID |
| uploaded_to_google_sheet | TinyInt | YES | 0 | 구글 시트 업로드 여부 |
| thread_ts | VarChar(20) | YES | - | Slack 스레드 타임스탬프 |
| calendar_id | VarChar(50) | YES | - | 구글 캘린더 ID |
| event_id | VarChar(50) | YES | - | 구글 캘린더 이벤트 ID |
| payout_amount | Int | YES | - | 정산 금액 |
| payout_service_amount | Int | YES | - | 서비스 정산 금액 |
| payout_option_amount | Int | YES | - | 옵션 정산 금액 |
| original_payout_amount | Int | YES | - | 원래 정산 금액 |
| payout_complete_yn | TinyInt | NO | 0 | 정산 완료 |
| penalty_yn | TinyInt | NO | 0 | 패널티 여부 |
| allow_shuffle_yn | TinyInt | NO | 1 | 셔플 허용 |
| happycall_yn | Boolean | YES | false | 해피콜 여부 |
| complaint_yn | Boolean | YES | false | 컴플레인 여부 |
| complaint_reason | Text | YES | - | 컴플레인 사유 |
| overtime_expected_yn | Boolean | YES | false | 시간 초과 예상 |
| overtime_expected_reason | Text | YES | - | 시간 초과 사유 |
| next_reservation_id | Int | YES | - | FK → reservation (다음 예약) |

**Unique:** `(subscription_id, terms)`
**인덱스:** `user_id`, `detailer_id`, `address_id`, `(user_id, status, deleted_yn)`, `(technician, status, deleted_yn, reservation_datetime)`

---

### reservation_car

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| car_id | Int | NO | - | FK → car |
| reservation_id | Int | NO | - | FK → reservation |
| confirmed_yn | TinyInt | NO | 0 | 확정 여부 (**주의: 실제 데이터 대부분 0. 차량 조인 시 이 조건으로 필터하면 결과 없음**) |

**Unique:** `(car_id, reservation_id)`

---

### reservation_status_log

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | NO | - | FK → reservation |
| status | VarChar(25) | NO | - | 상태 |

---

### reservation_metadata

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | NO | - | FK → reservation |
| key | VarChar(255) | NO | - | 키 |
| value | Text | NO | - | 값 |

---

### temporary_reservation

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| car_id | Int | NO | - | FK → car |
| address_id | Int | NO | - | FK → user_address |
| product_id | Int | NO | - | FK → product |
| service_group_id | Int | NO | - | FK → service_group |
| contact | VarChar(255) | NO | - | 연락처 |
| note | Text | YES | - | 메모 |
| datetime | DateTime | YES | - | 예약 일시 |

---

## Payment: 결제

### payment

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| subscription_id | Int | YES | - | FK → subscription |
| amount | Int | NO | - | 결제 금액 |
| original_amount | Int | YES | - | 원래 금액 |
| cancel_amount | Int | YES | 0 | 취소 금액 |
| name | VarChar(250) | YES | - | 결제명 |
| paid_at | DateTime | YES | - | 결제일 |
| status | VarChar(25) | NO | CREATED | 상태 (실제 값: `CREATED`, `PAID`, `PARTIAL_CANCELED`, `CANCELED`. 매출 집계 대상: `PAID`, `PARTIAL_CANCELED`) |
| cancelled_at | DateTime | YES | - | 취소일 |
| cancel_reason | Text | YES | - | 취소 사유 |
| type | VarChar(25) | YES | - | 결제 타입 |
| portone_id | VarChar(25) | YES | - | 포트원 ID |
| cart_id | Int | YES | - | FK → cart |
| product_id | Int | YES | - | FK → product |
| reservation_id | Int | YES | - | FK → reservation |
| uuid | VarChar(50) | YES | - | UUID (unique) |
| user_id | Int | YES | - | FK → user |
| buyer_type | VarChar(15) | NO | USER | 구매자 타입 |
| buyer_id | Int | YES | - | 구매자 ID |
| quantity | Int | YES | 1 | 수량 |
| metadata | Json | YES | - | 메타데이터 |
| promotion_application_id | Int | YES | - | FK → promotion_application |
| deleted_yn | Boolean | YES | false | 삭제 여부 |

---

### card_payment

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| imp_uid | VarChar(20) | YES | - | 아임포트 UID |
| imp_customer_uid | VarChar(50) | YES | - | 고객 UID |
| subscription_id | Int | YES | - | FK → subscription |
| card_id | Int | YES | - | FK → payment_method |
| amount | Int | YES | - | 금액 |
| cancel_amount | Int | YES | 0 | 취소 금액 |
| status | VarChar(45) | YES | - | 상태 |
| paid_at | DateTime | YES | - | 결제일 |
| cancelled_at | DateTime | YES | - | 취소일 |
| fail_reason | VarChar(245) | YES | - | 실패 사유 |
| cancel_reason | VarChar(245) | YES | - | 취소 사유 |
| payment_id | Int | NO | - | FK → payment |

---

### payment_method

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| billing_key | VarChar(50) | YES | - | 빌링키 |
| type | VarChar(10) | YES | card | 결제 수단 타입 |
| card_number | VarChar(200) | YES | - | 카드 번호 |
| card_brand | VarChar(20) | YES | - | 카드 브랜드 |
| card_issuer | VarChar(50) | YES | - | 발급사 |
| user_id | Int | YES | - | FK → user |
| subscription_id | Int | YES | - | FK → subscription |

---

### payment_medium

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| medium | VarChar(100) | NO | - | 결제 수단 (CASH, POINT) |
| amount | Int | NO | - | 금액 |
| payment_id | Int | NO | - | FK → payment |

---

## Service & Option: 서비스/옵션

### product

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(50) | NO | - | 상품명 |
| type | VarChar(15) | YES | - | 타입 |
| price | Int | NO | - | 가격 |
| original_price | Int | YES | - | 원래 가격 |
| period | Int | YES | - | 기간 |
| period_unit | VarChar(15) | YES | - | 기간 단위 (month, week) |
| uuid | VarChar(50) | YES | - | UUID (unique) |
| max_purchase_amount | Int | YES | - | 최대 구매 수량 |
| abbr_name | VarChar(50) | YES | - | 약어 |
| unit_price | Int | YES | - | 단가 |
| description | Text | YES | - | 설명 |
| category | VarChar(15) | YES | - | 카테고리 |
| product_type | VarChar(50) | YES | - | 상품 유형 |
| refundable_yn | Boolean | NO | true | 환불 가능 여부 |
| entity_id | Int | YES | - | 엔티티 ID |

**알려진 product 조건 예시 (2026-03 확인):**
- 1회권 외부+내부: `type = 'VOUCHER'`, `category = 'CAR_WASH'`, `name = '외부 + 내부'`

---

### 구독 상품 분류 기준 *(2026-03 확인)*

분석 시 아래 분류를 사용한다. 소량/커스텀 상품은 분석 대상에서 제외.

| 카테고리 | product_id 목록 | 총 구독수 (2026-03 기준) | period_unit |
|---------|----------------|----------------------|-------------|
| **월1회 외부+내부** | 178, 182, 186, 174, 190, 170, 87, 82, 194 | ~2,676 | month |
| **월2회 외부만** | 3555, 3556, 3557, 3558, 3559, 3560, 3561 | ~1,262 | week (period=4) |
| **월4회 외부만** | 3563, 3564, 3565, 3566, 3567, 3568 | ~200 | week (period=4) |
| 두달1회 | 212, 211, 213, 214, 215, 210 | ~235 | month (period=2) |
| 월2회 외부+내부 | 2956, 2957, 2958, 2959, 2960, 83, 179 | ~145 | month |

**SQL에서 사용:**
```sql
CASE
    WHEN p.id IN (178,182,186,174,190,170,87,82,194) THEN '월1회_외부내부'
    WHEN p.id IN (3555,3556,3557,3558,3559,3560,3561) THEN '월2회_외부만'
    WHEN p.id IN (3563,3564,3565,3566,3567,3568)       THEN '월4회_외부만'
END AS sub_type
```

**⚠️ 주의사항:**
- 월2회/월4회 외부만은 `period_unit = 'week'`, `period = 4`로 설정되어 있음 (4주 = 약 1개월)
- 같은 카테고리 내에서도 가격이 다른 이유: 차량 티어별 가격 차이
- `[카라멜]` 접두어가 붙은 상품은 개인/특수 링크 — 분석 시 주의 또는 제외

---

### service

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| service_group_id | Int | YES | - | FK → service_group |
| name | VarChar(25) | NO | - | 서비스명 |
| description | Text | YES | - | 설명 |
| description_for_detailer | VarChar(50) | YES | - | 디테일러용 설명 |
| type | VarChar(25) | YES | - | 타입 |
| steam_yn | TinyInt | NO | 0 | 스팀 여부 |
| deleted_yn | Boolean | NO | false | 삭제 여부 |
| tier_id | Int | YES | - | FK → car_tier |
| price | Int | NO | - | 가격 |
| metadata | Json | YES | - | 메타데이터 |
| wash_type | VarChar(15) | YES | - | 세차 타입 |
| subscription_type_id | Int | YES | - | FK → subscription_type |
| time_required | Int | YES | - | 소요 시간 (분) |

---

### service_group

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| type | VarChar(50) | NO | - | 타입 |
| name | VarChar(50) | YES | - | 그룹명 |
| description | VarChar(150) | YES | - | 설명 |
| steam_yn | Boolean | NO | false | 스팀 여부 |
| option_yn | Boolean | YES | true | 옵션 가능 여부 |

---

### option (실제 테이블: `options`)

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(25) | NO | - | 옵션명 |
| price | Int | NO | - | 가격 |
| type | VarChar(50) | YES | - | 타입 |
| description | VarChar(200) | YES | - | 설명 |
| image_url | VarChar(250) | YES | - | 이미지 URL |
| extra_time | Int | YES | - | 추가 소요시간 |
| metadata | Json | YES | - | 메타데이터 |
| order | Int | NO | 0 | 정렬 순서 |
| deleted_yn | Boolean | YES | false | 삭제 여부 |

---

### user_service

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | YES | - | FK → reservation |
| user_id | Int | NO | - | FK → user |
| service_id | Int | NO | - | FK → service |
| subscription_id | Int | YES | - | FK → subscription |
| payment_id | Int | YES | - | FK → payment |
| product_id | Int | YES | - | FK → product |
| promotion_application_id | Int | YES | - | FK → promotion_application |
| coupon_code_reward_id | Int | YES | - | FK → coupon_code_reward |
| partner_activity_log_id | Int | YES | - | FK → partner_activity_log |
| started_at | DateTime | YES | now() | 시작일 |
| ended_at | DateTime | YES | - | 종료일 |
| used_yn | TinyInt | NO | 0 | 사용 여부 |
| paid_yn | TinyInt | NO | 0 | 결제 여부 |
| deleted_yn | Boolean | NO | false | 삭제 여부 |
| postpaid_yn | Boolean | NO | false | 후불 여부 |
| applicable_car_id | Int | YES | - | 사용 가능 차량 제한. NULL=모든 차량, 값=그 차량 전용. `user_service_applicability` 행이 있으면 그쪽 `config`가 우선(prisma @deprecated) |
| deleted_at | DateTime | YES | - | 삭제 시각 |
| delete_reason | Text | YES | - | 삭제 사유 (실값은 QUERY_REFERENCE user_service 표) |
| paid_amount | Int | YES | - | 결제 금액 |
| coupon_campaign_reward_id | Int | YES | - | FK → coupon_campaign_reward |

**인덱스:** `(user_id, deleted_yn, paid_yn, used_yn, ended_at)`, `(user_id, deleted_yn, paid_yn, used_yn, postpaid_yn, ended_at)`, `(user_id, applicable_car_id, deleted_yn, paid_yn, used_yn, ended_at)`

---

### user_service_applicability

세차권 1장의 사용 가능 차량 조건 스냅샷. 예약 생성 시 서버가 이 `config`(없으면 `user_service.applicable_car_id`, 둘 다 없으면 모든 차량)로 차량 적합성을 판정한다.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime | NO | now() | 생성일 |
| modified_at | DateTime | NO | now() | 수정일 |
| user_service_id | Int | NO | - | FK → user_service (unique, 1:1) |
| definition_id | Int | YES | - | FK → entitlement_applicability_rule_definition |
| source_type | VarChar(50) | YES | - | PRODUCT_SERVICE / COUPON_CAMPAIGN_REWARD / COUPON_CODE_REWARD |
| source_id | Int | YES | - | source_type 별 원본 id |
| config | Json | NO | - | 필터 목록 `{filters:[{key,matchType,value}]}` (예: CAR_ID EQUALS, 차량 티어). 빈 목록=모든 차량 |

**인덱스:** `definition_id`, `(source_type, source_id)`

---

### user_option

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | YES | - | FK → reservation |
| user_id | Int | NO | - | FK → user |
| option_id | Int | NO | - | FK → option |
| payment_id | Int | YES | - | FK → payment |
| product_id | Int | YES | - | FK → product |
| promotion_application_id | Int | YES | - | FK → promotion_application |
| started_at | DateTime | YES | now() | 시작일 |
| ended_at | DateTime | YES | - | 종료일 |
| used_yn | TinyInt | NO | 0 | 사용 여부 |
| paid_yn | TinyInt | NO | 0 | 결제 여부 |
| deleted_yn | TinyInt | NO | 0 | 삭제 여부 |
| coupon_code_reward_id | Int | YES | - | FK → coupon_code_reward |
| partner_activity_log_id | Int | YES | - | FK → partner_activity_log |

**인덱스:** `(user_id, used_yn, paid_yn, deleted_yn)`

---

### product_service

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| product_id | Int | NO | - | FK → product |
| service_id | Int | NO | - | FK → service |
| starts_after | Int | NO | - | 시작 기준 (일) |
| ends_after | Int | YES | - | 종료 기준 (일) |
| type | VarChar(25) | NO | - | 타입 |

---

### product_option

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| product_id | Int | NO | - | FK → product |
| option_id | Int | NO | - | FK → option |
| starts_after | Int | NO | - | 시작 기준 (일) |
| ends_after | Int | YES | - | 종료 기준 (일) |
| type | VarChar(25) | NO | - | 타입 |

---

### car_tier_product

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| car_tier_id | Int | NO | - | FK → car_tier |
| product_id | Int | NO | - | FK → product |
| type | VarChar(100) | YES | - | 타입 |
| deleted_yn | Boolean | YES | false | 삭제 여부 |

---

### available_day

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| table_name | VarChar(100) | NO | - | 대상 테이블 (service, option, promotion) |
| record_id | Int | NO | - | 대상 레코드 ID |
| from | Date | NO | - | 시작일 |
| to | Date | NO | - | 종료일 |

**Unique:** `(table_name, record_id, from, to)`

---

## Report & Checkup: 리포트/점검

### report

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| partner_id | Int | YES | - | FK → partner |
| wash_result_id | Int | YES | - | FK → wash_result |
| car_id | Int | YES | - | FK → car |
| reservation_id | Int | YES | - | FK → reservation |
| uuid | VarChar(50) | YES | - | UUID (unique) |
| version | Int | YES | - | 버전 |
| reported_at | DateTime | NO | now() | 리포트 작성일 |
| sent_at | DateTime | YES | - | 발송일 |
| opened_yn | TinyInt | YES | 0 | 열람 여부 |
| solution | VarChar(500) | YES | - | 솔루션 |

---

### report_card

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| report_id | Int | NO | - | FK → report |
| type | VarChar(45) | NO | - | 카드 타입 |
| data | Json | YES | - | 카드 데이터 |

---

### wash_result

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | NO | - | FK → reservation |
| status | VarChar(100) | NO | CHECKUP_DASHBOARD | 현재 상태 |
| max_status | VarChar(100) | NO | CHECKUP_DASHBOARD | 최대 도달 상태 |
| crm_type | VarChar(30) | YES | - | CRM 타입 |
| mileage | Int | YES | - | 주행거리 |
| tire_treads | Json | YES | - | 타이어 트레드 |
| tire_sizes | Json | YES | - | 타이어 사이즈 |
| washer_fluid_yn | Boolean | YES | false | 워셔액 여부 |
| tire_pressure_yn | Boolean | YES | false | 타이어 공기압 여부 |
| finished_at | DateTime | YES | - | 세차 완료 시간 |

**view_log 조인 시 사용:** `view_log.record_id = wash_result.id` (where `table_name = 'wash_result'`)

---

### checkup

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| mileage | Int | YES | - | 주행거리 |
| checkup_datetime | DateTime | NO | - | 점검 일시 |
| note | Text | YES | - | 메모 |
| opened_yn | TinyInt | NO | 0 | 열람 여부 |
| car_id | Int | NO | - | FK → car |
| detailer_id | Int | NO | - | FK → detailer |
| reservation_id | Int | YES | - | FK → reservation (unique) |
| sent_at | DateTime | YES | - | 발송일 |

---

### checkup_detail

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| checkup_id | Int | NO | - | FK → checkup |
| category | VarChar(255) | NO | - | 카테고리 (TIRE_TREAD_*, DASHBOARD_*) |
| sub_category | VarChar(255) | YES | - | 서브 카테고리 |
| main_area | VarChar(255) | YES | - | 주요 영역 (전면부, 후면부 등) |
| sub_area | VarChar(255) | YES | - | 세부 영역 (보닛, 범퍼 등) |
| note | Text | YES | - | 메모 |
| value | Float | YES | - | 수치 |
| car_tire_id | Int | YES | - | FK → car_tire |

---

## Review: 리뷰

### review

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| reservation_id | Int | NO | - | FK → reservation (unique) |
| score | Int | NO | - | 평점 |
| text | Text | NO | - | 리뷰 내용 |
| promotion_application_id | Int | YES | - | FK → promotion_application |
| nps_score | Int | YES | - | NPS 점수 |
| nps_text | Text | YES | - | NPS 텍스트 |
| public_yn | TinyInt | NO | 1 | 공개 여부 (고객 선택) |
| hide_yn | TinyInt | NO | 0 | 숨김 여부 (관리자) |
| priority | Int | NO | 0 | 우선순위 |

**⚠️ 실제 데이터 주의사항 (2026-03 확인):**
- `promotion_application_id` 컬럼은 스키마에 존재하나 **실제 데이터는 NULL** → 리뷰-쿠폰 연결에 사용 불가
- 리뷰 작성 시 최소 40자 텍스트 조건 존재 → 별점만 찍고 텍스트 미달로 미제출되는 케이스 발생 (리뷰율 측정 시 유의)
- 리뷰-쿠폰 연결은 `promotion_application.note` 의 Review ID로만 추적 가능 → 아래 promotion_application 섹션 참고
- `hide_yn = 0` 필터 필수

---

## CRM: 고객 관리

### partner

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(50) | NO | - | 파트너명 |
| username | VarChar(50) | YES | - | 로그인 ID |
| user_id | Int | YES | - | FK → user |

---

### crm_consultation

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| partner_id | Int | NO | - | FK → partner |
| user_id | Int | NO | - | FK → user |
| car_id | Int | YES | - | FK → car |
| memo | Text | YES | - | 상담 메모 |

---

### crm_issue

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| title | String | NO | - | 이슈 제목 |
| memo | Text | YES | - | 메모 |
| status | String | NO | - | 상태 (NEW, WON, PRICE_ESTIMATION_NEEDED, DEFERRED, LOST, UNNECESSARY, EXTERNALLY_RESOLVED) |
| car_id | Int | NO | - | FK → car |
| car_tire_id | Int | YES | - | FK → car_tire |
| source_type | String | NO | - | 소스 (CARWASH_REPORT, CHANNELTALK, CALL, REPAIR_SHOP, CHECKUP) |
| source_record_id | Int | YES | - | 소스 레코드 ID |
| main_category | String | NO | - | 대분류 (소모품 교체, 복원, 점검, 튜닝, 정비, 기타) |
| sub_category | String | NO | - | 소분류 |
| suggested_price | Int | YES | - | 제안 가격 |
| cost | Int | YES | - | 비용 |
| severity | VarChar(255) | YES | - | 심각도 |
| consultation_id | Int | YES | - | FK → crm_consultation |
| partner_id | Int | YES | - | FK → partner |

---

### crm_repair_order

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| partner_id | Int | NO | - | FK → partner |
| repair_shop_id | Int | YES | - | FK → repair_shop |
| user_id | Int | NO | - | FK → user |
| car_id | Int | YES | - | FK → car |
| status | VarChar(128) | NO | - | 상태 (NOT_STARTED, IN_PROGRESS, COMPLETED, PAID, CANCELLED) |
| address_id | Int | YES | - | FK → user_address |
| pickup_datetime | DateTime | YES | - | 픽업 일시 |
| suggested_price | Int | YES | - | **매출액** (고객 청구 = 안내가격) |
| cost | Int | YES | - | **정산금액 = 원가** (수리업체 지급액) |
| consultation_id | Int | NO | - | FK → crm_consultation |
| delivery_fee | Int | YES | - | 탁송비 |

> 정비마진 = `suggested_price - cost - delivery_fee` (마이너스 정상 존재).
> **정산완료일 컬럼 없음** → `crm_activity_log`의 `REPAIR_ORDER_STATUS_PAID` 시각 사용 (아래 참조, `modified_at` 금지).
> 디테일러 영업 건은 `crm_repair_order_issue` → `crm_issue.source_type='DETAILER'` → `source_record_id`=`detailer.id`. 자세한 쿼리: `QUERY_REFERENCE.md` 정비정산 섹션.

---

### crm_activity_log

CRM(영업/정비) 객체의 생성·수정·상태전환 감사 로그. caramel_sales_admin 고객상세 화면의 **활동 피드**. 2025-02-13~. **상태 전환 시각의 정본** — 객체 테이블엔 전환일 컬럼이 없으므로 "언제 X 상태가 됐나"는 여기 `created_at`으로 답한다.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime | NO | now | **활동 발생 시각 = 상태 전환 시각** |
| partner_id | Int | YES | - | 운영자(담당자). FK → partner. ※디테일러 아님 |
| user_id | Int | NO | - | 고객. FK → user |
| car_id | Int | YES | - | FK → car |
| memo | Text | YES | - | 메모 |
| activity_type | VarChar(100) | NO | - | 활동 종류 (아래) |
| activity_record_id | Int | NO | - | 대상 레코드 PK (activity_type이 가리키는 테이블의 id) |
| source_type | VarChar(100) | YES | - | 활동 출처 (예: 이슈가 DETAILER/CHECKUP/CUSTOMER_INQUIRY 등에서 발생) |
| source_record_id | Int | YES | - | 출처 레코드 PK (source_type='DETAILER'이면 `detailer.id`) |

`activity_type` 주요 값:
- `{OBJECT}_CREATED / _UPDATED / _DELETED` — OBJECT ∈ ISSUE, CONSULTATION, NOTE, TODO, REPAIR_ORDER
- 수리 상태전환: `REPAIR_ORDER_STATUS_{PAID|COMPLETED|IN_PROGRESS|CANCELLED|NOT_STARTED}`
- 이슈 상태전환: `ISSUE_STATUS_{WON|LOST|DEFERRED|EXTERNALLY_RESOLVED|REPAIR_COMPLETED|...}`

> **커버리지 ~99%**: 화면 플로우 안 거치고 직접 입력된 백엔트리는 로그가 없다 → 상태전환일 쿼리는 `LEFT JOIN` + `COALESCE(MIN(log.created_at), 객체.modified_at)` 폴백 권장. (`QUERY_REFERENCE.md` 정비정산 섹션 참조)

---

### crm_note

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| partner_id | Int | NO | - | FK → partner |
| user_id | Int | NO | - | FK → user |
| car_id | Int | YES | - | FK → car |
| memo | Text | YES | - | 메모 |
| consultation_id | Int | YES | - | FK → crm_consultation |
| reservation_id | Int | YES | - | FK → reservation |

---

### crm_todo

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| partner_id | Int | NO | - | FK → partner |
| user_id | Int | NO | - | FK → user |
| car_id | Int | YES | - | FK → car |
| memo | String | NO | - | 할일 내용 |
| due_at | DateTime | YES | - | 마감일 |
| done_at | DateTime | YES | - | 완료일 |
| consultation_id | Int | YES | - | FK → crm_consultation |

---

### crm_campaign

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 캠페인명 |
| description | Text | YES | - | 설명 |
| activated_yn | Boolean | NO | true | 활성화 여부 |

---

## Detailer: 디테일러

### detailer

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | YES | - | FK → user |
| name | VarChar(100) | YES | - | 이름 |
| phone | VarChar(11) | YES | - | 전화번호 |
| plate_number | VarChar(20) | YES | - | 차량번호 |
| booking_yn | TinyInt | NO | 0 | 예약 가능 |
| steam_yn | TinyInt | NO | 0 | 스팀 가능 |
| admin_yn | TinyInt | NO | 0 | 관리자 |
| tier | Int | NO | - | 등급 |
| service_area | Json | YES | - | 서비스 지역 |
| calendar_id | VarChar(100) | YES | - | 캘린더 ID |
| slack_member_id | VarChar(100) | YES | - | 슬랙 멤버 ID |
| work_type | String | YES | - | 근무 유형 |
| direct_yn | TinyInt | NO | 0 | 직접 예약 |
| retired_yn | TinyInt | NO | 0 | 퇴직 여부 |
| option_commission_rate | Float | NO | 0.3 | 옵션 수수료율 |
| service_commission_rate | Int | NO | 0 | 서비스 수수료 |

---

### detailer_work_schedule

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| detailer_id | Int | NO | - | FK → detailer |
| slot_id | Int | YES | - | FK → detailer_slot |
| effective_from | DateTime | NO | - | 적용 시작일 |
| effective_to | DateTime | NO | - | 적용 종료일 |
| description | Text | YES | - | 설명 |
| type | String | YES | DEFAULT | 타입 |

---

### detailer_work_schedule_rule

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| schedule_id | Int | NO | - | FK → detailer_work_schedule |
| day_of_week | VarChar(10) | NO | - | 요일 (MON~SUN) |
| start_time | DateTime | NO | - | 시작 시간 |
| end_time | DateTime | NO | - | 종료 시간 |
| service_region_group_id | Int | YES | - | FK → service_region_group |
| deleted_at | DateTime | YES | - | 삭제일 (soft delete) |
| zone_id | Int | YES | - | FK → zone (**zone 기반 디테일러 배정에 사용. 2026-03 추가**) |

**인덱스:** `schedule_id`, `service_region_group_id`, `zone_id`

---

### detailer_holiday

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| detailer_id | Int | NO | - | FK → detailer |
| from | DateTime | YES | - | 휴일 시작 |
| to | DateTime | YES | - | 휴일 종료 |
| memo | Text | YES | - | 메모 |

---

### detailer_incentive

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| detailer_id | Int | NO | - | FK → detailer |
| type | VarChar(255) | NO | - | 타입 |
| sub_type | VarChar(255) | NO | - | 서브 타입 |
| status | VarChar(255) | NO | - | 상태 |
| amount | Int | NO | - | 금액 |
| related_user_id | Int | YES | - | FK → user |
| related_reservation_id | Int | YES | - | FK → reservation |
| related_issue_id | Int | YES | - | FK → crm_issue |
| related_repair_order_id | Int | YES | - | FK → crm_repair_order |
| paid_yn | Boolean | NO | false | 지급 여부 |
| tax_yn | Boolean | NO | true | 과세 여부 |
| title | VarChar(255) | YES | - | 제목 |

---

### detailer_routes

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| distance | Float | NO | - | 거리 |
| estimated_time | Float | NO | - | 예상 소요시간 |
| detailer_id | Int | NO | - | FK → detailer |
| from_reservation_id | Int | YES | - | FK → reservation (출발) |
| to_reservation_id | Int | YES | - | FK → reservation (도착) |
| detailer_address_id | Int | YES | - | FK → detailer_address |
| date | Date | YES | - | 날짜 |

---

### detailer_supply_sheet (외부 연동 테이블)

> 구글 스프레드시트를 Google Apps Script로 주기적으로 긁어오는 테이블. Prisma 스키마 외부에서 관리됨.

| 컬럼 | 타입 | nullable | 설명 |
|------|------|----------|------|
| id | bigint unsigned | NO | PK (autoincrement) |
| sheet_row_no | int | YES | 시트 행 번호 |
| batch | varchar(50) | YES | 기수 (예: 1기, 2기) |
| name | varchar(100) | YES | 이름 (**사실상 고유 식별자**) |
| status | varchar(20) | YES | 재직 상태 (`현직` / `퇴사` / `교육중` / `파견`) |
| birth_date | date | YES | 생년월일 |
| hire_date | date | YES | 입사일 |
| training_start_date | date | YES | 교육 시작일 |
| work_start_date | date | YES | 실투입 시작일 |
| retired_date | date | YES | 퇴직일 |
| phone_raw | varchar(50) | YES | 원본 전화번호 |
| phone_norm | varchar(30) | NO | 정규화된 전화번호 (unique, **detailer 테이블 매핑 키**) |
| emergency_contact | varchar(100) | YES | 비상연락처 |
| car_plate | varchar(30) | YES | 차량번호 |
| home_address | text | YES | 자택 주소 |
| wash_level | text | YES | 세차 레벨 |
| repair_level | text | YES | 수리 레벨 |
| loaded_at | datetime | NO | 적재일시 |
| updated_at | datetime | NO | 수정일시 |

#### 주요 특성 및 주의사항

- `detailer_id` 컬럼 없음 → `detailer` 테이블과의 매핑 키는 `phone_norm ↔ detailer.phone`
- **JOIN 시 collation 불일치 주의** (utf8mb4_general_ci vs utf8mb4_0900_ai_ci) → 반드시 `COLLATE` 명시

```sql
LEFT JOIN detailer d ON d.phone = s.phone_norm COLLATE utf8mb4_general_ci
```

- supply sheet ↔ detailer 매핑 **100% 작동** 확인 (2026-03-05 기준)
- `detailer` 테이블에만 있는 현직자 **0건** → CBR `active_cnt` 모수 정확

#### CBR capable 판단 조건

| status | 조건 | capable 여부 | 비고 |
|--------|------|-------------|------|
| 퇴사 | - | ❌ 제외 | |
| 현직 | `retired_date` 도래 | ❌ 제외 | |
| 현직 | 오늘 `off_factor >= 1` | ❌ 제외 (연차) | |
| 현직 | 오늘 `off_factor > 0` | ⚠️ capable (반차) | |
| 현직 | 그 외 | ✅ capable | |
| 파견 | - | ✅ **전체 CBR capable에는 포함** | zone별 가용 인원에서는 **제외** |
| 교육중 | `work_start_date > 오늘` | ❌ 제외 | |
| 교육중 | `work_start_date <= 오늘` | ✅ capable | |

#### 파견 디테일러 처리 *(2026-03-11 추가)*

> 파견 디테일러는 다른 zone/업무에 투입 중이지만 세차 자체는 수행함.

- **전체 CBR 캐파**: `status IN ('현직', '파견')` — 파견 포함
- **zone별 가용 인원**: `status = '현직'`만 — 파견 제외
- `detailer_holiday`에 파견 기간이 holiday로 등록되어 있을 수 있으나, 메모에 `연차`/`반차`가 아닌 경우 `v_detailer_holiday_daily`의 off_factor 계산에 반영되지 않음 (시간 겹침 비율로만 계산)
- `v_detailer_holiday_daily` 뷰는 파견 holiday를 **필터하지 않음** — 전체 CBR off_sum에 반영되어야 하므로

#### Apps Script 연동 주의사항 *(2026-03-11 추가)*

- `ALLOWED_STATUS`에 `'파견'` 포함 필요: `new Set(['현직', '교육중', '퇴사', '파견'])`
- `writeWeeklySnapshot`의 `active_cnt` / `active_status_cnt` 조건에 `status IN ('현직', '파견')` 반영 필요

---

## Promotion & Coupon: 프로모션/쿠폰

### promotion

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(50) | NO | - | 프로모션명 |
| description | VarChar(250) | YES | - | 설명 |
| type | VarChar(25) | NO | - | 타입 |
| metadata | Json | YES | - | 메타데이터 |
| expired_at | DateTime | YES | 2999-12-31 | 만료일 |
| key | VarChar(250) | YES | - | 키 |

**알려진 promotion_id (2026-03 확인):**
- `122` = 프리미엄 왁스코팅 무료 이용권 (리뷰 보상용, `key = 'FREE_WAX/REVIEW'`, `type = 'COUPON'`)

---

### promotion_application

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| promotion_id | Int | NO | - | FK → promotion |
| promotion_application_id | Int | YES | - | FK → self (부모) |
| record_id | Int | NO | - | 대상 레코드 ID |
| table_name | VarChar(100) | NO | - | 대상 테이블 |
| used_yn | Boolean | NO | false | 사용 여부 |
| payment_id | Int | YES | - | FK → payment |
| expired_at | DateTime | YES | 2999-12-31 | 만료일 |
| used_at | DateTime | YES | - | 사용일 |
| coupon_code_reward_id | Int | YES | - | FK → coupon_code_reward |
| car_id | Int | YES | - | FK → car |
| note | Text | YES | - | 발급 메모 (**리뷰 보상 시 Review ID 기록됨**) |

**⚠️ 실제 데이터 주의사항 (2026-03 확인):**
- 리뷰 작성 시 자동 발급되는 쿠폰은 `table_name = 'app_user'`, `record_id = user.id` 로 유저에 귀속
- `note` 컬럼에 발급 맥락 기록. 리뷰 보상의 경우: `"First wash all-clean reward - Review ID {review.id}"` 형식
- 특정 리뷰로 발급된 쿠폰을 특정할 때: `note LIKE CONCAT('%Review ID ', rv.id)` 조건 사용
- `used_yn = 1` + `used_at` 으로 사용 여부/시점 확인 가능
- **⚠️ 시간 조건 없이 user_id로만 조인하면 해당 유저의 과거 쿠폰 전체가 잡힘** → 반드시 `note` 조건으로 특정 리뷰와 1:1 매핑할 것

---

### promotion_group

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| key | VarChar(50) | YES | - | 키 |
| name | VarChar(25) | NO | - | 그룹명 |
| max_usage_count | Int | NO | -1 | 최대 사용 횟수 (-1 = 무제한) |

---

### coupon_code

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| code | VarChar(255) | NO | - | 쿠폰 코드 (unique) |
| name | VarChar(255) | NO | - | 쿠폰명 |
| description | Text | YES | - | 설명 |
| max_usage_count | Int | NO | 1 | 최대 사용 횟수 |
| total_supply_count | Int | NO | 1 | 총 발행 수량 |
| expired_at | DateTime | NO | - | 만료일 |
| campaign_id | Int | YES | - | FK → coupon_campaign |

---

### coupon_code_reward

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| coupon_code_id | Int | NO | - | FK → coupon_code |
| reward_type | VarChar(255) | NO | - | 보상 유형 (POINT, SERVICE, OPTION, PROMOTION_APPLICATION) |
| reward_id | Int | NO | - | 보상 ID |
| expires_type | VarChar(255) | YES | - | 만료 유형 (DATE, AFTER_SECONDS) |
| expires_value | Int | YES | - | 만료 값 |

---

## Point: 포인트

### user_point

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| partner_activity_log_id | Int | YES | - | FK → partner_activity_log |
| point | Int | NO | 0 | 적립 포인트 |
| left_point | Int | NO | 0 | 잔여 포인트 |
| expired_at | DateTime | YES | - | 만료일 |
| coupon_code_reward_id | Int | YES | - | FK → coupon_code_reward |

---

### user_point_history

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| payment_id | Int | YES | - | FK → payment |
| amount | Int | NO | 0 | 변동 금액 |
| before_point | Int | NO | 0 | 변동 전 포인트 |
| after_point | Int | NO | 0 | 변동 후 포인트 |
| reason | Text | YES | - | 사유 |
| internal_reason | Text | YES | - | 내부 사유 |
| refunded_yn | Boolean | YES | false | 환불 여부 |

---

## Cart: 장바구니

### cart

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| reservation_id | Int | YES | - | FK → reservation |
| uuid | VarChar(100) | NO | - | UUID (unique) |
| anyone_purchasable_yn | Boolean | YES | false | 누구나 구매 가능 |

---

### cart_item

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| cart_id | Int | NO | - | FK → cart |
| type | VarChar(50) | NO | - | 아이템 타입 |
| description | VarChar(50) | YES | - | 설명 |
| record_id | Int | YES | - | 레코드 ID |
| user_service_id | Int | YES | - | FK → user_service |
| reservation_car_id | Int | YES | - | FK → reservation_car |
| to_car_id | Int | YES | - | FK → car (대상 차량) |
| service_group_id | Int | YES | - | FK → service_group |
| service_type | VarChar(15) | YES | - | 서비스 타입 |
| price | Int | YES | - | 가격 |
| quantity | Int | NO | 1 | 수량 |

---

## Message & Notification: 메시지

### message

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| customer_id | Int | YES | - | FK → user |
| scheduled_at | DateTime | YES | - | 예약 발송 시간 |
| sent_yn | Int | YES | 0 | 발송 여부 |
| message | Json | YES | - | 메시지 내용 |
| lms_type | VarChar(45) | YES | KAKAO | 메시지 타입 |
| type | VarChar(200) | YES | - | 발송 유형 |
| status | VarChar(50) | YES | REQUESTED | 상태 |
| reservation_id | Int | YES | - | 예약 ID |
| detailer_id | Int | YES | - | 디테일러 ID |
| job_execution_id | Int | YES | - | FK → job_execution |
| tracking_key | VarChar(100) | YES | - | 추적 키 (unique) |
| user_device_id | Int | YES | - | FK → user_device |

---

### message_template

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| key | VarChar(50) | NO | - | 템플릿 키 |
| channel | VarChar(50) | NO | - | 채널 |
| payload | Json | YES | - | 페이로드 |
| activated_yn | Boolean | YES | true | 활성화 여부 |

**Unique:** `(key, channel)`

---

### message_definition

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| message_key | VarChar(100) | NO | - | 메시지 키 (unique) |
| channel | VarChar(50) | NO | - | 채널 |
| trigger_type | VarChar(20) | NO | - | 트리거 타입 |
| trigger_source | VarChar(200) | YES | - | 트리거 소스 |
| audience_description | Text | YES | - | 수신 대상 설명 |
| template_ref | VarChar(100) | YES | - | 템플릿 참조 |
| code_location | VarChar(300) | YES | - | 코드 위치 |
| description | Text | YES | - | 설명 |
| owner | VarChar(50) | YES | - | 담당자 |
| is_active | Boolean | NO | true | 활성화 여부 |

---

## Experiment & Link: 실험/링크

### experiment

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| identifier | VarChar(100) | NO | - | 식별자 (unique) |
| title | Text | NO | - | 제목 |
| description | Text | YES | - | 설명 |
| group | VarChar(50) | YES | - | 그룹 |
| metadata | Json | YES | - | 메타데이터 |

---

### link

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| key | VarChar(25) | NO | - | 키 (unique) |
| alias | VarChar(100) | YES | - | 별칭 (unique) |
| target_url | VarChar(250) | YES | - | 대상 URL |
| auth_required | Boolean | NO | false | 인증 필요 여부 |
| og_data | Json | YES | - | OG 데이터 |
| experiment_id | Int | YES | - | FK → experiment |

---

### user_event

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| car_id | Int | YES | - | FK → car |
| name | VarChar(150) | NO | - | 이벤트명 |
| properties | Json | YES | - | 속성 |
| metadata | Json | YES | - | 메타데이터 |

**인덱스:** `(user_id, name, car_id)`

---

## Referral: 친구 초대 *(신규 - 2026-03)*

### user_referral

> 유저별 레퍼럴 코드 발급 테이블. **레퍼럴 페이지 진입 시 코드가 자동 생성**되므로, 코드 발급 수 ≠ 실제 공유 의도.

| 컬럼 | 타입 | nullable | 설명 |
|------|------|----------|------|
| id | Int | NO | PK |
| user_id | Int | NO | FK → user (코드 발급 대상) |
| referral_code | VarChar | NO | 레퍼럴 코드 |
| message | Text | YES | 메시지 |
| version | Int | NO | 프로그램 버전 (1, 2, 3) |
| deleted_yn | TinyInt | NO | 삭제 여부 |
| deleted_at | DateTime | YES | 삭제일 |
| updated_at | DateTime | YES | 수정일 |
| created_at | DateTime | NO | 생성일 |

#### version 현황 (2026-03 확인)

| version | 건수 | 비고 |
|---------|------|------|
| 1 | 39 | 구버전 |
| 2 | 796 | 구버전 (마지막 가입일이 최근이 아님) |
| 3 | 567+ | **현재 운영 버전** (2026-02-26 시작) |

---

### user_referral_log

> 레퍼럴 코드를 통해 실제 가입한 기록. **실질적인 초대 성과 지표는 이 테이블 기준.**

| 컬럼 | 타입 | nullable | 설명 |
|------|------|----------|------|
| id | Int | NO | PK |
| user_id | Int | NO | FK → user (초대받아 가입한 사람, invitee) |
| recommender_user_id | Int | NO | FK → user (초대한 사람, inviter) |
| user_referral_id | Int | NO | FK → user_referral (사용된 코드) |
| status | VarChar | YES | 상태 (`BOOKED`, `WASHED` 등) |
| deleted_yn | TinyInt | NO | 삭제 여부 |
| created_at | DateTime | NO | 가입일 |

#### 주요 주의사항

- **`user_referral.version` 필터 필수**: version=3 기준으로만 현재 프로그램 분석할 것
- **코드 발급 ≠ 공유**: `user_referral`에 코드가 있어도 `user_referral_log`에 대응 레코드가 없으면 실제 초대 미발생
- 초대 성과 분석 시 `user_referral_log`를 LEFT JOIN으로 연결해야 "코드만 있고 초대 안 된" 케이스 포함 가능

#### 초대자별 성과 집계 쿼리 패턴

```sql
SELECT
    inviter.id AS inviter_id,
    inviter.name AS 초대자,
    COUNT(DISTINCT ur.referral_code) AS 보유코드수,
    COUNT(DISTINCT url.user_id) AS 가입한친구수,
    COUNT(DISTINCT CASE WHEN url.status IN ('BOOKED', 'WASHED') THEN url.user_id END) AS 예약한친구수,
    COUNT(DISTINCT CASE WHEN url.status = 'WASHED' THEN url.user_id END) AS 세차완료친구수
FROM user_referral ur
JOIN app_user inviter ON inviter.id = ur.user_id
LEFT JOIN user_referral_log url
    ON url.user_referral_id = ur.id
    AND url.deleted_yn = 0
WHERE ur.version = 3
  AND inviter.test_yn = 0
  AND inviter.deleted_yn = 0
GROUP BY inviter.id, inviter.name
ORDER BY 가입한친구수 DESC;
```

> ⚠️ `user_referral_log`를 기준으로 JOIN하면 코드만 있고 가입자 없는 inviter가 누락됨.  
> 반드시 `user_referral` 기준으로 LEFT JOIN할 것.

---

## View Log: 페이지 조회 로그 *(신규 - 2026-03)*

### view_log

> 고객의 앱 내 특정 페이지 조회를 로깅하는 테이블. Prisma 스키마 외부에서 관리됨.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| table_name | VarChar(100) | YES | - | 조회 대상 테이블명 |
| record_id | Int | NO | - | 조회 대상 레코드 ID |
| created_at | DateTime | YES | CURRENT_TIMESTAMP | 조회 일시 |
| user_id | Int | YES | - | FK → user |

**인덱스:** `table_name`, `user_id`

**알려진 table_name 값 (2026-03 확인):**
- `'wash_result'` → 세차 결과 페이지 조회. `record_id = wash_result.id`

**조회 여부 확인 패턴:**
```sql
-- reservation → wash_result → view_log 순서로 조인
LEFT JOIN wash_result wr ON wr.reservation_id = r.id
LEFT JOIN view_log vl
    ON vl.table_name = 'wash_result'
    AND vl.record_id = wr.id
-- vl.id IS NOT NULL → 결과 페이지 조회한 사람
```

**참고 수치 (2026-02 기준):**
- 첫 세차 고객 결과 페이지 조회율: **55.9%** (649명 중 363명)
- n번째 세차 고객 결과 페이지 조회율: **69.1%** (2262명 중 1564명)

---

## Community: 커뮤니티

### post

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| user_id | Int | NO | - | FK → user |
| title | VarChar(255) | NO | - | 제목 |
| content | Text | NO | - | 내용 |
| car_id | Int | NO | - | FK → car |

---

### mechanic

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 정비사 이름 |
| description | Text | YES | - | 소개 |
| metadata | Json | NO | - | 메타데이터 (경력, 평균 답변 시간 등) |

---

### maintenance_category

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 카테고리명 (세차/디테일링, 판금/도색, 커스터마이징, 매매/리스, 사고/보험, 정비/점검) |

---

## Config & System: 설정/시스템

### config

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| key | VarChar(255) | NO | - | 설정 키 |
| value | Text | NO | - | 설정 값 |

---

### config_data

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| label | VarChar(45) | NO | - | 라벨 (unique) |
| string_value | Text | YES | - | 문자열 값 |
| double_value | Float | YES | - | 실수 값 |
| int_value | Int | YES | - | 정수 값 |
| value_type | Int | YES | 0 | 값 타입 |

---

### ~~log~~ (⚠️ 실제 DB에 존재하지 않음)

> Prisma 스키마에는 정의되어 있으나 실제 DB 테이블 없음. 쿼리에 사용 불가.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| table_name | VarChar(100) | YES | - | 대상 테이블 |
| column_name | VarChar(100) | YES | - | 대상 컬럼 |
| record_id | Int | YES | - | 레코드 ID |
| old_value | Text | YES | - | 이전 값 |
| new_value | Text | YES | - | 새 값 |
| type | VarChar(25) | NO | - | 로그 타입 |
| status | VarChar(25) | YES | - | 상태 |

---

### job

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 잡 이름 (unique) |
| status | VarChar(255) | NO | - | ACTIVE / INACTIVE |
| cron_time | Text | YES | - | 크론 표현식 |
| type | VarChar(255) | YES | - | LMS / ALIMTALK |
| metrics | Json | YES | - | 메트릭 |

---

### job_execution

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| status | VarChar(255) | NO | - | RUNNING / SUCCESS / FAILED |
| metadata | Json | YES | - | 메타데이터 |
| result | Json | YES | - | 결과 |
| job_id | Int | NO | - | FK → job |

---

### nocode_page

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| key | VarChar(255) | NO | - | 페이지 키 (unique) |
| title | VarChar(255) | YES | - | 제목 |
| go_back_yn | Boolean | NO | true | 뒤로가기 표시 |
| auth_required_yn | Boolean | NO | false | 인증 필요 |
| container_padding_yn | Boolean | NO | true | 패딩 사용 |

---

## Region & Scheduling: 지역/스케줄링

### service_region_group

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 지역 그룹명 |

---

### service_region

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| available_yn | TinyInt | NO | 0 | 서비스 가능 여부 |
| code | VarChar(255) | NO | - | 행정동 코드 (unique) |
| sido | VarChar(255) | NO | - | 시/도 |
| sigungu | VarChar(255) | NO | - | 시/군/구 |
| dong | VarChar(255) | NO | - | 동 |
| service_region_group_id | Int | YES | - | FK → service_region_group |

---

### national_holiday

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| date | VarChar(255) | NO | - | 날짜 (YYYY-MM-DD) |

---

### repair_shop

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| name | VarChar(255) | NO | - | 정비소명 |
| address | VarChar(255) | NO | - | 주소 |
| latitude | Float | NO | - | 위도 |
| longitude | Float | NO | - | 경도 |
| contact | VarChar(255) | NO | - | 연락처 |

---

## Ads & Analytics: 광고/분석

### meta_ads

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| campaign_name | VarChar(255) | NO | - | 캠페인명 |
| adset_name | VarChar(255) | NO | - | 광고 세트명 |
| ad_name | VarChar(255) | NO | - | 광고명 |
| impressions | Int | NO | - | 노출수 |
| clicks | Int | NO | - | 클릭수 |
| reach | Int | NO | - | 도달수 |
| spend | Float | NO | - | 지출 |
| date_start | DateTime | NO | - | 시작일 |
| date_stop | DateTime | NO | - | 종료일 |

---

## Zone & Slot: 슬롯 수요 분석 *(신규 - 2026-03-11)*

### zone

> 디테일러 배정 및 수요 분석의 기준 지리 단위. 서울/수도권을 13개 zone으로 분할.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int | NO | autoincrement | PK |
| created_at | DateTime(3) | NO | CURRENT_TIMESTAMP(3) | 생성일 |
| modified_at | DateTime(3) | NO | - | 수정일 |
| name | VarChar(191) | NO | - | zone명 (예: `Z1 (마포구/용산구)`) |
| area | Polygon | NO | - | 폴리곤 영역 |

**인덱스:** `area` (SPATIAL)

**현재 zone 목록 (2026-03 기준, 13개):**
Z0 성남, Z1 마포/용산, Z3 강남/송파, Z4 강서/김포, Z5 서초/용산, Z6 인천 연수/서구, Z7 안양/과천, Z9 성동/성북, Z10 영등포/금천, Z12 강남/서초, Z14 용인/화성, Z16 강동/송파, Z17 고양/파주

#### 좌표 → zone 매핑 패턴

```sql
-- ST_Contains로 폴리곤 내 포함 여부 판단
LEFT JOIN zone z ON ST_Contains(
    z.area,
    ST_GeomFromText(CONCAT('POINT(', longitude, ' ', latitude, ')'))
)
```

> ⚠️ zone 폴리곤이 서비스 가능 지역 전체를 커버하지 않음 (약 11% 주소가 zone 밖).
> 백엔드는 zone 밖 주소에 대해 가장 가까운 zone 중심 기준으로 슬롯 할당하지만, DB에는 그 매핑이 저장되지 않음.

---

### time_slot_request_log

> 유저가 예약 가능 슬롯을 조회할 때 요청을 로깅하는 테이블.

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | VarChar(50) | NO | - | PK (UUID) |
| created_at | DateTime | NO | CURRENT_TIMESTAMP | 요청 시점 (**UTC 저장**) |
| user_id | Int | YES | - | FK → user |
| address_id | Int | YES | - | FK → user_address |
| location | VarChar(255) | YES | - | 주소 텍스트 |
| latitude | Decimal(11,8) | YES | - | 위도 |
| longitude | Decimal(11,8) | YES | - | 경도 |
| duration | Int | YES | - | 소요시간 (분) |
| from_date | DateTime | YES | - | 조회 시작일 (**UTC**) |
| to_date | DateTime | YES | - | 조회 종료일 (**UTC**) |
| time_range | Json | YES | - | 시간 필터 조건 (고객이 설정한 시간대) |
| last_detailer_id | Int | YES | - | 마지막 담당 디테일러 |
| reservation_id | Int | YES | - | FK → reservation (예약 전환된 경우) |
| reserved_at | DateTime | YES | - | 예약 전환 시점 |

**인덱스:** `user_id`, `address_id`, `reservation_id`

#### 주요 특성 및 주의사항

- `reserved_at` + `reservation_id`가 있으면 실제 예약으로 전환된 건
- 동일 유저가 단시간 내 반복 조회 빈번 → **분석 시 user × address × date(KST) 기준 dedup 필요**
- `address_id IS NULL`인 건은 분석 대상에서 제외

---

### time_slot_result_log

> request에 대해 실제 리턴된 슬롯 목록. **1행 = 슬롯 1개.**

| 컬럼 | 타입 | nullable | 기본값 | 설명 |
|------|------|-----|--------|------|
| id | Int/BigInt | NO | autoincrement | PK |
| request_id | VarChar(50) | NO | - | FK → time_slot_request_log.id |
| time_slot | DateTime | NO | - | 슬롯 일시 (**UTC 저장**) |
| detailer_id | Int | YES | - | 해당 슬롯의 디테일러 |
| priority | Int | YES | - | 슬롯 우선순위 |
| is_last_detailer | TinyInt | YES | - | 마지막 담당 디테일러 여부 |
| valid_until | DateTime | YES | - | 유효기한 |
| show_yn | TinyInt | YES | - | **고객에게 실제 노출 여부. 분석 시 `show_yn = 1`만 사용** |

**인덱스:** `request_id`

#### 주요 특성 및 주의사항

- result_log가 **아예 없는** request = 서비스 제한 지역 요청 (전체의 약 2%)
- result_log가 있으면 `show_yn = 1` 슬롯이 **항상 1건 이상** 존재 (0건 케이스 미확인, 2026-03-11 기준)
- 인덱스 미비로 대량 JOIN 시 느림 → `WHERE trl.id > {최근ID}` 또는 날짜 범위로 필터 권장
- 2026-01-01부터 데이터 존재, 일 1,000~2,500 request × request당 수십 result row

---

## Views

### car_model_target (View) — 고가차 타겟 (출고가 6500↑)

> **고가차 타겟의 정본.** 데이터 정본은 `car_price` 테이블(model_id·launch_price), 이 뷰는 그 위에서 `is_target = (launch_price >= 6500)`로 계산. 현재 220개. 폐기된 `car_brand.target_yn` 대신 사용.
> 쿼리: `SELECT * FROM car_model_target WHERE is_target=1` 또는 `SELECT * FROM car_price WHERE launch_price>=6500` (동일 결과).
> ⚠️ 아래 `car_target`(고객 실차의 주행신호 타겟)과 **다른 개념** — "이 모델이 고가차냐"는 여기, "이 고객 차가 활성 타겟이냐"는 car_target.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | Int | car_model.id |
| name | VarChar | 모델명 |
| is_target | Boolean | 출고가 6500만↑ 여부 (launch_price>=6500) |

### car_target (View)

> 고객 실차 타겟 계산 뷰 (연식 5년 미만 + 연평균 주행거리 10,000km 이하). ⚠️ 가격 기준 고가차가 아니라 **고객 차량의 활성/주행신호 타겟**. 가격 기준 고가차는 위 `car_model_target`.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | Int | car.id (unique) |
| plate_number | VarChar(10) | 차량번호 |
| user_id | Int | FK → user |
| mileage | Int | 주행거리 |
| current_mileage | Int | 최신 주행거리 (checkup.mileage ?? car.mileage) |
| is_target | Boolean | 타겟 여부 (NULL=판단불가, TRUE=타겟, FALSE=비타겟) |

---

### v_detailer_holiday_daily (View)

> 디테일러별 일별 휴가 반영 뷰. CBR 캐파 계산에 사용.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| d | Date | 날짜 |
| detailer_id | Int | FK → detailer |
| off_factor | Float | 휴가 반영 비율 (0.0 ~ 1.0) |
| memos | Text | 휴가 메모 (복수 건 `\|` 구분) |

#### 뷰 구조 특성

- `detailer_holiday` + `detailer_work_schedule` + `detailer_work_schedule_rule`을 **INNER JOIN**
- **스케줄이 미등록된 디테일러는 휴가 데이터가 있어도 뷰에 잡히지 않음**
  - 해당 디테일러의 `off_factor`가 NULL → CBR에서 `COALESCE(..., 0)`으로 0 처리
  - 즉 스케줄 미등록 + 휴가 입력 케이스는 휴가가 캐파 계산에 미반영됨
- `detailer_holiday.from/to`는 **UTC 저장** → 뷰 내부에서 +9h KST 변환 처리됨
- 퇴사 관련 메모(`%퇴사%`, `%헤이딜러%`)가 포함된 holiday 레코드는 필터링됨

#### off_factor 계산 방식

| 케이스 | off_factor |
|--------|-----------|
| 메모에 `연차` 포함 | 1.0 |
| 메모에 `반차` 포함 | 0.5 |
| 그 외 | 휴가 시간 ∩ 근무 스케줄 겹침 비율 (초 단위, 9시간 기준) |
| 복수 건 합산 | `LEAST(1.0, SUM(off_factor))` |

#### CBR과의 관계

- CBR은 `active_cnt(supply sheet) - off_sum(holiday 뷰)` **집계 빼기 방식**으로 동작
- 개인별 1:1 매핑이 아닌 합산 차감 구조
- supply sheet ↔ detailer 매핑이 100% 정확한 현재 상태에서는 결과 신뢰 가능

---

## 실험/코호트 분석 쿼리 패턴 *(신규 - 2026-03)*

> 실험군/대조군 비교 분석 시 반복적으로 사용되는 패턴. 실수하기 쉬운 케이스 위주로 정리.

---

### 1. 표준 live_user 필터

> 분석 쿼리의 시작점. 테스트/탈퇴/임시 계정 및 내부 직원 번호 제외.

```sql
live_user AS (
    SELECT id FROM app_user
    WHERE deleted_yn = 0
      AND test_yn = 0
      AND temp_yn = 0
      AND phone NOT IN (
          '01020866510','01035474964','01093277016','01091350157',
          '01043446885','01049664316','01050373300','01066943645',
          '01073740979','01092828753',
          '01051415705','01091622508','01000000000'  -- 2026-03 추가
      )
)
```

---

### 2. 고객별 세차 순서 번호 매기기

```sql
washed AS (
    SELECT r.id AS reservation_id, r.user_id,
           DATE_ADD(r.reservation_datetime, INTERVAL 9 HOUR) AS washed_kst_dt
    FROM reservation r
    JOIN live_user lu ON lu.id = r.user_id
    WHERE r.status IN ('WASHED', 'REPORT_SENT')
),
ranked_wash AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY user_id ORDER BY washed_kst_dt, reservation_id
        ) AS wash_n
    FROM washed
)
-- wash_n = 1 → 생애 첫 세차
-- wash_n >= 2 → 재방문
```

---

### 3. 실험군 모수 정의 — 쿠폰 수령자로 잡으면 안 되는 이유

```sql
-- ❌ 잘못된 패턴: promotion_id 기준으로 실험군 정의
-- promotion_id=122 받은 사람 = 이미 리뷰 쓴 사람만 → 모수 왜곡
experiment AS (
    SELECT rw.user_id ...
    FROM ranked_wash rw
    JOIN promotion_application pa ON pa.record_id = rw.user_id
        AND pa.promotion_id = 122
    WHERE rw.wash_n = 1
)

-- ✅ 올바른 패턴: 구매/세차 조건으로 모수 정의 (대조군과 동일 기준)
first_paid_purchase AS (
    SELECT p.user_id,
           ROW_NUMBER() OVER (PARTITION BY p.user_id ORDER BY p.paid_at, p.id) AS rn
    FROM payment p
    JOIN product pr ON pr.id = p.product_id
    WHERE p.status = 'PAID'
      AND p.amount > 0
      AND pr.type = 'VOUCHER'
      AND pr.category = 'CAR_WASH'
      AND pr.name = '외부 + 내부'
),
experiment AS (
    SELECT rw.user_id ...
    FROM ranked_wash rw
    JOIN first_paid_purchase fpp ON fpp.user_id = rw.user_id AND fpp.rn = 1
    WHERE rw.wash_n = 1
      AND rw.washed_kst_dt >= '2026-02-11'
)
```

---

### 4. 재예약 관찰 기간 — 고정 날짜 대신 개인별 N일

```sql
-- ❌ 잘못된 패턴: 고정 날짜 컷
-- 첫 세차 날짜에 따라 관찰 기간이 사람마다 달라짐
AND r.created_at < '2026-03-05'

-- ✅ 올바른 패턴: 개인별 동일 관찰 기간 적용
AND r.created_at > c.first_wash_dt
AND r.created_at < DATE_ADD(c.first_wash_dt, INTERVAL 21 DAY)
```

---

### 5. 리뷰-쿠폰 1:1 연결 패턴

> `promotion_application.note` 에 `Review ID {id}` 형식으로 기록됨.
> note 조건 없이 user_id로만 조인하면 과거 쿠폰 전체가 잡혀 숫자 왜곡 발생.

```sql
LEFT JOIN review rv
    ON rv.reservation_id = e.first_reservation_id
    AND rv.hide_yn = 0
LEFT JOIN promotion_application pa
    ON pa.record_id = e.user_id
    AND pa.table_name = 'app_user'
    AND pa.promotion_id = 122
    AND pa.deleted_yn = 0
    AND pa.note LIKE CONCAT('%Review ID ', rv.id)  -- 이 조건 필수
```

---

### 6. 구독 리텐션 코호트 분석 *(신규 - 2026-03)*

구독 시작월 기준 코호트별 N개월차 유지율을 산출한다.

**핵심 설계 원칙:**
- 코호트 기준: `subscription.started_at` (KST 변환)
- 이탈 시점: ACTIVE → NULL(유지 중), STOPPED → `stopped_at`, ENDED → `ended_at`
- **ACTIVE 유저는 "유지"로 처리** — 관찰 기간이 아직 도래하지 않은 경우 모수(observable)에서 제외
- 상품별 분리 필수 (월1회/2회/4회 특성이 다름)

```sql
WITH RECURSIVE months_seq AS (
    SELECT 0 AS month_offset
    UNION ALL
    SELECT month_offset + 1 FROM months_seq WHERE month_offset < 4
),

live_users AS (
    SELECT id FROM app_user
    WHERE deleted_yn = 0 AND test_yn = 0 AND temp_yn = 0
      AND phone NOT IN (
          '01020866510','01035474964','01093277016','01091350157',
          '01043446885','01049664316','01050373300','01066943645',
          '01073740979','01092828753','01051415705','01091622508',
          '01000000000'
      )
),

subs AS (
    SELECT
        s.id AS sub_id,
        s.user_id,
        s.status,
        DATE(CONVERT_TZ(s.started_at, '+00:00', '+09:00')) AS started_kst,
        DATE_FORMAT(CONVERT_TZ(s.started_at, '+00:00', '+09:00'), '%Y-%m') AS cohort_month,
        CASE
            WHEN s.status = 'ACTIVE' THEN NULL
            WHEN s.stopped_at IS NOT NULL
                THEN DATE(CONVERT_TZ(s.stopped_at, '+00:00', '+09:00'))
            WHEN s.ended_at IS NOT NULL
                THEN DATE(CONVERT_TZ(s.ended_at, '+00:00', '+09:00'))
            ELSE DATE(CONVERT_TZ(s.started_at, '+00:00', '+09:00'))
        END AS churned_at_kst,
        CASE
            WHEN p.id IN (178,182,186,174,190,170,87,82,194) THEN '월1회_외부내부'
            WHEN p.id IN (3555,3556,3557,3558,3559,3560,3561) THEN '월2회_외부만'
            WHEN p.id IN (3563,3564,3565,3566,3567,3568)       THEN '월4회_외부만'
        END AS sub_type
    FROM subscription s
    JOIN product p ON p.id = s.product_id
    JOIN live_users lu ON lu.id = s.user_id
    WHERE s.status IN ('ACTIVE', 'STOPPED', 'ENDED')
      AND p.id IN (
          178,182,186,174,190,170,87,82,194,
          3555,3556,3557,3558,3559,3560,3561,
          3563,3564,3565,3566,3567,3568
      )
      AND CONVERT_TZ(s.started_at, '+00:00', '+09:00') >= '2025-12-01'
)

SELECT
    sub_type,
    cohort_month,
    month_offset,
    COUNT(CASE
        WHEN DATE_ADD(started_kst, INTERVAL month_offset MONTH) <= CURDATE()
        THEN 1
    END) AS observable,
    COUNT(CASE
        WHEN DATE_ADD(started_kst, INTERVAL month_offset MONTH) <= CURDATE()
         AND (churned_at_kst IS NULL
              OR churned_at_kst >= DATE_ADD(started_kst, INTERVAL month_offset MONTH))
        THEN 1
    END) AS retained,
    ROUND(
        COUNT(CASE
            WHEN DATE_ADD(started_kst, INTERVAL month_offset MONTH) <= CURDATE()
             AND (churned_at_kst IS NULL
                  OR churned_at_kst >= DATE_ADD(started_kst, INTERVAL month_offset MONTH))
            THEN 1
        END)
        /
        NULLIF(COUNT(CASE
            WHEN DATE_ADD(started_kst, INTERVAL month_offset MONTH) <= CURDATE()
            THEN 1
        END), 0) * 100
    , 1) AS retention_pct
FROM subs
CROSS JOIN months_seq
GROUP BY sub_type, cohort_month, month_offset
HAVING observable > 0
ORDER BY sub_type, cohort_month, month_offset;
```

**⚠️ 흔한 실수:**
- ACTIVE 유저의 이탈 시점을 `CURDATE()`로 잡으면 → 관찰 기간 도래 전인 사람이 "이탈"로 잡힘 (3개월차가 급락하는 현상 발생)
- `reservation.subscription_id`로 구독 여부를 판단하면 안 됨 (98% NULL)
- `created_at`과 `started_at`을 혼동하면 코호트가 틀어짐

**참고 수치 (2025-12 ~ 2026-02 코호트):**

| 상품 | 1개월차 유지율 (평균) | 2개월차 유지율 (12~1월) |
|------|---------------------|---------------------|
| 월1회 외부+내부 | ~65% | ~53% |
| 월2회 외부만 | ~73% | ~56% |
| 월4회 외부만 | ~72% | ~50% |

---

### 7. 디테일러 연속 배정 vs 재예약율 비교 *(신규 - 2026-03)*

"같은 디테일러가 연속으로 세차하면 재예약율이 높은가?"를 검증하는 패턴.

**핵심 로직:**
- 완료된 세차를 유저별로 순서 매김 (`ROW_NUMBER`)
- 2회차부터, 직전 세차(wash_n - 1)의 디테일러와 비교
- 다음 세차(wash_n + 1)의 존재 여부 + 간격(일수)으로 재예약 판단

```sql
WITH live_users AS ( /* 표준 live_user 필터 */ ),

completed_washes AS (
    SELECT
        r.id AS reservation_id,
        r.user_id,
        r.detailer_id,
        DATE(CONVERT_TZ(r.washed_at, '+00:00', '+09:00')) AS washed_date_kst,
        CASE
            WHEN EXISTS (
                SELECT 1 FROM subscription s
                WHERE s.user_id = r.user_id
                  AND s.status = 'ACTIVE'
                  AND s.started_at <= r.washed_at
            ) THEN '구독'
            ELSE '1회권'
        END AS user_type,
        ROW_NUMBER() OVER (
            PARTITION BY r.user_id ORDER BY r.washed_at, r.id
        ) AS wash_n
    FROM reservation r
    JOIN live_users lu ON lu.id = r.user_id
    WHERE r.status IN ('WASHED', 'REPORT_SENT')
      AND r.deleted_yn = 0
      AND r.detailer_id IS NOT NULL
      AND r.washed_at IS NOT NULL
      AND r.washed_at >= '2025-09-01'
),

wash_context AS (
    SELECT
        cur.user_id,
        cur.user_type,
        cur.washed_date_kst AS cur_date,
        nxt.washed_date_kst AS next_date,
        DATEDIFF(nxt.washed_date_kst, cur.washed_date_kst) AS days_to_next,
        CASE
            WHEN cur.detailer_id = prev.detailer_id THEN '같은 디테일러'
            ELSE '다른 디테일러'
        END AS detailer_continuity
    FROM completed_washes cur
    JOIN completed_washes prev
        ON prev.user_id = cur.user_id AND prev.wash_n = cur.wash_n - 1
    LEFT JOIN completed_washes nxt
        ON nxt.user_id = cur.user_id AND nxt.wash_n = cur.wash_n + 1
    WHERE cur.wash_n >= 2
)

SELECT
    user_type,
    detailer_continuity,
    COUNT(*) AS total_washes,
    SUM(CASE WHEN days_to_next <= 30 THEN 1 ELSE 0 END) AS rebook_30d,
    ROUND(SUM(CASE WHEN days_to_next <= 30 THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) AS rebook_30d_pct,
    SUM(CASE WHEN days_to_next <= 45 THEN 1 ELSE 0 END) AS rebook_45d,
    ROUND(SUM(CASE WHEN days_to_next <= 45 THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) AS rebook_45d_pct,
    ROUND(AVG(days_to_next), 1) AS avg_days_to_next,
    SUM(CASE WHEN next_date IS NULL THEN 1 ELSE 0 END) AS no_rebook_cnt
FROM wash_context
GROUP BY user_type, detailer_continuity
ORDER BY user_type, detailer_continuity;
```

**⚠️ 해석 주의:**
- 이 결과는 **상관관계**이지 인과관계가 아님
- 충성도 높은 고객이 재예약도 많이 하고, 같은 디테일러에 걸릴 확률도 높은 "역인과" 가능성 존재
- 인과 검증은 디테일러 제안 리텐션 실험으로 진행 중 (2026-03)

**참고 수치 (2025-09 ~ 2026-03):**

| 구분 | 같은 디테일러 30일 재예약 | 다른 디테일러 30일 재예약 | 차이 |
|------|----------------------|----------------------|------|
| 1회권 | 44.9% | 35.6% | +9.3pp |
| 구독 | 65.7% | 59.0% | +6.7pp |

---

## Zone 수요-공급 분석 쿼리 패턴 *(신규 - 2026-03-11)*

> zone별 수요(슬롯 조회)와 공급(디테일러 인원)을 비교하여 배정 우선순위를 결정하는 패턴.

---

### 1. 슬롯 조회 request dedup 패턴

> 동일 유저가 같은 주소로 하루에 여러 번 조회 → user × address × date(KST) 기준 마지막 1건만 사용.

```sql
request_dedup AS (
    SELECT tsr.id, tsr.created_at, tsr.user_id, tsr.address_id,
           DATE(CONVERT_TZ(tsr.created_at, '+00:00', '+09:00')) AS request_date_kst,
           ROW_NUMBER() OVER (
               PARTITION BY tsr.user_id, tsr.address_id,
                            DATE(CONVERT_TZ(tsr.created_at, '+00:00', '+09:00'))
               ORDER BY tsr.created_at DESC
           ) AS rn
    FROM time_slot_request_log tsr
    WHERE tsr.address_id IS NOT NULL
)
-- WHERE rn = 1 로 필터
```

---

### 2. 슬롯 지표: D+N 및 3일 내 슬롯 수

> D+N = 요청일 기준 가장 빠른 show_yn=1 슬롯까지 일수. **median 사용 필수** (AVG는 장기 슬롯 outlier에 끌려 왜곡됨).

```sql
request_metrics AS (
    SELECT
        rd.id AS request_id,
        rd.request_date_kst,
        az.zone_name,
        -- D+N
        DATEDIFF(
            DATE(CONVERT_TZ(
                MIN(CASE WHEN trl.show_yn = 1 THEN trl.time_slot END),
                '+00:00', '+09:00'
            )),
            rd.request_date_kst
        ) AS days_to_first_slot,
        -- 3일 내 슬롯 수 (당일 포함 3 calendar days)
        COUNT(CASE
            WHEN trl.show_yn = 1
             AND DATE(CONVERT_TZ(trl.time_slot, '+00:00', '+09:00'))
                 BETWEEN rd.request_date_kst
                     AND DATE_ADD(rd.request_date_kst, INTERVAL 2 DAY)
            THEN 1
        END) AS slots_within_3days
    FROM request_dedup rd
    JOIN addr_zone az ON az.address_id = rd.address_id
    LEFT JOIN time_slot_result_log trl ON trl.request_id = rd.id
    WHERE rd.rn = 1
      AND az.zone_id IS NOT NULL
    GROUP BY rd.id, rd.request_date_kst, az.zone_name
)
```

---

### 3. D+N과 3일 내 슬롯의 일관성 검증

> D+N=4일인데 3일 내 슬롯=2개 같은 결과가 나오면 AVG를 쓰고 있을 가능성 높음. 반드시 median으로.

| D+N (median) | 기대되는 3일 내 슬롯 |
|---|---|
| 0일 | 1개 이상 (당일 슬롯 존재) |
| 1~2일 | 1개 이상 |
| 3일 이상 | 0개 |

---

### 4. Zone별 가용 디테일러 수 (현직만, 파견 제외)

```sql
zone_detailers AS (
    SELECT
        z.name AS zone_name,
        COUNT(DISTINCT d.id) AS headcount
    FROM detailer_work_schedule_rule dwsr
    JOIN detailer_work_schedule dws ON dws.id = dwsr.schedule_id
    JOIN detailer d ON d.id = dws.detailer_id
    JOIN zone z ON z.id = dwsr.zone_id
    LEFT JOIN detailer_supply_sheet dss
        ON dss.phone_norm = d.phone COLLATE utf8mb4_general_ci
    WHERE dwsr.deleted_at IS NULL
      AND dws.effective_from <= NOW()
      AND dws.effective_to >= NOW()
      AND d.retired_yn = 0
      AND dss.status = '현직'
      AND (dss.retired_date IS NULL OR dss.retired_date > CURDATE())
    GROUP BY z.name
)
```

> ⚠️ 겸임 디테일러 존재 (동일 detailer_id가 복수 zone의 schedule_rule에 배정됨) → headcount 시 각 zone에서 1명으로 카운트되므로, 전체 합산 시 중복 주의.

---

### 5. 배정 우선순위 점수

> 점수 = 일평균 요청 × D+N 중앙값 / 인원. 높을수록 인력 부족.

```sql
SELECT
    za.zone_name,
    zd.headcount,
    za.avg_daily_requests,
    za.median_d_plus_n,
    za.median_slots_3d,
    ROUND(za.avg_daily_requests * za.median_d_plus_n, 0) AS total_score,
    ROUND(za.avg_daily_requests * za.median_d_plus_n / zd.headcount, 1) AS score_per_person
FROM zone_agg za
JOIN zone_detailers zd ON zd.zone_name = za.zone_name
ORDER BY score_per_person DESC;
```

> - **score_per_person ≥ 60**: 긴급 증원 필요
> - **score_per_person 40~60**: 주의
> - **score_per_person < 20**: 여유 (재배분 후보)

---

## 테이블 관계 요약

### 핵심 관계

```
user (app_user)
  ├── car (1:N) ─── car_brand, car_model, car_tier
  ├── subscription (1:N) ─── product
  ├── reservation (1:N) ─── detailer, user_address
  ├── payment (1:N) ─── card_payment, payment_method
  ├── user_service (1:N) ─── service, product
  ├── user_option (1:N) ─── option (options), product
  ├── user_point (1:N)
  ├── user_point_history (1:N)
  ├── user_attribution (1:N)
  ├── user_device (1:N)
  ├── user_referral (1:N) ─── [레퍼럴 코드 발급]
  └── user_referral_log (1:N, as invitee) / user_referral_log.recommender_user_id (as inviter)

reservation
  ├── reservation_car (1:N) ─── car
  ├── reservation_care (1:N) ─── care_item
  ├── reservation_status_log (1:N)
  ├── wash_result (1:1) ←── view_log (N) [table_name='wash_result', record_id=wash_result.id]
  ├── report (1:N) ─── report_card
  ├── checkup (1:1) ─── checkup_detail
  ├── review (1:1) ←── promotion_application.note 로 리뷰-쿠폰 연결
  ├── payment (1:N)
  └── user_service / user_option (사용 처리)

detailer
  ├── reservation (1:N)
  ├── detailer_work_schedule (1:N) ─── detailer_work_schedule_rule ─── zone
  ├── detailer_holiday (1:N)
  ├── detailer_incentive (1:N)
  └── detailer_routes (1:N)

zone
  ├── detailer_work_schedule_rule (1:N) ─── detailer 배정
  └── [time_slot_request_log ← user_address 좌표 기반 ST_Contains 매핑]

time_slot_request_log
  ├── time_slot_result_log (1:N, via request_id)
  ├── user_address (N:1, via address_id)
  └── reservation (N:1, via reservation_id, 예약 전환 건만)

CRM Flow:
  crm_consultation
    ├── crm_issue (1:N)
    ├── crm_repair_order (1:N) ─── crm_repair_order_issue
    ├── crm_note (1:N)
    └── crm_todo (1:N)
```

### 테이블명 매핑 (Prisma → DB)

| Prisma 모델 | 실제 DB 테이블명 |
|-------------|-----------------|
| user | app_user |
| option | options |
| car_target | car_target (View) |
| - | detailer_supply_sheet (외부 연동, Google Apps Script) |
| - | v_detailer_holiday_daily (View) |
| - | view_log (앱 내 페이지 조회 로그, Prisma 외부 관리) |
| - | zone (zone 폴리곤 정의) |
| - | time_slot_request_log (슬롯 조회 요청 로그) |
| - | time_slot_result_log (슬롯 조회 결과 로그) |

### 외부 테이블 (Prisma 외부, Grafana/분석용)

#### meta_daily_performance

Meta Ads Manager 일별 성과 데이터. Grafana CBR 대시보드의 광고비/CAC/ROAS 쿼리에서 사용.

| 컬럼 | 타입 | nullable | 설명 |
|------|------|---------|------|
| date | Date | NO (PK) | 성과 집계 날짜 |
| total_spending | Int | YES | 일별 총 광고비 (원) |
| total_purchase | Int | YES | Meta 리포트 기준 구매수 |
| total_purchase_value | Int | YES | Meta 리포트 기준 구매 금액 |
| avg_cac | Int/Float | YES | Meta 리포트 기준 평균 CAC |

#### naver_daily_performance

Naver 검색광고 일별 성과 데이터. 마케팅 대시보드 Naver_RawData 시트에서 Apps Script JDBC로 동기화.

| 컬럼 | 타입 | nullable | 설명 |
|------|------|---------|------|
| date | Date | NO (PK) | 성과 집계 날짜 |
| total_cost | Int | YES | 일별 총 광고비 (원) |
| impressions | Int | YES | 노출수 |
| clicks | Int | YES | 클릭수 |
| conversions | Int | YES | Naver 자체 전환수 |
| created_at | Timestamp | NO | 레코드 생성일 |
| updated_at | Timestamp | NO | 마지막 업데이트일 |

#### user_utm_triage

유저 유입 채널 분류 (paid/organic). CBR의 CAC_DB, Organic:Paid 비율 쿼리에서 사용.

| 컬럼 | 타입 | nullable | 설명 |
|------|------|---------|------|
| user_id | Int | NO | FK → app_user |
| category | VarChar | YES | 'paid' 또는 'organic' |
| source | VarChar | YES | 'meta' 등 채널 소스 |
| paid_at | DateTime | YES | 유입 시점 (UTC) |

---

### Deprecated 테이블

- `wash_available_area` → service_region 사용
- `jurisdiction`, `jurisdiction_region`, `detailer_jurisdiction` → service_region + detailer_work_schedule 사용
- `detailer_region` → detailer_work_schedule 사용
- `service_available_day` → available_day 사용
- `region` → service_region 사용