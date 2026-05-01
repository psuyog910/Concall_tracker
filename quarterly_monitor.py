"""
Quarterly results monitor: detect latest Screener.in quarterly PDF, extract metrics with Gemini,
render a generic quarterly snapshot PNG, notify Telegram.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

import google.generativeai as genai

from config import (
    BASE_DIR,
    GEMINI_API_KEY,
    GEMINI_GEMMA_FALLBACK,
    GEMINI_MODEL_FALLBACKS,
    GEMINI_MODEL_PRIORITY,
    NVIDIA_MODELS,
    NVIDIA_API_KEY,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    log,
)

# Import shared scraper utilities
from scraper import (
    ALERTS_SENT_THIS_RUN,
    MAX_RETRIES,
    RETRY_DELAY,
    SILENT_MODE,
    extract_pdf_text_enriched,
    fetch_screener_soup_consolidated,
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

# Post-LLM: minimum share of numeric cells that must match PDF text (fuzzy).
QUARTERLY_PDF_MATCH_RATIO = float(os.environ.get("QUARTERLY_PDF_MATCH_RATIO", "0.65"))
# Telegram Bot API caption hard limit.
TELEGRAM_CAPTION_MAX = 1024
# Extra LLM prompt passes when PDF spot-check fails (same model).
MAX_QUARTERLY_VALIDATION_PASSES = min(3, max(1, int(os.environ.get("MAX_QUARTERLY_VALIDATION_PASSES", "2"))))

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

def _normalize_quarter_pdf_url(href: str) -> str:
    href = href.strip()
    if href.startswith("/"):
        href = "https://www.screener.in" + href
    if not href.endswith("/"):
        href = href + "/"
    return href


def _link_consolidated_score(anchor: Any) -> int:
    """
    Prefer links labelled or surrounded by 'consolidated' over 'standalone' when Screener lists both.
    """
    blob = ((anchor.get_text() or "") + " " + (anchor.get("href") or "")).lower()
    for par in (anchor.find_parent("tr"), anchor.find_parent("li"), anchor.parent):
        if par is not None:
            blob += " " + (par.get_text(" ", strip=True) or "").lower()[:800]
            break
    score = 0
    if "consolidated" in blob:
        score += 4
    if "standalone" in blob or "stand alone" in blob:
        score -= 3
    if "consolidate" in blob and "consolidated" not in blob:
        score += 1
    return score


def _iter_quarter_links(soup: Optional[BeautifulSoup]) -> list[tuple[int, int, str, Any]]:
    out: list[tuple[int, int, str, Any]] = []
    if soup is None:
        return out
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = QUARTER_LINK_RE.search(href)
        if not m:
            continue
        _cid, month_s, year_s = m.groups()
        month, year = int(month_s), int(year_s)
        url = _normalize_quarter_pdf_url(href)
        out.append((year, month, url, a))
    return out


def find_latest_quarterly_pdf(
    soup: BeautifulSoup,
    soup_consolidated: Optional[BeautifulSoup] = None,
) -> Optional[dict[str, Any]]:
    """
    Pick latest quarter PDF by calendar (year, month) from Screener HTML.
    Merges links from the main company page and /consolidated/ when provided.
    When multiple links exist for the same period, prefers consolidated-labelled anchors.
    """
    from collections import defaultdict

    scores: dict[str, int] = defaultdict(int)
    period_by_url: dict[str, tuple[int, int]] = {}
    for y, m, url, a in _iter_quarter_links(soup) + _iter_quarter_links(soup_consolidated):
        sc = _link_consolidated_score(a)
        if sc > scores[url]:
            scores[url] = sc
        period_by_url[url] = (y, m)

    if not period_by_url:
        return None

    best_period: Optional[tuple[int, int]] = None
    for y, m in period_by_url.values():
        if best_period is None or (y, m) > best_period:
            best_period = (y, m)
    assert best_period is not None
    by_period = [u for u, p in period_by_url.items() if p == best_period]
    best_url = max(by_period, key=lambda u: (scores.get(u, 0), u))

    year, month = period_by_url[best_url]
    period_key = f"{year}-{month:02d}"
    log.info(
        "Latest quarterly PDF slot: %s → %s (consolidated_score=%s)",
        period_key,
        best_url,
        scores.get(best_url, 0),
    )
    return {"period_key": period_key, "pdf_url": best_url, "year": year, "month": month}


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
#
# Gemma models (gemma-*-it) do not reliably honor Gemini's structured-output
# `response_mime_type: application/json`; the API often returns an empty body,
# which surfaces as json.loads(""). Use plain text + "JSON only" suffix for Gemma.
#
# Order: Gemini first for JSON, then Gemma 4. Deprecated 2.0 Flash* may show quota 0.
# Model IDs: https://ai.google.dev/gemini-api/docs/models
#
_JSON_ONLY_SUFFIX = (
    "\n\nOutput: reply with exactly one JSON object and nothing else "
    "(no markdown fences, no commentary)."
)

# (model_id, supports_application_json_mime)
# Priority: Gemini -> NVIDIA -> Gemini Fallbacks -> Gemma Fallbacks
_QUARTERLY_MODEL_TRIALS: tuple[tuple[str, bool], ...] = tuple(
    (m, True) for m in GEMINI_MODEL_PRIORITY
) + tuple((m, False) for m in NVIDIA_MODELS) + tuple(
    (m, True) for m in GEMINI_MODEL_FALLBACKS
) + tuple((m, False) for m in GEMINI_GEMMA_FALLBACK)


def _gemini_response_text(response: Any) -> str:
    """Robust text extraction (blocked / empty .text / multi-part)."""
    if response is None:
        return ""
    try:
        t = response.text
        if t and str(t).strip():
            return str(t).strip()
    except Exception:
        pass
    try:
        parts: list[str] = []
        for cand in response.candidates or []:
            content = getattr(cand, "content", None)
            if not content or not getattr(content, "parts", None):
                continue
            for part in content.parts:
                txt = getattr(part, "text", None)
                if txt:
                    parts.append(txt)
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _log_prompt_feedback(response: Any) -> None:
    try:
        fb = getattr(response, "prompt_feedback", None)
        if fb is not None:
            br = getattr(fb, "block_reason", None)
            if br:
                log.warning("Gemini prompt_feedback block_reason=%s (%s)", br, fb)
    except Exception:
        pass


def _parse_financial_scalar(s: Any) -> Optional[Decimal]:
    """Parse a cell like '1,234.5', '8.2%', '—' into Decimal (percent stored as the number, e.g. 8.2)."""
    if s is None:
        return None
    t = str(s).strip()
    if not t or t in ("—", "–", "-", "n/a", "N/A"):
        return None
    pct = t.endswith("%")
    if pct:
        t = t[:-1].strip()
    t = t.replace(",", "").replace("₹", "").strip()
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _pct_change_vs_prior(curr: Optional[Decimal], prior: Optional[Decimal]) -> Optional[Decimal]:
    if curr is None or prior is None:
        return None
    if prior == 0:
        return None
    return ((curr - prior) / abs(prior)) * Decimal(100)


def _fmt_signed_pct(d: Decimal) -> str:
    q = d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    sign = "+" if q >= 0 else ""
    return f"{sign}{q}%"


def _fmt_pp_or_bps_delta(curr: Optional[Decimal], prior: Optional[Decimal]) -> str:
    """Difference between two %-style figures; show as bps when near-integer, else pp."""
    if curr is None or prior is None:
        return "—"
    diff_pp = curr - prior
    if diff_pp == 0:
        return "0 bps"
    bps = diff_pp * Decimal(100)
    bps_r = bps.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if abs(bps - bps_r) < Decimal("0.01"):
        sign = "+" if bps_r >= 0 else ""
        return f"{sign}{int(bps_r)} bps"
    pp_q = diff_pp.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if pp_q >= 0 else ""
    return f"{sign}{pp_q}pp"


def _tone_from_pct_change(
    sense: str,
    curr: Optional[Decimal],
    prior: Optional[Decimal],
) -> str:
    if curr is None or prior is None:
        return "neutral"
    if curr == prior:
        return "neutral"
    higher = curr > prior
    good = (sense == "higher_better" and higher) or (sense == "lower_better" and not higher)
    return "pos" if good else "neg"


def _tone_from_pp_delta(
    sense: str,
    curr: Optional[Decimal],
    prior: Optional[Decimal],
) -> str:
    if curr is None or prior is None:
        return "neutral"
    diff = curr - prior
    if diff == 0:
        return "neutral"
    higher = diff > 0
    good = (sense == "higher_better" and higher) or (sense == "lower_better" and not higher)
    return "pos" if good else "neg"


def apply_computed_quarterly_changes(payload: dict[str, Any]) -> None:
    """
    Fill qoq / yoy / tones from v_curr, v_prev_q, v_prev_y using Decimal arithmetic.
    Expects each row to have value_kind and delta_sense (defaults applied if missing).
    """
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("value_kind") or "amount").lower()
        if kind not in ("amount", "percent", "per_share"):
            kind = "amount"
        sense = str(row.get("delta_sense") or "higher_better").lower()
        if sense not in ("higher_better", "lower_better"):
            sense = "higher_better"

        vc = _parse_financial_scalar(row.get("v_curr"))
        vq = _parse_financial_scalar(row.get("v_prev_q"))
        vy = _parse_financial_scalar(row.get("v_prev_y"))

        if kind == "percent":
            row["qoq"] = _fmt_pp_or_bps_delta(vc, vq)
            row["yoy"] = _fmt_pp_or_bps_delta(vc, vy)
            row["qoq_tone"] = _tone_from_pp_delta(sense, vc, vq)
            row["yoy_tone"] = _tone_from_pp_delta(sense, vc, vy)
            if row["qoq"] == "—":
                row["qoq_tone"] = "neutral"
            if row["yoy"] == "—":
                row["yoy_tone"] = "neutral"
            continue

        # amount or per_share: % growth vs prior period
        qchg = _pct_change_vs_prior(vc, vq)
        ychg = _pct_change_vs_prior(vc, vy)
        row["qoq"] = _fmt_signed_pct(qchg) if qchg is not None else "—"
        row["yoy"] = _fmt_signed_pct(ychg) if ychg is not None else "—"
        row["qoq_tone"] = (
            _tone_from_pct_change(sense, vc, vq) if qchg is not None else "neutral"
        )
        row["yoy_tone"] = (
            _tone_from_pct_change(sense, vc, vy) if ychg is not None else "neutral"
        )
        if row["qoq"] == "—":
            row["qoq_tone"] = "neutral"
        if row["yoy"] == "—":
            row["yoy_tone"] = "neutral"


def _compact_for_match(s: str) -> str:
    t = (s or "").lower().replace(",", "").replace("₹", "").replace("\u00a0", " ")
    return re.sub(r"\s+", "", t)


def _value_match_variants(cell: str) -> list[str]:
    """Strings to look for inside compacted PDF text (fuzzy but cheap)."""
    raw = str(cell).strip()
    if not raw or raw in ("—", "–", "-", "n/a", "N/A"):
        return []
    out: list[str] = []
    for v in (raw, raw.replace(" ", "")):
        out.append(_compact_for_match(v))
    t = raw.replace(",", "")
    if t.endswith("%"):
        base = t[:-1].strip()
        out.append(_compact_for_match(base + "%"))
        out.append(_compact_for_match(base))
        try:
            d = Decimal(base)
            out.append(_compact_for_match(f"{d:.2f}%"))
            out.append(_compact_for_match(f"{d:.1f}%"))
        except InvalidOperation:
            pass
    else:
        try:
            d = Decimal(re.sub(r"[^\d.\-]", "", t))
            s0 = format(d, "f").rstrip("0").rstrip(".")
            out.append(_compact_for_match(s0))
            out.append(_compact_for_match(f"{d:.2f}"))
        except (InvalidOperation, ValueError):
            out.append(_compact_for_match(t))
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if len(x) >= 1 and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _cell_found_in_pdf(cell: str, hay_compact: str) -> bool:
    vars_ = _value_match_variants(cell)
    if not vars_:
        return True
    for v in vars_:
        if len(v) < 2 and v.isdigit():
            continue
        if v in hay_compact:
            return True
    return False


def validate_payload_numbers_against_pdf(
    payload: dict[str, Any],
    pdf_text: str,
) -> tuple[bool, list[str], float]:
    """
    Check that LLM numeric cells appear in the merged PDF extraction (spot-check).
    Returns (ok, human-readable issues, matched_ratio among non-missing cells).
    """
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return False, ["no rows in payload"], 0.0
    hay = (pdf_text or "") + "\n"
    hay_compact = _compact_for_match(hay)
    issues: list[str] = []
    total = 0
    matched = 0
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "?")
        for key in ("v_curr", "v_prev_q", "v_prev_y"):
            cell = str(row.get(key) or "").strip()
            if not cell or cell in ("—", "–", "-", "n/a", "N/A"):
                continue
            total += 1
            if _cell_found_in_pdf(cell, hay_compact):
                matched += 1
            else:
                issues.append(f"{metric} {key}={cell!r}")

    ratio = (matched / total) if total else 1.0
    ok = ratio >= QUARTERLY_PDF_MATCH_RATIO and total > 0
    if total == 0:
        ok = False
        issues.append("no numeric cells to validate")
    return ok, issues, ratio


def _validation_retry_instruction(issues: list[str]) -> str:
    joined = "; ".join(issues[:12])
    if len(issues) > 12:
        joined += f" … (+{len(issues) - 12} more)"
    return (
        "\n\n[VALIDATION — REQUIRED]\n"
        "The previous JSON listed figures that do not appear in the PDF text excerpt. "
        "Reply again with ONE JSON object. Fix ONLY the numeric fields v_curr, v_prev_q, v_prev_y "
        "so each value appears in the filing text (use the same basis: consolidated if available). "
        f"Problem cells: {joined}\n"
    )


def _parse_json_flexible(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty model output")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text).strip()
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object in model output")


def _sleep_after_rate_limit(exc: Exception) -> None:
    msg = str(exc)
    m = re.search(r"retry in ([\d.]+)\s*s", msg, re.IGNORECASE)
    if m:
        time.sleep(min(125.0, float(m.group(1)) + 1.5))
    else:
        time.sleep(25)


def _is_model_not_found(exc: Exception) -> bool:
    s = str(exc).lower()
    return "404" in s and "not found" in s


def _is_quota_exhausted(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "quota" in s or "resource exhausted" in s


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

    base_prompt = template.format(
        symbol=symbol,
        company_name=company_name or symbol,
        pdf_text=pdf_text[:120000],
        page_snippet=page_snippet[:8000],
    )

    genai.configure(api_key=GEMINI_API_KEY)

    for model_name, use_json_mime in _QUARTERLY_MODEL_TRIALS:
        log.info("Quarterly JSON: trying model %s (json_mime=%s)", model_name, use_json_mime)

        gen_cfg: dict[str, Any] = {"temperature": 0.15}
        if use_json_mime:
            gen_cfg["response_mime_type"] = "application/json"

        try:
            model = genai.GenerativeModel(model_name, generation_config=gen_cfg)
        except Exception as exc:
            log.warning("Quarterly model %s init failed: %s", model_name, exc)
            continue

        model_gave_404 = False
        quota_skip_model = False
        validation_extra = ""
        last_payload: Optional[dict[str, Any]] = None

        for val_pass in range(MAX_QUARTERLY_VALIDATION_PASSES):
            json_suffix = _JSON_ONLY_SUFFIX if not use_json_mime else ""
            prompt = base_prompt + validation_extra + json_suffix

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if model_name in NVIDIA_MODELS:
                        if not NVIDIA_API_KEY:
                            log.warning("NVIDIA_API_KEY not set - skipping model %s", model_name)
                            quota_skip_model = True
                            break
                        
                        try:
                            from openai import OpenAI
                        except ImportError:
                            log.error("openai package not installed.")
                            quota_skip_model = True
                            break
                            
                        client = OpenAI(
                            base_url="https://integrate.api.nvidia.com/v1",
                            api_key=NVIDIA_API_KEY
                        )
                        completion = client.chat.completions.create(
                            model=model_name,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.15,
                            max_tokens=8192,
                            extra_body={"chat_template_kwargs": {"enable_thinking": True, "clear_thinking": False}},
                            stream=True
                        )
                        raw = ""
                        for chunk in completion:
                            if not getattr(chunk, "choices", None):
                                continue
                            if len(chunk.choices) == 0 or getattr(chunk.choices[0], "delta", None) is None:
                                continue
                            delta = chunk.choices[0].delta
                            if getattr(delta, "content", None) is not None:
                                raw += delta.content
                                
                    else:
                        response = model.generate_content(prompt)
                        raw = _gemini_response_text(response)

                    if not raw:
                        if model_name not in NVIDIA_MODELS:
                            finish_reason = "UNKNOWN"
                            if hasattr(response, "candidates") and response.candidates:
                                finish_reason = response.candidates[0].finish_reason.name
    
                            log.warning(
                                "Quarterly model %s attempt %d/%d: empty output (reason: %s). "
                                "Note: Gemma requires json_mime=false.",
                                model_name,
                                attempt,
                                MAX_RETRIES,
                                finish_reason,
                            )
                            _log_prompt_feedback(response)
                        else:
                            log.warning("Quarterly model %s attempt %d/%d: empty output.", model_name, attempt, MAX_RETRIES)
                            
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY * attempt)
                        continue
                    payload = _parse_json_flexible(raw)
                    apply_computed_quarterly_changes(payload)
                    rows = payload.get("rows")
                    if not isinstance(rows, list) or len(rows) < 3:
                        log.warning("Gemini returned unusable rows; retrying")
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY * attempt)
                        continue

                    ok, issues, ratio = validate_payload_numbers_against_pdf(payload, pdf_text)
                    payload["_pdf_validation_ok"] = ok
                    payload["_pdf_validation_ratio"] = round(ratio, 4)
                    payload["_pdf_validation_issues"] = issues
                    last_payload = payload

                    if ok:
                        payload["_model"] = model_name
                        log.info(
                            "Quarterly PDF spot-check OK (%.0f%% of cells matched)",
                            ratio * 100,
                        )
                        return payload

                    log.warning(
                        "Quarterly PDF spot-check %.0f%% (need %.0f%%) pass %d/%d: %s",
                        ratio * 100,
                        QUARTERLY_PDF_MATCH_RATIO * 100,
                        val_pass + 1,
                        MAX_QUARTERLY_VALIDATION_PASSES,
                        issues[:6],
                    )
                    if val_pass + 1 < MAX_QUARTERLY_VALIDATION_PASSES:
                        validation_extra = _validation_retry_instruction(issues)
                    break

                except Exception as exc:
                    if _is_model_not_found(exc):
                        log.warning("Quarterly model %s not available (404), skipping", model_name)
                        model_gave_404 = True
                        break
                    if _is_quota_exhausted(exc):
                        log.warning(
                            "Quarterly model %s quota / rate limit, trying next model",
                            model_name,
                        )
                        _sleep_after_rate_limit(exc)
                        quota_skip_model = True
                        break
                    log.warning(
                        "Quarterly JSON model %s attempt %d/%d: %s",
                        model_name,
                        attempt,
                        MAX_RETRIES,
                        exc,
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * attempt)
            if model_gave_404 or quota_skip_model:
                break

        if last_payload is not None:
            last_payload["_model"] = model_name
            log.error(
                "Quarterly PDF validation still failing after %s passes — using last JSON anyway",
                MAX_QUARTERLY_VALIDATION_PASSES,
            )
            return last_payload
        if model_gave_404 or quota_skip_model:
            continue

    log.error("Quarterly JSON extraction failed for all models")
    return None


# ---------------------------------------------------------------------------
# Quarterly snapshot PNG (generic template, no third-party branding)
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    regular = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]
    bold_faces = [
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    candidates = bold_faces if bold else regular
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _tone_color(tone: str) -> tuple[int, int, int]:
    """QoQ / YoY colours (mint rose / teal — distinct from the old cyan / red pairing)."""
    t = (tone or "neutral").lower()
    if t == "neg":
        return (251, 113, 133)
    if t == "pos":
        return (94, 234, 212)
    return (148, 163, 184)


def render_quarterly_card(
    symbol: str,
    payload: dict[str, Any],
    out_path: Path,
) -> bool:
    W = 920
    # Tall scratch canvas; cropped to content after drawing (no large empty band).
    canvas_h = 1800
    # Cool slate / petrol palette (not the previous navy + lime stack)
    BG = (15, 24, 32)
    TABLE_HEAD = (28, 42, 58)
    ROW_A = (22, 34, 48)
    ROW_B = (18, 30, 44)
    FOOTER_BAR = (20, 32, 46)
    ACCENT = (56, 189, 172)
    RATING = (244, 180, 76)
    WHITE = (238, 242, 255)
    GREY = (130, 146, 168)
    RULE = (48, 62, 78)

    img = Image.new("RGB", (W, canvas_h), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 4], fill=ACCENT)

    pad = 32
    y = 28

    f_tag = _load_font(14)
    f_q = _load_font(22)
    f_sym = _load_font(36, bold=True)
    f_sub = _load_font(17)
    f_rate = _load_font(20)
    f_small = _load_font(13)
    f_th = _load_font(14, bold=True)
    f_td = _load_font(14)
    f_foot = _load_font(26, bold=True)
    f_dis = _load_font(11)

    quarter_label = str(payload.get("quarter_label") or "—")
    display_sym = str(payload.get("display_symbol") or symbol).upper()
    company_line = str(payload.get("company_name") or payload.get("company_line") or "")
    rating = str(payload.get("rating") or "—")

    tag = "Quarterly snapshot"
    draw.text((pad, y + 2), tag, fill=GREY, font=f_tag)
    tw = draw.textlength(quarter_label, font=f_q)
    draw.text((W - pad - tw, y), quarter_label, fill=ACCENT, font=f_q)
    y += 34

    draw.text((pad, y), display_sym, fill=WHITE, font=f_sym)
    y_sym = y
    rate_text = f"Rating: {rating}"
    draw.text((W - pad - draw.textlength(rate_text, font=f_rate), y_sym + 8), rate_text, fill=RATING, font=f_rate)
    y += 46
    y_block_top = y
    y_line = y_block_top
    if company_line:
        draw.text((pad, y_line), company_line, fill=WHITE, font=f_sub)
        y_line += 24
    basis = str(payload.get("financial_basis") or "").strip().lower()
    basis_note = ""
    if basis == "consolidated":
        basis_note = "Consolidated"
    elif basis == "standalone":
        basis_note = "Standalone"
    elif basis == "mixed":
        basis_note = "Mixed basis"
    if basis_note:
        draw.text((pad, y_line), basis_note, fill=GREY, font=f_small)
        y_line += 16
    raw_unit = str(payload.get("unit") or "₹ Crores").strip()
    # Clean up common variations from LLM
    if "lakhs" in raw_unit.lower():
        unit_note = "₹ Lakhs | EPS in ₹"
    elif "millions" in raw_unit.lower():
        unit_note = "₹ Millions | EPS in ₹"
    else:
        unit_note = "₹ Cr | EPS in ₹"
        
    draw.text(
        (W - pad - draw.textlength(unit_note, font=f_small), y_block_top + 2),
        unit_note,
        fill=GREY,
        font=f_small,
    )
    y = max(y_line + 10, y_block_top + 38)

    # Table
    col_curr = str(payload.get("col_current") or "Curr")
    col_pq = str(payload.get("col_prev_q") or "Prev Q")
    col_py = str(payload.get("col_prev_y") or "YoY Q")

    headers = ["Metric", "QoQ", "YoY", col_curr, col_pq, col_py]
    col_x = [pad, 200, 290, 380, 520, 660]
    row_h = 36
    head_h = 32

    draw.rectangle([pad - 8, y, W - pad + 8, y + head_h], fill=TABLE_HEAD)
    draw.line([pad - 8, y + head_h, W - pad + 8, y + head_h], fill=RULE, width=1)
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
        draw.line([pad - 8, y, W - pad + 8, y], fill=RULE, width=1)

    y += 20

    # Footer metrics
    fh = 72
    draw.rectangle([0, y, W, y + fh], fill=FOOTER_BAR)
    cmp_s = str(payload.get("footer_cmp") or "—")
    eps_s = str(payload.get("footer_fwd_eps") or "—")
    pe_s = str(payload.get("footer_fwd_pe") or "—")

    block_w = W // 3
    for i, (label, value, use_accent) in enumerate(
        [
            ("CMP", cmp_s, True),
            ("FWD EPS", eps_s, False),
            ("Forward PE", pe_s, True),
        ]
    ):
        bx = i * block_w
        color = ACCENT if use_accent else WHITE
        draw.text((bx + 24, y + 10), label, fill=GREY, font=f_small)
        draw.text((bx + 24, y + 28), value, fill=color, font=f_foot)
        if i < 2:
            draw.line([(bx + block_w, y + 12), (bx + block_w, y + fh - 12)], fill=RULE, width=1)
    y += fh + 12

    model_line = str(payload.get("_model") or "").strip() or "unknown"
    draw.text((pad, y), f"Model: {model_line}", fill=GREY, font=f_dis)
    y += 18
    WARN = (244, 180, 120)
    notes: list[str] = []
    if payload.get("_pdf_validation_ok") is False:
        r = payload.get("_pdf_validation_ratio")
        try:
            pct = int(float(r) * 100) if r is not None else 0
        except (TypeError, ValueError):
            pct = 0
        notes.append(f"Spot-check: only ~{pct}% of figures matched PDF text")
    if payload.get("_pdf_warn_scan_only"):
        notes.append("Low extracted text — possible scan PDF (enable ENABLE_PDF_OCR if needed)")
    if notes:
        draw.text((pad, y), " · ".join(notes), fill=WARN, font=f_dis)
        y += 16

    ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d-%b-%Y %H:%M IST")
    disclaimer = "Snapshot from publicly available filings. Verify with official filing."
    source = "Source: NSE/BSE filing"
    draw.text((pad, y), disclaimer, fill=GREY, font=f_dis)
    draw.text((pad, y + 14), source, fill=GREY, font=f_dis)
    rw = draw.textlength(ist, font=f_dis)
    draw.text((W - pad - rw, y + 7), ist, fill=GREY, font=f_dis)
    y += 34
    bottom_pad = 20
    crop_h = min(canvas_h, max(y + bottom_pad, 120))

    try:
        img = img.crop((0, 0, W, crop_h))
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
    vok = payload.get("_pdf_validation_ok")
    vr = payload.get("_pdf_validation_ratio")
    issues = payload.get("_pdf_validation_issues") or []
    iss_txt = ""
    if isinstance(issues, list) and issues:
        iss_txt = "\n**Spot-check gaps:** " + "; ".join(str(x) for x in issues[:20])
        if len(issues) > 20:
            iss_txt += f" … (+{len(issues) - 20} more)"
    body = (
        f"# {symbol} Quarterly snapshot ({period_key})\n\n"
        f"**PDF (via Screener):** {pdf_url}\n\n"
        f"**Model:** {payload.get('_model', 'unknown')}\n\n"
        f"**PDF spot-check:** "
        f"{'OK' if vok else 'FAILED or partial'} "
        f"(matched ratio {vr!s}){iss_txt}\n\n"
        f"**PDF pages (approx):** {payload.get('_pdf_pages', '—')} · "
        f"**OCR used:** {payload.get('_pdf_ocr_used', False)} · "
        f"**Low-text warning:** {payload.get('_pdf_warn_scan_only', False)}\n\n"
        f"---\n\n{summary}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Telegram photo
# ---------------------------------------------------------------------------

def _escape_tg_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_quarterly_telegram_caption(
    symbol: str,
    period_key: str,
    summary: str,
    pdf_url: str,
    model_id: str,
    validation_ok: Optional[bool] = None,
    validation_ratio: Optional[float] = None,
) -> str:
    """
    Build HTML caption under Telegram's 1024-character limit (UTF-16 length on API may vary; stay conservative).
    """
    m = (model_id or "").strip()
    model_line = f"\n🤖 <b>Model</b>: <code>{_escape_tg_html(m)}</code>" if m else ""
    warn_line = ""
    if validation_ok is False:
        pct = int((validation_ratio or 0) * 100)
        warn_line = f"\n⚠️ <b>PDF spot-check</b>: {pct}% cells matched — verify figures."
    head = (
        f"📊 <b>NEW QUARTERLY RESULTS</b>\n"
        f"🏢 <code>{_escape_tg_html(symbol)}</code> · <b>{_escape_tg_html(period_key)}</b>"
        f"{model_line}{warn_line}\n\n"
    )
    foot = f"\n\n🔗 <a href=\"{pdf_url}\">Raw PDF (Screener)</a>"
    room = max(80, TELEGRAM_CAPTION_MAX - len(head) - len(foot) - 8)
    body = _escape_tg_html((summary or "").strip())
    if len(body) > room:
        body = body[: max(0, room - 1)] + "…"
    return head + body + foot


def send_telegram_quarterly_photo(
    symbol: str,
    period_key: str,
    image_path: Path,
    caption_html: str,
) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured – skip quarterly photo")
        return False

    alert_key = ("qr", symbol.upper().strip(), period_key)
    if alert_key in ALERTS_SENT_THIS_RUN:
        log.warning("Duplicate quarterly Telegram for %s %s", symbol, period_key)
        return True

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    text = (caption_html or "").strip()
    if len(text) > TELEGRAM_CAPTION_MAX:
        text = text[: TELEGRAM_CAPTION_MAX - 1] + "…"
        log.warning("Telegram caption truncated to %d chars", TELEGRAM_CAPTION_MAX)
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

def process_quarterly_stock(
    symbol: str,
    state: dict[str, str],
    soup: BeautifulSoup,
    soup_consolidated: Optional[BeautifulSoup] = None,
) -> bool:
    """Returns True if a new quarter was processed (and state updated)."""
    sym = symbol.upper().strip()
    log.info("-" * 40)
    log.info("Quarterly check: %s", sym)

    latest = find_latest_quarterly_pdf(soup, soup_consolidated)
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

    bundle = extract_pdf_text_enriched(pdf_url)
    if not bundle or not str(bundle.get("text") or "").strip():
        log.error("Could not read quarterly PDF for %s", sym)
        return False
    pdf_text = str(bundle["text"])
    company_name = scrape_company_heading(soup)
    snippet = page_text_snippet(soup)
    payload = build_quarterly_payload(sym, company_name, pdf_text, snippet)
    if not payload:
        log.error("Quarterly AI payload failed for %s", sym)
        return False

    payload["_pdf_pages"] = bundle.get("pages")
    payload["_pdf_warn_scan_only"] = bool(bundle.get("warn_scan_only"))
    payload["_pdf_ocr_used"] = bool(bundle.get("ocr_used"))

    out_png = QUARTERLY_CARDS_DIR / f"{sym}_Q_{period_key.replace('-', '_')}.png"
    if not render_quarterly_card(sym, payload, out_png):
        return False

    save_quarterly_summary_md(sym, period_key, pdf_url, payload)

    if not SILENT_MODE:
        cap = build_quarterly_telegram_caption(
            sym,
            period_key,
            str(payload.get("summary_short") or ""),
            pdf_url,
            str(payload.get("_model") or ""),
            validation_ok=payload.get("_pdf_validation_ok"),
            validation_ratio=float(payload.get("_pdf_validation_ratio") or 0),
        )
        ok = send_telegram_quarterly_photo(sym, period_key, out_png, cap)
        if not ok:
            log.error("Telegram failed for quarterly %s – state not updated", sym)
            return False
    else:
        log.info("Silent mode: skip Telegram for quarterly %s", sym)

    state[sym] = period_key
    log.info("Quarterly state updated %s → %s", sym, period_key)
    return True
