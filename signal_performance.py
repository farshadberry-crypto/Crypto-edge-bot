import os
import json
import requests
from datetime import datetime, timezone

CMC_API_KEY = os.getenv("CMC_API_KEY")
LOG_FILE = "signal_log.json"
RESULT_FILE = "signal_performance_results.json"

BASE_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"


def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def main():
    print("Starting Signal Performance Tracker...")

    signals = load_json(LOG_FILE, [])
    results = load_json(RESULT_FILE, [])

    now = datetime.now(timezone.utc)

    if not signals:
        summary = {
            "updated_at": now.isoformat(),
            "signals_checked": 0,
            "checkpoints_recorded": 0,
            "status": "waiting_for_signals",
            "note": "No signals have been recorded by smart_money.py yet."
        }

        results.append(summary)
        save_json(RESULT_FILE, results)
        print("No signals yet. Empty performance report created.")
        return

    if not CMC_API_KEY:
        raise RuntimeError("Missing CMC_API_KEY environment variable.")

    checkpoints = {"1h": 1, "4h": 4, "24h": 24}
    pending_ids = []

    for signal in signals:
        timestamp = signal.get("timestamp")
        if not timestamp:
            continue

        try:
            signal_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue

        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        for checkpoint, hours in checkpoints.items():
            if checkpoint not in signal.get("results", {}) and \
                    (now - signal_time).total_seconds() >= hours * 3600:
                pending_ids.append(signal.get("coin_id"))

    prices = {}

    if pending_ids:
        ids = list(dict.fromkeys(str(cid) for cid in pending_ids if cid))

        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            response = requests.get(
                BASE_URL,
                headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
                params={"id": ",".join(batch), "convert": "USD"},
                timeout=30
            )
            response.raise_for_status()

            for coin_id, item in response.json().get("data", {}).items():
                price = item.get("quote", {}).get("USD", {}).get("price")
                if price is not None:
                    prices[str(coin_id)] = float(price)

    updated = 0

    for signal in signals:
        coin_id = str(signal.get("coin_id", ""))
        entry_price = signal.get("entry_price")
        timestamp = signal.get("timestamp")

        if not coin_id or not entry_price or not timestamp:
            continue

        try:
            signal_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue

        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        signal.setdefault("results", {})

        for checkpoint, hours in checkpoints.items():
            if checkpoint in signal["results"]:
                continue

            if (now - signal_time).total_seconds() < hours * 3600:
                continue

            current_price = prices.get(coin_id)
            if current_price is None:
                continue

            change_pct = (
                (current_price - float(entry_price)) / float(entry_price)
            ) * 100

            direction = signal.get("direction", "bullish").lower()
            directional_pct = -change_pct if direction in ("bearish", "short") else change_pct

            signal["results"][checkpoint] = {
                "checked_at": now.isoformat(),
                "price": current_price,
                "price_change_pct": round(change_pct, 4),
                "directional_change_pct": round(directional_pct, 4)
            }
            updated += 1

    save_json(LOG_FILE, signals)

    summary = {
        "updated_at": now.isoformat(),
        "signals_checked": len(signals),
        "checkpoints_recorded": updated,
        "status": "completed",
        "note": "Directional price change is an estimate, not actual trading profit."
    }

    results.append(summary)
    save_json(RESULT_FILE, results)

    print(f"Signals loaded: {len(signals)}")
    print(f"Checkpoint results recorded: {updated}")
    print("Performance tracking finished.")


if __name__ == "__main__":
    main()
