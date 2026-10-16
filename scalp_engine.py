import os, json, time, requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as patches
from datetime import datetime, timezone

# CRYPTO EDGE FUTURES SCALP ENGINE v4.1 DIAGNOSTIC
# Kraken linear Futures | 5m entry | 15m trend | paper signals only
FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"
FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"
STATE_FILE = "scalp_signals.json"
CHART_DIR = "signal_charts"
SCAN_LIMIT = 12
MAX_SIGNALS_PER_RUN = 2
FIXED_LEVERAGE = 10.0
VOLUME_RATIO_MIN = 1.20
MIN_SCORE = 6
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.25
MIN_STOP_DISTANCE_PERCENT = 0.15
MAX_STOP_DISTANCE_PERCENT = 1.20
MIN_TP1_RR = 1.30
TP2_RR = 1.80
TP3_RR = 2.50
MAX_CANDLE_COUNT = 60
MAX_STORED_SIGNALS = 1000
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoEdge-FuturesScalpEngine/4.1", "Accept": "application/json"})


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, params=None):
    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response format.")
    return payload


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("sent", [])
            data.setdefault("signals", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"sent": [], "signals": []}


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, STATE_FILE)


def display_symbol(contract):
    value = str(contract or "").strip()
    if value.startswith(("PF_", "PI_")):
        value = value[3:]
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "BTC"):
        if value.endswith(quote) and len(value) > len(quote):
            base = value[:-len(quote)]
            if base == "XBT":
                base = "BTC"
            return f"{base}/{quote}"
    return value


def telegram_send_photo(path, caption):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "@cryptoedgeAlerts")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing.")
        return False
    try:
        with open(path, "rb") as photo:
            response = SESSION.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": photo}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            print("Telegram photo rejected:", payload)
