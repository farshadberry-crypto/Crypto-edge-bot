
import json
import os
from datetime import datetime, timezone

SIGNALS_FILE = "scalp_signals.json"
JOURNAL_FILE = "trade_journal.json"


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

    if category not in ("spot", "linear"):
        return None

    direction = signal.get("direction")

    if direction not in ("LONG", "SHORT"):
        return None

    if category == "spot" and direction != "LONG":
        return None

    try:
        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        targets = [float(value) for value in signal["targets"]]
    except (KeyError, TypeError, ValueError):
        return None

    if entry <= 0 or stop <= 0 or len(targets) != 3:
        return None

    if direction == "LONG":
        if not stop < entry < targets[0] < targets[1] < targets[2]:
            return None
    else:
        if not targets[2] < targets[1] < targets[0] < entry < stop:
            return None

    signal_id = signal.get("signal_id")
    symbol = signal.get("symbol")

    if not signal_id or not symbol:
        return None

    return {
        "signal_id": signal_id,
        "market": "SPOT" if category == "spot" else "FUTURES",
        "category": category,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "targets": targets,
        "score": signal.get("score"),
        "timeframe": signal.get("timeframe", "5m entry / 15m trend"),
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
    print("Crypto Edge Signal Registration")
    print("UTC:", now_utc())

    signals_data = load_json(SIGNALS_FILE, {"sent": []})

    # The current scalp engine only saves sent IDs.
    # Do not fabricate trade details from those IDs.
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

    for signal in signals:
        if not isinstance(signal, dict):
            skipped += 1
            continue

        trade = normalize_signal(signal)

        if trade is None:
            print("Skipped invalid signal.")
            skipped += 1
            continue

        if trade["signal_id"] in existing_ids:
            continue

        trades.append(trade)
        existing_ids.add(trade["signal_id"])
        added += 1

    journal["trades"] = trades
    journal["updated_at"] = now_utc()

    save_json(JOURNAL_FILE, journal)

    print("New trades registered:", added)
    print("Invalid signals skipped:", skipped)
    print("Total journal records:", len(trades))


if __name__ == "__main__":
    main()
