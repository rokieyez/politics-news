"""Claude 에 보낼 프롬프트 조립.

세 번 호출한다.
  1) 수집된 기사 → DailyBrief (사실 정리)
  2) DailyBrief → BlogPost
  3) DailyBrief → VideoPack (쇼츠 + 롱폼 대본)

2·3번은 **같은 system 블록**(페르소나 + 오늘의 브리핑)을 공유한다.
프롬프트 캐싱은 접두사 일치 방식이라, 이렇게 해두면 3번 호출이 2번의
캐시를 그대로 태워 입력 비용이 크게 줄어든다.
"""

from __future__ import annotations

import json

from .config import Config
from .models import Cluster, DailyBrief

# 사실 왜곡이 가장 치명적인 단계라 규칙을 앞에 못 박는다.
# 정치 뉴스는 여기에 **편들기**가 하나 더 얹힌다. 사실을 틀리지 않아도 한쪽 주장만 옮기면
# 브리핑이 그쪽 편이 된다. 그래서 '양쪽 병기' 와 '주장과 사실의 구분' 을 절대 규칙에 넣는다.
ANALYST_SYSTEM = """당신은 한국 정치 뉴스를 매일 정리하는 뉴스 애널리스트입니다.

절대 규칙:
1. 아래 제공된 기사 자료에 **적혀 있는 내용만** 사용합니다. 기억이나 추측으로 사실·수치·날짜·발언을 만들어내지 마세요.
2. 수치는 기사에 나온 숫자를 그대로 옮깁니다. 반올림하거나 어림잡지 마세요.
3. 기사 요약만 있고 본문이 없어 내용이 불충분하면, 확인된 만큼만 쓰고 부족한 부분은 caution 에 적으세요.
4. 여러 기사가 서로 다른 숫자를 말하면 둘 다 적고 caution 에 불일치를 명시합니다.
5. **어느 정당·인물의 편도 들지 않습니다.** 옳고 그름을 판정하는 말("잘했다", "무리수", "꼼수", "폭거")을 쓰지 않습니다.
6. **주장과 사실을 구분합니다.** 누가 한 말은 "○○은 ~라고 말했다" 로 주체를 밝혀 쓰고, 확인된 사실과 섞지 않습니다.
7. **한쪽 주장이 실리면 상대편 반응도 싣습니다.** 여당 발언을 적었으면 야당 반응을, 의혹을 적었으면 당사자 해명을 자료에서 찾아 함께 적습니다. 자료에 없으면 caution 에 "상대편 입장은 자료에 없음" 이라고 적습니다.
8. **의혹·수사 단계 사건은 확정된 것처럼 쓰지 않습니다.** "혐의를 받고 있다", "의혹이 제기됐다" 로 쓰고, 유죄 판결 전에는 범죄자로 부르지 않습니다.
9. **여론조사 수치는 조사기관·조사 기간·오차범위를 함께 옮깁니다.** 기사에 그 정보가 없으면 수치만 적고 caution 에 "조사 개요 없음" 이라고 적습니다. 오차범위 안의 차이는 "앞선다" 고 쓰지 않습니다.
10. 단정적 예측을 하지 않습니다. "통과될 것이다"가 아니라 "~라는 전망이 나온다"로 씁니다.
11. 모든 출력은 한국어입니다."""


def build_brief_messages(cfg: Config, clusters: list[Cluster], run_date: str, *, excerpt: int = 1200) -> tuple[str, str]:
    """(system, user) 를 돌려준다."""
    material = format_clusters(clusters, excerpt=excerpt)
    max_issues = len(clusters)

    user = f"""오늘은 {run_date} 입니다. 아래는 오늘 오전까지 수집한 한국 정치 관련 기사 {max_issues}개 이슈입니다.

{material}

이 자료만 근거로 오늘의 정치 데일리 브리핑을 작성하세요.

작성 지침:
- issues 는 제공된 이슈 순서를 유지하되, 자료가 부실해 쓸 내용이 없는 이슈는 빼도 됩니다.
- what_happened 에는 해석이 아니라 확인된 사실만 넣습니다.
- numbers 에는 기사에 등장한 수치(표결 결과, 지지율, 의석수, 예산액 등)를 빠짐없이 담습니다. 영상 자막 카드로 쓸 재료입니다.
- why_it_matters 는 '그래서 다음에 무엇이 달라지는가' 를 절차와 일정 중심으로 씁니다. 누가 유리한지가 아니라 **무엇이 정해지고 무엇이 남았는지**를. 일반론 말고 이 이슈에 한정해서.
- tomorrow_watch 에는 발표 예정 통계, 회의 일정처럼 **자료에 언급된** 예정 사항만 적습니다. 없으면 빈 배열."""

    return ANALYST_SYSTEM, user


def build_shared_context(cfg: Config, brief: DailyBrief) -> str:
    """블로그·영상 호출이 공유하는 system 블록 (캐시 대상)."""
    video = cfg.get("video", {}) or {}
    banned = video.get("banned_phrases", []) or []

    # 주소는 빼고 넘긴다. 블로그·대본은 주소를 쓰지 않고, 글 끝의 '참고한 기사' 목록은
    # 프로그램이 클러스터에서 직접 만든다. 넣어 봐야 세 호출의 입력만 불린다.
    payload = brief.model_dump()
    for issue in payload.get("issues", []):
        issue.pop("source_urls", None)
        issue.pop("source_ids", None)
    brief_json = json.dumps(payload, ensure_ascii=False, indent=2)

    return f"""당신은 정치 콘텐츠를 만드는 프로듀서입니다.
채널명은 "{video.get('channel_name', '정치 브리핑')}" 입니다.

시청자: {video.get('audience', '정치에 관심 있는 일반 시청자')}
톤앤매너: {video.get('tone', '차분하고 정확한 정보 전달. 어느 편도 들지 않는다')}
진행자 캐릭터: {video.get('persona', '정치 뉴스 해설자')}

지켜야 할 것:
- 아래 브리핑에 있는 사실과 수치만 씁니다. 없는 내용을 채워 넣지 마세요.
- **어느 정당·인물의 편도 들지 않습니다.** 브리핑에 양쪽 입장이 있으면 둘 다 싣고, 한쪽만 있으면 "○○ 측 입장은 확인되지 않았다" 고 밝힙니다.
- 누가 한 말은 반드시 주체를 붙여 씁니다("○○ 대표는 ~라고 말했습니다"). 주장을 사실처럼 쓰지 않습니다.
- 의혹·수사 중인 사건은 "혐의", "의혹" 이라는 말을 빼지 않습니다.
- 여론조사 수치를 쓸 때는 조사기관과 오차범위를 같이 말합니다. 오차범위 안의 차이를 "앞선다" 고 하지 않습니다.
- 어려운 용어(필리버스터, 재의요구권 등)는 처음 나올 때 한 번 짧게 풀어 줍니다.
- 다음 표현은 쓰지 마세요: {', '.join(banned) if banned else '(없음)'}
- 단정적 예측 대신 근거와 전망 주체를 밝힙니다.
- 모든 출력은 한국어입니다.

────────── 오늘의 브리핑 (JSON) ──────────
{brief_json}
──────────────────────────────────────"""


def build_blog_user(cfg: Config, regions: list[str] | None = None) -> str:
    blog = cfg.get("blog", {}) or {}
    if str(blog.get("platform", "naver")).lower() == "naver":
        return _blog_user_naver(cfg, blog, regions or [])
    return _blog_user_markdown(blog)


def _blog_user_markdown(blog: dict) -> str:
    min_chars = int(blog.get("min_chars", 1800))
    max_chars = int(blog.get("max_chars", 3500))

    return f"""위 브리핑을 바탕으로 블로그 글 한 편을 완성하세요.

- 분량: 본문 {min_chars}~{max_chars}자
- 구조: 도입(오늘 시장 한 문단) → 이슈별 H2 소제목 → 정리/체크포인트
- 수치는 표(마크다운 테이블)로 정리하면 읽기 좋습니다. 수치가 2개 이상인 이슈는 표를 쓰세요.
- 마지막에 '오늘의 체크포인트' 3줄 요약을 붙입니다.
- title 은 검색해서 들어올 만한 제목으로 짓되, 과장하거나 낚지 않습니다.
- 글 안에서 독자를 '여러분'으로 부르고, 존댓말로 씁니다.
- tags 는 5~8개. image_slots 는 빈 배열로 두세요."""


def _blog_user_naver(cfg: Config, blog: dict, regions: list[str] | None = None) -> str:
    """네이버 블로그는 검색 유입과 모바일 열람 비중이 커서 요구사항이 다르다.

    - 검색으로 들어온 사람은 스크롤을 거의 안 한다 → 결론을 맨 앞에
    - 대부분 휴대폰으로 본다 → 문단이 길면 읽지 않는다
    - 본문에 쓴 #해시태그가 그대로 블로그 태그로 등록된다
    """
    min_chars = int(blog.get("min_chars", 1800))
    max_chars = int(blog.get("max_chars", 3500))
    naver = blog.get("naver", {}) or {}
    tag_count = int(naver.get("tag_count", 20))
    image_slots = int(naver.get("image_slots", 3))
    para_max = int(naver.get("paragraph_max_chars", 120))

    region_rule = ""
    if regions:
        names = " · ".join(regions[:5])
        region_rule = f"""

■ 지역명 ({names})
- 위 지역이 오늘 이슈와 **실제로 관계있을 때만**(지방선거·지자체장·지역구 이슈) 제목이나
  소제목에 넣으세요. 관계없는 지역을 끼워 넣지는 마세요.
- tags 에도 그 지역명을 넣습니다."""

    return f"""위 브리핑을 바탕으로 **네이버 블로그**에 올릴 글 한 편을 완성하세요.
네이버 블로그의 특성에 맞춰야 하므로 아래 규칙을 정확히 지켜 주세요.

■ 대표 검색어 (focus_keyword) — 이 글의 유입을 책임지는 말
- 오늘 이슈 가운데 **사람들이 실제로 검색창에 칠 말** 하나를 고릅니다. 2~4어절.
  좋은 예: "필리버스터 종결 표결", "국무총리 인사청문회 일정". 나쁜 예: "정치"(너무 넓음), "오늘의 브리핑"(아무도 안 침).
- 고른 말은 **제목 앞쪽 · 첫 문단 100자 안 · 소제목 한 곳 이상**에 글자 그대로 넣습니다.
  변형("종결 표결 필리버스터")이 아니라 같은 표기로 넣어야 검색에 걸립니다.

■ 요약 3줄 (summary_lines)
- 본문 맨 앞에 얹습니다. 검색으로 들어온 사람은 이것만 보고 나가기도 합니다.
- 각 45자 내외. "무슨 일 / 숫자로 얼마 / 그래서 뭘 보면 되는지" 순서.

■ 마무리 질문 (closing_question)
- 글 끝에 붙일 물음 한 문장. 독자가 자기 상황을 떠올리게 하는 질문이 좋습니다.
- "댓글 부탁드립니다" 같은 구걸은 쓰지 않습니다.

■ 제목 (title)
- 위에서 고른 focus_keyword 를 **앞쪽에** 배치합니다. 검색 노출에 유리합니다.
- 25~35자. 날짜를 넣으면 좋습니다. 예: "본회의 통과 법안 3건 정리, 9월 6일 정치 브리핑"
- **정당·인물을 평가하는 말을 제목에 쓰지 않습니다.** "○○당 강행", "○○ 몽니" 같은 표현은 한쪽 말입니다.
- 과장·낚시성 표현은 쓰지 않습니다.

■ 첫 문단 (본문 맨 앞)
- 검색으로 들어온 사람은 스크롤하지 않습니다. **결론부터** 씁니다.
- 3줄 이내로 "오늘 무슨 일이 있었고, 그래서 어떻다"를 끝냅니다.

■ 그래서 나는? (takeaways)
- 2~3개. "자영업자라면", "청년이라면", "이번 선거 유권자라면" 처럼 **읽는 사람 유형**으로 시작합니다.
- 각 45자 내외로, 오늘 소식이 그 사람의 생활에 뜻하는 바나 확인할 일정을 한 줄로 적습니다.
- 누구를 지지하라거나 어느 쪽이 옳다고 쓰지 않습니다. "~이 달라질 수 있습니다", "~일정을 확인해 보세요" 정도로.

■ 본문 (body_markdown) — **짧게 쓰는 것이 핵심입니다**
- 분량 {min_chars}~{max_chars}자(공백 포함). **넘기지도 모자라지도 마세요.** 길면 아무도 끝까지 읽지 않고, 짧으면 검색에서 밀립니다.
- 오늘 이슈를 전부 길게 다루지 마세요. **가장 중요한 이슈 하나만** 깊게 씁니다.
- **같은 수치를 되풀이하지 마세요.** 핵심 숫자는 처음 나올 때 한 번만 또렷이 쓰고, 뒤에서는
  "이 상승률", "앞서 본 수치" 처럼 가리키기만 합니다. (2026-09-08 글은 29.5% 를 아홉 번
  적어 읽는 사람이 같은 이야기를 계속 듣는 느낌이었습니다.)
- 순서를 이렇게 잡습니다:
  1) 도입 2~3문장 — 결론부터
  2) 메인 이슈 `##` 소제목 2~3개 — 무슨 일 → 숫자 → 그래서 어떤 뜻인지
  3) `## 그 밖의 오늘 소식` — 나머지 이슈를 **한 줄씩** 불릿으로. 한 줄 45자 이내, 설명하지 말고 사실만.
  4) `## 오늘의 체크포인트` — 3줄 요약
- **한 문단은 2~3문장, {para_max}자 이내.** 휴대폰 화면에서 벽처럼 보이면 읽지 않습니다.
- 문단 사이는 반드시 빈 줄로 띄웁니다.
- 어려운 말은 **처음 나올 때 괄호로 짧게** 풉니다. 예: 재의요구권(국회가 통과시킨 법안을 대통령이 돌려보내는 권한).
- **양쪽 입장은 같은 분량으로** 씁니다. 여당 주장 세 문장에 야당 반응 한 마디면 기울어 보입니다.
  배경지식이 없는 사람이 첫 문단에서 막히면 그대로 나갑니다.
- `##` 소제목 가운데 **한 곳 이상에 focus_keyword 를 그대로** 씁니다.
- focus_keyword 를 본문 전체에 3~5회 자연스럽게 반복합니다. 억지로 끼워 넣지는 마세요.
- 소제목은 검색어처럼 씁니다. "정리" 보다 "필리버스터는 언제 끝나나" 가 낫습니다.
- 어제 글과 같은 문장을 쓰지 마세요. 표현을 매일 바꿔야 검색에서 중복으로 취급되지 않습니다.
- 수치가 2개 이상인 이슈는 마크다운 표로 정리합니다.
- 이미지 자리를 본문 흐름에 맞게 {image_slots}곳 넣습니다. 형식은 정확히 이렇게 씁니다:
  `[이미지: 어떤 이미지를 넣을지 설명]`
  같은 순서로 image_slots 배열에 담되, 그 자리가 **브리핑의 수치를 그림으로 보여주는 자리**라면
  datapoint_label 에 그 수치의 label 을 **글자 그대로** 적습니다(프로그램이 그 수치로 그림을
  자동 생성해 자리에 넣습니다). 현장 사진·화면 캡처처럼 수치가 아닌 자리는 빈 문자열로 둡니다.
  {image_slots}곳 중 적어도 한 곳은 수치 자리로 잡으세요.
  사진 자리에는 search_keywords 에 스톡 사진 사이트용 **영어 검색어** 2~4단어를 적습니다.
- 표는 꼭 필요할 때 하나만 씁니다. 휴대폰에서 표는 가로로 잘립니다.
- 독자를 '여러분'으로 부르고 존댓말로 씁니다. 딱딱한 보고서 문체는 피합니다.

■ 태그 (tags)
- {tag_count}개. 본문 하단에 해시태그로 붙일 것이며 네이버가 이를 태그로 인식합니다.
- 넓은 키워드(정치, 국회)와 좁은 키워드(필리버스터종결, 인사청문회일정)를 섞습니다. 정당·인물 이름은 그대로 태그로 씁니다.
- 띄어쓰기 없이 붙여 씁니다. # 기호는 빼고 단어만 담으세요.{region_rule}"""


def build_policy_messages(docs: list) -> tuple[str, str]:
    """정부 보도자료 요약. 원문에 없는 말을 보태지 않는 것이 가장 중요하다."""
    system = (
        "당신은 정부 보도자료를 일반 독자용으로 줄이는 편집자입니다.\n"
        "규칙:\n"
        "- 주어진 요약문에 있는 사실만 씁니다. 배경지식으로 보태거나 해석하지 마세요.\n"
        "- 숫자와 날짜는 그대로 옮깁니다.\n"
        "- 전문 용어는 쉬운 말로 바꾸되 뜻이 달라지면 안 됩니다.\n"
        "- **각 줄은 그 자체로 끝나는 완결된 문장**입니다. 한 문장을 세 줄로 쪼개지 마세요.\n"
        "- 한 줄 45자 내외, 존댓말, 단정적 전망 금지.\n"
        "- 1줄: 무엇이 발표됐는지 / 2줄: 숫자나 기준 / 3줄: 누구에게 어떤 영향인지."
    )
    blocks = []
    for d in docs:
        summary = " / ".join(d.summary or [])
        block = f"[번호 {d.news_id}] ({d.dept} · {d.date})\n제목: {d.title}\n부처 요약: {summary}"
        body = " ".join((getattr(d, "body", "") or "").split())[:1500]
        if body:
            block += f"\n보도자료 본문(첨부 문서에서 뽑음): {body}"
        blocks.append(block)
    user = ("아래 보도자료를 각각 3줄로 줄여 주세요. 번호(news_id)를 그대로 돌려주세요.\n\n"
            + "\n\n".join(blocks))
    return system, user


def stats_context(stats: dict | None) -> str:
    """영상 대본에 넘길 실거래 자료. 값은 프로그램이 센 것이라 그대로 인용하게 한다."""
    if not stats or not stats.get("districts"):
        return ""
    rows = "\n".join(
        f"- {r['name']}: {r['now']['count']}건 (전달 대비 {r['change']:+d}건), "
        f"평균 {r['now']['avg'] / 100_000_000:.1f}억"
        for r in stats["districts"][:5]
    )
    hot = "\n".join(
        f"- [{h['kind']}] {h['district']} {h['name']} {h['area']}㎡ "
        f"{h['amount'] / 100_000_000:.1f}억 (이전 {h['before'] / 100_000_000:.1f}억, {h['pct']:+.1f}%)"
        for h in (stats.get("highlights") or [])[:5]
    )
    return f"""

────────── 실거래 자료 ({stats.get('month_label', '')}) ──────────
국토교통부 신고 자료를 우리가 직접 집계한 값입니다. **숫자를 바꾸지 말고 그대로 인용하세요.**
전체 신고 매매 {stats.get('total', 0)}건 ({stats.get('before_label', '')} {stats.get('total_before', 0)}건)

지역별
{rows}
{"" if not hot else "눈에 띄는 거래" + chr(10) + hot}
※ 이 수치는 브리핑에 없는 자료입니다. 쓸 때는 "국토교통부 실거래가 신고 자료 기준" 이라고 밝히세요.
※ 신고가는 '같은 단지 같은 면적의 지난 거래보다 높다' 는 뜻입니다. 지역 전체가 올랐다는 뜻이 아닙니다.
──────────────────────────────────────"""


def civics_context(data: dict | None) -> str:
    """영상 대본에 넘길 국회·여론조사 자료. 값은 프로그램이 센 것이라 그대로 인용하게 한다."""
    if not data:
        return ""
    bills, plen, polls = data.get("bills") or {}, data.get("plenary") or {}, data.get("polls") or []
    lines = []
    if bills.get("latest"):
        head = (f"최근 {data.get('days', 7)}일 국회의원 발의 법률안 {bills['count']}건" if bills.get("count") is not None
                else "최근 발의 법률안(견본 5건, 전체 건수는 모름)")
        lines.append(head + " — " + ", ".join(b["name"] for b in bills["latest"][:3]))
    if plen.get("items"):
        said = " · ".join(f"{k} {v}건" for k, v in (plen.get("by_result") or {}).items())
        lines.append("본회의 처리 법률안: " + said)
        lines += [f"  - {p['name']} ({p['result']}, {p['date']}"
                  + (f", 찬성 {p['yes']}·반대 {p['no']}·기권 {p['blank']}" if p.get("yes") is not None else "") + ")"
                  for p in plen["items"][:6]]
    if polls:
        lines.append(f"등록된 선거 여론조사 {len(polls)}건:")
        lines += [f"  - {p.get('title', '')} — {p.get('summary', '')}" for p in polls[:5]]
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"""

────────── 국회·여론조사 자료 (우리가 직접 센 값, {data.get('since', '')} 이후) ──────────
{body}
※ 열린국회정보·선거여론조사심의위원회 자료를 프로그램이 직접 센 값입니다. **숫자를 바꾸지 말고 그대로 인용하세요.**
※ 여론조사를 말할 때는 위 개요(조사기관·조사 기간·표본·응답률·오차범위)를 함께 말합니다. 오차범위 안의 차이는 "앞선다" 고 하지 않습니다.
──────────────────────────────────────"""


def build_video_user(cfg: Config, stats: dict | None = None, civics: dict | None = None) -> str:
    video = cfg.get("video", {}) or {}
    shorts_sec = int(video.get("shorts_seconds", 60))
    long_min = float(video.get("longform_minutes", 8))
    cpm = int(video.get("speaking_rate_cpm", 330))
    cta = video.get("cta", "구독과 알림 설정 부탁드립니다.")

    shorts_chars = int(shorts_sec / 60 * cpm)
    long_chars = int(long_min * cpm)

    return f"""위 브리핑{"과 아래 자료" if (stats or civics) else ""}를 바탕으로 오늘 촬영할 영상 두 편의 제작 자료를 만드세요.
{stats_context(stats)}{civics_context(civics)}

■ 쇼츠 ({shorts_sec}초)
- 브리핑에서 **가장 임팩트 있는 이슈 하나만** 고릅니다. 여러 개 담지 마세요.
- 발화 분량 합계 약 {shorts_chars}자 (분당 {cpm}자 기준). 이 분량을 넘기지 마세요.
- lines 는 화면에 한 번에 뜨는 자막 단위로 끊습니다. 한 줄 18자 이내.
- at 은 00:00 부터 시작해 실제 읽는 속도에 맞춰 매깁니다.
- visual 에는 그 구간에 무엇을 띄울지 적습니다. 촬영/편집자가 그대로 보고 작업할 수 있게 구체적으로.
  나쁜 예: "관련 화면". 좋은 예: "국회의사당 전경 스톡 + 좌하단에 '찬성 178표' 자막 카드".
  **정치인 얼굴 사진·영상은 지시하지 마세요.** 초상권과 편집 오해 문제가 있어 건물·장소·자막 카드로 갑니다.

■ 롱폼 ({long_min}분)
- 발화 분량 합계 약 {long_chars}자.
- sections 는 4~6개. 각 섹션 at 은 누적 타임코드로 매깁니다.
- script 는 실제로 읽을 원고입니다. 구어체로, 한 문장을 짧게 씁니다. 개조식으로 쓰지 마세요.
- broll 은 구간마다 **2개까지**. 짧은 명사구로 적고, 스톡으로 될 것은 '스톡:' 을 앞에 붙입니다.
  나쁜 예: "관련 화면". 좋은 예: "스톡: 국회의사당 전경". 인물 얼굴은 지시하지 않습니다.
- graphics 는 구간마다 **1~2개**. 자막 카드로 띄울 수치·문구만 짧게. 브리핑 numbers 를 씁니다.
- thumbnail_texts 는 썸네일에 크게 박을 문구 **3개**입니다. 12자 이내, 숫자를 넣으면 좋습니다.
- title_candidates 도 **3개**면 충분합니다. 서로 다른 각도로 지으세요.
- outro 는 다음 문장으로 마무리합니다: "{cta}"

설명란과 고정 댓글은 쓰지 마세요. 챕터 타임코드도 출처 주소도 이미 우리가 가진 값이라
프로그램이 만듭니다."""


# ── 자료 직렬화 ──────────────────────────────────────────────


ARTICLE_LIMIT = 6      # 이슈 하나에 넣어 줄 기사 수


def article_ids(clusters: list[Cluster]) -> dict[str, str]:
    """프롬프트에 붙일 기사 번호 → 실제 주소.

    format_clusters 와 **같은 규칙**으로 번호를 매깁니다. 모델은 주소 대신 이 번호만
    돌려주면 되고, 프로그램이 여기서 주소를 되찾습니다.
    """
    out: dict[str, str] = {}
    for index, cluster in enumerate(clusters, start=1):
        for n, article in enumerate(cluster.articles[:ARTICLE_LIMIT], start=1):
            out[f"{index}-{n}"] = article.url
    return out


def format_clusters(clusters: list[Cluster], excerpt: int = 1200) -> str:
    """클러스터를 프롬프트에 넣을 텍스트로 변환.

    기사 주소는 넣지 않고 **번호**만 붙입니다. 구글뉴스 주소는 한 개가 220자나 되는데,
    모델은 그걸 읽고 그대로 되돌려 적을 뿐이라 오갈 때마다 값을 두 번 냅니다
    (2026-09-07 브리핑 출력의 20%가 주소였습니다).
    """
    blocks: list[str] = []

    for index, cluster in enumerate(clusters, start=1):
        lead = cluster.lead
        header = (
            f"### 이슈 {index}. {lead.title}\n"
            f"- 보도 매체 {cluster.size}건: {', '.join(cluster.publishers[:8])}\n"
            f"- 분류: {', '.join(cluster.categories) or '미분류'}"
        )

        article_lines: list[str] = []
        for n, article in enumerate(cluster.articles[:ARTICLE_LIMIT], start=1):
            when = article.published.strftime("%m-%d %H:%M") if article.published else "시각미상"
            source = article.publisher or article.feed_name
            text = _truncate(article.best_text, excerpt)
            article_lines.append(
                f"* ({index}-{n}) [{source} / {when}] {article.title}\n"
                f"  내용: {text or '(요약 없음)'}"
            )

        blocks.append(header + "\n" + "\n".join(article_lines))

    return "\n\n".join(blocks)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


PACK_EXCERPT_CHARS = 500     # 붙여넣기 묶음의 기사 발췌 길이. 공개 저장소에 올라가므로 본문 전문(1,200자)은 넣지 않는다


def answer_schemas() -> str:
    """답으로 받을 JSON 세 덩이의 스키마. 모델이 이 틀대로 적어야 프로그램이 읽는다."""
    from .models import BlogPost, DailyBrief, VideoPack

    parts = []
    for tag, model in (("brief", DailyBrief), ("blog", BlogPost), ("script", VideoPack)):
        parts.append(f"[{tag}]\n" + json.dumps(model.model_json_schema(), ensure_ascii=False))
    return "\n\n".join(parts)


def build_prompt_pack(cfg: Config, clusters: list[Cluster], run_date: str) -> str:
    """0원 방식의 붙여넣기 묶음 — claude.ai 채팅에 **한 번** 붙여 넣으면 브리핑·블로그·대본 JSON 세 덩이가 온다.

    2026-09-10 사용자 선택. API 크레딧(하루 $0.5) 대신 구독 채팅을 쓴다. 답은 저장소 이슈에 붙여 넣으면
    「답 받기」 워크플로가 읽어 글·카드·대본을 만든다(`rebrief.answer`).
    기사 발췌는 `PACK_EXCERPT_CHARS` 까지만 — 이 파일은 공개 저장소에 올라간다.
    """
    system, user = build_brief_messages(cfg, clusters, run_date, excerpt=PACK_EXCERPT_CHARS)
    video = cfg.get("video", {}) or {}
    blog_user = build_blog_user(cfg)
    video_user = build_video_user(cfg)

    return f"""{system}

{user}

────────── 그다음 할 일 ──────────

위 브리핑을 다 만들었으면, **같은 답 안에서 이어서** 아래 두 가지도 만드세요.
(블로그·대본은 방금 만든 브리핑만 근거로 씁니다. 채널명 "{video.get('channel_name', '정치 브리핑')}",
시청자 "{video.get('audience', '')}", 톤 "{video.get('tone', '')}".)

[블로그 글]
{blog_user}

[영상 대본]
{video_user}

────────── 답의 형식 (꼭 지키세요) ──────────

답은 설명 없이 **JSON 코드 블록 세 개**만 적습니다. 각 블록의 첫 줄은 정확히 ```json brief / ```json blog / ```json script 입니다.
각 블록은 아래 스키마를 그대로 따르는 JSON 객체 하나입니다 (설명·주석·말줄임표 없이, 값은 전부 한국어).
source_ids 에는 자료의 기사 번호("1-2" 꼴)를 그대로 적습니다.

{answer_schemas()}
"""


# ── 주간 결산 ────────────────────────────────────────────────


def build_weekly_messages(cfg: Config, days: list[dict], week_label: str) -> tuple[str, str]:
    """일주일치 data.json 을 묶어 결산 글을 부탁한다. 하루치 브리핑보다 압축해서 넘긴다."""
    video = cfg.get("video", {}) or {}
    blog = cfg.get("blog", {}) or {}
    banned = video.get("banned_phrases", []) or []
    naver = blog.get("naver", {}) or {}
    tag_count = int(naver.get("tag_count", 20))
    min_chars = int(blog.get("weekly_min_chars", 1500))
    max_chars = int(blog.get("weekly_max_chars", 3000))

    compact = [
        {
            "date": d["date"],
            "headline": d.get("headline", ""),
            "market_temperature": d.get("market_temperature", ""),
            "issues": [
                {"title": i.get("title"), "category": i.get("category"),
                 "one_liner": i.get("one_liner"), "numbers": i.get("numbers", []),
                 **({"days_seen": i["days_seen"]} if i.get("days_seen") else {})}
                for i in d.get("issues", [])
            ],
        }
        for d in days
    ]
    streaks = [s for d in days for s in d.get("streaks", [])]
    streak_note = ""
    if streaks:
        lines_ = "\n".join(f"- {s['title']} — {len(s['dates'])}일 ({', '.join(s['dates'])})" for s in streaks)
        streak_note = f"""

여러 날 반복된 이슈 (같은 사건이 날짜만 바뀌어 다시 나온 것입니다. 각각 세지 말고 흐름으로 묶으세요):
{lines_}"""
    system = f"""당신은 정치 콘텐츠를 만드는 프로듀서입니다.
채널명은 "{video.get('channel_name', '정치 브리핑')}" 입니다.

시청자: {video.get('audience', '정치에 관심 있는 일반 시청자')}
톤앤매너: {video.get('tone', '차분하고 정확한 정보 전달. 어느 편도 들지 않는다')}

지켜야 할 것:
- 아래 일주일치 브리핑에 있는 사실과 수치만 씁니다. 없는 내용을 채워 넣지 마세요.
- 다음 표현은 쓰지 마세요: {', '.join(banned) if banned else '(없음)'}
- 단정적 예측 대신 근거와 전망 주체를 밝힙니다.
- 모든 출력은 한국어입니다.

────────── {week_label} 브리핑 모음 (JSON, 날짜순) ──────────
{json.dumps(compact, ensure_ascii=False, indent=1)}
──────────────────────────────────────{streak_note}"""

    user = f"""위 일주일치 브리핑으로 **주간 결산 글** 한 편을 완성하세요. 네이버 블로그에 올립니다.

■ five_lines
- 이번 주를 다섯 줄로 요약합니다. 각 줄 40자 이내, 가능하면 숫자를 넣습니다.
- 요일 순이 아니라 **중요한 순**입니다.

■ 본문 (body_markdown)
- 분량 {min_chars}~{max_chars}자. 한 문단 2~3문장, 문단 사이 빈 줄.
- 첫 문단에 결론(이번 주 시장을 한 문장으로)을 씁니다.
- `##` 소제목 3~5개. **날짜별이 아니라 주제별**로 묶습니다. 같은 이슈가 여러 날 나왔으면
  흐름(무엇이 바뀌었는지)을 짚습니다.
- 수치가 2개 이상인 주제는 마크다운 표로 정리합니다. 표에는 날짜 열을 둡니다.
- 마지막 소제목은 '다음 주 볼 것'으로 하고 next_week_watch 와 같은 내용을 넣습니다.
- 독자를 '여러분'으로 부르고 존댓말로 씁니다.

■ 태그 (tags)
- {tag_count}개. 띄어쓰기 없이, # 기호 없이 단어만."""
    return system, user


def build_weekly_prompt_pack(cfg: Config, days: list[dict], week_label: str) -> str:
    """API 키가 없을 때 챗봇에 붙여넣을 수 있게 두 메시지를 하나의 문서로 묶는다."""
    system, user = build_weekly_messages(cfg, days, week_label)
    return f"""# {week_label} 주간 결산 — 프롬프트 팩

API 키가 없어 자동 생성을 건너뛰었습니다. 아래를 통째로 복사해 챗봇에 붙여넣으면 같은 결과를
얻을 수 있습니다. (WeeklyReview 형식: title, slug, meta_description, five_lines, body_markdown,
next_week_watch, tags)

---

{system}

---

{user}
"""


def build_monthly_messages(cfg: Config, days: list[dict], month_label_: str,
                           trades: dict | None = None) -> tuple[str, str]:
    """한 달치 브리핑 + 그달 확정된 실거래를 묶어 월간 결산을 부탁한다.

    주간과 다른 점은 두 가지입니다. 하나는 **우리가 직접 센 실거래**를 함께 넘긴다는 것,
    다른 하나는 날짜가 아니라 **무엇이 언제 바뀌었는지**를 묻는다는 것입니다. 한 달이면
    같은 이슈가 열 번도 나오므로 나열하면 읽히지 않습니다.
    """
    video = cfg.get("video", {}) or {}
    blog = cfg.get("blog", {}) or {}
    banned = video.get("banned_phrases", []) or []
    naver = blog.get("naver", {}) or {}
    tag_count = int(naver.get("tag_count", 20))
    min_chars = int(blog.get("monthly_min_chars", 2000))
    max_chars = int(blog.get("monthly_max_chars", 4000))

    compact = [
        {
            "date": d["date"],
            "headline": d.get("headline", ""),
            "issues": [{"title": i.get("title"), "category": i.get("category"),
                        "numbers": i.get("numbers", [])}
                       for i in d.get("issues", [])[:3]],
        }
        for d in days
    ]
    trade_note = ""
    if trades:
        trade_note = f"""

────────── 우리가 직접 센 실거래 (JSON) ──────────
{json.dumps(trades, ensure_ascii=False, indent=1)}
──────────────────────────────────────
이 수치는 국토교통부 실거래가 신고 자료를 우리가 직접 집계한 것입니다. 기사에서 온 값이
아니므로 **숫자를 바꾸지 말고 그대로 인용**하세요. 신고 기한 때문에 확정된 달이 이달보다
두 달쯤 앞섭니다 — 어느 달 수치인지 반드시 밝혀 쓰세요."""

    system = f"""당신은 정치 콘텐츠를 만드는 프로듀서입니다.
채널명은 "{video.get('channel_name', '정치 브리핑')}" 입니다.

시청자: {video.get('audience', '정치에 관심 있는 일반 시청자')}
톤앤매너: {video.get('tone', '차분하고 정확한 정보 전달. 어느 편도 들지 않는다')}

지켜야 할 것:
- 아래 한 달치 브리핑과 실거래 수치에 있는 사실만 씁니다. 없는 내용을 채워 넣지 마세요.
- 다음 표현은 쓰지 마세요: {', '.join(banned) if banned else '(없음)'}
- 단정적 예측 대신 근거와 전망 주체를 밝힙니다.
- 모든 출력은 한국어입니다.

────────── {month_label_} 브리핑 모음 (JSON, 날짜순) ──────────
{json.dumps(compact, ensure_ascii=False, indent=1)}
──────────────────────────────────────{trade_note}"""

    user = f"""위 한 달치 자료로 **월간 결산 글** 한 편을 완성하세요. 네이버 블로그에 올립니다.

■ month_lines
- 이달을 다섯 줄로 요약합니다. 각 줄 40자 이내, 가능하면 숫자를 넣습니다.
- 날짜 순이 아니라 **중요한 순**입니다.

■ turning_points
- 이달 안에서 **흐름이 바뀐 지점**을 2~4개 집습니다. "언제부터 무엇이 달라졌다" 형태로 씁니다.
- 한 달 내내 같은 이야기였다면 그렇게 쓰세요. 없는 전환점을 만들지 마세요.

■ 본문 (body_markdown)
- 분량 {min_chars}~{max_chars}자. 한 문단 2~3문장, 문단 사이 빈 줄.
- 첫 문단에 결론(이달 시장을 한 문장으로)을 씁니다.
- `##` 소제목 4~6개. **날짜별이 아니라 주제별**로 묶습니다.
- 실거래 수치를 넘겨받았다면 소제목 하나를 통째로 거기에 씁니다. 표로 정리하세요.
- 마지막 소제목은 '다음 달 볼 것'으로 하고 next_month_watch 와 같은 내용을 넣습니다.
- 독자를 '여러분'으로 부르고 존댓말로 씁니다.

■ 태그 (tags)
- {tag_count}개. 띄어쓰기 없이, # 기호 없이 단어만."""
    return system, user


def build_monthly_prompt_pack(cfg: Config, days: list[dict], month_label_: str,
                              trades: dict | None = None) -> str:
    """API 키가 없을 때 챗봇에 붙여넣을 수 있게 두 메시지를 하나의 문서로 묶는다."""
    system, user = build_monthly_messages(cfg, days, month_label_, trades)
    return f"""# {month_label_} 월간 결산 — 프롬프트 팩

API 키가 없어 자동 생성을 건너뛰었습니다. 아래를 통째로 복사해 챗봇에 붙여넣으면 같은 결과를
얻을 수 있습니다. (MonthlyReview 형식: title, slug, meta_description, month_lines, body_markdown,
turning_points, next_month_watch, tags)

---

{system}

---

{user}
"""



# ── 인물 조사 ────────────────────────────────────────────────

# 전기(傳記)는 뉴스 브리핑보다 '기억으로 쓰기' 유혹이 큽니다. 유명한 사람일수록 모델이 아는 것이
# 많고, 그중 일부는 틀립니다. 그래서 자료 번호를 문장마다 달게 하고, 위키백과에만 있는 사실은
# 따로 표시하게 합니다. 논란은 의혹·해명·현재 상태 세 칸을 반드시 채우게 해 확정처럼 쓰는 것을 막습니다.
PROFILE_SYSTEM = """당신은 정치 배경지식 영상을 준비하는 리서처입니다. 시청자는 이 정치인을 잘 모르는 사람입니다.
아래 자료만으로 '이 사람이 누구이고 어떤 길을 걸어왔는지' 를 일목요연하게 정리합니다.

절대 규칙:
1. **자료에 적힌 내용만** 씁니다. 기억이나 배경지식으로 사실·날짜·직함·수치를 보태지 마세요. 유명한 사람이라도 마찬가지입니다.
2. 모든 항목에 근거 자료 번호(A, W, B, N0-3, N2-7 …)를 답니다. 번호를 댈 수 없는 문장은 쓰지 않습니다.
3. [A](국회 공식 기록)와 [B](발의법률안)는 1차 자료, [N](기사 제목)은 보도, [W](위키백과)는 누구나 고칠 수 있는 2차 자료입니다. [W] 에만 있고 다른 자료로 뒷받침되지 않는 사실은 cautions 에 "위키백과에만 있음: …" 으로 적습니다.
4. 기사 **제목**만 있고 본문이 없습니다. 제목이 암시하는 것을 사실로 부풀리지 마세요. 제목이 말하는 만큼만 씁니다.
5. **어느 편도 들지 않습니다.** 옳고 그름을 판정하는 말("잘했다", "무리수", "꼼수", "배신", "변절", "철새")을 쓰지 않습니다. 당적 변경은 날짜와 정당 이름으로만 적습니다.
6. **주장과 사실을 구분합니다.** 누가 한 말은 "○○은 ~라고 말했다" 로 주체를 밝힙니다.
7. **논란·의혹은 세 칸을 모두 채웁니다** — 무엇이 제기됐는지, 당사자가 뭐라고 했는지, 지금 어떤 상태인지. 해명이 자료에 없으면 "해명은 자료에 없음" 이라고 씁니다. 유죄 판결이 자료에 명시되지 않은 사건을 확정처럼 쓰지 않습니다.
8. 여론조사 수치는 조사기관·기간·오차범위가 자료에 있을 때만 옮깁니다.
9. 나이·재직 여부는 오늘 날짜 기준으로 계산하되, 자료에 없는 현재 직함을 추정하지 않습니다.
10. 단정적 예측을 하지 않습니다.
11. 모든 출력은 한국어이고, 시청자가 읽는 글이므로 쉬운 말로 씁니다."""


def build_profile_messages(cfg: Config, materials_text: str, name: str, today: str) -> tuple[str, str]:
    channel = cfg.get("video.channel_name", "") or ""
    user = (
        f"오늘은 {today} 입니다. 정치인 「{name}」 의 배경지식 정리를 만들어 주세요."
        + (f" 채널 「{channel}」 의 영상 준비 자료입니다." if channel else "")
        + "\n\n요청:\n"
        "- timeline: 오래된 것부터 10~25개. 당선·낙선·당적·직책·큰 사건 위주.\n"
        "- chapters: 발자취를 3~6개 장으로 나누고, 장마다 3~5문장 요약과 핵심 사실.\n"
        "- controversies: 자료에 있는 논란만, 의혹·해명·현재 상태 세 칸 모두.\n"
        "- recent: 최근 기사 제목([N0])에서 지금 무슨 일이 있는지.\n"
        "- outline: 8~12분 영상 구성안. 부분마다 시간(초)과 말할 것.\n"
        "- title_candidates: 낚시·평가어 없는 제목 3개.\n"
        "- cautions·unknowns: 확인이 필요한 곳과 자료에 없는 것.\n\n"
        "=== 자료 ===\n\n" + materials_text
    )
    return PROFILE_SYSTEM, user


def build_profile_pack(cfg: Config, materials_text: str, name: str, today: str) -> str:
    """모델을 부르지 않을 때 사람이 다른 AI 에 그대로 붙여 넣는 자료묶음."""
    system, user = build_profile_messages(cfg, materials_text, name, today)
    return (f"# {name} 인물 조사 자료묶음 ({today})\n\n"
            "아래 글을 통째로 복사해 AI(Claude·ChatGPT 등)에 붙여 넣으면 정리 글이 나옵니다.\n"
            "인증키가 있으면 `python -m rebrief profile \"이름\"` 이 이 일을 대신합니다.\n\n"
            "---\n\n## 지시\n\n" + system + "\n\n## 요청과 자료\n\n" + user)
