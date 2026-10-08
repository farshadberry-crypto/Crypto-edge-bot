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

count = 0

for item in root.findall(".//item")[:5]:
    title = item.findtext("title")

    if not title:
        continue

    message = f"""📰 CRYPTO NEWS

{title}

— Crypto Edge"""

    telegram_response = requests.post(
        telegram_url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=15
    )

    telegram_response.raise_for_status()

    count += 1

print(f"{count} news posts sent successfully.")
