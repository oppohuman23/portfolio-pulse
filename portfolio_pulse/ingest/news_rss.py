"""Verified-news ingestion from a whitelisted set of Indian financial RSS feeds.

Unlike NSE filings (primary, always trusted), news items must clear two gates:
  1. The item's link host must be on NEWS_SOURCE_WHITELIST — anything else is
     dropped, which is what "only verified news, not misleading ones" means here.
  2. The item text must actually mention a tracked symbol or its company name.

The item's own description is the ONLY text handed to the summariser, so the
'no fabricated facts' guarantee holds for news exactly as it does for filings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import mktime
from typing import Optional
from urllib.parse import urlparse

import feedparser
import requests

from portfolio_pulse import config
from portfolio_pulse.ingest.matching import text_mentions_symbol
from portfolio_pulse.store.db import guid_hash


@dataclass
class NewsItem:
    symbol: str
    publisher: str         # host, e.g. 'moneycontrol.com'
    title: str
    description: str
    link: str
    published_at: Optional[datetime]
    feed_name: str
    guid: str

    @property
    def source_type(self) -> str:
        return f"News: {self.publisher}"


def _host(url: str) -> str:
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except ValueError:
        return ""


def _whitelisted(url: str) -> Optional[str]:
    """Return the matched whitelist host for `url`, or None if not trusted."""
    host = _host(url)
    if not host:
        return None
    for allowed in config.NEWS_SOURCE_WHITELIST:
        if host == allowed or host.endswith("." + allowed):
            return allowed
    return None


def _pubdate(entry) -> Optional[datetime]:
    tstruct = entry.get("published_parsed") or entry.get("updated_parsed")
    if tstruct:
        try:
            return datetime.fromtimestamp(mktime(tstruct), tz=config.IST)
        except (ValueError, OverflowError):
            return None
    return None


def fetch_raw(feed_url: str) -> list[dict]:
    try:
        resp = requests.get(feed_url, headers=config.HTTP_HEADERS,
                            timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    return list(feedparser.parse(resp.content).entries)


def parse_feed(feed_name: str, feed_url: str,
               symbol_names: dict[str, str]) -> list[NewsItem]:
    """Fetch one news feed and return whitelisted items matching a tracked symbol."""
    items: list[NewsItem] = []
    for entry in fetch_raw(feed_url):
        link = (entry.get("link") or "").strip()
        publisher = _whitelisted(link)
        if not publisher:
            continue
        title = (entry.get("title") or "").strip()
        desc = (entry.get("summary") or entry.get("description") or "").strip()
        blob = f"{title} {desc}"
        for symbol, cname in symbol_names.items():
            if text_mentions_symbol(blob, symbol, cname):
                guid = guid_hash(link or title, symbol)
                items.append(NewsItem(
                    symbol=symbol, publisher=publisher, title=title,
                    description=desc, link=link, published_at=_pubdate(entry),
                    feed_name=feed_name, guid=guid,
                ))
                break  # one symbol per item is enough
    return items


def mark_seen(store, item: "NewsItem") -> None:
    """Persist a news item's dedup key (called after the item is handled)."""
    store.mark_seen(
        item.guid, item.symbol, item.source_type, item.title, item.link,
        item.published_at.isoformat() if item.published_at else None,
    )


def poll(store, symbol_names: dict[str, str],
         feeds: Optional[dict[str, str]] = None,
         mark: bool = True) -> list[NewsItem]:
    """Poll all news feeds and return whitelisted, matched, not-yet-seen items.

    mark=False leaves marking to the caller (see nse_rss.poll for the rationale).
    """
    feeds = feeds or config.NEWS_RSS_FEEDS
    fresh: list[NewsItem] = []
    batch: set[str] = set()
    for feed_name, url in feeds.items():
        for item in parse_feed(feed_name, url, symbol_names):
            if mark:
                if store.mark_seen(
                        item.guid, item.symbol, item.source_type, item.title,
                        item.link,
                        item.published_at.isoformat() if item.published_at else None):
                    fresh.append(item)
            else:
                if item.guid in batch or store.is_seen(item.guid):
                    continue
                batch.add(item.guid)
                fresh.append(item)
    return fresh
