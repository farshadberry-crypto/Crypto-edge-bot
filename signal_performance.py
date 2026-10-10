
import os
import json
import requests

from datetime import datetime, timezone


# =========================
# CONFIGURATION
# =========================

CMC_API_KEY = os.getenv("CMC_API_KEY")

LOG_FILE = "signal_log.json"
RESULT_FILE = "signal_performance_results.json"

BASE_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

CHECKPOINTS = {
    "1h": 1,
    "4h": 4,
    "24h": 24,
}


# =========================
# FILE HELPERS
# =========================

def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(filename, data):
    temp_file = filename + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, filename)


# =========================
# TIME HELPERS
# =========================

def parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        return None


def utc_now():
    return datetime.now(timezone.utc)


# =========================
# PRICE FETCHING
# =========================

def fetch_prices(coin_ids):
    prices = {}

    if not coin_ids:
        return prices

    if not CMC_API_KEY:
        raise RuntimeError(
            "Missing CMC_API_KEY environment variable."
        )

    unique_ids = sorted(set(str(coin_id) for coin_id in coin_ids))

    for start in range(0, len(unique_ids), 100):
        batch = unique_ids[start:start + 100]

        response = requests.get(
            BASE_URL,
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
            params={
                "id": ",".join(batch),
                "convert": "USD",
            },
            timeout=30,
        )

        print(
            f"CoinMarketCap HTTP status: {response.status_code}"
        )

        response.raise_for_status()

        payload = response.json()
        data = payload.get("data", {})

        for coin_id, item in data.items():
            quote = item.get("quote", {}).get("USD", {})
            price = quote.get("price")

            if price is not None:
                prices[str(coin_id)] = float(price)

    print(f"Prices received: {len(prices)}")
    return prices


# =========================
# PERFORMANCE TRACKER
# =========================

def main():
    print("Starting Signal Performance Tracker...")

    now = utc_now()

    print(f"Current UTC time: {now.isoformat()}")

    signals = load_json(LOG_FILE, [])
    summaries = load_json(RESULT_FILE, [])

    if not isinstance(signals, list):
        raise RuntimeError(
            f"{LOG_FILE} must contain a JSON list."
        )

    if not isinstance(summaries, list):
        raise RuntimeError(
            f"{RESULT_FILE} must contain a JSON list."
        )

    print(f"Signals loaded: {len(signals)}")

    if not signals:
        print("No signals recorded yet.")
        return

    pending = []

    for signal in signals:
        if not isinstance(signal, dict):
            continue

        signal_time = parse_timestamp(
            signal.get("price_time") or signal.get("time")
        )

        coin_id = signal.get("id")
        entry_price = signal.get("price")

        if not signal_time or not coin_id:
            print(
                f"Skipping invalid signal: "
                f"{signal.get('symbol', 'UNKNOWN')}"
            )
            continue

        try:
            entry_price = float(entry_price)
        except (TypeError, ValueError):
            print(
                f"Skipping invalid entry price: "
                f"{signal.get('symbol', 'UNKNOWN')}"
            )
            continue

        if entry_price <= 0:
            continue

        signal.setdefault("results", {})

        if not isinstance(signal["results"], dict):
            print(
                f"Invalid results structure for "
                f"{signal.get('symbol', 'UNKNOWN')}; resetting."
            )
            signal["results"] = {}

        elapsed_hours = (
            now - signal_time
        ).total_seconds() / 3600

        if elapsed_hours < 0:
            print(
                f"Future timestamp; skipping "
                f"{signal.get('symbol', 'UNKNOWN')}"
            )
            continue

        due_checkpoints = [
            checkpoint
            for checkpoint, hours in CHECKPOINTS.items()
            if checkpoint not in signal["results"]
            and elapsed_hours >= hours
        ]

        if due_checkpoints:
            pending.append({
                "signal": signal,
                "coin_id": str(coin_id),
                "entry_price": entry_price,
                "direction": str(
                    signal.get("direction", "BULLISH")
                ).upper(),
                "signal_time": signal_time,
                "due_checkpoints": due_checkpoints,
            })

        print(
            f"{signal.get('symbol', 'UNKNOWN')}: "
            f"age={elapsed_hours:.2f}h, "
            f"pending={due_checkpoints}"
        )

    pending_ids = [
        item["coin_id"] for item in pending
    ]

    print(f"Pending signals: {len(pending)}")

    if not pending:
        print("No checkpoints are due yet.")
        return

    prices = fetch_prices(pending_ids)

    updated = 0
    signals_with_results = 0
    missing_prices = 0

    for item in pending:
        signal = item["signal"]
        coin_id = item["coin_id"]
        entry_price = item["entry_price"]
        direction = item["direction"]
        signal_time = item["signal_time"]

        current_price = prices.get(coin_id)

        if current_price is None:
            missing_prices += 1
            print(
                f"No price returned for "
                f"{signal.get('symbol', coin_id)}"
            )
            continue

        if current_price <= 0:
            print(
                f"Invalid current price for "
                f"{signal.get('symbol', coin_id)}"
            )
            continue

        checked_at = utc_now()
        elapsed_hours = (
            checked_at - signal_time
        ).total_seconds() / 3600

        price_change_pct = (
            (current_price - entry_price) / entry_price
        ) * 100

        directional_pct = (
            -price_change_pct
            if direction in ("BEARISH", "SHORT")
            else price_change_pct
        )

        for checkpoint in item["due_checkpoints"]:
            # Prevent overwriting a checkpoint recorded earlier.
            if checkpoint in signal["results"]:
                continue

            target_hours = CHECKPOINTS[checkpoint]

            signal["results"][checkpoint] = {
                "target_hours": target_hours,
                "actual_elapsed_hours": round(
                    elapsed_hours, 3
                ),
                "checked_at": checked_at.isoformat(),
                "entry_price": entry_price,
                "price": current_price,
                "price_change_pct": round(
                    price_change_pct, 4
                ),
                "directional_change_pct": round(
                    directional_pct, 4
                ),
                "direction": direction,
                "evaluation_version": 2,
            }

            updated += 1

            print(
                f"Recorded {checkpoint} for "
                f"{signal.get('symbol', coin_id)}: "
                f"directional={directional_pct:.4f}%, "
                f"elapsed={elapsed_hours:.2f}h"
            )

        if signal["results"]:
            signals_with_results += 1

    # Save only after successful price fetching and processing.
    save_json(LOG_FILE, signals)

    summary = {
        "updated_at": utc_now().isoformat(),
        "signals_checked": len(signals),
        "pending_signals": len(pending),
        "signals_with_results": signals_with_results,
        "checkpoints_recorded": updated,
        "missing_prices": missing_prices,
        "status": "completed",
        "note": (
            "Checkpoint results use the price available when "
            "the workflow runs, not an exact historical price "
            "at the target hour. actual_elapsed_hours records "
            "the real elapsed time. Directional change is an "
            "estimate, not actual trading profit."
        ),
    }

    summaries.append(summary)
    summaries = summaries[-1000:]

    save_json(RESULT_FILE, summaries)

    print(f"Signals with results: {signals_with_results}")
    print(f"Checkpoints recorded: {updated}")
    print(f"Missing prices: {missing_prices}")
    print("Performance tracking finished.")


if __name__ == "__main__":
    main()
