
import json
import os
import math
from datetime import datetime, timezone, timedelta

SIGNALS_FILE = "scalp_signals.json"
JOURNAL_FILE = "trade_journal.json"

FIXED_LEVERAGE = 10.0
LEVERAGE_TOLERANCE = 0.001
MIN_SCORE = 6
MIN_VOLUME_RATIO = 1.20
MAX_STOP_DISTANCE_PERCENT = 1.20
MAX_SIGNAL_AGE_HOURS = 6
MAX_FUTURE_CLOCK_SKEW_MINUTES = 5


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"

        parsed = datetime.fromisoformat(normalized)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    temporary = path + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, path)


def display_symbol(exchange_symbol, fallback=""):
    symbol = str(exchange_symbol or fallback).strip().upper()

    for prefix in ("PF_", "PI_"):
        if symbol.startswith(prefix):
            symbol = symbol[len(prefix):]
            break

    for suffix in ("USDT", "USD", "USDC", "EUR", "GBP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            base = symbol[:-len(suffix)]

            if base == "XBT":
                base = "BTC"

            return f"{base}/{suffix}"

    return symbol


def finite_positive(value):
    try:
        number = float(value)
        return math.isfinite(number) and number > 0
    except (TypeError, ValueError, OverflowError):
        return False


def normalize_signal(signal, current_time):
    if not isinstance(signal, dict):
        return None, "not an object"

    if str(signal.get("exchange", "")).lower() != "kraken":
        return None, "not Kraken"

    if str(signal.get("market_type", "")).lower() != "futures":
        return None, "not futures"

    if str(signal.get("category", "")).lower() != "linear":
        return None, "not linear futures"

    direction = str(signal.get("direction", "")).upper()

    if direction not in ("LONG", "SHORT"):
        return None, "invalid direction"

    try:
        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        targets_raw = signal["targets"]

        if not isinstance(targets_raw, list) or len(targets_raw) != 3:
            return None, "must have exactly three targets"

        targets = [float(value) for value in targets_raw]
        leverage = float(signal["leverage"])
        score_number = float(signal.get("score", 0))
        volume_ratio = float(signal.get("volume_ratio", 0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "missing or invalid numeric fields"

    numbers = [entry, stop, leverage, score_number, volume_ratio] + targets

    if not all(math.isfinite(value) for value in numbers):
        return None, "non-finite number"

    if not all(value > 0 for value in [entry, stop, leverage] + targets):
        return None, "non-positive price or leverage"

    if not score_number.is_integer():
        return None, "score must be an integer"

    score = int(score_number)

    if abs(leverage - FIXED_LEVERAGE) > LEVERAGE_TOLERANCE:
        return None, "leverage is not exactly 10x"

    if score < MIN_SCORE:
        return None, "score below minimum"

    if volume_ratio < MIN_VOLUME_RATIO:
        return None, "volume ratio below minimum"

    if direction == "LONG":
        if not stop < entry < targets[0] < targets[1] < targets[2]:
            return None, "invalid LONG price ordering"
    else:
        if not targets[2] < targets[1] < targets[0] < entry < stop:
            return None, "invalid SHORT price ordering"

    stop_distance = abs(entry - stop) / entry * 100

    if not 0 < stop_distance <= MAX_STOP_DISTANCE_PERCENT:
        return None, "stop distance outside allowed range"

    signal_id = signal.get("signal_id")
    exchange_symbol = signal.get("exchange_symbol")
    symbol = display_symbol(exchange_symbol, signal.get("symbol"))

    if not signal_id or not exchange_symbol or not symbol:
        return None, "missing signal identifier or symbol"

    created_at_raw = signal.get("created_at")
    created_at = parse_timestamp(created_at_raw) if created_at_raw else current_time

    if created_at is None:
        return None, "invalid signal creation timestamp"

    age = current_time - created_at

    if age > timedelta(hours=MAX_SIGNAL_AGE_HOURS):
        return None, "signal is older than 6 hours"

    if age < -timedelta(minutes=MAX_FUTURE_CLOCK_SKEW_MINUTES):
        return None, "signal timestamp is too far in the future"

    return {
        "signal_id": str(signal_id),
        "exchange": "kraken",
        "exchange_symbol": str(exchange_symbol),
        "market_type": "futures",
        "market": "FUTURES",
        "category": "linear",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "leverage": FIXED_LEVERAGE,
        "stop_distance_percent": stop_distance,
        "estimated_margin_loss_percent": stop_distance * FIXED_LEVERAGE,
        "score": score,
        "rsi": signal.get("rsi"),
        "atr": signal.get("atr"),
        "volume_ratio": volume_ratio,
        "timeframe": signal.get("timeframe", "5m entry / 15m trend"),
        "candle_time": signal.get("candle_time"),
        "evidence": signal.get("evidence", []),
        "created_at": created_at.isoformat(),
        "status": "WAITING_ENTRY",
        "entry_confirmed_at": None,
        "confirmed_entry_price": None,
        "closed_at": None,
        "last_checked_at": None,
        "targets_hit": [],
        "exits": [],
        "remaining_fraction": 1.0,
        "realized_pnl_percent": 0.0,
        "messages_sent": [],
        "result": None,
    }, None


def main():
    current_time = datetime.now(timezone.utc)

    print("Crypto Edge Futures Signal Registration v2.0")
    print("UTC:", current_time.isoformat())
    print("Market: Kraken linear Futures only")
    print("Required leverage: 10x")
    print("Minimum score:", MIN_SCORE)
    print("Minimum volume ratio:", MIN_VOLUME_RATIO)
    print("Maximum signal age:", MAX_SIGNAL_AGE_HOURS, "hours")

    signals_data = load_json(
        SIGNALS_FILE,
        {"sent": [], "signals": []},
    )

    if isinstance(signals_data, dict):
        signals = signals_data.get("signals", [])
    elif isinstance(signals_data, list):
        signals = signals_data
    else:
        signals = []

    if not isinstance(signals, list):
        signals = []

    journal = load_json(JOURNAL_FILE, {"trades": []})

    if not isinstance(journal, dict):
        journal = {"trades": []}

    trades = journal.get("trades", [])

    if not isinstance(trades, list):
        trades = []

    existing_ids = {
        str(trade.get("signal_id"))
        for trade in trades
        if isinstance(trade, dict) and trade.get("signal_id")
    }

    added = 0
    skipped = 0
    duplicates = 0

    for signal in signals:
        trade, reason = normalize_signal(signal, current_time)

        signal_id = (
            str(signal.get("signal_id", "unknown"))
            if isinstance(signal, dict)
            else "unknown"
        )

        if trade is None:
            skipped += 1
            print(f"Skipped {signal_id}: {reason}")
            continue

        if trade["signal_id"] in existing_ids:
            duplicates += 1
            continue

        trades.append(trade)
        existing_ids.add(trade["signal_id"])
        added += 1

        print(
            f"Registered: {trade['symbol']} {trade['direction']} "
            f"leverage={trade['leverage']:.1f}x "
            f"score={trade['score']} "
            f"volume={trade['volume_ratio']:.2f}x "
            f"stop={trade['stop_distance_percent']:.3f}% "
            f"estimated_margin_risk~"
            f"{trade['estimated_margin_loss_percent']:.2f}%"
        )

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_json(JOURNAL_FILE, journal)

    print("New Futures trades registered:", added)
    print("Invalid, rejected, or expired signals:", skipped)
    print("Duplicate signals skipped:", duplicates)
    print("Total journal records:", len(trades))


if __name__ == "__main__":
    main()
