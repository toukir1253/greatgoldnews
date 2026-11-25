"""Gold signal bot V2.8 — /price /news /performance, DXY filter, S/R + lot size, Ichimoku + VWAP.

V2.10: multi-user support — TELEGRAM_CHAT_ID is a comma-separated list of
  allowed chat IDs (owner + friends); send() delivers to every chat.

V2.8 changes (on top of V2.7: 24-hour operation, no daily signal limit):
  - New Telegram commands: /price (live GC=F quote), /news (latest 5 gold/USD
    headlines, on demand only — does not touch the 10-min auto news), /performance
    (per-strategy WIN/LOSS from state.json history).
  - DXY filter: DXY (^DX-Y.NYB / DX=F, 1h) rising strongly only WARNS on BUY
    signals ("DXY শক্তিশালী — BUY সাবধানে"); it never blocks a Strong signal.
  - Support/Resistance: H1 last-50-candle high/low shown in every signal.
  - Lot size: SL distance + lot size for $10 / $20 / $50 risk (0.01 lot = $1 per $1 move).
  - New strategies (both WEAK until proven otherwise): Ichimoku TK-cross + VWAP cross.
    Backtest 2026-09-16..2026-11-24, GC=F M15, 2R target, 96-bar expiry:
    Ichimoku 26 signals, 5W/21L, -11.0R; VWAP 85 signals, 33W/52L, +14.0R.
    VWAP's +14R comes from one strong downtrend window with no spread/slippage
    modelled, so it stays Weak — one window is not proof.
  - V2.7 categories from 69-day backtest (2026-09-16 to 2026-11-24, GC=F M15, gold -18.6%):
  STRONG: EMA Pullback baseline +6.0R (27 signals); ADX(14)>25 subset +10.0R (11 signals).
  MEDIUM: Stochastic +1.0R (62), Fibonacci+trend +1.0R (68) — breakeven before spread.
  WEAK (lost money in backtest — trade only as information, with care):
    MACD -8.0R, Breakout -9.7R, Engulfing -11.0R, Bollinger -17.1R, RSI divergence -22.2R.
  Backtest was one strong downtrend, so mean-reversion (Weak) strategies were punished
  and trend strategies flattered; results are not a promise. Spread/slippage not modelled.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

SYMBOL = "GC=F"  # Yahoo gold-futures proxy; not broker spot XAU/USD
MAX_SIGNALS = 999  # V2.7: effectively unlimited — no daily signal limit (was 8 in V2.5)
MIN_GAP_MINUTES = 45
MIN_SCORE = 80  # V2.4 loosened from 85: accept slightly weaker qualifying setups
SL_ATR_BUFFER = 0.20
REWARD_RISK = 2.0
LIVE_MAX_DRIFT_ATR = 0.50  # V2.4 loosened from 0.25: live price may drift up to 0.50 ATR from candle close
LIVE_QUOTE_MAX_AGE_SECONDS = 900  # V2.7: was 180 — Yahoo GC=F 1m feed lags ~10 min (quote stale 659s blocked signals), so allow up to 15 min; drift check (0.50 ATR) still protects entry
TRADE_EXPIRY_BARS = 96
LOOKBACK_CANDLES = 4  # V2.3: also check the last 4 closed M15 candles (~60 min)

# V2.7 multi-strategy categories (backtest 2026-09-16..2026-11-24, 69 days, gold -18.6%).
CATEGORY_LABEL = {"strong": "🟢 শক্তিশালী", "medium": "🟡 মাঝারি", "weak": "🔴 দুর্বল"}
# Backtest total R per strategy, shown as a warning for Weak strategies.
# V2.8: Ichimoku / VWAP values are filled in from the V2.8 backtest (same
# 2026-09-16..2026-11-24 GC=F M15 window as V2.5).
STRATEGY_BACKTEST_R = {
    "EMA Pullback": 6.0, "Stochastic": 1.0, "Fibonacci": 1.0, "MACD": -8.0,
    "Bollinger": -17.1, "Breakout": -9.7, "Engulfing": -11.0, "RSI Divergence": -22.2,
    "Ichimoku": -11.0, "VWAP": 14.0,
}
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"]
CALENDAR_CACHE = Path("calendar_cache.json")
CALENDAR_MAX_AGE_HOURS = 36
STATE = Path("state.json")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
CHAT = CHATS[0] if CHATS else ""  # backward-compat alias: first (owner) chat
UTC = timezone.utc
BD = timezone(timedelta(hours=6))


def send(message):
    if not TOKEN or not CHATS:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secret")
    errors = []
    for chat_id in CHATS:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
                timeout=25,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("Telegram API rejected the message")
        except Exception as exc:
            errors.append(f"{chat_id}: {exc}")
    if errors and len(errors) == len(CHATS):
        raise RuntimeError("Telegram send failed for all chats: " + "; ".join(errors))


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
    # V2.7: MACD 12,26,9
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    frame["macd"] = ema12 - ema26
    frame["macd_sig"] = frame["macd"].ewm(span=9, adjust=False).mean()
    # V2.7: Bollinger 20,2
    sma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std(ddof=0)
    frame["bb_mid"] = sma20
    frame["bb_up"] = sma20 + 2 * sd20
    frame["bb_lo"] = sma20 - 2 * sd20
    # V2.7: Stochastic 14,3,3
    lo14 = frame["Low"].rolling(14).min()
    hi14 = frame["High"].rolling(14).max()
    stoch_k = 100 * (close - lo14) / (hi14 - lo14).replace(0, float("nan"))
    frame["stoch_k"] = stoch_k.rolling(3).mean()
    frame["stoch_d"] = frame["stoch_k"].rolling(3).mean()
    # V2.7: ADX(14) Wilder
    up_move = frame["High"].diff()
    down_move = -frame["Low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr14 = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    frame["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    frame["plus_di"] = plus_di
    frame["minus_di"] = minus_di
    # V2.8: Ichimoku (9/26/52). Senkou values here are computed from past data
    # at the same bar (not shifted 26 forward) so the signal is lookahead-free.
    high9 = frame["High"].rolling(9).max()
    low9 = frame["Low"].rolling(9).min()
    frame["tenkan"] = (high9 + low9) / 2
    high26 = frame["High"].rolling(26).max()
    low26 = frame["Low"].rolling(26).min()
    frame["kijun"] = (high26 + low26) / 2
    high52 = frame["High"].rolling(52).max()
    low52 = frame["Low"].rolling(52).min()
    frame["senkou_b"] = (high52 + low52) / 2
    frame["senkou_a"] = (frame["tenkan"] + frame["kijun"]) / 2
    # V2.8: intraday VWAP, reset each UTC day. Typical price weighted by volume;
    # if volume is missing/zero, fall back to equal weighting (count).
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    vol = frame["Volume"].fillna(0) if "Volume" in frame else pd.Series(0.0, index=frame.index)
    vol = vol.where(vol > 0, 1.0)
    day = frame.index.date
    frame["vwap"] = (
        (typical * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
    )
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
    adx = float(current["adx"]) if "adx" in current and pd.notna(current["adx"]) else float("nan")
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
        "strategy": "EMA Pullback",
        "category": "strong",
        "adx": adx,
        "high_confidence": bool(pd.notna(adx) and adx > 25),  # V2.7: ADX>25 subset made +10.0R in backtest
    }, "ok"



def _rows(m15, h1, offset=0):
    """Return (current, previous, higher, reason) for one closed M15 candle."""
    if len(m15) < 60 + offset or len(h1) < 60:
        return None, None, None, "not enough data"
    current = m15.iloc[-2 - offset]
    previous = m15.iloc[-3 - offset]
    if offset == 0:
        higher = h1.iloc[-2]
    else:
        cutoff = current.name + pd.Timedelta(minutes=15) - pd.Timedelta(hours=1)
        eligible = h1.loc[h1.index <= cutoff]
        if eligible.empty:
            return None, None, None, "no H1 data for that time"
        higher = eligible.iloc[-1]
    return current, previous, higher, ""


def _trend_ok(current, higher, side):
    if side == "BUY":
        return (higher["ema20"] > higher["ema50"] and higher["Close"] > higher["ema20"]
                and current["ema20"] > current["ema50"] and float(current["Close"]) > current["ema20"])
    return (higher["ema20"] < higher["ema50"] and higher["Close"] < higher["ema20"]
            and current["ema20"] < current["ema50"] and float(current["Close"]) < current["ema20"])


def make_candidate(side, price, atr, current, previous, strategy_name, category, score=80):
    """Build SL/TP exactly like build_signal (SL = candle low/high ∓ 0.20 ATR, TP = 2R).
    Returns (candidate_or_None, reason)."""
    rsi = float(current["rsi"]) if pd.notna(current["rsi"]) else float("nan")
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
        "side": side, "entry": price, "sl": stop, "tp": target, "rsi": rsi,
        "score": score, "risk": risk, "atr": atr, "candle": candle_time,
        "time": datetime.now(UTC).isoformat(),
        "strategy": strategy_name, "category": category,
    }, "ok"


def _window(m15, offset):
    """20 M15 bars before the current candle (same window as the backtest)."""
    return m15.iloc[-22 - offset:-2 - offset]


def macd_signal(m15, h1, offset=0):
    """WEAK (backtest -8.0R): MACD crossover + EMA trend alignment + confirmation candle."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    if pd.isna(current["macd"]) or pd.isna(current["macd_sig"]):
        return None, "MACD not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    side = None
    if (previous["macd"] <= previous["macd_sig"] and current["macd"] > current["macd_sig"]
            and price > float(current["Open"]) and _trend_ok(current, higher, "BUY")):
        side = "BUY"
    elif (previous["macd"] >= previous["macd_sig"] and current["macd"] < current["macd_sig"]
            and price < float(current["Open"]) and _trend_ok(current, higher, "SELL")):
        side = "SELL"
    if side is None:
        return None, "no MACD crossover with trend"
    return make_candidate(side, price, atr, current, previous, "MACD", "weak")


def stochastic_signal(m15, h1, offset=0):
    """MEDIUM (backtest +1.0R): Stochastic crossover in oversold/overbought + EMA trend."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    if pd.isna(current["stoch_k"]) or pd.isna(current["stoch_d"]):
        return None, "Stochastic not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    side = None
    if (previous["stoch_k"] <= previous["stoch_d"] and current["stoch_k"] > current["stoch_d"]
            and current["stoch_k"] < 30 and current["ema20"] > current["ema50"]
            and price > float(current["Open"])):
        side = "BUY"
    elif (previous["stoch_k"] >= previous["stoch_d"] and current["stoch_k"] < current["stoch_d"]
            and current["stoch_k"] > 70 and current["ema20"] < current["ema50"]
            and price < float(current["Open"])):
        side = "SELL"
    if side is None:
        return None, "no Stochastic crossover in zone"
    return make_candidate(side, price, atr, current, previous, "Stochastic", "medium")


def fib_signal(m15, h1, offset=0):
    """MEDIUM (backtest +1.0R): Fibonacci 38.2/50/61.8% retracement bounce + EMA trend."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    win = _window(m15, offset)
    hi, lo = float(win["High"].max()), float(win["Low"].min())
    rng = hi - lo
    if rng <= 0:
        return None, "no Fib swing range"
    hi_t, lo_t = win["High"].idxmax(), win["Low"].idxmin()
    side = None
    if lo_t < hi_t:  # up-swing: buy at retracement from the high
        for f in (0.382, 0.50, 0.618):
            lvl = hi - f * rng
            if (abs(float(previous["Low"]) - lvl) <= 0.25 * atr
                    and price > float(current["Open"]) and price > float(previous["High"])
                    and _trend_ok(current, higher, "BUY")):
                side = "BUY"
                break
    if side is None and hi_t < lo_t:  # down-swing: sell at retracement from the low
        for f in (0.382, 0.50, 0.618):
            lvl = lo + f * rng
            if (abs(float(previous["High"]) - lvl) <= 0.25 * atr
                    and price < float(current["Open"]) and price < float(previous["Low"])
                    and _trend_ok(current, higher, "SELL")):
                side = "SELL"
                break
    if side is None:
        return None, "no Fibonacci bounce with trend"
    return make_candidate(side, price, atr, current, previous, "Fibonacci", "medium")


def bollinger_signal(m15, h1, offset=0):
    """WEAK (backtest -17.1R): Bollinger bounce — close back inside after touching a band."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    if pd.isna(current["bb_lo"]):
        return None, "Bollinger not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    side = None
    if (previous["Close"] < previous["bb_lo"] and price > current["bb_lo"]
            and price > float(current["Open"])):
        side = "BUY"
    elif (previous["Close"] > previous["bb_up"] and price < current["bb_up"]
            and price < float(current["Open"])):
        side = "SELL"
    if side is None:
        return None, "no Bollinger bounce"
    return make_candidate(side, price, atr, current, previous, "Bollinger", "weak")


def breakout_signal(m15, h1, offset=0):
    """WEAK (backtest -9.7R): breakout of previous 20-candle high/low + confirmation."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    prev20 = _window(m15, offset)
    hi20, lo20 = float(prev20["High"].max()), float(prev20["Low"].min())
    side = None
    if price > hi20 + 0.10 * atr and price > float(current["Open"]):
        side = "BUY"
    elif price < lo20 - 0.10 * atr and price < float(current["Open"]):
        side = "SELL"
    if side is None:
        return None, "no 20-candle breakout"
    return make_candidate(side, price, atr, current, previous, "Breakout", "weak")


def engulfing_signal(m15, h1, offset=0):
    """WEAK (backtest -11.0R): engulfing candle at/near EMA20 with EMA trend."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    bull = (current["Open"] < previous["Close"] and price > previous["Open"]
            and price > float(current["Open"]) and previous["Close"] < previous["Open"])
    bear = (current["Open"] > previous["Close"] and price < previous["Open"]
            and price < float(current["Open"]) and previous["Close"] > previous["Open"])
    side = None
    if bull and (abs(float(current["Low"]) - current["ema20"]) <= 0.5 * atr
                 or abs(price - current["ema20"]) <= 0.5 * atr) and current["ema20"] > current["ema50"]:
        side = "BUY"
    elif bear and (abs(float(current["High"]) - current["ema20"]) <= 0.5 * atr
                   or abs(price - current["ema20"]) <= 0.5 * atr) and current["ema20"] < current["ema50"]:
        side = "SELL"
    if side is None:
        return None, "no engulfing at EMA20"
    return make_candidate(side, price, atr, current, previous, "Engulfing", "weak")


def rsi_divergence_signal(m15, h1, offset=0):
    """WEAK (backtest -22.2R, worst): RSI divergence — price new extreme, RSI weaker + reversal."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    if pd.isna(current["rsi"]):
        return None, "RSI not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    win = _window(m15, offset)
    side = None
    hi_idx = win["High"].idxmax()
    hi_pos = win.index.get_loc(hi_idx)
    if (float(current["High"]) > float(win["High"].max()) and float(current["rsi"]) < float(win["rsi"].iloc[hi_pos])
            and price < float(current["Open"]) and price < float(previous["Close"])):
        side = "SELL"
    lo_idx = win["Low"].idxmin()
    lo_pos = win.index.get_loc(lo_idx)
    if side is None and (float(current["Low"]) < float(win["Low"].min()) and float(current["rsi"]) > float(win["rsi"].iloc[lo_pos])
            and price > float(current["Open"]) and price > float(previous["Close"])):
        side = "BUY"
    if side is None:
        return None, "no RSI divergence"
    return make_candidate(side, price, atr, current, previous, "RSI Divergence", "weak")


def ichimoku_signal(m15, h1, offset=0):
    """V2.8 WEAK: Ichimoku Tenkan/Kijun cross — a true event (not a persistent
    state, so it does not re-fire on every candle while price sits above the
    cloud). BUY: Tenkan crosses above Kijun, price above cloud, Chikou
    (today's close vs price 26 bars ago) confirming, H1 trend aligned. SELL
    is the mirror image."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    for col in ("tenkan", "kijun", "senkou_a", "senkou_b"):
        if col not in current or pd.isna(current[col]) or pd.isna(previous[col]):
            return None, "Ichimoku not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    cloud_top = max(float(current["senkou_a"]), float(current["senkou_b"]))
    cloud_bot = min(float(current["senkou_a"]), float(current["senkou_b"]))
    # Chikou span: today's close vs the price 26 bars ago.
    idx = m15.index.get_loc(current.name)
    if idx < 27:
        return None, "Ichimoku not ready"
    price_26_ago = float(m15["Close"].iloc[idx - 26])
    cross_up = (float(previous["tenkan"]) <= float(previous["kijun"])
                and float(current["tenkan"]) > float(current["kijun"]))
    cross_down = (float(previous["tenkan"]) >= float(previous["kijun"])
                  and float(current["tenkan"]) < float(current["kijun"]))
    side = None
    if (cross_up and price > cloud_top and price > price_26_ago
            and price > float(current["Open"])
            and _trend_ok(current, higher, "BUY")):
        side = "BUY"
    elif (cross_down and price < cloud_bot and price < price_26_ago
            and price < float(current["Open"])
            and _trend_ok(current, higher, "SELL")):
        side = "SELL"
    if side is None:
        return None, "no Ichimoku TK cross with cloud"
    return make_candidate(side, price, atr, current, previous, "Ichimoku", "weak")


def vwap_signal(m15, h1, offset=0):
    """V2.8 WEAK: VWAP cross — close crosses the intraday VWAP with EMA trend
    alignment. VWAP is a mean level, so this is a mean-reversion-flavoured
    signal and is kept Weak until its backtest proves otherwise."""
    current, previous, higher, reason = _rows(m15, h1, offset)
    if current is None:
        return None, reason
    if "vwap" not in current or pd.isna(current["vwap"]) or pd.isna(previous["vwap"]):
        return None, "VWAP not ready"
    price = float(current["Close"])
    atr = float(current["atr"])
    if not pd.notna(atr) or atr <= 0:
        return None, "indicator not ready"
    side = None
    if (float(previous["Close"]) <= float(previous["vwap"]) and price > float(current["vwap"])
            and price > float(current["Open"]) and _trend_ok(current, higher, "BUY")):
        side = "BUY"
    elif (float(previous["Close"]) >= float(previous["vwap"]) and price < float(current["vwap"])
            and price < float(current["Open"]) and _trend_ok(current, higher, "SELL")):
        side = "SELL"
    if side is None:
        return None, "no VWAP cross"
    return make_candidate(side, price, atr, current, previous, "VWAP", "weak")


def dxy_status():
    """V2.8: Dollar Index (DXY) 1h trend. Fail-open: returns None when Yahoo
    has no DXY data, so a missing DXY feed never blocks a signal.
    Returns dict(value, change_1h_pct, rising_strongly) or None."""
    # DX-Y.NYB (no caret) works on Yahoo; DX=F / ^DX-Y.NYB returned no data
    # in testing, UUP (dollar ETF) is a last-resort proxy.
    for symbol in ("DX-Y.NYB", "UUP", "DX=F", "^DX-Y.NYB"):
        try:
            frame = yf.download(symbol, period="5d", interval="1h", progress=False,
                                auto_adjust=False, threads=False)
            if frame is None or frame.empty:
                continue
            if isinstance(frame.columns, pd.MultiIndex):
                frame.columns = frame.columns.get_level_values(0)
            close = frame["Close"].dropna()
            if len(close) < 25:
                continue
            ema20 = close.ewm(span=20, adjust=False).mean()
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            change = (last / prev - 1) * 100 if prev else 0.0
            rising = bool(change > 0.05 and last > float(ema20.iloc[-1]))
            return {"symbol": symbol, "value": last, "change_1h_pct": change,
                    "rising_strongly": rising}
        except Exception as exc:
            print(f"DXY fetch failed ({symbol}): {exc}")
            continue
    return None


def support_resistance(h1):
    """V2.8: support = lowest low of last 50 H1 candles, resistance = highest
    high of last 50 H1 candles. Returns (support, resistance) or (None, None)."""
    if h1 is None or len(h1) < 50:
        return None, None
    tail = h1.iloc[-50:]
    return float(tail["Low"].min()), float(tail["High"].max())


def lot_size_text(risk_points):
    """V2.8: lot size for $10 / $20 / $50 risk. Gold 0.01 lot = 1 oz, so $1 move
    = $1 per 0.01 lot, i.e. $100 per 1.00 lot per $1 move. Demo only — broker
    contract sizes differ, so verify on your own broker before trading live."""
    if not risk_points or risk_points <= 0:
        return ""
    parts = []
    for dollars in (10, 20, 50):
        lots = dollars / (risk_points * 100.0)
        lots = max(0.01, min(2.00, round(lots, 2)))
        parts.append(f"${dollars} রিস্ক → {lots:.2f} lot")
    return " | ".join(parts)


# V2.7 priority order: Strong -> Medium -> Weak. If several match one candle,
# the strongest category wins (checked first). V2.8: Ichimoku + VWAP are Weak,
# so they are checked last and never override a Strong/Medium signal.
STRATEGY_FUNCTIONS = [
    build_signal,        # STRONG: EMA Pullback (V2.7 baseline)
    stochastic_signal,   # MEDIUM
    fib_signal,          # MEDIUM
    macd_signal,         # WEAK
    bollinger_signal,    # WEAK
    breakout_signal,     # WEAK
    engulfing_signal,    # WEAK
    rsi_divergence_signal,  # WEAK
    ichimoku_signal,     # V2.8 WEAK
    vwap_signal,         # V2.8 WEAK
]

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


def price_command():
    """V2.8 /price: live GC=F 1m quote + change vs previous daily close."""
    quote = yf.download(SYMBOL, period="1d", interval="1m", progress=False,
                        auto_adjust=False, threads=False)
    if quote is None or quote.empty:
        send("🥇 GOLD PRICE\nলাইভ দাম পাওয়া যায়নি — Yahoo ফিড চেক করো।")
        return
    if isinstance(quote.columns, pd.MultiIndex):
        quote.columns = quote.columns.get_level_values(0)
    quote = quote.dropna(subset=["Close"])
    stamp = pd.Timestamp(quote.index[-1])
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    price = float(quote["Close"].iloc[-1])
    age = (pd.Timestamp.now(tz="UTC") - stamp).total_seconds()
    change_txt = ""
    try:
        daily = yf.download(SYMBOL, period="5d", interval="1d", progress=False,
                            auto_adjust=False, threads=False)
        if daily is not None and not daily.empty:
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            prev_close = float(daily["Close"].dropna().iloc[-2])
            change_txt = f"\nগতকালের ক্লোজ থেকে: {(price / prev_close - 1) * 100:+.2f}%"
    except Exception as exc:
        print("Price change unavailable:", exc)
    bd_stamp = stamp.astimezone(BD).strftime("%d %b, %I:%M %p")
    send(
        f"🥇 GOLD PRICE — {bd_stamp} (বাংলাদেশ)\n"
        f"দাম: {price:.2f}{change_txt}\n"
        f"কোটের বয়স: {age:.0f}s\n"
        "Yahoo GC=F futures proxy; spot XAU/USD নয়, delayed হতে পারে।"
    )


def news_command():
    """V2.8 /news: latest 5 relevant gold/USD headlines on demand.
    Reads the feeds directly and does NOT mark anything seen, so the 10-min
    auto news (news.py) is not disturbed and headlines are not spammed."""
    from news import NEWS_FEEDS, fetch_feed, is_relevant, classify_impact, bd_time
    items = []
    for source, url in NEWS_FEEDS:
        try:
            items.extend(fetch_feed(source, url))
        except Exception as exc:
            print(f"News feed failed ({source}): {exc}")
    if not items:
        send("📰 GOLD / USD NEWS\nএই মুহূর্তে কোনো নিউজ ফিড থেকে খবর পাওয়া যায়নি।")
        return
    seen_links, picked = set(), []
    # Gold/USD feed first (Fed/BLS feeds also carry admin headlines that pass
    # the relevance filter); newest first within each feed.
    ordered = sorted(
        items,
        key=lambda x: (x["source"] != "Gold / USD market news", -x["published"].timestamp()),
    )
    for item in ordered:
        if not is_relevant(item["title"], item["description"]):
            continue
        link = (item["link"] or "").strip()
        if link in seen_links:
            continue
        seen_links.add(link)
        picked.append(item)
        if len(picked) >= 5:
            break
    if not picked:
        send("📰 GOLD / USD NEWS\nসাম্প্রতিক প্রাসঙ্গিক কোনো শিরোনাম পাওয়া যায়নি।")
        return
    lines = ["📰 GOLD / USD NEWS — সর্বশেষ ৫টি\n"]
    for item in picked:
        impact, marker = classify_impact(item["title"], item["description"])
        lines.append(
            f"{marker} {item['title']}\n"
            f"🕒 {bd_time(item['published'])} | উৎস: {item['source']}\n"
            f"🔗 {item['link']}\n"
        )
    lines.append("⚠️ শিরোনাম মানেই দামের দিক নয়; এটি BUY/SELL সিগন্যাল নয়।")
    send("\n".join(lines))


def performance_command():
    """V2.8 /performance: per-strategy WIN/LOSS/TIMEOUT + win-rate from
    state.json history (last 300 closed trades) plus open trades."""
    state = load_state()
    trades = state.get("history", []) + state.get("open", [])
    if not trades:
        send("📊 PERFORMANCE\nএখনো কোনো বন্ধ বা চলমান ট্রেডের হিসাব নেই।")
        return
    by_strategy = {}
    for trade in trades:
        name = trade.get("strategy", "Unknown")
        bucket = by_strategy.setdefault(name, {"WIN": 0, "LOSS": 0, "TIMEOUT": 0,
                                               "AMBIGUOUS": 0, "OPEN": 0})
        result = trade.get("result")
        if result in bucket:
            bucket[result] += 1
        else:
            bucket["OPEN"] += 1
    lines = ["📊 PERFORMANCE — স্ট্র্যাটেজি অনুযায়ী (সর্বশেষ ৩০০ ট্রেড)\n"]
    total_wins = total_losses = 0
    for name in sorted(by_strategy, key=lambda n: -by_strategy[n]["WIN"]):
        b = by_strategy[name]
        decided = b["WIN"] + b["LOSS"]
        win_rate = (b["WIN"] / decided * 100) if decided else 0.0
        total_wins += b["WIN"]
        total_losses += b["LOSS"]
        lines.append(
            f"{name}: WIN {b['WIN']} | LOSS {b['LOSS']} | Timeout {b['TIMEOUT']} | "
            f"চলমান {b['OPEN']} | Win rate {win_rate:.0f}%"
        )
    total_decided = total_wins + total_losses
    overall = (total_wins / total_decided * 100) if total_decided else 0.0
    lines.append(
        f"\nমোট: WIN {total_wins} | LOSS {total_losses} | Win rate {overall:.0f}%\n"
        "ফলাফল candle-based আনুমানিক হিসাব; spread/slippage অন্তর্ভুক্ত নয়।"
    )
    send("\n".join(lines))


def send_daily_report(state, today, signal_count=None):
    trades = [t for t in state["history"] if is_today_bd(t.get("closed") or t.get("time", ""), today)]
    wins = sum(t.get("result") == "WIN" for t in trades)
    losses = sum(t.get("result") == "LOSS" for t in trades)
    ambiguous = sum(t.get("result") == "AMBIGUOUS" for t in trades)
    timeouts = sum(t.get("result") == "TIMEOUT" for t in trades)
    message = (
        f"📊 GOLD দৈনিক রিপোর্ট — {today}\n"
        f"সিগন্যাল: {state['count'] if signal_count is None else signal_count}\n"
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
        elif bd_now.hour == 23 and bd_now.minute >= 50 and state["summary"] != today:  # V2.7: daily report every day, not Mon-Fri only
            send_daily_report(state, today, state["count"])
            state["summary"] = today

    # V2.7: no time gate and no daily limit — signals run 24 hours, any day.
    # Gold market itself is closed on weekends, so weekend runs normally find
    # no setup / stale data, but the bot no longer blocks them.
    if state["last_ts"]:
        last_time = datetime.fromisoformat(state["last_ts"])
        waited = (now - last_time).total_seconds()
        if waited < MIN_GAP_MINUTES * 60:
            print(f"Skipped: minimum gap {MIN_GAP_MINUTES} min (only {waited / 60:.0f} min passed).")
            if manual:
                send(f"⏳ শেষ সিগন্যালের পর {MIN_GAP_MINUTES} মিনিট বিরতি রাখতে হবে। আরও {max(1, int((MIN_GAP_MINUTES * 60 - waited) / 60))} মিনিট পরে চেষ্টা করো।")
            save_state(state)
            return

    # V2.7: newest closed candle first, strategies in Strong -> Medium -> Weak order.
    # If several strategies match one candle, the strongest category wins.
    # seen_candles stores "candle|strategy" (V2.7); plain candle strings from V2.4
    # are kept backward compatible and block every strategy for that candle.
    seen = set(state["seen_candles"]) | ({state["last_candle"]} if state["last_candle"] else set())
    candidate, age_candles, reasons = None, 0, []
    for offset in range(LOOKBACK_CANDLES):
        for strategy_fn in STRATEGY_FUNCTIONS:
            found, reason = strategy_fn(m15, h1, offset)
            if not found:
                reasons.append(f"[-{offset}] {strategy_fn.__name__}: {reason}")
                continue
            key = f"{found['candle']}|{found['strategy']}"
            if key in seen or found["candle"] in seen:
                reasons.append(f"[-{offset}] {found['strategy']} already alerted")
                continue
            candidate, age_candles = found, offset
            break
        if candidate:
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
    cat_label = CATEGORY_LABEL.get(candidate.get("category", "medium"), "🟡 মাঝারি")
    strat = candidate.get("strategy", "EMA Pullback")
    star = " ⭐" if candidate.get("high_confidence") else ""
    weak_note = ""
    if candidate.get("category") == "weak":
        br = STRATEGY_BACKTEST_R.get(strat, 0)
        if br < 0:
            weak_note = (f"⚠️ ব্যাকটেস্টে লস করেছে (৬৯ দিনে {br}R), সাবধানে ট্রেড করো।\n")
        else:
            weak_note = (f"⚠️ নতুন স্ট্র্যাটেজি — ব্যাকটেস্টে {br}R হলেও মাত্র একটা "
                         "ডাউনট্রেন্ড উইন্ডোর ফল, সাবধানে ট্রেড করো।\n")
    # V2.8: DXY filter (warn only, never blocks a Strong signal) + S/R + lot size.
    dxy = dxy_status()
    dxy_note = ""
    if dxy is not None:
        dxy_note = f"DXY: {dxy['value']:.2f} ({dxy['change_1h_pct']:+.2f}% 1h)\n"
        if candidate["side"] == "BUY" and dxy["rising_strongly"]:
            dxy_note += "⚠️ DXY শক্তিশালী — BUY সাবধানে (ডলার শক্ত হলে সোনায় চাপ পড়ে)।\n"
    elif dxy is None:
        dxy_note = "DXY: তথ্য পাওয়া যায়নি (ফিল্টার প্রয়োগ হয়নি)।\n"
    sup, res = support_resistance(h1)
    sr_note = ""
    if sup is not None and res is not None:
        sr_note = f"সাপোর্ট: {sup:.2f} | রেজিস্ট্যান্স: {res:.2f} (H1 শেষ ৫০ candle)\n"
    lot_note = lot_size_text(sl_points)
    message = (
        f"🥇 GOLD {candidate['side']} — V2.8\n"
        f"{cat_label} — {strat}{star}\n"
        f"{weak_note}"
        f"Entry: {candidate['entry']:.2f} (live quote)\n"
        f"Stop Loss: {candidate['sl']:.2f}\n"
        f"Take Profit: {candidate['tp']:.2f}\n"
        f"SL দূরত্ব: {sl_points:.2f} pts | TP দূরত্ব: {tp_points:.2f} pts\n"
        f"{sr_note}"
        f"{dxy_note}"
        f"Lot size (0.01 lot = $1 per $1 move): {lot_note}\n"
        "MT5-তে নিজের স্পট দামের সাথে এই দূরত্ব বসান; উপরের সংখ্যা হুবহু কপি করবেন না (ফিউচার ≠ স্পট)।\n"
        "⚠️ লট সাইজ শুধু উদাহরণ — আগে demo-তে যাচাই করো, ব্রোকারের কন্ট্রাক্ট সাইজ ভিন্ন হতে পারে।\n"
        f"Risk/Reward: 1:{REWARD_RISK:.1f}\n"
        f"Setup score: {candidate['score']}/100 | RSI: {candidate['rsi']:.1f}\n"
        f"Live quote: {candidate['quote_time']} | Drift: {candidate['drift']:.2f}\n"
        f"টাইমফ্রেম: M15 entry + H1 trend | Drift limit: {LIVE_MAX_DRIFT_ATR:.2f} ATR\n"
        f"{old_note}"
        f"আজকের সিগন্যাল: {number} | বাংলাদেশ সময়\n"
        f"News calendar: {calendar_source}\n\n"
        "ডেটা: Yahoo GC=F gold-futures proxy, spot XAU/USD নয়। "
        "স্কোর জয়ের সম্ভাবনা নয়; আগে demo-তে যাচাই করো।" + ("\n⚠️ ক্যালেন্ডার অনুপলব্ধ: নিউজ ব্ল্যাকআউট যাচাই করা যায়নি।" if calendar_source == "unavailable" else "")
    )
    send(message)  # Persist count only after Telegram confirms delivery.
    state["count"] = number
    state["last_ts"] = now.isoformat()
    state["last_candle"] = candidate["candle"]
    state["seen_candles"] = (state["seen_candles"] + [f"{candidate['candle']}|{candidate.get('strategy', 'EMA Pullback')}"])[-40:]
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
        elif command == "price":
            price_command()
        elif command == "news":
            news_command()
        elif command == "performance":
            performance_command()
        elif command in {"calendar", "forecast", "analysis"}:
            from reports import run_report
            run_report(command)
        else:
            raise SystemExit(f"Unknown command: {command}")
    else:
        main(manual="--manual" in sys.argv)
