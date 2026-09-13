#!/bin/bash
# 아침 브리핑을 밖에서 깨운다 (맥의 launchd 가 부른다).
#
# 왜 있나: 이 저장소의 **깃허브 예약(schedule)은 몇 시간 늦게 옵니다** (2026-09-09 실측:
# 21:45 UTC 예약이 23:39 에 옴. estate-news 는 2시간, rokieyez.github.io 는 5시간 늦음).
# 아침 7시를 지키려면 맥이 깨워야 합니다. 2026-09-08 에 「통째로 죽었다」고 적었던 것은
# 20분짜리 탐침의 오판이었습니다.
#
# 깨우기 전에 **국회·여론조사 자료를 맥에서 먼저 받아 저장소에 밀어 넣습니다** (2026-09-09).
# 깃허브 러너(해외)에서는 열린국회정보·korea.kr 접속이 흔들려 아침마다 발의법률안·본회의를
# 못 받았습니다. 맥(한국 IP)은 안정적입니다. 러너는 state/civics/<날짜>.json 이 있으면 그것을 씁니다.
# 이 단계가 실패해도 워크플로는 부릅니다 — 러너가 직접 받는 예전 방식으로 돌아갈 뿐입니다.
#
# 안전합니다 — 워크플로 첫 단계가 "오늘 산출물이 이미 있으면 건너뜀" 이라
# 깃허브 예약과 겹쳐도 두 번 만들지 않습니다.
#
# 새 인증키가 필요 없습니다. gh 가 이미 로그인돼 있고 키체인에서 읽습니다
# (빈 환경에서도 되는 것을 확인했습니다). 미리 받기의 열린국회정보 키는 작업 폴더의 .env 에서
# 읽습니다 — 거기 ASSEMBLY_API_KEY 가 없으면 견본(5건)만 받고, 러너가 제 키로 다시 받습니다.
set -uo pipefail

GH=/opt/homebrew/bin/gh
REPO=rokieyez/politics-news
# WAKE_* 는 시험용 덮어쓰기다 (가짜 저장소·로그로 미리 받기만 돌려 볼 때). 평소엔 비어 있다.
LOG="${WAKE_LOG:-$HOME/Library/Logs/politics-news-wake.log}"

mkdir -p "$(dirname "$LOG")"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 잠에서 막 깬 참이면 네트워크가 아직 안 붙어 있을 수 있다. 처음엔 최대 5분 기다린다.
wait_net() {
    local tries="${1:-10}" i
    for i in $(seq 1 "$tries"); do
        /usr/bin/curl -sf -m 10 -o /dev/null https://api.github.com && return 0
        say "네트워크를 기다립니다 ($i/$tries)"
        sleep 30
    done
    return 1
}
wait_net 10 || true

# ── 1. 맥에서 국회·여론조사 자료를 미리 받아 저장소에 밀어 넣기 ──────────────────
# 작업 폴더(WORK)의 파이썬·설정·.env 로 받고, 커밋·푸시는 **따로 받아 둔 사본(CLONE)** 에서 한다.
# 작업 폴더에는 편집 중인 것이 있을 수 있어 거기서 git 을 만지지 않는다.
WORK="${WAKE_WORK:-$HOME/Desktop/Projects/Active/politics-news}"
CLONE="${WAKE_CLONE:-$HOME/Library/Caches/rokiz/politics-news-wake}"
REMOTE="${WAKE_REMOTE:-https://github.com/$REPO.git}"
RETRY_SLEEP="${WAKE_RETRY_SLEEP:-20}"
GIT=/usr/bin/git
TODAY=$(TZ=Asia/Seoul date +%F)
prefetch() {
    [ -x "$WORK/.venv/bin/python" ] || { say "미리 받기 건너뜀: $WORK/.venv 없음"; return 1; }
    local out
    out=$(cd "$WORK" && "$WORK/.venv/bin/python" -m rebrief civics --prefetch --date "$TODAY" 2>&1) \
        || { say "미리 받기 실패: $(echo "$out" | tail -3 | tr '\n' ' ')"; return 1; }
    say "미리 받음: $(echo "$out" | tail -2 | tr '\n' ' ')"
    local file="$WORK/state/civics/$TODAY.json"
    [ -f "$file" ] || { say "미리 받기 결과 파일이 없음"; return 1; }
    # 작업 폴더는 곧바로 비운다 — 뒤에서 밀어 넣기가 실패해도 남겨 두면 나중에 git pull 이
    # "추적하지 않는 파일을 덮어쓴다" 며 막힌다 (9/10 실제로 그랬고, 9/13 에도 실패 뒤 남아 있었다).
    local keep
    keep=$(mktemp -t politics-civics) && mv "$file" "$keep" || { say "임시 파일을 만들지 못함"; return 1; }
    if [ ! -d "$CLONE/.git" ]; then
        mkdir -p "$(dirname "$CLONE")"
        "$GIT" clone -q --depth 1 "$REMOTE" "$CLONE" 2>>"$LOG" || { say "사본 받기 실패"; rm -f "$keep"; return 1; }
    fi
    # 받기·올리기를 한 덩이로 세 번까지. 9/12 는 올리다, 9/13 은 받다가 연결이 끊겼다(Recv failure:
    # Operation timed out) — 둘 다 막 깬 맥의 흔들리는 연결이었고, 한 번 더 했으면 됐을 일이다.
    # 매번 원격 끝에 맞춘 뒤 파일을 다시 얹으므로, 사이에 러너가 밀어 넣어도 충돌하지 않는다.
    publish() {
        (cd "$CLONE" && "$GIT" fetch -q --depth 1 "$REMOTE" main && "$GIT" reset -q --hard FETCH_HEAD) 2>>"$LOG" \
            || return 1
        mkdir -p "$CLONE/state/civics" && cp "$keep" "$CLONE/state/civics/$TODAY.json"
        (cd "$CLONE" && "$GIT" add state/civics \
            && { "$GIT" diff --cached --quiet \
                 || "$GIT" -c user.name="rokiz-mac" -c user.email="rokieyez@gmail.com" \
                        commit -q -m "국회 자료 미리 받음: $TODAY (맥)"; } \
            && "$GIT" push -q "$REMOTE" HEAD:main) 2>>"$LOG"
    }
    local n
    for n in 1 2 3; do
        if publish; then
            say "미리 받은 자료를 밀어 넣었습니다: state/civics/$TODAY.json$([ "$n" -gt 1 ] && echo " ($n번째 시도)")"
            rm -f "$keep"
            return 0
        fi
        say "밀어 넣기 실패 ($n/3)"
        [ "$n" -lt 3 ] && { sleep "$RETRY_SLEEP"; wait_net 3 || true; }
    done
    rm -f "$keep"
    say "미리 받은 자료 푸시 실패 (세 번) — 러너가 직접 받습니다"
    return 1
}
prefetch || true            # 07:25 두 번째 호출이면 한 번 더 받는다 — 몇십 초이고 더 새 자료다
# 손으로 시험할 때: `scripts/wake-daily-brief.sh --prefetch-only` 는 여기서 멈춘다 (워크플로를 부르지 않는다).
[ "${1:-}" = "--prefetch-only" ] && exit 0

# ── 2. 워크플로 깨우기 ────────────────────────────────────────────────────

# 세 번까지 다시 시도한다. 뚜껑을 닫아 둔 사이 06:45 과 07:25 이 **한 번으로 합쳐져**
# 깨어날 때 실행되므로(launchd.plist 설명서), 그 한 번이 실패하면 그날은 재시도가 없다.
for attempt in 1 2 3; do
    if out=$("$GH" workflow run daily-brief.yml --repo "$REPO" --ref main 2>&1); then
        say "깨웠습니다 (${attempt}번째 시도)"
        exit 0
    fi
    say "실패 ($attempt/3): $out"
    [ "$attempt" -lt 3 ] && sleep 60
done
exit 1
