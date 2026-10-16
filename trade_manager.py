
import os
import json
import time
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE FUTURES TRADE MANAGER
# Kraken Futures only
# Paper tracking only. No real orders are placed.
# =========================================================

FUTURES_BASE_URL = "https://futures.kraken.com/derivatives/api/v3"
JOURNAL_FILE = "trade_journal.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

ENTRY_ZONE_PERCENT = 0.15
TP_ALLOCATIONS = [0.40, 0.30, 0.30]
MAX_LEVERAGE = 5.0
MIN_LEVERAGE = 1.0

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-Futures-TradeManager/3.0",
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


def is_futures_trade(trade):
    return (
        str(trade.get("market_type", "")).lower() == "futures"
        or str(trade.get("category", "")).lower() == "linear"
        or str(trade.get("market", "")).upper() == "FUTURES"
    )


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
            if str(item.get("symbol", "")).upper() == str(symbol).upper()
        ),
        None,
    )

    if ticker is None:
        raise RuntimeError(f"Kraken Futures contract not found: {symbol}")

    raw_price = ticker.get("last")

    if raw_price is None:
        raw_price = ticker.get("markPrice")

    if raw_price is None:
        raise RuntimeError(f"No usable last/mark price for {symbol}")

    price = float(raw_price)

    if price <= 0:
        raise RuntimeError(f"Invalid Futures price for {symbol}: {price}")

    return price


def get_price(trade):
    if not is_futures_trade(trade):
        raise RuntimeError("This manager only processes Futures trades.")

    exchange = str(trade.get("exchange", "")).lower()

    if exchange != "kraken":
        raise RuntimeError(
            f"Unsupported exchange '{exchange}'. Only Kraken Futures "
            "contracts are supported."
        )

    symbol = trade.get("exchange_symbol")

    if not symbol:
        raise RuntimeError("Missing exact Kraken Futures exchange_symbol.")

    return get_futures_price(symbol)


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
    price = float(price)

    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.7f}"


def get_leverage(trade):
    try:
        leverage = float(trade.get("leverage", MIN_LEVERAGE))
    except (TypeError, ValueError):
        leverage = MIN_LEVERAGE

    return max(MIN_LEVERAGE, min(MAX_LEVERAGE, leverage))


def get_stop_risk_on_margin(trade):
    try:
        stored_risk = float(trade.get("estimated_margin_loss_percent", 0))
        if stored_risk > 0:
            return stored_risk
    except (TypeError, ValueError):
        pass

    try:
        entry = float(trade["entry"])
        stop = float(trade["stop_loss"])

        if entry <= 0:
            return 0.0

        stop_distance_percent = abs(entry - stop) / entry * 100
        return stop_distance_percent * get_leverage(trade)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0.0


def leverage_summary(trade):
    leverage = get_leverage(trade)
    stop_risk = get_stop_risk_on_margin(trade)

    return (
        f"⚙️ Suggested leverage: {leverage:.1f}x\n"
        f"⚠️ Estimated margin loss at SL: {stop_risk:.2f}%\n"
        "Estimate excludes fees, slippage, funding and liquidation effects."
    )


def entry_zone(trade):
    entry = float(trade["entry"])
    tolerance = entry * ENTRY_ZONE_PERCENT / 100
    return entry - tolerance, entry + tolerance


def crossed_entry(trade, price):
    low, high = entry_zone(trade)
    return low <= price <= high


def is_stop_hit(trade, price):
    stop = float(trade["stop_loss"])

    if trade["direction"] == "LONG":
        return price <= stop

    return price >= stop


def is_target_hit(trade, price, target):
    if trade["direction"] == "LONG":
        return price >= target

    return price <= target


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

    if not isinstance(trade["targets_hit"], list):
        trade["targets_hit"] = []

    if not isinstance(trade["exits"], list):
        trade["exits"] = []

    if not isinstance(trade["messages_sent"], list):
        trade["messages_sent"] = []

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
        "🟢 CRYPTO EDGE | FUTURES TRADE OPENED\n\n"
        f"🏦 Market: FUTURES\n"
        f"🪙 Coin: {trade['symbol']}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"🎯 Planned entry: {fmt(trade['entry'])}\n"
        f"✅ Observed entry-zone price: {fmt(price)}\n"
        f"📍 Entry zone: {fmt(low)} - {fmt(high)}\n"
        f"🛑 Stop loss: {fmt(trade['stop_loss'])}\n"
        f"💰 TP1: {fmt(trade['targets'][0])}\n"
        f"💰 TP2: {fmt(trade['targets'][1])}\n"
        f"💰 TP3: {fmt(trade['targets'][2])}\n\n"
        f"{leverage_summary(trade)}\n\n"
        "📒 Paper tracking only. No real order was placed."
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
    leverage = get_leverage(trade)
    estimated_margin_return = total * leverage

    trade["estimated_margin_return_percent"] = estimated_margin_return
    trade["result"] = (
        "WIN" if total > 0
        else "LOSS" if total < 0
        else "BREAKEVEN"
    )

    message = (
        "🏁 CRYPTO EDGE | FUTURES TRADE CLOSED\n\n"
        f"🪙 Coin: {trade['symbol']}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"📌 Reason: {reason}\n"
        f"💵 Observed exit price: {fmt(price)}\n"
        f"⚙️ Suggested leverage: {leverage:.1f}x\n"
        f"📊 Estimated underlying return: {total:+.3f}%\n"
        f"💹 Estimated return on allocated margin: "
        f"{estimated_margin_return:+.3f}%\n"
        f"📋 Result: {trade['result']}\n\n"
        "⚠️ Paper-trade estimates before fees, slippage, funding "
        "and liquidation effects. Not an actual account return."
    )

    notify_once(trade, "closed", message)


def process_open_trade(trade, price):
    # Periodic ticker polling cannot establish the exact order of
    # price movements between scans. This is only a paper estimate.

    if is_stop_hit(trade, price):
        close_remaining(trade, price, "STOP LOSS")
        return

    targets = trade["targets"]
    targets_hit = set(trade["targets_hit"])
    leverage = get_leverage(trade)

    for index, target in enumerate(targets):
        key = f"TP{index + 1}"

        if key in targets_hit:
            continue

        target = float(target)

        if not is_target_hit(trade, price, target):
            continue

        allocation = min(
            TP_ALLOCATIONS[index],
            float(trade["remaining_fraction"]),
        )

        if allocation <= 0:
            continue

        move = pnl_percent(trade, target)
        trade["realized_pnl_percent"] += move * allocation
        trade["remaining_fraction"] -= allocation
        trade["targets_hit"].append(key)

        trade["exits"].append({
            "time": now_utc(),
            "price": target,
            "fraction": allocation,
            "pnl_percent": move,
            "reason": key,
        })

        accumulated = float(trade["realized_pnl_percent"])
        accumulated_margin_return = accumulated * leverage
        portion_margin_return = move * leverage

        message = (
            f"💰 CRYPTO EDGE | FUTURES {key} HIT\n\n"
            f"🪙 Coin: {trade['symbol']}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"🎯 Target price: {fmt(target)}\n"
            f"⚙️ Suggested leverage: {leverage:.1f}x\n"
            f"📤 Suggested paper exit: {allocation * 100:.0f}% "
            "of original position\n"
            f"📦 Remaining position: "
            f"{trade['remaining_fraction'] * 100:.0f}%\n"
            f"📊 Underlying move at target: {move:+.3f}%\n"
            f"💹 Estimated leveraged move on exited portion: "
            f"{portion_margin_return:+.3f}%\n"
            f"🧾 Accumulated underlying return: {accumulated:+.3f}%\n"
            f"📈 Accumulated estimated margin return: "
            f"{accumulated_margin_return:+.3f}%\n\n"
            "Paper-trade update only. No real order was executed. "
            "Estimates exclude fees, slippage and funding."
        )

        notify_once(trade, key, message)

    if trade["remaining_fraction"] <= 0.000001:
        trade["remaining_fraction"] = 0.0
        trade["status"] = "CLOSED"
        trade["closed_at"] = now_utc()

        total = float(trade["realized_pnl_percent"])
        estimated_margin_return = total * leverage

        trade["estimated_margin_return_percent"] = estimated_margin_return
        trade["result"] = (
            "WIN" if total > 0
            else "LOSS" if total < 0
            else "BREAKEVEN"
        )

        notify_once(
            trade,
            "closed",
            "🏁 CRYPTO EDGE | ALL FUTURES TARGETS COMPLETED\n\n"
            f"🪙 Coin: {trade['symbol']}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"⚙️ Suggested leverage: {leverage:.1f}x\n"
            f"📊 Estimated underlying return: {total:+.3f}%\n"
            f"💹 Estimated return on allocated margin: "
            f"{estimated_margin_return:+.3f}%\n"
            f"📋 Result: {trade['result']}\n\n"
            "Paper-trade estimate before fees, slippage, funding "
            "and liquidation effects. No real order was placed."
        )


def calculate_stats(trades):
    futures_closed = [
        trade for trade in trades
        if isinstance(trade, dict)
        and is_futures_trade(trade)
        and trade.get("status") == "CLOSED"
    ]

    wins = sum(
        1 for trade in futures_closed
        if float(trade.get("realized_pnl_percent", 0)) > 0
    )
    losses = sum(
        1 for trade in futures_closed
        if float(trade.get("realized_pnl_percent", 0)) < 0
    )

    total = len(futures_closed)
    win_rate = wins / total * 100 if total else 0.0

    total_return = sum(
        float(trade.get("realized_pnl_percent", 0))
        for trade in futures_closed
    )

    total_margin_return = sum(
        float(
            trade.get(
                "estimated_margin_return_percent",
                float(trade.get("realized_pnl_percent", 0))
                * get_leverage(trade),
            )
        )
        for trade in futures_closed
    )

    gross_profit = sum(
        max(0.0, float(trade.get("realized_pnl_percent", 0)))
        for trade in futures_closed
    )

    gross_loss = abs(sum(
        min(0.0, float(trade.get("realized_pnl_percent", 0)))
        for trade in futures_closed
    ))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else None
    )

    return {
        "closed_futures": total,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(win_rate, 2),
        "total_underlying_return_percent": round(total_return, 3),
        "estimated_total_margin_return_percent": round(
            total_margin_return, 3
        ),
        "profit_factor": (
            round(profit_factor, 3)
            if profit_factor is not None
            else None
        ),
    }


def main():
    print("Crypto Edge Futures Trade Manager started:", now_utc())

    journal = load_journal()
    trades = journal["trades"]

    for trade in trades:
        if not isinstance(trade, dict):
            continue

        # Old Spot records remain in the journal but are not processed.
        if not is_futures_trade(trade):
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
                        f"Waiting Futures: {trade['symbol']} "
                        f"direction={trade.get('direction')} "
                        f"leverage={get_leverage(trade):.1f}x "
                        f"price={fmt(price)} "
                        f"entry_zone={fmt(low)}-{fmt(high)}"
                    )

            elif trade["status"] == "OPEN":
                process_open_trade(trade, price)

        except Exception as exc:
            print(
                f"Futures manager error for "
                f"{trade.get('symbol', 'unknown')}: {exc}"
            )

        time.sleep(0.20)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_journal(journal)

    print("Closed Futures statistics:")
    print(json.dumps(calculate_stats(trades), ensure_ascii=False, indent=2))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
