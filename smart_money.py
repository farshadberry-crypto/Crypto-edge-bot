import os
import json
import time
import requests
from datetime import datetime, timezone

CMC_API_KEY = os.environ["CMC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_EXCHANGE_URL = "https://api.binance.com/api/v3/exchangeInfo"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
GLOBAL_URL = "https://api.coinpaprika.com/v1/global"

HISTORY_FILE = "smart_money_history.json"
SIGNAL_LOG_FILE = "signal_log.json"

MAX_COINS = 100
MAX_ALERTS = 8
MIN_VOLUME_USD = 5_000_000
MIN_HISTORY_POINTS = 3
MIN_VOLUME_RATIO = 1.25
MIN_PRICE_CHANGE = 2.0
REQUEST_TIMEOUT = 15


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            print("Telegram returned an unsuccessful response.")
            return False

        return True

    except (requests.RequestException, ValueError) as error:
        print(f"Telegram error: {error}")
        return False


def get_sentiment():
    try:
        response = requests.get(
            FEAR_GREED_URL,
            params={"limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()["data"][0]
        return data["value_classification"].upper(), int(data["value"])

    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        print(f"Sentiment unavailable: {error}")
        return "UNKNOWN", None


def get_global_market():
    try:
        response = requests.get(GLOBAL_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        return {
            "market_cap": data.get("market_cap_usd"),
            "volume_24h": data.get("volume_24h_usd"),
            "btc_dominance": data.get("bitcoin_dominance_percentage"),
            "market_change": data.get("market_cap_change_24h"),
        }

    except (requests.RequestException, ValueError) as error:
        print(f"Global market data unavailable: {error}")
        return {}


def get_market_data():
    response = requests.get(
        CMC_URL,
        headers={
            "Accepts": "application/json",
            "X-CMC_PRO_API_KEY": CMC_API_KEY,
        },
        params={
            "start": 1,
            "limit": MAX_COINS,
            "convert": "USD",
            "sort": "market_cap",
            "sort_dir": "desc",
            "cryptocurrency_type": "coins",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    if "data" not in payload:
        raise RuntimeError("CoinMarketCap did not return market data.")

    coins = []

    for item in payload["data"]:
        quote = item.get("quote", {}).get("USD", {})
        price = quote.get("price")
        volume = quote.get("volume_24h")
        change = quote.get("percent_change_24h")

        if price is None or volume is None or change is None:
            continue

        coins.append({
            "id": str(item["id"]),
            "name": item["name"],
            "symbol": item["symbol"].upper(),
            "price": float(price),
            "volume_24h": float(volume),
            "change_24h": float(change),
            "market_cap": float(quote.get("market_cap") or 0),
        })

    return coins


def get_binance_symbols():
    try:
        response = requests.get(
            BINANCE_EXCHANGE_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        return {
            item["symbol"]
            for item in data.get("symbols", [])
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("isSpotTradingAllowed", True)
        }

    except (requests.RequestException, ValueError) as error:
        print(f"Binance exchange info unavailable: {error}")
        return set()


def get_hourly_volume_ratio(symbol, available_symbols):
    pair = f"{symbol.upper()}USDT"

    if pair not in available_symbols:
        return None

    try:
        response = requests.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": pair,
                "interval": "1h",
                "limit": 26,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        candles = response.json()

        if not isinstance(candles, list) or len(candles) < 26:
            return None

        # Remove the currently forming candle.
        completed = candles[:-1]
        volumes = [float(candle[7]) for candle in completed]

        # Last completed hour compared with the previous 24 hours.
        latest_volume = volumes[-1]
        previous_24 = volumes[-25:-1]

        if len(previous_24) != 24:
            return None

        average_volume = sum(previous_24) / 24

        if average_volume <= 0:
            return None

        return latest_volume / average_volume

    except (requests.RequestException, ValueError, TypeError, IndexError) as error:
        print(f"Hourly volume error for {pair}: {error}")
        return None


def load_history():
    data = load_json(HISTORY_FILE, {})
    return data if isinstance(data, dict) else {}


def update_history(history, coin):
    coin_id = coin["id"]

    if not isinstance(history.get(coin_id), list):
        history[coin_id] = []

    history[coin_id].append({
        "time": now_utc(),
        "price": coin["price"],
        "volume": coin["volume_24h"],
    })

    history[coin_id] = history[coin_id][-14:]


def analyze_coin(coin, history, volume_ratio, diagnostics):
    previous = history.get(coin["id"], [])

    if not isinstance(previous, list) or len(previous) < MIN_HISTORY_POINTS:
        diagnostics["insufficient_history"] += 1
        return None

    if coin["volume_24h"] < MIN_VOLUME_USD:
        diagnostics["below_min_volume"] += 1
        return None

    diagnostics["checked_history"] += 1

    if volume_ratio is None:
        diagnostics["missing_hourly_volume"] += 1
        return None

    diagnostics["volume_ratios"].append(volume_ratio)

    if volume_ratio >= MIN_VOLUME_RATIO:
        diagnostics["passed_volume"] += 1

    if abs(coin["change_24h"]) >= MIN_PRICE_CHANGE:
        diagnostics["passed_change"] += 1

    score = 0
    reasons = []

    if volume_ratio >= 2.0:
        score += 30
        reasons.append("Hourly volume at least 2x average")
    elif volume_ratio >= 1.5:
        score += 25
        reasons.append("Hourly volume at least 1.5x average")
    elif volume_ratio >= MIN_VOLUME_RATIO:
        score += 15
        reasons.append("Hourly volume above threshold")

    if abs(coin["change_24h"]) >= 5:
        score += 20
        reasons.append("24h price change at least 5%")
    elif abs(coin["change_24h"]) >= MIN_PRICE_CHANGE:
        score += 15
        reasons.append("24h price change at least 2%")

    old_prices = [
        float(item["price"])
        for item in previous
        if isinstance(item, dict) and item.get("price") is not None
    ]

    if old_prices:
        if coin["price"] > max(old_prices):
            score += 30
            reasons.append("Price above stored history")
        elif coin["price"] < min(old_prices):
            score += 30
            reasons.append("Price below stored history")

    if score >= 50:
        diagnostics["passed_score"] += 1

    if (
        volume_ratio < MIN_VOLUME_RATIO
        or abs(coin["change_24h"]) < MIN_PRICE_CHANGE
        or score < 50
    ):
        return None

    diagnostics["passed_all"] += 1

    return {
        "time": now_utc(),
        "id": coin["id"],
        "name": coin["name"],
        "symbol": coin["symbol"],
        "price": coin["price"],
        "volume_24h": coin["volume_24h"],
        "hourly_volume_ratio": round(volume_ratio, 4),
        "change_24h": coin["change_24h"],
        "score": score,
        "direction": "BULLISH" if coin["change_24h"] > 0 else "BEARISH",
        "reasons": reasons,
    }


def format_money(value):
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def build_market_message(sentiment, fear_greed, market):
    lines = [
        "CRYPTO EDGE | MARKET CONTEXT",
        "",
        f"Sentiment: {sentiment}",
        f"Fear & Greed: {fear_greed if fear_greed is not None else 'N/A'}",
    ]

    if market:
        lines.extend([
            "",
            f"Market Cap: {format_money(market.get('market_cap'))}",
            f"24h Market Volume: {format_money(market.get('volume_24h'))}",
            f"BTC Dominance: {market.get('btc_dominance', 'N/A')}%",
            f"Market Cap Change: {market.get('market_change', 'N/A')}%",
        ])

    lines.extend([
        "",
        "Hourly volume compares the latest completed hour with the previous 24 completed hours.",
        "Screening results are not guaranteed trading signals.",
    ])

    return "\n".join(lines)


def build_signal_message(signal):
    lines = [
        f"CRYPTO EDGE | {signal['direction']} SIGNAL",
        "",
        f"Coin: {signal['name']} ({signal['symbol']})",
        f"Price: ${signal['price']:.8f}",
        f"24h Change: {signal['change_24h']:+.2f}%",
        f"24h Volume: ${signal['volume_24h']:,.0f}",
        f"Hourly Volume Ratio: {signal['hourly_volume_ratio']:.2f}x",
        f"Score: {signal['score']}/100",
        "",
        "Reasons:",
    ]

    lines.extend(f"- {reason}" for reason in signal["reasons"])
    lines.append("")
    lines.append("Analysis only, not financial advice.")

    return "\n".join(lines)


def main():
    print("Starting Crypto Edge Smart Money Radar...")

    sentiment, fear_greed = get_sentiment()
    print(f"Sentiment: {sentiment} | Fear & Greed: {fear_greed}")

    market = get_global_market()
    coins = get_market_data()
    history = load_history()
    available_symbols = get_binance_symbols()

    diagnostics = {
        "insufficient_history": 0,
        "below_min_volume": 0,
        "checked_history": 0,
        "missing_hourly_volume": 0,
        "passed_volume": 0,
        "passed_change": 0,
        "passed_score": 0,
        "passed_all": 0,
        "volume_ratios": [],
    }

    signals = []

    for coin in coins:
        ratio = get_hourly_volume_ratio(
            coin["symbol"],
            available_symbols,
        )

        signal = analyze_coin(coin, history, ratio, diagnostics)

        if signal:
            signals.append(signal)

        update_history(history, coin)
        time.sleep(0.05)

    save_json(HISTORY_FILE, history)

    signals.sort(key=lambda item: item["score"], reverse=True)
    signals = signals[:MAX_ALERTS]

    signal_log = load_json(SIGNAL_LOG_FILE, [])
    if not isinstance(signal_log, list):
        signal_log = []

    signal_log.extend(signals)
    save_json(SIGNAL_LOG_FILE, signal_log[-1000:])

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
            "volume_ratio_below_threshold: "
            f"{sum(1 for value in ratios if value < MIN_VOLUME_RATIO)}"
        )
    else:
        print("No hourly volume ratios were available.")

    if send_telegram(build_market_message(sentiment, fear_greed, market)):
        print("Market Context Report sent successfully.")
    else:
        print("Market Context Report failed to send.")

    if not signals:
        print("No signals met the required criteria.")
        return

    for signal in signals:
        if send_telegram(build_signal_message(signal)):
            print(f"Signal sent: {signal['symbol']}")
        else:
            print(f"Signal failed: {signal['symbol']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Smart Money Radar failed: {error}")
        raise
