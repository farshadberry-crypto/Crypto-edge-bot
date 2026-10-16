
import os
import json
import time
import math
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE FUTURES TRADE MANAGER
# Kraken linear futures only
# Paper tracking only. No real orders are placed.
# =========================================================

FUTURES_BASE_URL = "https://futures.kraken.com/derivatives/api/v3"
JOURNAL_FILE = "trade_journal.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

FIXED_LEVERAGE = 10.0
ENTRY_ZONE_PERCENT = 0.15
TP_ALLOCATIONS = [0.40, 0.30, 0.30]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-Futures-TradeManager/4.0",
    "Accept": "application/json",
})

_TICKER_CACHE = {}
_TICKER_CACHE_TIME = 0.0
TICKER_CACHE_SECONDS = 15


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


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
        str(trade.get("exchange", "")).lower() == "kraken"
        and str(trade.get("market_type", "")).lower() == "futures"
        and str(trade.get("category", "")).lower() == "linear"
        and str(trade.get("market", "")).upper() == "FUTURES"
    )


def get_tickers():
    global _TICKER_CACHE, _TICKER_CACHE_TIME

    now = time.monotonic()

    if (
        _TICKER_CACHE
        and now - _TICKER_CACHE_TIME < TICKER_CACHE_SECONDS
    ):
        return _TICKER_CACHE

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
    result = {}

    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()
        if symbol:
            result[symbol] = ticker

    if not result:
        raise RuntimeError("Kraken Futures returned an empty ticker list.")

    _TICKER_CACHE = result
    _TICKER_CACHE_TIME = now

    return result


def get_futures_price(symbol, tickers=None):
    if not symbol:
        raise RuntimeError("Missing exact Kraken Futures contract symbol.")

    if tickers is None:
        tickers = get_tickers()

    ticker = tickers.get(str(symbol).upper())

    if ticker is None:
        raise RuntimeError(f"Kraken Futures contract not found: {symbol}")

    raw_price = ticker.get("last")

    if not finite_number(raw_price) or float(raw_price) <= 0:
        raw_price = ticker.get("markPrice")

    if not finite_number(raw_price) or float(raw_price) <= 0:
        raise RuntimeError(f"No usable market price for {symbol}")

    return float(raw_price)


def get_price(trade, tickers=None):
    if not is_futures_trade(trade):
        raise RuntimeError("Trade is not a supported Kraken linear future.")

    symbol = trade.get("exchange_symbol")

    if not symbol:
        raise RuntimeError("Missing exact exchange_symbol.")

    if not str(symbol).upper().startswith(("PF_", "PI_")):
        raise RuntimeError(
            f"Unexpected Futures contract format: {symbol}"
        )

    return get_futures_price(symbol, tickers)


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

    if abs(price) >= 1000:
        return f"{price:,.2f}"
    if abs(price) >= 1:
        return f"{price:.4f}"
    if abs(price) >= 0.01:
        return f"{price:.6f}"
    if abs(price) >= 0.0001:
        return f"{price:.8f}"

    return f"{price:.10f}".rstrip("0").rstrip(".")


def display_symbol(trade):
    raw = str(
        trade.get("symbol")
        or trade.get("exchange_symbol")
        or "UNKNOWN"
    ).upper()

    if raw.startswith("PF_"):
        raw = raw[3:]
    elif raw.startswith("PI_"):
        raw = raw[3:]

    for quote in ("USDT", "USDC", "USD", "EUR", "GBP"):
        if raw.endswith(quote) and len(raw) > len(quote):
            base = raw[:-len(quote)]
            if base == "XBT":
                base = "BTC"
            return f"{base}/{quote}"

    return raw


def get_leverage(trade):
    # Preserve historical leverage. New trades should be registered
    # by register_signals.py with leverage exactly equal to 10x.
    try:
        leverage = float(trade.get("leverage", FIXED_LEVERAGE))
    except (TypeError, ValueError):
        leverage = FIXED_LEVERAGE

    if not math.isfinite(leverage) or leverage <= 0:
        return FIXED_LEVERAGE

    return leverage


def get_stop_risk_on_margin(trade):
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
        f"⚙️ Leverage: {leverage:.1f}x\n"
        f"⚠️ Estimated margin loss at SL: {stop_risk:.2f}%\n"
        "Excludes fees, slippage, funding and liquidation effects."
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

    for key in ("targets_hit", "exits", "messages_sent"):
        if not isinstance(trade.get(key), list):
            trade[key] = []

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
    symbol = display_symbol(trade)

    message = (
        "🟢 CRYPTO EDGE | FUTURES TRADE OPENED\n\n"
        f"🏦 Market: FUTURES\n"
        f"🪙 Coin: {symbol}\n"
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


def finalize_trade(trade):
    total = float(trade.get("realized_pnl_percent", 0.0))
    leverage = get_leverage(trade)
    estimated_margin_return = total * leverage

    trade["remaining_fraction"] = 0.0
    trade["status"] = "CLOSED"
    trade["closed_at"] = now_utc()
    trade["estimated_margin_return_percent"] = estimated_margin_return
    trade["result"] = (
        "WIN" if total > 0
        else "LOSS" if total < 0
        else "BREAKEVEN"
    )

    return total, leverage, estimated_margin_return


def close_remaining(trade, price, reason):
    remaining = max(0.0, float(trade["remaining_fraction"]))

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

    total, leverage, margin_return = finalize_trade(trade)
    symbol = display_symbol(trade)

    message = (
        "🏁 CRYPTO EDGE | FUTURES TRADE CLOSED\n\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"📌 Reason: {reason}\n"
        f"💵 Observed exit price: {fmt(price)}\n"
        f"⚙️ Leverage: {leverage:.1f}x\n"
        f"📊 Estimated underlying return: {total:+.3f}%\n"
        f"💹 Estimated return on allocated margin: {margin_return:+.3f}%\n"
        f"📋 Result: {trade['result']}\n\n"
        "Paper-trade estimate before fees, slippage, funding "
        "and liquidation effects. Not an actual account return."
    )

    notify_once(trade, "closed", message)


def process_open_trade(trade, price):
    # Ticker polling cannot reveal the exact intrabar sequence.
    # Stop is checked first as a conservative paper-tracking rule.
    if is_stop_hit(trade, price):
        close_remaining(trade, price, "STOP LOSS")
        return

    targets = trade["targets"]
    targets_hit = set(trade["targets_hit"])
    leverage = get_leverage(trade)
    symbol = display_symbol(trade)

    for index, target in enumerate(targets):
        key = f"TP{index + 1}"

        if key in targets_hit:
            continue

        target = float(target)

        if not is_target_hit(trade, price, target):
            continue

        remaining = max(0.0, float(trade["remaining_fraction"]))
        allocation = min(TP_ALLOCATIONS[index], remaining)

        if allocation <= 0:
            continue

        move = pnl_percent(trade, target)
        trade["realized_pnl_percent"] += move * allocation
        trade["remaining_fraction"] = max(0.0, remaining - allocation)
        trade["targets_hit"].append(key)

        trade["exits"].append({
            "time": now_utc(),
            "price": target,
            "fraction": allocation,
            "pnl_percent": move,
            "reason": key,
        })

        accumulated = float(trade["realized_pnl_percent"])
        margin_return = accumulated * leverage
        portion_margin_return = move * leverage

        message = (
            f"💰 CRYPTO EDGE | FUTURES {key} HIT\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"🎯 Target price: {fmt(target)}\n"
            f"⚙️ Leverage: {leverage:.1f}x\n"
            f"📤 Paper exit: {allocation * 100:.0f}% of original position\n"
            f"📦 Remaining position: "
            f"{trade['remaining_fraction'] * 100:.0f}%\n"
            f"📊 Underlying move at target: {move:+.3f}%\n"
            f"💹 Leveraged move on exited portion: "
            f"{portion_margin_return:+.3f}%\n"
            f"🧾 Accumulated underlying return: {accumulated:+.3f}%\n"
            f"📈 Accumulated estimated margin return: {margin_return:+.3f}%\n\n"
            "Paper-trade update only. No real order was executed. "
            "Estimates exclude fees, slippage and funding."
        )

        notify_once(trade, key, message)

    if trade["remaining_fraction"] <= 0.000001:
        trade["remaining_fraction"] = 0.0
        total, leverage, margin_return = finalize_trade(trade)

        message = (
            "🏁 CRYPTO EDGE | ALL FUTURES TARGETS COMPLETED\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"⚙️ Leverage: {leverage:.1f}x\n"
            f"📊 Estimated underlying return: {total:+.3f}%\n"
            f"💹 Estimated return on allocated margin: {margin_return:+.3f}%\n"
            f"📋 Result: {trade['result']}\n\n"
            "Paper-trade estimate before fees, slippage and funding. "
            "No real order was placed."
        )

        notify_once(trade, "closed", message)


def calculate_stats(trades):
    closed = [
        trade for trade in trades
        if isinstance(trade, dict)
        and is_futures_trade(trade)
        and trade.get("status") == "CLOSED"
    ]

    def realized(trade):
        try:
            value = float(trade.get("realized_pnl_percent", 0))
            return value if math.isfinite(value) else 0.0
        except (TypeError, ValueError):
            return 0.0

    wins = sum(1 for trade in closed if realized(trade) > 0)
    losses = sum(1 for trade in closed if realized(trade) < 0)
    total = len(closed)

    gross_profit = sum(max(0.0, realized(trade)) for trade in closed)
    gross_loss = abs(sum(min(0.0, realized(trade)) for trade in closed))

    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else None
    )

    return {
        "closed_futures": total,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(wins / total * 100, 2) if total else 0.0,
        "total_underlying_return_percent": round(
            sum(realized(trade) for trade in closed), 3
        ),
        "estimated_total_margin_return_percent": round(
            sum(
                float(
                    trade.get(
                        "estimated_margin_return_percent",
                        realized(trade) * get_leverage(trade),
                    )
                )
                for trade in closed
            ),
            3,
        ),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
    }


def main():
    print("Crypto Edge Futures Trade Manager started:", now_utc())
    print("New-trade target leverage:", f"{FIXED_LEVERAGE:.1f}x")
    print("Paper tracking only.")

    journal = load_journal()
    trades = journal["trades"]

    active_trades = [
        trade for trade in trades
        if isinstance(trade, dict)
        and is_futures_trade(trade)
        and trade.get("status", "WAITING_ENTRY") in ("WAITING_ENTRY", "OPEN")
    ]

    tickers = None

    if active_trades:
        try:
            tickers = get_tickers()
            print("Fetched Kraken Futures tickers:", len(tickers))
        except Exception as exc:
            print("Could not fetch Futures tickers:", exc)
            journal["updated_at"] = now_utc()
            save_journal(journal)
            raise

    for trade in active_trades:
        initialize_trade(trade)

        try:
            price = get_price(trade, tickers)
            trade["last_checked_at"] = now_utc()

            if trade["status"] == "WAITING_ENTRY":
                if crossed_entry(trade, price):
                    open_trade(trade, price)
                else:
                    low, high = entry_zone(trade)
                    print(
                        f"Waiting: {display_symbol(trade)} "
                        f"{trade.get('direction')} "
                        f"lev={get_leverage(trade):.1f}x "
                        f"price={fmt(price)} "
                        f"zone={fmt(low)}-{fmt(high)}"
                    )

            elif trade["status"] == "OPEN":
                process_open_trade(trade, price)

        except Exception as exc:
            print(
                f"Manager error for {display_symbol(trade)} "
                f"({trade.get('signal_id', 'unknown')}): {exc}"
            )

        time.sleep(0.10)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_journal(journal)

    print("Closed Futures statistics:")
    print(json.dumps(calculate_stats(trades), ensure_ascii=False, indent=2))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
