
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        return None


def main():
    print("Starting Signal Performance Tracker...")

    signals = load_json(LOG_FILE, [])
    summaries = load_json(RESULT_FILE, [])

    if not isinstance(signals, list):
        signals = []

    if not isinstance(summaries, list):
        summaries = []

    now = datetime.now(timezone.utc)

    print(f"Current UTC time: {now.isoformat()}")
    print(f"Signals loaded: {len(signals)}")

    if not signals:
        print("No signals recorded yet.")
        return

    if not CMC_API_KEY:
        raise RuntimeError("Missing CMC_API_KEY environment variable.")

    checkpoints = {"1h": 1, "4h": 4, "24h": 24}
    pending_ids = set()

    for signal in signals:
        signal_time = parse_timestamp(signal.get("time"))
        coin_id = signal.get("id")
        entry_price = signal.get("price")

        if not signal_time or not coin_id or entry_price is None:
            print(f"Skipping invalid signal: {signal.get('symbol', 'UNKNOWN')}")
            continue

        signal.setdefault("results", {})
        elapsed = (now - signal_time).total_seconds() / 3600

        print(
            f"Signal {signal.get('symbol', 'UNKNOWN')}: "
            f"age={elapsed:.2f}h, direction={signal.get('direction')}, "
            f"results={list(signal['results'].keys())}"
        )

        if elapsed < 0:
            print("  Warning: signal timestamp is in the future.")
            continue

        for checkpoint, hours in checkpoints.items():
            if checkpoint not in signal["results"] and elapsed >= hours:
                pending_ids.add(str(coin_id))

    print(f"Pending coin IDs: {sorted(pending_ids)}")

    prices = {}
    ids = sorted(pending_ids)

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]

        try:
            response = requests.get(
                BASE_URL,
                headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
                params={"id": ",".join(batch), "convert": "USD"},
                timeout=30,
            )

            print(f"CoinMarketCap HTTP status: {response.status_code}")
            response.raise_for_status()

            payload = response.json()
            data = payload.get("data", {})

            for coin_id, item in data.items():
                price = item.get("quote", {}).get("USD", {}).get("price")

                if price is not None:
                    prices[str(coin_id)] = float(price)

            print(f"Prices received: {sorted(prices.keys())}")

        except (requests.RequestException, ValueError) as error:
            print(f"CoinMarketCap request failed: {error}")
            raise

    updated = 0
    signals_with_results = 0

    for signal in signals:
        signal_time = parse_timestamp(signal.get("time"))
        coin_id = str(signal.get("id", ""))
        entry_price = signal.get("price")

        if not signal_time or entry_price is None:
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
            if coin_id in pending_ids:
                print(f"No current price returned for {signal.get('symbol', coin_id)}")
            continue

        direction = str(signal.get("direction", "BULLISH")).upper()
        elapsed = (now - signal_time).total_seconds() / 3600

        for checkpoint, hours in checkpoints.items():
            if checkpoint in signal["results"] or elapsed < hours:
                continue

            change_pct = ((current_price - entry_price) / entry_price) * 100
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
                "directional_change_pct": round(directional_pct, 4),
                "direction": direction,
            }

            updated += 1
            print(
                f"Recorded {checkpoint} for {signal.get('symbol', coin_id)}: "
                f"directional change {directional_pct:.4f}%"
            )

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
            "Directional price change is an estimate, not actual trading profit. "
            "Prices are checked when this workflow runs after each checkpoint."
        ),
    }

    summaries.append(summary)
    save_json(RESULT_FILE, summaries)

    print(f"Signals with results: {signals_with_results}")
    print(f"Checkpoint results recorded: {updated}")
    print("Performance tracking finished.")


if __name__ == "__main__":
    main()
