# Caramel DB 쿼리 레퍼런스

caramel-prod DB 분석 쿼리 시 반드시 따를 규칙. `grafana-audit/CLAUDE.md`와 함께 참조.

---

## 0. 모든 쿼리 체크리스트

모든 쿼리를 짜기 전 아래 세 가지를 반드시 확인한다.

- **테스터 제외** — `deleted_yn=0, test_yn=0, temp_yn=0` (앱 유저 기준). 디테일러는 → §3a
- **UTC→KST 변환** — DB 전체 UTC 저장. 날짜 집계 전 반드시 변환 → §5a
- **유령예약 제거** — CONFIRMED 포함 예약 집계 시 `user_service` + `car` 존재 여부 확인 → §2b
- **디테일러 화면과 고객 화면의 예약이 다르다** — 후불 예약이 고객 앱에서만 숨겨짐(둘 다 실재) → §2b-1
- **차량/타겟(고가차) 분석** — `reservation`엔 car_id 없음. **`reservation_car` 경유**가 정본 → §2d (⚠️ `user_service.applicable_car_id`는 ~60% NULL 함정). 타겟 판별 = `car_model_target.is_target` → §2d
- **행 나열 + 합계 함께 제시 시 합계는 SQL로** — 합계·상태별 건수를 답변에서 손으로 세지 말고 `GROUP BY status` 별도 쿼리로 산출해 행 수와 일치하는지 확인 (실사례: 39행 받아놓고 답변에서 35건으로 오기)
- **dev에서 검증할 때 prod의 `service.id`를 그대로 쓰지 말 것** (2026-08-06 실측) — dev와 prod는 `service.id`가 다르다: prod `137`=`프리미엄 세차 패키지 올클린 케어`인데 **dev `137`=`[B2B] 외부만`**이고, prod `120`(반얀)·`140`(자스민)·`142`는 **dev에 없다**. 이 문서의 id는 **전부 prod 기준**이므로 dev 쿼리·dev E2E 테스트는 `service.name LIKE`로 id를 먼저 되찾아 쓴다. ⚠️틀려도 에러가 안 나고 0건이 나와서 "기능 미동작"으로 오판하게 된다

---

## 1. 핵심 테이블 맵

`reservation`을 중심으로 한 연결 구조:

```
app_user (고객. NOT user/users — 그 테이블명 없음)
    │
    ├── user_address (주소/좌표. r.latitude deprecated → COALESCE 필수 → §2e)
    │
    └── subscription (구독. status=ACTIVE 필터 → §5d)

reservation
    │
    ├── user_service  ← 허브: 세차권·구독·결제 모두 연결
    │       ├── service (서비스명)
    │       ├── subscription_id (NULL이면 1회권)
    │       └── coupon_code_reward_id (쿠폰 경로)
    │
    ├── reservation_car → car  (car_id 직접 없음! → §2d)
    │       ├── car_brand (브랜드. car.brand 레거시 → §2d)
    │       └── car_model → car_model_target (타겟 판별 뷰)
    │
    ├── user_address (address_id 경유. 작업주소 스냅샷은 reservation.location → §2e)
    │
    └── wash_result → wash_result_image (사진. status='BEFORE'/'AFTER')
            └── report → report_card (리포트/타이어)
```

---

## 2. 예약 쿼리 필수 패턴

### 2a. 상태 필터

| 목적 | 조건 |
|------|------|
| 세차 완료 | `status IN ('WASHED', 'REPORT_SENT')` |
| 확정 예약(미완료) | `status = 'CONFIRMED'` |
| 유효 예약 전체 | `status IN ('WASHED', 'REPORT_SENT', 'CONFIRMED')` |
| 취소 | `status = 'CANCELED'` (제외할 것) |
| 미확정 | `status = 'CREATED'` (제외할 것) |

⚠️ `NOT IN ('CANCELED')` 사용 금지 — CREATED가 포함되어 데이터 왜곡됨.

⚠️ **`user_service` 경로로 예약을 붙일 때도 status 화이트리스트 필수** — 세차권에 CANCELED 예약이 연결된 채 남는 경우가 실제로 있어(취소 후 반납된 캠페인 세차권 등), status 필터 없는 LEFT JOIN은 취소·미확정 건을 유효 예약처럼 보이게 한다. 표준 패턴:
```sql
LEFT JOIN reservation r ON r.id = us.reservation_id
  AND r.deleted_yn = 0
  AND r.status IN ('WASHED', 'REPORT_SENT', 'CONFIRMED')
```
CONFIRMED가 포함되므로 §2b 유령예약 체크(`car` EXISTS)도 함께 적용.

### 2b. 유령예약 제거 (CONFIRMED 집계 시 필수)

반복구독 고객의 미래 예약은 배치 생성되는데, 세차권(`user_service`)이 없거나 차량(`car`)이 삭제됐거나 **고객이 탈퇴(`app_user.deleted_yn=1`)** 한 상태로 CONFIRMED가 남아 있을 수 있음. 이 유령예약이 집계에 포함되면 과다 카운트.

**예약수 세는 모든 쿼리에 아래 세 조건 추가 필수:**
```sql
AND EXISTS (SELECT 1 FROM user_service us WHERE us.reservation_id = r.id AND us.deleted_yn = 0)
AND EXISTS (SELECT 1 FROM reservation_car rc JOIN car c ON rc.car_id = c.id WHERE rc.reservation_id = r.id AND c.deleted_yn = 0)
AND EXISTS (SELECT 1 FROM app_user u WHERE u.id = r.user_id AND u.deleted_yn = 0)
```

WASHED 완료 건만 세는 쿼리엔 실질적 영향 없음. CONFIRMED 포함 집계에서 특히 중요.

⚠️ **고객 탈퇴 축(`app_user.deleted_yn`)을 빠뜨리기 쉬움.** 디테일러앱/어드민 예약목록 API(caramel-api `careplus-detailer.service.ts`)는 Prisma where에 `user:{deleted_yn:0}`(user 모델=`@@map("app_user")`)를 걸어 **탈퇴 고객 예약을 노출하지 않는다** — 특히 QA 테스트 계정(name='asdf' 등)이 탈퇴 후 CONFIRMED 예약만 잔존하는 경우가 흔함. 세차권·차량만 체크하면 이 유령이 남아 이중예약/충돌 검출에서 오탐(2026-07-21 충돌감사 크론이 김민호 동시각 2건으로 오보 — 1건은 탈퇴고객 유령).

🔴 **탈퇴 고객 유령예약은 "안 보일" 뿐 슬롯은 실제로 점유한다. 그리고 zero 어드민 API로는 못 지운다 (2026-08-06 실측).**
- **점유**: 재배정 후보를 찾을 때 `그 시각 예약 있음`으로 잡혀 멀쩡한 후보를 탈락시킨다(8/7 실사례 — 안용희170 10:00·한승헌218 16:00이 각각 탈퇴 QA 계정 예약에 막혀 있었고, 청담 10:00 대체 후보 산정이 실제로 왜곡됐다). ⟹ **§3c 후보 탐색 쿼리의 `res` CTE에도 위 3조건(특히 `app_user.deleted_yn=0`)을 걸 것.**
- **삭제 경로**: `POST /v1/admin/users/{userId}/reservations/bulk-cancel`(zero-api)은 `assertTargetUserExists`가 먼저 걸려 **404 `고객을 찾을 수 없습니다`** 로 거부된다 — 탈퇴 유저라 어드민 화면·API 어느 쪽으로도 손댈 수 없다. **우회 = sales-admin 레거시** `POST https://gateway-prod.thetrive.com/careplus/reservations-admin/{reservationId}/cancel` `{"reason":"CARAMEL_PROBLEM","detailReason":"…"}` (userId를 안 받아 탈퇴 계정도 통과, HTTP 201 → `[{"id":…}]`). `reason` enum = `CUSTOMER_PROBLEM`(→REFUND)·`CARAMEL_PROBLEM`/`RAIN`(→GIVE_BACK_WITHOUT_CHARGE) 3종뿐이고, **테스트/정리성 취소엔 환불이 걸리지 않는 `CARAMEL_PROBLEM`**을 쓸 것.
- **찾는 쿼리**: `JOIN app_user au ON au.id=r.user_id WHERE au.deleted_yn=1 AND r.deleted_yn=0 AND r.status IN ('CONFIRMED','IN_PROGRESS') AND r.reservation_datetime >= NOW()` — 과거 날짜인데 CONFIRMED로 남은 것도 같이 훑을 것(적재·완료율 분모를 오염시킨다).

### 2b-1. 디테일러엔 보이는데 고객 앱엔 안 보이는 예약 (후불 숨김)

🔴 **`user_service.postpaid_yn=1` 예약은 같은 차에 선불(`postpaid_yn=0`) 대기예약이 하나라도 있으면 고객 앱에서 통째로 숨겨진다 — 날짜를 안 본다 (2026-08-28 실측, caramel-zero origin/main).** 정본 = zero-api `selectVisibleCustomerReservationIds`(`reservation/application/ports/customer-reservation-read.repository.port.ts`), 적용처 = 예약목록·홈(`get-me`)·차고(`get-my-garage`) 3곳. 예약 단위 후불 판정 = 붙은 세차권 중 하나라도 `postpaid_yn=0`이면 선불, 전부 1이면 후불.
- **디테일러앱·어드민 스케줄엔 이 필터가 없다** (caramel-api `careplus-detailer.service.ts`·`detailer.service.ts`에 postpaid 언급 0건). ⟹ **"디테일러 화면과 고객 화면의 예약이 다르다" CS 문의의 1순위 원인.** 둘 다 실재하는 예약이고 고객만 못 보는 것 — 어느 쪽이 "가짜"냐고 묻지 말 것.
- **날짜 무관이 핵심 함정**: 9/18에 선불 예약이 하나 있으면 9/1 후불 예약까지 숨는다. 2026-08-28 기준 다음 7일 7건 중 5건은 같은 날 다른 예약이 아예 없어 **고객은 그날 예약이 있다는 사실 자체를 모르고 디테일러만 방문**한다. 규모: 미래 숨김 1,691건 / 720명.
- ⚠️ **고객 앱의 "유효 세차권" 필터는 `deleted_yn=0` 하나가 아니라 `deleted_yn=0 AND paid_yn=1 AND used_yn=1` 셋이다** (zero `customer-reservation.where.ts` `validReservationVisibilityWhere`). §2b의 집계용 유령 필터보다 좁다 — "고객 화면에 뜨는가"를 재현할 땐 셋 다 걸 것.
- **숨겨진 예약 찾는 쿼리** (고객 앱 로직 재현):
```sql
WITH valid AS (   -- 고객 앱이 후보로 삼는 집합
  SELECT r.id, r.user_id, r.reservation_datetime, MIN(us.postpaid_yn) minp
  FROM reservation r
  JOIN app_user u ON u.id = r.user_id AND u.deleted_yn = 0
  JOIN user_service us ON us.reservation_id = r.id
    AND us.deleted_yn = 0 AND us.paid_yn = 1 AND us.used_yn = 1
  WHERE r.deleted_yn = 0 AND r.status IN ('CONFIRMED','IN_PROGRESS')
    AND EXISTS (SELECT 1 FROM reservation_car rc JOIN car c ON c.id = rc.car_id AND c.deleted_yn = 0
                WHERE rc.reservation_id = r.id)
  GROUP BY r.id, r.user_id, r.reservation_datetime
), rcv AS (
  SELECT rc.reservation_id, rc.car_id FROM reservation_car rc
  JOIN car c ON c.id = rc.car_id AND c.deleted_yn = 0
), prepaid_cars AS (
  SELECT DISTINCT rcv.car_id FROM valid v JOIN rcv ON rcv.reservation_id = v.id WHERE v.minp = 0
)
SELECT DISTINCT v.id            -- 디테일러에겐 보이고 고객에겐 안 보이는 예약
FROM valid v JOIN rcv ON rcv.reservation_id = v.id
WHERE v.minp = 1 AND rcv.car_id IN (SELECT car_id FROM prepaid_cars);
```

### 2c. 완료 시각 vs 예약 시각 (washed_at vs reservation_datetime)

- **`washed_at`**: 실제 세차 완료 처리 시각(UTC). 완료 건 날짜별 집계의 기준 컬럼.
  ```sql
  DATE(CONVERT_TZ(r.washed_at, '+00:00', '+09:00')) AS wash_date
  ```
- **`reservation_datetime`**: 고객이 예약한 시작 시각(UTC). 실제 완료 시각이 아님.
- `washed_at`은 `status IN ('WASHED', 'REPORT_SENT')`일 때만 NOT NULL 보장.
- 🔴 **그 보장이 깨지는 구간이 있다 — 반얀트리 세차는 `washed_at`이 전건 NULL이다 (2026-08-25 실측).** `location REGEXP '장충단로 ?60...'` 건은 **2026-07-20부터** 어드민/작전보드 완료 처리 경로로 바뀌면서 `status='WASHED'`인데 `washed_at`이 안 채워진다: 2026-07 151/169(89.3%) · 2026-08 **215/215(100%)**, 같은 기간 일반 예약은 0.1~0.7%. ⟹ **반얀 세차를 `washed_at` 기준으로 날짜 집계하면 368건이 23건으로 쪼그라들고 "8월 데이터 없음"으로 오판한다.** 반얀(및 어드민 완료 처리 의심 구간)은 `reservation_datetime` 기준으로 셀 것. 0건이 아니라 *줄어든* 형태로 나오므로 눈에 안 띈다.
- **"예약 시점" 일수 측정** (가입→예약 며칠, 당일 예약 비중 등)은 `created_at`(예약을 *잡은* 시각) 기준. `reservation_datetime`(세차 *예정일*)로 재면 왜곡 — "가입 당일 예약" 71%가 14%로 추락한다.

### 2d. 차량 조인 (reservation_car 경유)

`reservation` 테이블엔 차량 FK가 없다. **가장 깔끔한 경로 = `reservation_car`** (reservation_id↔car_id, 거의 1:1):
```sql
JOIN (SELECT reservation_id, MAX(car_id) car_id FROM reservation_car GROUP BY reservation_id) rc
  ON rc.reservation_id = r.id
JOIN car c ON c.id = rc.car_id AND c.deleted_yn = 0
```

대안: `checkup.car_id`(WASHED만), `subscription.represent_car_id`(구독세차 7.5%만). `user_service.applicable_car_id`는 ~60%가 NULL이라 부적합(직관적이라 빠지기 쉬운 함정 — reservation_car는 100% 커버). 검증 2026-06-26.

**⚠️ 외부공급/B2B 더미 차량 = `car.plate_number='00가0000'` (검증 2026-07-29)**
- 활성 83대·소유자 81명, 세차 587건. 차주명이 사람 이름이 아니라 `한남 테슬라`·`마이바흐`·`벤틀리` 같은 **물량 단위 계정**(딜러·제휴 공급).
- 🔴 **차량 단위 집계(차량당 세차 횟수, 기록 깊이, 최다 세차 차량)에서 반드시 제외.** 미제외 시 상위가 통째로 오염된다 — 실측: 최다 357회(한남 테슬라)·123회(마이바흐)·100회(벤틀리) vs **제외 후 실고객 최다 70회**. 총량 영향은 1.4%로 작지만 분포 상단은 치명적.
- `app_user.test_yn`·블랙리스트로는 걸러지지 않는다(정상 고객 계정으로 등록돼 있음). 필터: `IFNULL(c.plate_number,'') <> '00가0000'`.

**⚠️ `reservation.reservation_datetime`에 zero-date 행이 있다 (검증 2026-07-29)**
- `< '2020-01-01'` 또는 NULL인 행 **2,538건**(대부분 CREATED 유령, CANCELED 684건, WASHED는 7건뿐).
- 🔴 `MIN(reservation_datetime)`이 **1970-01-01**을 반환한다 → "서비스 언제부터"·최초 세차 시점 판정이 통째로 틀린다. **실제 최초 = 2024-01-19.**
- 기간 시작점을 뽑을 땐 `WHERE reservation_datetime > '2020-01-01'` 하한을 걸 것. WASHED 기준 손실은 7건으로 무해.

**⚠️ "다차(2대+) 유저" 정의 — 등록 기준 금지**
- 등록 차량(`car` 활성 2대+) 5,913명 vs 실제 2대+ 세차(WASHED) 1,484명 — **등록 기준은 ~4배 과대** (검증 2026-06-26).
- 다차 세그먼트/리텐션 분석의 모수 = `reservation_car` 경유 `COUNT(DISTINCT car_id) >= 2` (WASHED 기준).

**차량 브랜드 필터 — `car.brand`는 레거시 nullable**
- `car.brand`는 레거시 VARCHAR 컬럼으로 NULL인 차량이 존재. `brand IN ('포르쉐','벤츠',...)` 단독으로 쓰면 `brand_id`만 세팅된 차량이 누락됨.
- **올바른 패턴:**
  ```sql
  JOIN car_brand cb ON cb.id = c.brand_id
  WHERE cb.name IN ('포르쉐', '벤츠', 'BMW', ...)
  ```
- ⚠️ 조건 충족 차량이 결과에서 누락되면 첫 의심은 `car.brand` 직접 필터. 쿼리 수정(`JOIN car_brand`)으로 포함.
- **국산차 브랜드 목록** (`car_brand.name` 기준): `'현대', '기아', '제네시스', 'KGM', 'KGM(쌍용)', '르노', '쉐보레', '캠시스', 'GM', '르노삼성', '대우', '삼성'` — 쌍용은 `'KGM(쌍용)'`으로 저장됨 주의.

**차량 모델 조인 — `car.car_model_id` 없음**
- FK 컬럼명은 **`car.model_id`** — `car_model_id`는 존재하지 않아 "Unknown column" 오류 발생.
- 올바른 조인: `LEFT JOIN car_model cm ON c.model_id = cm.id`
- 🔴 **`model_id`가 NULL인 실차가 실존한다** (2026-08 기준 233대·148명, `deleted_yn=0 AND temp_yn=0`). 어드민·디테일러 차량등록 API가 modelId 없이도 INSERT를 허용해서 생긴다. ⟹ **`INNER JOIN car_model`이나 `JOIN car_model_target cmt ON cmt.id=c.model_id`로 티어·타겟을 집계하면 이 차량들이 조용히 분모에서 사라진다.** "차량 수가 안 맞는다" 싶으면 `SUM(c.model_id IS NULL)`을 먼저 세볼 것. 모수 카운트는 `deleted_yn=0 AND temp_yn=0` **둘 다** 필요(제품 코드도 `temp_yn: false`로 필터하므로 이게 실제 영향 모수와 일치).
- 참고: 이 row는 고객 차량목록 API(`/v1/vehicles/garage`·`/v1/my-garage`)를 **404로 깨뜨려 그 고객은 예약을 못 한다**. 발견하면 제품팀에 알릴 것. (마이차고 화면은 `/v1/me`를 써서 정상으로 보이는 비대칭이 있음 — 브랜드 없이 번호판만 뜨는 차량이 시그널.)
- ⚠️ **재측정(2026-09-02): `deleted_yn=0 AND temp_yn=0`으로는 `model_id IS NULL`이 이제 **0대**다.** 남은 NULL 3,276대(전체 3,552 중)는 **전부 `temp_yn=1`**. 위 233대 수치는 2026-08 시점 값이니 인용 전 다시 세라 — 함정 자체(INNER JOIN이 조용히 삼킴)는 temp 차량을 모수에 넣는 쿼리에서 그대로 유효하다.

**차량 표시명(브랜드+모델) 조립 — `CONCAT(cb.name, ' ', c.model)`은 두 군데서 깨진다 (2026-09-02 실측, 전체 173,432대 `deleted_yn=0 AND temp_yn=0`)**
- 🔴 **`car.model`(자유입력)이 비어 있는 차가 69,793대 = 40.2%.** 표시명을 `c.model`만으로 만들면 열에 넷은 브랜드만 남는다. ⟹ **`COALESCE(NULLIF(c.model,''), cm.name)`** 로 `car_model.name` 폴백을 반드시 건다.
- 🔴 **`car.model`에 이미 영문 브랜드가 붙어 있는 차가 17,845대 = 10.3%.** 앞에 `cb.name`을 그대로 붙이면 `BMW BMW X5 xDrive40i`·`벤츠 Mercedes-Benz GLC220 d`가 나온다. 고객에게 나가는 목록(콜리스트·리포트·시트)이면 **한글 브랜드명 ↔ 영문 별칭 매핑으로 접두어 중복을 걷어낼 것**(`벤츠`↔`mercedes`/`mercedes-benz`/`mercedes-amg`/`amg`, `랜드로버`↔`land`/`range` 등). 화면 코드가 하는 정규화를 SQL은 안 해준다.
- 브랜드는 `car.brand_id` 경유가 맞다(`cb.id = c.brand_id`). NULL은 11대뿐이라 `LEFT JOIN` + `COALESCE`로 충분 — `car_model` 경유(`cm.brand_id`)로 우회할 필요 없다.
```sql
TRIM(CONCAT(COALESCE(cb.name,''),' ',COALESCE(NULLIF(c.model,''), cm.name, ''))) AS car_display
-- 뽑은 뒤 '한글브랜드 + 영문브랜드' 중복 접두어를 애플리케이션에서 한 번 더 제거
```

**원부 이력 `car_wonbu_history` — 브랜드 컬럼 없음**
- 있는 컬럼: `plate_number, search_number, mileage, registered_at, final_registered_at, inspection_valid_start_at, inspection_valid_end_at, model, type, vin, car_year, color, form, engine_type, capacity, is_commercial_car, deleted_yn, requested_at, created_at`.
- **`brand` 컬럼은 없다** — 쓰면 "Unknown column 'brand'". 브랜드는 `car.brand_id → car_brand` 경유. 차종 문자열은 `model`, 연식은 `car_year`.
- 조인 키는 `plate_number`(car_id 아님). 번호판이 원부에 아예 없는 차량도 있으니 `LEFT JOIN` + NULL 처리.

**차종 티어(T1~T7) 판정 — `car_model.tier_id` → `car_tier.tier`**
- `car`에도 `car_model`에도 **티어 숫자 컬럼은 없다**. 정본은 `car_tier.tier`(int).
- 전체 경로: `car c JOIN car_model cm ON c.model_id=cm.id JOIN car_tier ct ON cm.tier_id=ct.id` → `ct.tier`. 브랜드까지 붙이려면 `JOIN car_brand cb ON cm.brand_id=cb.id`.
- ⚠️ **`product.product_type='TIER_1'~'TIER_7'`은 "이 상품이 몇 티어용인가"이지 "이 차가 몇 티어인가"가 아니다.** 차종 티어를 product에서 찾으면 헛다리 — 상품 가격표(1회권 정가 등)를 볼 때만 쓴다.
- 티어는 차 크기·작업량 기준(출고가 아님 — 아래 타겟 판별 참조). 실측 예(2026-07): T4=GV70·GLC·X3·S클래스·911·XC60·파나메라·마칸 / T5=GV80·카이엔·X5·GLE·G클래스·레인지로버.

**S·A 타겟(신차 출고가 6,500만↑) 판별 — `car_model_target` 뷰**
- `JOIN car_model_target cmt ON cmt.id = c.model_id WHERE cmt.is_target = 1`
- ❌ `car_brand.target_yn`(수입차 브랜드 21개 단위)은 부정확 — 제네시스 G90·GV80 누락 + BMW 1시리즈·벤츠 A클래스 오포함.
- `car_tier`(T1~T7)도 차 크기 기준이라 출고가 대리변수로 못 씀.
- 뷰 `is_target=1` = 220개 모델(2026-06 기준).

**⚠️ 타겟 "고객" 판별 3패턴 — 패턴에 따라 숫자가 다르다 (세컨카 포함/제외 차이)**
- **패턴 A (세차 건)**: `reservation_car`→`car`→`car_model_target` — 세차한 그 차가 타겟인지. 타겟 유저가 비타겟 세컨카로 세차하면 제외됨.
- **패턴 B (유저)** ✅ **타겟 고객 분석 표준**: 유저가 타겟차 1대+ 보유 → 그 유저의 세차·결제 전부 포함 (비타겟 세컨카 세차도 포함).
  ```sql
  target_users AS (
    SELECT DISTINCT c.user_id FROM car c
    JOIN car_model_target cmt ON cmt.id = c.model_id
    WHERE c.deleted_yn = 0 AND cmt.is_target = 1
  )
  -- 이후 JOIN target_users tu ON tu.user_id = r.user_id
  ```
- **패턴 C (첫차)**: 유저의 첫 등록차가 타겟인지 — 신규 등록 코호트 지표에만.
- 같은 "타겟 비율"이라도 A/B/C 숫자가 다르다 (실측: B 전환 시 고객수 +7%, 침투율 +5%p). **타겟 고객 분석은 B로 통일** (CBR v2 표준). 예외: 침투율 분모=전체 유입(타겟 필터 없음), 타겟차 신규 등록수=패턴 C 유지.

**검사임박(정기검사 만료) 차량 조회 — `car.next_car_inspection_at`**
- ⚠️ **타임존 트랩**: `next_car_inspection_at`은 UTC 저장이나 값이 **KST 자정**이라 raw가 `...15:00:00`로 보임 (예 raw `2026-08-12 15:00:00` = 검사만료 KST `2026-08-13`). **raw로 D±N 윈도우 필터하면 하루/타임존 어긋남** → 반드시 `DATE(CONVERT_TZ(c.next_car_inspection_at,'+00:00','+09:00'))` 후 비교. `last_wonbu_at`(마지막 원부조회 시각)도 UTC 저장 → KST 표기 시 CONVERT_TZ.
- 표준 패턴 (타겟 + 실고객 + D+20~D+30 예시):
  ```sql
  SELECT c.plate_number, cm.name, c.model_year,
    DATE_FORMAT(CONVERT_TZ(c.next_car_inspection_at,'+00:00','+09:00'),'%Y-%m-%d') AS insp_end_kst
  FROM car c
  JOIN car_model_target t ON c.model_id=t.id AND t.is_target=1   -- 타겟
  LEFT JOIN car_model cm ON c.model_id=cm.id
  JOIN app_user u ON c.user_id=u.id
  WHERE c.deleted_yn=0 AND (c.temp_yn IS NULL OR c.temp_yn=0)      -- 실차
    AND u.test_yn=0 AND u.phone IS NOT NULL                        -- 실고객
    AND DATE(CONVERT_TZ(c.next_car_inspection_at,'+00:00','+09:00'))
        BETWEEN DATE_ADD(CURDATE(),INTERVAL 20 DAY) AND DATE_ADD(CURDATE(),INTERVAL 30 DAY)
  ```
- **원부 배치 재조회는 `next_car_inspection_at`을 갱신한다** (`curl -sG gateway-prod.thetrive.com/careplus/wonbu --data-urlencode carPlateNumber=<판>`, 무인증, 건당 `#caramel_원부조회` 채널 게시). ⟹ 재조회 후 검사완료 차량은 만료일이 미래로 밀려 **윈도우 밖으로 이동**하므로, "재조회 → 같은 윈도우 재쿼리" 시 대상 수가 줄어드는 게 정상. 임시/말소 번호판은 HTTP 500(원부서버 미반환).

### 2e. 주소/좌표 COALESCE 패턴

`reservation.latitude/longitude`는 @deprecated (Prisma 스키마 2026-04-28~). 신규 예약은 `user_address`에만 좌표가 있을 수 있음. Zone 매핑·좌표 쿼리는 반드시 COALESCE:
```sql
JOIN user_address ua ON ua.id = r.address_id
LEFT JOIN zone z ON ST_Contains(z.area,
  ST_GeomFromText(CONCAT('POINT(',
    COALESCE(r.longitude, ua.longitude), ' ',
    COALESCE(r.latitude, ua.latitude), ')'), 0))
```

`r.latitude` 단독 사용 시 신규 예약이 NULL로 처리돼 zone 매핑에서 누락됨.

**`reservation.location` 이중 의미 — 반드시 구분해서 사용**
- `reservation.location` = **작업주소 텍스트 스냅샷** (업무용 주소 문자열, 디테일러 앱·알림이 읽음)
  - geometry가 **아니라 text** 타입이며 인코딩이 깨져 있음 (`ST_AsText`/`ST_SRID` 시도 시 `Geometry byte string must be little endian` 오류)
  - 업무 주소로는 쓰되, 좌표 파싱 불가
- **위치 파악용 좌표** = `latitude`/`longitude` (decimal(11,8), deprecated → COALESCE with `user_address`)

🔴 **현장(반얀·천호) 예약을 주소로 셀 때 `r.location`과 `user_address.address`가 갈린다 — 정본은 `user_address` 쪽이다 (2026-08-26 실측).** 같은 엄격 regex로 세면 반얀이 **`r.location` 2,242 vs `ua.address` 2,249**(불일치 9건, 0.4%). 양방향으로 어긋난다:
- **8건** = `r.location`은 고객의 옛 집주소 스냅샷이고 `ua.address`가 장충단로 60 — 주소 레코드를 **사후에 현장 주소로 편집**한 케이스(#13953·#30091·#52081·#52082·#70133·#70138·#70140 등)
- **1건** = 반대 방향(#94233, 2026-08-25). `r.location`은 장충단로 60인데 `ua.address`는 압구정으로 편집됨 ⟹ **`user_address`도 불변 기록이 아니다.** 과거 세차 이력을 주소로 재구성하면 시점에 따라 답이 바뀐다
- **1건** = `r.location`에 오타 `장충잔로 60`(#33214). regex가 못 잡는데 `ua.address`는 정상

⟹ **코드와 같은 답을 원하면 `user_address` 를 조인한다** — zero 가 `row.user_address?.address ?? row.location` 순서로 읽는다(`prisma-reservation-facts.repository.ts`). `r.location` 단독으로 세면 0.4% 적게 나오고 **0건이 아니라 줄어든 형태**라 눈에 안 띈다. 문서 다른 곳의 `location REGEXP '장충단로 ?60...'` 서술은 근사값이다.

⚠️ **MySQL REGEXP 에서 `\s` 는 안 통한다** — POSIX 클래스를 쓴다. 코드의 lookahead 경계
`(^|\s)장충단로\s*60(?=$|[\s,(])` 를 SQL 로 옮기면
`'(^|[[:space:]])장충단로[[:space:]]*60($|[[:space:]),])'`. 경계를 빼고 `LIKE '%장충단로 60%'` 로 세면 `장충단로 600`·`천호대로 1005번길` 같은 **이웃 주소가 샌다**(둘 다 실재).

⚠️ **현장 주소 매칭은 소급 적용된다.** 현장을 새로 추가하면 그 주소의 **과거 예약이 통째로 현장 예약이 된다** — 천호는 시연(8/31) 전인데 이미 83건이 잡힌다(2026-04 옛 천호 행사 82건). "현장 오픈 이후"를 보려면 기간을 직접 자를 것.

**일회성 작업지 변경(이번 건만 다른 주소):**
- `reservation`의 `location`/`detailed_location`/`latitude`/`longitude` 4필드만 UPDATE.
- `address_id`·`user_address`는 건드리지 말 것 — 고객 저장 주소(집)를 고치면 향후 예약까지 오염.
- 새 좌표: NAVER 지오코딩 `GET https://naveropenapi.apigw.ntruss.com/map-geocode/v2/geocode?query=<주소>`, 헤더 `x-ncp-apigw-api-key-id` / `x-ncp-apigw-api-key` (키: caramel-zero `.env.dev`의 `NAVER_MAPS_GEOCODE_CLIENT_ID/SECRET`). 응답 `addresses[0].x`=lng, `.y`=lat.

**건물·단지 단위 집계 — `apartment_yn`으로 오피스 오염을 제거한다 (2026-08-04)**

`user_address.building_name` GROUP BY로 "어느 단지에 우리 고객이 사는가"를 뽑으면 **서울오토갤러리(딜러 상사)·캐피탈타워·한국지식재산센터 같은 오피스가 섞인다.** 이름으로 수동 배제하지 말 것 — `apartment_yn = 1` 한 컬럼으로 전량 걸러진다(실측: 강남3구+용산 상위 120개 단지에서 오염 0건).

```sql
FROM user_address ua
WHERE ua.sigungu IN ('강남구','서초구','송파구','용산구')
  AND ua.apartment_yn = 1
  AND ua.deleted_yn = 0
GROUP BY ua.building_name, ua.sigungu
HAVING COUNT(DISTINCT ua.user_id) >= 5   -- 오탈자성 1~2건 단지 제거
```
- **좌표는 `user_address`에 내장돼 있다** — 단지 지도·지오코딩이 필요하면 `ua.latitude`/`ua.longitude`를 그대로 쓴다. 외부 지오코딩 API를 다시 태울 필요 없음(실측 좌표 확보율 120/120 = 100%).
- 타겟 차량 수는 `reservation_car`→`car`→`car_model_target` 경유(§2d 패턴 A). 단지별 세차 건수는 재현되지만 **고객 수는 DB가 누적이라 과거 기록값보다 계속 커진다** — 시점을 병기할 것.

🔴 **단지별 "고객 수"는 정의가 3갈래고 결과가 단지당 2~8배 갈린다 (2026-08-10 실측).** 위 스니펫은 ①이다. **어느 정의인지 안 밝힌 단지별 고객 수·침투율은 재현되지 않는다.**

| | 정의 | 조인 | 강남3구+용산·5명+ 컷 |
|---|---|---|---|
| ① | **주소를 등록한 유저** (세차 이력 무관) | `user_address` GROUP BY | **393곳 / 7,343명** |
| ② | **그 주소에서 세차를 완료한 유저** | `reservation.address_id = ua.id` + `status IN ('WASHED','REPORT_SENT')` | **145곳 / 1,724명** |
| ③ | **그 단지 거주자 중 세차 경험자** (세차 장소는 무관) | `EXISTS (SELECT 1 FROM reservation r WHERE r.user_id=ua.user_id AND r.status IN (...))` | **158곳 / 1,952명** |

- 개별 단지 편차가 크다: 디에이치퍼스티어아이파크 ①217 / ②80 / ③87, **반포자이아파트 ①226 / ②27 / ③38(8.4배)**. 주소만 등록하고 세차는 안 한 유저가 단지마다 다른 비율로 깔려 있어서다.
- "이 단지에 우리 고객이 N명 있다"는 대외 문구는 ③, "이 단지에서 세차가 돌고 있다"는 ②가 맞다. **①을 침투율 분자로 쓰면 과대**다.
- 🔴 **세차 건수가 늘었는데 고객 수가 기록값보다 작아지면 시점 문제가 아니라 정의가 다른 것이다.** 실제로 2026-07 기록(121곳/2,666명/4,954건)을 2026-08에 ②로 재현하니 5,681건인데 1,724명이 나왔다 — 건수는 늘고 고객은 줄었으므로 그 기록값은 ②가 아니다.

⚠️ **`building_name`은 표기가 갈린다 — 정확 일치도 LIKE도 둘 다 틀린다 (2026-08-10 실측).**
- 같은 단지가 쪼개진다: `송파 레이크파크 호반써밋Ⅱ`(124명) / `송파 레이크파크 호반써밋 Ⅰ`(40명) — **로마숫자 + 공백 유무**.
- LIKE로 묶으면 다른 단지가 섞인다: `%반포자이%` → `반포자이아파트`·`신반포자이`·`반포자이플라자`(별개 건물) / `%퍼스티어%` → `강서금호어울림퍼스티어`(강서구) / `%레이크파크%` → `청라더샵레이크파크`·`광교 더샵 레이크파크`.
- ⟹ 특정 단지를 지목하는 쿼리는 **`sigungu` + `building_name` 쌍으로 좁히고, 후보를 먼저 `GROUP BY building_name`으로 눈으로 확인**한 뒤 IN 리스트를 확정할 것.

### 2f. Zone 매핑 (ST_Contains)

- `reservation`엔 `zone_id` 없음. `zone.area`는 polygon(SRID 0, 좌표 순서 **(lng lat)**)
- 경로: reservation → COALESCE 좌표(§2e) → `ST_Contains(zone.area, POINT(lng lat))`
- 어느 zone에도 안 들어가면 z가 NULL → 미커버/이탈 후보
- ⚠️ **거리 계산도 SRID 0으로 통일할 것.** `ST_Distance_Sphere`에 SRID **4326**을 쓰면 `Latitude out of range` 에러로 죽는다(4326은 **위도-경도** 순서를 강제하는데 우리 좌표는 경도-위도). polygon과 축을 맞춰 **SRID 0 + `POINT(경도 위도)`**:
  ```sql
  ST_Distance_Sphere(ST_GeomFromText('POINT(127.0097435 37.5500494)',0),
                     ST_GeomFromText(CONCAT('POINT(',lng,' ',lat,')'),0)) / 1000  -- km
  ```
- ⚠️ **zone 폴리곤이 아예 없는 구가 있다**(중구·광진·양천·동작·관악·종로 등). 그 주소는 최근접 fallback으로 엉뚱한 zone에 떨어진다(예: 중구 신당동 → Z9). **"존 외"로 잡힌 건이 실은 폴리곤 공백 산물일 수 있으니, 존 일치를 목표로 삼기 전에 실거리부터 볼 것.**
- ⚠️ **반대로 폴리곤이 행정구역을 넘어 과하게 뻗은 경우도 있다.** 실측(2026-07-26): **성남시 중원구 여수동(127.12757, 37.41799)은 `ST_Contains`상 Z16(강동/송파) 단독 포함**이고 Z0(성남)은 미포함(convex hull에만 걸림). ⟹ **성남 예약이 Z16 담당자에게 붙는 것은 시스템상 "정상"**이다. 행정구역명과 zone 이름이 안 맞는다고 곧바로 오배정을 선언하지 말고 `ST_Contains`로 실판정할 것.

- 🛑 **아래 "공동구역은 `zone` 테이블에 없다"는 2026-09-08 체계 전환으로 폐기됐다** — 공용존은 `zone` id 14~19(`kind='CELL_SHARED'`)에 폴리곤으로 들어와 있다. 세대 필터·셀 매핑·셔플 제외는 §3b 존 체계 항목을 볼 것. 아래 문단은 8월 이전 데이터를 볼 때만 유효하다.
- 🔴 **"공동구역"은 `zone` 테이블에 없다 (2026-08-20).** 셀장 6명 체제의 공동구역(강남·서초 중심에 용산·송파 일부)은 `zone6_areas.geojson`의 `properties.code='BELT'` MultiPolygon이 정본이고, 점-내부 판정을 애플리케이션에서 해야 한다(ray casting, `area` 대신 geojson 좌표 순서 **(lng, lat)**). Z12·Z3·Z5로 근사하면 경계가 달라 물량이 안 맞는다. 같은 파일에 팀존 Z1~Z6·서브존 SZ*도 들어 있어 `zone` 테이블의 Z0~Z17과 **이름이 겹치지만 다른 체계**다 — 섞지 말 것. 팀 배포물에는 없는 파일이라 없으면 요청할 것.
- 🔴 **세차 "건수"를 영업 대상 "고객 수"로 보고하지 말 것 (2026-08-20 실측).** 기존 고객은 한 달에 여러 번 받으므로 두 숫자가 1.6배 벌어진다. 공동구역 타겟 실측(2026-04~07) = **기존 고객 1인당 월 1.55~1.83건**, 신규는 정의상 1인 1건. 9월 추산도 건수로는 508건인데 사람으로는 **322명**(신규 51 + 기존 271)이었다. "대상이 몇 명이냐"는 질문에 건수를 답하면 영업 물량이 통째로 부풀려진다. ⟹ 모수 질문에는 `COUNT(DISTINCT (월, user_id))`로 세고, **현장 단위까지 환산해 끝낸다**(322명을 디테일러 12명이 20 운영일에 → 1인당 하루 1.5명). ⚠️ 월별 distinct 유저는 월끼리 더하면 중복되니 **월 평균으로 내고 운영일 계수로 보정**할 것.
- ⚠️ **일평균의 분모는 "실제 세차가 발생한 distinct 날짜"로 세라.** 달력 영업일로 나누면 공휴일·전사 셧다운(2026-07-17 전국 4건)이 분모에 들어가 일평균이 낮게 나온다.

### 2g. source_type 능동/자동 구분

- **고객 직접(능동)**: `source_type IS NULL OR source_type = 'CUSTOMER_DIRECT'` — 대부분의 고객 예약 (NULL이 절대다수, 약 85%)
- ⚠️ **`source_type IS NULL`을 곧바로 "고객 직접"으로 쓰면 안 된다 (2026-08-06 실측, DS-1830).** `booked_online_yn`으로 갈린다.
  - NULL + `booked_online_yn=0` = **어드민 생성** (2026-06-01~ 541건, 7월 change_log 389건 전부 `ADMIN_RESERVATION_CREATED`).
  - NULL + `booked_online_yn=1` = 5,841건(동기간). `*_RESERVATION_CREATED` change_log가 없는 경로라 **능동으로 단정 금지.**
  - ⚠️ **`CUSTOMER_DIRECT` + `booked_online_yn=1`도 "고객이 직접 골랐다"의 증거가 아니다.** 빙의(`POST /users-admin/{id}/access-token`)와 전화 안내가 같은 지문을 만든다. 고객 자발성 판정은 **선행 이벤트(세차권 발급 등)와의 시간차**로 — 수분 이내면 CS 오케스트레이션이다.
- **제외 대상(자동/관리자)**:
  - `CHECKOUT_SETTLEMENT`: 소량만 이 값을 갖는다. 🔴 **구독 자동예약을 이 값으로 세지 말 것 (2026-09-10 코드+실측).** 자동예약 생성 코드(caramel-api `subscription-renewal.service.ts` `createAutoReservations`)가 템플릿에서 `source_type`을 지우고 다시 넣지 않아 **NULL로 저장돼 왔다.** 고객 직접 예약도 NULL이라 둘을 구분할 수 없었다. 🔵 **2026-09-10 prod 배포(caramel-api #562, main `44c0f83`)부터 자동예약은 `source_type='SUBSCRIPTION_AUTO'`로 찍힌다. 그 이전에 만들어진 행은 영원히 NULL이므로, 과거 구간과 이후 구간을 한 쿼리로 세면 경계에서 갈린다.** 과거 구간을 세려면 아래 근사를 쓴다. 실측(2026-09-09 미래 확정예약 5,225건): NULL 4,488 · CUSTOMER_DIRECT 632 · CHECKOUT_SETTLEMENT **70** · ONBOARDING_V3 35. 자동예약을 세려면 `source_type` 대신 **리드타임**(자동=평균 46일, 고객 직접=8.6일)이나 `user_service.subscription_id` 유무로 근사할 것
  - `RAIN_RETOUCH`: 비 오는 날 재세차 자동 배정
  - `MANUAL_EVENT_IMPORT`: 관리자 수동 입력
- ⚠️ `reserved_with_date` 컬럼은 레거시 — 능동/자동 구분에 사용 불가. 실제 분포: 0=~12500건, 1=98건뿐.

### 2h. "중복 예약" 신고 진단 — 신고된 날짜/시각으로 좁혀 검색하지 말 것

디테일러/CS가 "N시·M시 중복 예약"이라 신고해도 **그 날짜에 해당 시각 예약이 아예 없을 수 있음** (2026-07-19 실사례: "4시·6시 중복" 신고 → 당일엔 18시 1건뿐, 실체는 7/31 18시 + 8/3 16시).

- 🔴 **`created_at`으로 원인을 판정하지 말 것 (2026-08-03 오진, 2026-09-01 재확인).** "두 건의 `created_at`이 초까지 같다 = 한 배치가 붕괴시켰다"는 **틀렸다**. `reservation_datetime`은 리스케줄로 덮어써지므로 `created_at`은 생성 시점만 말할 뿐 **그때 어느 날짜에 잡혔는지는 말하지 않는다.** 전수 판정 2회 모두 **배치 기원 중복 0건**이었다(8/3 미래 32그룹 · 9/1 하루차이 15쌍 전부 고객 리스케줄).
- **판정 정본 = `reservation_change_log`.** 각 예약의 `RESERVATION_DATETIME_CHANGED` row에서 `from/toReservationDatetime`을 읽어 **마지막으로 옮긴 쪽이 어디서 왔는지**를 본다. 로그가 한쪽에라도 있으면 고객 기원이다.
```sql
SELECT reservation_id, LEFT(JSON_UNQUOTE(JSON_EXTRACT(data,'$.fromReservationDatetime')),16) f,
       LEFT(JSON_UNQUOTE(JSON_EXTRACT(data,'$.toReservationDatetime')),16) t,
       JSON_UNQUOTE(JSON_EXTRACT(data,'$.reason')) reason, actor_type,
       DATE_FORMAT(CONVERT_TZ(created_at,'+00:00','+09:00'),'%m-%d %H:%i') at_kst
FROM reservation_change_log WHERE reservation_id IN (...) ORDER BY reservation_id, id;
```
- **간격별 결말 기준선** (2026-02~08 실측. `CREATED`·데모차·테스트계정·리터치 제외, 세차권이 붙었던 예약만, 자기조인 중복 제거):

  | 간격 | 쌍 | 둘 다 WASHED | 한쪽 취소 | 둘 다 취소 |
  |---|---|---|---|---|
  | 0일(같은 날) | 1,312 | **1.4%** | 65.0% | 32.8% |
  | 1일 | 781 | **2.6%** | 69.3% | 27.5% |
  | 2일 | 500 | 6.0% | 67.2% | 26.2% |
  | 3일 | 482 | 6.4% | 63.7% | 29.5% |

  ⟹ **하루 차이는 2~3일 쪽이 아니라 같은 날 쪽에 붙어 있다** (2~3일에서 2배 넘게 꺾인다). 97%가 결국 하나로 정리되지만 **공짜가 아니다 — 6~8월 199건 중 32%가 세차 3일 이내, 17%가 하루 전·당일에 취소된다.** 그만큼 슬롯이 잠기고 동선이 그 세차를 낀 채 짜인다.
- 🔴 **`user_service`에 `deleted_yn=0 AND paid_yn=1 AND used_yn=1`을 걸고 과거 결말을 세지 말 것 (2026-09-01 실사고).** 예약을 취소하면 세차권이 `deleted_yn=1`이 되므로 **이 필터는 취소 건을 표본에서 통째로 지운다.** 같은 질문에 "하루 차이 둘 다 세차 47.6%"라는 정반대 답이 나왔다(실제 2.6%). 이 필터는 **지금 살아있는 예약을 고를 때만** 쓰고, 결말 분석에는 `EXISTS (SELECT 1 FROM user_service WHERE reservation_id=r.id)`(삭제분 포함)로 바꿀 것.
- ⚠️ **자기조인 쌍 세기: `y.id <> x.id`를 빼먹으면 gap=0에서 예약이 자기 자신과 매칭돼 건수가 20배로 부풀고 "98.6% 둘 다 세차"가 나온다.** gap≥1은 `y.d > x.d`라 무해하지만 0을 범위에 넣는 순간 터진다.
- 🔴 **"구독 종료 후 잔여 소진이라 둘 다 원하는 것"은 틀렸다 (2026-09-01 반증).** 하루 차이 쌍을 구독 상태로 갈라보면 구독 유효 533쌍 2.3% vs **구독 종료 후 126쌍 0.0%** — 만료 임박 소진 유형이 오히려 **가장 확실하게 하나가 죽는다.** 몰아 잡아두고 결국 버린다. `ended_at`을 "취소하지 말 근거"로 쓰지 말 것. 조인은 `user_service`(reservation_id ↔ subscription_id) 경유 — `reservation.subscription_id`는 전부 NULL.
- 처리는 어드민 API `bulk-cancel` + `ticketAction: GIVE_BACK`(세차권 반환, §5c 재발급 메커니즘 참고). DB 직접 UPDATE 금지.

#### 알림 슬랙 숫자를 재현할 때 — 크론 필터를 그대로 복제하지 않으면 "숫자만 같고 구성이 다른" 답이 나온다

`#caramel_세차신청_알림`의 `:broom: 같은 날 중복 예약 자동 정리`가 말하는 건수를 검산할 때, 대충 짠 쿼리로도 우연히 같은 숫자가 나올 수 있다(2026-09-01 실사례: 15 vs 15인데 구성이 2건 달랐다). 스캔 조건 정본은 zero `prisma-reservation-anomaly-scan.repository.ts`이고, 빼먹기 쉬운 것:

- `status IN ('CONFIRMED','IN_PROGRESS')` — `CREATED`·`RESERVED` 아님
- `user_service`에 `deleted_yn=0 AND paid_yn=1 AND used_yn=1`인 row가 **반드시 있어야** 함(무티켓 예약 제외)
- 테스트 계정 전화번호 4개 제외: `01000000000`·`01000010001`·`01010000000`·`01012345678`
- **리터치 제외**: `NOT EXISTS (SELECT 1 FROM reservation_retouch rt WHERE rt.retouch_reservation_id = r.id)` — 리터치는 원 예약과 같은 날 잡혀도 정상
- 차량이 2대 이상 붙은 예약 제외(살아있는 `car`만 세어 1대인 것만)
- 스캔 창 = 오늘+1일 ~ 오늘+10일 KST
- 🔴 **슬랙 문구 "예약이 N건"은 실제로 N**쌍**이다** — `countNextDayDuplicates`가 차량별 인접일 **전이**를 세므로 예약 건수는 그 2배.

### 2i. "세차 시작 오조작" 되돌리기 = 3행 세트. status만 바꾸면 재시작이 안 된다 (2026-08-07 실측)

디테일러가 실수로 "세차 시작"을 누른 예약을 세차 전으로 돌릴 때. `POST /v1/detailer/reservations/:id/start`(zero `prisma-detailer-reservation.repository.ts startReservation`)는 한 트랜잭션에서 **①`reservation.status='IN_PROGRESS'` ②`reservation_status_log` 1행 ③`wash_result` 1행 ④`checkup` 1행**을 만든다.

- 🔴 **`startReservation`은 살아있는 `wash_result` + `checkup`이 둘 다 있으면 그걸 그대로 반환하고 status를 건드리지 않고 조기 리턴한다.** 그래서 status만 `CONFIRMED`로 되돌리면 **디테일러가 버튼을 다시 눌러도 IN_PROGRESS로 안 돌아온다**. 디테일러앱도 `!washResult && status !== 'IN_PROGRESS'`일 때만 시작 모달을 띄우고(`ReservationDetailScreen.tsx`) 아니면 세차 화면으로 직행.
- 🔴 **soft delete로는 안 된다 — `checkup.reservation_id`가 UNIQUE(`checkup_reservation_id_key`)다.** 코드는 `checkup.deleted_at`을 찍고 같은 예약으로 `create`를 시도하므로 **soft delete된 checkup이 남아 있으면 재시작이 UNIQUE 위반으로 터진다**(코드 버그. `wash_result.reservation_id`는 일반 KEY라 중복 허용). ⟹ **되돌리기는 실제 `DELETE`.**
  ```sql
  DELETE FROM checkup     WHERE id=?;   -- 자식 checkup_detail 0건 확인 후
  DELETE FROM wash_result WHERE id=?;   -- 자식 wash_result_image/audio/report/wcc 0건 확인 후
  UPDATE reservation SET status='CONFIRMED' WHERE id=? AND status='IN_PROGRESS';
  INSERT INTO reservation_status_log (reservation_id, status) VALUES (?,'CONFIRMED');
  ```
  어드민 `PATCH /v1/admin/users/:userId/reservations/:id`는 status만 바꾸므로 **단독으로는 반쪽 조치**.
- ⚠️ **`reservation_status_log`에 손으로 INSERT하면 tz가 어긋난다.** 이 테이블은 Prisma가 `created_at`/`modified_at`을 둘 다 **UTC로 명시 세팅**하는데, 컬럼 DEFAULT `CURRENT_TIMESTAMP`는 서버 tz(KST)라 raw INSERT는 형제 행보다 **9시간 미래**로 박힌다. INSERT 후 `SET created_at=DATE_SUB(created_at, INTERVAL 9 HOUR), modified_at=created_at`으로 맞출 것(같은 UPDATE 안에서 `modified_at`을 `DATE_SUB(created_at,...)`으로 쓰면 이미 갱신된 created_at을 참조해 **또 −9h** 되므로 두 문장으로 나눠라).
- 진행분 확인 후 실행: `wash_result.status`가 초기값(`CHECKUP/TIRE/DRIVER_FRONT`)이고 `wash_result_image` 0장이면 잃을 데이터 없음. 사진이 있으면 이미 진행된 것이므로 되돌리기 전에 확인.
```sql
SELECT r.status, wr.id wr_id, wr.status wr_status, wr.deleted_yn,
       ch.id ch_id, ch.deleted_at, (SELECT COUNT(*) FROM wash_result_image WHERE wash_result_id=wr.id) imgs
FROM reservation r
LEFT JOIN wash_result wr ON wr.reservation_id=r.id AND wr.deleted_yn=0
LEFT JOIN checkup ch ON ch.reservation_id=r.id AND ch.deleted_at IS NULL
WHERE r.id=?;
```
- 시작 시각 지문 = `wash_result.created_at`(=`checkup.created_at`, 초까지 동일). `reservation_change_log`엔 **IN_PROGRESS 전환이 안 남는다** — 상태 이력은 `reservation_status_log`를 봐야 한다.

---

## 3. 디테일러 쿼리 필수 패턴

### 3a. Active 4조건 + 테스터 제외

**코드 기준 active 4조건**: `booking_yn=1, retired_yn=0, deleted_yn=0, direct_yn=1`

테스터 제외:
- `detailer.name != '이상민'` (테스트 계정)
- `detailer.id != 159` (성지원, supply_sheet 미등록 테스터)

참고: `detailer_supply_sheet.status = '현직'`은 40명, Grafana 기준(위 조건)은 **65명**(2026-08-07 실측, 구 문서값 60명은 stale) — **Grafana 기준 사용할 것**

🔴 **`detailer_supply_sheet`는 dev에 0행이다 — INNER JOIN 하면 dev 결과가 통째로 0이 된다 (2026-08-26 실측).** prod 149행 / **dev 0행**(`detailer_supply_load_log`도 비어 있다 — 구글 시트 동기화가 dev엔 안 돈다). `JOIN detailer_supply_sheet` 로 "현직" 필터를 걸면 dev에서 **에러 없이 0건**이 나오고 "현직 디테일러가 없다"로 오독한다(§0의 dev/prod 괴리 함정과 같은 유형).
- ⟹ 로스터 필터는 **LEFT JOIN + 명시적 비현직만 제외**: `(ss.status IS NULL OR ss.status='현직')`. 미매칭·미동기화가 조용히 사람을 지우지 않는다. prod 실측: 예약가능 63명 → 비현직 7명 제외 = **56명**(INNER JOIN이면 54명, 미매칭 2명 소실).
- ⚠️ **prod에만 있는 컬럼이 6개** — `cell_name`·`position`·`region`·`level0_seminar`·`source_sheet_id`·`source_sheet_tab`. dev엔 없어서 `Unknown column`으로 죽는다. **prisma 스키마엔 6개 전부 없으므로 raw query 전용**이고, 코드에서 쓸 때는 컬럼 부재 폴백을 둬야 한다(`prisma-detailer-schedule.repository.ts`의 `findDetailerSupplyRows` 선례).
- `region`(고정샵 `'오토랩'` 판별)은 **prod 현직 기준 해당자 0명**이라 지금은 필터 효과가 없다 — 없다고 놀라지 말 것.

🔴 **"디테일러 몇 명"은 층이 3개다. 숫자만 쓰면 재현이 안 된다 (2026-08-07 실측).** 같은 날 같은 DB에서:

| 층 | 조건 | 인원 |
|---|---|---|
| 미퇴사 | `deleted_yn=0 AND retired_yn=0` | 129명 |
| **예약 가능 (§3a 4조건)** | + `booking_yn=1 AND direct_yn=1` | **65명** |
| **구역 배정 (§3a ∩ §3b 체인)** | + 현재 유효 `work_schedule` × `rule.zone_id` | **53명** (13개 구역) |

- **외부(IR·팀 공유)에 "직영 디테일러 N명"으로 쓰는 값은 구역 배정 층**이다 — 실제로 고객 차를 받는 사람 수이고, 구역별 명단·사진과 개수가 맞는 유일한 층. IR 덱의 "62명"은 **어느 층으로도 재현되지 않아** 폐기했다(2026-08-07).
- `direct_yn=1`을 빼면 66명이 된다. 차이 1명은 구역 배정이 없어서 **구역 배정 층에는 영향이 없다** — 그래서 direct_yn 누락은 조용히 통과한다. 인원 수를 보고할 땐 4조건을 다 걸고 층을 명시할 것.
- 층별 인원은 계속 바뀐다. **숫자를 인용하지 말고 층 정의를 인용하고 매번 재측정한다.**

`detailer.profile_image` = 프로필 사진 URL(전원 보유 수준). 호스트가 `cdn.thetrive.com`과 `trive-attachment.s3...` 두 갈래이고 **S3 쪽은 경로에 한글이 percent-encoding**되어 있다 — 내려받을 땐 `urllib.parse.quote(url, safe=':/')`로 감싸야 404가 안 난다.

**파견 디테일러**: `supply_sheet.status='파견'` 기준 (현재 7명).

🔴 **파견자는 `detailer_work_schedule` 행은 있고 `detailer_work_schedule_rule` 은 없다 — 조인 깊이에 따라 7명이 0명이 된다 (2026-08-21 실측: 유효 스케줄 7/7, 유효 룰 0/7).** 같은 "근무 있음"을 물어도
- `JOIN detailer_work_schedule` 까지만 → 파견자 **잡힌다**
- `+ JOIN detailer_work_schedule_rule` → 파견자 **전원 빠진다**

⟹ 룰까지 INNER JOIN 하는 소스는 파견자를 **구조적으로 못 본다.** 실제로 `/v1/admin/scheduling/slot-demand-map`(그날 근무자 목록)이 그 형태라 파견자가 응답에 없다. "근무 중인 사람" 명단을 이런 소스로만 만들면 파견 7명이 조용히 사라지고, 반대로 스케줄 기반 공백 감사에 넣으면 파견자가 **상시 공백으로 오탐**된다. 명단은 `supply_sheet.status IN ('현직','파견')` 로 잡고, 공백 판정에서는 파견자를 따로 다뤄야 한다.
- capacity 쿼리, "근무 디테일러수" 메트릭 모두 UNION으로 합산 (= 워크 발생 디테일러 ∪ 현재 파견자)
- `supply_sheet`에 status 변경 이력 없음 — 파견↔현직 전환이 발생하면 12개월 시계열 전체가 retroactive하게 변동됨.

**셀(cell) 소속 — `detailer_supply_sheet.cell_name` (⚠️ zone과 다른 축)**:
- 셀장 판별 = `position='셀장' AND status='현직'`. 셀원 = `cell_name = <셀장 이름>` (같은 status 조건).
- ⚠️ **`status='현직'`만 걸면 파견 셀장이 조용히 빠진다** (2026-08-14 실측: 셀장 현직 6 + **파견 2** = 8, 8명 모두 §3a 활성 65명 안에 있다). 코드 선례(`prisma-auth.repository.ts`·`prisma-admin-wow-flow.repository.ts`)는 전부 현직만 잡는다 — 그게 맞는 건 "지금 그 존을 맡은 셀장" 판정일 때뿐이다. **공지·알림처럼 "셀장 전원에게 도달"이 목적이면 `status IN ('현직','파견')`**. 목적을 안 정하고 선례를 복사하면 2명이 영구 누락된다.
- ⚠️ **한 셀이 여러 zone(region)에 걸친다.** cell_name ≠ region/zone. 예: 이승원 셀 = Z1·Z6·Z17 3개 zone에 멤버 분산. 셀장 본인 `region`(Z1)은 셀장이 일하는 zone 하나일 뿐 셀 커버리지가 아님.
- 셀원 명단: `WHERE cell_name='<셀장>' AND status='현직'` — zone/region으로 셀을 재구성하려 하면 누락(타 zone 셀원)+혼입(같은 zone 타 셀원)이 동시에 발생.
- ⚠️ **`position` 화이트리스트로 셀 구성원을 뽑지 말 것.** 실측 값 분포(2026-08-17)는 `셀원` 60 · `''`(빈문자) 50 · `교육생` 21 · NULL 9 · `셀장` 8 · `팀장` 1 이라, 흔히 쓰는 `position IN ('셀장','셀원')`은 **교육생·팀장을 조용히 배제한다**(교육생은 장태훈·이순행 셀에 실재). "그 셀장의 사람 전원"이 목적이면 **같은 `cell_name`만** 걸어라. 빈문자·NULL row는 `cell_name`이 없어 어차피 안 붙는다.
- 파견 셀(오토랩·헤이딜러·인천테슬라 등)은 region이 Z코드가 아닌 텍스트.
- 🔴 **셀장 정본이 둘이다 (2026-09-04).** 위 `supply_sheet.position='셀장'`은 HR 로스터 기준이고, **앱·zero-api가 쓰는 셀장 권한(`/v1/detailer/me`의 `cellAdminAvailable`, 셀 스케줄 화면, 동행·컨시어지 지정)은 `cell_membership` 테이블만 본다.** 판정 = `role='LEADER' AND deleted_at IS NULL AND effective_from <= UTC_TIMESTAMP() AND (effective_to IS NULL OR effective_to > UTC_TIMESTAMP())` (from 포함·to 미포함, UTC 저장). prod에는 셀 6개(`cell.code` Z1~Z6)·멤버십 50행이 **2026-09-08 00:00 KST부터 유효**하게 들어가 있다 — 그 전엔 활성 LEADER 0이라 "앱 셀장 없음"이 정답이다. "앱에서 셀장 메뉴가 안 보인다"는 이 테이블을, "누가 셀장인가(인사)"는 supply_sheet를 봐라. 두 결과가 다르면 둘 다 맞을 수 있다.
- ⚠️ **`role='LEADER'`로 먼저 거르면 옛 셀장이 섞인다 (2026-09-08).** `cell_membership`은 겹치는 행을 DB가 막지 못해 **한 사람에게 동시에 유효한 행이 여러 개** 있을 수 있다. 서버 판정은 항상 **그 사람의 최신 행 하나**(`effective_from DESC, id DESC`)를 먼저 고르고 그 행의 role을 본다 — 옛 LEADER 행이 안 닫힌 채 남아 있고 최신 행이 MEMBER면 그 사람은 셀장이 아니다. 셀장 목록을 뽑을 땐 `role` 필터를 **SQL이 아니라 최신 행을 고른 뒤에** 걸어라. 같은 시점 두 셀에 동시 소속이면 서버는 **소속 없음으로 fail-closed**한다(어느 셀 scope도 주지 않음). role 텍스트는 대소문자·공백 무시 collation이라 `'leader '`도 필터를 통과한다 — 값 비교 전에 trim+upper.
- ⚠️ **`cell.code` Z1~Z6은 `zone` 테이블의 Z1~Z6과 이름만 같은 다른 축**(§위 BELT 경고와 같은 함정). 셀↔존 매핑은 `zone_cell_assignment`(effective-dated, 셀당 zone 3개·공용 zone은 셀 2개)로 따라가고, `cell_name`·`region`으로 재구성하지 말 것. 셀장의 그날 동행 셀원은 `cell_accompaniment`(service_date KST, deleted_at NULL).
- 🔴 **셀장이 셀원 예약을 대신 실행해도 `reservation.detailer_id`는 셀원 그대로다 (2026-09-08 실측).** 동행 실행(2026-09-03 배포)에서 예약·정산·통계·인센티브는 전부 셀원 소유로 남고, 권한만 `cell_accompaniment` + `cell_membership`(LEADER)로 그날 하루 열린다. ⟹ **"디테일러 X가 오늘 실행한 세차"를 `reservation.detailer_id=X`로 뽑으면 셀장이 실행한 건이 통째로 빠진다.** 디테일러 이름으로 문의가 들어왔는데 그 사람 예약이 안 나오면 그날 `cell_accompaniment`에서 `leader_detailer_id=X`인 행을 보고 `member_detailer_id`로 다시 조회할 것.
  - ⚠️ **누가 실제로 눌렀는지는 서버에 안 남는다.** `checkup.detailer_id`·`detailer_reservation_call.detailer_id`는 그 행을 만든 시점의 actor 스냅샷일 뿐이고, 이후 단계를 셀장이 이어받았는지 셀원이 했는지 구분하는 컬럼·로그가 없다. "누가 했나"를 DB로 확정하려 하지 말 것.
  - `cell_accompaniment.service_date`는 DATE라 드라이버가 하루 밀려 렌더한다(§5a) — `DATE_FORMAT(service_date,'%Y-%m-%d')`로 읽을 것. 렌더 `2026-09-07T15:00:00.000Z` = 저장 `2026-09-08`.

**디테일러 로그인 정체성 = `detailer.user_id → app_user`**:
- 디테일러앱 인증번호 발송(`POST /v1/auth/detailer/certification-code/send`)은 `detailer.deleted_yn=0 AND retired_yn=0` **AND 연결 `app_user.deleted_yn=0 AND test_yn=0`** 이어야 201. 하나라도 깨지면 앱은 "인증번호 발송에 실패했어요"라고만 보여 SMS 장애로 오해한다. **가장 흔한 원인 = 그 사람의 고객앱 탈퇴**(app_user deleted_yn=1)라 SMS 로그를 볼 필요가 없다.
- 진단: `SELECT d.id,d.retired_yn,d.deleted_yn,d.user_id,u.deleted_yn u_del,u.test_yn FROM detailer d LEFT JOIN app_user u ON u.id=d.user_id WHERE d.phone='010...'` — 넷 다 0인 행이 없으면 로그인 불가. 복구는 같은 phone의 살아있는 app_user로 `detailer.user_id` 재연결(SQL 아니라 어드민 경로가 있으면 그쪽).

두 테이블 JOIN 시 `COLLATE utf8mb4_general_ci` 필수.

### 3b. Zone 배정 조인 체인

배정은 `detailer_work_schedule_rule.zone_id`로 결정.

```sql
-- 현재 유효 zone 배정
JOIN detailer_work_schedule ws ON ws.detailer_id = d.id
  AND UTC_TIMESTAMP() BETWEEN ws.effective_from AND ws.effective_to
JOIN detailer_work_schedule_rule r ON r.schedule_id = ws.id
  AND r.deleted_at IS NULL          -- ⚠️ deleted_yn 없음 — deleted_at IS NULL 사용
JOIN zone z ON z.id = r.zone_id     -- ⚠️ service_zone 테이블 없음 — zone 테이블만 있음
```

⚠️ `detailer_region` 테이블 쓰지 말 것 — `service_region_id` 기반 구식 구조.

🔴 **존은 하드 파티션이다 — "옆 존 사람이 왜 슬롯에 안 뜨나"의 답 (2026-07-31 코드+DB 확정).** 슬롯 조회 코드(zero-api `query-time-slots.handler.ts`)엔 "존 기반 결과가 0건이면 `zoneId: undefined`로 재조회해 **구(`service_region_group_id`) 단위로 넓히는**" 폴백이 있지만 실제로는 안 돈다:
- ① 판정이 **조회 창 전체** 기준(`primarySlots.length === 0`)이다. 앱은 한 달을 조회하므로 후반에 1개라도 있으면 폴백 미발동 → 근일(D+0~2) 공백이 그대로 노출된다.
- ② **DEFAULT 근무룰은 전부 `zone_id`만 있고 `service_region_group_id`는 NULL**이다(전수 확인). srg만 가진 룰은 구형 디테일러 1명씩(종로1·중구2·용산3·성동4·광진5·동대문6·중랑7·성북8·강동25·인천53~58·구리99·남양주100)뿐이고 **서초22·강남23·마포14 보유자는 0명**, `zone_id`·srg 둘 다 NULL인 전지역 룰도 없다 → 폴백이 돌아도 후보 0명.
- ⟹ 존별 캐파가 남아도 **다른 존 고객은 그 캐파를 쓸 수 없다**. "전사 슬롯은 여유 있는데 특정 지역만 예약 불가"의 구조적 원인. 🔑 룰 필터는 **rule 단위 OR**이고 같은 요일에 복수 룰이 허용되므로, 한 사람을 하루 안에 두 존으로 쪼개 넣는 것(오전 원존/오후 인접존)은 **코드 변경 없이 DB만으로** 된다.

**zone id → name 매핑:**

| id | name |
|----|------|
| 1  | Z0 (경기 성남시) |
| 2  | Z1 (마포구/용산구) |
| 3  | Z3 (강남구/송파구) |
| 4  | Z4 (강서구/경기김포시) |
| 5  | Z5 (서초구/용산구) |
| 6  | Z6 (인천 연수구/인천 서구) |
| 7  | Z7 (경기 안양시/경기 과천시) |
| 8  | Z9 (성동구/성북구) |
| 9  | Z10 (영등포구/금천구) |
| 10 | Z12 (강남구/서초구) |
| 11 | Z14 (경기 용인시/경기 화성시) |
| 12 | Z16 (강동구/송파구) |
| 13 | Z17 (경기 고양시/경기 파주시) |
| 14~19 | CELL_SHARED P0~P5 |
| 20 | Z1 강북서·고양 |
| 21 | Z2 강북동·도심 |
| 22 | Z3 서남내 |
| 23 | Z4 서부·인천 |
| 24 | Z5 동남 |
| 25 | Z6 경기남부 |

🔴🔴 **존 체계가 두 벌 겹쳐 있어, 세대 필터 없이 `ST_Contains`를 쓰면 좌표 하나가 존 여러 개에 들어간다 (2026-09-04 실측).** 옛 Z-존(id 1~13, 이름 `Zn (구/구)`)과 9월 조직개편 체계(id 14~19 `CELL_SHARED Pn` + id 20~25 `Zn 지역명`)가 서울을 **각각 독립적으로 타일링**한다 — 방배동 → `5:Z5 (서초구/용산구)` + `14:CELL_SHARED P0` / 천호 → `12:Z16 (강동구/송파구)` + `24:Z5 동남` / 옥수동 → `8:Z9` + `18:CELL_SHARED P4`.
- 🔴 **`zone` 자체에 세대가 있다 — 조인에 유효일자를 걸면 좌표당 1행으로 정리된다 (2026-09-07 실측).** `zone`에 `effective_from`·`effective_to`·`kind`·`group_code` 컬럼이 있다. 구존 13개는 `effective_to = 2026-09-07 15:00:00`(UTC), 신존 12개는 같은 값이 `effective_from`이다.
  ```sql
  LEFT JOIN zone z ON z.deleted_at IS NULL
    AND z.effective_from <= :date AND (z.effective_to IS NULL OR z.effective_to > :date)
    AND ST_Contains(z.area, ST_GeomFromText(CONCAT('POINT(',ua.longitude,' ',ua.latitude,')'),0))
  ```
  실측(2026-09-08 기준 예약 주소 693개): **1행 684 · 0행 8 · 2행 1**. ⟹ "룰 분포로 어느 체계가 사는지 역추적"하는 아래 우회는 이제 보조 확인용이다.
- ⚠️ **세대 필터를 안 걸었을 때 "항상 2행"은 아니다** — 구세대에 없던 지역은 1행(예 `25:Z6 경기남부`), 신구 경계가 겹치면 3행, 폴리곤 밖은 0행이다. 신세대끼리 겹치는 좌표도 1건 있다(주소 45440 → `20:Z1 강북서·고양` + `21:Z2 강북동·도심`) — `LIMIT 1`로 집으면 조용히 갈린다.
- 🔑 **"공용구역 디테일러"는 별도 로스터가 아니라 근무 룰의 zone_id로만 갈린다 (2026-09-09 실측).** 유효 `DEFAULT` 스케줄의 활성 룰을 `GROUP BY detailer_id`로 펼치면 **두 모양뿐**이다: 요일마다 **공용존 2개**를 든 사람(예 `MON:17,MON:16,TUE:17,TUE:16,…`)과 **전담존 1개**만 든 사람. 겸업(공용+전담)은 0명이라, 공용구역 담당자 = **자기 룰의 zone_id가 전부 14~19인 사람**으로 깔끔하게 갈린다(2026-09-09 기준 52명 중 11명). `detailer_supply_sheet.cell_name`·`region`으로 재구성하면 안 된다 — 셀은 공용존과 전담존을 동시에 맡으므로 사람이 갈리지 않는다.
  - ⚠️ 룰이 구세대 zone_id(1~13)인 사람은 이 판정에서 **어느 쪽도 아니다**(2026-09-09: 셀장 6명 + 1명이 여기 남아 있다). "공용도 전담도 아닌 사람"이 나오면 버그가 아니라 §위 세대 잔존이다.
- **존↔셀 매핑 = `zone_cell_assignment`**(zone_id, cell_id, effective_from/to, deleted_at) + `cell.code`(Z1~Z6). 전담존(`kind='CELL_EXCLUSIVE'`, id 20~25)은 셀 1:1이라 존 게이트가 셀 게이트 구실을 하고, **공용존(`kind='CELL_SHARED'`, id 14~19)은 셀 2개가 나눠 맡는다**(P0=Z3·Z4, P1=Z3·Z6, P2=Z5·Z6, P3=Z2·Z5, P4=Z1·Z2, P5=Z1·Z4).
- 🔴 **셔플은 공용존 예약을 아예 건드리지 않는다 (caramel-api, 2026-09-05 prod).** `route-optimization.service.ts`가 `zoneKind='CELL_SHARED'`인 예약을 대상에서 뺀다 — 옮기지도, 교환으로 받지도 않는다(공용 조각을 누가 맡을 자격이 있는지가 그 레포에 없어서 내린 보수 조치). 물량이 작지 않다: 2026-09-08~11 CONFIRMED **185/732건 = 25.3%**. "셔플이 이 예약을 왜 안 옮겼나"의 첫 확인 항목.
- 🔴 **룰의 `zone_id`가 구세대면 그 디테일러는 신세대 예약과 절대 매칭되지 않는다** — 셔플에서는 내보내기만 되고 **수신 0**이 된다. 실측(2026-09-08): 셀장 6명(21·80·87·97·114·161)이 `cell_membership`엔 LEADER로 들어갔는데 근무 룰 zone_id는 여전히 2·3·5·6·7·10·12였다. 사람별 존을 볼 때 **룰의 zone_id 세대와 대상 날짜의 세대를 대조**할 것.
- 🔴🔴 **전환 시점은 2026-09-08 00:00 KST다 — 그 이전 날짜엔 아래 "새 체계를 써라"가 정반대로 틀린다 (2026-09-06 실측).** 새 zone_id(20~25)를 쓰는 `detailer_work_schedule`은 `effective_from = 2026-09-07 15:00:00`(UTC)부터 시작한다. **2026-09-07(월) 유효 룰의 담당자 분포는 옛 Z-존이 전부**다 — `zone_id` 8(Z9) 8명 · 2(Z1) 6명 · 10(Z12) 6명 · 5·9 각 5명이고 **id 20~25는 0명**. 하루 뒤인 09-08(화)엔 반대로 뒤집힌다. ⟹ **존 id를 고르기 전에 대상 날짜의 effective 룰로 `GROUP BY zone_id` 를 한 번 돌려 어느 체계가 살아 있는지 확인할 것.** 스케줄은 주 단위로 새 row가 나므로(§3b) 날짜 경계 하나 차이로 후보가 통째로 0명이 된다.
- 🔴 **후보 탐색에 쓸 것은 새 체계(id ≥ 14)다** (위 전환 시점 이후 한정). 활성 DEFAULT 룰의 담당자 분포(2026-09-08 유효 기준)로 보면 새 체계는 존당 **4~6명**인데 **옛 Z-존은 전부 존당 1명**(Z1·Z3·Z5·Z6·Z7·Z9·Z12·Z16 각 1명)뿐인 잔존 껍데기다. ⟹ 옛 Z-존 id로 대체 후보를 뽑으면 **1명 나오고 그 1명이 휴무면 "후보 없음"으로 오답**한다(2026-09-04 천호 재배정에서 실제로 발생 — Z5·Z9·Z10·Z12로 7건을 찾았을 때 전원 휴무/0명이었고, 같은 좌표를 `CELL_SHARED`로 다시 찾자 후보가 나왔다).
- ⚠️ **이름이 겹친다.** `Z5`가 id 5(`Z5 (서초구/용산구)`)와 id 24(`Z5 동남`) 둘, `Z3`·`Z6`도 마찬가지다. **사람에게 말할 때도 `zone.name`을 통째로 인용**하고 "Z5"로 줄이지 말 것.
- ⟹ 위 §"공동구역은 `zone` 테이블에 없다"(2026-08-20)는 **더는 사실이 아니다.** 셀은 이제 `zone` 테이블 id 14~19에 폴리곤으로 들어와 있어 `ST_Contains`로 직접 판정된다. `zone6_areas.geojson`은 이 체계 이전 근사치다.

### 3c. 재배정 후보 탐색 — "이 존 외 건, 누구로 바꿀 수 있나" (2026-07-26)

§6b의 사전검증은 **이미 고른 대상을 검사**하는 절차다. 후보를 **찾는** 건 별개이고, 순서를 틀리면 "교체 불가"라는 오답이 나온다.

1. **같은 zone 담당자로 1:1 교체는 대개 불가** — 그 zone·그 요일 rule 보유자를 전부 뽑아도 인기 시간대(14시 등)엔 전원 예약이 차 있다. **여기서 멈추지 말 것.**
2. **존을 풀고 "그 시각 공백 × 목표주소 최단거리"로 확장**한다. 1위가 보통 압도적으로 가깝다(실사례 1.4km vs 2위 4.9km).
```sql
WITH sched AS (   -- 그 요일 근무자 (⚠️ rule은 schedule_id로 직접 필터 — §3b)
  SELECT dws.detailer_id FROM detailer_work_schedule dws
  JOIN detailer_work_schedule_rule r ON r.schedule_id = dws.id AND r.deleted_at IS NULL
  WHERE dws.effective_from <= :date AND dws.effective_to > :date AND r.day_of_week = 'MON'
),
res AS (          -- 그날 전체 예약 (KST 하루 = UTC 전날15:00 ~ 당일15:00)
  SELECT r.detailer_id, DATE_FORMAT(CONVERT_TZ(r.reservation_datetime,'+00:00','+09:00'),'%H:%i') kst,
         r.location, COALESCE(r.longitude,ua.longitude) lng, COALESCE(r.latitude,ua.latitude) lat
  FROM reservation r LEFT JOIN user_address ua ON ua.id = r.address_id
  WHERE r.reservation_datetime >= :utc_from AND r.reservation_datetime < :utc_to
    AND r.status NOT IN ('CANCELED','CREATED')
)
SELECT s.detailer_id, d.name,
       ROUND(MIN(ST_Distance_Sphere(ST_GeomFromText('POINT(:lng :lat)',0),
              ST_GeomFromText(CONCAT('POINT(',x.lng,' ',x.lat,')'),0)))/1000,1) AS min_km
FROM sched s
JOIN detailer d ON d.id = s.detailer_id
  AND d.booking_yn=1 AND d.direct_yn=1 AND d.retired_yn=0 AND d.deleted_yn=0   -- §3a
LEFT JOIN res x ON x.detailer_id = s.detailer_id
WHERE NOT EXISTS (SELECT 1 FROM res y WHERE y.detailer_id=s.detailer_id AND y.kst=:target_hhmm)
  -- ① 종일 휴무만 종일 탈락 (부분 블록을 여기 섞으면 근무 가능자가 통째로 사라진다 — 항목 4)
  AND NOT EXISTS (SELECT 1 FROM detailer_holiday h WHERE h.detailer_id=s.detailer_id
                    AND h.from <> h.to AND TIMESTAMPDIFF(HOUR, h.from, h.to) > 8
                    AND h.from < :kst_day_end_utc AND h.to > :kst_day_start_utc)
  -- ② 부분 시간 블록은 "그 시각만" 탈락
  AND NOT EXISTS (SELECT 1 FROM detailer_holiday h WHERE h.detailer_id=s.detailer_id
                    AND h.from <> h.to AND h.from <= :target_utc AND h.to > :target_utc)
GROUP BY s.detailer_id, d.name ORDER BY min_km;
```
3. **채택 판단은 거리가 아니라 "동선 사이에 끼는가"** — 후보의 직전/직후 예약 시각·좌표를 뽑아 삽입 가능한지 본다(+ 하루 5건 캡). 목표가 기존 동선 한복판에 떨어지는 후보가 정답.
4. ⚠️ `detailer_holiday`는 **UTC 저장**이라 "그날 휴무" 판정 윈도우는 `from < 'X일 14:59:59' AND to > '(X-1)일 15:00:00'`. `from <> to` 필터도 같이(§6b 무력화 row). 그래도 예약이 있는 사람이 휴무로 잡히는 경우가 있으니 **route와 교차확인**.
   - 🔴 **하루 겹침만 보면 "부분 시간 블록"이 종일 탈락으로 번져 후보를 잃는다 (2026-07-26 실사례).** 황석찬(114)에게 memo `셀원 품질 점검`으로 **매일 UTC 05:00~09:00(=KST 14~18시) 4시간 row가 4월~8월 대량 선삽입**돼 있어, 겹침 필터로는 "휴무 있음"이 되지만 오전은 근무 가능이다. **판별 = `TIMESTAMPDIFF(HOUR, from, to) > 8`이면 종일, 이하면 부분 블록**(+`memo` 확인). 위 쿼리처럼 두 NOT EXISTS로 분리할 것.
   - 🔴 **거꾸로, 부분 블록 여러 개가 이어 붙어 근무일 전체를 덮는다 — row 하나만 보면 "반나절 가능"으로 오답한다 (2026-09-08 실측).** 위 `> 8시간` 판별은 **row 단위**라, 하루를 두세 토막으로 나눠 등록하면 전부 "부분 블록"으로 통과한다. 실사례: 김용빈96 9/10 = `백화점 시연·팝업 파견` KST 10:00~14:00 + `연차 - 반차/오후` 14:00~19:00 → 각 4·5시간이라 둘 다 부분 블록인데 **합치면 근무창 10~19시 전체**다. 천호 5칸(10:30·12:30·14:30·16:30·18:00)이 통째로 겹친다. 드문 일이 아니다 — 6월 이후 **10건**(`셀원 품질 점검`+`셀장 영업 교육`, `SA급 공정 교육`+`반얀트리 체험`, `06시 출근` 3토막 등). ⟹ **가용 판정은 row별로 하지 말고 그 사람의 그날 휴무 row를 전부 모아 구간 합집합을 만든 뒤 예약 구간과 대조할 것.** 탐지 쿼리 = `GROUP BY day, detailer_id HAVING COUNT(*)>=2 AND MAX(TIMESTAMPDIFF(HOUR,from,to))<=8 AND SUM(...)>=8`.
   - 🔴 **다일 종일 휴무의 마지막 날은 `to`의 날짜가 아니다 (2026-09-01 실측).** 종일 휴무는 `to`가 **끝난 다음 날 KST 00:00**으로 박힌다 — id 12284 = KST `8/31 00:00 ~ 9/2 00:00` = 8/31·9/1 **이틀**이고 9/2는 근무일이다. `DATE(to)`로 마지막 날을 세면 하루를 더 센다(마지막 날 = `DATE(to - INTERVAL 1 SECOND)`). 위 윈도우식의 `to >`가 strict인 이유가 이것이고, `>=`로 바꾸면 안 쉬는 날이 휴무로 잡힌다. 같은 실수를 편성 화면 코드가 냈다 — 지울 수 없는 유령 휴무 줄(zero PR #1859).
   - ⚠️ 반대 방향도 틀린다 — 같은 사람에게 **종일 row가 별도로 존재**할 수 있다(황석찬은 `출산 휴가 - 연차` 7/19~7/31 종일 row가 있어 결과적으로 탈락). **부분/종일 둘 다 조회해야 정답.** 한쪽만 보고 "가용/불가"를 확정하지 말 것.
5. 실행 전 §6b "재배정 대상 사전검증"을 반드시 통과시키고, 실행은 재배정 API로(DB 직접 UPDATE 금지). `skipConflictCheckYn=false`로 두면 TMap 실이동시간 기반 충돌체크가 돌아 삽입 타당성을 한 번 더 걸러준다.
6. **교체 전 고객 통지 상태 확인** — D-1 알림톡 본문엔 담당 디테일러 **이름**이 들어간다(§6g). 이미 나갔으면 교체 시 고객이 본 이름이 바뀌므로 재통지 필요.

### 3d. 파견 전환 잔존 예약 — "존 외"의 세 번째 원인 (2026-07-26)

오배정도 슬롯 누수도 아닌 통로. **디테일러를 파견(반얀 등)으로 전환할 때 `detailer_work_schedule`만 갈아끼우고, 그 전에 이미 잡혀 있던 미래 예약은 아무도 되돌아보지 않는다.** 스케줄 변경 → 기존 미래 예약 재검증/재배정 메커니즘이 시스템에 **없다**.

- 실사례: 한수용(191) `BANYAN_TREE_EXTENDED`(7/20~8/31) 스케줄을 **7/18 21:59에 생성** → 그 전 배정된 Z16 권역(강동·송파·하남·성남) 예약이 파견기간에 **45건/18일** 잔존. 하루 것만 처리하면 4~5건 겹치는 날에 계속 재발하므로 **처음부터 파견기간 전체를 뽑을 것.**
- **진단 = 파견 스케줄 `created_at`을 기준선으로 before/after 가르기.** 파견기간 내 비-파견지 예약 중 `r.created_at >= (파견 스케줄 created_at)`인 게 0건이면 **잔존 tail**(과거 배정분), >0이면 **진행형 누수**(신규가 계속 붙는 중). 처방이 다르다 — tail은 일괄 재배정, 누수는 코드/스케줄 수정.
- 🔴 **`reservation_datetime` 상한을 파견 `effective_to`로 반드시 걸어라.** 파견 **종료 후** 날짜는 복귀 DEFAULT 스케줄 구간이라 그 존 예약이 정상이다. 상한 없이 세면 복귀 구간 예약(한수용 9월 8건, 구독 자동예약 `created_at` 03:0x)을 "누수"로 오판한다.
- 구독 자동예약이 몇 주 앞을 미리 깔아두므로(§6b) 파견 결정 시점엔 이미 한 달 반 뒤까지 채워져 있다 = tail은 항상 크다.
- ⚠️ **배정 당시엔 존 외가 아니었을 수 있다** — 판정은 "지금 스케줄"이 아니라 **예약 생성 시점의 effective 스케줄**(`effective_from <= 생성시점 < effective_to`)로. 지금 기준으로 존 외라고 오배정 선언하지 말 것.
- 🔴 **근무창 *안*이어도 이동시간 때문에 불가능해진다 — 휴무·근무창 감사 어느 쪽에도 안 걸리는 세 번째 유형 (2026-08-06 실측).** 한수용(191)이 반얀 **오전조→오후조**(sched 898 `BANYAN_TREE_EXTENDED`, 8/3~8/9 KST 16~21)로 재편성되자 원존(Z16 고덕) **18:00** 예약이 파견 근무창 **안**에 남았다. `detailer_holiday` **0건**이고 근무창 이탈도 아니라 위 두 감사(장소 불일치·교대창 이탈)를 **모두 통과**한다. 실제로는 16:00 반얀 90분 → 17:30 종료 + 장충동→고덕 20km(35~45분) = 18:10 도착으로 불가. ⟹ **파견 감사 조건에 이걸 추가할 것: `파견지 직전 예약 종료시각 + 파견지→목표 이동시간 > 목표 시각`.** 파견 근무창을 **시간대만 옮기는**(오전조↔오후조) 재편성이 이 유형을 만든다 — 날짜·장소·건수가 그대로라 눈에 안 띄고, 디테일러가 전날 전화해서야 드러난다.
  - 🔴 **파견 감사는 반드시 스케줄 토막을 *전수*로 뽑고 시작할 것 — "지금 유효한 한 토막"을 파견 전체로 읽으면 남은 충돌을 통째로 놓친다 (2026-08-06 실수).** 한수용 파견은 **7/20~9/4**인데 `effective_from <= X AND effective_to > X`로 **오늘 것 한 토막(8/3~8/9)만** 뽑고 "파견은 8/9까지"로 단정해, 9/1·9/4 잔존 3건을 "없음"으로 보고했다(사용자가 잡아냄). 실제 편성은 **주 단위 8토막이고 오전조(08~16)/오후조(16~21)가 번갈아** 든다(811·855 오전 → 898 오후 → 899 오전 → 900 오후 → 901 오전 → 902 오후 → 807 복귀). ⟹ 진입 쿼리는 `WHERE detailer_id=? AND type LIKE 'BANYAN_TREE%'` **전체 토막 + 각 토막 rule의 `start_time`/`end_time`**을 먼저 펼쳐 보고, 예약 감사는 예약 1건마다 **그 시점에 유효한 토막**을 조인해서(`dws.effective_from < r.reservation_datetime AND dws.effective_to > r.reservation_datetime`) 근무창을 판정한다. 토막 사이 **공백일**(한수용 7/29)도 이때 드러난다.
- 🔴 **2026-08-06 이후 잔존 tail은 자동으로 안 없어진다 — 셔플이 파견조를 통째로 제외하기 때문 (2026-08-17 확인).** caramel-api PR #525→#526(main `c3194e2e`, prod)이 `shuffleTargets` 필터에 `workScheduleType !== 'BANYAN_TREE'`(prefix 매칭이라 `_EXTENDED` 포함)를 넣어 파견조는 **assignment 자체가 안 생긴다 = 내보내기·받기 양쪽 차단**. 유입(반얀 밖 예약이 파견조로 새로 들어옴)은 이걸로 멈췄지만, **파견 편성 전에 이미 배정돼 있던 반얀 외 예약은 셔플이 회수해 가지 못하고 파견 종료일까지 그대로 남는다**(당시 의도적으로 택한 트레이드오프). ⟹ 파견 편성 시점에 **사람이 스윕하지 않으면 영구 잔존**이다. 실사례: #85607(김요한 8/18 12:00 옥수동)이 7/6 자동예약으로 임사명193에 배정 → 7/30에 그의 8/17~8/23 반얀 파견 스케줄 생성 → 아무도 회수 안 함 → 전날 셀장이 발견.
- 🔴 **파견조 예약의 대체 후보를 뽑을 땐 `dws.type NOT LIKE 'BANYAN_TREE%'`를 반드시 넣어라 (2026-08-17 실측).** 반얀 파견 스케줄의 rule도 **`zone_id = 8`(Z9)** 을 들고 있어서, 존으로만 후보를 뽑으면 **파견자 본인과 동료 파견자가 자기 대체 후보로 올라온다**(8/18 Z9 후보 쿼리에서 실제로 발생). 집계 왜곡판(아래 §대시보드 주의)과 같은 원인인데, 재배정에서는 왜곡이 아니라 **못 가는 사람에게 배정하는 사고**가 된다.
- 🔴 **파견 타입 필터가 전부 `BANYAN_TREE` 문자열 전제라 새 현장 타입을 안 잡는다 (2026-08-26 확인).** 위 두 항목의 `LIKE 'BANYAN_TREE%'`도, 코드 쪽 필터도 마찬가지다 — `route-optimization.service.ts`의 `normalizeWorkScheduleType()`은 **`=== 'BANYAN_TREE'` 정확 일치**라 `HD_CHEONHO`는 물론 `BANYAN_TREE_EXTENDED`도 `DEFAULT`로 정규화하고, `careplus-reservation.service.ts`의 후보 조회는 `type in ['DEFAULT', ...(반얀이면 'BANYAN_TREE')]`로 뽑는다. ⟹ **파견 감사·대체 후보·셔플 관련 쿼리를 쓸 땐 `type NOT IN ('DEFAULT')` 쪽으로 뒤집거나 현장 타입을 명시 열거하라.** `BANYAN_TREE%`로 거른 결과를 "파견 전체"로 읽으면 천호 파견자가 통째로 빠진다. 🔴🔴 **셔플은 천호를 전혀 격리하지 못한다 (2026-08-26 코드+실측 확정).** 반얀을 지키는 건 `allow_shuffle_yn`이 **아니다** — prod 반얀 파견 예약도 `allow_shuffle_yn=1`이 대다수(`BANYAN_TREE_EXTENDED` 521/685, `BANYAN_TREE` 24/27, 7월 이후). 유일한 방어는 `isWorkScheduleCompatible`(**주소 판정 == 디테일러 타입**) 하나뿐이고, 천호는 `BANYAN_TREE_ADDRESS_PATTERNS`(`반얀트리`·`장충단로 60`·`장충동2가 201`)에도 `normalizeWorkScheduleType`(`startsWith('BANYAN_TREE')` — `_EXTENDED`는 흡수하지만 다른 현장은 못 잡는다)에도 없어 **양쪽 다 `DEFAULT`로 접혀 게이트를 통과한다.** 존 게이트도 못 막는다 — `isZoneCompatible`은 srg·zone이 **둘 다 비면 `return true`(하위 호환 무제약 허용)** 이고, 천호 파견 rule이 둘 다 NULL이다.
  - ⚠️ **정정 (2026-08-26 실측): "파견 rule은 보통 둘 다 NULL"은 반얀에는 안 맞는다.** 활성 룰 기준 `BANYAN_TREE` srg=2/zone=NULL 92 · srg=2/zone=8 10 · `BANYAN_TREE_EXTENDED` srg=2/zone=8 245 — **반얀은 srg 2를 항상 들고 있다.** 둘 다 NULL 인 건 `HD_CHEONHO` 10건뿐이다. 무제약 폴백 경로를 타는 건 천호이고, 반얀은 srg 로 걸린다.
  - 🔑 **`srg`·`zone` 이 둘 다 NULL = 현장 파견 전용 지문이다.** `DEFAULT` 룰(9,000+건)은 **반드시 한쪽이 채워져 있고 둘 다 NULL 인 건 0건**이다. ⟹ 근무 유형 문자열을 열거하지 않고 파견을 찾아야 할 때 쓸 수 있는 축이고, 반대로 **현장 룰을 새로 넣을 때 둘 다 NULL 로 넣어야** `selectApplicableWorkScheduleRules` 의 무제약 버킷으로 떨어져 슬롯이 잡힌다.
  - 🔑 **`detailer_work_schedule.type` 실존 값은 6개로 닫혀 있다 (2026-08-26 prod 전수):** `DEFAULT` · `BANYAN_TREE` · `BANYAN_TREE_EXTENDED` · `HEY_DEALER` · `INCHEON_TESLA` · `HD_CHEONHO`. `LIKE 'BANYAN_TREE%'` 로 "파견 전체"를 세면 뒤 3개가 빠진다. 컬럼이 `String?` 이라 값 추가에 DDL 이 없으므로 **쿼리 짜기 전에 `GROUP BY type` 을 한 번 돌려 목록을 확인할 것.** ⟹ **새 현장을 추가하면 `route-optimization.service.ts`의 주소 패턴·타입 유니온·`normalizeWorkScheduleType` 3곳을 반드시 함께 고칠 것.** 셔플의 예약 조회(`planOptimizeRoutes`)엔 파견조 제외 필터가 **아예 없다**(spec 주석의 "파견조 자체는 셔플에서 빠지므로"는 사실이 아니다). ✅ 수정 = caramel-api **#543+#544**(develop 머지 완료, prod 승격 #545 대기). 🔑 #525 가 넣은 파견조 제외 조건이 `!== 'BANYAN_TREE'` **부정형**이라, #543 으로 HD_CHEONHO 가 정규화를 통과하자 이 필터도 함께 통과했다(파견자끼리 맞교환 가능) — #544 에서 `=== 'DEFAULT'` 긍정형으로 뒤집었다. **현장 관련 필터는 부정형으로 쓰지 말 것.** ⚠️ 이 레포는 **PR 레벨 CI가 없다**(`ci-apps.yml`은 develop/main push 시에만) — "no checks"가 정상이고 검증 책임은 로컬 테스트에 있다.
- 🔴 **잔존 예약은 "원존 예약"만이 아니다 — 같은 장소 안에서 근무창을 쪼개도 발생한다 (2026-07-27 실측).** 한 장소를 오전/오후 2교대로 분할하면 **분할 전 생성된 그 장소 예약이 교대창 밖으로 떨어진다.** 실사례: 반얀 09·10·11시 예약 4건이 오후조(KST 14~21)인 이형준161·임사명193·박현규207에 잔존 — created 7/13~7/15로 2교대 편성(7/19) 전이고, 당시엔 기본 `BANYAN_TREE` 그리드(09·11시)가 열려 있었다. ⟹ **근무창 감사에 "장소가 맞는 건"도 반드시 포함할 것.** 장소 필터(`location` 불일치)만으로 잔존을 찾으면 이 유형이 통째로 안 보인다.

---

### 3e. 디테일러 생산성 — 작업 소요시간·이동 간격 (2026-08-06 실측)

"1인당 하루 몇 대까지 가능한가"를 따질 때 쓰는 3종. 세 군데 다 함정이 있다.

🔴 **계획 소요분(`estimated_time`)의 산식은 `service.time_required`가 아니다 (2026-09-07 코드 확정, `derive-wash-duration.policy.ts`).** `service_group_id`가 1·2·3·7이면 **코드 상수 60·50·30·60**이 쓰이고 그 서비스 행의 `time_required`(예: `프리미엄 세차 패키지 올클린 케어` 70)는 **무시된다**. 여기에 `user_option`→`options.extra_time` 합(왁스 15·살균 15 등) + `car_tier.tier >= 5`면 +10 + 첫 세차면 +10을 더한다. ⟹ DB의 `time_required`로 소요분을 재구성하면 값이 안 맞는다. 그룹이 위 4개에 없을 때만 `time_required`가 쓰인다.

🔴 **컨시어지 케어 예약은 위 산식 위에 최소값(floor)이 얹힌다 (2026-09-08 코드 확정, `concierge-care-duration.policy.ts`).** 활성 케어 marker가 그 예약의 **현재 담당자와 일치할 때만** 산식값을 **외부만 100분 / 내부까지 140분**으로 끌어올린다(산식값이 이미 더 크면 그대로). 판정 입력은 서비스 묶음이 아니라 **실제로 내부까지 하는가** — 원래 묶음이 외부+내부거나 `내부 세차 추가` 옵션이 붙으면 140분이다. 내부만·미지 묶음엔 floor가 없다. ⟹ 케어 예약 소요분을 §3e 산식만으로 재구성하면 안 맞는다.

🔴 **케어 지정은 예약의 세차권 서비스를 `컨시어지 케어 체험`(service 145, `service_group_id=1` = 외부+내부)으로 갈아치운다. 원래 상품은 `user_service`에 남지 않는다 (2026-09-08 실측).** 모니터링을 위한 의도된 교체다. 그래서 케어 예약에 `JOIN service`로 세차 범위를 읽으면 **전부 외부+내부로 나온다** — prod 18건 중 12건이 원래 외부만인데 그렇게 보인다. 원래 상품의 정본은 `reservation_change_log`의 `JSON_EXTRACT(data,'$.type') = 'CONCIERGE_CARE_SERVICE_CLASSIFICATION_CHANGED'` → `$.fromServiceId`(전 기간 커버, 예약별 첫 로그가 원본)다. "케어 예약의 세차 범위 비중"을 물으면 `service`가 아니라 이걸 봐라. ✅ **2026-09-08부터 `reservation_metadata` `key='concierge_care_origin_service'`(JSON `serviceId`·`serviceGroupId`·`serviceName`·`timeRequired`)가 정본이다** — prod 배포 + 기존 18건 소급 생성 완료(change log와 1:1 대조). 세차 범위는 이 스냅샷 `$.serviceGroupId`(1=외부+내부·3=외부만)를 먼저 읽고, 없으면 `user_service→service.service_group_id`로 폴백. 해제돼도 스냅샷은 안 지우므로 `concierge_care` marker `deleted_at IS NULL`과 함께 걸어야 "지금 케어인 건"이 된다.

🔴 **실제 작업 소요시간의 정본은 `wash_result.created_at` → `wash_result.finished_at`이다. `reservation.estimated_time`을 쓰지 마라** — 그건 차량 티어·서비스에서 나온 **산식(계획값)**이지 측정값이 아니다. 부하 랭킹(§6b)엔 계획값이 맞지만 "실제로 몇 분 걸리나"엔 틀린다.

⚠️ **`wash_result.status`는 BEFORE/AFTER가 아니다** — 리포트 작성 **워크플로 단계**이고 `'DONE'`이 완료다(2026-05~07 10,788 / 10,804 = 99.9%. 나머지는 `CHECKUP/TIRE/...`·`SUBMIT/NOTE` 등 중간에 멈춘 행). BEFORE/AFTER는 `wash_result_image.status`다(§6d). **예약당 1행**이라 소요시간 집계에 중복이 안 생기고, `finished_at IS NOT NULL`만 걸면 미완료 행은 자동으로 빠진다.

🔴 **디테일러 귀속·`GROUP BY`는 `reservation.detailer_id`(FK)로. `technician`으로 묶지 마라** — `technician`은 디테일러 **이름 문자열**(비정규화)이고 99.9% 채워져 있어 그럴듯해 보이지만, 2026-05~07 구간에서 `COUNT(DISTINCT technician)` 74 vs `COUNT(DISTINCT detailer_id)` 75로 **동명이인이 한 명 합쳐진다.**

🔴 **`technician`은 *확정된* 담당자의 이름이다 — 미래 예약에서는 `detailer.name`과 갈리는 게 정상이고, 그것이 곧 오류는 아니다 (2026-09-06 실측).** 미래 CONFIRMED 5,027건 중 3,234건(64.3%)이 `technician <> detailer.name`인데, **과거로 갈수록 0에 수렴한다**: 완료 세차 기준 8/24~9/4 1,792건 중 불일치 **3건(0.17%)**, 내일자 예약 172건 중 **0건**, 모레 62/185, 그 뒤로 계속 벌어진다. 매일 14시 셔플이 그날치를 정리하면서 `detailer_id`와 `technician`을 **같이** 갱신하기 때문이다(zero `prisma-reservation-facts.repository.ts`·`prisma-cell-reservation-swap.repository.ts`, 레거시 `careplus_task.service.ts` 전부 두 컬럼을 한 update로 쓴다).
- ⟹ **미래 예약의 불일치는 "아직 안 굳은 배정"이지 데이터 오류가 아니다.** 고쳐 넣지 말 것.
- ⟹ 그래도 **담당자 조회·귀속·`GROUP BY`는 `detailer_id`로 한다.** `technician`으로 뽑으면 아직 안 굳은 남의 예약이 딸려 온다 — 실사례: 맹주원(132)은 `detailer_id` 기준 9/7 이후 3건인데 `technician='맹주원'`으로는 93건이고, 그 차이 90건은 29명의 다른 디테일러 몫이다. 0건이 아니라 *부풀려진* 형태라 눈에 안 띈다.
- ⚠️ **한 배치가 한 이름을 통째로 찍는 일이 있다**: 2026-09-04(KST) 생성분 96건이 전부 `technician='맹주원'`인데 실제 담당자는 30명이었다(같은 날 다른 이름은 전부 담당자 1명). 생성 시 배정이 한 사람에게 쏠린 뒤 재배정된 흔적으로 읽힌다 — **생성일 기준으로 세면 이런 쏠림이 드러난다.**
- 🔴 **알림 2종이 이 컬럼을 이름으로 조인한다 (2026-09-06 확인, 수정 PR 진행 중).** ①**디테일러 본인에게 가는 내일 일정 알림**(`SELECT_TOMORROW_DETAILER_SCHEDULE`: `join detailer d on d.name=r.technician`, zero·레거시 양쪽 동일. 레거시는 묶는 것까지 `detailer_name` 기준이라 동명이인 둘의 일정이 한 통으로 합쳐져 **남의 고객 이름·연락처가 섞여 나간다**), ②**고객에게 가는 당일 안내**(`messaging-careplus.service.ts` `sendDDayReminderMessage`: `findFirstOrThrow({where:{name: reservation.technician}})` → 디테일러 **이름·연락처·차량번호**를 문자에 넣는다). 발송 시점엔 셔플이 이미 맞춰놔서 평소엔 안 터지지만, **동명이인·이름 변경이 생기는 순간 곧바로 샌다**(`findFirstOrThrow`는 매칭 실패 시 throw).
  - ⚠️ **고객에게 가는 전날 18시 "내일 세차 담당자" 안내(`reservationUpcoming003`)는 이 축이 아니다** — Prisma relation(`target.detailer.name`)으로 읽어 원래부터 외래키 기준이다. "고객 문자 2종이 이름 조인"이라고 읽으면 틀린다.
- ⚠️ `technician`(utf8mb4_general_ci)과 `detailer.name`(utf8mb4_unicode_ci)은 **collation이 달라 그냥 비교하면 `Illegal mix of collations` 로 죽는다** — `r.technician COLLATE utf8mb4_general_ci = d.name COLLATE utf8mb4_general_ci`.


🔴 **이동시간을 `LEAD` 간격으로 재지 마라 — 그건 이동이 아니라 "이동 + 유휴"다 (2026-08-06 실측, 내가 틀렸던 것).**
연속 작업의 `LEAD` 차이는 4시간 트림 후 평균 **59.7분**(n=7,333)이라 "이동이 오래 걸린다"로 읽힌다. 그런데 실제 이동거리는 **홉당 3~5km · 하루 총 7~14km**(서울 시내 15~45분)다. **간격의 대부분은 예약 슬롯 사이 빈 시간**이지 이동이 아니다. 이걸 이동으로 읽으면 "생산성 제약 = 동선"이라는 **정반대 진단**이 나온다.

**정본 = `cbr_detailer_daily_productivity_snapshot`** (디테일러·일 단위 사전집계. 이동·시간을 다 갖고 있다)

| 컬럼 | 뜻 |
|---|---|
| `daily_total_km` / `daily_avg_km` / `daily_max_gap_km` | 하루 총 이동 / **홉당** 평균 / 최대 홉 |
| `daily_work_minutes` | 근무 분 |
| `daily_total_wash_minutes` / `daily_avg_wash_minutes` | 세차 총 분 / 건당 분 |
| `washes_total`, `is_workday`, `detailer_segment`, `region` | 건수·근무일 플래그·세그먼트 |

- `is_workday = 1` 필터 필수. 주간 집계는 **중앙값**(주별 편차가 크다 — 2026-07 주간 세차수 2.18~4.22대).
- 🔑 **가동 판정식 = `daily_work_minutes − daily_total_wash_minutes` (여유분).** 2026-07-26주 실측: 근무 467분 · 세차 274분 · **여유 192분** · 세차시간 비중 58.8%. 여유의 대부분이 유휴라 **제약은 동선이 아니라 배정량(수요 밀도)**이다.
- `LEAD` 간격은 여전히 쓸모가 있다 — 단 이름을 **"작업 간 간격"**으로 부르고 이동시간이라 부르지 말 것. 음수(기록 겹침, 20건)는 `>= 0`으로 제외.

### 3f. 디테일러 일일업무(세차 준비 체크리스트·고객 연락) 완료 판정 (2026-09-01 실측)

"체크리스트 했나"는 **두 소스**가 있고 뜻이 다르다. 섞으면 오판한다.

| 소스 | 뜻 | 쓸 곳 |
|---|---|---|
| `detailer_checklist_submission` (+`_item`) | 실제 제출물. `date` = **세차일 D**, 제출은 **D-1 19~22시** | "정말 했나" 판정 |
| `detailer_daily_task_log` | D-1 22:00 크론이 찍는 **스냅샷**(`task`=ATTENDANCE/CHECKLIST/CUSTOMER_CALL/PEER_REVIEW, `status`=COMPLETE/INCOMPLETE/NOT_APPLICABLE) | 슬랙 미완료 명단의 정본 |

- 🔴 **미완료 = 행이 없다.** 작성 중이면 `submitted_at IS NULL` 행이 남고(앱이 문항별로 저장), 아예 안 열었으면 행 자체가 없다. `status` 컬럼은 submission에 없고 **파생**이다 — 제출 전=CREATED, 제출됐고 `_item.value=false`가 있으면 NEEDS_SUPPLEMENT, 전부 true면 PERFECT.
- `date`는 DATE 컬럼이라 헬퍼 JSON이 하루 밀려 보인다 → `CAST(date AS CHAR)` (§5a).
- **그날 근무자 분모** = `reservation` CONFIRMED + KST 변환(`DATE(DATE_ADD(reservation_datetime, INTERVAL 9 HOUR))`). 예약 없는 날은 `detailer_daily_task_log` 행도 안 생긴다 → "이번주 미완료 N회"를 셀 때 결근/무예약일을 분모에 넣지 말 것.
- 디테일러 이름은 `detailer.name`으로 찾을 것(§3a) — `app_user` 조인은 일부 누락.

```sql
-- 특정 세차일에 체크리스트 미제출인 근무자 (= 슬랙 명단 재현)
SELECT d.id, d.name, COUNT(DISTINCT r.id) rsv,
       (SELECT CAST(s.submitted_at AS CHAR) FROM detailer_checklist_submission s
         WHERE s.detailer_id = d.id AND s.date = '2026-09-02') sub
FROM reservation r JOIN detailer d ON d.id = r.detailer_id
WHERE DATE(DATE_ADD(r.reservation_datetime, INTERVAL 9 HOUR)) = '2026-09-02'
  AND r.status = 'CONFIRMED'
GROUP BY d.id HAVING sub IS NULL;
```

⚠️ **"앱에 완료라고 떴다"는 디테일러 신고는 DB로 판정할 것.** 2026-09-01까지 zero-api가 마감 후 미제출을 '해당없음'으로 분류해, 미제출자가 22:00~24:00에 앱을 열면 배너가 `완료`로 떴다(PR #1875로 prod 수정 완료). 그 이전 날짜의 신고를 조사할 때는 화면이 근거가 못 된다.

---

## 4. 검증 기준 (Invariant)

분석 결과가 아래를 위반하면 쿼리 로직에 버그가 있는 것:

1. **예약 수 ≤ 공급 수**: 어떤 날/시간대든 예약 디테일러 수가 스케줄 공급보다 클 수 없음
2. **Fill Rate 0~100%**: 분자(예약) ≤ 분모(공급)이므로 100% 초과 불가
3. **일 최대 슬롯 ≤ 7**: 디테일러 1인당 하루 최대 예약 7건 (API 코드 상한)
4. **CREATED 미포함**: 분석 대상 예약에 `status='CREATED'`가 섞이면 안 됨
5. **start_time 매칭**: `HOUR(start_time) <= 23` 같은 항상-참 조건은 버그. 슬롯별 정확한 UTC 시간만 매칭할 것

**검증 스크립트:**
```bash
./scripts/validate-analysis.sh                        # 이번 달
./scripts/validate-analysis.sh 2026-03-01 2026-03-23  # 기간 지정
```
검증 항목: 예약≤공급, 상태 분포, 디테일러 필터 수, Fill Rate 범위, start_time 분포

---

## 4b. 월별 실적·코호트 쿼리 함정 (2026-07-28 실측)

월 단위 실적·리텐션·매출을 뽑을 때 **조용히 틀리는** 지점들. 전부 prod 실측으로 확인됐고, 합계 검증만으로는 안 잡히는 것들이 섞여 있다.

### 4b-1. 코호트·세그먼트: 유저 이력 CTE에 날짜 하한을 걸지 마라

신규/온보딩/충성/복귀 같은 세그먼트를 뽑을 때, **유저 이력을 만드는 CTE**(`all_washed`/`washed`)에 하한을 걸면 `MIN(세차일)`이 "진짜 첫 세차일"이 아니라 **"창 안에서의 첫 세차일"**이 된다. 그러면 오래 쉬다 돌아온 유저의 first_d가 복귀월로 잡혀 `first_d < 전월시작` 조건에 못 걸리고 **어느 세그먼트에도 안 들어간다.**

```sql
-- ❌ 틀림: 이력 CTE에 하한
washed AS (SELECT ... FROM reservation WHERE ... AND 세차일 >= '2025-08-01')
-- ✅ 맞음: 이력은 무제한, 출력 월 범위만 제한
washed AS (SELECT ... FROM reservation WHERE status IN ('WASHED','REPORT_SENT') AND deleted_yn=0)
```

실측 누락: 복귀(resurrected) 2026-04 −36명 / 05 −11 / 06 −21. 누락 수가 오차와 정확히 일치.
⚠️ **"세그먼트 합 = active" 검증으로는 이 버그가 안 잡힌다** — 누락분이 다른 버킷으로 밀려 총합이 보존된다. **월별 값을 확정 실적과 개별 대조**할 것(허용 ±5).
빠른 진단: SQL 안의 날짜 리터럴 개수를 세라. 이력 하한이 있으면 정상 메트릭보다 하나 많다.

### 4b-2. 상태 스냅샷: 미래 데이터를 참조하면 과거가 소급 변한다 (look-ahead 편향)

"그 달에 휴면이었나" 같은 **시점 상태**를 구할 때 `MAX(전체 기간 세차일)`을 쓰면 **미래의 세차가 과거 달의 판정을 지운다.**

```sql
-- ❌ 틀림: 전체 기간 MAX → 2026-06에 복귀하면 2026-05 휴면 판정이 사라짐
user_last AS (SELECT user_id, MAX(k) last_d FROM all_washed GROUP BY user_id)
... WHERE last_d < 월시작
-- ✅ 맞음: 해당 월 시작 이전만 참조
ever_before  = 첫세차 < 월시작인 유저
returning_in = 그 중 해당 월에 세차한 유저
pool         = ever_before − returning_in
```

실측: 휴면풀이 2026-04 −938 / 05 −781 / 06 −295 과소. **오차가 최근일수록 작아지는 것이 이 버그의 지문**(과거 달일수록 지울 미래 데이터가 많다). 고치면 별도 산출 기준과 0.1% 안에서 일치.

### 4b-3. 확정 월별 실적의 기준일은 `reservation_datetime`이다

세차 건수·활성 유저수·세그먼트·세차 매출 **전부** `reservation_datetime` 기준이다. `washed_at`을 쓰면 어긋난다.

실측(2026-06 카라멜 세차): `washed_at` 3,922 / **`reservation_datetime` 3,971 = 확정값**.
⚠️ `washed_at`을 쓰면 **예약일이 250일 전인데 완료가 이번 달인 건**(2026-07에 216건 관측) 같은 케이스가 엉뚱한 달로 귀속돼 "세차당 매출"이 특정 월에서 과소·다른 월에서 과대로 왜곡된다.
⚠️ 완료 세차 중 `washed_at IS NULL`이 소수 존재(2025-08~2026-06 0.05~0.88%, 진행 중인 달은 최대 3.7%) → `washed_at` 기준은 그만큼 언더카운트한다.

### 4b-4. 세차 매출 SQL은 월 단위로만 정확하다

`scripts/tmp_mar_revenue.sql`은 **기간 창을 여러 달로 넓히면 값이 샌다.** 결제 1건이 여러 달에 걸쳐 소진되는 구조(구독·다회권) 때문에 payment 단위 집계가 월 경계를 넘는다.

실측(2026-06): 단일월 실행 **216,111,879원**(확정값 일치) vs 12개월 한 번에 실행 217,412,416원(+130만원).
→ **CTE를 다월용으로 재작성하지 말고 월 창을 갈아끼워 N번 실행하라.** 부분 수정(내부 GROUP BY에 month 추가)으로는 안 막힌다. 그리고 이 SQL이 월마감이 쓰는 정의이므로 재작성하면 마감과 다른 숫자가 나온다.

### 4b-5. 부분취소(PARTIAL_CANCELED) 취소액을 차감하라

매출 SQL이 `status IN ('PAID','PARTIAL_CANCELED')`로 부분취소 결제를 **포함시키면서 `cancel_amount`를 빼지 않으면** 그만큼 과대계상된다.
→ 포인트와 **동일하게 비례배분** 차감: `cancel_alloc = ROUND(cancel_amount × 항목_정가 / 정가합)`.
실측 과대분: 2026-03 ~194만 / 04 ~91만 / 05 ~143만 / 06 ~314만원. **12개월 누적 약 1,090만원.**

🔴 **`payment.status`에는 철자 2종이 섞여 있다 — `CANCELLED`/`PARTIAL_CANCELLED`(L 두 개) (2026-08-25 실측).** 전 기간 `CANCELLED` 24건·`PARTIAL_CANCELLED` 4건(2025-01~2026-06, 지금도 발생). `IN ('PAID','PARTIAL_CANCELED')`나 `status='CANCELED'`로 쓰면 이 건들이 조용히 반대편으로 샌다(취소분이 매출에 남거나, 취소 집계에서 빠짐). → **`status LIKE 'PA%'`(PAID+PARTIAL_*) 또는 6종 전부 열거**로 방어할 것.

### 4b-6. 합산 CPA는 채널 믹스가 바뀌면 해석이 없다

광고비는 3채널 합산이 맞지만(§6 참조), **믹스가 급변한 구간에서 합산 CPA를 효율 지표로 읽으면 오독한다.**

| 월 | Meta | Naver | Google | 합계 | 신규 첫구매 | 합산 CPA |
|---|---:|---:|---:|---:|---:|---:|
| 2026-03 | 1,264 | 59 | – | 1,323 | 853 | 1.55 |
| 04 | 1,494 | 209 | 10 | 1,713 | 816 | 2.00 |
| 05 | 1,371 | 158 | 590 | 2,119 | 565 | 3.75 |
| 06 | 911 | 245 | 1,044 | 2,200 | 670 | 3.28 |

(단위 만원) Meta를 끄고 Google로 갈아탄 결과이지 한 채널이 나빠진 게 아니다. **예산을 +66% 올린 구간에서 신규는 853→670으로 줄었다** — "예산↑ ⇒ 신규↑ 비례" 가정은 실측과 부호가 반대다.
→ CPA 변화를 논할 때 **채널별 집행액을 같이 뽑아 믹스 변화를 먼저 배제**하라. ⚠️ 다만 **채널별 신규(분모)는 현재 못 만든다** — `user_attribution` 귀속률이 2026-06~07도 unknown 55%다(그 이전은 98~99%).

### 4b-7. 외부공급(헤이딜러) 세차 = `manual_wash_adjustment` 전체 합산

memo 화이트리스트로 좁히지 마라. `memo IN ('헤이딜러','조준호')`로 거르면 표기 변형(`'인천 테슬라 (조준호)'` 등)을 놓친다.
memo 실측(2026-01~07, 247행): 헤이딜러 4,559 / 조준호 527 / 인천 테슬라 (조준호) 30 / 현장 긴급 세차 36 / 마이바흐 추가세차분 반영 11 → **전부 외부공급**.
⚠️ `wash_date`는 **DATE 타입**이라 KST 변환 불필요. ⚠️ 테이블이 **2026-01-02부터** 시작하므로 2025년은 0행이 정상.
⚠️ **파견 디테일러(`detailer_supply_sheet.status='파견'`)의 앱 예약은 외부공급이 아니다** — 카라멜 자산으로 센다. 2025-08~2026-02에 669건 있고 고객이 개인 585명(평균 1.14건)인 일반 소매 예약이다. `manual_wash_adjustment`는 앱에 안 잡히는 현장 물량을 따로 더하는 장치라 두 소스는 겹치지 않는다.

### 4b-8. live_user 블랙리스트에 `OR phone IS NULL`을 빼먹지 마라

`phone NOT IN (...)`만 쓰면 SQL의 `NULL NOT IN (...)` 규칙 때문에 결과가 NULL(falsy)이 되어 **phone이 NULL인 유저가 조용히 제외된다.**

```sql
AND (u.phone NOT IN ('01020866510', ...) OR u.phone IS NULL)
```

같은 분석 안에서 어떤 지표는 블랙리스트를 쓰고 어떤 지표는 안 쓰면 **분자와 분모가 다른 모집단**이 된다(실측: 세차 건수 12개월 합 ~100건 오염).

### 4b-9. `payment_medium`은 2025-10부터 존재한다

기존 문서 일부에 "2025-09부터"로 적혀 있으나 실측은 **2025-09까지 0건, 2025-10부터 전환 시작**(2025-10 혼재, 2025-11 이후 거의 전부).
→ 그 이전 기간의 포인트는 `metadata.$.point` 폴백 필수: `COALESCE((payment_medium POINT SUM), metadata.$.point, 0)`. 안 하면 과거 포인트 차감이 0이 되어 매출이 과대된다.

🔴 **`payment_medium`은 CASH·POINT 행을 항상 쌍으로 만든다 — POINT 행 존재 ≠ 포인트 사용 (2026-08-25 실측).** 2026-05~08 기준 CASH 14,614행 / POINT 14,614행으로 정확히 같고, 그 POINT 중 **11,089행(76%)이 `amount=0`**이다. "포인트 쓴 결제 수"를 `EXISTS(medium='POINT')`로 세면 실제 3,525건이 14,614건으로 **4.1배** 부푼다. → 반드시 `medium='POINT' AND amount>0`. 반대로 **전액 포인트 결제는 CASH 행이 `amount=0`으로 남는다**(2026-05~ PAID 1,110건) — 카드 결제 건수를 CASH 행 존재로 세도 같은 함정.

### 4b-12. 연도별과 누적을 한 화면에 쓸 때는 필터를 통일하라 (2026-08-04 실측)

같은 "세차 완료 건수"인데 **연도별은 raw(사용자 필터 없음), 누적은 live_users 필터**로 뽑아 한 장표에 나란히 두면 합계가 안 맞는다. 실측 갭 **437건(1.04%)** — 손으로 더하는 자리에서 바로 들킨다.

| 기준 | 2024 | 2025 | 2026 1~7월 | 합계 |
|---|---:|---:|---:|---:|
| raw (필터 없음) | 2,118 | 16,474 | 24,258 | 42,850 |
| **live_users (정본)** | **2,076** | **16,298** | **24,078** | **42,452** |

- 원인 2가지: (1) `live_users`(deleted/test/temp + 블랙리스트 전화번호 + `OR phone IS NULL`) 적용 여부 (2) **측정 시점** — 같은 정의라도 7/29 스냅샷과 7/31 마감이 400건 이상 벌어진다.
- ⟹ **한 장표·한 문단에 들어가는 수치는 같은 쿼리에서 한 번에 뽑는다.** 연도별을 A에서, 누적을 B에서 가져오지 말 것. 인용할 땐 "테스터 제외 실고객 기준 · YYYY-MM-DD까지"를 같이 적는다.
- ⚠️ 모델·재무 시트의 2025 세차 완료수(16,301)는 또 다른 스냅샷이다. DB 값(16,298)과 3건 차이라 실무상 무해하지만, **DB 수치와 모델 수치를 같은 표에 섞지 말 것.**

### 4b-13. 🔴 과거 월의 세차 완료수는 계속 바뀐다 — 원인 3가지 (2026-08-06 실측)

"이미 지난 달인데 왜 숫자가 달라지냐"의 답. **세차가 늦게 완료돼서가 아니다** (2026-04 완료 4,037건 중 4,034건이 세차 당일 `wash_result` 입력, 나머지 3건은 소급 생성 예약).

| 원인 | 방향 | 메커니즘 | 2026-04 실측 |
|---|---|---|---:|
| 고객 탈퇴 | 감소 ↓ | `live_users`의 `deleted_yn=0` → 탈퇴하면 **그 사람의 과거 세차가 전 기간에서 사라진다** | 21건 (탈퇴 시점 4월 6·5월 11·6월 1·7월 2·8월 1) |
| 수기 외부공급 입력 지연 | 증가 ↑ | `manual_wash_adjustment`는 사람이 매일 입력 → 월말 며칠분이 익월 초에 들어옴 | 78건(4/29·4/30분이 5/1~5/2 입력). 07월은 151건이 8월 입력 |
| 소급 예약 생성 | 증가 ↑ | 어드민이 나중에 과거 `reservation_datetime`으로 예약 생성 | 4건 |

- ⟹ **마감 확정값(`Caramel_monthly(A)`)이 정본이고 Grafana는 모니터링용.** 두 숫자가 다르면 버그가 아니라 조회 시점 차이일 수 있다. 인용 시 `YYYY-MM-DD 조회` 병기(§4b-12).
- ⚠️ 정의 변경도 과거를 소급으로 바꾼다 — 2026-07-28 외부공급 memo 화이트리스트 → 전체 합산(§4b-7) 전환으로 2026-02가 +30건 됐다.

### 4b-14. 🔴 "언제 완료됐나"를 `reservation.modified_at`으로 판정하지 마라 (2026-08-06 실측)

`modified_at`은 무관한 배치·셔플·백필에도 갱신된다. 2026-04 완료 예약 36건이 `modified_at` 2026-07~08이라 "3개월 늦게 완료된 건"으로 보였지만, **전부 `wash_result.created_at`이 세차 당일**이었다(7/19~21에 뭔가가 훑고 지나간 흔적일 뿐).

- **완료 시점 정본 = `wash_result.created_at`(≈`checkup.created_at`, 같은 값).** 지연 판정은 `DATEDIFF(wr.created_at, KST(r.reservation_datetime))`.
- 🔴 **`reservation_change_log`는 2026-05-22부터만 존재한다** (전체 18,824행, 그 이전 0행). 2026-04 이전 기간의 상태 전이·재배정 이력은 **이 테이블로 추적 불가** — 없다고 "변경이 없었다"로 읽지 말 것.
- 소급 생성 예약 판별: `DATE_FORMAT(KST(r.created_at),'%Y-%m') > DATE_FORMAT(KST(r.reservation_datetime),'%Y-%m')`.

### 4b-10. 경과일 버킷의 누적 평균은 시계열로 비교할 수 없다 (2026-08-04 실측)

구독 코호트의 "경과일 버킷별 누적 세차 횟수"를 뽑으면 버킷마다 **모집단이 다르다.** 실측:

| 버킷 | 관찰 n | 누적 평균 세차 |
|---|---:|---:|
| D1-90 | 6,282 | 2.09회 |
| D91-180 | 3,112 | 4.11회 |
| D181-270 | 1,675 | **4.97회** |
| D271-365 | 814 | **4.09회** |

D181-270이 D271-365보다 높은 건 개선이 아니라, D181 버킷에 **D271 도달 전 해지한 유저가 섞여** 있어서다. 같은 유저를 따라간 값이 아니므로 "6개월차에 4.97 → 1년차에 4.09로 줄었다"는 해석은 틀린다.
- **연간 횟수를 말할 땐 12개월 완주 코호트만 쓰고 `n`을 병기한다** — 실측 = 완주 814명 · 평균 4.1회 · **중앙값 2회**(평균만 쓰면 과대). 상품별로 크게 갈린다(월2회 8.0 / 월1회 4.8 / 월1회 외부+내부 5.6 / 두달1회 2.5 / 상품 미지정 1.2 / 1개월 무료 0.6).
- 비교군은 비구독(1회권) 1.6회 → 구독/비구독 = **2.6배**.
- 절대 하지 말 것: 짧은 버킷 평균에 연환산 계수를 곱하기. §4b-1(이력 CTE 하한)과 같은 계열의 오류다.

### 4b-11. 🔴 '월 2회(외부만)' 구독 리텐션은 기존 기록값이 재현되지 않는다 (미해결)

아래 정의로 짜면 기존 기록과 **8.5%p까지 벌어진다.** 어느 쪽이 맞는지 미확정이므로 **인용 시 반드시 쿼리 정의와 n을 같이 쓴다.**

```sql
-- 재현 시도한 정의 (2026-08-04)
payment.name LIKE '월 2회(외부만)%' AND payment.type='SUBSCRIPTION' AND payment.amount > 0
  + car_model_target.is_target = 1        -- 타겟 차량 보유
  + first_paid_kst 기준 cohort_date, D1~365 경과일 버킷 MAX 활성 여부
```

| 버킷 | 위 정의 (n=490) | 기존 기록 (n=368) |
|---|---:|---:|
| D1-30 | 89.0% | 96.5% |
| D151-180 | **57.8%** | **66.3%** |
| D181-210 | 54.1% (분모 292) | — |
| D211-240 | 47.5% (분모 80) | — |

- 원본이 **세차 활동 기준 추가 필터**를 걸었을 가능성이 크다(결제 존재 ≠ 실사용). 확인되면 이 절을 갱신할 것.
- **분모를 반드시 라벨링한다** — 코호트 리텐션(분모=코호트 전체)과 조건부 생존율(분모=직전 버킷 생존자)은 전혀 다른 값이다. 위 표는 코호트 리텐션이고, 조건부 생존율은 버킷마다 83~88%로 훨씬 높게 보인다. 섞으면 큰 오차.
- **상품 출시가 2025-10이라 D271 이상 도달자가 0명이다** — "1년 리텐션"은 아직 데이터가 없다. 만들어 쓰지 말 것.

### 4b-13. 전화번호 리스트를 DB에 대조할 땐 `GROUP BY 전화번호` — `app_user.id`로 묶으면 한 사람이 쪼개진다 (2026-08-04 실측)

**한 전화번호에 `app_user` row가 여러 개 존재한다.** 영업·CS 리스트(시트)를 번호로 조회할 때 `JOIN app_user` 후 `GROUP BY u.id`로 집계하면 **같은 사람이 계정 수만큼 여러 행으로 갈라지고, 예약이 한쪽 계정에만 있으면 다른 행은 "활동 0"으로 나온다.** 대상자 명단을 뽑는 쿼리에서 이건 오탐이다.

```sql
-- 정본: 번호로 묶고, 계정 수도 같이 확인
SELECT REPLACE(u.phone,'-','') AS ph, COUNT(DISTINCT u.id) AS accounts, SUM(...) 
FROM app_user u
LEFT JOIN reservation r ON r.user_id=u.id AND r.status NOT IN ('CANCELED','CREATED')
WHERE REPLACE(u.phone,'-','') IN (:phones)
GROUP BY ph          -- ⚠️ GROUP BY u.id 아님
```

- 실측(반얀 고객 552개 번호 대조): **33/552 = 6.0%가 계정 2~3개**, 그중 **11개 번호는 조회 기간 예약이 한쪽 계정에만 몰려 있었다** → `u.id` 기준으로 짰으면 11명(2.0%)이 "미접촉"으로 잘못 분류된다. 차량·주소로 유저를 좁혀 들어가는 경로(`car.user_id` 등)도 같은 함정.
- 시트 번호는 `010-1234-5678` 하이픈 포함이 흔하므로 **양쪽 다 `REPLACE(phone,'-','')`** 로 정규화. `app_user.phone`은 하이픈 없는 게 원칙이지만 신뢰하지 말 것.
- ⚠️ **번호 자체가 깨진 계정이 있다** — 실측으로 `app_user.phone='0079'`(4자리)가 존재. `LENGTH(REPLACE(phone,'-',''))<>11`을 먼저 세서 "매칭 실패"와 "번호 불량"을 구분할 것. 관련: 050 vno는 phone이 아니라 user_id 단위 키잉(§ `customer_vno`).

---

### 4b-14. 시점 기준 미사용 세차권(선수금) 잔액 — `used_yn`은 **현재** 상태다 (2026-08-05 실측)

"월말 기준 미이행 세차권이 얼마였나"(선수금·이연매출 추이)를 구할 때 `used_yn=0`을 쓰면 **과거 시점이 통째로 과소집계된다.** `used_yn`은 오늘 상태이므로, 과거에 미사용이었지만 그 후 소진된 세차권이 전부 빠진다. 최근 달일수록 오차가 작아지는 것이 이 버그의 지문이다(§4b-2와 같은 계열).

- **`user_service`에 `used_at` 컬럼이 없다.** 소진 시점의 정본 = **연결된 `reservation`의 세차일**.
- 시점 D의 미사용 판정 4조건: ①`created_at ≤ D` ②`deleted_yn=0 OR deleted_at > D` ③`ended_at IS NULL OR ended_at > D`(만료분은 부채 아님) ④`reservation_id IS NULL` **또는** 연결 예약의 세차일 `> D` **또는** 그 예약이 `WASHED/REPORT_SENT`가 아님.

```sql
LEFT JOIN (SELECT id, DATE_ADD(reservation_datetime, INTERVAL 9 HOUR) AS wash_kst, status
           FROM reservation WHERE deleted_yn=0) r ON r.id = us.reservation_id
WHERE us.reservation_id IS NULL OR r.id IS NULL
   OR r.wash_kst > :asof OR r.status NOT IN ('WASHED','REPORT_SENT')
```

- 실측 격차: 2026-03-31 잔액이 `used_yn=0`으로는 6,660장, 시점 판정으로는 **10,652장 — 37% 과소**.
- **금액은 `service.price`(정가)로 환산할 것.** `user_service.paid_amount`는 2026-05분부터만 채워져 추이를 못 만든다(위 `user_service` 치트시트). 정가 기준이라 실수령액보다 크다는 단서를 반드시 병기.
  - 🔴 **커버리지 실측(2026-08-30, `deleted_yn=0` 전수): 2024년 0/5,456 · 2025년 0/28,805 · 2026년 7,750/40,083 = 19.3%.** 즉 2026년 안에서도 5건 중 1건만 값이 있다. **`paid_amount > 0`으로 "유료 전환했나"를 판정하면 과거 코호트가 통째로 미전환으로 잡힌다** — 실사고: 선물 수령자 174명의 유료 전환을 이 컬럼으로 재서 **27.0%를 2.3%로 오판**했다. 정답 경로는 `payment`(`deleted_yn=0 AND status='PAID' AND amount>0`)를 유저·시점으로 조인하는 것.
- **발급 경로를 갈라야 해석이 된다** — `promotion_application_id`/`coupon_code_reward_id`/`coupon_campaign_reward_id` 중 하나라도 있으면 무상권, 아니면 `payment_id` 유무로 '결제로 발급' vs '어드민 지급'. 실측(2026-06-30): 결제 발급 4.90억 / 어드민 지급 1.33억 / 무상 0.15억 — 무상권을 섞으면 "선수금 증가"가 부풀려진다.

### 4b-15. 세차 객단가 = 정의부터 맞춰라. 그리고 상승 원인은 **정가 vs 실현율**로 갈라야 한다 (2026-08-05 실측)

- **카라멜 세차 객단가 정본 = (전체 세차매출 − 헤이딜러 건수×8.80만) ÷ 카라멜 세차 횟수.** `Caramel_monthly(A)` 시트 22행('세차 객단가 > 카라멜')이고, `tmp_mar_revenue.sql`의 `avg_revenue_per_wash`와 같은 값이 나온다(실측 2026-04 50,020원 / 05 51,657 / 06 54,423 = 시트 5.01·5.17·5.44와 일치).
- ⚠️ **`세차 매출 ÷ 합계 세차 횟수`로 계산하면 틀린다** — 헤이딜러(외부공급) 건수가 분모에 섞여 값이 눌린다(2026-04 기준 4.88만 vs 정본 5.00만).
- 🔴 **객단가 상승을 "옵션이 팔렸다"로 먼저 결론내지 말 것.** 분해 순서는 ①`item_kind`별(세차권/옵션/서비스변경) 기여 ②세차권 안에서 **정가 불변인지** 확인. 실측 2026-04→06 +8.8% 중 세차권 73% · 옵션 23%였고, **정가 인상은 0원이었다** — 같은 티어·같은 서비스의 `originalPrice`가 그대로였고(1회권 외부+내부 T3 59,536→60,000) 바뀐 건 **정가 실현율**이다(외부만 T4 67%→97%, 외부만 T5 48%→98%). 즉 원인은 가격 인상이 아니라 **프로모션·쿠폰 할인 축소 + 무상(0원) 세차 비중 감소**다.
- 🔑 **"슬롯 하나가 얼마짜리냐"(구독 세그먼트별 단가)는 정가 ÷ 횟수로 계산하지 말 것.** 일시정지·미사용·부분환불 때문에 실현값과 벌어진다. 정본 = **그 달 그 상품의 구독 결제액 ÷ 그 달 그 구독으로 나간 완료세차 건수**(결제는 `payment.type='SUBSCRIPTION' AND status IN ('PAID','PARTIAL_CANCELED') AND amount>0`, 세차는 `user_service.subscription_id` 경유). 2026-08 실측: 외부만 월2회 26,140원 · 외부만 월4회 24,534원 · 그 외 구독 81,539원 — 정가 나누기(월2회 42,461÷2=21,231)보다 20% 이상 높다.
- **실현율 뽑는 법**: `tmp_mar_revenue.sql`의 `items_with_point`에 이미 `original_price`(=`metadata.prices[].originalPrice`)가 있다. `SUM(net_amount)/SUM(list_amount)`를 **동일 `service_id`(티어×세차범위) 단위로** 볼 것 — 티어 믹스가 섞이면 가격 변화와 구분이 안 된다.

---

### 4b-16. 🔴 코호트 성숙게이트는 **버킷(주/월) 단위**로 걸어라 — 유저 단위면 마지막 막대가 매주 바뀐다 (2026-08-10 실측)

"등록 후 N일 내 전환" 같은 코호트 지표에서 성숙게이트를 **유저 단위**(`user.first_event + INTERVAL N DAY <= NOW()`)로 걸면, 마지막 버킷이 **부분 코호트**가 된다. 그 주에 속한 유저 중 N일이 지난 사람만 들어가므로, 같은 SQL을 다음 주에 돌리면 **같은 막대의 값이 바뀐다**(재현 불가).

- 실측(ap4j74 #213, 30일 게이트): 2026-07-06주 등록 172명 중 **141명(82%)만** 집계 → 표시 18.4%. 나머지 31명이 성숙하면 값이 달라진다.
- **정본 = 버킷 전체가 성숙한 버킷만 표시.**
  - 주간: `DATE_ADD(bkt, INTERVAL 6 DAY) + INTERVAL N DAY <= DATE(now_kst)`
  - 월간: `LAST_DAY(bkt) + INTERVAL N DAY <= DATE(now_kst)`
- **게이트 일수 = 전환 창 일수와 같아야 한다.** 창 없이 생애(ever) 전환을 세면서 게이트만 N일로 걸면, 성숙 안 된 전환이 계속 쌓여 과거 막대가 자란다.
- **창 길이는 lag 분포를 재서 정하라.** 게이트가 길수록 최신 막대가 뒤로 밀린다(30일 게이트 = 최신 막대가 약 5주 지연). 실측 결과 30일 내 전환의 92%가 14일 안에 끝나 창을 14일로 줄였다(차량등록→예약 91.8% · 첫신청→첫세차 92.4%).

---

### 4b-17. 🔴 **미래 확정예약의 세그먼트 구성은 실제 구성이 아니다 — 예약 리드타임이 세그먼트마다 5배 갈린다 (2026-09-09 실측)**

"앞으로 잡혀 있는 예약이 누구 것이냐"를 `reservation_datetime >= NOW()`로 세면 **구독이 과대**하게 나온다. 구독 자동예약은 배치로 두 달치 달력을 미리 잡고, 1회권 고객은 임박해서 잡기 때문이다.

2026-08 완료세차 기준 `DATEDIFF(reservation_datetime, created_at)`:

| 세그먼트 | n | 평균 리드 | ≤3일 | >14일 |
|---|---:|---:|---:|---:|
| 외부만 월2회 | 1,617 | 46.3일 | 134 | 1,390 (86%) |
| 외부만 월4회 | 352 | 45.8일 | 27 | 293 |
| 그 외 구독 | 303 | 5.5일 | 169 | 27 |
| 비구독 | 1,421 | 8.6일 | 884 (62%) | 126 |

- 실측 괴리: 같은 날 미래 확정예약으로 재면 외부만 구독이 **4,675/5,179 = 90.3%**인데, 실제로 끝난 세차(2026-08)로 재면 **1,900/3,564 = 53.3%**다. 37%p 차이가 전부 리드타임 비대칭이다.
- ⟹ **점유율·믹스·객단가는 완료(`WASHED`/`REPORT_SENT`) 기준으로 센다.** 미래 예약은 "지금 달력에 무엇이 잡혀 있나"에만 쓰고, 그 비율을 최종 구성으로 인용하지 말 것.
- 🔴 미래 예약 건수는 날짜가 멀수록 급감한다(2026-09-09 기준: 9월 2,224 · 10월 1,987 · 11월 690 · 12월 219). **먼 달의 낮은 건수를 "수요 감소"로 읽지 말 것** — 예약 지평선 산물이다.


## 5. 공통 패턴

### 5a. KST 변환

DB는 UTC 저장 → `CONVERT_TZ(col, '+00:00', '+09:00')` 또는 `+ INTERVAL 9 HOUR`

GROUP BY에 날짜 쓸 때 반드시 KST 변환 후 사용.

예외: `paused_at`, `ended_at`은 코드에서 KST(`Asia/Seoul`)로 할당 → UTC +9H 변환 불필요.

**🔴 정정: `reservation.created_at`은 UTC다 (2026-08-06 재실측). 아래 "KST 벽시계" 서술은 틀렸다.**
- **재실측 근거 2개.** ①`NOW()`=17:02 KST(session tz `Asia/Seoul`)일 때 `MAX(created_at)`가 `reservation`·`message`·`payment`·`user_service` **4개 테이블 전부 08:0x** = 정확히 −9h. ②`created_at` 시각 분포에서 **0–5시가 4,150/6,577 = 63.1%**(2026-05, 06·07·08월도 63~68%로 동일). 사람이 새벽에 예약을 63% 만들 리 없고, 0–5시 UTC = 09–14시 KST 업무시간이다.
- 즉 **`+ INTERVAL 9 HOUR` 변환이 필요하다.** `DATE_FORMAT`으로 뽑은 문자열을 KST로 읽으면 9시간 어긋난다.
- 🔴 **그래서 `NOW()`를 상대 기간 필터에 쓰면 항상 9시간 어긋난다 (2026-09-06 실측).** 세션 tz가 `Asia/Seoul`이라 `NOW()`·`CURDATE()`는 **KST**를 돌려주는데(`SELECT DATE_FORMAT(NOW(),'%H:%i')` = 벽시계 그대로), 비교 대상인 `reservation_datetime`·`created_at`·`washed_at`은 `datetime` 타입 **UTC 저장**이다. `WHERE created_at >= DATE_SUB(NOW(), INTERVAL 1 MONTH)` 같은 식은 창을 9시간 밀어 놓는다 — 건수가 그럴듯하게 나와서 눈에 안 띈다. ⟹ **UTC 컬럼과 비교할 땐 `UTC_TIMESTAMP()`를 쓰고, `NOW()`는 KST 저장 컬럼(`modified_at`·`paused_at`·`ended_at`)에만 쓴다.**
- ⚠️ **이 오기가 실제로 사고를 냈다 (2026-08-06):** 티켓 러너 세션이 이 문장을 믿고 정상적인 티켓 본문(`10:38 KST`)을 `01:38 KST`로 "정정"했다. 같은 날 다른 세션은 SENS 응답의 KST `requestTime`과 19,606행 대조로 UTC임을 독립 확인했다.
- (구 서술: "MySQL `DEFAULT CURRENT_TIMESTAMP`=서버 KST라 `created_at`/`modified_at`은 KST 벽시계, 2026-07-12 실측" — 최소 2026-05 이후 데이터에선 성립하지 않는다. `modified_at`은 **KST 저장으로 재확인됐다**(2026-09-04: 대량 재배정 행의 `modified_at + 9h`가 `22:26`으로 나왔는데 조회 시각이 `15:02`였다 — 미래값이 나오면 그 컬럼은 이미 KST다. 실제 시각은 13:26). ⟹ `reservation.modified_at`에 `+9h`·`CONVERT_TZ`를 걸지 말 것.)
- **디테일러 재배정 역추적 시그니처**: 재배정 전용 이력 테이블/로그 type은 없다. `modified_at`의 **정각 분대 = 셔플 크론이 detailer_id 변경**, 그 직후 분대(예 :51) = 사람이 어드민에서 재배정했을 개연성 (2026-07-13 임세혁 셔플 진단 실사례). 🔴 **크론 시각은 2026-07-30부로 17시 → KST 14시**(PR #515) — 지문은 `HOUR(modified_at)=14 AND MINUTE(modified_at)=0`이고, 그 이전 날짜를 조사할 때만 17시를 쓴다. `modified_at`은 **KST 저장**이라 `+9h` 하지 말 것.
  - 🔴 **단 "change_log엔 아예 안 남는다"는 반쪽 진술이다 — 두 경로를 함께 봐야 한다 (2026-07-27 예약 #79702 실측 교정).**
    - **고객이 날짜를 바꾸면서 디테일러도 바뀐 경우는 남는다**: `RESERVATION_DATETIME_CHANGED` row의 `data` JSON에 `fromDetailerId`/`toDetailerId`가 같이 실린다(#79702: 고객이 7/27→7/28 변경하며 78 이승제→88 강지성). **재배정 전용 `data.type`이 없어서 datetime 로그 안에 숨어 있다** — `data.type`으로 재배정을 찾으면 못 찾는다. `WHERE JSON_EXTRACT(data,'$.toDetailerId') IS NOT NULL`로 잡을 것.
    - **셔플 크론·어드민 단독 재배정은 안 남는다**: 같은 #79702가 다음날 17:00 셔플로 88→78 되돌아왔는데 change_log엔 row 0건. 이건 `modified_at`으로만 추적된다.
    - ⟹ change_log만 보면 셔플 이동을 놓치고, `modified_at`만 보면 고객 변경분의 before/after 디테일러를 잃는다. **"재배정 이력" 질문엔 항상 두 쿼리를 돌릴 것.**
  - 🔴 **초는 `00`이 아니다 — `TIME(modified_at)='17:00:00'` 등호 필터는 0건이 나온다 (2026-07-26 실측).** 배치가 17:00:00에 시작해 수 초간 쓰므로 실제 저장값은 `17:00:14` 같은 형태다. **판별은 `HOUR(modified_at)=17 AND MINUTE(modified_at)=0`.** "정각"이라는 표현에 낚여 초까지 등호 비교하면 "셔플이 안 돌고 있다"고 오판한다 — 실제로는 매일 돌고 있다(7/23~26 각 12·27·50·93건 변경).

**⚠️ mysql-query.sh DATETIME 렌더링 함정 (읽기·쓰기 둘 다 치명적)**
- DATETIME 컬럼을 그냥 SELECT하면 `...Z` ISO로 보이지만 **실제 저장값이 아니라 저장값−9h** (드라이버가 naive 값을 KST 로컬로 해석해 UTC ISO로 직렬화).
- 저장 원문이 필요하면 `DATE_FORMAT(col,'%Y-%m-%d %H:%i:%s')`로 문자열화해서 읽을 것.
- 🔴 **읽기 판정도 뒤집는다 (2026-09-01 실사례).** `detailer_holiday` 겹침 조회에서 raw JSON의 `"2026-08-30T06:00:00.000Z"`를 UTC로 믿고 "9/2를 덮는 휴무 0건"이라 결론냈는데, 저장 원문은 `2026-08-30 15:00:00`(=KST 8/31 00:00)이라 **9시간 어긋난 오답**이었다. ⟹ 날짜 경계가 걸린 판정은 처음부터 `DATE_FORMAT(DATE_ADD(col, INTERVAL 9 HOUR),'%Y-%m-%d %H:%i:%s')`로 KST 문자열을 뽑아 볼 것.
- 세션 tz는 `Asia/Seoul`, 컬럼은 `datetime`(MySQL 무변환)이라 **저장 원문 = UTC 벽시계**다. 드라이버만 이걸 KST로 오해한다. Prisma는 같은 값을 UTC로 읽으므로 **앱이 보는 값 = DATE_FORMAT 원문**이고, JSON 렌더값이 아니다.
- INSERT/UPDATE의 인라인 리터럴은 **verbatim 저장**됨 → SELECT에서 본 `Z` ISO 값을 그대로 복붙해 넣으면 9시간 어긋난다. 반드시 DATE_FORMAT으로 읽은 원문 기준으로 쓸 것.

**⚠️ mysql-query.sh 실행 함정 (2026-09-08)**
- **절대경로로 부르면 죽는다** — `~/.caramel-team-setup/mysql-query.sh "..."`는 `Cannot find module 'mysql2/promise'`로 실패한다(`node_modules`가 그 디렉터리에 있고 스크립트가 cwd 기준으로 require). **`cd ~/.caramel-team-setup && ./mysql-query.sh "..."`로 부를 것.** 스킬·문서에 절대경로로 적힌 곳이 있으니 그대로 믿지 말 것.
- **prod는 `sql_mode=only_full_group_by`다** — `GROUP BY`에 없는 컬럼을 그냥 SELECT하면 쿼리가 통째로 거부된다(에러만 나고 결과 0). JOIN해온 부가 컬럼(`ss.status`·`ss.region` 등)은 `MAX(...)`로 감싸거나 GROUP BY에 넣을 것.
- SQL이 `--`로 시작하면 옵션으로 파싱돼 실패한다 → 맨 앞에 공백 한 칸.
- **dev DB도 같은 커넥션에서 읽는다 — 테이블 앞에 `` `caramel-dev`. `` 를 붙인다** (2026-09-08 실측): `` SELECT ... FROM `caramel-dev`.partner ``. 스크립트 헤더가 "기본 대상은 prod"라고만 적어 dev는 못 본다고 오해하기 쉽다. 쓰기는 가드가 막으니 SELECT 전용으로만 쓸 것(prefix 빠뜨린 DDL이 prod에 만들어진 사고가 이 경고의 출처다).

**⚠️ DATE 컬럼(시각 없음)도 렌더링 함정 — 하루 밀림**
- DATE 컬럼도 `...T15:00:00.000Z` ISO로 렌더됨: **렌더 X일T15:00Z = 저장 X+1일** (naive date를 KST 자정으로 해석해 UTC 직렬화).
- 날짜별 결과를 눈으로 해석할 때 하루 밀려 읽기 쉬움 → `DATE_FORMAT(col,'%Y-%m-%d')`로 뽑을 것. (실사례: `forecast_log.forecast_date`)

### 5b. 테스터/테스트 계정 제외

```sql
-- 앱 유저 (live_users CTE 패턴)
WHERE au.deleted_yn = 0 AND au.test_yn = 0 AND au.temp_yn = 0

-- 디테일러
WHERE d.name != '이상민' AND d.id != 159
```

🔴 **위 3플래그로 안 걸러지는 내부 테스트 계정이 실재한다 — 판매 실적을 19% 부풀렸다 (2026-08-25 실측).** 반얀 패키지 지급자 87명 중 **13명이 내부 테스트 계정인데 `deleted_yn=0 AND test_yn=0 AND temp_yn=0`을 전부 통과**한다. 빼기 전 지급자 수는 10회권 77명·5회권 10명, 뺀 뒤 실판매는 **10회권 69명·5회권 5명**(운영이 아는 숫자와 일치). 지문:
- `name`에 `테스트`/`test`가 들어감(`맹주성테스트`·`이형준테스트`·`한수용 테스트`·`보희test`·`노준서 테스트`) — 디테일러·기획자 본인 이름이 붙은 계정이 많다.
- `phone`이 `010000099xx`·`01000001235` 같은 더미, 또는 **같은 번호로 계정 2개**(`01049664316`가 `테스트`·`ㅇㅇㅇ` 2개).
- 한글 자모 나열(`ㅇㅇㅇ`·`ㅅㄷㄴ`), 그리고 사내 인원 본인 계정(`맹주성` `01020866510` = live_user 블랙리스트 1번 번호, 디테일러 테스터 `성지원`).
⟹ **매출·판매 실적을 셀 때는 §5b 3플래그로 끝내지 말고 `name`/`phone` 지문 필터를 반드시 덧붙일 것**(블랙리스트 번호 목록은 그 자체로는 이 계정들을 못 덮는다). 반대로 *세차 건수* 집계에는 이 계정들이 거의 안 섞인다(반얀 세차 고객 345명 중 1명) — **지급·결제 쪽에만 몰려 있다.**

⚠️ **패키지 "구매 건수"를 `entitlement_package_instance` 행 수로 세면 10배가 된다.** 1행 = 1회차이므로(§6e) 10회권 1건이 10행이다. 구매 이벤트 단위는 `GROUP BY user_id, package_key, DATE_FORMAT(created_at,'%Y-%m-%d %H:%i')`(같은 사람이 회권을 두 번 살 수 있으므로 분 단위까지)로 묶고, 취소분은 `status='ACTIVE' AND deleted_at IS NULL`로 뺀다.

🔴 **어드민 반얀 보드의 "판매" 행은 판매일이 아니라 *그 고객의 첫 반얀 세차일*에 붙는다 (2026-09-02 실측).** 미사용 세차권은 예약과 연결이 없어 세트별 판매일을 붙일 데이터가 없기 때문에, `GET /v1/admin/banyan/board/summary?date=`가 그 유저의 반얀 세차 `MIN(reservation_datetime)` KST 날짜로 판매 행을 귀속한다. 실사례: 9/1에 판 김의성(226015) 5·10회권이 **8/6 보드에만** 뜨고 9/1·9/2 보드엔 없다(그날의 회수 실패 신고는 별건 — 권한 403이었다). ⟹ ① `epi.created_at`으로 센 판매일과 화면의 일자별 판매 숫자는 원래 안 맞는다(누계 `promoTotal`만 일치) ② 세트 판정은 `(user_id, package_key)` 버킷을 `created_at` **60초 gap**으로 쪼갠 뒤 **유효 회차수가 정확히 5 또는 10일 때만** 행을 만든다 — 그 외(4장 남은 세트, 60초 안에 두 번 지급 등)는 화면에서 **조용히 사라진다**(서버 warn 로그만). 유효 회차 = `entitlement_package_item.item_type='SERVICE'` ∧ `user_service.service_id=137` ∧ `deleted_yn=0`.

### 5c. 유료 예약 판정

**유료 예약** (프로모션 무료 제외): `user_service.paid_yn = 1` + 0원 VOUCHER 프로모션 제외:
```sql
AND r.id NOT IN (
  SELECT p.reservation_id FROM payment p
  WHERE p.type = 'VOUCHER' AND p.amount = 0 AND p.status = 'PAID'
    AND p.reservation_id IS NOT NULL
)
```

**취소율 측정 함정**: `user_service`는 예약 취소 시 `deleted_yn=1`로 soft-delete됨. 취소 건을 분모에 넣으려면 `deleted_yn` 필터를 빼야 함 — 안 그러면 취소가 통째로 빠져 취소율이 0%로 왜곡.

**취소 시 세차권 "반환" = 새 row 재발급 (2026-07-19 실측)**: 예약 취소(어드민 bulk-cancel `ticketAction=GIVE_BACK` 등)로 세차권이 반환되면 기존 `user_service` row의 `used_yn`을 0으로 되돌리는 게 아니라 ① 기존 row는 `used_yn=1`·`deleted_yn=1`로 soft-delete되고 `reservation_id` 연결이 그대로 남으며 ② 동일 `service_id`의 새 미사용 row(`used_yn=0`, `reservation_id=NULL`)가 새로 생성됨. ⟹ row 수를 발급 수로 세면 이중계산, `deleted_yn=1`을 "소실"로 세면 오판(반환분은 새 row로 살아 있음).

**🔴 재발급 대체행은 원래 배치의 `partner_activity_log_id`를 그대로 물려받는다 — 발급 배치 크기를 `created_at` 날짜로 세면 과소집계된다 (2026-08-28 실측)**: 위 GIVE_BACK 재발급으로 생기는 새 row는 발급일이 **몇 달 뒤**인데 `partner_activity_log_id`는 원 배치 값을 유지한다. 실측(user 131331, 반얀 10회권): `created_at`이 1/25인 행은 8장인데 `partner_activity_log_id=576`으로 묶으면 9장(4/8자 대체행 1장 포함)이라 **운영자가 말하는 "그때 9장 발급"과 DB가 어긋나 보인다.** ⟹ "이 뭉치가 어느 판매에서 나왔나"·"몇 장 발급했나"는 `created_at`이 아니라 `partner_activity_log_id`로 묶을 것.

**🔴 "고객이 가진 세차권 N장" = 미사용 + 미래예약에 물린 것 (2026-07-31 실측)**: CS 문의("몇 장 남았냐", "유효기간 연장해달라")에 `used_yn=0`만 세면 **틀린다.** 구독 고객은 자동예약 배치가 미래 예약을 미리 잡으면서 세차권을 `used_yn=1`로 선점해두기 때문에, 미사용 row가 0장인데 고객은 "7장 남았다"고 말하는 상황이 정상적으로 발생한다. 그 예약을 취소하면 위 GIVE_BACK 경로로 새 미사용 row가 나오므로 고객 인식이 맞다.
```sql
-- 고객 보유 세차권 (실질)
SELECT us.id, s.name, r.status,
       CASE WHEN us.used_yn = 0 THEN '미사용' ELSE '예약선점' END AS state
FROM user_service us
JOIN service s ON s.id = us.service_id
LEFT JOIN reservation r ON r.id = us.reservation_id
WHERE us.user_id = ? AND us.deleted_yn = 0
  AND (us.ended_at IS NULL OR us.ended_at > NOW())
  AND (us.used_yn = 0 OR r.status = 'CONFIRMED')   -- WASHED = 실소진이라 제외
```
전체 규모(2026-07-31, 살아있고 미만료인 `user_service` 기준): 미사용 18,860 / 미래 CONFIRMED 선점 4,816 / 소진(WASHED) 35,248. **선점분이 실질 보유의 4,816÷23,676 = 20.3%** — 무시하면 CS 답변이 대량으로 틀린다.

**🔴 화면의 "N장 보유"는 위 실질 보유와 다른 질문이다 — 차량 적용까지 통과해야 뜬다 (2026-09-10 코드+dev 실측)**: 콜 콘솔 세차권 목록과 고객 앱 `listMyWashTickets`가 말하는 "지금 이 차에 쓸 수 있는 보유분"은 더 좁다. ①쿼리 필터 = `deleted_at IS NULL AND deleted_yn=0 AND paid_yn=1 AND postpaid_yn=0 AND reservation_id IS NULL AND used_yn=0 AND (ended_at IS NULL OR ended_at > NOW())` — **후불(`postpaid_yn=1`)은 수금에 묶여 있어 보유로 세지 않는다.** ②그 다음 **차량 적용 조건**(`user_service_applicability.config`의 `CAR_ID`·`CAR_BRAND_ID`·`CAR_TIER_ID`·`CAR_REGISTERED_AT`·`CAR_MILEAGE` 필터 대 그 차의 값)을 통과한 것만 남는다. ⟹ **SQL로 행만 세고 화면을 예측하면 어긋난다** — dev에서 위 필터로 5장·2장이 잡힌 고객이 콜 콘솔엔 "보유" 표시가 하나도 없었다(그 차가 적용 대상 밖). 정본 = caramel-zero `listOwnedUserServiceGroups`(`prisma-admin-wash-pass-products.repository.ts`) → `selectApplicableOwnedUserServices`. 표시와 실제 소진이 같은 함수를 쓰도록 묶여 있으므로, "왜 화면엔 안 뜨냐"는 문의도 이 두 단계로 갈라서 답할 것.

### 5c-2. 🔴 세차 1건이 "무엇으로 결제됐나"(세그먼트) 판정 — `paid_yn`으로는 못 가른다 (2026-08-18 실측)

"이 달 세차를 구독/1회권/제휴/무료로 쪼개라"는 요청의 정본 축은 **예약에 물린 `user_service` 1행**이다(`us.reservation_id = r.id AND us.deleted_yn = 0`). 판정 순서:

1. `us.subscription_id IS NOT NULL` → **구독**. 종류는 `subscription.product_id → product.name`(실값 `월 2회(외부만)`·`월 4회(외부만)`·`월 1회` …). ⚠️ 이름이 제각각인 **커스텀·레거시 구독이 상시 존재**(2026-07 세차 2,077건 중 41건, 17종) → 화이트리스트 3종으로만 매칭하면 조용히 샌다.
2. `us.coupon_campaign_reward_id` → 제휴/캠페인. **회권 크기는 캠페인명으로만** 갈린다(현백 73=1회권/74=3회권/75=5회권).
3. `us.payment_id IS NOT NULL` → 유료. **회권 크기 = 그 payment로 발급된 티켓 수**: `SELECT payment_id, COUNT(*) FROM user_service WHERE deleted_yn=0 GROUP BY payment_id` — ⚠️ **`payment.quantity`가 아니다**(NULL 다수). 1이면 1회권 단품, 5/10/12면 회권 소진.
4. 나머지 = **어드민 수동지급**(아래 함정).

🔴 **`paid_yn`은 유·무료 판별에 못 쓴다 — 무료 지급분도 전부 `paid_yn=1`이다.** §5c의 "유료 예약 = `paid_yn=1`"은 프로모션 0원 payment를 거르는 용도이지, payment 자체가 없는 지급분은 걸러내지 못한다.

🔴 **비구독 세차의 41%(2026-07: 498/1,202)는 `payment_id`가 NULL이다.** 리터치·제휴 무료·어드민 지급·현장수금이 전부 여기 섞여 있고, `paid_amount`도 NULL이라 금액이 없다. 명분·금액 정본은 **`crm_note.memo`**(`결제받을 금액: N원` = 현장수금 1회권 / `수금할 금액` = 반얀 등 제휴 회권 / §6c 참조)뿐인데, **메모가 아예 없는 건이 213/3,278 = 6.5%**다 → 이 구간은 **DB만으로 유·무료 판정 불가**. 보조 축은 `reservation.booked_online_yn`(0=어드민 대행예약)뿐이고, **`log` 테이블로는 추적 안 된다**(user_service는 `used_yn` 변경만 기록되고 지급 INSERT 로그가 없다).

⚠️ **예약 1건에 살아있는 `user_service`가 2행 붙는 경우가 있다**(2026-07: 3,278예약 → 3,279행, 구독 티켓 + 수동지급 티켓 중복). 세그먼트 집계는 `COUNT(DISTINCT r.id)`나 예약당 1행 dedupe(구독 우선)를 써야 총합이 예약 수와 맞는다.

무료 명분은 대부분 **`service.name` 접두사**로 갈린다: `[리터치]`(재세차·AS)·`[AS]`·`[토스]`·`[프로모션]`·`[B2B]`·`[체험단]`·`올클린 케어 for 반얀트리`. 제휴 무료는 캠페인명(`[레슨북]`·`쟈스민` 등)으로.

🔴 **단, 세차권 *이름*으로 제휴처를 세면 틀린다 — 제휴처 여러 곳이 한 `service.id`를 공유한다 (2026-08-18 실측).** `service 137 프리미엄 세차 패키지 올클린 케어` 1,556장은 **현대백화점(campaign 73·74·75) / 반얀트리 760 / 캐딜락·GMC(camp 90) / 레슨북 / 테스트**가 전부 같은 이름으로 섞여 있다. **`service_id`·`service.name`엔 판매처 정보가 없다** — 정본은 아래 `package_key`. 위 접두사 규칙은 무료 *명분*엔 통해도 제휴처 *귀속*엔 안 통한다.

🔴 **패키지 세차권의 판매처 정본은 `entitlement_package_instance.package_key`다 — `user_service`의 귀속 컬럼 3개를 보면 안 된다 (2026-08-18 실측).** service 137의 **760장(49%)** 은 `coupon_campaign_reward_id`·`coupon_code_reward_id`·`payment_id`가 **전부 NULL**이라 `user_service`만 보면 "출처 불명"으로 보이는데, 실제로는 `package_key='BANYAN_WASH_PACKAGE_10'`(700장)·`'BANYAN_WASH_PACKAGE_5'`(60장)·`source_type='ADMIN_GRANT'`로 명시 기록돼 있다. 조인 경로:
```sql
JOIN entitlement_package_item epit ON epit.user_service_id = us.id
JOIN entitlement_package_instance epi ON epi.id = epit.package_instance_id
-- epi.package_key: 'BANYAN_WASH_PACKAGE_10' | 'campaign:75:group:1' | 'PREMIUM_WASH_PACKAGE_5' ...
-- epi.source_type: 'ADMIN_GRANT' | 'COUPON_CODE'
```
커버리지 실측 = service 137의 **1,556장 중 1,553장(99.8%)**. 캠페인 발급분은 `package_key='campaign:{campaignId}:group:{n}'` 형태라 캠페인 id까지 이 컬럼 하나로 나온다(73·74·75=현백 1·3·5회권, 90=캐딜락/GMC).

🔴 **패키지 세차권의 "취소"는 두 종류다 — `entitlement_package_item.status`로 갈라야 장수가 맞는다 (2026-08-24 실측).** `user_service.deleted_yn=1`만 보면 **재발급된 원본과 영구 소멸분이 한 덩어리로 섞인다.**
- `status='REPLACED'` = 예약 취소로 소멸했으나 **새 `user_service` id로 재발급됨**(`replaced_by_item_id`가 후속 item을 가리킨다). 재발급분이 별도 행으로 이미 살아 있으므로 **매출·선수금 집계에서 빼야 이중계상이 안 된다.**
- `status='CANCELLED'` = 재발급 없이 **영구 소멸**(환불·bulk-cancel·delete_reason NULL 경유). 이쪽이 진짜 감소분이다.
- 실측(SERVICE item 1,759장): ACTIVE 1,649 / REPLACED 55 / CANCELLED 55. `deleted_yn=1`은 110장으로 뭉뚱그려진다.
- 지문: 코드 등록 수 × 회차로 계산한 기대 장수보다 alive가 **모자란 만큼이 곧 CANCELLED**다(현백 campaign 73 −1장·75 −19장이 이 정체였고, EARLY_CANCELLATION 45건은 전부 REPLACED라 장수에 영향 없음).

⚠️ **"세차권을 어디서 받았나"와 "고객이 반얀 고객인가"를 섞지 말 것.** 전자의 정본은 위 `package_key`이고, 후자를 굳이 유저 단위로 봐야 하면 `app_user.utm_source IN ('반얀트리','banyantree','반얀트리_정기세차')` + `app_user.note LIKE '%반얀%'`이다 — 단 **`utm_source`는 가입 유입경로라 판매처가 아니다**(반얀 지급자 중 `direct`·`lms`·`naver.searchad`·NULL이 9명 실재). **주소(`장충단로 60`)로 세는 것도 금지** — 테스트 계정 11명이 그 주소로 등록돼 있어 섞이고, 실거주지가 성산동·역삼동인 실제 반얀 고객은 빠진다. (⚠️ *예약* 주소 매칭은 또 별개 — `location REGEXP '장충단로 ?60($|[^0-9길])'`, §3d.)

"첫 세차 vs n번째"는 유저별 선행 `WASHED`/`REPORT_SENT` 카운트로 — 하한 없이(§4b-1).

### 5d. 구독 status=ACTIVE 필터

- `status='ACTIVE'` 단독 조건은 일시정지 포함 → "현재 세차 가능한 활성 구독자" 집계 시 왜곡
- **실사용 구독자(일시정지 제외)**: `status='ACTIVE' AND paused_at IS NULL` — 단 아래 함정 때문에 **과소집계**다.

🔴 **`paused_at`은 "지금 정지 중"이 아니라 "마지막으로 정지를 걸었던 시점"이다 (2026-09-08 실측).** 재개해도 NULL로 되돌아가지 않는다 — api `pause-subscription.handler`는 status를 ACTIVE로 두고 `paused_at=now` + `ended_at`을 정지기간만큼 미는 것이 전부이고, **`paused_at`을 지우는 코드 경로가 리포 전체에 없다.** 정지기간을 담는 컬럼도 없어서(`period`는 구독 주기다) **"정지가 끝났는지"는 구독 행만 봐서는 알 수 없다.**
- 실측: `status='ACTIVE' AND paused_at IS NOT NULL` **289건 중 80건(27.7%)** 이 `paused_at` 이후 그 구독으로 세차권이 발급됐다 = **이미 재개됐다.** 정지 중이면 세차권이 안 나간다.
- ⟹ `paused_at IS NOT NULL`을 "일시정지 중"으로 세면 **최소 27.7% 과대**, `paused_at IS NULL`을 "실사용"으로 세면 그만큼 **과소**다.
- 재개 판별이 필요하면 발급으로 본다: `NOT EXISTS (SELECT 1 FROM user_service us WHERE us.subscription_id=s.id AND us.created_at > s.paused_at)` 인 것만 정지 중 후보.
- ⚠️ `paused_at`이 오래됐다는 것만으로는 재개 근거가 못 된다 — 장기 정지도 같은 모습이다(90일 기준으로 갈랐을 때 22건뿐이었고, 발급 기준으로는 80건이었다).

**⚠️ stopped_at·churn 판정 함정**
- `stopped_at` = 해지 시점 (churn 판정 컬럼). **STOPPED 4,500건+도 `deleted_yn=0`** → deleted_yn만으로 활성 판단하면 해지 구독이 오염된다. 활성 = `status='ACTIVE' AND deleted_yn=0`.
- status 실값: `STOPPED`/`ACTIVE`/`CREATED`/`ENDED`/NULL(+오타 `STOPPPED` 소량). **`PAUSED` status는 없다** — 일시정지는 `paused_at`으로만 판별.
- **churn 계산**: 분모 = 월초 ACTIVE (`started_at < 월초 AND (stopped_at IS NULL OR stopped_at >= 월초)`), 분자 = 월내 `stopped_at` 전이, user DISTINCT, paused 제외. ⚠️ **분자에도 `started_at < 월초` 코호트 조건을 걸 것** — 안 걸면 월중 가입→같은 달 해지 유저가 분모 없이 분자에만 새서 churn이 과대된다 (실측: 주간 기준 10~12% 상대 과대).
- ⚠️ 과거 시점 활성 구독 수 스냅샷에 `status='ACTIVE'`(현재 상태) 필터를 쓰면 이후 해지된 구독이 과거에서도 빠져 역사 시계열이 통째로 과소된다 — 과거 스냅샷은 `started_at`/`stopped_at` 경계로만 판정.
- 데이터 품질: STOPPED인데 stopped_at NULL ~90건, ENDED는 stopped_at 전부 NULL → 경계식 분모에 영구 잔류. 해결 = 종료시점에 `ended_at` fallback(아래).
- ⚠️⚠️ **`ended_at`은 "종료시점"이 아니다 — churn/활성 판정에 `COALESCE(stopped_at, ended_at)`를 전 행에 쓰지 말 것.** `subscription.ended_at`은 **ACTIVE 구독에선 현재 결제주기 종료일(=다음 갱신 예정일, 미래)**이다(2026-07 기준 ACTIVE 1861건 중 1609건 미래). 전 행에 coalesce하면 아직 구독 중인 유저의 갱신일이 "해지일"로 둔갑 → 현재월 churn이 폭등한다(실측 4.75%→83%). **올바른 종료시점**:
  ```sql
  CASE WHEN status IN ('STOPPED','STOPPPED','ENDED')
       THEN COALESCE(stopped_at, ended_at)  -- 종료구독만 ended_at fallback(stopped_at NULL ~120건 구제)
       ELSE stopped_at END                   -- ACTIVE 등은 ended_at 무시(NULL=미해지)
  ```
  churn 분모·분자, 과거 활성 스냅샷의 "경계 종료시점" 모두 이 식을 쓸 것. `stopped_at`=실제 해지일, `ended_at`=다음 결제 예정일 — 섞으면 갱신을 해지로 오해.
- **⚠️ 구독-차량 1:1 바인딩은 2026-04-01 도입** (`represent_car_id` 설정률 ~95%→100% 시점). 그 전엔 구독 세차의 ~39%가 represent_car가 아닌 차에 발생 → **2026-04 이전 기간에 `represent_car_id`로 "구독으로 세차한 차"를 판정하면 왜곡**. 이전 기간은 reservation_car로 실세차 차량을 직접 볼 것 (검증 2026-07-07).

**⚠️ 구독에 결제 공백(N개월 미청구)이 보이면 순서대로 갈라라 — 결제 실패로 단정 금지 (2026-08-14 실측)**
정액 구독인데 특정 월 `payment` row가 아예 없는 경우, 대부분 원인은 **고객이 누른 일시정지**다.
1. **미시도 vs 실패** = `card_payment`(`payment_id` 조인)에 **row 자체가 없으면 청구를 시도조차 안 한 것**. 실패는 row가 남고 `fail_reason`이 채워진다. `payment`만 보면 둘을 구분할 수 없다.
2. **일시정지가 `ended_at`(=다음 결제일)을 미룬다.** 고객이 앱에서 1~4주기를 직접 고르고(zero `pause-subscription.handler.ts`, `pausePeriods` 1~4 밖은 400), `ended_at += period × pausePeriods` + `paused_at` 세팅. `period_unit='month'`면 그만큼 **청구월이 통째로 사라진다**. 정지기간 역산 = `(변경 후 ended_at − 변경 전) ÷ period`.
3. **재개하면 `paused_at`은 NULL로 덮인다**(zero `prisma-subscription.repository.ts`) → **과거 일시정지 이력은 현재 행에 남지 않는다.** 2026-05 이전 건은 `log`(`table_name='subscription'`, `column_name='ended_at'`)로 복원되지만 **그 이후는 DB에 이력이 없다** → §7 `log`의 커버 경고를 먼저 읽을 것.
- 실사례: 전용 결제링크 구독(234,000원/월)이 2026-05·06월 청구 0건이었고 `card_payment`도 0행 = 미시도. 원인은 04-06 결제 58분 뒤 고객이 누른 **2개월 일시정지**(log에 `ended_at 2026-05-05 → 2026-07-05`, `type=USER`)였다.

**1회권 vs 구독 구분:**
- **구독 세차**: `user_service.subscription_id IS NOT NULL`
- **비구독**: `user_service.subscription_id IS NULL`
- 🔴 **이 NULL 칸을 "1회권"이라고 부르면 틀린다 (2026-08-20 실측).** 5·10회 횟수권 소진분·제휴 커스텀 상품·`product_id`가 NULL인 지급분이 전부 같은 칸에 들어온다. 공동구역 타겟 완료세차(2026-04-01~08-18) 비구독 621건의 내역 = 1회권 성격 352(`외부 + 내부` 202·`외부만` 150) + `5회/10회 이용권` 89 + `product_id` NULL 167 + 제휴·커스텀 13. **"1회권 N건"으로 보고하면 상품명 기준 실제 1회권보다 1.8배 부풀려진다.** 최소한 `us.product_id → product.name`까지 까고, 진짜 세그먼트가 필요하면 §5c-2의 4단 판정을 쓸 것.
- 🔴 **구독 상품은 `product_id`로 고르면 안 된다 — 같은 이름이 여러 id로 흩어져 있다 (2026-09-09 실측).** ACTIVE 구독 기준 `월 2회(외부만)`는 product **3555~3561 7개**, `월 4회(외부만)`는 **3563~3567 5개**에 나뉘어 있다(가격대·발급시기별). id 하나로 뽑으면 그 상품의 3분의 1만 잡힌다.
  - 매칭은 **이름 부분일치**로: `p.name LIKE '%월 2회(외부만)%'`. **앞을 고정하지 말 것** — `[카라멜] 월 2회(외부만) AMG GT` 같은 개별 결제링크 상품이 `LIKE '월 2회%'`에서 빠진다.
  - 세차권 3종(1회권)은 반대로 이름 매칭이 금지고 `car_tier_product.type`이 정본이다(§7 세차권). **1회권은 type, 구독은 이름** — 규칙이 반대라는 걸 헷갈리지 말 것.
  - 실사고: 같은 모수를 "구독/1회권/신규 3종"으로 집계한 기존 산출물이 월 468건이었는데, 비구독을 통째로 세면 508건이 된다. 구독·신규 칸은 ±3건으로 재현되고 **차이 40건이 전부 이 칸에서 나왔다.** 3종 합계를 인용할 때는 "무엇이 3종 밖으로 빠졌나"를 같이 확인할 것.

**구독 첫 세차 식별** (user_id + subscription_id 기준):
```sql
first_sub_reservations AS (
  SELECT MIN(r.id) AS reservation_id, us.user_id, us.subscription_id
  FROM reservation r
  JOIN user_service us ON us.reservation_id = r.id AND us.deleted_yn = 0
  WHERE us.subscription_id IS NOT NULL
  GROUP BY us.user_id, us.subscription_id
)
```
`MIN(r.id)` 사용 이유: 같은 `created_at` 충돌 방지.

### 5e. 🔴 결제 행 해석 — `deleted_yn`은 취소가 아니고, 어느 표면에서 만들어졌는지는 metadata로 갈린다 (2026-08-19 실측)

- **`payment.deleted_yn=1`은 PG 취소·환불이 아니다.** 레거시 `createCartPayment`가 같은 카트로 재시도할 때 그 카트의 이전 payment를 **`status='PAID'`인 것까지 전부** soft-delete하고, 딸린 `user_service`·`user_option`도 같이 `deleted_yn=1`로 만든다. `deleted_yn`으로 "취소됐네"라고 판정하면 **실제 승인된 결제를 없는 것으로 센다.**
  - **환불 여부 정본 = `payment.metadata.$.refundRecords`** 존재 여부(+`status IN ('CANCELED','PARTIAL_CANCELED')`).
  - ⟹ **한 `cart_id`에 `status='PAID'`가 2건 이상이면 중복 청구 의심 신호다.** `deleted_yn`을 무시하고 세라:
    ```sql
    SELECT cart_id, COUNT(*) paid_cnt, SUM(amount) total
    FROM payment WHERE status='PAID' GROUP BY cart_id HAVING paid_cnt > 1
    ```
- **`payment.metadata.$.__platform__`이 있으면 레거시 웹(careplus-web)에서 만든 결제다.** zero(`caramel-zero`)는 이 키를 쓰지 않고 `metadata`에 `{point}`만 넣는다. 같은 기능이 두 표면에 걸쳐 있을 때 **어느 프론트에서 결제가 났는지 가르는 유일한 축**이다. 레거시 결제는 `metadata.$.prices`(품목 배열)도 함께 갖는다 — zero엔 없다.
- **`cart.metadata.$.reservationProjectedDurationMinutes`가 있으면 zero가 만든 옵션 결제 링크 카트다.** 옵션 1개당 `cart_item` 1개를 `type='PRODUCT'`(`record_id`=**product.id**, 그 product의 `type='OPTION'`)로 넣는다 — 옵션이 `type='OPTION'` 행으로 들어오는 레거시 카트와 모양이 다르다. 옵션 상품↔옵션 매핑은 `product_option(product_id, option_id)`.
- **`crmel.link/<key>` 역추적** = `SELECT target_url FROM link WHERE \`key\`='<key>'`. **`target_url`이 상대경로면 단축링크 리졸버가 레거시 호스트(`caramel.thetrive.com`)에 붙여 해석한다** — zero 웹이 아니다. 전체 98,722건 중 절대 URL 93,922 / 상대경로 4,796.
- **포인트 잔액 = `SELECT SUM(amount) FROM user_point_history WHERE user_id=?`** (잔액 컬럼 없음). 음수면 중복 차감이 실제로 일어난 확정 증거다 — 차감 행은 `payment_id`를 갖고 `reason='commerce checkout point usage'`.
- 🔴 **포인트 "사용액" 집계는 `payment_medium`이 정본이다 — `user_point_history`는 상위집합이 아니다 (2026-08-25 실측).** 잔액은 위 식(history 합계)이 맞지만, **결제에 쓰인 포인트**를 셀 땐 갈린다. 2026-05~08 PAID 기준 `payment_medium`에 `POINT>0`인 결제 3,525건 중 history에도 차감 행이 있는 건 **2,269건뿐**(1,256건 누락). 역방향은 2,275/2,275 전량 일치 → **medium ⊃ history**. 구독 갱신 등 일부 경로가 history 행을 안 남긴다. 결제 화면·매출 차감에는 `medium='POINT' AND amount>0`을 쓸 것.
- 🔴 **환불 이력 = `metadata.$.refundRecords` 배열. 필드 의미를 틀리면 고객에게 표시할 금액이 틀린다 (2026-08-25 실측).** 한 레코드 = `{refundedAt, cashRefundAmount, pointRefundAmount, fee, refundableAmount, reason, actor{id,type}, actions[], targets[{id,type}]}`. append 경로 2곳(`prisma-refund.repository.ts:274` 일반환불 / `prisma-customer-reservation.repository.ts:4697` 예약취소).
  - **`fee`(취소 수수료)는 `refundableAmount` 계산 시 이미 차감된 값**이다: `refundable = 원금 − fee`, `cash + point = refundable`(fee>0인 149건 전부 성립). **다시 빼면 이중 차감.** 정책가 40,000원 고정·당일취소만(`refund-policy-calculator.ts:4,93`), 결제액이 40,000원 미만이면 전액이 fee가 된다.
  - **실제 환불액 = `cash + point`이고 `payment.cancel_amount`가 아니다.** 둘은 980건 중 928건(94.7%)만 일치한다.
  - ⚠️ **`status='PAID'`·`cancel_amount=0`·`cancelled_at=NULL`인데 이미 취소된 결제가 156건**(2026-01~). 환불액 0원이라 결제 행을 안 건드리는 설계이고, `refundRecords`엔 `CANCEL_RESERVATION`+`REFUND_VOUCHER`가 찍혀 있다(106건은 세차권 실제 회수, fee 합 209.9만원). **`actions`에 `REFUND_PAYMENT`가 없으면 이 경우** — status만 보고 "정상 결제"로 세면 취소분이 매출에 남는다.
- ⚠️ **`card_payment`에 행이 없어도 결제가 안 된 게 아니다.** PortOne v2 간편결제 경로는 행을 남기지 않는 경우가 있다. §5d의 "row 없으면 미시도"는 정기결제(빌링키) 청구에 한한 규칙이다.
- 🔴 **"무슨 수단으로 결제했나"는 한 컬럼에 없다 — 결제 세대별로 3갈래다 (2026-08-25 실측).** ① **신결제(zero/PortOne v2)** = `payment.payment_method`(`CARD`/`EASY_PAY`) + `metadata.$.provider`(`KPN`/`NAVERPAY`/`KAKAOPAY`…). ② **구결제(빌링키 구독)** = `card_payment` → `payment_method`(카드사·마스킹 `card_number`). ③ **아무것도 없음**. `payment.payment_method`는 **NULL이 최다**(6,808/14,677 = 46%, 2026-05~08)라 이 컬럼만으로 수단 분포를 세면 절반이 증발한다. 카드사+뒷4자리까지 나오는 건은 **전체 PAID의 20%뿐**(2,061/10,352): `type='OPTION'`은 `card_payment` 0행, `VOUCHER`는 22%만 행이 있고 `card_number`는 0건, `SUBSCRIPTION`만 36%가 카드번호를 갖는다.

### 5f. 🔴 고객이 결제 퍼널에서 고른 방문시각은 `reservation_draft`에만 남는다 — 예약이 됐다는 뜻이 아니다 (2026-08-31 실측)

`payment`에는 "고객이 몇 시를 골랐나"가 없다. 그 값은 `reservation_draft`에 있고, **FK가 아니라 카트 metadata의 JSON 문자열로 이어져 있다.**

```sql
FROM payment p
JOIN cart c ON c.id = p.cart_id
JOIN reservation_draft d
  ON d.uuid = JSON_UNQUOTE(JSON_EXTRACT(c.metadata, '$.draftUuid'))
```
`reservation_draft`: `user_id`·`car_id`·`address_id`·`reservation_datetime`·`last_step_key`(`address`→`visit-time`→`precaution`→`payment`).

- 🔴 **`payment.type='PACKAGE'`는 예약을 만들지 않는다.** 정산(`checkout-settlement`)이 `product.type='PACKAGE'`면 예약 생성 레시피를 비운다. 그래서 패키지 결제에 딸린 draft의 `reservation_datetime`은 **고객이 골랐지만 버려진 값**이다. 그 시각에 예약이 있다면 **고객이 결제 뒤 같은 시각을 한 번 더 고른 것**이지 결제가 만든 게 아니다 — 2026-08-31 실측 21건 중 14건이 결제 **73~485초 뒤** `source_type='CUSTOMER_DIRECT'`로 재예약했고 7건은 안 했다. 결제→예약 인과로 읽으면 오답. (화면은 2026-08-31에 고쳤다 — 패키지는 이제 예약 퍼널을 안 태운다.)
- **draft가 예약이 됐는지 알려주는 컬럼은 없다.** `reservation`을 `(user_id, reservation_datetime)`으로 대조하는 근사뿐이고, 고객이 나중에 같은 시각을 따로 잡으면 위양성이 난다.
- ⚠️ **`payment.cart_id`는 NULL이 흔하다**(어드민 지급·레거시 경로). 카트 경유 분석은 실질적으로 zero 웹/앱 결제만 본다.
- `cart.metadata.$.source`로 어느 화면에서 들어왔는지 갈린다: `wash-catalog`(세차권 구매 화면), `reservation-funnel`(예약 퍼널).

**컬럼명 함정 2개**: `payment`엔 `total_amount`가 없다(=`amount`). `product_post_purchase_behavior`는 `type`/`value`가 아니라 **`behavior_type`/`behavior_config`**(예: `LANDING` + `{"page":"/my-garage"}`).

---

## 6. 도메인별 심화

### 6a. Zone (날씨 조인)

**예약 → 날씨 조인 (forecast_log dedup 필수)**
- 같은 zone+date에 row가 여러 개 쌓임 → `ROW_NUMBER() OVER (PARTITION BY zone_id, forecast_date ORDER BY forecasted_at DESC)` 로 dedup 필수.
- ⚠️ **`source` 필터도 필수** — 4종 혼재: 예보=`KMA_PUBLIC_API`(probability 항상 있음)·`OPEN_METEO`, 실황=`KMA_PUBLIC_API_OBSERVED`·`OPEN_METEO_ARCHIVE`(probability NULL, amount_mm만). source 없이 dedup하면 예보/실황이 뒤섞임. 앱 우천 로직 기준 = 예보 `KMA_PUBLIC_API` + 실황 `KMA_PUBLIC_API_OBSERVED`.
- 참고: 앱의 "비예보 표시" 판정 = `RAIN` + 확률≥50%(3일 내)/60%(이후) + 강수량≥5mm (`rain-forecast-display.policy.ts`). 리터치 신청 가능 날짜에서 비예보일·주말 제외도 이 기준.
- ⚠️ **`reservation_retouch` 링크 부재를 "리터치가 아니다"의 근거로 쓰면 안 된다 (2026-08-06 실측, DS-1830).** 자동 리터치(`source_type='RAIN_RETOUCH'`)만 row를 만들고, **CS 수동 리터치(세차권 지급 후 예약)는 row를 아예 안 만든다** — 어드민 생성 수동 리터치 12건 전부 링크 0. 수동분 판정은 `service_id=136` + `partner_activity_log.description`(`리터치`/`우천 리터치`) + 직전 WASHED 세차 존재로.
- ⚠️ `zone_rain_log` 테이블은 드롭됨 — `forecast_log`만 사용.
- 경로: `reservation` → zone(polygon join, COALESCE 패턴 §2e) → `forecast_log`(zone_id + forecast_date)
- 🔴 **`forecast_log.zone_id`는 "그 존의 날씨"가 아니라 "존 폴리곤 `ST_Centroid` 한 점의 5km 격자 날씨"다 (2026-08-25 실측).** 적재 코드가 `SELECT ST_Y/ST_X(ST_Centroid(z.area)) FROM zone`으로 존당 좌표 1개를 뽑아 기상청 격자(nx,ny)로 변환해 긁는다. 존이 20~290km²라 **세차 1,246건 중 자기 존 중심점과 같은 KMA 5km 격자에 있는 건 25.6%뿐**(Z9 성동/성북 2%·Z1 마포/용산 4%·Z14 용인/화성 0%). ⟹ **"이 고객 동네에 비가 왔나"를 `forecast_log`로 판정하면 74%는 남의 동네 날씨다.** 고객 단위 강수 판정이 필요하면 그 예약 좌표를 직접 격자로 접어 외부 소스(기상청 `getUltraSrtNcst` / Open-Meteo)를 조회할 것. 존 단위 집계(그날 어느 존에 비가 왔나)에만 쓰면 안전하다.
- ⚠️ **실황 row는 하루 1행 집계라 시간 단위 질문에 못 쓴다 — 시간별 값은 `raw_payload.rows`에 있다.** `precipitation_amount_mm`은 그날 00~23시 RN1 합계고, `weather_condition`은 그중 한 시간이라도 비면 `RAIN`이다. "세차 끝난 뒤에 비가 왔나" 같은 판정은 `raw_payload.rows[]`(`baseTime`·`category` = `RN1`/`PTY`·`obsrValue`)를 파싱해야 한다. 실측: 그날 비 온 세차 495건 중 **29.5%는 세차 종료 후엔 비가 안 왔다**(아침 비 → 오후 세차) — 일 단위로 세면 우천 피해를 3할 과대 집계한다.
- `reservation_retouch` 컬럼 함정: soft delete가 **`deleted_at`(datetime)** 이다 — 카라멜 표준 `deleted_yn`으로 쓰면 `Unknown column`. status 값은 **`REQUESTED`/`CONFIRMED`/`CANCELED` 3종뿐**(`ASSIGNED` 없음), `parent_reservation_id`가 UNIQUE라 예약 1건당 리터치 1건. 접수 경로는 `metadata.source`(`customer`/`cs-manual`)로 갈린다.

**야외/실내 주차장 필터**
- 주차장 유형은 `reservation`에 없고 `user_address.parking_lot_type`에 있음.
- 값: `'OUTDOOR'`(야외), `'INDOOR'`(실내), **`NULL`(48k건, 미등록)**.
- ⚠️ NULL ≠ OUTDOOR — 야외 필터 시 `= 'OUTDOOR'` 명시 필수. `!= 'INDOOR'`로 쓰면 미등록 주소가 모두 포함됨.
- 패턴: `JOIN user_address ua ON r.address_id = ua.id WHERE ua.parking_lot_type = 'OUTDOOR'`
- ⚠️ **"야외 고객 수"는 주소 단위 필터로 못 센다 — 한 유저가 야외 주소와 실내 주소를 함께 갖는다 (2026-09-06 실측).** OUTDOOR 주소 보유 유저 6,380명 중 987명은 INDOOR 주소도 있어서, 야외 차단(혹서기·우천)을 실제로 겪은 모수는 **OUTDOOR만 가진 유저**다. 유저 단위 집계는 `GROUP BY user_id` 뒤 `MAX(parking_lot_type='OUTDOOR')`·`MAX(parking_lot_type='INDOOR')` 두 플래그로 갈라 쓸 것. 테스터 제외(§5b)까지 걸면 야외 전용 4,084명.
- 🔴 **혹서기 야외 세차 차단 기간은 DB에 없다 — caramel-zero 코드 상수다 (2026-09-06 확인).** 정본 = `apps/api/src/domains/reservation/domain/caramel-event.policy.ts`의 `heatSeasonOutdoorParkingEvent`(2026년분 = 6/22 00:00 - 9/7 00:00 KST). 이 기간엔 주소가 OUTDOOR면 예약 자체가 막혀서(`HEAT_SEASON_OUTDOOR_PARKING_NOT_ALLOWED`) **야외 전용 유저의 완료 세차가 월 250-350건에서 7월 14건·8월 12건으로 주저앉는다.** DB만 뒤지면 이 급락의 원인을 못 찾으니 야외 관련 시계열을 해석하기 전에 이 상수를 먼저 볼 것. 외부만 구독은 예약일이 종료일 이전이기만 해도 막히는 별도 분기가 있다. 우천 차단(`RAINY_OUTDOOR_PARKING_NOT_ALLOWED`, 강수확률 60% 이상)은 혹서기와 무관하게 상시 유효.

**서비스 가능 지역 조회 (service_region)**
- 컬럼: `sido`, `sigungu`, `dong`, `available_yn` (1=가능)
- ⚠️ `city`, `district`, `name` 컬럼 없음 — 사용 금지
- ⚠️ `sigungu` 통합값 함정: `"화성시"` 단독 행 없음. `"화성시 동탄구"` 형태. `= '화성시'` 사용 시 누락 → **`LIKE '%화성%'`** 사용
- 표준 조회 패턴:
  ```sql
  SELECT srg.name AS srg_name, sr.sido, sr.sigungu, sr.dong, sr.available_yn
  FROM service_region sr
  JOIN service_region_group srg ON srg.id = sr.service_region_group_id
  WHERE sr.sigungu LIKE '%화성%' AND sr.available_yn = 1
  ```

### 6b. 공급량

**테이블 구조**
- `detailer_work_schedule`: detailer_id, effective_from, effective_to, type, description, slot_id
  - ⚠️ **`deleted_at` 컬럼이 없다** (soft-delete는 `_rule`에만 있다). 헤더에 `dws.deleted_at IS NULL`을 붙이면 `Unknown column`으로 쿼리가 죽는다 — 헤더 무효화 관례는 `effective_from = effective_to`(아래 참조)다.
  - ⚠️ **`description`의 기간 문자열을 파견 기간으로 읽지 말 것 (2026-08-17 실측).** 로테이션 보조 파견은 desc에 사이클 전체를 적고 row는 **하루짜리**인 경우가 정상이다(sched 924 desc `반얀 파견 오전조(1~4타임) 08-17~08-20` → 실제 effective는 KST 8/20 하루 / sched 861 desc `8/18~8/20` → 8/20 하루). 기간 정본은 `effective_from`/`effective_to`뿐이고, desc를 믿으면 있지도 않은 파견일을 감사 대상에 넣는다.
- `detailer_work_schedule_rule`: schedule_id, day_of_week(MON~SUN), start_time, end_time (UTC), zone_id
  - `deleted_at IS NULL` 조건 필수
  - 🔴 **`start_time`/`end_time`은 TIME 타입에 UTC 저장이라 KST 아침 근무가 "전날 밤" 값으로 들어 있다 (2026-08-26 실측).** 07:30 KST=`22:30` · 08:00 KST=`23:00` · 09:00 KST=`00:00` · 10:00 KST=`01:00` · 14:00 KST=`05:00` · 16:00 KST=`07:00`. ⟹ **raw 로 정렬하면 가장 이른 조가 맨 뒤로 간다.** `WHERE start_time <= '09:00'`으로 "오전조"를 뽑으면 10~16시 KST 인원이 잡히고 정작 **08시 조가 통째로 빠진다** — 0건이 아니라 *다른 사람들*이 나와서 눈에 안 띈다. 변환식:
    ```sql
    SEC_TO_TIME((TIME_TO_SEC(ru.start_time) + 9*3600) MOD 86400) AS start_kst
    ```
    (`CONVERT_TZ`·`+ INTERVAL 9 HOUR`도 값은 맞지만 결과가 DATETIME이고 날짜가 `1970-01-02`로 넘어간다 — 쓰려면 `TIME()`으로 감쌀 것.)
  - **"몇 시부터 근무 가능한가"의 정본은 이 컬럼이다.** 유효 스케줄(`effective_from <= CURDATE() < effective_to` + `deleted_at IS NULL`) 기준으로 `GROUP BY start_kst`를 돌리면 시작 시각은 **몇 종 안 된다**(2026-08-26: KST `08:00`·`10:00`·`14:00`·`16:00` 4종). ⟹ **가장 이른 시각보다 앞선 예약(06시 등)·마지막 창보다 늦은 예약(22시 등)은 커버 후보가 구조적으로 0**이다. 버그로 오판하지 말고 시작 시각 분포부터 뽑을 것.
  - ⚠️ **`detailer_slot`(5행 템플릿)으로 근무·슬롯 시각을 판단하면 틀린다** — 데드 데이터다(§7 `slot_id` 항목). 2026-08-26 세션이 이걸로 "최이른 07:30"이라고 결론냈다가 위 실측(08:00)으로 정정했다.

**슬롯 타임 그리드 (capacity 집계용 근사, UTC → KST)**

⚠️ 아래 08~22 그리드는 **Grafana 공급량/가동률 집계용 근사 그리드**일 뿐, 고객에게 실제 노출되는 슬롯 시각의 출처가 아니다. 캐파 카운팅(1인당 5슬롯 등)에만 쓴다.

| UTC | KST |
|-----|-----|
| 23:00 | 08:00 |
| 01:00 | 10:00 |
| 03:00 | 12:00 |
| 05:00 | 14:00 |
| 07:00 | 16:00 |
| 09:00 | 18:00 |
| 11:00 | 20:00 |
| 13:00 | 22:00 |

**실제 노출 슬롯 시각의 출처 (2026-07-13 확정) — ⚠️ caramel-api 파지 말 것**
- 콜 콘솔·고객앱 예약 슬롯 = **caramel-zero `apps/api` `scheduling` 도메인의 하드코딩 상수** (`generate-time-slots.policy.ts`, zero-api `POST /v1/admin/scheduling/time-slots/query`). ⚠️ 레거시 caramel-api `time-slot.service.ts`(TARGET_TIMES 08·10·12…)는 죽은 경로 — 여기 파면 헛다리.
- ⚠️⚠️ **반얀트리(BANYAN_TREE) 슬롯 ≠ 일반(DEFAULT) 슬롯 = 완전히 다른 시스템.**
  - **반얀트리**: 고정 상수 `SEOUL_BANYAN_TREE_SLOT_START_TIMES_UTC` → **KST 09·11·14·16·18** (반얀 주소=장충단로 60 매칭 시에만).
  - **반얀 확장(`BANYAN_TREE_EXTENDED`)**: 고정 상수 `SEOUL_BANYAN_TREE_EXTENDED_SLOT_START_TIMES_UTC` → **KST 08·10·12·14·16·18·20** (같은 반얀 주소, 7칸). 🔴 **반얀 근무자는 최근 사실상 전원 이 타입이라, 기본 `BANYAN_TREE` 그리드(09·11·14·16·18)로 "그 시각은 원래 없는 슬롯"이라고 판정하면 뒤집힌다** — 2026-09-01 실사례: 9/4 12시 미노출의 원인은 그리드 부재가 아니라 12시를 여는 오전조 3명 전원 예약 참. ⟹ 반얀 슬롯을 판정하기 전에 **그날 유효한 스케줄의 `type`을 먼저 뽑아** 어느 그리드인지 확정할 것.
  - **현대백화점 천호(`HD_CHEONHO`)**: 고정 상수 `SEOUL_HD_CHEONHO_SLOT_START_TIMES_UTC` → **KST 10:30·12:30·14:30·16:30·18:30** (천호 주소=천호대로 1005 매칭 시에만). 2026-08 신설, 이 코드베이스 **최초의 30분 오프셋 그리드**다.
  - 🔴 **그리드는 근무창에서 파생되지 않는다 — 타입별 코드 상수다** (`slotStartTimesForWorkScheduleType()`). 근무 rule은 그중 **몇 칸이 보일지만** 정한다. ⟹ 근무창이 KST 10~21이어도 천호는 5칸이지 11칸이 아니다. **새 현장 시각이 다르면 반드시 새 상수를 추가**해야 한다.
  - 🔴 **타입 문자열이 매핑 계층에서 조용히 `DEFAULT`로 치환될 수 있다 (2026-08-26 실사고).** `prisma-detailer-schedule.repository.ts`의 `toDetailerWorkScheduleType`이 **화이트리스트**라 여기 빠진 타입은 DB에 `HD_CHEONHO`로 있어도 `DEFAULT`로 읽힌다 → **파견자는 정확히 잡히는데 슬롯 시각만 일반 그리드(정각)로 뜬다.** 그리드 함수·근무타입 판정은 각각 유닛 테스트를 통과하고 그 사이에서 값이 죽으므로 **실화면/E2E에서만 드러난다.** 현장 타입 추가 시 고칠 곳 넷: 레지스트리 / `serviceability-resolver` / `generate-time-slots.policy` / **이 화이트리스트**.
  - 🔴🔴 **같은 사고가 하루에 세 번 났다 — 원인은 전부 '병렬 목록'이다 (2026-08-26).** ①위 화이트리스트 ②셔플 파견조 제외가 `!== 'BANYAN_TREE'` **부정형**이라 새 현장이 그냥 통과(파견자끼리 맞교환 가능) ③패키지 지급·취소 맵이 반얀 2종뿐이라 새 현장 상품을 **팔 수도 취소할 수도** 없었다. ⟹ 새 목록을 만들기 전에 **정본을 참조할 수 없는지** 먼저 보고(예: 취소 회차수는 `EntitlementPackageDefinition.instanceCount`, 키 목록은 `ENTITLEMENT_PACKAGE_KEYS`), 어쩔 수 없으면 **긍정형**(`=== 'DEFAULT'`)으로 쓴다. 부정형은 새 값을 조용히 통과시킨다.
  - 🔑 **prod 배포 검증은 쓰기 요청이 아니라 `https://api-prod.thetrive.com/openapi.json` 읽기로 한다 (2026-08-26).** 무인증으로 열려 있고 1MB 스펙 전문이 나온다 → `components.schemas.<Name>.properties.<field>.enum` 을 보면 **계약이 배포됐는지 즉시 확정**된다. ⚠️ 나는 이걸 모르고 지급 엔드포인트에 `POST` 를 30초마다 12번 보내 확인하려 했다 — user id 가 없어서 404 로 끝났지만 **존재하는 user 였으면 prod 에 12건을 지급**했다. 배포 확인용 폴링에 쓰기 메서드를 쓰지 말 것. (⚠️ 별건: prod API 스펙이 무인증 공개인 것 자체는 보안 검토 대상.)
- 🔑 **패키지 세차권의 `service_id` 는 코드 상수가 아니라 DB 실측으로 확인한다.** `PREMIUM_WASH_PACKAGE_*` 도 반얀과 같은 **137**('프리미엄 세차 패키지 올클린 케어')이다. 뽑는 법 = `entitlement_package_instance` → `entitlement_package_item`(`package_instance_id`) → `user_service`(`epit.user_service_id = us.id`). ⚠️ `user_service` 에는 패키지로 가는 컬럼이 **없다** — `entitlement_package_item` 을 반드시 거친다.
  - **일반(DEFAULT)**: 상수 `SEOUL_SLOT_START_TIMES_UTC` → **KST 짝수시 12칸(00·02·…·22)**, 하루 전체가 열려 있다 (2026-09-08 코드 확정 — 종전 "미조사" 해소). ⟹ **DEFAULT는 그리드가 제약이 아니다.** 실제 칸은 rule 근무창이 자른다(표준 KST 10~19 → 10·12·14·16·18). 위 08~22 근사 그리드로 추론하지 말 것.
- 실제 노출 = 상수 그리드 ∩ rule 근무 윈도우 ∩ 가드(예약버퍼·시각겹침·하루 `MAX_RESERVATIONS_PER_DAY=7`).
- 그리드는 타입별 공용 상수 → 특정 디테일러만 다른 시각대 주려면 DB 아닌 **코드 변경 필요**.

**슬롯 가용 판단**
- X시 슬롯 공급 가능 = rule의 `start_time(KST) ≤ X시` AND `end_time(KST) ≥ X+1시`
- **`effective_from~to` 범위만 체크하면 과대 카운트** — 반드시 해당 요일의 rule 존재 여부를 함께 확인
- 🔴 **그 "존재"는 `deleted_at IS NULL` 기준이다 — 활성 rule이 0개인 유효 스케줄이 실재한다 (2026-08-14 실측).** `detailer_work_schedule_rule` 14,776행 중 **3,060행(20.7%)이 soft-delete**다. 스케줄 헤더는 effective 구간 안인데 그 요일 rule이 전부 삭제된 경우가 있어(홍세영123 sched 236 = 그날 rule 3개 전부 삭제 / 김형현169 sched 817 = 15행 중 10행 삭제·활성 5행 유지), `deleted_at`을 빼면 **근무 안 하는 사람을 "정상 근무"로 센다.** 반대로 `COUNT(rule)`만 세면 삭제분까지 세어 존·근무창이 있다고 오독한다. 근무 판정·존 판정·부하 랭킹 전부 `ru.deleted_at IS NULL`을 붙이고, **활성 rule 0개면 "그날 근무 없음"으로 취급**할 것.

**effective 경계 저장 컨벤션 (스케줄 생성/수정 시)**
- KST 자정 경계를 UTC로 저장: **D일부터 유효 = effective_from `'(D-1) 15:00:00'`**, 영구 = effective_to `'2099-12-30 23:59:59'`.
- 코드 lookup은 `dayjs(date).startOf('day')`(UTC 자정)와 `effective_from <= date <= effective_to` 비교 + 해당 요일 rule 매칭.
- 🔴 **노출 슬롯 "개수"를 SQL로 재현하지 말 것 — 그리드 ∩ 근무창 모델은 과대추정이다 (2026-08-12 실측).** SQL은 예약의 **실제 점유 길이**(반얀은 조회 시 90분 하한으로 재산출)와 부분휴무를 못 빼서, 남은 FREE 조각이 duration보다 짧아 실제로는 안 뜨는 칸을 "열림"으로 센다. 실측: 이승원21 8/13 근무창 KST 08~16이고 08시에 예약이 없는데도 08시 슬롯 **0** — 09시 예약이 90분(09:00~10:30)이라 FREE가 `08:00~09:00` **60분**뿐이었다. SQL로 판정 가능한 건 "그 요일에 스케줄·rule이 있나"까지(effective 판정은 아래 양쪽 strict 항목). **칸 수는 API로 센다.**
- 🔴 **어드민 예약 생성·시각변경의 "다른 예약 이동"(`requiresShuffle`)은 그 칸에 이미 예약이 있는 시각에서만 만들어진다** (2026-09-08 코드 확정, `reservation-slot-search.service.ts` 의 `candidateStartTimes` = occupant 시작시각). ⟹ **빈 칸이 뒤 예약과 겹쳐 사라진 경우엔 이동 제안이 아예 안 나온다** — 화면상 그 시각은 그냥 없다. 실측: 최지현172 9/22 근무창 KST 10~19·예약 12시·14시 → 컨시어지(140분 floor)로 10시를 잡으면 10:00~12:20 이 12시 예약과 겹쳐 소멸하는데, 10시엔 occupant 가 없어 이동 후보도 못 된다 → 노출은 `14*·16·18` 뿐. 대조군 9/17(18시만 예약)은 10시 정상 노출. ⟹ "왜 X시가 안 뜨냐"는 그리드·근무창·휴무보다 **요청 duration 이 다음 예약까지 물리는지**를 먼저 볼 것.
- **그날 그 주소에 실제 몇 칸 뜨는가 = 무인증** `POST https://api-prod.thetrive.com/v1/scheduling/time-slots/query` body `{"addressId":N,"fromDate":"YYYY-MM-DD","toDate":"...","durationMinutes":90}` → `slots[]`를 KST 시각으로 group by. 반얀 주소 `addressId=12053`. ⚠️ `durationMinutes`를 고정하지 않으면 개수가 달라진다. 또 같은 조건 재조회 시 **개수는 같고 `detailerId`는 바뀐다**(동시각 후보 여럿이면 랜덤 1명 dedupe) → **"누가 열었나"는 이 응답으로 판정 금지, 사람은 work-day API로.**

**🔴 디테일러 일부하 비교는 건수만으로 하면 틀린다 — 개인 근무창을 반드시 함께 조인 (2026-07-27 이승제 6건 항의 실사례)**
- 표준 근무창은 rule `01:00:00~10:00:00` UTC(=**KST 10~19**)지만 **이른 조 `23:00:00~08:00:00`(=KST 08~17)가 여럿 있다** — 실측 명단: 이한결(177)·이승제(78)·이재형b(167)·황석찬(114). "이한결만 예외"로 알고 있으면 틀린다.
- **이른 조는 18시 이후를 못 받아 같은 건수가 더 짧은 창에 압축된다.** 실측 6건 배정 비교(2026-07-28): 10~19조는 `10·12·14·16·17·18`로 9시간에 퍼지지만 이른 조는 `08·10·11·12·14·16`으로 오전에 4건이 붙고 마지막 건 종료가 근무 종료(17시)에 딱 붙는다. (DEFAULT 슬롯 시각 상수 자체는 여전히 미조사 — 위 슬롯 섹션 경고 참조. 위 시각들은 배정 실측값이지 그리드 단정이 아니다.)
- ⟹ 부하 랭킹 쿼리에 `SUM(estimated_time)` + rule 윈도우(`DATE_FORMAT(wr.start_time,'%H:%i')`)를 함께 뽑을 것. 건수만 세면 이른 조 과부하가 안 보인다(실사례: 6건 이상 5명 중 이른 조가 2명이었고, 건수·총소요분으로는 이승제가 오히려 최하위였다).
- 근무창 밖 건을 "동선 좋으니 소화 가능"으로 임의 판단 금지 — 수락 여부는 디테일러별로 다르다(§6b 재배정 사전검증 참조).
- ⚠️ **파견 상주자는 파견기간에 `type='DEFAULT'` 스케줄이 0행일 수 있다 (2026-07-27 반얀 상주 6명 전원 확인).** 즉 그 기간의 근무시간 = 파견 근무창이 유일하다. DEFAULT로 근무창을 찾아 0행이 나오면 "판정 불가"가 아니라 **파견 스케줄(`type LIKE 'BANYAN_TREE%'` 등)을 볼 신호**다. 근무외 판정 전에 그 사람이 그 주에 가진 스케줄 `type`을 먼저 전부 뽑을 것.

**신규/복귀 디테일러 스케줄 생성 (활성 스케줄 0건인 경우)**
- 🔴🔴 **반대 경우가 더 위험하다 — 파견자가 파견기간에 `DEFAULT` 스케줄을 *동시에* 들고 있을 수 있다 (2026-09-04 실사고).** 위 항목("0행일 수 있다")만 알고 있으면 파견자에게 DEFAULT가 없다고 전제하는데, 겹쳐 있으면 **셔플이 그를 일반 존 인력으로 보고 원존 예약을 꽂는다.** 실사례: 천호 파견자 정순욱187·박현규207·한수용191에게 `DEFAULT` 스케줄(id 1082·1091·1084, `CELL_SHARED` 룰)이 **9/8 00:00 KST~2099 무기한**으로 새로 생성됐는데 `HD_CHEONHO` 편성(187 ~9/10 · 207 ~9/8 · 191 9/9~9/10)이 그대로 살아 있어 기간이 겹쳤다. 그 상태로 대량 재배정이 돌자 **천호 예약 15/30건이 남에게 나가고 일반 존 예약 7건이 파견자에게 들어와** 파견지 슬롯을 물리적으로 막았다(정순욱 9/10 = 여의도 10:00·옥수동 12:00·잠원동 16:00 + 천호 10:30·12:30·16:30).
  - **원인은 편성 화면이다.** `segment.ts`의 `SegmentWorkSite` 유니온에 `HD_CHEONHO`가 없어 편성 UI가 그 사람의 현장 편성을 **보지 못하고** DEFAULT를 새로 얹었다(`assertEditableSchedules`는 현장 편성 *편집*만 막고 새 DEFAULT *생성*은 막지 않는다).
    - ✅ **정정 (2026-09-08): 편성 화면은 이제 천호를 읽는다** — zero **#1825**(`dc3dcbe08`)가 `LEGACY_FIELD_WORK_SITES = ['HD_CHEONHO']`를 넣어 `FIELD:<현장키>` 형식과 함께 `isFieldSegmentWorkSite()`로 인식한다. ⟹ "파견 나간 사람이 화면에서 빈칸으로 보인다"는 더 이상 사실이 아니다. **다만 위 9/4 사고로 이미 생긴 겹친 DEFAULT 토막은 코드 수정으로 사라지지 않는다** — 감사 쿼리는 그대로 필요하다.
  - ⟹ **파견 감사 진입 쿼리는 `type` 을 필터하지 말고 그 사람의 그 기간 스케줄을 전부 펼쳐라.** `WHERE detailer_id=? AND effective_to > :from` 로 뽑아 **토막이 시간축에서 겹치는지**부터 본다. `type='HD_CHEONHO'`만 뽑으면 겹친 DEFAULT가 안 보이고, `type='DEFAULT'`만 뽑으면 파견 사실이 안 보인다.
  - ⚠️ 겹침 판정 시 `effective_from`/`effective_to`는 **UTC 저장 + KST 하루 경계**다(`(D-1) 15:00:00` ~ `D 14:59:59`). `9/8 00:00 KST` = `2026-09-07 15:00:00`.
  - 🔑 **관례는 "파견기간엔 DEFAULT를 비운다"이고 이건 실측으로 닫혀 있다 (2026-09-04).** 8월 반얀 파견 토막 **40개 전수에서 살아있는 DEFAULT(`effective_from <> effective_to`) 겹침이 0건 = 0%**다. 반대로 **종일 휴무로 막는 것은 관례가 아니다** — 같은 44토막 중 종일 휴무가 있는 건 36%뿐이고 내용이 전사휴무·연차·일회성 차단 요청이다. ⟹ 파견자에게 원존 예약이 새는 것을 막을 때 **휴무를 걸지 말고 DEFAULT 토막의 경계를 옮겨라.**
  - 🔴 **현장 파견자에게 종일 휴무를 걸면 그 현장 예약이 전부 "휴무자 배정"으로 잡힌다.** 휴무충돌 감사·재배정 예외 조항은 `type LIKE 'BANYAN%'`에만 걸려 있어(위 §휴무 윈도우) **천호는 예외가 아니다.** 천호 30건에 걸면 30건이 통째로 감사·재배정 대상이 된다.
  - 🔴🔴 **`PUT /v1/admin/scheduling/detailers/{id}/segments`는 현장 파견자에게 쓸 수 없다 (2026-09-04 `preview`로 실측).** 이 API는 `from`~`to` **창의 세그먼트를 통째로 교체**하므로, DEFAULT만 지우려고 창을 비우면 **그 창에 있는 `HD_CHEONHO` 편성까지 사라진다** → `coverProposals` 38건이 뜨고 그중 `origin:'NEW'` 15건이 **그 사람의 천호 예약 전부를 다른 디테일러로 되돌린다**(나머지 23건 `origin:'STANDING'`은 무관한 남의 예약 연쇄 — [[project_schedule_change_reservation_cover]]). 그렇다고 payload 에 천호 세그먼트를 되살릴 수도 없다: `place`가 **`zoneId` 필수(number)** 인데 천호 룰은 `zone_id=NULL`이라 `400 Invalid request body`다. ⟹ **파견 편성의 경계 이동은 `~/claude/mysql-write.sh`로 `effective_from`/`effective_to`만 UPDATE**하는 게 유일한 경로다(예약을 안 건드리니 cover 연쇄가 없다).
    - 🔴🔴 **정정·강화 (2026-09-08): 이제 저장 자체가 400으로 막힌다. 그런데 `preview`는 안 막힌다 — 가드가 비대칭이다.** zero **#1825**(`dc3dcbe08`)가 `assertEditableSchedules`에 ⓪ 분기를 넣어, 편집 범위에 현장 편성이 **하나라도** 있으면 `현장 파견 근무는 구간 편집으로 다룰 수 없습니다: HD_CHEONHO` **BAD_REQUEST**를 던진다. 가드 호출부는 `prisma-detailer-segment.repository.ts`의 **`replaceSegments` 한 곳뿐**이고, 이건 실제 저장(`apply-detailer-segments`) 경로다. **`preview` 경로에는 이 가드가 없다** — 그래서 위 9/4 실측에서 `coverProposals` 38건이 떴던 것이고, 지금 다시 돌려도 똑같이 뜬다. ⟹ **`preview`가 응답했다는 것을 "이 편집이 된다"의 근거로 쓰지 말 것.** 현장 파견자에게는 preview가 그럴듯한 계획을 그려주고 저장에서 400이 난다.
    - ⚠️ 이때 `detailer_work_schedule`·`_rule`의 `created_at`/`modified_at`은 **UTC 저장**인데 컬럼 DEFAULT는 서버 tz(`Asia/Seoul`)라, INSERT에서 기본값에 맡기면 형제 행보다 **9시간 미래**로 박힌다 → `UTC_TIMESTAMP()`를 명시할 것(`reservation_status_log`와 같은 함정).
    - 🔑 **잘라낸 꼬리를 되살리는 것을 잊지 말 것.** `effective_to`를 당겨 파견기간을 비우면 파견 종료 후가 통째로 빈다 → 그날부터 예약이 안 들어온다. 새 토막 + 룰 복제(`INSERT ... SELECT LAST_INSERT_ID(), r.* FROM ..._rule r WHERE r.schedule_id = <원본>`)까지 한 트랜잭션에 넣는다.
  - 🔑 **겹침 여부는 어드민 API가 직접 알려준다** — `GET /v1/admin/scheduling/detailer-schedules/day?date=…`의 `fieldDeployment`(현장) × `hasNonFieldWork`(비현장 근무 보유). **`fieldDeployment != null && hasNonFieldWork == true` 가 곧 이상 신호**이고, 정상화되면 그날 `workScheduleTypes`가 현장 타입 단독 + `workZoneIds: []`가 된다. 수정 전후 검증에 이걸 쓸 것.
- rule만으로는 안 되고 `detailer_work_schedule` 헤더부터 INSERT 필요.
- 최근 운영 컨벤션: `slot_id = NULL`, `type = 'DEFAULT'`, description에 사유 메모.
- rule의 start/end_time은 `'1970-01-01 HH:MM:SS'` UTC (예: 10~19시 KST = `01:00:00`~`10:00:00`), `service_region_group_id = NULL`.
- Fill Rate = `실제 예약 디테일러 수 / 스케줄 기반 공급 가능 디테일러 수`
- 🔴 **raw `end_time <= start_time` 을 "자정 넘김 근무"로 읽지 말 것 (2026-08-24 실측).** UTC 저장이라 **KST 08~17시 근무가 `23:00:00`~`08:00:00`** 으로 들어간다. 활성 rule 11,756행 중 **3,469행(29.5%)이 raw 기준 뒤집혀 있고 전부 정상 주간 근무다.** KST로 정규화하면 진짜 자정을 넘는 rule은 **0건**(길이 0인 `10:00=10:00` 5행만 있고 그마저 2026-08-20 만료). 정규화식 = `MOD(TIME_TO_SEC(t)+32400, 86400)`. 도메인 `work-schedule.ts intervalOnSeoulDate` 가 `endAt <= startAt` 이면 +1일 올리는 것도 **이 UTC 랩을 흡수하는 장치이지 야간 근무 지원이 아니다** — 이 코드를 근거로 "야간 근무가 있다"고 결론내면 틀린다.
- ⚠️ **TIME 컬럼도 헬퍼 JSON에서 tz 시프트된다** — `start_time` 을 그냥 SELECT 하면 `1970-01-01T14:00:00.000Z`(=저장 `23:00:00`)로 찍혀 값이 통째로 달라 보인다. 원값은 `CAST(start_time AS CHAR)` 로 뽑을 것.

**detailer_work_schedule.type — DEFAULT만 있는 게 아니다 (2026-07-13)**
- `type` 값: `DEFAULT`(일반 존 배정) / `BANYAN_TREE`(반얀트리 주소 전용) / `BANYAN_TREE_EXTENDED`(반얀 확장 그리드, 2026-07-16 prod 실존 확인) / `HEY_DEALER`(외부 사업 이탈 마커, lookup에서 제외).
- ⚠️ 반얀 상주 조회 시 `type='BANYAN_TREE'` 등호 필터는 `BANYAN_TREE_EXTENDED`를 놓친다 — **`type LIKE 'BANYAN_TREE%'`** 로 잡을 것.
- 코드 lookup이 type별로 분리 조회 → **반얀트리 상주 배정은 zone rule이 아니라 `type='BANYAN_TREE'`** + rule `service_region_group_id=2`(서울 중구)·`zone_id=NULL`. zone 테이블만 뒤지면 못 찾는다.
- ⚠️ **`slot_id`(→`detailer_slot`)는 데드 데이터 (2026-07-13 확정).** 슬롯 생성 경로에서 안 읽힘 — slot_id를 바꾸거나 `detailer_slot`을 INSERT해도 실제 노출 슬롯 시각엔 무영향. 반얀 세팅 시 관례로 채우긴 하나(김형현·손정민 slot 3), 시각을 결정하는 건 slot_id가 아니라 **상수 그리드 ∩ rule 윈도우**다. 슬롯 조사에서 이 테이블 보지 말 것.
- 반얀 상주 디테일러 N명 = 슬롯당 캐파 N배 (slot_id 때문이 아니라 같은 시각대에 N명이 서빙).
- **기간 파견 패턴 = 스케줄 3토막**: ①기존 스케줄 `effective_to` 단축 ②파견 스케줄(기간 한정) INSERT ③복귀 스케줄(파견 종료 익일~영구) INSERT. ③을 빼먹으면 파견 종료 후 배정 공백.
- 🔴 **스케줄 무효화 관례 = `effective_from = effective_to` (2026-08-06 실측).** 파견 중 원존 차단 등에서 행을 지우지 않고 길이 0으로 눌러둔다(염철림165 sched 786 = `2026-08-06 15:00` 양쪽 동일). ⟹ **effective 판정은 반드시 `effective_from < X AND effective_to > X` (양쪽 strict)**. `BETWEEN`이나 `effective_to >= X`로 쓰면 무효화된 스케줄이 근무 중으로 잡힌다(위 §슬롯/근무 예시 중 `BETWEEN`·`effective_to >= 'D일 00:00:00'` 형태는 이 관례 이전 것이니 그대로 복사하지 말 것). 무효화 결과 그날 어떤 type의 스케줄도 없으면 슬롯 0 · work-day API `blocks: []`.
  - ⚠️ **파견 일정이 뒤로 밀리면 무효화가 남아 공백이 된다** — 염철림은 원존 차단으로 786을 눌렀는데 반얀 파견이 8/10~8/11로 미뤄져 **8/7~8/9 사흘간 아무 스케줄도 없는** 상태가 됐다. 파견 일정 변경 시 무효화 행을 되돌렸는지 함께 확인할 것.

**`detailer_schedule_change_log` — 근무 편성 저장 이력 (2026-09-01 신규)**
- 편성 화면(`/admin/detailer/schedule-compose`)과 휴무 API의 **저장이 1건씩 남는다.** `detailer_work_schedule`·`detailer_holiday`엔 이력이 없으므로 **"왜 저장이 안 먹었나"를 되짚는 유일한 소스**다.
- `data` JSON = `beforeSegments`/`afterSegments`(구간 배열) + `editedFrom`/`editedTo`(편집 창) + `acknowledgedReservationIds`. before/after를 나란히 놓으면 그 저장이 실제로 무엇을 바꿨는지 그대로 보인다.
- ⚠️ **스냅샷은 편집 창으로 잘려 있다** — 창 밖 편성은 안 담기니 전체 타임라인으로 읽지 말 것.
- `reason`은 사람이 쓴 사유(`교육 취소`) 또는 상수(`ADMIN_SCHEDULE_BOARD_HOLIDAY_*`). 여러 명 일괄 저장은 `batch_id`로 묶인다.
- ``SELECT id, DATE_ADD(created_at, INTERVAL 9 HOUR) kst, reason, JSON_PRETTY(data) FROM detailer_schedule_change_log WHERE detailer_id=? ORDER BY id DESC``

**detailer_holiday 처리**
- 단기(≤7일) full-day (`from ≤ 당일 00:00` AND `to ≥ 익일 00:00`) → 실제 off
- 장기(>7일) → capacity 집계에선 무시 (파견/퇴사 등 운영 메모). ⚠️ **단 `memo`에 `퇴사`·`하차`·`출격보류`가 들어간 장기 row는 예외 — 실질 비가용인데 `booking_yn=1`로 남아 있는 경우가 있다**(김승규190 `퇴사 예정` 2026-08-05~12-30, 2026-08-06 실측). 인계·재배정 후보 선정에선 장기라도 배제할 것.
- 부분 시간 → 겹치는 슬롯만 차감. ⚠️ **매일 반복되는 4시간짜리 부분 블록이 수개월분 선삽입돼 있는 경우가 있다**(memo `셀원 품질 점검` = UTC 05:00~09:00 = KST 14~18시, 황석찬114에 4~8월분). 하루 겹침 COUNT로 "휴무"를 세면 오전 근무 가능자가 통째로 탈락 → **`TIMESTAMPDIFF(HOUR, from, to) > 8`로 종일/부분을 갈라** 종일만 종일 탈락시킬 것(§3c 항목 4).
- `v_detailer_holiday_daily` 뷰 한계 있음 — capacity 쿼리에서는 `detailer_holiday` 직접 조회 권장
- 🔴 **공휴일·전사휴무 캘린더 테이블은 없다 — `detailer_holiday`에 디테일러 1인당 1행으로 깔려 있다** (2026-07-29 확인). memo 규칙 `[전사휴무]<휴일명>`(예 `[전사휴무]대체휴일(광복절)` = 2026-08-17, 101행 종일 `(D-1) 15:00 ~ D 15:00`). ⟹ ①"그날 회사가 쉬는 날인가"는 `WHERE memo LIKE '[전사휴무]%'` 분포로 판별할 것 — 예약/슬롯 0건을 "수요 없음"이나 데이터 누락으로 오독하기 쉽다 ②**그 휴일에 0분(`from`=`to`) row인 사람 = 그날 예외적으로 일하는 사람**(반얀 단독 파견 등). 즉 0분 row는 무력화 표시일 뿐 아니라 "이 사람만 출근"의 시그널이다.
- ⚠️ **"왜 슬롯에 안 뜨나" 진단에선 반대 — 휴무는 길이 무관 하드 차단** (2026-07-13). 플래그(booking_yn 등)·스케줄·rule이 다 정상이어도 해당 날짜에 holiday row 있으면 노출 0. 운영이 파견/별동대를 **매일 full-day 휴무 bulk INSERT**로 마킹하는 패턴이 있으니(같은 created_at·memo 예 "정비별동대") 미노출 진단 시 `memo` 확인 필수.

**🔴 슬롯을 실제로 닫는 유일한 레버 = `detailer_holiday` 부분블록 INSERT (2026-08-05 실행·검증)**
- 특정 디테일러의 슬롯 1~2칸만 즉시 막을 때(개인 사유 외출 등): ``INSERT INTO detailer_holiday (detailer_id, `from`, `to`, memo)`` **4컬럼만**. 값은 **UTC**(KST−9h).
- ⛔ **rule `start_time`/`end_time` 축소로 근무창을 줄이려 하지 말 것** — DB엔 반영되는데 **prod 슬롯 API가 계속 옛 창을 반환**(2026-07-31 실측, 원인 미확정). 즉시 움직이는 건 holiday뿐.
- ⚠️ **`holiday_date`/`start_time`/`end_time`(prod 실존, DB_SCHEMA 표엔 누락)에 시각을 넣지 말 것** — 슬롯 경로가 안 읽는다. NULL로 둬도 정상 동작하고 `from`/`to`가 정본. 컬럼명만 보고 여기 `08:00`~`12:00`을 넣으면 조용히 무효.
- 🔑 **휴무 끝을 다음 그리드 시각과 정확히 같게 두면 그 칸만 죽는다** — 길이 0 FREE는 생성되지 않아 인접 칸은 보존된다. 예) 08·10시만 막고 12시 유지 = `from` KST 08:00 / `to` KST 12:00.
- **휴무는 신규 접수만 막고 기존 CONFIRMED 예약은 남는다** → 그 시각에 예약이 이미 있으면 재배정·연락이 별도로 필요.
- **검증 = 무인증** `GET https://api-prod.thetrive.com/v1/scheduling/detailers/{id}/work-day?addressId={addrId}&fromDate=&toDate=` → 해당 구간 `HOLIDAY` 전환 + **다음날 정상 FREE** 둘 다 확인. ⚠️ 블록 타입 키는 `type`이 아니라 **`kind`**(`FREE`/`HOLIDAY`/`RESERVATION`), 근무 없는 날은 `blocks: []`.
- 롤백 = 그 row DELETE 또는 `to`=`from`(위 무력화 관례).

**예약↔근무스케줄 정합성 감사 패턴 (2026-07-19, 반얀 재배정 사고 전수조사)**
- 재배정/파견 후 "예약이 디테일러 근무 밖에 배정됐나" 검증은 3축: ①근무윈도우 밖 = `reservation` × `NOT EXISTS`(rule 윈도우 매칭) ②휴무 겹침 ③동시각 이중배정 = `GROUP BY detailer_id, reservation_datetime HAVING COUNT(*)>1`.
- rule 윈도우 매칭 정석: `ru.day_of_week = UPPER(DATE_FORMAT(CONVERT_TZ(r.reservation_datetime,'+00:00','+09:00'),'%a'))` (**KST 요일** 기준) + `TIME(CONVERT_TZ(r.reservation_datetime,...,'+09:00')) >= TIME(DATE_ADD(ru.start_time, INTERVAL 9 HOUR)) AND ... < TIME(DATE_ADD(ru.end_time, INTERVAL 9 HOUR))` (end 배타적). `ru.deleted_at IS NULL` 포함. 스케줄은 `effective_from <= r.reservation_datetime < effective_to`.
- ⚠️ **재배정 API는 근무윈도우 밖 배정도 통과시킬 수 있다** (실증 2026-07-19: 08~17 근무자에게 18시 예약 배정 성공 — 이한결 사례). "시스템이 막아줬겠지" 가정 금지 — 대량 재배정 후엔 위 감사 필수.
- ⚠️ **`detailer_holiday`에 `from`=`to`인 '무력화' row 실존** (blanket 휴무 해제 시 삭제 대신 from=to로 눌러두는 관례, 2026-07-18 반얀 파견 해제). from/to range 겹침 판정에선 자동 배제되지만, row 존재/COUNT 기반 "휴무 있음" 판정은 오판 → **`from <> to` 필터** 필수.
- 🔴 **휴무 겹침 ≠ 충돌 — 운영이 `detailer_holiday`를 "전담 잠금"으로도 쓴다 (2026-08-30 실측).** memo `종일 비활성화 - <고객> N건 전용` 패턴은 그 디테일러를 **그 예약들에만** 붙이려고 하루를 통째로 막아둔 것이라, 그 안의 예약은 의도된 배정이고 충돌이 아니다(최우석143 · 2026-09-04 · 정영환 4건). ⟹ 휴무 겹침을 기계로 판정할 땐 **`memo`를 같이 뽑아 볼 것.** 단 같은 블록에 **전용 대상이 아닌 예약이 섞여 있으면 그건 진짜 충돌**이다(같은 날 권샛별 18:00 1건 실존) — 오탐이라고 블록 통째로 버리지 말고 고객으로 갈라야 한다.
- 🔴 **대량 행사 홀드: 홀드 인원 ≠ 실제 배정 인원 — "그날 여력 0"의 절반이 예비 인력이다 (2026-09-06 실측).** 단체 출장이 잡히면 `detailer_holiday`에 `memo LIKE '%행사지원%'` 종일 row가 인원수만큼 깔리고, 예약은 같은 `location` 1곳에 **한 사람당 4건이 전부 같은 시각(10:00)** 으로 들어간다(2026-09-07 곤지암 건업길 195). 그런데 홀드 41명 중 **곤지암 예약을 실제로 든 사람은 21명뿐이고 19명은 예약 0건**이다. ⟹ 재배정 후보를 뽑으면 이 19명은 휴무 필터에 걸려 전부 탈락해 "서울 전역 후보 0명"이 되지만, 실체는 **운영이 풀 수 있는 예비 인력**이다. **`memo`에 행사 키워드가 있는 홀드는 그날 예약 건수를 같이 세어, 0건인 사람을 "해제 후보"로 따로 보고할 것** — 후보 없음으로 끝내지 말 것. (같은 방향의 함정: 아래 "휴무 겹침 ≠ 충돌")
- ⚠️ **`detailer_holiday`엔 soft-delete 컬럼이 없다 — 하드 삭제다.** 과거 알림·리포트가 왜 그 판정을 냈는지 되짚을 때 그 시점의 휴무 row가 이미 사라져 있을 수 있다(운영이 날짜를 옮길 때 DELETE 후 재INSERT). 흔적은 **id 공백**뿐 — `SELECT a.id+1 gap_start, MIN(b.id)-1 gap_end FROM detailer_holiday a JOIN detailer_holiday b ON b.id>a.id WHERE a.id BETWEEN ? AND ? GROUP BY a.id HAVING gap_start<=gap_end`. **"지금 DB에 없으니 그때도 없었다"로 결론내지 말 것.**

**재배정을 직접 실행할 때 — 대상 사전검증 필수 (2026-07-24)**
- 재배정 API(sales-admin `PUT /careplus/reservations-admin/{id}/schedule`, zero-api admin `PATCH`)는 대상 디테일러의 **근무시간·휴무·현직/퇴사를 전혀 검증 안 함** (`checkScheduleConflict`=같은 디테일러 동일시각 겹침만, `skipConflictCheckYn=true`면 그마저 스킵). 검증은 고객 슬롯조회 경로(`findActiveDetailers`)에만 있음 → **API 성공 ≠ 실제 가용.**
- ⟹ 재배정 대상을 고를 땐 아래를 **직접** 걸 것: ①Active 4조건(§3a: `booking_yn=1·retired_yn=0·deleted_yn=0·direct_yn=1`) ②출장 재배정이면 `supply_sheet.region <> '오토랩'`(고정샵은 필드 안 돎) ③대상 시각이 `detailer_holiday`(부분휴무 포함, from~to 둘 다 UTC 직접비교) 안에 없음 ④그 시각 겹치는 CONFIRMED 예약 없음 ⑤더미 `132/125/168` 제외.
- 🔴 **"그날 예약 0건 = 여유 있음"이 아니다 (2026-08-06 실측).** 2026-08-07 예약 0건인 디테일러 8명 중 7명이 연차·퇴사예정이었다(한홍구·김승규·남경우·김민호·김남용·장태훈·이승제). 건수로 인계처를 고르면 **가장 안 되는 사람만 뽑힌다.** 후보는 위 ①~⑤ + effective 스케줄 존재를 먼저 통과시킨 뒤 건수로 정렬할 것.
- ⚠️ **실제 테이블명은 `detailer_supply_sheet`** (문서·구두로 "supply_sheet"라 부르지만 `SHOW TABLES LIKE '%supply%'`엔 `detailer_supply_sheet`·`detailer_supply_load_log`·`detailer_supply_weekly_snapshot`뿐). 유용 컬럼: `name`·`status`·`cell_name`(셀장)·`region`(Z번호)·`phone_norm`·**`home_address`(자택, 디테일러 출퇴근 동선 판단용)**·`car_plate`·`batch`(기수)·**`hire_date`(입사일)**·`work_start_date`(실투입일)·`retired_date`. ⚠️ `name`·`phone_norm` 비교 시에도 **`COLLATE utf8mb4_general_ci` 양쪽에 붙일 것** — 안 붙이면 `Illegal mix of collations`로 죽는다.
- 🔴 **이 테이블은 사본이다. SOT는 시트, DB는 매일 1회 전량 덮어쓰기 (2026-08-16 실측).** 시트 `1jAoHuZRjoQ0W2Amsf1rUvxMSwMsqO9L_BJuWW0YQrSw` "공급 현황" 탭에 붙은 **바운드 Apps Script**(`loadDetailerSupplyToDB`)가 매일 12:38 KST에 142행을 읽어 `detailer_supply_sheet`(+`_weekly_snapshot`)에 UPSERT한다. **DB 행을 직접 UPDATE하면 다음 실행에 덮여 사라진다** — 고칠 값은 시트에서 고칠 것. 바운드 스크립트라 clasp 프로젝트(`~/claude/*-gas-live`)엔 없다.
- 🔴🔴 **`status`는 허용값 5개 밖이면 그 행이 통째로 스킵되고 DB는 옛 값에 얼어붙는다 (2026-08-20 실측).** 적재기 `ALLOWED_STATUS = {현직, 교육중, 파견, 퇴사, 하차}`. 시트에 `오토랩 근무`처럼 목록 밖 문자열을 적으면 그 행은 `INSERT`도 `UPDATE`도 안 되고 **직전 값이 그대로 남는다** — 에러도 나지 않고 `error_message`도 null이다. 실사례: 송민호·최윤호·손정민·전승엽 4명이 시트상 `오토랩 근무`인데 DB엔 계속 `현직`으로 남아(송민호는 2026-02-13부터) 카라멜포스 스케줄 상단 요약이 총원 65·현장 58로 과대집계됐다. **DB의 `status`를 "지금 시트에 적힌 값"으로 믿지 말 것.**
  ```sql
  -- 얼어붙은 행 탐지: skipped_bad_status > 0 이면 그 수만큼 stale 행이 있다
  SELECT DATE_FORMAT(started_at,'%Y-%m-%d %H:%i') started, total_rows, eligible_rows,
         updated_rows, skipped_no_phone, skipped_bad_status
  FROM detailer_supply_load_log ORDER BY started_at DESC LIMIT 3;
  ```
  ⚠️ 새 상태값이 필요하면 시트만 고쳐선 안 되고 **바운드 스크립트의 `ALLOWED_STATUS`를 먼저 늘려야** 한다. 순서를 반대로 하면 계속 스킵된다.
  ⚠️ **정상 baseline = `skipped_bad_status` 1** (시트의 `퇴사자` 구분행이 상태 빈칸이라 항상 1건 잡힌다). **2 이상이면 누군가 목록 밖 값을 적은 것.**
  ✅ **허용값은 6개다: `현직·교육중·파견·퇴사·하차·타부서`** (2026-08-20 `타부서` 추가 — 오토랩·정비영업 등 세차 조직 밖 재직자. 부서 구분은 `region`에 적는다). `타부서`는 주간 스냅샷 `active_cnt`/`active_assumed_cnt`에서도 `퇴사`와 함께 0 처리된다 — 안 그러면 `work_start_date`가 채워져 있어 활성으로 세어진다.
- 🔑 **카라멜포스(sales-admin) 스케줄 보드 상단 요약 = `status` 3개 값의 단순 합.** `GET /careplus/detailer/supply-sheet/schedule-summary`가 `GROUP BY status` 후 `현직`=현장·`파견`·`교육중`만 세고 총원 = 그 셋의 합이다(`region` 무관). 그래서 오토랩 등 세차 안 하는 인원을 빼려면 **`status`를 고치는 게 유일한 경로**이고, 새 값을 쓰면 집계 코드 변경 없이 자동 제외된다. 반대로 `region`으로 거르려면 소비자 전부(요약·셀장 스코프·디렉터리·공지)를 고쳐야 한다.
- **적재 가동/신선도 판정 = `detailer_supply_load_log`** (`started_at`·`total_rows`·`inserted_rows`/`updated_rows`·`error_message`). 값이 이상하면 쿼리를 의심하기 전에 여기부터 볼 것.
  ```sql
  SELECT TIMESTAMPDIFF(HOUR, MAX(started_at), NOW()) hours_ago,
         (SELECT COUNT(*) FROM detailer_supply_load_log WHERE DATE(started_at)=CURDATE()) today
  FROM detailer_supply_load_log;   -- 정상 = hours_ago<24, today=1
  ```
  ⚠️ **판정은 서버가 계산한 값으로**(`TIMESTAMPDIFF`·`COUNT`). JSON에 찍힌 시각 문자열을 눈으로 읽으면 §직렬화 함정(표시값 = 저장값−9h)에 걸리고, KST로 저장된 컬럼에 `CONVERT_TZ('+00:00','+09:00')`까지 씌우면 **이중 변환**돼 날짜가 하루 어긋난다. 2026-08-16 실측 `hours_ago=34, today=0` = 그날 적재 누락.
- 🔑 **`*_load_log` 패턴은 외부 적재기 공통 지문이다.** 어떤 테이블이 시트/외부에서 적재되는지 찾을 땐 코드가 아니라 DB에서 역추적할 것 — `information_schema.TABLES WHERE TABLE_NAME LIKE '%_load_log%'`. 현재 `detailer_supply_load_log`(가동)·`complaint_log_load_log`(휴면, 마지막 2026-05-18) 2개. 전체 지도는 아래 §외부 적재 테이블.
- 🔴 **디테일러 근속·입사일 정본 = `detailer_supply_sheet.hire_date`. `detailer.created_at`을 쓰면 틀린다 (2026-08-14 실측).** `detailer`엔 입사일 컬럼이 아예 없고 `created_at`은 행 생성일이다 — 최솟값이 2024-12-17(테이블 이관 흔적)이라 실제보다 근속이 짧게 나온다(김희헌80: row 2025-02-13인데 첫 WASHED 2024-05-06 = 9개월 손실). **`schema.prisma`에 없는 GAS 동기화 테이블이라 Prisma만 보면 "입사일 데이터가 없다"고 오판하기 쉽다**(실제로 그렇게 오판했다). `hire_date`는 `batch`·첫 세차와 정합(이승원21 12-18 입사 → 12-27 첫 세차). ⚠️ 활성 65명 중 1명 미매칭 + 김희헌 1건 어긋남 → **`created_at` 폴백 금지, 예외는 수동 확인.** 참고 분포(2026-08-14, §3a 65명): 근속 1년+ 19명 · **2년+ 0명 · 3년+ 0명** — 결함이 아니라 직영 조직이 2024-12(1기) 시작이라 사실이다.
- 현직 판별: `detailer_supply_sheet.status='현직'`이 정본(퇴사/하차/삭제/교육중 제외). ⚠️ `detailer.retired_yn`은 미신뢰 — 실제 퇴사자도 0인 경우 있음(주진우147, retired_yn=0인데 booking_yn=0·supply_sheet 퇴사). `booking_yn=0`이 실질 비활성 시그널. supply_sheet 조인=phone `REPLACE(phone,'-','') COLLATE utf8mb4_general_ci`, `status IS NULL`=로스터 누락(퇴사 아님, 확인 필요).
- ⚠️ **반얀 파견 예외**: 반얀 파견 디테일러(`detailer_work_schedule.type LIKE 'BANYAN%'`, 예 `BANYAN_TREE_EXTENDED`)는 정상근무 차단용 **종일 휴무**가 걸려도 그날 배정된 **반얀 예약(장충단로 60)은 본인 담당** → 휴무충돌 감사·재배정 대상에서 제외(2026-07-24 이형준161 사례).
- 🔴 **존별 인당 부하(per_head)를 낼 땐 파견조를 분모에서 빼라 — 안 그러면 여유가 있는 것처럼 보인다 (2026-08-17 실측).** 반얀 파견조는 GPS상 Z9(장충동)에 찍히지만 장충단로 60 상주라 **그 존의 일반 예약을 받을 수 없다.** 8/18 Z9를 통으로 세면 37건/11명 = 3.4로 가장 여유로워 보이는데, 파견 12건/5명을 분리하면 일반은 **25건/6명 = 4.2**로 상위권이다. ⟹ 부하 랭킹·재배정 후보 탐색은 `type LIKE 'BANYAN_TREE%'`를 **별도 집계**로 뺀 뒤 볼 것. 같은 논리가 종일휴무자에도 적용된다 — "그날 예약 0건"은 여유 인력이 아니라 대개 연차다(§휴무 윈도우로 반드시 교차확인).
- ⚠️ **반얀 예약 매칭은 `LIKE '%장충단로 60%'` 금지** — '장충단로 600'·'장충단로 60길'을 오탐한다. **`location REGEXP '장충단로 ?60($|[^0-9길])'`** 를 쓸 것(공백 없는 '장충단로60'까지 커버, caramel-zero `isBanyanAddress` 정규식과 같은 기준). ⚠️ 파이썬 `mysql.connector`로 실행할 때 `%`가 들어가면 이스케이프 함정이 있으니 REGEXP가 안전하다.
- 🔴 **셔플은 2026-07-30부터 17시가 아니라 KST 14시다** (caramel-api PR #515, main `634d562`, prod 반영). 아래 항목들과 §재배정 역추적의 "17시" 서술은 그 이전 기준이니 **`modified_at` 지문은 `HOUR=14 AND MINUTE=0`**으로 볼 것. 그리고 **2026-08-06부터 파견조(`type LIKE 'BANYAN_TREE%'`)는 셔플 대상에서 완전 제외**됐다(#525/#526, main `c3194e2e`) — 아래 "파견조에 새 위반이 계속 생긴다"는 서술은 그 이전 상태다(§3d 참조).
- ⚠️ **"이 예약이 셔플(당시 17시, 현재 14시 동선 재배정) 대상인가"는 DB 컬럼만으로 판정할 수 없다 (2026-07-26 확정).** `reservation.allow_shuffle_yn`(DEFAULT 1)은 *옮겨지는 예약* 쪽만 막는다 — 코드의 move 로직은 **받는 디테일러를 보지 않는다**(swap은 양쪽 예약을 본다). 즉 `allow_shuffle_yn=0`으로도 "그 디테일러에게 다른 예약이 들어오는 것"은 못 막는다(실사례: 2026-07-24 셔플이 을지로 예약 81101을 반얀 파견조 정순욱187에 배정). 반대로 반얀 예약은 **주소 문자열 게이트**(`user_address`의 address+building_name+jibun_address에 '반얀트리'/'장충단로 60'/'장충동2가 201' 포함 여부)로 이미 이동이 막혀 있어 `allow_shuffle_yn=1`이어도 안 옮겨진다. → 셔플 영향 판정은 반드시 코드 게이트(`libs/route-optimization`)를 함께 확인.
- 🔴 **운영이 슬랙에서 말하는 "이 예약 고정돼 있어요" = `reservation.allow_shuffle_yn=0`이며, "이 디테일러로 고정"이라는 뜻이 아니다 (2026-08-06 확립).** 어드민 콘솔 고정 버튼 = `pinNoShuffle(userId)`(zero `prisma-console-reservation-schedule.repository.ts`)이고, **유저 단위로** 그 사람의 활성 예약(CONFIRMED/IN_PROGRESS) 전부에 `allow_shuffle_yn=0`을 박는다. 담당자를 지정하는 필드가 **아니고, 수동 재배정을 막지도 않는다**(셔플 크론만 제외).
  - ⟹ "고정이라 못 옮긴다"는 보고를 그대로 받지 말 것. **검증법 = 그 고객의 과거·미래 예약 담당자 명단을 뽑는다**(`WHERE user_id=? ORDER BY reservation_datetime`). 매회 담당자가 다르면 고정 아님 → 인계 가능. 실사례: 8/7 잠실 건이 "고정"으로 보고됐으나 같은 고객의 9건이 남경우·진정철·이형준·고대진으로 제각각이었다.
  - ⚠️ 부작용: 이 플래그가 걸린 예약은 **셔플이 교정하지 못해 존 외 배정이 그대로 남는다**. 존 외 예약 진단 시 `allow_shuffle_yn`을 함께 뽑을 것(2026-08-07 예약 159건 중 0인 건 23건=14.5%).
  - 🔴 **이 누수는 진행형이었다 — 다만 파견조 한정으로는 2026-08-06에 멈췄다.** 같은 패턴이 7/27 17:00:22에도 발생했고(#87098 반얀 16시 → 오전조 08~16인 정순욱187 = 근무 종료 경계 초과) 당시엔 매일 셔플 후 새 위반이 생겼다. **#525/#526(main `c3194e2e`, 2026-08-06 prod) 이후 파견조는 셔플이 아예 안 건드리므로 이 유형의 신규 발생은 없다** — 대신 기존 tail이 자동 회수되지 않고 남는다(§3d). **파견조가 아닌 디테일러에는 이 경고가 그대로 유효**하다. 반얀 장소 예약도 안전하지 않다 — 장소 게이트는 이동을 막지만 **근무창은 아무도 안 본다.**

**주말 데이터 주의**
- 주말 디테일러 2~3명 → fill rate 스윙이 큼
- 평일만 분석하거나 8+ 디테일러 운영일 필터 적용 권장

**Grafana 참조**
- 디테일러 가동률 대시보드: uid `fe6dr4x83wwlca`
- Grafana API: `https://thetrive.grafana.net`
- 가동률 공식: `count(*) / (5 * count(distinct detailer_id))` — 총 예약 / (5슬롯 × 디테일러 수)

### 6b-2. 슬롯 수요 계측 = 미충족 수요 정본 (`time_slot_request_log` + `time_slot_result_log`) (2026-07-31)

**"고객이 예약하려 했는데 자리가 없었나"는 `reservation`으로 볼 수 없다.** 슬롯 조회가 일어날 때마다 남는 이 두 테이블이 유일한 경로다(2025-10-30~, 40.7만건). 존별 공급 부족·핵심지역 예약 불가 진단은 여기서 출발할 것.

- `time_slot_request_log`: `id`(**varchar UUID**, int 아님) · `user_id`(**NULL 많음** — 미인증 경로) · `address_id` · `latitude/longitude` · `from_date`/`to_date`(조회 창, 앱은 보통 **한 달**) · `zone_id`(2026-06~ 채움, 커버 75%) · `duration` · `last_detailer_id`
- `time_slot_result_log`: `request_id`(위 `id`와 조인) · `time_slot`(UTC) · `detailer_id` · `priority` · **`show_yn`**
- 🔴 **`show_yn = 1` 필터 필수.** 안 걸면 미노출 슬롯까지 세어 "슬롯 있었다"로 오독한다.
- 🔴 **`reservation_id`·`reserved_at`으로 전환율을 계산하지 말 것 — 0.2%만 채워져 있다**(2026-07: 63,536건 중 139건). 그대로 쓰면 전 존 전환율 0.0%가 나오고 "아무도 예약 안 한다"로 오답한다(실제로 한 번 걸림). 전환은 `reservation.created_at`이 요청 시각 +24h 안에 있는지로 따로 판정.
- **정본 지표 3개** (Grafana `0. 카라멜_TV 대시보드` uid `ju4ln4m` 패널 9 `원인_공급`이 이 조합):
  - `days_to_first_slot` = 요청일 → 첫 `show_yn=1` 슬롯까지 일수 (존별 P75가 핵심 — Z5 11일 vs Z12 3일 식으로 갈린다)
  - `slots_within_3days` = 요청일 ~ +2일 노출 슬롯 수 (중앙값 0 = 그 존은 사실상 예약 불가)
  - `D3초과%` = `days_to_first_slot >= 3 OR IS NULL` 비율
  - 요청 단위는 세션이 아니라 `(user_id, address_id, 요청일 KST)` **dedup 후** 세야 한다(한 번 보면 로그가 3~5건씩 쌓인다).
- 🔴 **슬롯 노출 ≠ 예약 가능. `duration`은 "그 조회가 가정한 세차 시간"일 뿐 실제 소요시간이 아니다 (2026-08-10 콜 콘솔 409 조사).** 어드민 콜 콘솔·워크인·반얀 발렛은 `fallbackDurationMinutes: 60` 고정으로 조회한다(차량·세차권 컨텍스트 미전달) → 로그 `duration=60`. 반면 예약 확정은 실제 `reservation.estimated_time`(외부+내부 90분 등)으로 겹침을 검사하므로, **60분엔 들어가고 90분엔 안 들어가는 슬롯이 노출된 뒤 확정에서 409 '이미 같은 시간대에 배정된 예약이 있습니다'로 튕긴다.** 슬롯 로그로 "자리 있었다"를 판정할 때 duration을 확인하지 않으면 오독한다.
- 🔴🔴 **하루 마지막 칸은 노출되지만 확정에서 튕긴다 — 슬롯 생성과 확정 검사가 근무창을 다르게 본다 (2026-09-07 코드+prod 확정).** 슬롯 생성(`generate-time-slots.policy.ts`)은 **시작 시각만** FREE 블록 안이면 칸을 만들고 끝 시각이 근무창을 넘는지는 안 본다. 반면 셀 배정 경로의 확정 검사(`prisma-scheduling-assignment-validator.repository.ts` → `findScheduledDetailerIds`)는 `근무시작 <= 시작 && 끝 <= 근무종료`를 요구한다. ⟹ **근무 10:00~19:00인 사람의 18:00 칸은 정확히 60분인 세차만 통과**하고, 옵션 하나만 붙어도(외부+내부 60+옵션 30=90분 → 19:30 종료) 목록엔 뜬 채 '선택한 시간은 예약할 수 없습니다.'로 거부된다. 08:00~17:00 조는 16:00 칸이 같은 문제. 실측(2026-09-09 유효 DEFAULT 스케줄): 종료 19:00 **39명** · 17:00 **12명**.
  - 이 끝시각 검사는 **셀 배정 경로에만** 있고 그 경로는 **예약 날짜가 2026-09-08 00:00 KST 이후일 때만** 켜진다(`COMMON_ZONE_RUNTIME_CUTOFF`). 그 전 날짜는 옛 경로로 가고 옛 경로는 예약 겹침만 봐서 근무창 초과를 허용했다 → **"어제까진 되던 18시가 오늘부터 안 된다"의 원인.**
  - 재현: `GET /v1/scheduling/detailers/available-at?addressId=..&startAt=..&durationMinutes=..`를 60과 실제 소요분 두 값으로 쳐서 `NO_FREE_INTERVAL`이 갈리는지 본다. 무인증.
- 🔑 **"같은 시각인데 어떤 땐 되고 어떤 땐 안 된다"의 재구성 정본 = `time_slot_result_log.detailer_id`.** 같은 주소·같은 시각이라도 **조회할 때마다 묶이는 디테일러가 바뀐다**(실측: 8/12 08:00이 17:04 조회 한수용 → 17:08 조회 정순욱). 겹침 검사는 **디테일러 축**이므로 결과가 갈린다. 조사 순서 = ①`time_slot_request_log`에서 해당 시각대 요청 찾기 ②`result_log`에서 문제 슬롯의 `detailer_id` 확인 ③그 디테일러의 같은 날 예약과 `estimated_time`으로 겹침 재현. `reservation`만 봐서는 "왜 실패했는지"가 안 나온다(실패는 롤백돼 흔적이 없다).
- 🔴🔴 **`days_to_first_slot`은 `from_date` 창을 안 좁히면 정반대 답이 나온다 (2026-09-09 실사고).** 앱은 고객이 **달력을 넘긴 달**도 그대로 조회하므로 `from_date`가 오늘이 아닌 로그가 섞인다. 그걸 그대로 `MIN(time_slot) - 요청일`로 재면 "첫 슬롯까지 평균 29일 · 72%가 7일 초과"가 나와 **"그 지역은 예약이 안 된다"는 결론**이 나온다. 같은 30일을 `l.from_date <= 요청일+1`로 좁히면 **2일 내 슬롯 노출 80.8%(84/104) · 슬롯 0건 요청 0건**으로 뒤집힌다.
  - ⟹ 가용성 지표는 **반드시 `from_date` 필터를 먼저 걸고**, 남은 n을 함께 보고할 것(좁히면 표본이 1/4로 준다: 1,684 → 378).
  - 참고: `slots_within_3days`(요청일~+2일 노출 슬롯 수)는 창 필터 없이도 방향이 맞는다 — 절대 개수라 조회창 시작점에 덜 민감하다.
- 존 배정은 `address_id → user_address` 좌표 → `ST_Contains`(§2f). `zone_id` 컬럼은 커버가 75%라 전 기간 분석엔 좌표 판정이 안전.
- **어드민 화면이 이미 있다**: `/admin/map` = 슬롯 수요 지도(날짜별 존별 요청 수 + 근무 디테일러 수 + 폴리곤). zero PR #577, 2026-06-18 배포. 존 수급 질문에 새 도구를 만들기 전에 이걸 먼저 볼 것.
- ⚠️ **존별 인원·캐파를 셀 때 `dws.type='DEFAULT'` 필터를 넣어라.** 반얀 파견 스케줄의 rule도 `zone_id=8`이라 Z9로 합산돼 인원이 과대 집계된다(13명 중 6명이 반얀 상주 = 실효 7명). 위 대시보드 패널 12·15도 이 왜곡이 있다.
- ⚠️ 근무창 2시간 그리드로 만든 capacity 모델은 개인 편차·부분휴무를 놓쳐 **가동률 100%를 넘는 칸이 나온다**(실측 Z4 150%). 존 간 상대 비교용으로만 쓸 것.

### 6c. 마케팅

⚠️ `utm_source`만 있음. **`utm_medium`, `utm_campaign` 컬럼 없음** — 쓰면 에러.

**마케팅 데이터 소스 테이블**

| 테이블 | 출처 | 주요 컬럼 | 적재 |
|--------|------|-----------|------|
| `meta_daily_performance` | Meta Ads API | `date`, `total_spending`, `total_purchase_value`, `total_purchase`, `avg_cac` | Apps Script |
| `naver_daily_performance` | Naver Search Ads API | `date`, `total_cost`, `impressions`, `clicks`, `conversions` | Apps Script `SyncNaverSpend.gs` |
| `google_daily_performance` | Google Ads API | `date`, `total_cost`, `impressions`, `clicks` | Apps Script `syncGoogleAdsSpendToDB` (매일 10:00 KST) |
| `airbridge_daily_install` | Airbridge MMP | `event_date`, `install_users` | Apps Script `SyncAirbridgeInstall.gs` (매일 11:00 KST) |
| `user_attribution` | App SDK (Airbridge attribution) | `user_id`, `source`, `channel`, `campaign` | NestJS app 직접 적재 (가입 시점) |

**⚠️ `user_attribution` fan-out 함정 (2026-07-01 실사례 — 같은 질문에 3연속 다른 오답)**
- **유저 1명당 여러 row**가 있다 (`source`별: `airbridge_sdk`/`offline`/`deeplink` 등). 예: `thehyundai_seoul_popup` 캠페인 = 978 row인데 **distinct 유저는 318명** (유저당 평균 3행).
- 따라서 **`payment` 등 다른 테이블에 직접 JOIN 후 SUM/COUNT 하면 row 수만큼 뻥튀기**된다 (attribution 3행 × 결제 5건 = 15배로 카운트). 실사례: 실제 766만원이 3,641만원으로, 유료 OPTION 4만원이 2만원으로 — 매번 틀림.
- **규칙: attribution은 반드시 `SELECT DISTINCT user_id`로 먼저 접어 `IN (…)` 서브쿼리로만 연결한다. 절대 직접 JOIN해서 집계하지 않는다.**

**박제 쿼리 — 특정 채널·캠페인 유입 고객의 결제 집계 (검증 SQL, 탐색 없이 그대로 실행)**
```sql
-- 유형별 결제 집계 (fan-out 없음). :channel/:campaign 만 바꿔 쓴다.
SELECT p.type,
       COUNT(DISTINCT p.user_id)                  AS users,
       COUNT(*)                                   AS payments,
       SUM(p.amount - IFNULL(p.cancel_amount, 0)) AS net_amount
FROM payment p
WHERE p.status IN ('PAID','PARTIAL_CANCELED')
  AND p.amount > 0            -- amount=0 무료 번들 옵션(휠분진·유막제거 등) 제외
  AND p.deleted_yn = 0
  AND p.user_id IN (
    SELECT DISTINCT user_id FROM user_attribution
    WHERE channel = :channel AND campaign = :campaign
  )
GROUP BY p.type ORDER BY net_amount DESC;
```
- 총 결제고객·총액은 위 쿼리에서 `GROUP BY p.type`를 빼고 `COUNT(DISTINCT p.user_id)`, `SUM(...)`.
- **신규 여부 주의**: attribution은 재유입(deeplink)도 잡으므로, 결제자 중 **캠페인 시작 전 첫 결제자는 기존고객**일 수 있다. 순수 신규 매출은 `AND p.paid_at >= '<캠페인 시작일>'`을 추가로 건다. (실사례: 팝업 결제자 28명 중 2명은 올해 1월·작년 첫결제 기존고객 → 순수 신규 26명 / 750.6만원.)

**총 광고비 집계 (반드시 3개 채널 합산)**
```sql
SELECT DATE(date) AS dt, SUM(cost) AS total_cost FROM (
  SELECT date, total_spending AS cost FROM meta_daily_performance WHERE date BETWEEN :from AND :to
  UNION ALL
  SELECT date, total_cost AS cost FROM naver_daily_performance WHERE date BETWEEN :from AND :to
  UNION ALL
  SELECT date, total_cost AS cost FROM google_daily_performance WHERE date BETWEEN :from AND :to
) sub GROUP BY dt ORDER BY dt
```
⚠️ `meta + naver`만 합산하면 Google 누락으로 20~30% 과소 집계됨 (2026-06-08 실사례: Meta 19.3만 + Naver 7.7만 = 27만, 실제 47만. Google 20.7만 누락).

**Mixed CAC 분모 옵션**
- **설치**: `airbridge_daily_install.install_users`
- **회원가입**: `app_user.created_at` 기준 신규 가입자 수
- **첫 결제**: `payment.paid_at` 첫 결제 (`status IN ('PAID','PARTIAL_CANCELED')`, `amount > 0`)
  - 세차 서비스 신규 고객 기준이면 `type IN ('VOUCHER','SUBSCRIPTION')`도 추가할 것. OPTION/PACKAGE가 먼저 들어올 수 있어 단순 `MIN(paid_at)`이 틀린 결과를 낸다.

**알라미 리포트 데이터 소스**
- `데일리 캠페인 트래커` Google Sheets에서 읽음 (DB 직접 조회 아님)
- 광고비 적재 타이밍: Meta/Naver는 09:00 KST, Google은 10:00 KST

**시계열 교란변수 — 외부만구독 런칭(2025-10-05)**
- 2025-10 전후 단순 비교 시 재방문율, 외부만 비중, 디테일러 생산성, 세차당 소요시간이 왜곡됨
- **외부만구독 필터**: `service.wash_type = 'OUTSIDE' AND reservation.subscription_id IS NOT NULL`

**CBR 6w 패널 cutoff 정책 (2026-04-28~)**
- CBR은 수요일 진행 → 진행중 주 데이터(월~화)는 false drop을 만드는 노이즈
- **모든 6w(주간) 패널은 outer wrap으로 진행중 주 제외**: `SELECT * FROM (...) cbr_cutoff_wrap WHERE cbr_cutoff_wrap.<time_col> < 이번_주_월요일`
- 마커 `cbr_cutoff_wrap`이 idempotency 가드 (이미 들어있으면 재변환 skip)
- 12m(월간) 패널은 그대로
- 일괄 적용 스크립트: `grafana-audit/apply_cbr_cutoff.py`
- ⚠️ cutoff 연산자는 `<` (strictly less than). `<=` 금지.

### 6d. 정비·리포트

**🔴 세차→정비 전환 퍼널 — 단계마다 테이블이 다르고, 어느 단계를 분자로 잡느냐로 2.5배 벌어진다 (2026-08-07)**

| 단계 | 분자 정의 | 2025-05 실측 |
|---|---|---|
| 정비 상담 | `crm_consultation.created_at` DISTINCT user_id | 185/954 = **19.4%** |
| 정비 오더 | `crm_repair_order.created_at`, `status != 'CANCELLED'` | 81/954 = **8.5%** |
| 정비 완료 | `crm_repair_order.status='PAID'`, `paid_at` 기준 | 73/954 = **7.7%** |

- 분모 = 그 달 세차 완료 고객 수 (`reservation.status IN ('WASHED','REPORT_SENT')` DISTINCT `user_id`, live_users 필터).
- 🔴 **IR 덱·핸드오프의 "세차→정비 전환율 8.5%"는 오더 기준이다.** "상담 기준"으로 적혀 있던 것을 그대로 믿고 `crm_consultation`으로 재현하려다 19.4%가 나와 오답한 이력이 있다(2026-08-07). **지표를 인용할 땐 어느 단계인지 먼저 확인할 것.**
- 🔴 **분모를 "세차 고객"으로 자르는지에 따라 정비 완료가 30% 달라진다** — live_users 전체 PAID **643건** vs 세차 이력 있는 고객만 **449건**(2026-08-07 기준). 세차→정비 누적 퍼널을 말할 땐 반드시 `∩ 세차 고객`으로 좁힌다.
- 신호 발견 단계 = `crm_issue` ∩ 세차 고객 (`source_type` 무관 전체).
- ⚠️ **프로젝션 모델(`repair_conv_rate`)의 분모는 또 다르다** — **타겟 차량을 가진** 세차 고객(타겟율 60.3%)이고 값은 고객수가 아니라 정비 **건수** 기준이다. DB 실측 전환율과 직접 비교하지 말 것.

**정비(수리) 정산 — `crm_repair_order`**
- status: `NOT_STARTED / IN_PROGRESS / COMPLETED / PAID(정산완료) / CANCELLED`
- 금액 필드: `suggested_price`(매출액/고객 청구), `cost`(정산금액/원가), `delivery_fee`(탁송비)
- 정비마진 = `suggested_price - cost - delivery_fee`

**⚠️ 2026-07-27 정정: `crm_repair_order.paid_at` 컬럼이 생겼다 (아래 activity-log 서술은 그 이전 기준)**
- `paid_at` 커버리지 = PAID 619건 중 **616건**. 아래 "정산완료일 컬럼이 없음"은 이제 사실이 아니다.
- ⚠️ **단 두 소스가 크게 어긋난다**: 둘 다 있는 614건 중 **5분 내 일치 373건뿐, 148건은 1일 이상 차이**. 월 집계도 벌어진다(2026-04: `paid_at` 145만원 vs log+`modified_at` 폴백 335만원).
- 어느 쪽이 "정산완료일" 정본인지 **미확인**(paid_at=운영자 입력 실제 수금일 / log=UI 상태변경 시각으로 추정). 매출 집계에 쓰기 전 제품·경영지원에 확인할 것. 어느 쪽을 쓰든 **기간 비교 시 한쪽으로 통일**.
- ⚠️ **투자자 레터의 정비 매출은 이 테이블로 재현되지 않는다** — '26년 1Q 레터(1월 699·2월 1,236·3월 1,448만원)는 `paid_at`·`created_at`·마진 어느 조합과도 불일치. 회계 자체기장 출처로 추정. 상세 = memory `reference_wash_revenue_sources`.

**정산 완료일 = activity log 기준 + modified_at 폴백** (paid_at 도입 이전 방식)
- `modified_at` 단독은 메모 수정에도 갱신돼 부정확.
- 정본 = `crm_activity_log`의 `activity_type='REPAIR_ORDER_STATUS_PAID'` 행 `created_at` (`MIN`).
- 로그 커버리지 ~99% — 화면 플로우 안 거친 백엔트리는 로그 없음. INNER JOIN으로 짜면 통째 누락.
- **`LEFT JOIN` 후 `COALESCE(MIN(log), modified_at)` 폴백:**
  ```sql
  LEFT JOIN (SELECT activity_record_id, MIN(created_at) paid_at
             FROM crm_activity_log WHERE activity_type='REPAIR_ORDER_STATUS_PAID'
             GROUP BY activity_record_id) paid ON paid.activity_record_id = ro.id
  WHERE ro.status='PAID' AND ro.deleted_yn=0
    AND COALESCE(paid.paid_at, ro.modified_at) >= :from
    AND COALESCE(paid.paid_at, ro.modified_at) <  :to
  ```

**디테일러 영업 건**
- `crm_repair_order` ↔ 디테일러 직접 FK 없음. `partner`는 정비데스크 운영자.
- 영업 출처 = `crm_repair_order_issue` → `crm_issue.source_type='DETAILER'`
- 디테일러 이름 = `crm_issue.source_record_id` = `detailer.id` 조인

**타이어 마모도 조인 경로**
- `report_card`에 `reservation_id` 없음. 경로: `reservation → report(reservation_id) → report_card(report_id)`
- 타이어 마모도 타입: `rc.type IN ('TIRE_TREAD', 'TIRE_SUMMARY')`. 값은 JSON `data` 컬럼.
- 완료 전 예약은 `report` 자체가 없음 → 반드시 `LEFT JOIN`
  ```sql
  LEFT JOIN report rp ON rp.reservation_id = r.id AND rp.deleted_yn = 0
  LEFT JOIN report_card rc ON rc.report_id = rp.id AND rc.deleted_yn = 0
    AND rc.type IN ('TIRE_TREAD', 'TIRE_SUMMARY')
  ```

**세차 내용(서비스명) 조회 — `reservation_care.name`은 항상 NULL**
- `reservation_care.name` / `content` 컬럼은 전량 NULL. `care_item_id`도 대부분 미매핑.
- 올바른 경로: `user_service` → `service.name`
  ```sql
  SELECT s.name AS service_name
  FROM user_service us
  JOIN service s ON s.id = us.service_id
  WHERE us.reservation_id = :rid AND us.deleted_yn = 0
  LIMIT 1
  ```
- 값 예시: `'외부만'`, `'외부 + 내부'`, `'[리터치] 외부만'`, `'월 2회(외부만)'`

**🔴 세차범위(외부만/외부+내부)를 *집계*할 때는 `service.name`이 아니라 `service_id` 격자로 (검증 2026-08-03)**
- 단건 조회는 위 `s.name`으로 충분하지만, **`GROUP BY s.name`으로 집계하면 못 쓴다** — `user_service`에 붙는 CAR_WASH service id가 **85종**이고 프로모션·제휴 변형(`[토스] 올클린 케어`·`[프로모션]`·`[리터치] 외부만`·`올클린 케어 for 반얀트리`·`올클린 케어 (55)`·`[선물] 실내 + 실외 세차 1회권` …)이 60행 넘게 쪼개진다. 이름 3개만 보고 clean하다고 판단하면 표가 산산조각난다.
- **정본 격자 = `service.tier_id`(T1~T7) × 세차범위:**
  | 범위 | service_id (T1→T7) |
  |---|---|
  | 외부 + 내부 (ALLCLEAN) | 14 · 17 · 20 · 23 · 26 · 29 · 32 |
  | 외부만 (OUTSIDE_ONLY) | 15 · 18 · 21 · 24 · 27 · 30 · 33 |
  | 내부만 | 16 · 19 · 22 · 25 · 28 · 31 · 34 |
- 이 21개가 앱 정규 카탈로그분이고, 나머지 64종은 프로모션/제휴/패키지/구독 전용이다. **"정규 구매 기준" 집계는 이 격자로 좁히고, 격자 밖 비중을 함께 보고**할 것(리터치·제휴가 통째로 빠지므로 목적에 따라 포함 여부를 명시).
- 참고: 격자 밖이 무시할 수준이 아니다 — 리터치 `[리터치] 외부만`만 해도 WASHED 기준 수백 건 규모.
- 예약당 `user_service` 다중 row는 410/53,457 = **0.8%**(옵션·패키지 동반분), CAR_WASH 아닌 service.type(INSPECTION·MEMBERSHIP·WATER_REPELLENT)은 219/70,187 = **0.3%** → `user_service` 스키마 절의 `MIN(service_id)` dedup 패턴으로 충분하고 별도 `service.type='CAR_WASH'` 필터는 정밀 집계에서만 필요.

**구독 플랜의 상품 구성 조회 — `payment.name`('월 2회')만으론 구성을 알 수 없음**
- 구독 플랜명(월 1회/월 2회)은 어떤 세차 조합인지 말해주지 않는다. 구성은 구독에 연결된 발급 세차권으로 확인:
  ```sql
  SELECT s.name, us.created_at
  FROM user_service us
  JOIN service s ON s.id = us.service_id
  WHERE us.subscription_id = :sid AND us.deleted_yn = 0
  ORDER BY us.created_at
  ```
- 예: "월 2회" 구독 = 매월 갱신 시 `'외부 + 내부'` 1매 + `'외부만'` 1매 자동 발급 (검증 2026-07-13, 구독 3101).

**서비스 상품 이름 동일해도 내용 다를 수 있음**
- 같은 이름이라도 `description`이 다름. 예: `올클린 케어 (29)`는 왁스코팅 포함, `(55)`/`(35)`는 미포함.
- 상품 비교·집계 시 `name`만 보고 "동일"로 단정 금지. `description`도 함께 조회·확인.

**🔑 대면/비대면(고객 입회) 여부 = `wash_result.crm_type` (검증 2026-08-03)**
컬럼명이 "CRM 타입"이라 대면 여부로 보이지 않지만, 디테일러 앱이 세차 후 여기에 기록한다. **다른 대면 판정 컬럼은 없다.**

| 값 | 의미 | 건수(전량) |
|---|---|---|
| `ON_CALL` | 비대면 | 28,855 |
| `FACE_TO_FACE` | 대면 | 11,796 |
| `FACE_TO_FACE_EXPLAIN` | 대면(설명까지) — **2024-09~2025-04 레거시, 이후 미발생** | 818 |
| NULL | 미기록 | 975 (2.3%) |

- ⚠️ **`= 'FACE_TO_FACE'`만 쓰면 2025-04 이전 구간에서 대면이 과소집계된다.** 전 기간 분석은 `IN ('FACE_TO_FACE','FACE_TO_FACE_EXPLAIN')`.
- ⚠️ **대면율 분모는 `COUNT(*)`가 아니라 `crm_type IS NOT NULL`** — NULL 2.3%를 비대면으로 밀면 대면율이 낮게 나온다.
- 조인: `LEFT JOIN wash_result wr ON wr.reservation_id = r.id` (완료 전 예약엔 row 없음). `deleted_yn=0`을 걸 것 — 세차 재시작 시 옛 행이 soft delete로 남는다.
- **컨시어지 케어 건의 "세차 후 대면"은 `crm_type`으로 본다**(케어 흐름에선 안내 방식 = 대면 안내 `FACE_TO_FACE` / 전화 안내 `ON_CALL`). 같은 뜻의 두 번째 기록 = `wash_completion_communication_context.payload` JSON `$.handoverMode`(`FACE_TO_FACE`/`CONTACTLESS`, `wash_result_id`로 조인, 최신 id 1행). 2026-09-08 실측 13건 전부 둘이 일치 — 둘 중 하나만 쓰면 되고 crm_type이 전 기간 커버라 우선.
- 🔴 **"세차 전 대면(미팅)"은 서버에 남는 컬럼이 없다 (2026-09-08 코드 확인).** 디테일러가 세차 시작 때 고르는 `pre_wash_meeting`은 `wash_result.status` 초깃값(`CONCIERGE_CARE/MEETING_REQUEST`)으로만 들어가고 진행하면 덮어써진다(`max_status`도 그 단계가 맨 앞이라 안 남음). 유일한 흔적 = `wash_result_audio.type='PRE_WASH_CONVERSATION'` 행 유무(미팅 화면 녹음). 녹음을 안 하면 미팅이 있어도 0으로 보이는 **하한 추정치**다. 세차범위별로 보려면 같은 §6d의 「세차범위 집계 = service_id 격자」와 교차.
- 🔑 **실측 시그널(2025-08~2026-08, 1회권·live_users)**: 대면율은 **세차범위가 회차보다 크게 좌우**한다 — 외부+내부 첫 세차 2,214/4,615 = **48.0%** vs 외부만 191/879 = **21.7%**(2.2배). 내부 세차는 차 안 접근이 필요해 키 전달·입회가 물리적으로 발생. 회차가 늘면 둘 다 감소(외부+내부 6회차+ 30.2% / 외부만 11.7%). **"대면 접점이 있는 세그먼트"를 정의할 때 회차만 보면 틀린다.**

**세차 회차(n번째 세차) 매기기**
```sql
ROW_NUMBER() OVER (PARTITION BY r.user_id ORDER BY r.reservation_datetime, r.id) AS nth
-- 모수는 status='WASHED'만. 구독/1회권을 섞어 순번을 매길지는 목적에 따라 명시할 것
-- (1회권만 필터한 뒤 순번을 매기면 "생애 n번째 세차"가 아니라 "n번째 1회권 세차"가 된다)
```
⚠️ MySQL `GROUP BY <alias>`는 SELECT의 CASE alias가 아니라 원본 표현식으로 묶이는 경우가 있다 — `CASE WHEN nth>=6 THEN '6+' ... END AS nth`로 버킷팅하고 `GROUP BY nth`하면 **에러 없이 6+가 안 합쳐진 채** 결과가 나온다. 버킷은 `LEAST(nth,6)` 같은 식으로 만들고 그 식으로 GROUP BY할 것.

🔴 **제품이 고객에게 말하는 "N번째 세차"는 위 분석용 회차와 기준이 다르다 (2026-09-06).** 컨시어지 케어 안내 채팅("이 차량의 3번째 세차에 동행해…")과 셀장 셀 스케줄 카드의 `· N번째`는 **차량 기준**이다 — 같은 `reservation_car.car_id`의 `status IN ('WASHED','REPORT_SENT')` 예약 수(`deleted_yn=0`, 자기 자신 제외) `+ 1`. 정본은 `apps/api/src/domains/reservation/infrastructure/persistence/concierge-care-chat-writer.ts`의 `loadConciergeCarePastWashCount`.
- 위 `PARTITION BY user_id` + `status='WASHED'`만으로 세면 **고객이 채팅으로 받은 숫자와 다른 값**이 나온다 — 차 2대 보유(user 기준이 더 큼)와 `REPORT_SENT` 건(WASHED만 세면 더 작음) 양쪽에서 갈린다.
- ⟹ **"고객이 본 회차와 맞춰야 하는" 질문(CS 문의·화면 대조)은 차량 기준, 코호트 분석은 고객 기준.** 어느 쪽인지 먼저 확정하고, 보고에 기준을 같이 적을 것.

**사진 테이블 구조**
세차 전/후 사진은 `wash_result_image`가 메인(신규), `reservation_image`는 구버전.

| 테이블 | 전/후 구분 컬럼 | 값 |
|--------|----------------|-----|
| `wash_result_image` | `status` | `'BEFORE'` / `'AFTER'` |
| `reservation_image` | `type` | `'BEFORE_WASH'` / `'AFTER_WASH'` |

```sql
-- wash_result_image → reservation 경로
JOIN wash_result wr ON wr.id = wri.wash_result_id AND wr.deleted_yn = 0
JOIN reservation r ON r.id = wr.reservation_id
WHERE wri.deleted_yn = 0
```

**⚠️ `wash_result_image.status`는 BEFORE/AFTER 둘만이 아니다 (검증 2026-07-29)**
전량 분포: AFTER 258,839 / BEFORE 243,191 / **DEFAULT 42,551**(정보성) / DONE 584 / ON_PROGRESS 208 + 타이어 상세(`FRONT_TIRE_TREAD`·`REAR_TIRE_SIZE` 등) 20여 종 소량.
→ **전·후 사진 매수를 셀 때 `status IN ('BEFORE','AFTER')` 필터 없으면 약 8% 과대**(DEFAULT가 대부분). 반대로 "이 세차의 모든 사진"이면 필터를 빼야 한다 — 목적에 따라 명시할 것.
(참고 규모 2026-07-29, 테스터 제외 실고객 WASHED 기준: 사진 540,464장 / 사진 보유 세차 39,631건 = 세차의 94.3%, 세차당 13.6장.)

BEFORE/AFTER 섹션 종류:
- 외부: `OUTSIDE_FRONT`, `OUTSIDE_DRIVER_SIDE`, `OUTSIDE_PASSENGER_SIDE`, `OUTSIDE_FRONT_GLASS`, `OUTSIDE_DRIVER_SIDE_WHEEL`
- 내부: `INSIDE_DRIVER_SEAT`, `INSIDE_CENTER_FASCIA`
- 컨시어지 케어(2026-09~): `CONCIERGE_CARE/CUSTOMER_CARE`(신경쓰이는 곳) · `/VEHICLE_TRAIT`(차량 특징) · `/SHOWCASE_CONTAMINATION`(전후 대비) · `/EXTERIOR_CONTAMINATION` · `/INTERIOR_CONTAMINATION`
- 🔴 **컨시어지 케어 항목의 식별자는 `section`이 아니라 `(section, tag, index)` 3개 조합이다 (2026-09-06 실측).** 한 섹션에 항목이 여러 개 들어간다 — 내부 오염 한 섹션에 '운전석 매트'·'콘솔'·'카시트'가 각각 전/후 한 쌍씩. `tag`가 고객에게 보이는 항목 이름(디테일러 자유 입력 또는 프리셋)이고, **같은 tag가 두 번 나올 수 있어** 그때 `index`(그룹 안에서 0부터)가 구분한다. `GROUP BY section`으로 항목 수를 세면 서로 다른 항목이 한 덩어리가 된다.
- ⚠️ **접두사가 `WOW_FLOW/` → `CONCIERGE_CARE/`로 바뀌었지만 기존 행은 마이그레이션 안 됐다** (2026-09-02 이름 통일). 2026-08 이전 건까지 세려면 `section LIKE 'WOW_FLOW/%' OR section LIKE 'CONCIERGE_CARE/%'` 둘 다 걸 것. 반대로 `CONCIERGE_CARE/FRONT_DIRTY`·`/FILM_REMOVAL_PROCESS`는 코드에만 있고 prod 행이 0건이다(실데이터는 전부 `WOW_FLOW/` 접두사).
- 🔴 **오염 사진 5종이 다 "오늘 한 일"이 아니다.** 그날 실제로 진행하는 건 `CUSTOMER_CARE`(신경쓰는 곳) · `VEHICLE_TRAIT`(차량 특징) · `SHOWCASE_CONTAMINATION`(전/후 대비, 추가 촬영) 3종이고, `EXTERIOR_CONTAMINATION`·`INTERIOR_CONTAMINATION`은 **케어 제안용 진단 사진**(세차 전 촬영 → '케어할 오염 선택' 단계 입력)이다. 그중 무엇을 실제로 진행했는지는 **디테일러 앱 로컬(AsyncStorage)에만 있고 서버엔 없다**(`wash_result_contamination_plan` 테이블은 비어 있는 dormant 구조). → **"진행한 케어 건수"를 오염 사진 수로 세지 말 것.**
- 🔴 **`status='BEFORE' AND section='CONCIERGE_CARE/VEHICLE_TRAIT'` 행이 없으면 그 세차는 마지막 제출에서 반드시 실패한다 (2026-09-08 실사례, wash_result 46924).** 차량 특징 화면엔 "건너뛰기"가 있어 특징 없이 지나갈 수 있는데, 케어 제안 화면의 제출은 그 값을 필수로 요구해 앱이 **서버 호출 전에** 예외를 던진다(`전송 실패 / 세차 정보를 저장하지 못했습니다`). 서버 계약(`WashCompletionCommunicationContextV1.vehicleTrait`)도 필수라 우회 없음.
  - ⟹ **API 로그·DB에 실패 흔적이 전혀 없다. 유일한 지문이 이 행의 부재다.** "제출이 안 된다" 문의가 오면 먼저 이걸 세라: `SELECT SUM(status='BEFORE' AND section='CONCIERGE_CARE/VEHICLE_TRAIT') FROM wash_result_image WHERE wash_result_id=?` → 0이면 원인 확정.
  - 동반 지문: `status='AFTER'`인 VEHICLE_TRAIT 행의 `tag`가 NULL이다(정상 건은 BEFORE와 같은 tag가 들어간다). 세차는 `wash_result.status`가 `CONCIERGE_CARE/GUIDE_RESPONSE`에, 예약은 `IN_PROGRESS`에 멈춘다.
- 🔴 **평가 컬럼(`evaluation_status`, `evaluated_at`, `evaluator`)은 가동 중이다 — "전량 PENDING·미사용"이라는 옛 서술은 폐기 (2026-08-14 실측).** Droplet 사진품질 크론이 KST 08:30에 채운다. 2026-06-17~08-13에 **11,319장** 평가됨(PASS 10,653 / WARN 590 / FAIL 76). 단 아래 셋을 안 걸면 결과가 뒤집힌다:
  - **샘플링이다. 전수가 아니다** — 디테일러당 **1예약/일**만 평가한다. "지적 없음"은 *"평가된 건 중 지적 없음"*이지 "무결점"이 아니다. 비율을 낼 때 분모를 세차 전체로 잡지 말 것.
  - **v1 평가기(~2026-06-15)는 과탐지라 반드시 컷오프** — v1은 WARN 84%(회전 오판·불가능한 기준·빈 사유). `evaluated_at >= '2026-06-17'`을 쓴다. 06-15가 아니라 **06-17**인 이유 = 크론 대상일 off-by-one 버그가 06-16에 고쳐져 그 이전 구간은 평가 대상일이 하루씩 밀려 있다.
  - `evaluated_at`은 **UTC 저장** — 어드민에서 "오늘" 선택 시 어제 예약이 보이는 것과 같은 이유.
  - 참고 분포(2026-08-14, §3a 활성 중 평가데이터 보유 59명): **전원이 지적을 1회 이상 받았다.** 최장 무결점 연속 = 평균 9.8일·최대 29일, ≥10일 27명 / ≥20일 4명 / **≥30일 0명**. "무결점 30일" 같은 기준을 세우기 전에 이 분포부터 확인할 것.

**🔴 차량 거래(매매)는 DB에 기록 시스템이 없다 (2026-08-04 전수 확인)**

회계상 **연 100억원 규모의 최대 매출원**인데 DB엔 거래 이력이 한 건도 없다. "매매 실적 0" = 사업이 없다는 뜻이 **아니다.** 매번 재탐색하지 말 것:

| 테이블 | 행 수 | 실제 용도 |
|---|---:|---|
| `deal` | **0** | 빈 테이블 |
| `transaction_receipt` | **0** | 빈 테이블 |
| `heydealer_daily_report` | **0** | 빈 테이블 |
| `dealer` | 171 | 내부 담당자 리스트(이름/전화) — 거래 기록 아님 |
| `bank_account_transaction` | 1,275 | **디테일러 급여 정산**(`detailer_id` FK) — 매매 아님 |
| `vehicle_inspection` | 332 | 점검 기록, 마지막 행 2025-11-20 |

- `SHOW TABLES LIKE '%trade%'`·`'%sale%'` → 0건. 매매 전용 테이블 자체가 없다.
- ⟹ 매매 대수·매출·"세차 고객의 매매 전환율"은 **회계 기장 또는 계획치**로만 말할 수 있다. DB로 산출한 것처럼 쓰면 안 된다. 상세 = memory `reference_repair_and_trade_revenue_sources.md`.

**반얀 현장 리포트 payload (`banyan_presentation`) — 사진 키가 코드와 다르다 (2026-08-21 실측)**

- 5섹션 구조: `concern`(신경쓰는 곳) · `feature`(차량 특징, `featureId` = 코드 `FEATURE_CATALOG` 14종) · `freestyle`(선택) · `front`(전면부 유막) · `leveler`(상하부 수평기) + `comment`.
- 🔴 **사진 키 = `beforePhotoUrl`/`afterPhotoUrl`.** 코드 타입(`apps/web/.../report-editor/report-state.ts`)의 `beforePhoto`/`afterPhoto`·`photo`로 `JSON_EXTRACT`하면 **에러 없이 전건 NULL**이 나와 "사진 없음"으로 오판한다. `front`·`leveler`도 코드상 단일 사진이지만 payload는 before/after 쌍이다.
- 커버리지(291건, 2026-07-21~08-21): `feature` 100% · `concern.note` 84.5%(246) · `freestyle` 107 · `comment` **9건뿐** · `feature.note`는 **전건 빈 문자열**(설계상 미사용).
- 리포트 사진은 `wash_result_image`에 **안 들어간다**(S3 `banyan-report-photos/` + 이 payload만). 그 테이블의 반얀 행은 어드민 완료 처리용 placeholder다.
- 고객/현장 화면은 **무인증 공개**: `https://b2b.thetrive.com/banyan/report/{token}` (현장 프레젠테이션 모드 `?mode=onsite`).

### 6e. 쿠폰

- 테이블: `coupon_code`(개별 코드), `coupon_campaign`(파트너 캠페인 — **`partner_name`** 필드), `coupon_code_reward`(보상 정의), `coupon_code_usage`(사용 이벤트). **`coupon`/`discount` 테이블은 없다.**
  - ⚠️ 컬럼명 함정: **`coupon_code`에 `used_yn`/`user_id` 없다**(사용 여부는 `coupon_code_usage` row 유무로만 판정). `coupon_code_reward`도 `type`/`value`가 아니라 **`reward_type`/`reward_id`/`package_name`/`package_service_id`**.
- ⚠️ 코드명 LIKE 검색 오탐: `code LIKE '%KCC%'`는 랜덤 발급코드(예: `YKCCHAZB`)가 대량 매칭됨. 파트너 프로모션은 정확 매칭으로 특정.
- 쿠폰 → 발급 세차권 조인: `coupon_code_reward.id` → `user_service.coupon_code_reward_id`
- ⚠️ **캠페인→예약전환 조회 시 발급경로 3가지 다 확인**: 캠페인마다 세차권 연결 컬럼이 다르다 — ① `user_service.coupon_code_reward_id`(코드별 보상 경유) ② `user_service.coupon_campaign_reward_id`(캠페인 단위 보상 `coupon_campaign_reward` 경유 — 예: 자스민 캠페인 80 → reward id 86) ③ `service` 직결(코드 등록 즉시 특정 서비스 지급 — 예: "자스민 전용 무료 세차권" = `service.id=140`, `coupon_code_reward` 레코드 자체가 0건). 한 경로가 0건이라고 "예약 전환 0건"으로 단정하지 말 것 — 캠페인명으로 `service.name` 매칭 + `coupon_campaign_reward.campaign_id` 양쪽을 교차 확인.
- **`coupon_campaign_reward.group_no` = 회차 묶음** (제휴처 N회권 패키지 구조): N회권 상품은 회권별로 **별도 캠페인**으로 등록된다(예: 현대백화점 프리미엄 세차 패키지 1/3/5회권 = campaign 73/74/75, `partner_name='현대백화점'`). 각 캠페인의 reward를 `group_no`로 회차별로 묶는다 — `group_no=1~N`이 각 회차분(그룹마다 `reward_type='SERVICE'` 1개 + `OPTION` 세트 반복), `group_no=NULL`은 회차 무관 패키지 전체 1회 보너스(`PROMOTION`·추가 `OPTION` 등). 따라서 "N회권"의 실제 세차 횟수는 `COUNT(DISTINCT group_no) WHERE reward_type='SERVICE'`로 세야 정확(reward row 수로 세면 OPTION/PROMOTION 포함돼 과대). `reward_type`은 `SERVICE`/`OPTION`/`PROMOTION` 혼재.
- 🔴 **어드민 쿠폰 발급 API로는 N회권(패키지) 캠페인을 만들 수 없다** — `POST /v1/admin/coupon-campaigns`(`prisma-admin-coupon-campaign.repository.ts`)는 리워드를 만들 때 `group_no`를 아예 넣지 않는다. 어드민으로 발급하면 리워드가 전부 `group_no=NULL`이 되어 **회차 묶음이 아니라 개별 지급**이 된다(예: 벤틀리 campaign 93은 어드민 발급이라 1회권 구조). 현백 73/74/75처럼 회차 묶음이 필요하면 SQL/스크립트로 `group_no`를 직접 채워야 한다. "어드민에서 만들면 된다"고 답하면 틀린다.
- 🔴 **`coupon_campaign`의 `name`·`partner_name`은 라벨이 아니라 zero-api/web 코드의 상수 키다 — 이름만 바꿔도 집계·알림·앱 화면이 조용히 틀어진다.** 실제로 문자열을 키로 쓰는 곳: ① `prisma-coupon.repository.ts`의 `COUPON_PACKAGE_REDEEM_PAYMENT_AMOUNT_BY_CAMPAIGN_NAME`(캠페인명 → 합성 POINT 결제 금액. 매칭 실패 시 기본 80,000으로 떨어져 세차당 매출이 부풀려진다) ② `department-store-premium-package-campaign.policy.ts`(`partner_name==='현대백화점'` + 이름 정규식 → 등록완료 화면·세차권 상세 진입 게이트) ③ `my-wash-ticket.helpers.ts`의 partner_name → 세차권 설명 맵 ④ `partner-vip-alert.rules.ts`의 `packageKeyPatterns: ['campaign:{id}:']`(제휴 슬랙 카드 라벨). ⟹ 제휴처를 갈아끼우려고 캠페인을 새로 파거나 이름을 고칠 땐 이 4곳을 같이 봐야 한다.
- **N회권 정의의 두 번째 경로 = 코드 상수 `EntitlementPackageDefinition`** (위 쿠폰 캠페인 구조와 별개, 2026-07 확인): 프리미엄/반얀트리 세차 패키지의 구성(회차 수·포함 옵션)은 **DB product 테이블에 없고** caramel-zero `apps/api/src/domains/commerce/domain/entitlement/entitlement-package-definition.ts`에 하드코딩(`PREMIUM_WASH_PACKAGE_1/3/5`, `BANYAN_WASH_PACKAGE_5/10`). product/car_tier_product에서 패키지 구성을 찾으면 허탕. **매 회차 왁스(option 1)+살균(option 2)은 전 패키지 공통 강제 세트**(`requiredOptionIds=[1,2]` 공통 상수), standalone 옵션(유막제거 option 3)만 패키지별로 다름(현백 5회권=1장, 반얀 10회권=2장, 반얀 5회권=0장).
- **패키지 발급 실데이터 조회** = `entitlement_package_instance`(1행=1회차, `package_name`·`status`) ⋈ `entitlement_package_item`(`package_instance_id`, `item_type`='SERVICE'/'OPTION', 연결 컬럼은 **`user_service_id`/`user_option_id`로 분리** — 범용 item_id 컬럼 없음) → `user_option.option_id` → `options`(⚠️ 테이블명 복수형, `option` 아님). 옵션 id: 1=프리미엄 왁스 코팅, 2=차량 전체 살균, 3=유막 제거/발수 코팅.
- ⚠️ **패키지 오지급 회수 = 5테이블 세트 전부** (2026-07-21 반얀 오발급 실사례, 회수 API 없음 — 승인된 SQL만 경로): ① `user_service.deleted_yn=1` ② `user_option.deleted_yn=1`(강제 왁스/살균 + standalone 유막까지) ③ `entitlement_package_instance.status='CANCELLED'` + `deleted_at=NOW()` ④ `entitlement_package_item.status='CANCELLED'` ⑤ 미수금 `crm_note.deleted_yn=1`(안 지우면 CS가 틀린 금액 수금). user_service만 지우면 instance/item이 ACTIVE로 남아 옵션 자동묶음·중복차단 로직이 유령 패키지를 봄. soft-delete 컬럼 혼재 주의: instance=`deleted_at`(datetime), item=`status`만(soft-delete 컬럼 없음), user_service/user_option/crm_note=`deleted_yn`. 실행 전 `used_yn=0 AND reservation_id IS NULL` 가드로 미사용분만 특정.
- ⚠️ **쿠폰 세차권으로 생성된 예약만 조회할 땐 `user_id` JOIN 금지** — `coupon_code_usage → user_id → reservation.user_id`로 붙이면 그 유저의 쿠폰과 무관한 **전체 예약**이 섞인다. 반드시 `user_service.reservation_id` 경유로 연결 (위 3가지 발급 컬럼 중 캠페인 구조에 맞는 것으로 user_service를 특정한 뒤 reservation_id로 조인).
- ⚠️ **`coupon_code.name`과 `payment.name`을 `UNION`/`UNION ALL`로 합치면 MySQL 1253 "Illegal mix of collations" 에러.** `payment.name`=`utf8mb4_general_ci`, `coupon_code.name`=`utf8mb4_unicode_ci`로 컬레이션이 다르다(테이블별 컬레이션 혼재 — 다른 테이블 쌍도 의심). 수정은 파라미터가 아니라 **컬럼 쪽에 `COLLATE`**: `p.name COLLATE utf8mb4_unicode_ci`. 목킹 유닛테스트로 못 잡고 실 DB 실행에서만 드러남 — 새 테이블 쌍을 UNION으로 합칠 땐 `SELECT TABLE_NAME,COLUMN_NAME,COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='caramel-prod' AND TABLE_NAME IN (...)`로 먼저 대조.
- **전환 퍼널 = 발급≠사용**: ① `coupon_code_usage`(수령) → ② `user_service.reservation_id IS NOT NULL`(예약) → ③ `reservation.status='WASHED'`(완료). 무료 쿠폰은 ①→②에서 대량 이탈.
- **등록된 쿠폰의 보상 종류(할인/무료세차/옵션) 집계는 두 경로 UNION**: ① `coupon_code_usage → coupon_code_reward(coupon_code_id)`(코드별 보상) ② `coupon_code_usage → coupon_code.campaign_id → coupon_campaign_reward`(캠페인별 보상). 한쪽만 조인하면 절반이 빠진다(2026-09-03 60일 실측: PROMOTION 코드별 97명 vs 캠페인별 166명, SERVICE 39 vs 109).
- 🔴 **쿠폰 "등록자" 모수는 `coupon_code_usage`로 세라. `promotion_application` 경유는 발급경로가 바뀌면 조용히 0이 된다 (2026-08-12 실측).** `promotion_application`은 쿠폰 등록과 **같은 트랜잭션에서** 생긴다(`created_at` diff 0초 — "적용 시점에 생긴다"가 아니다). 함정은 **보상 연결 컬럼이 시점에 따라 바뀐다**는 것: jyc 캠페인은 **2026-06-26부터 `coupon_code_reward_id` → `coupon_campaign_reward_id`로 전환**됐고 7/1 이후 등록은 100% 후자다(등록월별 code_reward/campaign_reward = 3~5월 56/0 → 6월 35/16 → **7월 0/17** → 8월 2/22). `coupon_code_reward_id`만 조인한 추적기는 **7월 이후 쿠폰 등록자를 0명 잡았다**(JYC 시트 실사고). ⚠️ 옛 캠페인만 보면 격차가 거의 없어(56 = 47/50) "두 경로 비슷하다"고 오판한다 — 위 §6e "발급경로 3가지 다 확인" 경고의 재발 사례이고, `coupon_code_usage`는 경로 변경에 면역이라 모수용으로 안전하다.
- 🔴 **제휴처 모수는 `campaign_id` 하드코딩 대신 `coupon_campaign.partner_name` 전수로 (2026-08-12 실측).** 같은 제휴처가 코드 소진 후 **동명 후속 캠페인을 새로 발행**한다: jyc = 56·57 `[jyc] 첫 세차 프리미엄 패키지`(2026-03-04) → **62·63 `..._2`(2026-05-04)**. campaign_id를 박아둔 추적기·시트는 **신규 캠페인을 조용히 통째로 놓친다** — 실사례: JYC 추적 GAS가 56·57만 봐서 _2 등록자 61명 중 **57명 누락(그중 30명은 이미 세차 완료)**, 전 기간 세차완료자가 113명으로 보였으나 실제 145명(32/145 = 22% 과소). 판정 = `JOIN coupon_campaign cpn ON cpn.id = cc.campaign_id AND cpn.partner_name = '<제휴처>'`. ⚠️ 위 현대백화점 N회권 분할(73/74/75)과는 **다른 축** — 그건 회권별 동시 분할, 이건 시간순 재발행이다.
- 리텐션/매출은 `user_service.paid_amount`와 `payment`(status='PAID') 양쪽으로 교차검증. 무료세차 당일 결제는 현장 옵션 업셀 — `payment.paid_at > 무료세차 washed_at`로 진짜 재방문만 분리.
- **쉘 계정 어뷰징**: 무료 쿠폰 코호트엔 `app_user.phone IS NULL` + 랜덤 이름(`name REGEXP '^[A-Za-z0-9]{6,8}$'`) + 예약 0건인 가짜 계정이 섞임. 실사용자 모수는 **`phone IS NOT NULL`** 필터.

### 6f. 고객 간 선물(`user_gift_service`) — `deleted_yn` 의미가 뒤집혀 있다 (2026-08-30 실측)

세차권을 다른 고객에게 넘기는 경로. `from_user_id` → `to_user_id`, 넘기는 대상은 `user_service_id`, 공유 링크 키는 `uuid`, `message`는 선물 메시지(21%만 채움).

- 🔴 **`deleted_yn=1`은 삭제가 아니라 수령 완료 마킹이다.** 표준대로 `deleted_yn=0`을 걸면 **수령분 195건이 통째로 사라져 수령률이 0%로 나온다.** 교차표 실측: `deleted=1 & to_user_id 있음`=195(수령 완료) / `deleted=1 & to_user_id 없음`=108(발신자 취소·만료) / `deleted=0 & to_user_id 없음`=93(수령 대기). **`deleted_yn=0 & to_user_id 있음`은 0건** — 즉 수령되면 반드시 1이 된다.
- 🔴 **`received_at` 컬럼은 존재하지만 396행 전부 NULL이다.** 이름만 보고 수령 판정에 쓰면 0건. **수령 판정 = `to_user_id IS NOT NULL`**.
- ⚠️ **발신자 집계는 전화번호로 묶어라.** `from_user_id` GROUP BY 하면 한 사람의 복수 계정이 갈라진다(§app_user 중복). 실측에서 상위 발신자 2명이 5개 계정으로 쪼개져 있었고, 내부 임직원 6계정이 396건 중 203건(51%)이라 **안 빼면 "발신자당 몇 장" 류 지표가 통째로 왜곡**된다.
- 실측(2024-07-22~2026-03-30, 396건): 수령 195/396 = 49.2%, 수령자 174명. **2026-04 이후 발행 0건** — 레거시 웹(`references/legacy/careplus-web/app/gift`)에만 있고 zero로 이관되지 않았다.
  - 어뷰징 점검: `user_address.address`+`detail_address`로 세대 묶기, 같은 주소 생성 버스트 탐지, `app_user.dealer_id`/`created_by`로 딜러 경유 확인, `app_user.phone`과 `detailer.phone` 대조(디테일러 셀프-어뷰징).
- ⚠️ **프로모션 "종료" 판단 함정**: 로그인 게이트(`/careplus/auth/promotion/*`)가 막혔다고 쿠폰까지 막힌 게 아니다. `POST /careplus/coupon/apply`는 프로모션 상태와 무관한 앱 공용 엔드포인트로, 검증은 `coupon_code.expired_at`/`max_usage_count`만 본다. "프로모션 막혔나요?" 질문엔 로그인 엔드포인트뿐 아니라 **해당 쿠폰의 `expired_at`도 같이 확인** 필수 — 안 그러면 로그인 게이트만 막고 코드 자체는 방치돼 계속 재적용 가능한 뒷문이 남는다(KCC·토스 사례 반복).
- ⚠️ **`coupon_code.name` LIKE 검색 시 코드 모델 착각 주의**: 같은 `name`으로 캠페인당 **1개 공유코드**(KCC `voucher_kcc`)인 경우와 **유저당 1개씩 개별 발급**(토스 "토스 유저 쿠폰", 5만 row)인 경우가 섞여 있다. `SELECT * WHERE name LIKE '%키워드%'`로 순진하게 조회하면 후자는 row가 수만 개 쏟아진다 — 먼저 `COUNT(*) GROUP BY name`으로 코드 개수 모델부터 확인. 미사용 코드 수 계산 시 `coupon_code_usage`는 `COUNT(*)`(usage row)와 `COUNT(DISTINCT coupon_code_id)`(실사용 고유 코드 수)가 다르므로 반드시 distinct 기준으로 뺄 것.

### 6f. 분석 코호트

**주행거리**
- `car.mileage` = 가장 최근 주행거리 스냅샷 (단일 조회용)
- `car_mileage` 테이블 = 이력 레코드(`car_id`, `mileage`, `record_date`, `type`) (시계열 비교용)
- ⚠️ **커버리지 함정**: `car.mileage` 단독은 전체 차량 ~39%(타겟차 ~36%)만 채워짐 — 이것만 쓰면 모수 절반 누락. 차량별 "현재 주행거리"는 3단 fallback으로 복구:
  `COALESCE(NULLIF(car.mileage,0), 최신 checkup.mileage, 최신 car_mileage.mileage)` — checkup=`ORDER BY checkup_datetime DESC LIMIT 1`(방문 실측), car_mileage=`ORDER BY record_date DESC LIMIT 1`. fallback 포함 시 타겟차 커버리지 ~36%→~51%.

**세차 횟수 — 고객 기준 vs 차량 기준**
- **고객 총 세차** (기본 해석): `COUNT(*) FROM reservation r2 WHERE r2.user_id = r.user_id AND r2.status IN ('WASHED','REPORT_SENT') AND r2.deleted_yn = 0`
- **특정 차량 세차**: `reservation_car` CTE로 `car_id` 기준 집계 → 이 차가 몇 번 세차됐나
- ⚠️ "세차 횟수" 요청 시 기준이 명시 안 되면 고객 총 세차(user_id 기준)가 기본. 차량 단위 요청엔 `reservation_car` 경유 필요.
- 완료 상태 필터: `status IN ('WASHED', 'REPORT_SENT')` (REPORT_SENT도 세차 완료로 처리됨)

🔴 **한 사람의 세차가 여러 `user_id`·여러 `car.id`로 갈린다 — user_id로 세도 car_id로 세도 과소 카운트다 (2026-08-27 실측).** `01047046662`: 실고객 계정 2개(`7263` 본계정 / `102709` 이름이 전화 뒷자리 `6662`, 2025-08 가입)에 카니발 `214오3008`도 car 행 2개(`76147`@7263 / `73300`@102709). 실제 완료 세차는 4회인데 car 76147 기준 1회·계정 7263 기준 3회로 화면마다 달라 CS 문의가 났다(2025-08 첫 세차가 옛 계정에 있어 양쪽 다 안 보임).
- ⟹ **고객 1명 이력 조회는 `phone`으로 계정을 먼저 모으고**(`REPLACE(phone,'-','')='...'`), 차량은 `car.id`가 아니라 `plate_number`로 묶는다.
- ⚠️ 이런 중복 계정은 `test_yn`/`temp_yn`으로 안 걸러진다(둘 다 정상 고객 계정). 반대로 이름이 전화 뒷자리·자모인 계정을 §5b 지문 필터로 테스트로 지우기 전에 **세차 이력이 있는지 먼저 볼 것** — 실고객일 수 있다.

**`reservation.key_direct_handover_yn`**
- "세차 당일 다른 사람이 키를 전달할거예요" 체크박스. TinyInt: 1=대리 전달, 0=본인 직접, null=미설정(구버전).

**매출 계산**
- ⚠️ `payment.amount` 직접 사용 금지 — 구독/횟수권 고객(~75%)은 payment 레코드 없음
- 정확한 방식 (개발팀 CBR 정산 쿼리):
  1. `user_service` + `service` → 서비스 기본가
  2. `user_option` + `options` → 옵션 추가가
  3. `cart_item` → `cart` → `payment` → `payment.metadata` JSON_TABLE로 실결제 항목 추출
  4. 포인트 차감: 비례 배분 (`item_price / total_price * point_amount`)
  5. 최종: `sale_price = base_price - point_alloc`
- **직접 짜지 말 것 — 완성본이 있다: `~/claude/scripts/tmp_mar_revenue.sql`.** 위 5단계가 전부 구현돼 있다. 날짜 리터럴(`'2026-03-01' AND '2026-03-31'`) 두 군데만 바꿔 실행하면 `wash_count / total_revenue / avg_revenue_per_wash`가 나온다.
- ⚠️ **`reservation_revenue`는 테이블이 아니다** — 위 SQL 안의 마지막 CTE 이름이다. `JOIN reservation_revenue`를 쓰면 실행 자체가 실패한다. (2026-07-26 실사례: 테이블로 착각해 "매출 산출 불가"로 오판 후 근사치로 대체함)
- ⚠️ **`cbr_daily_revenue_snapshot.total_revenue`를 보고용 매출로 쓰지 말 것** — 실제 대비 **25~30% 과소**. (2026년 5월: 스냅샷 1.36억 vs 실제 1.80억) 빠른 감만 볼 때 외 금지.
- 검증 기준: 위 SQL 재현값 vs `Caramel_monthly(A)` 시트 확정값 오차는 **+0.4~0.6%가 정상**(2026년 3·4월 실측). 이 범위를 넘으면 필터를 의심할 것.
- ⚠️ **후불(현장수금) 예약은 이 매출 SQL에서 통째로 0원이다.** CTE가 `IF(us.payment_id IS NULL, 0, …)`라 payment가 없는 후불 예약은 금액이 0으로 깔린다. 후불을 포함한 매출을 내려면 `reservation_onsite_collection` 수금액(§ 해당 섹션 공식)을 **세차일(`reservation.reservation_datetime`) 기준으로 별도 가산**해야 한다. (2026-07-27 CBR v2 실측: 7/20주 타겟 매출 1,477만 → 후불 8건 74.7만 누락 = -5.1%)
- ⚠️ **제휴처 오프라인 수금 매출은 DB 어느 매출 쿼리에도 안 잡힌다.** 두 패턴 모두 "결제 row를 봤으니 반영됐다"고 착각하기 쉬우니 `amount`가 아니라 **`amount − point`(현금)** 로 확인할 것:
  - **현대백화점 팝업 패키지** = `payment` row는 **있다**. 다만 `metadata.syntheticPointPayment=true`·`source='COUPON_PACKAGE_REDEEM'`로 **amount 전액이 POINT**(`payment_medium` CASH=0, POINT=amount)라, `amount − point` 공식을 그대로 쓰면 **0원으로 상쇄**된다. (2026년 6~7월 PACKAGE amount 합 5,635만 vs 그 공식상 현금 654만) → **이 point는 고객 포인트 잔액이 아니라 제휴 정산용 합성값이므로 차감하면 안 된다.** 매출/GMV 집계 시 예외 처리할 것:
    ```sql
    -- 포인트 차감에서 COUPON_PACKAGE_REDEEM 제외 (CBR v2 #335/336 채택, 2026-07-27)
    - IF(JSON_UNQUOTE(JSON_EXTRACT(p.metadata,'$.source')) = 'COUPON_PACKAGE_REDEEM', 0,
         COALESCE((SELECT SUM(pm.amount) FROM payment_medium pm
                   WHERE pm.payment_id = p.id AND pm.medium = 'POINT'),
                  CAST(JSON_UNQUOTE(JSON_EXTRACT(p.metadata,'$.point')) AS SIGNED), 0))
    ```
    인식 시점 = `paid_at` = 쿠폰 등록/지급일(구매일 아님. 실제 현금은 제휴처가 받아 우리 PG를 안 거친다).
    🔴 **그래서 `payment.type='PACKAGE'`의 건수·금액 추이를 "5·10회권 판매"로 읽으면 오답이다.** 팝업 쿠폰 사용분이 같은 type으로 들어와 판매처럼 보인다 — 실측 2026-06 PACKAGE 195건 중 **279건 상당이 `프리미엄 세차 패키지 1회권`(COUPON_PACKAGE_REDEEM)**이고 실제 5·10회권 판매는 **8건 399만원**뿐이었다(2026-07은 380건 vs 7건). 회권 판매만 세려면 `source <> 'COUPON_PACKAGE_REDEEM'` + `name LIKE '%회 이용권%'`로 좁히고, **반얀 지급분은 payment에 없으니 `crm_note`를 따로 더할 것**(바로 아래 항목).
  - **반얀트리 5·10회권** = `payment` row 자체가 없다(어드민/콜콘솔 grant + 계좌입금). `user_service`(service **137** '프리미엄 세차 패키지 올클린 케어')가 `product_id NULL·payment_id NULL·paid_amount 0`으로 지급되므로 **user_service엔 금액이 없다.** **금액 정본 = `crm_note.memo LIKE '%회권 지급 · 수금할 금액%'`** — grant 1tx가 남기는 로그에 금액이 박혀 있다(예: "반얀트리 프리미엄 세차 10회권 지급 · 수금할 금액 750,000원"). 5회권 400,000 / 10회권 750,000, 회차 구분은 memo 텍스트로만(`entitlement_package_instance.package_name`엔 회차수 없음). ⚠️`crm_note.created_at`은 UTC.
  - **유효 판매 판정 4조건** (그냥 crm_note를 세면 과대집계된다):
    ```sql
    -- 반얀 회권 유효 판매/수금액 (2026-07-27 검증: 16건 11,650,000원 = 운영 수동집계 일치)
    SELECT MIN(n.created_at) ts, n.user_id,
           CAST(REPLACE(REGEXP_SUBSTR(n.memo,'[0-9,]+원'),',','') AS UNSIGNED) amt
    FROM crm_note n JOIN live_users lu ON lu.id = n.user_id
    WHERE n.memo LIKE '%회권 지급 · 수금할 금액%'
      AND n.deleted_yn = 0                                    -- ① 취소된 note 제외
      AND EXISTS (SELECT 1 FROM user_service us               -- ② 지급 세차권 생존 = 회수분 제외
                  WHERE us.user_id = n.user_id AND us.service_id = 137 AND us.deleted_yn = 0)
    GROUP BY n.user_id, DATE(DATE_ADD(n.created_at, INTERVAL 9 HOUR)),  -- ③ 오지급→재지급 중복제거
             CAST(REPLACE(REGEXP_SUBSTR(n.memo,'[0-9,]+원'),',','') AS UNSIGNED);
    -- ④ live_users(테스트 계정 제외) — 7/19 내부 테스트 배치 8명이 여기서 걸러진다
    ```

**헤이딜러(외부공급) 세차 — `manual_wash_adjustment`**
- `reservation`에 안 잡힌다. 별도 테이블 `manual_wash_adjustment`의 `SUM(count)`, 날짜 컬럼은 `wash_date`(KST 저장, 변환 불필요).
- ⚠️ **`memo='헤이딜러'`만 필터하면 누락** — `memo='조준호'`도 외부공급이다. **둘 다 합산**(전체 합산이 맞다). 2026년 월평균 조준호분 ~100건.
- 매출 환산은 `건수 × 8.80만원`(고정 가정, (A)시트 헤이딜러 객단가와 동일). 위 세차 매출 SQL엔 포함되지 않으므로 따로 더할 것.
- 마감 후 retro 입력으로 과거 월 수치가 ±25건 움직일 수 있다 — 이전 마감값과 다르면 정상.

**구독 갱신 결제액이 매달 다른 이유 — `payment.metadata.prices`로 추적**
- 같은 구독인데 결제액이 월마다 변동(예: 111k~124k)하면 프로모션 할인. `metadata.prices[]`의 `originalPrice`(정가) vs `price`(실결제) 차이 + `promotionApplicationId` 존재 여부로 판별.
- 첫 결제의 `metadata.params`엔 유입 UTM(utm_source 등)이 그대로 저장돼 있어 구매 유입 채널 역추적 가능. (검증 2026-07-13)

### 6g. CRM 메시지 발송 로그 (`message`)

CRM·트랜잭션 메시지 발송 기록 테이블.

- **컬럼 의미**: 수신자=`customer_id`(→`app_user.id`, ⚠️ `user_id` 아님), 발송시각=`created_at`(UTC), 채널=`lms_type`(`ALIMTALK`/`PUSH`/`MMS`/`SMS`/`LMS`), 캠페인 식별=`type`(varchar 200, 예 `reservationGuide002`·`firstWash_expire`), 발송상태=`status`(기본 `REQUESTED`).
- ⚠️ **`sent_yn` 함정**: **ALIMTALK은 발송돼도 `sent_yn=0`·`status='REQUESTED'` 고정**(PUSH만 `sent_yn=1`). `sent_yn=1`로 필터하면 알림톡이 통째 누락된다. **행 존재 = 발송요청**으로 집계(도달 확정 아님 — BizM 도달 콜백 미반영).
- 🔴 **단, 위 `sent_yn` 함정은 레거시 CRM 경로 한정이다 — zero-api signal 경로에는 정반대로 적용된다 (2026-08-10 실측).** 두 경로는 **`tracking_key` 모양**으로 갈린다: **semantic key**(`wash-start-91024`·`wash-complete-{id}`·`reservation-info-detailer-{id}`·`reservation-cancel-{id}`) = zero-api signal / **랜덤 8자**(`02c2XFru`)·NULL = 레거시 CRM. signal 경로는 `sent_yn=1`+`status='success'`가 정상이고 **`sent_yn`이 실제 성공 여부를 담는다** (최근 30일 semantic key ALIMTALK: success 19,063 / **fail 465는 `sent_yn=0`**). ⟹ **signal 경로에 "행 존재 = 발송"을 쓰면 실패분까지 발송으로 센다.** 경로를 먼저 가르고 술어를 고를 것. (실사용: 세차 시작 알림 `wash-start-*` 2,462건은 100% `sent_yn=1`+`success`.)
- ⚠️ **`message`에 `message_group`·`group_name` 컬럼은 없다** — 템플릿 구분은 `type`(`washStart003`·`washCompleted006` 등). 쓰면 `Unknown column`.
- 🔴 **`type` 키에 두 세대가 있고 **동시에 살아 있다** — 한쪽만 세면 같은 알림을 몇 배 적게 센다 (2026-08-29 실측).** `UPPER_SNAKE`(464키·763,559행, 최종 2026-08-29)와 `camelCase`(82키·10,799행, 최종 2026-08-27)가 둘 다 지금 기록 중이다. 같은 알림이 두 키로 나뉜 실례: 디테일러 예약정보 `RESERVATION_INFO_DETAILER`(59,128·2024-09~) + `reservationInfoToDetailer001`(15,515·2026-05~) — **둘 다 8/29까지 기록**된다. 반대로 **교체된** 짝도 있다: 주차 리마인더 `PARKING_INFO_REMINDER`(2025-08-21 종료) → `parkingInfo_reminder001`(현행). ⟹ **`type = '<하나>'`로 발송량을 세지 말 것.** `type IN (...)` 로 두 표기를 함께 넣거나 `type REGEXP` 로 잡는다. 어느 패턴인지(공존/교체)는 짝마다 다르니 **`GROUP BY type` + `MIN/MAX(created_at)` 로 먼저 확인**한다.
- ⚠️ **발송 여부를 `message` **본문** LIKE 로 세지 말 것 — false 0 이 나온다.** 본문은 `request.msg`/`request.content` 2종에 채널별로 한쪽이 NULL이라(아래 항목) `WHERE message LIKE '%문구%'`가 통째로 0을 돌려준다. 실사례(2026-08-29): 반얀 주차 리마인더를 `message LIKE '%주차 위치%'`로 세어 "60일간 0건"이라 결론냈다가, `type IN ('parkingInfo_reminder001','PARKING_INFO_REMINDER')`로 다시 세니 **172건**이었다. **발송량은 항상 `type`으로 센다.**
- 채널은 `type`별로 대체로 고정(윈백·구독갱신·자동예약=ALIMTALK, 쿠폰만료는 알림톡/푸시가 별도 `type`).
- 🔴 **`reservation_id`는 대체로 NULL이다 — 예약 통지 이력을 `reservation_id`로 찾으면 "안 나갔다"는 오답이 나온다** (2026-07-26 실측: 당일 `reservationUpcoming003` **216건 전부 NULL**). **예약 통지 조회 = `WHERE customer_id = :app_user_id AND created_at >= :당일`** 로. `reservation_id`가 채워지는 type도 일부 있으니(`RESERVATION_INFO_DETAILER` 등) 둘 다 확인.
  - 🔴 **zero-api signal 경로는 `tracking_key = '<이벤트>-<reservationId>'`가 정본 조회 경로다 (2026-08-10 실측).** 여기도 `reservation_id`는 NULL이다(`wash-start-91024`·`wash-start-91543` 둘 다). 위 `customer_id` 방식은 레거시용이고, signal 경로는 tracking_key가 **고객 단위가 아니라 예약 단위로 바로 잡혀** 훨씬 정확하다. 예: `WHERE tracking_key = CONCAT('wash-start-', r.id) AND sent_yn = 1`. 코드측 생성기는 caramel-zero `reservation-notification.port.ts`의 `buildWashStartTrackingKey` 등.
- **D-1 예약확인 알림톡 `reservationUpcoming003` = 매일 18:00 KST 발송, 본문에 담당 디테일러 실명이 들어간다** (`message.message` JSON → `request.msg`: "안녕하세요 고객님, 내일 세차를 담당할 **{디테일러명}**입니다…" + 예약시간·방문장소). ⟹ **재배정 판단 시 "고객이 이미 이 이름을 봤는가"의 판정 근거**(§3c 항목 6). 본문 확인은 `SUBSTRING(m.message,1,150)`으로 충분.
  - 🔴 **발송시각 09:00은 오답이다 (2026-08-06 교정).** 최근 11일 전수(`GROUP BY 날짜, MIN(created_at)`) **전부 18:00 KST**. 이걸 09:00으로 알고 있으면 **재배정 시한을 반나절 잘못 잡는다** — "오전에 이미 이름이 나갔으니 늦었다"고 포기하거나, 반대로 "내일 아침까지 여유 있다"고 오판한다(실사례: 8/7 존 외 예약 조율에서 시한을 "내일 07:00"으로 잘못 보고했다가 정정).
  - ⚠️ **결번이 있다** — 주말 외에도 안 나가는 날이 섞인다(7/31·8/1 0건). "오늘 아직 없다"를 곧바로 "장애"로 읽지 말고 **당일 18:00 이전인지부터 확인**할 것.
  - **당일 07:00 `parkingInfo001`(주차위치 안내)에도 디테일러 이름·연락처·차량번호가 들어간다** — 통지 노출 시점은 D-1 18:00과 D-day 07:00 **두 번**이다.
- 🔴 **`message.message`의 `request` JSON은 구/신 2종이 혼재한다 — `request.msg`로만 뽑으면 신규분이 통째로 NULL이다 (2026-08-06 실측).** 구=`{msg, phn, tmplId, title, …}`(알림톡 레거시 경로) / 신=`{content, recipient, channel, metadata, trackingKey, …}`. 최근 30일 기준 `tmplId` NULL이 32,366건으로 **최대 그룹**인데 이건 "템플릿 없음"이 아니라 **신규 스키마라 그 키가 없는 것**이다. 본문·템플릿 조회는 `COALESCE(JSON_UNQUOTE(JSON_EXTRACT(message,'$.request.msg')), JSON_UNQUOTE(JSON_EXTRACT(message,'$.request.content')))` 처럼 **양쪽을 함께** 볼 것. 수신번호도 `request.phn`(구) vs `request.recipient`(신)로 갈린다.
  - **판별 키 = `$.request.channel` (2026-08-10 전수 실측).** `KAKAO`(6,803건)·`MMS`(961)·`PUSH`(4)는 **`content` 100% / `msg` 0건**, `channel` NULL(5,553)만 `msg` 위주(3,549). 즉 알림톡·MMS는 `msg`로 뽑으면 **무조건 NULL**이다. ⟹ **body NULL을 "발송 안 됨"으로 읽지 말 것** — 발송 여부는 row 존재와 `created_at`으로 판정한다.
- **CRM 7일 예약전환 측정**: received(테스터 제외 live_users §5b) → 발송 후 7일 내 `reservation` 생성(`r.user_id = m.customer_id`, `r.created_at` 기준, `r.deleted_yn=0`. raw·비인과). `(customer_id, type)`별 첫 발송 dedup. 상세·재현쿼리 = caramel-api `docs/superpowers/specs/2026-06-30-crm-kill-keep-map.md` §2/§6.
- **세차 시작 되돌리기(cancel-start) 이벤트 지문 = `reservation_status_log`에서 같은 예약의 `IN_PROGRESS` 뒤에 오는 `CONFIRMED`** (2026-08-10). 전용 로그 테이블은 없다. 한 예약에 여러 번 찍힐 수 있다(시작→되돌림→재시작→되돌림).
  ```sql
  SELECT l.reservation_id, l.created_at
  FROM reservation_status_log l
  WHERE l.status = 'CONFIRMED'
    AND EXISTS (SELECT 1 FROM reservation_status_log p
                WHERE p.reservation_id = l.reservation_id
                  AND p.status = 'IN_PROGRESS' AND p.id < l.id)
  ```

### 6h. 050 안심번호/통화 (`telephony_call_log`·`customer_vno`) (2026-07-16)

디테일러↔고객 050 통화(세종 050Biz) 기록. 2026-07-08 prod 가동.

- **`telephony_call_log`**: 050 경유 통화의 CDR. `call_started_at`은 **UTC 저장**(+9h 필요, `reservation_datetime`과 동일). 예약 귀속 = `reservation_id`/`detailer_id`/`customer_vno_id` — 크론 `telephonyAttributeCalls`(*/10분)가 사후에 채움. **미귀속 2~4건/일은 정상**(부분귀속 설계: vno 매칭만 되고 예약 모호). 발신자/수신자 = `calling_num`(디테일러 실번호)·`vno`(고객 050)·`called_num`(고객 실번호).
- ⚠️ **"통화 수 적다" ≠ 장애**: 예약 중 050 통화가 잡히는 비율은 **30~45%가 정상 밴드**. 디테일러 절반가량은 앱 050 발신을 안 씀(문자 사용 — SMS는 시스템 미캡처 / 저장된 실번호 직발신). 19시 KST 통화 스파이크(일요일 포함)는 D-1 저녁 사전 확인콜로 정상.
- **`customer_vno`**: 고객에게 050 동적 부여. **user_id 단위 키잉(폰번호 아님)** — 한 폰이 여러 app_user면 특정 user_id에만 붙음. 통화↔vno 매칭 구간 = `[assigned_at, COALESCE(cleared_at, expires_at)]`. ⚠️ `ASSIGN_FAILED` 대량(수십 건/일) = 더미폰 유저(01012345678류) 매시 재시도 반복이지 시스템 장애 아님 — `COUNT(DISTINCT user_id)`로 먼저 확인.
- 🔴 **`customer_vno`에 050 번호 문자열이 없다** — `SELECT vno FROM customer_vno`는 `Unknown column`. 번호는 `vno_pool_id` FK로 **`vno_pool`(id·vno·status)** 을 조인해야 나온다(prod 전수: 번호 문자열 컬럼 `vno`를 가진 테이블은 `vno_pool`·`telephony_call_log`·`detailer` 셋뿐이고, **`detailer.vno`는 폐기된 "디테일러 고정 부여" 설계의 잔재로 전부 NULL**(163/163, 2026-08-21) — 고객 050을 찾다가 여기 조인하면 빈 결과가 난다). 배정 현황 표준형: `FROM customer_vno cv LEFT JOIN vno_pool p ON p.id = cv.vno_pool_id`. 현재 유효 배정 = `cv.status='ASSIGNED'`(과거 시점 커버리지엔 쓰지 말 것 — 현재상태 컬럼이라 회수된 과거 건이 전부 0으로 보인다. 과거는 `assigned_at`/`cleared_at` 구간으로).
- 🔴 **"이 예약에 050이 붙었나" 커버리지 판정은 `status='ASSIGNED'`와 `expires_at > UTC_TIMESTAMP()`를 둘 다 걸어야 한다** (2026-09-08 실측). 앱이 실제 통화에 050을 쓰는 조건이 이 둘이다(zero-api `findActiveCustomerVnos`). 회수 크론(`telephonyClearCustomerVno`)이 매시 정각에만 돌아 만료됐는데 `ASSIGNED`로 남은 행이 최대 1시간 존재하므로 status만 보면 과다 카운트. ⚠️ **여기서 `NOW()`를 쓰면 안 된다** — `expires_at`은 UTC 저장, `NOW()`는 KST라 9시간 어긋난다(§5a). 실제로 `expires_at > NOW()` + `reservation_datetime >= CURDATE()`로 세서 "162건 중 2건 누락"이라는 오답이 나왔고, `UTC_TIMESTAMP()`와 KST 일자 창(`CURDATE()-9h ~ +15h`)으로 고치니 **153건 전건 배정**이었다. 표준형:
  ```sql
  LEFT JOIN customer_vno cv ON cv.user_id = r.user_id
    AND cv.status = 'ASSIGNED' AND cv.expires_at > UTC_TIMESTAMP()
  ```
- **크론 실행 기록 = `job_execution`**(`job_id`→`job.name`, telephony 크론 8종). ⚠️ **status='FAILED'여도 장애 단정 금지** — 유저 1명 실패해도 execution 전체가 FAILED로 기록됨. `result` JSON의 `failureCount`/`successCount`를 먼저 볼 것.
- 🔴 **`job_execution.status='SUCCESS'`도 "그 크론이 일했다"의 근거가 아니다 (2026-08-31 실측).** 핸들러가 내부 실패를 `warn` 로그로만 삼키면 실행은 SUCCESS로 남는다(`sweepUnjudged` 실사례 — 며칠간 아무도 못 봤다). **일했는지는 산출물 테이블에서 센다**: 그 크론이 쓰는 행의 `created_at`을 KST 시(hour)로 잘라 크론 시각대에 몇 건이 쓰였는지 본다. 예 — 20시 크론이면 `SUM(HOUR(CONVERT_TZ(created_at,'+00:00','+09:00'))=20)`. 이걸로 "SUCCESS인데 0건"과 "정상"이 갈린다.
- 🔴 **같은 크론 이름·같은 `job_id` 를 zero-api 와 레거시 caramel-api 가 공유한다 — `job_execution` 만 보면 어느 쪽이 보냈는지 못 가른다 (2026-09-01 실측).** 발신 주체 판정 2단계: ① **`job.metrics.source`** 가 채워져 있으면 zero-api(파일 경로가 들어온다), **NULL 이면 레거시**. ② charts 레포 **`caramel-api-cron/values-prod.yaml`** 에서 그 이름을 찾는다 — `defaults.suspend: true` 라서 **항목에 `suspend: false` 가 없으면 zero 쪽은 안 돈다**(2026-09-01 기준 리마인더 중 `dMinus2WashReminder` 만 활성, `beforeWashReminder`·`dDayWashReminder`·`reservationReminder`·`parkingInfoReminder` 는 suspend). 레거시 스케줄은 데코레이터에 박혀 있다(`apps/batch/src/messaging/messaging.service.ts` 의 `@Cron(CronExpression.EVERY_DAY_AT_6PM)` 등) — `job.cron_time` 은 레거시 행에선 한글 문구("매일 20시")까지 섞인 **참고값**이라 믿을 게 아니다.
  - ⟹ **"제외 로직을 넣었는데 왜 계속 나가나"는 여기서 갈린다.** zero 코드만 읽고 "제외됨"으로 판정하지 말 것.
  - 🔑 **이미 나간 건은 `message` 행 하나로 발신 주체가 갈린다 — charts 레포 없이 사후 판정 가능 (2026-09-07 실측).** `JSON_EXTRACT(message,'$.result.messageId')` 가 있으면 **zero-api**(신형), `JSON_EXTRACT(message,'$.response.data.msgid')` 가 있으면 **레거시 caramel-api**. 한 날짜를 `GROUP BY type` + 두 포맷 카운트로 찍으면 어느 알림이 어느 서비스에서 나갔는지 한 장에 보인다. 실사례: 9/7 `parkingInfo001` 157건 = 전부 구형(레거시), 같은 날 나머지 알림톡 13건 = 전부 신형(zero).
  - 발송한 크론 역추적 = `message.job_execution_id` → `job_execution.job_id` → `job.name`. 대상 필터를 확인할 코드를 어느 레포에서 열지는 위 포맷 판정으로 정한다.
- 🔴 **`job.status='ACTIVE'`는 "돌고 있다"의 근거가 아니다 (2026-08-25 실측).** 테이블명은 **`job`**(`cron_job` 아님). 살아 있는지 판정하려면 두 가지를 같이 봐라: ① `job_execution`의 최종 실행 시각(`MAX(created_at)`), ② 그 job 이름의 핸들러가 코드에 실존하는지(zero-api `cron-internal.controller.ts`의 `@Post('/<jobName>')`). 실사례 — `sendRainRetouchAvailablePush`는 status `ACTIVE`인데 컨트롤러에 엔드포인트가 없고 2026-05-26에 5회 돌고 멈춰 있었다(리터치 알림이 통째로 안 나감), `sendRainPolicyUpdatedNotifications`는 61일 연속 매일 돌다 2026-07-19에 정지.

### 6i. 고객↔디테일러 인앱 채팅 읽음 판정 (`chat_room`·`chat_participant`·`chat_message`) (2026-09-08 실측)

`chat_room`(방) → `chat_participant`(참가 entity, 단톡 가능) → `chat_message`(버블) → `chat_message_content`(본문 JSON). 2026-07-01 prod~.

- 🔴 **`chat_message.sender_participant_id`는 `chat_participant.id`다 — `detailer.id`도 `app_user.id`도 아니다.** 이걸 `detailer.id`에 바로 조인하면 **에러 없이 엉뚱한 디테일러 이름**이 나온다(실측: sender_participant_id 137·175·194는 실제 발신자 김희헌·이형준·황석찬인데, detailer로 직접 조인하면 김성영·김민호·박정규가 찍힌다). 표준형은 항상 2단 조인:
  ```sql
  JOIN chat_participant sp ON sp.id = m.sender_participant_id
  JOIN detailer d ON d.id = sp.participant_id AND sp.participant_type = 'DETAILER'
  ```
- **`chat_participant.participant_id`는 `participant_type`에 따라 가리키는 테이블이 갈린다** (polymorphic, FK 없음): `DETAILER`→`detailer.id`(⚠️ `detailer.user_id` 아님), `USER`→`app_user.id`, `PARTNER`→`partner.id`. type을 안 걸고 한 테이블에 조인하면 id가 우연히 겹쳐 조용히 틀린다.
- **읽음 판정 = `cp.last_read_message_id >= m.id`** (진우님 설계: id 작으면 읽음, 크면 안읽음). `last_read_message_id IS NULL`이면 **그 방에서 한 번도 안 열어봤다는 뜻**. 방별 고객 읽음 여부:
  ```sql
  JOIN chat_participant cp ON cp.chat_room_id = m.chat_room_id AND cp.participant_type = 'USER'
  -- 읽음 = cp.last_read_message_id >= m.id
  ```
  ⚠️ NULL을 "추적 미구현"으로 읽지 말 것 — 같은 시점 82명 중 26명은 값이 찍혀 있다(2026-09-08). 기능은 돈다.
- 🔴 **`visibility='STAFF_ONLY'`는 고객에게 안 보이는 내부 메모다 — 읽음률 분모에서 빼라.** `content_type='INTERNAL_MEMO'`와 짝. 이걸 안 빼면 **"고객이 안 읽은 메시지"로 오분류**된다(실사례 2026-09-08: 셀장 채팅 읽음 집계에서 김정수 건이 "6건 중 5건 읽음"으로 나왔는데, 안 읽힌 1건이 STAFF_ONLY 내부 메모였고 고객에게 보인 5건은 전부 읽음이었다). 고객 관점 집계엔 항상 `m.visibility='ALL' AND m.deleted_at IS NULL`.
- `content_type` = `TEXT`·`IMAGE_GALLERY`·`CATALOG_CARE_PROGRAM`·`CATALOG_CONDITION_RESET_PROGRAM`·`INTERNAL_MEMO`. 본문은 `content` JSON이라 텍스트 검색은 `chat_message_content`를 조인해야 한다.
- ⚠️ **한 방에 디테일러가 2명 들어간 방이 있다**(실측 room 80·81·82). "방의 DETAILER = 담당자"로 가정하지 말고 **발신자(`sender_participant_id`) 기준**으로 셀 것.
- **셀장이 보낸 것만 세려면** `cell_membership`으로 좁힌다 — `detailer` 테이블엔 셀장 컬럼이 없다. 판정 규칙은 §3a 셀장 판정 주의사항(최신 행 하나 → role 확인)을 그대로 따를 것. 2026-09-08 시점엔 사람별 유효 행이 1개씩이라 `role='LEADER'` 선필터와 결과가 같았지만, 겹침이 생기면 갈린다.
- 시각 필터는 `created_at` **UTC 저장** — "오늘(KST)"은 `created_at >= '<어제> 15:00:00'`처럼 UTC 경계로 주고, `DATE(created_at)=CURDATE()`는 쓰지 말 것(§5a).
- 🔴 **예약에서 방으로 가는 조인이 없다 — `chat_room`엔 `reservation_id`도 `user_id`도 없다** (컬럼 = `id`·`name`·`created_at`·`modified_at`·`deleted_at`, 2026-09-09 실측). 컨시어지 케어 지정 건은 marker JSON이 유일한 경로다: `reservation_metadata` `key='concierge_care'` → `$.notification.roomId` = 그 지정의 안내 채팅방(`$.notification.messageIds` = 인사 채팅 `chat_message.id` 배열, `$.notification.status='SENT'`가 발송 완료). 지정 건이 아니면 `chat_participant`에서 `participant_type='USER'` + `participant_id=app_user.id`로 역추적할 수밖에 없다.
- ⚠️ **`chat_message.created_at`은 초 단위(`DateTime(0)`)다 — 밀리초 타임스탬프와 `>=`로 비교하면 같은 초 메시지가 통째로 빠진다.** marker `$.markedAt`(`...T06:30:13.376Z`) 이후 채팅만 세면, 지정 직전에 나간 인사 채팅이 같은 초(`06:30:13`)에 찍혀 사라진다(2026-09-09 실측: 오늘 대상 17건 중 9건이 이렇게 메시지를 잃고 2건은 대화가 통째로 0건이 됐다). 초로 절삭한 하한을 쓸 것. 같은 초 안의 순서는 `id` 오름차순이 정본이다.


---

## 6z. 외부 적재 테이블 — 앱이 안 쓰는 것들 (2026-08-16 전수)

> **판별법**: prod DB에 있는데 `schema.prisma`에 없으면 외부 적재/수동 테이블이다. 실측 268개 중 **34개**가 여기 해당. 앱 코드를 아무리 뒤져도 이 테이블들의 갱신 주체는 안 나온다.
> ```sql
> SELECT TABLE_NAME FROM information_schema.TABLES
> WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE';   -- prisma model 목록과 diff
> ```

**A. 시트 → DB (Google Apps Script가 쓴다)**

| 테이블 | 적재기 | 주기 |
|---|---|---|
| `detailer_supply_sheet` `_weekly_snapshot` `_load_log` | **바운드 스크립트**(공급 현황 시트) | 매일 12:38 |
| `google_daily_performance` | `syncGoogleAdsSpendToDB` (marketing-gas-live) | 매일 10:00 |
| `naver_daily_performance` | `syncNaverSpendToDB` | 매일 10:00 |
| `airbridge_daily_install` | `syncAirbridgeInstallToDB` | 매일 11:00 |
| `meta_daily_performance` | 마케터 계정 GAS | 매일 (소유자 계정에서만 보임) |
| `complaint_log` `complaint_log_load_log` | `loadComplaintsToDB.gs` | **휴면** (마지막 2026-05-18, 총 5회 수동) |

**B. DB → DB 스냅샷 (GAS가 prod를 읽어 prod에 캐시)** — 시트 안 거침(`getSheetByName` 0회)

`cbr_daily_revenue_snapshot` · `cbr_cohort_repurchase_snapshot` · `cbr_daily_time_compliance_snapshot`(02:00) / `cbr_detailer_daily_productivity_snapshot`(02:30) / `cbr_first_wash_cohort_snapshot`(03:00) / `cbr_daily_option_snapshot`(03:30) / `cbr_daily_wash_sequence_snapshot`(04:00)

⚠️ **스냅샷은 원본이 아니다.** 값이 이상하면 스냅샷을 고치지 말고 원본 쿼리를 재실행해 대조할 것.

**C. 그 외 (앱 밖에서 관리 — 갱신 주체 미확정 포함)**

`manual_wash_adjustment`(외부운영 세차 수기) · `price_reference`(가격 검수) · `quote_version` `quote_version_item`(견적) · `banyan_presentation` · `opportunity` · `user_acquisition_channel` · `user_utm_criteria` · `wash_plan_monthly` · `_region` · `b2b_console_account`(0행) · `heydealer_daily_report`(0행) · `ssot_*` 6종(0행, 드로플릿 `/opt/ssot`에 INSERT문은 있으나 prod 적재 흔적 없음 — **미가동 추정, 미확정**) · `_prisma_migrations`(Prisma 내부)

**D. prod DB에 쓰지 *않는* 자동화** (혼동 방지)
- **카이사르**(scrum-linear-bridge) — DB 접점 0. Linear + Slack + Upstash KV.
- **차비스 드로플릿 크론 26개** — `.env`가 `DB_USER=readonly_user`라 SQL 쓰기가 구조적으로 불가. prod 변경은 API 경유뿐(`retouch_dispatch` 배정, `qa_reservation_autocancel` 취소).
- **`jyc-gas-live`**(2번째 GAS 프로젝트, "최재윤 이사님 VIP" 시트) — 매시 **DB→시트** 읽기 전용.

## 7. 주요 테이블 컬럼 치트시트

> DESCRIBE 없이 바로 쿼리 작성하기 위한 핵심 컬럼 목록. 전체 스키마는 `DB_SCHEMA.md` 참조.

### car
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| plate_number | varchar(10) | 차량번호 |
| model_year | int | 연식 — 🔴 **NULL 42.3%**(73,080/172,888, `deleted_yn=0 AND temp_yn=0`, 2026-08-19 실측). 대체 컬럼 없음 → 아래 참조 |
| brand | varchar(15) | 브랜드명 **레거시 — 99% NULL. `brand_id` 사용** |
| brand_id | int | FK → car_brand.id (name 컬럼으로 JOIN) |
| model | varchar(100) | 모델명 레거시. `model_id` 우선 |
| model_id | int | FK → car_model.id (name 컬럼으로 JOIN) |
| mileage | int | 최근 주행거리 스냅샷 |
| user_id | int | FK → app_user.id |
| deleted_yn | tinyint | 0=정상 |
| temp_yn | tinyint | 1=임시 차량 (필터 제거 권장) |
| created_at | datetime | 차량 등록 시각 (UTC). ⚠️ **2026-07-11 이후 의미가 바뀜 — 아래 참조** |

🔴 **온보딩 v3(2026-07-11 main 머지, PR #787) 이후 `car.created_at`은 "차량 등록 시점"이 아니라 "예약 완료 시점"이다 (2026-08-10 확인).**
v3에서 로그인이 방문정보 입력 뒤로 밀리면서(`isOnboardingV3VisitInfoHandoff`), 차량은 로그인 전까지 intent로만 들고 있다가 **예약 완료 트랜잭션에서 예약과 함께 커밋**된다 — `prisma-onboarding-reservation-completion-transaction.repository.ts`의 `tx.car.create()`와 `tx.reservation_car.create()`가 같은 트랜잭션이다.
- ⟹ **예약까지 못 간 사람은 `car` row가 아예 안 생긴다.** 등록수 급감은 이탈 악화가 아니라 계측 대상이 사라진 것.
- ⟹ **"차량등록→예약 전환율"류 지표는 7/13을 기점으로 단절된다.** 분모가 분자의 부분집합에 가까워져 전환율이 구조적으로 튄다. 실측 지문: 타겟차 첫등록 후 **1시간 내 예약 비율** 7/08~7/12 0~11% → 7/13(월) **39%** → 7/27~ 43~75%. 주간 타겟차 등록수 361명(6/29주) → 52명(7/20주).
- ⟹ 가입→등록→예약 퍼널을 7/13 전후로 한 축에 놓고 읽지 말 것. 온보딩 전환은 DB로 못 잡고 Amplitude 온보딩 진입→완료 퍼널로 봐야 한다.
- ⚠️ 앱 내 차고 추가 등 온보딩 밖 경로는 여전히 독립적으로 `car` row를 만든다 — 그래서 전환율이 100%가 아니다.

🔴 **연식(`model_year`) 결측은 다른 컬럼으로 못 채운다 (2026-08-19 실측).** `model_year`가 NULL인 73,080대 중 `manufacture_at`이 있는 건 **24대**, `vin` 15대, `registered_at`·`mileage` **0대**. = 차량 상세정보는 한 덩어리로 같이 채워지고 같이 빈다(`model_year` NOT NULL 99,808 ≈ `manufacture_at` NOT NULL 99,831).
- ⟹ **연식 분포/평균을 낼 때 분모는 전체 대수가 아니라 `model_year IS NOT NULL` 대수다.** 42%가 빠지므로 "N대 중 X%가 2020년 이후"식 서술은 어느 분모인지 반드시 병기할 것.
- ⟹ 결측을 `manufacture_at`/`registered_at`으로 보정하려는 시도는 헛수고. 커버리지 0에 가깝다.
- ⚠️ 결측이 랜덤이라는 보장은 없다(상세정보 입력은 구독·정비 고객에 쏠릴 수 있음) → 연식 분포는 편향 가능성을 함께 적을 것.

**국산차 브랜드 제외 패턴:**
```sql
JOIN car_brand cb ON cb.id = c.brand_id
WHERE cb.name NOT IN ('현대','기아','제네시스','KGM','KGM(쌍용)','르노','쉐보레','캠시스','GM','르노삼성','대우','삼성')
```

---

### reservation
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| reservation_datetime | datetime | 예약 일시 **UTC 저장 → KST: CONVERT_TZ(...,'+00:00','+09:00')** |
| washed_at | datetime | 세차 완료 시각 UTC |
| status | varchar(25) | 완료: `WASHED` / `REPORT_SENT`. `COMPLETED` 미사용 |
| user_id | int | FK → app_user.id |
| detailer_id | int | FK → detailer.id |
| technician | varchar | 디테일러 **이름 문자열**(비정규화, 99.9% 채워짐). **고객 문자(D-1·당일 07시)가 이 이름으로 디테일러를 조인한다.** 미래 예약은 배정이 안 굳어 `detailer.name`과 갈리고(64%) 셔플이 D-1에 맞춘다 — 담당자 조회·귀속·`GROUP BY`는 `detailer_id` → §3e |
| estimated_time | int | 티어·서비스에서 나온 **산식(계획 소요분)**. 🔴 실제 소요시간 아님 — 실측은 `wash_result.created_at`→`finished_at` → §3e |
| address_id | int | FK → user_address.id |
| subscription_id | int | **98% NULL — 구독 여부 판단에 사용 불가** |
| location | text | 주소 문자열 |
| detailed_location | text | 상세 주소 (동/호수) |
| parking_info_content | text | 주차 안내 메모. 🔴 **알림 차단 스위치로도 쓰인다** — 30분전 "주차 위치를 알려주세요" 크론이 `parking_info_content IS NULL` 인 예약만 집으므로, 값이 있으면 그 알림이 안 나간다 → 아래 |
| deleted_yn | tinyint | 0=정상 |
| allow_shuffle_yn | tinyint | 1=셔플 크론 리배정 허용 / **0=콘솔 "고정"(유저단위 `pinNoShuffle`). 담당자 지정 아님·수동 재배정은 가능** → §3c |
| note | text | **디테일러앱 현재 예약 상세 "메모" 블록에 보이는 내부 지시란**(고객 미노출). 판매 약속·동반 방문 등은 여기 써야 담당자가 본다 |
| detailer_note | text | 디테일러가 세차 완료 시 쓰는 칸. **다음 방문 이력 탭에만 렌더** → 이번 예약 지시로 쓰면 안 보인다 |

- 🔴 **현장(반얀·천호) 예약이 "주차 위치를 알려주세요" 알림을 안 받는 기전은 크론 필터가 아니라 `parking_info_content = '자유'` 선채움이다 (2026-08-29 실측).** 예약 **생성 코드**가 반얀 주소면 이 값을 미리 박는다(`prisma-reservation-facts.repository.ts`). 30분전 크론은 `parking_info_content IS NULL` + `user_address.parking_location_content IS NULL` 만 집으므로 자동으로 빠진다. ⟹ **크론 코드만 읽고 "현장 제외가 없다"고 결론내면 오답이다** — 실제로 그렇게 잘못 답한 적이 있다.
  - ⚠️ **선채움은 생성 경로에 의존해서 샌다.** `reserved_with_date=0` 으로 만들어진 예약엔 안 붙는다. 2026-08-29 기준 미래 반얀 예약 **4건**이 조건에 걸렸고 그중 **3건은 푸시 토큰까지 있었다.** 현장 알림 차단 여부를 볼 때 "코드에 분기가 있으니 괜찮다"로 넘기지 말고 **위 두 컬럼으로 실제 대상 건수를 세라.**
  - 정본 스위치는 `field_site.flags.skipReminder` 로 옮겼다(zero PR, 2026-08-29). 이건 생성 경로와 무관하게 크론에서 막는다.
  - 🔴 **단, 이 스위치는 리마인더 4종 중 D-2 하나에만 실제로 걸린다 (2026-09-01 실측). "현장 고객은 리마인더 안 받는다"로 읽으면 오답이다.** 제외 코드는 zero-api 에 있는데 나머지 3종의 zero 크론이 charts 에서 suspend 라, 실제 발송은 제외가 없는 레거시 caramel-api(`apps/batch/src/messaging/messaging.service.ts`)가 한다.

    | 안내 | type | KST | 발송 주체 | 제외 |
    |---|---|---|---|---|
    | D-2 | `washReminderTwoDays003` | 13:00 | zero-api | ✅ |
    | 내일세차 | `reservationUpcoming003` | 18:00 | 레거시 | ❌ |
    | 주차위치(D-Day) | `parkingInfo001` | 07:00 | 레거시 | ❌ |
    | 예약안내 | `reservationReminder` | 13:00 | 레거시 | ❌ |

    실측(2026-08-31): 18:00 수신 119명 중 **현장 전용 고객 17명**(천호 10·반얀 7), 07:00 은 22명(천호 10·반얀 12).
    ⟹ **현장 알림 누수를 볼 때 "제외 코드가 머지됐다"로 끝내지 말고, 아래 발신 주체 판정(§6g)까지 갈 것.**

🔴 **`note`/`detailer_note` 쓰기 경로 = DB UPDATE만이 아니다 (2026-08-06 실측).** sales-admin `PATCH https://gateway-prod.thetrive.com/careplus/reservations-admin/{id}` 가 `note`·`detailerNote`·`complaintYn`·`complaintReason`·`overtimeExpectedReason`를 받는다(`UpdateReservationAdminDto`, `'note' in dto` 방식이라 **보낸 키만** 갱신 → 다른 필드 안전). 알림 부작용 없음. ⟹ **prod DB 직접 UPDATE 하지 말고 이 PATCH를 쓸 것.** 단 이 API는 **덮어쓰기**이므로 기존 값이 있으면 먼저 SELECT해서 이어붙인 전문을 보낼 것.

**차량 조인 (car_id 직접 없음 → reservation_car 경유):**
```sql
JOIN (SELECT reservation_id, MAX(car_id) car_id FROM reservation_car GROUP BY reservation_id) rc
  ON rc.reservation_id = r.id
JOIN car c ON c.id = rc.car_id AND c.deleted_yn = 0
```

**생성 경로·영업자 귀속 = `reservation_metadata`(`reservation_id` + `key` + JSON `value`) (2026-08-14 실측)**
- ⚠️ **한 예약에 key가 여러 개 공존한다 — `reservation_id`만으로 조회하면 엉뚱한 행이 섞인다 (2026-09-08 실측).** active 행 분포: `__platform__` 30,573 · `timeSlotRequestId` 17,873 · `admin/call` 424 · `banyan/strategy` 239 · `partner`·`partnerReason` 각 189 · `concierge_care` 100 · `flow` 19 · `concierge-care-sales/strategy` 11. 항상 `key=` + **`deleted_at IS NULL`**을 함께 걸 것 — 소프트 삭제라 해제된 marker가 행으로 남아 있다.
- `key='admin/walk-in'` = 현장접수(워크인), `key='admin/call'` = 콜콘솔 컨시어지, 둘 다 없으면 고객앱. **워크인만 세면 콜콘솔분이 통째로 빠진다.**
- 워크인 value JSON에 **`intakeChannel`**(`FIELD_SALES`/발렛/직접방문) + **`fieldSalesDetailerId`·`fieldSalesDetailerName`** = 현장영업 실제 영업자. 접수 계정(`sales.partnerId`)은 반얀 공용 `오퍼레이터`라 영업자 특정에 못 쓴다 — **"누가 팔았나"는 이 필드가 정본**.
- `key='partner'` / `key='partnerReason'` = 제휴처·VIP 예약 판정 결과(라벨과 판정 근거). **제휴 예약을 세는 정본이 여기다** — 쿠폰·utm으로 역산하면 판정 규칙과 어긋난다. 예약 생성 시점에 쓰이는 게 원칙이고, 구독 자동예약처럼 생성 이펙트를 안 타는 건은 세차 **D-1 20시 크론**(`partnerVipDailyDigest`)이 사후에 채운다 ⟹ 두 시각대가 섞여 있는 게 정상이다.
- `key='concierge_care'` = **컨시어지 케어 지정 marker(셀장이 셀 스케줄에서 카드 단위로 지정)**. value JSON: `serviceDate`(지정 대상 세차일, KST 'YYYY-MM-DD')·`leaderDetailerId`(셀장)·`memberDetailerId`(수행 디테일러)·`cellId`·`actorPartnerId`·`markedAt`·`reason`·`trialRound`(1~3, 어드민 2·3회차 생성분만)·`source`(`ADMIN_FOLLOWUP`이면 어드민 생성). (2026-09-08 실측)
  - **"셀장별 지정 건수"는 `reservation.detailer_id`가 아니라 marker의 `$.leaderDetailerId`/`$.memberDetailerId`로 센다** — 동행·재배정으로 예약 담당자와 다를 수 있다. 날짜 축은 `$.serviceDate`(18:00·22:00 컨시어지 다이제스트가 이 값으로 대상을 고른다). `reservation_datetime` KST 날짜와 어긋난 행이 실재하므로 리포트는 둘 중 하나를 명시하고 쓸 것.
  - 🔴 **테스트 예약이 섞인다**: `key='admin/bulk-free-reservation'`(어드민 무료 일괄 생성)이 같은 예약에 공존하는 행 + `app_user.name LIKE '%테스트%'`. 2026-09-07 교육용 77건이 케어 지정된 채 CONFIRMED로 남아 있다(셀장 1명 계정으로 일괄 지정, `reason=''`). 케어 실적을 셀 때 §5b 3플래그로는 안 걸러진다 — 이 key 공존 여부로 뺄 것.
- `key='banyan/strategy'` = 셀장이 쓴 판매 작전 텍스트(`{text, authorName, updatedAt}`).
  - 🔴 **수정 여부를 `modified_at`으로 판정하지 말 것 — 이 테이블은 갱신해도 `modified_at`이 안 변한다** (2026-08-17 실측: 43행 전부 `modified_at = created_at`). Prisma 모델에 `@updatedAt`이 없고 컬럼에도 `ON UPDATE`가 없다. 게다가 `(reservation_id, key)` unique 인덱스가 없어 저장 로직이 `findFirst`→`update`로 **같은 행을 덮어쓰므로 행 수도 안 늘어난다** → "아무도 수정 안 했다"로 오독한다.
  - **수정 판정 정본 = value JSON `updatedAt` vs `created_at` 비교**: `STR_TO_DATE(REPLACE(REPLACE(JSON_UNQUOTE(JSON_EXTRACT(value,'$.updatedAt')),'T',' '),'Z',''),'%Y-%m-%d %H:%i:%s.%f')`. 실측 43행 중 4행이 최대 **+972분** 뒤 수정돼 있었다(둘 다 UTC).
  - ⚠️ **`authorName`은 작성자가 아니다** — 보드 "내 카드만" 필터로 고른 **담당 디테일러 이름**이다(반얀은 공용 계정이라 세션으로 작성자를 못 가른다). 작성자별 집계에 쓰면 틀린다. 슬랙 게시에서도 이 이유로 작성자 줄을 뺐다.

---

### reservation_change_log (예약 변경 이력, 행위자 판별용)
컬럼은 `id·created_at·modified_at·reservation_id·actor_type·data` **6개뿐**.
- ⚠️ **`actor_id`·`reason`·`from_status`·`to_status` 컬럼은 없다.** 전부 `data` JSON 안이다:
  `JSON_UNQUOTE(JSON_EXTRACT(data,'$.reason'))`, `$.actorId`, `$.toStatus`.
- 생성 사유 → 경로 매핑 (2026-07 실측): `CUSTOMER_RESERVATION_CREATED`=`CUSTOMER_DIRECT` / `CHECKOUT_SETTLED_RESERVATION_CREATED`=`CHECKOUT_SETTLEMENT` / `ADMIN_RESERVATION_CREATED`=`source_type` NULL+`booked_online_yn=0` / `ADMIN_RAIN_RETOUCH_RESERVATION_CREATED`=`RAIN_RETOUCH`.
- 빙의로 만든 예약은 `actor_type='CUSTOMER'` + `actorId`=고객으로 남는다 → **어드민 행위를 이 컬럼으로 못 걸러낸다.**

### reservation_car
| 컬럼 | 타입 | 설명 |
|------|------|------|
| reservation_id | int | FK → reservation.id |
| car_id | int | FK → car.id |
| confirmed_yn | tinyint | **⚠️ 대부분 0 — 차량 조인 시 이 컬럼으로 필터 금지** |

---

### app_user
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| name | varchar(50) | 이름 |
| phone | varchar(15) | 전화번호 (하이픈 없음) |
| uuid | varchar(50) | UUID (Amplitude userId) |
| utm_source | text | 유입 채널 (`utm_medium`·`utm_campaign` 컬럼 없음) |
| referrer | text | 유입 referrer |
| test_yn | tinyint | 1=테스트 계정 |
| temp_yn | tinyint | 1=임시 계정 |
| deleted_yn | tinyint | 0=정상 |
| deleted_at | datetime(3) | 삭제일 |
| promotion_group_id | int | 프로모션 그룹 |
| note | text | 운영 메모 **⚠️ 디테일러앱에 노출된다 — 아래 참조** |

**테스터 제외 패턴:** `u.deleted_yn = 0 AND u.test_yn = 0 AND u.temp_yn = 0`

🔴 **`note` = 디테일러앱 "고객 메모"로 렌더된다 (2026-08-13 코드 확인).** zero `ReservationHeader.tsx:225`가 `reservation.user?.note`를 예약상세 **최상단 파란 박스**에 띄운다 → 담당자가 누구든·그 고객의 모든 예약에 뜬다.
- ⟹ **"예약별 지시" = `reservation.note` / "고객 상시 지시" = `app_user.note`.** 미래 예약이 0건인 고객(반얀 등 회차마다 새로 잡는 고객)은 예약 메모에 쓸 row가 아예 없으므로 이쪽이 유일한 경로다. 예약 4필드(`note`·`user_note`·`detailer_note`·`extra_care`)의 노출 위치는 메모리 `reference_reservation_note_fields_visibility`.
- 실사용 935/226,345행, 대부분 유입 태그(`블로그 체험단`·`조준영 현장영업`) → **덮어쓰지 말고 append**.
- 쓰기 = `PATCH /v1/admin/users/{userId}` (`{name,phone,note,adminYn}` `.strict()` **전필드 덮어쓰기** — 현재값 읽어 재전송). sales-admin엔 편집 UI가 없고(고객 생성 시에만 입력) zero 어드민 고객상세 모달에만 있다.

🔴 **`phone`은 UNIQUE가 아니다 — `WHERE phone = ?` 스칼라 서브쿼리는 터진다 (2026-08-06 실측).** 재가입·탈퇴 반복으로 한 번호에 행이 쌓인다(실측 `01092828753` = **218행 중 217행 `deleted_yn=1`**). `WHERE user_id = (SELECT id FROM app_user WHERE phone='…')` 는 `Subquery returns more than 1 row`로 **에러**, `IN (...)`으로 바꾸면 이번엔 탈퇴 계정들의 옛 예약이 섞여 조용히 오답이 된다.
- 특정 예약의 고객을 찾을 땐 **역방향**으로: `WHERE r.user_id = (SELECT user_id FROM reservation WHERE id = :rid)`.
- 번호로 시작해야 하면 `deleted_yn=0`으로 좁히고 **그래도 여러 행이면 최신 `id`**를 쓰되, "한 사람"으로 묶는 집계는 §4b-13(`GROUP BY 전화번호`)을 따를 것.

---

### detailer
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| name | varchar(100) | 디테일러 이름 |
| user_id | int | FK → app_user.id |
| deleted_yn | tinyint | 0=정상 |
| retired_yn | tinyint | 1=퇴사 |
| booking_yn | tinyint | 1=예약 가능 |
| admin_yn | tinyint | 1=관리자 계정 |
| tier | int | 티어 |
| slack_member_id | varchar(100) | 슬랙 멤버 ID |

🔴 **플래그는 전부 tinyint다 — `booking_yn='Y'`로 쓰면 에러 없이 정반대 집합이 나온다** (2026-08-17 실측). MySQL이 `'Y'`를 0으로 캐스팅하므로 `booking_yn='Y'`는 **퇴사·비활성 96명**을 고르고 활성 67명을 통째로 버린다. 경고도 빈 결과도 없이 그럴듯한 숫자가 나오는 게 함정 — 실제로 이걸로 "활성 디테일러 중 5명만 근무스케줄 보유"라는 엉뚱한 결론이 한 번 나왔다. `retired_yn`·`deleted_yn`·`direct_yn`·`admin_yn`도 같다. **반드시 `= 1` / `= 0`.**

🔴 **Active 디테일러 필터는 §3a를 쓴다** — `booking_yn=1 AND retired_yn=0 AND deleted_yn=0 AND direct_yn=1`(65명).
~~`d.deleted_yn=0 AND d.retired_yn=0 AND d.admin_yn=0`~~ 은 **틀렸다**(2026-08-10 폐기). `retired_yn`만 걸면 129명이 나오는데 supply_sheet 조인 결과 그중 **퇴사 38·하차 8명이 섞여 있다**(retired_yn이 0으로 남아 있음). §3a 4조건은 그 46명을 **100% 배제**한다(0/38·0/8).
- **"앱을 쓰는 사람" 기준도 §3a를 쓴다.** `booking_yn`이 예약 배정용이라 앱 권한과 무관해 보여 `retired_yn`으로 갈아타고 싶어지지만, 실데이터에선 그게 분모를 2배 부풀린다. `direct_yn=0`은 로스터 누락 1명뿐이라 빼도 무해하다(실측).
- 왜 중요한가: 디테일러별 도달/확인을 세는 기능(공지 확인 등)에서 분모에 퇴사자가 들어가면 **영구 미확인으로 남아 확인율이 64% 천장에 걸리고 미확인 명단이 퇴사자로 채워진다.**
- 층별 인원·재현 절차는 §3a 표 참조.

⚠️ **이름으로 디테일러 찾을 땐 `detailer.name`으로 검색** — `JOIN app_user ON user_id` 후 `app_user.name`으로 찾으면 일부 디테일러가 누락된다 (실사례 2026-07-16: 염철림(165)은 app_user.name으로 안 잡히고 detailer.name에만 있음).

---

### partner (어드민·현장 계정) — 🔴 "어드민 기능이 갑자기 안 된다"의 1순위 조회 대상 (2026-08-17 실측)
컬럼: `id·name·username·password·role·phone_number·user_id·detailer_id·deleted_yn·created_at`.

- **`role`이 API 권한을 결정한다.** zero-api가 로그인 시 role → capability를 JWT에 박고(`partner-capability-resolver.ts`), `admin-partner-jwt.guard.ts`가 **capability별 라우트 allowlist**로 검사한다.

| `partner.role` | capability | 접근 범위 |
|---|---|---|
| `ADMIN` | `FULL_ADMIN` | **전 `/v1/admin/*` 통과**(검사 없음) |
| `MASTER_DETAILER` | `DETAILER_CONCIERGE`+`ASSISTED_BOOKING` | allowlist에 **명시된 경로만** |
| `REPAIR` | 위와 동일 | 동일 |
| `DETAILER` | `ASSISTED_BOOKING` | 예약 잡기 계열만 (**반얀 보드 접근 불가**) |

- 🔴 **2026-08-17, 공용 계정(`operator` = partner 41, `detailer1`·`detailer2`)이 전부 `deleted_yn=1`로 정지되고 현장 인원이 개인 `MASTER_DETAILER` 계정으로 전환됐다**(username = 본인 휴대폰번호). ⟹ **allowlist에 없는 엔드포인트가 그 순간부터 전부 403**이 된다. 현장에서 "저장이 안 된다"고 오면 코드·배포보다 **먼저 `partner.role`을 조회**할 것(실사례: 반얀 판매 작전 저장 403, PR #1606).
- 🔴 **디테일러도 어드민 권한 행을 갖는다 — "어드민 권한자"를 `partner`만 보고 세지 말 것 (2026-09-08 실측).** 지금 권한 판정의 정본은 role 표가 아니라 **`admin_partner_permission`**(`partner_id·resource·action`)이고, 가드는 그 행만 본다 — **`role='admin'`인지는 확인하지 않는다.** 게다가 `AdminPartnerJwtGuard`는 디테일러앱 토큰도 partner로 해석하므로(`partner.detailer_id` 연결) **디테일러가 어드민 API에 도달할 수 있다.** prod에 `detailer_id IS NOT NULL`인 partner가 72행이고 그중 여럿이 `SCHEDULE` 등 권한 행을 갖고 있다(셀장 본인 포함). 조회: `SELECT p.id,p.detailer_id,app.resource,app.action FROM partner p JOIN admin_partner_permission app ON app.partner_id=p.id`.
- **"권한 A는 있고 B는 없는 계정"은 anti-join이다 (2026-09-08).** 권한이 `partner_id·resource·action` **행**이라 한 행 필터로는 안 나온다. `resource IN ('SCHEDULE','RESERVATION') AND action='WRITE'`로 세면 각각의 보유자 수만 나오고 교집합을 놓쳐 정반대 결론이 난다. 판정 = `SELECT ... FROM (A 권한 partner_id) a LEFT JOIN (B 권한 partner_id) b ON a.partner_id=b.partner_id WHERE b.partner_id IS NULL`. 실측(2026-09-08 prod): `SCHEDULE:WRITE` 21명 전원이 `RESERVATION:WRITE`도 보유 → 둘만 가진 계정 0명.
- ⚠️ **dev에는 권한이 모자란 어드민이 없다 (2026-09-08 확인).** dev 어드민 partner 전원이 `SCHEDULE`·`RESERVATION` 전 권한을 갖고 있어, **권한 게이트 403을 dev에서 재현하려면 계정을 새로 만들거나 권한 행을 지워야 한다.** 기존 계정으로 시도하다 "게이트가 안 걸린다"고 오판하지 말 것.
- ⚠️ **권한 변경은 재로그인해야 적용된다** — capability가 JWT 발급 시점에 박히므로 `role`만 UPDATE하면 기존 토큰은 그대로다.
- 옛 서술 정정: "반얀 현장은 공용 오퍼레이터 계정이라 개인 특정 불가"는 **2026-08-17부터 성립하지 않는다** — `crm_note.partner_id`·`partner_activity_log.partner_id`로 개인이 특정된다.

---

### subscription
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| user_id | int | FK → app_user.id |
| status | varchar(25) | `ACTIVE`/`STOPPED`/`CREATED`/`ENDED`/NULL (오타 `STOPPPED` 소량). ⚠️`PAUSED` 없음 — 정지도 ACTIVE로 남는다(§5d) |
| represent_car_id | int | FK → car.id (구독 대표 차량) |
| product_id | int | FK → product.id |
| started_at | datetime | 구독 시작일 |
| stopped_at | datetime | 해지 시점 (churn 판정 → §5d). STOPPED인데 NULL ~90건 |
| paused_at | datetime | **마지막으로 정지를 건 시점.** 재개해도 안 지워진다 → `NOT NULL`≠정지 중(§5d, 27.7% 과대) |
| ended_at | datetime | ⚠️해지일 아님. ACTIVE에선 현재 결제주기 종료일(=다음 갱신 예정일, 미래). 종료판정은 §5d CASE식으로 |
| deleted_yn | tinyint | 0=정상 |
| period | int | 주기 (숫자, period_unit과 조합) |
| period_unit | varchar(15) | `'week'` / `'month'` |

---

### user_service (예약-서비스 연결)
| 컬럼 | 타입 | 설명 |
|------|------|------|
| reservation_id | int | FK → reservation.id |
| user_id | int | FK → app_user.id |
| service_id | int | FK → service.id |
| deleted_yn | tinyint(1) | 0=정상 |
| postpaid_yn | tinyint(1) | 0=선불, 1=후불(레거시: 선불권 소진 시 자동생성 후불권) **⚠️ 온보딩 '후불 결제(현장수금)' 예약은 postpaid_yn=0으로 생성됨 — 후불 판별에 이 컬럼 단독 사용 금지, `reservation_onsite_collection` 참조** / 🔴 `postpaid_yn=1` 예약은 고객 앱에서 숨겨질 수 있다 → §2b-1 |
| applicable_car_id | int | 차량 FK **⚠️ 15%만 채워짐 — 차량 조인 부적합** |
| ended_at | datetime | 세차권 만료일 **⚠️ 무한대 sentinel이 여러 값으로 혼재 — 아래 참조** |
| delete_reason | varchar | 삭제 사유. 실값 `'삭제 후 새로운 세차권 부여'`(예약취소 반환 재발급) · `'후불 세차권 상계처리 (<us_id>)'`(갱신 상계, 괄호 안이 상계 대상 id) · `'ADMIN_BULK_CANCEL'` · `'B2B_CONSOLE_TICKET_REVOKE'`(zero `revokeUnusedAdminUserTickets` = 콘솔 미사용권 회수 API). **재발급분과 상계 소멸분을 가르는 유일한 단서** — 이 값 없이 `deleted_yn=1`만 보면 "취소로 반환된 권"과 "상계로 사라진 권"이 같아 보인다. 🔴 **`NULL`인 채 초 단위 간격으로 한 행씩 지워져 있으면 정식 회수 API가 아니라 운영자가 화면에서 손으로 지운 것** — 이 경로는 `entitlement_package_instance`를 `CANCELLED`로 안 바꿔서 유령 패키지가 `ACTIVE`로 남는다(2026-08-07 반얀 5회권→10회권 교체 실측: 세차권 5 + 옵션 10을 15:45:48~15:46:24에 2초 간격 개별 삭제, instance 5행은 ACTIVE 잔존) |

- ⚠️ **취소 반환 = soft-delete + 새 row 재발급** (기존 row는 `deleted_yn=1`·`reservation_id` 유지, 새 미사용 row 생성) → 상세 §5c.
- ⚠️ **고객 보유 세차권 수는 `used_yn=0`만 세면 안 된다** (미래예약 선점분 20% 누락) → 상세 §5c.

**`ended_at` = 실효 만료일이고, 값이 지저분하다 (2026-07-31 실측)**
- **게이팅 실재**: zero-api `prisma-entitlement.repository.ts`가 사용가능 세차권을 `OR: [{ended_at: null}, {ended_at: {gte: now}}]`로 거른다. 장식 컬럼이 아니므로 "보유 세차권" 쿼리엔 만료 조건을 반드시 넣을 것.
- **무한대 sentinel이 한 값이 아니다** (deleted_yn=0 기준 분포): `2999`=39,832 · `2027`=17,619 · `2026`=5,553 · `2029`=3,104 · **`2099`=1,613** · `2025`=1,305 · NULL=568 · `2028`=208 · **`9999`=93** · 2030/2083/2098 소량. `YEAR(ended_at)=2999`만 무한으로 처리하면 2099·9999가 실만료일로 오분류된다.
- **발급 경로별 만료 규칙**: 어드민 지급(`POST /v1/admin/users/{id}/tickets`)·쿠폰 발급 = **정확히 3년** (`user-service-expiration.policy.ts` `ISSUED_ENTITLEMENT_VALID_YEARS=3` + `endOfSeoulDateAfterYears` → 저장값 `YYYY-MM-DD 14:59:59` UTC = KST 23:59:59). 구독 발급분은 결제주기마다 제각각.
- ⚠️ **어드민 지급 경로가 두 개고 만료 규칙이 다르다 (2026-08-06 실측, DS-1830).**
  - **zero** `POST /v1/admin/users/{id}/tickets` → 만료 **정확히 3년**, `product_id` 채움.
  - **레거시** `POST /users-admin/{userId}/rewards` (sales-admin `사용권 지급` 드로어) → **운영자가 만료일을 직접 고른다. 화면 기본값이 `dayjs().add(1,'month')`** (`GrantRewardDrawer.tsx:38`)라 대부분 +30/31일로 찍힌다.
  - **레거시 경로 지문**: `partner_activity_log_id` NOT NULL + `product_id` NULL + `paid_amount` NULL.
    `partner_activity_log`(`action='SERVICE_ISSUED'`) 조인 → `description`에 **운영자가 쓴 메모**, `partner_id`에 지급자가 남는다. 발급 출처를 물으면 이 조인이 1순위다.
  - **세 번째 경로 = 패키지 grant**(zero `grantAdminBanyanEntitlementPackage`, 반얀 5·10회권) → 만료 **정확히 1년**(`calculateIssuedUserServiceEndedAt`, 파일 로컬 3년 상수와 다름). 지문 = `service_id=137` + `product_id` NULL + `paid_amount=0` + **`partner_activity_log_id` NULL**. 🔴 **이 경로는 `partner_activity_log`에 아무것도 안 남긴다** — 지급 주체를 물으면 `crm_note`(수금 문구)의 `partner_id`가 유일한 단서다. 반얀 현장은 **2026-08-16까지는** 공용 `오퍼레이터`(partner 41) 계정이라 개인 특정이 불가했고, 실제 영업자는 예약 쪽 `reservation_metadata`로 가야 나왔다(바로 아래 §reservation). **2026-08-17부터 개인 계정으로 전환돼 `partner_id`로 특정된다** — §partner 참조. 기간 경계를 무시하고 한쪽 규칙만 쓰면 절반이 틀린다.
  - ⚠️ **만료기간으로 발급 경로를 가르지 말 것** — "30일권"은 별도 경로가 아니라 화면 기본값이다. 코드 상수인지 UI 기본값인지 확인 없이 나누면 한 경로를 둘로 센다.
  - ⚠️ 드로어의 service 드롭다운은 `comment !== 'DEPRECATED'`만 필터한다(`:28`) → **내부용 service도 CS 지급 화면에 그대로 노출된다.**
- 🔴 **유효기간 일괄 연장 시 `ended_at < 목표일` 가드 필수** — 조건 없이 UPDATE하면 2999/2099/9999 행이 함께 걸려 **연장이 아니라 단축**이 된다.
- 만료일 변경은 SQL 직접 UPDATE 대신 어드민 API `PATCH /v1/admin/users/{userId}/tickets/SERVICE/{ticketId}`. **전필드 덮어쓰기**(`deletedYn,endedAt,paidYn,postpaidYn,reservationId,usedYn` 전부 `.strict()`)라 빠뜨린 값은 날아간다 — 현재값을 읽어 그대로 재전송할 것.

**⚠️ 무료/유료 판별에 쓰면 안 되는 컬럼 2개 (2026-07-26 실측)**
- **`paid_amount`는 2026-05부터 채워지기 시작했다.** 2026-01~04 첫 세차 `user_service`는 **전부 0**이고, 5월 565건 중 443건·6월 670건 중 233건만 0이다. 시계열로 유·무료를 가르면 4월 이전이 통째로 "무료"가 되어 완전히 틀린다.
- **`paid_yn`은 거의 항상 1이라 판별력이 없다** (첫 세차 기준 월별 99~100%). 무료 프로모션 세차도 1로 들어온다.
- **권장 판별**: 결제 조인으로 판정한다 — `user_service.payment_id` → `payment`(`deleted_yn=0`, `status IN ('PAID','PARTIAL_CANCELED')`)의 `amount > 0`이면 유료, payment 없거나 `amount=0`이면 무료. 구독/1회권 구분은 `user_service.subscription_id` NULL 여부(§5d). 첫 결제 유형으로 가르는 대안은 first-touch `payment.type`(`SUBSCRIPTION`/`VOUCHER`/`PACKAGE`).

**세차 내용(서비스명) 조회:**
```sql
LEFT JOIN (
  SELECT reservation_id, MIN(service_id) AS service_id
  FROM user_service WHERE deleted_yn = 0 GROUP BY reservation_id
) us ON us.reservation_id = r.id
LEFT JOIN service s ON s.id = us.service_id
-- s.name 예시: '외부만', '외부 + 내부', '[리터치] 외부만'
```

---

### user_option (예약-옵션 연결) (2026-08-06 실측)
옵션(내부세차 추가·왁스·살균 등)은 **`user_service`가 아니라 `user_option`**. 옵션 마스터는 `options`(복수형, `option` 아님).

- **"결제한 옵션이 그 예약에 반영됐나" 판정 = `user_option`에 `reservation_id=<예약> AND paid_yn=1 AND used_yn=1 AND deleted_yn=0` row 존재.** `user_service`에서 옵션 row를 찾으면 영영 못 찾는다.
- 🔴 **`paid_yn=1` + `used_yn=0` + `reservation_id` 있음 = 미반영 버그 상태.** 디테일러 앱 예약상세 API(`prisma-detailer-reservation.repository.ts`)가 `reservation_id=X AND used_yn=1`인 옵션만 조회하므로, 결제는 됐는데 디테일러에게는 안 보인다. (2026-08-06 예약 #83490 CS 실사례)
  - 원인: 옵션 결제 정산을 레거시 caramel-api와 zero-api가 경합한다. `cart.metadata.autoUseOptions` → `used_yn=1` 후처리는 **레거시에만** 있고, zero-api가 먼저 `payment.status='PAID'`로 바꾸면 레거시가 멱등 early-return 해서 후처리를 통째로 스킵한다. 빈도는 월 1~3건.
  - 잔존 건 탐지: `WHERE paid_yn=1 AND used_yn=0 AND reservation_id IS NOT NULL AND deleted_yn=0` + 예약 status 미완료.
- 🔴 **"고객이 지금 보유한 옵션권 장수" = `used_yn=0 AND deleted_yn=0` 둘 다 필요. `deleted_yn`을 빼면 같은 엔타이틀먼트가 2행으로 중복 집계된다 (2026-08-10 실측).** 예약 취소(`POST /v1/admin/users/{id}/reservations/bulk-cancel {ticketAction:'GIVE_BACK'}`)가 소진된 옵션권을 **되돌리는 방식이 "그 행을 미사용으로 복구"가 아니라 "소진 행을 `deleted_yn=1`로 죽이고 같은 만료일의 새 행을 발급"**이다. 실측(user 33001): 1266→1362 · 1289→1363 · 1332→1364 · 1340→1365 · 1342→1366. ⟹ ①보유 집계에 `deleted_yn=0` 필수 ②**픽스처·핸드오프 문서에 `user_option.id`를 박아두면 취소 한 번에 어긋난다**(id로 지목하지 말고 `option_id`+`ended_at`으로).
- **어느 서버가 그 row를 썼는지 판별 = `modified_at` tz 지문** (다른 테이블에도 적용 가능): `modified_at`이 `ON UPDATE CURRENT_TIMESTAMP`(서버 tz=**KST**)인 테이블에서, 레거시 caramel-api처럼 `modified_at`을 안 넘기는 writer가 쓰면 **KST 벽시계**로 찍히고, zero-api처럼 Prisma가 `modified_at: new Date()`를 명시하는 writer가 쓰면 **UTC**로 찍힌다. `created_at`(UTC)과 대조해 **+9h면 레거시, 같은 tz면 zero-api**. 로그 없이 writer를 특정할 수 있는 거의 유일한 단서.
- 🔴 **옵션 결제는 경로가 둘이고 `payment.type`이 다르다 — 한쪽만 세면 90%를 놓친다 (2026-08-10 prod 실측 정정).**
  - **zero 카트 경로 = `payment.type='OPTION'`** + `cart_id` 채움 + `metadata={point,provider}`. 2026-05-11부터 등장, prod 840건/3,216만원(2026-05~08-09).
  - **레거시 링크 경로 = `payment.type='VOUCHER'`** + `metadata.pathname='/payment/options'`·`metadata.autoUseOptions=true`. 꼬리만 남았다.
  - 월별 건수(prod PAID): 2026-04 `OPTION 0 / 레거시 56` → 05 `139/45` → 06 `295/33` → 07 `302/30` → 08(9일까지) `104/10`. ⟹ **이관이 이미 대부분 끝났다.** 예전 문서가 "옵션 결제는 `type='OPTION'`이 **아니라** `VOUCHER`"라고 적어둔 건 레거시 단독 시절 기준이라 **지금은 오답**이다.
  - **옵션 매출 정본 필터 = `type='OPTION'` OR (`type='VOUCHER'` AND `metadata.pathname='/payment/options'`).** 둘을 합쳐야 시계열이 안 끊긴다.
  - **writer 판별은 `type` + `metadata`로만 한다. `cart_id`로는 못 가른다** — 2026-08-11 실측: 두 경로 다 `cart_id`가 채워진다(레거시 40/40, zero 413/413 non-null). 레거시는 `metadata.href`가 `caramel.thetrive.com/payment/options?reservationId=N`(레거시 웹) 40/40, zero는 `href` 없음.
  - 표면도 갈려 있었다: **디테일러 앱·CS가 만든 링크 = 레거시 웹 결제 페이지**, **고객 앱 = zero 카트**(`/payment/cart/{uuid}`). 2026-08-10 승격으로 디테일러·어드민 발급도 zero로 넘어왔다. [[reference_wash_revenue_sources]]
  - 🔴 **2026-08-11부터 세 번째 조합이 생겼다 — `type='OPTION'` + `metadata.__platform__` 있음 = zero가 만든 카트를 레거시 웹이 결제한 것이고, 100% 실패한다 (2026-08-19 실측).** 디테일러 앱 발급 링크(`crmel.link`)의 `link.target_url`이 상대경로라 리졸버가 **레거시 호스트**(`caramel.thetrive.com`)에 붙여 careplus-web이 체크아웃한다. 레거시 `PurchaseButton`의 `switch(payment.type)`에 `OPTION` 케이스가 없어 클릭이 조용히 죽는다. 실측 **146건 시도 / PAID 0건**(카트 4개·고객 3명, 08-11~08-18). 같은 기간 `__platform__` 있는 `VOUCHER`는 74건 PAID → **`__platform__` 하나로 "레거시 웹이 결제했다"를 가른다.** ⟹ `type='OPTION'`을 전부 zero로 귀속하면 오답.
  - **결제 CTA가 죽었는지 탐지 = 한 `cart_id`에 `payment` row가 여러 개 쌓였는데 PAID가 0인 것.** 고객이 버튼을 반복해서 누른 흔적이다(실측 카트 69418 = **82회**). ⚠️ **`deleted_yn=0`으로 거르면 안 보인다** — 재시도마다 직전 미결제 payment를 `deleted_yn=1`로 죽이고 새로 만들어서 마지막 1행만 남는다.
  - **옵션 결제링크로 만들어진 카트 식별 = `JSON_EXTRACT(cart.metadata,'$.reservationProjectedDurationMinutes') IS NOT NULL`.** 고객 앱 자체 옵션 추가 카트와 구분되는 유일한 표식.
  - **고객이 받은 단축링크가 뭘 가리키는지 = ``SELECT target_url FROM link WHERE `key`='<crmel.link 뒤 문자열>'``.** 전체 98,722건 중 상대경로 4,796건인데, **상대경로는 레거시 웹 기준으로 해석**된다(절대 URL 93,922건은 그대로).
  - ⚠️ 잔함정: `product` 테이블엔 **`deleted_yn` 컬럼이 없다**(폐기 판정은 `sales_status`). `user_coupon` 테이블도 없다(쿠폰은 `coupon_code*`/`promotion_application`).
- 🔴 **옵션은 한 이름이 티어별 여러 `option_id`로 흩어져 있다 — id 하나로 필터하면 절반 이상 누락 (2026-08-10 실측).** `내부 세차 추가` = **74·85·86·87·88·89·90·91** 8개(건수 88=441 · 87=322 · 89=299 · 86=78 · 90=64 · 91=17 · 74=13 · 85=11). 상품 코스 `product_id BETWEEN 4037 AND 4057`과 같은 유형의 함정이다. **정본 필터 = `options.name = '내부 세차 추가'`**(티어가 늘어도 자동 포함).
  - ⚠️ **`name LIKE '%내부%'`로 넓히지 말 것** — `내부 스팀 청소`(62, 1,754건)가 섞인다. 이건 내부세차 추가가 아니라 별개 심화옵션이고, 외부만 예약엔 주당 0~3건만 붙는다.
  - 구 UI `내부까지 청소해 주세요`(68, 60건)는 2024-12~**2025-05로 종료**. 그 이전 기간을 보는 쿼리에서만 합칠 것.
  - **"외부만 예약에 내부세차를 추가했나" 판정** = 예약의 `MIN(service.service_group_id)=3`(외부만) + `user_option`에 위 옵션이 `paid_yn=1 AND used_yn=1 AND deleted_yn=0`으로 존재. ⚠️ `service_group_id`만으로 세면(=`sg=1` 비중) 그건 **"풀서비스 상품 판매 비중"**이지 옵션 추가율이 아니다.
- 🔴 **옵션권을 지급할 product를 이름으로 고르면 틀린다 (2026-08-13 실측).** `유막`으로 검색하면 product가 20개+ 나오지만 대부분 `offer_kind='CUSTOM_PAYMENT_LINK'`(레거시 결제링크 전용)이고, 이름이 거의 같은 **4076 `유막 제거`(30,000원)는 `option_id=101`**로 3950과 **다른 옵션**이다.
  - 정본 절차: ① `product WHERE type='OPTION' AND offer_kind='CATALOG'` ② `product_option`으로 `option_id`를 뽑아 **고객이 이미 보유한 `user_option.option_id`와 일치하는지 대조** ③ 그 product만 지급. (유막 제거/발수 코팅 = product **3950** → `option_id=3`)
  - 지급 = `POST /v1/admin/users/{id}/tickets {"productIds":[...],"memo":"..."}`. **같은 id를 N번 넣으면 N장 발급된다** — `createAdminUserTickets`가 `productIds.map`을 그대로 돌려 dedupe하지 않는다.
  - ⚠️ 이 경로 만료는 **3년**(`ISSUED_ENTITLEMENT_VALID_YEARS`)이라 패키지 번들로 받은 기존 옵션권(1년)과 **만료일이 갈린다** → `ticketSummaries`가 같은 이름으로 두 줄로 쪼개져 나온다. 잔량은 줄별 `remaining` 합으로 읽을 것.
  - 카탈로그 조회 API(`GET /v1/admin/users/{id}/entitlement-grant-catalog`)는 prod 404(미배포) — 위 SQL 절차로 우회.

---

### onboarding_reservation_intent (온보딩 v3 퍼널 상태, 2026-07-11 prod~)
온보딩 v3(랜딩→주소→차량→상품→시간→방문정보→예약확인)의 **진행 상태 1행/시도**. 앰플리튜드 없이 DB로 퍼널 단계·이탈·결제방식을 보는 유일한 소스.

- `status`: `ACTIVE`(진행중, 이탈 포함) / `COMPLETED`(예약 생성됨, `reservation_id`·`completed_at` 채워짐) / `ABANDONED`. 실측(2026-09-03, 60일) ACTIVE 6,766 · COMPLETED 568 · ABANDONED 108 — ABANDONED는 소수라 **이탈 모수는 `status<>'COMPLETED'`** 로 잡는다.
- 🔴 **`user_id`는 인증(휴대폰 로그인) 전엔 NULL** — 비로그인으로 시작해 `client_key`가 소유한다(ACTIVE 6,766 중 user_id 있는 건 1,312). 인증은 방문정보 뒤에 오므로 "유저별 퍼널"은 인증 이후 단계만 보인다. 시작 시각 `created_at`, 인증 시각 `user_attached_at`.
- `last_step_key` 순서: `ADDRESS_RESULT` → `VEHICLE_MODEL_SELECTED` → `VEHICLE_PLATE_COMPLETED` → `PRODUCT_SELECTED` → `SLOT_SELECTED` → `VISIT_INFO_CONTACT_COMPLETED`(=예약확인·결제 직전). 이탈 단계 분포는 이 컬럼을 GROUP BY.
- 결제 방식 = `JSON_UNQUOTE(JSON_EXTRACT(metadata,'$.completionPaymentMode'))` → `PREPAID` / `POSTPAID_ONSITE_COLLECTION`. ⚠️ 7월 말 이전 완료 건은 키 자체가 없어 NULL(60일 568건 중 95건). 후불 여부 정본은 아래 `reservation_onsite_collection`.
- `pricing_snapshot.coupon`(`promotionId`·`promotionApplicationId`) = 결제에 **자동 적용된 할인쿠폰**(대부분 웰컴 20% = promotion 55, 선결제의 83%). 온보딩은 `promotion`(할인)만 적용하고 **무료 세차권(`user_service`)은 소비하지 않는다** — "세차권 있는데 결제한 사람"은 `user_service`(`used_yn=0`·`deleted_yn=0`·`reservation_id IS NULL`·`created_at < i.completed_at`)를 따로 조인해서 본다.
- JSON 컬럼(`address_candidate`·`vehicle_candidate`·`product_selection`·`slot_selection`·`visit_info`·`metadata`)은 `JSON_KEYS()`로 키를 먼저 확인. `metadata.entrySource`(`MY_GARAGE_FIRST_WASH`/`CALENDAR_FIRST_WASH`)가 있으면 랜딩이 아니라 기존 고객의 첫세차 진입.

### reservation_onsite_collection (온보딩 후불 결제/현장 수금, 2026-07-11 prod~)
온보딩 v3의 **'후불 결제' 예약 canonical marker**. 예약 시 결제 없이 세차 현장에서 수금.

- **후불 예약 판별 = 이 테이블에 row 존재 + `status <> 'CANCELED'`.** (⚠️ `user_service.postpaid_yn` 아님 — 온보딩 후불의 user_service는 `postpaid_yn=0`으로 생성됨. postpaid_yn=1은 레거시 구독 후불권.)
- 컬럼: `reservation_id`(UNIQUE FK→reservation), `user_id`(FK→app_user), `status`(PENDING=수금대기/REQUESTED/CONFIRMED/CANCELED, NOT NULL DEFAULT PENDING), `collection_method`(CARD_TERMINAL/BANK_TRANSFER/PAYMENT_LINK), `requested_at`. created_at은 UTC 저장.
- 🔴 **`payment_link_amount` 컬럼을 수금액으로 쓰지 말 것 — 실사용 전량 NULL**(2026-08-11 실측: 온보딩 첫예약 후불 71건 전건 NULL). 이름 때문에 금액 컬럼으로 보이지만 PAYMENT_LINK 방식에만 쓰이고 현재 수금은 BANK_TRANSFER/CARD_TERMINAL뿐이다. 스키마를 직접 보고 들어오면 반드시 걸리는 함정 — 금액은 아래 item 합 공식이 유일한 정본.
- 예약 취소 시 status→CANCELED로 함께 전이됨.
- **수금액 계산**: `reservation_onsite_collection_item.amount_snapshot` 합(`canceled_at IS NULL`만) + `reservation_onsite_collection_item_adjustment.amount` 합(할인=음수, 쿠폰 등).
  - 🔴 **`user_service.product_id → product.price`로 환산하지 말 것 — adjustment(쿠폰·할인)를 통째로 놓쳐 과대집계된다.** 실측(2026-08-11, V3 첫예약 후불 40건): product.price 합 2,933,000원 vs 정본 공식 2,661,600원 = **+10.2% 과대**. product_id는 "무엇을 샀나"(구매 건수 집계)엔 정본이지만 "얼마 받나"엔 아니다.
- 후불 선택 가능 조건: 타겟 차량(`car_model_target.is_target=1`)만.

```sql
-- 신규 예약 선불/후불 분류 (테스터·취소 제외)
SELECT CASE WHEN roc.id IS NOT NULL THEN '후불' ELSE '선불' END AS pay_type, COUNT(*)
FROM reservation r
JOIN app_user au ON au.id=r.user_id AND au.test_yn=0 AND au.deleted_yn=0
LEFT JOIN reservation_onsite_collection roc
  ON roc.reservation_id=r.id AND roc.status<>'CANCELED'
WHERE r.created_at >= %s AND r.created_at < %s  -- UTC 경계
  AND r.deleted_yn=0 AND r.status<>'CANCELED'
GROUP BY 1;

-- 후불 예약별 수금액
SELECT roc.reservation_id, roc.status,
 (SELECT COALESCE(SUM(i.amount_snapshot),0) FROM reservation_onsite_collection_item i
   WHERE i.collection_id=roc.id AND i.canceled_at IS NULL)
 + (SELECT COALESCE(SUM(a.amount),0) FROM reservation_onsite_collection_item_adjustment a
   JOIN reservation_onsite_collection_item i2 ON a.collection_item_id=i2.id
   WHERE i2.collection_id=roc.id AND i2.canceled_at IS NULL) AS amount_to_collect
FROM reservation_onsite_collection roc WHERE roc.status<>'CANCELED';
```
- ⚠️ **수금완료 전이가 없다 — "수금 완료" 필터를 걸면 전부 0건.** `status <> 'CANCELED'`로만 거를 것.
  - 2026-07-27 실측: 49건이 PENDING/CANCELED 2값뿐, WASHED 예약도 PENDING.
  - **2026-08-11 갱신: `REQUESTED`가 등장했다**(V3 첫예약 71건 = REQUESTED 27 / CANCELED 31 / PENDING 13). `CONFIRMED`·완료류는 여전히 0건이라 결론은 동일. **`collection_method`는 REQUESTED일 때만 채워진다**(BANK_TRANSFER 17 / CARD_TERMINAL 10) — PENDING·CANCELED는 NULL이므로 method로 후불을 세면 40%가 빠진다.
  - ⚠️ **후불 취소율이 높다**: 온보딩 첫예약 기준 31/71 = **43.7%**(같은 기간 전체 첫예약 취소율 82/473 = 17.3%의 2.5배). 후불 예약 건수를 매출 기대치로 환산할 때 취소분을 빼지 않으면 크게 과대해진다.
- **온보딩 상품 구매를 선불·후불 통틀어 세려면 `user_service.product_id`를 쓴다.** 후불도 온보딩 시점에 user_service가 product_id와 함께 생성되고 `payment_id`만 NULL이라, 결제 유무와 무관하게 한 소스로 잡힌다. `payment`/`cart`로 조회하면 후불 절반이 통째로 빠지고, `reservation_onsite_collection_item`으로 조회하면 취소분이 남는다(user_service는 `deleted_yn=0`으로 자동 정리됨).
  ```sql
  -- 온보딩 코스(라이트/베이직/장마 대비 풀코스) 일별 구매 고객수, 선불+후불 통합
  SELECT DATE(DATE_ADD(us.created_at, INTERVAL 9 HOUR)) d, pr.name, COUNT(DISTINCT us.user_id)
  FROM user_service us JOIN product pr ON pr.id = us.product_id
  WHERE us.deleted_yn = 0 AND pr.type = 'VOUCHER'
    AND (pr.name LIKE '라이트%' OR pr.name LIKE '베이직%' OR pr.name LIKE '장마%')
  GROUP BY d, pr.name;
  ```
  ⚠️ 코스 상품은 **tier별로 product row가 7개씩** 따로 있다(2026-07-15 생성, id 4037~4057). `product_id IN (…)`로 특정 코스를 집으려 하지 말고 **이름으로 묶을 것**.

---

### service
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| name | varchar(25) | 서비스명 (`'외부만'`, `'외부 + 내부'` 등) — **이름만으로는 티어를 알 수 없다**(같은 이름이 T1~T7로 7행) |
| tier_id | int | FK → `car_tier.id` → `car_tier.tier`(1~7). **"이 세차권이 몇 티어 차량용인가"** — 차량 자체의 티어는 `car_model.tier_id`로 별개 경로. 티어 무관 세차권은 NULL |
| deleted_yn | tinyint(1) | 0=정상 |
| price | int | 기본가 (같은 이름·다른 티어 예: 외부+내부 T4 69,000 / T5 75,000) |
| wash_type | varchar(15) | 세차 유형 (`OUTSIDE`/`BOTH`) |

🔴 **세차권 티어 ≠ 예약 차량 티어일 수 있고, 그 차액은 포인트로 적립된다 (2026-08-14 실측).** 다차 고객이 T5 구독 세차권을 T4 차량에 쓰면 `payment`에 `type=NULL` · `amount=0` · `name='세차권 티어 차액 포인트 적립'` row가 생기고 `user_point`에 차액이 적립된다(T5 75,000 − T4 69,000 = 6,000P). 반대 방향(`외부만 4티어 → 5티어`)도 실존.
- ⟹ 예약의 `service_id`가 구독 상품 티어와 다르다고 **오지급·오사용으로 판정하지 말 것** — 정상 전환이다. `payment.name`에 `'N티어 → M티어'`가 찍힌 amount=0 row가 그 지문.
- ⟹ **차량이 2대 이상이고 티어가 갈리는 고객은 매 세차마다 이 row가 생긴다.** 티어 단일 구독으로 다차를 운용하는 구조적 마찰이라, 이 row 빈도가 다차 고객 탐지 신호로도 쓸 수 있다.

---

### payment
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | int | PK |
| user_id | int | FK → app_user.id |
| type | varchar(25) | `VOUCHER`=1회권, `SUBSCRIPTION`=구독, `OPTION`=옵션, `PACKAGE`=패키지 **⚠️ PACKAGE엔 제휴 쿠폰 사용분이 섞인다 — 회권 판매 집계 전 §6c의 `COUPON_PACKAGE_REDEEM` 경고 확인** / ⚠️ NULL 존재(`IFNULL(type,'')` 비교) / ⚠️ 앱 '세차 옵션 추가' 결제는 `OPTION`이 아니라 `VOUCHER`로 저장됨 |
| status | varchar(25) | 집계 대상: `IN ('PAID','PARTIAL_CANCELED')` |
| amount | int | 결제금액 **⚠️ 구독/횟수권 고객 75%는 payment 없음** / 🔴 **포인트 상계 후 "실제 카드 청구액"이다 — 상품 정가 아님(아래 참조)** |
| cancel_amount | int | 취소금액 (PARTIAL_CANCELED 시 `amount-cancel_amount`=실매출) |
| paid_at | datetime | 결제 완료일 |
| name | varchar(250) | 상품명 (구독은 플랜명 포함, `'외 N개'` suffix 주의) |
| deleted_yn | tinyint(1) | NULL 가능 — `IS NOT TRUE` 패턴 사용 |

🔴 **`amount`는 포인트 상계 후 금액이다 — 정액 구독인데 금액이 매달 다르면 가격 변경이 아니라 포인트 차감이다 (2026-08-14 실측).**
- 실측: 234,000원 정액 링크 구독이 `234,000 / 225,000 / 216,000 / 234,000 / 219,000 / 234,000`으로 찍혔다. 차액 9,000·18,000·15,000은 전부 그 달에 태운 포인트였다.
- **대조 경로 = `user_point`**(`user_id`, `point`, `left_point`, `updated_at`, `expired_at`). 결제일과 **같은 날 `left_point=0`으로 바뀐 행들의 `point` 합** = 그 결제에서 소진된 포인트. 위 3건 모두 정확히 일치.
- ⚠️ `user_point_history`에는 `type` 컬럼이 **없다** — 적립/소진 구분으로 조회하려다 막힌다. 소진 시점 판정은 `user_point.updated_at`.
- ⚠️ 적립 원천이 섞인다: 리뷰 리워드 3,000원(만료 있음) · **세차권 티어 차액 6,000원**(만료 NULL, §service 참조). 만료 유무로 원천을 가를 수 있다.
- ✅ **어드민 API는 이미 갈라서 준다** — `GET /v1/admin/users/{userId}` → `payments[]`의 `cashPaidAmount` · `pointPaidAmount` · `refundableCashAmount` · `refundablePointAmount`. **현금/포인트 분해가 필요하면 SQL보다 이 EP가 정답.**
- 🔴 **구독 해지 환불액은 비례배분이 아니라 "결제액 − 사용분의 1회권 정가"다 (2026-08-14 실측).** `47,400원(월 2회) 중 1회 사용 → 환불 23,700원`으로 계산하면 틀린다. 실제 판정식(zero `PrismaSubscriptionRepository.planOwnedSubscriptionCancel`) = **`max(payment.amount, SUM(payment_medium.amount)) − cancel_amount − 사용세차권_1회권_정가`**. 실사례(user 57550): 결제 47,400 − 외부만 T5 1회권 정가 41,000 = **6,400원**이고 `payment.cancel_amount`와 정확히 일치.
  - ⟹ **1회권 정가를 모르면 환불액 검증이 불가**하다. 정가 경로 = `car_model.tier_id → car_tier.tier` → `product`(`type='VOUCHER'` + `product_type='TIER_n'` + 같은 서비스명). 티어를 안 맞추면 다른 금액이 나와 "환불 부족"으로 오진한다.
  - gross basis(`max(amount, medium합)`)는 **포인트 사용 결제에서 필수** — `payment.amount`만 쓰면 포인트가 이중 차감된다(DEV-1240). `payment_medium`이 없는 legacy row만 `payment.amount` 단독.
  - 해지 시 소멸한 잔여권 지문 = `user_service.delete_reason='subscription cancel refund'`.

---

### card_payment (PG 결제 시도 원장) (2026-08-14 실측)
`payment` 1행에 대응하는 **실제 PG 시도 기록**. 결제 진단의 정본.

| 컬럼 | 설명 |
|------|------|
| payment_id | FK → `payment.id` |
| subscription_id | 구독 갱신 결제면 채워짐 (1회권은 NULL) |
| amount / status | 청구액 / **소문자** `'paid'` (⚠️ `payment.status`는 대문자 `'PAID'` — 섞어 쓰면 0행) |
| fail_reason | 실패 사유. **성공 건은 NULL** |
| imp_customer_uid | 빌링키(`billing-key-…`) — 자동갱신 카드 식별 |
| pg_provider / imp_pg_id | 실측 `kpn` / `porthetrive3` (firstpay), `kakaopay` / `CAZQNEKBIF` (간편결제, `pay_method='easy_pay'`) |
| receipt_url | firstpay 영수증. `mxissuedate`에 **KST 청구시각**이 박혀 있어 tz 교차검증에 쓸 수 있다 |

- 🔴 **"청구 시도조차 안 했다"의 유일한 증거 = 이 테이블에 row가 없는 것.** `payment`엔 실패 row가 남지 않는 경로가 있어, `payment` 부재만으로는 미시도/실패를 못 가른다 → §5d 결제 공백 진단.
- ⚠️ `started_at`·`paid_at`이 자동갱신 건에선 전부 NULL이다. 청구 시각은 `created_at`(UTC)으로 볼 것.
- 🔴 **`card_payment_cancel_log`가 비어 있다고 "환불 안 됐다"로 읽지 말 것 (2026-08-14 실측).** 이 테이블은 **레거시(caramel-api) 전용**이고 zero-api 환불 경로는 여기 아무것도 안 남긴다(zero 코드베이스 참조 0건). 실사례: payment 81770이 `PARTIAL_CANCELED`·`cancel_amount=6,400`인데 cancel_log는 0행.
  - **환불 실행 여부 정본 = `payment.status`(`PARTIAL_CANCELED`/`CANCELED`) + `cancel_amount`.** `refund-orchestrator.service.ts`가 **PG 취소 성공 뒤에만** `applyRefundBatch`를 부르고 실패하면 예외로 중단하므로, `cancel_amount`가 올라가 있으면 PG 취소는 나간 것이다.
  - ⚠️ `card_payment.cancel_amount`는 환불이 나가도 **0인 채로 남는다**(같은 실사례). 카드 원장이 아니라 `payment`를 볼 것.
  - 환불 시각 = `payment.modified_at`(**KST 저장** — `created_at`은 UTC라 같은 행에서 tz가 다르다).
- 🔴 **키인(어드민 대리청구) 결제는 PortOne을 안 거친다 — KPN 직연동이다 (2026-09-03 실측).** 식별 = `payment.type='ADMIN_KEYIN'`(또는 `payment.external_request_id LIKE 'admin-keyin%'`). 카드 원장에선 **`pg_provider='KPN'` + `imp_pg_id IS NULL`**이 지문이다 — PortOne 경유 건은 채널 id(`porthetrive`·`porthetrive2`·`porthetrive3`·`im_thethrive`·`IMPTcarepl02`)가 반드시 채워진다.
  - ⚠️ **`imp_uid IS NULL`로 "PortOne 미경유"를 판정하면 틀린다.** V2 경로도 전부 NULL이다(2026-06 이후 약 4,900행 중 imp_uid가 채워진 건 `tosspayments`/`im_thethrive` 21건뿐).
  - KPN 거래번호(tid)는 **`card_payment.pg_id`**에 들어간다(`thetrive2___…`). `card_payment`에 `tid` 컬럼은 없다. 승인 식별자 `mxIssueNo`·`mxIssueDate`는 `payment.metadata`.
  - 이 MID 거래는 포트원 대시보드·정산에 안 나온다. 집계 경로는 우리 DB 아니면 KPN 가맹점 관리페이지(`pm.firstpay.co.kr`).
  - 2026-09 이후 정비 건은 `payment.name`이 `[카라멜] `로 시작한다(세차는 접두어 없음) — 재무 집계용 구분.

---

### log (레거시 변경 이력) 🔴 5개 컬럼만 기록 + 구독분은 2026-06에 죽었다 (2026-08-14 실측)
이름이 범용이라 "전체 감사 로그"로 착각하기 쉽다. **아니다.**

| 컬럼 | 설명 |
|------|------|
| table_name / column_name / record_id | 변경 대상. 조합이 **아래 5개가 전부** |
| old_value / new_value | 변경 전후 값 (문자열) |
| type | `USER` 85,059 / `SYSTEM` 698 — **고객 본인 액션인지 시스템인지 갈림** |
| created_by | 행위자 `app_user.id` |
| created_at | UTC |

| table.column | 건수 | 마지막 기록 | 상태 |
|---|---|---|---|
| `user_service.used_yn` | 47,915 | 2026-08-13 | ✅ 살아있음 |
| `user_option.used_yn` | 20,007 | 2026-08-13 | ✅ 살아있음 |
| `subscription.status` | 11,474 | 2026-07-23 | 🔴 사실상 사망 |
| `subscription.ended_at` | 5,797 | 2026-08-11 | 🔴 사실상 사망 |
| `promotion_application.payment_id` | 562 | 2026-03-19 | 🔴 사망 |

🔴 **`log`에 없다 ≠ 그 변경이 없었다.** 구독 기록은 쓰기 경로가 zero-api로 넘어가며 2026-06에 끊겼다 — 월별 `subscription.ended_at`은 2026-04 **519** → 05 **191** → 06 **13** → 07 **4**, `subscription.status`는 04 **931** → 05 **355** → 06 **0**.
- 커버율 실측(2026-07-20~08-13): 실제 일시정지 **114건+** vs `log` 기록 **11건 = ≤9.6%**. ⟹ **2026-06 이후 구독 상태·결제일 변경 이력은 DB에 사실상 없다.** 그 구간은 `paused_at`·`ended_at` **현재값에서 역산**하는 수밖에 없다.
- ✅ 지금도 쓸 수 있는 것 = **`user_service.used_yn 0→1`이 "그 세차권을 언제·누가 썼나"의 정본**(`created_by`로 행위자까지). `user_option`도 동일.
- ⚠️ 같은 사람의 서로 다른 계정이 `created_by`로 섞일 수 있다(한 번호에 계정 복수 → §app_user). 행위자를 사람 단위로 볼 땐 전화번호로 묶을 것(§4b-13).

---

## 8. 박제 쿼리 (그대로 실행 — 탐색·DESCRIBE 금지)

> 아래는 **고정 형태로 반복되는 질문**이다. 질문이 트리거와 맞으면 **스키마 탐색·DESCRIBE 없이 아래 SQL을 그대로 한 번에 실행**하고, 날짜 등 변수만 치환하라. 여러 턴에 걸쳐 탐색하지 마라 — 이미 검증된 쿼리다.

### 8a. 정비 타겟 일일 리스트 (수입차·연식 5년↑ 당일 예약 고객)

**트리거**: "N월 N일 예약 고객 중 … 국산차 제외 / 수입차 / 연식 5년 이상 … 차량명·차량번호·주행거리·세차 시작시간·담당 디테일러·예약 주소지·세차 내용·세차 횟수" 형태의 **일일 리스트** 요청 (정비 담당자가 매일 날짜만 바꿔 요청).

**규칙**:
- `<DATE>`(2곳)를 요청 날짜(KST, `YYYY-MM-DD`)로만 치환해 그대로 실행.
- 연식 5년 이상 = `model_year <= 조회연도 - 5` (쿼리가 `<DATE>`에서 자동 계산).
- 연식(`model_year`) NULL 차량은 결과 맨 뒤 별도 그룹(`분류='연식미상'`)으로 나옴 → "Null 따로 분류" 충족.
- 세차 횟수 = 그 고객의 누적 세차완료(`WASHED`/`REPORT_SENT`) 횟수.
- 취소(`CANCELED`)·테스터(`app_user.test_yn=1`)·임시차(`car.temp_yn=1`) 제외 포함됨. 정렬=연식 오래된 순.

```sql
SELECT
  CASE WHEN c.model_year IS NULL THEN '연식미상' ELSE '연식확인' END AS 분류,
  cm.name AS 차량명, c.plate_number AS 차량번호, c.mileage AS 주행거리,
  DATE_FORMAT(CONVERT_TZ(r.reservation_datetime,'+00:00','+09:00'),'%Y-%m-%d %H:%i') AS 세차시작,
  d.name AS 담당디테일러, r.location AS 예약주소지, s.name AS 세차내용,
  (SELECT COUNT(*) FROM reservation r2
     WHERE r2.user_id=r.user_id AND r2.status IN ('WASHED','REPORT_SENT') AND r2.deleted_yn=0) AS 세차횟수,
  c.model_year AS 연식, cb.name AS 브랜드
FROM reservation r
JOIN (SELECT reservation_id, MAX(car_id) car_id FROM reservation_car GROUP BY reservation_id) rc ON rc.reservation_id=r.id
JOIN car c ON c.id=rc.car_id AND c.deleted_yn=0 AND c.temp_yn=0
JOIN car_brand cb ON cb.id=c.brand_id
LEFT JOIN car_model cm ON cm.id=c.model_id
LEFT JOIN detailer d ON d.id=r.detailer_id
LEFT JOIN user_service us ON us.reservation_id=r.id AND us.deleted_yn=0
LEFT JOIN service s ON s.id=us.service_id
JOIN app_user au ON au.id=r.user_id AND au.deleted_yn=0 AND au.test_yn=0
WHERE DATE(CONVERT_TZ(r.reservation_datetime,'+00:00','+09:00'))='<DATE>'
  AND r.deleted_yn=0 AND r.status IN ('CONFIRMED','WASHED','REPORT_SENT')
  AND cb.name NOT IN ('현대','기아','제네시스','KGM','KGM(쌍용)','르노','쉐보레','캠시스','GM','르노삼성','대우','삼성')
  AND (c.model_year <= YEAR('<DATE>') - 5 OR c.model_year IS NULL)
ORDER BY (c.model_year IS NULL), c.model_year ASC;
```
- 행이 5개 이상이면 자동으로 스프레드시트로 내보내진다(정상). 검증 기준: 2026-06-26 → 41건(연식확인 32 + 연식미상 9).
