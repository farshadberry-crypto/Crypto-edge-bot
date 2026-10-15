
import os
import json
import time
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE TRADE MANAGER
# Kraken Spot + Kraken Futures
# Paper tracking only. No real orders are placed.
# =========================================================

SPOT_BASE_URL = "https://api.kraken.com/0/public"
FUTURES_BASE_URL = "https://futures.kraken.com/derivatives/api/v3"

JOURNAL_FILE = "trade_journal.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

ENTRY_ZONE_PERCENT = 0.15
TP_ALLOCATIONS = [0.40, 0.30, 0.30]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-TradeManager/2.0",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def load_journal():
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, dict) and isinstance(data.get("trades"), list):
                return data
    except (OSError, json.JSONDecodeError):
        pass

    return {"trades": []}


def save_journal(journal):
    temporary_file = JOURNAL_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(journal, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary_file, JOURNAL_FILE)


def get_spot_price(pair):
    if not pair:
        raise RuntimeError("Missing Kraken Spot pair identifier.")

    response = SESSION.get(
        f"{SPOT_BASE_URL}/Ticker",
        params={"pair": pair},
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    errors = payload.get("error", [])

    if errors:
        raise RuntimeError("Kraken Spot API: " + "; ".join(errors))

    result = payload.get("result", {})

    if not result:
        raise RuntimeError(f"No Kraken Spot ticker returned for {pair}")

    ticker = next(iter(result.values()))
    last_trade = ticker.get("c", [])

    if not last_trade:
        raise RuntimeError(f"Missing last-trade price for {pair}")

    price = float(last_trade[0])

    if price <= 0:
        raise RuntimeError(f"Invalid Spot price for {pair}: {price}")

    return price


def get_futures_price(symbol):
    if not symbol:
        raise RuntimeError("Missing Kraken Futures contract symbol.")

    response = SESSION.get(
        f"{FUTURES_BASE_URL}/tickers",
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()

    if payload.get("result") != "success":
        raise RuntimeError(
            "Kraken Futures API error: "
            + str(payload.get("error", payload))
        )

    tickers = payload.get("tickers", [])

    ticker = next(
        (
            item for item in tickers
            if item.get("symbol", "").upper() == symbol.upper()
        ),
        None,
    )

    if ticker is None:
        raise RuntimeError(
            f"Kraken Futures contract not found: {symbol}"
        )

    raw_price = ticker.get("last")

    if raw_price is None:
        raw_price = ticker.get("markPrice")

    if raw_price is None:
        raise RuntimeError(
            f"No usable last/mark price for contract {symbol}"
        )

    price = float(raw_price)

    if price <= 0:
        raise RuntimeError(
            f"Invalid Futures price for {symbol}: {price}"
        )

    return price


def get_price(trade):
    exchange = str(trade.get("exchange", "")).lower()
    market_type = str(
        trade.get("market_type", "")
    ).lower()

    exchange_symbol = trade.get("exchange_symbol")

    if exchange != "kraken":
        raise RuntimeError(
            f"Unsupported exchange '{exchange}'. "
            "Only Kraken trades are accepted by this manager."
        )

    if not exchange_symbol:
        raise RuntimeError(
            "Missing exchange_symbol. The signal engine must "
            "save the exact Kraken market or contract identifier."
        )

    if market_type == "spot" or trade.get("category") == "spot":
        return get_spot_price(exchange_symbol)

    if market_type == "futures" or trade.get("category") == "linear":
        return get_futures_price(exchange_symbol)

    raise RuntimeError(
        f"Unsupported market type '{market_type}' "
        f"for {trade.get('symbol', 'unknown')}."
    )


def send_telegram(message):
    if not BOT_TOKEN:
        print("Telegram token missing. Message not sent:")
        print(message)
        return False

    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()

        if not payload.get("ok"):
            print("Telegram rejected message:", payload)
            return False

        return True

    except requests.RequestException as exc:
        print("Telegram delivery failed:", exc)
        return False


def fmt(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.7f}"


def entry_zone(trade):
    entry = float(trade["entry"])
    tolerance = entry * ENTRY_ZONE_PERCENT / 100
    return entry - tolerance, entry + tolerance


def crossed_entry(trade, price):
    low, high = entry_zone(trade)
    return low <= price <= high


def is_stop_hit(trade, price):
    if trade["direction"] == "LONG":
        return price <= float(trade["stop_loss"])
    return price >= float(trade["stop_loss"])


def is_target_hit(trade, price, target):
    if trade["direction"] == "LONG":
        return price >= float(target)
    return price <= float(target)


def pnl_percent(trade, exit_price):
    entry = float(trade["entry"])

    if entry <= 0:
        return 0.0

    if trade["direction"] == "LONG":
        return (exit_price - entry) / entry * 100

    return (entry - exit_price) / entry * 100


def initialize_trade(trade):
    trade.setdefault("status", "WAITING_ENTRY")
    trade.setdefault("created_at", now_utc())
    trade.setdefault("entry_confirmed_at", None)
    trade.setdefault("confirmed_entry_price", None)
    trade.setdefault("closed_at", None)
    trade.setdefault("last_checked_at", None)
    trade.setdefault("targets_hit", [])
    trade.setdefault("exits", [])
    trade.setdefault("remaining_fraction", 1.0)
    trade.setdefault("realized_pnl_percent", 0.0)
    trade.setdefault("messages_sent", [])
    trade.setdefault("result", None)
    return trade


def notify_once(trade, key, message):
    if key in trade["messages_sent"]:
        return

    if send_telegram(message):
        trade["messages_sent"].append(key)


def open_trade(trade, price):
    trade["status"] = "OPEN"
    trade["entry_confirmed_at"] = now_utc()
    trade["confirmed_entry_price"] = price

    low, high = entry_zone(trade)

    message = (
        "🟢 CRYPTO EDGE | TRADE OPENED\n\n"
        f"🏦 Market: {trade['market']}\n"
        f"🪙 Coin: {trade['symbol']}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"🎯 Planned entry: {fmt(float(trade['entry']))}\n"
        f"✅ Observed entry-zone price: {fmt(price)}\n"
        f"📍 Entry zone: {fmt(low)} - {fmt(high)}\n"
        f"🛑 Stop loss: {fmt(float(trade['stop_loss']))}\n"
        f"💰 TP1: {fmt(float(trade['targets'][0]))}\n"
        f"💰 TP2: {fmt(float(trade['targets'][1]))}\n"
        f"💰 TP3: {fmt(float(trade['targets'][2]))}\n\n"
        "📒 Paper-trade tracking only. No real order was placed."
    )

    notify_once(trade, "opened", message)


def close_remaining(trade, price, reason):
    remaining = float(trade["remaining_fraction"])

    if remaining > 0:
        move = pnl_percent(trade, price)
        trade["realized_pnl_percent"] += move * remaining

        trade["exits"].append({
            "time": now_utc(),
            "price": price,
            "fraction": remaining,
            "pnl_percent": move,
            "reason": reason,
        })

        trade["remaining_fraction"] = 0.0

    trade["status"] = "CLOSED"
    trade["closed_at"] = now_utc()

    total = float(trade["realized_pnl_percent"])
    trade["result"] = (
        "WIN" if total > 0
        else "LOSS" if total < 0
        else "BREAKEVEN"
    )

    message = (
        "🏁 CRYPTO EDGE | TRADE CLOSED\n\n"
        f"🏦 Market: {trade['market']}\n"
        f"🪙 Coin: {trade['symbol']}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"📌 Reason: {reason}\n"
        f"💵 Observed exit price: {fmt(price)}\n"
        f"📊 Estimated realized return: {total:+.3f}%\n"
        f"📋 Result: {trade['result']}\n\n"
        "⚠️ Paper-trade estimate, before fees, slippage and funding."
    )

    notify_once(trade, "closed", message)


def process_open_trade(trade, price):
    # Ticker polling cannot prove the order of price movements
    # between scans. This is a paper-trading estimate.

    if is_stop_hit(trade, price):
        close_remaining(trade, price, "STOP LOSS")
        return

    targets = trade["targets"]
    targets_hit = set(trade["targets_hit"])

    for index, target in enumerate(targets):
        key = f"TP{index + 1}"

        if key in targets_hit:
            continue

        if not is_target_hit(trade, price, float(target)):
            continue

        allocation = min(
            TP_ALLOCATIONS[index],
            float(trade["remaining_fraction"]),
        )

        if allocation <= 0:
            continue

        move = pnl_percent(trade, float(target))
        trade["realized_pnl_percent"] += move * allocation
        trade["remaining_fraction"] -= allocation
        trade["targets_hit"].append(key)

        trade["exits"].append({
            "time": now_utc(),
            "price": float(target),
            "fraction": allocation,
            "pnl_percent": move,
            "reason": key,
        })

        message = (
            f"💰 CRYPTO EDGE | {key} HIT\n\n"
            f"🪙 Coin: {trade['symbol']}\n"
            f"🏦 Market: {trade['market']}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"🎯 Target price: {fmt(float(target))}\n"
            f"📤 Suggested paper exit: {allocation * 100:.0f}% "
            "of original position\n"
            f"📦 Remaining position: "
            f"{trade['remaining_fraction'] * 100:.0f}%\n"
            f"📊 Return on this portion: {move:+.3f}%\n"
            f"🧾 Accumulated estimated return: "
            f"{trade['realized_pnl_percent']:+.3f}%\n\n"
            "Paper-trade update only. No real order was executed."
        )

        notify_once(trade, key, message)

    if trade["remaining_fraction"] <= 0.000001:
        trade["remaining_fraction"] = 0.0
        trade["status"] = "CLOSED"
        trade["closed_at"] = now_utc()

        total = float(trade["realized_pnl_percent"])
        trade["result"] = (
            "WIN" if total > 0
            else "LOSS" if total < 0
            else "BREAKEVEN"
        )

        notify_once(
            trade,
            "closed",
            "🏁 CRYPTO EDGE | ALL TARGETS COMPLETED\n\n"
            f"🪙 Coin: {trade['symbol']}\n"
            f"🏦 Market: {trade['market']}\n"
            f"📊 Estimated realized return: {total:+.3f}%\n"
            f"📋 Result: {trade['result']}\n\n"
            "Paper-trade estimate, before fees and slippage.",
        )


def calculate_stats(trades, market=None):
    closed = [
        trade for trade in trades
        if trade.get("status") == "CLOSED"
        and (market is None or trade.get("market") == market)
    ]

    wins = sum(
        1 for trade in closed
        if float(trade.get("realized_pnl_percent", 0)) > 0
    )
    losses = sum(
        1 for trade in closed
        if float(trade.get("realized_pnl_percent", 0)) < 0
    )

    total = len(closed)
    win_rate = wins / total * 100 if total else 0.0

    total_return = sum(
        float(trade.get("realized_pnl_percent", 0))
        for trade in closed
    )

    gross_profit = sum(
        max(0.0, float(trade.get("realized_pnl_percent", 0)))
        for trade in closed
    )

    gross_loss = abs(sum(
        min(0.0, float(trade.get("realized_pnl_percent", 0)))
        for trade in closed
    ))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else None
    )

    return {
        "closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_return_percent": total_return,
        "profit_factor": profit_factor,
    }


def main():
    print("Crypto Edge Kraken Trade Manager started:", now_utc())

    journal = load_journal()
    trades = journal["trades"]

    for trade in trades:
        if not isinstance(trade, dict):
            continue

        initialize_trade(trade)

        if trade["status"] not in ("WAITING_ENTRY", "OPEN"):
            continue

        try:
            price = get_price(trade)
            trade["last_checked_at"] = now_utc()

            if trade["status"] == "WAITING_ENTRY":
                if crossed_entry(trade, price):
                    open_trade(trade, price)
                else:
                    low, high = entry_zone(trade)
                    print(
                        f"Waiting: {trade['symbol']} "
                        f"market={trade.get('market')} "
                        f"price={fmt(price)} "
                        f"entry_zone={fmt(low)}-{fmt(high)}"
                    )

            elif trade["status"] == "OPEN":
                process_open_trade(trade, price)

        except Exception as exc:
            print(
                f"Trade manager error for "
                f"{trade.get('symbol', 'unknown')}: {exc}"
            )

        time.sleep(0.20)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_journal(journal)

    print("Closed-trade statistics:")
    print("All markets:", calculate_stats(trades))
    print("Spot:", calculate_stats(trades, "SPOT"))
    print("Futures:", calculate_stats(trades, "FUTURES"))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
