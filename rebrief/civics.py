"""국회·여론조사 — 정치 뉴스의 '직접 센 숫자'.

기사에 실린 숫자를 받아쓰는 대신 원자료를 직접 셉니다. 두 곳입니다.

* **열린국회정보(open.assembly.go.kr)** — 국회의원 발의법률안(`nzmimeepazxkubdpn`)과
  본회의 처리안건(`nwbpacrgavhjryiph`). 인증키(`ASSEMBLY_API_KEY`) 없이도 응답하지만
  **견본 모드라 한 번에 5건만** 옵니다 (2026-09-08 실측: pSize 를 100 으로 줘도 5건).
  키가 있으면 쪽을 넘겨 기간 안의 건수를 셉니다. 키는 홈페이지 회원가입 → 마이페이지 →
  인증키 신청(무료). `KEY=sample` 처럼 아무 값이나 넣으면 ERROR-290 으로 거부되므로
  키가 없을 때는 **KEY 를 아예 빼고** 부릅니다.
* **중앙선거여론조사심의위원회(nesdc.go.kr)** — 등록된 선거 여론조사 목록. 인증키가 없고
  목록 화면을 직접 파싱합니다. 상세 화면에 조사일시·표본·응답률·표본오차·공표 매체가 있어,
  글이 여론조사를 인용할 때 붙여야 하는 개요(공직선거법 108조)를 여기서 그대로 가져옵니다.

둘 다 실패하면 조용히 빈 값을 냅니다 — 없어도 글은 나갑니다. 숫자는 프로그램이 그대로
글에 넣습니다. 모델을 거치면 대조할 원문이 없기 때문입니다.
"""

from __future__ import annotations

import html as html_mod
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests

from .config import Config

log = logging.getLogger(__name__)

ASSEMBLY_BASE = "https://open.assembly.go.kr/portal/openapi/"
BILLS = "nzmimeepazxkubdpn"        # 국회의원 발의법률안
PLENARY = "nwbpacrgavhjryiph"      # 본회의 처리안건 (법률안)
AGE = 22                           # 제22대 국회 (2024-05-30 ~ 2028-05-29)
PAGE = 100
SAMPLE_ROWS = 5                    # 키 없이 부르면 이만큼만 온다

NESDC_LIST = "https://www.nesdc.go.kr/portal/bbs/B0000005/list.do"
NESDC_VIEW = "https://www.nesdc.go.kr/portal/bbs/B0000005/view.do"
NESDC_MENU = "200467"
UA = "Mozilla/5.0 (compatible; rebrief/1.0; +https://github.com/rokieyez/politics-news)"
TIMEOUT = 20


class CivicsError(RuntimeError):
    pass


def assembly_key() -> str:
    return (os.environ.get("ASSEMBLY_API_KEY") or "").strip()


def _get(url: str, params: dict | None = None) -> requests.Response:
    """시험에서 갈아끼우는 자리. 모듈 밖에서 requests 를 패치하면 수집기 스텁과 부딪힌다."""
    resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


# ── 열린국회정보 ─────────────────────────────────────────────


def _assembly_rows(endpoint: str, *, key: str, page: int, size: int = PAGE) -> tuple[list[dict], int]:
    """한 쪽. (행 목록, 전체 건수). 자료 없음(INFO-200)은 빈 목록이지 오류가 아니다."""
    params: dict = {"Type": "json", "pIndex": page, "pSize": size, "AGE": AGE}
    if key:
        params["KEY"] = key
    data = _get(ASSEMBLY_BASE + endpoint, params).json()
    body = data.get(endpoint)
    if not body:                                    # {"RESULT": {"CODE": "ERROR-290", ...}}
        code = ((data.get("RESULT") or {}).get("CODE") or "?")
        msg = ((data.get("RESULT") or {}).get("MESSAGE") or "")
        raise CivicsError(f"열린국회정보 {code} {msg}".strip())
    head = body[0].get("head") or []
    total = int((head[0] or {}).get("list_total_count", 0)) if head else 0
    rows = (body[1].get("row") or []) if len(body) > 1 else []
    return rows, total


def bills_since(since: str, *, key: str, max_pages: int = 20) -> dict:
    """`since`(YYYY-MM-DD) 이후 발의된 법률안. 목록이 최신순이라 날짜가 앞으로 넘어가면 멈춘다.

    키가 없으면 5건만 오므로 **건수를 세지 않고**(`count=None`) 최근 5건만 보여 준다.
    5건을 '이번 주 5건' 으로 적으면 거짓말이다.
    """
    sample = not key
    kept: list[dict] = []
    total = 0
    for page in range(1, max_pages + 1):
        rows, total = _assembly_rows(BILLS, key=key, page=page)
        if not rows:
            break
        stop = False
        for r in rows:
            proposed = (r.get("PROPOSE_DT") or "")[:10]
            if proposed and proposed < since:
                stop = True
                break
            kept.append(r)
        if stop or sample or len(rows) < PAGE:
            break
    latest = [{"name": r.get("BILL_NAME") or "", "proposer": r.get("PROPOSER") or "",
               "date": (r.get("PROPOSE_DT") or "")[:10], "link": r.get("DETAIL_LINK") or "",
               "committee": r.get("COMMITTEE") or ""} for r in kept[:5]]
    return {"count": None if sample else len(kept), "latest": latest,
            "sample": sample, "age_total": total}


def plenary_since(since: str, *, key: str, max_pages: int = 20) -> dict:
    """`since` 이후 본회의에서 처리된 법률안. 결과별(가결·부결·철회…) 건수와 목록.

    이 목록은 처리일 순이 아니라 의안번호 순이라 날짜로 멈출 수 없다. 키가 있으면 쪽을 다
    넘기고(1,700여 건 = 18쪽), 없으면 5건 안에서만 고른다.
    """
    sample = not key
    kept: list[dict] = []
    for page in range(1, max_pages + 1):
        rows, _total = _assembly_rows(PLENARY, key=key, page=page)
        if not rows:
            break
        for r in rows:
            done = (r.get("RGS_PROC_DT") or "")[:10]
            if done and done >= since:
                kept.append(r)
        if sample or len(rows) < PAGE:
            break
    kept.sort(key=lambda r: r.get("RGS_PROC_DT") or "", reverse=True)
    by_result: dict[str, int] = {}
    items = []
    for r in kept:
        result = (r.get("PROC_RESULT_CD") or "").strip() or "처리"
        by_result[result] = by_result.get(result, 0) + 1
        items.append({"name": r.get("BILL_NM") or "", "result": result,
                      "date": (r.get("RGS_PROC_DT") or "")[:10],
                      "committee": r.get("COMMITTEE_NM") or "",
                      "yes": _int(r.get("YES_TCNT")), "no": _int(r.get("NO_TCNT")),
                      "blank": _int(r.get("BLANK_TCNT")), "link": r.get("LINK_URL") or ""})
    return {"count": None if sample else len(kept), "by_result": by_result,
            "items": items[:12], "sample": sample}


def _int(value) -> int | None:
    try:
        return int(str(value).replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None


# ── 선거여론조사심의위원회 ───────────────────────────────────


@dataclass
class Poll:
    reg_no: str
    agency: str
    client: str
    method: str
    title: str
    registered: str
    region: str
    ntt_id: str
    period: str = ""          # 조사일시 → "2026-09-06~07"
    sample_size: int | None = None
    response_rate: str = ""   # "2.1%"
    margin: str = ""          # "95% 신뢰수준에 ±3.1%P"
    outlet: str = ""          # 공표·보도 매체명
    publish_at: str = ""      # 최초 공표·보도 지정일시
    link: str = ""
    extra: dict = field(default_factory=dict)

    def summary(self) -> str:
        """글에 붙일 한 줄 개요. 선거법이 요구하는 항목을 빠짐없이 한 줄에."""
        bits = [f"{self.agency}"]
        if self.client and self.client != self.agency:
            bits.append(f"{self.client} 의뢰")
        if self.period:
            bits.append(self.period)
        if self.sample_size:
            bits.append(f"{self.sample_size:,}명")
        if self.method:
            bits.append(self.method)
        if self.response_rate:
            bits.append(f"응답률 {self.response_rate}")
        if self.margin:
            bits.append(self.margin)
        return " · ".join(bits)


_ROW = re.compile(r'<a href="([^"]*view\.do\?nttId=(\d+)[^"]*)"[^>]*class="row tr"[^>]*>(.*?)</a>', re.S)
_COL = re.compile(r'<span class="col[^"]*"[^>]*>(.*?)</span>', re.S)
_PAIR = re.compile(r"<(?:th|dt)[^>]*>(.*?)</(?:th|dt)>\s*<(?:td|dd)[^>]*>(.*?)</(?:td|dd)>", re.S)


def _text(fragment: str) -> str:
    out = re.sub(r"<br\s*/?>", " ", fragment)
    out = re.sub(r"<[^>]+>", " ", out)
    out = html_mod.unescape(out).replace("\xa0", " ")
    return re.sub(r"\s+", " ", out).strip()


def parse_poll_list(page_html: str) -> list[Poll]:
    """목록 화면 한 쪽. 칸 차례: 등록번호·조사기관·의뢰자·조사방법·추출틀·명칭(지역)·등록일·시도."""
    polls = []
    for href, ntt_id, inner in _ROW.findall(page_html):
        cols = [_text(c) for c in _COL.findall(inner)]
        if len(cols) < 8:
            continue
        polls.append(Poll(reg_no=cols[0], agency=cols[1], client=cols[2], method=cols[3],
                          title=cols[5], registered=cols[6], region=cols[7], ntt_id=ntt_id,
                          link=f"{NESDC_VIEW}?nttId={ntt_id}&menuNo={NESDC_MENU}"))
    return polls


def parse_poll_detail(page_html: str) -> dict:
    """상세 화면에서 인용에 필요한 항목만. 화면의 th/td 짝을 훑어 낱말로 맞춘다."""
    pairs = [(_text(k), _text(v)) for k, v in _PAIR.findall(page_html)]
    out: dict = {}
    for key, value in pairs:
        if not value:
            continue
        if key.startswith("조사일시") and "period" not in out:
            days = re.findall(r"\d{4}-\d{2}-\d{2}", value)
            if days:
                first, last = days[0], days[-1]
                out["period"] = first if first == last else (
                    f"{first}~{last[8:]}" if first[:7] == last[:7] else f"{first}~{last[5:]}")
        elif "조사완료 사례수" in key and "sample_size" not in out:
            out["sample_size"] = _int(re.sub(r"[^\d,]", "", value) or None)
        elif key.startswith("전체 응답률") and "response_rate" not in out:
            out["response_rate"] = value
        elif key.startswith("표본오차") and "margin" not in out:
            out["margin"] = value.replace("%P", "%p")
        elif key.startswith("공표·보도 매체명") and "outlet" not in out:
            out["outlet"] = value
        elif key.startswith("최초 공표·보도 지정일시") and "publish_at" not in out:
            out["publish_at"] = value
        elif key.startswith("조사방법 1") and "method" not in out:
            out["method"] = value
    return out


def polls_since(since: str, *, limit: int = 8, pages: int = 2) -> list[Poll]:
    """`since` 이후 등록된 여론조사. 상세는 `limit` 건까지만 연다 (한 건에 한 번 접속)."""
    found: list[Poll] = []
    for page in range(1, pages + 1):
        page_html = _get(NESDC_LIST, {"menuNo": NESDC_MENU, "pageIndex": page}).text
        rows = parse_poll_list(page_html)
        if not rows:
            break
        older = [p for p in rows if p.registered and p.registered < since]
        found += [p for p in rows if not p.registered or p.registered >= since]
        if older:
            break
    for poll in found[:limit]:
        try:
            detail = parse_poll_detail(_get(NESDC_VIEW, {"nttId": poll.ntt_id, "menuNo": NESDC_MENU}).text)
        except Exception as exc:                     # 한 건 못 열어도 목록은 남긴다
            log.debug("여론조사 %s 상세 실패: %s", poll.reg_no, exc)
            continue
        for k, v in detail.items():
            if v not in (None, ""):
                setattr(poll, k, v)
    return found


# ── 한 번에 ──────────────────────────────────────────────────


def collect(cfg: Config, date_str: str) -> dict:
    """그날 글에 넣을 국회·여론조사 집계. 하나라도 받았으면 dict, 아무것도 없으면 {}."""
    settings = cfg.get("civics", {}) or {}
    if not settings.get("enabled", True):
        return {}
    days = int(settings.get("days", 7) or 7)
    today = date.fromisoformat(date_str)
    since = (today - timedelta(days=days)).isoformat()
    key = assembly_key()
    out: dict = {"as_of": date_str, "days": days, "since": since, "sample": not key,
                 "bills": {}, "plenary": {}, "polls": [], "warnings": []}
    try:
        out["bills"] = bills_since(since, key=key)
    except Exception as exc:
        log.warning("발의법률안 집계 실패: %s", exc)
        out["warnings"].append(f"발의법률안을 받지 못했습니다 — {type(exc).__name__}")
    try:
        out["plenary"] = plenary_since(since, key=key)
    except Exception as exc:
        log.warning("본회의 처리안건 집계 실패: %s", exc)
        out["warnings"].append(f"본회의 처리안건을 받지 못했습니다 — {type(exc).__name__}")
    try:
        polls = polls_since(since, limit=int(settings.get("polls_max", 8) or 8))
        out["polls"] = [{**p.__dict__, "summary": p.summary()} for p in polls]
    except Exception as exc:
        log.warning("여론조사 목록 실패: %s", exc)
        out["warnings"].append(f"등록 여론조사 목록을 받지 못했습니다 — {type(exc).__name__}")
    if not (out["bills"] or out["plenary"] or out["polls"]):
        return {}
    return out


def check_source() -> tuple[bool, str, bool, int, str]:
    """doctor 용. (국회 API 응답?, 키 상태, 심의위 목록 응답?, 목록 건수, 오류)."""
    key = assembly_key()
    ok_a, err = False, ""
    try:
        rows, _ = _assembly_rows(BILLS, key=key, page=1, size=5)
        ok_a = bool(rows)
    except Exception as exc:
        err = str(exc)[:120]
    key_state = "인증키 있음" if key else "인증키 없음 → 견본 모드(5건)"
    ok_n, n = False, 0
    try:
        n = len(parse_poll_list(_get(NESDC_LIST, {"menuNo": NESDC_MENU, "pageIndex": 1}).text))
        ok_n = n > 0
    except Exception as exc:
        err = (err + " / " if err else "") + str(exc)[:120]
    return ok_a, key_state, ok_n, n, err
