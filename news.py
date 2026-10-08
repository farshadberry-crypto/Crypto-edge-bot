import os
import requests
import xml.etree.ElementTree as ET

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"
RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"

response = requests.get(RSS_URL, timeout=15)
response.raise_for_status()
root = ET.fromstring(response.content)

telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

items = root.findall(".//item")[:5]
titles = [item.findtext("title", default="").strip() for item in items]
titles = [title for title in titles if title]

messages = [
f"""⚡ CRYPTO EDGE
━━━━━━━━━━━━━━━━━━

🚨 BREAKING CRYPTO NEWS

📰 {title}

━━━━━━━━━━━━━━━━━━

📊 THE MARKET NEVER SLEEPS
🎯 STAY AHEAD
⚡ DATA OVER HYPE"""
for title in titles
]

results = [
requests.post(
telegram_url,
data={"chat_id": CHAT_ID, "text": message},
timeout=15
)
for message in messages
]

[ result.raise_for_status() for result in results ]

print(f"{len(results)} news posts sent successfully.")
