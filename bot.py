import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

response = requests.get(
"https://api.coingecko.com/api/v3/simple/price",
params={
"ids": "bitcoin,ethereum",
"vs_currencies": "usd",
"include_24hr_change": "true"
},
timeout=20
)
response.raise_for_status()
data = response.json()

btc = data["bitcoin"]
eth = data["ethereum"]

btc_change = btc["usd_24h_change"]
eth_change = eth["usd_24h_change"]

btc_movement = (
f"🟢 🚀 +{btc_change:.2f}%" if btc_change > 0
else f"🔴 📉 {btc_change:.2f}%" if btc_change < 0
else "🟡 ➖ 0.00%"
)

eth_movement = (
f"🟢 🚀 +{eth_change:.2f}%" if eth_change > 0
else f"🔴 📉 {eth_change:.2f}%" if eth_change < 0
else "🟡 ➖ 0.00%"
)

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
data={"chat_id": CHAT_ID, "text": message},
timeout=20
)
telegram_response.raise_for_status()

print("Market update sent successfully.")
