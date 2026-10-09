
import json
import os
from datetime import datetime, timezone

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CMC_API_KEY = os.environ["CMC_API_KEY"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"

HISTORY_FILE = "smart_money_history.json"

MAX_COINS = 100
MAX_ALERTS = 8
MIN_VOLUME_USD = 5_000_000
MIN_HISTORY_POINTS = 3
MIN_VOLUME_RATIO = 1.25
MIN_PRICE_CHANGE = 2.0


def money(value):
    value = float(value)

    for suffix, divisor in [
        ("T", 1_000_000_000_000),
        ("B", 1_000_000_000),
        ("M", 1_000_000),
        ("K", 1_000),
    ]:
        if abs(value) >= divisor:
            return f"${value / divisor:.2f}{suffix}"

    return f"${value:,.4f}"


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        print("History file could not be read. Starting fresh.")
        return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(history, file, indent=2)


def get_market_data():
    response = requests.get(
        CMC_URL,
        headers={
            "X-CMC_PRO_API_KEY": CMC_API_KEY,
            "Accept": "application/json",
        },
        params={
            "start": 1,
            "limit": MAX_COINS,
            "convert": "USD",
            "sort": "market_cap",
            "sort_dir": "desc",
        },
        timeout=30,
    )

    response.raise_for_status()
    result = response.json()

    status = result.get("status", {})
    if status.get("error_code", 0) != 0:
        raise RuntimeError(
            "CoinMarketCap API error: "
            + str(status.get("error_message"))
        )

    return result.get("data", [])


def analyze_coin(coin, history, now):
    coin_id = str(coin["id"])
    quote = coin.get("quote", {}).get("USD", {})

    price = quote.get("price")
    volume = quote.get("volume_24h")
    change = quote.get("percent_change_24h")
    rank = coin.get("cmc_rank")

    if price is None or volume is None or change is None:
        return None

    price = float(price)
    volume = float(volume)
    change = float(change)

    if price <= 0 or volume < MIN_VOLUME_USD:
        return None

    previous = history.get(coin_id, [])

    old_volumes = [
        float(item["volume"])
        for item in previous
        if item.get("volume") is not None
    ]

    old_prices = [
        float(item["price"])
        for item in previous
        if item.get("price") is not None
    ]

    alert = None

    if len(old_volumes) >= MIN_HISTORY_POINTS:
        average_volume = sum(old_volumes) / len(old_volumes)

        if average_volume > 0:
            volume_ratio = volume / average_volume

            previous_high = max(old_prices) if old_prices else price
            previous_low = min(old_prices) if old_prices else price

            breakout_up = price > previous_high
            breakout_down = price < previous_low

            score = 0
            reasons = []

            if volume_ratio >= 2.0:
                score += 30
                reasons.append("24h volume is 2x+ saved baseline")
            elif volume_ratio >= 1.5:
                score += 25
                reasons.append("24h volume is 1.5x+ saved baseline")
            elif volume_ratio >= MIN_VOLUME_RATIO:
                score += 15
                reasons.append("24h volume is above saved baseline")

            if abs(change) >= 5:
                score += 20
                reasons.append("Strong 24h price movement")
            elif abs(change) >= MIN_PRICE_CHANGE:
                score += 15
                reasons.append("Notable 24h price movement")

            if breakout_up:
                score += 30
                reasons.append("Price above saved snapshot highs")
            elif breakout_down:
                score += 30
                reasons.append("Price below saved snapshot lows")

            if change > 0:
                direction = "🟢 BULLISH MOMENTUM"
            elif change < 0:
                direction = "🔴 BEARISH MOMENTUM"
            else:
                direction = "⚪ NEUTRAL"

            if (
                volume_ratio >= MIN_VOLUME_RATIO
                and abs(change) >= MIN_PRICE_CHANGE
                and score >= 50
            ):
                alert = {
                    "name": coin.get("name", "Unknown"),
                    "symbol": coin.get("symbol", "UNKNOWN"),
                    "rank": rank,
                    "price": price,
                    "volume": volume,
                    "volume_ratio": volume_ratio,
                    "change": change,
                    "score": score,
                    "direction": direction,
                    "breakout_up": breakout_up,
                    "breakout_down": breakout_down,
                    "reasons": reasons,
                }

    previous.append({
        "time": now,
        "price": price,
        "volume": volume,
    })

    history[coin_id] = previous[-14:]

    return alert


def build_message(alerts):
    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🛰 SMART MONEY RADAR",
        "📊 MARKET ACTIVITY SCANNER",
        "",
        "Source: CoinMarketCap",
        "Signals use saved market snapshots.",
        "",
    ]

    for item in alerts:
        if item["breakout_up"]:
            setup = "🚀 ABOVE SAVED PRICE RANGE"
        elif item["breakout_down"]:
            setup = "⚠️ BELOW SAVED PRICE RANGE"
        else:
            setup = "📊 MOMENTUM WATCH"

        change_text = f"{item['change']:+.2f}%"

        lines.extend([
            f"{item['direction']}",
            f"💎 {item['symbol']} | Rank #{item['rank']}",
            f"🎯 Signal Score: {item['score']}/80",
            f"💵 Price: {money(item['price'])}",
            f"📈 24H Change: {change_text}",
            f"📊 24H Volume: {money(item['volume'])}",
            f"🔥 Volume vs Baseline: {item['volume_ratio']:.2f}x",
            f"🔎 Setup: {setup}",
            "🧠 Evidence:",
        ])

        for reason in item["reasons"]:
            lines.append(f"• {reason}")

        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "ℹ️ Score measures observed conditions, not probability.",
        "⚠️ Volume snapshots overlap across the 24h window.",
        "⚠️ This does not confirm institutional money flows.",
        "⚠️ Not a guaranteed trading signal.",
        "🎯 DATA OVER HYPE",
        "⚡ STAY AHEAD",
    ])

    return "\n".join(lines)


def send_telegram(message):
    response = requests.post(
        TELEGRAM_URL,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected the message: "
            + str(result)
        )


def main():
    print("Starting Crypto Edge Smart Money Radar...")

    history = load_history()
    coins = get_market_data()
    now = datetime.now(timezone.utc).isoformat()

    alerts = []

    for coin in coins:
        alert = analyze_coin(coin, history, now)

        if alert is not None:
            alerts.append(alert)

    # Save market snapshots even if no alerts are found.
    save_history(history)

    alerts.sort(
        key=lambda item: item["score"],
        reverse=True,
    )
    alerts = alerts[:MAX_ALERTS]

    print(f"Processed {len(coins)} assets.")

    if not alerts:
        print("No signals met the required criteria.")
        return

    send_telegram(build_message(alerts))
    print(f"Sent {len(alerts)} Smart Money Radar alerts.")


if __name__ == "__main__":
    main()
