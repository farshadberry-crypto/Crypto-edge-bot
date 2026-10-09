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


def parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        return None


def main():
    print("Starting Signal Performance Tracker...")

    signals = load_json(LOG_FILE, [])
    results = load_json(RESULT_FILE, [])

    if not isinstance(signals, list):
        signals = []

    if not isinstance(results, list):
        results = []

    now = datetime.now(timezone.utc)

    if not signals:
        print("No signals recorded yet.")
        return

    if not CMC_API_KEY:
        raise RuntimeError(
            "Missing CMC_API_KEY environment variable."
        )

    checkpoints = {
        "1h": 1,
        "4h": 4,
        "24h": 24,
    }

    pending_ids = set()

    for signal in signals:
        signal_time = parse_timestamp(signal.get("time"))
        coin_id = signal.get("id")
        entry_price = signal.get("price")

        if not signal_time or not coin_id or not entry_price:
            continue

        signal.setdefault("results", {})

        for checkpoint, hours in checkpoints.items():
            elapsed = (now - signal_time).total_seconds()

            if (
                checkpoint not in signal["results"]
                and elapsed >= hours * 3600
            ):
                pending_ids.add(str(coin_id))

    prices = {}

    ids = sorted(pending_ids)

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]

        response = requests.get(
            BASE_URL,
            headers={
                "X-CMC_PRO_API_KEY": CMC_API_KEY,
            },
            params={
                "id": ",".join(batch),
                "convert": "USD",
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json().get("data", {})

        for coin_id, item in data.items():
            price = (
                item.get("quote", {})
                .get("USD", {})
                .get("price")
            )

            if price is not None:
                prices[str(coin_id)] = float(price)

    updated = 0
    signals_with_results = 0

    for signal in signals:
        signal_time = parse_timestamp(signal.get("time"))
        coin_id = str(signal.get("id", ""))
        entry_price = signal.get("price")

        if (
            not signal_time
            or not coin_id
            or entry_price is None
        ):
            continue

        try:
            entry_price = float(entry_price)
        except (ValueError, TypeError):
            continue

        if entry_price <= 0:
            continue

        signal.setdefault("results", {})
        current_price = prices.get(coin_id)

        if current_price is None:
            continue

        direction = str(
            signal.get("direction", "BULLISH")
        ).upper()

        for checkpoint, hours in checkpoints.items():
            elapsed = (now - signal_time).total_seconds()

            if checkpoint in signal["results"]:
                continue

            if elapsed < hours * 3600:
                continue

            change_pct = (
                (current_price - entry_price) / entry_price
            ) * 100

            directional_pct = (
                -change_pct
                if direction in ("BEARISH", "SHORT")
                else change_pct
            )

            signal["results"][checkpoint] = {
                "checked_at": now.isoformat(),
                "entry_price": entry_price,
                "price": current_price,
                "price_change_pct": round(change_pct, 4),
                "directional_change_pct": round(
                    directional_pct, 4
                ),
                "direction": direction,
            }

            updated += 1

        if signal["results"]:
            signals_with_results += 1

    save_json(LOG_FILE, signals)

    summary = {
        "updated_at": now.isoformat(),
        "signals_checked": len(signals),
        "signals_with_results": signals_with_results,
        "checkpoints_recorded": updated,
        "status": "completed",
        "note": (
            "Directional price change is an estimate, "
            "not actual trading profit. Prices are checked "
            "when this workflow runs after each checkpoint."
        ),
    }

    results.append(summary)
    save_json(RESULT_FILE, results)

    print(f"Signals loaded: {len(signals)}")
    print(f"Signals with results: {signals_with_results}")
    print(f"Checkpoint results recorded: {updated}")
    print("Performance tracking finished.")


if __name__ == "__main__":
    main()
