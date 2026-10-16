
import os
import json
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as patches
from datetime import datetime, timezone


# =========================================================
# CRYPTO EDGE FUTURES SCALP ENGINE v4.1 DIAGNOSTIC
# Kraken Futures | 5m entry | 15m trend
# Paper signals only. No real orders are placed.
# =========================================================

FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"

STATE_FILE = "scalp_signals.json"
CHART_DIR = "signal_charts"

SCAN_LIMIT = 12
MAX_SIGNALS_PER_RUN = 2
MAX_CANDLE_COUNT = 60
MAX_STORED_SIGNALS = 1000

FIXED_LEVERAGE = 10.0
VOLUME_RATIO_MIN = 1.20
MIN_SCORE = 6

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.25
MIN_STOP_DISTANCE_PERCENT = 0.15
MAX_STOP_DISTANCE_PERCENT = 1.20

TP1_RR = 1.30
TP2_RR = 1.80
TP3_RR = 2.50

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-FuturesScalpEngine/4.1",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, params=None):
    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response format")
    return payload


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("sent", [])
            data.setdefault("signals", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"sent": [], "signals": []}


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, STATE_FILE)


def display_symbol(contract):
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


def fmt(price):
    price = float(price)
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.9f}".rstrip("0").rstrip(".")


def telegram_send_photo(path, caption):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

    if not token:
        print("TELEGRAM_BOT_TOKEN is missing")
        return False

    try:
        with open(path, "rb") as photo:
            response = SESSION.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": photo},
                timeout=30,
            )
        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            print("Telegram photo rejected:", payload)
            return False
        return True

    except (requests.RequestException, OSError, ValueError) as exc:
        print("Telegram photo error:", exc)
        return False


def fetch_futures_universe():
    payload = get_json(f"{FUTURES_API}/tickers")
    if payload.get("result") != "success":
        raise RuntimeError("Kraken Futures ticker error: " + str(payload))

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
            volume = float(
                ticker.get("vol24h")
                or ticker.get("volume24h")
                or ticker.get("volume")
                or 0
            )
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        ranked.append({
            "contract": contract,
            "symbol": display_symbol(contract),
            "reported_volume": volume,
        })

    ranked.sort(
        key=lambda item: item["reported_volume"],
        reverse=True,
    )

    if not ranked:
        raise RuntimeError("No eligible Kraken Futures contracts returned")

    return ranked[:SCAN_LIMIT]


def fetch_futures_candles(contract, resolution):
    payload = get_json(
        f"{FUTURES_CHARTS}/trade/{contract}/{resolution}",
        {"count": 120},
    )

    candles = []
    interval = {"5m": 300, "15m": 900}.get(resolution, 300)

    for row in payload.get("candles", []):
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

            # Exclude candles that have not fully closed.
            if candle_time + interval > int(time.time()):
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    unique = {c["time"]: c for c in candles}
    candles = sorted(unique.values(), key=lambda c: c["time"])

    if len(candles) < 35:
        raise RuntimeError(
            f"Not enough closed {resolution} candles: {len(candles)}"
        )

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

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
    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) <= period:
        return None

    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    if len(ranges) < period:
        return None

    value = sum(ranges[:period]) / period
    for current in ranges[period:]:
        value = (value * (period - 1) + current) / period

    return value


def calculate_structure(candles, direction, atr, entry):
    lookback = candles[-12:-1]
    if len(lookback) < 8:
        return None, "insufficient candles for stop structure"

    if direction == "LONG":
        swing_low = min(c["low"] for c in lookback)
        stop = min(
            swing_low - atr * 0.15,
            entry - atr * ATR_STOP_MULTIPLIER,
        )
        if stop <= 0 or stop >= entry:
            return None, "invalid LONG stop placement"
        risk = entry - stop
    else:
        swing_high = max(c["high"] for c in lookback)
        stop = max(
            swing_high + atr * 0.15,
            entry + atr * ATR_STOP_MULTIPLIER,
        )
        if stop <= entry:
            return None, "invalid SHORT stop placement"
        risk = stop - entry

    distance = risk / entry * 100
    if distance < MIN_STOP_DISTANCE_PERCENT:
        return None, f"stop too tight ({distance:.3f}%)"
    if distance > MAX_STOP_DISTANCE_PERCENT:
        return None, f"stop too wide ({distance:.3f}%)"

    return (stop, risk, distance), "OK"


def evaluate_signal(contract, candles_5m, candles_15m):
    if len(candles_5m) < 35 or len(candles_15m) < 35:
        return None, "insufficient candle history"

    closes = [c["close"] for c in candles_5m]
    volumes = [c["volume"] for c in candles_5m]
    closes_15m = [c["close"] for c in candles_15m]

    last = candles_5m[-1]
    previous = candles_5m[-2]
    entry = last["close"]

    fast15 = ema(closes_15m, 9)
    slow15 = ema(closes_15m, 21)
    fast5 = ema(closes, 9)
    slow5 = ema(closes, 21)
    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles_5m)

    if any(v is None for v in (fast15, slow15, fast5, slow5, rsi, atr)):
        return None, "indicator calculation failed"
    if entry <= 0 or atr <= 0:
        return None, "invalid price or ATR"

    average_volume = sum(volumes[-21:-1]) / 20
    if average_volume <= 0:
        return None, "average volume is zero"

    volume_ratio = volumes[-1] / average_volume
    if volume_ratio < VOLUME_RATIO_MIN:
        return None, f"volume too low ({volume_ratio:.2f}x < {VOLUME_RATIO_MIN:.2f}x)"

    structure = candles_5m[-21:-1]
    recent_high = max(c["high"] for c in structure)
    recent_low = min(c["low"] for c in structure)

    bull15 = fast15 > slow15
    bear15 = fast15 < slow15
    bull5 = fast5 > slow5
    bear5 = fast5 < slow5
    breakout_up = last["close"] > recent_high
    breakout_down = last["close"] < recent_low
    momentum_up = last["close"] > previous["close"]
    momentum_down = last["close"] < previous["close"]

    long_score = 0
    short_score = 0
    evidence_long = []
    evidence_short = []

    if bull15:
        long_score += 2
        evidence_long.append("15m EMA 9 above EMA 21")
    if bear15:
        short_score += 2
        evidence_short.append("15m EMA 9 below EMA 21")

    if bull5:
        long_score += 1
        evidence_long.append("5m EMA 9 above EMA 21")
    if bear5:
        short_score += 1
        evidence_short.append("5m EMA 9 below EMA 21")

    if 50 <= rsi <= 68:
        long_score += 1
        evidence_long.append(f"5m RSI bullish range: {rsi:.1f}")
    if 32 <= rsi <= 50:
        short_score += 1
        evidence_short.append(f"5m RSI bearish range: {rsi:.1f}")

    evidence_volume = f"Closed-candle volume {volume_ratio:.2f}x average"
    long_score += 1
    short_score += 1
    evidence_long.append(evidence_volume)
    evidence_short.append(evidence_volume)

    if breakout_up:
        long_score += 2
        evidence_long.append("5m close broke above prior 20-candle high")
    if breakout_down:
        short_score += 2
        evidence_short.append("5m close broke below prior 20-candle low")

    if momentum_up:
        long_score += 1
        evidence_long.append("Latest 5m candle has bullish momentum")
    if momentum_down:
        short_score += 1
        evidence_short.append("Latest 5m candle has bearish momentum")

    long_ok = (
        bull15 and bull5 and breakout_up
        and long_score >= MIN_SCORE and 50 <= rsi <= 68
    )
    short_ok = (
        bear15 and bear5 and breakout_down
        and short_score >= MIN_SCORE and 32 <= rsi <= 50
    )

    if long_ok and short_ok:
        return None, "ambiguous LONG and SHORT conditions"
    if not long_ok and not short_ok:
        return None, (
            f"filters not aligned | LONG score={long_score}, "
            f"SHORT score={short_score}, RSI={rsi:.1f}, "
            f"trend15={'bullish' if bull15 else 'bearish' if bear15 else 'flat'}, "
            f"trend5={'bullish' if bull5 else 'bearish' if bear5 else 'flat'}, "
            f"breakout={'up' if breakout_up else 'down' if breakout_down else 'none'}"
        )

    if long_ok:
        direction, score, evidence = "LONG", long_score, evidence_long
    else:
        direction, score, evidence = "SHORT", short_score, evidence_short

    stop_result, stop_reason = calculate_structure(
        candles_5m, direction, atr, entry
    )
    if stop_result is None:
        return None, stop_reason

    stop, risk, stop_distance = stop_result

    if direction == "LONG":
        targets = [
            entry + risk * TP1_RR,
            entry + risk * TP2_RR,
            entry + risk * TP3_RR,
        ]
        resistance = max(c["high"] for c in candles_5m[-31:-1])
        if entry < resistance < targets[0]:
            return None, "TP1 obstructed by nearby resistance"
        support = min(c["low"] for c in candles_5m[-31:-1])
    else:
        targets = [
            entry - risk * TP1_RR,
            entry - risk * TP2_RR,
            entry - risk * TP3_RR,
        ]
        support = min(c["low"] for c in candles_5m[-31:-1])
        if targets[0] < support < entry:
            return None, "TP1 obstructed by nearby support"
        resistance = max(c["high"] for c in candles_5m[-31:-1])

    if any(target <= 0 for target in targets):
        return None, "target price is invalid"

    if direction == "LONG":
        valid = stop < entry < targets[0] < targets[1] < targets[2]
    else:
        valid = targets[2] < targets[1] < targets[0] < entry < stop
    if not valid:
        return None, "invalid stop/target ordering"

    margin_loss = stop_distance * FIXED_LEVERAGE
    signal_id = f"kraken-linear-{contract}-{direction}-{last['time']}"

    signal = {
        "signal_id": signal_id,
        "exchange": "kraken",
        "exchange_symbol": contract,
        "market_type": "futures",
        "market": "FUTURES",
        "category": "linear",
        "symbol": display_symbol(contract),
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "score": score,
        "rsi": round(rsi, 2),
        "atr": atr,
        "volume_ratio": round(volume_ratio, 3),
        "leverage": FIXED_LEVERAGE,
        "stop_distance_percent": round(stop_distance, 4),
        "estimated_margin_loss_percent": round(margin_loss, 3),
        "risk_reward": {"tp1": TP1_RR, "tp2": TP2_RR, "tp3": TP3_RR},
        "support_reference": support,
        "resistance_reference": resistance,
        "timeframe": "5m entry / 15m trend",
        "entry_timeframe": "5m",
        "trend_timeframe": "15m",
        "candle_time": last["time"],
        "evidence": evidence,
        "candles": candles_5m[-MAX_CANDLE_COUNT:],
        "created_at": now_utc(),
    }
    return signal, "SIGNAL QUALIFIED"


def create_chart(signal):
    os.makedirs(CHART_DIR, exist_ok=True)
    candles = signal["candles"]
    if len(candles) < 10:
        raise RuntimeError("Not enough candles to draw chart")

    dates = [
        datetime.fromtimestamp(c["time"], tz=timezone.utc)
        for c in candles
    ]
    x_values = mdates.date2num(dates)
    candle_width = (5 / (24 * 60)) * 0.68

    fig, (ax, volume_ax) = plt.subplots(
        2, 1, figsize=(15, 9), sharex=True,
        gridspec_kw={"height_ratios": [4, 1]},
    )

    for i, candle in enumerate(candles):
        x = x_values[i]
        opened = candle["open"]
        high = candle["high"]
        low = candle["low"]
        closed = candle["close"]
        color = "#00b894" if closed >= opened else "#e74c3c"

        ax.vlines(x, low, high, color=color, linewidth=1.2, zorder=2)
        body_height = abs(closed - opened)
        if body_height == 0:
            body_height = max(abs(closed) * 0.00002, 1e-12)

        ax.add_patch(patches.Rectangle(
            (x - candle_width / 2, min(opened, closed)),
            candle_width, body_height,
            facecolor=color, edgecolor=color, linewidth=0.8, zorder=3,
        ))
        volume_ax.bar(
            x, candle["volume"], width=candle_width, color=color, alpha=0.8
        )

    entry = signal["entry"]
    stop = signal["stop_loss"]
    targets = signal["targets"]

    ax.axhline(
        entry, color="#2980b9", linewidth=1.8,
        label=f"ENTRY {fmt(entry)}",
    )
    ax.axhline(
        stop, color="#e74c3c", linestyle="--", linewidth=1.8,
        label=f"STOP {fmt(stop)}",
    )

    for i, target in enumerate(targets):
        ax.axhline(
            target,
            color=["#27ae60", "#16a085", "#2ecc71"][i],
            linestyle=":", linewidth=1.6,
            label=f"TP{i + 1} {fmt(target)}",
        )

    ax.set_title(
        f"CRYPTO EDGE | {signal['symbol']} | {signal['direction']} | "
        f"10x | 5m ENTRY / 15m TREND",
        fontsize=15, fontweight="bold", pad=14,
    )
    ax.set_ylabel("Price")
    volume_ax.set_ylabel("Volume")
    volume_ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.22)
    volume_ax.grid(True, axis="y", alpha=0.18)
    ax.legend(loc="upper left", fontsize=8, ncol=2, framealpha=0.9)
    volume_ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M", tz=timezone.utc)
    )

    fig.autofmt_xdate()
    fig.tight_layout()
    safe_symbol = signal["symbol"].replace("/", "_").replace(":", "_")
    path = os.path.join(
        CHART_DIR, f"{safe_symbol}_{signal['candle_time']}.png"
    )
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def build_caption(signal):
    targets = signal["targets"]
    evidence = "\n".join(f"• {item}" for item in signal["evidence"])
    return (
        "🚨 CRYPTO EDGE | FUTURES SCALP\n\n"
        f"🪙 Pair: {signal['symbol']}\n"
        f"📈 Direction: {signal['direction']}\n"
        "⏱ Entry: 5m candles | Trend: 15m candles\n\n"
        f"🎯 Reference entry: {fmt(signal['entry'])}\n"
        f"🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
        f"💰 TP1 (1.3R): {fmt(targets[0])}\n"
        f"💰 TP2 (1.8R): {fmt(targets[1])}\n"
        f"💰 TP3 (2.5R): {fmt(targets[2])}\n\n"
        f"⚙️ Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x\n"
        f"📏 Price distance to SL: {signal['stop_distance_percent']}%\n"
        f"🧮 Approx. allocated-margin loss at SL: "
        f"{signal['estimated_margin_loss_percent']}%\n"
        "Excludes fees, slippage, funding and liquidation effects.\n\n"
        f"📊 Signal score: {signal['score']}\n"
        f"📉 5m RSI: {signal['rsi']}\n"
        f"📦 Closed-candle volume ratio: {signal['volume_ratio']}x\n"
        f"🧱 Support reference: {fmt(signal['support_reference'])}\n"
        f"🧱 Resistance reference: {fmt(signal['resistance_reference'])}\n\n"
        f"🔎 Evidence:\n{evidence}\n\n"
        "🟡 STATUS: WAITING FOR ENTRY\n"
        "Entry is a reference price, not a confirmed fill.\n"
        "Paper tracking only. No real order has been placed."
    )


def scan_market(instrument):
    contract = instrument["contract"]
    symbol = instrument["symbol"]

    try:
        candles_5m = fetch_futures_candles(contract, "5m")
        time.sleep(0.25)
        candles_15m = fetch_futures_candles(contract, "15m")
        signal, reason = evaluate_signal(contract, candles_5m, candles_15m)
        print(f"DIAGNOSTIC {symbol} ({contract}): {reason}")
        return signal

    except Exception as exc:
        print(f"DIAGNOSTIC {symbol} ({contract}): DATA/API ERROR: {exc}")
        return None


def main():
    print("Starting Crypto Edge Kraken Futures Scalp Engine v4.1 DIAGNOSTIC")
    print("UTC:", now_utc())
    print("Market mode: FUTURES ONLY")
    print(f"Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x")
    print("Entry timeframe: 5m | Trend timeframe: 15m")
    print("Chart: real OHLC candlesticks + volume")
    print("Diagnostic mode: rejection reasons enabled; thresholds unchanged.")

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
            print("Already sent; skipping duplicate:", signal["signal_id"])
            continue

        try:
            chart_path = create_chart(signal)
            caption = build_caption(signal)
        except Exception as exc:
            print(f"Chart/caption error for {signal['symbol']}: {exc}")
            continue

        if telegram_send_photo(chart_path, caption):
            sent_ids.add(signal["signal_id"])
            saved_signals.append({
                k: v for k, v in signal.items() if k != "candles"
            })
            sent_count += 1
            print(
                f"Signal sent: {signal['symbol']} {signal['direction']} "
                f"score={signal['score']} leverage={signal['leverage']}x "
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
