"""Fetch the actual filing document behind an alert so the summariser has
substance to work with — not just the RSS blurb.

NSE filing links point at PDFs (and sometimes XBRL XML) on nsearchives. Reading
them turns "Company informed the Exchange regarding Bagging of order" into a
summary that can say what the order IS. All output remains guarded: the text
extracted here is the ONLY thing the model may compress, and the numeric-
grounding check applies to it.

Fail-soft by design: any failure (too big, not a PDF, extraction error) returns
'' and the alert falls back to the RSS description — never blocked, never wrong.
"""

from __future__ import annotations

import io
import re

import requests

from portfolio_pulse import config

_MAX_BYTES = 6_000_000          # skip monster scans
_MAX_PAGES = 8                  # filings front-load their substance
_MAX_CHARS = 9_000              # keep the model prompt lean


def fetch_filing_text(url: str) -> str:
    """Best-effort text of the filing document at `url` ('' on any failure)."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=config.HTTP_HEADERS,
                            timeout=config.HTTP_TIMEOUT_PDF)
        if not resp.ok or len(resp.content) > _MAX_BYTES:
            return ""
        ctype = resp.headers.get("Content-Type", "").lower()
        if url.lower().endswith(".pdf") or "pdf" in ctype:
            return _pdf_text(resp.content)
        if url.lower().endswith(".xml") or "xml" in ctype:
            return _xml_text(resp.text)
        return ""
    except Exception:
        return ""


def _pdf_text(blob: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        parts = []
        for page in reader.pages[:_MAX_PAGES]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
        return text[:_MAX_CHARS]
    except Exception:
        return ""


def _xml_text(raw: str) -> str:
    """Crude but effective for XBRL: strip tags, keep the values."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_CHARS]
