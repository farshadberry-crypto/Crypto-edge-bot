
import os
import io
import json
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE SCALP ENGINE
# Spot + Futures | 5m entry confirmation | 15m trend
# Public Bybit market data | Telegram chart signals
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

BASE_URL = "https://api.bybit.com"
STATE_FILE = "scalp_signals.json"

SCAN_LIMIT = 30
MAX_SIGNALS_PER_MARKET = 2
MIN_SPOT_TURNOVER = 10_000_000
MIN_FUTURES_TURNOVER = 20_000_000

VOLUME_RATIO_MIN = 1.15
MIN_SCORE = 5
STOP_ATR_MULTIPLIER = 1.4

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoEdge-Scalp/1.0"})


# ------------------------- UTILITIES ----------------------

def utc_now():
    return datetime.now(timezone.utc)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass

    return {"sent": []}


def save_state(state):
    state["sent"] = state.get("sent", [])[-1000:]
    temp = STATE_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, STATE_FILE)


def api_get(path, params):
    response = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit API error: {payload.get('retMsg', 'unknown')}"
        )

    return payload.get("result", {})


def get_tickers(category):
    result = api_get(
        "/v5/market/tickers",
        {"category": category},
    )
    return result.get("list", [])


def get_klines(category, symbol, interval, limit=100):
    result = api_get(
        "/v5/market/kline",
        {
            "category": category,
            "symbol": symbol,
            "interval": str(interval),
            "limit": limit,
        },
    )

    rows = result.get("list", [])

    # Bybit returns newest candles first.
    rows.reverse()

    candles = []
    for row in rows:
        try:
            candles.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6]),
            })
        except (IndexError, TypeError, ValueError):
            continue

    return candles


# ---------------------- INDICATORS ------------------------

def ema(values, period):
    if len(values) < period:
        return []

    alpha = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append(
            alpha * value + (1 - alpha) * result[-1]
        )

    return [None] * (period - 1) + result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    return sum(true_ranges[-period:]) / period


def volume_ratio(candles, period=20):
    # Use the last fully closed candle, not the live candle.
    if len(candles) < period + 2:
        return None

    volumes = [c["volume"] for c in candles]
    previous = volumes[-period - 1:-1]

    average = sum(previous) / len(previous)
    if average <= 0:
        return None

    return volumes[-1] / average


def analyze(candles):
    if len(candles) < 60:
        return None

    # The last returned candle may still be forming.
    closed = candles[:-1]

    closes = [c["close"] for c in closed]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    current_rsi = rsi(closes)
    current_atr = atr(closed)
    vol_ratio = volume_ratio(closed)

    if (
        not e9 or not e21
        or e9[-1] is None or e21[-1] is None
        or current_rsi is None
        or current_atr is None
        or vol_ratio is None
        or current_atr <= 0
    ):
        return None

    previous = closed[-2]
    latest = closed[-1]

    return {
        "candles": closed,
        "close": latest["close"],
        "time": latest["time"],
        "ema9": e9[-1],
        "ema21": e21[-1],
        "rsi": current_rsi,
        "atr": current_atr,
        "volume_ratio": vol_ratio,
        "previous_high": previous["high"],
        "previous_low": previous["low"],
    }


# --------------------- SIGNAL LOGIC -----------------------

def build_signal(category, symbol, candles5, candles15):
    five = analyze(candles5)
    fifteen = analyze(candles15)

    if not five or not fifteen:
        return None

    price = five["close"]
    atr_value = five["atr"]
    ratio = five["volume_ratio"]
    rsi_value = five["rsi"]

    # Trend must agree on both timeframes.
    long_trend = (
        fifteen["ema9"] > fifteen["ema21"]
        and five["ema9"] > five["ema21"]
        and price > five["ema21"]
    )

    short_trend = (
        fifteen["ema9"] < fifteen["ema21"]
        and five["ema9"] < five["ema21"]
        and price < five["ema21"]
    )

    long_breakout = price > five["previous_high"]
    short_breakout = price < five["previous_low"]

    long_rsi_ok = 52 <= rsi_value <= 70
    short_rsi_ok = 30 <= rsi_value <= 48
    volume_ok = ratio >= VOLUME_RATIO_MIN

    candidates = []

    if long_trend:
        score = 2
        evidence = [
            "15m and 5m EMA trend agree bullish",
            "Price is above 5m EMA21",
        ]

        if long_breakout:
            score += 1
            evidence.append("Closed candle broke previous high")

        if long_rsi_ok:
            score += 1
            evidence.append(f"RSI confirmation: {rsi_value:.1f}")

        if volume_ok:
            score += 1
            evidence.append(f"Volume ratio: {ratio:.2f}x")

        candidates.append(("LONG", score, evidence))

    # Spot signals are long-only.
    if category == "linear" and short_trend:
        score = 2
        evidence = [
            "15m and 5m EMA trend agree bearish",
            "Price is below 5m EMA21",
        ]

        if short_breakout:
            score += 1
            evidence.append("Closed candle broke previous low")

        if short_rsi_ok:
            score += 1
            evidence.append(f"RSI confirmation: {rsi_value:.1f}")

        if volume_ok:
            score += 1
            evidence.append(f"Volume ratio: {ratio:.2f}x")

        candidates.append(("SHORT", score, evidence))

    valid = [
        item for item in candidates
        if item[1] >= MIN_SCORE
    ]

    if not valid:
        return None

    direction, score, evidence = max(
        valid, key=lambda item: item[1]
    )

    # Initial entry reference: last fully closed 5m candle.
    entry = price
    risk_distance = atr_value * STOP_ATR_MULTIPLIER

    if direction == "LONG":
        stop = entry - risk_distance
        targets = [
            entry + risk_distance * 1.0,
            entry + risk_distance * 1.8,
            entry + risk_distance * 2.6,
        ]
    else:
        stop = entry + risk_distance
        targets = [
            entry - risk_distance * 1.0,
            entry - risk_distance * 1.8,
            entry - risk_distance * 2.6,
        ]

    # A unique signal ID is tied to the closed 5m candle.
    signal_id = (
        f"{category}:{symbol}:{direction}:{five['time']}"
    )

    return {
        "signal_id": signal_id,
        "market": "SPOT" if category == "spot" else "FUTURES",
        "category": category,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "score": score,
        "rsi": rsi_value,
        "atr": atr_value,
        "volume_ratio": ratio,
        "timeframe": "5m entry / 15m trend",
        "candle_time": five["time"],
        "evidence": evidence,
        "candles": five["candles"][-60:],
        "created_at": utc_now().isoformat(),
    }


# --------------------- CHART IMAGE ------------------------

def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.7f}"


def create_chart(signal):
    candles = signal["candles"]
    fig, ax = plt.subplots(figsize=(12, 7))

    fig.patch.set_facecolor("#0b1020")
    ax.set_facecolor("#111827")

    for index, candle in enumerate(candles):
        rising = candle["close"] >= candle["open"]
        color = "#22c55e" if rising else "#ef4444"

        ax.vlines(
            index,
            candle["low"],
            candle["high"],
            color=color,
            linewidth=1,
        )

        bottom = min(candle["open"], candle["close"])
        height = abs(candle["close"] - candle["open"])
        minimum_height = max(candle["close"] * 0.00001, 1e-10)

        ax.bar(
            index,
            max(height, minimum_height),
            bottom=bottom,
            width=0.65,
            color=color,
            edgecolor=color,
        )

    levels = [
        ("ENTRY", signal["entry"], "#38bdf8"),
        ("STOP LOSS", signal["stop_loss"], "#ef4444"),
        ("TP1", signal["targets"][0], "#a3e635"),
        ("TP2", signal["targets"][1], "#4ade80"),
        ("TP3", signal["targets"][2], "#22c55e"),
    ]

    for label, price, color in levels:
        ax.axhline(
            price,
            color=color,
            linestyle="--",
            linewidth=1.2,
        )
        ax.text(
            len(candles) - 1,
            price,
            f" {label}: {format_price(price)}",
            color=color,
            fontsize=8,
            va="bottom",
            ha="right",
        )

    ax.set_title(
        f"CRYPTO EDGE | {signal['market']} "
        f"{signal['symbol']} {signal['direction']}\n"
        f"5m entry / 15m trend | Score {signal['score']}/5",
        color="white",
        fontsize=13,
    )

    ax.tick_params(colors="#cbd5e1")
    for spine in ax.spines.values():
        spine.set_color("#374151")

    ax.grid(alpha=0.15, color="#94a3b8")
    fig.tight_layout()

    output = io.BytesIO()
    fig.savefig(
        output,
        format="png",
        dpi=140,
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
    output.seek(0)
    return output


# -------------------- TELEGRAM SENDER ---------------------

def send_signal(signal):
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN.")

    chart = create_chart(signal)
    symbol = signal["symbol"]
    direction = signal["direction"]
    market = signal["market"]

    evidence_text = "\n".join(
        f"• {item}" for item in signal["evidence"]
    )

    caption = (
        f"⚡ CRYPTO EDGE | SCALP SIGNAL\n\n"
        f"🏦 Market: {market}\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {direction}\n"
        f"⏱ Timeframe: {signal['timeframe']}\n\n"
        f"🎯 Entry reference: {format_price(signal['entry'])}\n"
        f"🛑 Stop Loss: {format_price(signal['stop_loss'])}\n"
        f"💰 TP1: {format_price(signal['targets'][0])}\n"
        f"💰 TP2: {format_price(signal['targets'][1])}\n"
        f"💰 TP3: {format_price(signal['targets'][2])}\n\n"
        f"📊 Setup score: {signal['score']}/5\n"
        f"📉 RSI: {signal['rsi']:.1f}\n"
        f"📦 Volume ratio: {signal['volume_ratio']:.2f}x\n\n"
        f"🔍 WHY THIS SETUP?\n{evidence_text}\n\n"
        f"⚠️ SIGNAL ONLY: entry is not yet confirmed.\n"
        f"Do not treat this post as a trade-opened alert.\n"
        f"Use risk controls. No outcome is guaranteed.\n\n"
        f"🆔 {signal['signal_id']}"
    )

    url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    )

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "caption": caption[:1024],
        },
        files={
            "photo": (
                f"{symbol}_scalp.png",
                chart,
                "image/png",
            )
        },
        timeout=40,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram error: {result.get('description')}"
        )

    print(f"Published {signal['signal_id']}")


# --------------------- MARKET SCANNER ---------------------

def get_candidates(category):
    tickers = get_tickers(category)

    minimum = (
        MIN_SPOT_TURNOVER
        if category == "spot"
        else MIN_FUTURES_TURNOVER
    )

    candidates = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        if ticker.get("symbol", "").endswith("USDC"):
            continue

        try:
            turnover = float(ticker.get("turnover24h", 0))
        except (TypeError, ValueError):
            continue

        if turnover < minimum:
            continue

        candidates.append((turnover, symbol))

    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[:SCAN_LIMIT]]


def main():
    print("Starting Crypto Edge Scalp Engine")
    print(f"UTC: {utc_now().isoformat()}")

    state = load_state()
    sent = set(state.get("sent", []))
    published_by_market = {"spot": 0, "linear": 0}

    for category in ("spot", "linear"):
        try:
            symbols = get_candidates(category)
        except Exception as exc:
            print(f"Could not load {category} tickers: {exc}")
            continue

        print(f"{category}: scanning {len(symbols)} symbols")

        for symbol in symbols:
            if published_by_market[category] >= MAX_SIGNALS_PER_MARKET:
                break

            try:
                candles5 = get_klines(category, symbol, "5", 100)
                candles15 = get_klines(category, symbol, "15", 100)

                signal = build_signal(
                    category,
                    symbol,
                    candles5,
                    candles15,
                )

                if not signal:
                    continue

                signal_id = signal["signal_id"]

                if signal_id in sent:
                    continue

                # Save the ID only after Telegram confirms success.
                send_signal(signal)

                sent.add(signal_id)
                state["sent"] = list(sent)
                save_state(state)

                published_by_market[category] += 1
                time.sleep(1)

            except Exception as exc:
                print(f"{category} {symbol} failed: {exc}")

            time.sleep(0.15)

    print("Scan completed.")
    print(f"Spot signals: {published_by_market['spot']}")
    print(f"Futures signals: {published_by_market['linear']}")


if __name__ == "__main__":
    main()
