"""정치인 인물 조사 — '이 사람이 누구이고 어떤 길을 걸어왔나' 영상용 자료를 한 번에 모은다.

이름 하나를 받아 네 곳에서 자료를 긁어 온다. 전부 인증키 없이 돈다.

* **열린국회정보 역대 국회의원 인적사항(`ALLNAMEMBER`)** — 생년·정당 이력·선거구·당선 대수·
  공식 약력(학력·경력). 이름으로 찾으면 키 없이도 통째로 온다 (2026-09-09 실측). 국회의원을
  지낸 적이 없는 사람은 자료 없음(INFO-200)이라 빈 값이다.
* **열린국회정보 발의법률안(`nzmimeepazxkubdpn`)** — `PROPOSER` 로 걸러 대수별 발의 건수와 최근
  법안. 키가 없으면 건수(`list_total_count`)는 정확히 오되 목록은 5건만 온다.
* **한국어 위키백과 API** — 생애·경력·논란·역대 선거 결과 절. CC BY-SA 라 출처를 적는다.
  누구나 고칠 수 있는 2차 자료이므로 프롬프트에서 '위키백과 기준' 이라고 표시하게 한다.
* **구글뉴스 검색 RSS** — 최근 기사 제목 100건과, 정치 입문 시기부터 지금까지를 몇 해씩 잘라
  시기별 기사 제목. 2001년 기사까지 온다(실측). 제목만 쓰고 본문은 긁지 않는다.

동명이인은 생년으로 가른다 (`--birth`). 안 주면 가장 최근 대수의 사람을 고르고 나머지를 알려 준다.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)

ASSEMBLY_BASE = "https://open.assembly.go.kr/portal/openapi/"
MEMBERS = "ALLNAMEMBER"            # 역대 국회의원 인적사항
BILLS = "nzmimeepazxkubdpn"        # 국회의원 발의법률안 (civics 와 같은 끝점)
WIKI_API = "https://ko.wikipedia.org/w/api.php"
GNEWS = "https://news.google.com/rss/search"
UA = "Mozilla/5.0 (compatible; rebrief/1.0; +https://github.com/rokieyez/politics-news)"
TIMEOUT = 20
NEWS_PAUSE = 0.6                   # 구글뉴스를 연달아 부를 때 쉬는 시간(초)

# 제N대 국회 임기 시작 연도. 시기별 기사 검색의 출발점을 잡는 데 쓴다.
AGE_START_YEAR = {13: 1988, 14: 1992, 15: 1996, 16: 2000, 17: 2004, 18: 2008,
                  19: 2012, 20: 2016, 21: 2020, 22: 2024, 23: 2028}
# 발의법률안 API 는 이 대수부터 자료가 있다고 보고 그 아래는 부르지 않는다.
BILLS_MIN_AGE = 20

# 위키백과에서 빼는 절 — 본문이 아니라 목록·링크다.
WIKI_SKIP = {"같이 보기", "외부 링크", "각주", "참고 문헌", "주석", "관련 항목", "둘러보기"}
WIKI_SECTION_CAP = 3500
WIKI_TOTAL_CAP = 16000
# 구글뉴스 검색어 뒤에 붙이는 조건 — 같은 이름의 배우·선수·교수 기사를 걸러 준다.
# '대표' 는 '국가대표' 에도 걸려 피겨 선수 김민석 기사가 섞였다(2026-09-09 실측) → '당대표' 로.
NEWS_QUALIFIER = "(의원 OR 정치 OR 정당 OR 장관 OR 총리 OR 당대표 OR 후보 OR 시장 OR 지사 OR 대통령)"


class ProfileError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None) -> requests.Response:
    """시험에서 갈아끼우는 자리."""
    resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


# ── 자료 구조 ────────────────────────────────────────────────


@dataclass
class Member:
    """열린국회정보 인적사항 한 사람."""

    name: str
    code: str = ""
    hanja: str = ""
    birth: str = ""                  # YYYY-MM-DD
    birth_calendar: str = ""         # 양 / 음
    terms: list[str] = field(default_factory=list)      # ["제15대", "제16대", …]
    parties: list[str] = field(default_factory=list)    # 대수 순서대로
    districts: list[str] = field(default_factory=list)
    elect_kinds: list[str] = field(default_factory=list)
    reelected: str = ""              # "4선"
    committee: str = ""
    career: str = ""                 # 공식 약력 원문 (학력·경력)

    @property
    def birth_year(self) -> int | None:
        return int(self.birth[:4]) if self.birth[:4].isdigit() else None

    @property
    def ages(self) -> list[int]:
        return [int(m) for m in re.findall(r"제(\d+)대", " ".join(self.terms))]

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.birth:
            parts.append(f"{self.birth[:4]}년생")
        if self.terms:
            parts.append("·".join(self.terms))
        if self.parties:
            parts.append(self.parties[-1])
        return " / ".join(parts)


@dataclass
class NewsItem:
    title: str
    source: str
    date: str                        # YYYY-MM-DD
    link: str


@dataclass
class NewsWindow:
    label: str                       # "2000~2003"
    start: str
    end: str
    items: list[NewsItem] = field(default_factory=list)


@dataclass
class Materials:
    """한 사람에 대해 모은 자료 전부. 프롬프트·자료묶음·sources.json 이 모두 이걸 읽는다."""

    name: str
    fetched_at: str
    member: Member | None = None
    others: list[Member] = field(default_factory=list)      # 같은 이름의 다른 사람
    wiki: dict | None = None         # {title, url, revised, intro, sections: {절: 본문}}
    bills: dict | None = None        # {total, by_age: {대수: 건수}, latest: [...], sample: bool}
    recent: list[NewsItem] = field(default_factory=list)
    windows: list[NewsWindow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)          # 사람에게 보여 줄 수집 메모

    def to_dict(self) -> dict:
        return asdict(self)

    # 자료 번호 → 링크. 정리 글의 근거 표시에 쓴다.
    def source_links(self) -> dict[str, tuple[str, str]]:
        links: dict[str, tuple[str, str]] = {}
        if self.member:
            links["A"] = ("열린국회정보 국회의원 인적사항", "https://open.assembly.go.kr/portal/openapi/ALLNAMEMBER")
        if self.wiki:
            links["W"] = (f"위키백과 「{self.wiki['title']}」 ({self.wiki.get('revised', '')[:10]} 판)", self.wiki["url"])
        if self.bills and self.bills.get("latest"):
            links["B"] = ("열린국회정보 발의법률안", "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn")
        for i, item in enumerate(self.recent, 1):
            links[f"N0-{i}"] = (f"{item.source} {item.date} {item.title}", item.link)
        for wi, win in enumerate(self.windows, 1):
            for i, item in enumerate(win.items, 1):
                links[f"N{wi}-{i}"] = (f"{item.source} {item.date} {item.title}", item.link)
        return links


# ── 열린국회정보 ─────────────────────────────────────────────


def _assembly(endpoint: str, params: dict, key: str = "") -> tuple[list[dict], int]:
    """(행 목록, 전체 건수). 자료 없음(INFO-200)은 빈 목록이다."""
    q: dict = {"Type": "json", "pIndex": 1, "pSize": 100, **params}
    if key:
        q["KEY"] = key
    data = _get(ASSEMBLY_BASE + endpoint, q).json()
    body = data.get(endpoint)
    if not body:
        code = ((data.get("RESULT") or {}).get("CODE") or "")
        if code.startswith("INFO-200"):
            return [], 0
        msg = ((data.get("RESULT") or {}).get("MESSAGE") or "")
        raise ProfileError(f"열린국회정보 {code} {msg}".strip())
    head = body[0].get("head") or []
    total = int((head[0] or {}).get("list_total_count", 0)) if head else 0
    rows = (body[1].get("row") or []) if len(body) > 1 else []
    return rows, total


def _split(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split("/") if p.strip()]


def member_from_row(row: dict) -> Member:
    terms = [t.strip() for t in (row.get("GTELT_ERACO") or "").split(",") if t.strip()]
    return Member(
        name=row.get("NAAS_NM") or "",
        code=row.get("NAAS_CD") or "",
        hanja=row.get("NAAS_CH_NM") or "",
        birth=(row.get("BIRDY_DT") or "")[:10],
        birth_calendar=row.get("BIRDY_DIV_CD") or "",
        terms=terms,
        parties=_split(row.get("PLPT_NM") or ""),
        districts=_split(row.get("ELECD_NM") or ""),
        elect_kinds=_split(row.get("ELECD_DIV_NM") or ""),
        reelected=row.get("RLCT_DIV_NM") or "",
        committee=row.get("CMIT_NM") or row.get("BLNG_CMIT_NM") or "",
        career=(row.get("BRF_HST") or "").replace("\r\n", "\n").strip(),
    )


def find_members(name: str, *, key: str = "") -> list[Member]:
    rows, _ = _assembly(MEMBERS, {"NAAS_NM": name}, key)
    members = [member_from_row(r) for r in rows if (r.get("NAAS_NM") or "") == name]
    # 최근 대수의 사람이 앞에 오게
    members.sort(key=lambda m: max(m.ages or [0]), reverse=True)
    return members


def pick_member(members: list[Member], birth: str | None) -> Member | None:
    """생년(YYYY 또는 YYYY-MM-DD)이 있으면 그걸로, 없으면 가장 최근 대수의 사람."""
    if not members:
        return None
    members = sorted(members, key=lambda m: max(m.ages or [0]), reverse=True)
    if birth:
        hit = [m for m in members if m.birth.startswith(birth)]
        if hit:
            return hit[0]
        return None
    return members[0]


def fetch_bills(name: str, ages: list[int], *, key: str = "", max_ages: int = 3) -> dict:
    """대수별 발의 건수와 최근 법안. 키 없이도 건수는 정확하고 목록은 5건뿐이다."""
    by_age: dict[str, int] = {}
    latest: list[dict] = []
    for age in sorted([a for a in ages if a >= BILLS_MIN_AGE], reverse=True)[:max_ages]:
        rows, total = _assembly(BILLS, {"AGE": age, "PROPOSER": name}, key)
        by_age[f"제{age}대"] = total
        for r in rows[:8 if key else 5]:
            latest.append({"age": f"제{age}대", "name": r.get("BILL_NAME") or "",
                           "date": (r.get("PROPOSE_DT") or "")[:10],
                           "proposer": r.get("PROPOSER") or "",
                           "committee": r.get("COMMITTEE") or "",
                           "link": r.get("DETAIL_LINK") or ""})
    return {"total": sum(by_age.values()), "by_age": by_age, "latest": latest, "sample": not key}


# ── 위키백과 ─────────────────────────────────────────────────


def _wiki(params: dict) -> dict:
    return _get(WIKI_API, {"format": "json", "formatversion": 2, **params}).json()


def wiki_candidates(name: str, limit: int = 6) -> list[str]:
    data = _wiki({"action": "query", "list": "search", "srsearch": f"{name} 정치인", "srlimit": limit})
    titles = [r["title"] for r in (data.get("query") or {}).get("search", [])]
    # 검색이 이름 자체를 못 올리는 때가 있어 정확히 같은 제목을 앞에 둔다
    if name not in titles:
        titles.insert(0, name)
    return titles


def _is_this_person(intro: str, name: str, birth_year: int | None) -> bool:
    head = " ".join((intro or "").split())[:400]
    if name not in head or "정치" not in head:
        return False
    if birth_year and str(birth_year) not in head:
        return False
    return True


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """extract 본문을 (머리글, {절 이름: 본문}) 으로. 소절(===)은 윗절에 붙인다."""
    parts = re.split(r"\n==\s*([^=\n]+?)\s*==\n", "\n" + text)
    intro = parts[0].strip()
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        title, body = parts[i].strip(), parts[i + 1]
        body = re.sub(r"\n===+\s*([^=\n]+?)\s*===+\n", r"\n▸ \1\n", body).strip()
        if title in WIKI_SKIP or not body:
            continue
        sections[title] = body
    return intro, sections


# 평문 추출에서 비어 버리는 표 절. 위키텍스트를 따로 받아 줄글로 푼다.
WIKI_TABLE_SECTIONS = ("역대 선거 결과", "소속 정당")


def _clean_wikitext(text: str) -> str:
    text = re.sub(r"\{\{정당색[^}]*\}\}", "", text)
    text = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", text)      # [[a|b]] → b
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)                 # [[a]] → a
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"'{2,}", "", text)
    return text


def wikitext_table_rows(wikitext: str) -> list[list[str]]:
    """선거기록 틀과 wikitable 을 행마다 칸 목록으로. 색·링크 같은 꾸밈은 버린다."""
    rows: list[list[str]] = []
    text = _clean_wikitext(wikitext)
    # {{선거기록/KR/개인 | 1992년 | 총선 | 14대 | 국회의원 | 서울 영등포구 을 | 민주당1991 | 48,151표 | 40.95 | 2위 | 낙선 | | }}
    for m in re.finditer(r"\{\{선거기록/[^|}]*\|(.*?)\}\}", text, flags=re.S):
        cells = [" ".join(c.split()) for c in m.group(1).split("|")]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    # wikitable: |- 로 행, || 로 칸. 머리글(!)은 버린다.
    for table in re.findall(r"\{\|(.*?)\|\}", text, flags=re.S):
        for row in re.split(r"\n\|-[^\n]*", table):
            cells: list[str] = []
            for raw in row.split("\n"):
                raw = raw.strip()
                if not raw.startswith("|") or raw.startswith("|+"):
                    continue
                for cell in raw[1:].split("||"):
                    cell = cell.split("|")[-1] if "style=" in cell or "colspan=" in cell else cell
                    cell = " ".join(cell.split())
                    if cell:
                        cells.append(cell)
            if cells:
                rows.append(cells)
    return rows


def wikitext_table_lines(wikitext: str) -> list[str]:
    return [" · ".join(r) for r in wikitext_table_rows(wikitext)]


def fetch_wiki_tables(title: str) -> dict[str, list[list[str]]]:
    """표로만 된 절(선거 결과·소속 정당)을 위키텍스트로 받아 {절: [행(칸 목록)]} 로 돌려준다."""
    data = _wiki({"action": "parse", "page": title, "prop": "sections"})
    found: dict[str, list[list[str]]] = {}
    for sec in (data.get("parse") or {}).get("sections", []):
        line = (sec.get("line") or "").strip()
        if line not in WIKI_TABLE_SECTIONS:
            continue
        wt = _wiki({"action": "parse", "page": title, "prop": "wikitext", "section": sec["index"]})
        body = ((wt.get("parse") or {}).get("wikitext") or "")
        rows = wikitext_table_rows(body)
        if rows:
            found[line] = rows
    return found


def fetch_wiki(name: str, birth_year: int | None) -> dict | None:
    titles = wiki_candidates(name)
    data = _wiki({"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                  "exlimit": 20, "titles": "|".join(titles[:8])})
    pages = (data.get("query") or {}).get("pages", [])
    chosen = None
    for t in titles:                                   # 검색 순서를 지킨다
        for p in pages:
            if p.get("title") == t and _is_this_person(p.get("extract", ""), name, birth_year):
                chosen = t
                break
        if chosen:
            break
    if not chosen:
        return None
    full = _wiki({"action": "query", "prop": "extracts|revisions|info", "explaintext": 1,
                  "rvprop": "timestamp", "inprop": "url", "titles": chosen})
    page = ((full.get("query") or {}).get("pages") or [{}])[0]
    intro, sections = split_sections(page.get("extract", "") or "")
    kept: dict[str, str] = {}
    used = 0
    for title, body in sections.items():
        body = body[:WIKI_SECTION_CAP]
        if used + len(body) > WIKI_TOTAL_CAP:
            break
        kept[title] = body
        used += len(body)
    tables: dict[str, list[list[str]]] = {}
    try:
        tables = fetch_wiki_tables(chosen)
        for title, rows in tables.items():
            kept[title] = "\n".join(f"- {' · '.join(r)}" for r in rows)[:WIKI_SECTION_CAP]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("위키백과 표 절을 받지 못했습니다: %s", exc)
    revs = page.get("revisions") or [{}]
    return {"title": page.get("title", chosen), "url": page.get("fullurl", ""),
            "revised": (revs[0].get("timestamp") or "")[:10], "intro": intro[:1500],
            "sections": kept, "tables": tables, "license": "CC BY-SA 4.0"}


# ── 구글뉴스 ─────────────────────────────────────────────────


def _news_query(name: str, after: str | None = None, before: str | None = None) -> str:
    q = f'"{name}" {NEWS_QUALIFIER}'
    if after:
        q += f" after:{after}"
    if before:
        q += f" before:{before}"
    return q


def parse_news(xml_bytes: bytes) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for node in root.findall(".//item"):
        title = " ".join((node.findtext("title") or "").split())
        src = node.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        try:
            when = parsedate_to_datetime(node.findtext("pubDate") or "").strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            when = ""
        key = re.sub(r"\W+", "", title)[:40]
        if not title or key in seen:
            continue
        seen.add(key)
        items.append(NewsItem(title=title, source=source, date=when, link=node.findtext("link") or ""))
    return items


def news_search(name: str, after: str | None = None, before: str | None = None,
                limit: int = 40) -> list[NewsItem]:
    resp = _get(GNEWS, {"q": _news_query(name, after, before), "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    items = parse_news(resp.content)
    items.sort(key=lambda x: x.date)
    return items[:limit]


def plan_windows(start_year: int, end_year: int, span: int, max_windows: int = 10) -> list[tuple[int, int]]:
    """[(첫해, 끝해), …]. 기간이 길면 창을 넓혀 개수를 상한 안에 둔다."""
    years = max(1, end_year - start_year + 1)
    while (years + span - 1) // span > max_windows:
        span += 1
    out = []
    y = start_year
    while y <= end_year:
        out.append((y, min(y + span - 1, end_year)))
        y += span
    return out


def career_start_year(member: Member | None, wiki: dict | None, today: date) -> int:
    """시기별 검색을 어디서 시작할지. 첫 당선 대수 1년 전 → 위키 머리글의 첫 연도 → 12년 전."""
    if member and member.ages:
        first = min(member.ages)
        if first in AGE_START_YEAR:
            return AGE_START_YEAR[first] - 1
    if wiki:
        years = [int(y) for y in re.findall(r"(19[5-9]\d|20[0-4]\d)년", wiki.get("intro", ""))]
        if member and member.birth_year:
            years = [y for y in years if y >= member.birth_year + 20]
        if years:
            return min(years)
    return today.year - 12


def fetch_news(name: str, start_year: int, today: date, *, span: int, per_window: int,
               recent_limit: int) -> tuple[list[NewsItem], list[NewsWindow]]:
    recent = news_search(name, limit=recent_limit)
    windows: list[NewsWindow] = []
    for a, b in plan_windows(start_year, today.year, span):
        time.sleep(NEWS_PAUSE)
        start, end = f"{a}-01-01", f"{b}-12-31"
        items = news_search(name, after=start, before=end, limit=per_window)
        windows.append(NewsWindow(label=f"{a}~{b}" if a != b else str(a), start=start, end=end, items=items))
    return recent, windows


# ── 한 번에 모으기 ───────────────────────────────────────────


def collect(cfg: Config, name: str, *, birth: str | None = None, today: date | None = None,
            key: str = "") -> Materials:
    today = today or date.today()
    settings = cfg.get("profile", {}) or {}
    m = Materials(name=name, fetched_at=datetime.now().isoformat(timespec="seconds"))

    try:
        members = find_members(name, key=key)
    except (requests.RequestException, ProfileError, ValueError) as exc:
        members = []
        m.notes.append(f"열린국회정보 인적사항을 받지 못했습니다: {exc}")
    m.member = pick_member(members, birth)
    if members and not m.member:
        m.notes.append(f"생년 {birth} 과 맞는 사람이 없습니다. 있는 사람: "
                       + " / ".join(x.label for x in members))
    m.others = [x for x in members if x is not m.member]
    if m.others:
        m.notes.append("같은 이름의 다른 의원: " + " / ".join(x.label for x in m.others)
                       + ". 다른 사람이면 --birth 에 생년을 주세요.")
    if not members:
        m.notes.append("열린국회정보에 국회의원 기록이 없습니다 (국회의원을 지낸 적이 없거나 이름 표기가 다름).")

    birth_year = m.member.birth_year if m.member else (int(birth[:4]) if birth and birth[:4].isdigit() else None)
    try:
        m.wiki = fetch_wiki(name, birth_year)
    except (requests.RequestException, ValueError, KeyError) as exc:
        m.notes.append(f"위키백과를 받지 못했습니다: {exc}")
    if m.wiki is None and "위키백과" not in " ".join(m.notes):
        m.notes.append("위키백과에서 같은 사람으로 확인되는 문서를 찾지 못했습니다.")

    if m.member and m.member.ages:
        try:
            m.bills = fetch_bills(name, m.member.ages, key=key)
        except (requests.RequestException, ProfileError, ValueError) as exc:
            m.notes.append(f"발의법률안을 받지 못했습니다: {exc}")

    start = career_start_year(m.member, m.wiki, today)
    try:
        m.recent, m.windows = fetch_news(
            name, start, today,
            span=int(settings.get("news_span_years", 4)),
            per_window=int(settings.get("news_per_window", 30)),
            recent_limit=int(settings.get("news_recent", 40)),
        )
    except (requests.RequestException, ET.ParseError) as exc:
        m.notes.append(f"구글뉴스 검색을 받지 못했습니다: {exc}")
    return m


# ── 자료묶음 (모델에 주는 글 = 사람이 손으로 쓸 때의 prompt-pack) ──────────


def format_materials(m: Materials) -> str:
    """번호를 붙여 자료를 한 덩어리 글로. 모델은 이 번호로 근거를 댄다."""
    out: list[str] = []
    if m.member:
        mem = m.member
        out.append("[A] 열린국회정보 국회의원 인적사항 (공식 기록)")
        out.append(f"이름: {mem.name}" + (f" ({mem.hanja})" if mem.hanja else ""))
        if mem.birth:
            out.append(f"생년월일: {mem.birth} ({mem.birth_calendar}력)")
        if mem.terms:
            out.append(f"당선: {', '.join(mem.terms)} ({mem.reelected})")
        for i, term in enumerate(mem.terms):
            party = mem.parties[i] if i < len(mem.parties) else ""
            district = mem.districts[i] if i < len(mem.districts) else ""
            kind = mem.elect_kinds[i] if i < len(mem.elect_kinds) else ""
            out.append(f"  - {term}: {party} · {district} · {kind}".rstrip(" ·"))
        if mem.committee:
            out.append(f"현재 위원회: {mem.committee}")
        if mem.career:
            out.append("공식 약력:\n" + mem.career)
        out.append("")
    if m.wiki:
        w = m.wiki
        out.append(f"[W] 위키백과 「{w['title']}」 ({w.get('revised', '')} 판, {w.get('license', '')}) — "
                   "누구나 고칠 수 있는 2차 자료")
        out.append(w.get("intro", ""))
        for title, body in (w.get("sections") or {}).items():
            out.append(f"\n[W·{title}]\n{body}")
        out.append("")
    if m.bills and m.bills.get("by_age"):
        b = m.bills
        out.append("[B] 열린국회정보 발의법률안 (이름이 발의자에 든 법안)")
        out.append("대수별 건수: " + ", ".join(f"{k} {v}건" for k, v in b["by_age"].items()))
        if b.get("sample"):
            out.append("(인증키가 없어 목록은 대수마다 5건까지만 왔습니다. 건수는 정확합니다.)")
        for x in b.get("latest", []):
            out.append(f"  - {x['date']} {x['name']} ({x['age']}, {x['committee']}) — {x['proposer']}")
        out.append("")
    if m.recent:
        out.append("[N0] 최근 기사 제목 (구글뉴스, 최신순)")
        for i, item in enumerate(sorted(m.recent, key=lambda x: x.date, reverse=True), 1):
            out.append(f"  (N0-{i}) {item.date} {item.source} · {item.title}")
        out.append("")
    for wi, win in enumerate(m.windows, 1):
        out.append(f"[N{wi}] {win.label} 기사 제목 (구글뉴스)")
        if not win.items:
            out.append("  (이 기간 기사를 찾지 못함)")
        for i, item in enumerate(win.items, 1):
            out.append(f"  (N{wi}-{i}) {item.date} {item.source} · {item.title}")
        out.append("")
    return "\n".join(out).strip() + "\n"


def slug(name: str, birth: str | None) -> str:
    safe = re.sub(r"[^\w가-힣]+", "", name)
    return f"{safe}-{birth[:4]}" if birth and birth[:4].isdigit() else safe


def file_base(m: Materials, today: str, birth: str | None = None) -> str:
    """산출물 이름의 앞부분: `이름(생년,정당)_날짜` (2026-09-09 사용자 지정 형식).

    생년이나 정당을 모르면 그 칸을 뺀다 — `이름(1964)_날짜`, `이름_날짜`.
    """
    safe = re.sub(r"[^\w가-힣]+", "", m.name)
    year = str(m.member.birth_year) if m.member and m.member.birth_year else (birth[:4] if birth and birth[:4].isdigit() else "")
    party = (m.member.parties[-1] if m.member and m.member.parties else "").replace(" ", "")
    inside = ",".join(x for x in (year, party) if x)
    return f"{safe}({inside})_{today}" if inside else f"{safe}_{today}"


# ── 정리표 (모델 없이 프로그램이 표로만) ──────────────────────

ELECTION_COLS = ("연도", "선거", "대수", "직책", "선거구", "정당", "득표", "득표율", "순위", "결과", "비고")


def election_rows(m: Materials) -> list[dict]:
    """위키 선거기록 행을 이름 붙은 칸으로. 없으면 국회 기록의 당선 대수만."""
    rows = ((m.wiki or {}).get("tables") or {}).get("역대 선거 결과") or []
    out = []
    for cells in rows:
        d = dict(zip(ELECTION_COLS, cells + [""] * len(ELECTION_COLS)))
        rate = d["득표율"]
        if rate and re.fullmatch(r"[\d.]+", rate):
            d["득표율"] = rate + "%"
        out.append(d)
    if out or not m.member:
        return out
    for i, term in enumerate(m.member.terms):
        age = re.search(r"\d+", term)
        year = AGE_START_YEAR.get(int(age.group()), 0) if age else 0
        out.append({"연도": f"{year}년" if year else "", "선거": "총선", "대수": term, "직책": "국회의원",
                    "선거구": m.member.districts[i] if i < len(m.member.districts) else "",
                    "정당": m.member.parties[i] if i < len(m.member.parties) else "",
                    "득표": "", "득표율": "", "순위": "", "결과": "당선", "비고": "국회 기록"})
    return out


def party_rows(m: Materials) -> list[dict]:
    rows = ((m.wiki or {}).get("tables") or {}).get("소속 정당") or []
    out = [{"정당": r[0], "기간": r[1] if len(r) > 1 else "", "비고": r[2] if len(r) > 2 else ""} for r in rows if r]
    if out or not m.member:
        return out
    for i, term in enumerate(m.member.terms):
        out.append({"정당": m.member.parties[i] if i < len(m.member.parties) else "", "기간": term, "비고": "국회 기록"})
    return out


def news_by_year(m: Materials, per_year: int = 8) -> list[tuple[str, list[NewsItem]]]:
    """모든 기사 제목을 해마다 묶는다. 같은 제목은 하나만, 한 해에 `per_year` 건까지."""
    seen: set[str] = set()
    years: dict[str, list[NewsItem]] = {}
    for item in [i for w in m.windows for i in w.items] + list(m.recent):
        key = re.sub(r"\W+", "", item.title)[:40]
        if not item.date or key in seen:
            continue
        seen.add(key)
        years.setdefault(item.date[:4], []).append(item)
    out = []
    for year in sorted(years):
        items = sorted(years[year], key=lambda x: x.date)
        # 구글이 본문만 보고 맞춘 기사(제목에 이름이 없음)는 잡음이 많다 (1996년 '안기부 건물 폭파' 같은 것).
        # 제목에 이름이 든 기사가 셋 이상이면 그것만 남긴다. 옛날 기사는 제목에 이름이 드물어 전부 둔다.
        named = [i for i in items if m.name in i.title]
        if len(named) >= 3:
            items = named
        # 한 해 안에서 고르게 — 앞뒤만 남기지 않고 띄엄띄엄 뽑는다
        if len(items) > per_year:
            step = len(items) / per_year
            items = [items[int(i * step)] for i in range(per_year)]
        out.append((year, items))
    return out


def render_tables(cfg: Config, m: Materials, today: str) -> str:
    from .render import make_env

    env = make_env()
    return env.get_template("profile_tables.md.j2").render(
        m=m, member=m.member, wiki=m.wiki, bills=m.bills, today=today,
        elections=election_rows(m), parties=party_rows(m), years=news_by_year(m),
        news_total=len({i.link for w in m.windows for i in w.items} | {i.link for i in m.recent}),
    )


def save_sources(path: Path, m: Materials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


# ── 정리 글 (모델 출력 → 인물정리.md) ────────────────────────


def _used_ids(profile) -> set[str]:
    ids: set[str] = set()
    for t in profile.timeline:
        ids.update(t.source_ids)
    for c in profile.chapters:
        ids.update(c.source_ids)
    for c in profile.controversies:
        ids.update(c.source_ids)
    for r in profile.recent:
        ids.update(re.findall(r"\b(N\d+-\d+|[ABW])\b", r))
    return ids


def checks(cfg: Config, profile, rendered: str) -> list[str]:
    """발행 전 점검. 데일리 점검표의 금지 표현 검사와 같은 낱말 목록을 쓴다."""
    from .checklist import _find_phrases

    out: list[str] = []
    banned = list(cfg.get("video.banned_phrases", []) or [])
    hits = _find_phrases(rendered, banned)
    if hits:
        out.append("⚠️ 금지 표현이 들어 있습니다: " + ", ".join(hits) + " — 인용이라도 영상에서는 빼세요.")
    missing = [c.topic for c in profile.controversies if "자료에 없" in (c.response or "") or not (c.response or "").strip()]
    if missing:
        out.append("⚠️ 해명이 자료에 없는 논란: " + ", ".join(missing) + " — 당사자 입장을 따로 찾아 넣으세요.")
    wiki_only = [c for c in profile.cautions if "위키백과" in c]
    if wiki_only:
        out.append(f"ℹ️ 위키백과에만 근거한 사실 {len(wiki_only)}건 — '확인할 것' 을 보세요.")
    if not out:
        out.append("✅ 금지 표현 없음 · 논란마다 당사자 입장 있음")
    return out


def render(cfg: Config, m: Materials, profile, today: str, out_dir: Path,
           filename: str = "인물정리.md") -> tuple[Path, list[str]]:
    from .render import make_env

    env = make_env()
    links = m.source_links()
    used = _used_ids(profile) & set(links)
    total = sum(int(s.seconds) for s in profile.outline)
    first = env.get_template("profile.md.j2").render(
        p=profile, member=m.member, bills=m.bills, notes=m.notes, today=today,
        total_seconds=total, links=links, used_ids=used, checks=[],
        wiki_title=(m.wiki or {}).get("title", ""),
    )
    found = checks(cfg, profile, first)
    text = env.get_template("profile.md.j2").render(
        p=profile, member=m.member, bills=m.bills, notes=m.notes, today=today,
        total_seconds=total, links=links, used_ids=used, checks=found,
        wiki_title=(m.wiki or {}).get("title", ""),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(text, encoding="utf-8")
    return path, found
