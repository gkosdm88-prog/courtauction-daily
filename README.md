# 법원경매 상가 이용상태 추출기

대법원 법원경매정보(courtauction.go.kr)에서 **경기도 근린생활시설 상가**를 전부 받아,
감정평가·현황조사 텍스트의 **이용상태**로 원하는 업종(스터디카페/학원/병원/의원 등)을 걸러낸다.
옥션원·탱크옥션·CODEF 같은 유료서비스 없이 **원천데이터(대법원) 직접 수집**.

## 실행
```bash
cd /Users/semi/online/rental/realestate-analyzer/courtauction
/Users/semi/online/venv/bin/python3 fetch.py
```
결과: `results_상가_이용상태.html` 자동 오픈.

## 옵션 (환경변수)
```bash
# ── 이용상태 키워드 ──
KW="스터디,카페,독서실,고시원" python3 fetch.py

# ── 옥션원식 필터 ──
YONGDO=의료 python3 fetch.py        # 근린/판매/의료/교육/업무/위락/숙박/사무실...
SIDO=41 python3 fetch.py             # 시도코드(41경기 11서울 28인천 등)
SGG=650 python3 fetch.py             # 시군구코드
AMT_MIN=100000000 AMT_MAX=500000000 python3 fetch.py   # 감정가(원)
LOW_MIN=0 LOW_MAX=300000000 python3 fetch.py            # 최저가(원)
FLBD_MIN=2 python3 fetch.py          # 유찰 2회 이상
BID_FROM=20260701 BID_TO=20260930 python3 fetch.py     # 매각기일 범위

# ── 채권자/NPL (유동화·대부·농협자산관리) ──
python3 fetch.py                     # 매칭물건에 NPL/채권자 자동표시(기본 ON)
NPL_ONLY=1 python3 fetch.py          # 이용상태 무관, 채권승계(NPL) 물건만 따로
CREDITOR=0 python3 fetch.py          # 채권자 조회 끄기(빠름)

# 특정 시군구만(결과 후필터) / 페이지 제한(테스트) / 토지포함
REGION="화성,평택" python3 fetch.py
MAX_PAGES=3 python3 fetch.py
INCLUDE_LAND=1 python3 fetch.py     # 순수 토지물건 포함(기본은 제외 - 상가만)
```
출력: 이용상태검색=`results_상가_이용상태.html`, NPL검색=`results_NPL_상가.html` (분리 저장).

### 채권자/NPL 원리 (중요)
대법원은 **채권자 이름을 마스킹**함(송○○, 주○○○). 그래서 유동화/대부/농협 이름 직접필터는 불가.
대신 **문건/송달내역의 "승계인·압류채권자승계·채권자변경"** = 채권이 양도된 **NPL 물건** 신호로 감지.
원채권자(은행)명은 노출되는 경우 추출됨. `selectDlvrOfdocDtsDtl.on` 사용. `captured2/`가 캡처 원본.

## 동작 구조
1. `capture.py` — 최초 1회, 브라우저로 실제 검색 요청을 캡처 (이미 완료, `captured/`)
2. `fetch.py` — 캡처한 요청형식으로 headless 자동수집
   - 리스트검색: `pgjsearch/searchControllerMain.on` (40건씩 페이징)
   - 물건상세: `pgj15B/selectAuctnCsSrchRslt.on` (사건당 1회, 이용상태 텍스트)
   - 이용상태 = 감정평가 요항표 항목 `00083006(이용상태)/00083015(건물)/00083009(토지)/00083026(임대)` + 물건비고
   - `captured/detail_cache.json` 캐시 → 키워드만 바꿔 재실행시 즉시(약 12초)

## 한계 / 주의
- 현재 검색범위 = **경기도 / 건물>상업용및업무용>근린생활시설** (캡처된 조건). 다른 시도·용도는 재캡처 또는 코드수정 필요.
- 이용상태는 집행관 현황조사·감정평가의 **비표준 텍스트**라 일부 누락 가능(예: 스터디카페가 '사무실'로만 기재).
- 비공식 내부 API라 대법원 사이트 개편 시 `capture.py` 재실행으로 요청형식 갱신 필요.
- 개인 투자조사·내부 분석용. 데이터 재판매/외부공개는 저작권·약관 이슈.
