"""0원 방식 — 채팅에서 받은 답을 읽어 산출물을 만든다.

흐름 (2026-09-10, 사용자 선택):
  1. 아침 데일리(러너)가 기사를 모아 붙여넣기 묶음(`prompt-pack.md/.html`)과 `pack.json` 을 남긴다. 모델은 안 부른다.
  2. 사용자가 묶음을 claude.ai 채팅에 붙여 넣고, 답(JSON 세 덩이)을 저장소 이슈에 붙여 넣는다.
  3. 「답 받기」 워크플로가 이 모듈로 답을 읽어 브리핑·블로그·카드·대본을 만든다.

`pack.json` 이 필요한 이유: 답을 읽는 러너에는 기사 원본(`raw/`, 커밋 안 함)이 없다. 근거 번호를 주소로
되돌리고(1-2 → URL), 수치 검산을 하고, 글 끝 '참고한 기사' 목록을 만들려면 기사 목록이 있어야 한다.
본문은 넣지 않는다 — 공개 저장소에 올라가므로 제목·매체·주소·RSS 요약과 **본문에 나온 숫자 토막**만 남긴다.
숫자 토막이 있으면 검산이 본문 없이도 대부분 맞는다.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .models import Article, BlogPost, Cluster, DailyBrief, VideoPack

log = logging.getLogger(__name__)

PACK_FILE = "pack.json"

# 본문에서 건질 숫자 토막 — 검산(verify)이 찾는 것은 값과 단위이므로 그 둘만 남긴다.
_NUMBER_TOKEN = re.compile(
    r"\d[\d,.]*\s*(?:%p|%|퍼센트|명|건|표|석|억|만|천|원|일|년|월|개|곳|호|차|배|위|시간|분|조|kg|km|㎡|평)?"
)


def number_tokens(text: str, limit: int = 120) -> list[str]:
    """본문에 나온 숫자(+단위) 토막. 중복은 빼고 앞에서부터 `limit` 개."""
    out: list[str] = []
    for m in _NUMBER_TOKEN.finditer(text or ""):
        tok = " ".join(m.group(0).split())
        if tok and tok not in out:
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def save_pack(out_dir: Path, clusters: list[Cluster]) -> Path:
    """답을 읽을 때 쓸 기사 목록(본문 없음)을 남긴다."""
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "clusters": [
            {
                "key": c.key, "score": c.score, "categories": c.categories,
                "matched_keywords": c.matched_keywords,
                "articles": [
                    {
                        "id": a.id, "title": a.title, "url": a.url, "feed_id": a.feed_id,
                        "feed_name": a.feed_name, "publisher": a.publisher,
                        "published": a.published.isoformat() if a.published else None,
                        "summary": a.summary, "source_weight": a.source_weight, "tags": a.tags,
                        "numbers": number_tokens(a.body or a.summary),
                    }
                    for a in c.articles
                ],
            }
            for c in clusters
        ],
    }
    path = out_dir / PACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def load_pack(out_dir: Path) -> list[Cluster]:
    """`pack.json` 을 클러스터로 되살린다. 본문 자리에는 숫자 토막을 넣어 검산이 돌게 한다."""
    path = out_dir / PACK_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    clusters: list[Cluster] = []
    for c in data.get("clusters") or []:
        articles = []
        for a in c.get("articles") or []:
            articles.append(Article(
                id=a["id"], title=a.get("title", ""), url=a.get("url", ""), feed_id=a.get("feed_id", ""),
                feed_name=a.get("feed_name", ""), publisher=a.get("publisher", ""),
                published=datetime.fromisoformat(a["published"]) if a.get("published") else None,
                summary=a.get("summary", ""), body=" ".join(a.get("numbers") or []),
                source_weight=float(a.get("source_weight", 1.0)), tags=list(a.get("tags") or []),
            ))
        if articles:
            clusters.append(Cluster(key=c.get("key", articles[0].id), articles=articles,
                                    score=float(c.get("score", 0.0)), categories=list(c.get("categories") or []),
                                    matched_keywords=list(c.get("matched_keywords") or [])))
    return clusters


# ── 답 읽기 ──────────────────────────────────────────────────

_FENCE = re.compile(r"```[ \t]*(?:json)?[ \t]*([A-Za-z_-]*)[ \t]*\r?\n(.*?)```", re.S)
TAGS = ("brief", "blog", "script")
MODELS = {"brief": DailyBrief, "blog": BlogPost, "script": VideoPack}


# 묶음(질문)을 답 자리에 붙여 넣었는지 알아보는 표식 — `prompts.ANALYST_SYSTEM` 첫 줄
_PACK_MARK = "뉴스 애널리스트입니다"


class AnswerError(ValueError):
    pass


def _blocks(text: str) -> list[tuple[str, str]]:
    """코드 블록 (표식, 본문) 목록. 표식이 없으면 빈 문자열."""
    return [(m.group(1).strip().lower(), m.group(2).strip()) for m in _FENCE.finditer(text or "")]


def _scan_objects(text: str) -> list[dict]:
    """글 안에 있는 JSON 객체를 앞에서부터 모두 꺼낸다.

    채팅 화면에서 답을 복사하면 코드 펜스(```)가 떨어져 나가, 객체 셋이 그냥 이어붙은 글이 된다
    (2026-09-10 첫 왕복에서 실제로 그랬음: `Extra data: line 170`). 그래서 통째로 읽지 않고
    `raw_decode` 로 한 덩이씩 끊어 읽는다. 사이에 낀 설명 문장은 건너뛴다.
    """
    dec = json.JSONDecoder()
    out: list[dict] = []
    i, n = 0, len(text or "")
    while i < n:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _guess_tag(obj: dict) -> str:
    keys = set(obj)
    if {"headline", "issues"} & keys and "body_markdown" not in keys:
        return "brief"
    if "body_markdown" in keys:
        return "blog"
    if {"shorts", "longform"} & keys:
        return "script"
    return ""


def parse_answer(text: str) -> dict:
    """채팅 답에서 브리핑·블로그·대본을 꺼내 검증한다. 없는 덩이는 빠지고, 틀린 덩이는 AnswerError.

    받아 주는 꼴 — ① ```json brief / blog / script 표식이 붙은 블록 셋 ② 표식 없는 블록(내용으로 짐작)
    ③ 블록 없이 {"brief": …, "blog": …, "script": …} 객체 하나. 사람이 채팅에서 복사하다 보면
    표식이 빠지거나 한 덩이만 오는 일이 흔해서다.
    """
    if _PACK_MARK in (text or "")[:400]:
        # 1단계(묶음)와 4단계(답)를 바꿔 넣은 경우. 2026-09-10 이슈 #2 가 실제로 그랬다.
        raise AnswerError(
            "이건 답이 아니라 붙여넣기 묶음(질문)입니다. 이 묶음을 claude.ai 채팅에 붙여 넣고, "
            "거기서 온 답을 이슈 본문에 넣어 주세요."
        )

    raw: dict[str, dict] = {}
    found: list[tuple[str, dict]] = []
    blocks = _blocks(text)
    if blocks:
        for tag, body in blocks:
            found.extend((tag, obj) for obj in _scan_objects(body))
    if not found:
        # 펜스가 없거나(화면에서 복사한 경우) 펜스 안이 비었으면 글 전체를 훑는다
        found = [("", obj) for obj in _scan_objects(text or "")]
    for tag, obj in found:
        if tag not in TAGS and set(obj) & set(TAGS) and all(isinstance(obj.get(t, {}), dict) for t in TAGS):
            for t in TAGS:
                if isinstance(obj.get(t), dict):
                    raw.setdefault(t, obj[t])
            continue
        tag = tag if tag in TAGS else _guess_tag(obj)
        if tag and tag not in raw:
            raw[tag] = obj
    if not raw:
        raise AnswerError(
            "답에서 JSON 을 찾지 못했습니다. 「{」 로 시작하는 브리핑 덩이가 통째로 들어 있어야 합니다 — "
            "복사할 때 중간이 잘리지 않았는지 확인해 주세요."
        )

    out: dict = {}
    problems = []
    for tag, obj in raw.items():
        try:
            out[tag] = MODELS[tag].model_validate(obj)
        except ValidationError as exc:
            short = "; ".join(f"{'/'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
            problems.append(f"[{tag}] {short}")
    if problems:
        raise AnswerError("답의 형식이 스키마와 다릅니다 — " + " · ".join(problems))
    if "brief" not in out:
        raise AnswerError("브리핑(brief) 덩이가 없습니다. 브리핑 없이는 글도 카드도 만들 수 없습니다.")
    return out


def describe(parsed: dict) -> str:
    got = [t for t in TAGS if t in parsed]
    names = {"brief": "브리핑", "blog": "블로그", "script": "대본"}
    return "·".join(names[t] for t in got) + (" (빠진 것: " + "·".join(names[t] for t in TAGS if t not in got) + ")"
                                            if len(got) < 3 else "")
