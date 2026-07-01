"""경매 상가 일일보고 → 슬랙 발송
webhook은 slack_config.json({"SLACK_WEBHOOK_URL":"https://hooks.slack.com/..."}) 또는 환경변수에서 읽음.
사용: python3 slack_post.py
"""
import json, os, urllib.request, datetime

BASE = os.path.dirname(os.path.abspath(__file__))

def get_webhook():
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        return url
    cfg = os.path.join(BASE, "slack_config.json")
    if os.path.exists(cfg):
        try:
            return (json.load(open(cfg)).get("SLACK_WEBHOOK_URL") or "").strip()
        except Exception:
            return ""
    return ""

def load(name):
    p = os.path.join(BASE, name)
    return json.load(open(p)) if os.path.exists(p) else None

def top_lines(s, n=6):
    out = []
    for t in (s.get("top") or [])[:n]:
        npl = " 🔴NPL" + ("(" + "·".join(t["types"]) + ")" if t.get("types") else "") if t.get("npl") else ""
        nm = " " + "/".join(t["names"][:1]) if t.get("names") else ""
        out.append(f"• `{t['csno']}` {t['court']} · {t['addr']}\n   감정 {t['gam']} → 최저 {t['low']} (*{t['drate']}%↓* · 유찰{t['yuchal']}회 · {t['giil']}){npl}{nm}")
    return "\n".join(out) or "_해당 없음_"

def build_blocks(iy, npl, today):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"🏢 오늘의 경매 상가 리포트  ({today})", "emoji": True}}]
    if iy:
        kw = " · ".join(f"{k} {v}" for k, v in list(iy["by_kw"].items())[:6])
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*📊 이용상태 매칭 {iy['count']}건* · 평균 할인율 {iy['avg_drate']}%\n{kw}"}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": top_lines(iy)}})
    if npl:
        ty = " · ".join(f"{k} {v}" for k, v in npl.get("types", {}).items()) or "유형 미상(마스킹)"
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔴 NPL 부실채권 {npl['count']}건*  (유동화·대부·자산관리)\n{ty}"}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": top_lines(npl)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "법원경매 원천데이터(대법원) 직수집 · 매일 자동 · 개인 투자조사용"}]})
    return blocks

def main():
    hook = get_webhook()
    iy = load("summary_이용상태.json")
    npl = load("summary_NPL.json")
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d (%a)")
    if not (iy or npl):
        print("요약 JSON 없음 — fetch.py 먼저 실행 필요")
        return 1
    blocks = build_blocks(iy, npl, today)
    payload = {"text": f"오늘의 경매 상가 리포트 ({today})", "blocks": blocks}
    if not hook:
        print("⚠ 슬랙 웹훅 미설정 — slack_config.json에 SLACK_WEBHOOK_URL 넣으면 발송됩니다.")
        print("메시지 미리보기:\n" + json.dumps(payload, ensure_ascii=False)[:500])
        return 2
    data = json.dumps(payload).encode()
    req = urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        print(f"✅ 슬랙 발송 완료 (HTTP {r.status})")
        return 0
    except Exception as e:
        print(f"❌ 슬랙 발송 실패: {e}")
        return 3

if __name__ == "__main__":
    raise SystemExit(main())
