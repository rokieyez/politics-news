"""국회·여론조사 직접 집계와 정치용 점검표 검사. 망을 타지 않는다 — 화면과 응답을 붙박이로 둔다."""

from __future__ import annotations

import json

import pytest

from rebrief import checklist as cl
from rebrief import civics


_LIST_HTML = """
<p class="row th"><span class="col">등록번호</span></p>
<a href="/portal/bbs/B0000005/view.do?nttId=19501&menuNo=200467&amp;pageIndex=1" class="row tr">
 <span class="col"><i class="tit"></i>17257</span>
 <span class="col"><i class="tit"></i>여론조사공정(주)</span>
 <span class="col"><i class="tit"></i>펜앤마이크</span>
 <span class="col"><i class="tit"></i>무선 ARS</span>
 <span class="col"><i class="tit"></i>무선전화번호</br>RDD</span>
 <span class="col tl"><i class="tit"></i>전국 정기(정례)조사 정당지지도 대통령선거 </span>
 <span class="col"><i class="tit"></i>2026-09-08</span>
 <span class="col"><i class="tit"></i>전국</span>
</a>
<a href="/portal/bbs/B0000005/view.do?nttId=19400&menuNo=200467&amp;pageIndex=1" class="row tr">
 <span class="col"><i class="tit"></i>17100</span>
 <span class="col"><i class="tit"></i>(주)여론조사꽃</span>
 <span class="col"><i class="tit"></i>(주)여론조사꽃</span>
 <span class="col"><i class="tit"></i>무선전화면접</span>
 <span class="col"><i class="tit"></i>무선전화번호</br>휴대전화 가상번호</span>
 <span class="col tl"><i class="tit"></i>전국 전체 정기(정례)조사정당지지도 </span>
 <span class="col"><i class="tit"></i>2026-08-20</span>
 <span class="col"><i class="tit"></i>전국</span>
</a>
"""

_VIEW_HTML = """
<table>
<tr><th>조사일시</th><td>2026-09-06 &nbsp; 15 시 00 분 ~ 21 시 40 분 2026-09-07 &nbsp; 12 시 00 분 ~ 20 시 50 분</td></tr>
<tr><th>표본의 크기 구분 조사완료 사례수(명)</th><td>1015</td></tr>
<tr><th>조사방법 1</th><td>무선 ARS</td></tr>
<tr><th>전체 응답률</th><td>2.1%</td></tr>
<tr><th>표본오차</th><td>95% 신뢰수준에 &plusmn;3.1%P</td></tr>
<tr><th>공표·보도 매체명</th><td>펜앤마이크</td></tr>
<tr><th>최초 공표·보도 지정일시</th><td>2026-09-08 14시 00분</td></tr>
</table>
"""


class _Resp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data

    def json(self):
        return self._data


def _assembly_page(endpoint, rows, total):
    return {endpoint: [{"head": [{"list_total_count": total},
                                 {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상"}}]},
                       {"row": rows}]}


def test_poll_list_and_detail_parse():
    polls = civics.parse_poll_list(_LIST_HTML)
    assert [p.reg_no for p in polls] == ["17257", "17100"]
    first = polls[0]
    assert first.agency == "여론조사공정(주)" and first.client == "펜앤마이크"
    assert first.method == "무선 ARS" and first.registered == "2026-09-08" and first.region == "전국"
    assert first.title.startswith("전국 정기") and first.ntt_id == "19501"

    detail = civics.parse_poll_detail(_VIEW_HTML)
    assert detail["period"] == "2026-09-06~07"          # 이틀에 걸친 조사는 날짜만 이어 적는다
    assert detail["sample_size"] == 1015
    assert detail["response_rate"] == "2.1%"
    assert detail["margin"] == "95% 신뢰수준에 ±3.1%p"   # HTML 엔티티가 풀린다
    assert detail["outlet"] == "펜앤마이크"
    for k, v in detail.items():
        setattr(first, k, v)
    line = first.summary()
    # 선거법이 요구하는 항목이 한 줄에 다 들어간다
    for must in ("여론조사공정(주)", "펜앤마이크 의뢰", "2026-09-06~07", "1,015명", "응답률 2.1%", "±3.1%p"):
        assert must in line, line


def test_polls_since_skips_older_and_opens_details(monkeypatch):
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params))
        if url == civics.NESDC_LIST:
            return _Resp(text=_LIST_HTML)
        return _Resp(text=_VIEW_HTML)

    monkeypatch.setattr(civics, "_get", fake_get)
    polls = civics.polls_since("2026-09-01", limit=8, pages=3)
    assert [p.reg_no for p in polls] == ["17257"]        # 8월 20일 등록분은 뺀다
    assert polls[0].sample_size == 1015
    # 옛 등록이 보이면 다음 쪽으로 넘어가지 않는다 (목록 1 + 상세 1)
    assert len(calls) == 2


def test_bills_without_key_show_five_but_do_not_count(monkeypatch):
    rows = [{"BILL_NAME": f"법안{i}", "PROPOSER": "홍길동의원 등 10인", "PROPOSE_DT": "2026-09-07",
             "DETAIL_LINK": f"https://x/{i}", "COMMITTEE": None} for i in range(5)]

    def fake_get(url, params=None):
        assert "KEY" not in (params or {})               # 키가 없으면 KEY 를 아예 빼고 부른다
        return _Resp(data=_assembly_page(civics.BILLS, rows, 19245))

    monkeypatch.setattr(civics, "_get", fake_get)
    monkeypatch.delenv("ASSEMBLY_API_KEY", raising=False)
    got = civics.bills_since("2026-09-01", key="")
    assert got["sample"] is True and got["count"] is None   # 5건을 '이번 주 5건' 으로 적으면 거짓말
    assert len(got["latest"]) == 5 and got["age_total"] == 19245


def test_bills_with_key_pages_until_the_date_passes(monkeypatch):
    def page(n):
        base = 1000 - (n - 1) * 100
        return [{"BILL_NAME": f"법안{base - i}", "PROPOSER": "x", "COMMITTEE": "",
                 "PROPOSE_DT": "2026-09-05" if n == 1 else "2026-08-01", "DETAIL_LINK": ""}
                for i in range(100)]

    seen = []

    def fake_get(url, params=None):
        seen.append(params["pIndex"])
        assert params["KEY"] == "abc"
        return _Resp(data=_assembly_page(civics.BILLS, page(params["pIndex"]), 19245))

    monkeypatch.setattr(civics, "_get", fake_get)
    got = civics.bills_since("2026-09-01", key="abc")
    assert got["count"] == 100 and got["sample"] is False
    assert seen == [1, 2]                                  # 둘째 쪽에서 날짜가 앞으로 넘어가 멈춘다


def test_plenary_groups_by_result(monkeypatch):
    rows = [
        {"BILL_NM": "가법", "PROC_RESULT_CD": "원안가결", "RGS_PROC_DT": "2026-09-04",
         "COMMITTEE_NM": "법사위", "YES_TCNT": "178", "NO_TCNT": "0", "BLANK_TCNT": "2", "LINK_URL": ""},
        {"BILL_NM": "나법", "PROC_RESULT_CD": "철회", "RGS_PROC_DT": "2026-09-02",
         "COMMITTEE_NM": "정무위", "YES_TCNT": None, "NO_TCNT": None, "BLANK_TCNT": None, "LINK_URL": ""},
        {"BILL_NM": "옛법", "PROC_RESULT_CD": "원안가결", "RGS_PROC_DT": "2026-07-01",
         "COMMITTEE_NM": "", "YES_TCNT": "1", "NO_TCNT": "1", "BLANK_TCNT": "1", "LINK_URL": ""},
    ]
    monkeypatch.setattr(civics, "_get", lambda url, params=None: _Resp(data=_assembly_page(civics.PLENARY, rows, 3)))
    got = civics.plenary_since("2026-09-01", key="k")
    assert got["count"] == 2 and got["by_result"] == {"원안가결": 1, "철회": 1}
    assert got["items"][0]["name"] == "가법" and got["items"][0]["yes"] == 178


def test_collect_survives_a_dead_source(cfg, monkeypatch):
    """한 곳이 죽어도 다른 곳 값은 남고, 둘 다 죽으면 {} 다."""
    cfg.settings["civics"] = {"enabled": True, "days": 7, "polls_max": 3}

    def fake_get(url, params=None):
        if url.startswith(civics.ASSEMBLY_BASE):
            raise RuntimeError("국회 서버 점검")
        return _Resp(text=_LIST_HTML if url == civics.NESDC_LIST else _VIEW_HTML)

    monkeypatch.setattr(civics, "_get", fake_get)
    monkeypatch.delenv("ASSEMBLY_API_KEY", raising=False)
    data = civics.collect(cfg, "2026-09-08")
    assert data["polls"] and data["polls"][0]["summary"].startswith("여론조사공정")
    assert len(data["warnings"]) == 2 and data["bills"] == {} and data["plenary"] == {}

    monkeypatch.setattr(civics, "_get", lambda url, params=None: (_ for _ in ()).throw(RuntimeError("죽음")))
    assert civics.collect(cfg, "2026-09-08") == {}


def test_civics_page_and_blog_block(cfg, tmp_path):
    from rebrief.render import Renderer, civics_block_html, civics_block_markdown

    data = {"as_of": "2026-09-08", "days": 7, "since": "2026-09-01", "sample": True, "warnings": [],
            "bills": {"count": None, "sample": True, "age_total": 19245,
                      "latest": [{"name": "한국발전공사법안", "proposer": "박해철의원 등 11인",
                                  "date": "2026-09-07", "link": "https://x/1", "committee": ""}]},
            "plenary": {"count": None, "sample": True, "by_result": {"원안가결": 1},
                        "items": [{"name": "가법", "result": "원안가결", "date": "2026-09-04", "committee": "법사위",
                                   "yes": 178, "no": 0, "blank": 2, "link": ""}]},
            "polls": [{"title": "전국 정례조사", "region": "전국", "agency": "한국갤럽", "client": "한국갤럽",
                       "summary": "한국갤럽 · 2026-09-02~04 · 1,001명 · 응답률 12.0% · 95% 신뢰수준에 ±3.1%p",
                       "outlet": "", "link": "https://nesdc/1", "registered": "2026-09-05"}]}
    renderer = Renderer(cfg, tmp_path / "out", "2026-09-08")
    page = renderer.civics(data)
    text = page.read_text(encoding="utf-8")
    assert "견본 모드" in text and "한국발전공사법안" in text and "178 / 0 / 2" in text
    assert json.loads((tmp_path / "out" / "civics.json").read_text(encoding="utf-8"))["polls"][0]["agency"] == "한국갤럽"

    html = civics_block_html(data)
    assert "직접 센 숫자" in html and "한국갤럽" in html and "±3.1%p" in html
    assert "중앙선거여론조사심의위원회" in html        # 인용 안내는 늘 붙는다
    md = civics_block_markdown(data)
    assert "원안가결 1건" in md
    assert civics_block_html({}) == "" and civics_block_markdown(None) == ""


# ── 정치용 점검표 ────────────────────────────────────────────


def test_election_blackout_window():
    elections = [{"name": "제23대 국회의원선거", "date": "2028-04-12"}]
    assert cl.election_blackout("2028-04-05", elections) is None            # D-7 은 아직
    assert cl.election_blackout("2028-04-06", elections) == ("제23대 국회의원선거", "2028-04-12")  # D-6 부터
    assert cl.election_blackout("2028-04-12", elections) == ("제23대 국회의원선거", "2028-04-12")  # 선거일까지
    assert cl.election_blackout("2028-04-13", elections) is None
    assert cl.election_blackout("2028-04-08", [{"name": "x", "date": "잘못된 날짜"}]) is None


def test_side_balance_counts_longest_alias_once():
    sides = {"더불어민주당": ["더불어민주당", "민주당"], "국민의힘": ["국민의힘", "국힘"]}
    got = cl.side_balance("더불어민주당은 찬성했고 민주당 의원들도 동의했다. 국민의힘은 반대했다.", sides)
    assert got == {"더불어민주당": 2, "국민의힘": 1}   # '더불어민주당' 안의 '민주당' 은 두 번 세지 않는다


def test_checklist_flags_lopsided_mentions_and_blackout(cfg, monkeypatch):
    from tests.test_pipeline import make_pack, make_post

    cfg.settings["balance"] = {"min_total": 4, "max_share": 0.75,
                               "sides": {"A당": ["A당"], "B당": ["B당"]}}
    cfg.settings["elections"] = [{"name": "시험 선거", "date": "2026-09-10"}]
    post = make_post()
    post.body_markdown += "\n\nA당은 말했다. A당은 또 말했다. A당이 A당을. B당은 침묵."
    items = cl.build(cfg, post=post, pack=make_pack(), llm_used=True, date="2026-09-08")
    balance = next(i for i in items if i.key == "balance")
    assert balance.level == cl.WARN and "A당 4" in balance.title and "B당 1" in balance.title
    election = next(i for i in items if i.key == "election")
    assert election.level == cl.WARN and "시험 선거" in election.title      # 여론조사 수치가 없으면 안내만

    post.body_markdown += "\n\n지지율은 45%로 나타났다."
    items = cl.build(cfg, post=post, pack=make_pack(), llm_used=True, date="2026-09-08")
    election = next(i for i in items if i.key == "election")
    assert election.level == cl.FAIL                                        # 금지 기간에 수치까지 있으면 막는다

    # 균형이 맞으면 ✅, 언급이 적으면 항목 자체를 만들지 않는다
    post = make_post(); post.body_markdown += "\n\nA당과 B당이 합의했다. A당은 B당에 제안했다."
    items = cl.build(cfg, post=post, pack=make_pack(), llm_used=True, date="2026-01-01")
    assert next(i for i in items if i.key == "balance").level == cl.OK
    assert not any(i.key == "election" for i in items)
    post = make_post(); post.body_markdown += "\n\nA당이 말했다."
    items = cl.build(cfg, post=post, pack=make_pack(), llm_used=True, date="2026-01-01")
    assert not any(i.key == "balance" for i in items)


@pytest.mark.parametrize("body,missing", [
    ("한국갤럽이 9월 2~4일 1,001명에게 물은 결과 지지율 45%였다. 응답률 12.0%, 오차범위 ±3.1%p.", []),
    ("지지율이 45%로 나타났다.", ["조사기관", "오차범위", "조사 기간", "표본 크기", "응답률"]),
    ("오늘 국회는 예산안을 처리했다.", []),
])
def test_poll_citation_gaps(body, missing):
    assert cl.poll_citations(body) == missing
