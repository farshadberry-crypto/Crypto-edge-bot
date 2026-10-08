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

btc_change = btc["usd_24h_change"]
eth_change = eth["usd_24h_change"]

if btc_change > 0:
btc_movement = f"🟢 🚀 +{btc_change:.2f}%"
elif btc_change < 0:
btc_movement = f"🔴 📉 {btc_change:.2f}%"
else:
btc_movement = "🟡 ➖ 0.00%"

if eth_change > 0:
eth_movement = f"🟢 🚀 +{eth_change:.2f}%"
elif eth_change < 0:
eth_movement = f"🔴 📉 {eth_change:.2f}%"
else:
eth_movement = "🟡 ➖ 0.00%"

message = f"""⚡ CRYPTO EDGE | MARKET PULSE

🌐 LIVE MARKET SNAPSHOT

🟠 ₿ BITCOIN · BTC
💰 ${btc['usd']:,.2f}
{btc_movement} · 24H

🔷 💎 ETHEREUM · ETH
💰 ${eth['usd']:,.2f}
{eth_movement} · 24H

━━━━━━━━━━━━━━━━

📊 TRACK THE MARKET
🎯 STAY AHEAD

⚡ DATA OVER HYPE
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
