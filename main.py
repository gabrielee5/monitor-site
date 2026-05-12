#!/usr/bin/env python3
"""Monitor Comune di Latina news for CIE / carta d'identità mentions; notify via Telegram."""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

URL = "https://www.comune.latina.it/home/novita.html"
STATE_FILE = Path(__file__).parent / "state.json"
ROME = ZoneInfo("Europe/Rome")
ACTIVE_HOURS = range(8, 19)  # 08:00–18:59 Europe/Rome inclusive

KEYWORDS = [
    re.compile(r"carta\s+d['’\s]*identit", re.IGNORECASE),
    re.compile(r"\bcie\b", re.IGNORECASE),
    re.compile(r"identit[àa]\s+elettronic", re.IGNORECASE),
    re.compile(r"\banagrafe\b", re.IGNORECASE),
    re.compile(r"documento\s+(di\s+)?riconoscim", re.IGNORECASE),
]

# Article links look like /notizie/<slug>.html — classes on the cards look auto-generated,
# so we identify articles by href shape, not by CSS.
ARTICLE_HREF = re.compile(r"/notizie/.+\.html$", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def load_state():
    if not STATE_FILE.exists():
        return {"seen_urls": [], "last_match_date": "", "last_eod_date": ""}
    data = json.loads(STATE_FILE.read_text())
    data.setdefault("seen_urls", [])
    data.setdefault("last_match_date", "")
    data.setdefault("last_eod_date", "")
    return data


def save_state(state):
    state["seen_urls"] = sorted(set(state["seen_urls"]))
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def fetch_html():
    # Site occasionally serves cold requests slowly; one inline retry smooths that out.
    # After both attempts fail we propagate, main() logs it, exit 3, next cron retries.
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        for attempt in range(2):
            try:
                resp = client.get(URL)
                resp.raise_for_status()
                return resp.text
            except httpx.RequestError as e:
                if attempt == 1:
                    raise
                print(f"fetch attempt 1 failed ({e!r}); retrying in 3s", file=sys.stderr)
                time.sleep(3)


def parse_articles(html):
    """Return list of (url, title, context_text). Context = surrounding container text."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen_local = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not ARTICLE_HREF.search(href.split("#")[0].split("?")[0]):
            continue
        abs_url = urljoin(URL, href).split("#")[0]
        if abs_url in seen_local:
            continue
        seen_local.add(abs_url)
        title = a.get_text(" ", strip=True) or abs_url.rsplit("/", 1)[-1]
        container = a.find_parent(["article", "li"]) or a.find_parent("div") or a.parent or a
        context = container.get_text(" ", strip=True) if container else title
        out.append((abs_url, title, context))
    return out


def matches_keywords(text):
    return any(p.search(text) for p in KEYWORDS)


def send_telegram(token, chat_id, text):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(api, json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        })
        resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't send Telegram or persist state; print what would happen.")
    args = parser.parse_args()

    now = datetime.now(ROME)
    today = now.date().isoformat()
    if now.hour not in ACTIVE_HOURS:
        print(f"[{now.isoformat()}] outside 08–17 Europe/Rome; exiting.")
        return 0

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.dry_run and (not token or not chat_id):
        print("Missing TELEGRAM_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2

    try:
        html = fetch_html()
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 3

    articles = parse_articles(html)
    print(f"parsed {len(articles)} articles at {now.isoformat()}")

    state = load_state()
    seen = set(state["seen_urls"])

    new_matches = []
    for url, title, context in articles:
        if url in seen:
            continue
        if matches_keywords(context):
            new_matches.append((url, title))
        else:
            seen.add(url)

    sent_any_match = False
    for url, title in new_matches:
        msg = f"✅ CIE/anagrafe news — comune.latina.it\n\n{title}\n\n{url}"
        if args.dry_run:
            print(f"[DRY] match: {title} | {url}")
            seen.add(url)
            sent_any_match = True
            continue
        try:
            send_telegram(token, chat_id, msg)
            seen.add(url)
            sent_any_match = True
            print(f"sent match: {url}")
        except Exception as e:
            # Don't mark seen — next run retries.
            print(f"telegram send failed for {url}: {e}", file=sys.stderr)

    if sent_any_match:
        state["last_match_date"] = today

    if now.hour >= 17:
        already_summarized = state["last_eod_date"] == today
        matched_today = state["last_match_date"] == today
        if not already_summarized and not matched_today:
            msg = "❌ No CIE-related news today on comune.latina.it"
            if args.dry_run:
                print("[DRY] would send EOD summary")
                state["last_eod_date"] = today
            else:
                try:
                    send_telegram(token, chat_id, msg)
                    state["last_eod_date"] = today
                    print("sent EOD summary")
                except Exception as e:
                    print(f"telegram EOD send failed: {e}", file=sys.stderr)

    state["seen_urls"] = sorted(seen)
    if args.dry_run:
        print(f"[DRY] would persist {len(state['seen_urls'])} seen URLs, "
              f"last_match_date={state['last_match_date']!r}, last_eod_date={state['last_eod_date']!r}")
    else:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
