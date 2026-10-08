```python
import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

url = "https://api.coingecko.com/api/v3/simple/price"

params = {
    "ids": "bitcoin,ethereum",
    "vs_currencies": "usd",
    "include_24hr_change": "true"
}

response = requests.get(url, params=params, timeout=15)
response.raise_for_status()
data = response.json()

btc = data["bitcoin"]
eth = data["ethereum"]

def movement(change):
    if change > 0:
        return f"🟢 🚀 +{change:.2f}%"
    elif change < 0:
        return f"🔴 📉 {change:.2f}%"
    return "🟡 ➖ 0.00%"

message = f"""⚡ CRYPTO EDGE | MARKET PULSE

🌐 LIVE MARKET SNAPSHOT

🟠 ₿ BITCOIN · BTC
💰 ${btc['usd']:,.2f}
{movement(btc['usd_24h_change'])} · 24H

🔷 💎 ETHEREUM · ETH
💰 ${eth['usd']:,.2f}
{movement(eth['usd_24h_change'])} · 24H

━━━━━━━━━━━━━━━━

📊 TRACK THE MARKET.
🎯 STAY AHEAD.

⚡ DATA OVER HYPE.
"""

telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

telegram_response = requests.post(
    telegram_url,
    data={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=15
)

telegram_response.raise_for_status()

print("Market update sent successfully.")
```
