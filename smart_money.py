
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
KRAKEN_API = "https://api.kraken.com/0/public"
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

HISTORY_HOURS = 24
MAX_HISTORY_POINTS = 200

REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.35


# =========================
# GENERAL HELPERS
# =========================

def now_utc():
    return datetime.now(timezone.utc)


def parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        return None


def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(filename, data):
    temporary_file = filename + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    os.replace(temporary_file, filename)


def get_json(url, params=None, headers=None):
    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


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
        data = get_json(
            FEAR_GREED_URL,
            params={"limit": 1},
        )

        item = data["data"][0]

        return (
            item["value_classification"].upper(),
            int(item["value"]),
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
        data = get_json(GLOBAL_URL)

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

    # Snapshot timestamp: when the CMC response was received.
    price_snapshot_time = now_utc().isoformat()
    coins = []

    for item in payload["data"]:
        quote = item.get("quote", {}).get("USD", {})

        price = quote.get("price")
        volume = quote.get("volume_24h")
        change = quote.get("percent_change_24h")

        if price is None or volume is None or change is None:
            continue

        try:
            price = float(price)
            volume = float(volume)
            change = float(change)
            market_cap = float(quote.get("market_cap") or 0)
        except (ValueError, TypeError):
            continue

        if price <= 0 or volume < 0:
            continue

        coins.append({
            "id": str(item["id"]),
            "name": item["name"],
            "symbol": item["symbol"].upper(),
            "price": price,
            "price_time": price_snapshot_time,
            "volume_24h": volume,
            "change_24h": change,
            "market_cap": market_cap,
        })

    return coins


# =========================
# KRAKEN PUBLIC MARKET API
# =========================

def normalize_symbol(symbol):
    symbol = str(symbol).upper()

    aliases = {
        "XBT": "BTC",
        "XDG": "DOGE",
    }

    return aliases.get(symbol, symbol)


def get_kraken_usd_pairs():
    try:
        data = get_json(f"{KRAKEN_API}/AssetPairs")

        errors = data.get("error", [])

        if errors:
            print(f"Kraken AssetPairs errors: {errors}")
            return None

        result = data.get("result", {})
        pairs = {}

        for _, item in result.items():
            if item.get("status") != "online":
                continue

            wsname = item.get("wsname", "")

            if "/" not in wsname:
                continue

            base, quote = wsname.split("/", 1)

            if quote != "USD":
                continue

            symbol = normalize_symbol(base)
            altname = item.get("altname")

            if altname and symbol not in pairs:
                pairs[symbol] = altname

        print(f"Kraken USD pairs available: {len(pairs)}")
        return pairs

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as error:
        print(f"Kraken AssetPairs unavailable: {error}")
        return None


def get_hourly_volume_ratio(symbol, available_pairs):
    """
    Compare the latest completed hourly USD-volume estimate
    against the average of the preceding 24 completed hours.

    Estimate = base volume * candle VWAP.
    The currently forming candle is excluded.
    """

    if available_pairs is None:
        return None

    pair = available_pairs.get(normalize_symbol(symbol))

    if not pair:
        return None

    try:
        data = get_json(
            f"{KRAKEN_API}/OHLC",
            params={
                "pair": pair,
                "interval": 60,
            },
        )

        errors = data.get("error", [])

        if errors:
            print(f"Kraken OHLC error for {symbol}: {errors}")
            return None

        result = data.get("result", {})
        candle_keys = [
            key for key in result if key != "last"
        ]

        if not candle_keys:
            return None

        candles = result[candle_keys[0]]

        if not isinstance(candles, list):
            return None

        current_timestamp = int(now_utc().timestamp())

        completed = [
            candle for candle in candles
            if len(candle) >= 8
            and int(candle[0]) + 3600 <= current_timestamp
        ]

        completed.sort(key=lambda candle: int(candle[0]))

        if len(completed) < 25:
            return None

        last_25 = completed[-25:]
        usd_volumes = []

        for candle in last_25:
            vwap = float(candle[5])
            base_volume = float(candle[6])

            if vwap <= 0 or base_volume < 0:
                return None

            usd_volumes.append(vwap * base_volume)

        previous_24 = usd_volumes[:-1]
        latest_hour = usd_volumes[-1]
        average_volume = sum(previous_24) / len(previous_24)

        if average_volume <= 0:
            return None

        return latest_hour / average_volume

    except requests.HTTPError as error:
        status = (
            error.response.status_code
            if error.response is not None
            else "unknown"
        )
        print(f"Kraken HTTP error for {symbol}: {status}")
        return None

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ) as error:
        print(f"Kraken candle error for {symbol}: {error}")
        return None


# =========================
# HISTORY
# =========================

def load_history():
    data = load_json(HISTORY_FILE, {})
    return data if isinstance(data, dict) else {}


def get_recent_history(history, coin_id, reference_time):
    records = history.get(coin_id, [])

    if not isinstance(records, list):
        return []

    cutoff = reference_time - timedelta(hours=HISTORY_HOURS)
    recent = []

    for item in records:
        if not isinstance(item, dict):
            continue

        timestamp = parse_timestamp(item.get("time"))
        price = item.get("price")

        if timestamp is None or price is None:
            continue

        try:
            price = float(price)
        except (ValueError, TypeError):
            continue

        if price <= 0:
            continue

        if cutoff <= timestamp < reference_time:
            recent.append({
                "time": timestamp,
                "price": price,
            })

    recent.sort(key=lambda item: item["time"])
    return recent


def update_history(history, coin):
    coin_id = coin["id"]
    records = history.get(coin_id, [])

    if not isinstance(records, list):
        records = []

    snapshot_time = (
        parse_timestamp(coin.get("price_time"))
        or now_utc()
    )

    # Avoid duplicate records for the same timestamp.
    if not any(
        isinstance(item, dict)
        and item.get("time") == snapshot_time.isoformat()
        for item in records
    ):
        records.append({
            "time": snapshot_time.isoformat(),
            "price": coin["price"],
            "volume": coin["volume_24h"],
        })

    valid_records = []

    for item in records:
        if not isinstance(item, dict):
            continue

        timestamp = parse_timestamp(item.get("time"))

        try:
            price = float(item.get("price"))
        except (ValueError, TypeError):
            continue

        if timestamp is None or price <= 0:
            continue

        item["time"] = timestamp.isoformat()
        item["price"] = price
        valid_records.append(item)

    valid_records.sort(
        key=lambda item: parse_timestamp(item["time"])
    )

    history[coin_id] = valid_records[-MAX_HISTORY_POINTS:]


# =========================
# SIGNAL ANALYSIS
# =========================

def analyze_coin(coin, history, volume_ratio, diagnostics):
    reference_time = (
        parse_timestamp(coin.get("price_time"))
        or now_utc()
    )

    previous = get_recent_history(
        history,
        coin["id"],
        reference_time,
    )

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
        reasons.append(
            "Kraken hourly volume is at least 2x its previous 24h hourly average"
        )
    elif volume_ratio >= 1.5:
        score += 25
        reasons.append(
            "Kraken hourly volume is at least 1.5x average"
        )
    elif volume_ratio >= MIN_VOLUME_RATIO:
        score += 15
        reasons.append(
            "Kraken hourly volume exceeds the threshold"
        )

    if abs(coin["change_24h"]) >= 5:
        score += 20
        reasons.append("24h price change is at least 5%")
    elif abs(coin["change_24h"]) >= MIN_PRICE_CHANGE:
        score += 15
        reasons.append("24h price change is at least 2%")

    old_prices = [item["price"] for item in previous]

    # Breakout checks use only observations from the previous 24 hours.
    if old_prices:
        if coin["price"] > max(old_prices):
            score += 30
            reasons.append(
                "Price is above recorded prices from the previous 24 hours"
            )
        elif coin["price"] < min(old_prices):
            score += 30
            reasons.append(
                "Price is below recorded prices from the previous 24 hours"
            )

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
        "time": reference_time.isoformat(),
        "price_time": reference_time.isoformat(),
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
        "results": {},
        "evaluation_version": 2,
    }


# =========================
# TELEGRAM MESSAGES
# =========================

def format_money(value):
    if value is None:
        return "N/A"

    try:
        return f"${float(value):,.2f}"
    except (ValueError, TypeError):
        return "N/A"


def build_market_message(sentiment, fear_greed, market):
    if sentiment == "GREED":
        sentiment_icon = "🟢"
    elif sentiment == "FEAR":
        sentiment_icon = "🔴"
    else:
        sentiment_icon = "⚪"

    lines = [
        "━━━━━━━━━━━━━━━━━━",
        "🌐 CRYPTO EDGE | MARKET REPORT",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"{sentiment_icon} Market Sentiment: {sentiment}",
        (
            f"🧠 Fear & Greed Index: {fear_greed}/100"
            if fear_greed is not None
            else "🧠 Fear & Greed Index: N/A"
        ),
    ]

    if market:
        lines.extend([
            "",
            "📊 GLOBAL MARKET DATA",
            f"💎 Market Cap: {format_money(market.get('market_cap'))}",
            f"💰 24h Market Volume: {format_money(market.get('volume_24h'))}",
            f"₿ BTC Dominance: {market.get('btc_dominance', 'N/A')}%",
            f"📈 Market Cap Change: {market.get('market_change', 'N/A')}%",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📡 CRYPTO EDGE SMART MONEY RADAR",
        "Hourly volume is based on Kraken USD spot candles.",
        "This reflects Kraken activity, not the entire market.",
        "⚠️ Screening results only. No guaranteed trades.",
    ])

    return "\n".join(lines)


def build_signal_message(signal):
    is_bullish = signal["direction"] == "BULLISH"

    if is_bullish:
        header = "🟢 🚀 CRYPTO EDGE | BULLISH SIGNAL"
        direction_label = "📈 Direction: BULLISH"
    else:
        header = "🔴 🐻 CRYPTO EDGE | BEARISH SIGNAL"
        direction_label = "📉 Direction: BEARISH"

    score = signal["score"]

    if score >= 80:
        strength = "🔥 VERY STRONG"
    elif score >= 65:
        strength = "💪 STRONG"
    elif score >= 50:
        strength = "⚡ MODERATE"
    else:
        strength = "⚪ WEAK"

    change = signal["change_24h"]
    change_emoji = "🟢" if change >= 0 else "🔴"

    lines = [
        "━━━━━━━━━━━━━━━━━━",
        header,
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"🪙 Coin: {signal['name']} (${signal['symbol']})",
        f"💵 Price: ${signal['price']:.8f}",
        f"{change_emoji} 24h Change: {change:+.2f}%",
        f"💰 CMC 24h Volume: ${signal['volume_24h']:,.0f}",
        (
            "📊 Kraken Hourly Volume: "
            f"{signal['hourly_volume_ratio']:.2f}x average"
        ),
        "",
        f"🎯 Signal Score: {score}/100",
        f"⚡ Signal Strength: {strength}",
        direction_label,
        "",
        "🔍 WHY THIS SIGNAL?",
    ]

    lines.extend(
        f"  • {reason}" for reason in signal["reasons"]
    )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ ANALYSIS ONLY — NOT FINANCIAL ADVICE",
        "📡 Crypto Edge Smart Money Radar",
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
    available_pairs = get_kraken_usd_pairs()

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

    if available_pairs is None:
        print(
            "WARNING: Kraken pair list failed. "
            "Hourly volume signals will be unavailable."
        )

    for coin in coins:
        previous = history.get(coin["id"], [])

        if (
            isinstance(previous, list)
            and len(get_recent_history(
                history,
                coin["id"],
                parse_timestamp(coin["price_time"]) or now_utc(),
            )) >= MIN_HISTORY_POINTS
            and coin["volume_24h"] >= MIN_VOLUME_USD
        ):
            ratio = get_hourly_volume_ratio(
                coin["symbol"],
                available_pairs,
            )
            time.sleep(REQUEST_DELAY)
        else:
            ratio = None

        signal = analyze_coin(
            coin,
            history,
            ratio,
            diagnostics,
        )

        if signal:
            signals.append(signal)

        update_history(history, coin)

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
        print("Market Context Report failed.")

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
