"""Gold news and market-status alerts for Telegram."""
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from bot import SYMBOL, send
from reports import calendar_report, chart_report, weekly_report

IMPACTS = {"High"}
CURRENCIES = {"USD"}
WARN_MINUTES = 60
STATUS_EVERY_MIN = 30
MAX_HEADLINES = 3
STATE_FILE = Path("news_state.json")
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"]
CALENDAR_CACHE = Path("news_calendar_cache.json")  # separate from signal bot cache
NEWS_URL = (
    "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+OR+%22gold+futures%22"
    "+when:1d&hl=en-US&gl=US&ceid=US:en"
)
UTC = timezone.utc
BD = timezone(timedelta(hours=6))


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {
        "pre": state.get("pre", []),
        "now": state.get("now", []),
        "seen": state.get("seen", []),
        "last_status": state.get("last_status", ""),
        "last_calendar_date": state.get("last_calendar_date", ""),
        "last_forecast_date": state.get("last_forecast_date", ""),
        "last_weekly_forecast": state.get("last_weekly_forecast", ""),
    }


def bd_time(value):
    return value.astimezone(BD).strftime("%d %b, %I:%M %p")


def get_calendar_alerts(state, now):
    alerts = []
    events = None
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
            print(f"Calendar feed failed ({url}): {exc}")
    if events is None:
        try:
            cached = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            age = (now - fetched.astimezone(UTC)).total_seconds()
            if 0 <= age <= 36 * 3600 and isinstance(cached.get("events"), list):
                events = cached["events"]
                print(f"Using cached calendar ({age / 3600:.1f}h old)")
        except Exception as exc:
            print("Calendar cache unavailable:", exc)
    if events is None:
        print("Calendar unavailable; no calendar alerts this run.")
        return alerts

    for event in events:
        if event.get("country") not in CURRENCIES or event.get("impact") not in IMPACTS:
            continue
        try:
            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            event_id = hashlib.sha256(
                (event.get("title", "") + event["date"]).encode()
            ).hexdigest()[:16]
            minutes = (event_time - now).total_seconds() / 60
            details = (
                f"খবর: {translate_to_bengali(event.get('title', 'গুরুত্বপূর্ণ মার্কিন অর্থনৈতিক তথ্য'))}\n"
                f"সময়: {bd_time(event_time)} (বাংলাদেশ)\n"
                f"সম্ভাব্য ফল: {event.get('forecast') or 'তথ্য নেই'} | আগের ফল: {event.get('previous') or 'তথ্য নেই'}"
            )
            if 0 < minutes <= WARN_MINUTES and event_id not in state["pre"]:
                state["pre"].append(event_id)
                alerts.append(
                    f"⏰ গুরুত্বপূর্ণ USD নিউজ আসছে (~{int(minutes)} মিনিট পর)\n"
                    f"{details}\nনিউজের সময় volatility বাড়তে পারে।"
                )
            elif -15 <= minutes <= 0 and event_id not in state["now"]:
                state["now"].append(event_id)
                if event_id not in state["pre"]:
                    state["pre"].append(event_id)
                alerts.append(f"🔔 USD নিউজ প্রকাশিত\n{details}\nস্প্রেড ও volatility লক্ষ্য করো।")
        except (KeyError, TypeError, ValueError):
            continue
    state["pre"] = state["pre"][-300:]
    state["now"] = state["now"][-300:]
    return alerts



def translate_to_bengali(text):
    """Translate a headline/snippet to Bengali; preserve original if translation is unavailable."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "bn", "dt": "t", "q": text[:900]},
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        parts = response.json()[0]
        translated = "".join(part[0] for part in parts if part and part[0])
        return translated.strip() or f"অনুবাদ পাওয়া যায়নি। মূল খবর: {text}"
    except Exception as exc:
        print("Bengali translation unavailable; using source text:", exc)
        return f"অনুবাদ পাওয়া যায়নি। মূল খবর: {text}"


def likely_gold_impact(text):
    """A cautious keyword-based hint, not a trading signal."""
    value = (text or "").lower()
    if any(k in value for k in ("inflation", "cpi", "ppi", "jobs report", "payroll", "employment", "fed rate", "interest rate", "rate cut", "rate hike", "federal reserve")):
        return "এই খবর ডলার ও সুদের হারকে প্রভাবিত করতে পারে। তাই সোনার দাম বাড়তেও বা কমতেও পারে।"
    if any(k in value for k in ("war", "conflict", "attack", "geopolitical", "sanction", "ceasefire")):
        return "বিশ্বে অস্থিরতা বাড়লে মানুষ সোনা কিনতে পারে। তবে দাম বাড়বেই—এমন নিশ্চয়তা নেই।"
    if any(k in value for k in ("central bank", "gold buying", "gold demand", "gold reserve", "etf")):
        return "বড় ব্যাংক বা বিনিয়োগকারীরা কত সোনা কিনছে, তা সোনার দামে প্রভাব ফেলতে পারে।"
    return "এই খবরের কারণে সোনার দাম কোন দিকে যাবে, তা এখনো পরিষ্কার নয়।"


def get_headline_alerts(state, now):
    alerts = []
    try:
        response = requests.get(NEWS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        items = ET.fromstring(response.content).findall("./channel/item")
    except Exception as exc:
        print("News RSS fetch failed:", exc)
        return alerts

    first_run = not state["seen"]
    fresh = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        lower_title = title.lower()
        relevant_terms = ("gold", "xau", "bullion", "precious metal", "federal reserve", "fed", "interest rate", "inflation", "cpi", "ppi", "payroll", "jobs report", "employment", "treasury yield", "dollar", "central bank")
        irrelevant_terms = ("gold coin", "commemorative coin", "coin collection", "golden boot", "golden globes", "jewelry design")
        if any(term in lower_title for term in irrelevant_terms) or not any(term in lower_title for term in relevant_terms):
            continue
        headline_id = hashlib.sha256(title.encode()).hexdigest()[:16]
        if headline_id in state["seen"]:
            continue
        state["seen"].append(headline_id)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            published = now
        age = now - published.astimezone(UTC)
        if not first_run and timedelta(0) <= age <= timedelta(minutes=45):
            fresh.append((title, link, item.findtext('description') or ''))

    state["seen"] = state["seen"][-500:]
    for title, link, description in fresh[:MAX_HEADLINES]:
        bn_title = translate_to_bengali(title)
        clean_description = re.sub(r"<[^>]+>", " ", html.unescape(description or ""))
        clean_description = re.sub(r"\s+", " ", clean_description).strip()
        bn_summary = ""  # RSS description প্রায়ই সারাংশ নয়; ভুল সারাংশ দেখাব না।
        impact = likely_gold_impact(title)
        message = f"📰 সোনার বাজারের খবর\n{bn_title}"
        if bn_summary:
            message += f"\n\n📝 সহজ বাংলা অনুবাদ:\n{bn_summary}"
        message += f"\n\nসহজ করে বললে: {impact}"
        message += f"\n\n🔗 মূল খবর: {link}"
        alerts.append(message)
    return alerts


def market_status():
    frame = yf.download(
        SYMBOL, period="5d", interval="15m", progress=False,
        auto_adjust=False, threads=False
    )
    if frame is None or frame.empty:
        raise RuntimeError("No gold price data available")
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.dropna().copy()
    if len(frame) < 55:
        raise RuntimeError("Not enough candles for gold status")

    close = frame["Close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi_series = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

    daily = yf.download(
        SYMBOL, period="10d", interval="1d", progress=False,
        auto_adjust=False, threads=False
    )
    if daily is None or daily.empty:
        raise RuntimeError("No daily gold data available")
    if isinstance(daily.columns, pd.MultiIndex):
        daily.columns = daily.columns.get_level_values(0)
    daily_close = daily["Close"].dropna()
    completed = -2
    candle_time = pd.Timestamp(frame.index[completed])
    candle_time = candle_time.tz_localize("UTC") if candle_time.tzinfo is None else candle_time.tz_convert("UTC")
    candle_age = (pd.Timestamp.now(tz="UTC") - candle_time).total_seconds()
    if candle_age > 1800 or candle_age < -60:
        raise RuntimeError(f"Completed M15 candle stale: {candle_age:.0f}s")
    price = float(close.iloc[completed])
    previous_close = float(daily_close.iloc[-2]) if len(daily_close) >= 2 else float("nan")
    change = (price / previous_close - 1) * 100 if pd.notna(previous_close) else 0.0
    rsi = float(rsi_series.iloc[completed])
    trend = "ঊর্ধ্বমুখী 📈" if ema20.iloc[completed] > ema50.iloc[completed] else "নিম্নমুখী 📉"
    condition = "দাম অতিরিক্ত বেড়েছে" if rsi >= 70 else "দাম অতিরিক্ত নেমেছে" if rsi <= 30 else "স্বাভাবিক অবস্থায়"
    return (
        f"🥇 GOLD MARKET STATUS\nদাম: {price:.2f} ({change:+.2f}% daily)\n"
        f"M15 trend: {trend}\nRSI: {rsi:.1f} ({condition})\n"
        f"শেষ সম্পূর্ণ M15 candle: {candle_time.isoformat()}\n"
        "দাম Yahoo GC=F futures proxy; spot XAU/USD নয়; delayed হতে পারে।"
    )


def main():
    now = datetime.now(UTC)
    state = load_state()
    messages = get_calendar_alerts(state, now)
    messages.extend(get_headline_alerts(state, now))
    bd_now = now.astimezone(BD)
    today = bd_now.strftime("%Y-%m-%d")
    week_key = bd_now.strftime("%G-W%V")
    if state["last_calendar_date"] != today:
        try:
            calendar_report()
            state["last_calendar_date"] = today
        except Exception as exc:
            print("Daily calendar image failed:", exc)
    if state["last_forecast_date"] != today:
        try:
            chart_report("forecast")
            state["last_forecast_date"] = today
        except Exception as exc:
            print("Daily forecast image failed:", exc)
    if bd_now.weekday() == 0 and state["last_weekly_forecast"] != week_key:
        try:
            weekly_report()
            state["last_weekly_forecast"] = week_key
        except Exception as exc:
            print("Weekly forecast image failed:", exc)

    status_due = True
    if state["last_status"]:
        try:
            last = datetime.fromisoformat(state["last_status"])
            status_due = (now - last).total_seconds() >= STATUS_EVERY_MIN * 60
        except ValueError:
            status_due = True

    if messages or status_due:
        try:
            messages.append(market_status())
            state["last_status"] = now.isoformat()
        except Exception as exc:
            print("Market status unavailable:", exc)

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if messages:
        send("\n\n".join(messages))
    print("\n\n".join(messages) if messages else "Nothing new to send.")


if __name__ == "__main__":
    main()
