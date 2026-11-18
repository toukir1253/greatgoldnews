"""Gold signal bot V2.1 — M15 entries with H1 confirmation and conservative tracking."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

SYMBOL = "GC=F"  # Yahoo gold-futures proxy; not broker spot XAU/USD
MAX_SIGNALS = 5
MIN_GAP_MINUTES = 45
MIN_SCORE = 75
SL_ATR_BUFFER = 0.20
REWARD_RISK = 2.0
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE = Path("state.json")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
UTC = timezone.utc
BD = timezone(timedelta(hours=6))


def send(message):
    if not TOKEN or not CHAT:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secret")
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": message, "disable_web_page_preview": True},
        timeout=25,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def load_state():
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {
        "date": raw.get("date", ""),
        "count": int(raw.get("count", 0)),
        "last_ts": raw.get("last_ts", ""),
        "last_candle": raw.get("last_candle", ""),
        "open": raw.get("open", []),
        "history": raw.get("history", []),
        "summary": raw.get("summary", ""),
    }


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def market_data(interval, period):
    frame = yf.download(
        SYMBOL, period=period, interval=interval, progress=False,
        auto_adjust=False, threads=False
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"No market data returned for {interval}")
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.dropna().copy()
    close = frame["Close"]
    frame["ema20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    frame["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    previous_close = close.shift()
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    return frame


def high_impact_news_blackout(now):
    """Fail closed if the calendar is unavailable; avoid major USD-news windows."""
    try:
        response = requests.get(
            CAL_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        events = response.json()
    except Exception as exc:
        print(f"Calendar unavailable; signal blocked for safety: {exc}")
        return True

    for event in events:
        if event.get("country") != "USD" or event.get("impact") != "High":
            continue
        try:
            event_time = datetime.fromisoformat(
                event["date"].replace("Z", "+00:00")
            )
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            minutes = (event_time - now).total_seconds() / 60
            if -15 <= minutes <= 30:
                print("High-impact news blackout:", event.get("title", "USD event"))
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def build_signal(m15, h1):
    """Use only completed candles; require trend, pullback, momentum and room."""
    if len(m15) < 60 or len(h1) < 60:
        return None

    # Yahoo's newest candle can still be forming; use the previous closed candle.
    current = m15.iloc[-2]
    previous = m15.iloc[-3]
    higher = h1.iloc[-2]
    price = float(current["Close"])
    atr = float(current["atr"])
    rsi = float(current["rsi"])
    if not pd.notna(atr) or atr <= 0 or not pd.notna(rsi):
        return None

    recent = m15.iloc[-22:-2]
    resistance = recent.loc[recent["High"] > price, "High"]
    support = recent.loc[recent["Low"] < price, "Low"]
    buy_room = float(resistance.min() - price) if not resistance.empty else float("inf")
    sell_room = float(price - support.max()) if not support.empty else float("inf")

    buy_trend = (
        higher["ema20"] > higher["ema50"]
        and higher["Close"] > higher["ema20"]
        and current["ema20"] > current["ema50"]
        and price > current["ema20"]
    )
    sell_trend = (
        higher["ema20"] < higher["ema50"]
        and higher["Close"] < higher["ema20"]
        and current["ema20"] < current["ema50"]
        and price < current["ema20"]
    )

    pullback_buy = float(previous["Low"]) <= float(previous["ema20"]) + 0.25 * atr
    pullback_sell = float(previous["High"]) >= float(previous["ema20"]) - 0.25 * atr
    bullish_confirmation = (
        price > float(current["Open"])
        and price > float(previous["High"])
    )
    bearish_confirmation = (
        price < float(current["Open"])
        and price < float(previous["Low"])
    )

    side = None
    if buy_trend and pullback_buy and bullish_confirmation and 50 <= rsi <= 68 and buy_room >= 2 * atr:
        side = "BUY"
    elif sell_trend and pullback_sell and bearish_confirmation and 32 <= rsi <= 50 and sell_room >= 2 * atr:
        side = "SELL"
    if side is None:
        return None

    # Transparent checklist score; this is a setup-quality filter, not win probability.
    score = 25 + 20 + 15 + 20 + 20
    if score < MIN_SCORE:
        return None

    if side == "BUY":
        stop = min(float(current["Low"]), float(previous["Low"])) - SL_ATR_BUFFER * atr
        risk = price - stop
        target = price + REWARD_RISK * risk
    else:
        stop = max(float(current["High"]), float(previous["High"])) + SL_ATR_BUFFER * atr
        risk = stop - price
        target = price - REWARD_RISK * risk

    if risk < 0.6 * atr or risk > 2.5 * atr:
        return None

    candle_time = current.name.isoformat() if hasattr(current.name, "isoformat") else str(current.name)
    return {
        "side": side,
        "entry": price,
        "sl": stop,
        "tp": target,
        "rsi": rsi,
        "score": score,
        "risk": risk,
        "candle": candle_time,
        "time": datetime.now(UTC).isoformat(),
    }


def update_outcomes(state, m15):
    """Evaluate each completed post-entry candle; same-bar SL/TP is ambiguous."""
    still_open = []
    for trade in state["open"]:
        try:
            entry_time = pd.Timestamp(trade["candle"])
            if m15.index.tz is not None and entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize(m15.index.tz)
            elif m15.index.tz is None and entry_time.tzinfo is not None:
                entry_time = entry_time.tz_convert(None)
            later = m15.loc[m15.index > entry_time]
        except Exception as exc:
            print("Could not evaluate stored trade:", exc)
            still_open.append(trade)
            continue

        result = None
        close_time = None
        # Exclude the newest possibly-forming M15 candle.
        for candle_time, bar in later.iloc[:-1].iterrows():
            high, low = float(bar["High"]), float(bar["Low"])
            if trade["side"] == "BUY":
                stop_hit = low <= float(trade["sl"])
                target_hit = high >= float(trade["tp"])
            else:
                stop_hit = high >= float(trade["sl"])
                target_hit = low <= float(trade["tp"])

            if stop_hit and target_hit:
                result = "AMBIGUOUS"
            elif stop_hit:
                result = "LOSS"
            elif target_hit:
                result = "WIN"
            if result:
                close_time = candle_time.isoformat() if hasattr(candle_time, "isoformat") else str(candle_time)
                break

        if result:
            trade["result"] = result
            trade["closed"] = close_time
            state["history"].append(trade)
        else:
            still_open.append(trade)

    state["open"] = still_open
    state["history"] = state["history"][-300:]


def is_today_bd(iso_time, today):
    try:
        value = datetime.fromisoformat(iso_time)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(BD).strftime("%Y-%m-%d") == today
    except (TypeError, ValueError):
        return False


def send_daily_report(state, today):
    trades = [t for t in state["history"] if is_today_bd(t.get("time", ""), today)]
    wins = sum(t.get("result") == "WIN" for t in trades)
    losses = sum(t.get("result") == "LOSS" for t in trades)
    ambiguous = sum(t.get("result") == "AMBIGUOUS" for t in trades)
    message = (
        f"📊 GOLD দৈনিক রিপোর্ট — {today}\n"
        f"সিগন্যাল: {state['count']}/{MAX_SIGNALS}\n"
        f"বন্ধ ট্রেড: {len(trades)} | WIN: {wins} | LOSS: {losses} | Ambiguous: {ambiguous}\n"
        f"চলমান: {len(state['open'])}\n"
        "ফলাফল candle-based আনুমানিক হিসাব; broker execution, spread বা slippage অন্তর্ভুক্ত নয়।"
    )
    send(message)


def main():
    now = datetime.now(UTC)
    bd_now = now.astimezone(BD)
    today = bd_now.strftime("%Y-%m-%d")
    state = load_state()

    if state["date"] != today:
        state["date"] = today
        state["count"] = 0

    m15 = market_data("15m", "5d")
    h1 = market_data("1h", "1mo")
    update_outcomes(state, m15)

    # One daily report, close to midnight Bangladesh time.
    if bd_now.weekday() < 5 and bd_now.hour == 23 and bd_now.minute >= 50 and state["summary"] != today:
        send_daily_report(state, today)
        state["summary"] = today

    # Gold market is normally closed on weekends; schedule covers weekdays.
    if bd_now.weekday() >= 5 or not 5 <= bd_now.hour < 24:
        save_state(state)
        return
    if state["count"] >= MAX_SIGNALS:
        save_state(state)
        return
    if state["last_ts"]:
        last_time = datetime.fromisoformat(state["last_ts"])
        if (now - last_time).total_seconds() < MIN_GAP_MINUTES * 60:
            save_state(state)
            return

    candidate = build_signal(m15, h1)
    if not candidate:
        save_state(state)
        print("No qualifying setup.")
        return
    if candidate["candle"] == state["last_candle"]:
        save_state(state)
        print("Duplicate M15 candle skipped.")
        return
    if high_impact_news_blackout(now):
        save_state(state)
        return

    number = state["count"] + 1
    message = (
        f"🥇 GOLD {candidate['side']} — V2.1\n"
        f"Entry: {candidate['entry']:.2f}\n"
        f"Stop Loss: {candidate['sl']:.2f}\n"
        f"Take Profit: {candidate['tp']:.2f}\n"
        f"Risk/Reward: 1:{REWARD_RISK:.1f}\n"
        f"Setup score: {candidate['score']}/100 | RSI: {candidate['rsi']:.1f}\n"
        f"টাইমফ্রেম: M15 entry + H1 trend\n"
        f"আজকের সিগন্যাল: {number}/{MAX_SIGNALS} | বাংলাদেশ সময়\n\n"
        "ডেটা: Yahoo GC=F gold-futures proxy, spot XAU/USD নয়। "
        "স্কোর জয়ের সম্ভাবনা নয়; আগে demo-তে যাচাই করো।"
    )
    send(message)  # Persist count only after Telegram confirms delivery.
    state["count"] = number
    state["last_ts"] = now.isoformat()
    state["last_candle"] = candidate["candle"]
    state["open"].append(candidate)
    save_state(state)
    print(message)


if __name__ == "__main__":
    main()
