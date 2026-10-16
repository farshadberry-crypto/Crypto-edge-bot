
import json
import os
import math
from datetime import datetime, timezone

SIGNALS_FILE = "scalp_signals.json"
JOURNAL_FILE = "trade_journal.json"

FIXED_LEVERAGE = 10.0
LEVERAGE_TOLERANCE = 0.001
MIN_SCORE = 6
MIN_VOLUME_RATIO = 1.20
MAX_STOP_DISTANCE_PERCENT = 1.20


def now_utc():
    return datetime.now(timezone.utc).isoformat()


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
    except (TypeError, ValueError):
        return False


def normalize_signal(signal):
    if not isinstance(signal, dict):
        return None, "not an object"

    if str(signal.get("exchange", "")).lower() != "kraken":
        return None, "not Kraken"

    if signal.get("market_type") != "futures":
        return None, "not futures"

    if signal.get("category") != "linear":
        return None, "not linear"

    direction = signal.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None, "invalid direction"

    try:
        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        targets = [float(value) for value in signal["targets"]]
        leverage = float(signal["leverage"])
        score = int(signal.get("score", 0))
        volume_ratio = float(signal.get("volume_ratio", 0))
    except (KeyError, TypeError, ValueError):
        return None, "missing or invalid numeric fields"

    numbers = [entry, stop, leverage, volume_ratio] + targets
    if not all(math.isfinite(value) for value in numbers):
        return None, "non-finite number"

    if not all(value > 0 for value in [entry, stop, leverage] + targets):
        return None, "non-positive price or leverage"

    if len(targets) != 3:
        return None, "must have exactly three targets"

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

    actual_stop_distance = abs(entry - stop) / entry * 100

    if actual_stop_distance <= 0 or actual_stop_distance > MAX_STOP_DISTANCE_PERCENT:
        return None, "stop distance outside allowed range"

    expected_margin_loss = actual_stop_distance * FIXED_LEVERAGE

    signal_id = signal.get("signal_id")
    exchange_symbol = signal.get("exchange_symbol")
    symbol = display_symbol(exchange_symbol, signal.get("symbol"))

    if not signal_id or not exchange_symbol or not symbol:
        return None, "missing signal identifier or symbol"

    return {
        "signal_id": signal_id,
        "exchange": "kraken",
        "exchange_symbol": exchange_symbol,
        "market_type": "futures",
        "market": "FUTURES",
        "category": "linear",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "leverage": FIXED_LEVERAGE,
        "stop_distance_percent": actual_stop_distance,
        "estimated_margin_loss_percent": expected_margin_loss,
        "score": score,
        "rsi": signal.get("rsi"),
        "atr": signal.get("atr"),
        "volume_ratio": volume_ratio,
        "timeframe": signal.get("timeframe", "5m entry / 15m trend"),
        "candle_time": signal.get("candle_time"),
        "evidence": signal.get("evidence", []),
        "created_at": signal.get("created_at", now_utc()),
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
    print("Crypto Edge Futures Signal Registration")
    print("UTC:", now_utc())
    print("Required leverage: 10x")
    print("Minimum score:", MIN_SCORE)
    print("Minimum volume ratio:", MIN_VOLUME_RATIO)

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

    journal = load_json(JOURNAL_FILE, {"trades": []})
    if not isinstance(journal, dict):
        journal = {"trades": []}

    trades = journal.get("trades", [])
    if not isinstance(trades, list):
        trades = []

    existing_ids = {
        trade.get("signal_id")
        for trade in trades
        if isinstance(trade, dict) and trade.get("signal_id")
    }

    added = 0
    skipped = 0
    duplicates = 0

    for signal in signals:
        trade, reason = normalize_signal(signal)

        if trade is None:
            skipped += 1
            signal_id = signal.get("signal_id", "unknown") if isinstance(signal, dict) else "unknown"
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
            f"stop={trade['stop_distance_percent']:.3f}% "
            f"margin-risk~{trade['estimated_margin_loss_percent']:.2f}%"
        )

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_json(JOURNAL_FILE, journal)

    print("New Futures trades registered:", added)
    print("Invalid or rejected signals:", skipped)
    print("Duplicate signals skipped:", duplicates)
    print("Total journal records:", len(trades))


if __name__ == "__main__":
    main()
