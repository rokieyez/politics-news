"""인물 조사(profile). 망을 타지 않는다 — 열린국회정보·위키백과·구글뉴스 응답을 붙박이로 둔다."""

from __future__ import annotations

import json
from datetime import date

import pytest

from rebrief import profile as prof
from rebrief.models import Chapter, Controversy, OutlineSection, PersonProfile, TimelineEntry


_MEMBER_ROW = {
    "NAAS_CD": "MLH1404S", "NAAS_NM": "김민석", "NAAS_CH_NM": "金民錫", "BIRDY_DT": "1964-05-29",
    "BIRDY_DIV_CD": "양", "PLPT_NM": "새정치국민회의/새천년민주당/더불어민주당/더불어민주당",
    "ELECD_NM": "서울 영등포구을/서울 영등포구을/서울 영등포구을/서울 영등포구을",
    "ELECD_DIV_NM": "지역구/지역구/지역구/지역구", "RLCT_DIV_NM": "4선",
    "GTELT_ERACO": "제15대, 제16대, 제21대, 제22대", "CMIT_NM": "국방위원회",
    "BRF_HST": "○ 학력\r\n- 서울대학교 사회학과 졸업\r\n○ 약력\r\n現) 제22대 국회의원",
}
_OTHER_ROW = dict(_MEMBER_ROW, NAAS_CD="X", BIRDY_DT="1948-02-13", GTELT_ERACO="제14대", PLPT_NM="민주자유당",
                  ELECD_NM="부산 동래구", ELECD_DIV_NM="지역구", RLCT_DIV_NM="초선", BRF_HST="")

_WIKI_TEXT = """김민석(金民錫, 1964년 5월 29일~)은 대한민국의 정치인이다.

== 생애 ==
1985년 서울대학교 총학생회장에 선출되었다.

=== 정계 입문 ===
1992년 총선에 출마했다.

== 경력 ==
제49대 국무총리

== 같이 보기 ==
* 목록
"""

_ELECTION_WT = """== 역대 선거 결과 ==
{{선거기록 시작|KR|개인}}
{{선거기록/KR/개인 | 1992년 | [[대한민국 제14대 국회의원 선거|총선]] | 14대 | [[대한민국의 국회의원|국회의원]] | [[영등포구 을|서울 영등포구 을]] | 민주당1991 | 48,151표 | 40.95 | 2위 | [[...|낙선]] |  | }}
{{선거기록 끝}}
"""
_PARTY_WT = """== 소속 정당 ==
{| class="wikitable"
! colspan="2" | 소속 정당 !! 소속 기간 !! 비고
|-
|style="background-color: {{정당색/대한민국|민주당1990|색1}}" | || [[민주당 (1990년 대한민국)|민주당]] || 1990~1991 || 정계 입문
|-
|style="background-color: {{정당색/대한민국|무소속|색1}}" | || [[무소속]] || 1995 || 탈당
|}
"""

_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>김민석 전의원 국민통합 21 합류 - KBS 뉴스</title><link>https://n/1</link>
<pubDate>Thu, 17 Oct 2002 00:00:00 GMT</pubDate><source url="https://kbs">KBS 뉴스</source></item>
<item><title>김민석 전의원 `통합21`합류 - 문화일보</title><link>https://n/2</link>
<pubDate>Thu, 17 Oct 2002 01:00:00 GMT</pubDate><source url="https://m">문화일보</source></item>
<item><title>김민석 "정치자금법 위반한 적 없어" - KBS 뉴스</title><link>https://n/3</link>
<pubDate>Wed, 29 Oct 2008 00:00:00 GMT</pubDate><source url="https://kbs">KBS 뉴스</source></item>
</channel></rss>"""


class _Resp:
    def __init__(self, data=None, content=b""):
        self._data = data
        self.content = content

    def json(self):
        return self._data


def _assembly(endpoint, rows, total):
    return {endpoint: [{"head": [{"list_total_count": total}, {"RESULT": {"CODE": "INFO-000"}}]},
                       {"row": rows}]}


def _fake_get(calls: list):
    def get(url, params=None):
        params = params or {}
        calls.append((url, params))
        if prof.MEMBERS in url:
            return _Resp(_assembly(prof.MEMBERS, [_MEMBER_ROW, _OTHER_ROW], 2))
        if prof.BILLS in url:
            rows = [{"BILL_NAME": f"법안{params['AGE']}", "PROPOSE_DT": "2025-04-24", "PROPOSER": "김민석의원 등 10인",
                     "COMMITTEE": "보건복지위원회", "DETAIL_LINK": "https://b/1"}]
            return _Resp(_assembly(prof.BILLS, rows, 11 if params["AGE"] == 22 else 86))
        if url == prof.WIKI_API:
            if params.get("list") == "search":
                return _Resp({"query": {"search": [{"title": "김민석 (1964년)"}, {"title": "김민석"}]}})
            if params.get("prop", "").startswith("extracts") and params.get("exintro"):
                return _Resp({"query": {"pages": [
                    {"title": "김민석 (1964년)", "extract": "김민석(1964년 5월 29일~)은 대한민국의 정치인이다."},
                    {"title": "김민석", "extract": "김민석은 동음이의어 문서이다."}]}})
            if params.get("action") == "parse" and params.get("prop") == "sections":
                return _Resp({"parse": {"sections": [{"index": "5", "line": "역대 선거 결과"},
                                                     {"index": "6", "line": "소속 정당"}, {"index": "7", "line": "각주"}]}})
            if params.get("action") == "parse" and params.get("prop") == "wikitext":
                return _Resp({"parse": {"wikitext": _ELECTION_WT if params["section"] == "5" else _PARTY_WT}})
            return _Resp({"query": {"pages": [{"title": "김민석 (1964년)", "extract": _WIKI_TEXT,
                                               "fullurl": "https://ko.wikipedia.org/wiki/x",
                                               "revisions": [{"timestamp": "2026-08-31T00:00:00Z"}]}]}})
        if url == prof.GNEWS:
            return _Resp(content=_RSS.encode("utf-8"))
        raise AssertionError(f"뜻밖의 요청: {url} {params}")
    return get


@pytest.fixture
def patched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(prof, "_get", _fake_get(calls))
    monkeypatch.setattr(prof.time, "sleep", lambda *_: None)
    return calls


def test_member_row_and_disambiguation():
    m = prof.member_from_row(_MEMBER_ROW)
    assert m.birth_year == 1964 and m.ages == [15, 16, 21, 22] and m.parties[-1] == "더불어민주당"
    assert "\r" not in m.career
    members = [prof.member_from_row(_OTHER_ROW), m]
    assert prof.pick_member(members, None) is m                 # 최근 대수가 먼저
    assert prof.pick_member(members, "1948").birth == "1948-02-13"
    assert prof.pick_member(members, "1999") is None            # 맞는 사람 없음


def test_wiki_sections_and_tables():
    intro, secs = prof.split_sections(_WIKI_TEXT)
    assert intro.startswith("김민석(") and "같이 보기" not in secs
    assert "▸ 정계 입문" in secs["생애"] and secs["경력"] == "제49대 국무총리"
    rows = prof.wikitext_table_lines(_ELECTION_WT)
    assert rows == ["1992년 · 총선 · 14대 · 국회의원 · 서울 영등포구 을 · 민주당1991 · 48,151표 · 40.95 · 2위 · 낙선"]
    rows = prof.wikitext_table_lines(_PARTY_WT)
    assert rows == ["민주당 · 1990~1991 · 정계 입문", "무소속 · 1995 · 탈당"]


def test_news_parse_strips_source_and_dedupes():
    items = prof.parse_news(_RSS.encode("utf-8"))
    assert [i.title for i in items][:2] == ["김민석 전의원 국민통합 21 합류", "김민석 전의원 `통합21`합류"]
    assert items[0].source == "KBS 뉴스" and items[0].date == "2002-10-17"
    # 제목이 똑같은 기사는 하나만 남는다
    twice = _RSS.replace("</channel>", _RSS.split("<channel>")[1].split("</channel>")[0] + "</channel>")
    assert len(prof.parse_news(twice.encode("utf-8"))) == 3


def test_windows_and_start_year():
    assert prof.plan_windows(1995, 2026, 4) == [(1995, 1998), (1999, 2002), (2003, 2006), (2007, 2010),
                                                (2011, 2014), (2015, 2018), (2019, 2022), (2023, 2026)]
    assert len(prof.plan_windows(1960, 2026, 4)) <= 10          # 창이 10개를 넘으면 넓힌다
    m = prof.member_from_row(_MEMBER_ROW)
    assert prof.career_start_year(m, None, date(2026, 9, 9)) == 1995
    assert prof.career_start_year(m, {"intro": "", "tables": {"역대 선거 결과": [["1992년", "총선"]]}}, date(2026, 9, 9)) == 1992
    assert prof.career_start_year(None, {"intro": "2010년 정계에 입문"}, date(2026, 9, 9)) == 2010
    assert prof.career_start_year(None, None, date(2026, 9, 9)) == 2014


def test_collect_end_to_end(cfg, patched, tmp_path):
    m = prof.collect(cfg, "김민석", today=date(2026, 9, 9))
    assert m.member and m.member.birth == "1964-05-29"
    assert len(m.others) == 1 and any("같은 이름" in n for n in m.notes)
    assert m.wiki["title"] == "김민석 (1964년)" and "역대 선거 결과" in m.wiki["sections"]
    assert m.bills["by_age"] == {"제22대": 11, "제21대": 86} and m.bills["sample"]
    assert m.recent and len(m.windows) == 10       # 위키 정당 표의 1990년부터
    # 제20대 아래 대수는 발의법률안을 부르지 않는다
    assert {c[1].get("AGE") for c in patched if prof.BILLS in c[0]} == {22, 21}

    text = prof.format_materials(m)
    for tag in ("[A]", "[W]", "[W·역대 선거 결과]", "[B]", "[N0]", "[N1] 1990~1993", "(N1-1)"):
        assert tag in text
    assert "인증키가 없어" in text

    links = m.source_links()
    assert links["W"][1] == "https://ko.wikipedia.org/wiki/x" and "N1-1" in links
    prof.save_sources(tmp_path / "s.json", m)
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["member"]["name"] == "김민석"
    assert prof.slug("김민석", "1964-05-29") == "김민석-1964" and prof.slug("홍 길동", None) == "홍길동"


def test_collect_without_member(cfg, monkeypatch):
    """국회의원을 지낸 적 없는 사람 — 인적사항은 INFO-200, 나머지는 그대로 돈다."""
    def get(url, params=None):
        params = params or {}
        if prof.MEMBERS in url:
            return _Resp({"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})
        if url == prof.WIKI_API:
            return _Resp({"query": {"search": [], "pages": []}})
        if url == prof.GNEWS:
            return _Resp(content=_RSS.encode("utf-8"))
        raise AssertionError(url)
    monkeypatch.setattr(prof, "_get", get)
    monkeypatch.setattr(prof.time, "sleep", lambda *_: None)
    m = prof.collect(cfg, "황교안", today=date(2026, 9, 9))
    assert m.member is None and m.bills is None and m.wiki is None
    assert any("국회의원 기록이 없습니다" in n for n in m.notes)
    assert m.recent and m.windows                                 # 기사 검색은 12년 전부터


def _profile() -> PersonProfile:
    return PersonProfile(
        name="김민석", one_liner="4선 국회의원", current_roles=["더불어민주당 대표"],
        timeline=[TimelineEntry(when="1996", event="제15대 국회의원 당선", source_ids=["A", "W"])],
        chapters=[Chapter(title="첫 당선", period="1992~2000", summary="요약.", facts=["사실"], source_ids=["N1-1"])],
        bills_note="법안 97건.",
        controversies=[Controversy(topic="정치자금", when="2008", allegation="검찰이 기소했다",
                                   response="해명은 자료에 없음", status="결과는 자료에 없음", source_ids=["N2-1"])],
        recent=["당대표로 선출됐다 (N0-1)"],
        outline=[OutlineSection(heading="도입", seconds=60, points=["누구인가"]),
                 OutlineSection(heading="본론", seconds=300, points=["발자취"])],
        title_candidates=["제목1", "제목2", "제목3"], cautions=["위키백과에만 있음: 총학생회장"], unknowns=[],
    )


def test_render_and_checks(cfg, patched, tmp_path):
    m = prof.collect(cfg, "김민석", today=date(2026, 9, 9))
    path, found = prof.render(cfg, m, _profile(), "2026-09-09", tmp_path, filename="x_AI정리.md")
    assert path.name == "x_AI정리.md"
    text = path.read_text(encoding="utf-8")
    assert "# 김민석 — 인물 정리" in text and "| 1996 | 제15대 국회의원 당선 | A, W |" in text
    assert "총 6분 0초" in text
    assert "- **N1-1**" in text and "- **N2-1**" in text and "- **N0-1**" in text
    assert "- **N3-1**" not in text                               # 안 쓴 근거는 목록에 없다
    assert any("해명이 자료에 없는 논란: 정치자금" in f for f in found)
    assert any("위키백과에만 근거한 사실 1건" in f for f in found)
    assert not any("금지 표현" in f for f in found)


def test_checks_flag_banned_phrase(cfg):
    p = _profile()
    p.controversies = []
    p.cautions = []
    p.chapters[0].summary = "상대를 종북이라 불렀다."
    found = prof.checks(cfg, p, p.chapters[0].summary)
    assert any("금지 표현" in f and "종북" in f for f in found)
    p.chapters[0].summary = "평범한 요약."
    assert prof.checks(cfg, p, p.chapters[0].summary) == ["✅ 금지 표현 없음 · 논란마다 당사자 입장 있음"]


def test_file_base_and_tables(cfg, patched):
    m = prof.collect(cfg, "김민석", today=date(2026, 9, 9))
    assert prof.file_base(m, "2026-09-09") == "김민석(1964,더불어민주당)_2026-09-09"
    bare = prof.Materials(name="황 교안", fetched_at="")
    assert prof.file_base(bare, "2026-09-09") == "황교안_2026-09-09"
    assert prof.file_base(bare, "2026-09-09", birth="1957") == "황교안(1957)_2026-09-09"

    rows = prof.election_rows(m)
    assert rows[0]["연도"] == "1992년" and rows[0]["결과"] == "낙선" and rows[0]["득표율"] == "40.95%"
    assert [p["정당"] for p in prof.party_rows(m)] == ["민주당", "무소속"]
    years = prof.news_by_year(m)
    assert [y for y, _ in years] == ["2002", "2008"]      # 같은 제목은 하나로, 해마다 묶임

    text = prof.render_tables(cfg, m, "2026-09-09")
    assert "# 김민석 — 자료 정리표" in text and "| 1992년 | 총선 14대 |" in text
    assert "### 2002" in text and "AI 는 쓰지 않았고" in text
    assert "위키백과 「김민석 (1964년)」" in text

    # 위키가 없으면 국회 기록만으로 표를 채운다
    m.wiki = None
    rows = prof.election_rows(m)
    assert [r["대수"] for r in rows] == ["제15대", "제16대", "제21대", "제22대"] and rows[0]["연도"] == "1996년"
    assert prof.party_rows(m)[0] == {"정당": "새정치국민회의", "기간": "제15대", "비고": "국회 기록"}


def test_cli_default_is_tables_only(cfg, patched, monkeypatch, capsys):
    from rebrief import cli

    monkeypatch.setattr(cli, "load_config", lambda *_: cfg)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _NoModel:                       # 시험이 진짜 모델을 부르면 여기서 터진다
        def __init__(self, *_a, **_k):
            raise AssertionError("시험이 모델을 불렀습니다")
    monkeypatch.setattr("rebrief.llm.ContentGenerator", _NoModel)
    assert cli.main(["profile", "김민석", "--date", "2026-09-09"]) == 0
    out = capsys.readouterr().out
    assert "정리표 →" in out and "자료묶음 →" not in out and "제22대 11건" in out
    folder = cfg.output_dir / "profiles" / "김민석-1964"
    base = "김민석(1964,더불어민주당)_2026-09-09"
    assert (folder / f"{base}_정리표.md").exists() and (folder / "sources.json").exists()
    assert not (folder / f"{base}.md").exists() and not (folder / f"{base}_AI정리.md").exists()

    # --pack 은 claude.ai 에 붙일 자료묶음을 더한다
    assert cli.main(["profile", "김민석", "--date", "2026-09-09", "--pack"]) == 0
    pack = (folder / f"{base}.md").read_text(encoding="utf-8")
    assert "절대 규칙" in pack and "[N1] 1990~1993" in pack

    # --llm 인데 열쇠가 없으면 정리표는 남기고 1 로 끝난다
    assert cli.main(["profile", "김민석", "--date", "2026-09-09", "--llm"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_wiki_match_tolerates_birth_year_off_by_one():
    intro = "이재명(李在明, 1963년 12월 8일~)은 대한민국의 정치인이다."
    assert prof._is_this_person(intro, "이재명", 1964)              # 호적 1964, 실제 1963
    assert not prof._is_this_person(intro, "이재명", 1970)
    assert prof._is_this_person(intro, "이재명", 1970, strict=False)
    assert prof._is_this_person("홍준표는 대한민국의 검사 출신 변호사이자 국회의원이다.", "홍준표", None)
    assert not prof._is_this_person("김민석은 대한민국의 피겨 스케이팅 선수이다.", "김민석", None)
    assert prof.wiki_birth_year(intro) == 1963 and prof.wiki_birth_year("연도 없음") is None


def test_start_year_uses_wiki_tables():
    """머리글엔 2024년뿐이어도 정당 표가 2011년부터면 거기서 시작한다 (이준석)."""
    wiki = {"intro": "이준석(1985년 3월 31일~)은 정치인이다. 2024년 총선에서 당선되었다.",
            "tables": {"소속 정당": [["한나라당", "2011~2012", "입당"], ["개혁신당", "2024~현재", "창당"]]}}
    m = prof.member_from_row(dict(_MEMBER_ROW, BIRDY_DT="1985-03-31", GTELT_ERACO="제22대"))
    assert prof.career_start_year(m, wiki, date(2026, 9, 9)) == 2011
    assert prof.career_start_year(m, None, date(2026, 9, 9)) == 2023


def test_assembly_blocked_falls_back_to_wiki(cfg, patched, monkeypatch):
    """깃허브 서버에서는 열린국회정보가 접속을 막는다 — 위키 표로 기본 정보·생년·정당을 채운다."""
    import requests

    def blocked(*_a, **_k):
        raise requests.ConnectTimeout("차단")
    monkeypatch.setattr(prof, "find_members", blocked)
    m = prof.collect(cfg, "김민석", today=date(2026, 9, 9))
    assert m.member is None and m.bills is None
    assert any("닿지 못해" in n for n in m.notes) and not any("기록이 없습니다" in n for n in m.notes)
    basics = prof.wiki_basics(m.wiki)
    assert basics["birth_year"] == 1964 and basics["party"] == "무소속" and basics["won"] == []
    assert prof.file_base(m, "2026-09-09") == "김민석(1964,무소속)_2026-09-09"
    assert prof.folder_name(m, None) == "김민석-1964"
    text = prof.render_tables(cfg, m, "2026-09-09")
    assert "값 (위키백과 표에서)" in text and "| 생년 | 1964 |" in text and "자료에 없음" in text
    assert "| 1992년 | 총선 14대 |" in text                       # 선거 이력은 위키에서 그대로


def test_not_a_member_note(cfg, monkeypatch):
    """국회의원을 지낸 적 없는 사람은 '없다' 로 안내한다 (못 받은 것과 가른다)."""
    def get(url, params=None):
        if prof.MEMBERS in url:
            return _Resp({"RESULT": {"CODE": "INFO-200", "MESSAGE": "없음"}})
        if url == prof.WIKI_API:
            return _Resp({"query": {"search": [], "pages": []}})
        if url == prof.GNEWS:
            return _Resp(content=_RSS.encode("utf-8"))
        raise AssertionError(url)
    monkeypatch.setattr(prof, "_get", get)
    monkeypatch.setattr(prof.time, "sleep", lambda *_: None)
    m = prof.collect(cfg, "황교안", today=date(2026, 9, 9))
    assert any("기록이 없습니다" in n for n in m.notes)
    assert prof.folder_name(m, None) == "황교안" and prof.file_base(m, "2026-09-09") == "황교안_2026-09-09"
    assert "국회 공식 기록도, 위키백과 표도 없습니다" in prof.render_tables(cfg, m, "2026-09-09")
