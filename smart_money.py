
import json
import os
from datetime import datetime, timezone

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CMC_API_KEY = os.environ["CMC_API_KEY"]
CHAT_ID = "@cryptoedgeAlerts"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
GLOBAL_MARKET_URL = "https://api.coinpaprika.com/v1/global"

HISTORY_FILE = "smart_money_history.json"
SIGNAL_LOG_FILE = "signal_log.json"

MAX_COINS = 100
MAX_ALERTS = 8
MIN_VOLUME_USD = 5_000_000
MIN_HISTORY_POINTS = 3
MIN_VOLUME_RATIO = 1.25
MIN_PRICE_CHANGE = 2.0


def money(value):
    if value is None:
        return "N/A"

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


def load_json(filename, default):
    if not os.path.exists(filename):
        return default

    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        print(f"Could not read {filename}. Using default data.")
        return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def load_history():
    return load_json(HISTORY_FILE, {})


def save_history(history):
    save_json(HISTORY_FILE, history)


def record_signals(alerts, timestamp):
    signals = load_json(SIGNAL_LOG_FILE, [])

    if not isinstance(signals, list):
        signals = []

    existing_ids = {
        signal.get("signal_id")
        for signal in signals
        if isinstance(signal, dict)
    }

    added = 0

    for alert in alerts:
        coin_id = str(alert["coin_id"])
        signal_id = f"{coin_id}_{timestamp}"

        if signal_id in existing_ids:
            continue

        signals.append({
            "signal_id": signal_id,
            "coin_id": coin_id,
            "name": alert["name"],
            "symbol": alert["symbol"],
            "timestamp": timestamp,
            "entry_price": alert["price"],
            "direction": (
                "bullish" if alert["change"] > 0 else "bearish"
            ),
            "score": alert["score"],
            "volume_ratio": round(alert["volume_ratio"], 4),
            "price_change_24h": alert["change"],
            "results": {},
        })

        existing_ids.add(signal_id)
        added += 1

    save_json(SIGNAL_LOG_FILE, signals)
    print(f"New signals recorded: {added}")


def get_sentiment():
    sentiment = {
        "fear_greed": None,
        "label": "UNAVAILABLE",
        "market_cap_change": None,
        "btc_dominance": None,
        "market_cap": None,
        "volume_24h": None,
    }

    try:
        response = requests.get(FEAR_GREED_URL, timeout=15)
        response.raise_for_status()
        data = response.json()["data"][0]
        value = int(data["value"])

        if value <= 24:
            label = "EXTREME FEAR"
        elif value <= 44:
            label = "FEAR"
        elif value <= 55:
            label = "NEUTRAL"
        elif value <= 74:
            label = "GREED"
        else:
            label = "EXTREME GREED"

        sentiment["fear_greed"] = value
        sentiment["label"] = label

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        print(f"Fear & Greed unavailable: {error}")

    try:
        response = requests.get(GLOBAL_MARKET_URL, timeout=15)
        response.raise_for_status()
        data = response.json()

        fields = {
            "market_cap_change": "market_cap_change_24h",
            "btc_dominance": "bitcoin_dominance_percentage",
            "market_cap": "market_cap_usd",
            "volume_24h": "volume_24h_usd",
        }

        for key, source_key in fields.items():
            value = data.get(source_key)
            if value is not None:
                sentiment[key] = float(value)

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as error:
        print(f"Global market data unavailable: {error}")

    return sentiment


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


def analyze_coin(coin, history, now, diagnostics):
    coin_id = str(coin["id"])
    quote = coin.get("quote", {}).get("USD", {})

    price = quote.get("price")
    volume = quote.get("volume_24h")
    change = quote.get("percent_change_24h")
    rank = coin.get("cmc_rank")

    if price is None or volume is None or change is None:
        diagnostics["missing_data"] += 1
        return None

    price = float(price)
    volume = float(volume)
    change = float(change)

    if price <= 0 or volume < MIN_VOLUME_USD:
        diagnostics["below_min_volume"] += 1
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
        diagnostics["checked_history"] += 1

        average_volume = sum(old_volumes) / len(old_volumes)

        if average_volume > 0:
            volume_ratio = volume / average_volume
            diagnostics["volume_ratios"].append(volume_ratio)

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

            if volume_ratio >= MIN_VOLUME_RATIO:
                diagnostics["passed_volume"] += 1

            if abs(change) >= MIN_PRICE_CHANGE:
                diagnostics["passed_change"] += 1

            if score >= 50:
                diagnostics["passed_score"] += 1

            if (
                volume_ratio >= MIN_VOLUME_RATIO
                and abs(change) >= MIN_PRICE_CHANGE
                and score >= 50
            ):
                diagnostics["passed_all"] += 1

                direction = (
                    "🟢 BULLISH MOMENTUM" if change > 0
                    else "🔴 BEARISH MOMENTUM" if change < 0
                    else "⚪ NEUTRAL"
                )

                alert = {
                    "coin_id": coin_id,
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


def build_market_report(sentiment, scanned, bullish, bearish, total_alerts):
    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🌍 MARKET CONTEXT REPORT",
        f"🕒 Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "🧠 FEAR & GREED",
    ]

    if sentiment["fear_greed"] is not None:
        lines.append(
            f"📊 Index: {sentiment['fear_greed']}/100 | "
            f"{sentiment['label']}"
        )
    else:
        lines.append("📊 Index: Unavailable")

    lines.extend([
        "",
        "🌐 GLOBAL MARKET",
        f"💎 Market Cap: {money(sentiment['market_cap'])}",
        f"💰 24H Volume: {money(sentiment['volume_24h'])}",
    ])

    change = sentiment["market_cap_change"]

    if change is not None:
        emoji = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
        lines.append(f"📈 Market Cap Change (24H): {emoji} {change:+.2f}%")
    else:
        lines.append("📈 Market Cap Change (24H): N/A")

    dominance = sentiment["btc_dominance"]

    if dominance is not None:
        lines.append(f"₿ BTC Dominance: {dominance:.2f}%")
    else:
        lines.append("₿ BTC Dominance: N/A")

    lines.extend([
        "",
        "🛰 SMART MONEY SCAN",
        f"🔍 Assets scanned: {scanned}",
        f"🚀 Bullish alerts: {bullish}",
        f"🔻 Bearish alerts: {bearish}",
        f"📌 Total alerts sent: {total_alerts}",
        "",
        "⚠️ Market context is not a buy/sell signal.",
        "⚠️ Volume snapshots overlap across the 24h window.",
        "⚠️ This scanner does not confirm institutional money flows.",
        "🎯 DATA OVER HYPE",
        "⚡ STAY AHEAD",
    ])

    return "\n".join(lines)


def build_alert_message(alerts, sentiment):
    lines = [
        "⚡ CRYPTO EDGE",
        "━━━━━━━━━━━━━━━━━━",
        "🛰 SMART MONEY RADAR",
        "Source: CoinMarketCap",
        "",
    ]

    if sentiment["fear_greed"] is not None:
        lines.append(
            f"🧠 Market Sentiment: {sentiment['label']} "
            f"({sentiment['fear_greed']}/100)"
        )
    else:
        lines.append("🧠 Market Sentiment: Unavailable")

    lines.append("")

    for item in alerts:
        if item["breakout_up"]:
            setup = "🚀 ABOVE SAVED PRICE RANGE"
        elif item["breakout_down"]:
            setup = "⚠️ BELOW SAVED PRICE RANGE"
        else:
            setup = "📊 MOMENTUM WATCH"

        lines.extend([
            item["direction"],
            f"💎 {item['symbol']} | Rank #{item['rank']}",
            f"🎯 Signal Score: {item['score']}/80",
            f"💵 Price: {money(item['price'])}",
            f"📈 24H Change: {item['change']:+.2f}%",
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
        "⚠️ Not a guaranteed trading signal.",
        "🎯 DATA OVER HYPE",
    ])

    return "\n".join(lines)


def send_telegram(message):
    response = requests.post(
        TELEGRAM_URL,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=30,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected the message: " + str(result)
        )


def main():
    print("Starting Crypto Edge Smart Money Radar...")

    history = load_history()
    coins = get_market_data()
    now = datetime.now(timezone.utc).isoformat()
    sentiment = get_sentiment()

    print(
        "Sentiment:",
        sentiment["label"],
        "| Fear & Greed:",
        sentiment["fear_greed"],
    )

    diagnostics = {
        "missing_data": 0,
        "below_min_volume": 0,
        "checked_history": 0,
        "passed_volume": 0,
        "passed_change": 0,
        "passed_score": 0,
        "passed_all": 0,
        "volume_ratios": [],
    }

    alerts = []

    for coin in coins:
        alert = analyze_coin(coin, history, now, diagnostics)
        if alert is not None:
            alerts.append(alert)

    save_history(history)

    print("Diagnostic report:")
    for key, value in diagnostics.items():
        if key != "volume_ratios":
            print(f"{key}: {value}")

    ratios = diagnostics["volume_ratios"]

    if ratios:
        print(f"volume_ratio_min: {min(ratios):.4f}")
        print(f"volume_ratio_max: {max(ratios):.4f}")
        print(f"volume_ratio_average: {sum(ratios) / len(ratios):.4f}")
        print(
            "volume_ratio_below_threshold:",
            sum(1 for ratio in ratios if ratio < MIN_VOLUME_RATIO),
        )

    alerts.sort(key=lambda item: item["score"], reverse=True)
    alerts = alerts[:MAX_ALERTS]

    if alerts:
        record_signals(alerts, now)

    bullish = sum(1 for item in alerts if item["change"] > 0)
    bearish = sum(1 for item in alerts if item["change"] < 0)

    report = build_market_report(
        sentiment,
        len(coins),
        bullish,
        bearish,
        len(alerts),
    )

    send_telegram(report)
    print("Market Context Report sent successfully.")

    if alerts:
        send_telegram(build_alert_message(alerts, sentiment))
        print(f"Sent {len(alerts)} Smart Money Radar alerts.")
    else:
        print("No signals met the required criteria.")


if __name__ == "__main__":
    main()
