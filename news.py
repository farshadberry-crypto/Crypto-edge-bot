
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

MAX_SEEN = 2000
MAX_NEWS_PER_RUN = 10

RSS_URL = "https://cointelegraph.com/rss"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

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
        return set(), set(), True

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        seen_ids = set(data.get("seen_ids", []))
        seen_titles = set(data.get("seen_titles", []))

        return seen_ids, seen_titles, False

    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Could not read news history: {error}"
        )


def save_history(seen_ids, seen_titles):
    data = {
        "seen_ids": list(seen_ids)[-MAX_SEEN:],
        "seen_titles": list(seen_titles)[-MAX_SEEN:],
    }

    temporary_file = STATE_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temporary_file, STATE_FILE)


# =========================
# FETCH NEWS
# =========================

def fetch_news():
    response = session.get(RSS_URL, timeout=25)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    items = root.findall(".//item")

    if not items:
        items = root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )

    stories = []

    for item in items:
        title_element = item.find("title")

        if title_element is None:
            title_element = item.find(
                "{http://www.w3.org/2005/Atom}title"
            )

        title = clean_text(
            "".join(title_element.itertext())
            if title_element is not None
            else ""
        )

        link = item.findtext("link") or ""

        if not link:
            atom_link = item.find(
                "{http://www.w3.org/2005/Atom}link"
            )

            if atom_link is not None:
                link = atom_link.attrib.get("href", "")

        if not title:
            continue

        stories.append({
            "id": make_id(link, title),
            "title": title,
            "normalized_title": normalize_title(title),
        })

    print(f"Cointelegraph: {len(stories)} stories fetched")

    return stories


# =========================
# TELEGRAM
# =========================

def send_news(story):
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
            timeout=25,
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

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                "Telegram rejected the news message."
            )

        time.sleep(2)
        return

    raise RuntimeError(
        "Could not send news after retries."
    )


# =========================
# MAIN
# =========================

def main():
    print("Starting Crypto Edge News...")

    seen_ids, seen_titles, first_run = load_history()

    # Fetch before changing history.
    # If the feed fails, preserve the existing history.
    stories = fetch_news()

    if not stories:
        raise RuntimeError(
            "Cointelegraph returned no stories. "
            "History was not changed."
        )

    # First run: record existing stories without sending old news.
    if first_run:
        initial_ids = {
            story["id"] for story in stories
        }

        initial_titles = {
            story["normalized_title"]
            for story in stories
            if story["normalized_title"]
        }

        save_history(initial_ids, initial_titles)

        print(
            "News history initialized. "
            "Existing stories were not sent."
        )
        return

    unique_stories = []
    run_ids = set()
    run_titles = set()

    for story in stories:
        story_id = story["id"]
        title_key = story["normalized_title"]

        if story_id in seen_ids:
            continue

        if title_key and (
            title_key in seen_titles
            or title_key in run_titles
        ):
            continue

        if story_id in run_ids:
            continue

        run_ids.add(story_id)

        if title_key:
            run_titles.add(title_key)

        unique_stories.append(story)

    # RSS generally lists newest first.
    # Send older unseen stories first.
    new_stories = list(reversed(unique_stories))
    new_stories = new_stories[-MAX_NEWS_PER_RUN:]

    sent_ids = set()
    sent_titles = set()

    for story in new_stories:
        try:
            send_news(story)

            sent_ids.add(story["id"])

            if story["normalized_title"]:
                sent_titles.add(story["normalized_title"])

            print(f"Sent news: {story['title']}")

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            print(f"News delivery failed: {error}")
            break

    # Save only successfully delivered stories.
    seen_ids.update(sent_ids)
    seen_titles.update(sent_titles)

    save_history(seen_ids, seen_titles)

    print(
        f"Finished. Stories fetched: {len(stories)}. "
        f"Stories sent: {len(sent_ids)}."
    )


if __name__ == "__main__":
    main()
