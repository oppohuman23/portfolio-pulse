"""NSE corporate-filings ingestion from the official RSS feeds.

This is the ToS-compliant channel: NSE publishes these feeds and invites
subscription, so we never scrape nseindia.com's JSON endpoints. Feeds live on
the `nsearchives` host, which serves plain requests (www.nseindia.com bot-blocks).

Feed quirks handled here:
  * <title> is the company name, not the ticker -> matched via ingest.matching.
  * <pubDate> is '18-Jul-2026 12:22:11' (NOT RFC-822) -> custom parse.
  * <description> ends with '|SUBJECT: <subject>' -> subject extracted.
  * Some items link an XBRL .xml instead of a .pdf, and a few are title-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import feedparser
import requests

from portfolio_pulse import config
from portfolio_pulse.ingest.matching import match_symbol
from portfolio_pulse.store.db import guid_hash


@dataclass
class FilingItem:
    symbol: str            # resolved tracked symbol
    company: str           # RSS <title> (company name)
    subject: str           # parsed from '|SUBJECT:' or falls back to description
    description: str       # full <description> text (source text for summariser)
    link: str              # PDF / XBRL URL (the primary source)
    published_at: Optional[datetime]
    feed_name: str         # e.g. 'announcements', 'board_meetings'
    guid: str              # dedup key

    @property
    def category(self) -> str:
        """Human category for the alert header, e.g. 'Order/Contract Win'.

        Derived from which feed the item came from, refined by subject keywords
        for the catch-all announcements feed (where order wins and results also
        appear before/besides their dedicated feeds).
        """
        feed_labels = {
            "financial_results": "Financial Results",
            "board_meetings": "Board Meeting",
            "corporate_actions": "Corporate Action",
            "insider_trading": "Insider Trading",
        }
        if self.feed_name in feed_labels:
            return feed_labels[self.feed_name]
        subject = (self.subject or "").lower()
        if any(k in subject for k in ("bagging", "receiving of order", "orders/contracts",
                                      "award of contract", "work order", "purchase order",
                                      "letter of intent", "loa ")):
            return "Order/Contract Win"
        if "result" in subject:
            return "Financial Results"
        if "dividend" in subject or "bonus" in subject or "split" in subject:
            return "Corporate Action"
        if "press release" in subject:
            return "Press Release"
        return "Announcement"

    @property
    def source_type(self) -> str:
        return f"Exchange Filing · {self.category}"


def _parse_pubdate(raw: str) -> Optional[datetime]:
    """Parse NSE's non-standard pubDate; return IST-aware datetime or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=config.IST)
        except ValueError:
            continue
    return None


def _split_subject(description: str) -> tuple[str, str]:
    """Return (subject, full_description). Subject parsed from '|SUBJECT: ...'."""
    desc = (description or "").strip()
    if "|SUBJECT:" in desc:
        subject = desc.split("|SUBJECT:", 1)[1].strip()
    elif "SUBJECT:" in desc:
        subject = desc.split("SUBJECT:", 1)[1].strip()
    else:
        subject = desc[:120]
    return subject, desc


def fetch_raw(feed_url: str) -> list[dict]:
    """Fetch a feed and return feedparser entries (empty list on any failure)."""
    try:
        resp = requests.get(
            feed_url, headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    parsed = feedparser.parse(resp.content)
    return list(parsed.entries)


def parse_feed(feed_name: str, feed_url: str, symbol_names: dict[str, str]) -> list[FilingItem]:
    """Fetch one feed and return items that match a tracked symbol."""
    items: list[FilingItem] = []
    for entry in fetch_raw(feed_url):
        company = (entry.get("title") or "").strip()
        symbol = match_symbol(company, symbol_names)
        if not symbol:
            continue
        subject, desc = _split_subject(entry.get("description") or entry.get("summary") or "")
        link = (entry.get("link") or "").strip()
        pub = _parse_pubdate(entry.get("pubDate") or entry.get("published") or "")
        guid = guid_hash(link or company, subject, entry.get("pubDate") or "")
        items.append(FilingItem(
            symbol=symbol, company=company, subject=subject, description=desc,
            link=link, published_at=pub, feed_name=feed_name, guid=guid,
        ))
    return items


def mark_seen(store, item: "FilingItem") -> None:
    """Persist a filing's dedup key. Called by the job AFTER the item is handled
    (delivered/muted/deduped) so a mid-poll timeout never loses an undelivered
    alert — unprocessed items simply reappear on the next poll."""
    store.mark_seen(
        item.guid, item.symbol, item.source_type, item.subject or item.company,
        item.link, item.published_at.isoformat() if item.published_at else None,
    )


def poll(store, symbol_names: dict[str, str],
         feeds: Optional[dict[str, str]] = None,
         mark: bool = True) -> list[FilingItem]:
    """Poll all NSE filing feeds and return matched items not yet seen.

    mark=True (default): persist each item's dedup key as it's discovered.
    mark=False: only filter out already-seen items (and de-dup within this
    batch) WITHOUT persisting — the caller marks each seen after handling it,
    so an interrupted poll leaves undelivered items to retry next time.
    """
    feeds = feeds or config.NSE_RSS_FEEDS
    fresh: list[FilingItem] = []
    batch: set[str] = set()
    for feed_name, url in feeds.items():
        for item in parse_feed(feed_name, url, symbol_names):
            if mark:
                if store.mark_seen(
                        item.guid, item.symbol, item.source_type,
                        item.subject or item.company, item.link,
                        item.published_at.isoformat() if item.published_at else None):
                    fresh.append(item)
            else:
                if item.guid in batch or store.is_seen(item.guid):
                    continue
                batch.add(item.guid)
                fresh.append(item)
    return fresh
