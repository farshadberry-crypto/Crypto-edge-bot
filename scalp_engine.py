
import os
import json
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE FUTURES SCALP ENGINE
# Kraken Futures only
# 5m entries / 15m trend
# Paper signals only. No real orders are placed.
# =========================================================

FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"

STATE_FILE = "scalp_signals.json"
CHART_DIR = "signal_charts"

SCAN_LIMIT = 12
MAX_SIGNALS_PER_RUN = 2

VOLUME_RATIO_MIN = 1.15
MIN_SCORE = 5
STOP_ATR_MULTIPLIER = 1.4

# Leverage is a suggestion, not an order.
MAX_LEVERAGE = 5.0
MIN_LEVERAGE = 1.0

# Target: approximately 1% of allocated margin at stop,
# before fees, slippage, funding, and liquidation effects.
TARGET_MARGIN_RISK_PERCENT = 1.0

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-FuturesScalpEngine/3.0",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, params=None):
    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response format.")

    return payload


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            data.setdefault("sent", [])
            data.setdefault("signals", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass

    return {"sent": [], "signals": []}


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, STATE_FILE)


def telegram_send_photo(path, caption):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

    if not token:
        print("TELEGRAM_BOT_TOKEN is missing.")
        return False

    try:
        with open(path, "rb") as photo:
            response = SESSION.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                },
                files={"photo": photo},
                timeout=30,
            )

        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            print("Telegram photo rejected:", payload)
            return False

        return True

    except (requests.RequestException, OSError) as exc:
        print("Telegram photo error:", exc)
        return False


def fmt(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.7f}"


def fetch_futures_universe():
    payload = get_json(f"{FUTURES_API}/tickers")

    if payload.get("result") != "success":
        raise RuntimeError("Kraken Futures ticker error: " + str(payload))

    ranked = []

    for ticker in payload.get("tickers", []):
        symbol = ticker.get("symbol", "")

        # Kraken perpetual and dated futures contracts.
        if not symbol.startswith(("PF_", "PI_")):
            continue

        try:
            price = float(
                ticker.get("last")
                or ticker.get("markPrice")
                or 0
            )
        except (TypeError, ValueError):
            continue

        raw_volume = (
            ticker.get("vol24h")
            or ticker.get("volume24h")
            or ticker.get("volume")
            or 0
        )

        try:
            volume = float(raw_volume)
        except (TypeError, ValueError):
            volume = 0.0

        if price <= 0:
            continue

        ranked.append({
            "symbol": symbol,
            "exchange_symbol": symbol,
            "last_price": price,
            "reported_volume": volume,
        })

    # Kraken volume fields can differ by contract.
    # This is only a ranking heuristic, not USD turnover.
    ranked.sort(
        key=lambda item: item["reported_volume"],
        reverse=True,
    )

    if not ranked:
        raise RuntimeError("No eligible Kraken Futures contracts returned.")

    return ranked[:SCAN_LIMIT]


def fetch_futures_candles(symbol, resolution):
    payload = get_json(
        f"{FUTURES_CHARTS}/trade/{symbol}/{resolution}",
        {"count": 100},
    )

    raw = payload.get("candles", [])
    candles = []

    for row in raw:
        try:
            candle_time = int(row["time"])

            # Kraken chart timestamps are normally milliseconds.
            if candle_time > 10_000_000_000:
                candle_time //= 1000

            candles.append({
                "time": candle_time,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda candle: candle["time"])

    if len(candles) < 30:
        raise RuntimeError(f"Not enough Futures candles for {symbol}")

    now = int(time.time())
    interval_seconds = {
        "5m": 300,
        "15m": 900,
    }.get(resolution, 300)

    # Only closed candles may generate signals.
    candles = [
        candle for candle in candles
        if candle["time"] + interval_seconds <= now
    ]

    if len(candles) < 30:
        raise RuntimeError(f"Not enough closed Futures candles for {symbol}")

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    value = sum(values[:period]) / period

    for current in values[period:]:
        value = (current - value) * multiplier + value

    return value


def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return None

    gains = []
    losses = []

    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=14):
    if len(candles) <= period:
        return None

    true_ranges = []

    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["close"]

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    if len(true_ranges) < period:
        return None

    atr_value = sum(true_ranges[:period]) / period

    for current in true_ranges[period:]:
        atr_value = (
            atr_value * (period - 1) + current
        ) / period

    return atr_value


def calculate_leverage(entry, stop):
    if entry <= 0:
        return None, None, None

    stop_distance_percent = abs(entry - stop) / entry * 100

    if stop_distance_percent <= 0:
        return None, None, None

    # If the stop is 0.25% away, 1% / 0.25% = 4x.
    # A 5x hard cap avoids recommending higher leverage.
    raw_leverage = TARGET_MARGIN_RISK_PERCENT / stop_distance_percent

    leverage = max(
        MIN_LEVERAGE,
        min(MAX_LEVERAGE, raw_leverage),
    )

    estimated_margin_loss_percent = (
        stop_distance_percent * leverage
    )

    return (
        round(leverage, 2),
        round(stop_distance_percent, 3),
        round(estimated_margin_loss_percent, 3),
    )


def evaluate_signal(symbol, exchange_symbol, candles_5m, candles_15m):
    closes = [candle["close"] for candle in candles_5m]
    volumes = [candle["volume"] for candle in candles_5m]
    closes_15m = [candle["close"] for candle in candles_15m]

    if len(closes) < 30 or len(closes_15m) < 30:
        return None

    price = closes[-1]
    previous_price = closes[-2]

    ema_fast = ema(closes_15m, 9)
    ema_slow = ema(closes_15m, 21)
    rsi = calculate_rsi(closes, 14)
    atr = calculate_atr(candles_5m, 14)

    if any(value is None for value in (ema_fast, ema_slow, rsi, atr)):
        return None

    average_volume = sum(volumes[-21:-1]) / 20

    if average_volume <= 0 or atr <= 0 or price <= 0:
        return None

    volume_ratio = volumes[-1] / average_volume

    recent_high = max(
        candle["high"] for candle in candles_5m[-21:-1]
    )
    recent_low = min(
        candle["low"] for candle in candles_5m[-21:-1]
    )

    bullish_trend = ema_fast > ema_slow
    bearish_trend = ema_fast < ema_slow
    breakout_up = price > recent_high
    breakout_down = price < recent_low
    momentum_up = price > previous_price
    momentum_down = price < previous_price

    long_score = 0
    short_score = 0
    evidence_long = []
    evidence_short = []

    if bullish_trend:
        long_score += 2
        evidence_long.append("15m EMA trend is bullish")

    if bearish_trend:
        short_score += 2
        evidence_short.append("15m EMA trend is bearish")

    if 50 <= rsi <= 70:
        long_score += 1
        evidence_long.append(
            f"5m RSI supports bullish momentum ({rsi:.1f})"
        )

    if 30 <= rsi <= 50:
        short_score += 1
        evidence_short.append(
            f"5m RSI supports bearish momentum ({rsi:.1f})"
        )

    if volume_ratio >= VOLUME_RATIO_MIN:
        long_score += 1
        short_score += 1
        evidence_long.append(
            f"Volume is {volume_ratio:.2f}x its previous 20-candle average"
        )
        evidence_short.append(
            f"Volume is {volume_ratio:.2f}x its previous 20-candle average"
        )

    if breakout_up:
        long_score += 2
        evidence_long.append(
            "Price broke above the previous 20 closed 5m candles"
        )

    if breakout_down:
        short_score += 2
        evidence_short.append(
            "Price broke below the previous 20 closed 5m candles"
        )

    if momentum_up:
        long_score += 1
        evidence_long.append(
            "Latest closed 5m candle closed above the prior close"
        )

    if momentum_down:
        short_score += 1
        evidence_short.append(
            "Latest closed 5m candle closed below the prior close"
        )

    candidates = []

    if long_score >= MIN_SCORE and bullish_trend:
        candidates.append(("LONG", long_score, evidence_long))

    if short_score >= MIN_SCORE and bearish_trend:
        candidates.append(("SHORT", short_score, evidence_short))

    if not candidates:
        return None

    direction, score, evidence = max(
        candidates,
        key=lambda candidate: candidate[1],
    )

    stop_distance = atr * STOP_ATR_MULTIPLIER

    if direction == "LONG":
        stop = price - stop_distance
        risk = price - stop
        targets = [
            price + risk,
            price + risk * 1.8,
            price + risk * 2.6,
        ]
    else:
        stop = price + stop_distance
        risk = stop - price
        targets = [
            price - risk,
            price - risk * 1.8,
            price - risk * 2.6,
        ]

    if stop <= 0 or any(target <= 0 for target in targets):
        return None

    leverage, stop_percent, margin_loss_percent = calculate_leverage(
        price, stop
    )

    if leverage is None:
        return None

    signal_id = (
        f"kraken-linear-{symbol}-{direction}-"
        f"{candles_5m[-1]['time']}"
    )

    return {
        "signal_id": signal_id,
        "exchange": "kraken",
        "exchange_symbol": exchange_symbol,
        "market_type": "futures",
        "market": "FUTURES",
        "category": "linear",
        "symbol": symbol,
        "direction": direction,
        "entry": price,
        "stop_loss": stop,
        "targets": targets,
        "score": score,
        "rsi": round(rsi, 2),
        "atr": atr,
        "volume_ratio": round(volume_ratio, 3),
        "leverage": leverage,
        "stop_distance_percent": stop_percent,
        "estimated_margin_loss_percent": margin_loss_percent,
        "timeframe": "5m entry / 15m trend",
        "candle_time": candles_5m[-1]["time"],
        "evidence": evidence,
        "candles": candles_5m[-60:],
        "created_at": now_utc(),
    }


def create_chart(signal):
    os.makedirs(CHART_DIR, exist_ok=True)

    candles = signal["candles"]
    times = list(range(len(candles)))
    closes = [candle["close"] for candle in candles]
    highs = [candle["high"] for candle in candles]
    lows = [candle["low"] for candle in candles]

    fig, ax = plt.subplots(figsize=(12, 7))

    ax.plot(times, closes, linewidth=1.7, label="Close")
    ax.fill_between(
        times, lows, highs, alpha=0.12, label="Candle range"
    )

    ax.axhline(
        signal["entry"],
        linestyle="--",
        linewidth=1.5,
        label="Entry",
    )
    ax.axhline(
        signal["stop_loss"],
        linestyle="--",
        linewidth=1.5,
        label="Stop Loss",
    )

    for index, target in enumerate(signal["targets"], start=1):
        ax.axhline(
            target,
            linestyle=":",
            linewidth=1.3,
            label=f"TP{index}",
        )

    ax.set_title(
        f"CRYPTO EDGE | FUTURES | "
        f"{signal['symbol']} | {signal['direction']} | "
        f"{signal['leverage']}x"
    )
    ax.set_xlabel("Closed 5-minute candles")
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()

    safe_symbol = (
        signal["symbol"]
        .replace("/", "_")
        .replace(":", "_")
    )
    path = os.path.join(
        CHART_DIR,
        f"{safe_symbol}_{signal['candle_time']}.png",
    )

    fig.savefig(path, dpi=140)
    plt.close(fig)

    return path


def build_caption(signal):
    targets = signal["targets"]
    evidence = "\n".join(
        f"• {item}" for item in signal["evidence"]
    )

    return (
        "🚨 CRYPTO EDGE | FUTURES SCALP\n\n"
        f"🪙 Contract: {signal['symbol']}\n"
        f"📈 Direction: {signal['direction']}\n"
        f"⏱ Timeframe: {signal['timeframe']}\n\n"
        f"🎯 Reference entry: {fmt(signal['entry'])}\n"
        f"🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
        f"💰 TP1: {fmt(targets[0])}\n"
        f"💰 TP2: {fmt(targets[1])}\n"
        f"💰 TP3: {fmt(targets[2])}\n\n"
        f"⚙️ Suggested leverage: {signal['leverage']}x "
        f"(maximum {MAX_LEVERAGE:.0f}x)\n"
        f"📏 Price distance to SL: "
        f"{signal['stop_distance_percent']}%\n"
        f"🧮 Estimated loss on allocated margin at SL: "
        f"about {signal['estimated_margin_loss_percent']}%\n"
        "Excludes fees, slippage, funding and liquidation effects.\n\n"
        f"📊 Signal score: {signal['score']}\n"
        f"📉 RSI: {signal['rsi']}\n"
        f"📦 Volume ratio: {signal['volume_ratio']}x\n\n"
        f"🔎 Evidence:\n{evidence}\n\n"
        "🟡 STATUS: WAITING FOR ENTRY\n"
        "Reference signal only. Entry is not a confirmed fill.\n"
        "Paper tracking only. No real order has been placed."
    )


def scan_market(instrument):
    symbol = instrument["symbol"]
    exchange_symbol = instrument["exchange_symbol"]

    try:
        candles_5m = fetch_futures_candles(exchange_symbol, "5m")
        time.sleep(0.25)
        candles_15m = fetch_futures_candles(exchange_symbol, "15m")

        return evaluate_signal(
            symbol=symbol,
            exchange_symbol=exchange_symbol,
            candles_5m=candles_5m,
            candles_15m=candles_15m,
        )

    except Exception as exc:
        print(f"Scan failed: FUTURES {symbol}: {exc}")
        return None


def main():
    print("Starting Crypto Edge Kraken Futures Scalp Engine")
    print("UTC:", now_utc())
    print("Market mode: FUTURES ONLY")
    print(
        f"Suggested leverage: {MIN_LEVERAGE:.0f}x to "
        f"{MAX_LEVERAGE:.0f}x"
    )

    state = load_state()
    sent_ids = set(state.get("sent", []))
    saved_signals = state.get("signals", [])

    if not isinstance(saved_signals, list):
        saved_signals = []

    try:
        universe = fetch_futures_universe()
        print("Eligible Futures contracts:", len(universe))
    except Exception as exc:
        print("Could not load Kraken Futures markets:", exc)
        raise RuntimeError("Kraken Futures market loading failed.") from exc

    attempted = 0
    sent_count = 0

    for instrument in universe:
        attempted += 1
        signal = scan_market(instrument)

        if signal is None:
            time.sleep(0.25)
            continue

        if signal["signal_id"] in sent_ids:
            continue

        chart_path = create_chart(signal)
        caption = build_caption(signal)

        # Record only after Telegram confirms photo delivery.
        if telegram_send_photo(chart_path, caption):
            sent_ids.add(signal["signal_id"])

            stored = {
                key: value
                for key, value in signal.items()
                if key != "candles"
            }

            saved_signals.append(stored)
            sent_count += 1

            print(
                f"Signal sent: FUTURES {signal['symbol']} "
                f"{signal['direction']} score={signal['score']} "
                f"leverage={signal['leverage']}x"
            )

        time.sleep(0.35)

        if sent_count >= MAX_SIGNALS_PER_RUN:
            break

    state["sent"] = list(sent_ids)[-5000:]
    state["signals"] = saved_signals[-1000:]
    state["updated_at"] = now_utc()
    save_state(state)

    print("Markets attempted:", attempted)
    print("Signals sent:", sent_count)
    print("Kraken Futures scan completed.")


if __name__ == "__main__":
    main()
