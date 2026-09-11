-- 차량등록→예약 전환율 (14일 내) ×타겟 (12개월 rolling, 월별)
-- Grafana timeseries 패널용 (ap4j74 #214)
-- 분모 = 타겟 고객의 첫 차량등록 월 코호트 / 분자 = 등록 후 14일 내 첫 예약 신청
-- ⚠️ 성숙게이트는 월(버킷) 단위 — 월말 +14일이 지난 월만 표시(부분 코호트 금지)
WITH
live_users AS (SELECT id FROM app_user WHERE deleted_yn=0 AND test_yn=0 AND temp_yn=0 AND (phone NOT IN ('01020866510','01035474964','01093277016','01091350157','01043446885','01049664316','01050373300','01066943645','01073740979','01092828753','01035420850','01051415705','01091622508','01000000000') OR phone IS NULL)),
target_users AS (SELECT DISTINCT c.user_id FROM car c JOIN car_model_target cmt ON cmt.id=c.model_id WHERE c.deleted_yn=0 AND cmt.is_target=1),
user_first_car AS (SELECT c.user_id, MIN(c.created_at) AS fcar_utc, MIN(CONVERT_TZ(c.created_at,'UTC','+09:00')) AS first_car_kst FROM car c JOIN live_users lu ON lu.id=c.user_id WHERE c.deleted_yn=0 GROUP BY c.user_id),
target_car_users AS (SELECT ufc.user_id, ufc.fcar_utc, CAST(DATE_FORMAT(ufc.first_car_kst, '%Y-%m-01') AS DATE) AS bkt FROM user_first_car ufc JOIN target_users tu ON tu.user_id=ufc.user_id WHERE ufc.first_car_kst >= DATE_SUB(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR), INTERVAL 12 MONTH)),
first_res AS (SELECT r.user_id, MIN(r.created_at) AS fres FROM reservation r JOIN live_users lu ON lu.id=r.user_id WHERE r.deleted_yn=0 AND r.status IN ('CONFIRMED','REPORT_SENT','WASHED','IN_PROGRESS','NO_SHOW') GROUP BY r.user_id)
SELECT t.bkt AS time, ROUND(SUM(fr.fres IS NOT NULL AND DATEDIFF(fr.fres, t.fcar_utc) <= 14) / COUNT(*) * 100, 1) AS target_car_to_res_rate
FROM target_car_users t LEFT JOIN first_res fr ON fr.user_id=t.user_id
WHERE LAST_DAY(t.bkt) + INTERVAL 14 DAY <= DATE(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR))
GROUP BY t.bkt ORDER BY t.bkt
