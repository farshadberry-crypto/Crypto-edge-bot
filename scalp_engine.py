import os, json, time, requestsش
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as patches
from datetime import datetime, timezone

# CRYPTO EDGE FUTURES SCALP ENGINE v4.1 DIAGNOSTIC
# Kraken linear Futures | 5m entry | 15m trend | paper signals only
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
SESSION.headers.update({"User-Agent": "CryptoEdge-FuturesScalpEngine/4.1", "Accept": "application/json"})


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
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": photo}, timeout=30)
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
    if price >= 1000: return f"{price:,.2f}"
    if price >= 1: return f"{price:.4f}"
    if price >= 0.01: return f"{price:.6f}"
    return f"{price:.9f}".rstrip("0").rstrip(".")


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
            price = float(ticker.get("last") or ticker.get("markPrice") or 0)
            volume = float(ticker.get("vol24h") or ticker.get("volume24h") or ticker.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        ranked.append({"contract": contract, "symbol": display_symbol(contract),
                       "last_price": price, "reported_volume": volume})
    ranked.sort(key=lambda item: item["reported_volume"], reverse=True)
    if not ranked:
        raise RuntimeError("No eligible Kraken Futures contracts returned.")
    return ranked[:SCAN_LIMIT]


def fetch_futures_candles(contract, resolution):
    payload = get_json(f"{FUTURES_CHARTS}/trade/{contract}/{resolution}", {"count": 120})
    candles = []
    for row in payload.get("candles", []):
        try:
            candle_time = int(row["time"])
            if candle_time > 10_000_000_000:
                candle_time //= 1000
            c = {"time": candle_time, "open": float(row["open"]), "high": float(row["high"]),
                 "low": float(row["low"]), "close": float(row["close"]),
                 "volume": float(row.get("volume", 0) or 0)}
            if min(c["open"], c["close"], c["low"], c["high"]) <= 0 or c["high"] < c["low"]:
                continue
            candles.append(c)
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["time"])
    interval = {"5m": 300, "15m": 900}.get(resolution, 300)
    now = int(time.time())
    candles = [c for c in candles if c["time"] + interval <= now]
    unique = {c["time"]: c for c in candles}
    candles = sorted(unique.values(), key=lambda c: c["time"])
    if len(candles) < 35:
        raise RuntimeError(f"not enough closed {resolution} candles ({len(candles)}; need 35)")
    return candles


def ema(values, period):
    if len(values) < period: return None
    multiplier = 2 / (period + 1)
    value = sum(values[:period]) / period
    for current in values[period:]:
        value = (current - value) * multiplier + value
    return value


def calculate_rsi(closes, period=14):
    if len(closes) <= period: return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) <= period: return None
    ranges = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]["close"]
        ranges.append(max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev)))
    if len(ranges) < period: return None
    value = sum(ranges[:period]) / period
    for current in ranges[period:]:
        value = (value * (period - 1) + current) / period
    return value


def calculate_leverage(entry, stop):
    if entry <= 0: return None, None, None
    distance = abs(entry - stop) / entry * 100
    if distance <= 0: return None, None, None
    return FIXED_LEVERAGE, round(distance, 4), round(distance * FIXED_LEVERAGE, 3)


def calculate_structure(candles, direction, atr, entry):
    lookback = candles[-12:-1]
    if len(lookback) < 8: return None, "not enough candles for stop structure"
    if direction == "LONG":
        swing_low = min(c["low"] for c in lookback)
        stop = min(swing_low - atr * 0.15, entry - atr * ATR_STOP_MULTIPLIER)
        if stop <= 0 or stop >= entry: return None, "invalid LONG stop price"
        risk = entry - stop
    else:
        swing_high = max(c["high"] for c in lookback)
        stop = max(swing_high + atr * 0.15, entry + atr * ATR_STOP_MULTIPLIER)
        if stop <= entry: return None, "invalid SHORT stop price"
        risk = stop - entry
    distance = risk / entry * 100
    if distance < MIN_STOP_DISTANCE_PERCENT:
        return None, f"stop too tight ({distance:.3f}%; min {MIN_STOP_DISTANCE_PERCENT:.2f}%)"
    if distance > MAX_STOP_DISTANCE_PERCENT:
        return None, f"stop too wide ({distance:.3f}%; max {MAX_STOP_DISTANCE_PERCENT:.2f}%)"
    return (stop, risk, distance), None


def evaluate_signal(contract, candles_5m, candles_15m):
    """Returns (signal, diagnostic reason). Entry rules are unchanged from v4.0."""
    closes = [c["close"] for c in candles_5m]
    volumes = [c["volume"] for c in candles_5m]
    closes_15m = [c["close"] for c in candles_15m]
    if len(closes) < 35 or len(closes_15m) < 35:
        return None, "insufficient candle history"
    last, previous = candles_5m[-1], candles_5m[-2]
    entry = last["close"]
    e15f, e15s = ema(closes_15m, 9), ema(closes_15m, 21)
    e5f, e5s = ema(closes, 9), ema(closes, 21)
    rsi, atr = calculate_rsi(closes), calculate_atr(candles_5m)
    if any(v is None for v in (e15f, e15s, e5f, e5s, rsi, atr)):
        return None, "indicator calculation returned no value"
    if entry <= 0 or atr <= 0:
        return None, "invalid entry or ATR"
    previous_volumes = volumes[-21:-1]
    average_volume = sum(previous_volumes) / len(previous_volumes)
    if average_volume <= 0:
        return None, "previous 20-candle average volume is zero"
    volume_ratio = volumes[-1] / average_volume
    structure = candles_5m[-21:-1]
    recent_high = max(c["high"] for c in structure)
    recent_low = min(c["low"] for c in structure)
    bull15, bear15 = e15f > e15s, e15f < e15s
    bull5, bear5 = e5f > e5s, e5f < e5s
    breakout_up, breakout_down = last["close"] > recent_high, last["close"] < recent_low
    momentum_up, momentum_down = last["close"] > previous["close"], last["close"] < previous["close"]
    long_score = (2 if bull15 else 0) + (1 if bull5 else 0)
    short_score = (2 if bear15 else 0) + (1 if bear5 else 0)
    if 50 <= rsi <= 68: long_score += 1
    if 32 <= rsi <= 50: short_score += 1
    # Same v4.0 scoring: volume is checked first; then one point to both sides.
    if volume_ratio < VOLUME_RATIO_MIN:
        return None, (f"volume filter failed: {volume_ratio:.2f}x < {VOLUME_RATIO_MIN:.2f}x; "
                      f"RSI={rsi:.1f}; score L/S={long_score}/{short_score}")
    long_score += 1
    short_score += 1
    if breakout_up: long_score += 2
    if breakout_down: short_score += 2
    if momentum_up: long_score += 1
    if momentum_down: short_score += 1
    diagnostics = (f"vol={volume_ratio:.2f}x (min {VOLUME_RATIO_MIN:.2f}x), RSI={rsi:.1f}, "
                   f"score L/S={long_score}/{short_score}, "
                   f"15m={'UP' if bull15 else 'DOWN' if bear15 else 'FLAT'}, "
                   f"5m={'UP' if bull5 else 'DOWN' if bear5 else 'FLAT'}, "
                   f"breakout={'UP' if breakout_up else 'DOWN' if breakout_down else 'NO'}")
    candidates = []
    evidence_volume = f"Closed-candle volume is {volume_ratio:.2f}x the previous 20-candle average"
    evidence_long, evidence_short = [], []
    if bull15: evidence_long.append("15m EMA 9 is above EMA 21")
    if bear15: evidence_short.append("15m EMA 9 is below EMA 21")
    if bull5: evidence_long.append("5m EMA 9 is above EMA 21")
    if bear5: evidence_short.append("5m EMA 9 is below EMA 21")
    if 50 <= rsi <= 68: evidence_long.append(f"5m RSI bullish range: {rsi:.1f}")
    if 32 <= rsi <= 50: evidence_short.append(f"5m RSI bearish range: {rsi:.1f}")
    evidence_long.append(evidence_volume); evidence_short.append(evidence_volume)
    if breakout_up: evidence_long.append("5m candle closed above the prior 20-candle high")
    if breakout_down: evidence_short.append("5m candle closed below the prior 20-candle low")
    if momentum_up: evidence_long.append("Latest closed 5m candle has bullish momentum")
    if momentum_down: evidence_short.append("Latest closed 5m candle has bearish momentum")
    if bull15 and bull5 and breakout_up and long_score >= MIN_SCORE and 50 <= rsi <= 68:
        candidates.append(("LONG", long_score, evidence_long))
    if bear15 and bear5 and breakout_down and short_score >= MIN_SCORE and 32 <= rsi <= 50:
        candidates.append(("SHORT", short_score, evidence_short))
    if not candidates:
        reasons = []
        if not (bull15 and bull5 and breakout_up): reasons.append("LONG requires aligned bullish 15m/5m EMAs and breakout")
        if not (bear15 and bear5 and breakout_down): reasons.append("SHORT requires aligned bearish 15m/5m EMAs and breakdown")
        if max(long_score, short_score) < MIN_SCORE: reasons.append(f"best score below {MIN_SCORE}")
        if not (50 <= rsi <= 68 or 32 <= rsi <= 50): reasons.append("RSI outside both allowed ranges")
        return None, "; ".join(reasons) + " | " + diagnostics
    if len(candidates) > 1:
        return None, "both LONG and SHORT qualified; ambiguous signal rejected | " + diagnostics
    direction, score, evidence = candidates[0]
    stop_result, stop_reason = calculate_structure(candles_5m, direction, atr, entry)
    if stop_result is None:
        return None, f"{stop_reason} | {diagnostics}"
    stop, risk, stop_distance_percent = stop_result
    if direction == "LONG":
        targets = [entry + risk * MIN_TP1_RR, entry + risk * TP2_RR, entry + risk * TP3_RR]
        resistance = max(c["high"] for c in candles_5m[-31:-1])
        if entry < resistance < targets[0]:
            return None, f"TP1 obstructed by nearby resistance ({fmt(resistance)}) | {diagnostics}"
        if not (stop < entry < targets[0] < targets[1] < targets[2]):
            return None, f"invalid LONG stop/target ordering | {diagnostics}"
    else:
        targets = [entry - risk * MIN_TP1_RR, entry - risk * TP2_RR, entry - risk * TP3_RR]
        support = min(c["low"] for c in candles_5m[-31:-1])
        if targets[0] < support < entry:
            return None, f"TP1 obstructed by nearby support ({fmt(support)}) | {diagnostics}"
        if not (targets[2] < targets[1] < targets[0] < entry < stop):
            return None, f"invalid SHORT stop/target ordering | {diagnostics}"
    if any(target <= 0 for target in targets):
        return None, f"non-positive target price | {diagnostics}"
    leverage, stop_percent, margin_loss_percent = calculate_leverage(entry, stop)
    if leverage is None:
        return None, f"leverage/stop calculation failed | {diagnostics}"
    support_level = min(c["low"] for c in candles_5m[-31:-1])
    resistance_level = max(c["high"] for c in candles_5m[-31:-1])
    signal = {
        "signal_id": f"kraken-linear-{contract}-{direction}-{last['time']}",
        "exchange": "kraken", "exchange_symbol": contract,
        "market_type": "futures", "market": "FUTURES", "category": "linear",
        "symbol": display_symbol(contract), "direction": direction,
        "entry": entry, "stop_loss": stop, "targets": targets,
        "score": score, "rsi": round(rsi, 2), "atr": atr,
        "volume_ratio": round(volume_ratio, 3), "leverage": FIXED_LEVERAGE,
        "stop_distance_percent": stop_percent,
        "estimated_margin_loss_percent": margin_loss_percent,
        "risk_reward": {"tp1": MIN_TP1_RR, "tp2": TP2_RR, "tp3": TP3_RR},
        "support_reference": support_level, "resistance_reference": resistance_level,
        "timeframe": "5m entry / 15m trend", "entry_timeframe": "5m",
        "trend_timeframe": "15m", "candle_time": last["time"],
        "evidence": evidence, "candles": candles_5m[-MAX_CANDLE_COUNT:],
        "created_at": now_utc(),
    }
    return signal, f"QUALIFIED: {direction} score={score} | {diagnostics}"


def create_chart(signal):
    os.makedirs(CHART_DIR, exist_ok=True)
    candles = signal["candles"]
    dates = [datetime.fromtimestamp(c["time"], tz=timezone.utc) for c in candles]
    x_values = mdates.date2num(dates)
    candle_width = (5 / (24 * 60)) * 0.68
    fig, (ax, volume_ax) = plt.subplots(2, 1, figsize=(15, 9), sharex=True,
                                       gridspec_kw={"height_ratios": [4, 1]})
    for i, c in enumerate(candles):
        x = x_values[i]
        color = "#00b894" if c["close"] >= c["open"] else "#e74c3c"
        ax.vlines(x, c["low"], c["high"], color=color, linewidth=1.2, zorder=2)
        body_height = abs(c["close"] - c["open"]) or max(abs(c["close"]) * 0.00002, 1e-12)
        ax.add_patch(patches.Rectangle((x-candle_width/2, min(c["open"], c["close"])), candle_width,
                                      body_height, facecolor=color, edgecolor=color, linewidth=0.8, zorder=3))
        volume_ax.bar(x, c["volume"], width=candle_width, color=color, alpha=0.8)
    ax.axhline(signal["entry"], color="#2980b9", linewidth=1.8, label=f"ENTRY {fmt(signal['entry'])}")
    ax.axhline(signal["stop_loss"], color="#e74c3c", linestyle="--", linewidth=1.8,
                label=f"STOP {fmt(signal['stop_loss'])}")
    for i, target in enumerate(signal["targets"]):
        ax.axhline(target, color=["#27ae60", "#16a085", "#2ecc71"][i], linestyle=":", linewidth=1.6,
                   label=f"TP{i+1} {fmt(target)}")
    ax.set_title(f"CRYPTO EDGE | {signal['symbol']} | {signal['direction']} | 10x | 5m ENTRY / 15m TREND",
                 fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel("Price"); volume_ax.set_ylabel("Volume"); volume_ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.22); volume_ax.grid(True, axis="y", alpha=0.18)
    ax.legend(loc="upper left", fontsize=8, ncol=2, framealpha=0.9)
    volume_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
    fig.autofmt_xdate(); fig.tight_layout()
    safe_symbol = signal["symbol"].replace("/", "_").replace(":", "_")
    path = os.path.join(CHART_DIR, f"{safe_symbol}_{signal['candle_time']}.png")
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def build_caption(signal):
    t = signal["targets"]
    evidence = "\n".join(f"• {item}" for item in signal["evidence"])
    return ("🚨 CRYPTO EDGE | FUTURES SCALP\n\n"
            f"🪙 Pair: {signal['symbol']}\n📈 Direction: {signal['direction']}\n"
            "⏱ Entry: 5m candles | Trend: 15m candles\n\n"
            f"🎯 Reference entry: {fmt(signal['entry'])}\n🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
            f"💰 TP1 (1.3R): {fmt(t[0])}\n💰 TP2 (1.8R): {fmt(t[1])}\n💰 TP3 (2.5R): {fmt(t[2])}\n\n"
            f"⚙️ Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x\n"
            f"📏 Price distance to SL: {signal['stop_distance_percent']}%\n"
            f"🧮 Approx. allocated-margin loss at SL: {signal['estimated_margin_loss_percent']}%\n"
            "Excludes fees, slippage, funding and liquidation effects.\n\n"
            f"📊 Signal score: {signal['score']}\n📉 5m RSI: {signal['rsi']}\n"
            f"📦 Closed-candle volume ratio: {signal['volume_ratio']}x\n"
            f"🧱 Support reference: {fmt(signal['support_reference'])}\n"
            f"🧱 Resistance reference: {fmt(signal['resistance_reference'])}\n\n"
            f"🔎 Evidence:\n{evidence}\n\n🟡 STATUS: WAITING FOR ENTRY\n"
            "Entry is a reference price, not a confirmed fill.\nPaper tracking only. No real order has been placed.")


def scan_market(instrument):
    contract = instrument["contract"]
    try:
        candles_5m = fetch_futures_candles(contract, "5m")
        time.sleep(0.25)
        candles_15m = fetch_futures_candles(contract, "15m")
        signal, reason = evaluate_signal(contract, candles_5m, candles_15m)
        print(f"DIAGNOSTIC {instrument['symbol']} ({contract}): {reason}")
        return signal
    except Exception as exc:
        print(f"DIAGNOSTIC {instrument['symbol']} ({contract}): DATA/API ERROR: {exc}")
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
    if not isinstance(saved_signals, list): saved_signals = []
    universe = fetch_futures_universe()
    print("Eligible Futures contracts:", len(universe))
    attempted = sent_count = 0
    for instrument in universe:
        attempted += 1
        signal = scan_market(instrument)
        if signal is None:
            time.sleep(0.25)
            continue
        if signal["signal_id"] in sent_ids:
            print(f"Already sent; skipping duplicate: {signal['signal_id']}")
            continue
        chart_path = create_chart(signal)
        if telegram_send_photo(chart_path, build_caption(signal)):
            sent_ids.add(signal["signal_id"])
            saved_signals.append({k: v for k, v in signal.items() if k != "candles"})
            sent_count += 1
            print(f"Signal sent: {signal['symbol']} {signal['direction']} score={signal['score']} "
                  f"leverage={signal['leverage']}x SL distance={signal['stop_distance_percent']}%")
        time.sleep(0.35)
        if sent_count >= MAX_SIGNALS_PER_RUN: break
    state["sent"] = list(sent_ids)[-5000:]
    state["signals"] = saved_signals[-MAX_STORED_SIGNALS:]
    state["updated_at"] = now_utc()
    save_state(state)
    print("Markets attempted:", attempted)
    print("Signals sent:", sent_count)
    print("Kraken Futures scan completed.")


if __name__ == "__main__":
    main()
