"""Central configuration: feed URLs, thresholds, IST market calendar, env plumbing.

Everything tunable lives here so the rest of the code reads cleanly. Secrets come
from environment variables (never hard-coded); see .env.example for the full list.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Load the project-root .env (if present) BEFORE any os.environ reads below, so
# locally-run jobs and the dashboard pick up credentials without manual exports.
# Real environment variables still win over .env values (override=False default).
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except ImportError:  # pragma: no cover — python-dotenv is in requirements.txt
    pass

# --------------------------------------------------------------------------- #
# Time / market calendar (all trading logic is in IST)
# --------------------------------------------------------------------------- #
IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# NSE trading-holiday list. Keep this current each year; the DMA scan skips these
# dates and weekends. (Filings/news polling still runs — companies file off-hours.)
NSE_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 4),    # Holi (indicative — verify against NSE circular yearly)
    date(2026, 3, 21),   # Eid-ul-Fitr (indicative)
    date(2026, 4, 1),    # Annual bank closing (indicative)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 9),   # Diwali (indicative)
    date(2026, 12, 25),  # Christmas
}


def now_ist() -> datetime:
    """Timezone-aware current time in IST."""
    return datetime.now(IST)


def is_trading_day(d: date | None = None) -> bool:
    """True if `d` (default today, IST) is a weekday and not an NSE holiday."""
    d = d or now_ist().date()
    return d.weekday() < 5 and d not in NSE_HOLIDAYS_2026


def is_market_hours(dt: datetime | None = None) -> bool:
    """True during the NSE cash session on a trading day."""
    dt = dt or now_ist()
    if not is_trading_day(dt.date()):
        return False
    return MARKET_OPEN <= dt.timetz().replace(tzinfo=None) <= MARKET_CLOSE


# --------------------------------------------------------------------------- #
# NSE official RSS feeds — the ToS-compliant filings channel.
# Verified live: the `nsearchives` host serves plain requests; www.nseindia.com
# bot-blocks. pubDates are NON-RFC-822 and need custom parsing (see ingest/nse_rss).
# --------------------------------------------------------------------------- #
NSE_RSS_BASE = "https://nsearchives.nseindia.com/content/RSS"
NSE_RSS_FEEDS: dict[str, str] = {
    "announcements": f"{NSE_RSS_BASE}/Online_announcements.xml",
    "board_meetings": f"{NSE_RSS_BASE}/Board_Meetings.xml",
    "financial_results": f"{NSE_RSS_BASE}/Financial_Results.xml",
    "corporate_actions": f"{NSE_RSS_BASE}/Corporate_action.xml",
    "insider_trading": f"{NSE_RSS_BASE}/Insider_Trading.xml",
}

# Routine paperwork filings that drown the signal — muted by default (still
# recorded in seen_items so they never resurface). PP_MUTE_ROUTINE=off disables.
NSE_ROUTINE_SUBJECTS: tuple = (
    "certificate under", "compliance report", "compliances-reg",
    "newspaper publication", "copy of newspaper", "trading window",
    "loss of share certificate", "duplicate share", "share certificate",
    "investor grievance", "reg. 74", "regulation 74", "book closure intimation",
    "spdi", "registrar & share transfer", "issue of duplicate",
    # analyst-meet chatter: scheduling notices with no investor substance
    "analyst", "investor meet", "institutional investor meet",
    "conference call", "con. call", "audio call", "earnings call",
    "investor presentation intimation",
)
MUTE_ROUTINE = os.environ.get("PP_MUTE_ROUTINE", "on").lower() not in ("off", "0", "false")

# A browser-like UA is polite and avoids naive bot filters on the archive host.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
HTTP_TIMEOUT = 20  # seconds (feeds)
HTTP_TIMEOUT_PDF = 8  # filing-document downloads — datacenter IPs get throttled,
                      # so keep this tight; a slow PDF just falls back to the blurb
# A single poll spends at most this long on the filing loop, then leaves the rest
# for the next tick (unprocessed items are NOT marked seen, so nothing is lost).
# Keeps every run well under the workflow's 10-minute ceiling.
FILING_POLL_BUDGET_SEC = 300

# --------------------------------------------------------------------------- #
# News feeds — verified-source whitelist. Anything not on this list is dropped,
# satisfying the "only verified news" requirement. Pulse aggregates the majors;
# publisher feeds are kept as direct, attributable sources.
# --------------------------------------------------------------------------- #
NEWS_RSS_FEEDS: dict[str, str] = {
    "pulse_zerodha": "https://pulse.zerodha.com/feed.php",
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "business_standard_markets": "https://www.business-standard.com/rss/markets-106.rss",
}

# Publisher domains we trust for news attribution. A news item whose link resolves
# outside this set is discarded. (NSE filings bypass this — they are primary.)
NEWS_SOURCE_WHITELIST: set[str] = {
    "economictimes.indiatimes.com",
    "moneycontrol.com",
    "business-standard.com",
    "thehindubusinessline.com",
    "livemint.com",
    "ndtvprofit.com",
    "reuters.com",
    "pulse.zerodha.com",
    "nseindia.com",
    "bseindia.com",
}

# --------------------------------------------------------------------------- #
# DMA (moving-average cross) thresholds
# --------------------------------------------------------------------------- #
DMA_SHORT = 50
DMA_LONG = 200
# Gap slope is measured over this many trading days (smooths one-day noise).
DMA_SLOPE_LOOKBACK = 5
# "Forming" = SMAs converging and projected to cross within this many trading
# days (~2 calendar weeks) — enough advance notice to actually act on.
DMA_FORMING_HORIZON_DAYS = 10
# Cross-check tolerance: yfinance latest close vs Kite quote. Above this ⇒ SUSPECT.
PRICE_CROSSCHECK_TOLERANCE = 0.01  # 1%
# How much daily history to pull so the 200-DMA is well warmed.
PRICE_HISTORY_DAYS = 400

# --------------------------------------------------------------------------- #
# LLM summarizer
# --------------------------------------------------------------------------- #
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MAX_TOKENS = 400
# Below this many characters of source text, we refuse to summarize (INSUFFICIENT).
SUMMARY_MIN_SOURCE_CHARS = 80

# --------------------------------------------------------------------------- #
# Storage backend selection
#   PP_STORE_BACKEND = "sqlite" (default, local/offline) | "supabase" (prod)
# --------------------------------------------------------------------------- #
STORE_BACKEND = os.environ.get("PP_STORE_BACKEND", "sqlite").lower()
SQLITE_PATH = os.environ.get(
    "PP_SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pulse.db"),
)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# --------------------------------------------------------------------------- #
# Secrets (read lazily by the modules that need them; empty is tolerated so the
# offline pieces run without every credential present)
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# "Set-and-forget" mode: sync holdings once and skip the daily Kite re-auth.
# Set PP_AUTH_NUDGE=off to silence the morning login reminder; filings, news and
# DMA alerts run unaffected (DMA shows 'single source' instead of 'verified').
AUTH_NUDGE = os.environ.get("PP_AUTH_NUDGE", "on").lower() not in ("off", "0", "false", "no")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")
# The system's own hosted dashboard URL (Streamlit Cloud). When set, OAuth
# brokers (Upstox/Dhan/Groww) become tap-a-link connects from Telegram: the
# dashboard page catches the login redirect — no local script needed.
DASHBOARD_URL = os.environ.get("PP_DASHBOARD_URL", "").strip().rstrip("/")

# Official hosted broker MCP servers — broker access with no API app/key.
# The Kite Connect API (above) is optional; MCP is the default connection path.
KITE_MCP_URL = os.environ.get("KITE_MCP_URL", "https://mcp.kite.trade/mcp")
UPSTOX_MCP_URL = os.environ.get("UPSTOX_MCP_URL", "https://mcp.upstox.com/mcp")


def morning_auth_deadline(dt: datetime | None = None) -> datetime:
    """The IST datetime by which a fresh Kite token is needed (08:15 next run)."""
    dt = dt or now_ist()
    target = dt.replace(hour=8, minute=15, second=0, microsecond=0)
    if dt >= target:
        target += timedelta(days=1)
    return target


def utc_now() -> datetime:
    """Timezone-aware UTC now (used for stored timestamps)."""
    return datetime.now(timezone.utc)
