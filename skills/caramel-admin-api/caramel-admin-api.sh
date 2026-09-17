#!/usr/bin/env bash
# caramel-admin-api.sh — 카라멜 어드민 REST API 헬퍼
# 사용법: caramel-admin-api.sh <METHOD> <path> [json_body]
# 예시:
#   caramel-admin-api.sh GET /v1/admin/users/225046
#   caramel-admin-api.sh POST /v1/admin/users/225046/points '{"point":1000,"reason":"테스트"}'

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDENTIALS_FILE="$SCRIPT_DIR/.credentials"
TOKEN_FILE="$SCRIPT_DIR/.token"

# ── 크레덴셜 로드 ──────────────────────────────────────────────────
if [[ ! -f "$CREDENTIALS_FILE" ]]; then
  echo "ERROR: credentials 파일이 없습니다: $CREDENTIALS_FILE" >&2
  echo "파일을 생성하고 CARAMEL_ADMIN_BASE, CARAMEL_ADMIN_USER, CARAMEL_ADMIN_PASS를 설정하세요." >&2
  exit 1
fi
# 환경변수로 넘긴 base를 source가 덮어쓰면 dev 호출이 조용히 prod로 간다(실제 발생).
BASE_FROM_ENV="${CARAMEL_ADMIN_BASE:-}"
# shellcheck source=/dev/null
source "$CREDENTIALS_FILE"
if [[ -n "$BASE_FROM_ENV" ]]; then
  CARAMEL_ADMIN_BASE="$BASE_FROM_ENV"
  # 토큰 캐시는 환경별로 분리한다. 안 하면 dev 호출에 prod 토큰이 붙는다.
  TOKEN_FILE="$TOKEN_FILE.$(echo "$CARAMEL_ADMIN_BASE" | tr -c '[:alnum:]' '-')"
fi

if [[ -z "${CARAMEL_ADMIN_BASE:-}" || -z "${CARAMEL_ADMIN_USER:-}" || -z "${CARAMEL_ADMIN_PASS:-}" ]]; then
  echo "ERROR: .credentials에 CARAMEL_ADMIN_BASE / CARAMEL_ADMIN_USER / CARAMEL_ADMIN_PASS가 필요합니다." >&2
  exit 1
fi

# ── 인자 파싱 ──────────────────────────────────────────────────────
if [[ $# -lt 2 ]]; then
  echo "사용법: $(basename "$0") <METHOD> <path> [json_body]" >&2
  exit 1
fi

METHOD=$(echo "$1" | tr '[:lower:]' '[:upper:]')
API_PATH="$2"
BODY="${3:-}"

# ── 3층 게이트 ────────────────────────────────────────────────────
# read      조회
# reversible 되돌릴 수 있는 고객 데이터(세차권·포인트·주소·차량·예약 취소)
# outbound   실제 슬롯을 점유하거나 고객·돈이 바깥으로 나가는 것
TEST_ACCOUNTS_FILE="$SCRIPT_DIR/.test-accounts"
AUDIT_LOG="$SCRIPT_DIR/.audit.log"

_tier() {
  if [[ "$METHOD" == "GET" || "$METHOD" == "HEAD" ]]; then echo read; return; fi
  case "$API_PATH" in
    */reservations|*/concierge-reservation|*/reservation-create-candidates/confirm) echo outbound; return ;;
    */payments/*/refund) echo outbound; return ;;
    */detailer-swap*|*/datetime-change*|*/address-change) echo outbound; return ;;
  esac
  # PATCH /v1/admin/users/{u}/reservations/{r} = 재배정·시각 변경
  if [[ "$METHOD" == "PATCH" && "$API_PATH" =~ /reservations/[0-9]+$ ]]; then echo outbound; return; fi
  echo reversible
}

_target_user_id() {
  [[ "$API_PATH" =~ /users/([0-9]+) ]] && echo "${BASH_REMATCH[1]}"
}

TIER=$(_tier)
TARGET_USER=$(_target_user_id || true)

if [[ "$TIER" != "read" ]]; then
  echo "[$TIER] $METHOD $API_PATH" >&2

  if [[ "${CARAMEL_ADMIN_WRITE_APPROVED:-}" != "1" ]]; then
    echo "ERROR: 쓰기 호출은 CARAMEL_ADMIN_WRITE_APPROVED=1 이 붙어야 실행됩니다." >&2
    echo "  CARAMEL_ADMIN_WRITE_APPROVED=1 $(basename "$0") $METHOD $API_PATH '<body>'" >&2
    exit 1
  fi

  if [[ "$TIER" == "outbound" ]]; then
    is_test_account=0
    if [[ -n "$TARGET_USER" && -f "$TEST_ACCOUNTS_FILE" ]] \
      && grep -qx "$TARGET_USER" "$TEST_ACCOUNTS_FILE"; then
      is_test_account=1
    fi
    if [[ $is_test_account -eq 0 && "${CARAMEL_ADMIN_OUTBOUND_APPROVED:-}" != "1" ]]; then
      echo "ERROR: 이 호출은 실제 디테일러 슬롯을 점유하거나 고객·돈이 바깥으로 나갑니다." >&2
      echo "  대상 userId=${TARGET_USER:-?} 는 $TEST_ACCOUNTS_FILE 의 테스트 계정이 아닙니다." >&2
      echo "  실고객 대상이면 사용자에게 대상과 body를 확인받은 뒤 CARAMEL_ADMIN_OUTBOUND_APPROVED=1 을 붙이세요." >&2
      exit 1
    fi
  fi

  printf '%s\t%s\t%s\t%s\t%s\tuser=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CARAMEL_ADMIN_BASE" "$TIER" "$METHOD" "$API_PATH" "${TARGET_USER:-}" \
    >> "$AUDIT_LOG"
fi

# ── 토큰 캐시 (20분) ──────────────────────────────────────────────
TOKEN=""
TOKEN_MAX_AGE=1200  # seconds

_token_valid() {
  [[ -f "$TOKEN_FILE" ]] || return 1
  local mtime now age
  mtime=$(python3 -c "import os; print(int(os.path.getmtime('$TOKEN_FILE')))")
  now=$(python3 -c "import time; print(int(time.time()))")
  age=$(( now - mtime ))
  [[ $age -lt $TOKEN_MAX_AGE ]]
}

_login() {
  # 로그인 — 응답 본문을 stdout에 절대 노출하지 않음
  local login_resp http_code
  login_resp=$(curl -s -w "\n__HTTP_CODE__:%{http_code}" \
    -X POST "$CARAMEL_ADMIN_BASE/v1/auth/admin/token" \
    -H "Content-Type: application/json" \
    --data-binary @- <<EOF_BODY 2>/dev/null
{"username":"$CARAMEL_ADMIN_USER","password":"$CARAMEL_ADMIN_PASS"}
EOF_BODY
  )
  http_code=$(echo "$login_resp" | grep "__HTTP_CODE__:" | sed 's/.*__HTTP_CODE__://')
  local body
  body=$(echo "$login_resp" | grep -v "__HTTP_CODE__:")

  if [[ "$http_code" != "200" && "$http_code" != "201" ]]; then
    echo "[HTTP $http_code] 로그인 실패" >&2
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body" >&2
    exit 1
  fi

  local token
  token=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['accessToken'])" 2>/dev/null)
  if [[ -z "$token" ]]; then
    echo "ERROR: accessToken을 응답에서 추출하지 못했습니다." >&2
    exit 1
  fi

  # 토큰을 파일에만 저장 (chmod 600)
  echo "$token" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  TOKEN="$token"
}

_load_token() {
  if _token_valid; then
    TOKEN=$(cat "$TOKEN_FILE")
  else
    _login
  fi
}

# ── API 호출 함수 ──────────────────────────────────────────────────
_call_api() {
  local method="$1" path="$2" body="$3" token="$4"
  local url="$CARAMEL_ADMIN_BASE$path"
  local curl_args=(-s -w "\n__HTTP_CODE__:%{http_code}"
    -X "$method"
    -H "Authorization: Bearer $token"
    -H "Content-Type: application/json"
  )

  if [[ -n "$body" ]]; then
    curl_args+=(--data-binary "$body")
  fi

  curl "${curl_args[@]}" "$url" 2>/dev/null
}

# ── 메인 실행 ─────────────────────────────────────────────────────
_load_token

raw=$(_call_api "$METHOD" "$API_PATH" "$BODY" "$TOKEN")
http_code=$(echo "$raw" | grep "__HTTP_CODE__:" | sed 's/.*__HTTP_CODE__://')
resp_body=$(echo "$raw" | grep -v "__HTTP_CODE__:")

echo "[HTTP $http_code]" >&2

# 401/403 → 토큰 폐기 후 1회 재시도
if [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
  echo "[AUTH] 토큰 만료 또는 권한 오류 — 재로그인 후 재시도합니다." >&2
  rm -f "$TOKEN_FILE"
  _login
  raw=$(_call_api "$METHOD" "$API_PATH" "$BODY" "$TOKEN")
  http_code=$(echo "$raw" | grep "__HTTP_CODE__:" | sed 's/.*__HTTP_CODE__://')
  resp_body=$(echo "$raw" | grep -v "__HTTP_CODE__:")
  echo "[HTTP $http_code] (재시도 후)" >&2
fi

# 응답 pretty print
echo "$resp_body" | python3 -m json.tool 2>/dev/null || echo "$resp_body"
