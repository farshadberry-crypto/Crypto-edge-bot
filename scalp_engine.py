
import os
import json
import time
import uuid
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE SCALP ENGINE
# Kraken Spot + Kraken Futures
# 5m entries / 15m trend
# Paper signals only. No real orders are placed.
# =========================================================

SPOT_API = "https://api.kraken.com/0/public"
FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"

STATE_FILE = "scalp_signals.json"
CHART_DIR = "signal_charts"

SCAN_LIMIT = 12
MAX_SIGNALS_PER_MARKET = 2

MIN_SPOT_TURNOVER = 10_000_000
MIN_FUTURES_TURNOVER = 5_000_000

VOLUME_RATIO_MIN = 1.15
MIN_SCORE = 5
STOP_ATR_MULTIPLIER = 1.4

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-ScalpEngine/2.0",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, params=None):
    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


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


def telegram_send_message(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

    if not token:
        print("TELEGRAM_BOT_TOKEN is missing.")
        return False

    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            print("Telegram message rejected:", payload)
            return False

        return True

    except requests.RequestException as exc:
        print("Telegram message error:", exc)
        return False


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


def fetch_spot_universe():
    payload = get_json(f"{SPOT_API}/AssetPairs")

    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))

    pairs = payload.get("result", {})
    candidates = []

    for internal_name, info in pairs.items():
        status = info.get("status", "online")
        quote = str(info.get("quote", "")).upper()
        wsname = info.get("wsname", "")
        altname = info.get("altname", internal_name)

        if status != "online":
            continue

        # Keep USD-quoted crypto markets to compare turnover consistently.
        if quote not in ("ZUSD", "USD"):
            continue

        if not wsname or "/" not in wsname:
            continue

        base = wsname.split("/")[0]

        if base in ("USD", "USDT", "USDC"):
            continue

        candidates.append({
            "symbol": wsname,
            "exchange_symbol": altname,
            "internal_symbol": internal_name,
        })

    if not candidates:
        raise RuntimeError("Kraken returned no eligible USD Spot markets.")

    # Batch ticker request, then rank by estimated USD turnover.
    pair_names = ",".join(item["exchange_symbol"] for item in candidates)
    ticker_payload = get_json(
        f"{SPOT_API}/Ticker",
        {"pair": pair_names},
    )

    if ticker_payload.get("error"):
        raise RuntimeError(str(ticker_payload["error"]))

    tickers = ticker_payload.get("result", {})
    ticker_by_key = {key: value for key, value in tickers.items()}

    ranked = []

    for item in candidates:
        ticker = (
            ticker_by_key.get(item["internal_symbol"])
            or ticker_by_key.get(item["exchange_symbol"])
        )

        if not ticker:
            continue

        try:
            price = float(ticker["c"][0])
            volume_24h = float(ticker["v"][1])
            vwap = float(ticker["p"][1])
            turnover = volume_24h * vwap
        except (KeyError, TypeError, ValueError, IndexError):
            continue

        if price <= 0 or turnover < MIN_SPOT_TURNOVER:
            continue

        item["last_price"] = price
        item["turnover"] = turnover
        ranked.append(item)

    ranked.sort(key=lambda item: item["turnover"], reverse=True)
    return ranked[:SCAN_LIMIT]


def fetch_futures_universe():
    payload = get_json(f"{FUTURES_API}/tickers")

    if payload.get("result") != "success":
        raise RuntimeError(
            "Kraken Futures ticker error: " + str(payload)
        )

    tickers = payload.get("tickers", [])
    ranked = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        # Kraken perpetual and dated futures contract identifiers.
        if not symbol.startswith(("PF_", "PI_")):
            continue

        try:
            price = float(ticker.get("last") or ticker.get("markPrice") or 0)
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

        # Volume field names and units may differ by contract.
        # Until validated, do not use it as a USD-turnover estimate.
        ranked.append({
            "symbol": symbol,
            "exchange_symbol": symbol,
            "last_price": price,
            "reported_volume": volume,
        })

    ranked.sort(
        key=lambda item: item["reported_volume"],
        reverse=True,
    )

    if not ranked:
        raise RuntimeError("No eligible Kraken Futures contracts returned.")

    return ranked[:SCAN_LIMIT]


def fetch_spot_candles(pair, interval):
    payload = get_json(
        f"{SPOT_API}/OHLC",
        {
            "pair": pair,
            "interval": interval,
        },
    )

    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))

    result = payload.get("result", {})
    candles_key = next(
        (key for key in result if key != "last"),
        None,
    )

    if not candles_key:
        raise RuntimeError(f"No Spot candles for {pair}")

    raw = result[candles_key]

    # Kraken includes the currently forming candle as the last row.
    # Exclude it so the strategy only uses closed candles.
    raw = raw[:-1]

    candles = []

    for row in raw:
        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })

    if len(candles) < 30:
        raise RuntimeError(f"Not enough Spot candles for {pair}")

    return candles


def fetch_futures_candles(symbol, resolution):
    payload = get_json(
        f"{FUTURES_CHARTS}/trade/{symbol}/{resolution}",
        {"count": 100},
    )

    raw = payload.get("candles", [])

    candles = []

    for row in raw:
        try:
            candles.append({
                "time": int(row["time"]) // 1000,
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

    # Exclude the active candle when its start time indicates
    # the current 5m/15m interval is still in progress.
    now = int(time.time())

    interval_seconds = {
        "5m": 300,
        "15m": 900,
    }.get(resolution, 300)

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

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        )
        true_ranges.append(true_range)

    if len(true_ranges) < period:
        return None

    atr_value = sum(true_ranges[:period]) / period

    for current in true_ranges[period:]:
        atr_value = (
            atr_value * (period - 1) + current
        ) / period

    return atr_value


def evaluate_signal(
    market,
    symbol,
    exchange_symbol,
    category,
    candles_5m,
    candles_15m,
):
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

    if average_volume <= 0 or atr <= 0:
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
        evidence_long.append(f"5m RSI supports bullish momentum ({rsi:.1f})")

    if 30 <= rsi <= 50:
        short_score += 1
        evidence_short.append(f"5m RSI supports bearish momentum ({rsi:.1f})")

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
        evidence_long.append("Price broke above the previous 20 closed 5m candles")

    if breakout_down:
        short_score += 2
        evidence_short.append("Price broke below the previous 20 closed 5m candles")

    if momentum_up:
        long_score += 1
        evidence_long.append("Latest closed 5m candle closed above the prior close")

    if momentum_down:
        short_score += 1
        evidence_short.append("Latest closed 5m candle closed below the prior close")

    candidates = []

    if long_score >= MIN_SCORE and bullish_trend:
        candidates.append(("LONG", long_score, evidence_long))

    # Spot is long-only. Short signals are futures-only.
    if category == "linear" and short_score >= MIN_SCORE and bearish_trend:
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

    signal_id = (
        f"kraken-{category}-{symbol}-{direction}-"
        f"{candles_5m[-1]['time']}"
    )

    return {
        "signal_id": signal_id,
        "exchange": "kraken",
        "exchange_symbol": exchange_symbol,
        "market_type": "spot" if category == "spot" else "futures",
        "market": market,
        "category": category,
        "symbol": symbol,
        "direction": direction,
        "entry": price,
        "stop_loss": stop,
        "targets": targets,
        "score": score,
        "rsi": round(rsi, 2),
        "atr": atr,
        "volume_ratio": round(volume_ratio, 3),
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
    ax.fill_between(times, lows, highs, alpha=0.12, label="Candle range")

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
        f"CRYPTO EDGE | {signal['market']} | "
        f"{signal['symbol']} | {signal['direction']}"
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
        "🚨 CRYPTO EDGE | SCALP SIGNAL\n\n"
        f"🏦 Market: {signal['market']}\n"
        f"🪙 Asset: {signal['symbol']}\n"
        f"📈 Direction: {signal['direction']}\n"
        f"⏱ Timeframe: {signal['timeframe']}\n\n"
        f"🎯 Reference entry: {fmt(signal['entry'])}\n"
        f"🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
        f"💰 TP1: {fmt(targets[0])}\n"
        f"💰 TP2: {fmt(targets[1])}\n"
        f"💰 TP3: {fmt(targets[2])}\n\n"
        f"📊 Signal score: {signal['score']}\n"
        f"📉 RSI: {signal['rsi']}\n"
        f"📦 Volume ratio: {signal['volume_ratio']}x\n\n"
        f"🔎 Evidence:\n{evidence}\n\n"
        "🟡 STATUS: WAITING FOR ENTRY\n"
        "This is a reference signal, not a confirmed fill.\n"
        "The manager will report when its observed price reaches the entry zone.\n"
        "Paper tracking only. Not financial advice."
    )


def scan_market(market, category, instrument):
    symbol = instrument["symbol"]
    exchange_symbol = instrument["exchange_symbol"]

    try:
        if category == "spot":
            candles_5m = fetch_spot_candles(exchange_symbol, 5)
            time.sleep(0.25)
            candles_15m = fetch_spot_candles(exchange_symbol, 15)
        else:
            candles_5m = fetch_futures_candles(exchange_symbol, "5m")
            time.sleep(0.25)
            candles_15m = fetch_futures_candles(exchange_symbol, "15m")

        return evaluate_signal(
            market=market,
            symbol=symbol,
            exchange_symbol=exchange_symbol,
            category=category,
            candles_5m=candles_5m,
            candles_15m=candles_15m,
        )

    except Exception as exc:
        print(f"Scan failed: {market} {symbol}: {exc}")
        return None


def main():
    print("Starting Crypto Edge Kraken Scalp Engine")
    print("UTC:", now_utc())

    state = load_state()
    sent_ids = set(state.get("sent", []))
    saved_signals = state.get("signals", [])

    if not isinstance(saved_signals, list):
        saved_signals = []

    run_counts = {"SPOT": 0, "FUTURES": 0}
    failures = {"SPOT": 0, "FUTURES": 0}
    attempted = {"SPOT": 0, "FUTURES": 0}

    try:
        spot_universe = fetch_spot_universe()
        print("Eligible Spot markets:", len(spot_universe))
    except Exception as exc:
        print("Could not load Kraken Spot markets:", exc)
        spot_universe = []
        failures["SPOT"] += 1

    try:
        futures_universe = fetch_futures_universe()
        print("Eligible Futures contracts:", len(futures_universe))
    except Exception as exc:
        print("Could not load Kraken Futures markets:", exc)
        futures_universe = []
        failures["FUTURES"] += 1

    for market, category, universe in (
        ("SPOT", "spot", spot_universe),
        ("FUTURES", "linear", futures_universe),
    ):
        for instrument in universe:
            attempted[market] += 1
            signal = scan_market(market, category, instrument)

            if signal is None:
                time.sleep(0.25)
                continue

            if signal["signal_id"] in sent_ids:
                continue

            chart_path = create_chart(signal)
            caption = build_caption(signal)

            # Only record a signal after Telegram confirms delivery.
            if telegram_send_photo(chart_path, caption):
                sent_ids.add(signal["signal_id"])

                stored = {
                    key: value
                    for key, value in signal.items()
                    if key != "candles"
                }

                saved_signals.append(stored)
                run_counts[market] += 1
                print(
                    f"Signal sent: {market} {signal['symbol']} "
                    f"{signal['direction']} score={signal['score']}"
                )

            time.sleep(0.35)

            if run_counts[market] >= MAX_SIGNALS_PER_MARKET:
                break

    state["sent"] = list(sent_ids)[-5000:]
    state["signals"] = saved_signals[-1000:]
    state["updated_at"] = now_utc()
    save_state(state)

    print("Markets attempted:", attempted)
    print("Signals sent:", run_counts)
    print("Market load failures:", failures)

    # Do not report a healthy scan when both market APIs failed.
    if not spot_universe and not futures_universe:
        raise RuntimeError("Both Kraken market APIs failed. No scan was completed.")

    print("Kraken scan completed.")


if __name__ == "__main__":
    main()
