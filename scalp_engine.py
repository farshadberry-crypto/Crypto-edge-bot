    fig, (ax, volume_ax) = plt.subplots(2, 1, figsize=(15, 9), sharex=True,
                                       gridspec_kw={"height_ratios": [4, 1]})
    for i, c in enumerate(candles):
        x = x_values[i]
        color = "#00b894" if c["close"] >= c["open"] else "#e74c3c"
        ax.vlines(x, c["low"], c["high"], color=color, linewidth=1.2, zorder=2)
        body_height = abs(c["close"] - c["open"]) or max(abs(c["close"]) * 0.00002, 1e-12)
        ax.add_patch(patches.Rectangle((x-candle_width/2, min(c["open"], c["close"])), candle_width,
                                      body_height, facecolor=color, edgecolor=color, linewidth=0.8, zorder=3))
        volume_ax.bar(x, c["volume"], width=candle_width, color=color, alpha=0.8)
    ax.axhline(signal["entry"], color="#2980b9", linewidth=1.8, label=f"ENTRY {fmt(signal['entry'])}")
    ax.axhline(signal["stop_loss"], color="#e74c3c", linestyle="--", linewidth=1.8,
                label=f"STOP {fmt(signal['stop_loss'])}")
    for i, target in enumerate(signal["targets"]):
        ax.axhline(target, color=["#27ae60", "#16a085", "#2ecc71"][i], linestyle=":", linewidth=1.6,
                   label=f"TP{i+1} {fmt(target)}")
    ax.set_title(f"CRYPTO EDGE | {signal['symbol']} | {signal['direction']} | 10x | 5m ENTRY / 15m TREND",
                 fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel("Price"); volume_ax.set_ylabel("Volume"); volume_ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.22); volume_ax.grid(True, axis="y", alpha=0.18)
    ax.legend(loc="upper left", fontsize=8, ncol=2, framealpha=0.9)
    volume_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
    fig.autofmt_xdate(); fig.tight_layout()
    safe_symbol = signal["symbol"].replace("/", "_").replace(":", "_")
    path = os.path.join(CHART_DIR, f"{safe_symbol}_{signal['candle_time']}.png")
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def build_caption(signal):
    t = signal["targets"]
    evidence = "\n".join(f"• {item}" for item in signal["evidence"])
    return ("🚨 CRYPTO EDGE | FUTURES SCALP\n\n"
            f"🪙 Pair: {signal['symbol']}\n📈 Direction: {signal['direction']}\n"
            "⏱ Entry: 5m candles | Trend: 15m candles\n\n"
            f"🎯 Reference entry: {fmt(signal['entry'])}\n🛑 Stop loss: {fmt(signal['stop_loss'])}\n"
            f"💰 TP1 (1.3R): {fmt(t[0])}\n💰 TP2 (1.8R): {fmt(t[1])}\n💰 TP3 (2.5R): {fmt(t[2])}\n\n"
            f"⚙️ Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x\n"
            f"📏 Price distance to SL: {signal['stop_distance_percent']}%\n"
            f"🧮 Approx. allocated-margin loss at SL: {signal['estimated_margin_loss_percent']}%\n"
            "Excludes fees, slippage, funding and liquidation effects.\n\n"
            f"📊 Signal score: {signal['score']}\n📉 5m RSI: {signal['rsi']}\n"
            f"📦 Closed-candle volume ratio: {signal['volume_ratio']}x\n"
            f"🧱 Support reference: {fmt(signal['support_reference'])}\n"
            f"🧱 Resistance reference: {fmt(signal['resistance_reference'])}\n\n"
            f"🔎 Evidence:\n{evidence}\n\n🟡 STATUS: WAITING FOR ENTRY\n"
            "Entry is a reference price, not a confirmed fill.\nPaper tracking only. No real order has been placed.")


def scan_market(instrument):
    contract = instrument["contract"]
    try:
        candles_5m = fetch_futures_candles(contract, "5m")
        time.sleep(0.25)
        candles_15m = fetch_futures_candles(contract, "15m")
        signal, reason = evaluate_signal(contract, candles_5m, candles_15m)
        print(f"DIAGNOSTIC {instrument['symbol']} ({contract}): {reason}")
        return signal
    except Exception as exc:
        print(f"DIAGNOSTIC {instrument['symbol']} ({contract}): DATA/API ERROR: {exc}")
        return None


def main():
    print("Starting Crypto Edge Kraken Futures Scalp Engine v4.1 DIAGNOSTIC")
    print("UTC:", now_utc())
    print("Market mode: FUTURES ONLY")
    print(f"Fixed suggested leverage: {FIXED_LEVERAGE:.0f}x")
    print("Entry timeframe: 5m | Trend timeframe: 15m")
    print("Chart: real OHLC candlesticks + volume")
    print("Diagnostic mode: rejection reasons enabled; thresholds unchanged.")
    state = load_state()
    sent_ids = set(state.get("sent", []))
    saved_signals = state.get("signals", [])
    if not isinstance(saved_signals, list): saved_signals = []
    universe = fetch_futures_universe()
    print("Eligible Futures contracts:", len(universe))
    attempted = sent_count = 0
    for instrument in universe:
        attempted += 1
        signal = scan_market(instrument)
        if signal is None:
            time.sleep(0.25)
            continue
        if signal["signal_id"] in sent_ids:
            print(f"Already sent; skipping duplicate: {signal['signal_id']}")
            continue
        chart_path = create_chart(signal)
        if telegram_send_photo(chart_path, build_caption(signal)):
            sent_ids.add(signal["signal_id"])
            saved_signals.append({k: v for k, v in signal.items() if k != "candles"})
            sent_count += 1
            print(f"Signal sent: {signal['symbol']} {signal['direction']} score={signal['score']} "
                  f"leverage={signal['leverage']}x SL distance={signal['stop_distance_percent']}%")
        time.sleep(0.35)
        if sent_count >= MAX_SIGNALS_PER_RUN: break
    state["sent"] = list(sent_ids)[-5000:]
    state["signals"] = saved_signals[-MAX_STORED_SIGNALS:]
    state["updated_at"] = now_utc()
    save_state(state)
    print("Markets attempted:", attempted)
    print("Signals sent:", sent_count)
    print("Kraken Futures scan completed.")


if __name__ == "__main__":
    main()
