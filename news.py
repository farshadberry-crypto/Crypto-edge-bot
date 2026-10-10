
import os
import json
import re
import time
import hashlib
import requests
import xml.etree.ElementTree as ET

from html import unescape


# =========================
# CONFIGURATION
# =========================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "@cryptoedgeAlerts"

STATE_FILE = ".crypto_edge_seen.json"
MAX_SEEN = 1000
MAX_NEWS_PER_RUN = 10

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# Only two selected news sources
RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 CryptoEdgeNewsBot/1.0",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

session = requests.Session()
session.headers.update(HEADERS)


# =========================
# HELPERS
# =========================

def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(title):
    title = clean_text(title).lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return title.strip()


def make_id(link, title):
    identity = (
        link.strip().rstrip("/").lower()
        if link
        else normalize_title(title)
    )
    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


def load_history():
    if not os.path.exists(STATE_FILE):
        return set(), True

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return set(data.get("seen_ids", [])), False

    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Could not read news history: {error}"
        )


def save_history(seen_ids):
    temporary_file = STATE_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(
            {"seen_ids": list(seen_ids)[-MAX_SEEN:]},
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temporary_file, STATE_FILE)


# =========================
# FETCH NEWS
# =========================

def fetch_feed(source, feed_url):
    response = session.get(feed_url, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    items = root.findall(".//item")

    # Support Atom feeds too
    if not items:
        items = root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )

    stories = []

    for item in items:
        title = clean_text(
            item.findtext("title")
            or item.findtext(
                "{http://www.w3.org/2005/Atom}title"
            )
        )

        link = (
            item.findtext("link")
            or item.findtext(
                "{http://www.w3.org/2005/Atom}link"
            )
            or ""
        ).strip()

        if not link:
            atom_link = item.find(
                "{http://www.w3.org/2005/Atom}link"
            )
            if atom_link is not None:
                link = atom_link.attrib.get("href", "").strip()

        if not title:
            continue

        stories.append({
            "id": make_id(link, title),
            "title": title,
            "normalized_title": normalize_title(title),
        })

    print(f"{source}: {len(stories)} stories fetched")
    return stories


# =========================
# TELEGRAM
# =========================

def send_news(story):
    # Show only the headline, never the source or article URL.
    message = (
        "⚡ CRYPTO EDGE\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🚨 CRYPTO NEWS\n\n"
        f"📰 {story['title']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 STAY AHEAD\n"
        "⚡ DATA OVER HYPE"
    )

    for attempt in range(5):
        response = session.post(
            TELEGRAM_URL,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )

        if response.status_code == 429:
            try:
                retry_after = int(
                    response.json()
                    .get("parameters", {})
                    .get("retry_after", 5)
                )
            except (ValueError, TypeError):
                retry_after = 5

            if attempt < 4:
                wait_seconds = max(retry_after, 1) + 1
                print(
                    f"Telegram rate limit. "
                    f"Retrying in {wait_seconds} seconds..."
                )
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()

        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(
                "Telegram rejected the news message."
            )

        # Small delay between successful messages
        time.sleep(2)
        return

    raise RuntimeError("Could not send news after retries.")


# =========================
# MAIN
# =========================

def main():
    print("Starting Crypto Edge News...")

    seen_ids, first_run = load_history()

    all_stories = []
    successful_feeds = 0

    for source, feed_url in RSS_FEEDS:
        try:
            stories = fetch_feed(source, feed_url)
            all_stories.extend(stories)
            successful_feeds += 1

        except (
            requests.RequestException,
            ET.ParseError,
            ValueError,
        ) as error:
            print(f"{source} feed failed: {error}")

    if successful_feeds == 0:
        raise RuntimeError(
            "All news feeds failed. History was not changed."
        )

    # On first run, register current headlines without posting old news.
    if first_run:
        initial_ids = {
            story["id"] for story in all_stories
        }
        save_history(initial_ids)
        print("News history initialized. No old stories sent.")
        return

    unique_stories = []
    run_ids = set()
    run_titles = set()

    for story in all_stories:
        if story["id"] in seen_ids:
            continue

        title_key = story["normalized_title"]

        if title_key and title_key in run_titles:
            continue

        run_ids.add(story["id"])

        if title_key:
            run_titles.add(title_key)

        unique_stories.append(story)

    # RSS usually lists newest first. Send older new stories first.
    new_stories = list(reversed(unique_stories))
    new_stories = new_stories[-MAX_NEWS_PER_RUN:]

    sent_ids = []

    for story in new_stories:
        try:
            send_news(story)
            sent_ids.append(story["id"])
            print(f"Sent news: {story['title']}")

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            print(f"News delivery failed: {error}")
            break

    # Save successfully sent stories only.
    seen_ids.update(sent_ids)
    save_history(seen_ids)

    print(
        f"Finished. Working feeds: "
        f"{successful_feeds}/{len(RSS_FEEDS)}. "
        f"Stories sent: {len(sent_ids)}."
    )


if __name__ == "__main__":
    main()
