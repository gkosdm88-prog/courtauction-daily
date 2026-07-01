"""경매 상가 일일보고 → 텔레그램 발송
telegram_config.json({"BOT_TOKEN":"...","CHAT_ID":"..."}) 에서 읽음.
CHAT_ID가 비어있으면 getUpdates로 자동 탐지(봇에게 아무 메시지나 한 번 보낸 뒤 실행).
"""
import json, os, urllib.request, urllib.parse, datetime, html as H

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "telegram_config.json")
REPORT_URL = "https://gkosdm88-prog.github.io/courtauction-daily/"  # Pages 링크(폰에서 열림)

def cfg():
    d = {}
    if os.path.exists(CFG):
        try: d = json.load(open(CFG))
        except Exception: d = {}
    d["BOT_TOKEN"] = os.environ.get("TG_BOT_TOKEN", d.get("BOT_TOKEN", "")).strip()
    d["CHAT_ID"] = os.environ.get("TG_CHAT_ID", d.get("CHAT_ID", "")).strip()
    return d

def save_cfg(d):
    json.dump(d, open(CFG, "w"), ensure_ascii=False, indent=2)

def api(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    return json.load(urllib.request.urlopen(req, timeout=20))

def detect_chat_id(token):
    try:
        r = api(token, "getUpdates", {})
        for u in reversed(r.get("result", [])):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
    except Exception as e:
        print("getUpdates 실패:", e)
    return ""

def load(name):
    p = os.path.join(BASE, name)
    return json.load(open(p)) if os.path.exists(p) else None

def top_lines(s, n=6):
    out = []
    for t in (s.get("top") or [])[:n]:
        npl = " 🔴NPL" + ("(" + "·".join(t["types"]) + ")" if t.get("types") else "") if t.get("npl") else ""
        nm = " " + H.escape("/".join(t["names"][:1])) if t.get("names") else ""
        out.append(f"• <b>{H.escape(t['csno'])}</b> {H.escape(t['court'])} · {H.escape(t['addr'])}\n"
                   f"   감정 {t['gam']} → 최저 {t['low']} (<b>{t['drate']}%↓</b> · 유찰{t['yuchal']}회 · {t['giil']}){npl}{nm}")
    return "\n".join(out) or "-"

def build_text(iy, npl, today):
    p = [f"🏢 <b>오늘의 경매 상가 리포트</b>  ({today})", "━━━━━━━━━━━━━━"]
    if iy:
        kw = " · ".join(f"{H.escape(k)} {v}" for k, v in list(iy["by_kw"].items())[:6])
        p += [f"📊 <b>이용상태 매칭 {iy['count']}건</b> · 평균 할인율 {iy['avg_drate']}%", kw, top_lines(iy), ""]
    if npl:
        ty = " · ".join(f"{H.escape(k)} {v}" for k, v in npl.get("types", {}).items()) or "유형 미상(마스킹)"
        p += ["━━━━━━━━━━━━━━", f"🔴 <b>NPL 부실채권 {npl['count']}건</b> (유동화·대부·자산관리)", ty, top_lines(npl)]
    p.append(f'\n📱 <a href="{REPORT_URL}">전체 리포트 보기(검색·정렬)</a>')
    p.append("<i>법원경매 원천데이터 직수집 · 매일 자동</i>")
    return "\n".join(p)

def main():
    d = cfg()
    if not d["BOT_TOKEN"]:
        print("⚠ BOT_TOKEN 없음 — telegram_config.json에 봇 토큰 넣으세요.")
        return 2
    if not d["CHAT_ID"]:
        d["CHAT_ID"] = detect_chat_id(d["BOT_TOKEN"])
        if d["CHAT_ID"]:
            save_cfg(d); print("✅ CHAT_ID 자동 탐지:", d["CHAT_ID"])
        else:
            print("⚠ CHAT_ID 못 찾음 — 텔레그램에서 봇에게 아무 메시지나 한 번 보낸 뒤 다시 실행하세요.")
            return 2
    iy, npl = load("summary_이용상태.json"), load("summary_NPL.json")
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d (%a)")
    if not (iy or npl):
        print("요약 JSON 없음 — fetch.py 먼저 실행"); return 1
    text = build_text(iy, npl, today)
    try:
        r = api(d["BOT_TOKEN"], "sendMessage",
                {"chat_id": d["CHAT_ID"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        print("✅ 텔레그램 발송 완료" if r.get("ok") else f"❌ 실패: {r}")
        return 0 if r.get("ok") else 3
    except Exception as e:
        print("❌ 발송 실패:", e); return 3

if __name__ == "__main__":
    raise SystemExit(main())
