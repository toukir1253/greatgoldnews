"""Gold news & status alerts -> Telegram (runs every 10 minutes).

1) Advance warning for high-impact USD economic events (60 min before)
2) "Released now" alert at event time
3) Fresh gold headlines (Google News RSS)
4) Gold status (price, daily change, trend, RSI)
"""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from bot import INTERVAL, SYMBOL, indicators, send_telegram

# ---------- Settings ----------
IMPACTS = {"High"}            # add "Medium" for more events
CURRENCIES = {"USD"}          # USD news moves gold the most
WARN_MINUTES = 60             # advance warning time
STATUS_EVERY_MIN = 30         # status when there is no news (set 10 for every run)
MAX_HEADLINES = 3
STATE_FILE = Path("news_state.json")
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_URL = (
    "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+OR+%22gold+futures%22"
    "+when:1d&hl=en-US&gl=US&ceid=US:en"
)
# ------------------------------

BN_TZ = timezone(timedelta(hours=6))


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"pre": [], "now": [], "seen": [], "last_status": ""}


def bd_time(dt):
    return dt.astimezone(BN_TZ).strftime("%d %b, %I:%M %p")


def calendar_alerts(state, now):
    out = []
    try:
        events = requests.get(CAL_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).json()
    except Exception as e:
        print("calendar error:", e)
        return out
    for ev in events:
        if ev.get("country") not in CURRENCIES or ev.get("impact") not in IMPACTS:
            continue
        try:
            when = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        eid = hashlib.md5((ev["title"] + ev["date"]).encode()).hexdigest()[:12]
        mins = (when - now).total_seconds() / 60
        info = f"{ev['country']} {ev['title']}\nসময়: {bd_time(when)} (বাংলাদেশ)\n" \
               f"পূর্বাভাস: {ev.get('forecast') or '-'} | আগের: {ev.get('previous') or '-'}"
        if 0 < mins <= WARN_MINUTES and eid not in state["pre"]:
            state["pre"].append(eid)
            out.append(f"⏰ গুরুত্বপূর্ণ নিউজ আসছে (~{int(mins)} মিনিট পর)\n{info}\n"
                       f"⚠️ দাম হঠাৎ লাফাতে পারে। নতুন ট্রেড এড়ানো বা স্টপ লস চেক করা ভালো।")
        elif -15 <= mins <= 0 and eid not in state["now"]:
            state["now"].append(eid)
            if eid not in state["pre"]:
                state["pre"].append(eid)
            out.append(f"🔔 এইমাত্র নিউজ প্রকাশিত\n{info}\n"
                       f"চার্টে দামের প্রতিক্রিয়া দেখুন, স্প্রেড বেড়ে যেতে পারে।")
    return out


def headline_alerts(state, now):
    out = []
    try:
        r = requests.get(NEWS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        items = ET.fromstring(r.content).findall("./channel/item")
    except Exception as e:
        print("news error:", e)
        return out
    first_run = not state["seen"]
    fresh = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        hid = hashlib.md5(title.encode()).hexdigest()[:12]
        if hid in state["seen"]:
            continue
        state["seen"].append(hid)
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            pub = now
        if not first_run and (now - pub) <= timedelta(minutes=45):
            fresh.append((title, link))
    state["seen"] = state["seen"][-300:]
    for title, link in fresh[:MAX_HEADLINES]:
        out.append(f"📰 {title}\n{link}")
    return out


def gold_status():
    df = yf.download(SYMBOL, period="5d", interval=INTERVAL, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = indicators(df.dropna())
    d = yf.download(SYMBOL, period="5d", interval="1d", progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    price = float(df["Close"].iloc[-1])
    prev_close = float(d["Close"].dropna().iloc[-2])
    chg = (price / prev_close - 1) * 100
    last = df.iloc[-1]
    trend = "ঊর্ধ্বমুখী 📈" if last["ema20"] > last["ema50"] else "নিম্নমুখী 📉"
    rsi = float(last["rsi"])
    note = "ওভারবট (নামতে পারে)" if rsi > 70 else "ওভারসোল্ড (উঠতে পারে)" if rsi < 30 else "স্বাভাবিক"
    return (f"🥇 গোল্ডের বর্তমান অবস্থা\nদাম: {price:.2f} ({chg:+.2f}% গতদিনের তুলনায়)\n"
            f"ট্রেন্ড ({INTERVAL}): {trend}\nRSI: {rsi:.0f} - {note}")


def main():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        print("Weekend.")
        return
    state = load_state()
    parts = calendar_alerts(state, now) + headline_alerts(state, now)

    due = True
    if state["last_status"]:
        due = (now - datetime.fromisoformat(state["last_status"])).total_seconds() >= STATUS_EVERY_MIN * 60 - 60
    if parts or due:
        try:
            parts.append(gold_status())
            state["last_status"] = now.isoformat()
        except Exception as e:
            print("status error:", e)

    STATE_FILE.write_text(json.dumps(state))
    if parts:
        send_telegram("\n\n".join(parts))
    print("\n\n".join(parts) or "Nothing to send.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Error:", e)
        sys.exit(1)
