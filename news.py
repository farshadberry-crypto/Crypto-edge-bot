import os
import requests
import xml.etree.ElementTree as ET

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"

response = requests.get(RSS_URL, timeout=15)
response.raise_for_status()

root = ET.fromstring(response.content)

news_items = []

for item in root.findall(".//item")[:5]:
    title = item.findtext("title")
    link = item.findtext("link")

    if title and link:
        news_items.append(f"📰 {title}\n{link}")

if news_items:
    message = "🔥 CRYPTO NEWS\n\n" + "\n\n".join(news_items)
else:
    message = "⚠️ No fresh crypto news found."

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

print("News sent successfully.")
