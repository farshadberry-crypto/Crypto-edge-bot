
import os
import json
import requests
import xml.etree.ElementTree as ET

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"
RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
STATE_FILE = ".crypto_edge_seen.json"
MAX_SEEN = 500

telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# Read saved news history
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as file:
        seen_ids = set(json.load(file).get("seen_ids", []))
    first_run = False
else:
    seen_ids = set()
    first_run = True

# Download news
response = requests.get(RSS_URL, timeout=20)
response.raise_for_status()
root = ET.fromstring(response.content)

items = root.findall(".//item")
new_items = []
current_ids = []

for item in items:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    guid = (item.findtext("guid") or link or title).strip()

    if not title or not guid:
        continue

    current_ids.append(guid)

    if guid not in seen_ids:
        new_items.append((guid, title))

# On the first run, save existing stories without posting them
if first_run:
    seen_ids.update(current_ids[:MAX_SEEN])
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump({"seen_ids": list(seen_ids)}, file, ensure_ascii=False, indent=2)
    print("News history initialized. No old stories sent.")
    raise SystemExit(0)

# Post new stories from oldest to newest
sent_ids = []

for guid, title in reversed(new_items):
    message = f"""⚡ CRYPTO EDGE
━━━━━━━━━━━━━━━━━━

🚨 CRYPTO NEWS

📰 {title}

━━━━━━━━━━━━━━━━━━

📊 THE MARKET NEVER SLEEPS
🎯 STAY AHEAD
⚡ DATA OVER HYPE"""

    result = requests.post(
        telegram_url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    result.raise_for_status()
    sent_ids.append(guid)

# Save only after successful sends
seen_ids.update(sent_ids)
seen_ids = set(list(seen_ids)[-MAX_SEEN:])

with open(STATE_FILE, "w", encoding="utf-8") as file:
    json.dump({"seen_ids": list(seen_ids)}, file, ensure_ascii=False, indent=2)

print(f"{len(sent_ids)} new stories sent.")
