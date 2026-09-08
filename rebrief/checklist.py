"""발행 전 점검표 — 흩어진 경고를 한 장에 모으고, 프롬프트로만 금지했던 것을 실제로 검사한다.

LLM 을 부르지 않는다. 산출물 텍스트와 설정만 본다.
  ✅ 통과   ⚠️ 확인 필요 (발행은 가능)   ❌ 막힘 (발행 전에 고쳐야 함)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


@dataclass
class Item:
    key: str
    level: str
    title: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return ICON[self.level]


def _find_phrases(text: str, phrases: list[str]) -> list[str]:
    hits = []
    flat = " ".join((text or "").split())
    for p in phrases:
        p = p.strip()
        if p and p in flat:
            hits.append(p)
    return hits


def long_sentences(markdown_text: str, limit: int = 90) -> list[str]:
    """너무 긴 문장들. 표·소제목·인용은 빼고 본문 문장만 센다."""
    out: list[str] = []
    for block in _body_blocks(markdown_text):
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            flat = " ".join(sentence.split())
            if len(flat) > limit:
                out.append(flat)
    return out


def long_paragraphs(markdown_text: str, limit: int = 320) -> list[str]:
    """한 문단이 너무 길면 휴대폰에서 벽처럼 보인다."""
    return [b for b in _body_blocks(markdown_text) if len(b) > limit]


def _body_blocks(markdown_text: str) -> list[str]:
    blocks: list[str] = []
    for raw in (markdown_text or "").split("\n\n"):
        block = " ".join(raw.split())
        if not block or block.startswith(("#", ">", "|", "-", "*", "!", "[이미지")):
            continue
        blocks.append(block)
    return blocks


# 여론조사를 인용한 글이 갖춰야 할 것 (공직선거법 제108조 ⑥항의 공표 요건을 글 기준으로 줄인 것).
# 기관 이름은 자주 인용되는 곳만 적었다. 모르는 기관이면 '조사기관' 낱말이라도 있으면 통과.
POLL_WORDS = ("여론조사", "지지율", "긍정 평가", "부정 평가", "국정수행", "정당 지지")
POLL_AGENCIES = ("갤럽", "리얼미터", "NBS", "엔비에스", "전국지표조사", "한국리서치", "케이스탯",
                 "코리아리서치", "에스티아이", "조원씨앤아이", "여론조사꽃", "미디어토마토",
                 "알앤써치", "조사기관", "조사 기관")


def poll_citations(text: str) -> list[str]:
    """여론조사 수치를 썼는데 조사 개요가 빠졌으면 빠진 항목 목록. 안 썼거나 다 갖췄으면 빈 목록.

    선거법은 여론조사를 **공표·인용** 할 때 조사기관·조사기간·표본·응답률·오차범위 등을
    함께 밝히라고 한다. 블로그 글도 인용이다. 수치만 덜렁 옮기면 글쓴이가 책임진다.
    """
    flat = " ".join((text or "").split())
    if not any(w in flat for w in POLL_WORDS) or "%" not in flat:
        return []
    missing = []
    if not any(a in flat for a in POLL_AGENCIES):
        missing.append("조사기관")
    if "오차범위" not in flat and "오차 범위" not in flat and "±" not in flat:
        missing.append("오차범위")
    if not re.search(r"\d{1,2}[~∼–-]\d{1,2}일|\d{1,2}일(?:부터|까지)|조사 기간|조사기간|실시한", flat):
        missing.append("조사 기간")
    if not re.search(r"\d[\d,]*\s*명|표본", flat):
        missing.append("표본 크기")
    if "응답률" not in flat:
        missing.append("응답률")
    return missing


def election_blackout(date_str: str, elections: list[dict] | None) -> tuple[str, str] | None:
    """선거일 전 6일부터 선거일까지면 (선거 이름, 선거일). 아니면 None.

    공직선거법 108조 ① — 선거일 전 6일부터 투표 마감까지 새 여론조사 결과를 공표·인용할 수 없다.
    날짜가 잘못 적힌 항목은 조용히 건너뛴다 — 설정 한 줄 때문에 점검표가 죽으면 안 된다.
    """
    from datetime import date, timedelta

    try:
        today = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return None
    for e in elections or []:
        try:
            day = date.fromisoformat(str(e.get("date", "")))
        except (TypeError, ValueError):
            continue
        if day - timedelta(days=6) <= today <= day:
            return str(e.get("name", "선거")), day.isoformat()
    return None


def side_balance(text: str, sides: dict[str, list[str]]) -> dict[str, int]:
    """진영별 언급 수. 긴 별칭부터 세고 지워서 '더불어민주당' 안의 '민주당' 을 두 번 세지 않는다."""
    flat = " ".join((text or "").split())
    counts = {name: 0 for name in sides}
    aliases = sorted(((a, name) for name, al in sides.items() for a in (al or []) if a),
                     key=lambda x: len(x[0]), reverse=True)
    for alias, name in aliases:
        n = flat.count(alias)
        if n:
            counts[name] += n
            flat = flat.replace(alias, " ")
    return counts


def long_captions(pack, max_chars: int = 16, max_lines: int = 2) -> list[str]:
    """두 줄에 담기지 않는 쇼츠 자막 컷. 화면에서 글자가 작아지거나 넘친다."""
    from .render import wrap_caption

    out: list[str] = []
    for i, line in enumerate(pack.shorts.lines, start=1):
        wrapped = wrap_caption(line.text, max_chars, max_lines)
        if any(len(part) > max_chars for part in wrapped.split("\n")):
            out.append(f"{i}컷 · {' '.join(line.text.split())}")
    return out


def keyword_placement(post, head_chars: int = 120, title_head: int = 15) -> tuple[str, list[str]]:
    """대표 검색어가 제목·첫 문단·소제목에 들어갔는지. (검색어, 빠진 자리 목록)"""
    from .render import outline_from_markdown

    keyword = " ".join((getattr(post, "focus_keyword", "") or "").split())
    if not keyword:
        return "", []
    title = " ".join((post.title or "").split())
    body = " ".join((post.body_markdown or "").split())
    missing: list[str] = []
    if keyword not in title:
        missing.append("제목")
    elif title.find(keyword) > title_head:
        missing.append("제목 앞쪽(지금은 뒤쪽)")
    if keyword not in body[:head_chars]:
        missing.append(f"첫 {head_chars}자")
    if not any(keyword in h for h in outline_from_markdown(post.body_markdown)):
        missing.append("소제목")
    return keyword, missing


def overlap_with_previous(body_markdown: str, prev_bodies, threshold: float = 0.8) -> tuple[float, list[str]]:
    """어제·그제 글과 사실상 같은 문장의 비율. 매일 같은 문장을 쓰면 검색에서 중복으로 취급된다."""
    from .cluster import similarity

    mine = _sentences(body_markdown)
    if not mine or not prev_bodies:
        return 0.0, []
    old = [s for _, text in prev_bodies for s in _sentences(text)]
    if not old:
        return 0.0, []
    hits = [m for m in mine if any(similarity(m, o) >= threshold for o in old)]
    return round(len(hits) / len(mine), 3), hits[:3]


def _sentences(markdown_text: str, min_len: int = 15) -> list[str]:
    out: list[str] = []
    for block in _body_blocks(markdown_text):
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            flat = " ".join(sentence.split())
            if len(flat) >= min_len:
                out.append(flat)
    return out


def build(cfg: Config, *, brief=None, post=None, pack=None, checks=None,
          link_status=None, warnings=None, llm_used: bool = True,
          empty_photo_slots: int = 0, repeats=None, prev_bodies=None,
          date: str = "") -> list[Item]:
    items: list[Item] = []
    video = cfg.get("video", {}) or {}
    blog = cfg.get("blog", {}) or {}
    naver = blog.get("naver", {}) or {}
    banned = [b for b in (video.get("banned_phrases", []) or []) if b]

    if not llm_used:
        items.append(Item("llm", FAIL, "요약·대본이 만들어지지 않았습니다",
                          "prompt-pack.md 를 챗봇에 붙여넣어 직접 만들거나, 키·한도를 확인한 뒤 다시 실행하세요."))
        return items

    # 1) 숫자 검산
    missing = [c for c in (checks or []) if c.status == "미확인"]
    if missing:
        items.append(Item("numbers", WARN, f"수치 {len(missing)}건이 기사 원문에서 확인되지 않음",
                          "아래 '원문' 링크를 눌러 기사에서 직접 찾아보세요. 기사에 없는 값이면 글에서 빼는 게 안전합니다. "
                          "[오늘의 정리 열기](brief.html)",
                          [f"{c.issue} · {c.label} **{c.display}**" + (f" — [원문]({c.url})" if c.url else "")
                           for c in missing]))
    elif checks:
        items.append(Item("numbers", OK, f"수치 {len(checks)}건 모두 기사 원문에서 확인"))

    # 2) 출처 링크
    dead = [u for u, st in (link_status or {}).items() if not st.ok]
    if dead:
        items.append(Item("links", WARN, f"출처 링크 {len(dead)}개가 열리지 않음",
                          "'기사 원문' 페이지의 ⚠️ 표시를 보고 링크를 바꾸거나 빼세요. [기사 원문 열기](sources.html)", dead[:5]))
    elif link_status:
        items.append(Item("links", OK, f"출처 링크 {len(link_status)}개 모두 정상"))

    # 3) 금지 표현 — 프롬프트로 금지했지만 실제로 안 썼는지는 여기서 본다
    if banned:
        texts = []
        if post is not None:
            texts.append(("블로그", f"{post.title}\n{post.body_markdown}"))
        if pack is not None:
            texts.append(("쇼츠", "\n".join(l.text for l in pack.shorts.lines) + "\n" + pack.shorts.hook))
            texts.append(("롱폼", pack.longform.cold_open + "\n" + "\n".join(s.script for s in pack.longform.sections)
                          + "\n" + pack.longform.outro))
        hits = [f"{where}: '{p}'" for where, t in texts for p in _find_phrases(t, banned)]
        if hits:
            items.append(Item("banned", FAIL, f"금지 표현 {len(hits)}건 발견", "해당 문장을 고친 뒤 발행하세요.", hits))
        elif texts:
            items.append(Item("banned", OK, "금지 표현 없음"))

    # 3-2) 여론조사 표기 — 수치를 인용했으면 조사 개요가 같이 있어야 한다
    if post is not None:
        gaps = poll_citations(f"{post.title}\n{post.body_markdown}")
        if gaps:
            items.append(Item("poll", WARN, f"여론조사를 인용했는데 {'·'.join(gaps)}이(가) 빠졌습니다",
                              "선거법은 여론조사를 인용할 때 조사기관·조사 기간·표본·응답률·오차범위를 함께 밝히라고 합니다. "
                              "기사 원문에서 찾아 한 줄로 덧붙이거나, 없으면 수치를 빼세요. "
                              "글 끝의 '자세한 사항은 중앙선거여론조사심의위원회 홈페이지 참조' 는 프로그램이 넣습니다."))
        elif any(w in post.body_markdown for w in POLL_WORDS):
            items.append(Item("poll", OK, "여론조사 인용 표기 갖춤"))

    # 3-3) 선거 기간 — D-6 부터 선거일까지는 새 여론조사 결과를 공표·인용할 수 없다
    hit = election_blackout(date, cfg.get("elections", []) or []) if date else None
    if hit and post is not None:
        name, day = hit
        body = f"{post.title}\n{post.body_markdown}"
        has_poll = any(w in body for w in POLL_WORDS) and "%" in body
        if has_poll:
            items.append(Item("election", FAIL, f"{name}({day}) 여론조사 공표 금지 기간에 조사 수치가 들어 있습니다",
                              "선거일 전 6일부터 투표 마감까지는 새 여론조사 결과를 공표·인용할 수 없습니다 "
                              "(공직선거법 108조). 수치와 '지지율' 문장을 빼고 발행하세요."))
        else:
            items.append(Item("election", WARN, f"{name}({day}) 여론조사 공표 금지 기간입니다",
                              "이 글엔 조사 수치가 없어 괜찮습니다. 손으로 덧붙일 때도 여론조사는 넣지 마세요."))

    # 3-4) 균형 — 두 진영 언급 수가 한쪽으로 몰리면 알린다
    balance = cfg.get("balance", {}) or {}
    sides = balance.get("sides") or {}
    if post is not None and len(sides) >= 2:
        counts = side_balance(f"{post.title}\n{post.body_markdown}", sides)
        total = sum(counts.values())
        min_total = int(balance.get("min_total", 6) or 6)
        if total >= min_total:
            top_name, top = max(counts.items(), key=lambda kv: kv[1])
            said = " · ".join(f"{k} {v}" for k, v in counts.items())
            if top / total > float(balance.get("max_share", 0.75) or 0.75):
                items.append(Item("balance", WARN, f"언급이 한쪽에 몰렸습니다 ({said})",
                                  f"'{top_name}' 이야기만 {top}번 나옵니다. 상대편 입장이 자료에 없으면 "
                                  "'○○ 측 입장은 확인되지 않았다' 한 줄이라도 넣으세요."))
            else:
                items.append(Item("balance", OK, f"양쪽 언급 균형 ({said})"))

    # 4) 블로그 분량·태그·이미지 자리
    if post is not None:
        # 프롬프트가 "본문 1,200~2,000자" 로 부탁하므로 **같은 방식으로** 셉니다.
        # 예전에는 공백을 빼고 셌는데, 한국어는 공백이 20%쯤이라 1,200자로 쓴 글이
        # 960자로 잡혔습니다. ±20% 여유가 그 차이를 메워 주는 바람에 정말 짧은 글도
        # 통과했습니다 (2026-09-08 실측: 960자 글이 ok 로 나왔음).
        n = len(post.body_markdown or "")
        lo, hi = int(blog.get("min_chars", 1800)), int(blog.get("max_chars", 3500))
        if n < lo * 0.9 or n > hi * 1.1:
            items.append(Item("blog_len", WARN, f"블로그 본문 {n:,}자 (목표 {lo:,}~{hi:,})",
                              "너무 짧으면 검색 노출이 약하고, 너무 길면 휴대폰에서 이탈합니다."))
        else:
            items.append(Item("blog_len", OK, f"블로그 본문 {n:,}자"))
        tags = len(post.tags or [])
        want = int(naver.get("tag_count", 20))
        if tags == 0 or tags > 30:
            items.append(Item("tags", WARN, f"태그 {tags}개", "네이버 태그는 최대 30개입니다."))
        elif abs(tags - want) > want // 2:
            items.append(Item("tags", WARN, f"태그 {tags}개 (설정 {want}개)"))
        else:
            items.append(Item("tags", OK, f"태그 {tags}개"))
        # 4-0) 유입의 시작 — 대표 검색어가 제자리에 있는지
        keyword, missing = keyword_placement(post)
        if not keyword:
            items.append(Item("keyword", WARN, "대표 검색어가 비어 있습니다",
                              "이 글로 누가 검색해 들어올지 정하지 않은 상태입니다. 제목과 첫 문단에 넣을 말을 하나 정하세요."))
        elif missing:
            items.append(Item("keyword", WARN, f"대표 검색어 '{keyword}' 가 {' · '.join(missing)} 에 없습니다",
                              "그 자리에 같은 표기로 넣어야 검색에 걸립니다. 변형이 아니라 글자 그대로."))
        else:
            items.append(Item("keyword", OK, f"대표 검색어 '{keyword}' 가 제목·첫 문단·소제목에 모두 있음"))

        # 4-0-2) 지난 글과 겹치는 문장 — 유사문서로 몰리지 않게
        ratio, samples = overlap_with_previous(post.body_markdown, prev_bodies or [])
        limit = float(blog.get("max_overlap_ratio", 0.15))
        if prev_bodies and ratio >= limit:
            items.append(Item("overlap", WARN, f"지난 글과 거의 같은 문장이 {ratio * 100:.0f}%",
                              "매일 같은 문장을 쓰면 검색에서 중복 문서로 취급될 수 있습니다. 표현을 바꾸세요.", samples))
        elif prev_bodies:
            items.append(Item("overlap", OK, f"지난 글과 겹치는 문장 {ratio * 100:.0f}%"))

        # 4-0-3) 짧게 쓰기 — 메인 하나 + 나머지 한 줄 구조인지
        from .render import outline_from_markdown

        heads = outline_from_markdown(post.body_markdown)
        if not any("그 밖의" in h for h in heads):
            items.append(Item("shape", WARN, "'그 밖의 오늘 소식' 묶음이 없습니다",
                              "이슈를 모두 길게 설명하면 글이 늘어집니다. 메인 하나만 깊게 쓰고 나머지는 한 줄씩 모으세요."))
        elif len(heads) > 6:
            items.append(Item("shape", WARN, f"소제목이 {len(heads)}개로 많습니다",
                              "메인 이슈 2~3개 + 그 밖의 소식 + 체크포인트면 충분합니다."))
        else:
            items.append(Item("shape", OK, f"소제목 {len(heads)}개 · 메인 하나에 집중"))

        if not getattr(post, "takeaways", None):
            items.append(Item("takeaways", WARN, "'그래서 나는?' 이 비어 있습니다",
                              "무주택자·1주택자처럼 읽는 사람 유형별로 한 줄씩 있어야 남 얘기로 안 읽힙니다."))

        # 4-1) 읽기 쉬움 — 긴 문장·긴 문단
        long_s = long_sentences(post.body_markdown, int(blog.get("max_sentence_chars", 90)))
        long_p = long_paragraphs(post.body_markdown, int(blog.get("max_paragraph_chars", 320)))
        if long_s or long_p:
            bits = []
            if long_s:
                bits.append(f"긴 문장 {len(long_s)}개")
            if long_p:
                bits.append(f"긴 문단 {len(long_p)}개")
            items.append(Item("readability", WARN, " · ".join(bits) + " — 휴대폰에서 답답해 보입니다",
                              "문장은 둘로 나누고, 문단은 두세 문장에서 끊으세요.",
                              [t[:60] + "…" for t in (long_s + long_p)[:3]]))
        else:
            items.append(Item("readability", OK, "문장·문단 길이 적당"))
        if empty_photo_slots:
            items.append(Item("photos", WARN, f"직접 넣을 사진 자리 {empty_photo_slots}곳",
                              "'네이버 블로그 글' 페이지의 점선 상자 아래 '사진 찾기' 링크를 쓰세요. [네이버 블로그 글 열기](blog-naver.html)"))

    # 5) 영상 발화량
    if pack is not None:
        cpm = int(video.get("speaking_rate_cpm", 330))
        s_target = int(video.get("shorts_seconds", 60)) / 60 * cpm
        s_chars = sum(len(l.text) for l in pack.shorts.lines)
        if s_chars > s_target * 1.15:
            items.append(Item("shorts_len", WARN, f"쇼츠 발화 {s_chars}자 — {int(video.get('shorts_seconds', 60))}초에 {int(s_target)}자가 적정",
                              "빠르게 읽어야 합니다. 자막 두세 컷을 줄이세요."))
        else:
            items.append(Item("shorts_len", OK, f"쇼츠 발화 {s_chars}자 (약 {s_chars / cpm * 60:.0f}초)"))
        l_target = float(video.get("longform_minutes", 8)) * cpm
        l_chars = len(pack.longform.cold_open) + sum(len(s.script) for s in pack.longform.sections)
        if l_chars > l_target * 1.25 or l_chars < l_target * 0.6:
            items.append(Item("long_len", WARN, f"롱폼 발화 {l_chars:,}자 (약 {l_chars / cpm:.1f}분, 목표 {video.get('longform_minutes', 8)}분)"))
        else:
            items.append(Item("long_len", OK, f"롱폼 발화 {l_chars:,}자 (약 {l_chars / cpm:.1f}분)"))

    # 5-1) 자막 길이 — 두 줄에 안 들어가는 컷
    if pack is not None:
        over = long_captions(pack, int(video.get("caption_max_chars", 16)),
                             int(video.get("caption_max_lines", 2)))
        if over:
            items.append(Item("caption_len", WARN, f"두 줄에 안 들어가는 자막 {len(over)}컷",
                              "쇼츠는 세로 화면입니다. 컷을 쪼개거나 문장을 줄이세요.", over[:3]))

    # 5-2) 같은 주제 반복
    for r in (repeats or []):
        items.append(Item("repeat", WARN,
                          f"'{r['title']}' 은 {r['days_ago']}일 전에도 다뤘습니다",
                          "그대로 또 쓰면 재탕으로 보입니다. 그때와 달라진 숫자를 앞세우거나 후속 국면을 잡으세요.",
                          [f"{r['prev_date']} · {r['prev_title']}"]))

    # 6) 실행 중 나온 경고 (max_tokens 잘림, 강등 등)
    for w in (warnings or []):
        if "확인되지 않았습니다" in w or "열리지 않습니다" in w:
            continue                          # 위에서 이미 항목으로 다뤘다
        items.append(Item("warn", WARN, w))

    return items


def summarize(items: list[Item]) -> dict[str, int]:
    out = {OK: 0, WARN: 0, FAIL: 0}
    for i in items:
        out[i.level] += 1
    return out


# ── ❌ 금지 표현 자동 수정 ─────────────────────────────────

_SENT = re.compile(r"[^.!?\n]*[.!?]?")


def sentences_with(text: str, phrases: list[str]) -> list[str]:
    """금지 표현이 든 문장들 (원문 그대로, 중복 없이)."""
    out: list[str] = []
    for m in _SENT.finditer(text or ""):
        s = m.group(0)
        if s.strip() and any(p in s for p in phrases) and s not in out:
            out.append(s)
    return out


def autofix(text: str, phrases: list[str], rewrite) -> tuple[str, list[tuple[str, str]]]:
    """금지 표현이 든 문장을 rewrite(문장, 표현들) 로 바꾼다. (새 본문, [(전, 후), ...])"""
    changes: list[tuple[str, str]] = []
    for s in sentences_with(text, phrases):
        hits = [p for p in phrases if p in s]
        try:
            new = rewrite(s.strip(), hits)
        except Exception as exc:                  # 고치기 실패는 점검표 ❌ 로 남기면 된다
            import logging
            logging.getLogger(__name__).warning("문장 고쳐 쓰기 실패: %s", exc)
            continue
        if new and not any(p in new for p in phrases):
            text = text.replace(s.strip(), new, 1)
            changes.append((s.strip(), new))
    return text, changes

