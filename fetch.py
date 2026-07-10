"""법원경매 상가(근린생활시설) → 이용상태 기반 '스터디카페/학원/병원/의원' 자동 추출
- 리스트검색(searchControllerMain) + 물건상세(감정평가 이용상태) 를 브라우저 세션 안에서 fetch
- 캡처한 실제 검색조건(captured/63)을 그대로 사용 (경기도 / 건물>상업용및업무용>근린생활시설)
"""
import asyncio, json, os, re, sys, html
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# Windows 콘솔(cp949)에서도 한글/특수문자 print 안전
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(BASE, "captured")
PAGE = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml"
LIST_URL = "https://www.courtauction.go.kr/pgj/pgjsearch/searchControllerMain.on"
DETAIL_URL = "https://www.courtauction.go.kr/pgj/pgj15B/selectAuctnCsSrchRslt.on"
DLVR_URL = "https://www.courtauction.go.kr/pgj/pgj15A/selectDlvrOfdocDtsDtl.on"  # 문건/송달내역

# ── 채권자/NPL 감지 ───────────────────────────────
CREDITOR_ON = os.environ.get("CREDITOR", "1") != "0"   # 매칭물건 채권자 분석
NPL_ONLY = os.environ.get("NPL_ONLY", "") == "1"       # 이용상태 무관, 채권승계(NPL) 물건만
NPL_SIG = ["승계인", "채권양도", "채권양수", "채권자변경", "양수인", "압류채권자승계"]
# 채권자 유형 (노출된 법인명 기준)
CRED_TYPE = {
    "유동화": ["유동화전문", "유동화"],
    "대부": ["대부", "에이엠씨대부", "F&I", "에프앤아이"],
    "농협자산관리": ["농협자산관리"],
    "자산관리": ["자산관리회사", "자산관리대부", "자산관리"],
    "캐피탈/저축은행": ["캐피탈", "저축은행"],
}
CRED_CACHE_FILE = os.path.join(CAP, "creditor_cache.json")
try:
    CCACHE = json.load(open(CRED_CACHE_FILE, encoding="utf-8"))
except Exception:
    CCACHE = {}

# 찾을 이용상태 키워드 (KW 환경변수로 콤마구분 재정의 가능)
DEFAULT_KW = ["스터디카페", "스터디 카페", "스터디까페", "스터디룸", "스터디", "독서실", "고시원", "고시텔",
              "학원", "교습소", "어학원", "교육원",
              "병원", "의원", "한의원", "치과", "요양", "약국", "산후조리"]
KEYWORDS = [k.strip() for k in os.environ.get("KW", "").split(",") if k.strip()] or DEFAULT_KW
KW_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS))
# 특정 시군구만 보기 (REGION=구리,남양주 처럼). 비면 전체
REGION_FILTER = [r.strip() for r in os.environ.get("REGION", "").split(",") if r.strip()]
# 순수 토지 물건 제외 (상가 매입 목적 기본값). INCLUDE_LAND=1 이면 포함
INCLUDE_LAND = os.environ.get("INCLUDE_LAND", "") == "1"

# 상세응답 텍스트 캐시 (키워드만 바꿔 재실행시 네트워크 스킵 → 즉시)
CACHE_FILE = os.path.join(CAP, "detail_cache.json")
try:
    DCACHE = json.load(open(CACHE_FILE, encoding="utf-8"))
except Exception:
    DCACHE = {}

# 페이지당 결과 수 (서버 고정: 40)
PAGE_SIZE = 40
MAX_PAGES = int(os.environ.get("MAX_PAGES", "999"))   # 테스트시 제한 가능
DETAIL_CONC = 6

base_payload = json.loads(open(os.path.join(CAP, "63_searchControllerMain.json"), encoding="utf-8").read())
BASE_SRCH = json.loads(base_payload["postData"])["dma_srchGdsDtlSrchInfo"]
detail_tmpl = json.loads(open(os.path.join(CAP, "74_selectAuctnCsSrchRslt.json"), encoding="utf-8").read())["postData"]
DETAIL_BASE = json.loads(detail_tmpl)["dma_srchGdsDtlSrch"]

# ── 옥션원식 검색필터 (환경변수) ───────────────────────────────
# 용도 소분류코드: 21101근린생활 21104판매 21106의료 21107교육 21111업무 21113위락 21112숙박 (빈값=상업용전체)
YONGDO = {  # 별칭 → scl코드
    "근린": "21101", "근린생활": "21101", "판매": "21104", "문화": "21102",
    "의료": "21106", "병원": "21106", "교육": "21107", "업무": "21111",
    "사무실": "21111", "위락": "21113", "숙박": "21112", "운동": "21110",
    "노유자": "21108", "전체상업": "",
}
def envf(k, d=""): return os.environ.get(k, d).strip()
_scl = envf("YONGDO")
SCL = YONGDO.get(_scl, _scl) if _scl else None   # None=캡처값 유지
# 매각기일 기간: 지정 없으면 오늘~오늘+AUCTION_DAYS일 자동 롤링(기본 21일)
AUCTION_DAYS = int(envf("AUCTION_DAYS") or "21")
_bid_from = envf("BID_FROM") or datetime.now().strftime("%Y%m%d")
_bid_to = envf("BID_TO") or (datetime.now() + timedelta(days=AUCTION_DAYS)).strftime("%Y%m%d")
FILT = {
    "sclDspslGdsLstUsgCd": SCL,
    "rprsAdongSdCd": envf("SIDO") or None,        # 시도코드 (41경기 11서울 등)
    "rprsAdongSggCd": envf("SGG") or None,        # 시군구코드
    "cortOfcCd": envf("COURT") or None,           # 법원코드
    "aeeEvlAmtMin": envf("AMT_MIN") or None, "aeeEvlAmtMax": envf("AMT_MAX") or None,     # 감정가(원)
    "rletLwsDspslPrcMin": envf("LOW_MIN") or None, "rletLwsDspslPrcMax": envf("LOW_MAX") or None,  # 최저가(원)
    "flbdNcntMin": envf("FLBD_MIN") or None, "flbdNcntMax": envf("FLBD_MAX") or None,     # 유찰횟수
    "bidBgngYmd": _bid_from, "bidEndYmd": _bid_to,                                        # 매각기일 YYYYMMDD (자동 롤링)
}
def build_search_template():
    tpl = json.loads(base_payload["postData"])
    for k, v in FILT.items():
        if v is not None:
            tpl["dma_srchGdsDtlSrchInfo"][k] = v
    return tpl
SEARCH_TEMPLATE = build_search_template()

FETCH_JS = """async ({url, body}) => {
  const r = await fetch(url, {method:'POST',
    headers:{'Content-Type':'application/json;charset=UTF-8','Accept':'application/json'},
    credentials:'include', body: JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""

def parse_csno(printCsNo):
    m = re.search(r"(\d{4}\s*타경\s*\d+)", (printCsNo or "").replace("<br/>", " "))
    return m.group(1).replace(" ", "") if m else None

async def evaluate_json(page, url, body):
    res = await page.evaluate(FETCH_JS, {"url": url, "body": body})
    if res["status"] != 200:
        return None
    try:
        return json.loads(res["text"])
    except Exception:
        return None

async def get_all_list(page):
    rows = []
    p1 = json.loads(json.dumps(SEARCH_TEMPLATE))
    p1["dma_pageInfo"].update({"pageNo": 1, "pageSize": str(PAGE_SIZE), "bfPageNo": 0,
                               "startRowNo": 1, "totalYn": "Y"})
    d = await evaluate_json(page, LIST_URL, p1)
    total = int(d["data"]["dma_pageInfo"].get("totalCnt") or 0)
    rows += d["data"]["dlt_srchResult"]
    pages = min((total + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES)
    print(f"총 {total}건 / {pages}페이지 수집중...", flush=True)
    for pno in range(2, pages + 1):
        pn = json.loads(json.dumps(p1))
        pn["dma_pageInfo"].update({"pageNo": pno, "bfPageNo": pno - 1,
                                   "startRowNo": (pno - 1) * PAGE_SIZE + 1, "totalYn": "N",
                                   "totalCnt": str(total)})
        d = await evaluate_json(page, LIST_URL, pn)
        if d and d.get("data", {}).get("dlt_srchResult"):
            rows += d["data"]["dlt_srchResult"]
        if pno % 5 == 0:
            print(f"  ...{pno}/{pages}p ({len(rows)}건)", flush=True)
    return rows, total

async def get_detail(page, cortOfcCd, csNo, seq):
    ckey = f"{cortOfcCd}|{csNo}"
    if ckey in DCACHE:            # 캐시 히트 → 네트워크 스킵
        return DCACHE[ckey]
    body = {"dma_srchGdsDtlSrch": json.loads(json.dumps(DETAIL_BASE))}
    body["dma_srchGdsDtlSrch"].update({"csNo": csNo, "cortOfcCd": cortOfcCd, "dspslGdsSeq": str(seq)})
    d = await evaluate_json(page, DETAIL_URL, body)
    if not d:
        return ""
    r = d.get("data", {}).get("dma_result", {})
    # 이용상태 관련 항목만: 이용상태(006)/건물개황·용도(015)/토지이용(009)/임대관계(026)
    # 제외: 위치·주위환경(001)/교통(003)/도로(005)/공법제한(011) → 주변시설 오탐 방지
    USE_ITEMS = {"00083006", "00083015", "00083009", "00083026"}
    parts = [r.get("dspslGdsDxdyInfo", {}).get("gdsSpcfcRmk", "") or ""]
    for a in r.get("aeeWevlMnpntLst", []) or []:
        if a.get("aeeWevlMnpntItmCd") in USE_ITEMS:
            parts.append(a.get("aeeWevlMnpntCtt", "") or "")
    text = "\n".join(parts)
    DCACHE[ckey] = text          # 캐시에 저장
    return text

CRED_NAME_RE = re.compile(r"(주식회사\s*[가-힣A-Za-z0-9]+은행|[가-힣]{2,}유동화전문[가-힣]*|[가-힣]{2,}자산관리[가-힣]*|[가-힣]{2,}대부(?:금융)?|[가-힣]{2,}캐피탈|[가-힣]{2,}저축은행|[가-힣]{2,}은행)")

async def get_creditor(page, cortOfcCd, saNo):
    """문건/송달내역 → NPL(채권승계) 여부 + 노출된 채권자 법인명/유형"""
    ck = f"{cortOfcCd}|{saNo}"
    if ck in CCACHE:
        return CCACHE[ck]
    body = {"dma_srchDlvrOfdocDts": {"cortOfcCd": cortOfcCd, "csNo": saNo, "srchFlag": "F"}}
    d = await evaluate_json(page, DLVR_URL, body)
    lines = []
    if d:
        data = d.get("data", {})
        for k in ("dlt_dlvrDtsLst", "dlt_ofdocDtsLst"):
            for row in data.get(k, []) or []:
                lines.append(row.get("dlvrDts") or row.get("rcptDts") or "")
    text = "\n".join(lines)
    npl = any(s in text for s in NPL_SIG)
    types = [t for t, pats in CRED_TYPE.items() if any(p in text for p in pats)]
    names = sorted(set(CRED_NAME_RE.findall(text)))
    npl_lines = [t for t in lines if any(s in t for s in NPL_SIG)]
    res = {"npl": npl, "types": types, "names": names[:6], "npl_lines": npl_lines[:3]}
    CCACHE[ck] = res
    return res

def write_report(out, recs, npl_cnt, type_cnt, kwc, sgg):
    """프리미엄 반응형 분석 대시보드 (카드 ↔ 콤팩트 표 · 라이트/다크 · 차트)"""
    import math, re as _re
    from datetime import datetime as _dt, date as _date

    PY = 3.3058  # 1평 = 3.3058㎡
    def i(v):
        try: return int(float(v))
        except Exception: return 0
    def won(v):
        try: return f"{int(v):,}"
        except Exception: return v or ""
    def eok(v):
        v = i(v)
        if v >= 10**8:
            s = f"{v/10**8:.1f}".rstrip("0").rstrip(".")
            return f"{s}억"
        if v >= 10**4:
            return f"{round(v/10**4):,}만"
        return f"{v:,}"
    def pyeong(sqm): return (sqm / PY) if sqm else 0
    def ppp_man(price, sqm):          # 평단가(만원) — 올림
        py = pyeong(sqm)
        if not py or not price: return 0
        return math.ceil((price / py) / 10**4)
    def floor_of(pjb):
        if not pjb: return ""
        t = pjb.replace("\r", " ").replace("\n", " ")
        m = _re.search(r"(지하\s*\d+층|지하층|반지하|옥탑\d*|\d+층건?내?\s*\d+층|\d+층|단층)", t)
        return m.group(0).replace(" ", "") if m else ""
    def region_of(addr):
        m = _re.search(r"경기도\s*([가-힣]+시)", addr or "")
        return m.group(1) if m else "기타"

    today = _dt.now().date()
    def dday(giil):
        if not giil or len(str(giil)) != 8: return None
        try: g = _date(int(giil[:4]), int(giil[4:6]), int(giil[6:]))
        except Exception: return None
        return (g - today).days

    title = "부실채권(NPL) 상가" if NPL_ONLY else "근린상가·근린시설 경매 분석"
    subtitle = "채권 승계(유동화·대부·자산관리) 물건" if NPL_ONLY else "이용상태 매칭 · 투자분석 대시보드"
    period = ""
    try:
        _si = SEARCH_TEMPLATE.get("dma_srchGdsDtlSrchInfo", {})
        bf = _si.get("bidBgngYmd") or ""; bt = _si.get("bidEndYmd") or ""
        if len(bf) == 8 and len(bt) == 8:
            period = f"{bf[:4]}.{bf[4:6]}.{bf[6:]}~{bt[4:6]}.{bt[6:]}"
    except Exception: pass
    cond = "경기도 · 근린상가·근린시설" + (" · " + ",".join(REGION_FILTER) if REGION_FILTER else "") + (" · " + period if period else "")

    USE_CAT = [(["의원", "병원", "한의원", "치과", "약국", "요양", "산후조리"], "med"),
               (["학원", "교습소", "독서실", "어학원", "교육원"], "edu"),
               (["스터디", "카페", "고시원", "고시텔"], "study")]
    def kw_cat(kw):
        for words, cls in USE_CAT:
            if any(w in kw for w in words): return cls
        return "npl"

    # ── 레코드 → 표시 데이터 ──
    data = []
    for h in recs:
        r = h["row"]
        addr = (r.get("printSt") or r.get("convAddr") or "").replace("\n", " ").strip()
        csno = parse_csno(r.get("printCsNo")) or ""
        court = r.get("jiwonNm") or ""
        dept = r.get("jpDeptNm") or ""
        gv = i(r.get("gamevalAmt")); mv = i(r.get("minmaePrice"))
        dr = round((1 - mv / gv) * 100) if gv else 0
        av = [x for x in (i(r.get("maxArea")), i(r.get("minArea"))) if x]
        if av:
            lo, hi = min(av), max(av)
            area_txt = (f"{lo}~{hi}㎡·{pyeong(lo):.0f}~{pyeong(hi):.0f}평" if lo != hi
                        else f"{hi}㎡·{pyeong(hi):.1f}평")
            sqm = hi
        else:
            area_txt, sqm = "", 0
        ppp = ppp_man(mv, sqm)
        floor = floor_of(r.get("pjbBuldList") or r.get("areaList") or "")
        rel = i(r.get("gwansMulRegCnt"))
        nxt = i(r.get("notifyMinmaePrice1")); nxtr = i(r.get("notifyMinmaePriceRate1"))
        yc = i(r.get("yuchalCnt"))
        giil = str(r.get("maeGiil") or "")
        giil_fmt = f"{giil[:4]}.{giil[4:6]}.{giil[6:]}" if len(giil) == 8 else giil
        hh1 = str(r.get("maeHh1") or "").strip()
        place = (r.get("maePlace") or "").strip()
        tel = (r.get("tel") or "").strip()
        c = h.get("cred") or {}
        kw = h["kw"]
        data.append(dict(addr=addr, csno=csno, court=court, dept=dept, gv=gv, mv=mv, dr=dr,
                         area_txt=area_txt, sqm=sqm, ppp=ppp, floor=floor, rel=rel,
                         nxt=nxt, nxtr=nxtr, yc=yc, giil=giil, giil_fmt=giil_fmt,
                         hh1=hh1, place=place, tel=tel,
                         dd=dday(giil), cred=c, kw=kw, cat=kw_cat(kw),
                         evi=(h.get("evi") or "").replace("\n", " ").strip(),
                         region=region_of(addr)))

    n = len(data)
    avg_dr = round(sum(d["dr"] for d in data) / n) if n else 0
    ppps = [d["ppp"] for d in data if d["ppp"]]
    avg_ppp = round(sum(ppps) / len(ppps)) if ppps else 0
    nreg = len({d["region"] for d in data})

    kpis = [("매칭 물건", f"{n}", "건", "k-main"),
            ("NPL 채권승계", f"{npl_cnt}", "건", "k-npl"),
            ("평균 할인율", f"{avg_dr}", "%", "k-dr"),
            ("평균 평단가", f"{avg_ppp:,}", "만/평", "k-ppp"),
            ("대상 지역", f"{nreg}", "곳", "k-reg")]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><div class="kv">{v}<small>{u}</small></div>'
        f'<div class="kl">{html.escape(lbl)}</div></div>'
        for lbl, v, u, cls in kpis)

    # ── 차트 ──
    # 1) 할인율 분포
    bins = [("~30%", 0, 30), ("30–40", 30, 40), ("40–50", 40, 50), ("50%+", 50, 10**4)]
    bcounts = [(lbl, sum(1 for d in data if lo <= d["dr"] < hi)) for lbl, lo, hi in bins]
    bmax = max([c for _, c in bcounts] + [1])
    seq = ["var(--seq1)", "var(--seq2)", "var(--seq3)", "var(--seq4)"]
    dbars = "".join(
        f'<div class="vbar" title="할인율 {lbl}: {c}건"><div class="vb-val">{c}</div>'
        f'<div class="vb-track"><div class="vb-fill" style="height:{round(c/bmax*100) if c else 0}%;background:{seq[idx]}"></div></div>'
        f'<div class="vb-lbl">{lbl}</div></div>'
        for idx, (lbl, c) in enumerate(bcounts))

    # 2) 지역별 매물 수
    reg_items = sgg.most_common(8)
    rmax = max([c for _, c in reg_items] + [1])
    rbars = "".join(
        f'<div class="hbar" title="{html.escape(name)}: {c}건"><div class="hb-name">{html.escape(name)}</div>'
        f'<div class="hb-track"><div class="hb-fill" style="width:{max(round(c/rmax*100),5)}%"></div></div>'
        f'<div class="hb-val">{c}</div></div>'
        for name, c in reg_items) or '<div class="empty">데이터 없음</div>'

    # 3) 이용유형 구성 (세그먼트)
    catsum = {}
    for d in data: catsum[d["cat"]] = catsum.get(d["cat"], 0) + 1
    CAT_META = {"med": ("의료", "var(--c-med)"), "edu": ("교육", "var(--c-edu)"),
                "study": ("생활", "var(--c-study)"), "npl": ("기타", "var(--mut)")}
    tot = sum(catsum.values()) or 1
    comp = "".join(
        f'<div class="seg" style="width:{catsum[k]/tot*100:.1f}%;background:{CAT_META[k][1]}" title="{CAT_META[k][0]}: {catsum[k]}건"></div>'
        for k in sorted(catsum, key=lambda x: -catsum[x]))
    legend = "".join(
        f'<span class="lg"><i style="background:{CAT_META[k][1]}"></i>{CAT_META[k][0]} <b>{catsum[k]}</b></span>'
        for k in sorted(catsum, key=lambda x: -catsum[x]))

    # ── 카드 + 표 ──
    cards, rows_tbl = "", ""
    for idx, d in enumerate(data):
        cat = d["cat"]; c = d["cred"]
        badges = f'<span class="pill p-{cat}">{html.escape(d["kw"])}</span>'
        if c.get("npl"): badges += '<span class="bdg b-npl">NPL</span>'
        for t in (c.get("types") or [])[:2]: badges += f'<span class="bdg b-type">{html.escape(t)}</span>'
        if d["rel"] > 1: badges += f'<span class="bdg b-rel">관련 {d["rel"]}</span>'
        ddtxt, ddcls = "", "dd-far"
        if d["dd"] is not None:
            if d["dd"] < 0: ddtxt, ddcls = "종료", "dd-over"
            elif d["dd"] == 0: ddtxt, ddcls = "D-DAY", "dd-now"
            else:
                ddtxt = f"D-{d['dd']}"; ddcls = "dd-now" if d["dd"] <= 7 else "dd-far"
        drcls = "d-hi" if d["dr"] >= 50 else ("d-mid" if d["dr"] >= 30 else "d-lo")
        ppp_txt = f'{d["ppp"]:,}만/평' if d["ppp"] else "—"
        nxt_txt = f'다음 예상최저 <b>{eok(d["nxt"])}</b> · 감정 {d["nxtr"]}%' if d["nxt"] else ""
        area_line = " · ".join(x for x in [d["area_txt"], d["floor"]] if x) or "면적 정보 없음"
        evi = html.escape(d["evi"][:70])
        stext = html.escape((d["addr"] + " " + d["csno"] + " " + d["court"] + " " + d["kw"] + " " +
                             " ".join(c.get("names", []))).lower())
        da = (f'data-idx="{idx}" data-text="{stext}" data-dr="{d["dr"]}" data-low="{d["mv"]}" '
              f'data-ppp="{d["ppp"] or 0}" data-giil="{d["giil"] or "0"}"')
        cards += (
            f'<article class="card" {da}>'
            f'<div class="c-top">{badges}<span class="dd {ddcls}">{ddtxt}</span></div>'
            f'<h3 class="c-case">{html.escape(d["csno"] or "사건번호 미상")}</h3>'
            f'<div class="c-court">{html.escape(d["court"])}'
            + ((" · " + html.escape(d["dept"])) if d["dept"] else "") + '</div>'
            f'<div class="c-addr">{html.escape(d["addr"])}</div>'
            f'<div class="c-area">{html.escape(area_line)}</div>'
            f'<div class="c-price">'
            f'<div class="pc"><span class="pl">감정가</span><span class="pv">{eok(d["gv"])}</span></div>'
            f'<div class="pc"><span class="pl">최저가</span><span class="pv hl">{eok(d["mv"])}</span></div>'
            f'<div class="pc"><span class="drate {drcls}">▼{d["dr"]}%</span></div>'
            f'</div>'
            f'<div class="c-ppp"><span class="pl">평단가</span><b>{ppp_txt}</b>'
            f'<span class="yc">유찰 {d["yc"]}회</span></div>'
            + (f'<div class="c-next">{nxt_txt}</div>' if nxt_txt else "")
            + (f'<div class="c-evi">{evi}</div>' if evi else "")
            + '</article>')
        rows_tbl += (
            f'<tr {da}>'
            f'<td data-label="이용"><span class="pill p-{cat}">{html.escape(d["kw"])}</span></td>'
            f'<td data-label="사건·소재지" class="t-addr"><b class="t-case">{html.escape(d["csno"] or "—")}</b>'
            f'<span class="t-sub">{html.escape(d["court"])} · {html.escape(d["addr"])}</span></td>'
            f'<td data-label="면적" class="num">{html.escape(d["area_txt"] or "—")}</td>'
            f'<td data-label="감정가" class="num">{won(d["gv"])}</td>'
            f'<td data-label="최저가" class="num hl">{won(d["mv"])}</td>'
            f'<td data-label="평단가" class="num">{(str(format(d["ppp"], ",")) + "만") if d["ppp"] else "—"}</td>'
            f'<td data-label="할인율" class="ctr"><span class="drate {drcls}">▼{d["dr"]}%</span></td>'
            f'<td data-label="유찰" class="ctr">{d["yc"]}회</td>'
            f'<td data-label="매각기일" class="ctr">{d["giil_fmt"]}'
            + (f' <span class="dd {ddcls}">{ddtxt}</span>' if ddtxt else "") + '</td>'
            f'<td data-label="NPL" class="ctr">'
            + ('<span class="bdg b-npl">NPL</span>' if c.get("npl") else "") + '</td>'
            f'</tr>')

    chips_kw = "".join(f'<button class="chip" data-q="{html.escape(k)}">{html.escape(k)} <b>{v}</b></button>'
                       for k, v in kwc.most_common())
    chips_rg = "".join(f'<button class="chip" data-q="{html.escape(k)}">{html.escape(k)} <b>{v}</b></button>'
                       for k, v in sgg.most_common())

    # ── 캘린더 뷰 (매각기일 월 달력 + 모바일 아젠다) ──
    from datetime import date as _cdate
    def _hhmm(hh):
        hh = str(hh or "").strip()
        return f"{hh[:2]}:{hh[2:]}" if len(hh) == 4 else ""
    def _drcls(dr): return "d-hi" if dr >= 50 else ("d-mid" if dr >= 30 else "d-lo")
    WD = ["일", "월", "화", "수", "목", "금", "토"]
    by_day = {}
    for idx, d in enumerate(data):
        if len(d["giil"]) == 8:
            by_day.setdefault(d["giil"], []).append(idx)
    today_k = today.strftime("%Y%m%d")

    def cal_entry(idx):
        d = data[idx]
        npl = " e-npl" if d["cred"].get("npl") else ""
        hh = _hhmm(d["hh1"])
        return (f'<button class="cal-entry ce-{d["cat"]}{npl}" data-idx="{idx}" '
                f'title="{html.escape((hh + " " + d["csno"] + " " + d["addr"]).strip())}">'
                f'<span class="ce-case">{html.escape(d["csno"] or "—")}</span>'
                f'<span class="ce-meta"><b>{eok(d["mv"])}</b>'
                f'<span class="ce-dr {_drcls(d["dr"])}">▼{d["dr"]}%</span></span></button>')

    cal_html = '<div class="empty">매각기일 정보가 없습니다.</div>'
    if by_day:
        gdays = sorted(by_day)
        y0, m0 = int(gdays[0][:4]), int(gdays[0][4:6])
        y1, m1 = int(gdays[-1][:4]), int(gdays[-1][4:6])
        months, (yy, mm) = [], (y0, m0)
        while (yy, mm) <= (y1, m1):
            months.append((yy, mm)); mm = mm % 12 + 1; yy = yy + (1 if mm == 1 else 0)
        grid = ""
        for (yy, mm) in months:
            first = _cdate(yy, mm, 1)
            start_wd = (first.weekday() + 1) % 7           # 그 달 1일의 요일열(일=0)
            nxt = _cdate(yy + 1, 1, 1) if mm == 12 else _cdate(yy, mm + 1, 1)
            ndays = (nxt - first).days
            cells = '<div class="cal-cell blank"></div>' * start_wd
            for day in range(1, ndays + 1):
                gk = f"{yy:04d}{mm:02d}{day:02d}"
                idxs = by_day.get(gk, [])
                col = (start_wd + day - 1) % 7
                cls = "cal-cell" + (" has" if idxs else "") + (" today" if gk == today_k else "") + (" we" if col in (0, 6) else "")
                ents = "".join(cal_entry(x) for x in idxs)
                cnt = f'<span class="cal-cnt">{len(idxs)}</span>' if idxs else ""
                cells += (f'<div class="{cls}"><div class="cal-day">{day}{cnt}</div>'
                          f'<div class="cal-ents">{ents}</div></div>')
            head = "".join(f'<div class="cal-wd{" we" if w in (0, 6) else ""}">{WD[w]}</div>' for w in range(7))
            grid += (f'<div class="cal-month"><div class="cal-mtitle">{yy}년 {mm}월</div>'
                     f'<div class="cal-grid"><div class="cal-wdrow">{head}</div>'
                     f'<div class="cal-cells">{cells}</div></div></div>')
        agenda = ""
        for gk in gdays:
            dt = _cdate(int(gk[:4]), int(gk[4:6]), int(gk[6:]))
            wd = WD[(dt.weekday() + 1) % 7]
            istoday = " today" if gk == today_k else ""
            items_h = "".join(
                f'<button class="cal-ag-item" data-idx="{x}"><span class="p-dot dot-{data[x]["cat"]}"></span>'
                f'<span class="ai-main"><b>{html.escape(data[x]["csno"] or "—")}</b>'
                f'<span class="ai-sub">{html.escape(data[x]["kw"])} · {html.escape(data[x]["addr"])}</span></span>'
                f'<span class="ai-right">{eok(data[x]["mv"])}'
                f'<span class="ai-dr {_drcls(data[x]["dr"])}">▼{data[x]["dr"]}%</span></span></button>'
                for x in by_day[gk])
            agenda += (f'<div class="cal-ag-day{istoday}"><div class="cal-ag-date">'
                       f'{int(gk[4:6])}월 {int(gk[6:])}일 <span class="agwd">({wd})</span> <b>{len(by_day[gk])}건</b></div>'
                       f'{items_h}</div>')
        cal_html = f'<div class="cal-grid-wrap">{grid}</div><div class="cal-agenda">{agenda}</div>'

    import json as _json
    cal_json = _json.dumps([
        {"i": idx, "g": d["giil"], "hh": _hhmm(d["hh1"]), "cs": d["csno"], "kw": d["kw"],
         "low": eok(d["mv"]), "dr": d["dr"], "ct": d["court"], "pl": d["place"],
         "ad": d["addr"], "tel": d["tel"]}
        for idx, d in enumerate(data) if len(d["giil"]) == 8], ensure_ascii=False)

    heads = [("이용", ""), ("사건·소재지", ""), ("면적", "num"), ("감정가", "num"), ("최저가", "num"),
             ("평단가", "num"), ("할인율", "ctr"), ("유찰", "ctr"), ("매각기일", "ctr"), ("NPL", "ctr")]
    sortkey = {2: "low", 5: "ppp", 6: "dr", 8: "giil"}  # 표 헤더 인덱스→정렬키
    thhtml = "".join(
        f'<th class="{cl}"'
        + (f' data-sk="{sortkey[idx]}"' if idx in sortkey else "")
        + f'>{h}<span class="ar"></span></th>'
        for idx, (h, cl) in enumerate(heads))

    STYLE = r"""
:root{
 --bg:#eef1f7;--surface:#fff;--surface2:#f7f9fd;--ink:#0d1526;
 --ink2:#39445a;--mut:#77839a;--line:#e5eaf2;--navy:#0f1e3d;--navy2:#22468a;--blue:#2a78d6;
 --sky:#eef4ff;--shadow:0 3px 16px rgba(20,40,80,.07);--shadow2:0 14px 38px rgba(15,30,61,.28);
 --seq1:#86b6ef;--seq2:#5598e7;--seq3:#2a78d6;--seq4:#184f95;
 --c-med:#1baf7a;--c-edu:#2a78d6;--c-study:#eb6834;
 --good:#0ca30c;--goodbg:#e5f6e6;--warn:#c98500;--warnbg:#fdf2dd;--crit:#d03b3b;--critbg:#fbe6e6;
 --npl:#d03b3b;
}
:root[data-theme=dark]{
 --bg:#0b0f16;--surface:#151b26;--surface2:#1b2431;--ink:#f2f5fb;--ink2:#c6cfdd;--mut:#8a97ac;
 --line:#26303f;--navy:#0b1730;--navy2:#1c3766;--blue:#3987e5;--sky:#17233a;
 --shadow:0 3px 16px rgba(0,0,0,.45);--shadow2:0 14px 38px rgba(0,0,0,.55);
 --seq1:#b7d3f6;--seq2:#6da7ec;--seq3:#3987e5;--seq4:#256abf;
 --c-med:#199e70;--c-edu:#3987e5;--c-study:#d95926;
 --good:#0ca30c;--goodbg:#123b1d;--warn:#e0a52a;--warnbg:#3a2f12;--crit:#e66767;--critbg:#3a1c1c;
 --npl:#e05252;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth}
body{margin:0;font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',system-ui,sans-serif;
 background:var(--bg);color:var(--ink);line-height:1.5;font-feature-settings:'tnum';
 transition:background .25s,color .25s}
.wrap{max-width:1240px;margin:0 auto;padding:18px}
/* Hero */
.hero{background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;border-radius:22px;
 padding:26px 28px;box-shadow:var(--shadow2);position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;right:-70px;top:-70px;width:260px;height:260px;
 background:radial-gradient(circle,rgba(57,135,229,.5),transparent 70%)}
.h-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;position:relative;z-index:1}
.brand{font-size:11.5px;letter-spacing:3px;color:#9fb6e8;font-weight:800;text-transform:uppercase}
.hero h1{margin:7px 0 4px;font-size:27px;font-weight:800;letter-spacing:-.6px}
.hero .sub{color:#c8d6f2;font-size:14px}
.hero .cond{margin-top:12px;font-size:12.5px;color:#bccbec;background:rgba(255,255,255,.1);
 display:inline-block;padding:6px 13px;border-radius:999px}
.theme-btn{flex-shrink:0;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);
 color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;transition:.15s}
.theme-btn:hover{background:rgba(255,255,255,.26)}
/* KPI */
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:13px;margin:16px 0}
.kpi{background:var(--surface);border-radius:16px;padding:17px 17px 15px;box-shadow:var(--shadow);
 border:1px solid var(--line);position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--blue)}
.k-npl::before{background:var(--crit)}.k-dr::before{background:var(--good)}
.k-ppp::before{background:#7c5cff}.k-reg::before{background:#e08a1e}
.kpi .kv{font-size:27px;font-weight:800;color:var(--ink);letter-spacing:-1px}
.kpi .kv small{font-size:12px;font-weight:600;color:var(--mut);margin-left:3px;letter-spacing:0}
.kpi .kl{font-size:12.5px;color:var(--mut);margin-top:3px;font-weight:600}
/* Charts */
.charts{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:13px;margin-bottom:16px}
.chart{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px 18px;
 box-shadow:var(--shadow)}
.chart h4{margin:0 0 14px;font-size:13.5px;font-weight:700;color:var(--ink2)}
.vbars{display:flex;align-items:flex-end;gap:12px;height:132px}
.vbar{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%}
.vb-val{font-size:13px;font-weight:800;color:var(--ink)}
.vb-track{flex:1;width:100%;display:flex;align-items:flex-end;background:var(--surface2);
 border-radius:7px;overflow:hidden}
.vb-fill{width:100%;border-radius:6px 6px 0 0;min-height:3px;transition:height .5s cubic-bezier(.2,.7,.2,1)}
.vb-lbl{font-size:11px;color:var(--mut);font-weight:600}
.hbars{display:flex;flex-direction:column;gap:9px}
.hbar{display:grid;grid-template-columns:52px 1fr 26px;align-items:center;gap:9px}
.hb-name{font-size:12px;color:var(--ink2);font-weight:600;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hb-track{background:var(--surface2);border-radius:6px;height:16px;overflow:hidden}
.hb-fill{height:100%;background:var(--blue);border-radius:6px;transition:width .5s cubic-bezier(.2,.7,.2,1)}
.hb-val{font-size:12px;font-weight:700;color:var(--ink);text-align:right}
.compbar{display:flex;height:22px;border-radius:7px;overflow:hidden;gap:2px;margin-bottom:12px}
.seg{min-width:3px;transition:.3s}
.legend{display:flex;flex-wrap:wrap;gap:10px 16px}
.lg{font-size:12px;color:var(--ink2);font-weight:600;display:inline-flex;align-items:center;gap:6px}
.lg i{width:11px;height:11px;border-radius:3px;display:inline-block}
.lg b{color:var(--ink)}
.empty{color:var(--mut);font-size:12.5px}
/* Toolbar */
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 12px}
.search{flex:1;min-width:200px;position:relative}
.search input{width:100%;padding:12px 14px 12px 40px;border:1px solid var(--line);border-radius:12px;
 font-size:14px;background:var(--surface);color:var(--ink);outline:none;transition:.15s}
.search::before{content:"";position:absolute;left:14px;top:50%;transform:translateY(-50%);width:16px;height:16px;
 background:no-repeat center/contain url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' stroke='%2377839a' stroke-width='2'%3E%3Ccircle cx='7' cy='7' r='5'/%3E%3Cpath d='M11 11l4 4'/%3E%3C/svg%3E")}
.search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(42,120,214,.15)}
select.sort{padding:11px 12px;border:1px solid var(--line);border-radius:12px;background:var(--surface);
 color:var(--ink);font-size:13px;font-weight:600;cursor:pointer;outline:none}
.vtoggle{display:flex;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:3px;gap:2px}
.vtoggle button{border:none;background:transparent;color:var(--mut);font-weight:700;font-size:13px;
 padding:8px 14px;border-radius:9px;cursor:pointer;transition:.15s}
.vtoggle button.on{background:var(--blue);color:#fff}
.count{font-size:13px;color:var(--mut);font-weight:600;white-space:nowrap;margin-left:auto}
.count b{color:var(--ink)}
/* Chips */
.chipwrap{margin:0 0 12px}
.chipwrap .lbl{font-size:11px;color:var(--mut);font-weight:700;margin:0 8px 7px 0;text-transform:uppercase;letter-spacing:.5px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--ink2);border-radius:999px;
 padding:6px 12px;font-size:12.5px;font-weight:600;cursor:pointer;margin:0 6px 7px 0;transition:.12s}
.chip:hover{border-color:var(--blue);color:var(--blue)}
.chip.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.chip b{margin-left:2px;opacity:.75}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px 17px;
 box-shadow:var(--shadow);transition:transform .15s,box-shadow .15s,border-color .15s;display:flex;flex-direction:column;gap:9px}
.card:hover{transform:translateY(-3px);box-shadow:var(--shadow2);border-color:rgba(42,120,214,.4)}
.c-top{display:flex;align-items:center;flex-wrap:wrap;gap:5px}
.pill{display:inline-block;padding:4px 10px;border-radius:8px;font-size:12px;font-weight:800;white-space:nowrap}
.p-med{background:var(--goodbg);color:var(--c-med)}.p-edu{background:var(--sky);color:var(--c-edu)}
.p-study{background:#fdeee6;color:var(--c-study)}.p-npl{background:var(--critbg);color:var(--crit)}
:root[data-theme=dark] .p-study{background:#33210f}
.bdg{display:inline-block;padding:3px 8px;border-radius:7px;font-size:11px;font-weight:800}
.b-npl{background:var(--npl);color:#fff}
.b-type{background:var(--warnbg);color:var(--warn);border:1px solid rgba(201,133,0,.3)}
.b-rel{background:var(--surface2);color:var(--mut);border:1px solid var(--line)}
.dd{margin-left:auto;font-size:11px;font-weight:800;padding:3px 8px;border-radius:7px}
.dd-now{background:var(--critbg);color:var(--crit)}.dd-far{background:var(--sky);color:var(--blue)}
.dd-over{background:var(--surface2);color:var(--mut)}
.c-case{margin:2px 0 0;font-size:18px;font-weight:800;line-height:1.25;color:var(--navy);letter-spacing:-.3px;font-variant-numeric:tabular-nums}
:root[data-theme=dark] .c-case{color:#8fbcf0}
.c-court{font-size:12px;color:var(--mut);font-weight:600}
.c-addr{font-size:13.5px;font-weight:600;line-height:1.4;color:var(--ink2)}
.c-area{font-size:12.5px;color:var(--ink2);font-weight:600;background:var(--surface2);
 padding:6px 10px;border-radius:9px;display:inline-block;width:fit-content}
.c-price{display:flex;align-items:center;gap:14px;padding:9px 0 3px;border-top:1px solid var(--line);margin-top:2px}
.pc{display:flex;flex-direction:column;gap:1px}
.pc .pl{font-size:10.5px;color:var(--mut);font-weight:600}
.pc .pv{font-size:16px;font-weight:800;color:var(--ink2)}
.pc .pv.hl{color:var(--navy);font-size:18px}
:root[data-theme=dark] .pc .pv.hl{color:var(--blue)}
.pc:last-child{margin-left:auto}
.drate{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:800;font-size:13px}
.d-hi{background:var(--goodbg);color:var(--good)}.d-mid{background:var(--warnbg);color:var(--warn)}
.d-lo{background:var(--surface2);color:var(--mut)}
.c-ppp{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--mut);font-weight:600}
.c-ppp b{color:var(--blue);font-size:14px;font-weight:800}
.c-ppp .yc{margin-left:auto}
.c-next{font-size:12px;color:var(--ink2);background:var(--surface2);padding:6px 10px;border-radius:9px}
.c-next b{color:var(--ink)}
.c-evi{font-size:11.5px;color:var(--mut);line-height:1.45;border-left:2px solid var(--line);padding-left:9px}
/* Table */
.tablewrap{display:none;background:var(--surface);border:1px solid var(--line);border-radius:16px;
 overflow:hidden;box-shadow:var(--shadow)}
body.view-table .cards{display:none}
body.view-table .tablewrap{display:block;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{position:sticky;top:0;background:var(--navy);color:#fff;font-weight:700;text-align:left;
 padding:12px;white-space:nowrap;font-size:12px;z-index:2}
thead th.num,thead th.ctr{text-align:right}
thead th[data-sk]{cursor:pointer;user-select:none}
thead th[data-sk]:hover{background:var(--navy2)}
thead th .ar{opacity:.5;font-size:9px;margin-left:3px}
tbody td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle;color:var(--ink2)}
tbody tr:hover{background:var(--sky)}
tbody tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ctr{text-align:center}
td.hl{font-weight:800;color:var(--ink)}
.t-addr{min-width:190px;color:var(--ink)}
.t-case{font-size:13.5px;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums}
.t-sub{display:block;font-size:11.5px;color:var(--mut);font-weight:600;margin-top:2px}
/* Footer */
.foot{text-align:center;color:var(--mut);font-size:12px;padding:24px 0 34px;line-height:1.7}
.foot b{color:var(--ink2)}
/* Calendar */
.calwrap{display:none}
body.view-cal .cards,body.view-cal .tablewrap{display:none}
body.view-cal .calwrap{display:block}
body.view-cal .sort{display:none}
.cal-toolbar{display:flex;justify-content:flex-end;margin-bottom:10px}
.ics-btn{background:var(--surface);border:1px solid var(--line);color:var(--ink2);font-weight:700;font-size:13px;padding:9px 14px;border-radius:11px;cursor:pointer;transition:.15s}
.ics-btn:hover{border-color:var(--blue);color:var(--blue)}
.cal-month{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:16px 18px;margin-bottom:14px}
.cal-mtitle{font-size:16px;font-weight:800;color:var(--ink);margin-bottom:12px;letter-spacing:-.3px}
.cal-wdrow{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-bottom:6px}
.cal-wd{text-align:center;font-size:12px;font-weight:700;color:var(--mut);padding:4px 0}
.cal-wd.we{color:var(--crit)}
.cal-cells{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
.cal-cell{min-height:96px;border:1px solid var(--line);border-radius:10px;padding:5px;background:var(--surface2);display:flex;flex-direction:column;gap:4px}
.cal-cell.blank{background:transparent;border:none}
.cal-cell.has{background:var(--surface)}
:root[data-theme=dark] .cal-cell.has{background:#1b2431}
.cal-cell.today{border-color:var(--blue);box-shadow:0 0 0 2px rgba(42,120,214,.25)}
.cal-day{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700;color:var(--ink2)}
.cal-cell.we .cal-day{color:var(--crit)}
.cal-cnt{background:var(--blue);color:#fff;font-size:10px;font-weight:800;border-radius:999px;padding:1px 6px}
.cal-ents{display:flex;flex-direction:column;gap:3px;overflow:hidden}
.cal-entry{text-align:left;border:none;background:var(--surface2);border-left:3px solid var(--mut);border-radius:6px;padding:4px 6px;cursor:pointer;transition:.12s;width:100%}
.cal-entry:hover{filter:brightness(.97);transform:translateX(1px)}
.cal-entry.ce-med{border-left-color:var(--c-med)}.cal-entry.ce-edu{border-left-color:var(--c-edu)}
.cal-entry.ce-study{border-left-color:var(--c-study)}.cal-entry.ce-npl{border-left-color:var(--crit)}
.cal-entry.e-npl{box-shadow:inset 0 0 0 1px rgba(208,59,59,.45)}
.ce-case{display:block;font-size:11px;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ce-meta{display:flex;justify-content:space-between;align-items:center;margin-top:1px}
.ce-meta b{font-size:11px;color:var(--ink2)}
.ce-dr{font-size:9.5px;font-weight:800;padding:0 4px;border-radius:5px}
.cal-agenda{display:none}
.cal-ag-day{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:12px 14px;margin-bottom:11px}
.cal-ag-day.today{border-color:var(--blue)}
.cal-ag-date{font-size:14px;font-weight:800;color:var(--ink);margin-bottom:9px}
.cal-ag-date .agwd{color:var(--mut);font-weight:600}.cal-ag-date b{color:var(--blue);margin-left:6px;font-size:12.5px}
.cal-ag-item{display:flex;align-items:center;gap:10px;width:100%;text-align:left;border:none;background:transparent;padding:9px 4px;border-top:1px solid var(--line);cursor:pointer}
.cal-ag-item:first-of-type{border-top:none}
.p-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.dot-med{background:var(--c-med)}.dot-edu{background:var(--c-edu)}.dot-study{background:var(--c-study)}.dot-npl{background:var(--mut)}
.ai-main{flex:1;min-width:0}.ai-main b{display:block;font-size:13.5px;color:var(--ink);font-variant-numeric:tabular-nums}
.ai-sub{display:block;font-size:11.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ai-right{text-align:right;font-size:13px;font-weight:800;color:var(--navy);white-space:nowrap}
:root[data-theme=dark] .ai-right{color:var(--blue)}
.ai-dr{display:block;font-size:10.5px;font-weight:800;padding:1px 5px;border-radius:5px;margin-top:2px}
.card.flash{animation:flash 1.6s ease}
@keyframes flash{0%,100%{box-shadow:var(--shadow)}18%{box-shadow:0 0 0 3px var(--blue),var(--shadow2)}}
@media(max-width:720px){.cal-grid-wrap{display:none}.cal-agenda{display:block}}
/* Responsive */
@media(max-width:1080px){.charts{grid-template-columns:1fr 1fr}.chart:nth-child(3){grid-column:1/-1}
 .kpis{grid-template-columns:repeat(3,1fr)}}
@media(max-width:760px){
 .wrap{padding:12px}.hero{padding:20px;border-radius:18px}.hero h1{font-size:22px}
 .kpis{grid-template-columns:repeat(2,1fr);gap:10px}.kpi .kv{font-size:23px}
 .charts{grid-template-columns:1fr}.chart:nth-child(3){grid-column:auto}
 .count{width:100%;margin-left:0}
 thead{display:none}table,tbody,tr,td{display:block;width:100%}
 tbody tr{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin:10px;padding:5px 4px}
 tbody td{border:none;padding:8px 13px;display:flex;justify-content:space-between;gap:14px;text-align:right}
 tbody td::before{content:attr(data-label);color:var(--mut);font-size:12px;font-weight:700;text-align:left}
 .t-addr{min-width:0}.t-sub{text-align:right}
 tbody tr:hover{background:var(--surface)}
}
@media(max-width:420px){.kpis{grid-template-columns:1fr 1fr}.cards{grid-template-columns:1fr}}
"""

    SCRIPT = r"""
(function(){
 var root=document.documentElement;
 try{var sv=localStorage.getItem('ca-theme');if(sv)root.setAttribute('data-theme',sv);}catch(e){}
 function setIcon(){var d=root.getAttribute('data-theme')==='dark';document.getElementById('themeBtn').textContent=d?'☀️':'🌙';}
 setIcon();
 document.getElementById('themeBtn').onclick=function(){
  var d=root.getAttribute('data-theme')==='dark';root.setAttribute('data-theme',d?'light':'dark');
  try{localStorage.setItem('ca-theme',d?'light':'dark');}catch(e){}setIcon();};

 var q=document.getElementById('q'),cnt=document.getElementById('cnt');
 var cards=[].slice.call(document.querySelectorAll('#cards .card'));
 var rows=[].slice.call(document.querySelectorAll('#tb tr'));
 var items=cards.map(function(c,idx){return {c:c,r:rows[idx],d:c.dataset};});
 document.querySelectorAll('.cal-entry,.cal-ag-item').forEach(function(el){
  var i=+el.dataset.idx;if(items[i]){(items[i].cals=items[i].cals||[]).push(el);}});
 var agDays=[].slice.call(document.querySelectorAll('.cal-ag-day'));
 var activeChip='';

 function apply(){
  var t=(q.value||'').trim().toLowerCase();var n=0;
  items.forEach(function(it){
   var ok=(!t||it.d.text.indexOf(t)>=0);
   it.c.style.display=ok?'':'none';if(it.r)it.r.style.display=ok?'':'none';
   if(it.cals)it.cals.forEach(function(el){el.style.display=ok?'':'none';});
   if(ok)n++;});
  agDays.forEach(function(day){
   var any=[].slice.call(day.querySelectorAll('.cal-ag-item')).some(function(el){return el.style.display!=='none';});
   day.style.display=any?'':'none';});
  cnt.innerHTML='<b>'+n+'</b> 건';
 }
 q.addEventListener('input',function(){activeChip='';paintChips();apply();});

 function paintChips(){document.querySelectorAll('.chip').forEach(function(c){
  c.classList.toggle('on',c.dataset.q===activeChip&&activeChip!=='');});}
 document.querySelectorAll('.chip').forEach(function(c){c.onclick=function(){
  if(activeChip===c.dataset.q){activeChip='';q.value='';}else{activeChip=c.dataset.q;q.value=c.dataset.q;}
  paintChips();apply();};});

 var sortSel=document.getElementById('sort');
 function sortBy(key,asc){
  var vis=items.slice();
  vis.sort(function(a,b){
   var x=a.d[key],y=b.d[key];var nx=parseFloat(x),ny=parseFloat(y);
   if(!isNaN(nx)&&!isNaN(ny)){x=nx;y=ny;}
   return (x>y?1:x<y?-1:0)*(asc?1:-1);});
  var cc=document.getElementById('cards'),tb=document.getElementById('tb');
  vis.forEach(function(it){cc.appendChild(it.c);if(it.r)tb.appendChild(it.r);});
 }
 sortSel.onchange=function(){
  var v=this.value;
  if(v==='giil')sortBy('giil',true);
  else if(v==='low')sortBy('low',true);
  else if(v==='ppp')sortBy('ppp',true);
  else if(v==='dr')sortBy('dr',false);
 };

 function setView(v){
  document.querySelectorAll('.vtoggle button').forEach(function(x){x.classList.toggle('on',x.dataset.v===v);});
  document.body.classList.remove('view-table','view-cal');
  if(v==='table')document.body.classList.add('view-table');
  else if(v==='cal')document.body.classList.add('view-cal');
 }
 document.querySelectorAll('.vtoggle button').forEach(function(b){b.onclick=function(){setView(b.dataset.v);};});

 function gotoCard(i){setView('card');var c=items[i]&&items[i].c;if(!c)return;
  c.scrollIntoView({behavior:'smooth',block:'center'});
  c.classList.remove('flash');void c.offsetWidth;c.classList.add('flash');}
 document.querySelectorAll('.cal-entry,.cal-ag-item').forEach(function(el){
  el.addEventListener('click',function(){gotoCard(+el.dataset.idx);});});

 function icsEsc(s){return String(s||'').replace(/\\/g,'\\\\').replace(/;/g,'\\;').replace(/,/g,'\\,').replace(/\n/g,'\\n');}
 function addHour(hhmm){var h=Math.min(parseInt(hhmm.slice(0,2),10)+1,23),m=hhmm.slice(3,5);return (h<10?'0'+h:''+h)+m;}
 var icsBtn=document.getElementById('icsBtn');
 if(icsBtn)icsBtn.onclick=function(){
  var vis={};items.forEach(function(it){if(it.c.style.display!=='none')vis[+it.d.idx]=1;});
  var evs=(window.CAL||[]).filter(function(e){return vis[e.i];});
  if(!evs.length){alert('내보낼 일정이 없습니다.');return;}
  var L=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//courtauction//KR//','CALSCALE:GREGORIAN','METHOD:PUBLISH'];
  evs.forEach(function(e){
   var g=e.g,timed=!!e.hh,hm=timed?e.hh.replace(':',''):'';
   L.push('BEGIN:VEVENT','UID:'+g+'-'+e.i+'@courtauction','DTSTAMP:'+g+'T000000');
   if(timed){L.push('DTSTART:'+g+'T'+hm+'00','DTEND:'+g+'T'+addHour(e.hh)+'00');}
   else{L.push('DTSTART;VALUE=DATE:'+g);}
   L.push('SUMMARY:'+icsEsc('[경매] '+e.cs+' '+e.kw+' 최저'+e.low+' ▼'+e.dr+'%'));
   L.push('LOCATION:'+icsEsc((e.ct+' '+e.pl).trim()));
   L.push('DESCRIPTION:'+icsEsc([e.ad,'감정가 대비 '+e.dr+'% 할인 · 최저 '+e.low,e.tel?('담당 '+e.tel):''].filter(Boolean).join('\n')));
   L.push('BEGIN:VALARM','TRIGGER:-P1D','ACTION:DISPLAY','DESCRIPTION:'+icsEsc('경매 1일 전 · '+e.cs),'END:VALARM');
   L.push('BEGIN:VALARM','TRIGGER:-P1W','ACTION:DISPLAY','DESCRIPTION:'+icsEsc('경매 1주 전 · '+e.cs),'END:VALARM');
   L.push('END:VEVENT');
  });
  L.push('END:VCALENDAR');
  var blob=new Blob([L.join('\r\n')],{type:'text/calendar;charset=utf-8'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='경매일정_'+evs.length+'건.ics';document.body.appendChild(a);a.click();a.remove();
 };

 document.querySelectorAll('thead th[data-sk]').forEach(function(th){var asc=true;th.onclick=function(){
  sortBy(th.dataset.sk,asc);asc=!asc;
  document.querySelectorAll('thead th .ar').forEach(function(a){a.textContent='';});
  th.querySelector('.ar').textContent=asc?'▲':'▼';};});
})();
"""

    from datetime import datetime as _dt2
    gen = _dt2.now().strftime("%Y.%m.%d %H:%M")
    page = (
        "<!doctype html><html lang=ko><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<meta name=robots content='noindex,nofollow'>"
        f"<title>{title} · 법원경매 분석</title>"
        "<link rel=preconnect href='https://cdn.jsdelivr.net'>"
        "<link rel=stylesheet href='https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css'>"
        f"<style>{STYLE}</style></head><body><div class=wrap>"
        "<div class=hero><div class=h-top><div>"
        "<div class=brand>COURT AUCTION INTELLIGENCE</div>"
        f"<h1>{title}</h1><div class=sub>{subtitle}</div></div>"
        "<button class=theme-btn id=themeBtn aria-label='테마 전환'>🌙</button></div>"
        f"<div class=cond>{html.escape(cond)}</div></div>"
        f"<div class=kpis>{kpi_html}</div>"
        "<div class=charts>"
        f"<div class=chart><h4>할인율 분포 (감정가 대비)</h4><div class=vbars>{dbars}</div></div>"
        f"<div class=chart><h4>지역별 매물 수</h4><div class=hbars>{rbars}</div></div>"
        f"<div class=chart><h4>이용유형 구성</h4><div class=compbar>{comp}</div><div class=legend>{legend}</div></div>"
        "</div>"
        "<div class=toolbar>"
        "<div class=search><input id=q placeholder='소재지·사건번호·채권자·이용상태 검색…'></div>"
        "<select class=sort id=sort><option value=giil>매각기일순</option>"
        "<option value=dr>할인율 높은순</option><option value=low>최저가 낮은순</option>"
        "<option value=ppp>평단가 낮은순</option></select>"
        "<div class=vtoggle><button class=on data-v=card>카드</button>"
        "<button data-v=table>표</button><button data-v=cal>달력</button></div>"
        f"<div class=count id=cnt><b>{n}</b> 건</div></div>"
        f"<div class=chipwrap><span class=lbl>이용유형</span>{chips_kw}</div>"
        f"<div class=chipwrap><span class=lbl>지역</span>{chips_rg}</div>"
        f"<div class=cards id=cards>{cards}</div>"
        f"<div class=tablewrap><table><thead><tr>{thhtml}</tr></thead><tbody id=tb>{rows_tbl}</tbody></table></div>"
        "<div class=calwrap><div class=cal-toolbar>"
        "<button class=ics-btn id=icsBtn>📅 화면의 일정을 내 캘린더로 (.ics)</button></div>"
        f"{cal_html}</div>"
        f"<div class=foot>법원경매 원천데이터(대법원) 직수집 · 총 <b>{n}</b>건 · 생성 {gen}<br>"
        "평단가는 건물 감정면적 기준 · 금액 올림 · 개인 투자조사용 (재판매·외부공개 금지)</div>"
        f"</div><script>window.CAL={cal_json};</script><script>{SCRIPT}</script></body></html>")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    # ── 슬랙/알림용 요약 JSON (스키마 유지) ──
    top = []
    for h in recs[:8]:
        r = h["row"]; c = h.get("cred") or {}
        gv = i(r.get("gamevalAmt")); mv = i(r.get("minmaePrice"))
        top.append({
            "kw": h["kw"], "csno": parse_csno(r.get("printCsNo")) or "",
            "court": r.get("jiwonNm") or "",
            "addr": (r.get("printSt") or r.get("convAddr") or "").replace("\n", " ").strip()[:40],
            "gam": won(gv), "low": won(mv),
            "drate": round((1 - mv / gv) * 100) if gv else 0,
            "yuchal": r.get("yuchalCnt") or "0", "giil": r.get("maeGiil") or "",
            "npl": bool(c.get("npl")), "types": c.get("types") or [], "names": c.get("names") or [],
        })
    summary = {
        "mode": "NPL" if NPL_ONLY else "이용상태", "title": title,
        "count": n, "npl": npl_cnt, "types": dict(type_cnt),
        "by_kw": dict(kwc.most_common()), "by_region": dict(sgg.most_common()),
        "avg_drate": avg_dr, "avg_ppp": avg_ppp, "top": top, "file": os.path.basename(out),
    }
    sjson = os.path.join(BASE, "summary_npl.json" if NPL_ONLY else "summary_use.json")
    with open(sjson, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(locale="ko-KR")).new_page()
        await page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)   # WebSquare 세션·쿠키 초기화 대기(CI 안정화)

        rows, total = await get_all_list(page)
        # 순수 토지 물건 제외 (건물/집합건물 없는 토지·임야·대·도로·전·답)
        if not INCLUDE_LAND:
            def is_land_only(r):
                ca = str(r.get("convAddr") or "")
                return ("건물" not in ca) and bool(re.search(r"\[\s*(토지|대|임야|도로|전|답|과수원|잡종지|목장)", ca))
            before = len(rows)
            rows = [r for r in rows if not is_land_only(r)]
            print(f"리스트 수집 완료: {len(rows)}건 (토지제외 {before - len(rows)}건)", flush=True)
        else:
            print(f"리스트 수집 완료: {len(rows)}건", flush=True)

        # 1차: 리스트 텍스트(convAddr 등) 직접 매칭
        def list_text(r):
            return " ".join(str(r.get(k) or "") for k in ("convAddr", "mulBigo", "printSt", "dspslUsgNm", "buldNm"))
        hits = {}   # key -> record
        def key(r):
            return (r.get("boCd"), r.get("saNo"), r.get("maemulSer"))
        for r in rows:
            m = KW_RE.search(list_text(r))
            if m:
                hits[key(r)] = {"row": r, "kw": m.group(0), "evi": list_text(r), "via": "리스트"}

        # 2차: 상세(감정평가 이용상태) — 사건 단위 dedupe 후 조회
        uniq = {}
        for r in rows:
            csno = parse_csno(r.get("printCsNo"))
            if not csno:
                continue
            uniq.setdefault((r.get("boCd"), csno), r)   # 사건당 대표 1행
        print(f"상세 조회 대상(사건 단위): {len(uniq)}건 — 이용상태 정밀 확인중...", flush=True)

        items = list(uniq.items())
        done = 0
        sem = asyncio.Semaphore(DETAIL_CONC)
        async def work(k, r):
            nonlocal done
            async with sem:
                csno = k[1]
                text = await get_detail(page, r.get("boCd"), csno, r.get("maemulSer") or "1")
            done += 1
            if done % 25 == 0:
                print(f"  상세 {done}/{len(items)}...", flush=True)
            m = KW_RE.search(text or "")
            if m:
                # 해당 사건의 모든 물건행을 hit로
                for rr in rows:
                    if rr.get("boCd") == k[0] and parse_csno(rr.get("printCsNo")) == csno:
                        kk = key(rr)
                        if kk not in hits:
                            snip = text[max(0, m.start()-30):m.start()+40].replace("\n", " ")
                            hits[kk] = {"row": rr, "kw": m.group(0), "evi": snip, "via": "감정평가/현황"}
        await asyncio.gather(*[work(k, r) for k, r in items])

        # ── 채권자/NPL 분석 ──
        cred_by_case = {}
        if NPL_ONLY:
            # 이용상태 무관, 전체 사건 채권자 조회 → 승계(NPL) 물건만
            targets = [(r.get("boCd"), r.get("saNo"), r) for _, r in items]
            print(f"채권자/NPL 조회(전체 {len(targets)}사건)...", flush=True)
        elif CREDITOR_ON:
            seen = {}
            for h in hits.values():
                rr = h["row"]
                seen[(rr.get("boCd"), rr.get("saNo"))] = rr
            targets = [(bo, sa, rr) for (bo, sa), rr in seen.items()]
            print(f"채권자/NPL 조회(매칭 {len(targets)}사건)...", flush=True)
        else:
            targets = []
        cdone = 0
        async def cwork(bo, sa, rr):
            nonlocal cdone
            async with sem:
                cred_by_case[(bo, sa)] = await get_creditor(page, bo, sa)
            cdone += 1
            if cdone % 25 == 0:
                print(f"  채권자 {cdone}/{len(targets)}...", flush=True)
        await asyncio.gather(*[cwork(bo, sa, rr) for bo, sa, rr in targets])

        # NPL_ONLY: 승계물건을 hits로 재구성 (사건 대표행 1개)
        if NPL_ONLY:
            hits = {}
            for _, r in items:
                cr = cred_by_case.get((r.get("boCd"), r.get("saNo")), {})
                if cr.get("npl") or cr.get("types"):
                    tag = "+".join(cr.get("types") or []) or "채권승계"
                    hits[key(r)] = {"row": r, "kw": tag, "evi": (cr.get("npl_lines") or [""])[0], "via": "NPL"}
        await browser.close()

    # 각 hit에 채권자 정보 부착
    for h in hits.values():
        rr = h["row"]
        h["cred"] = cred_by_case.get((rr.get("boCd"), rr.get("saNo")), {})
    try:
        json.dump(CCACHE, open(CRED_CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

    # 결과 정리/출력
    # 캐시 저장 (다음 실행부터 즉시)
    try:
        json.dump(DCACHE, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

    recs = list(hits.values())
    # 지역 필터 (REGION 지정시)
    if REGION_FILTER:
        def in_region(h):
            addr = (h["row"].get("printSt") or h["row"].get("convAddr") or "")
            return any(rg in addr for rg in REGION_FILTER)
        recs = [h for h in recs if in_region(h)]
    recs.sort(key=lambda x: x["row"].get("maeGiil") or "")

    # 채권자 요약 문자열
    def cred_str(h):
        c = h.get("cred") or {}
        if not c:
            return ""
        tag = "🔴NPL" if c.get("npl") else ""
        if c.get("types"):
            tag += "(" + ",".join(c["types"]) + ")"
        if c.get("names"):
            tag += " 채권자:" + "/".join(c["names"][:3])
        return tag.strip()

    # 요약 통계
    from collections import Counter
    kwc = Counter(h["kw"] for h in recs)
    sgg = Counter((re.search(r"경기도\s*([가-힣]+시)", (h["row"].get("printSt") or h["row"].get("convAddr") or "")) or [None, "기타"])[1] for h in recs)
    npl_cnt = sum(1 for h in recs if (h.get("cred") or {}).get("npl"))
    type_cnt = Counter(t for h in recs for t in ((h.get("cred") or {}).get("types") or []))
    print(f"\n★ 매칭 물건: {len(recs)}건" + (f" (지역필터: {','.join(REGION_FILTER)})" if REGION_FILTER else ""), flush=True)
    print("  키워드별:", dict(kwc.most_common()), flush=True)
    print("  지역별:", dict(sgg.most_common()), flush=True)
    print(f"  NPL(채권승계): {npl_cnt}건  유형:{dict(type_cnt)}", flush=True)
    print("=" * 70, flush=True)
    def won(v):
        try: return f"{int(v):,}"
        except: return v or ""
    for h in recs:
        r = h["row"]
        addr = (r.get("printSt") or r.get("convAddr") or "").replace("\n", " ").strip()
        print(f"[{h['kw']}] {parse_csno(r.get('printCsNo'))} · {r.get('jiwonNm')}  {cred_str(h)}")
        print(f"   {addr[:60]}")
        print(f"   감정 {won(r.get('gamevalAmt'))} / 최저 {won(r.get('minmaePrice'))} / 유찰 {r.get('yuchalCnt')}회 / 기일 {r.get('maeGiil')} · [{h['via']}] {h['evi'][:50]}")

    # ── 판매급 반응형 HTML 리포트 ──
    out = os.path.join(BASE, "results_npl.html" if NPL_ONLY else "results_use.html")
    write_report(out, recs, npl_cnt, type_cnt, kwc, sgg)
    print(f"\nHTML 저장: {out}", flush=True)
    if os.environ.get("NO_OPEN", "") != "1":
        try:
            if sys.platform == "win32":
                os.startfile(out)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f"open '{out}'")
            else:
                os.system(f"xdg-open '{out}'")
        except Exception:
            pass

asyncio.run(main())
