"""Telegram chart reports: economic calendar, gold forecast and M15 analysis."""
import io
from datetime import datetime, timedelta, timezone
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
import yfinance as yf
from bot import SYMBOL, TOKEN, CHAT, CHATS, REPLY_TO, CAL_URLS

UTC = timezone.utc
BD = timezone(timedelta(hours=6))

def send_chart(fig, caption):
    if not TOKEN or not CHATS:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    image = buf.getvalue()
    # V2.11: manual reply goes only to the requester; auto runs broadcast.
    targets = [REPLY_TO] if REPLY_TO else CHATS
    errors = []
    for chat_id in targets:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": ("greatnews.png", io.BytesIO(image), "image/png")}, timeout=45
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("Telegram rejected chart image")
        except Exception as exc:
            errors.append(f"{chat_id}: {exc}")
    if errors and len(errors) == len(targets):
        raise RuntimeError("Telegram chart send failed for all chats: " + "; ".join(errors))

def get_calendar():
    errors = []
    for url in CAL_URLS:
        try:
            response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            events = response.json()
            if isinstance(events, list): return events
            raise ValueError("Invalid calendar response")
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("Economic calendar unavailable: " + " | ".join(errors[-2:]))

def calendar_report():
    now = datetime.now(UTC)
    rows = []
    for item in get_calendar():
        if item.get("country") != "USD": continue
        try:
            stamp = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=UTC)
            if now - timedelta(hours=2) <= stamp <= now + timedelta(days=7):
                rows.append((stamp, item))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    if not rows:
        from bot import send
        send("📅 USD Economic Calendar\nআগামী ৭ দিনের জন্য কোনো USD ইভেন্ট পাওয়া যায়নি।")
        return
    rows = rows[:20]
    colors = {"High": "#e74c3c", "Medium": "#f39c12", "Low": "#f1c40f", "Holiday": "#95a5a6"}
    fig, ax = plt.subplots(figsize=(11, max(4, len(rows) * .4)))
    ax.axis("off")
    cells = [[t.astimezone(BD).strftime("%a %d %b %I:%M %p"), str(e.get("title", "USD event"))[:45],
              str(e.get("impact", "Other")), str(e.get("forecast") or "—"), str(e.get("previous") or "—")] for t,e in rows]
    table = ax.table(cellText=cells, colLabels=["Bangladesh time", "USD event", "Impact", "Forecast", "Previous"],
                     cellLoc="left", loc="center", colWidths=[.23,.39,.13,.12,.12])
    table.auto_set_font_size(False); table.set_fontsize(8); table.scale(1, 1.5)
    for i, (_, event) in enumerate(rows, start=1):
        table[(i,2)].set_facecolor(colors.get(event.get("impact"), "#ecf0f1"))
    ax.set_title("USD ECONOMIC CALENDAR • NEXT 7 DAYS", fontsize=14, pad=18)
    fig.tight_layout()
    send_chart(fig, "📅 USD Economic Calendar — বাংলাদেশ সময়। লাল=High, কমলা=Medium, হলুদ=Low impact।")

def get_market(period, interval):
    data = yf.download(SYMBOL, period=period, interval=interval, progress=False, auto_adjust=False, threads=False)
    if data is None or data.empty: raise RuntimeError("Gold market data unavailable from Yahoo Finance")
    if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
    data = data.dropna().copy()
    close = data["Close"]
    data["ema20"] = close.ewm(span=20, adjust=False).mean()
    data["ema50"] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    data["rsi"] = 100 - 100/(1 + gain/loss.replace(0, float("nan")))
    tr = pd.concat([data["High"]-data["Low"], (data["High"]-close.shift()).abs(),
                    (data["Low"]-close.shift()).abs()], axis=1).max(axis=1)
    data["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    return data

def weekly_report():
    data = get_market("1y", "1d")
    row = data.iloc[-2]
    price, e20, e50, rsi = [float(row[k]) for k in ["Close","ema20","ema50","rsi"]]
    if e20 > e50 and price > e20: bias = "Bullish / ঊর্ধ্বমুখী"
    elif e20 < e50 and price < e20: bias = "Bearish / নিম্নমুখী"
    else: bias = "Neutral / Range"
    caption = ("📆 GOLD WEEKLY FORECAST\n" + f"সাপ্তাহিক bias: {bias}\nদাম: {price:.2f}\nEMA20: {e20:.2f} | EMA50: {e50:.2f}\nRSI: {rsi:.1f}\n" +
               "এটি দৈনিক চার্টভিত্তিক সাপ্তাহিক দিকনির্দেশ; নিশ্চিত পূর্বাভাস বা BUY/SELL signal নয়। Yahoo GC=F futures proxy, spot XAU/USD নয়।")
    tail = data.iloc[-120:]
    fig, ax = plt.subplots(figsize=(10,5))
    ax.plot(tail.index, tail["Close"], label="Gold futures proxy")
    ax.plot(tail.index, tail["ema20"], label="EMA20")
    ax.plot(tail.index, tail["ema50"], label="EMA50")
    ax.set_title("GOLD WEEKLY OUTLOOK"); ax.grid(alpha=.25); ax.legend(); fig.autofmt_xdate(); fig.tight_layout()
    send_chart(fig, caption)

def multi_timeframe_analysis():
    """Build an original H1/H4/D1 gold chart using completed candles only."""
    hourly = get_market("3mo", "1h")
    daily = get_market("1y", "1d")
    # Build 4-hour candles from hourly OHLC; use only completed resampled bars.
    four_hour = hourly[["Open", "High", "Low", "Close"]].resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()
    def enrich(frame):
        frame = frame.copy()
        close = frame["Close"]
        frame["ema20"] = close.ewm(span=20, adjust=False).mean()
        frame["ema50"] = close.ewm(span=50, adjust=False).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        frame["rsi"] = 100 - 100/(1 + gain/loss.replace(0, float("nan")))
        return frame.dropna()
    frames = [("1H", enrich(hourly).iloc[:-1]), ("4H", enrich(four_hour).iloc[:-1]), ("D1", enrich(daily).iloc[:-1])]
    if any(len(frame) < 25 for _, frame in frames):
        raise RuntimeError("Not enough completed candles for multi-timeframe analysis")
    # V2.9: structured per-timeframe details for the formatted caption.
    details = []
    fig, axes = plt.subplots(3, 1, figsize=(11, 12))
    for ax, (label, frame) in zip(axes, frames):
        row = frame.iloc[-1]
        price, e20, e50, rsi = [float(row[k]) for k in ("Close", "ema20", "ema50", "rsi")]
        if e20 > e50 and price > e20:
            bias = "Bullish"
        elif e20 < e50 and price < e20:
            bias = "Bearish"
        else:
            bias = "Neutral"
        recent = frame.iloc[-20:]
        support, resistance = float(recent["Low"].min()), float(recent["High"].max())
        details.append((label, bias, rsi, support, resistance))
        tail = frame.iloc[-(72 if label == "1H" else 60 if label == "4H" else 90):]
        ax.plot(tail.index, tail["Close"], label="Gold")
        ax.plot(tail.index, tail["ema20"], label="EMA20")
        ax.plot(tail.index, tail["ema50"], label="EMA50")
        ax.axhline(support, linestyle="--", alpha=.45, label="20-bar support")
        ax.axhline(resistance, linestyle="--", alpha=.45, label="20-bar resistance")
        ax.set_title(f"{label} • {bias} • RSI {rsi:.1f}")
        ax.grid(alpha=.25)
        ax.legend(loc="upper left", ncol=3, fontsize=8)
    fig.suptitle("GOLD MULTI-TIMEFRAME TECHNICAL ANALYSIS", fontsize=14)
    fig.tight_layout()
    # V2.9: formatted caption with overall bias + one block per timeframe.
    bias_icon = {"Bullish": "📈", "Bearish": "📉", "Neutral": "➖"}
    tf_icon = {"1H": "🕐", "4H": "🕓", "D1": "📅"}
    counts = {"Bullish": 0, "Bearish": 0, "Neutral": 0}
    for _, bias, _, _, _ in details:
        counts[bias] += 1
    majority = max(counts, key=lambda b: counts[b])
    if counts[majority] >= 2:
        overall = f"{bias_icon[majority]} Overall: {majority} ({counts[majority]}/3)"
    else:
        overall = ("➖ Overall: Mixed "
                   f"({counts['Bullish']} Bullish, {counts['Bearish']} Bearish, {counts['Neutral']} Neutral)")
    cap_lines = [
        "📊 GOLD DAILY ANALYSIS",
        f"🕐 {datetime.now(BD):%d %b %Y, %I:%M %p} বাংলাদেশ",
        "",
        overall,
        "",
    ]
    for label, bias, rsi, support, resistance in details:
        cap_lines += [
            f"{tf_icon.get(label, '🕐')} {label} — {bias_icon[bias]} {bias}",
            f"   RSI: {rsi:.1f}",
            f"   Support: {support:.2f} | Resistance: {resistance:.2f}",
            "",
        ]
    cap_lines.append("⚠️ এটা বিশ্লেষণ, BUY/SELL সিগন্যাল নয়। Support/Resistance সাম্প্রতিক ২০টি candle-এর range। Yahoo GC=F futures proxy; spot XAU/USD নয়।")
    caption = chr(10).join(cap_lines)
    send_chart(fig, caption)


def chart_report(command):
    if command == "analysis":
        multi_timeframe_analysis()
        return
    if command == "forecast": data = get_market("1mo", "1h")
    else: data = get_market("5d", "15m")
    row = data.iloc[-2]
    price, e20, e50, rsi, atr = [float(row[k]) for k in ["Close","ema20","ema50","rsi","atr"]]
    if e20 > e50 and price > e20: bias = "Bullish / ঊর্ধ্বমুখী"
    elif e20 < e50 and price < e20: bias = "Bearish / নিম্নমুখী"
    else: bias = "Neutral / Range"
    if command == "forecast":
        caption = ("🥇 GOLD DAILY FORECAST\n" + f"দিকনির্দেশ: {bias}\nদাম: {price:.2f}\nEMA20: {e20:.2f}\nEMA50: {e50:.2f}\nRSI: {rsi:.1f}\n" +
                   f"সময়: {datetime.now(BD):%d %b %Y, %I:%M %p} বাংলাদেশ\nএটি সূচকভিত্তিক সম্ভাব্য bias, নিশ্চিত ভবিষ্যদ্বাণী বা BUY/SELL signal নয়।\n" +
                   "ডেটা Yahoo GC=F futures proxy; spot XAU/USD নয়।")
        tail = data.iloc[-72:]; title = "GOLD DAILY DIRECTIONAL OUTLOOK"
    else:
        caption = ("📊 GOLD M15 MARKET ANALYSIS\n" + f"Bias: {bias}\nPrice: {price:.2f}\nEMA20: {e20:.2f} | EMA50: {e50:.2f}\n" +
                   f"RSI: {rsi:.1f} | ATR(14): {atr:.2f}\nশেষ সম্পূর্ণ M15 candle: {data.index[-2]}\n" +
                   "এটি বিশ্লেষণ, এন্ট্রি সিগন্যাল নয়। ডেটা Yahoo GC=F futures proxy; spot XAU/USD নয়।")
        tail = data.iloc[-80:]; title = "GOLD M15 TECHNICAL ANALYSIS"
    fig, ax = plt.subplots(figsize=(10,5))
    ax.plot(tail.index, tail["Close"], label="Gold futures proxy")
    ax.plot(tail.index, tail["ema20"], label="EMA20")
    ax.plot(tail.index, tail["ema50"], label="EMA50")
    ax.set_title(title); ax.grid(alpha=.25); ax.legend(); fig.autofmt_xdate(); fig.tight_layout()
    send_chart(fig, caption)

def run_report(command):
    try:
        if command == "calendar": calendar_report()
        elif command in ("forecast", "analysis"): chart_report(command)
        elif command == "weekly": weekly_report()
        else: raise ValueError("Unknown report command")
    except Exception as exc:
        from bot import send
        send(f"⚠️ /{command}: রিপোর্ট তৈরি করা যায়নি। কারণ: {str(exc)[:700]}")
        raise

if __name__ == "__main__":
    import sys
    run_report(sys.argv[1] if len(sys.argv) > 1 else "analysis")
