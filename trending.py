
import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

COINS_URL = "https://api.coinpaprika.com/v1/tickers"
TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"


def format_price(value):
    value = float(value)

    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.3f}"

    return "$" + f"{value:.8f}".rstrip("0").rstrip(".")


def format_money(value):
    if value is None:
        return "N/A"

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


def get_market_coins():
    response = requests.get(
        COINS_URL,
        params={"quotes": "USD"},
        timeout=30,
    )
    response.raise_for_status()

    coins = response.json()

    valid_coins = []

    for coin in coins:
        quote = coin.get("quotes", {}).get("USD", {})

        if (
            coin.get("rank")
            and quote.get("price") is not None
            and quote.get("volume_24h") is not None
            and quote.get("percent_change_24h") is not None
        ):
            if float(quote["volume_24h"]) > 0:
                valid_coins.append(coin)

    return valid_coins


def get_trending_coins():
    response = requests.get(
        TRENDING_URL,
        timeout=30,
        headers={"accept": "application/json"},
    )
    response.raise_for_status()

    data = response.json()
    results = []

    for entry in data.get("coins", [])[:5]:
        item = entry.get("item", {})

        name = item.get("name", "Unknown")
        symbol = item.get("symbol", "").upper()
        rank = item.get("market_cap_rank")

        rank_text = f" | Rank #{rank}" if rank else ""

        results.append(
            f"🔥 {name} ({symbol}){rank_text}"
        )

    return results


def main():
    print("📡 Collecting trending and market data...")

    coins = get_market_coins()

    if not coins:
        raise RuntimeError("No market data received.")

    # Avoid very low-ranked assets in gainers and losers.
    ranked_coins = [
        coin for coin in coins
        if 1 <= coin["rank"] <= 100
    ]

    if not ranked_coins:
        raise RuntimeError("No eligible ranked assets found.")

    gainers = sorted(
        ranked_coins,
        key=lambda c: c["quotes"]["USD"]["percent_change_24h"],
        reverse=True,
    )[:5]

    losers = sorted(
        ranked_coins,
        key=lambda c: c["quotes"]["USD"]["percent_change_24h"],
    )[:5]

    volume_coins = sorted(
        ranked_coins,
        key=lambda c: c["quotes"]["USD"]["volume_24h"],
        reverse=True,
    )[:5]

    message_parts = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🔥 TRENDING & MARKET MOVERS",
        "🕒 24-HOUR MARKET REPORT",
        "",
        "🚀 TOP GAINERS",
    ]

    for coin in gainers:
        quote = coin["quotes"]["USD"]
        change = quote["percent_change_24h"]

        message_parts.append(
            f"🟢 {coin['symbol'].upper()} | "
            f"{change:+.2f}% | "
            f"{format_price(quote['price'])}"
        )

    message_parts.extend([
        "",
        "🔻 TOP LOSERS",
    ])

    for coin in losers:
        quote = coin["quotes"]["USD"]
        change = quote["percent_change_24h"]

        message_parts.append(
            f"🔴 {coin['symbol'].upper()} | "
            f"{change:+.2f}% | "
            f"{format_price(quote['price'])}"
        )

    message_parts.extend([
        "",
        "💰 HIGHEST 24H TRADING VOLUME",
    ])

    for coin in volume_coins:
        quote = coin["quotes"]["USD"]

        message_parts.append(
            f"💎 {coin['symbol'].upper()} | "
            f"{format_money(quote['volume_24h'])}"
        )

    message_parts.extend([
        "",
        "🔎 TRENDING SEARCHES",
    ])

    try:
        trending = get_trending_coins()
        message_parts.extend(trending)

    except requests.RequestException as error:
        print(f"Trending search temporarily unavailable: {error}")
        message_parts.append("⚪ Trending data temporarily unavailable.")

    message_parts.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ High volatility. Verify before trading.",
        "🎯 DATA OVER HYPE",
        "⚡ STAY AHEAD",
    ])

    message = "\n".join(message_parts)

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

    print("✅ Trending report sent successfully.")


if __name__ == "__main__":
    main()
