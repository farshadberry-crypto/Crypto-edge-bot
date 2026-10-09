
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
BINANCE_URL = "https://api.binance.com"

MAX_PAIRS = 60
MAX_ALERTS = 8
MIN_QUOTE_VOLUME = 5_000_000
MIN_VOLUME_RATIO = 1.5
MIN_SCORE = 55
CANDLE_INTERVAL = "1h"
CANDLE_COUNT = 22

EXCLUDED_BASES = {
    "USDC", "USDT", "BUSD", "FDUSD", "TUSD",
    "USDE", "USDD", "DAI", "EUR", "TRY",
    "BRL", "USDP", "PYUSD", "USTC"
}


def get_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        timeout=20,
        headers={"User-Agent": "CryptoEdgeRadar/1.0"},
    )
    response.raise_for_status()
    return response.json()


def money(value):
    value = float(value)

    for suffix, divisor in [
        ("B", 1_000_000_000),
        ("M", 1_000_000),
        ("K", 1_000),
    ]:
        if abs(value) >= divisor:
            return f"${value / divisor:.2f}{suffix}"

    return f"${value:,.4f}"


def get_top_pairs():
    tickers = get_json(
        f"{BINANCE_URL}/api/v3/ticker/24hr"
    )

    exchange_info = get_json(
        f"{BINANCE_URL}/api/v3/exchangeInfo"
    )

    tradable = {
        item["symbol"]
        for item in exchange_info["symbols"]
        if item.get("status") == "TRADING"
        and item.get("isSpotTradingAllowed", False)
        and item.get("quoteAsset") == "USDT"
    }

    pairs = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        if symbol not in tradable:
            continue

        base = symbol[:-4]

        if base in EXCLUDED_BASES:
            continue

        try:
            quote_volume = float(ticker["quoteVolume"])
            price = float(ticker["lastPrice"])
            change_24h = float(ticker["priceChangePercent"])
        except (KeyError, TypeError, ValueError):
            continue

        if quote_volume < MIN_QUOTE_VOLUME or price <= 0:
            continue

        pairs.append({
            "symbol": symbol,
            "base": base,
            "quote_volume": quote_volume,
            "price": price,
            "change_24h": change_24h,
        })

    pairs.sort(
        key=lambda item: item["quote_volume"],
        reverse=True,
    )

    return pairs[:MAX_PAIRS]


def analyze_pair(pair):
    symbol = pair["symbol"]

    try:
        candles = get_json(
            f"{BINANCE_URL}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": CANDLE_INTERVAL,
                "limit": CANDLE_COUNT,
            },
        )

        # Exclude the current, unfinished candle.
        candles = candles[:-1]

        if len(candles) < 21:
            return None

        previous = candles[:-1]
        latest = candles[-1]

        open_price = float(latest[1])
        high_price = float(latest[2])
        low_price = float(latest[3])
        close_price = float(latest[4])
        current_volume = float(latest[5])

        previous_volumes = [
            float(candle[5])
            for candle in previous[-20:]
        ]

        average_volume = (
            sum(previous_volumes) / len(previous_volumes)
        )

        if average_volume <= 0 or open_price <= 0:
            return None

        volume_ratio = current_volume / average_volume
        candle_change = (
            (close_price - open_price) / open_price
        ) * 100

        previous_high = max(
            float(candle[2]) for candle in previous[-20:]
        )
        previous_low = min(
            float(candle[3]) for candle in previous[-20:]
        )

        breakout_up = close_price > previous_high
        breakout_down = close_price < previous_low

        score = 0
        reasons = []

        if volume_ratio >= 2.5:
            score += 35
            reasons.append("Volume spike > 2.5x")
        elif volume_ratio >= 1.8:
            score += 25
            reasons.append("Volume spike > 1.8x")
        elif volume_ratio >= MIN_VOLUME_RATIO:
            score += 15
            reasons.append("Volume above baseline")

        if breakout_up:
            score += 35
            reasons.append("Breakout above 20-candle high")
        elif breakout_down:
            score += 35
            reasons.append("Breakdown below 20-candle low")

        if abs(candle_change) >= 1.5:
            score += 20
            reasons.append("Strong hourly candle")
        elif abs(candle_change) >= 0.75:
            score += 10
            reasons.append("Notable hourly candle")

        if candle_change > 0:
            direction = "🟢 BULLISH"
        elif candle_change < 0:
            direction = "🔴 BEARISH"
        else:
            direction = "⚪ NEUTRAL"

        # Require volume confirmation plus price movement
        # or a confirmed breakout/breakdown.
        has_price_confirmation = (
            breakout_up
            or breakout_down
            or abs(candle_change) >= 0.75
        )

        if (
            volume_ratio < MIN_VOLUME_RATIO
            or score < MIN_SCORE
            or not has_price_confirmation
        ):
            return None

        return {
            "base": pair["base"],
            "symbol": symbol,
            "price": close_price,
            "volume_ratio": volume_ratio,
            "volume_24h": pair["quote_volume"],
            "change_24h": pair["change_24h"],
            "candle_change": candle_change,
            "score": score,
            "direction": direction,
            "breakout_up": breakout_up,
            "breakout_down": breakout_down,
            "reasons": reasons,
        }

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
        IndexError,
    ) as error:
        print(f"Skipped {symbol}: {error}")
        return None


def build_message(alerts):
    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🛰 SMART MONEY RADAR",
        "📊 SPOT MARKET ACTIVITY",
        "",
        "Signals based on completed 1H candles.",
        "Volume is compared with the previous 20 candles.",
        "",
    ]

    for item in alerts:
        if item["breakout_up"]:
            setup = "🚀 RANGE BREAKOUT"
        elif item["breakout_down"]:
            setup = "⚠️ RANGE BREAKDOWN"
        elif item["candle_change"] > 0:
            setup = "📈 BULLISH MOMENTUM"
        else:
            setup = "📉 BEARISH MOMENTUM"

        lines.extend([
            f"{item['direction']} | {item['base']}",
            f"🎯 Signal Score: {item['score']}/90",
            f"💵 Price: {money(item['price'])}",
            f"🔥 Hourly Volume: {item['volume_ratio']:.2f}x baseline",
            f"📊 24H Volume: {money(item['volume_24h'])}",
            f"📈 24H Change: {item['change_24h']:+.2f}%",
            f"🕯 Hourly Change: {item['candle_change']:+.2f}%",
            f"🔎 Setup: {setup}",
            "🧠 Evidence:",
        ])

        for reason in item["reasons"]:
            lines.append(f"• {reason}")

        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "ℹ️ Score measures observed conditions, not probability.",
        "⚠️ This does not confirm institutional or smart-money flows.",
        "⚠️ Not financial advice or an automatic trade signal.",
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
            f"Telegram rejected the message: {result}"
        )


def main():
    print("Starting Crypto Edge Smart Money Radar...")

    pairs = get_top_pairs()
    print(f"Scanning {len(pairs)} USDT spot pairs...")

    alerts = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(analyze_pair, pair)
            for pair in pairs
        ]

        for future in as_completed(futures):
            result = future.result()

            if result is not None:
                alerts.append(result)

    alerts.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    alerts = alerts[:MAX_ALERTS]

    if not alerts:
        print("No signals met the required criteria.")
        return

    message = build_message(alerts)
    send_telegram(message)

    print(f"Successfully sent {len(alerts)} radar signals.")


if __name__ == "__main__":
    main()
