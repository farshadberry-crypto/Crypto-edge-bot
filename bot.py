
import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

API_URL = "https://api.coinpaprika.com/v1/tickers"
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def format_price(value):
    value = float(value)

    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.3f}"
    return "$" + f"{value:.8f}".rstrip("0").rstrip(".")


def format_volume(value):
    value = float(value)

    for unit, divisor in [
        ("T", 1_000_000_000_000),
        ("B", 1_000_000_000),
        ("M", 1_000_000),
        ("K", 1_000),
    ]:
        if value >= divisor:
            return f"${value / divisor:.2f}{unit}"

    return f"${value:,.0f}"


def main():
    print("📡 Fetching cryptocurrency market data...")

    response = requests.get(
        API_URL,
        params={"quotes": "USD"},
        timeout=30,
    )
    response.raise_for_status()

    coins = response.json()

    coins = [
        coin for coin in coins
        if coin.get("rank")
        and coin.get("quotes", {}).get("USD", {}).get("price") is not None
    ]

    coins.sort(key=lambda coin: coin["rank"])
    coins = coins[:25]

    if not coins:
        raise RuntimeError("No cryptocurrency data received.")

    lines = []

    for coin in coins:
        quote = coin["quotes"]["USD"]

        symbol = coin["symbol"].upper()
        price = format_price(quote["price"])
        change = quote.get("percent_change_24h")
        volume = quote.get("volume_24h")

        if change is None:
            change_text = "⚪ N/A"
        elif change > 0:
            change_text = f"🟢 +{change:.2f}%"
        elif change < 0:
            change_text = f"🔴 {change:.2f}%"
        else:
            change_text = "⚪ 0.00%"

        volume_text = (
            format_volume(volume)
            if volume is not None
            else "N/A"
        )

        lines.append(
            f"#{coin['rank']} 🪙 {symbol}\n"
            f"   💵 Price: {price}\n"
            f"   📊 24H Change: {change_text}\n"
            f"   💰 24H Volume: {volume_text}"
        )

    message = (
        "⚡ CRYPTO EDGE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 MARKET PULSE\n"
        "📊 TOP 25 CRYPTO ASSETS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(lines)
        + "\n\n━━━━━━━━━━━━━━━━━━\n"
        "📈 Market Data: CoinPaprika\n"
        "🕒 Price Changes: 24 Hours\n"
        "🎯 DATA OVER HYPE\n"
        "⚡ STAY AHEAD"
    )

    print("📨 Sending market report to Telegram...")

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
        raise RuntimeError("Telegram rejected the market report.")

    print(
        f"✅ Market Pulse sent successfully: "
        f"{len(coins)} cryptocurrencies."
    )


if __name__ == "__main__":
    main()
