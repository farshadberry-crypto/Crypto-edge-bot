import os
import json
import time
import requests

from datetime import datetime, timezone, timedelta


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
COINBASE_API = "https://api.exchange.coinbase.com"
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
REQUEST_TIMEOUT = 20


# =========================
# GENERAL HELPERS
# =========================

def now_utc():
    return datetime.now(timezone.utc)


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

        if not response.json().get("ok"):
            print("Telegram returned an unsuccessful response.")
            return False

        return True

    except (requests.RequestException, ValueError) as error:
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

        return (
            data["value_classification"].upper(),
            int(data["value"]),
        )

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        print(f"Sentiment unavailable: {error}")
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
            "btc_dominance": data.get(
                "bitcoin_dominance_percentage"
            ),
            "market_change": data.get(
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
        raise RuntimeError(
            "CoinMarketCap did not return market data."
        )

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
            "market_cap": float(
                quote.get("market_cap") or 0
            ),
        })

    return coins


# =========================
# COINBASE PUBLIC API
# =========================

def get_coinbase_products():
    """
    Get available Coinbase USD spot products.
    No Coinbase API key is required for this public endpoint.
    """

    try:
        response = requests.get(
            f"{COINBASE_API}/products",
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        products = response.json()

        available = set()

        for product in products:
            if (
                product.get("quote_currency") == "USD"
                and product.get("status") == "online"
                and product.get("trading_disabled") is not True
            ):
                available.add(product.get("product_id"))

        print(
            f"Coinbase USD products available: {len(available)}"
        )

        return available

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as error:
        print(f"Coinbase product list unavailable: {error}")
        return None


def get_hourly_volume_ratio(symbol, available_products):
    """
    Compare the latest completed 1-hour candle with the
    average of the preceding 24 completed hourly candles.

    Coinbase candle fields:
    [time, low, high, open, close, base_volume]

    Approximate USD volume = base volume * candle close price.
    """

    product_id = f"{symbol.upper()}-USD"

    if available_products is None:
        return None

    if product_id not in available_products:
        return None

    current_time = now_utc()

    # Request a 27-hour window to allow for candle boundaries.
    start_time = current_time - timedelta(hours=27)

    params = {
        "granularity": 3600,
        "start": start_time.isoformat(),
        "end": current_time.isoformat(),
    }

    try:
        response = requests.get(
            f"{COINBASE_API}/products/"
            f"{product_id}/candles",
            params=params,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        candles = response.json()

        if not isinstance(candles, list):
            return None

        current_timestamp = int(current_time.timestamp())

        # Sort chronologically; API ordering may vary.
        candles = sorted(
            candles,
            key=lambda candle: int(candle[0]),
        )

        # Keep only completed hourly candles.
        completed = [
            candle
            for candle in candles
            if len(candle) >= 6
            and int(candle[0]) + 3600 <= current_timestamp
        ]

        # Remove duplicate candle timestamps if any.
        unique_candles = {}

        for candle in completed:
            unique_candles[int(candle[0])] = candle

        completed = [
            unique_candles[timestamp]
            for timestamp in sorted(unique_candles)
        ]

        if len(completed) < 25:
            return None

        # Latest completed hour plus 24 preceding hours.
        last_25 = completed[-25:]

        quote_volumes = []

        for candle in last_25:
            close_price = float(candle[4])
            base_volume = float(candle[5])

            quote_volume = close_price * base_volume

            quote_volumes.append(quote_volume)

        previous_24 = quote_volumes[:-1]
        latest_hour = quote_volumes[-1]

        average_volume = sum(previous_24) / len(previous_24)

        if average_volume <= 0:
            return None

        return latest_hour / average_volume

    except requests.HTTPError as error:
        status_code = (
            error.response.status_code
            if error.response is not None
            else "unknown"
        )

        print(
            f"Coinbase HTTP error for {product_id}: "
            f"{status_code}"
        )
        return None

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ) as error:
        print(
            f"Coinbase candle error for {product_id}: {error}"
        )
        return None


# =========================
# PRICE HISTORY
# =========================

def load_history():
    data = load_json(HISTORY_FILE, {})

    return data if isinstance(data, dict) else {}


def update_history(history, coin):
    coin_id = coin["id"]

    if not isinstance(history.get(coin_id), list):
        history[coin_id] = []

    history[coin_id].append({
        "time": now_utc().isoformat(),
        "price": coin["price"],
        "volume": coin["volume_24h"],
    })

    history[coin_id] = history[coin_id][-14:]


# =========================
# SIGNAL ANALYSIS
# =========================

def analyze_coin(coin, history, volume_ratio, diagnostics):
    previous = history.get(coin["id"], [])

    if (
        not isinstance(previous, list)
        or len(previous) < MIN_HISTORY_POINTS
    ):
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
        reasons.append(
            "Coinbase hourly volume at least 2x its 24h hourly average"
        )
    elif volume_ratio >= 1.5:
        score += 25
        reasons.append(
            "Coinbase hourly volume at least 1.5x average"
        )
    elif volume_ratio >= MIN_VOLUME_RATIO:
        score += 15
        reasons.append(
            "Coinbase hourly volume above threshold"
        )

    if abs(coin["change_24h"]) >= 5:
        score += 20
        reasons.append("24h price change at least 5%")
    elif abs(coin["change_24h"]) >= MIN_PRICE_CHANGE:
        score += 15
        reasons.append("24h price change at least 2%")

    old_prices = [
        float(item["price"])
        for item in previous
        if (
            isinstance(item, dict)
            and item.get("price") is not None
        )
    ]

    if old_prices:
        if coin["price"] > max(old_prices):
            score += 30
            reasons.append("Price above recorded history")
        elif coin["price"] < min(old_prices):
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

    return {
        "time": now_utc().isoformat(),
        "id": coin["id"],
        "name": coin["name"],
        "symbol": coin["symbol"],
        "price": coin["price"],
        "volume_24h": coin["volume_24h"],
        "hourly_volume_ratio": round(volume_ratio, 4),
        "change_24h": coin["change_24h"],
        "score": score,
        "direction": (
            "BULLISH"
            if coin["change_24h"] > 0
            else "BEARISH"
        ),
        "reasons": reasons,
    }


# =========================
# TELEGRAM MESSAGES
# =========================

def format_money(value):
    if value is None:
        return "N/A"

    return f"${value:,.2f}"


def build_market_message(sentiment, fear_greed, market):
    lines = [
        "CRYPTO EDGE | MARKET CONTEXT",
        "",
        f"Sentiment: {sentiment}",
        (
            f"Fear & Greed: {fear_greed}"
            if fear_greed is not None
            else "Fear & Greed: N/A"
        ),
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
        "Hourly volume uses Coinbase USD spot candles.",
        "This represents Coinbase activity, not the entire crypto market.",
        "Signals are screening results, not guaranteed trades.",
    ])

    return "\n".join(lines)


def build_signal_message(signal):
    lines = [
        f"CRYPTO EDGE | {signal['direction']} SIGNAL",
        "",
        f"Coin: {signal['name']} ({signal['symbol']})",
        f"Price: ${signal['price']:.8f}",
        f"24h Change: {signal['change_24h']:+.2f}%",
        f"CMC 24h Volume: ${signal['volume_24h']:,.0f}",
        (
            "Coinbase Hourly Volume Ratio: "
            f"{signal['hourly_volume_ratio']:.2f}x"
        ),
        f"Score: {signal['score']}/100",
        "",
        "Reasons:",
    ]

    lines.extend(
        f"- {reason}" for reason in signal["reasons"]
    )

    lines.extend([
        "",
        "Analysis only, not financial advice.",
    ])

    return "\n".join(lines)


# =========================
# MAIN
# =========================

def main():
    print("Starting Crypto Edge Smart Money Radar...")

    sentiment, fear_greed = get_sentiment()

    print(
        f"Sentiment: {sentiment} | "
        f"Fear & Greed: {fear_greed}"
    )

    market = get_global_market()
    coins = get_market_data()

    history = load_history()
    available_products = get_coinbase_products()

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

    if available_products is None:
        print(
            "WARNING: Coinbase product list failed. "
            "Hourly volume signals will be unavailable."
        )

    for coin in coins:
        ratio = get_hourly_volume_ratio(
            coin["symbol"],
            available_products,
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

        # Avoid sending requests too quickly.
        time.sleep(0.15)

    save_json(HISTORY_FILE, history)

    signals.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    signals = signals[:MAX_ALERTS]

    signal_log = load_json(SIGNAL_LOG_FILE, [])

    if not isinstance(signal_log, list):
        signal_log = []

    signal_log.extend(signals)

    save_json(
        SIGNAL_LOG_FILE,
        signal_log[-1000:],
    )

    print("Diagnostic report:")

    for key, value in diagnostics.items():
        if key != "volume_ratios":
            print(f"{key}: {value}")

    ratios = diagnostics["volume_ratios"]

    if ratios:
        print(f"volume_ratio_min: {min(ratios):.4f}")
        print(f"volume_ratio_max: {max(ratios):.4f}")
        print(
            "volume_ratio_average: "
            f"{sum(ratios) / len(ratios):.4f}"
        )
        print(
            "volume_ratio_below_threshold: "
            f"{sum(1 for value in ratios if value < MIN_VOLUME_RATIO)}"
        )
    else:
        print("No hourly volume ratios were available.")

    message = build_market_message(
        sentiment,
        fear_greed,
        market,
    )

    if send_telegram(message):
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
