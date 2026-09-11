-- 타겟 외부만 세차 중 내부세차 추가율 (12개월 rolling, 월별)
-- Grafana timeseries 패널용 (ap4j74 #340)
-- 분모 = 타겟 고객의 완료 세차 중 '외부만'(service_group_id=3) 상품으로 예약된 건
-- 분자 = 그 중 '내부 세차 추가' 옵션이 결제·반영(user_option paid=1/used=1)된 건
WITH live_users AS (SELECT id FROM app_user WHERE deleted_yn=0 AND test_yn=0 AND temp_yn=0 AND (phone NOT IN ('01020866510','01035474964','01093277016','01091350157','01043446885','01049664316','01050373300','01066943645','01073740979','01092828753','01035420850','01051415705','01091622508','01000000000') OR phone IS NULL)),
target_users AS (SELECT DISTINCT c.user_id FROM car c JOIN car_model_target cmt ON cmt.id=c.model_id WHERE c.deleted_yn=0 AND cmt.is_target=1),
ext_only AS (
  SELECT r.id AS rid, r.reservation_datetime AS rd
  FROM reservation r
  JOIN live_users lu ON lu.id=r.user_id
  JOIN target_users tu ON tu.user_id=r.user_id
  JOIN user_service us ON us.reservation_id=r.id AND us.deleted_yn=0 AND us.paid_yn=1 AND us.used_yn=1
  JOIN service s ON s.id=us.service_id
  WHERE r.status IN ('WASHED','REPORT_SENT') AND r.deleted_yn=0
  GROUP BY r.id, r.reservation_datetime
  HAVING MIN(s.service_group_id)=3
),
int_add AS (
  SELECT DISTINCT uo.reservation_id
  FROM user_option uo JOIN options o ON o.id=uo.option_id
  WHERE uo.deleted_yn=0 AND uo.paid_yn=1 AND uo.used_yn=1 AND o.name='내부 세차 추가'
)
SELECT CAST(DATE_FORMAT(DATE_ADD(e.rd, INTERVAL 9 HOUR), '%Y-%m-01') AS DATE) AS time,
  ROUND(SUM(ia.reservation_id IS NOT NULL) / NULLIF(COUNT(*),0) * 100, 1) AS interior_add_rate
FROM ext_only e
LEFT JOIN int_add ia ON ia.reservation_id = e.rid
WHERE DATE_ADD(e.rd, INTERVAL 9 HOUR) >= DATE_SUB(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR), INTERVAL 12 MONTH)
GROUP BY time ORDER BY time
