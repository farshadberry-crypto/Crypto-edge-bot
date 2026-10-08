import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

# Get BTC and ETH prices
url = "https://api.coingecko.com/api/v3/simple/price"
params = {
    "ids": "bitcoin,ethereum",
    "vs_currencies": "usd",
    "include_24hr_change": "true"
}

data = requests.get(url, params=params, timeout=15).json()

btc = data["bitcoin"]
eth = data["ethereum"]

message = f"""📊 CRYPTO MARKET UPDATE

₿ BTC: ${btc["usd"]:,.0f}
24H: {btc["usd_24h_change"]:+.2f}%

Ξ ETH: ${eth["usd"]:,.0f}
24H: {eth["usd_24h_change"]:+.2f}%

━━━━━━━━━━━━━━
Crypto Edge
Market data • Analysis • Free Signals
"""

telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

response = requests.post(
    telegram_url,
    data={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=15
)

response.raise_for_status()

print("Message sent successfully.")
