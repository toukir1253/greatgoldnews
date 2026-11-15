"""Gold (XAU/USD) signal bot -> Telegram.

Max 3 signals per UTC day, only inside the chosen trading session.
Strategy: EMA20/EMA50 trend + pullback to EMA20 + RSI filter, ATR-based SL/TP.
This is an alert tool, NOT financial advice and NOT a guarantee of profit.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ---------- Settings (change these) ----------
SYMBOL = "GC=F"            # Gold futures (free data). XAU/USD spot is very close.
INTERVAL = "15m"
SESSION_START_UTC = 7      # London open ~07:00 UTC
SESSION_END_UTC = 17       # NY session end ~17:00 UTC
MAX_SIGNALS_PER_DAY = 3
MIN_GAP_MINUTES = 60       # minimum gap between two signals
SL_ATR = 1.5               # stop loss = 1.5 x ATR
TP_ATR = 3.0               # take profit = 3.0 x ATR (risk:reward 1:2)
STATE_FILE = Path("state.json")
# ---------------------------------------------

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "count": 0, "last_ts": ""}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set. Message was:\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    r.raise_for_status()


def indicators(df):
    c = df["Close"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - c.shift()).abs(),
            (df["Low"] - c.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def find_signal(df):
    """Use the last CLOSED candle (index -2); -1 is still forming."""
    a, b = df.iloc[-2], df.iloc[-3]
    price = float(a["Close"])
    atr = float(a["atr"])

    up = a["ema20"] > a["ema50"] and a["Close"] > a["ema50"]
    down = a["ema20"] < a["ema50"] and a["Close"] < a["ema50"]

    if up and b["Low"] <= b["ema20"] and a["Close"] > a["ema20"] and 45 <= a["rsi"] <= 65:
        return "BUY", price, price - SL_ATR * atr, price + TP_ATR * atr, a
    if down and b["High"] >= b["ema20"] and a["Close"] < a["ema20"] and 35 <= a["rsi"] <= 55:
        return "SELL", price, price + SL_ATR * atr, price - TP_ATR * atr, a
    return None


def main():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        print("Weekend, market closed.")
        return
    if not (SESSION_START_UTC <= now.hour < SESSION_END_UTC):
        print("Outside session.")
        return

    state = load_state()
    today = now.strftime("%Y-%m-%d")
    if state["date"] != today:
        state = {"date": today, "count": 0, "last_ts": ""}
    if state["count"] >= MAX_SIGNALS_PER_DAY:
        print("Daily limit reached.")
        return
    if state["last_ts"]:
        last = datetime.fromisoformat(state["last_ts"])
        if (now - last).total_seconds() < MIN_GAP_MINUTES * 60:
            print("Too soon after last signal.")
            return

    df = yf.download(SYMBOL, period="5d", interval=INTERVAL, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    if len(df) < 60:
        print("Not enough data.")
        return

    df = indicators(df)
    sig = find_signal(df)
    if not sig:
        print("No signal.")
        return

    side, entry, sl, tp, row = sig
    state["count"] += 1
    state["last_ts"] = now.isoformat()
    save_state(state)

    msg = (
        f"🥇 GOLD (XAU/USD) {side}\n"
        f"Entry: {entry:.2f}\n"
        f"Stop Loss: {sl:.2f}\n"
        f"Take Profit: {tp:.2f}\n"
        f"RSI: {row['rsi']:.0f} | Timeframe: {INTERVAL}\n"
        f"Signal {state['count']}/{MAX_SIGNALS_PER_DAY} today\n\n"
        f"⚠️ কোনো সিগন্যালই নিশ্চিত নয়। ঝুঁকি নিয়ন্ত্রণে রাখুন: প্রতি ট্রেডে ব্যালেন্সের ১-২% এর বেশি নয়।"
    )
    send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Error:", e)
        sys.exit(1)
