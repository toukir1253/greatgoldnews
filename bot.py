"""Gold signal bot V2: M15 entry, H1 trend, filters and outcome tracking."""
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd, requests, yfinance as yf

SYMBOL="GC=F"  # Yahoo gold futures proxy, not spot XAU/USD
INTERVAL="15m"; HTF="1h"; MAX_SIGNALS=5; MIN_GAP=60
SL_ATR=1.5; TP_ATR=3.0; MIN_SCORE=70
CAL_URL="https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE=Path("state.json"); TOKEN=os.getenv("TELEGRAM_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
UTC=timezone.utc; BD=timezone(timedelta(hours=6))

def send(msg):
    if not TOKEN or not CHAT: raise RuntimeError("Missing Telegram secrets")
    r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":CHAT,"text":msg,"disable_web_page_preview":True},timeout=25)
    r.raise_for_status()
    if not r.json().get("ok"): raise RuntimeError("Telegram API rejected message")

def load():
    try: s=json.loads(STATE.read_text())
    except Exception: s={}
    return {"date":s.get("date",""),"count":int(s.get("count",0)),"last_ts":s.get("last_ts",""),"last_candle":s.get("last_candle",""),"open":s.get("open",[]),"history":s.get("history",[]),"summary":s.get("summary","")}
def save(s): STATE.write_text(json.dumps(s,ensure_ascii=False,indent=2))

def data(interval,period):
    d=yf.download(SYMBOL,period=period,interval=interval,progress=False,auto_adjust=False)
    if isinstance(d.columns,pd.MultiIndex): d.columns=d.columns.get_level_values(0)
    d=d.dropna().copy(); c=d["Close"]
    d["e20"]=c.ewm(span=20,adjust=False).mean(); d["e50"]=c.ewm(span=50,adjust=False).mean()
    delta=c.diff(); gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d["rsi"]=100-100/(1+gain/loss.replace(0,float("nan")))
    tr=pd.concat([d["High"]-d["Low"],(d["High"]-c.shift()).abs(),(d["Low"]-c.shift()).abs()],axis=1).max(axis=1)
    d["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()
    return d

def news_block(now):
    try: events=requests.get(CAL_URL,timeout=15,headers={"User-Agent":"Mozilla/5.0"}).json()
    except Exception as e:
        print("Calendar unavailable; trading filter skipped:",e); return False
    for e in events:
        if e.get("country")!="USD" or e.get("impact")!="High": continue
        try:
            t=datetime.fromisoformat(e["date"].replace("Z","+00:00"))
            if t.tzinfo is None: t=t.replace(tzinfo=UTC)
            mins=(t-now).total_seconds()/60
            if -15<=mins<=30: print("News blackout:",e.get("title")); return True
        except Exception: pass
    return False

def signal(m,h):
    if len(m)<60 or len(h)<60: return None
    a,b=m.iloc[-2],m.iloc[-3]; x=h.iloc[-2]; p=float(a.Close); atr=float(a.atr)
    if not pd.notna(atr) or atr<=0: return None
    up=x.e20>x.e50 and x.Close>x.e50 and a.e20>a.e50 and a.Close>a.e50
    dn=x.e20<x.e50 and x.Close<x.e50 and a.e20<a.e50 and a.Close<a.e50
    side="BUY" if up and b.Low<=b.e20 and a.Close>a.e20 and 45<=a.rsi<=65 else "SELL" if dn and b.High>=b.e20 and a.Close<a.e20 and 35<=a.rsi<=55 else None
    if not side: return None
    recent=m.iloc[-22:-2]
    room=float(recent.High.max()-p) if side=="BUY" else float(p-recent.Low.min())
    score=85 if room>=2*atr else 60
    if score<MIN_SCORE: return None
    sl,tp=(p-SL_ATR*atr,p+TP_ATR*atr) if side=="BUY" else (p+SL_ATR*atr,p-TP_ATR*atr)
    ts=a.name.isoformat() if hasattr(a.name,"isoformat") else str(a.name)
    return {"side":side,"entry":p,"sl":sl,"tp":tp,"rsi":float(a.rsi),"score":score,"candle":ts,"time":datetime.now(UTC).isoformat(),"checked":""}

def outcomes(s,m):
    if len(m)<2:return
    b=m.iloc[-2]; stamp=b.name.isoformat() if hasattr(b.name,"isoformat") else str(b.name); keep=[]
    for t in s["open"]:
        if t.get("checked")==stamp: keep.append(t); continue
        hi,lo=float(b.High),float(b.Low)
        sl,tp=(lo<=t["sl"],hi>=t["tp"]) if t["side"]=="BUY" else (hi>=t["sl"],lo<=t["tp"])
        result="AMBIGUOUS" if sl and tp else "LOSS" if sl else "WIN" if tp else ""
        if result: t["result"]=result; t["closed"]=stamp; s["history"].append(t)
        else: t["checked"]=stamp; keep.append(t)
    s["open"]=keep; s["history"]=s["history"][-200:]

def main():
    now=datetime.now(UTC); bd=now.astimezone(BD); today=bd.strftime("%Y-%m-%d"); s=load()
    if s["date"]!=today:s["date"],s["count"]=today,0
    m=data("15m","5d"); h=data("1h","1mo")
    if len(m)<60 or len(h)<60: raise RuntimeError("Insufficient market data")
    outcomes(s,m)
    if bd.weekday()<5 and bd.hour==23 and bd.minute>=50 and s["summary"]!=today:
        hist=[t for t in s["history"] if t.get("time","").startswith(today)]
        send(f"📊 GOLD দৈনিক রিপোর্ট {today}\nSignals: {s['count']}/{MAX_SIGNALS}\nClosed: {len(hist)} | WIN: {sum(t.get('result')=='WIN' for t in hist)} | LOSS: {sum(t.get('result')=='LOSS' for t in hist)} | Ambiguous: {sum(t.get('result')=='AMBIGUOUS' for t in hist)}\nOpen: {len(s['open'])}\nএটি কেবল candle-based আনুমানিক ফলাফল, broker execution নয়।")
        s["summary"]=today
    if bd.weekday()>=5 or not 5<=bd.hour<24 or s["count"]>=MAX_SIGNALS: save(s); return
    if s["last_ts"] and (now-datetime.fromisoformat(s["last_ts"])).total_seconds()<MIN_GAP*60: save(s); return
    sig=signal(m,h)
    if not sig: save(s); print("No qualifying setup."); return
    if sig["candle"]==s["last_candle"]: save(s); print("Duplicate candle skipped."); return
    if news_block(now): save(s); return
    n=s["count"]+1
    msg=(f"🥇 GOLD FUTURES PROXY {sig['side']} | V2\nEntry: {sig['entry']:.2f}\nSL: {sig['sl']:.2f}\nTP: {sig['tp']:.2f}\nQuality filter score: {sig['score']}/100\nRSI: {sig['rsi']:.1f} | M15 + H1 confirmation\nSignal {n}/{MAX_SIGNALS} (BD time)\n\nদাম Yahoo GC=F futures proxy, spot XAU/USD নয়। স্কোর জয়ের সম্ভাবনা নয়। Demo test ছাড়া real trade নয়।")
    send(msg) # increment only after Telegram confirms
    s["count"]=n; s["last_ts"]=now.isoformat(); s["last_candle"]=sig["candle"]; s["open"].append(sig); save(s); print(msg)

if __name__=="__main__": main()
