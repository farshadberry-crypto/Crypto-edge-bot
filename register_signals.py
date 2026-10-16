
import json
import os
from datetime import datetime, timezone

SIGNALS_FILE = "scalp_signals.json"
JOURNAL_FILE = "trade_journal.json"

MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 5.0


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


def normalize_signal(signal):
    category = signal.get("category")
    direction = signal.get("direction")
    market_type = signal.get("market_type")

    if category != "linear" or market_type != "futures":
        return None

    if direction not in ("LONG", "SHORT"):
        return None

    try:
        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        targets = [float(value) for value in signal["targets"]]
        leverage = float(signal["leverage"])
        stop_distance_percent = float(
            signal["stop_distance_percent"]
        )
        estimated_margin_loss_percent = float(
            signal["estimated_margin_loss_percent"]
        )
    except (KeyError, TypeError, ValueError):
        return None

    if not all(
        map(
            lambda value: value > 0 and value < float("inf"),
            [entry, stop, leverage, stop_distance_percent,
             estimated_margin_loss_percent] + targets,
        )
    ):
        return None

    if len(targets) != 3:
        return None

    if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
        return None

    if direction == "LONG":
        if not stop < entry < targets[0] < targets[1] < targets[2]:
            return None
    else:
        if not targets[2] < targets[1] < targets[0] < entry < stop:
            return None

    actual_stop_distance = abs(entry - stop) / entry * 100

    # Reject risk metadata that materially disagrees with the prices.
    if abs(actual_stop_distance - stop_distance_percent) > max(
        0.15, actual_stop_distance * 0.10
    ):
        return None

    expected_margin_loss = actual_stop_distance * leverage

    if abs(
        expected_margin_loss - estimated_margin_loss_percent
    ) > max(0.25, expected_margin_loss * 0.15):
        return None

    signal_id = signal.get("signal_id")
    symbol = signal.get("symbol")
    exchange_symbol = signal.get("exchange_symbol")

    if not signal_id or not symbol or not exchange_symbol:
        return None

    if str(signal.get("exchange", "")).lower() != "kraken":
        return None

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
        "leverage": leverage,
        "stop_distance_percent": actual_stop_distance,
        "estimated_margin_loss_percent": expected_margin_loss,
        "score": signal.get("score"),
        "rsi": signal.get("rsi"),
        "atr": signal.get("atr"),
        "volume_ratio": signal.get("volume_ratio"),
        "timeframe": signal.get(
            "timeframe",
            "5m entry / 15m trend",
        ),
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
    }


def main():
    print("Crypto Edge Futures Signal Registration")
    print("UTC:", now_utc())

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
        if isinstance(trade, dict)
    }

    added = 0
    skipped = 0
    duplicates = 0

    for signal in signals:
        if not isinstance(signal, dict):
            skipped += 1
            continue

        trade = normalize_signal(signal)

        if trade is None:
            skipped += 1
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
            f"stop_distance={trade['stop_distance_percent']:.3f}%"
        )

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_json(JOURNAL_FILE, journal)

    print("New Futures trades registered:", added)
    print("Invalid or non-Futures signals skipped:", skipped)
    print("Duplicate signals skipped:", duplicates)
    print("Total journal records:", len(trades))


if __name__ == "__main__":
    main()
