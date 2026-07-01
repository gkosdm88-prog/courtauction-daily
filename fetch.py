"""법원경매 상가(근린생활시설) → 이용상태 기반 '스터디카페/학원/병원/의원' 자동 추출
- 리스트검색(searchControllerMain) + 물건상세(감정평가 이용상태) 를 브라우저 세션 안에서 fetch
- 캡처한 실제 검색조건(captured/63)을 그대로 사용 (경기도 / 건물>상업용및업무용>근린생활시설)
"""
import asyncio, json, os, re, sys, html
from playwright.async_api import async_playwright

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
    CCACHE = json.load(open(CRED_CACHE_FILE))
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
    DCACHE = json.load(open(CACHE_FILE))
except Exception:
    DCACHE = {}

# 페이지당 결과 수 (서버 고정: 40)
PAGE_SIZE = 40
MAX_PAGES = int(os.environ.get("MAX_PAGES", "999"))   # 테스트시 제한 가능
DETAIL_CONC = 6

base_payload = json.loads(open(os.path.join(CAP, "63_searchControllerMain.json")).read())
BASE_SRCH = json.loads(base_payload["postData"])["dma_srchGdsDtlSrchInfo"]
detail_tmpl = json.loads(open(os.path.join(CAP, "74_selectAuctnCsSrchRslt.json")).read())["postData"]
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
FILT = {
    "sclDspslGdsLstUsgCd": SCL,
    "rprsAdongSdCd": envf("SIDO") or None,        # 시도코드 (41경기 11서울 등)
    "rprsAdongSggCd": envf("SGG") or None,        # 시군구코드
    "cortOfcCd": envf("COURT") or None,           # 법원코드
    "aeeEvlAmtMin": envf("AMT_MIN") or None, "aeeEvlAmtMax": envf("AMT_MAX") or None,     # 감정가(원)
    "rletLwsDspslPrcMin": envf("LOW_MIN") or None, "rletLwsDspslPrcMax": envf("LOW_MAX") or None,  # 최저가(원)
    "flbdNcntMin": envf("FLBD_MIN") or None, "flbdNcntMax": envf("FLBD_MAX") or None,     # 유찰횟수
    "bidBgngYmd": envf("BID_FROM") or None, "bidEndYmd": envf("BID_TO") or None,          # 매각기일 YYYYMMDD
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
    """판매급 반응형 HTML 리포트 생성 (데스크톱 테이블 ↔ 모바일 카드)"""
    def won(v):
        try: return f"{int(v):,}"
        except Exception: return v or ""

    title = "부실채권(NPL) 상가" if NPL_ONLY else "상가 이용상태 분석"
    subtitle = "채권 승계(유동화·대부·자산관리) 물건" if NPL_ONLY else "이용상태 키워드 매칭 물건"
    cond = "경기도 · 건물＞상업용및업무용＞근린생활시설" + (" · " + ",".join(REGION_FILTER) if REGION_FILTER else "")

    USE_CAT = [(["의원","병원","한의원","치과","약국","요양","산후조리"], "b-med"),
               (["학원","교습소","독서실","어학원","교육원"], "b-edu"),
               (["스터디","카페","고시원","고시텔"], "b-study")]
    def kw_cls(kw):
        for words, cls in USE_CAT:
            if any(w in kw for w in words): return cls
        return "b-npl"

    rows_out = []
    for h in recs:
        r = h["row"]
        addr = html.escape((r.get("printSt") or r.get("convAddr") or "").replace("\n", " ").strip())
        csno = html.escape(parse_csno(r.get("printCsNo")) or "")
        court = html.escape(r.get("jiwonNm") or "")
        gv = int(r.get("gamevalAmt") or 0); mv = int(r.get("minmaePrice") or 0)
        dr = round((1 - mv / gv) * 100) if gv else 0
        drcls = "d-hi" if dr >= 50 else ("d-mid" if dr >= 30 else "d-lo")
        yc = str(r.get("yuchalCnt") or "0")
        giil = str(r.get("maeGiil") or "")
        giil_fmt = f"{giil[:4]}.{giil[4:6]}.{giil[6:]}" if len(giil) == 8 else giil
        c = h.get("cred") or {}
        badges = ""
        if c.get("npl"): badges += '<span class="tag t-npl">NPL</span>'
        for t in (c.get("types") or []): badges += f'<span class="tag t-type">{html.escape(t)}</span>'
        names = html.escape("/".join(c.get("names", [])[:2])) if c.get("names") else ""
        evi = html.escape((h.get("evi") or "")[:80])
        kw = html.escape(h["kw"])
        rows_out.append(
            f'<tr>'
            f'<td data-label="이용/유형"><span class="pill {kw_cls(h["kw"])}">{kw}</span></td>'
            f'<td data-label="사건"><span class="cs">{csno}</span><span class="sub">{court}</span></td>'
            f'<td data-label="소재지" class="addr">{addr}</td>'
            f'<td data-label="감정가" class="num" data-sort="{gv}">{won(gv)}</td>'
            f'<td data-label="최저가" class="num hl" data-sort="{mv}">{won(mv)}</td>'
            f'<td data-label="할인율" class="ctr" data-sort="{dr}"><span class="drate {drcls}">{dr}%</span></td>'
            f'<td data-label="유찰" class="ctr" data-sort="{yc}">{yc}회</td>'
            f'<td data-label="매각기일" class="ctr" data-sort="{giil}">{giil_fmt}</td>'
            f'<td data-label="채권자/NPL">{badges}<div class="sub">{names}</div></td>'
            f'<td data-label="근거" class="evi">{evi}</td>'
            f'</tr>')
    rowshtml = "\n".join(rows_out)

    typed = sum(type_cnt.values())
    avg_dr = round(sum(round((1 - int(h["row"].get("minmaePrice") or 0) / int(h["row"].get("gamevalAmt") or 1)) * 100)
                       for h in recs) / len(recs)) if recs else 0
    kpis = [("매칭 물건", f"{len(recs)}", "건", "k-main"),
            ("NPL 채권승계", f"{npl_cnt}", "건", "k-npl"),
            ("유동화·대부·자산관리", f"{typed}", "건 확인", "k-type"),
            ("평균 할인율", f"{avg_dr}", "%", "k-dr"),
            ("지역", f"{len(sgg)}", "곳", "k-reg")]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><div class="kv">{v}<small>{u}</small></div><div class="kl">{html.escape(lbl)}</div></div>'
        for lbl, v, u, cls in kpis)
    chips_kw = "".join(f'<button class="chip" data-q="{html.escape(k)}">{html.escape(k)} <b>{v}</b></button>' for k, v in kwc.most_common())
    chips_rg = "".join(f'<button class="chip" data-q="{html.escape(k)}">{html.escape(k)} <b>{v}</b></button>' for k, v in sgg.most_common())

    STYLE = """
:root{--navy:#0f1e3d;--navy2:#1c3a6e;--blue:#2563eb;--sky:#e8f0fe;--ink:#1a2233;--mut:#6b7688;--line:#e6eaf0;--bg:#f4f6fb;--red:#e0245e;--amber:#f59e0b;--green:#0f9d58}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',system-ui,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;font-feature-settings:'tnum'}
a{color:inherit;text-decoration:none}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
.hero{background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;border-radius:20px;padding:26px 26px 22px;box-shadow:0 10px 30px rgba(15,30,61,.25);position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;background:radial-gradient(circle,rgba(37,99,235,.45),transparent 70%)}
.brand{font-size:12px;letter-spacing:3px;color:#9fb4e8;font-weight:700;text-transform:uppercase}
.hero h1{margin:6px 0 4px;font-size:26px;font-weight:800;letter-spacing:-.5px}
.hero .sub{color:#c7d5f2;font-size:14px}
.hero .cond{margin-top:10px;font-size:12.5px;color:#aebfe4;background:rgba(255,255,255,.08);display:inline-block;padding:5px 11px;border-radius:999px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:16px 0}
.kpi{background:#fff;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(20,40,80,.05);border:1px solid var(--line);position:relative;overflow:hidden}
.kpi .kv{font-size:26px;font-weight:800;color:var(--navy);letter-spacing:-1px}
.kpi .kv small{font-size:12px;font-weight:600;color:var(--mut);margin-left:3px}
.kpi .kl{font-size:12.5px;color:var(--mut);margin-top:3px;font-weight:600}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--blue)}
.k-npl::before{background:var(--red)}.k-type::before{background:var(--amber)}.k-dr::before{background:var(--green)}.k-reg::before{background:#8b5cf6}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0 10px}
.search{flex:1;min-width:200px;position:relative}
.search input{width:100%;padding:12px 14px 12px 40px;border:1px solid var(--line);border-radius:12px;font-size:14px;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' fill='none' stroke='%236b7688' stroke-width='2'%3E%3Ccircle cx='8' cy='8' r='6'/%3E%3Cpath d='M13 13l4 4'/%3E%3C/svg%3E") 12px center no-repeat;outline:none;transition:.15s}
.search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.count{font-size:13px;color:var(--mut);font-weight:600;white-space:nowrap}
.chipwrap{margin:6px 0 14px}
.chipwrap .lbl{font-size:11.5px;color:var(--mut);font-weight:700;margin:0 6px 6px 0;text-transform:uppercase;letter-spacing:.5px}
.chip{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:999px;padding:6px 12px;font-size:12.5px;font-weight:600;cursor:pointer;margin:0 6px 6px 0;transition:.12s}
.chip:hover{border-color:var(--blue);color:var(--blue)}
.chip b{color:var(--blue);margin-left:2px}
.card{background:#fff;border-radius:16px;box-shadow:0 2px 14px rgba(20,40,80,.06);border:1px solid var(--line);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13.5px}
thead th{position:sticky;top:0;background:var(--navy);color:#fff;font-weight:700;text-align:left;padding:13px 12px;white-space:nowrap;cursor:pointer;user-select:none;font-size:12.5px;z-index:2}
thead th:hover{background:var(--navy2)}
thead th.num,thead th.ctr{text-align:right}
thead th .ar{opacity:.4;font-size:10px;margin-left:3px}
tbody td{padding:12px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:hover{background:var(--sky)}
tbody tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ctr{text-align:center}
.hl{font-weight:800;color:var(--navy)}
.cs{font-weight:700;color:var(--blue);display:block}
.sub{font-size:11.5px;color:var(--mut)}
.addr{min-width:180px;color:#2a3550}
.evi{font-size:11.5px;color:var(--mut);max-width:220px}
.pill{display:inline-block;padding:4px 10px;border-radius:8px;font-size:12px;font-weight:700;white-space:nowrap}
.b-med{background:#e6f6ee;color:#0f9d58}.b-edu{background:#e8f0fe;color:#2563eb}.b-study{background:#f3e8fd;color:#8b5cf6}.b-npl{background:#fde8ef;color:#e0245e}
.tag{display:inline-block;padding:3px 8px;border-radius:7px;font-size:11px;font-weight:800;margin:1px 3px 1px 0}
.t-npl{background:var(--red);color:#fff}.t-type{background:#fff3e0;color:#c47500;border:1px solid #ffd699}
.drate{display:inline-block;padding:3px 9px;border-radius:999px;font-weight:800;font-size:12px}
.d-hi{background:#e6f6ee;color:#0f9d58}.d-mid{background:#fff7e6;color:#c47500}.d-lo{background:#eef1f6;color:#6b7688}
.foot{text-align:center;color:var(--mut);font-size:12px;padding:22px 0 30px}
.foot b{color:var(--navy)}
@media(max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}}
@media(max-width:820px){
 .wrap{padding:10px}.hero{border-radius:16px;padding:20px}.hero h1{font-size:21px}
 .kpis{grid-template-columns:repeat(2,1fr);gap:9px}.kpi{padding:13px}.kpi .kv{font-size:22px}
 thead{display:none}
 table,tbody,tr,td{display:block;width:100%}
 tbody tr{background:#fff;border:1px solid var(--line);border-radius:14px;margin-bottom:12px;padding:6px 4px;box-shadow:0 1px 6px rgba(20,40,80,.05)}
 tbody td{border:none;padding:9px 14px;display:flex;justify-content:space-between;align-items:center;gap:14px;text-align:right}
 tbody td::before{content:attr(data-label);color:var(--mut);font-size:12px;font-weight:700;text-align:left;flex-shrink:0}
 td.addr,td.evi{text-align:right}.addr{min-width:0}.evi{max-width:none}
 tbody tr:hover{background:#fff}
}
@media(max-width:420px){.kpis{grid-template-columns:1fr 1fr}.kpi .kv{font-size:20px}}
"""
    SCRIPT = """
const q=document.getElementById('q'),tb=document.getElementById('tb'),cnt=document.getElementById('cnt');
const rows=[...tb.rows];
function apply(){const t=q.value.trim().toLowerCase();let n=0;
 rows.forEach(r=>{const v=r.textContent.toLowerCase().includes(t);r.style.display=v?'':'none';if(v)n++;});
 cnt.textContent=n+' 건';}
q.addEventListener('input',apply);
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{q.value=c.dataset.q;apply();window.scrollTo({top:document.querySelector('.card').offsetTop-70,behavior:'smooth'});});
document.querySelectorAll('thead th').forEach((th,i)=>{let asc=true;th.onclick=()=>{
 const vis=rows.filter(r=>r.style.display!=='none');
 vis.sort((a,b)=>{let x=a.cells[i].dataset.sort??a.cells[i].textContent,y=b.cells[i].dataset.sort??b.cells[i].textContent;
  const nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)){x=nx;y=ny;}return (x>y?1:x<y?-1:0)*(asc?1:-1);});
 asc=!asc;vis.forEach(r=>tb.appendChild(r));
 document.querySelectorAll('thead th .ar').forEach(a=>a.textContent='');
 const ar=th.querySelector('.ar');if(ar)ar.textContent=asc?'▼':'▲';};});
"""
    heads = ["이용/유형", "사건", "소재지", "감정가", "최저가", "할인율", "유찰", "매각기일", "채권자/NPL", "근거"]
    thhtml = "".join(f'<th class="{"num" if h in ("감정가","최저가") else ("ctr" if h in ("할인율","유찰","매각기일") else "")}">{h}<span class="ar"></span></th>' for h in heads)
    from datetime import datetime as _dt
    page = (
        "<!doctype html><html lang=ko><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        f"<title>{title} · 법원경매 분석 리포트</title>"
        "<link rel=preconnect href='https://cdn.jsdelivr.net'>"
        "<link rel=stylesheet href='https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css'>"
        f"<style>{STYLE}</style></head><body><div class=wrap>"
        f"<div class=hero><div class=brand>COURT AUCTION INTELLIGENCE</div>"
        f"<h1>{title}</h1><div class=sub>{subtitle}</div>"
        f"<div class=cond>{html.escape(cond)}</div></div>"
        f"<div class=kpis>{kpi_html}</div>"
        f"<div class=toolbar><div class=search><input id=q placeholder='소재지·사건번호·채권자·이용상태 검색…'></div>"
        f"<div class=count id=cnt>{len(recs)} 건</div></div>"
        f"<div class=chipwrap><span class=lbl>{'유형' if NPL_ONLY else '이용상태'}</span>{chips_kw}</div>"
        f"<div class=chipwrap><span class=lbl>지역</span>{chips_rg}</div>"
        f"<div class=card><table><thead><tr>{thhtml}</tr></thead><tbody id=tb>{rowshtml}</tbody></table></div>"
        f"<div class=foot>법원경매 원천데이터(대법원) 직수집 · 총 <b>{len(recs)}</b>건 · 열 제목 클릭시 정렬 · 개인 투자조사용</div>"
        f"</div><script>{SCRIPT}</script></body></html>")
    with open(out, "w") as f:
        f.write(page)

    # 슬랙/알림용 요약 JSON (일일보고 파이프라인이 읽음)
    top = []
    for h in recs[:8]:
        r = h["row"]
        c = h.get("cred") or {}
        gv = int(r.get("gamevalAmt") or 0); mv = int(r.get("minmaePrice") or 0)
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
        "count": len(recs), "npl": npl_cnt, "types": dict(type_cnt),
        "by_kw": dict(kwc.most_common()), "by_region": dict(sgg.most_common()),
        "avg_drate": avg_dr, "top": top, "file": os.path.basename(out),
    }
    sjson = os.path.join(BASE, "summary_npl.json" if NPL_ONLY else "summary_use.json")
    with open(sjson, "w") as f:
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
        json.dump(CCACHE, open(CRED_CACHE_FILE, "w"), ensure_ascii=False)
    except Exception:
        pass

    # 결과 정리/출력
    # 캐시 저장 (다음 실행부터 즉시)
    try:
        json.dump(DCACHE, open(CACHE_FILE, "w"), ensure_ascii=False)
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
        os.system(f"open '{out}'")

asyncio.run(main())
