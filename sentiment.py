import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
GLOBAL_MARKET_URL = "https://api.coinpaprika.com/v1/global"


def format_money(value):
    if value is None:
        return "N/A"

    value = float(value)

    for unit, divisor in [
        ("T", 1_000_000_000_000),
        ("B", 1_000_000_000),
        ("M", 1_000_000),
    ]:
        if abs(value) >= divisor:
            return f"${value / divisor:.2f}{unit}"

    return f"${value:,.0f}"


def get_sentiment_label(value):
    if value <= 24:
        return "EXTREME FEAR"
    elif value <= 44:
        return "FEAR"
    elif value <= 55:
        return "NEUTRAL"
    elif value <= 74:
        return "GREED"
    else:
        return "EXTREME GREED"


def get_market_interpretation(value):
    if value <= 24:
        return "Market sentiment is extremely fearful."
    elif value <= 44:
        return "Market sentiment is fearful and cautious."
    elif value <= 55:
        return "Market sentiment is relatively neutral."
    elif value <= 74:
        return "Market sentiment leans toward greed."
    else:
        return "Market sentiment shows extreme greed."


def main():
    print("Collecting market sentiment data...")

    fear_response = requests.get(
        FEAR_GREED_URL,
        timeout=30,
    )
    fear_response.raise_for_status()

    fear_data = fear_response.json()["data"][0]

    fear_value = int(fear_data["value"])
    fear_label = get_sentiment_label(fear_value)

    market_response = requests.get(
        GLOBAL_MARKET_URL,
        timeout=30,
    )
    market_response.raise_for_status()

    market_data = market_response.json()

    market_cap = market_data.get("market_cap_usd")
    btc_dominance = market_data.get(
        "bitcoin_dominance_percentage"
    )
    volume_24h = market_data.get("volume_24h_usd")
    market_change = market_data.get(
        "market_cap_change_24h"
    )

    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🧠 MARKET SENTIMENT",
        "📅 DAILY MARKET OVERVIEW",
        "",
        "😨 FEAR & GREED INDEX",
        f"📊 Score: {fear_value}/100",
        f"🎭 Sentiment: {fear_label}",
        "",
        "🌍 GLOBAL MARKET",
        f"💎 Total Market Cap: {format_money(market_cap)}",
        (
            f"₿ Bitcoin Dominance: {float(btc_dominance):.2f}%"
            if btc_dominance is not None
            else "₿ Bitcoin Dominance: N/A"
        ),
        f"💰 24H Trading Volume: {format_money(volume_24h)}",
    ]

    if market_change is not None:
        change = float(market_change)
        emoji = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
        lines.append(
            f"📈 Market Cap Change (24H): {emoji} {change:+.2f}%"
        )

    lines.extend([
        "",
        "🧭 MARKET INTERPRETATION",
        get_market_interpretation(fear_value),
        "",
        "⚠️ Sentiment is not a trading signal.",
        "━━━━━━━━━━━━━━━━━━",
        "🎯 DATA OVER HYPE",
        "⚡ STAY AHEAD",
    ])

    message = "\n".join(lines)

    print("Sending Market Sentiment report...")

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
        raise RuntimeError("Telegram rejected the report.")

    print("Market Sentiment report sent successfully.")


if __name__ == "__main__":
    main()
