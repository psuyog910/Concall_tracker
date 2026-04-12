# 📊 Stock Concall & Results Tracker

An automated monitoring agent that tracks earnings call transcripts and quarterly result filings for your watchlist stocks. It extracts key insights using **Gemini AI** and delivers sleek notifications directly to **Telegram**.

## 🚀 Features

- **Concall Monitoring**: Automatically detects new transcripts on Screener.in.
- **AI-Powered Summaries**: Generates concise, professional summaries from long PDF transcripts using Google's Gemini models.
- **Quarterly Results**: Extracts key financial metrics (Sales, Profit, OPM, EPS) from quarterly filings and compares them QoQ and YoY.
- **Sleek Notifications**: 
  - Concall summaries delivered as beautifully rendered, dark-themed PDFs.
  - Quarterly results presented as high-quality snapshot images for quick review.
- **Automated Workflow**: Runs daily via GitHub Actions.
- **Smart Retries & Fallbacks**: Uses multiple Gemini models and OCR fallbacks to ensure reliability.

## 🛠️ Setup

### Prerequisites

- Python 3.10+
- Google Gemini API Key
- Telegram Bot Token and Chat ID

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/psuyog910/Concall_tracker.git
   cd Concall_tracker
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install --with-deps chromium
   ```

3. Configure your watchlist:
   Edit `stocks.txt` and add the NSE/BSE symbols (one per line).

### Local Usage

Set the required environment variables:
```bash
set GEMINI_API_KEY=your_key
set TELEGRAM_TOKEN=your_bot_token
set TELEGRAM_CHAT_ID=your_chat_id
```

Run the tracker:
```bash
python scraper.py
```

## 🤖 GitHub Actions Configuration

This project is designed to run automatically. To set it up:

1. Go to your GitHub Repository **Settings** > **Secrets and variables** > **Actions**.
2. Add the following **Repository secrets**:
   - `GEMINI_API_KEY`: Your Google AI Studio API key.
   - `TELEGRAM_TOKEN`: Your Telegram bot token.
   - `TELEGRAM_CHAT_ID`: Your personal or group chat ID.
3. The workflow is configured in `.github/workflows/concall-monitor.yml` and runs daily at 9:00 AM IST.

## 📄 License

MIT License - feel free to use and modify!
