```python
import os
import json
import time
import requests

from datetime import datetime, timezone


# =========================
# CONFIGURATION
# =========================

CMC_API_KEY = os.environ["CMC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "@cryptoedgeAlerts"
)

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

# Minimum hourly volume compared with the average of
# the previous 24 completed hourly candles.
MIN_VOLUME_RATIO = 1.25

MIN_PRICE_CHANGE = 2.0
REQUEST_TIMEOUT = 15


# =========================
# GENERAL HELPERS
# =========================

def utc_now():
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
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

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

        result = response.json()
        if not result.get("ok"):
            print("Telegram API returned an unsuccessful response.")
            return False

        return True

    except requests.RequestException as error:
        print(f"Telegram error: {error}")
        return False


# =========================
# MARKET SENTIMENT
# =========================

def get_sentiment():
    try:
        response = requests.get(
            FEAR_GREED_URL,
            params={"limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()["data"][0]
        value = int(data["value"])
        classification = data["value_classification"].upper()

        return classification, value

    except (
        requests.RequestException,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Fear & Greed unavailable: {error}")
        return "UNKNOWN", None


def get_global_market():
    try:
        response = requests.get(
            GLOBAL_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        return {
            "market_cap": data.get("market_cap_usd"),
            "volume_24h": data.get("volume_24h_usd"),
            "bitcoin_dominance": data.get(
                "bitcoin_dominance_percentage"
            ),
            "market_cap_change_24h": data.get(
                "market_cap_change_24h"
            ),
        }

    except (requests.RequestException, ValueError) as error:
        print(f"Global market data unavailable: {error}")
        return {}


# =========================
# COINMARKETCAP DATA
# =========================

def get_market_data():
    headers = {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": CMC_API_KEY,
    }

    params = {
        "start": 1,
        "limit": MAX_COINS,
        "convert": "USD",
        "sort": "market_cap",
        "sort_dir": "desc",
        "cryptocurrency_type": "coins",
    }

    response = requests.get(
        CMC_URL,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    payload = response.json()

    if "data" not in payload:
        raise RuntimeError(
            "CoinMarketCap response did not contain market data."
        )

    coins = []

    for item in payload["data"]:
        quote = item.get("quote", {}).get("USD", {})

        price = quote.get("price")
        volume_24h = quote.get("volume_24h")
        change_24h = quote.get("percent_change_24h")

        if price is None or volume_24h is None or change_24h is None:
            continue

        coins.append({
            "id": str(item["id"]),
            "name": item["name"],
            "symbol": item["symbol"].upper(),
            "price": float(price),
            "volume_24h": float(volume_24h),
            "change_24h": float(change_24h),
            "market_cap": float(quote.get("market_cap") or 0),
            "rank": item.get("cmc_rank"),
        })

    return coins


# =========================
# BINANCE HOURLY VOLUME
# =========================

def get_binance_usdt_symbols():
    """
    Fetch tradable Binance spot pairs quoted in USDT.
    Symbols without a matching pair will not receive
    a fabricated hourly-volume ratio.
    """
    try:
        response = requests.get(
            BINANCE_EXCHANGE_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        info = response.json()
        symbols = set()

        for item in info.get("symbols", []):
            if (
                item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed", True)
                and item.get("quoteAsset") == "USDT"
            ):
                symbols.add(item["symbol"])

        return symbols

    except (requests.RequestException, ValueError) as error:
        print(f"Binance exchange info unavailable: {error}")
        return set()


def get_hourly_volume_ratio(symbol, available_symbols):
    """
    Compare the latest completed hourly USDT volume with
    the average volume of the previous 24 completed hours.

    Binance kline field [7] is quote-asset volume.
    The currently forming candle is excluded.
    """
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

        # Exclude the candle that is currently forming.
        completed = candles[:-1]

        if len(completed) < 25:
            return None

        volumes = [float(candle[7]) for candle in completed]

        # 24 completed hours before the latest completed hour.
        previous_24 = volumes[-25:-1]
        latest_hour = volumes[-1]

        if len(previous_24) != 24:
            return None

        average_volume = sum(previous_24) / 24

        if average_volume <= 0:
            return None

        return latest_hour / average_volume

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        IndexError,
    ) as error:
        print(f"Hourly volume unavailable for {pair}: {error}")
        return None


# =========================
# HISTORY
# =========================

def load_history():
    history = load_json(HISTORY_FILE, {})

    if not isinstance(history, dict):
        return {}

    return history


def save_history(history):
    save_json(HISTORY_FILE, history)


def update_history(history, coin):
    coin_id = coin["id"]

    if coin_id not in history or not isinstance(history[coin_id], list):
        history[coin_id] = []

    history[coin_id].append({
        "time": utc_now(),
        "price": coin["price"],
        "volume": coin["volume_24h"],
    })

    history[coin_id] = history[coin_id][-14:]


# =========================
# SIGNAL ANALYSIS
# =========================

def analyze_coin(coin, history, volume_ratio, diagnostics):
    coin_id = coin["id"]
    previous = history.get(coin_id, [])

    if len(previous) < MIN_HISTORY_POINTS:
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
        reasons.append("Strong hourly volume spike")
    elif volume_ratio >= 1.5:
        score += 25
        reasons.append("Elevated hourly volume")
    elif volume_ratio >= MIN_VOLUME_RATIO:
        score += 15
        reasons.append("Above-average hourly volume")

    if abs(coin["change_24h"]) >= 5:
        score += 20
        reasons.append("Strong 24h price movement")
    elif abs(coin["change_24h"]) >= MIN_PRICE_CHANGE:
        score += 15
        reasons.append("Significant 24h price movement")

    old_prices = [
        float(item["price"])
        for item in previous
        if item.get("price") is not None
    ]

    if old_prices:
        previous_high = max(old_prices)
        previous_low = min(old_prices)

        if coin["price"] > previous_high:
            score += 30
            reasons.append("Price above recorded history")
        elif coin["price"] < previous_low:
            score += 30
            reasons.append("Price below recorded history")

    if score >= 50:
        diagnostics["passed_score"] += 1

    if (
        volume_ratio < MIN_VOLUME_RATIO
        or abs(coin["change_24h"]) < MIN_PRICE_CHANGE
        or score < 50
    ):
        return None

    diagnostics["passed_all"] += 1

    direction = (
        "BULLISH" if coin["change_24h"] > 0 else "BEARISH"
    )

    return {
        "time": utc_now(),
        "id": coin_id,
        "name": coin["name"],
        "symbol": coin["symbol"],
        "price": coin["price"],
        "volume_24h": coin["volume_24h"],
        "hourly_volume_ratio": round(volume_ratio, 4),
        "change_24h": coin["change_24h"],
        "score": score,
        "direction": direction,
        "reasons": reasons,
    }


# =========================
# REPORT FORMATTING
# =========================

def format_number(value):
    if value is None:
        return "N/A"

    return f"{value:,.2f}"


def market_context_message(sentiment, fear_greed, market):
    lines = [
        "🌐 CRYPTO EDGE | MARKET CONTEXT",
        "",
        f"Sentiment: {sentiment}",
        f"Fear & Greed Index: {fear_greed if fear_greed is not None else 'N/A'}",
    ]

    if market:
        lines.extend([
            "",
            f"Total Market Cap: ${format_number(market.get('market_cap'))}",
            f"24h Market Volume: ${format_number(market.get('volume_24h'))}",
            f"BTC Dominance: {format_number(market.get('bitcoin_dominance'))}%",
            f"Market Cap Change (24h): {format_number(market.get('market_cap_change_24h'))}%",
        ])

    lines.extend([
        "",
        "Hourly volume is compared against the previous 24 completed hourly candles.",
        "This report is market analysis, not financial advice.",
    ])

    return "\n".join(lines)


def signal_message(signal):
    lines = [
        f"🚨 CRYPTO EDGE | {signal['direction']} SIGNAL",
        "",
        f"Coin: {signal['name']} ({signal['symbol']})",
        f"Price: ${signal['price']:,.8f}",
        f"24h Change: {signal['change_24h']:+.2f}%",
        f"24h Volume: ${signal['volume_24h']:,.0f}",
        f"Hourly Volume Ratio: {signal['hourly_volume_ratio']:.2f}x",
        f"Score: {signal['score']}/100",
        "",
        "Reasons:",
    ]

    lines.extend(f"• {reason}" for reason in signal["reasons"])

    lines.extend([
        "",
        "Signal is a screening result, not a guaranteed trade.",
    ])

    return "\n".join(lines)


# =========================
# MAIN
# =========================

def main():
    print("Starting Crypto Edge Smart Money Radar...")

    sentiment, fear_greed = get_sentiment()
    print(f"Sentiment: {sentiment} | Fear & Greed: {fear_greed}")

    market = get_global_market()
    coins = get_market_data()

    history = load_history()
    available_symbols = get_binance_usdt_symbols()

    diagnostics = {
        "missing_data": 0,
        "below_min_volume": 0,
        "insufficient_history": 0,
        "checked_history": 0,
        "missing_hourly_volume": 0,
        "passed_volume": 0,
        "passed_change": 0,
        "passed_score": 0,
        "passed_all": 0,
        "volume_ratios": [],
    }

    signals = []

    # Analyze using the existing history first, then append
    # this run's snapshot so it cannot count as its own past.
    for coin in coins:
        ratio = get_hourly_volume_ratio(
            coin["symbol"],
            available_symbols,
        )

        signal = analyze_coin(
            coin,
            history,
            ratio,
            diagnostics,
        )

        if signal:
            signals.append(signal)

        update_history(history, coin)

        # Keep API requests spaced out.
        time.sleep(0.05)

    save_history(history)

    signals.sort(key=lambda item: item["score"], reverse=True)
    signals = signals[:MAX_ALERTS]

    # Always maintain the signal log file, even with no signals.
    signal_log = load_json(SIGNAL_LOG_FILE, [])

    if not isinstance(signal_log, list):
        signal_log = []

    signal_log.extend(signals)
    save_json(SIGNAL_LOG_FILE, signal_log[-1000:])

    ratios = diagnostics["volume_ratios"]

    print("Diagnostic report:")
    for key, value in diagnostics.items():
        if key != "volume_ratios":
            print(f"{key}: {value}")

    if ratios:
        print(f"volume_ratio_min: {min(ratios):.4f}")
        print(f"volume_ratio_max: {max(ratios):.4f}")
        print(f"volume_ratio_average: {sum(ratios) / len(ratios):.4f}")
        print(
            "volume_ratio_below_threshold: "
            f"{sum(1 for ratio in ratios if ratio < MIN_VOLUME_RATIO)}"
        )
    else:
        print("No hourly volume ratios were available.")

    context_message = market_context_message(
        sentiment,
        fear_greed,
        market,
    )

    if send_telegram(context_message):
        print("Market Context Report sent successfully.")
    else:
        print("Failed to send Market Context Report.")

    if not signals:
        print("No signals met the required criteria.")
        return

    for signal in signals:
        message = signal_message(signal)

        if send_telegram(message):
            print(f"Signal sent: {signal['symbol']}")
        else:
            print(f"Failed to send signal: {signal['symbol']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Smart Money Radar failed: {error}")
        raise
```
