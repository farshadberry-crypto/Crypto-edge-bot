
import os
import json
import time
import math
import requests
from datetime import datetime, timezone

# =========================================================
# CRYPTO EDGE FUTURES TRADE MANAGER v6.0
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

MIN_SCORE = 6
MIN_VOLUME_RATIO = 1.20
MIN_STOP_DISTANCE_PERCENT = 0.15
MAX_STOP_DISTANCE_PERCENT = 1.20
MAX_WAITING_AGE_HOURS = 6

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "CryptoEdge-Futures-TradeManager/6.0",
    "Accept": "application/json",
})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
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
    value = trade.get("leverage")
    if not finite_number(value):
        return None
    value = float(value)
    return value if value > 0 else None


def leverage_summary(trade):
    entry = number(trade.get("entry"))
    stop = number(trade.get("stop_loss"))
    leverage = get_leverage(trade)

    if entry <= 0 or stop <= 0 or leverage is None:
        return (
            "⚙️ Leverage: unknown in legacy record\n"
            "⚠️ Margin risk cannot be calculated reliably."
        )

    distance = abs(entry - stop) / entry * 100
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
    if key not in trade["messages_sent"]:
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


def validate_waiting_trade(trade):
    """
    Apply current entry rules only to waiting trades.
    Existing OPEN trades are preserved and managed to completion.
    """
    symbol = display_symbol(trade)

    if not is_futures_trade(trade):
        return f"{symbol}: not a Kraken linear Futures trade"

    contract = str(trade.get("exchange_symbol", "")).upper()
    if not contract.startswith(("PF_", "PI_")):
        return f"{symbol}: invalid Futures contract"

    direction = str(trade.get("direction", "")).upper()
    if direction not in ("LONG", "SHORT"):
        return f"{symbol}: invalid direction"

    entry = number(trade.get("entry"), -1)
    stop = number(trade.get("stop_loss"), -1)
    targets = trade.get("targets")

    if entry <= 0 or stop <= 0:
        return f"{symbol}: invalid entry or stop"

    if not isinstance(targets, list) or len(targets) < 3:
        return f"{symbol}: three targets are required"

    if not all(finite_number(target) for target in targets[:3]):
        return f"{symbol}: invalid target prices"

    targets = [float(target) for target in targets[:3]]

    if direction == "LONG":
        if not (stop < entry < targets[0] < targets[1] < targets[2]):
            return f"{symbol}: invalid LONG price ordering"
    else:
        if not (targets[2] < targets[1] < targets[0] < entry < stop):
            return f"{symbol}: invalid SHORT price ordering"

    score = number(trade.get("score"), -1)
    if score < MIN_SCORE:
        return f"{symbol}: legacy score {score:g} below {MIN_SCORE}"

    volume_ratio = number(trade.get("volume_ratio"), -1)
    if volume_ratio < MIN_VOLUME_RATIO:
        return (
            f"{symbol}: volume ratio {volume_ratio:g}x "
            f"below {MIN_VOLUME_RATIO:.2f}x"
        )

    leverage = get_leverage(trade)
    if leverage is None or abs(leverage - FIXED_LEVERAGE) > 0.0001:
        return f"{symbol}: leverage is missing or not 10x"

    stop_distance = abs(entry - stop) / entry * 100

    if not (
        MIN_STOP_DISTANCE_PERCENT
        <= stop_distance
        <= MAX_STOP_DISTANCE_PERCENT
    ):
        return (
            f"{symbol}: stop distance {stop_distance:.3f}% "
            "outside current limits"
        )

    return None


def cancel_waiting_trade(trade, reason):
    trade["status"] = "CANCELLED"
    trade["cancelled_at"] = now_utc()
    trade["cancel_reason"] = reason
    trade["remaining_fraction"] = 0.0

    print(
        f"CANCELLED waiting trade: {display_symbol(trade)} "
        f"{trade.get('direction')} | {reason}"
    )


def expire_waiting_trade(trade):
    created = parse_time(trade.get("created_at"))

    if created is None:
        return False

    age_seconds = time.time() - created
    return age_seconds > MAX_WAITING_AGE_HOURS * 3600


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

    if previous_check is None:
        return candles[-1:]

    return [
        candle for candle in candles
        if previous_check < candle["time"] + CANDLE_SECONDS
    ]


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
    total = number(trade.get("realized_pnl_percent"))
    leverage = get_leverage(trade)

    trade["remaining_fraction"] = 0.0
    trade["status"] = "CLOSED"
    trade["closed_at"] = now_utc()
    trade["result"] = (
        "WIN" if total > 0
        else "LOSS" if total < 0
        else "BREAKEVEN"
    )

    if leverage is not None:
        trade["estimated_margin_return_percent"] = total * leverage
    else:
        # Do not invent leverage for historical records.
        trade.pop("estimated_margin_return_percent", None)

    margin_return = (
        total * leverage if leverage is not None else None
    )

    return total, leverage, margin_return


def open_trade(trade, candle):
    trade["status"] = "OPEN"
    trade["entry_confirmed_at"] = now_utc()
    trade["confirmed_entry_price"] = float(trade["entry"])
    trade["entry_confirmation_method"] = (
        "closed_1m_candle_entry_zone_touch"
    )

    low, high = entry_zone(trade)
    symbol = display_symbol(trade)

    message = (
        "🟢 CRYPTO EDGE | FUTURES TRADE OPENED (PAPER)\n\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"🎯 Planned entry: {fmt(trade['entry'])}\n"
        f"📍 Entry zone: {fmt(low)} - {fmt(high)}\n"
        "🕯️ Entry zone touched on a closed 1m candle\n"
        f"🧮 Assumed paper fill: {fmt(trade['entry'])}\n"
        f"🛑 Stop loss: {fmt(trade['stop_loss'])}\n"
        f"💰 TP1: {fmt(trade['targets'][0])}\n"
        f"💰 TP2: {fmt(trade['targets'][1])}\n"
        f"💰 TP3: {fmt(trade['targets'][2])}\n\n"
        f"{leverage_summary(trade)}\n\n"
        "Estimated paper fill only, not a real execution. "
        "The intraminute price path is unknown."
    )

    notify_once(trade, "opened", message)


def close_remaining(trade, price, reason, candle_time=None):
    remaining = max(0.0, number(trade.get("remaining_fraction")))

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

    margin_text = (
        f"{margin_return:+.3f}%"
        if margin_return is not None else "unknown"
    )
    leverage_text = (
        f"{leverage:.1f}x" if leverage is not None else "unknown"
    )

    message = (
        "🏁 CRYPTO EDGE | FUTURES TRADE CLOSED (PAPER)\n\n"
        f"🪙 Coin: {symbol}\n"
        f"📈 Direction: {trade['direction']}\n"
        f"📌 Reason: {reason}\n"
        f"💵 Assumed exit price: {fmt(price)}\n"
        f"⚙️ Leverage: {leverage_text}\n"
        f"📊 Estimated underlying return: {total:+.3f}%\n"
        f"💹 Estimated margin return: {margin_text}\n"
        f"📋 Result: {trade['result']}\n\n"
        "Paper estimate before costs. No real order was placed."
    )

    notify_once(trade, "closed", message)


def process_open_candle(trade, candle):
    # Conservative OHLC assumption: stop takes priority if both
    # stop and targets were touched during the same candle.
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

    for index, raw_target in enumerate(targets[:3]):
        key = f"TP{index + 1}"

        if key in already_hit:
            continue

        target = float(raw_target)

        if not is_target_hit_in_candle(trade, candle, target):
            continue

        remaining = max(0.0, number(trade["remaining_fraction"]))
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

        accumulated = number(trade["realized_pnl_percent"])
        margin_return = (
            accumulated * leverage if leverage is not None else None
        )

        leverage_text = (
            f"{leverage:.1f}x" if leverage is not None else "unknown"
        )
        margin_text = (
            f"{margin_return:+.3f}%"
            if margin_return is not None else "unknown"
        )

        message = (
            f"💰 CRYPTO EDGE | FUTURES {key} HIT (PAPER)\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"🎯 Target price: {fmt(target)}\n"
            f"⚙️ Leverage: {leverage_text}\n"
            f"📤 Paper exit: {allocation * 100:.0f}% of original position\n"
            f"📦 Remaining position: "
            f"{trade['remaining_fraction'] * 100:.0f}%\n"
            f"📊 Underlying move at target: {move:+.3f}%\n"
            f"📈 Accumulated weighted underlying return: {accumulated:+.3f}%\n"
            f"💹 Estimated margin return: {margin_text}\n\n"
            "Paper tracking only. Trading costs are excluded."
        )

        notify_once(trade, key, message)
        already_hit.add(key)

    if trade["remaining_fraction"] <= 0.000001:
        total, leverage, margin_return = finalize_trade(trade)

        leverage_text = (
            f"{leverage:.1f}x" if leverage is not None else "unknown"
        )
        margin_text = (
            f"{margin_return:+.3f}%"
            if margin_return is not None else "unknown"
        )

        message = (
            "🏁 CRYPTO EDGE | ALL FUTURES TARGETS COMPLETED (PAPER)\n\n"
            f"🪙 Coin: {symbol}\n"
            f"📈 Direction: {trade['direction']}\n"
            f"⚙️ Leverage: {leverage_text}\n"
            f"📊 Estimated underlying return: {total:+.3f}%\n"
            f"💹 Estimated return on allocated margin: {margin_text}\n"
            f"📋 Result: {trade['result']}\n\n"
            "Paper estimate before trading costs. No real order was placed."
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
        return number(trade.get("realized_pnl_percent"))

    wins = sum(1 for trade in closed if realized(trade) > 0)
    losses = sum(1 for trade in closed if realized(trade) < 0)
    total = len(closed)

    gross_profit = sum(max(0.0, realized(t)) for t in closed)
    gross_loss = abs(sum(min(0.0, realized(t)) for t in closed))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    known_margin_returns = []
    unknown_leverage_count = 0

    for trade in closed:
        leverage = get_leverage(trade)
        if leverage is None:
            unknown_leverage_count += 1
            continue

        # Recalculate consistently from realized underlying return and
        # the recorded leverage. This is still an estimate, not account PnL.
        known_margin_returns.append(realized(trade) * leverage)

    return {
        "closed_futures": total,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(wins / total * 100, 2) if total else 0.0,
        "total_underlying_return_percent": round(
            sum(realized(t) for t in closed), 3
        ),
        "estimated_margin_return_known_leverage_only_percent": round(
            sum(known_margin_returns), 3
        ),
        "closed_trades_with_unknown_leverage": unknown_leverage_count,
        "profit_factor_underlying_returns": (
            round(profit_factor, 3)
            if profit_factor is not None else None
        ),
    }


def main():
    print("Crypto Edge Futures Trade Manager v6.0:", now_utc())
    print("Closed 1m candles | Paper tracking only")
    print(f"New waiting-trade rules: score >= {MIN_SCORE}, "
          f"volume >= {MIN_VOLUME_RATIO:.2f}x, leverage = 10x")
    print(f"Waiting-entry expiry: {MAX_WAITING_AGE_HOURS} hours")

    journal = load_journal()
    trades = journal["trades"]

    active_trades = [
        trade for trade in trades
        if isinstance(trade, dict)
        and is_futures_trade(trade)
        and trade.get("status", "WAITING_ENTRY")
        in ("WAITING_ENTRY", "OPEN")
    ]

    for trade in active_trades:
        initialize_trade(trade)

        # Only waiting trades are subject to the new-signal filters.
        # Existing OPEN trades are preserved to completion.
        if trade["status"] == "WAITING_ENTRY":
            invalid_reason = validate_waiting_trade(trade)

            if invalid_reason:
                cancel_waiting_trade(trade, invalid_reason)
                continue

            if expire_waiting_trade(trade):
                cancel_waiting_trade(
                    trade,
                    f"Entry not reached within {MAX_WAITING_AGE_HOURS} hours",
                )
                continue

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
                        # Do not process exits on the entry candle.
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
            # Keep the previous checkpoint after errors so the next run
            # can retry candles not successfully processed.

        time.sleep(0.15)

    journal["trades"] = trades
    journal["updated_at"] = now_utc()
    save_journal(journal)

    print("Closed Futures statistics:")
    print(json.dumps(calculate_stats(trades), ensure_ascii=False, indent=2))
    print("Manager scan completed.")


if __name__ == "__main__":
    main()
