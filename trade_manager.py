
import os
import json
import time
import math
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE FUTURES TRADE MANAGER
# Kraken linear futures | Closed 1m candles
# Paper tracking only. No real orders are placed.
# =========================================================

CHARTS_URL = "https://futures.kraken.com/api/charts/v1"
JOURNAL_FILE = "trade_journal.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")

FIXED_LEVERAGE = 10.0
ENTRY_ZONE_PERCENT = 0.15
TP_ALLOCATIONS = [0.40, 0.30, 0.30]
CANDLE_SECONDS = 60
MAX_CANDLES = 120

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-Futures-TradeManager/5.0",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


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
    temporary = JOURNAL_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(journal, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, JOURNAL_FILE)


def is_futures_trade(trade):
    return (
        str(trade.get("exchange", "")).lower() == "kraken"
        and str(trade.get("market_type", "")).lower() == "futures"
        and str(trade.get("category", "")).lower() == "linear"
        and str(trade.get("market", "")).upper() == "FUTURES"
    )


def display_symbol(trade):
    raw = str(
        trade.get("symbol")
        or trade.get("exchange_symbol")
        or "UNKNOWN"
    ).upper()

    if raw.startswith(("PF_", "PI_")):
        raw = raw[3:]

    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "BTC"):
        if raw.endswith(quote) and len(raw) > len(quote):
            base = raw[:-len(quote)]
            if base == "XBT":
                base = "BTC"
            return f"{base}/{quote}"

    return raw


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


def get_leverage(trade):
    value = trade.get("leverage", FIXED_LEVERAGE)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return FIXED_LEVERAGE
    return value if math.isfinite(value) and value > 0 else FIXED_LEVERAGE


def leverage_summary(trade):
    entry = float(trade["entry"])
    stop = float(trade["stop_loss"])
    leverage = get_leverage(trade)
    distance = abs(entry - stop) / entry * 100 if entry > 0 else 0
    margin_risk = distance * leverage

    return (
        f"⚙️ Leverage: {leverage:.1f}x\n"
        f"⚠️ Estimated margin loss at SL: {margin_risk:.2f}%\n"
        "Excludes fees, slippage, funding and liquidation effects."
    )


def send_telegram(message):
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN missing. Message not sent:")
        print(message)
        return False

    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            print("Telegram rejected message:", payload)
            return False

        return True
    except (requests.RequestException, ValueError) as exc:
        print("Telegram delivery failed:", exc)
        return False


def notify_once(trade, key, message):
    if key in trade["messages_sent"]:
        return
    if send_telegram(message):
        trade["messages_sent"].append(key)


def initialize_trade(trade):
    defaults = {
        "status": "WAITING_ENTRY",
        "created_at": now_utc(),
        "entry_confirmed_at": None,
        "confirmed_entry_price": None,
        "closed_at": None,
        "last_checked_at": None,
        "targets_hit": [],
        "exits": [],
        "remaining_fraction": 1.0,
        "realized_pnl_percent": 0.0,
        "messages_sent": [],
        "result": None,
    }

    for key, value in defaults.items():
        if key not in trade:
            trade[key] = value

    for key in ("targets_hit", "exits", "messages_sent"):
        if not isinstance(trade.get(key), list):
            trade[key] = []

    if not finite_number(trade.get("remaining_fraction")):
        trade["remaining_fraction"] = 1.0

    if not finite_number(trade.get("realized_pnl_percent")):
        trade["realized_pnl_percent"] = 0.0

    return trade


def fetch_closed_candles(trade):
    symbol = str(trade.get("exchange_symbol", "")).upper()

    if not symbol.startswith(("PF_", "PI_")):
        raise RuntimeError(f"Invalid Futures contract: {symbol}")

    response = SESSION.get(
        f"{CHARTS_URL}/trade/{symbol}/1m",
        params={"count": MAX_CANDLES},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    raw_candles = payload.get("candles", [])
    if not isinstance(raw_candles, list):
        raise RuntimeError("Unexpected candle API response.")

    now = time.time()
    unique = {}

    for row in raw_candles:
        try:
            stamp = int(row["time"])
            if stamp > 10_000_000_000:
                stamp //= 1000

            candle = {
                "time": stamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }

            if not all(
                math.isfinite(candle[key])
                for key in ("open", "high", "low", "close")
            ):
                continue

            if min(
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
            ) <= 0:
                continue

            if candle["high"] < candle["low"]:
                continue

            # Only fully closed candles are eligible.
            if stamp + CANDLE_SECONDS > now:
                continue

            unique[stamp] = candle

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles = sorted(unique.values(), key=lambda item: item["time"])

    if not candles:
        raise RuntimeError(f"No closed 1m candles available for {symbol}")

    return candles


def candles_to_process(trade, candles):
    previous_check = parse_time(trade.get("last_checked_at"))

    # On first use, process only the newest closed candle.
    # Do not invent the path price took before tracking began.
    if previous_check is None:
        return candles[-1:]

    # Include candles that closed after the previous successful check.
    # Candle timestamps represent their opening time.
    pending = [
        candle for candle in candles
        if previous_check < candle["time"] + CANDLE_SECONDS
    ]

    return pending


def entry_zone(trade):
    entry = float(trade["entry"])
    tolerance = entry * ENTRY_ZONE_PERCENT / 100
    return entry - tolerance, entry + tolerance


def candle_touches_entry(trade, candle):
    low, high = entry_zone(trade)
    return candle["low"] <= high and candle["high"] >= low


def pnl_percent(trade, exit_price):
    entry = float(trade["entry"])
    if entry <= 0:
        return 0.0

    if trade["direction"] == "LONG":
        return (exit_price - entry) / entry * 100

    return (entry - exit_price) / entry * 100


def is_stop_hit_in_candle(trade, candle):
    stop = float(trade["stop_loss"])

    if trade["direction"] == "LONG":
        return candle["low"] <= stop

    return candle["high"] >= stop


def is_target_hit_in_candle(trade, candle, target):
    if trade["direction"] == "LONG":
        return candle["high"] >= target

    return candle["low"] <= target


def finalize_trade(trade):
    total = float(trade.get("realized_pnl_percent", 0.0))
    leverage = get_leverage(trade)

    trade["remaining_fraction"] = 0.0
    trade["status"] = "CLOSED"
    trade["closed_at"] = now_utc()
    trade["estimated_margin_return_percent"] = total * leverage
    trade["result"] = (
        "WIN" if total > 0
        else "LOSS" if total < 0
        else "BREAKEVEN"
    )

    return total, leverage, total * leverage


def open_trade(trade, candle):
    trade["status"] = "OPEN"
    trade["entry_confirmed_at"] = now_utc()

    # Assumed fill: planned entry, not a verified exchange execution.
    trade["confirmed_entry_price"] = float(trade["entry"])
    trade["entry_confirmation_method"] = "closed_1m_candle_entry_zone_touch"

    low, high = entry_zone(trade)
    symbol = display_symbol(trade)

    message = (
        "🟢 CRYPTO EDGE | FUTURES TRADE OPENED (PAPER)\n\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"🎯 Planned entry: {fmt(trade['entry'])}\n"
        f"📍 Entry zone: {fmt(low)} - {fmt(high)}\n"
        f"🕯️ Entry zone touched on a closed 1m candle\n"
        f"🧮 Assumed paper fill: {fmt(trade['entry'])}\n"
        f"🛑 Stop loss: {fmt(trade['stop_loss'])}\n"
        f"💰 TP1: {fmt(trade['targets'][0])}\n"
        f"💰 TP2: {fmt(trade['targets'][1])}\n"
        f"💰 TP3: {fmt(trade['targets'][2])}\n\n"
        f"{leverage_summary(trade)}\n\n"
        "The candle's intraminute price path is unknown. "
        "This is an estimated paper fill, not a real execution."
    )

    notify_once(trade, "opened", message)


def close_remaining(trade, price, reason, candle_time=None):
    remaining = max(0.0, float(trade["remaining_fraction"]))

    if remaining > 0:
        move = pnl_percent(trade, price)
        trade["realized_pnl_percent"] += move * remaining

        trade["exits"].append({
            "time": now_utc(),
            "candle_time": candle_time,
            "price": price,
            "fraction": remaining,
            "pnl_percent": move,
            "reason": reason,
        })

        trade["remaining_fraction"] = 0.0

    total, leverage, margin_return = finalize_trade(trade)
    symbol = display_symbol(trade)

    message = (
        "🏁 CRYPTO EDGE | FUTURES TRADE CLOSED (PAPER)\n\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"📌 Reason: {reason}\n"
        f"💵 Assumed exit price: {fmt(price)}\n"
        f"⚙️ Leverage: {leverage:.1f}x\n"
        f"📊 Estimated underlying return: {total:+.3f}%\n"
        f"💹 Estimated return on allocated margin: {margin_return:+.3f}%\n"
        f"📋 Result: {trade['result']}\n\n"
        "Paper estimate before fees, slippage, funding and liquidation "
        "effects. Not an actual account return."
    )

    notify_once(trade, "closed", message)


def process_open_candle(trade, candle):
    # Conservative rule: if the same 1m candle touched SL and any TP,
    # assume SL happened first because OHLC cannot prove the sequence.
    if is_stop_hit_in_candle(trade, candle):
        close_remaining(
            trade,
            float(trade["stop_loss"]),
            "STOP LOSS (conservative candle rule)",
            candle["time"],
        )
        return

    targets = trade["targets"]
    already_hit = set(trade["targets_hit"])
    leverage = get_leverage(trade)
    symbol = display_symbol(trade)

    # If several targets are reached within one candle and SL is not
    # reached, process targets from nearest to farthest.
    for index, raw_target in enumerate(targets):
        key = f"TP{index + 1}"

        if key in already_hit:
            continue

        target = float(raw_target)

        if not is_target_hit_in_candle(trade, candle, target):
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
            "candle_time": candle["time"],
            "price": target,
            "fraction": allocation,
            "pnl_percent": move,
            "reason": key,
        })

        accumulated = float(trade["realized_pnl_percent"])
        margin_return = accumulated * leverage

        message = (
            f"💰 CRYPTO EDGE | FUTURES {key} HIT (PAPER)\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"🎯 Target price: {fmt(target)}\n"
            f"⚙️ Leverage: {leverage:.1f}x\n"
            f"📤 Paper exit: {allocation * 100:.0f}% of original position\n"
            f"📦 Remaining position: "
            f"{trade['remaining_fraction'] * 100:.0f}%\n"
            f"📊 Underlying move at target: {move:+.3f}%\n"
            f"📈 Accumulated weighted underlying return: {accumulated:+.3f}%\n"
            f"💹 Estimated margin return: {margin_return:+.3f}%\n\n"
            "Estimated paper tracking only. Fees, slippage and funding "
            "are excluded."
        )

        notify_once(trade, key, message)
        already_hit.add(key)

    if trade["remaining_fraction"] <= 0.000001:
        total, leverage, margin_return = finalize_trade(trade)

        message = (
            "🏁 CRYPTO EDGE | ALL FUTURES TARGETS COMPLETED (PAPER)\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"⚙️ Leverage: {leverage:.1f}x\n"
            f"📊 Estimated underlying return: {total:+.3f}%\n"
            f"💹 Estimated return on allocated margin: {margin_return:+.3f}%\n"
            f"📋 Result: {trade['result']}\n\n"
            "Paper estimate before fees, slippage and funding. "
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

    gross_profit = sum(max(0.0, realized(t)) for t in closed)
    gross_loss = abs(sum(min(0.0, realized(t)) for t in closed))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    margin_returns = []
    for trade in closed:
        try:
            stored = trade.get("estimated_margin_return_percent")
            value = (
                float(stored) if stored is not None
                else realized(trade) * get_leverage(trade)
            )
            if math.isfinite(value):
                margin_returns.append(value)
        except (TypeError, ValueError):
            continue

    return {
        "closed_futures": total,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(wins / total * 100, 2) if total else 0.0,
        "total_underlying_return_percent": round(
            sum(realized(t) for t in closed), 3
        ),
        "estimated_total_margin_return_percent": round(
            sum(margin_returns), 3
        ),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
    }


def main():
    print("Crypto Edge Futures Trade Manager v5.0:", now_utc())
    print("Closed 1m candles | Paper tracking only")

    journal = load_journal()
    trades = journal["trades"]

    active_trades = [
        trade for trade in trades
        if isinstance(trade, dict)
        and is_futures_trade(trade)
        and trade.get("status", "WAITING_ENTRY") in ("WAITING_ENTRY", "OPEN")
    ]

    for trade in active_trades:
        initialize_trade(trade)

        try:
            candles = fetch_closed_candles(trade)
            pending = candles_to_process(trade, candles)

            if not pending:
                print(
                    f"No new closed candles: {display_symbol(trade)} "
                    f"{trade.get('direction')}"
                )
                trade["last_checked_at"] = now_utc()
                continue

            for candle in pending:
                if trade["status"] not in ("WAITING_ENTRY", "OPEN"):
                    break

                if trade["status"] == "WAITING_ENTRY":
                    if candle_touches_entry(trade, candle):
                        open_trade(trade, candle)
                        # Do not process exits in the entry candle:
                        # the order of entry, stop and target touches is unknown.
                        continue

                    low, high = entry_zone(trade)
                    print(
                        f"Waiting: {display_symbol(trade)} "
                        f"{trade.get('direction')} "
                        f"candle={datetime.fromtimestamp(candle['time'], timezone.utc).isoformat()} "
                        f"range={fmt(candle['low'])}-{fmt(candle['high'])} "
                        f"zone={fmt(low)}-{fmt(high)}"
                    )

                elif trade["status"] == "OPEN":
                    process_open_candle(trade, candle)

            trade["last_checked_at"] = now_utc()

        except Exception as exc:
            print(
                f"Manager error for {display_symbol(trade)} "
                f"({trade.get('signal_id', 'unknown')}): {exc}"
            )
            # Do not advance last_checked_at on API or processing errors.
            # The next run can retry the unprocessed candles.

        time.sleep(0.15)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_journal(journal)

    print("Closed Futures statistics:")
    print(json.dumps(calculate_stats(trades), ensure_ascii=False, indent=2))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
