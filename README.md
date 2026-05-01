# 📈 Screener.in AI Watchlist Alerts

[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-blue?logo=github-actions)](https://github.com/psuyog910/Concall_tracker/actions)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An automated AI agent that tracks earnings call transcripts and quarterly financial results for Indian stocks on [Screener.in](https://www.screener.in/). It uses **Google Gemini AI** to extract key insights, crunch the numbers, and deliver beautiful, highly-readable summaries directly to your **Telegram** app.

---

## ✨ Features

- **🎯 Earnings Concall Monitoring**: Automatically detects new concall transcripts for your watchlist.
- **📊 Quarterly Results Extraction**: Extracts raw P&L figures (Revenue, PAT, Margins, EPS) directly from exchange PDF filings and automatically calculates QoQ / YoY growth.
- **🧠 AI-Powered Insights**: Uses Gemini's advanced reasoning (Chain of Thought) to prevent hallucinations, summarize complex management commentary, and reliably extract structured financial data from dense tables.
- **📱 Sleek Telegram Notifications**: 
  - Concall summaries are delivered as beautifully rendered, dark-themed PDF documents.
  - Quarterly results are delivered as clean, high-quality PNG snapshots (Crores/Lakhs accurately formatted).
- **🛡️ Robust Scraper Engine**: Built-in bot-detection bypass, trailing-slash handling, and automated retries to fetch PDFs from BSE/NSE reliably.
- **☁️ 100% Free Automation**: Designed to run entirely on GitHub Actions' free tier with zero server costs.

## 🛠️ Architecture & Tech Stack

- **Core**: Python 3.10+
- **AI Models**: `google-genai` / `google.generativeai` (Gemini models)
- **PDF Extraction**: `pdfplumber`, `PyMuPDF` (fitz), with Tesseract OCR fallback
- **Visuals**: `playwright` (HTML to PDF rendering), `Pillow` (Image creation)
- **Web Scraping**: `requests`, `BeautifulSoup4`
- **CI/CD**: GitHub Actions

---

## 🚀 Setup & Installation

### 1. Prerequisites
- Google Gemini API Key (Get it from Google AI Studio)
- Telegram Bot Token (from `@BotFather`)
- Telegram Chat ID (where you want to receive alerts)

### 2. Local Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/psuyog910/Concall_tracker.git
cd Concall_tracker

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser binaries (required for rendering PDF summaries)
playwright install --with-deps chromium
```

### 3. Configure Watchlist
Edit the `stocks.txt` file in the root directory. Add the exact NSE/BSE symbols from Screener.in, one per line:
```text
RELIANCE
TCS
VBL
```

### 4. Running Locally
Set your environment variables and run the specific monitor:

```bash
# Windows
set GEMINI_API_KEY=your_key
set TELEGRAM_TOKEN=your_bot_token
set TELEGRAM_CHAT_ID=your_chat_id

# 1. Run Concall Summarizer
python scraper.py

# 2. Run Quarterly Results Extractor
python quarterly_monitor.py
```

---

## 🤖 GitHub Actions Deployment (Recommended)

This project is built to run hands-free.

1. Go to your GitHub Repository **Settings** > **Secrets and variables** > **Actions**.
2. Add the following **Repository secrets**:
   - `GEMINI_API_KEY`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. The workflow is located in `.github/workflows/concall-monitor.yml`. It runs automatically every weekday at 9:00 AM IST.
4. *Note: Since this is a lightweight script (under 15 minutes per run), it easily fits within the generous free-tier limits of GitHub Actions!*

---

## 📜 License
This project is licensed under the MIT License.
