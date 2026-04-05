"""
Stock Concall Monitoring Agent
==============================
Scrapes Screener.in for new earnings concall transcripts,
summarizes them with Gemini, and sends Telegram alerts.
"""

import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

import google.generativeai as genai

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STOCKS_FILE = BASE_DIR / "stocks.txt"
STATE_FILE = BASE_DIR / "last_concall.json"
SUMMARIES_DIR = BASE_DIR / "summaries"
SUMMARIES_DIR.mkdir(exist_ok=True)

SCREENER_URL = "https://www.screener.in/company/{symbol}/"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("concall-monitor")

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})


def _get(url: str, **kwargs) -> requests.Response:
    """GET with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d for %s failed: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load last-seen concall dates from JSON."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt state file – starting fresh")
    return {}


def save_state(state: dict) -> None:
    """Persist state to JSON."""
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("State saved → %s", STATE_FILE)


# ---------------------------------------------------------------------------
# Stock list
# ---------------------------------------------------------------------------

def load_stocks() -> list[str]:
    """Read stock symbols from stocks.txt (one per line)."""
    if not STOCKS_FILE.exists():
        log.error("stocks.txt not found at %s", STOCKS_FILE)
        return []
    symbols = [
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    log.info("Loaded %d stock(s): %s", len(symbols), ", ".join(symbols))
    return symbols


# ---------------------------------------------------------------------------
# Scraper – find latest concall transcript
# ---------------------------------------------------------------------------

def _parse_concall_date(raw: str) -> Optional[str]:
    """
    Parse a concall date like 'Feb 2026' into ISO format 'YYYY-MM-DD'.
    Day defaults to 1 when only month/year is given.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = dateparser.parse(raw, dayfirst=True, default=datetime(2000, 1, 1))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def find_latest_transcript(symbol: str) -> Optional[dict]:
    """
    Scrape Screener.in for the latest concall transcript link and date.

    Returns dict with keys: symbol, date, date_raw, pdf_url
    or None if no transcript found.
    """
    url = SCREENER_URL.format(symbol=symbol)
    log.info("Fetching %s", url)

    try:
        resp = _get(url)
    except RuntimeError:
        log.error("Could not fetch Screener page for %s", symbol)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy: find all <a> tags with class "concall-link" whose text is "Transcript"
    # and that have an href pointing to a PDF.
    transcript_links = []

    for a_tag in soup.find_all("a", class_="concall-link"):
        text = a_tag.get_text(strip=True)
        if text.lower() != "transcript":
            continue
        href = a_tag.get("href", "")
        if not href:
            continue

        # Walk up to the parent row / list-item to find the date text.
        parent = a_tag.find_parent("li") or a_tag.find_parent("div")
        if parent is None:
            continue

        # The date is usually in a plain text node or a child element
        # before the links. We extract all text and look for a month-year pattern.
        parent_text = parent.get_text(" ", strip=True)
        # Match patterns like "Feb 2026", "Nov 2025", "Jan 2023"
        date_match = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}",
            parent_text,
        )
        if date_match:
            date_raw = date_match.group(0)
            date_iso = _parse_concall_date(date_raw)
        else:
            date_raw = ""
            date_iso = None

        transcript_links.append({
            "symbol": symbol,
            "date": date_iso,
            "date_raw": date_raw,
            "pdf_url": href,
        })

    if not transcript_links:
        log.info("No transcript found for %s", symbol)
        return None

    # Sort by date descending – the latest transcript is first.
    transcript_links.sort(key=lambda x: x["date"] or "", reverse=True)
    latest = transcript_links[0]
    log.info(
        "Latest transcript for %s: %s (%s)",
        symbol, latest["date_raw"], latest["pdf_url"],
    )
    return latest


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_url: str) -> Optional[str]:
    """Download a PDF and extract its text content."""
    log.info("Downloading PDF: %s", pdf_url)
    try:
        resp = _get(pdf_url)
    except RuntimeError:
        log.error("Failed to download PDF: %s", pdf_url)
        return None

    if PyPDF2 is None:
        log.error("PyPDF2 not installed – cannot extract PDF text")
        return None

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(resp.content))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        full_text = "\n".join(pages_text)
        log.info("Extracted %d characters from %d pages", len(full_text), len(reader.pages))
        return full_text if full_text.strip() else None
    except Exception as exc:
        log.error("PDF extraction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Gemini summarisation
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """\
You are a professional equity research analyst.

Summarize the following earnings concall transcript into:

## Key Highlights

## Growth Drivers

## Management Guidance

## Risks / Concerns

## Capex / Expansion

## Order Book / Demand

## Margins Commentary

## Analyst Q&A Key Points

## Red Flags (if any)

## Overall Tone (Bullish / Neutral / Bearish)

Keep summary concise and professional.

Transcript:
{transcript}
"""


def summarise_transcript(transcript_text: str) -> Optional[str]:
    """Send transcript to Gemini and return the summary."""
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set – skipping summarisation")
        return None

    genai.configure(api_key=GEMINI_API_KEY)

    # Truncate if too long (Gemini free tier context window)
    max_chars = 900_000  # ~225k tokens approx
    if len(transcript_text) > max_chars:
        log.warning("Transcript truncated from %d to %d chars", len(transcript_text), max_chars)
        transcript_text = transcript_text[:max_chars]

    prompt = SUMMARY_PROMPT.format(transcript=transcript_text)

    # Try multiple models as fallback (quota is per-model on free tier)
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]

    for model_name in models_to_try:
        log.info("Trying Gemini model: %s", model_name)
        model = genai.GenerativeModel(model_name)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = model.generate_content(prompt)
                summary = response.text
                log.info("Gemini summary generated with %s – %d chars", model_name, len(summary))
                return summary
            except Exception as exc:
                log.warning(
                    "Model %s attempt %d/%d failed: %s",
                    model_name, attempt, MAX_RETRIES, exc,
                )
                if "429" in str(exc) or "quota" in str(exc).lower():
                    log.info("Quota exhausted for %s, trying next model…", model_name)
                    break  # Skip remaining retries, move to next model
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)

    log.error("Gemini summarisation failed across all models")
    return None


# ---------------------------------------------------------------------------
# Save markdown summary
# ---------------------------------------------------------------------------

def save_summary(symbol: str, date_iso: str, date_raw: str, summary: str) -> Path:
    """Write the summary to summaries/SYMBOL_YYYY_MM_DD.md."""
    date_part = date_iso.replace("-", "_") if date_iso else "unknown"
    filename = f"{symbol}_{date_part}.md"
    filepath = SUMMARIES_DIR / filename

    header = (
        f"# {symbol} Concall Summary\n\n"
        f"**Date:** {date_raw}\n\n"
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"---\n\n"
    )
    filepath.write_text(header + summary, encoding="utf-8")
    log.info("Summary saved → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def send_telegram(symbol: str, date_raw: str, summary: str) -> bool:
    """Send a Telegram message when a new concall is detected."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set – skipping notification")
        return False

    # Truncate summary for Telegram (4096 char limit)
    short_summary = summary[:3000] if len(summary) > 3000 else summary

    message = (
        f"🚨 *New Concall Detected*\n\n"
        f"*Stock:* `{symbol}`\n"
        f"*Date:* {date_raw}\n\n"
        f"*AI Summary:*\n\n"
        f"{short_summary}\n\n"
        f"🔗 [View on Screener](https://www.screener.in/company/{symbol}/)"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                log.info("Telegram alert sent for %s", symbol)
                return True
            else:
                log.warning(
                    "Telegram attempt %d/%d – status %d: %s",
                    attempt, MAX_RETRIES, resp.status_code, resp.text,
                )
        except requests.RequestException as exc:
            log.warning("Telegram attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    log.error("Telegram notification failed after %d attempts", MAX_RETRIES)
    return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_stock(symbol: str, state: dict) -> bool:
    """
    Process a single stock. Returns True if a new concall was found and handled.
    """
    log.info("=" * 60)
    log.info("Processing: %s", symbol)
    log.info("=" * 60)

    # Step 1: Find latest transcript
    latest = find_latest_transcript(symbol)
    if latest is None:
        log.info("No transcript available for %s – skipping", symbol)
        return False

    # Step 2: Check if it's new
    last_date = state.get(symbol)
    current_date = latest["date"]

    if last_date and current_date and current_date <= last_date:
        log.info(
            "No new concall for %s (latest: %s, stored: %s)",
            symbol, current_date, last_date,
        )
        return False

    log.info("🆕 NEW concall detected for %s: %s", symbol, latest["date_raw"])

    # Step 3: Download and extract transcript PDF
    transcript_text = extract_pdf_text(latest["pdf_url"])
    if not transcript_text:
        log.error("Could not extract transcript text for %s – skipping", symbol)
        return False

    # Step 4: Summarise with Gemini
    summary = summarise_transcript(transcript_text)
    if not summary:
        log.error("Summarisation failed for %s – skipping", symbol)
        return False

    # Step 5: Save markdown summary
    save_summary(symbol, current_date, latest["date_raw"], summary)

    # Step 6: Send Telegram alert
    send_telegram(symbol, latest["date_raw"], summary)

    # Step 7: Update state
    state[symbol] = current_date
    log.info("State updated: %s → %s", symbol, current_date)

    return True


def main() -> None:
    """Entry point – loop through all stocks."""
    log.info("🚀 Concall Monitor starting…")

    stocks = load_stocks()
    if not stocks:
        log.error("No stocks to process – exiting")
        sys.exit(1)

    state = load_state()
    new_count = 0

    for symbol in stocks:
        try:
            if process_stock(symbol, state):
                new_count += 1
        except Exception as exc:
            log.error("Unhandled error for %s: %s", symbol, exc, exc_info=True)

        # Be polite to Screener.in
        time.sleep(2)

    save_state(state)

    log.info("=" * 60)
    log.info("✅ Done. %d new concall(s) found out of %d stock(s).", new_count, len(stocks))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
