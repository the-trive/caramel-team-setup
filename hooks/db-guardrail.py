#!/usr/bin/env python3
"""PreToolUse(Bash) DB 파괴 방어 가드레일.

DROP TABLE / DROP DATABASE / TRUNCATE / prisma --accept-data-loss 등
되돌릴 수 없는 DB 파괴 패턴을 bypassPermissions · autoMode 무관하게 차단한다.

설계 원칙:
- 스키마를 바꾸는 DDL(CREATE/ALTER/DROP/RENAME/TRUNCATE)을 DB에 직접 실행하는 것은
  추가성이든 파괴적이든 전부 deny. 정규 경로는 prisma migrate 뿐이다.
- prisma --accept-data-loss / migrate reset / --force-reset 은 무조건 deny.
- 파일에 SQL을 쓰는 행위(echo "DROP..." > file.sql)는 실행이 아니므로 통과.
  → DB 실행 컨텍스트가 있을 때만 차단.
- SHOW CREATE TABLE 같은 읽기는 통과.
- 오탐보다 과차단을 선택한다. 막혔으면 직접 터미널에서 실행.

2026-08-13 확장 이유: 이 훅은 원래 "데이터 손실 방지"만 목표라 추가성 DDL을 일부러
통과시켰다. 그 구멍으로 detailer_post 3개 테이블을 mysql-query.sh로 직접 만들었고,
_prisma_migrations에 기록이 없어 공유 dev DB가 drift 상태가 됐다(다음 사람의
migrate dev가 리셋을 제안한다). prisma-migration-guard.py는 migration.sql '파일 쓰기'만
막아서 이 경로는 검사조차 안 됐다.
또한 인터프리터(python + mysql.connector 등) 경로는 MYSQL_EXEC에 안 걸려
파괴적 DDL조차 통과했다 — 그쪽도 함께 막는다.
"""
import json
import re
import sys


def allow():
    sys.exit(0)


def deny(reason: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# ── 패턴 ────────────────────────────────────────────────────────────────────

# 1. Prisma 위험 플래그 (따옴표 여부 무관, 명령 어디에 있어도 위험)
PRISMA_DANGER = re.compile(
    r"(?:npx\s+)?(?:dotenv\s+-e\s+\S+\s+--\s+)?(?:npx\s+)?prisma\b"
    r".*?(?:--accept-data-loss|--force-reset|migrate\s+reset)",
    re.IGNORECASE | re.DOTALL,
)

# 2. MySQL 실행 컨텍스트
#    mysql-query.sh, mysql-write.sh, ~/claude/mysql-query.sh,
#    mysql -h ... -e "SQL", mysql -u ... <<EOF
MYSQL_EXEC = re.compile(
    r"(?:mysql(?:-query|-write)?(?:\.sh)?|mysql\b(?:\s+-\w+)*\s+-e\b)",
    re.IGNORECASE,
)

# 3. SQL 파괴 패턴
SQL_DESTRUCTIVE = re.compile(
    r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|COLUMN)|TRUNCATE(?:\s+TABLE)?)\b",
    re.IGNORECASE,
)

# 4. SSH + mysql 조합 (원격 DROP)
SSH_MYSQL = re.compile(r"\bssh\b.*?\bmysql\b", re.DOTALL | re.IGNORECASE)

# 5. 잘못된 레포(caramel-api)에 prisma 스키마 작업 → DB 형상 SOT는 caramel-zero
#    db push / migrate 를 outdated caramel-api 스키마 대상으로 돌리면 테이블 삭제 사고남
#    (2026-06-08 dev DB 테이블 4개 삭제, 2026-06-15 노진우 정정)
PRISMA_PUSH_MIGRATE = re.compile(
    r"prisma\b.*?(?:db\s+push|migrate\s+(?:dev|deploy|diff|resolve))",
    re.IGNORECASE | re.DOTALL,
)
WRONG_REPO_PRISMA = re.compile(
    r"libs/caramel-prisma|caramel-api\b",
    re.IGNORECASE,
)

# 6. DB 실행 컨텍스트 (MYSQL_EXEC보다 넓다)
#    mysql 계열 헬퍼·CLI에 더해, 인터프리터로 드라이버를 태우는 경로까지 본다.
#    python devq.py "CREATE TABLE ..." 처럼 스크립트에 SQL을 넘기는 것도
#    인터프리터 호출이므로 걸린다 — 과차단이 누락보다 싸다.
DB_EXEC = re.compile(
    r"(?:"
    r"mysql(?:-query|-write)?(?:\.sh)?"
    r"|\bmysql\b(?:\s+-\w+)*\s+-e\b"
    r"|\bmysql\b[^|;&]*<<"
    r"|mysql\.connector|pymysql|mysql2|mariadb|sqlalchemy|psycopg"
    r"|\b(?:python3?|node|deno|bun|ruby|perl)\b"
    r")",
    re.IGNORECASE,
)

# 7. 스키마를 바꾸는 DDL. 추가성도 포함한다 — 정규 경로는 prisma migrate 뿐이다.
SCHEMA_DDL = re.compile(
    r"\b(?:"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?"
    r"(?:TABLE|INDEX|DATABASE|SCHEMA|VIEW)"
    r"|ALTER\s+(?:TABLE|DATABASE|SCHEMA)"
    r"|DROP\s+(?:TABLE|DATABASE|SCHEMA|COLUMN|INDEX|VIEW|CONSTRAINT|FOREIGN\s+KEY)"
    r"|RENAME\s+TABLE"
    # TABLE 키워드를 요구한다. 맨몸 TRUNCATE 를 잡으면 Tailwind 클래스
    # `truncate` 가 걸려 프런트 파일 편집이 전부 막힌다(2026-09-07 4회 실측).
    # `mysql -e "TRUNCATE t"` 는 검사 2·3의 SQL_DESTRUCTIVE 가 그대로 잡는다.
    r"|TRUNCATE\s+TABLE"
    r")\b",
    re.IGNORECASE,
)

# 읽기 전용이라 DDL로 보면 안 되는 것. 검사 전에 지운다.
READ_ONLY_DDL = re.compile(
    r"\bSHOW\s+CREATE\s+\w+",
    re.IGNORECASE,
)

# Prisma가 스스로 DDL을 만들어 적용하는 정규 경로. 여기 걸리면 통과시킨다.
PRISMA_SANCTIONED = re.compile(
    r"prisma\b.*?migrate\s+(?:dev|diff|deploy|resolve|status)"
    r"|\bdb:migrate\b",
    re.IGNORECASE | re.DOTALL,
)

DDL_HOWTO = (
    "[db-guardrail] 차단: DB에 스키마 DDL을 직접 실행하려 합니다.\n"
    "추가성(CREATE TABLE)이어도 막습니다 — 이력이 _prisma_migrations 에 남지 않아\n"
    "공유 dev DB가 drift 상태가 되고, 다음 사람의 migrate dev 가 리셋을 제안합니다.\n"
    "(2026-08-13 detailer_post 3개 테이블에서 실제로 발생)\n"
    "\n"
    "정규 경로:\n"
    "  1) apps/api/prisma/schema.prisma 를 고친다\n"
    "  2) cd apps/api && pnpm db:migrate   ← Prisma가 폴더+SQL을 만들고 dev에 적용\n"
    "  3) 생성된 migrations 폴더를 커밋한다\n"
    "  4) prod 는 그 SQL 을 사람이 수동 실행한다\n"
    "\n"
    "스키마가 dev에 없어 E2E가 막히면 DDL을 직접 치지 말고 2)를 요청하세요.\n"
    "읽기(SHOW CREATE TABLE / SELECT)는 막지 않습니다."
)


def strip_quotes(text: str) -> str:
    """따옴표로 둘러싼 문자열 내용을 제거한다 (echo '...' 오탐 방지)."""
    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    return text


def strip_heredocs(text: str) -> str:
    """heredoc 본문을 제거한다."""
    return re.sub(
        r"<<-?\s*[\"']?(\w+)[\"']?.*?\n\s*\1\b",
        " ",
        text,
        flags=re.DOTALL,
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()

    raw_cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
    # 따옴표·heredoc 제거한 버전 (echo/cat 등 오탐 방지용)
    stripped_cmd = strip_quotes(strip_heredocs(raw_cmd))

    # ── 검사 1: Prisma 위험 플래그 ─────────────────────────────────────────
    # raw에서 먼저 확인 (직접 실행), strip에서도 확인 (bash -c "npx prisma ..." 등)
    if PRISMA_DANGER.search(stripped_cmd):
        deny(
            "[db-guardrail] 차단: prisma --accept-data-loss / migrate reset / "
            "--force-reset 는 테이블을 영구 삭제합니다.\n"
            "정말 필요하면 직접 터미널에서 실행하세요. "
            "(Claude는 이 명령을 자동 실행할 수 없습니다.)"
        )

    # ── 검사 1b: 잘못된 레포(caramel-api) prisma db push / migrate ──────────
    if PRISMA_PUSH_MIGRATE.search(stripped_cmd) and WRONG_REPO_PRISMA.search(raw_cmd):
        deny(
            "[db-guardrail] 차단: caramel-api(레거시) 스키마 대상 prisma db push / "
            "migrate 가 감지됐습니다. 이 스키마는 outdated이며, 그걸 기준으로 DB "
            "형상을 바꾸면 테이블이 삭제됩니다 (2026-06-08 사고 원인).\n"
            "DB 스키마 변경의 진짜 SOT는 caramel-zero 입니다:\n"
            "  1) caramel-zero/apps/api/prisma/schema.prisma 수정\n"
            "  2) cd apps/api && pnpm db:migrate (--name <설명>) → migration.sql 생성\n"
            "메모리 reference_prisma_schema_sot_caramel_zero 참조."
        )

    # ── 검사 2: MySQL 실행 컨텍스트 + SQL 파괴 패턴 ─────────────────────────
    if MYSQL_EXEC.search(raw_cmd) and SQL_DESTRUCTIVE.search(raw_cmd):
        deny(
            "[db-guardrail] 차단: mysql 명령 안에서 DROP TABLE / TRUNCATE 가 "
            "감지됐습니다. 되돌릴 수 없는 작업입니다.\n"
            "정말 필요하면 직접 터미널에서 실행하세요."
        )

    # ── 검사 3: SSH + mysql + DROP/TRUNCATE ─────────────────────────────────
    if SSH_MYSQL.search(raw_cmd) and SQL_DESTRUCTIVE.search(raw_cmd):
        deny(
            "[db-guardrail] 차단: SSH 원격 mysql DROP / TRUNCATE 가 감지됐습니다. "
            "직접 터미널에서 실행하세요."
        )

    # ── 검사 4: DB 실행 컨텍스트 + 스키마 DDL ───────────────────────────────
    # 추가성 DDL도 막는다. 마이그레이션 이력을 남기는 경로는 prisma migrate 뿐이다.
    ddl_target = READ_ONLY_DDL.sub(" ", raw_cmd)
    if (
        SCHEMA_DDL.search(ddl_target)
        and (DB_EXEC.search(raw_cmd) or SSH_MYSQL.search(raw_cmd))
        and not PRISMA_SANCTIONED.search(raw_cmd)
    ):
        deny(DDL_HOWTO)

    allow()


if __name__ == "__main__":
    main()
