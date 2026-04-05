# 🧠 How the Concall Monitor Works (Layman's Guide)

This document explains exactly what happens step-by-step when your Stock Concall Monitor runs. Think of it as a tireless digital intern that never sleeps!

---

## 🕒 Step 1: The Alarm Clock (GitHub Actions)
Every day at **9:00 AM IST**, GitHub wakes up a small computer in the cloud to run your code. 
*   This is "Serverless" — you don't need to keep your laptop on.
*   GitHub handles the scheduling for free.

## 📄 Step 2: Reading the Watchlist
The first thing the computer does is open your `stocks.txt` file.
*   It reads every stock symbol you've added (e.g., `AIAENG`, `TCS`, `VBL`).
*   It prepares a loop to check each of these companies one by one.

## 🔍 Step 3: Visiting Screener.in
For every stock on your list, the bot "visits" the company page on Screener.in (just like you would in a browser).
*   It looks specifically for the **"Documents"** or **"Concalls"** section.
*   It searches for the word **"Transcript"**.
*   It grabs the date of the latest transcript it finds (e.g., "Feb 2026").

## 🛑 Step 4: Is it actually NEW?
The bot opens a special file called `last_concall.json`. This is its "memory."
*   It compares the date it just found on Screener with the date it saw last time.
*   **If they match:** The bot says "Nothing new here" and moves to the next stock.
*   **If it's a newer date:** The bot gets excited! It proceeds to the next steps.

## 📥 Step 5: Downloading & Reading the PDF
The bot clicks the transcript link, which usually leads to a **BSE India PDF file**.
*   The PDF is downloaded into the computer's temporary memory (RAM).
*   The bot uses a specialized "PDF Reader" tool to extract all the text from the 20-30 pages of the transcript.
*   *Note: The PDF itself is deleted immediately after the text is extracted to save space.*

## 🤖 Step 6: The AI Brain (Gemini)
Now, the bot sends that massive wall of text to Google's **Gemini AI**.
*   **The Request:** "You are a professional analyst. Summarize this 20-page document into key highlights, risks, and management guidance."
*   **Model Fallback:** The bot is smart. It tries the smartest model first (`Gemini 3.1 Pro`). If that is busy, it automatically tries `Gemini 2.5 Pro`, then the `Flash` versions, ensuring you always get a summary.

## 📁 Step 7: Saving the Summary
Once Gemini sends back the professional summary, the bot does two things:
1.  **Saves a File:** It creates a beautiful Markdown (`.md`) file in the `summaries/` folder (e.g., `TCS_2026_01_01.md`).
2.  **Updates Memory:** It records the new date in `last_concall.json` so it won't alert you for the same transcript tomorrow.

## 📱 Step 8: The Telegram Alert
This is the final and most important step. The bot sends a message to your **Telegram Bot**.
*   It includes the **Stock Name**, the **Date**, and the **AI Summary**.
*   It also adds a direct link back to the Screener page so you can verify.

## 💾 Step 9: Committing Changes
Before the cloud computer turns itself off, it "pushes" the new summary files and the updated memory file (`last_concall.json`) back to your GitHub repository.
*   This ensures your repository is always up-to-date for the next day's run.

---

### Summary of Tech Used:
- **Python**: The language the bot speaks.
- **GitHub Actions**: The cloud scheduler.
- **BeautifulSoup**: The tool used to "read" the Screener website.
- **PyPDF2**: The tool used to "read" the PDF files.
- **Gemini 1.5/2.0/3.1**: The AI brain provided by Google.
- **Telegram API**: The delivery service for your alerts.
