#!/usr/bin/env python3
"""
ma_news_digest.py
=================
Aggregates M&A news from multiple machine-readable sources, dedupes it, and
(optionally) uses Claude to filter, extract deal terms, and write a ranked
daily digest.

WHY NOT JUST SCRAPE EVERYTHING:
  Brute-force scraping of Bloomberg / WSJ / FT fights paywalls and bot
  detection and breaks whenever a site changes its HTML. This tool instead
  reads channels designed to be consumed by machines:
    1. Google News RSS  -> broad keyword coverage across thousands of outlets
    2. Curated RSS feeds -> high-signal M&A-specific sources
    3. SEC EDGAR EFTS    -> primary-source 8-K / merger-proxy filings (no paywall)
  The AI layer does the hard part: relevance filtering, dedup of the same deal
  reported many times, and structured extraction (acquirer/target/value/sector).

SETUP:
    pip install feedparser requests
    pip install anthropic        # optional, only for the AI digest layer

    # optional, enables the AI layer:
    export ANTHROPIC_API_KEY="sk-ant-..."

RUN:
    python ma_news_digest.py
    python ma_news_digest.py --days 2 --out digest.md --no-ai

The script degrades gracefully: with no API key it still aggregates, dedupes,
and writes a plain digest. With a key it produces a ranked, structured one.
"""

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict

import subprocess

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------
# CONFIG  (edit these to tune coverage)
# ----------------------------------------------------------------------------

# Google News RSS keyword searches. Each becomes its own feed query.
# Add sector terms to focus, e.g. "pharma acquisition", "biotech merger".
GOOGLE_NEWS_QUERIES = [
    '"agreed to acquire"',
    '"agrees to acquire"',
    '"to be acquired by"',
    '"completes acquisition of"',
    '"definitive agreement to acquire"',
    '"agreement and plan of merger"',
    '"subject to customary closing conditions"',
    '"merger agreement"',
    '"takeover bid"',
    '"private equity buyout"',
    '"to take private"',
]

# Curated M&A RSS / Atom feeds. These move around over time; verify and prune.
# Google News queries above already cover most outlets, so treat these as bonus.
CURATED_RSS_FEEDS = [
    # Harvard Law School Forum on Corporate Governance - M&A
    "https://corpgov.law.harvard.edu/category/mergers-acquisitions/feed/",
    # Business Wire - Mergers & Acquisitions subject feed (press releases)
    "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWA==",
]

# SEC EDGAR full-text search. 8-K Item 1.01 = "material definitive agreement",
# which is where most public-company deals are first disclosed. DEFM14A is the
# merger proxy. No API key needed; the SEC only requires a descriptive
# User-Agent with contact info.
SEC_QUERIES = [
    {"q": '"agreement and plan of merger"', "forms": "8-K"},
    {"q": "merger", "forms": "DEFM14A"},
]
# REQUIRED by the SEC: set this to your real name + email before heavy use.
SEC_USER_AGENT = "MA News Digest [email protected]"

# Default model for the AI layer. Swap for cost/quality:
#   claude-haiku-4-5-20251001  (cheapest, fine for daily digests)
#   claude-sonnet-4-6          (balanced, default here)
#   claude-opus-4-8            (highest quality)
AI_MODEL = "claude-sonnet-4-6"

DEFAULT_LOOKBACK_DAYS = 1
DEFAULT_OUTPUT = "ma_digest.md"
REQUEST_TIMEOUT = 20

# Optional editorial note shown at the top of the dashboard. Edit before each run.
MY_TAKE = ""

# Override for the AI-generated "Key Insight of the Day" banner.
# When non-empty, this text is shown instead of the AI-generated insight.
# Leave empty ("") to let the AI generate one automatically each run.
KEY_INSIGHT_OVERRIDE = ""

# Standard GICS-aligned sectors used everywhere (AI prompt, log, dashboard).
GICS_SECTORS = [
    "Technology", "Healthcare", "Financials", "Communications",
    "Consumer Discretionary", "Consumer Staples", "Industrials",
    "Energy", "Materials", "Real Estate", "Utilities",
]

# One fixed color per sector — spread across the full color wheel so all 11
# are distinguishable in both light and dark mode.  Change values here to
# retheme; these are the single source of truth used everywhere in the dashboard.
SECTOR_COLORS: dict[str, str] = {
    "Financials":             "#2563eb",  # vivid blue
    "Healthcare":             "#dc2626",  # red
    "Technology":             "#7c3aed",  # violet
    "Industrials":            "#16a34a",  # green
    "Consumer Discretionary": "#ea580c",  # orange
    "Consumer Staples":       "#0d9488",  # teal
    "Communications":         "#db2777",  # pink/magenta
    "Energy":                 "#d97706",  # amber/gold
    "Materials":              "#78350f",  # brown
    "Utilities":              "#64748b",  # slate grey
    "Real Estate":            "#06b6d4",  # cyan
}
_SECTOR_COLOR_FALLBACK = "#6b7280"  # grey for any unknown sector

SECTOR_GUIDE: dict[str, str] = {
    "Financials":             "banks, insurance, asset & wealth management, capital markets, fintech, consumer finance",
    "Healthcare":             "pharmaceuticals, biotech, medical devices, healthcare providers, life sciences tools, diagnostics",
    "Technology":             "software, semiconductors, IT services, hardware, electronic components",
    "Communications":         "telecom, media, entertainment, advertising, publishing, interactive media",
    "Consumer Discretionary": "retail, autos, apparel, gaming & hospitality, homebuilders, leisure, restaurants",
    "Consumer Staples":       "food & beverage, household products, personal care, packaged goods, food retail",
    "Industrials":            "aerospace & defense, machinery, transportation & logistics, airlines, shipping, construction & engineering, building products",
    "Energy":                 "oil & gas, exploration & production, energy equipment & services, refining, renewables",
    "Materials":              "chemicals, metals & mining, construction materials, containers & packaging, agricultural inputs",
    "Real Estate":            "REITs, real estate development, property management, data center & infrastructure real estate",
    "Utilities":              "electric, gas, water utilities, independent power producers, renewable electricity",
}


# ----------------------------------------------------------------------------
# DATA MODEL
# ----------------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: dt.datetime | None = None
    summary: str = ""
    # filled in by the AI layer when available:
    acquirer: str = ""
    target: str = ""
    value: str = ""
    sector: str = ""
    status: str = ""       # "closed" or "pending"
    importance: int = 0  # 1-5

    def key(self) -> str:
        """Dedup key: normalized title."""
        t = re.sub(r"[^a-z0-9 ]", "", self.title.lower())
        t = re.sub(r"\s+", " ", t).strip()
        return t[:90]


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)        # strip any HTML tags
    return re.sub(r"\s+", " ", text).strip()


def _parsed_to_dt(struct_time) -> dt.datetime | None:
    if not struct_time:
        return None
    try:
        return dt.datetime(*struct_time[:6], tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _within_window(when: dt.datetime | None, cutoff: dt.datetime) -> bool:
    # Keep items with no date (better to over-include than silently drop).
    return when is None or when >= cutoff


_LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|corporation|holdings?|inc|corp|ltd|limited|llc|llp|"
    r"group|plc|co|sa|ag|nv|se|bv|lp)\b\.?",
    re.IGNORECASE,
)


def _norm_party(name: str) -> str:
    """Normalize a company name for entity-based dedup.

    Strips tickers, legal suffixes, punctuation; collapses whitespace.
    Returns empty string when name is blank.
    """
    s = name.strip()
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)       # strip tickers like (AAPL) or (NYSE: AAPL)
    s = re.sub(r"[’']s\b", "", s)          # strip possessive ('s / 's) before punctuation strip
    s = re.sub(r"\band\b", " ", s, flags=re.IGNORECASE)  # strip connector so "X and Y" aligns with "X / Y"
    s = _LEGAL_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _names_match(n1: str, n2: str) -> bool:
    """True when two normalized party names clearly refer to the same entity.

    Exact match always qualifies.  A whole-word prefix match (one name's
    words are exactly the leading words of the other, e.g. "iks" vs.
    "iks health") qualifies at any length, since it's anchored to word
    boundaries rather than an arbitrary substring.  Otherwise, a substring
    match (one contains the other) requires both strings to be at least
    5 chars, avoiding false positives from short tokens like 'co' or 'us'.
    """
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    w1, w2 = n1.split(), n2.split()
    if w1 and w2:
        shorter, longer = (w1, w2) if len(w1) <= len(w2) else (w2, w1)
        if longer[:len(shorter)] == shorter:
            return True
    if len(n1) >= 5 and len(n2) >= 5:
        return n1 in n2 or n2 in n1
    return False


def _richer_item(a: "NewsItem", b: "NewsItem") -> "NewsItem":
    """Return the richer of two items representing the same deal."""
    if a.source == "SEC EDGAR" and b.source != "SEC EDGAR":
        return b
    if b.source == "SEC EDGAR" and a.source != "SEC EDGAR":
        return a
    if bool(b.value) and not bool(a.value):
        return b
    if bool(a.value) and not bool(b.value):
        return a
    return b if b.importance > a.importance else a


def _richer_row(a: dict, b: dict) -> dict:
    """Return the richer of two CSV rows representing the same deal."""
    if a.get("source") == "SEC EDGAR" and b.get("source") != "SEC EDGAR":
        return b
    if b.get("source") == "SEC EDGAR" and a.get("source") != "SEC EDGAR":
        return a
    if bool(b.get("value")) and not bool(a.get("value")):
        return b
    if bool(a.get("value")) and not bool(b.get("value")):
        return a
    try:
        if int(b.get("importance", 0) or 0) > int(a.get("importance", 0) or 0):
            return b
    except (ValueError, TypeError):
        pass
    return a


def _log_key(acquirer: str, target: str, url: str) -> str:
    """Stable dedup key for the deal log (simple equality version).

    Used as a fast-path check; entity_dedupe and append_to_deal_log also
    apply acquirer-contains matching for richer dedup.
    """
    a = _norm_party(acquirer)
    t = _norm_party(target)
    if a and t:
        return f"{a}|{t}"
    return re.sub(r"[?#].*", "", url).rstrip("/")


def _keyword_sector(name: str) -> str:
    """Keyword fallback: map a free-text sector label to the nearest GICS sector."""
    s = name.lower()
    if any(x in s for x in ["tech", "software", "semiconductor", "cloud", "cyber",
                             "data", "saas", "it serv", "artificial intel"]):
        return "Technology"
    if any(x in s for x in ["health", "pharma", "bio", "medic", "hospital",
                             "drug", "clinic", "therapeut", "device"]):
        return "Healthcare"
    if any(x in s for x in ["bank", "financ", "invest", "asset manag", "insur",
                             "credit", "capital", "fund", "wealth", "brokerage"]):
        return "Financials"
    if any(x in s for x in ["telecom", "media", "broadcast", "cable", "wireless",
                             "communication", "gaming", "streaming", "platform",
                             "social", "internet"]):
        return "Communications"
    if any(x in s for x in ["retail", "consumer disc", "home", "leisure",
                             "travel", "hotel", "restaurant", "auto", "vehicle",
                             "apparel", "luxury", "e-commerce"]):
        return "Consumer Discretionary"
    if any(x in s for x in ["food", "beverage", "tobacco", "household prod",
                             "personal care", "staple"]):
        return "Consumer Staples"
    if any(x in s for x in ["industri", "manufactur", "transport", "logistic",
                             "shipping", "airline", "aerospace", "defense",
                             "construction", "hvac", "engineer", "machinery"]):
        return "Industrials"
    if any(x in s for x in ["energy", "oil", "gas", "petroleum", "power",
                             "renewable", "solar", "wind", "mining fuel"]):
        return "Energy"
    if any(x in s for x in ["material", "chemical", "mining", "metal", "steel",
                             "paper", "forest", "packaging", "glass"]):
        return "Materials"
    if any(x in s for x in ["real estate", "reit", "propert", "realty"]):
        return "Real Estate"
    if any(x in s for x in ["util", "electric util", "water util", "sewer"]):
        return "Utilities"
    return "Industrials"   # safe default for uncategorised industrial/misc


def normalize_log_sectors(path: str = "deal_log.csv") -> None:
    """Re-map any non-standard sector values in deal_log.csv to GICS_SECTORS.

    Uses the Claude API when available; falls back to keyword matching.
    Rewrites the file in-place only when at least one row needs updating.
    """
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        return

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"  ! normalize_log_sectors: could not read {path}: {e}", file=sys.stderr)
        return

    gics_set = set(GICS_SECTORS)
    bad = sorted({r.get("sector", "").strip() for r in rows
                  if r.get("sector", "").strip() not in gics_set
                  and r.get("sector", "").strip()})
    if not bad:
        return

    print(f"  Normalizing {len(bad)} non-standard sector label(s)...")

    # Try Claude first, fall back to keywords
    mapping: dict[str, str] = {}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic as _ant
            client = _ant.Anthropic(api_key=api_key)
            gics_str = ", ".join(GICS_SECTORS)
            items_str = "\n".join(f'  "{s}"' for s in bad)
            prompt = (
                f"Map each label below to exactly one of these 11 GICS sectors: "
                f"{gics_str}.\n"
                "Return ONLY a JSON object like {\"old label\": \"GICS sector\"}. "
                "No prose, no fences.\n\nLabels:\n" + items_str
            )
            resp = client.messages.create(
                model=AI_MODEL, max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            text = re.sub(r"^```(?:json)?|```$", "",
                          resp.content[0].text.strip()).strip()
            mapping = json.loads(text)
        except Exception as e:
            print(f"  ! AI sector mapping failed ({e}), using keywords.", file=sys.stderr)

    # Fill any gaps with keyword fallback
    for label in bad:
        if label not in mapping or mapping[label] not in gics_set:
            mapping[label] = _keyword_sector(label)

    changed = 0
    for row in rows:
        old = row.get("sector", "").strip()
        if old in mapping:
            row["sector"] = mapping[old]
            changed += 1

    if changed:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Sector log: {changed} row(s) re-mapped → "
              + ", ".join(f"{k} → {v}" for k, v in sorted(mapping.items())))


# ----------------------------------------------------------------------------
# FETCHERS
# ----------------------------------------------------------------------------

def fetch_google_news(query: str, cutoff: dt.datetime) -> list[NewsItem]:
    q = urllib.parse.quote(query)
    feed_url = (
        f"https://news.google.com/rss/search?q={q}"
        "&hl=en-US&gl=US&ceid=US:en"
    )
    items: list[NewsItem] = []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Google News query failed ({query}): {e}", file=sys.stderr)
        return items

    for entry in parsed.entries:
        when = _parsed_to_dt(getattr(entry, "published_parsed", None))
        if not _within_window(when, cutoff):
            continue
        # Google News appends " - Outlet" to titles; pull the outlet out.
        title = _clean(entry.get("title", ""))
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)
        items.append(NewsItem(
            title=title,
            url=entry.get("link", ""),
            source=source or "Google News",
            published=when,
            summary=_clean(entry.get("summary", "")),
        ))
    return items


def fetch_rss(url: str, cutoff: dt.datetime) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        parsed = feedparser.parse(url)
    except Exception as e:  # noqa: BLE001
        print(f"  ! RSS feed failed ({url}): {e}", file=sys.stderr)
        return items

    feed_title = _clean(parsed.feed.get("title", "")) or url
    for entry in parsed.entries:
        when = _parsed_to_dt(getattr(entry, "published_parsed", None))
        if not _within_window(when, cutoff):
            continue
        items.append(NewsItem(
            title=_clean(entry.get("title", "")),
            url=entry.get("link", ""),
            source=feed_title,
            published=when,
            summary=_clean(entry.get("summary", "")),
        ))
    return items


def fetch_sec_edgar(query: dict, cutoff: dt.datetime) -> list[NewsItem]:
    """Query the EDGAR full-text search API (efts.sec.gov)."""
    start = cutoff.date().isoformat()
    end = dt.date.today().isoformat()
    params = {
        "q": query["q"],
        "forms": query.get("forms", ""),
        "startdt": start,
        "enddt": end,
    }
    url = "https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params)
    items: list[NewsItem] = []
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"  ! SEC query failed ({query['q']}): {e}", file=sys.stderr)
        return items

    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        _id = hit.get("_id", "")
        # _id looks like "0000320193-24-000123:filename.htm"
        if ":" not in _id:
            continue
        accession, doc = _id.split(":", 1)
        ciks = src.get("ciks") or ["0"]
        try:
            cik = int(ciks[0])
        except (ValueError, TypeError):
            cik = 0
        accession_nodash = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik}/{accession_nodash}/{doc}"
        )
        names = src.get("display_names") or ["Unknown filer"]
        file_date = src.get("file_date", "")
        when = None
        if file_date:
            try:
                when = dt.datetime.fromisoformat(file_date).replace(
                    tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        form = src.get("form", query.get("forms", ""))
        items.append(NewsItem(
            title=f"[{form}] {names[0]}",
            url=filing_url,
            source="SEC EDGAR",
            published=when,
            summary=f"Full-text match for {query['q']} in a {form} filing.",
        ))
    # Be polite to the SEC servers.
    time.sleep(0.2)
    return items


# ----------------------------------------------------------------------------
# DEDUP
# ----------------------------------------------------------------------------

def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    seen: dict[str, NewsItem] = {}
    for item in items:
        if not item.title or not item.url:
            continue
        k = item.key()
        if k not in seen:
            seen[k] = item
    # Newest first; undated items sink to the bottom.
    return sorted(
        seen.values(),
        key=lambda x: x.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )


def entity_dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Post-AI dedup using normalized acquirer+target entity matching.

    Two items are considered the same deal when:
      - Their normalized targets are equal, AND
      - Their normalized acquirers are equal OR one contains the other
        (catches "Palo Alto" vs "Palo Alto Networks").
    Items with no extractable parties fall back to title-based dedup.
    When merging, the richer item is kept (non-SEC > has value > higher importance).
    Conservative: ambiguous cases keep both items.
    """
    _sort_key = lambda x: x.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    with_parties: list[NewsItem] = []
    without_parties: list[NewsItem] = []
    for item in items:
        na = _norm_party(item.acquirer)
        nt = _norm_party(item.target)
        # Require both normalized names to be at least 3 chars to be meaningful.
        if na and nt and len(na) >= 3 and len(nt) >= 3:
            with_parties.append(item)
        else:
            without_parties.append(item)

    # Merge items-with-parties using target equality + acquirer-contains.
    merged: list[tuple[str, str, NewsItem]] = []
    for item in with_parties:
        na = _norm_party(item.acquirer)
        nt = _norm_party(item.target)
        found = False
        for i, (ea, et, existing) in enumerate(merged):
            if _names_match(et, nt) and _names_match(na, ea):
                merged[i] = (ea, et, _richer_item(existing, item))
                found = True
                break
        if not found:
            merged.append((na, nt, item))

    result: list[NewsItem] = [item for _, _, item in merged]

    # Title-based dedup for items without parties (e.g. SEC filings).
    seen_title: dict[str, NewsItem] = {}
    for item in without_parties:
        k = item.key()
        if k not in seen_title:
            seen_title[k] = item
    result.extend(seen_title.values())

    return sorted(result, key=_sort_key, reverse=True)


# ----------------------------------------------------------------------------
# AI LAYER (optional)
# ----------------------------------------------------------------------------

def ai_enrich(items: list[NewsItem]) -> list[NewsItem]:
    """Use Claude to filter to real deals, extract terms, and rank.

    Returns the original list unchanged if no API key or the SDK is missing.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  (no ANTHROPIC_API_KEY set; skipping AI enrichment)")
        return items
    try:
        import anthropic
    except ImportError:
        print("  (anthropic SDK not installed; skipping AI enrichment)")
        return items

    client = anthropic.Anthropic(api_key=api_key)

    BATCH_SIZE = 35

    def _build_prompt(batch: list[dict]) -> str:
        return (
            "You are screening news headlines for a finance professional who tracks "
            "corporate mergers and acquisitions.\n\n"
            "KEEP only items that are confirmed, announced corporate M&A transactions: "
            "mergers, acquisitions, takeovers, private equity buyouts, or purchases of "
            "companies, business units, or controlling stakes.\n\n"
            "DISCARD everything else — including:\n"
            "- IPOs, SPAC listings, or public offerings\n"
            "- Debt financings, credit facilities, bond issuances, or equity raises\n"
            "- Product launches, partnerships, licensing deals, or joint ventures\n"
            "- Sports team or franchise ownership changes\n"
            "- Entertainment, celebrity, or talent deals\n"
            "- Real estate, art, or personal asset sales\n"
            "- Market commentary, opinion pieces, analyst notes, or shareholder letters\n"
            "- Deals that have fallen through — rejected, withdrawn, abandoned, "
            "scrapped, terminated, called off, collapsed, dropped, or lapsed bids\n"
            "- Any item you are not certain is a genuine M&A deal\n\n"
            "DEDUP: If a news-source item and an SEC EDGAR item clearly describe the "
            "same deal, keep ONLY the news item.\n\n"
            "STATUS: For every kept item, classify deal stage from the headline's "
            "language alone. 'closed' ONLY when the headline clearly signals a "
            "completed deal (completes, completed, closes, finalizes, completion, "
            "acquisition complete, or clear past-tense 'acquired' in a completion "
            "sense). Everything else that is a live deal is 'pending', including "
            "agreed/definitive-agreement deals, bids, offers, rival bids, regulatory-"
            "clearance steps, and anything ambiguous.\n\n"
            "FIELD RULES:\n"
            "- Items whose source is 'SEC EDGAR': set acquirer and target to empty "
            "string — do not guess from a filing label.\n"
            "- All other items: fill acquirer, target, value, and sector where "
            "identifiable. sector MUST be exactly one of: "
            + ", ".join(GICS_SECTORS) + ". Never invent a different value. "
            "Quick reference: airlines/shipping/aerospace/defense → Industrials; "
            "gaming/homebuilders/auto/retail/travel → Consumer Discretionary; "
            "banks/insurers/asset managers/PE firms → Financials; "
            "telecom/media/cable/internet platforms → Communications; "
            "pharma/biotech/medtech/hospitals → Healthcare.\n\n"
            "Return ONLY a JSON array — no prose, no markdown fences. Do not explain "
            "your reasoning; output only the JSON array. Each element:\n"
            '{"i": <index>, "keep": true, "acquirer": "", "target": "", '
            '"value": "", "sector": "", "importance": 1-5, '
            '"status": "closed"|"pending"}\n'
            "Only include entries where keep is true. importance: 5 = landmark/large, "
            "1 = minor. sector is REQUIRED for non-SEC items — if you cannot assign "
            "one of the 11 sectors with confidence, set keep to false. status is "
            "REQUIRED for every kept item and MUST be exactly 'closed' or 'pending'.\n\n"
            "ITEMS:\n" + json.dumps(batch, ensure_ascii=False)
        )

    # Process in batches; items that fail a batch are silently dropped.
    kept: list[NewsItem] = []
    for batch_start in range(0, len(items), BATCH_SIZE):
        batch_items = items[batch_start: batch_start + BATCH_SIZE]
        catalog = [
            {"i": batch_start + i, "title": it.title, "source": it.source,
             "summary": it.summary[:300]}
            for i, it in enumerate(batch_items)
        ]
        try:
            resp = client.messages.create(
                model=AI_MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": _build_prompt(catalog)}],
            )
            text = "".join(
                block.text for block in resp.content if block.type == "text"
            )
            start, end = text.find("["), text.rfind("]")
            if start == -1 or end == -1 or end < start:
                raise ValueError("no JSON array found in response")
            verdicts = json.loads(text[start:end + 1])
        except Exception as e:  # noqa: BLE001
            print(f"  ! AI batch {batch_start//BATCH_SIZE + 1} failed, dropping batch: {e}",
                  file=sys.stderr)
            continue  # drop the whole batch — never pass unverified items

        for v in verdicts:
            idx = v.get("i")
            if not isinstance(idx, int) or idx < 0 or idx >= len(items):
                continue
            if not v.get("keep"):
                continue
            it = items[idx]
            sec = it.source == "SEC EDGAR"
            acquirer = "" if sec else v.get("acquirer", "")
            target = "" if sec else v.get("target", "")
            sector = v.get("sector", "")
            # Drop non-SEC items that didn't get a valid sector assigned.
            if not sec and sector not in GICS_SECTORS:
                continue
            it.acquirer = acquirer
            it.target = target
            it.value = v.get("value", "")
            it.sector = sector
            it.status = v.get("status", "")
            it.importance = int(v.get("importance", 0) or 0)
            kept.append(it)

    kept.sort(key=lambda x: x.importance, reverse=True)
    return kept


# ----------------------------------------------------------------------------
# OUTPUT
# ----------------------------------------------------------------------------

def _read_saved_insight() -> str:
    """Return the manually saved insight from key_insight.json, or ''.

    This is the same file the Flask app writes when the user edits the banner.
    Resolves relative to the directory of this script so CLI and web app agree.
    """
    import json as _json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_insight.json")
    try:
        if os.path.isfile(path):
            return _json.loads(open(path, encoding="utf-8").read()).get("manual", "").strip()
    except Exception:
        pass
    return ""


def generate_key_insight(items: list[NewsItem]) -> str:
    """Return a 1-2 sentence analytical insight about the most significant deal.

    Priority: KEY_INSIGHT_OVERRIDE (in-code) → key_insight.json (saved via web
    app) → AI-generated. Returns '' if the API is unavailable or fails.
    """
    if KEY_INSIGHT_OVERRIDE.strip():
        return KEY_INSIGHT_OVERRIDE.strip()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    try:
        import anthropic
    except ImportError:
        return ""

    # Build a compact deal list sorted by importance for the prompt.
    candidates = sorted(
        [it for it in items if it.source != "SEC EDGAR" and it.sector],
        key=lambda x: x.importance,
        reverse=True,
    )[:20]
    if not candidates:
        return ""

    deal_list = "\n".join(
        f"- [{it.importance}/5] {it.acquirer or '?'} → {it.target or '?'} "
        f"({it.sector}){': ' + it.value if it.value else ''} | {it.title}"
        for it in candidates
    )

    prompt = (
        "You are a senior M&A analyst writing a one-sentence daily insight for a "
        "finance professional's private deal tracker.\n\n"
        "From the deals below, identify the single most strategically significant "
        "transaction and write ONE to TWO sentences explaining why it matters: its "
        "strategic rationale, what it signals about that sector, or how it fits a "
        "broader M&A trend. Be specific and analytical. Do NOT restate the headline. "
        "Do NOT use filler phrases like 'this deal highlights' or 'this acquisition "
        "underscores.' Write like a sharp analyst, not a press release.\n\n"
        "Return only the insight text — no labels, no quotes, no markdown.\n\n"
        f"DEALS:\n{deal_list}"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text
    except Exception as e:
        print(f"  ! Key insight generation failed: {e}", file=sys.stderr)
        return ""


def write_digest(items: list[NewsItem], path: str, ai_used: bool) -> None:
    today = dt.date.today().strftime("%B %d, %Y")
    lines = [f"# M&A News Digest, {today}", ""]
    lines.append(f"{len(items)} items after dedup"
                 + (" and AI filtering." if ai_used else ".") + "\n")

    for it in items:
        when = it.published.strftime("%b %d") if it.published else ""
        header = it.title
        if it.importance:
            header = f"{'★' * it.importance} {header}"
        lines.append(f"### {header}")
        meta = " | ".join(filter(None, [it.source, when]))
        if meta:
            lines.append(f"*{meta}*")
        deal_bits = []
        if it.acquirer:
            deal_bits.append(f"Acquirer: {it.acquirer}")
        if it.target:
            deal_bits.append(f"Target: {it.target}")
        if it.value:
            deal_bits.append(f"Value: {it.value}")
        if it.sector:
            deal_bits.append(f"Sector: {it.sector}")
        if deal_bits:
            lines.append("  ".join(deal_bits))
        if it.summary:
            lines.append(it.summary[:400])
        lines.append(f"[Source]({it.url})")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nWrote {len(items)} items to {path}")


# ----------------------------------------------------------------------------
# HTML DASHBOARD
# ----------------------------------------------------------------------------

def write_html(items: list[NewsItem], path: str, ai_used: bool, key_insight: str = "") -> None:
    import json as _json

    today_str = dt.date.today().strftime("%B %d, %Y")

    # ── load full deal log ─────────────────────────────────────────────────
    log_path = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", "deal_log.csv")
    log_rows: list[dict] = []
    if os.path.isfile(log_path):
        try:
            with open(log_path, newline="", encoding="utf-8") as f:
                log_rows = list(csv.DictReader(f))
        except Exception as e:
            print(f"  ! Could not read {log_path}: {e}", file=sys.stderr)

    # Fallback to current-run items when log not yet populated
    if not log_rows:
        log_rows = [
            {
                "date": (it.published.date().isoformat() if it.published
                         else dt.date.today().isoformat()),
                "title": it.title, "acquirer": it.acquirer, "target": it.target,
                "value": it.value, "sector": it.sector,
                "importance": str(it.importance), "source": it.source, "url": it.url,
                "status": it.status,
            }
            for it in items if it.source != "SEC EDGAR"
        ]

    news_rows = [r for r in log_rows if r.get("source", "") != "SEC EDGAR"]

    # ── sector → color: use the fixed module-level mapping ─────────────────
    all_sectors = sorted({r.get("sector", "").strip() for r in news_rows
                          if r.get("sector", "").strip()})
    sector_colors: dict[str, str] = {
        s: SECTOR_COLORS.get(s, _SECTOR_COLOR_FALLBACK) for s in all_sectors
    }

    # ── stats from full log ────────────────────────────────────────────────
    # Temporary exclusion for entries whose reported value is in a non-USD
    # currency with no vetted USD conversion, so they don't distort Largest
    # Deal / Top 5 / Disclosed Value while still appearing as deal cards.
    # TODO: remove once structured currency conversion lands.
    _VALUE_EXCLUDED_URLS = {
        "https://news.google.com/rss/articles/CBMihAFBVV95cUxOMDFSX0hIOTcxU3VhaEItYjBmcERKQnh4SUJCa1d2SS1iRlVlNmQ1aWpzUndNYjZweHlERmkzb091VE42ZEw5Z2lmSm9ZV2lrcWNpOE55eTNpNWtsel9hNi0yYmtjM2U4MUNsbHV2Zy1iVzd0eHBYYzV1VTczcWUtYWFWUVI?oc=5",  # ECARX -> Flyme software business (RMB)
        "https://news.google.com/rss/articles/CBMiswFBVV95cUxNZFhTUkNzQ3NSTjRYbjVTTUl0TlhWcE5UV0toWkNCajQyby1aaHBIVnBfaWhFYU9PQ3Q1bTZ4OUNOQkZ4NlRlYU16dktwVDJBZTczVmJvajlXTFdyVTAyN19LRjNpZEhheS1yanRWYXZqVWRPRC1NVUotbUd3NTBqdHZUcW1SOFJiX2FDLXctV1NDWTg2YzVFQkdnQVh6cDZXeGpnTmN3UGN2LURLUk5FRnYxYw?oc=5",  # ECARX -> Flyme Auto and Mobile OS (RMB)
    }

    def parse_m(v: str) -> float | None:
        m = re.search(r'([\d,]+(?:\.\d+)?)\s*([TBMK])', (v or "").upper())
        if not m:
            return None
        n = float(m.group(1).replace(",", ""))
        return n * {'T': 1_000_000, 'B': 1_000, 'M': 1, 'K': 0.001}[m.group(2)]

    def fmt_m(mills: float) -> str:
        if mills >= 1_000_000:
            return f"${mills/1_000_000:.1f}T"
        if mills >= 1_000:
            return f"${mills/1_000:.1f}B"
        return f"${mills:.0f}M"

    valued = [(r, parse_m(r.get("value", ""))) for r in news_rows
              if r.get("url", "") not in _VALUE_EXCLUDED_URLS]
    valued = [(r, v) for r, v in valued if v is not None]
    total_m = sum(v for _, v in valued)
    biggest_r, biggest_m = max(valued, key=lambda x: x[1], default=(None, None))

    stat_total     = str(len(news_rows))
    stat_value     = fmt_m(total_m) if total_m else "—"
    stat_value_sub = (f"across {len(valued)} of {len(news_rows)} deals with disclosed values"
                      if valued else "none disclosed")
    stat_sectors   = str(len(all_sectors)) if all_sectors else "—"
    if biggest_r and biggest_m:
        stat_top     = fmt_m(biggest_m)
        stat_top_sub = (biggest_r.get("acquirer") or biggest_r.get("target")
                        or biggest_r.get("title", "")[:42] or "—")
        stat_top_url = biggest_r.get("url", "")
    else:
        stat_top, stat_top_sub, stat_top_url = "—", "—", ""

    # ── panel A: donut chart by sector ────────────────────────────────────
    sector_counts: dict[str, int] = {}
    for r in news_rows:
        s = r.get("sector", "").strip()
        if s:
            sector_counts[s] = sector_counts.get(s, 0) + 1

    def make_donut_svg() -> str:
        import math as _m
        if not sector_counts:
            return '<p class="panel-empty">No sector data yet.</p>'
        total = sum(sector_counts.values())
        items_s = sorted(sector_counts.items(), key=lambda x: -x[1])
        cx, cy, R, ri = 100, 100, 82, 46
        paths: list[str] = []
        ang = -_m.pi / 2
        for sec, cnt in items_s:
            col = sector_colors.get(sec, _SECTOR_COLOR_FALLBACK)
            sweep = 2 * _m.pi * cnt / total
            if sweep < 0.005:
                ang += sweep
                continue
            halves = [sweep / 2, sweep / 2] if cnt == total else [sweep]
            for da in halves:
                x1 = cx + R * _m.cos(ang);       y1 = cy + R * _m.sin(ang)
                x2 = cx + R * _m.cos(ang + da);  y2 = cy + R * _m.sin(ang + da)
                x3 = cx + ri * _m.cos(ang + da); y3 = cy + ri * _m.sin(ang + da)
                x4 = cx + ri * _m.cos(ang);      y4 = cy + ri * _m.sin(ang)
                la = 1 if da > _m.pi else 0
                d = (f"M{x1:.2f},{y1:.2f} A{R},{R} 0 {la},1 {x2:.2f},{y2:.2f} "
                     f"L{x3:.2f},{y3:.2f} A{ri},{ri} 0 {la},0 {x4:.2f},{y4:.2f}Z")
                paths.append(f'<path d="{d}" fill="{col}" stroke="var(--bg-2)" stroke-width="2"/>')
                ang += da
        svg = ('<svg viewBox="0 0 200 200" width="150" height="150" style="flex-shrink:0">'
               + ''.join(paths) + '</svg>')
        leg = ''.join(
            f'<div class="dl-row">'
            f'<span class="dl-dot" style="background:{sector_colors.get(s, _SECTOR_COLOR_FALLBACK)}"></span>'
            f'<span class="dl-name">{html.escape(s)}</span>'
            f'<span class="dl-ct">{c} <span class="dl-pct">{round(c/total*100)}%</span></span>'
            f'</div>'
            for s, c in items_s
        )
        return f'<div class="donut-inner">{svg}<div class="dl-legend">{leg}</div></div>'

    donut_html = make_donut_svg()

    # ── panel B: top 5 deals — last 30 days by value ───────────────────────
    cutoff_30 = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    recent_valued = sorted(
        ((r, v) for r in news_rows
         if r.get("date", "") >= cutoff_30
         and r.get("url", "") not in _VALUE_EXCLUDED_URLS
         for v in [parse_m(r.get("value", ""))] if v is not None),
        key=lambda x: -x[1]
    )

    def make_top5_html() -> str:
        if not recent_valued:
            return '<p class="panel-empty">No deals with disclosed values in the last 30 days.</p>'
        rows = []
        for r, v in recent_valued[:5]:
            acq = html.escape(r.get("acquirer", "") or "")
            tgt = html.escape(r.get("target", "") or "")
            sec = r.get("sector", "").strip()
            col = sector_colors.get(sec, _SECTOR_COLOR_FALLBACK)
            url = html.escape(r.get("url", "") or "")
            parties = (
                (f'<span class="t5-acq">{acq}</span>' if acq else "") +
                (" <span class='t5-arr'>→</span> " if acq and tgt else "") +
                (f'<span class="t5-tgt">{tgt}</span>' if tgt else "")
            )
            href = f' href="{url}" target="_blank" rel="noopener"' if url else ""
            rows.append(
                f'<a class="t5-row"{href}>'
                f'<div class="t5-parties">{parties}</div>'
                f'<div class="t5-meta">'
                f'<span class="t5-val">{html.escape(fmt_m(v))}</span>'
                f'<span class="t5-tag" style="color:{col};border-color:{col}40;background:{col}14">'
                f'{html.escape(sec)}</span>'
                f'</div></a>'
            )
        return "".join(rows)

    top5_html = make_top5_html()

    # ── panel C: deal activity over time (weekly line chart, inline SVG) ────
    def make_activity_svg() -> str:
        wk: dict[tuple, int] = {}
        for r in news_rows:
            ds = r.get("date", "")
            if ds:
                try:
                    d = dt.date.fromisoformat(ds)
                    key = (d.isocalendar()[0], d.isocalendar()[1])
                    wk[key] = wk.get(key, 0) + 1
                except ValueError:
                    pass
        if not wk:
            return '<p class="panel-empty">No date data yet.</p>'
        weeks = sorted(wk.items())[-16:]
        counts = [c for _, c in weeks]
        max_c = max(counts) or 1
        n = len(weeks)
        W, H, pl, pr, pt, pb = 420, 140, 32, 10, 14, 28
        cw, ch = W - pl - pr, H - pt - pb
        parts: list[str] = []

        def px(i: int) -> float:
            return pl + (i / max(n - 1, 1)) * cw

        def py(c: int) -> float:
            return pt + ch - ch * c / max_c

        # gridlines + y-axis labels
        for tick in sorted({max_c, max_c // 2, 0}):
            yt = py(tick)
            parts.append(
                f'<line x1="{pl}" y1="{yt:.1f}" x2="{W-pr}" y2="{yt:.1f}"'
                f' stroke="var(--border)" stroke-width="1" stroke-dasharray="3 3"/>'
            )
            if tick:
                parts.append(
                    f'<text x="{pl-4}" y="{yt+3:.1f}" text-anchor="end"'
                    f' font-size="8" fill="var(--text-3)">{tick}</text>'
                )

        # filled area under the line
        if n > 1:
            area_pts = (
                f"{px(0):.1f},{py(0):.1f} " +
                " ".join(f"{px(i):.1f},{py(c):.1f}" for i, (_, c) in enumerate(weeks)) +
                f" {px(n-1):.1f},{pt+ch:.1f} {px(0):.1f},{pt+ch:.1f}"
            )
            parts.append(
                f'<polygon points="{area_pts}"'
                f' fill="var(--accent)" opacity="0.12"/>'
            )

        # polyline
        line_pts = " ".join(f"{px(i):.1f},{py(c):.1f}" for i, (_, c) in enumerate(weeks))
        parts.append(
            f'<polyline points="{line_pts}"'
            f' fill="none" stroke="var(--accent)" stroke-width="2"'
            f' stroke-linejoin="round" stroke-linecap="round"/>'
        )

        # data point dots + labels on significant points
        for i, ((yr, w_num), cnt) in enumerate(weeks):
            x, y = px(i), py(cnt)
            is_edge = (i == 0 or i == n - 1)
            is_peak = (cnt == max_c)
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3"'
                f' fill="var(--accent)" stroke="var(--bg-1)" stroke-width="1.5"/>'
            )
            if is_edge or is_peak:
                parts.append(
                    f'<text x="{x:.1f}" y="{y-6:.1f}" text-anchor="middle"'
                    f' font-size="8" fill="var(--text-3)">{cnt}</text>'
                )

        # x-axis date labels: first, last, and every 4th
        for i, ((yr, w_num), _) in enumerate(weeks):
            if i == 0 or i == n - 1 or i % 4 == 0:
                try:
                    lbl = dt.date.fromisocalendar(yr, w_num, 1).strftime("%-m/%-d")
                except Exception:
                    lbl = f"W{w_num}"
                anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
                parts.append(
                    f'<text x="{px(i):.1f}" y="{H - 4:.1f}" text-anchor="{anchor}"'
                    f' font-size="8" fill="var(--text-3)">{lbl}</text>'
                )

        return f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">{"".join(parts)}</svg>'

    activity_html = make_activity_svg()

    # ── panel D: sector guide (static reference strip) ─────────────────────
    def make_sector_guide_html() -> str:
        blocks = []
        for sec in GICS_SECTORS:
            col = SECTOR_COLORS.get(sec, _SECTOR_COLOR_FALLBACK)
            desc = SECTOR_GUIDE.get(sec, "")
            blocks.append(
                f'<div class="sg-block">'
                f'<div class="sg-name" style="color:{col}">{html.escape(sec)}</div>'
                f'<div class="sg-industries">{html.escape(desc)}</div>'
                f'</div>'
            )
        return (
            '<div class="sg-overlay" id="sg-overlay">'
            '<div class="sg-modal" id="sg-modal">'
            '<button class="sg-close" id="sg-close" aria-label="Close">&#x2715;</button>'
            '<div class="sg-header">'
            '<span class="sg-title">Sector Guide</span>'
            '<span class="sg-caption">what each sector tab covers</span>'
            '</div>'
            f'<div class="sg-grid">{"".join(blocks)}</div>'
            '</div>'
            '</div>'
        )

    sector_guide_html = make_sector_guide_html()

    # ── "My take" banner ───────────────────────────────────────────────────
    my_take_html = ""
    if MY_TAKE:
        my_take_html = (
            f'<div class="my-take">'
            f'<span class="my-take-lbl">My Take</span>'
            f'<span class="my-take-txt">{html.escape(MY_TAKE)}</span>'
            f'</div>'
        )

    # ── bake deal data as JSON ─────────────────────────────────────────────
    sorted_rows = sorted(
        news_rows,
        key=lambda r: (r.get("date", ""), int(r.get("importance", "0") or "0")),
        reverse=True,
    )

    def row_title(r: dict) -> str:
        t = r.get("title", "").strip()
        if t:
            return t
        acq, tgt = r.get("acquirer", ""), r.get("target", "")
        if acq and tgt:
            return f"{acq} acquires {tgt}"
        return acq or tgt or r.get("source", "")

    deals_payload = [
        {
            "date":       r.get("date", ""),
            "title":      row_title(r),
            "acquirer":   r.get("acquirer", ""),
            "target":     r.get("target", ""),
            "value":      r.get("value", ""),
            "sector":     r.get("sector", "").strip(),
            "importance": int(r.get("importance", "0") or "0"),
            "source":     r.get("source", ""),
            "url":        r.get("url", ""),
        }
        for r in sorted_rows
    ]

    js_data = (
        f"const LOG = {_json.dumps(deals_payload, ensure_ascii=False)};\n"
        f"const SECTOR_COLORS = {_json.dumps(sector_colors)};\n"
    )

    # ── CSS (plain string — braces need no escaping) ───────────────────────
    css = """
:root[data-theme="dark"] {
  --bg:      #141417;
  --bg-2:    #1e1e22;
  --bg-3:    #28282d;
  --border:  #32323a;
  --text-1:  #ededf0;
  --text-2:  #8c8c98;
  --text-3:  #46464f;
  --accent:  #4f8ef7;
  --shadow:  0 1px 4px rgba(0,0,0,.55);
}
:root[data-theme="light"] {
  --bg:      #f3f3ee;
  --bg-2:    #ffffff;
  --bg-3:    #ebebе6;
  --border:  #d8d8d2;
  --text-1:  #1a1a1e;
  --text-2:  #60606a;
  --text-3:  #a8a8b0;
  --accent:  #2563eb;
  --shadow:  0 1px 3px rgba(0,0,0,.09);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Roboto', system-ui, -apple-system, sans-serif;
  background: var(--bg); color: var(--text-1);
  font-size: 16px; line-height: 1.55; -webkit-font-smoothing: antialiased;
}
a { text-decoration: none; color: inherit; }
button { cursor: pointer; font-family: inherit; border: none; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

/* ── shell ── */
.app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* ── nav ── */
.nav {
  display: flex; align-items: center; justify-content: space-between;
  padding: .95rem 2.5rem;
  background: var(--bg-2); border-bottom: 1px solid var(--border);
  flex-shrink: 0; gap: 1rem;
}
.nav-brand { font-size: 1rem; font-weight: 700; letter-spacing: -.02em; }
.nav-brand em { font-style: normal; color: var(--accent); }
.nav-right { display: flex; align-items: center; gap: 1.5rem; }
.nav-date { font-size: .7rem; color: var(--text-3); font-family: 'Roboto Mono', monospace; }
.nav-meta { font-size: .68rem; color: var(--accent); }
.theme-btn {
  width: 28px; height: 28px; border-radius: 50%;
  border: 1px solid var(--border) !important;
  background: var(--bg-3); color: var(--text-2);
  font-size: .85rem; display: flex; align-items: center; justify-content: center;
  transition: background .15s;
}
.theme-btn:hover { background: var(--border); }

/* ── stats strip ── */
.stats {
  display: grid; grid-template-columns: repeat(4, 1fr);
  border-bottom: 1px solid var(--border);
  background: var(--bg-2); flex-shrink: 0;
}
.stat { padding: 1.25rem 2.5rem; border-right: 1px solid var(--border); }
.stat:last-child { border-right: none; }
.s-lbl {
  font-size: .63rem; letter-spacing: .1em; text-transform: uppercase;
  color: var(--text-3); margin-bottom: .4rem; font-weight: 500;
}
.s-val {
  font-size: 1.9rem; font-weight: 700; color: var(--accent);
  font-family: 'Roboto Mono', monospace; letter-spacing: -.02em; line-height: 1.1;
}
.s-sub { font-size: .68rem; color: var(--text-3); margin-top: .25rem; }

/* ── key insight banner ── */
.key-insight {
  position: relative;
  padding: 1rem 2.5rem;
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-2));
  border-top: 3px solid var(--accent);
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  display: flex; flex-direction: column; gap: .3rem; flex-shrink: 0;
}
.ki-lbl {
  font-size: .72rem; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: var(--accent);
}
.ki-txt {
  font-size: .95rem; font-weight: 400; color: var(--text-1);
  line-height: 1.5; text-align: left;
}

/* ── my-take ── */
.my-take {
  padding: .6rem 2rem;
  background: var(--bg-2); border-bottom: 1px solid var(--border);
  display: flex; align-items: baseline; gap: .9rem; flex-shrink: 0;
}
.my-take-lbl {
  font-size: .58rem; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: var(--accent); flex-shrink: 0;
}
.my-take-txt { font-size: .82rem; color: var(--text-2); font-style: italic; }

/* ── tab panels ── */
.tab-panel { flex: 1; overflow-y: auto; min-height: 0; }
.tab-panel.hidden { display: none !important; }

/* ── home panel grid ── */
.home-panels {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 1.75rem; padding: 2rem 2.5rem 3rem;
}
.panel {
  background: var(--bg-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 1.75rem 2rem; display: flex; flex-direction: column; gap: 1rem;
}
.panel-title {
  font-size: .68rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--text-3); padding-bottom: .65rem; border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.panel-sub { font-size: .65rem; color: var(--text-3); margin-top: -.5rem; }
.panel-empty { font-size: .8rem; color: var(--text-3); }

/* donut chart */
.donut-inner { display: flex; align-items: flex-start; gap: 1rem; }
.dl-legend { display: flex; flex-direction: column; gap: .38rem; flex: 1; min-width: 0; }
.dl-row { display: flex; align-items: center; gap: .4rem; min-width: 0; }
.dl-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dl-name { font-size: .7rem; color: var(--text-2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl-ct { font-size: .67rem; color: var(--text-1); font-family: 'Roboto Mono', monospace; white-space: nowrap; flex-shrink: 0; }
.dl-pct { color: var(--text-3); }

/* top 5 */
.t5-row {
  display: flex; flex-direction: column; gap: .22rem;
  padding: .6rem .5rem; border-bottom: 1px solid var(--border);
  text-decoration: none; border-radius: 4px; margin: 0 -.5rem;
  transition: background .12s; cursor: pointer;
}
.t5-row:hover { background: color-mix(in srgb, var(--accent) 8%, var(--bg-2)); }
.t5-row:last-child { border-bottom: none; }
.t5-parties { font-size: .8rem; display: flex; align-items: center; gap: .28rem; flex-wrap: wrap; }
.t5-acq { color: var(--text-1); font-weight: 500; }
.t5-arr { color: var(--text-3); }
.t5-tgt { color: var(--accent); font-weight: 500; }
.t5-meta { display: flex; align-items: center; gap: .5rem; }
.t5-val { font-size: .78rem; font-weight: 600; color: #10b981; font-family: 'Roboto Mono', monospace; }
.t5-tag { font-size: .57rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; padding: .1rem .42rem; border-radius: 3px; border: 1px solid; }

/* ── sectors button (nav) ── */
.sectors-btn {
  background: var(--accent); border: none; border-radius: 4px;
  color: #fff; font-size: .68rem; font-family: inherit; font-weight: 600;
  padding: .28rem .72rem; cursor: pointer; letter-spacing: .03em;
  transition: opacity .13s;
}
.sectors-btn:hover { opacity: .82; }

/* ── sector guide modal ── */
.sg-overlay {
  position: fixed; inset: 0; z-index: 500;
  background: rgba(0,0,0,.72);
  display: flex; align-items: center; justify-content: center;
  padding: 2rem; overflow-y: auto;
  opacity: 0; pointer-events: none; transition: opacity .18s;
}
.sg-overlay.open { opacity: 1; pointer-events: auto; }
.sg-modal {
  background: var(--bg-2);
  border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
  box-shadow: 0 8px 40px rgba(0,0,0,.55), 0 1px 6px rgba(0,0,0,.3);
  border-radius: 12px; width: 82vw; max-width: 1100px;
  max-height: 92vh; overflow-y: auto;
  padding: 3rem 3.5rem 3.5rem; position: relative;
  transform: scale(.97); transition: transform .18s;
}
.sg-overlay.open .sg-modal { transform: scale(1); }
.sg-close {
  position: absolute; top: 1.1rem; right: 1.3rem;
  background: none; border: none; color: var(--text-3);
  font-size: 1.2rem; cursor: pointer; padding: .2rem .45rem;
  border-radius: 3px; line-height: 1; transition: color .12s, background .12s;
}
.sg-close:hover { color: var(--text-1); background: var(--border); }
.sg-header {
  display: flex; align-items: baseline; gap: .85rem; margin-bottom: 1.75rem;
}
.sg-title {
  font-size: .7rem; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: var(--text-2);
}
.sg-caption { font-size: .75rem; color: var(--text-2); opacity: .8; }
.sg-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 2.5rem 3rem;
}
@media (max-width: 760px) { .sg-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 520px) { .sg-grid { grid-template-columns: repeat(2, 1fr); } }
.sg-name {
  font-size: .85rem; font-weight: 700; margin-bottom: .5rem; letter-spacing: .01em;
}
.sg-industries { font-size: .78rem; color: var(--text-1); line-height: 1.65; }

/* ── tab bar ── */
.tabbar {
  display: flex; align-items: stretch;
  background: var(--bg-2); border-bottom: 1px solid var(--border);
  padding: 0 1.5rem; overflow-x: auto; flex-shrink: 0;
  --tab-accent: var(--accent);
}
.tabbar::-webkit-scrollbar { height: 0; }
.tab {
  padding: .78rem 1.1rem; font-size: .8rem; font-weight: 500;
  color: var(--text-3); cursor: pointer; white-space: nowrap;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  transition: color .12s; user-select: none;
  display: flex; align-items: center; gap: .45rem;
}
.tab:hover { color: var(--text-2); }
.tab.active { color: var(--tab-accent); border-bottom-color: var(--tab-accent); }
.tab-ct { font-size: .68rem; font-family: 'Roboto Mono', monospace; color: var(--text-3); }
.tab.active .tab-ct { color: inherit; opacity: .6; }

/* ── cards panel ── */
.cards-panel { flex: 1; overflow-y: auto; padding: 2rem 2.5rem; }
#cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1.35rem; align-content: start;
}
.empty-state { color: var(--text-3); font-size: .9rem; padding: 2.5rem 0; }

/* ── deal cards ── */
.card {
  background: var(--bg-2); border: 1px solid var(--border);
  border-left: 3px solid var(--border); border-radius: 6px;
  padding: 1.5rem 1.65rem;
  box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: .75rem;
}
.card--top { background: var(--bg-3); }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.card-head-r { display: flex; align-items: center; gap: .6rem; }
.sector-tag {
  font-size: .65rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase;
  padding: .18rem .6rem; border-radius: 3px; border: 1px solid;
}
.top-badge { font-size: .63rem; font-weight: 700; color: var(--accent); letter-spacing: .04em; }
.imp-dots { font-size: .68rem; color: var(--text-3); letter-spacing: 1.5px; font-family: 'Roboto Mono', monospace; }
.card-date { font-size: .68rem; color: var(--text-3); font-family: 'Roboto Mono', monospace; }
.card-parties { display: flex; align-items: center; gap: .35rem; flex-wrap: wrap; }
.acq { font-size: .96rem; font-weight: 600; color: var(--text-1); }
.arr { font-size: .82rem; color: var(--text-3); }
.tgt { font-size: .96rem; font-weight: 600; color: var(--accent); }
.card-title { font-size: .87rem; color: var(--text-2); line-height: 1.6; }
.source-cta {
  display: flex; align-items: center; justify-content: space-between;
  padding: .7rem .95rem; border: 1px solid var(--border); border-radius: 4px;
  background: var(--bg-3); font-size: .82rem; font-weight: 500; color: var(--text-1);
  transition: background .12s, border-color .12s;
}
.source-cta:hover { background: var(--border); }
.source-name { font-size: .72rem; color: var(--text-3); font-weight: 400; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-value { font-size: .72rem; font-weight: 600; color: #10b981; font-family: 'Roboto Mono', monospace; }

/* ── footer ── */
.footer {
  background: var(--bg-2); border-top: 1px solid var(--border);
  padding: .45rem 2rem; font-size: .61rem; color: var(--text-3);
  flex-shrink: 0; display: flex; justify-content: space-between;
}
"""

    # ── JS rendering functions (regular string — no escaping needed) ───────
    js_code = """
const LIMIT = 20;

function dealsForTab(tab) {
  return tab === '_all'
    ? LOG.slice(0, LIMIT)
    : LOG.filter(d => d.sector === tab).slice(0, LIMIT);
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderCard(deal, isTop) {
  const color  = SECTOR_COLORS[deal.sector] || '#6b7280';
  const imp    = deal.importance || 0;
  const alphas = [0, 0.18, 0.34, 0.52, 0.72, 1.0];
  const ha     = Math.round((alphas[imp] || 0.18) * 255).toString(16).padStart(2,'0');
  const border = color + ha;

  const dp = (deal.date || '').split('-');
  const dateStr = dp.length === 3 ? dp[1] + '/' + dp[2] : (deal.date || '');

  const parties = (deal.acquirer || deal.target) ? (
    '<div class="card-parties">' +
    (deal.acquirer ? '<span class="acq">' + esc(deal.acquirer) + '</span>' : '') +
    (deal.acquirer && deal.target ? '<span class="arr">&#8594;</span>' : '') +
    (deal.target   ? '<span class="tgt">' + esc(deal.target)   + '</span>' : '') +
    '</div>'
  ) : '';

  const impDots = imp > 0
    ? '<span class="imp-dots" title="Importance ' + imp + '/5">'
      + '●'.repeat(imp) + '○'.repeat(5 - imp) + '</span>'
    : '';

  const topBadge = isTop ? '<span class="top-badge">&#8593; TOP</span>' : '';
  const valHtml  = deal.value ? '<div><span class="card-value">' + esc(deal.value) + '</span></div>' : '';

  return (
    '<article class="card' + (isTop ? ' card--top' : '') + '"'
    + ' style="border-left-color:' + border + '">'
    + '<div class="card-head">'
    +   '<span class="sector-tag"'
    +     ' style="color:' + color + ';border-color:' + color + '40;background:' + color + '14">'
    +     esc(deal.sector || '—')
    +   '</span>'
    +   '<div class="card-head-r">' + topBadge + impDots
    +     '<span class="card-date">' + dateStr + '</span>'
    +   '</div>'
    + '</div>'
    + parties
    + '<p class="card-title">' + esc(deal.title) + '</p>'
    + '<a class="source-cta" href="' + esc(deal.url) + '" target="_blank" rel="noopener">'
    +   '<span>Read full story &rarr;</span>'
    +   '<span class="source-name">' + esc(deal.source) + '</span>'
    + '</a>'
    + valHtml
    + '</article>'
  );
}

function renderGrid(tab) {
  const deals = dealsForTab(tab);
  if (!deals.length) return '<p class="empty-state">No deals in this category yet.</p>';
  const topIdx = deals.reduce((b, d, i) => d.importance > deals[b].importance ? i : b, 0);
  return deals.map((d, i) => renderCard(d, i === topIdx)).join('');
}

function activate(el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  const color = el.dataset.color || 'var(--accent)';
  document.querySelector('.tabbar').style.setProperty('--tab-accent', color);

  const homePanel = document.getElementById('home-panel');
  const dealPanel = document.getElementById('deal-panel');
  const tab = el.dataset.tab;

  if (tab === '_home') {
    homePanel.classList.remove('hidden');
    dealPanel.classList.add('hidden');
  } else {
    homePanel.classList.add('hidden');
    dealPanel.classList.remove('hidden');
    document.getElementById('cards').innerHTML = renderGrid(tab);
  }
}

function init() {
  const counts = {};
  LOG.forEach(d => { if (d.sector) counts[d.sector] = (counts[d.sector] || 0) + 1; });
  const sectors = Object.entries(counts).sort((a, b) => b[1] - a[1]).map(x => x[0]);

  const specs = [
    { tab: '_home',  label: 'Home',      color: null, count: null },
    { tab: '_all',   label: 'All Deals', color: null, count: Math.min(LOG.length, 20) },
    ...sectors.map(s => ({
      tab: s, label: s,
      color: SECTOR_COLORS[s] || null,
      count: Math.min(LOG.filter(d => d.sector === s).length, 20)
    }))
  ];

  const tabbar = document.getElementById('tabbar');
  specs.forEach((spec, i) => {
    const el = document.createElement('div');
    el.className = 'tab' + (i === 0 ? ' active' : '');
    el.dataset.tab   = spec.tab;
    el.dataset.color = spec.color || '';
    const ct = spec.count !== null ? ' <span class="tab-ct">' + spec.count + '</span>' : '';
    el.innerHTML = esc(spec.label) + ct;
    el.addEventListener('click', () => activate(el));
    tabbar.appendChild(el);
  });
  // Home is active by default; deal-panel starts hidden via HTML class.
}

document.getElementById('theme-toggle').addEventListener('click', function () {
  const root = document.documentElement;
  const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  this.textContent = next === 'dark' ? '◑' : '●';
});

document.addEventListener('DOMContentLoaded', init);

// ── sector guide modal ──
(function () {
  const overlay  = document.getElementById('sg-overlay');
  const modal    = document.getElementById('sg-modal');
  const openBtn  = document.getElementById('sectors-btn');
  const closeBtn = document.getElementById('sg-close');
  if (!overlay || !openBtn) return;

  function openModal()  { overlay.classList.add('open'); }
  function closeModal() { overlay.classList.remove('open'); }

  openBtn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', function (e) {
    if (!modal.contains(e.target)) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeModal();
  });
})();
"""

    doc = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>John Raup's Daily M&amp;A Brief — {today_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="app">

<nav class="nav">
  <div class="nav-brand">John Raup's <em>Daily M&amp;A Brief</em></div>
  <button class="sectors-btn" id="sectors-btn">Sectors</button>
  <div class="nav-right">
    <span class="nav-date">{today_str}</span>
    <span class="nav-meta">{len(news_rows)} deals logged{"  ·  AI-filtered" if ai_used else ""}</span>
    <button class="theme-btn" id="theme-toggle" title="Toggle light / dark">◑</button>
  </div>
</nav>

<div class="tabbar" id="tabbar"></div>

<div id="home-panel" class="tab-panel">
  {('<div class="key-insight"><span class="ki-lbl">Key Insight of the Day</span><span class="ki-txt">' + html.escape(key_insight) + '</span></div>') if key_insight else ''}
  <div class="stats">
    <div class="stat">
      <div class="s-lbl">Total Deals</div>
      <div class="s-val">{stat_total}</div>
    </div>
    <div class="stat">
      <div class="s-lbl">Disclosed Value</div>
      <div class="s-val">{stat_value}</div>
      <div class="s-sub">{stat_value_sub}</div>
    </div>
    <div class="stat">
      <div class="s-lbl">Sectors</div>
      <div class="s-val">{stat_sectors}</div>
    </div>
    <div class="stat">
      <div class="s-lbl">Largest Deal</div>
      {'<a href="' + html.escape(stat_top_url) + '" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;">' if stat_top_url else ''}
      <div class="s-val">{stat_top}</div>
      <div class="s-sub">{html.escape(stat_top_sub)}</div>
      {'</a>' if stat_top_url else ''}
    </div>
  </div>
  {my_take_html}
  <div class="home-panels">
    <div class="panel">
      <div class="panel-title">Deals by Sector</div>
      {donut_html}
    </div>
    <div class="panel">
      <div class="panel-title">Top 5 Deals — Last 30 Days</div>
      <div class="panel-sub">by disclosed value · based on deals tracked</div>
      {top5_html}
    </div>
    <div class="panel">
      <div class="panel-title">Deal Activity Over Time</div>
      <div class="panel-sub">weekly deal count · based on deals tracked</div>
      {activity_html}
    </div>
  </div>
</div>

<div id="deal-panel" class="tab-panel hidden">
  <div class="cards-panel"><div id="cards"></div></div>
</div>

<footer class="footer">
  <span>Auto-generated daily · {today_str}{"  ·  AI-filtered" if ai_used else ""}</span>
  <span>Source: deal_log.csv</span>
</footer>

{sector_guide_html}
</div>
<script>
{js_data}{js_code}
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"Wrote HTML dashboard to {path}")

# PLACEHOLDER — replaced by new write_html above

# ----------------------------------------------------------------------------
# DEAL LOG
# ----------------------------------------------------------------------------

_LOG_COLUMNS = ["date", "acquirer", "target", "value", "sector",
                "importance", "source", "url", "title", "status"]


def _item_matches_row(it: NewsItem, row: dict) -> bool:
    """True when a new item and an existing CSV row refer to the same deal.

    Entity-based: normalized targets must be equal AND normalized acquirers
    must be equal or one contains the other.  Falls back to URL comparison
    for no-party items (e.g. SEC filings).
    """
    ia = _norm_party(it.acquirer)
    it_ = _norm_party(it.target)
    ra = _norm_party(row.get("acquirer", ""))
    rt = _norm_party(row.get("target", ""))
    if ia and it_ and len(ia) >= 3 and len(it_) >= 3 and ra and rt:
        return _names_match(rt, it_) and _names_match(ia, ra)
    # Fall back to canonical URL for no-party items.
    iu = re.sub(r"[?#].*", "", it.url).rstrip("/")
    ru = re.sub(r"[?#].*", "", row.get("url", "")).rstrip("/")
    return bool(iu) and iu == ru


def append_to_deal_log(items: list[NewsItem], path: str = "deal_log.csv") -> int:
    """Append new deals to a persistent CSV log; return the number of rows added.

    Uses entity-based dedup (normalized acquirer+target with acquirer-contains
    matching) rather than a flat key set, so name variants of the same company
    are recognized as the same deal.
    """
    file_exists = os.path.isfile(path) and os.path.getsize(path) > 0

    existing_rows: list[dict] = []
    needs_migration = False

    if file_exists:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                needs_migration = not {"title", "status"}.issubset(reader.fieldnames or [])
                existing_rows = list(reader)
        except Exception as e:
            print(f"  ! Could not read {path}: {e}", file=sys.stderr)

    today = dt.date.today().isoformat()
    # Start accumulated list with existing rows; check each new item against it.
    accumulated = list(existing_rows)
    new_rows: list[dict] = []
    for it in items:
        if any(_item_matches_row(it, row) for row in accumulated):
            continue
        row = {
            "date": it.published.date().isoformat() if it.published else today,
            "acquirer": it.acquirer,
            "target": it.target,
            "value": it.value,
            "sector": it.sector,
            "importance": it.importance,
            "source": it.source,
            "url": it.url,
            "title": it.title,
            "status": it.status,
        }
        accumulated.append(row)
        new_rows.append(row)

    if needs_migration and existing_rows:
        for row in existing_rows:
            row.setdefault("title", "")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing_rows + new_rows)
    elif new_rows:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS)
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_rows)

    n = len(new_rows)
    print(f"Deal log: {n} new row{'s' if n != 1 else ''} "
          f"appended to {path} ({len(accumulated)} total).")
    return n


def dedup_deal_log(path: str = "deal_log.csv") -> int:
    """Deduplicate deal_log.csv in place using entity-based matching.

    Reads all rows, merges duplicates by keeping the richer row, rewrites
    the file only when at least one row is removed.  Returns the count removed.
    """
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        print(f"  ! Could not read {path} for dedup: {e}", file=sys.stderr)
        return 0

    if not rows:
        return 0

    kept: list[dict] = []
    for row in rows:
        ra = _norm_party(row.get("acquirer", ""))
        rt = _norm_party(row.get("target", ""))
        merged_into = None
        if ra and rt and len(ra) >= 3 and len(rt) >= 3:
            for i, existing in enumerate(kept):
                ea = _norm_party(existing.get("acquirer", ""))
                et = _norm_party(existing.get("target", ""))
                if ea and et and _names_match(et, rt) and _names_match(ra, ea):
                    kept[i] = _richer_row(existing, row)
                    merged_into = i
                    break
        if merged_into is None:
            kept.append(row)

    removed = len(rows) - len(kept)
    if removed > 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(kept)
        print(f"  Log dedup: removed {removed} duplicate row(s), {len(kept)} remain.")
    return removed


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def run_pipeline(
    days: int = DEFAULT_LOOKBACK_DAYS,
    out: str = DEFAULT_OUTPUT,
    no_ai: bool = False,
    no_sec: bool = False,
) -> str:
    """Run the full M&A pipeline and return the path to the generated HTML file.

    Callable both from the CLI (via main()) and from the Flask web app.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    all_items: list[NewsItem] = []

    print("Fetching Google News searches...")
    for q in GOOGLE_NEWS_QUERIES:
        got = fetch_google_news(q, cutoff)
        print(f"  {q}: {len(got)}")
        all_items += got

    print("Fetching curated RSS feeds...")
    for feed_url in CURATED_RSS_FEEDS:
        got = fetch_rss(feed_url, cutoff)
        print(f"  {feed_url[:50]}...: {len(got)}")
        all_items += got

    if not no_sec:
        print("Fetching SEC EDGAR filings...")
        for query in SEC_QUERIES:
            got = fetch_sec_edgar(query, cutoff)
            print(f"  {query['q']} ({query.get('forms')}): {len(got)}")
            all_items += got

    print(f"\nTotal raw items: {len(all_items)}")
    deduped = dedupe(all_items)
    print(f"After dedup: {len(deduped)}")

    ai_used = False
    if not no_ai:
        print("Running AI enrichment...")
        before = len(deduped)
        deduped = ai_enrich(deduped)
        ai_used = len(deduped) != before or any(i.importance for i in deduped)
        deduped = entity_dedupe(deduped)
        print(f"After entity dedup: {len(deduped)}")

    append_to_deal_log(deduped)
    dedup_deal_log()
    normalize_log_sectors()

    key_insight = _read_saved_insight()
    if key_insight:
        print(f"  Using saved manual insight.")
    elif not no_ai:
        print("Generating key insight...")
        key_insight = generate_key_insight(deduped)
        if key_insight:
            print(f"  Insight: {key_insight[:80]}{'…' if len(key_insight) > 80 else ''}")

    write_digest(deduped, out, ai_used)

    html_path = os.path.join(os.path.dirname(os.path.abspath(out)), "index.html")
    write_html(deduped, html_path, ai_used, key_insight=key_insight)
    return html_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggregate and digest M&A news.")
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="Look back this many days.")
    ap.add_argument("--out", default=DEFAULT_OUTPUT, help="Output markdown file.")
    ap.add_argument("--no-ai", action="store_true",
                    help="Skip the Claude enrichment layer.")
    ap.add_argument("--no-sec", action="store_true",
                    help="Skip SEC EDGAR filings.")
    args = ap.parse_args()

    html_path = run_pipeline(
        days=args.days,
        out=args.out,
        no_ai=args.no_ai,
        no_sec=args.no_sec,
    )
    subprocess.run(["open", html_path], check=False)


if __name__ == "__main__":
    main()
