"""Gold/USD macro-news alerts and market status for Telegram.

News impact labels describe likely market relevance, NOT a guaranteed price direction
or a trading signal. Existing calendar, report and market-status entry points are kept.
"""
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
import yfinance as yf

from bot import SYMBOL, send
from reports import calendar_report, chart_report, weekly_report

# Existing calendar / automation settings
IMPACTS = {"High"}
CURRENCIES = {"USD"}
WARN_MINUTES = 60
STATUS_EVERY_MIN = 30
MAX_HEADLINES = 4
STATE_FILE = Path("news_state.json")
CAL_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]
CALENDAR_CACHE = Path("news_calendar_cache.json")
UTC = timezone.utc
BD = timezone(timedelta(hours=6))

# A mix of official US releases and a tightly-scoped gold/USD headline search.
# If one feed is unavailable, the others continue to work.
NEWS_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("US Bureau of Labor Statistics", "https://www.bls.gov/feed/bls_latest.rss"),
    ("Gold / USD market news", (
        "https://news.google.com/rss/search?q="
        "(gold+OR+XAUUSD+OR+bullion+OR+%22gold+prices%22)+"
        "(Fed+OR+inflation+OR+CPI+OR+jobs+OR+Treasury+OR+geopolitical+OR+ETF)"
        "+when:2d&hl=en-US&gl=US&ceid=US:en"
    )),
]
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GoldMarketNewsBot/1.0)"}
MAX_SEEN = 600
FRESH_MINUTES = 120
RELEVANT_TERMS = (
    "gold", "xau", "bullion", "precious metal", "federal reserve", "fed ",
    "interest rate", "rate cut", "rate hike", "inflation", "consumer price",
    "producer price", "cpi", "ppi", "payroll", "employment", "unemployment",
    "treasury yield", "treasury bond", "us dollar", "dollar index", "dxy",
    "central bank", "gold etf", "gold demand", "gold reserve", "geopolitical",
    "sanction", "war ", "conflict", "safe haven",
)
EXCLUDE_TERMS = (
    "golden globes", "golden boot", "golden state", "gold coin collection",
    "coin collecting", "jewelry design", "gold necklace", "wedding ring",
    "gold medal", "gold mining job",
)


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        state = {}
    return {
        "pre": state.get("pre", []),
        "now": state.get("now", []),
        "seen": state.get("seen", []),
        "seen_titles": state.get("seen_titles", []),
        "last_status": state.get("last_status", ""),
        "last_calendar_date": state.get("last_calendar_date", ""),
        "last_forecast_date": state.get("last_forecast_date", ""),
        "last_analysis_date": state.get("last_analysis_date", ""),
        "last_weekly_forecast": state.get("last_weekly_forecast", ""),
    }


def bd_time(value):
    return value.astimezone(BD).strftime("%d %b, %I:%M %p")


def parse_datetime(value, fallback=None):
    if not value:
        return fallback or datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return fallback or datetime.now(UTC)


def get_calendar_alerts(state, now):
    """Keep the original Forex Factory high-impact USD warning behavior."""
    alerts = []
    events = None
    for url in CAL_URLS:
        try:
            response = requests.get(url, timeout=12, headers=REQUEST_HEADERS)
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("Calendar response is not a list")
            CALENDAR_CACHE.write_text(
                json.dumps({"fetched_at": now.isoformat(), "events": events}, ensure_ascii=False),
                encoding="utf-8",
            )
            break
        except Exception as exc:
            print(f"Calendar feed failed ({url}): {exc}")

    if events is None:
        try:
            cached = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
            fetched = parse_datetime(cached.get("fetched_at"))
            age = (now - fetched).total_seconds()
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
            event_time = parse_datetime(event["date"])
            event_id = hashlib.sha256(
                (event.get("title", "") + event["date"]).encode("utf-8")
            ).hexdigest()[:16]
            minutes = (event_time - now).total_seconds() / 60
            details = (
                f"খবর: {translate_to_bengali(event.get('title', 'গুরুত্বপূর্ণ মার্কিন অর্থনৈতিক তথ্য'))}\n"
                f"সময়: {bd_time(event_time)} (বাংলাদেশ)\n"
                f"সম্ভাব্য ফল: {event.get('forecast') or 'তথ্য নেই'} | "
                f"আগের ফল: {event.get('previous') or 'তথ্য নেই'}"
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
    """Translate a short source title/snippet; fall back safely to original text."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "bn", "dt": "t", "q": text[:1100]},
            timeout=12,
            headers=REQUEST_HEADERS,
        )
        response.raise_for_status()
        parts = response.json()[0]
        translated = "".join(part[0] for part in parts if part and part[0])
        return translated.strip() or text
    except Exception as exc:
        print("Bengali translation unavailable:", exc)
        return text


def clean_html(value):
    value = html.unescape(value or "")
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def child_text(element, names):
    for name in names:
        found = element.find(name)
        if found is not None:
            value = "".join(found.itertext()).strip()
            if value:
                return value
    # Atom feeds use namespaced tags.
    for child in list(element):
        if child.tag.rsplit("}", 1)[-1].lower() in {n.lower() for n in names}:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def canonical_url(url):
    try:
        parts = urlsplit(url.strip())
        if not parts.scheme or not parts.netloc:
            return url.strip()
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    except Exception:
        return (url or "").strip()


def normalize_title(title):
    value = html.unescape(title or "").lower()
    value = re.sub(r"\s+-\s+[^-]{2,45}$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_relevant(title, description=""):
    text = f"{title} {description}".lower()
    if any(term in text for term in EXCLUDE_TERMS):
        return False
    return any(term in text for term in RELEVANT_TERMS)


def classify_impact(title, description=""):
    """Classify likely relevance to USD/gold, not the direction of price movement."""
    text = f"{title} {description}".lower()
    high = (
        "federal reserve", "fed rate", "interest rate decision", "rate decision",
        "fomc", "cpi", "consumer price index", "ppi", "nonfarm payroll",
        "payrolls", "unemployment rate", "powell", "rate cut", "rate hike",
        "emergency rate", "us inflation",
    )
    medium = (
        "treasury yield", "10-year yield", "dollar index", "dxy", "gold etf",
        "central bank", "gold reserve", "gold demand", "gold price", "bullion",
        "geopolitical", "sanction", "military conflict", "safe haven",
        "retail sales", "gdp", "jobless claims", "pce",
    )
    if any(term in text for term in high):
        return "High", "🔴"
    if any(term in text for term in medium):
        return "Medium", "🟠"
    return "Low", "🟡"


def market_context(title, description=""):
    """Give conditional context only; never turn a headline into a BUY/SELL call."""
    text = f"{title} {description}".lower()
    if any(k in text for k in ("inflation", "cpi", "ppi", "payroll", "employment", "unemployment", "fed", "fomc", "interest rate", "powell", "pce")):
        return (
            "USD ও মার্কিন বন্ডের ইয়েল্ডের প্রতিক্রিয়া গুরুত্বপূর্ণ। "
            "ইয়েল্ড/ডলার বাড়লে সাধারণত সোনায় চাপ পড়ে; কমলে সোনা সহায়তা পেতে পারে। "
            "প্রকৃত প্রতিক্রিয়া তথ্যের প্রত্যাশার তুলনায় ফলাফলের ওপর নির্ভর করবে।"
        )
    if any(k in text for k in ("treasury", "yield", "dollar index", "dxy", "us dollar")):
        return (
            "ডলার ও Treasury yield-এর দিক দেখো। সাধারণত ডলার বা ইয়েল্ড শক্তিশালী হলে "
            "সোনায় চাপ এবং দুর্বল হলে সোনায় সহায়তা দেখা যেতে পারে।"
        )
    if any(k in text for k in ("war", "conflict", "attack", "geopolitical", "sanction", "safe haven")):
        return (
            "ভূরাজনৈতিক ঝুঁকি বাড়লে নিরাপদ সম্পদ হিসেবে সোনার চাহিদা বাড়তে পারে; "
            "তবে ডলার ও ইয়েল্ডের বিপরীত প্রতিক্রিয়ায় দাম কমতেও পারে।"
        )
    if any(k in text for k in ("central bank", "gold reserve", "gold demand", "gold etf", "bullion")):
        return (
            "কেন্দ্রীয় ব্যাংক/ETF-এর কেনাবেচা ও বিনিয়োগ চাহিদা সোনার বাজারে প্রভাব ফেলতে পারে। "
            "খবরটির পরিমাণ ও বাজারের প্রত্যাশা যাচাই করা জরুরি।"
        )
    return "খবরটি সোনার বাজারের সঙ্গে সম্পর্কিত, তবে শুধু এই শিরোনাম থেকে দামের দিক নিশ্চিত করা যায় না।"


def fetch_feed(source, url):
    response = requests.get(url, timeout=18, headers=REQUEST_HEADERS)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries = root.findall(".//item")
    if not entries:
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "entry"]
    results = []
    for entry in entries:
        title = clean_html(child_text(entry, ["title"]))
        link = child_text(entry, ["link"])
        if not link:
            for node in list(entry):
                if node.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = node.attrib.get("href", "")
                    if link:
                        break
        description = clean_html(child_text(entry, ["description", "summary", "content:encoded", "content"]))
        published = child_text(entry, ["pubDate", "published", "updated", "dc:date"])
        if title and link:
            results.append({
                "title": title,
                "link": link.strip(),
                "description": description,
                "published": parse_datetime(published),
                "source": source,
            })
    return results


def already_seen(item, state):
    link = canonical_url(item["link"])
    title = normalize_title(item["title"])
    identity = hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:20]
    if identity in state["seen"]:
        return True
    # Catch syndicated copies with slightly different punctuation/source suffixes.
    for old in state["seen_titles"][-160:]:
        if title and SequenceMatcher(None, title, old).ratio() >= 0.88:
            return True
    return False


def get_headline_alerts(state, now):
    alerts = []
    items = []
    for source, url in NEWS_FEEDS:
        try:
            batch = fetch_feed(source, url)
            items.extend(batch)
            print(f"News feed OK: {source} ({len(batch)} items)")
        except Exception as exc:
            print(f"News feed failed ({source}): {exc}")

    if not items:
        print("No news feeds returned items.")
        return alerts

    # New installations should not flood Telegram with old headlines on first run.
    first_run = not state["seen"] and not state["seen_titles"]
    candidates = []
    unique_links = set()
    for item in sorted(items, key=lambda x: x["published"], reverse=True):
        title = item["title"]
        desc = item["description"]
        if not is_relevant(title, desc):
            continue
        link_key = canonical_url(item["link"])
        if link_key in unique_links:
            continue
        unique_links.add(link_key)
        if already_seen(item, state):
            continue
        # Mark examined items as seen, including old ones, so they are not replayed.
        identity = hashlib.sha256((link_key or normalize_title(title)).encode("utf-8")).hexdigest()[:20]
        state["seen"].append(identity)
        state["seen_titles"].append(normalize_title(title))
        age = now - item["published"]
        if not first_run and timedelta(0) <= age <= timedelta(minutes=FRESH_MINUTES):
            candidates.append(item)

    state["seen"] = state["seen"][-MAX_SEEN:]
    state["seen_titles"] = state["seen_titles"][-160:]
    for item in candidates[:MAX_HEADLINES]:
        title = item["title"]
        description = item["description"]
        impact, marker = classify_impact(title, description)
        bn_title = translate_to_bengali(title)
        # RSS summary is translated as a short factual excerpt, not invented article content.
        excerpt = description[:700].strip()
        bn_summary = translate_to_bengali(excerpt) if excerpt else (
            "প্রকাশিত শিরোনাম অনুযায়ী: " + bn_title
        )
        message = (
            f"{marker} {impact.upper()} IMPACT — GOLD / USD NEWS\n"
            f"📰 {bn_title}\n\n"
            f"📝 বাংলা সারাংশ:\n{bn_summary}\n\n"
            f"📊 USD / XAUUSD প্রভাব:\n{market_context(title, description)}\n\n"
            f"🕒 {bd_time(item['published'])} (বাংলাদেশ)\n"
            f"источник: {item['source']}\n"
            f"🔗 মূল প্রতিবেদন: {item['link']}\n\n"
            "⚠️ Impact মানে সম্ভাব্য বাজার-গুরুত্ব; এটি BUY/SELL সিগন্যাল নয়।"
        )
        # Replace the Russian source label with a Bengali label.
        message = message.replace("источник:", "উৎস:")
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

    # Keep the existing 06:00 Bangladesh-time report schedule unchanged.
    if bd_now.hour == 6 and bd_now.minute < 10:
        if state["last_forecast_date"] != today:
            try:
                chart_report("forecast")
                state["last_forecast_date"] = today
            except Exception as exc:
                print("Daily forecast image failed:", exc)
        if state["last_analysis_date"] != today:
            try:
                chart_report("analysis")
                state["last_analysis_date"] = today
            except Exception as exc:
                print("Daily analysis image failed:", exc)

    if bd_now.weekday() == 6 and bd_now.hour == 6 and bd_now.minute < 10 and state["last_weekly_forecast"] != week_key:
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
