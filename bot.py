"""Gold signal bot V2.4 — V2.3 plus loosened filter: wider RSI, lower score, wider drift/pullback."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

SYMBOL = "GC=F"  # Yahoo gold-futures proxy; not broker spot XAU/USD
MAX_SIGNALS = 5
MIN_GAP_MINUTES = 45
MIN_SCORE = 80  # V2.4 loosened from 85: accept slightly weaker qualifying setups
SL_ATR_BUFFER = 0.20
REWARD_RISK = 2.0
LIVE_MAX_DRIFT_ATR = 0.50  # V2.4 loosened from 0.25: live price may drift up to 0.50 ATR from candle close
LIVE_QUOTE_MAX_AGE_SECONDS = 180
TRADE_EXPIRY_BARS = 96
LOOKBACK_CANDLES = 4  # V2.3: also check the last 4 closed M15 candles (~60 min)
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"]
CALENDAR_CACHE = Path("calendar_cache.json")
CALENDAR_MAX_AGE_HOURS = 36
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
        "seen_candles": raw.get("seen_candles", []),  # V2.3: candles already alerted
        "open": raw.get("open", []),
        "history": raw.get("history", []),
        "summary": raw.get("summary", ""),
        "pending_report": raw.get("pending_report", None),
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
    """Try primary/backup feeds, then recent cached calendar; outage won't block whole day."""
    events, source, errors = None, "live", []
    for url in CAL_URLS:
        try:
            response = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("Calendar response is not a list")
            CALENDAR_CACHE.write_text(json.dumps({"fetched_at": now.isoformat(), "events": events}, ensure_ascii=False), encoding="utf-8")
            break
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if events is None:
        source = "cache"
        try:
            cached = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            age = (now - fetched.astimezone(UTC)).total_seconds()
            if 0 <= age <= CALENDAR_MAX_AGE_HOURS * 3600 and isinstance(cached.get("events"), list):
                events = cached["events"]
                print(f"Using cached calendar ({age / 3600:.1f}h old)")
        except Exception as exc:
            errors.append(f"cache: {exc}")
    if events is None:
        print("Calendar unavailable; fail-closed: signal blocked.")
        print("Calendar errors:", " | ".join(errors))
        return True, "unavailable"
    for event in events:
        if event.get("country") != "USD" or event.get("impact") != "High":
            continue
        try:
            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            minutes = (event_time - now).total_seconds() / 60
            if -15 <= minutes <= 30:
                print("High-impact news blackout:", event.get("title", "USD event"))
                return True, source
        except (KeyError, TypeError, ValueError):
            continue
    return False, source


def build_signal(m15, h1, offset=0):
    """Check one COMPLETED M15 candle. offset=0 is the newest closed candle,
    offset=1 the one before it, and so on (V2.3 lookback).
    Returns (candidate_or_None, reason_text)."""
    if len(m15) < 60 + offset or len(h1) < 60:
        return None, "not enough data"

    # Yahoo's newest candle can still be forming; use the previous closed candle.
    current = m15.iloc[-2 - offset]
    previous = m15.iloc[-3 - offset]
    if offset == 0:
        higher = h1.iloc[-2]
    else:
        # last H1 candle that was already closed when this M15 candle closed
        cutoff = current.name + pd.Timedelta(minutes=15) - pd.Timedelta(hours=1)
        eligible = h1.loc[h1.index <= cutoff]
        if eligible.empty:
            return None, "no H1 data for that time"
        higher = eligible.iloc[-1]
    price = float(current["Close"])
    atr = float(current["atr"])
    rsi = float(current["rsi"])
    if not pd.notna(atr) or atr <= 0 or not pd.notna(rsi):
        return None, "indicator not ready"

    recent = m15.iloc[-22 - offset:-2 - offset]
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

    pullback_buy = float(previous["Low"]) <= float(previous["ema20"]) + 0.35 * atr  # V2.4 loosened from 0.25
    pullback_sell = float(previous["High"]) >= float(previous["ema20"]) - 0.35 * atr  # V2.4 loosened from 0.25
    bullish_confirmation = (
        price > float(current["Open"])
        and price > float(previous["High"])
    )
    bearish_confirmation = (
        price < float(current["Open"])
        and price < float(previous["Low"])
    )

    side = None
    if buy_trend and pullback_buy and bullish_confirmation and 48 <= rsi <= 70 and buy_room >= 2 * atr:  # V2.4: was 50-68
        side = "BUY"
    elif sell_trend and pullback_sell and bearish_confirmation and 30 <= rsi <= 52 and sell_room >= 2 * atr:  # V2.4: was 32-50
        side = "SELL"
    if side is None:
        if buy_trend:
            return None, "uptrend but pullback/confirmation/RSI/room not met"
        if sell_trend:
            return None, "downtrend but pullback/confirmation/RSI/room not met"
        return None, "no aligned M15+H1 trend"

    # Setup-quality score (not a probability of winning).
    pullback_distance = abs(float(previous["Low"] if side == "BUY" else previous["High"]) - float(previous["ema20"]))
    pullback_points = 20 if pullback_distance <= 0.10 * atr else 15
    momentum_points = 15 if (52 <= rsi <= 66 if side == "BUY" else 34 <= rsi <= 48) else 10  # V2.4 widened to match new RSI range
    candle_body = abs(float(current["Close"]) - float(current["Open"]))
    candle_points = 20 if candle_body >= 0.50 * atr else 14
    available_room = buy_room if side == "BUY" else sell_room
    room_points = 20 if available_room >= 3.0 * atr else 15
    score = 25 + pullback_points + momentum_points + candle_points + room_points
    if score < MIN_SCORE:
        return None, f"{side} setup score {score} below {MIN_SCORE}"

    if side == "BUY":
        stop = min(float(current["Low"]), float(previous["Low"])) - SL_ATR_BUFFER * atr
        risk = price - stop
        target = price + REWARD_RISK * risk
    else:
        stop = max(float(current["High"]), float(previous["High"])) + SL_ATR_BUFFER * atr
        risk = stop - price
        target = price - REWARD_RISK * risk

    if risk < 0.6 * atr or risk > 2.5 * atr:
        return None, f"{side} stop distance {risk:.2f} outside 0.6-2.5 ATR"

    candle_time = current.name.isoformat() if hasattr(current.name, "isoformat") else str(current.name)
    return {
        "side": side,
        "entry": price,
        "sl": stop,
        "tp": target,
        "rsi": rsi,
        "score": score,
        "risk": risk,
        "atr": atr,
        "candle": candle_time,
        "time": datetime.now(UTC).isoformat(),
    }, "ok"


def validate_live_entry(candidate):
    """Fetch a fresh 1-minute quote and reject entries that have drifted."""
    quote = yf.download(SYMBOL, period="1d", interval="1m", progress=False, auto_adjust=False, threads=False)
    if quote is None or quote.empty:
        raise RuntimeError("No live quote; signal blocked")
    if isinstance(quote.columns, pd.MultiIndex):
        quote.columns = quote.columns.get_level_values(0)
    quote = quote.dropna(subset=["Close"])
    stamp = pd.Timestamp(quote.index[-1])
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    age = (pd.Timestamp.now(tz="UTC") - stamp).total_seconds()
    if age < -60 or age > LIVE_QUOTE_MAX_AGE_SECONDS:
        raise RuntimeError(f"Quote stale: {age:.0f}s")
    live = float(quote["Close"].iloc[-1])
    atr = float(candidate["atr"])
    drift = abs(live - float(candidate["entry"]))
    if drift > LIVE_MAX_DRIFT_ATR * atr:
        raise RuntimeError(f"Entry drift {drift:.2f} exceeds ATR limit")
    stop = float(candidate["sl"])
    risk = live - stop if candidate["side"] == "BUY" else stop - live
    if risk <= 0 or risk < 0.6 * atr or risk > 2.5 * atr:
        raise RuntimeError("Invalid stop distance at live price")
    candidate["entry"] = live
    candidate["risk"] = risk
    candidate["tp"] = live + REWARD_RISK*risk if candidate["side"] == "BUY" else live - REWARD_RISK*risk
    candidate["quote_time"] = stamp.isoformat()
    candidate["drift"] = drift
    return candidate


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

        completed_later = later.iloc[:-1]
        if not result and len(completed_later) >= TRADE_EXPIRY_BARS:
            result = "TIMEOUT"
            expiry_index = completed_later.index[TRADE_EXPIRY_BARS - 1]
            expiry_bar = completed_later.iloc[TRADE_EXPIRY_BARS - 1]
            close_time = expiry_index.isoformat() if hasattr(expiry_index, "isoformat") else str(expiry_index)
            trade["expiry_close"] = float(expiry_bar["Close"])
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


def send_daily_report(state, today, signal_count=None):
    trades = [t for t in state["history"] if is_today_bd(t.get("closed") or t.get("time", ""), today)]
    wins = sum(t.get("result") == "WIN" for t in trades)
    losses = sum(t.get("result") == "LOSS" for t in trades)
    ambiguous = sum(t.get("result") == "AMBIGUOUS" for t in trades)
    timeouts = sum(t.get("result") == "TIMEOUT" for t in trades)
    message = (
        f"📊 GOLD দৈনিক রিপোর্ট — {today}\n"
        f"সিগন্যাল: {state['count'] if signal_count is None else signal_count}/{MAX_SIGNALS}\n"
        f"বন্ধ ট্রেড: {len(trades)} | WIN: {wins} | LOSS: {losses} | Timeout: {timeouts} | Ambiguous: {ambiguous}\n"
        f"চলমান: {len(state['open'])}\n"
        "ফলাফল candle-based আনুমানিক হিসাব; broker execution, spread বা slippage অন্তর্ভুক্ত নয়।"
    )
    send(message)


def main(manual=False):
    now = datetime.now(UTC)
    bd_now = now.astimezone(BD)
    today = bd_now.strftime("%Y-%m-%d")
    state = load_state()

    if state["date"] != today:
        if state["date"] and state["summary"] != state["date"]:
            state["pending_report"] = {"date": state["date"], "count": state["count"]}
        state["date"] = today
        state["count"] = 0

    m15 = market_data("15m", "5d")
    h1 = market_data("1h", "1mo")
    update_outcomes(state, m15)

    # Manual /signal requests must not trigger the scheduled daily report.
    if not manual:
        pending = state.get("pending_report")
        if pending:
            send_daily_report(state, pending["date"], pending["count"])
            state["summary"] = pending["date"]
            state["pending_report"] = None
        elif bd_now.weekday() < 5 and bd_now.hour == 23 and bd_now.minute >= 50 and state["summary"] != today:
            send_daily_report(state, today, state["count"])
            state["summary"] = today

    # Gold market is normally closed on weekends; schedule covers weekdays.
    if bd_now.weekday() >= 5 or not 5 <= bd_now.hour < 24:
        reason = "সাপ্তাহিক ছুটি বা নির্ধারিত বাজার-সময়ের বাইরে। এখন সিগন্যাল দেওয়া হবে না।"
        print("Skipped: weekend or outside 05:00-24:00 Bangladesh time.")
        if manual:
            send("⏸️ /signal: " + reason)
        save_state(state)
        return
    if state["count"] >= MAX_SIGNALS:
        print(f"Skipped: daily limit {MAX_SIGNALS} reached.")
        if manual:
            send(f"⏸️ আজকের সর্বোচ্চ {MAX_SIGNALS}টি সিগন্যাল দেওয়া হয়ে গেছে।")
        save_state(state)
        return
    if state["last_ts"]:
        last_time = datetime.fromisoformat(state["last_ts"])
        waited = (now - last_time).total_seconds()
        if waited < MIN_GAP_MINUTES * 60:
            print(f"Skipped: minimum gap {MIN_GAP_MINUTES} min (only {waited / 60:.0f} min passed).")
            if manual:
                send(f"⏳ শেষ সিগন্যালের পর {MIN_GAP_MINUTES} মিনিট বিরতি রাখতে হবে। আরও {max(1, int((MIN_GAP_MINUTES * 60 - waited) / 60))} মিনিট পরে চেষ্টা করো।")
            save_state(state)
            return

    # V2.3: look at the newest closed candle first, then up to LOOKBACK_CANDLES back.
    seen = set(state["seen_candles"]) | ({state["last_candle"]} if state["last_candle"] else set())
    candidate, age_candles, reasons = None, 0, []
    for offset in range(LOOKBACK_CANDLES):
        found, reason = build_signal(m15, h1, offset)
        if not found:
            reasons.append(f"[-{offset}] {reason}")
            continue
        if found["candle"] in seen:
            reasons.append(f"[-{offset}] already alerted")
            continue
        candidate, age_candles = found, offset
        break
    if not candidate:
        save_state(state)
        print("No qualifying setup. " + " | ".join(reasons))
        if manual:
            send("🔎 এখন ভালো BUY/SELL সেটআপ নেই। নিরাপত্তার কারণে জোর করে কোনো সিগন্যাল দেওয়া হয়নি।")
        return

    blackout, calendar_source = high_impact_news_blackout(now)
    if blackout:
        print("Skipped: high-impact USD news blackout or calendar unavailable.")
        if manual:
            reason = "USD-র উচ্চ-প্রভাবের নিউজের সময়; তাই সিগন্যাল বন্ধ।" if calendar_source != "unavailable" else "নিউজ ক্যালেন্ডার পাওয়া যাচ্ছে না; যাচাই ছাড়া সিগন্যাল দেওয়া নিরাপদ নয়।"
            send("📰 /signal: " + reason)
        save_state(state)
        return
    try:
        candidate = validate_live_entry(candidate)
    except Exception as exc:
        print(f"Skipped: live-entry validation blocked signal: {exc}")
        if manual:
            send(f"⚠️ লাইভ দাম যাচাই করা যায়নি, তাই সিগন্যাল বন্ধ রাখা হয়েছে। কারণ: {exc}")
        save_state(state)
        return

    number = state["count"] + 1
    sl_points = abs(candidate["entry"] - candidate["sl"])
    tp_points = abs(candidate["tp"] - candidate["entry"])
    old_note = (
        f"⚠️ সিগন্যাল ক্যান্ডেল ~{age_candles * 15} মিনিট আগের; লাইভ দামে যাচাই করা হয়েছে।\n"
        if age_candles else ""
    )
    message = (
        f"🥇 GOLD {candidate['side']} — V2.4\n"
        f"Entry: {candidate['entry']:.2f} (live quote)\n"
        f"Stop Loss: {candidate['sl']:.2f}\n"
        f"Take Profit: {candidate['tp']:.2f}\n"
        f"SL দূরত্ব: {sl_points:.2f} pts | TP দূরত্ব: {tp_points:.2f} pts\n"
        "MT5-তে নিজের স্পট দামের সাথে এই দূরত্ব বসান; উপরের সংখ্যা হুবহু কপি করবেন না (ফিউচার ≠ স্পট)।\n"
        f"Risk/Reward: 1:{REWARD_RISK:.1f}\n"
        f"Setup score: {candidate['score']}/100 | RSI: {candidate['rsi']:.1f}\n"
        f"Live quote: {candidate['quote_time']} | Drift: {candidate['drift']:.2f}\n"
        f"টাইমফ্রেম: M15 entry + H1 trend | Drift limit: {LIVE_MAX_DRIFT_ATR:.2f} ATR\n"
        f"{old_note}"
        f"আজকের সিগন্যাল: {number}/{MAX_SIGNALS} | বাংলাদেশ সময়\n"
        f"News calendar: {calendar_source}\n\n"
        "ডেটা: Yahoo GC=F gold-futures proxy, spot XAU/USD নয়। "
        "স্কোর জয়ের সম্ভাবনা নয়; আগে demo-তে যাচাই করো।" + ("\n⚠️ ক্যালেন্ডার অনুপলব্ধ: নিউজ ব্ল্যাকআউট যাচাই করা যায়নি।" if calendar_source == "unavailable" else "")
    )
    send(message)  # Persist count only after Telegram confirms delivery.
    state["count"] = number
    state["last_ts"] = now.isoformat()
    state["last_candle"] = candidate["candle"]
    state["seen_candles"] = (state["seen_candles"] + [candidate["candle"]])[-20:]
    state["open"].append(candidate)
    save_state(state)
    print(message)


if __name__ == "__main__":
    if "--command" in sys.argv:
        try:
            command = sys.argv[sys.argv.index("--command") + 1].lower()
        except (ValueError, IndexError):
            raise SystemExit("Missing command after --command")
        if command == "signal":
            main(manual=True)
        elif command in {"calendar", "forecast", "analysis"}:
            from reports import run_report
            run_report(command)
        else:
            raise SystemExit(f"Unknown command: {command}")
    else:
        main(manual="--manual" in sys.argv)
