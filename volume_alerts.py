import json
import os
from datetime import datetime, timezone

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MARKET_URL = "https://api.coinpaprika.com/v1/tickers"

HISTORY_FILE = "volume_history.json"

MIN_RANK = 1
MAX_RANK = 100
MIN_HISTORY_POINTS = 3
MIN_VOLUME_MULTIPLE = 2.0
MAX_ALERTS = 10


def format_money(value):
    value = float(value)

    for unit, divisor in [
        ("T", 1_000_000_000_000),
        ("B", 1_000_000_000),
        ("M", 1_000_000),
        ("K", 1_000),
    ]:
        if abs(value) >= divisor:
            return f"${value / divisor:.2f}{unit}"

    return f"${value:,.0f}"


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    with open(HISTORY_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(history, file, indent=2)


def get_market_data():
    response = requests.get(
        MARKET_URL,
        params={"quotes": "USD"},
        timeout=30,
    )
    response.raise_for_status()

    coins = response.json()

    return [
        coin for coin in coins
        if coin.get("rank")
        and MIN_RANK <= coin["rank"] <= MAX_RANK
        and coin.get("quotes", {}).get("USD", {}).get("volume_24h")
        and coin.get("quotes", {}).get("USD", {}).get("price") is not None
        and coin.get("quotes", {}).get("USD", {}).get("percent_change_24h") is not None
    ]


def main():
    print("Collecting volume snapshots...")

    history = load_history()
    coins = get_market_data()

    now = datetime.now(timezone.utc).isoformat()
    alerts = []

    for coin in coins:
        symbol = coin["symbol"].upper()
        coin_id = coin["id"]
        quote = coin["quotes"]["USD"]

        current_volume = float(quote["volume_24h"])
        price = float(quote["price"])
        price_change = float(quote["percent_change_24h"])

        coin_history = history.get(coin_id, [])
        previous_volumes = [
            float(item["volume"])
            for item in coin_history
            if item.get("volume") is not None
        ]

        if len(previous_volumes) >= MIN_HISTORY_POINTS:
            average_volume = sum(previous_volumes) / len(previous_volumes)

            if average_volume > 0:
                multiple = current_volume / average_volume

                if multiple >= MIN_VOLUME_MULTIPLE:
                    alerts.append({
                        "symbol": symbol,
                        "rank": coin["rank"],
                        "multiple": multiple,
                        "volume": current_volume,
                        "price": price,
                        "change": price_change,
                    })

        coin_history.append({
            "time": now,
            "volume": current_volume,
        })

        # Keep the latest 7 snapshots for the rolling baseline.
        history[coin_id] = coin_history[-7:]

    save_history(history)

    alerts.sort(
        key=lambda item: item["multiple"],
        reverse=True,
    )
    alerts = alerts[:MAX_ALERTS]

    if not alerts:
        print("No unusual volume alerts found.")
        return

    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🚨 UNUSUAL VOLUME ALERTS",
        "📊 TOP 100 CRYPTO ASSETS",
        "",
        "Volume is compared with saved snapshots.",
        "",
    ]

    for item in alerts:
        if item["change"] > 0:
            change_text = f"🟢 +{item['change']:.2f}%"
        elif item["change"] < 0:
            change_text = f"🔴 {item['change']:.2f}%"
        else:
            change_text = "⚪ 0.00%"

        lines.extend([
            f"💎 {item['symbol']} | Rank #{item['rank']}",
            f"📈 Volume: {format_money(item['volume'])}",
            f"🔥 Baseline Multiple: {item['multiple']:.2f}x",
            f"💵 Price: {format_money(item['price'])}",
            f"📊 24H Price Change: {change_text}",
            "",
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ Volume spikes are not buy signals.",
        "🎯 DATA OVER HYPE",
        "⚡ STAY AHEAD",
    ])

    message = "\n".join(lines)

    result = requests.post(
        TELEGRAM_URL,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=30,
    )
    result.raise_for_status()

    if not result.json().get("ok"):
        raise RuntimeError("Telegram rejected the volume report.")

    print(f"Sent {len(alerts)} unusual volume alerts.")


if __name__ == "__main__":
    main()
