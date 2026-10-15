
import os
import json
import time
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE TRADE MANAGER
# Paper-trade tracking | Spot + Futures
# Waiting entry -> Open -> Partial exits -> Closed
# =========================================================

BASE_URL = "https://api.bybit.com"
JOURNAL_FILE = "trade_journal.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

# Entry is confirmed when price reaches this zone around entry.
ENTRY_ZONE_PERCENT = 0.15

# Exit allocation: 40% at TP1, 30% at TP2, 30% at TP3.
TP_ALLOCATIONS = [0.40, 0.30, 0.30]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoEdge-TradeManager/1.0"})


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


def api_get(path, params):
    response = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()

    if payload.get("retCode") != 0:
        raise RuntimeError(
            payload.get("retMsg", "Unknown Bybit API error")
        )

    return payload.get("result", {})


def get_price(category, symbol):
    result = api_get(
        "/v5/market/tickers",
        {
            "category": category,
            "symbol": symbol,
        },
    )

    tickers = result.get("list", [])

    if not tickers:
        raise RuntimeError(f"No ticker returned for {symbol}")

    return float(tickers[0]["lastPrice"])


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

    # Only mark a message as sent if Telegram confirms delivery.
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
    trade["result"] = (
        "WIN" if trade["realized_pnl_percent"] > 0
        else "LOSS" if trade["realized_pnl_percent"] < 0
        else "BREAKEVEN"
    )

    total = trade["realized_pnl_percent"]

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
    # If a target and stop could both have been crossed between
    # scans, ticker snapshots cannot establish which happened first.
    # This manager uses the current observed price only and therefore
    # does not claim historical execution accuracy.

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
        trade["result"] = (
            "WIN" if trade["realized_pnl_percent"] > 0
            else "LOSS" if trade["realized_pnl_percent"] < 0
            else "BREAKEVEN"
        )

        notify_once(
            trade,
            "closed",
            "🏁 CRYPTO EDGE | ALL TARGETS COMPLETED\n\n"
            f"🪙 Coin: {trade['symbol']}\n"
            f"🏦 Market: {trade['market']}\n"
            f"📊 Estimated realized return: "
            f"{trade['realized_pnl_percent']:+.3f}%\n"
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
    print("Crypto Edge Trade Manager started:", now_utc())

    journal = load_journal()
    trades = journal["trades"]
    changed = False

    for trade in trades:
        initialize_trade(trade)

        if trade["status"] not in ("WAITING_ENTRY", "OPEN"):
            continue

        try:
            price = get_price(
                trade["category"],
                trade["symbol"],
            )
            trade["last_checked_at"] = now_utc()

            if trade["status"] == "WAITING_ENTRY":
                if crossed_entry(trade, price):
                    open_trade(trade, price)
                    changed = True
                else:
                    low, high = entry_zone(trade)
                    print(
                        f"Waiting: {trade['symbol']} "
                        f"price={fmt(price)} "
                        f"entry_zone={fmt(low)}-{fmt(high)}"
                    )

            elif trade["status"] == "OPEN":
                before_status = trade["status"]
                before_targets = len(trade["targets_hit"])
                before_exits = len(trade["exits"])

                process_open_trade(trade, price)

                if (
                    trade["status"] != before_status
                    or len(trade["targets_hit"]) != before_targets
                    or len(trade["exits"]) != before_exits
                ):
                    changed = True

        except Exception as exc:
            print(
                f"Trade manager error for "
                f"{trade.get('symbol', 'unknown')}: {exc}"
            )

        time.sleep(0.15)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()

    # Save every run so the last-check timestamp is retained.
    save_journal(journal)

    print("Closed-trade statistics:")
    print("All markets:", calculate_stats(trades))
    print("Spot:", calculate_stats(trades, "SPOT"))
    print("Futures:", calculate_stats(trades, "FUTURES"))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
