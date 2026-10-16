
import os
import json
import time
import math
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as patches

from datetime import datetime, timezone


# =========================================================
# CRYPTO EDGE FUTURES SCALP ENGINE
# Kraken Futures | 5m entry | 15m trend
# Real OHLC candlestick charts | Paper signals only
# =========================================================

FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"

STATE_FILE = "scalp_signals.json"
CHART_DIR = "signal_charts"

SCAN_LIMIT = 12
MAX_SIGNALS_PER_RUN = 2

FIXED_LEVERAGE = 10.0

VOLUME_RATIO_MIN = 1.20
MIN_SCORE = 6

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.25

MIN_STOP_DISTANCE_PERCENT = 0.15
MAX_STOP_DISTANCE_PERCENT = 1.20

MIN_TP1_RR = 1.30
TP2_RR = 1.80
TP3_RR = 2.50

MAX_CANDLE_COUNT = 60
MAX_STORED_SIGNALS = 1000

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-FuturesScalpEngine/4.0",
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


def display_symbol(contract):
    """
    Display a readable pair without changing the Kraken contract ID.
    Example: PF_STRKUSD -> STRK/USD
    """
    value = str(contract or "").strip()

    if value.startswith(("PF_", "PI_")):
        value = value[3:]

    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "BTC"):
        if value.endswith(quote) and len(value) > len(quote):
            base = value[:-len(quote)]

            if base == "XBT":
                base = "BTC"

            return f"{base}/{quote}"

    return value


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
    price = float(price)

    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"

    return f"{price:.9f}".rstrip("0").rstrip(".")


def fetch_futures_universe():
    payload = get_json(f"{FUTURES_API}/tickers")

    if payload.get("result") != "success":
        raise RuntimeError(
            "Kraken Futures ticker error: " + str(payload)
        )

    ranked = []

    for ticker in payload.get("tickers", []):
        contract = ticker.get("symbol", "")

        if not contract.startswith(("PF_", "PI_")):
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
            "contract": contract,
            "symbol": display_symbol(contract),
            "last_price": price,
            "reported_volume": volume,
        })

    ranked.sort(
        key=lambda item: item["reported_volume"],
        reverse=True,
    )

    if not ranked:
        raise RuntimeError(
            "No eligible Kraken Futures contracts returned."
        )

    return ranked[:SCAN_LIMIT]


def fetch_futures_candles(contract, resolution):
    payload = get_json(
        f"{FUTURES_CHARTS}/trade/{contract}/{resolution}",
        {"count": 120},
    )

    raw = payload.get("candles", [])
    candles = []

    for row in raw:
        try:
            candle_time = int(row["time"])

            if candle_time > 10_000_000_000:
                candle_time //= 1000

            candle = {
                "time": candle_time,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            }

            if (
                candle["open"] <= 0
                or candle["close"] <= 0
                or candle["low"] <= 0
                or candle["high"] <= 0
                or candle["high"] < candle["low"]
            ):
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda candle: candle["time"])

    interval_seconds = {
        "5m": 300,
        "15m": 900,
    }.get(resolution, 300)

    now = int(time.time())

    # Do not evaluate candles that have not fully closed.
    candles = [
        candle for candle in candles
        if candle["time"] + interval_seconds <= now
    ]

    # Remove duplicate timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda candle: candle["time"],
    )

    if len(candles) < 35:
        raise RuntimeError(
            f"Not enough closed {resolution} candles for {contract}"
        )

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

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

    if avg_loss == 0:
        return 100.0

    relative_strength = avg_gain / avg_loss

    return 100 - (100 / (1 + relative_strength))


def calculate_atr(candles, period=ATR_PERIOD):
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

    value = sum(true_ranges[:period]) / period

    for current in true_ranges[period:]:
        value = (
            value * (period - 1) + current
        ) / period

    return value


def calculate_leverage(entry, stop):
    if entry <= 0:
        return None, None, None

    stop_distance_percent = abs(entry - stop) / entry * 100

    if stop_distance_percent <= 0:
        return None, None, None

    # Fixed leverage for every newly generated signal.
    leverage = FIXED_LEVERAGE

    # Approximate loss relative to allocated margin.
    # Fees, funding, slippage and liquidation are excluded.
    margin_loss_percent = stop_distance_percent * leverage

    return (
        leverage,
        round(stop_distance_percent, 4),
        round(margin_loss_percent, 3),
    )


def calculate_structure(candles, direction, atr, entry):
    """
    Put the stop beyond a recent local structure with an ATR buffer.
    Reject stops that are too tight or too wide for this engine.
    """
    lookback = candles[-12:-1]

    if len(lookback) < 8:
        return None

    if direction == "LONG":
        swing_low = min(candle["low"] for candle in lookback)
        stop = min(
            swing_low - atr * 0.15,
            entry - atr * ATR_STOP_MULTIPLIER,
        )

        if stop <= 0 or stop >= entry:
            return None

        risk = entry - stop

    else:
        swing_high = max(candle["high"] for candle in lookback)
        stop = max(
            swing_high + atr * 0.15,
            entry + atr * ATR_STOP_MULTIPLIER,
        )

        if stop <= entry:
            return None

        risk = stop - entry

    distance_percent = risk / entry * 100

    if not (
        MIN_STOP_DISTANCE_PERCENT
        <= distance_percent
        <= MAX_STOP_DISTANCE_PERCENT
    ):
        return None

    return stop, risk, distance_percent


def evaluate_signal(contract, candles_5m, candles_15m):
    closes = [candle["close"] for candle in candles_5m]
    volumes = [candle["volume"] for candle in candles_5m]
    closes_15m = [candle["close"] for candle in candles_15m]

    if len(closes) < 35 or len(closes_15m) < 35:
        return None

    last = candles_5m[-1]
    previous = candles_5m[-2]
    entry = last["close"]

    ema_fast_15m = ema(closes_15m, 9)
    ema_slow_15m = ema(closes_15m, 21)
    ema_fast_5m = ema(closes, 9)
    ema_slow_5m = ema(closes, 21)
    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles_5m)

    if any(value is None for value in (
        ema_fast_15m,
        ema_slow_15m,
        ema_fast_5m,
        ema_slow_5m,
        rsi,
        atr,
    )):
        return None

    if entry <= 0 or atr <= 0:
        return None

    previous_volumes = volumes[-21:-1]
    average_volume = sum(previous_volumes) / len(previous_volumes)

    if average_volume <= 0:
        return None

    volume_ratio = volumes[-1] / average_volume

    # Recent structure excludes the current candle.
    structure = candles_5m[-21:-1]
    recent_high = max(candle["high"] for candle in structure)
    recent_low = min(candle["low"] for candle in structure)

    bullish_trend = ema_fast_15m > ema_slow_15m
    bearish_trend = ema_fast_15m < ema_slow_15m

    bullish_5m = ema_fast_5m > ema_slow_5m
    bearish_5m = ema_fast_5m < ema_slow_5m

    breakout_up = last["close"] > recent_high
    breakout_down = last["close"] < recent_low

    momentum_up = last["close"] > previous["close"]
    momentum_down = last["close"] < previous["close"]

    # Reject weak volume instead of awarding a score to it.
    if volume_ratio < VOLUME_RATIO_MIN:
        return None

    candidates = []

    long_score = 0
    short_score = 0
    evidence_long = []
    evidence_short = []

    if bullish_trend:
        long_score += 2
        evidence_long.append("15m EMA 9 is above EMA 21")

    if bearish_trend:
        short_score += 2
        evidence_short.append("15m EMA 9 is below EMA 21")

    if bullish_5m:
        long_score += 1
        evidence_long.append("5m EMA 9 is above EMA 21")

    if bearish_5m:
        short_score += 1
        evidence_short.append("5m EMA 9 is below EMA 21")

    if 50 <= rsi <= 68:
        long_score += 1
        evidence_long.append(f"5m RSI bullish range: {rsi:.1f}")

    if 32 <= rsi <= 50:
        short_score += 1
        evidence_short.append(f"5m RSI bearish range: {rsi:.1f}")

    evidence_volume = (
        f"Closed-candle volume is {volume_ratio:.2f}x "
        "the previous 20-candle average"
    )

    long_score += 1
    short_score += 1
    evidence_long.append(evidence_volume)
    evidence_short.append(evidence_volume)

    if breakout_up:
        long_score += 2
        evidence_long.append(
            "5m candle closed above the prior 20-candle high"
        )

    if breakout_down:
        short_score += 2
        evidence_short.append(
            "5m candle closed below the prior 20-candle low"
        )

    if momentum_up:
        long_score += 1
        evidence_long.append("Latest closed 5m candle has bullish momentum")

    if momentum_down:
        short_score += 1
        evidence_short.append("Latest closed 5m candle has bearish momentum")

    # Require trend alignment on both timeframes and a breakout.
    if (
        bullish_trend
        and bullish_5m
        and breakout_up
        and long_score >= MIN_SCORE
        and 50 <= rsi <= 68
    ):
        candidates.append(("LONG", long_score, evidence_long))

    if (
        bearish_trend
        and bearish_5m
        and breakout_down
        and short_score >= MIN_SCORE
        and 32 <= rsi <= 50
    ):
        candidates.append(("SHORT", short_score, evidence_short))

    if not candidates:
        return None

    direction, score, evidence = max(
        candidates,
        key=lambda candidate: candidate[1],
    )

    # Do not issue ambiguous signals if both sides qualify.
    if len(candidates) > 1:
        return None

    stop_result = calculate_structure(
        candles_5m,
        direction,
        atr,
        entry,
    )

    if stop_result is None:
        return None

    stop, risk, stop_distance_percent = stop_result

    if direction == "LONG":
        targets = [
            entry + risk * MIN_TP1_RR,
            entry + risk * TP2_RR,
            entry + risk * TP3_RR,
        ]

        # Reject TP1 if a nearby prior high obstructs the move.
        resistance = max(candle["high"] for candle in candles_5m[-31:-1])

        if entry < resistance < targets[0]:
            return None

    else:
        targets = [
            entry - risk * MIN_TP1_RR,
            entry - risk * TP2_RR,
            entry - risk * TP3_RR,
        ]

        support = min(candle["low"] for candle in candles_5m[-31:-1])

        if targets[0] < support < entry:
            return None

    if any(target <= 0 for target in targets):
        return None

    # Verify price ordering and minimum reward/risk.
    if direction == "LONG":
        if not (stop < entry < targets[0] < targets[1] < targets[2]):
            return None
    else:
        if not (targets[2] < targets[1] < targets[0] < entry < stop):
            return None

    leverage, stop_percent, margin_loss_percent = calculate_leverage(
        entry, stop
    )

    if leverage is None:
        return None

    raw_contract = contract
    shown_symbol = display_symbol(contract)

    signal_id = (
        f"kraken-linear-{raw_contract}-{direction}-"
        f"{last['time']}"
    )

    # Identify nearby support/resistance for context.
    support_level = min(candle["low"] for candle in candles_5m[-31:-1])
    resistance_level = max(candle["high"] for candle in candles_5m[-31:-1])

    return {
        "signal_id": signal_id,
        "exchange": "kraken",
        "exchange_symbol": raw_contract,
        "market_type": "futures",
        "market": "FUTURES",
        "category": "linear",
        "symbol": shown_symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "score": score,
        "rsi": round(rsi, 2),
        "atr": atr,
        "volume_ratio": round(volume_ratio, 3),
        "leverage": FIXED_LEVERAGE,
        "stop_distance_percent": stop_percent,
        "estimated_margin_loss_percent": margin_loss_percent,
        "risk_reward": {
            "tp1": MIN_TP1_RR,
            "tp2": TP2_RR,
            "tp3": TP3_RR,
        },
        "support_reference": support_level,
        "resistance_reference": resistance_level,
        "timeframe": "5m entry / 15m trend",
        "entry_timeframe": "5m",
        "trend_timeframe": "15m",
        "candle_time": last["time"],
        "evidence": evidence,
        "candles": candles_5m[-MAX_CANDLE_COUNT:],
        "created_at": now_utc(),
    }


def create_chart(signal):
    os.makedirs(CHART_DIR, exist_ok=True)

    candles = signal["candles"]
    if len(candles) < 10:
        raise RuntimeError("Not enough candles to draw a chart.")

    dates = [
        datetime.fromtimestamp(candle["time"], tz=timezone.utc)
        for candle in candles
    ]

    x_values = mdates.date2num(dates)

    candle_width = (5 / (24 * 60)) * 0.68

    fig, (ax, volume_ax) = plt.subplots(
        2,
        1,
        figsize=(15, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [4, 1]},
    )

    for index, candle in enumerate(candles):
        x = x_values[index]
        opened = candle["open"]
        high = candle["high"]
        low = candle["low"]
        closed = candle["close"]

        bullish = closed >= opened
        candle_color = "#00b894" if bullish else "#e74c3c"

        # Real OHLC wick.
        ax.vlines(
            x,
            low,
            high,
            color=candle_color,
            linewidth=1.2,
            zorder=2,
        )

        # Real OHLC body.
        body_bottom = min(opened, closed)
        body_height = abs(closed - opened)

        # Keep doji candles visible.
        if body_height == 0:
            body_height = max(abs(closed) * 0.00002, 1e-12)

        body = patches.Rectangle(
            (x - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            facecolor=candle_color,
            edgecolor=candle_color,
            linewidth=0.8,
            zorder=3,
        )

        ax.add_patch(body)

        volume_ax.bar(
            x,
            candle["volume"],
            width=candle_width,
            color=candle_color,
            alpha=0.8,
        )

    entry = signal["entry"]
    stop = signal["stop_loss"]
    targets = signal["targets"]
    direction = signal["direction"]

    ax.axhline(
        entry,
        color="#2980b9",
        linestyle="-",
        linewidth=1.8,
        label=f"ENTRY {fmt(entry)}",
    )

    ax.axhline(
        stop,
        color="#e74c3c",
        linestyle="--",
        linewidth=1.8,
        label=f"STOP {fmt(stop)}",
    )

    target_colors = ["#27ae60", "#16a085", "#2ecc71"]

    for index, target in enumerate(targets):
        ax.axhline(
            target,
            color=target_colors[index],
            linestyle=":",
            linewidth=1.6,
            label=f"TP{index + 1} {fmt(target)}",
        )

    ax.set_title(
        f"CRYPTO EDGE | {signal['symbol']} | {direction} | "
        f"{FIXED_LEVERAGE:.0f}x | 5m ENTRY / 15m TREND",
        fontsize=15,
        fontweight="bold",
        pad=14,
    )

    ax.set_ylabel("Price")
    volume_ax.set_ylabel("Volume")
    volume_ax.set_xlabel("Time (UTC)")

    ax.grid(True, alpha=0.22)
    volume_ax.grid(True, axis="y", alpha=0.18)

    ax.legend(
        loc="upper left",
        fontsize=8,
        ncol=2,
        framealpha=0.9,
    )

    volume_ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M", tz=timezone.utc)
    )

    fig.autofmt_xdate()
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

    fig.savefig(
        path,
        dpi=180,
        bbox_inches="tight",
        facecolor="white",
    )

    plt.close(fig)

    return path


def build_caption(signal):
    targets = signal["targets"]

    evidence = "\n".join(
        f"• {item}" for item in signal["evidence"]
    )

    return (
        "🚨 CRYPTO EDGE | FUTURES SCALP\n\n"
        f"🪙 Pair: {signal['symbol']}\n"
        f"📈 Direction: {signal['direction']}\n"
        f"⏱ Entry: 5m candles | Trend: 15m candles\n\n"
        f"🎯 Reference entry: {fmt(signal['entry'])}\n"
        f"🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
        f"💰 TP1 (1.3R): {fmt(targets[0])}\n"
        f"💰 TP2 (1.8R): {fmt(targets[1])}\n"
        f"💰 TP3 (2.5R): {fmt(targets[2])}\n\n"
        f"⚙️ Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x\n"
        f"📏 Price distance to SL: "
        f"{signal['stop_distance_percent']}%\n"
        f"🧮 Approx. allocated-margin loss at SL: "
        f"{signal['estimated_margin_loss_percent']}%\n"
        "Excludes fees, slippage, funding and liquidation effects.\n\n"
        f"📊 Signal score: {signal['score']}\n"
        f"📉 5m RSI: {signal['rsi']}\n"
        f"📦 Closed-candle volume ratio: "
        f"{signal['volume_ratio']}x\n"
        f"🧱 Support reference: "
        f"{fmt(signal['support_reference'])}\n"
        f"🧱 Resistance reference: "
        f"{fmt(signal['resistance_reference'])}\n\n"
        f"🔎 Evidence:\n{evidence}\n\n"
        "🟡 STATUS: WAITING FOR ENTRY\n"
        "Entry is a reference price, not a confirmed fill.\n"
        "Paper tracking only. No real order has been placed."
    )


def scan_market(instrument):
    contract = instrument["contract"]

    try:
        candles_5m = fetch_futures_candles(contract, "5m")
        time.sleep(0.25)

        candles_15m = fetch_futures_candles(contract, "15m")

        return evaluate_signal(
            contract=contract,
            candles_5m=candles_5m,
            candles_15m=candles_15m,
        )

    except Exception as exc:
        print(f"Scan failed: FUTURES {contract}: {exc}")
        return None


def main():
    print("Starting Crypto Edge Kraken Futures Scalp Engine v4.0")
    print("UTC:", now_utc())
    print("Market mode: FUTURES ONLY")
    print(f"Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x")
    print("Entry timeframe: 5m | Trend timeframe: 15m")
    print("Chart: real OHLC candlesticks + volume")

    state = load_state()

    sent_ids = set(state.get("sent", []))
    saved_signals = state.get("signals", [])

    if not isinstance(saved_signals, list):
        saved_signals = []

    universe = fetch_futures_universe()
    print("Eligible Futures contracts:", len(universe))

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
                f"Signal sent: {signal['symbol']} "
                f"{signal['direction']} "
                f"score={signal['score']} "
                f"leverage={signal['leverage']}x "
                f"SL distance={signal['stop_distance_percent']}%"
            )

        time.sleep(0.35)

        if sent_count >= MAX_SIGNALS_PER_RUN:
            break

    state["sent"] = list(sent_ids)[-5000:]
    state["signals"] = saved_signals[-MAX_STORED_SIGNALS:]
    state["updated_at"] = now_utc()

    save_state(state)

    print("Markets attempted:", attempted)
    print("Signals sent:", sent_count)
    print("Kraken Futures scan completed.")


if __name__ == "__main__":
    main()
