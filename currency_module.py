"""Currency normalization for the M&A dashboard.

fetch_rates() pulls live USD exchange rates and caches them; to_usd()
converts a single deal-log value string to a USD-formatted string for
display. Never mutates deal_log.csv -- conversion happens in memory at
dashboard-generation time only.
"""

import json
import os
import re
import sys

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
RATES_PATH = os.path.join(_DIR, "exchange_rates.json")
API_URL = "https://open.er-api.com/v6/latest/USD"
REQUEST_TIMEOUT = 10

# Last-resort table if there's no live rate and no cached file either.
# Approximate; only ever used when both the API and the cache are unavailable.
_HARDCODED_FALLBACK_RATES = {
    "USD": 1.0,
    "GBP": 1.33,
    "EUR": 1.09,
    "CAD": 0.73,
    "SGD": 0.74,
    "CNY": 0.14,
    "NOK": 0.096,
    "INR": 0.0117,
}


def fetch_rates() -> dict:
    """Return a {currency_code: usd_per_unit} map, refreshed from the live API.

    The API reports USD-to-foreign quotes (1 USD = X foreign); this inverts
    each to foreign-to-USD (1 foreign unit = 1/X USD) so to_usd() can just
    multiply. Saves the result to exchange_rates.json. On any failure, falls
    back to the last saved cache, then a hardcoded table -- a rate lookup
    must never crash the pipeline run.
    """
    try:
        resp = requests.get(API_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        quotes = data["rates"]  # USD -> foreign
        rates = {"USD": 1.0}
        for code, quote in quotes.items():
            if code == "USD" or not quote:
                continue
            rates[code] = 1.0 / quote
        with open(RATES_PATH, "w", encoding="utf-8") as f:
            json.dump(rates, f, indent=2)
        return rates
    except Exception as e:  # noqa: BLE001
        print(f"  ! currency_module: live rate fetch failed ({e}); using fallback",
              file=sys.stderr)
        if os.path.isfile(RATES_PATH):
            try:
                with open(RATES_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return dict(_HARDCODED_FALLBACK_RATES)


# ----------------------------------------------------------------------------
# to_usd
# ----------------------------------------------------------------------------

# Checked in order -- multi-character markers before bare "$", and within
# that, markers that are substrings of another marker (S$ is a substring of
# US$) are ordered so the more specific one is tried first.
_CURRENCY_MARKERS = [
    ("US$", "USD"),
    ("C$", "CAD"),
    ("CAD", "CAD"),
    ("S$", "SGD"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("₹", "INR"),
    ("Rs", "INR"),
    ("RMB", "CNY"),
    ("NOK", "NOK"),
    ("$", "USD"),
]

_NOISE_PATTERN = re.compile(
    r"~|\+|\bup to\b|\bapprox\.?\b|\babout\b|\bover\b|\baround\b",
    re.IGNORECASE,
)

# Longest/most-specific alternatives first so "billion" wins over a bare "b",
# "lacs"/"lakh" aren't swallowed by a shorter neighbor, etc.
_MAGNITUDE_PATTERN = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*"
    r"(billion|bn|million|mn|thousand|crore|lakh|lacs|lac|[bmk])\b",
    re.IGNORECASE,
)

_MAGNITUDE_SCALE = {
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
    "crore": 1e7,
    "lakh": 1e5, "lacs": 1e5, "lac": 1e5,
}


def _detect_currency(v: str) -> str | None:
    for marker, code in _CURRENCY_MARKERS:
        if marker in v:
            return code
    return None


def _format_usd(amount: float) -> str:
    if amount >= 1e9:
        return f"${amount / 1e9:.1f}B"
    if amount >= 1e6:
        return f"${amount / 1e6:.0f} M"
    return f"${amount / 1e3:.0f}K"


def to_usd(value_str: str, rates: dict) -> str:
    """Convert a single deal-log value string to a USD-formatted display string.

    Rows already in USD are reformatted but not converted. Blank values stay
    blank. Anything that can't be confidently parsed is returned UNCHANGED --
    this never guesses, never returns zero, and never silently mangles a
    value it doesn't understand (e.g. per-share prices, ranges, non-monetary
    text all pass through untouched).
    """
    if not value_str or not value_str.strip():
        return value_str

    original = value_str
    cleaned = _NOISE_PATTERN.sub(" ", value_str)

    currency = _detect_currency(cleaned)
    if currency is None:
        return original

    m = _MAGNITUDE_PATTERN.search(cleaned)
    if not m:
        return original

    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return original

    scale = _MAGNITUDE_SCALE.get(m.group(2).lower())
    if scale is None:
        return original

    native_amount = number * scale

    rate = rates.get(currency)
    if rate is None:
        return original

    usd_amount = native_amount * rate
    return _format_usd(usd_amount)
