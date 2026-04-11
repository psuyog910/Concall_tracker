"""
Quarterly results monitor: detect latest Screener.in quarterly PDF, extract metrics with Gemini,
render a ResultRadar-style PNG, notify Telegram.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

import google.generativeai as genai

# Import shared scraper utilities (scraper must be loaded first in normal runs)
from scraper import (
    ALERTS_SENT_THIS_RUN,
    BASE_DIR,
    GEMINI_API_KEY,
    MAX_RETRIES,
    RETRY_DELAY,
    SILENT_MODE,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    _get,
    extract_pdf_text,
    log,
)

QUARTER_LINK_RE = re.compile(
    r"/company/source/quarter/(\d+)/(\d{1,2})/(\d{4})/?",
    re.IGNORECASE,
)
QUARTERLY_STATE_FILE = BASE_DIR / "last_quarterly.json"
QUARTERLY_CARDS_DIR = BASE_DIR / "quarterly_cards"
QUARTERLY_PROMPT_FILE = BASE_DIR / "quarterly_prompt.md"
SUMMARIES_DIR = BASE_DIR / "summaries"

SEED_QUARTERLY_ONLY = os.environ.get("SEED_QUARTERLY_ONLY", "false").lower() == "true"

QUARTERLY_CARDS_DIR.mkdir(exist_ok=True)
SUMMARIES_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_quarterly_state() -> dict[str, str]:
    if QUARTERLY_STATE_FILE.exists():
        try:
            data = json.loads(QUARTERLY_STATE_FILE.read_text(encoding="utf-8"))
            return {str(k).upper(): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt quarterly state – starting fresh")
    return {}


def save_quarterly_state(state: dict[str, str]) -> None:
    temp = QUARTERLY_STATE_FILE.with_suffix(".json.tmp")
    try:
        temp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(QUARTERLY_STATE_FILE)
        log.info("Quarterly state saved → %s", QUARTERLY_STATE_FILE)
    except Exception as exc:
        log.error("Failed to save quarterly state: %s", exc)
        if temp.exists():
            temp.unlink()


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

def find_latest_quarterly_pdf(soup: BeautifulSoup) -> Optional[dict[str, Any]]:
    """Pick latest quarter PDF link by calendar (year, month) from Screener HTML."""
    best: Optional[tuple[int, int, str]] = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = QUARTER_LINK_RE.search(href)
        if not m:
            continue
        _cid, month_s, year_s = m.groups()
        month, year = int(month_s), int(year_s)
        if href.startswith("/"):
            href = "https://www.screener.in" + href
        if not href.endswith("/"):
            href = href + "/"
        cand = (year, month, href)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if not best:
        return None
    year, month, url = best
    period_key = f"{year}-{month:02d}"
    log.info("Latest quarterly PDF slot: %s → %s", period_key, url)
    return {"period_key": period_key, "pdf_url": url, "year": year, "month": month}


def scrape_company_heading(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return ""


def page_text_snippet(soup: BeautifulSoup, limit: int = 10000) -> str:
    text = soup.get_text("\n", strip=True)
    return text[:limit]


# ---------------------------------------------------------------------------
# Gemini → JSON payload
# ---------------------------------------------------------------------------

def _parse_json_loose(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


def build_quarterly_payload(
    symbol: str,
    company_name: str,
    pdf_text: str,
    page_snippet: str,
) -> Optional[dict[str, Any]]:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set – cannot build quarterly card")
        return None

    if QUARTERLY_PROMPT_FILE.exists():
        template = QUARTERLY_PROMPT_FILE.read_text(encoding="utf-8")
    else:
        log.error("quarterly_prompt.md missing")
        return None

    prompt = template.format(
        symbol=symbol,
        company_name=company_name or symbol,
        pdf_text=pdf_text[:120000],
        page_snippet=page_snippet[:8000],
    )

    genai.configure(api_key=GEMINI_API_KEY)
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
        log.info("Quarterly JSON: trying model %s", model_name)
        try:
            model = genai.GenerativeModel(
                model_name,
                generation_config={
                    "temperature": 0.15,
                    "response_mime_type": "application/json",
                },
            )
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = model.generate_content(prompt)
                    payload = _parse_json_loose(response.text)
                    if not isinstance(payload, dict):
                        continue
                    rows = payload.get("rows")
                    if not isinstance(rows, list) or len(rows) < 3:
                        log.warning("Gemini returned unusable rows; retrying")
                        continue
                    payload["_model"] = model_name
                    return payload
                except Exception as exc:
                    log.warning(
                        "Quarterly JSON model %s attempt %d/%d: %s",
                        model_name,
                        attempt,
                        MAX_RETRIES,
                        exc,
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * attempt)
        except Exception as exc:
            log.warning("Quarterly model %s init/generate failed: %s", model_name, exc)

    log.error("Quarterly JSON extraction failed for all models")
    return None


# ---------------------------------------------------------------------------
# ResultRadar-style PNG
# ---------------------------------------------------------------------------

def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _tone_color(tone: str) -> tuple[int, int, int]:
    t = (tone or "neutral").lower()
    if t == "neg":
        return (248, 113, 113)
    if t == "pos":
        return (125, 211, 252)
    return (148, 163, 184)


def render_result_radar_card(
    symbol: str,
    payload: dict[str, Any],
    out_path: Path,
) -> bool:
    W, H = 920, 1280
    BG = (26, 38, 63)
    HEADER_ROW = (20, 28, 48)
    TABLE_HEAD = (30, 41, 68)
    ROW_A = (24, 34, 56)
    ROW_B = (22, 32, 52)
    FOOTER_BAR = (30, 41, 59)
    LIME = (190, 242, 100)
    ORANGE = (251, 146, 60)
    WHITE = (241, 245, 249)
    GREY = (148, 163, 184)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Watermark
    wm_font = _load_font(18)
    for x in range(-40, W, 180):
        for y in range(0, H, 140):
            draw.text((x, y), "ResultRadar", fill=(36, 50, 78), font=wm_font)

    pad = 32
    y = 24

    f_title = _load_font(15)
    f_q = _load_font(22)
    f_sym = _load_font(34)
    f_sub = _load_font(18)
    f_rate = _load_font(22)
    f_small = _load_font(13)
    f_th = _load_font(14)
    f_td = _load_font(14)
    f_foot = _load_font(26)
    f_dis = _load_font(11)

    quarter_label = str(payload.get("quarter_label") or "—")
    display_sym = str(payload.get("display_symbol") or symbol).upper()
    company_line = str(payload.get("company_name") or payload.get("company_line") or "")
    rating = str(payload.get("rating") or "—")

    draw.text((pad, y), "ResultRadar by QuantPilotLabs", fill=WHITE, font=f_title)
    tw = draw.textlength(quarter_label, font=f_q)
    draw.text((W - pad - tw, y - 2), quarter_label, fill=LIME, font=f_q)
    y += 36

    draw.text((pad, y), display_sym, fill=WHITE, font=f_sym)
    y_sym = y
    rate_text = f"Rating: {rating}"
    draw.text((W - pad - draw.textlength(rate_text, font=f_rate), y_sym + 4), rate_text, fill=ORANGE, font=f_rate)
    y += 42
    if company_line:
        draw.text((pad, y), company_line, fill=GREY, font=f_sub)
    unit_note = "₹ Cr | EPS in ₹"
    draw.text((W - pad - draw.textlength(unit_note, font=f_small), y + 2), unit_note, fill=GREY, font=f_small)
    y += 36

    # Table
    col_curr = str(payload.get("col_current") or "Curr")
    col_pq = str(payload.get("col_prev_q") or "Prev Q")
    col_py = str(payload.get("col_prev_y") or "YoY Q")

    headers = ["Metric", "QoQ", "YoY", col_curr, col_pq, col_py]
    col_x = [pad, 200, 290, 380, 520, 660]
    row_h = 36
    head_h = 32

    draw.rectangle([pad - 8, y, W - pad + 8, y + head_h], fill=TABLE_HEAD)
    for i, htxt in enumerate(headers):
        draw.text((col_x[i], y + 7), htxt, fill=WHITE, font=f_th)
    y += head_h

    rows: list[dict[str, Any]] = payload.get("rows") or []
    for idx, row in enumerate(rows[:5]):
        bg = ROW_A if idx % 2 == 0 else ROW_B
        draw.rectangle([pad - 8, y, W - pad + 8, y + row_h], fill=bg)
        metric = str(row.get("metric", "—"))
        qoq = str(row.get("qoq", "—"))
        yoy = str(row.get("yoy", "—"))
        vc = str(row.get("v_curr", row.get("c", "—")))
        vpq = str(row.get("v_prev_q", row.get("pq", "—")))
        vpy = str(row.get("v_prev_y", row.get("py", "—")))
        draw.text((col_x[0], y + 9), metric[:22], fill=WHITE, font=f_td)
        draw.text((col_x[1], y + 9), qoq, fill=_tone_color(str(row.get("qoq_tone", "neutral"))), font=f_td)
        draw.text((col_x[2], y + 9), yoy, fill=_tone_color(str(row.get("yoy_tone", "neutral"))), font=f_td)
        draw.text((col_x[3], y + 9), vc, fill=WHITE, font=f_td)
        draw.text((col_x[4], y + 9), vpq, fill=WHITE, font=f_td)
        draw.text((col_x[5], y + 9), vpy, fill=WHITE, font=f_td)
        y += row_h

    y += 20

    # Footer metrics
    fh = 72
    draw.rectangle([0, y, W, y + fh], fill=FOOTER_BAR)
    cmp_s = str(payload.get("footer_cmp") or "—")
    eps_s = str(payload.get("footer_fwd_eps") or "—")
    pe_s = str(payload.get("footer_fwd_pe") or "—")

    block_w = W // 3
    for i, (label, value, is_lime) in enumerate(
        [
            ("CMP", cmp_s, True),
            ("FWD EPS", eps_s, False),
            ("Forward PE", pe_s, True),
        ]
    ):
        bx = i * block_w
        color = LIME if is_lime else WHITE
        draw.text((bx + 24, y + 10), label, fill=GREY, font=f_small)
        draw.text((bx + 24, y + 28), value, fill=color, font=f_foot)
        if i < 2:
            draw.line([(bx + block_w, y + 12), (bx + block_w, y + fh - 12)], fill=(55, 65, 85), width=1)
    y += fh + 16

    ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d-%b-%Y %H:%M IST")
    left = "Snapshot from publicly available filings. Verify with official filing."
    mid = "Source: NSE/BSE filing"
    draw.text((pad, y), left, fill=GREY, font=f_dis)
    mw = draw.textlength(mid, font=f_dis)
    draw.text(((W - mw) // 2, y), mid, fill=GREY, font=f_dis)
    rw = draw.textlength(ist, font=f_dis)
    draw.text((W - pad - rw, y), ist, fill=GREY, font=f_dis)

    try:
        img.save(out_path, format="PNG", optimize=True)
        log.info("Quarterly card saved → %s", out_path)
        return True
    except OSError as exc:
        log.error("Failed to save PNG: %s", exc)
        return False


def save_quarterly_summary_md(
    symbol: str,
    period_key: str,
    pdf_url: str,
    payload: dict[str, Any],
) -> Path:
    slug = period_key.replace("-", "_")
    path = SUMMARIES_DIR / f"{symbol}_quarterly_{slug}.md"
    summary = str(payload.get("summary_short") or "").strip()
    body = (
        f"# {symbol} Quarterly snapshot ({period_key})\n\n"
        f"**PDF (via Screener):** {pdf_url}\n\n"
        f"**Model:** {payload.get('_model', 'unknown')}\n\n"
        f"---\n\n{summary}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Telegram photo
# ---------------------------------------------------------------------------

def _escape_tg_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram_quarterly_photo(
    symbol: str,
    period_key: str,
    image_path: Path,
    caption: str,
    pdf_url: str,
) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured – skip quarterly photo")
        return False

    alert_key = ("qr", symbol.upper().strip(), period_key)
    if alert_key in ALERTS_SENT_THIS_RUN:
        log.warning("Duplicate quarterly Telegram for %s %s", symbol, period_key)
        return True

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    safe_caption = _escape_tg_html((caption or "")[:900])
    text = (
        f"📊 <b>NEW QUARTERLY RESULTS</b>\n"
        f"🏢 <code>{symbol}</code> · <b>{period_key}</b>\n\n"
        f"{safe_caption}\n\n"
        f"🔗 <a href=\"{pdf_url}\">Raw PDF (Screener)</a>"
    )
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": text,
        "parse_mode": "HTML",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(image_path, "rb") as f:
                resp = requests.post(
                    url,
                    data=payload,
                    files={"photo": f},
                    timeout=60,
                )
            if resp.status_code == 200:
                ALERTS_SENT_THIS_RUN.add(alert_key)
                log.info("Telegram photo sent for %s quarterly %s", symbol, period_key)
                return True
            log.warning(
                "Telegram photo %d/%d status %d: %s",
                attempt,
                MAX_RETRIES,
                resp.status_code,
                resp.text[:500],
            )
        except requests.RequestException as exc:
            log.warning("Telegram photo attempt failed: %s", exc)
        time.sleep(RETRY_DELAY)
    return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_quarterly_stock(symbol: str, state: dict[str, str], soup: BeautifulSoup) -> bool:
    """Returns True if a new quarter was processed (and state updated)."""
    sym = symbol.upper().strip()
    log.info("-" * 40)
    log.info("Quarterly check: %s", sym)

    latest = find_latest_quarterly_pdf(soup)
    if not latest:
        log.info("No quarterly PDF links for %s", sym)
        return False

    period_key = latest["period_key"]
    pdf_url = latest["pdf_url"]
    prev = state.get(sym)
    if prev and prev >= period_key:
        log.info("No new quarterly for %s (latest %s, stored %s)", sym, period_key, prev)
        return False

    log.info("🆕 NEW quarterly slot for %s: %s", sym, period_key)

    if SEED_QUARTERLY_ONLY:
        state[sym] = period_key
        log.info("SEED_QUARTERLY_ONLY: state → %s for %s", period_key, sym)
        return True

    pdf_text = extract_pdf_text(pdf_url)
    if not pdf_text:
        log.error("Could not read quarterly PDF for %s", sym)
        return False

    company_name = scrape_company_heading(soup)
    snippet = page_text_snippet(soup)
    payload = build_quarterly_payload(sym, company_name, pdf_text, snippet)
    if not payload:
        log.error("Quarterly AI payload failed for %s", sym)
        return False

    out_png = QUARTERLY_CARDS_DIR / f"{sym}_Q_{period_key.replace('-', '_')}.png"
    if not render_result_radar_card(sym, payload, out_png):
        return False

    save_quarterly_summary_md(sym, period_key, pdf_url, payload)

    if not SILENT_MODE:
        cap = str(payload.get("summary_short") or "")[:700]
        ok = send_telegram_quarterly_photo(sym, period_key, out_png, cap, pdf_url)
        if not ok:
            log.error("Telegram failed for quarterly %s – state not updated", sym)
            return False
    else:
        log.info("Silent mode: skip Telegram for quarterly %s", sym)

    state[sym] = period_key
    log.info("Quarterly state updated %s → %s", sym, period_key)
    return True
