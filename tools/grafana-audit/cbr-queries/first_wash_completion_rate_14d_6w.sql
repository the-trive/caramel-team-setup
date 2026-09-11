-- 첫세차 완료율 (14일 내) ×타겟 (6주 rolling, 주차별)
-- Grafana barchart 패널용 (ap4j74 #309)
-- 분모 = 타겟 고객의 생애 첫 예약 신청 주 코호트 / 분자 = 신청 후 14일 내 첫 세차 완료
-- ⚠️ 성숙게이트는 주(버킷) 단위 — 그 주 전체가 14일 성숙한 주만 표시(부분 코호트 금지)
SELECT * FROM (
SELECT * FROM (
WITH
live_users AS (SELECT id FROM app_user WHERE deleted_yn=0 AND test_yn=0 AND temp_yn=0 AND (phone NOT IN ('01020866510','01035474964','01093277016','01091350157','01043446885','01049664316','01050373300','01066943645','01073740979','01092828753','01035420850','01051415705','01091622508','01000000000') OR phone IS NULL)),
target_users AS (SELECT DISTINCT c.user_id FROM car c JOIN car_model_target cmt ON cmt.id=c.model_id WHERE c.deleted_yn=0 AND cmt.is_target=1),
first_req AS (SELECT r.user_id, MIN(r.created_at) AS freq FROM reservation r JOIN live_users lu ON lu.id=r.user_id JOIN target_users tu ON tu.user_id=r.user_id WHERE r.status IN ('CONFIRMED','REPORT_SENT','WASHED','IN_PROGRESS','NO_SHOW') AND r.deleted_yn=0 GROUP BY r.user_id),
first_wash AS (SELECT r.user_id, MIN(COALESCE(r.washed_at, r.reservation_datetime)) AS fw FROM reservation r JOIN target_users tu ON tu.user_id=r.user_id WHERE r.status IN ('WASHED','REPORT_SENT') AND r.deleted_yn=0 GROUP BY r.user_id),
cohort AS (SELECT fr.user_id, fr.freq, DATE_SUB(DATE(DATE_ADD(fr.freq, INTERVAL 9 HOUR)), INTERVAL WEEKDAY(DATE_ADD(fr.freq, INTERVAL 9 HOUR)) DAY) AS time FROM first_req fr WHERE DATE_ADD(fr.freq, INTERVAL 9 HOUR) >= DATE_SUB(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR), INTERVAL 16 WEEK))
SELECT c.time, ROUND(SUM(CASE WHEN fw.fw IS NOT NULL AND DATEDIFF(fw.fw, c.freq) <= 14 THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) AS value
FROM cohort c LEFT JOIN first_wash fw ON fw.user_id=c.user_id
WHERE DATE_ADD(c.time, INTERVAL 6 DAY) + INTERVAL 14 DAY <= DATE(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR))
GROUP BY c.time
) w WHERE w.time < DATE_SUB(DATE(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR)), INTERVAL WEEKDAY(DATE(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 9 HOUR))) DAY) ORDER BY w.time DESC LIMIT 6
) cbr_trim6 ORDER BY cbr_trim6.`time`
