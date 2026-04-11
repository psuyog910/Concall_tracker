"""
Stock Concall Monitoring Agent
==============================
Scrapes Screener.in for new earnings concall transcripts,
summarizes them with Gemini, and sends Telegram alerts.
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

try:
    import pypdf
except ImportError:
    pypdf = None
    
try:
    import markdown
except ImportError:
    markdown = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

import google.generativeai as genai

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STOCKS_FILE = BASE_DIR / "stocks.txt"
STATE_FILE = BASE_DIR / "last_concall.json"
SUMMARIES_DIR = BASE_DIR / "summaries"
SUMMARIES_DIR.mkdir(exist_ok=True)
PROMPT_FILE = BASE_DIR / "prompt.md"

SCREENER_URL = "https://www.screener.in/company/{symbol}/"

# Forcefully strip all invisible/illegal characters (even inside the string)
GEMINI_API_KEY = re.sub(r'[^A-Za-z0-9_\-\.]', '', os.environ.get("GEMINI_API_KEY", ""))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# If True, saves summaries but DOES NOT send Telegram alerts.
SILENT_MODE = False

# If True, skips PDF/AI steps and only updates the date memory (Seed Mode).
# Controlled via SEED_ONLY env var — set it in GitHub Actions for the seeding run,
# then remove it. No code edits needed.
SEED_ONLY = os.environ.get("SEED_ONLY", "false").lower() == "true"

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

    if pypdf is None:
        log.error("pypdf not installed – cannot extract PDF text")
        return None

    try:
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
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

    # Load prompt from external file
    if PROMPT_FILE.exists():
        prompt_template = PROMPT_FILE.read_text(encoding="utf-8")
    else:
        log.warning("prompt.md not found - using simple fallback prompt")
        prompt_template = "Summarize this concall transcript briefly:\n\n{transcript}"

    prompt = prompt_template.format(transcript=transcript_text)

    # Try multiple models as fallback (quota is per-model on free tier)
    models_to_try = [
        "gemma-4-31b-it",          # Newest Gemma 4 (31B Dense)
        "gemma-4-26b-a4b-it",      # Newest Gemma 4 (26B MoE)
        "gemini-2.5-pro-exp-03-25",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    for model_name in models_to_try:
        log.info("Trying Gemini model: %s", model_name)
        model = genai.GenerativeModel(
            model_name,
            generation_config={"max_output_tokens": 1000, "temperature": 0.2}
        )
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = model.generate_content(prompt)
                summary = response.text
                
                # Strip out <think> tags if model includes reasoning
                summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()
                
                # Strip out any preamble before the first expected header
                # We check for exact match of the first section to ensure we don't include prompt leaking or drafting text
                financial_header = "## 📊 Financial Performance"
                if financial_header in summary:
                    summary = financial_header + summary.split(financial_header, 1)[1]
                elif "## 📊" in summary:
                    # Fallback just in case
                    summary = "## 📊" + summary.split("## 📊", 1)[1]
                
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

def extract_tone(markdown_text: str) -> str:
    """Extract the overall tone from the markdown summary."""
    # Pattern 1: Same line (Overall tone: **Bullish**)
    match = re.search(r"Overall tone:\s*\*\*?(.*?)\*\*?", markdown_text, re.IGNORECASE)
    if match:
        return match.group(1).split(".")[0].strip() # Clean up leading dots if any
    
    # Pattern 2: Header followed by bold text (## Overall Tone ... \n\n **Bullish**)
    match = re.search(r"## Overall Tone.*?\n+\s*\*\*?(.*?)\*\*?", markdown_text, re.IGNORECASE | re.DOTALL)
    if match:
        # Avoid taking the whole paragraph if it's long
        tone_str = match.group(1).split(".")[0].strip()
        if len(tone_str) < 50:
            return tone_str
            
    return "Neutral"


def generate_summary_pdf(symbol: str, date_raw: str, markdown_text: str) -> Optional[Path]:
    """Render the markdown summary to a sleek dark-themed PDF document."""
    if not markdown or not sync_playwright:
        log.warning("markdown or playwright not installed – skipping PDF generation")
        return None

    log.info("Generating sleek PDF summary for %s...", symbol)
    
    # Convert MD to HTML
    content_html = markdown.markdown(markdown_text, extensions=['extra', 'sane_lists', 'tables'])
    
    # Premium Sleek/Dark Template
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
            
            :root {{
                --bg: #0f172a;
                --card: #1e293b;
                --accent: #38bdf8;
                --text: #f1f5f9;
                --text-muted: #94a3b8;
                --border: rgba(255, 255, 255, 0.1);
            }}
            
            body {{
                background: var(--bg);
                color: var(--text);
                font-family: 'Inter', -apple-system, sans-serif;
                margin: 0;
                padding: 20px;
                line-height: 1.6;
                font-size: 16px;
            }}
            
            .container {{
                max-width: 100%;
                background: var(--card);
                border-radius: 12px;
                padding: 30px;
                box-sizing: border-box;
            }}
            
            .header {{
                border-bottom: 1px solid var(--border);
                margin-bottom: 20px;
                padding-bottom: 15px;
            }}
            
            .badge {{
                display: inline-block;
                background: rgba(56, 189, 248, 0.1);
                color: var(--accent);
                padding: 6px 14px;
                border-radius: 99px;
                font-size: 13px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 15px;
            }}
            
            h1 {{
                margin: 0;
                font-size: 30px;
                font-weight: 700;
                background: linear-gradient(to right, #fff, #94a3b8);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            
            .meta {{
                color: var(--text-muted);
                font-size: 14px;
                margin-top: 8px;
            }}
            
            h2 {{
                color: var(--accent);
                font-size: 22px;
                margin-top: 35px;
                margin-bottom: 20px;
                display: flex;
                align-items: center;
                gap: 10px;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            
            ul {{
                padding-left: 24px;
                margin: 0;
            }}
            
            li {{
                margin-bottom: 12px;
                padding-left: 4px;
            }}
            
            strong {{
                color: #fff;
                font-weight: 600;
            }}
            
            code {{
                background: rgba(0,0,0,0.3);
                padding: 2px 6px;
                border-radius: 4px;
                font-family: monospace;
                font-size: 0.9em;
            }}
            
            hr {{
                border: 0;
                border-top: 1px solid var(--border);
                margin: 30px 0;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                margin: 25px 0;
                font-size: 15px;
                background: rgba(255, 255, 255, 0.02);
                border-radius: 8px;
                overflow: hidden;
            }}

            th, td {{
                padding: 14px 16px;
                text-align: left;
                border-bottom: 1px solid var(--border);
            }}

            th {{
                background: rgba(56, 189, 248, 0.08);
                color: var(--accent);
                font-weight: 600;
            }}

            .footer-watermark {{
                margin-top: 40px;
                text-align: center;
                font-size: 12px;
                color: var(--text-muted);
                opacity: 0.5;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="badge">Equity Research Summary</div>
                <h1>{symbol}</h1>
                <div class="meta">Earnings Call Transcript • {date_raw}</div>
            </div>
            
            <div class="content">
                {content_html}
            </div>

            <div class="footer-watermark">
                AI Generated Research • Screener.in
            </div>
        </div>
    </body>
    </html>
    """

    output_path = SUMMARIES_DIR / f"{symbol}_Summary.pdf"
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # Set a narrower viewport for better mobile readability when converted to PDF
            page = browser.new_page(viewport={'width': 800, 'height': 1200})
            page.set_content(html_template)
            # Wait for any webfonts to load
            page.wait_for_timeout(1000)
            
            # Generate PDF
            page.pdf(path=str(output_path), print_background=True, width="800px", margin={"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"})
                
            browser.close()
            log.info("PDF saved to %s", output_path)
            return output_path
    except Exception as exc:
        log.error("Failed to generate PDF: %s", exc)
        return None


def format_telegram_html(text: str) -> str:
    """Convert AI markdown summary to Telegram-compatible HTML safely with better spacing."""
    # 1. Escape HTML special chars (must do this first!)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 2. Convert headers (## Header) to bold and add significant spacing
    text = re.sub(r'(?m)^#{1,3}\s*(.*?)\s*$', r'\n\n<b>\1</b>', text)
    
    # 3. Convert **bold** to <b>bold</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # 4. Convert standard bullet points to a nice unicode bullet with a newline buffer
    text = re.sub(r'(?m)^[-*]\s+', r'\n▪️ ', text)
    
    # 5. Clean up any accidental triple-newlines created by the above logic
    text = re.sub(r'\n{3,}', r'\n\n', text)
    
    return text.strip()

def send_telegram(symbol: str, date_raw: str, summary: str, pdf_url: str = "") -> bool:
    """Send a Telegram notification (text or PDF fallback)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set – skipping notification")
        return False

    # Metadata for caption
    tone = extract_tone(summary)
    
    # Decide if we need a PDF (length-based)
    use_pdf = len(summary) > 3000
    pdf_path = None
    
    if use_pdf:
        log.info("Summary too long (%d chars) – switching to PDF document", len(summary))
        pdf_path = generate_summary_pdf(symbol, date_raw, summary)
        
        if not pdf_path:
            log.warning("PDF generation failed, falling back to truncated text")
            use_pdf = False

    if use_pdf and pdf_path:
        # Send Document (PDF)
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        
        caption = (
            f"🏢 <b>Stock:</b> <code>{symbol}</code>\n"
            f"📅 <b>Date:</b> {date_raw}\n"
            f"🎯 <b>Tone:</b> {tone}\n"
            f"🔗 <a href='{pdf_url or f'https://www.screener.in/company/{symbol}/'}'>View Concall Transcript</a>"
        )
        
        files = {
            "document": open(pdf_path, "rb")
        }
            
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }
        
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(url, data=payload, files=files, timeout=40)
                if resp.status_code == 200:
                    log.info("Telegram PDF Document sent for %s", symbol)
                    return True
                else:
                    log.warning("Telegram PDF attempt %d/%d – status %d: %s", attempt, MAX_RETRIES, resp.status_code, resp.text)
            except requests.RequestException as exc:
                log.warning("Telegram PDF attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            time.sleep(RETRY_DELAY)
        return False

    else:
        # Send Text Message (Original logic)
        msg_suffix = ""
        if len(summary) > 3000:
             msg_suffix = "\n\n... [Truncated - PDF Generation Failed]" if use_pdf else "\n\n... [Truncated]"
        
        short_summary = summary[:3000] + msg_suffix
        html_summary = format_telegram_html(short_summary)

        message = (
            f"🚨 <b>NEW CONCALL DETECTED</b>\n\n"
            f"🏢 <b>Stock:</b> <code>{symbol}</code>\n"
            f"📅 <b>Date:</b> {date_raw}\n"
            f"🎯 <b>Tone:</b> {tone}\n\n"
            f"🤖 <b>AI Summary:</b>\n"
            f"<blockquote expandable>{html_summary}</blockquote>\n\n"
            f"🔗 <a href='{pdf_url or f'https://www.screener.in/company/{symbol}/'}'>View Concall Transcript</a>"
        )

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    log.info("Telegram text alert sent for %s", symbol)
                    return True
                log.warning("Telegram attempt %d/%d – status %d", attempt, MAX_RETRIES, resp.status_code)
            except requests.RequestException as exc:
                log.warning("Telegram attempt failed: %s", exc)
            time.sleep(RETRY_DELAY)
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

    # --- Quick Seed Mode ---
    if SEED_ONLY:
        state[symbol] = current_date
        log.info("Seed mode: Memory updated for %s \u2192 %s (Skipped AI)", symbol, current_date)
        return True

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

    # Step 6: Send Telegram alert (if not in silent mode)
    if not SILENT_MODE:
        send_telegram(symbol, latest["date_raw"], summary, pdf_url=latest["pdf_url"])
    else:
        log.info("Silent mode is ON: skipping Telegram alert for %s", symbol)

    # Step 7: Update state
    state[symbol] = current_date
    log.info("State updated: %s → %s", symbol, current_date)

    return True


def main() -> None:
    """Entry point – handle arguments and loop through all stocks."""
    parser = argparse.ArgumentParser(description="Stock Concall Monitor")
    parser.add_argument("--test", type=str, help="Path to a markdown file to test sending a summary")
    parser.add_argument("--symbol", type=str, default="TEST_STOCK", help="Symbol for the test run")
    parser.add_argument("--date", type=str, default="Now", help="Date for the test run")
    parser.add_argument("--pdf_url", type=str, default="", help="PDF URL for the test run")
    args = parser.parse_args()

    if args.test:
        test_path = Path(args.test)
        if not test_path.exists():
            log.error("Test file not found: %s", test_path)
            sys.exit(1)
        
        summary_content = test_path.read_text(encoding="utf-8")
        
        # Auto-extract date if not explicitly provided or is "Now"
        date_to_use = args.date
        if date_to_use.lower() == "now":
            date_match = re.search(r"\*\*Date:\*\*\s*(.*)", summary_content)
            if date_match:
                date_to_use = date_match.group(1).strip()
                log.info("Detected actual date from file: %s", date_to_use)
        
        log.info("🚀 Running Test Mode: sending %s to Telegram...", test_path.name)
        success = send_telegram(args.symbol, date_to_use, summary_content, pdf_url=args.pdf_url)
        if success:
            log.info("✅ Test run successful.")
        else:
            log.error("❌ Test run failed.")
        return

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
