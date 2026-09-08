#!/usr/bin/env python3
"""PreToolUse(Bash) git 가드레일.

팀원 작업과 절대 꼬이지 않도록, 실행 '전에' 위험한 git 명령을 차단한다.
stdin 으로 훅 JSON 을 받아, 차단할 경우 deny 결정 JSON 을 stdout 으로 내보낸다.
권한 모드(bypassPermissions 포함)와 무관하게 PreToolUse deny 는 도구 실행을 막는다.

설계 원칙:
- 원격(공유)에 영향을 주거나 남의 미커밋 작업을 파괴할 수 있는 명령만 막는다.
- 일반적인 기능-브랜치 작업(`git push -u origin feature/...`, 경로 명시 add/commit)은 통과.
- 애매하면 막는 쪽(fail-safe). 정말 필요하면 사람이 직접 터미널에서 실행.

오탐 방지(중요):
- 검사는 '실제로 git 바이너리를 호출하는' 세그먼트에만 적용한다. `gh`(GitHub CLI),
  `github`, 그 밖의 명령은 통과한다. PR 생성(`gh pr create`, `gh api .../pulls`)은
  금지 대상이 아니라 '권장 경로'다 — 절대 막지 않는다.
- 따옴표 문자열과 heredoc 본문(커밋 메시지·PR 본문 등)은 검사 전에 제거한다.
  메시지에 "force push", "main", "-f" 같은 단어가 들어 있어도 오탐하지 않게 한다.
"""
import json
import os
import re
import subprocess
import sys

PROTECTED = ("main", "master", "develop")
# 직접 push는 허용하되(자동배포 운영 브랜치), stale push만 막을 대상.
# 작업 브랜치가 origin 최신본보다 뒤처진 채 여기로 push하면 운영 코드가 되돌아간다.
DEPLOY_BRANCHES = ("deploy",)


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


def strip_heredocs(text: str) -> str:
    """heredoc 본문(<< EOF ... EOF)을 제거한다. 커밋 메시지/PR 본문이 여기 들어온다."""
    # << TOKEN, <<- TOKEN, << 'TOKEN', << "TOKEN" 모두 처리. 닫는 토큰 줄까지 제거.
    return re.sub(
        r"<<-?\s*[\"']?(\w+)[\"']?.*?\n\s*\1\b",
        " ",
        text,
        flags=re.DOTALL,
    )


def strip_quotes(text: str) -> str:
    """따옴표로 둘러싼 문자열 내용을 제거한다(플래그/구조는 보존)."""
    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    return text


def command_word(seg: str) -> str:
    """세그먼트가 실제로 호출하는 명령어 이름을 돌려준다.

    선행 환경변수 할당(VAR=..., VAR="..."), sudo/command/env 래퍼를 벗겨낸 뒤
    첫 토큰을 명령어로 본다. 예: `GIT_SSH_COMMAND="ssh ..." git clone` -> "git".
    """
    s = seg.strip()
    # 선행 환경변수 할당 제거 (값이 따옴표로 묶여 공백을 포함할 수 있음)
    while True:
        m = re.match(
            r'^[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S+)\s+',
            s,
        )
        if not m:
            break
        s = s[m.end():]
    # sudo / command / env 래퍼 제거
    m = re.match(r'^(?:sudo|command|env)\s+', s)
    if m:
        s = s[m.end():]
    m = re.match(r'^\\?([A-Za-z0-9_./-]+)', s)
    return os.path.basename(m.group(1)) if m else ""


def warn(message: str):
    """사용자에게 경고만 표시하고 계속 진행."""
    print(json.dumps({"systemMessage": message}))
    sys.exit(0)


def inject_fetch(raw_cmd: str, base: str) -> str:
    """git checkout -b ... origin/<base> 앞에 git fetch origin <base> && 를 주입."""
    fetch_cmd = f"git fetch origin {base}"

    # 이미 fetch가 있으면 주입 불필요
    if re.search(r"git\s+fetch", raw_cmd):
        return raw_cmd

    # git checkout -b 바로 앞에 fetch 삽입 (cd는 이미 앞에 있으므로 git만)
    injected = re.sub(
        r"(git\s+checkout\s+-b)",
        f"{fetch_cmd} && \\1",
        raw_cmd,
        count=1,
    )
    return injected


def _push_target_source(seg: str):
    """push 세그먼트에서 (src, dst) 브랜치를 추출. 못 찾으면 None.

    `push origin HEAD:deploy` -> ("HEAD", "deploy")
    `push origin deploy`      -> ("HEAD", "deploy")  # 로컬 HEAD를 deploy로
    플래그(-u 등)는 건너뛴다. seg는 따옴표 제거·공백 정규화된 상태.
    """
    m = re.search(r"\bpush\b((?:\s+-\S+)*)\s+(\S+)\s+(\S+)", seg)
    if not m:
        return None
    refspec = m.group(3)
    if ":" in refspec:
        src, dst = refspec.split(":", 1)
    else:
        src, dst = "HEAD", refspec
    return src.strip().rstrip("'\""), dst.strip().rstrip("'\"")


def _guard_stale_deploy(raw_cmd: str, src: str, dst: str):
    """운영 브랜치로의 push 전, origin/<dst>가 push 대상(src)에 포함됐는지 검사.

    포함 안 됐으면(= 작업 브랜치가 운영 최신본보다 stale) deny.
    환경 문제(fetch 실패·src 미존재·예외)는 통과(fail-open) — 기존 가드 철학
    대로 명확한 stale 신호일 때만 막아 오탐으로 인한 가드 우회를 예방한다.
    """
    m = re.search(r"\bcd\s+([^\s&|;]+)", raw_cmd)
    workdir = os.path.expanduser(m.group(1)) if m else os.getcwd()

    def git(*args):
        return subprocess.run(
            ["git", "-C", workdir, *args],
            capture_output=True, text=True, timeout=20,
        )

    try:
        if git("fetch", "origin", dst).returncode != 0:
            return  # 원격 못 받음 → 검사 불가, 통과
        if git("rev-parse", "--verify", "--quiet", src).returncode != 0:
            return  # push 대상 ref 알 수 없음 → 통과
        anc = git("merge-base", "--is-ancestor", f"origin/{dst}", src)
    except Exception:
        return  # 검사 자체 실패 → 통과
    if anc.returncode == 1:  # origin/dst가 src의 조상이 아님 = stale
        deny(
            f"운영 브랜치 '{dst}' STALE push 차단: origin/{dst}가 push 대상({src})에 "
            f"포함돼 있지 않습니다 — 작업 브랜치가 운영 최신본보다 뒤처졌습니다. "
            f"이대로 push하면 운영 코드가 되돌아갑니다.\n"
            f"→ origin/{dst} 기준 fresh worktree에서 변경을 재적용하세요:\n"
            f"   git fetch origin {dst} && git worktree add /tmp/deploy-fix origin/{dst} -b fix-<name>\n"
            f"   (변경 재적용·커밋) → git -C /tmp/deploy-fix push origin HEAD:{dst}"
        )
    # anc.returncode == 0 → origin/dst ⊆ src (최신 포함) → 통과


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()  # 파싱 실패 시 막지 않음(오탐 방지). 다른 레이어가 받친다.
    raw_cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""

    # 섹션 0: git checkout -b ... origin/<base> 감지 → fetch 자동 주입
    # 따옴표 안의 내용(echo/heredoc 등)은 제외하고 실제 실행 명령에서만 감지
    cmd_stripped = strip_quotes(strip_heredocs(raw_cmd))
    checkout_b_match = re.search(
        r"git\s+checkout\s+-b\s+\S+\s+origin/(\S+)", cmd_stripped
    )
    if checkout_b_match and not re.search(r"git\s+fetch", cmd_stripped):
        base = checkout_b_match.group(1).rstrip("'\"")
        new_cmd = inject_fetch(raw_cmd, base)
        if new_cmd != raw_cmd:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {"command": new_cmd},
                },
                "systemMessage": f"[git-guardrail] stale ref 방지: git fetch origin {base} 자동 주입"
            }))
            sys.exit(0)

    # 검사 대상 정규화: heredoc 본문 + 따옴표 문자열 제거 후 세그먼트 분리.
    cmd = strip_quotes(strip_heredocs(raw_cmd))

    if not re.search(r"\bgit\b", cmd):
        allow()

    # 명령을 세그먼트(&&, ||, ;, | 기준)로 쪼개 명령 단위로 검사.
    segments = re.split(r"&&|\|\||;|\|", cmd)

    git_has_commit = False   # 섹션 7: 보호 브랜치 위 direct commit 차단용
# (push는 섹션 3에서 명시적 브랜치명 기준으로 처리 — 섹션 7에서 중복 차단 안 함)

    for seg in segments:
        # 실제 git 바이너리 호출이 아니면 통과 (gh 등 다른 명령은 검사 안 함)
        if command_word(seg) != "git":
            continue

        s = " " + re.sub(r"\s+", " ", seg.strip()) + " "


        is_push = re.search(r"\bpush\b", s) is not None
        is_commit = re.search(r"\bcommit\b", s) is not None
        if is_commit:
            git_has_commit = True

        # 1) force push (어떤 형태든)
        # -qf 처럼 묶인 단축 플래그와 줄 끝의 -f 까지 잡는다. `+refspec` 도 force push다.
        if is_push and re.search(
            r"(?:^|\s)(?:--force(?:-with-lease)?(?:=\S*)?|-[A-Za-z]*f[A-Za-z]*|\+[^\s]+)(?=\s|$)",
            s,
        ):
            deny("force push 차단: 팀원 커밋을 덮어쓸 수 있음. 정말 필요하면 사람이 직접 터미널에서 실행하세요.")

        # 2) 원격 브랜치 삭제
        if is_push and (re.search(r"\s--delete(\s|=)", s) or re.search(r"\s:", s)):
            deny("원격 브랜치 삭제 차단: 팀 브랜치를 지울 수 있음. 사람이 직접 확인 후 실행하세요.")

        # 3) 보호 브랜치(main/master/develop) 직접 push (이름 기준)
        if is_push:
            for b in PROTECTED:
                if re.search(r"(\s|:)" + re.escape(b) + r"(\s|$)", s):
                    deny(f"보호 브랜치 '{b}' 직접 push 차단: 반드시 PR로. 기능 브랜치에 push 후 gh pr create 하세요.")

        # 3.5) 운영 브랜치(deploy 등)로의 STALE push 차단
        if is_push:
            ts = _push_target_source(s)
            if ts and ts[1] in DEPLOY_BRANCHES:
                _guard_stale_deploy(raw_cmd, ts[0], ts[1])

        # 4) 전체 스테이징 (남의 미커밋 작업까지 휩쓸림)
        if re.search(r"\bgit\b\s+add\b", s) and re.search(r"(\s)(-A|--all|\.)(\s|$)", s):
            deny("git add -A/--all/. 차단: 다른 사람 변경까지 스테이징될 수 있음. 내가 바꾼 파일 경로를 명시해서 add 하세요.")

        # commit -a / -am (추적 중인 모든 변경 커밋)
        if is_commit and re.search(r"(\s)(-a|-am|-ma|--all)(\s|$)", s):
            deny("git commit -a/-am 차단: 추적 중인 모든 변경이 커밋됨. git add <경로> 후 git commit -m 하세요.")

        # 5) 전체 트리 파괴 (미커밋 작업 소실)
        if re.search(r"\bgit\b\s+reset\b.*--hard", s):
            deny("git reset --hard 차단: 커밋 안 된 팀원 작업이 날아갈 수 있음.")
        if re.search(r"\bgit\b\s+clean\b\s+-[a-z]*f", s):
            deny("git clean -f 차단: 추적 안 된 파일이 삭제됨.")
        if re.search(r"\bgit\b\s+(checkout|restore)\b.*(\s--)?\s(\.|:/)(\s|$)", s):
            deny("git checkout/restore 전체 되돌리기 차단: 커밋 안 된 변경이 날아감. 파일 경로를 명시하세요.")

        # 6) 브랜치 강제 삭제 (병합 안 된 작업 소실)
        if re.search(r"\bgit\b\s+branch\b.*\s-D(\s|$)", s):
            deny("git branch -D 차단: 병합 안 된 브랜치가 소실될 수 있음. -d(안전 삭제)만 쓰거나 사람이 확인하세요.")

    # 7) best-effort: 현재 보호 브랜치 위에서 직접 commit 차단
    # push는 섹션 3(명시적 브랜치명)이 담당 — ff-only 머지 후 git push는 허용
    if git_has_commit:
        m = re.search(r"\bcd\s+([^\s&|;]+)", raw_cmd)
        workdir = os.path.expanduser(m.group(1)) if m else os.getcwd()
        try:
            branch = subprocess.run(
                ["git", "-C", workdir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            branch = ""
        if branch in PROTECTED:
            deny(f"현재 보호 브랜치 '{branch}' 위에서 직접 commit 차단: 기능 브랜치에서 작업 후 "
                 f"ff-only 머지만 허용 (git checkout -b <branch>).")

    allow()


if __name__ == "__main__":
    main()
